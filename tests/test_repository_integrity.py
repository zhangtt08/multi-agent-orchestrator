"""仓库完整性守卫（阶段七收口 §16-§19）。

背景：`.gitignore` 裸 `memory/` 曾把源码包 `mao/memory/` 整个排除在版本
控制外，项目换机迁移时才发现"仓库不含运行所需源码"。这类缺陷本地全绿、
一迁移就炸，必须用测试锁死。

三层防线：
    1. GitignoreAnchoring      —— /memory/ 根锚定，mao/memory/ 永不被 ignore
    2. SourcePackageTrackedGuard —— 五个源码包的 *.py 必须被 git 跟踪
    3. FreshCloneSimulation    —— tracked files 复制到临时目录后可独立 import
                                  并跑最小 smoke（memory.enabled false/true）

本文件是 repository health test，不是运行时功能。
要求：项目以 git 仓库形态检出（无 .git 时显式 FAIL，不静默跳过）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# §17：必须被 git 跟踪的源码包（memory 层曾整包漏网，故显式点名；
# workspaces 层曾因裸 `workspaces/` 规则整包漏网 —— 2026-09-25 P9 发现）
SOURCE_PACKAGES = (
    "mao/core",
    "mao/agents",
    "mao/transports",
    "mao/memory",
    "mao/harness",
    "mao/scheduler",
    "mao/workspaces",
    "mao/checkpoints",
)


def _git(*args: str) -> "subprocess.CompletedProcess[str]":
    """在 PROJECT_ROOT 跑 git 命令，utf-8 解码，check=False。"""
    return subprocess.run(
        ["git", *args], cwd=str(PROJECT_ROOT), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _enclosing_repo(path: Path) -> "str | None":
    """`path` 是否位于某个 git 仓库内（git 会向上找）。None = 不在任何仓库里。

    这个判断本身就是要显式测的前提：迁移轮踩过"空 .git 让 git 走到父仓库，
    于是把项目状态当成了探针目录的状态"，所以这里不能靠猜目录，要问 git。
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(path),
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or str(path)


def _require_git() -> None:
    """git 不可用或非仓库 -> FAIL（完整性无法验证时不能假装通过）。"""
    probe = _git("rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.fail(
            "仓库完整性测试需要 git 仓库：git rev-parse 失败（"
            f"rc={probe.returncode}, stderr={probe.stderr.strip()[:200]}）。"
            "没有 git 就无法证明源码被跟踪 —— 这是迁移事故的直接教训。"
        )


