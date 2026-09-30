"""Phase 9 workspace 隔离测试（§9-§22/§58-§64/§70）。

真实 git 操作（本机 git 已验证可用），全部在 tmp_path 内。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mao.core.models import Task, TaskState
from mao.scheduler import (FakeClock, Priority, RuntimeScheduler,
                           RuntimeStatus, TaskRepository,
                           TaskSubmissionService)
from mao.workspaces import (DefaultProcessRunner, WorkspacePreparationError,
                            WorkspaceStrategy, WorkspaceStrategyManager)
from tests.test_p8_integration import FakeOrchestrator


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    """干净 git 仓库：file.txt = BASE，一个已提交基线。"""
    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "file.txt").write_text("BASE\n", encoding="utf-8")
    git("init", "-q", "-b", "master", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    return repo


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceStrategyManager:
    return WorkspaceStrategyManager(worktree_root=tmp_path / "worktrees")


# ===========================================================================
# §62/§63/§64/§80：提交期校验
# ===========================================================================
class TestSubmissionValidation:
    def test_clean_repo_captures_base_revision(self, source_repo, manager):
        result = manager.validate_for_submission(source_repo,
                                                 WorkspaceStrategy.GIT_WORKTREE)
        assert len(result["base_revision"]) == 40    # HEAD SHA 钉住（§62）
        assert result["warnings"] == []

    def test_dirty_source_rejected(self, source_repo, manager):
        """§63：未提交修改 -> 拒绝提交，不自动退回 DIRECT。"""
        (source_repo / "file.txt").write_text("DIRTY\n", encoding="utf-8")
        with pytest.raises(WorkspacePreparationError, match="未提交修改"):
            manager.validate_for_submission(source_repo,
                                            WorkspaceStrategy.GIT_WORKTREE)

    def test_untracked_warns(self, source_repo, manager):
        """§64：untracked -> 警告（worktree 不含它们）。"""
        (source_repo / "extra.txt").write_text("x", encoding="utf-8")
        result = manager.validate_for_submission(source_repo,
                                                 WorkspaceStrategy.GIT_WORKTREE)
        assert result["warnings"]
        assert "untracked" in result["warnings"][0]

    def test_non_git_repo_rejected(self, tmp_path, manager):
        """§80：git 不可用/仓库无效 -> 拒绝，不退回 DIRECT。"""
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(WorkspacePreparationError, match="git"):
            manager.validate_for_submission(plain,
                                            WorkspaceStrategy.GIT_WORKTREE)


# ===========================================================================
# §12/§14/§60/§61/§70：worktree 创建 / 隔离 / 取证
# ===========================================================================
class TestWorktreeIsolation:
    def test_prepare_creates_detached_worktree_with_metadata(
            self, source_repo, manager, tmp_path):
        base = manager.validate_for_submission(
            source_repo, WorkspaceStrategy.GIT_WORKTREE)["base_revision"]
        plan = manager.prepare(runtime_task_id="rt-x",
                               source_path=source_repo,
                               strategy=WorkspaceStrategy.GIT_WORKTREE,
                               base_revision=base, now_iso="now")
        exec_path = Path(plan.execution_workspace_path)
        assert exec_path.is_dir()
        head = git("rev-parse", "HEAD", cwd=exec_path).strip()
        assert head == base                       # detach 在基线 commit
        meta = json.loads(
            (exec_path / ".mao-worktree-meta.json").read_text(encoding="utf-8"))
        assert meta["base_commit"] == base
        assert meta["runtime_task_id"] == "rt-x"

    def test_relative_worktree_root_never_lands_inside_source(
            self, source_repo, tmp_path, monkeypatch):
        """回归（2026-09-25 实证 bug）：git worktree add 的 <path> 相对
        **进程 cwd** 解析，而 git 调用的 cwd=source —— 若 worktree_root
        是相对路径，worktree 会创建进 source 仓库内部（污染源仓、
        触发 dirty guard）。构造期必须 resolve 成绝对路径。"""
        monkeypatch.chdir(tmp_path)               # 进程 cwd != source
        mgr = WorkspaceStrategyManager(worktree_root="./runtime_worktrees")
        base = mgr.validate_for_submission(
            source_repo, WorkspaceStrategy.GIT_WORKTREE)["base_revision"]
        plan = mgr.prepare(runtime_task_id="rt-rel",
                           source_path=source_repo,
                           strategy=WorkspaceStrategy.GIT_WORKTREE,
                           base_revision=base, now_iso="now")
        exec_path = Path(plan.execution_workspace_path)
        assert exec_path.is_absolute()
        assert exec_path.is_dir()
        # worktree 必须落在解析后的 root 下，而不是 source 里
        assert exec_path == Path(tmp_path / "runtime_worktrees" / "rt-rel")
        assert not (source_repo / "runtime_worktrees").exists()
        # source 全程 clean（§70）
        status = git("status", "--porcelain", cwd=source_repo)
        assert status.strip() == ""

    def test_two_worktrees_isolated_and_source_clean(
            self, source_repo, manager, tmp_path):
        """§60/§61/§70（核心验收）：同 repo 两 worktree 并发修改互不可见。"""
        base = manager.validate_for_submission(
            source_repo, WorkspaceStrategy.GIT_WORKTREE)["base_revision"]
        plan_a = manager.prepare(runtime_task_id="rt-a",
                                 source_path=source_repo,
                                 strategy=WorkspaceStrategy.GIT_WORKTREE,
                                 base_revision=base, now_iso="now")
        plan_b = manager.prepare(runtime_task_id="rt-b",
                                 source_path=source_repo,
                                 strategy=WorkspaceStrategy.GIT_WORKTREE,
                                 base_revision=base, now_iso="now")
        exec_a = Path(plan_a.execution_workspace_path)
        exec_b = Path(plan_b.execution_workspace_path)

        (exec_a / "file.txt").write_text("AAA\n", encoding="utf-8")
        (exec_b / "file.txt").write_text("BBB\n", encoding="utf-8")

        assert (exec_a / "file.txt").read_text(
            encoding="utf-8").strip() == "AAA"
        assert (exec_b / "file.txt").read_text(
            encoding="utf-8").strip() == "BBB"
        # source repo 保持 clean（§70）
        assert git("status", "--porcelain",
                   cwd=source_repo).strip() == ""
        assert (source_repo / "file.txt").read_text(
            encoding="utf-8").strip() == "BASE"

        # §61：各自 diff 只见自己的修改
        result_a = manager.collect_result(plan_a, tmp_path / "artifacts_a")
        result_b = manager.collect_result(plan_b, tmp_path / "artifacts_b")
        patch_a = Path(result_a["changes_patch"]).read_text(encoding="utf-8")
        patch_b = Path(result_b["changes_patch"]).read_text(encoding="utf-8")
        assert "+AAA" in patch_a and "BBB" not in patch_a
        assert "+BBB" in patch_b and "AAA" not in patch_b
        assert result_a["changed_files"] == ["file.txt"]

    def test_bytecode_is_not_a_deliverable(self, source_repo, manager, tmp_path):
        """跑测试留下的 `.pyc` 不该进改动清单，更不该进补丁。

        真实那一跑（2026-09-30）的现场：执行者写完文件后跑了自己的测试，
        `tests/__pycache__/*.pyc` 因此出现在框架采集的清单里，比自述多两项 ——
        闸门判"改动清单不一致（事实冲突）"，一份框架验收命令 exit 0 的合格交付
        就此被拒；补丁里还混进了 `Binary files differ`。
        """
        base = manager.validate_for_submission(
            source_repo, WorkspaceStrategy.GIT_WORKTREE)["base_revision"]
        plan = manager.prepare(runtime_task_id="rt-pyc",
                               source_path=source_repo,
                               strategy=WorkspaceStrategy.GIT_WORKTREE,
                               base_revision=base, now_iso="now")
        exec_path = Path(plan.execution_workspace_path)
        (exec_path / "acceptance.md").write_text("# 验收基线\n", encoding="utf-8")
        (exec_path / "tests" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (exec_path / "tests" / "__pycache__" / "x.cpython-313.pyc").write_bytes(
            b"\x00\x01fake-compiled-payload")

        result = manager.collect_result(plan, tmp_path / "artifacts_pyc")
        text = Path(result["changes_patch"]).read_text(encoding="utf-8")
        assert result["changed_files"] == ["acceptance.md"], result["changed_files"]
        assert "__pycache__" not in text and "Binary files" not in text

    def test_untracked_new_files_reach_the_patch(self, source_repo, manager,
                                                 tmp_path):
        """回归（2026-09-28 实测）：只新增文件的里程碑，补丁不能是 0 行。

        `git diff HEAD` 看不见未跟踪文件，而"从零建出 index.html"正是内容工作
        最常见的交付形状。补丁是唯一能交接出去的东西 —— 漏掉它，Reviewer 判了
        pass、文件确实在工作区里，accept 却只能说"没有可合入的东西"。
        """
        base = manager.validate_for_submission(
            source_repo, WorkspaceStrategy.GIT_WORKTREE)["base_revision"]
        plan = manager.prepare(runtime_task_id="rt-new",
                               source_path=source_repo,
                               strategy=WorkspaceStrategy.GIT_WORKTREE,
                               base_revision=base, now_iso="now")
        exec_path = Path(plan.execution_workspace_path)
        (exec_path / "index.html").write_text("<html>hi</html>\n",
                                              encoding="utf-8")
        (exec_path / "assets").mkdir()
        (exec_path / "assets" / "site.css").write_text("body{}\n",
                                                       encoding="utf-8")

        result = manager.collect_result(plan, tmp_path / "artifacts_new")
        patch = Path(result["changes_patch"])
        text = patch.read_text(encoding="utf-8")

        assert text.count("new file mode") == 2
        assert "+<html>hi</html>" in text and "+body{}" in text
        # porcelain 把整个新目录折叠成 `?? assets/` —— 不展开就整包漏掉
        assert "assets/site.css" in text
        # 补丁能被源仓库接受，是 accept 的唯一前提
        check = subprocess.run(["git", "apply", "--check", str(patch)],
                               cwd=str(source_repo), capture_output=True,
                               text=True)
        assert check.returncode == 0, check.stderr
        assert git("status", "--porcelain", cwd=source_repo).strip() == ""

    def test_copy_strategy_excludes_heavy_dirs(self, tmp_path, manager):
        """§20：COPY 排除 .git / node_modules 等。"""
        src = tmp_path / "copysrc"
        (src / "node_modules").mkdir(parents=True)
        (src / ".git").mkdir()
        (src / "app.txt").write_text("app", encoding="utf-8")
        (src / "node_modules" / "x.js").write_text("x", encoding="utf-8")
        plan = manager.prepare(runtime_task_id="rt-c",
                               source_path=src,
                               strategy=WorkspaceStrategy.COPY,
                               now_iso="now")
        exec_path = Path(plan.execution_workspace_path)
        assert (exec_path / "app.txt").is_file()
        assert not (exec_path / "node_modules").exists()
        assert not (exec_path / ".git").exists()


# ===========================================================================
# §17/§18/§19：清理策略
# ===========================================================================
class TestCleanup:
    def test_cleanup_refused_unless_safe(self, source_repo, manager,
                                         tmp_path):
        base = manager.validate_for_submission(
            source_repo, WorkspaceStrategy.GIT_WORKTREE)["base_revision"]
        plan = manager.prepare(runtime_task_id="rt-cleanup",
                               source_path=source_repo,
                               strategy=WorkspaceStrategy.GIT_WORKTREE,
                               base_revision=base, now_iso="now")
        with pytest.raises(WorkspacePreparationError, match="未到终态"):
            manager.cleanup(plan, task_terminal=False, lease_active=False,
                            patch_saved=True)
        with pytest.raises(WorkspacePreparationError, match="lease"):
            manager.cleanup(plan, task_terminal=True, lease_active=True,
                            patch_saved=True)
        with pytest.raises(WorkspacePreparationError, match="patch"):
            manager.cleanup(plan, task_terminal=True, lease_active=False,
                            patch_saved=False)
        # 全部安全条件满足 -> 真正清理
        assert manager.cleanup(plan, task_terminal=True, lease_active=False,
                               patch_saved=True) is True
        assert not Path(plan.execution_workspace_path).exists()

    def test_prune_is_safe(self, source_repo, manager):
        assert manager.prune(source_repo) is True   # 不删除有效工作树
        assert (source_repo / "file.txt").is_file()


# ===========================================================================
# §58/§59：scheduler 层的 DIRECT / WORKTREE 冲突语义
# ===========================================================================
class TestConflictSemantics:
    def test_direct_conflict_blocks_second_claim(self, tmp_path):
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        submission = TaskSubmissionService(repo, clock=clock)
        a = submission.submit(Task(goal="a", workspace_path="w://ws"),
                              workspace_strategy="DIRECT")
        b = submission.submit(Task(goal="b", workspace_path="w://other"),
                              workspace_strategy="DIRECT")
        # 先 claim a（此刻两任务 workspace 不同）；翻转在前会互锁死锁
        first = repo.try_acquire_next("w", lease_seconds=120,
                                      max_concurrent=2)
        assert first is not None
        # 竞态：b 的 workspace 被改成与 a 完全一致 -> acquire 期锁挡住
        repo.update_fields(b.runtime_task_id,
                           source_workspace_path=a.workspace_path,
                           workspace_path=a.workspace_path)
        assert repo.try_acquire_next("w2", lease_seconds=120,
                                     max_concurrent=2) is None

    def test_worktree_tasks_same_source_can_both_run(self, tmp_path,
                                                     source_repo):
        """§21/§59：同 source repo 的两个 WORKTREE 任务可同时 RUNNING。"""
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        manager = WorkspaceStrategyManager(worktree_root=tmp_path / "wt")
        submission = TaskSubmissionService(repo, clock=clock,
                                           workspace_manager=manager)
        for i in range(2):
            submission.submit(Task(goal=f"t{i}",
                                   workspace_path=str(source_repo)),
                              workspace_strategy="GIT_WORKTREE")
        leases = [repo.try_acquire_next(f"w{i}", lease_seconds=120,
                                        max_concurrent=2)
                  for i in range(2)]
        assert all(l is not None for l in leases)   # 同 source 不互斥
        assert leases[0].runtime_task_id != leases[1].runtime_task_id

    def test_scheduler_prepares_worktree_and_isolates(self, tmp_path,
                                                      source_repo):
        """端到端：scheduler claim -> 准备 worktree -> 执行 -> 取证。"""
        from mao.scheduler import (RuntimeOutcome, RetryPolicy,
                                   RuntimeScheduler)
        clock = FakeClock()
        repo = TaskRepository(tmp_path / "queue.db", clock=clock)
        manager = WorkspaceStrategyManager(worktree_root=tmp_path / "wt")
        submission = TaskSubmissionService(repo, clock=clock,
                                           workspace_manager=manager)
        rt = submission.submit(Task(goal="worktree task",
                                    workspace_path=str(source_repo)),
                               workspace_strategy="GIT_WORKTREE")

        def factory(*, runtime_dir, config_profile, control, **kw):
            return FakeOrchestrator(control=control,
                                    script=[TaskState.COMPLETED])

        sched = RuntimeScheduler(
            repo, factory, clock=clock, max_concurrent_tasks=1,
            pool_size=0, lease_timeout_seconds=120,
            attempts_root=tmp_path / "rt", worker_id="w",
            workspace_manager=manager)
        tick = sched.tick()
        assert tick.outcome == RuntimeOutcome.COMPLETED
        got = repo.get(rt.runtime_task_id)
        assert got.status == RuntimeStatus.COMPLETED
        assert got.execution_workspace_path != str(source_repo)
        assert (Path(got.execution_workspace_path) / "file.txt").is_file()
        # §16：artifacts 落盘
        artifacts = (tmp_path / "rt" / rt.runtime_task_id /
                     "attempt1" / "artifacts")
        assert (artifacts / "workspace_result.json").is_file()
        record = repo.get_workspace_record(rt.runtime_task_id)
        assert record is not None
        assert record["strategy"] == "GIT_WORKTREE"
        # source repo 保持 clean（§70）
        assert git("status", "--porcelain", cwd=source_repo).strip() == ""
