"""WorkspaceStrategyManager —— 隔离执行工作区的准备/取证/清理（§9-§22）。

Phase 9 核心（§21/§59/§60）：GIT_WORKTREE 下同一 source repo 的多个
Task 可并发 —— 冲突检查看 execution workspace 唯一性，不看 source。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .runner import DefaultProcessRunner, ProcessRunner
from .strategies import COPY_DEFAULT_EXCLUDES, WorkspaceStrategy

Emit = Callable[..., None]


#: worktree 里的框架自带记账文件（§14 溯源 sidecar）。
#: 它不是业务改动 —— 取证与指纹都必须按同一个判断处理它。
WORKTREE_META_NAME = ".mao-worktree-meta.json"


class WorkspacePreparationError(RuntimeError):
    """工作区准备失败（§81）。kind: PERMANENT（配置/仓库问题）| TRANSIENT。"""

    def __init__(self, message: str, kind: str = "PERMANENT") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class WorkspacePlan:
    """一次 RuntimeTask 的执行工作区决议（§10）。"""

    strategy: WorkspaceStrategy
    source_workspace_path: str
    execution_workspace_path: str
    base_revision: str = ""
    workspace_id: str = ""          # §22：DIRECT=source 身份；WORKTREE=rt 身份
    metadata_path: str = ""         # WorktreeMetadata sidecar（§14）


@dataclass
class WorktreeMetadata:
    """§14：worktree 溯源记录（sidecar JSON + artifacts 引用）。"""

    runtime_task_id: str
    source_repository: str
    execution_workspace: str
    base_commit: str
    created_at: str
    strategy: str
    status: str = "ACTIVE"          # ACTIVE / PRESERVED / CLEANED
    result_diff_path: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_path(cls, path: Path) -> "WorktreeMetadata":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def _git(runner: ProcessRunner, *args: str, cwd: str | None = None,
         timeout: float = 60.0):
    return runner.run(["git", *args], cwd=cwd, timeout=timeout)


def _is_deliverable(rel: str) -> bool:
    """字节码与缓存目录不算交付物，既不进改动清单也不进补丁。

    真实那一跑（2026-09-30）的现场：执行者跑测试时留下了
    `tests/__pycache__/*.pyc`，框架采集到的清单因此比自述多两项 ——
    闸门把"两份清单不一致"当拒绝理由，一份框架验收命令 exit 0 的合格交付被判 FAILED。
    补丁里混进 `Binary files differ` 也是同一件事：交接出去的东西不该是缓存。
    """
    parts = (rel or "").replace("\\", "/").split("/")
    return not (parts[-1].endswith((".pyc", ".pyo")) or "__pycache__" in parts)


def _untracked_files(status_lines: list[str], exec_path: Path) -> list[str]:
    """porcelain 里的未跟踪项 → 真实文件路径列表。

    `?? 新目录/` 是 git 折叠显示的**目录**，不是文件 —— 不展开就会把整个新目录
    从补丁里漏掉，而补丁恰恰是唯一能交接出去的东西。
    """
    out: list[str] = []
    for ln in status_lines:
        if not ln.startswith("?? "):
            continue
        rel = ln[3:].strip().strip('"')
        if not rel:
            continue
        target = exec_path / rel
        if target.is_dir():
            for f in sorted(target.rglob("*")):
                if f.is_file():
                    out.append(f.relative_to(exec_path).as_posix())
        elif target.is_file():
            out.append(rel.replace("\\", "/"))
    return out


class WorkspaceStrategyManager:
    def __init__(self, *, worktree_root: str | Path = "./runtime_worktrees",
                 runner: Optional[ProcessRunner] = None,
                 emit: Optional[Emit] = None) -> None:
        # 必须在构造期 resolve 成绝对路径：git worktree add 的 <path>
        # 参数相对**进程 cwd**解析，而我们的 git 调用 cwd=source ——
        # 相对路径会把 worktree 创建进 source 仓库内部（污染源仓，
        # 触发 dirty guard / §70）。真实 Demo 前的 fresh-clone smoke
        # 抓到的实证 bug（2026-09-25），回归测试见 test_p9_workspace。
        self.worktree_root = Path(worktree_root).resolve()
        self.runner = runner or DefaultProcessRunner()
        self._emit = emit or (lambda *a, **k: None)

    # ------------------------------------------------------------------
    # §62/§63/§64 提交期校验：base revision 钉住 + dirty/untracked 防护
    # ------------------------------------------------------------------
    def validate_for_submission(self, source_path: str | Path,
                                strategy: WorkspaceStrategy,
                                ) -> Dict[str, Any]:
        """返回 {"base_revision": sha, "warnings": [...]}。

        GIT_WORKTREE：非 git repo / git 缺失 -> Submission 拒绝（§80，
        绝不自动退回 DIRECT）；有未提交修改 -> 拒绝（§63）；有 untracked
        -> 警告（§64，worktree 不含它们）。
        """
        source = Path(source_path).resolve()
        if strategy == WorkspaceStrategy.DIRECT:
            return {"base_revision": "", "warnings": []}
        if strategy == WorkspaceStrategy.COPY:
            return {"base_revision": "", "warnings": []}
        # GIT_WORKTREE
        probe = _git(self.runner, "rev-parse", "--git-dir", cwd=str(source))
        if not probe.ok:
            raise WorkspacePreparationError(
                f"source 不是有效 git 仓库（策略 GIT_WORKTREE 要求 git）：{source} "
                f"—— 拒绝提交，不自动退回 DIRECT（§80）")
        # git 会**向上**找仓库。指一个不是仓库的子目录时，上面那条探测照样通过，
        # 于是任务会在父仓库的 worktree 上改代码 —— 写进错误的仓库，比报错糟得多。
        # 判据用 --show-prefix：仓库根时它是空串，子目录时是相对路径。
        # 不比较两条绝对路径，是因为 Windows 上大小写/链接/junction 都会让
        # "同一路径"写出两种字面，而前缀为空这件事没有这种歧义。
        prefix = _git(self.runner, "rev-parse", "--show-prefix", cwd=str(source))
        if prefix.ok and (prefix.stdout or "").strip():
            raise WorkspacePreparationError(
                f"--workspace 必须是仓库根本身：{source} 位于某个仓库的子目录 "
                f"(prefix={(prefix.stdout or '').strip()[:80]}) —— 改为指向该仓库根，"
                f"给这个目录自己 git init，或用 --strategy COPY")
        head = _git(self.runner, "rev-parse", "HEAD", cwd=str(source))
        if not head.ok:
            raise WorkspacePreparationError(
                f"无法读取 HEAD（空仓库？）：{head.stderr[:160]}")
        status = _git(self.runner, "status", "--porcelain", cwd=str(source))
        out = (status.stdout or "").strip()
        warnings: List[str] = []
        if status.ok and out:
            dirty_lines = [ln for ln in out.splitlines()
                           if ln.strip() and not ln.startswith("??")]
            untracked = [ln for ln in out.splitlines()
                         if ln.startswith("??")]
            if dirty_lines:
                raise WorkspacePreparationError(
                    f"source repo 有未提交修改（§63）：{len(dirty_lines)} 个文件 —— "
                    "GIT_WORKTREE 基于 commit，不会包含未提交内容。请先提交或改用 COPY。")
            if untracked:
                warnings.append(
                    f"source repo 有 {len(untracked)} 个 untracked 文件："
                    "worktree 不包含它们；如业务依赖请先提交或改用 COPY（§64）")
        return {"base_revision": head.stdout.strip(), "warnings": warnings}

    # ------------------------------------------------------------------
    # 准备（§81：失败 -> WORKSPACE_PREPARE_FAILED，任务不得真正执行）
    # ------------------------------------------------------------------
    def prepare(self, *, runtime_task_id: str, source_path: str | Path,
                strategy: WorkspaceStrategy | str, base_revision: str = "",
                now_iso: str = "") -> WorkspacePlan:
        # scheduler 传入的是字符串（RuntimeTask.workspace_strategy 列）——
        # 统一归一为枚举，避免 'str' has no attribute 'value'
        strategy = WorkspaceStrategy(strategy)
        source = Path(source_path).resolve()
        self._emit("WORKSPACE_PREPARE_STARTED",
                   detail=f"strategy={strategy.value} source={source}")
        if strategy == WorkspaceStrategy.DIRECT:
            plan = WorkspacePlan(
                strategy=strategy, source_workspace_path=str(source),
                execution_workspace_path=str(source),
                base_revision=base_revision,
                workspace_id=f"direct:{str(source).lower()}")
            self._emit("WORKSPACE_PREPARED", detail=plan.execution_workspace_path)
            return plan

        if strategy == WorkspaceStrategy.GIT_WORKTREE:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            exec_path = self.worktree_root / runtime_task_id
            if exec_path.exists():
                raise WorkspacePreparationError(
                    f"worktree 路径已存在（上次残留？）：{exec_path}",
                    kind="TRANSIENT")
            rev = base_revision or "HEAD"
            result = _git(self.runner, "worktree", "add", "--detach",
                          str(exec_path), rev, cwd=str(source), timeout=120)
            if not result.ok:
                raise WorkspacePreparationError(
                    f"git worktree add 失败：{result.stderr[:200]}",
                    kind="TRANSIENT")
            meta = WorktreeMetadata(
                runtime_task_id=runtime_task_id,
                source_repository=str(source),
                execution_workspace=str(exec_path),
                base_commit=rev,
                created_at=now_iso,
                strategy=strategy.value,
            )
            meta_path = exec_path / WORKTREE_META_NAME
            meta_path.write_text(meta.to_json(), encoding="utf-8")
            plan = WorkspacePlan(
                strategy=strategy, source_workspace_path=str(source),
                execution_workspace_path=str(exec_path),
                base_revision=rev,
                workspace_id=f"worktree:{runtime_task_id}",
                metadata_path=str(meta_path))
            self._emit("WORKTREE_CREATED",
                       detail=f"{exec_path} @ {rev}")
            self._emit("WORKSPACE_PREPARED", detail=str(exec_path))
            return plan

        # COPY（§20）
        copy_root = self.worktree_root.parent / "runtime_workspaces"
        copy_root.mkdir(parents=True, exist_ok=True)
        exec_path = copy_root / runtime_task_id
        if exec_path.exists():
            raise WorkspacePreparationError(
                f"复制目标已存在：{exec_path}", kind="TRANSIENT")
        shutil.copytree(
            source, exec_path,
            ignore=shutil.ignore_patterns(*COPY_DEFAULT_EXCLUDES),
            dirs_exist_ok=False)
        plan = WorkspacePlan(
            strategy=strategy, source_workspace_path=str(source),
            execution_workspace_path=str(exec_path),
            base_revision=base_revision,
            workspace_id=f"copy:{runtime_task_id}")
        self._emit("WORKSPACE_PREPARED", detail=str(exec_path))
        return plan

    # ------------------------------------------------------------------
    # §16 Result Artifact：终态时保存 base/status/diff/changed_files
    # ------------------------------------------------------------------
    def collect_result(self, plan: WorkspacePlan,
                       artifacts_dir: str | Path) -> Dict[str, Any]:
        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        exec_path = Path(plan.execution_workspace_path)
        result: Dict[str, Any] = {
            "workspace_strategy": plan.strategy.value,
            "execution_workspace_path": plan.execution_workspace_path,
            "base_revision": plan.base_revision,
        }
        if plan.strategy == WorkspaceStrategy.GIT_WORKTREE:
            status = _git(self.runner, "status", "--porcelain",
                          cwd=str(exec_path))
            lines = [ln.rstrip("\r") for ln in (status.stdout or "").splitlines()
                     if ln.strip()]
            result["git_status"] = "\n".join(lines)
            # §14：worktree 元数据 sidecar 不算业务改动
            lines = [ln for ln in lines
                     if WORKTREE_META_NAME not in ln]
            diff = _git(self.runner, "diff", plan.base_revision or "HEAD",
                        cwd=str(exec_path), timeout=120)
            patch_text = diff.stdout or ""
            # porcelain v1：XY（2 字符）+ 1 空格 + path —— 必须 ln[3:]，
            # split(" ", 1) 会把行首空格算进去导致 'M file.txt' 这种残缺路径
            #
            # 未跟踪的那几项必须换成**展开后的文件路径**：整个新目录会被 porcelain
            # 折叠成 `src/hooks/` 一项，而执行者自述的是里面的文件 —— 两份清单
            # 永远对不上，于是"新建目录"这种最常见的交付形状会被判成事实冲突。
            # 补丁本来就是按展开后的文件逐个生成的，清单用同一份才不会自相矛盾。
            untracked = _untracked_files(lines, exec_path)
            # 字节码不是交付物（`_is_deliverable` 的理由是现场实测出来的：一条 exit 0
            # 的合格交付曾被 `tests/__pycache__/*.pyc` 顶成"事实冲突"而拒收）。
            tracked = [ln[3:].strip().strip('"') for ln in lines
                       if len(ln) > 3 and not ln.startswith("??")
                       and _is_deliverable(ln[3:].strip().strip('"'))]
            result["changed_files"] = tracked + [
                u for u in untracked if u not in tracked and _is_deliverable(u)]
            # 未跟踪的新文件不出现在 `git diff` 里。而"从零新增文件"恰恰是内容
            # 工作最常见的交付形状 —— 不补这一段，里程碑明明建出了 index.html，
            # changes.patch 却是 0 行，判据只能说"未确认"（2026-09-28 实测）。
            # 用 `git diff --no-index` 逐个生成：不改索引、不动工作区状态；
            # 它的退出码 1 表示"有差异"，属正常结果。
            for rel in untracked:
                if not _is_deliverable(rel):
                    continue
                nd = _git(self.runner, "diff", "--no-index", "--", "/dev/null",
                          rel, cwd=str(exec_path), timeout=120)
                if nd.exit_code in (0, 1) and (nd.stdout or "").startswith(
                        "diff --git"):
                    patch_text += nd.stdout
            patch = artifacts / "changes.patch"
            patch.write_text(patch_text, encoding="utf-8")
            result["changes_patch"] = str(patch)
            if plan.metadata_path and Path(plan.metadata_path).is_file():
                try:
                    meta = WorktreeMetadata.from_path(Path(plan.metadata_path))
                    meta.status = "PRESERVED"
                    meta.result_diff_path = str(patch)
                    Path(plan.metadata_path).write_text(
                        meta.to_json(), encoding="utf-8")
                except Exception:  # noqa: BLE001 - 元数据损坏不阻塞取证
                    pass
            self._emit("WORKTREE_PRESERVED", detail=str(patch))
        (artifacts / "workspace_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return result

    # ------------------------------------------------------------------
    # §18 Cleanup Safety / §19 Prune
    # ------------------------------------------------------------------
    def cleanup(self, plan: WorkspacePlan, *, task_terminal: bool,
                lease_active: bool, patch_saved: bool) -> bool:
        """§18：四个安全条件不满足即拒绝。返回是否真正清理。"""
        if plan.strategy != WorkspaceStrategy.GIT_WORKTREE:
            return False
        if not task_terminal:
            raise WorkspacePreparationError(
                "cleanup 拒绝：任务未到终态（§18）")
        if lease_active:
            raise WorkspacePreparationError(
                "cleanup 拒绝：仍持有 active lease（§18）")
        if not patch_saved:
            raise WorkspacePreparationError(
                "cleanup 拒绝：结果 patch 未保存（§18）")
        exec_path = Path(plan.execution_workspace_path)
        source = Path(plan.source_workspace_path)
        result = _git(self.runner, "worktree", "remove", "--force",
                      str(exec_path), cwd=str(source), timeout=120)
        if result.ok:
            self._emit("WORKTREE_CLEANED", detail=str(exec_path))
            return True
        return False

    def prune(self, source_path: str | Path) -> bool:
        """§19：git worktree prune（只清 stale 管理数据，不删有效工作树）。"""
        result = _git(self.runner, "worktree", "prune",
                      cwd=str(Path(source_path).resolve()))
        return result.ok


__all__ = ["WorkspaceStrategyManager", "WorkspacePlan", "WorktreeMetadata",
           "WorkspacePreparationError"]