# ===========================================================================
# §16 .gitignore 防回归：根锚定必须永远成立
# ===========================================================================
class TestGitignoreAnchoring:
    def test_memory_source_package_is_not_ignored(self):
        """mao/memory/ 的源码绝不能被 gitignore（裸 memory/ 事故回归锁）。"""
        _require_git()
        sentinels = [
            "mao/memory/__init__.py",
            "mao/memory/store.py",
            "mao/memory/outcome.py",
            "mao/memory/hybrid.py",
        ]
        for rel in sentinels:
            assert (PROJECT_ROOT / rel).is_file(), f"源码文件缺失: {rel}"
            probe = _git("check-ignore", "-q", rel)
            assert probe.returncode != 0, (
                f"{rel} 被 .gitignore 忽略！裸 memory/ 规则回归 —— "
                "规则必须根锚定为 /memory/"
            )

    def test_root_memory_dir_is_ignored(self):
        """/memory/（运行时产物目录）必须被 ignore —— memory.db 不入库。"""
        _require_git()
        probe = _git("check-ignore", "-q", "memory/")
        assert probe.returncode == 0, (
            "根级 memory/ 运行时目录没有被 ignore —— memory.db / FAISS index "
            "有被误提交的风险（§19/§20）"
        )
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "/memory/" in text, ".gitignore 缺少根锚定规则 /memory/"

    @pytest.mark.parametrize("runtime_dir", [
        "runtime/",
        "runtime_p7/",
        "workspaces/",
        "workspace/",
        "archive/config-history/config_p7/local.yaml",
        "runtime_scheduler/",
    ])
    def test_runtime_data_stays_ignored(self, runtime_dir: str):
        """§19：运行时数据 / 本机私有配置继续被 ignore。"""
        _require_git()
        probe = _git("check-ignore", "-q", runtime_dir)
        assert probe.returncode == 0, f"{runtime_dir} 应被 ignore 实则没有"

    def test_scheduler_source_package_not_ignored(self):
        """§68：裸 `scheduler/` 会吃掉 mao/scheduler/ —— 规则必须根锚定
        为 /runtime_scheduler/（与裸 memory/ 事故同型，锁死）。"""
        _require_git()
        sentinels = [
            "mao/scheduler/__init__.py",
            "mao/scheduler/repository.py",
            "mao/scheduler/scheduler.py",
        ]
        for rel in sentinels:
            assert (PROJECT_ROOT / rel).is_file(), f"源码文件缺失: {rel}"
            probe = _git("check-ignore", "-q", rel)
            assert probe.returncode != 0, (
                f"{rel} 被 .gitignore 忽略！scheduler 规则未根锚定"
            )
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "/runtime_scheduler/" in text

    def test_workspaces_source_package_not_ignored(self):
        """§94：裸 `workspaces/` 会吃掉源码包 mao/workspaces/ ——
        2026-09-25 真实发生（Phase 9 工作区策略包整包未版本化），
        规则已根锚定为 /workspaces/，本测试锁死不再回归。"""
        _require_git()
        sentinels = [
            "mao/workspaces/__init__.py",
            "mao/workspaces/manager.py",
            "mao/workspaces/strategies.py",
            "mao/workspaces/runner.py",
        ]
        for rel in sentinels:
            assert (PROJECT_ROOT / rel).is_file(), f"源码文件缺失: {rel}"
            probe = _git("check-ignore", "-q", rel)
            assert probe.returncode != 0, (
                f"{rel} 被 .gitignore 忽略！裸 workspaces/ 规则回归 —— "
                "规则必须根锚定为 /workspaces/"
            )
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "/workspaces/" in text, ".gitignore 缺少根锚定规则 /workspaces/"

    def test_phase9_runtime_dirs_root_anchored_and_ignored(self):
        """§94：Phase 9 运行目录 /runtime_worktrees/ /runtime_workspaces/
        必须被 ignore 且规则显式根锚定（worktree/副本工作区不入库）。"""
        _require_git()
        for runtime_dir in ("runtime_worktrees/", "runtime_workspaces/"):
            probe = _git("check-ignore", "-q", runtime_dir)
            assert probe.returncode == 0, (
                f"{runtime_dir} 应被 ignore 实则没有"
            )
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for rule in ("/runtime_worktrees/", "/runtime_workspaces/"):
            assert rule in text, f".gitignore 缺少根锚定规则 {rule}"
        # 根锚定的反例：子目录形态绝不能被任何规则吃掉。
        # 注意用**文件路径**而不是目录路径 —— git check-ignore 对
        # 不存在的目录路径（尾斜杠）会被 .gitignore 里的空行/CR 行误报。
        deep = "mao/core/subdir/runtime_worktrees/keep.txt"
        assert _git("check-ignore", "-q", deep).returncode != 0, (
            "根锚定被破坏：mao/*/ 下的同名路径也被 ignore 了"
        )

    def test_no_runtime_data_tracked(self):
        """§19：git 索引里不允许出现运行时产物（memory.db / FAISS / runtime）。"""
        _require_git()
        tracked = _git("ls-files").stdout.splitlines()
        bad = [
            f for f in tracked
            if f.startswith(("memory/", "runtime", "workspace", "workspaces/"))
            or f.endswith((".memory.db", ".faiss", ".bin"))
        ]
        assert not bad, f"运行时数据被误跟踪：{bad[:10]}"

    def test_memory_db_not_tracked_anywhere(self):
        """§20：任何形式的 memory.db 都不许进 git（demo 专用库也不行）。"""
        _require_git()
        tracked = _git("ls-files").stdout.splitlines()
        offenders = [f for f in tracked if "memory.db" in Path(f).name]
        assert not offenders, f"memory.db 被跟踪：{offenders}"


