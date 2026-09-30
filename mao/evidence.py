"""EvidenceCollector —— 由框架采集证据，而不是听 Agent 自述。

核心区分（第二阶段最重要的观念之一）
------------------------------------
    Executor 说："build passed"
    Agent 自述  -> 不可信，因为它可能改代码的同时也改了验收脚本

    Framework Evidence: build exit_code = 1
    框架采集    -> 可信，因为命令是框架跑的，退出码是操作系统给的

Reviewer 的输入必须是后者。否则多 Agent 循环就退化成了"三个人互相点头"。

采集内容（与需求 §15 对齐）
---------------------------
    git_status      工作区是否有未提交改动
    git_diff        完整差异文本（截断保护）
    changed_files   变更文件清单（相对于某个基线）
    build_result    由 VerificationRunner 填写
    test_result     由 VerificationRunner 填写
    lint_result     由 VerificationRunner 填写
    artifact_list   工作区里生成的产物文件

所有 git 命令一律 `shell=False`、argv 列表传递。实际执行委托给
`mao.transports.process.run_once` —— 全仓只有 transports/ 能起进程，
这样"Agent 怎么被调用"始终只有一处需要审计。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .core.models import Artifact, Evidence, new_id
from .transports.process import run_once

# 非 git 目录遍历时忽略的目录（缓存/依赖，跟工作区内容无关）
_IGNORED_DIRS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".tox", ".idea", ".vscode",
}

# diff 全文可能极大，默认只留前若干行 + 尾部统计
DEFAULT_DIFF_LIMIT = 20000

# ---- 阶段七（§16 证据链）：变更文件源码快照的采集上限 ----
SNAPSHOT_PER_FILE_LIMIT = 4000     # 单文件文本上限（字符）
SNAPSHOT_TOTAL_LIMIT = 24000       # 全部快照总上限（字符）
SNAPSHOT_MAX_FILES = 8             # 最多快照的文件数
SNAPSHOT_MAX_BYTES = 200_000       # 超过即视为"非文本候选"，直接跳过
SNAPSHOT_TRUNCATED_MARK = "\n... [snapshot truncated at {limit} chars, total {total}]"


def collect_source_snapshots(
    workspace_path: Optional[Any],
    files: Sequence[str],
    *,
    per_file_limit: int = SNAPSHOT_PER_FILE_LIMIT,
    total_limit: int = SNAPSHOT_TOTAL_LIMIT,
    max_files: int = SNAPSHOT_MAX_FILES,
    max_bytes: int = SNAPSHOT_MAX_BYTES,
    fill_from_workspace: bool = True,
    scan_limit: int = 200,
) -> Dict[str, str]:
    """读取变更文件（及工作区源码）的当前内容，供 Reviewer 独立核对。

    为什么要有这个函数（§16，Phase 7 outcome demo 的真实教训 x2）：
        教训一：Executor 改完 conftest.py 后，框架验证只有聚合退出码，
        Reviewer 复核时看不到代码本体，只能 FAIL 掉"缺少源码证据"。
        教训二：只快照**变更**文件还不够 —— 验收标准常常引用**未变**的
        实现文件（"独立确认 multiply 的语义"），而它们不在 changed_files
        里。Reviewer 需要"被验收的源码"，不只是"被修改的源码"。

    行为约定：
        - 相对路径为键；changed_files 优先（占预算），随后按需从工作区
          补齐文本文件（有界扫描：scan_limit 个候选封顶）
        - 文本解码 utf-8（errors="replace"，兼容中文注释/乱码字节）
        - 含 NUL 字节视为二进制，跳过
        - 超过 max_bytes 的文件跳过（防内存，也为提示"这不是可快照文本"）
        - 单文件与总量双截断，截断处带显式标记（计入预算）
        - 任何 IO 失败只跳过该文件，绝不抛异常 —— 快照是增强证据，
          采集失败不能反过来弄死证据链
    """
    root = Path(workspace_path) if workspace_path else None
    if root is None:
        return {}

    candidates: List[str] = []
    seen: set[str] = set()
    for relative in list(files)[:max_files]:
        if relative not in seen:
            seen.add(relative)
            candidates.append(relative)

    if fill_from_workspace and len(candidates) < max_files:
        for path in _workspace_text_candidates(
                root, _IGNORED_DIRS, scan_limit=scan_limit):
            rel = path.relative_to(root).as_posix()
            if rel not in seen:
                seen.add(rel)
                candidates.append(rel)
            if len(candidates) >= max_files:
                break

    snapshots: Dict[str, str] = {}
    used = 0
    for relative in candidates:
        target = root / relative
        try:
            if not target.is_file():
                continue
            if target.stat().st_size > max_bytes:
                continue
            raw = target.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue  # 二进制文件不进 prompt

        text = raw.decode("utf-8", errors="replace")
        budget = min(per_file_limit, total_limit - used)
        if budget <= 0:
            break
        if len(text) > budget:
            # 截断标记计入本文件预算：先给 marker 预留足量空间，
            # 再按真实 keep/total 格式化（位数 <= 预估上界，总长必 <= budget）
            total_chars = len(text)
            marker_bound = len(
                SNAPSHOT_TRUNCATED_MARK.format(limit=total_chars,
                                               total=total_chars)) + 2
            keep = max(0, budget - marker_bound)
            if keep == 0:
                break  # 预算耗尽：不产出"只有标记没有内容"的空壳快照
            marker = SNAPSHOT_TRUNCATED_MARK.format(limit=keep,
                                                    total=total_chars)
            text = text[:keep] + marker
        snapshots[relative] = text
        used += len(text)

    return snapshots


def _workspace_text_candidates(
    root: Path,
    ignored_dirs: set,
    *,
    scan_limit: int,
) -> List[Path]:
    """有界遍历工作区，给出候选文本文件（变更文件之外补齐用）。

    刻意 rglob + 提前断流，而不是先 list 全仓再排序 —— 大仓上
    collect 必须是 O(scan_limit) 而不是 O(仓库文件数)。
    """
    out: List[Path] = []
    for path in root.rglob("*"):
        if len(out) >= scan_limit:
            break
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in ignored_dirs for part in rel_parts):
            continue
        if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES:
            out.append(path)
    return sorted(out, key=lambda p: p.as_posix())


# 补齐扫描只认文本类后缀（实现/测试/配置），避免在二进制资产上浪费名额
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".sh", ".ps1", ".bat",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".md", ".txt", ".rst", ".html", ".css", ".scss", ".sql",
}


def format_verification_outputs(results: Sequence[Any]) -> str:
    """把每条验收命令的真实输出排成 Reviewer 可引用的证据块。

    只给聚合退出码时，Reviewer 无法回答"逐项测试是不是真的过了"；
    这里把 VerificationRunner 捕获的 output_excerpt 原样交出去。
    """
    if not results:
        return "(no verification commands ran)"
    blocks: List[str] = []
    for item in results:
        mark = "PASS" if getattr(item, "passed", False) else "FAIL"
        exit_code = getattr(item, "exit_code", None)
        header = f"### {item.name} — exit={exit_code} [{mark}]"
        body = getattr(item, "output_excerpt", None) or "(no output captured)"
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


def format_source_snapshots(snapshots: Optional[Dict[str, str]]) -> str:
    """把源码快照渲染成带文件头的文本块，方便 Reviewer 逐行引用。"""
    if not snapshots:
        return "(no changed-file snapshots collected)"
    blocks: List[str] = []
    for relative, content in snapshots.items():
        blocks.append(f"### {relative}\n{content}")
    return "\n\n".join(blocks)


class EvidenceCollector:
    """从工作区采集客观证据。

    `git_available=None` 时自动探测；探测失败则安静降级为"无 git 证据"，
    而不是让整个任务失败 —— 很多任务本来就不在 git 仓库里跑。
    """

    def __init__(
        self,
        *,
        diff_limit: int = DEFAULT_DIFF_LIMIT,
        include_untracked: bool = True,
        git_available: Optional[bool] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.diff_limit = int(diff_limit)
        self.include_untracked = bool(include_untracked)
        self._git_available = git_available
        self.timeout_seconds = float(timeout_seconds)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def collect(
        self,
        workspace_path: Optional[Any],
        *,
        baseline_commit: Optional[str] = None,
        build_result: Optional[str] = None,
        test_result: Optional[str] = None,
        lint_result: Optional[str] = None,
        verification: Optional[Sequence[Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Evidence:
        """采集一次完整证据快照。"""
        path = Path(workspace_path) if workspace_path else None
        evidence = Evidence()

        if path is not None and path.exists():
            evidence.git_diff = self._git_diff(path, baseline_commit)
            evidence.git_diff_stat = self._git_diff_stat(path)
            evidence.changed_files = self._changed_files(path, baseline_commit)
            evidence.artifacts = self._artifacts(path, evidence.changed_files)

        evidence.build_result = build_result
        evidence.test_result = test_result
        evidence.lint_result = lint_result

        block: Dict[str, Any] = dict(extra or {})
        if verification:
            block["verification"] = [self._verification_entry(v) for v in verification]
        # git_status 也作为结构化字段保留，便于 Reviewer 直接读
        block["git_status"] = self._git_status(path) if path else None
        evidence.extra = block

        return evidence

    # ------------------------------------------------------------------
    def _verification_entry(self, item: Any) -> Dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return dict(item)

    # ------------------------------------------------------------------
    # 工作区指纹（§11：验证 Reviewer 真的没写工作区）
    # ------------------------------------------------------------------
    def fingerprint(self, path: Path) -> Optional[str]:
        """给工作区算一个稳定指纹，用于"调用前后是否被改动"的对比。

        优先用 git（`status --porcelain` + `diff`），因为它是工作区状态的
        权威视图，且天然忽略 .gitignore 的东西。
        非 git 目录退化为"遍历文件算 sha256"。

        拿不到指纹时返回 None —— 调用方**必须**把 None 当作"无法判定"，
        而不是"没变化"。
        """
        root = Path(path)
        if not root.exists():
            return None

        if self.git_available(root):
            status = self._run_git(root, ["status", "--porcelain"])
            diff = self._run_git(root, ["diff"])
            if status is None or diff is None:
                return None
            # 也带上未跟踪文件的内容摘要，避免"新增文件"被漏掉
            digest = hashlib.sha256()
            digest.update(status.encode("utf-8", "replace"))
            digest.update(b"\x00")
            digest.update(diff.encode("utf-8", "replace"))
            for rel in sorted(self._untracked_files(root)):
                digest.update(rel.encode("utf-8", "replace"))
                file_hash = self._sha256(root / rel)
                digest.update((file_hash or "").encode("utf-8"))
            return digest.hexdigest()

        return self._tree_hash(root)

    def _untracked_files(self, root: Path) -> list:
        raw = self._run_git(root, ["ls-files", "--others", "--exclude-standard"])
        if not raw:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _tree_hash(self, root: Path) -> Optional[str]:
        """非 git 目录：按相对路径 + 内容哈希遍历。"""
        digest = hashlib.sha256()
        try:
            entries = sorted(
                (p for p in root.rglob("*") if p.is_file()),
                key=lambda p: p.as_posix(),
            )
        except OSError:
            return None
        for entry in entries:
            if any(part in _IGNORED_DIRS for part in entry.relative_to(root).parts):
                continue
            digest.update(entry.relative_to(root).as_posix().encode("utf-8", "replace"))
            digest.update(b"\x00")
            digest.update((self._sha256(entry) or "").encode("utf-8"))
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # git 采集
    # ------------------------------------------------------------------
    def git_available(self, path: Path) -> bool:
        if self._git_available is not None:
            return self._git_available
        self._git_available = (path / ".git").exists() and self._which_git() is not None
        return self._git_available

    @staticmethod
    def _which_git() -> Optional[str]:
        from shutil import which

        return which("git")

    def _run_git(self, path: Path, args: Sequence[str]) -> Optional[str]:
        """跑一条只读 git 命令。失败返回 None（不进 git 仓库也能用）。"""
        if not self.git_available(path):
            return None
        git = self._which_git()
        if git is None:
            return None
        result = run_once(
            [git, *args],
            cwd=path,
            timeout=self.timeout_seconds,
        )
        if not result.ok:
            return None
        return result.stdout or ""

    def _git_status(self, path: Path) -> Optional[str]:
        raw = self._run_git(path, ["status", "--porcelain"])
        if raw is None:
            return None
        return raw.strip()

    def _git_diff(self, path: Path, baseline: Optional[str]) -> Optional[str]:
        args = ["diff", "--no-color"]
        if baseline:
            args.append(baseline)
        raw = self._run_git(path, args)
        if not raw:
            # 没有已跟踪文件的改动时，看看有没有未跟踪内容
            status = self._git_status(path)
            return status or raw
        if len(raw) > self.diff_limit:
            head = raw[: self.diff_limit]
            return f"{head}\n\n[... diff truncated at {self.diff_limit} chars, total {len(raw)} ...]"
        return raw

    def _git_diff_stat(self, path: Path, baseline: Optional[str] = None
                       ) -> Optional[Dict[str, int]]:
        args = ["diff", "--numstat"]
        if baseline:
            args.append(baseline)
        raw = self._run_git(path, args)
        if not raw:
            return None
        stat: Dict[str, int] = {"files": 0, "added": 0, "deleted": 0}
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, deleted, _name = parts[0], parts[1], parts[2]
            stat["files"] += 1
            for key, value in (("added", added), ("deleted", deleted)):
                try:
                    stat[key] += int(value)
                except ValueError:
                    pass  # 二进制文件的 numstat 是 "-"
        return stat if stat["files"] else None

    def _changed_files(self, path: Path, baseline: Optional[str]) -> List[str]:
        """变更文件清单：优先用 git，退化为 mtime 扫描。"""
        files: List[str] = []

        if self.git_available(path):
            args = ["diff", "--name-only"]
            if baseline:
                args.append(baseline)
            raw = self._run_git(path, args) or ""
            files.extend(line.strip() for line in raw.splitlines() if line.strip())

            if self.include_untracked:
                untracked = self._run_git(
                    path, ["ls-files", "--others", "--exclude-standard"]
                ) or ""
                files.extend(
                    line.strip() for line in untracked.splitlines() if line.strip()
                )

        # 去重保序
        seen: set[str] = set()
        unique: List[str] = []
        for item in files:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique[:500]

    def _artifacts(self, path: Path, changed: Sequence[str]) -> List[Artifact]:
        """把工作区里存在的变更文件登记为 artifact（只记路径与大小）。"""
        out: List[Artifact] = []
        for relative in list(changed)[:100]:
            target = path / relative
            if not target.is_file():
                continue
            try:
                size = target.stat().st_size
            except OSError:
                continue
            out.append(
                Artifact(
                    artifact_id=new_id("art"),
                    kind=self._classify(relative),
                    path=str(target),
                    description=relative,
                    size_bytes=size,
                    sha256=self._sha256(target) if size and size <= 2_000_000 else None,
                )
            )
        return out

    @staticmethod
    def _classify(relative: str) -> str:
        lowered = relative.lower()
        if lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return "screenshot"
        if lowered.endswith((".diff", ".patch")):
            return "diff"
        if lowered.endswith((".log", ".txt")):
            return "log"
        if lowered.endswith((".md", ".rst")):
            return "report"
        return "other"

    @staticmethod
    def _sha256(path: Path) -> Optional[str]:
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None


__all__ = [
    "EvidenceCollector",
    "DEFAULT_DIFF_LIMIT",
    "collect_source_snapshots",
    "format_verification_outputs",
    "format_source_snapshots",
]