# ===========================================================================
# §17 SourcePackageTrackedGuard：源码包 *.py 必须 tracked 且未被 ignore
# ===========================================================================
class TestSourcePackageTrackedGuard:
    def test_all_source_files_tracked(self):
        _require_git()
        tracked = set(_git("ls-files").stdout.splitlines())
        ignored = set(_git("ls-files", "--ignored", "--others",
                           "--exclude-standard", "--directory").stdout.splitlines())
        untracked = set(_git("ls-files", "--others", "--exclude-standard")
                        .stdout.splitlines())
        offenders: list[str] = []
        for pkg in SOURCE_PACKAGES:
            pkg_dir = PROJECT_ROOT / pkg
            if not pkg_dir.is_dir():
                offenders.append(f"{pkg}: 包目录不存在")
                continue
            for py in pkg_dir.rglob("*.py"):
                rel = py.relative_to(PROJECT_ROOT).as_posix()
                if rel not in tracked:
                    state = ("ignored" if rel in ignored
                             else "untracked" if rel in untracked else "unknown")
                    offenders.append(f"{rel} ({state})")
        assert not offenders, (
            "源码包内存在未被 git 跟踪的 *.py（ignored+untracked = 迁移即丢源码）：\n"
            + "\n".join(offenders)
        )

    def test_prompts_and_configs_tracked(self):
        """Prompt 模板与配置目录也是运行所需 —— 同样必须入库。

        覆盖全部 config 目录（2026-09-25 Audit S 教训：新建的
        config_p9 曾漏 add —— fresh clone 里 doctor 直接
        "config file not found"，历史阶段同样可能迁移时才炸）。
        阶段性历史档（config_p2 … config_p10、离线 Mock 档）自 2026-10-02 起住在
        `archive/config-history/` 下 —— 搬家不改变"它们也必须入库"这条判据，
        所以这里同时扫两处，判据看的是**内容清单**而不是目录在根上还是归档里。
        """
        _require_git()
        tracked = set(_git("ls-files").stdout.splitlines())
        roots = [PROJECT_ROOT / "config", PROJECT_ROOT / "archive" / "config-history"]
        config_dirs = sorted(p for r in roots if r.is_dir()
                             for p in ([r] + sorted(d for d in r.iterdir()
                                                    if d.is_dir())))
        names = {d.name for d in config_dirs}
        assert "config_p9" in names, "config_p9 目录意外缺失（archive/config-history/）"
        missing: list[str] = []
        for root in [PROJECT_ROOT / "prompts", *config_dirs]:
            for f in root.rglob("*"):
                if f.is_file() and not f.name.endswith(".pyc"):
                    rel = f.relative_to(PROJECT_ROOT).as_posix()
                    if rel not in tracked:
                        missing.append(rel)
        assert not missing, f"运行所需文件未跟踪：{missing}"


# ===========================================================================
# §18 FreshCloneSimulation：tracked files 独立成仓后可运行
# ===========================================================================
_SMOKE_SCRIPT = textwrap.dedent("""
    import sys
    sys.path.insert(0, ".")
    import mao.core
    import mao.agents
    import mao.memory
    import mao.scheduler
    import mao.workspaces
    import mao.checkpoints

    from mao.checkpoints import SQLiteCheckpointStore
    _cp = SQLiteCheckpointStore("./p9_smoke/checkpoints.db",
                                artifacts_root="./p9_smoke")
    assert _cp.schema_version() if hasattr(_cp, "schema_version") else True

    from mao.core.config import load_config
    from mao.memory import build_memory_layer

    # enabled=false -> 整层缺席（§35/§47）
    cfg = load_config("archive/config-history/config_p7")
    cfg.settings.memory.enabled = False
    assert build_memory_layer(cfg) is None, "enabled=false 应返回 None"

    # enabled=true -> 层可构造（语义层允许缺席：fresh clone 无 ML venv，
    # provider 不可用 -> hybrid=None，SQLite 层独立工作，§12 降级）
    cfg = load_config("archive/config-history/config_p7")
    cfg.settings.memory.enabled = True
    cfg.settings.memory.path = "./memory/memory.db"
    layer = build_memory_layer(cfg)
    assert layer is not None, "enabled=true 应构造出 MemoryLayer"
    assert layer.store.list_recent(limit=1) is not None
    print("SMOKE_OK")
""")

# §95：fresh clone 里的 scheduler / worktree smoke —— 不依赖 tests/ 的
# FakeOrchestrator，内联最小 fake factory（core 只认接口）。
_SCHEDULER_SMOKE_SCRIPT = textwrap.dedent("""
    import subprocess, sys
    from pathlib import Path

    sys.path.insert(0, ".")
    from mao.core.models import Task, TaskState
    from mao.scheduler import (Priority, RetryPolicy, RuntimeOutcomeMapper,
                               RuntimeScheduler, SystemClock, TaskRepository,
                               TaskSubmissionService)
    from mao.workspaces import WorkspaceStrategyManager

    # ---- 1) max_concurrent_tasks=1 inline smoke（Phase 8 等价模式）----
    repo = TaskRepository("./p9_smoke/queue.db", clock=SystemClock())
    svc = TaskSubmissionService(repo, clock=repo.clock)
    ra = svc.submit(Task(goal="smoke A", context={"task_type": "smoke"}))
    rb = svc.submit(Task(goal="smoke B", context={"task_type": "smoke"}))

    class _FakeResult:
        final_state = TaskState.COMPLETED
        reason = "smoke"

    class _FakeOrch:
        def __init__(self, **kw):
            pass
        def run(self, task):
            return _FakeResult()

    def factory(**kw):
        return _FakeOrch()

    sched = RuntimeScheduler(
        repo, factory, clock=repo.clock,
        max_concurrent_tasks=1, pool_size=0,
        lease_timeout_seconds=120, heartbeat_seconds=15,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=1,
                                 max_delay_seconds=2, jitter_seconds=0),
        attempts_root="./p9_smoke/rt", worker_id="smoke_worker")
    sched.run(max_ticks=4, poll_seconds=0.05, with_heartbeat=False)
    assert repo.get(ra.runtime_task_id).status.value == "COMPLETED"
    assert repo.get(rb.runtime_task_id).status.value == "COMPLETED"
    repo.close()

    # ---- 2) GIT_WORKTREE 准备/清理 smoke（真实 git，临时仓库）----
    src = Path("./p9_smoke/demo-src").resolve()
    src.mkdir(parents=True, exist_ok=True)
    def git(*args):
        r = subprocess.run(["git", "-c", "user.email=smoke@example.com",
                            "-c", "user.name=smoke", *args],
                           cwd=str(src), capture_output=True, text=True)
        assert r.returncode == 0, f"git {args} failed: {r.stderr}"
        return r
    git("init", "-q")
    (src / "hello.txt").write_text("hello\\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "baseline")
    head = git("rev-parse", "HEAD").stdout.strip()

    mgr = WorkspaceStrategyManager(worktree_root="./p9_smoke/worktrees")
    plan = mgr.prepare(runtime_task_id="smoke-wt", source_path=str(src),
                       strategy="GIT_WORKTREE", base_revision=head)
    wt = Path(plan.execution_workspace_path)
    assert wt.is_dir() and (wt / "hello.txt").is_file()
    assert (wt / ".mao-worktree-meta.json").is_file()
    # 隔离写入不回源
    (wt / "hello.txt").write_text("changed\\n", encoding="utf-8")
    assert (src / "hello.txt").read_text(encoding="utf-8") == "hello\\n"
    assert mgr.cleanup(plan, task_terminal=True, lease_active=False,
                       patch_saved=True), "cleanup 应成功"
    assert not wt.exists(), "cleanup 后 worktree 应已移除"

    # ---- 3) Phase 10（§62）：checkpoint DB + 假 resume smoke（不起 Agent）----
    from mao.core.config import load_config as _load
    from mao.checkpoints import (CheckpointRecord, CheckpointStage,
                                 CheckpointStatus, ResumeManager,
                                 SQLiteCheckpointStore)
    for _dir in ("archive/config-history/config_p10", "archive/config-history/config_p10_offline"):
        _cfg = _load(_dir, require_harness_file=False)
        assert _cfg.settings.checkpoint.enabled is True, _dir

    cp_root = Path("./p9_smoke/rt")
    cp_root.mkdir(parents=True, exist_ok=True)
    store = SQLiteCheckpointStore(cp_root / "checkpoints.db",
                                  artifacts_root=cp_root)
    plan_file = cp_root / "smoke-plan.json"
    plan_file.write_text('{"goal": "smoke", "tasks": [], '
                         '"acceptance_criteria": []}', encoding="utf-8")
    rec = CheckpointRecord(
        checkpoint_id="CP-smoke-verify", task_id="task_smoke",
        runtime_task_id="rt_smoke", attempt=1, round_no=1,
        stage=CheckpointStage.VERIFICATION_COMPLETED,
        status=CheckpointStatus.PREPARING,
        created_at="2026-09-26T00:00:00+00:00")
    store.prepare(rec)
    store.commit(rec.checkpoint_id, artifact_files={"plan.json": plan_file},
                 workspace_fingerprint="")
    assert store.verify_integrity(store.get("CP-smoke-verify")) is None, \\
        "刚提交的 checkpoint 应当通过完整性校验"
    ev = ResumeManager(store, config=_cfg.settings.checkpoint).find_resume_point(
        task_id="task_smoke", runtime_task_id="rt_smoke", attempt=1,
        workspace_path=None)
    assert ev.ok and ev.resume_point.next_stage == "REVIEWING", ev.failure_kind
    assert ev.resume_point.plan and ev.resume_point.plan["goal"] == "smoke", \\
        "resume point 必须能从快照恢复 plan（§30）"

    # 只有 PREPARING 的任务：未提交内容一律不得当恢复点（§100）
    pre = CheckpointRecord(
        checkpoint_id="CP-smoke-preparing", task_id="task_smoke2",
        runtime_task_id="rt_smoke2", attempt=1, round_no=1,
        stage=CheckpointStage.REVIEW_COMPLETED,
        status=CheckpointStatus.PREPARING,
        created_at="2026-09-26T00:00:00+00:00")
    store.prepare(pre)
    ev2 = ResumeManager(store,
                        config=_cfg.settings.checkpoint).find_resume_point(
        task_id="task_smoke2", runtime_task_id="rt_smoke2", attempt=1,
        workspace_path=None)
    assert not (ev2.ok and ev2.resume_point.review), \\
        "PREPARING 的 review 工件被当成恢复点了（§100）"
    print("SCHEDULER_SMOKE_OK")
""")


class TestFreshCloneSimulation:
    """不必真 clone：复制 tracked files 到临时目录（排除一切 ignored），
    在干净目录里 import + 最小 smoke —— 证明仓库自包含。"""

    def _fresh_clone(self, tmp_path: Path) -> Path:
        """tracked-files-only 复制（§95：禁止整目录复制 —— 会偷带
        未版本化文件），返回克隆目录。"""
        _require_git()
        tracked = [f for f in _git("ls-files").stdout.splitlines() if f.strip()]
        assert tracked, "git ls-files 为空 —— 仓库没有跟踪任何文件？"

        copied = 0
        for rel in tracked:
            src = PROJECT_ROOT / rel
            if not src.is_file():
                continue  # git 索引里有但工作树没有（如被删）——跳过
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        assert copied >= 50, f"tracked files 只复制到 {copied} 个，异常"

        # 忽略的运行时数据绝不能被带进 fresh clone（§19）
        assert not (tmp_path / "memory" / "memory.db").exists(), (
            "memory.db 混进了 tracked 复制集"
        )
        assert not (tmp_path / "memory" / "vector_index").exists(), (
            "FAISS index 混进了 tracked 复制集"
        )
        # Phase 9 教训：mao/workspaces 必须出现在 tracked 复制集里
        assert (tmp_path / "mao" / "workspaces" / "manager.py").is_file(), (
            "mao/workspaces 源码未进 tracked 复制集 —— 裸 workspaces/ 回归？"
        )
        return tmp_path

    @staticmethod
    def _clean_env() -> dict:
        env = {
            **os.environ,
            "PYTHONPATH": "",
            # 显式清掉本机私有环境变量：fresh clone 没有 ml venv / HF 缓存
        }
        for var in ("MEMORY_EMBEDDING_INTERPRETER",
                    "MEMORY_EMBEDDING_MODEL_PATH", "MEMORY_HF_HOME"):
            env.pop(var, None)
        return env

    def test_tracked_files_alone_can_import_and_smoke(self, tmp_path: Path):
        clone = self._fresh_clone(tmp_path)
        # 干净解释器里 import + 最小 smoke（cwd=克隆目录，
        # ./memory/memory.db 落在克隆内，不碰真实运行库）
        env = self._clean_env()
        env["PYTHONPATH"] = str(clone)
        probe = subprocess.run(
            [sys.executable, "-c", _SMOKE_SCRIPT],
            cwd=str(clone), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=120,
        )
        assert probe.returncode == 0, (
            "fresh clone simulation 失败 —— 仓库缺少运行所需源码/配置：\n"
            f"stdout: {probe.stdout[-2000:]}\nstderr: {probe.stderr[-2000:]}"
        )
        assert "SMOKE_OK" in probe.stdout

    def test_fresh_clone_scheduler_and_worktree_smoke(self, tmp_path: Path):
        """§95：fresh clone 里 scheduler（inline 模式）+ GIT_WORKTREE
        准备/清理都能工作 —— 调度层不依赖本机历史残留。"""
        clone = self._fresh_clone(tmp_path)
        env = self._clean_env()
        env["PYTHONPATH"] = str(clone)
        probe = subprocess.run(
            [sys.executable, "-c", _SCHEDULER_SMOKE_SCRIPT],
            cwd=str(clone), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=180,
        )
        assert probe.returncode == 0, (
            "fresh clone scheduler/worktree smoke 失败：\n"
            f"stdout: {probe.stdout[-2000:]}\nstderr: {probe.stderr[-2000:]}"
        )
        assert "SCHEDULER_SMOKE_OK" in probe.stdout


class TestLineEndingPolicy:
    """行尾策略守卫（迁移轮 §21-§24）。

    为什么这也要锁：本项目的多处判据按**文件字节**算 —— evidence.py 的源码
    快照、checkpoints/fingerprints.py 的工作区指纹、checkpoint 的 artifact
    SHA256。而 Git for Windows 默认 `core.autocrlf=true` 是**每台机器各自的
    设置**：检出给你 CRLF，一点 nothing 就"全仓被改写"。迁移到新机器后实测
    到的正是这个形态（索引 215 个 LF，工作树 51 个 CRLF）。

    一条规则不该依赖克隆者的 git 配置，所以这里验的不是"我配对了"，而是
    "仓库自带的答案把人配置掉了"。
    """

    def _eol_rows(self):
        _require_git()
        proc = _git("ls-files", "--eol")
        assert proc.returncode == 0, proc.stderr
        rows = []
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                rows.append((fields[-1], fields[0], fields[1]))
        return rows

    def test_gitattributes_exists_and_declares_lf(self):
        attr = PROJECT_ROOT / ".gitattributes"
        assert attr.is_file(), (
            "缺少 .gitattributes：行尾策略不能依赖每台机器的 core.autocrlf")
        text = attr.read_text(encoding="utf-8")
        assert "text=auto" in text
        assert "eol=lf" in text, "必须显式钉住检出行尾，而不是只做归一化"

    def test_no_tracked_file_is_crlf_in_worktree(self):
        bad = [name for name, _index, worktree in self._eol_rows()
               if worktree == "w/crlf"]
        assert not bad, (
            "工作树里出现 CRLF 的受跟踪文件（%d 个），会与索引里的 LF 形成"
            "指纹/hash 漂移：%s" % (len(bad), ", ".join(bad[:8])))

    def test_index_stores_lf_for_every_text_file(self):
        bad = [name for name, index, _wt in self._eol_rows()
               if index not in ("i/lf", "i/none", "i/-text")]
        assert not bad, "索引里不该有非 LF 的文本对象：%s" % ", ".join(bad[:8])

    def test_fingerprint_changes_when_only_line_endings_change(self):
        """把"为什么需要上面三条"钉成一个可执行事实。

        工作区指纹对 EOL **敏感**（它就是按字节算的）。这不是缺陷 —— 正因为
        敏感，才不能让一次 checkout 把整棵树判成"被改写过"。若哪天有人给
        指纹加行尾归一化，本用例转红，逼他重新考虑这个决定。

        必须在仓库**外面**做：项目根已经是 git 仓库，在任何仓库内子目录里跑
        指纹都会走 git 分支（HEAD + status），而 `/runtime_*/` 之类的 ignore
        规则会让内容根本不进指纹 —— 那测的就不是字节敏感性了。
        """
        from mao.checkpoints.fingerprints import capture_workspace_fingerprint

        # 不能用 tmp_path：调用方给 --basetemp 指到仓库内（例如本仓约定的
        # .basetemp_run/）时，探针就落进了 git 的 ignore 区 —— git 分支既不看
        # HEAD 变化也不看被忽略的内容，于是 LF/CRLF 得到同一指纹，本用例以
        # 与"指纹变钝"完全相同的样子转红。测的是字节敏感性，就该自己挑一个
        # 确定不在任何仓库里的目录。
        import tempfile

        workspace = Path(tempfile.mkdtemp(prefix="mao-eol-probe-"))
        try:
            enclosing = _enclosing_repo(workspace)
            assert enclosing is None, (
                f"探针目录 {workspace} 落在某个 git 仓库里（{enclosing}），"
                "指纹会走 git 分支，本用例测不到字节敏感性")

            target = workspace / "sample.py"

            target.write_bytes(b"def f():\n    return 1\n")
            lf = capture_workspace_fingerprint(workspace).overall
            target.write_bytes(b"def f():\r\n    return 1\r\n")
            crlf = capture_workspace_fingerprint(workspace).overall
            target.write_bytes(b"def f():\n    return 1\n")
            again = capture_workspace_fingerprint(workspace).overall

            assert lf and lf == again, "同一批字节必须得到同一指纹（幂等）"
            assert lf != crlf, (
                "指纹竟然对 EOL 不敏感了 —— 那么 .gitattributes 的理由也一起"
                "失效，请连同上面三条守卫一起重新评估")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


class TestNoIgnoredFileIsTracked:
    """被跟踪的文件不得同时命中 ignore 规则（迁移轮 §61）。

    为什么单独一条：`.final.txt` 这类调试输出先被 `git add -A` 收了进去，
    之后才补上 ignore 规则 —— 而 gitignore 对**已跟踪**文件永远无效。
    结果是仓库里长着一个"看起来被管理、实际每次都会带着走"的残留：
    fresh clone 里有它，换机迁移也有它，而所有人以为它已被排除。
    同一形态此前发生在 10 个 pytest 调试 log 上（审计 P1）。
    """

    def test_no_tracked_file_matches_ignore_rules(self):
        _require_git()
        proc = _git("ls-files", "-i", "-c", "--exclude-standard")
        assert proc.returncode == 0, proc.stderr
        offenders = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        assert not offenders, (
            "这些文件既被 git 跟踪又命中 .gitignore（gitignore 对已跟踪文件"
            "无效，所以它们会跟着每一次克隆与每一次迁移）：%s\n"
            "处理：git rm --cached <file>（保留磁盘文件，只脱离版本库）"
            % ", ".join(offenders))
