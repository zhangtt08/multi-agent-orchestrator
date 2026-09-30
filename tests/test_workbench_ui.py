"""前端外壳的守卫测试。

风格可以照参考图抄，数字不行 —— 所以这里测的不是"好不好看"，
是"有没有把不存在的东西印成判据"，以及"看一眼会不会改动现场"。
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Dict

import pytest

from tools import delivery_view as dv
from tools import workbench_ui as ui


def _runner():
    from tools.scheduler_cli import SchedulerRunner

    return SchedulerRunner("config")


@dataclass
class FakeCtx:
    config_dir: str = "config"
    real_roles: bool = True
    default_strategy: str = "GIT_WORKTREE"
    last_form: Dict[str, str] = field(default_factory=dict)
    watch: bool = False
    runner: object = field(default_factory=_runner)


@pytest.fixture()
def empty_cfg(tmp_path, monkeypatch):
    """一份能加载、但队列里什么都没有的 config。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "settings.yaml").write_text(textwrap.dedent("""
        runtime_dir: runtime
        scheduler:
          enabled: true
          db_path: ./runtime_scheduler/queue.db
          attempts_root: runtime
        checkpoint:
          enabled: true
    """), encoding="utf-8")
    (cfg / "agents.yaml").write_text(
        "supervisor:\n  provider: mock_supervisor\n"
        "executor:\n  provider: mock_executor_a\n"
        "reviewer:\n  provider: mock_supervisor\n", encoding="utf-8")
    (cfg / "harness.yaml").write_text("# 空\n", encoding="utf-8")
    monkeypatch.setattr(dv, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "ROOTISH", tmp_path)
    import tools.scheduler_cli as sc
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    return tmp_path


PAGES = ("dashboard", "tasks", "agents", "memory", "workspaces", "settings")
# 参考图里有、这个项目里没有的东西。出现任何一个都等于在编判据。
FABRICATED = ("98%", "T-1040", "Online", "+33%", "vs yesterday",
              "All systems healthy", "5/5", "12 files", "from last hour")


def render(name, ctx):
    return getattr(ui, name)(ctx)


class TestNoInventedNumbers:
    def test_no_reference_mockup_values_leak_into_any_page(self, empty_cfg):
        ctx = FakeCtx(real_roles=False, default_strategy="COPY")
        for name in PAGES:
            page = render(name, ctx)
            leaked = [f for f in FABRICATED if f in page]
            assert not leaked, f"{name} 页面上出现了编造值：{leaked}"

    def test_empty_queue_reports_zero_and_says_so(self, empty_cfg):
        page = render("dashboard", FakeCtx(real_roles=False))
        assert "进行中" in page and "队列为空" in page
        assert "没有 checkpoint 记录" in page or "先跑一条任务才会有" in page

    def test_agent_tile_never_claims_liveness(self, empty_cfg):
        lines = [text for text, _cls in ui.role_lines("config")]
        assert len(lines) == 3
        assert all(("没有该角色的调用记录" in t) or ("调用" in t) for t in lines)
        assert not any("Online" in t for t in lines), lines


class TestReadOnly:
    def test_rendering_every_page_changes_no_bytes(self, empty_cfg):
        from mao.core.models import Task
        from mao.scheduler import (SystemClock, TaskRepository,
                                   TaskSubmissionService)

        repo = TaskRepository(empty_cfg / "runtime_scheduler" / "queue.db",
                              clock=SystemClock())
        try:
            TaskSubmissionService(repo, clock=repo.clock).submit(
                Task(goal="把 multiply 改成返回 a * b，并让测试通过",
                     max_rounds=1))
        finally:
            repo.close()
        db = empty_cfg / "runtime_scheduler" / "queue.db"
        before = db.read_bytes()

        ctx = FakeCtx(real_roles=False, default_strategy="COPY")
        for name in PAGES:
            page = render(name, ctx)
            assert page.startswith("<!doctype html>")
        assert db.read_bytes() == before, "看一眼页面就改动了队列库"

    def test_missing_databases_are_not_created(self, empty_cfg):
        render("dashboard", FakeCtx(real_roles=False))
        render("memory", FakeCtx(real_roles=False))
        assert not (empty_cfg / "memory" / "memory.db").exists()
        assert not (empty_cfg / "runtime_scheduler" / "queue.db").exists()

    def test_user_text_is_escaped(self, empty_cfg):
        from mao.core.models import Task
        from mao.scheduler import (SystemClock, TaskRepository,
                                   TaskSubmissionService)

        repo = TaskRepository(empty_cfg / "runtime_scheduler" / "queue.db",
                              clock=SystemClock())
        try:
            TaskSubmissionService(repo, clock=repo.clock).submit(
                Task(goal="<script>alert(1)</script> 这条需求", max_rounds=1))
        finally:
            repo.close()
        page = render("tasks", FakeCtx(real_roles=False))
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestGoalComesFromTheQueueRow:
    def test_goal_shows_before_any_attempt_artifact(self, empty_cfg):
        """goal 在队列行里就有 —— 不该等到 attempt 落盘才看得见。"""
        from mao.core.models import Task
        from mao.scheduler import (SystemClock, TaskRepository,
                                   TaskSubmissionService)

        repo = TaskRepository(empty_cfg / "runtime_scheduler" / "queue.db",
                              clock=SystemClock())
        try:
            TaskSubmissionService(repo, clock=repo.clock).submit(
                Task(goal="整理 notes/2026-09.md 的三条待办", max_rounds=1))
        finally:
            repo.close()
        rows = dv.board(["config"])
        assert rows and "整理 notes" in rows[0]["goal"]
        assert "整理 notes" in render("dashboard", FakeCtx(real_roles=False))


class TestTilesAreWiredToTheRightSource:
    def test_verification_tile_uses_the_board_row_not_the_view(self, empty_cfg,
                                                               monkeypatch):
        """验证计数住在看板行里（来自 checkpoint）。从 view 上取一个不存在的键
        只会得到 0 —— 屏幕上就是一个看起来像判据的假数。"""
        row = {"runtime_task_id": "rt-1", "status": "COMPLETED", "goal": "修 bug",
               "stage": "TASK_TERMINAL", "calls": {}, "patch_lines": 3,
               "workspace": "", "workspace_here": True, "also_in": [],
               "verification_ran": 2, "verification_failed": 0,
               "submitted_at": "2026-09-28T00:00:00"}
        view = {"runtime_task_id": "rt-1", "status": "COMPLETED",
                "config_dir": "config", "patch_lines": 3, "ws_result":
                {"changed_files": ["calc.py"]}}
        monkeypatch.setattr(dv, "board", lambda *a, **k: [row])
        monkeypatch.setattr(dv, "collect", lambda *a, **k: (view, ""))
        monkeypatch.setattr(dv, "judge", lambda v: {
            "delivered": True, "stable": True, "delivery_label": "已交付",
            "stability_label": "稳定", "delivery": [], "stability": [],
            "conflicts": []})
        page = render("dashboard", FakeCtx(real_roles=False))
        assert "验证命令 2 条，非零退出 0 条" in page
        assert "采集到改动 1 个" in page


class TestShell:
    def test_sidebar_marks_the_current_page_only(self, empty_cfg):
        page = render("tasks", FakeCtx(real_roles=False))
        assert page.count("class='on'") == 1
        assert "href='/ui/agents'" in page           # 六个入口都在

    def test_status_pill_class_matches_the_real_status(self, empty_cfg):
        assert "pill QUEUED" in ui._pill("QUEUED")
        assert "pill BLOCKED" in ui._pill("BLOCKED")


class TestBatchSection:
    """批次那一格：人是在网页上决定要不要授权的，所以它必须只说采集到的事。"""

    def _state(self, root, milestones, final="not-run"):
        import json
        batch = root / "runtime_batch"
        batch.mkdir(parents=True, exist_ok=True)
        (batch / "site.json").write_text(json.dumps(
            {"name": "site", "workspace": str(root), "milestones": milestones,
             "final": {"status": final}}, ensure_ascii=False), encoding="utf-8")
        return batch

    def test_no_state_file_says_no_record(self, empty_cfg):
        page = render("tasks", FakeCtx(real_roles=False))
        assert "批次交付" in page
        assert "没有任何批次状态文件" in page and "不是 0" in page

    def test_runtime_batch_dir_is_not_created_by_looking(self, empty_cfg):
        render("tasks", FakeCtx(real_roles=False))
        assert not (empty_cfg / "runtime_batch").exists()

    def test_patch_line_count_comes_from_the_recorded_file(self, empty_cfg):
        self._state(empty_cfg, {
            "m1": {"status": "awaiting-merge", "runtime_task_id": "rt-a1",
                   "patch": str(empty_cfg / "p.patch"),
                   "demo": {"status": "ok", "exit_code": 0}}})
        (empty_cfg / "p.patch").write_text("a\nb\nc\n", encoding="utf-8")
        page = render("tasks", FakeCtx(real_roles=False))
        assert ">3<" in page, "补丁行数没数自文件本身"
        assert "等你授权合入" in page and "batch_project.py accept" in page
        assert "href='/run/rt-a1'" in page
        assert "exit=0" in page

    def test_missing_patch_is_a_dash_not_a_zero(self, empty_cfg):
        self._state(empty_cfg, {"m1": {"status": "failed", "patch": ""}})
        page = render("tasks", FakeCtx(real_roles=False))
        assert "run --retry m1" in page
        assert ">—<" in page

    def test_pending_milestone_does_not_claim_a_wait(self, empty_cfg):
        self._state(empty_cfg, {"m1": {"status": "pending"},
                                "m2": {"status": "pending"}}, final="pass")
        page = render("tasks", FakeCtx(real_roles=False))
        assert "等你授权合入" not in page
        assert "等上一格被授权" in page
        assert "批次总验收=pass" in page

    def test_the_owner_words_sit_above_the_checklist(self, empty_cfg):
        """核查这一格要同时看得见原话与拆出来那句 —— 漂移只有并排才看得出来。"""
        self._state(empty_cfg, {"m1": {"status": "awaiting-merge"}})
        import json
        p = empty_cfg / "runtime_batch" / "site.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["owner_goal"] = "交付一个可打开的中文演示站点"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        page = render("tasks", FakeCtx(real_roles=False))
        assert "业主原话：交付一个可打开的中文演示站点" in page

    def test_no_owner_goal_prints_no_quote(self, empty_cfg):
        self._state(empty_cfg, {"m1": {"status": "pending"}})
        assert "业主原话" not in render("tasks", FakeCtx(real_roles=False))

    def test_each_batch_is_labeled_by_its_state_file(self, empty_cfg):
        """name 是验收 agent 写的，文件名是框架定的 —— 归档掉的那份也是
        awaiting-merge，只印 name 时两格都写着"等你授权合入"，合错没人拦得住。
        """
        self._state(empty_cfg, {"m1": {"status": "awaiting-merge"}})
        import json
        (empty_cfg / "runtime_batch" / "ARCHIVE-do-not-accept-old.json").write_text(
            json.dumps({"name": "site", "workspace": str(empty_cfg),
                        "milestones": {"m1": {"status": "awaiting-merge"}},
                        "final": {"status": "not-run"}}, ensure_ascii=False),
            encoding="utf-8")
        page = render("tasks", FakeCtx(real_roles=False))
        assert page.count("等你授权合入") == 2
        assert "状态文件=site.json" in page
        assert "状态文件=ARCHIVE-do-not-accept-old.json" in page

    def test_preview_images_and_the_live_workspace_are_pointed_out(self, empty_cfg):
        """核查要看的那几张图早就记在状态文件里了，可这一格从来没印过它们 ——
        记了不印等于没有。demo 字典的键名照真实档抄（exit_code/preview/produced/
        status/tail/workspace，实测自 runtime_batch/showcase-site.json）。
        """
        self._state(empty_cfg, {
            "m1": {"status": "awaiting-merge",
                   "runtime_task_id": "rt-a1",
                   "demo": {"status": "ok", "exit_code": 0,
                            "workspace": str(empty_cfg / "worktree"),
                            "produced": True, "tail": [],
                            "preview": {"status": "ok",
                                        "shots": [str(empty_cfg / "index.png")]}}}})
        page = render("tasks", FakeCtx(real_roles=False))
        assert "核查这一格看这里（m1）" in page
        assert "index.png" in page
        assert "可打开的执行现场" in page
        assert str(empty_cfg / "worktree") in page

    def test_a_slice_without_previews_prints_no_image_line(self, empty_cfg):
        self._state(empty_cfg, {"m1": {"status": "pending", "demo": {}}})
        assert "核查这一格看这里" not in render("tasks", FakeCtx(real_roles=False))

    def test_the_prompt_handed_to_the_executor_is_readable(self, empty_cfg):
        """用户要的是"可见可控"：验收 agent 交给执行 agent 的那段话得能在页面上读到原文。

        状态文件里 prompt / prompt_sha256 这两个键是 batch_project.submit_next 在
        真实提交路径上写的（见 tests/test_batch_project.py::TestPromptIsRecorded）。
        """
        self._state(empty_cfg, {
            "m1": {"status": "awaiting-merge", "runtime_task_id": "rt-a1",
                   "prompt": "本格要做的是：修 multiply()\n验收命令：pytest -q\n"
                             "这是 2 个里程碑里的第 1 个（m1）。",
                   "prompt_sha256": "01108d28afc9" + "0" * 56}})
        page = render("tasks", FakeCtx(real_roles=False))
        assert "交给执行者的提示词（框架组装，逐字）— m1" in page
        assert "验收命令：pytest -q" in page
        assert "sha256=01108d28afc9" in page
        assert "Reviewer 的判定与它交回给执行者的话" in page
        assert "href='/run/rt-a1'" in page
        # 等人授权的那一格，提示词不许藏在一次点击后面
        assert "<details open>" in page

    def test_a_settled_slice_keeps_its_prompt_collapsed(self, empty_cfg):
        """已合入的格子不必展开 —— 展开的是"现在该看的那一格"，不是一屏噪音。"""
        self._state(empty_cfg, {"m1": {"status": "done",
                                       "prompt": "本格要做的是：修 multiply()"}})
        page = render("tasks", FakeCtx(real_roles=False))
        assert "交给执行者的提示词" in page
        assert "<details open>" not in page

    def test_patch_hash_and_demo_output_are_on_the_page(self, empty_cfg):
        """退出码只说"没失败"。授权那一刻要看的是补丁哈希（accept 拿它核对
        "你点的还是刚才那份"）与 demo 实际说了什么。"""
        self._state(empty_cfg, {
            "m1": {"status": "awaiting-merge", "runtime_task_id": "rt-a1",
                   "patch": str(empty_cfg / "p.patch"),
                   "patch_sha256": "c45369398524" + "0" * 52,
                   "demo": {"status": "ok", "exit_code": 0,
                            "tail": ["index.html ['#hero', 'about.html']"]}}})
        (empty_cfg / "p.patch").write_text("a\nb\n", encoding="utf-8")
        page = render("tasks", FakeCtx(real_roles=False))
        assert "sha256=c45369398524" in page
        assert "demo 说了什么" in page
        assert "about.html" in page

    def test_an_accepted_slice_shows_which_commit_it_became(self, empty_cfg):
        """"已合入"不算证据，commit 号才算 —— 审计链要能在看板上走完整。"""
        self._state(empty_cfg, {
            "m1": {"status": "done", "runtime_task_id": "rt-a1",
                   "commit": "3c0254faf9d9" + "0" * 28,
                   "accepted_at": "2026-09-29T12:40:18+0800"}})
        page = render("tasks", FakeCtx(real_roles=False))
        assert "已合入" in page and "3c0254faf9d9" in page
        assert "2026-09-29 12:40:18" in page

    def test_a_slice_without_a_recorded_prompt_prints_no_details(self, empty_cfg):
        self._state(empty_cfg, {"m1": {"status": "pending"}})
        # 锁批次那一格本身，不锁整页 —— 输入区的说明文字里也会出现"交给执行者的
        # 提示词"这几个字，整页断言测的不是这条判据（真红过一次）。
        from tools import workbench_ui as ui
        assert "交给执行者的提示词" not in "\n".join(ui.batch_section())

    def test_the_batch_verdict_shows_up_on_the_page(self, empty_cfg):
        """「项目完成」以前只在 CLI 的 status 里，面板读状态文件所以看不见。"""
        self._state(empty_cfg, {"m1": {"status": "done"}})
        import json
        p = empty_cfg / "runtime_batch" / "site.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["verdict"] = "项目完成"
        d["final"] = {"status": "pass"}
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        page = render("tasks", FakeCtx(real_roles=False))
        assert "批次判定=<b>项目完成</b>" in page
        assert "批次总验收=pass" in page

    def test_no_recorded_verdict_prints_no_verdict(self, empty_cfg):
        self._state(empty_cfg, {"m1": {"status": "pending"}})
        assert "批次判定" not in render("tasks", FakeCtx(real_roles=False))

    def test_a_planned_project_shows_its_checklist_before_any_run(self, empty_cfg):
        """plan 的产物是一张还没跑的清单：不在批次状态里，也不在队列里。
        不在界面上印出来，用户点完"切分"之后拿到的就只是一句"已生成"。
        """
        import json
        d = empty_cfg / "runtime_batch" / "planned"
        d.mkdir(parents=True)
        (d / "site-1234.project.json").write_text(json.dumps({
            "name": "site", "workspace": str(empty_cfg), "strategy": "GIT_WORKTREE",
            "owner_goal": "交付一个可打开的中文演示站点",
            "final_acceptance": {"name": "whole", "command": ["pytest", "-q"]},
            "milestones": [{"id": "m1-skeleton",
                            "goal": "静态首页：index.html + styles.css",
                            "acceptance": "pytest tests/test_structure.py -q"}]},
            ensure_ascii=False), encoding="utf-8")
        page = render("tasks", FakeCtx(real_roles=False))
        assert "界面切出来的项目档" in page
        assert "m1-skeleton" in page
        assert "pytest tests/test_structure.py -q" in page
        assert "交付一个可打开的中文演示站点" in page
        assert "run --project" in page

    def test_no_planned_files_says_so_instead_of_nothing(self, empty_cfg):
        page = render("tasks", FakeCtx(real_roles=False))
        assert "还没有从这一格切分过项目" in page


class TestOneClickRepoAndAgentStrip:
    """2026-09-30 业主的两条原话：「怎么填都不行」与「我在哪里设置调用哪个 agent」。

    两条都不是措辞问题：页面上一个只给终端命令，另一个把答案藏在另一页里。
    """

    def test_the_button_offers_to_do_the_git_step_and_carries_the_sentence(self):
        ctx = FakeCtx()
        ctx.go_init_hint = {"prompt": "创建一个1111文档",
                            "workspace": r"C:\Users\EDY\Desktop\测试",
                            "strategy": "COPY", "max_rounds": "2"}
        page = render("tasks", ctx)
        assert "建仓库并开工" in page
        assert "创建一个1111文档" in page, "原话要带回去，不许让人重敲"
        assert "Desktop\\\\测试" in page or "Desktop\\测试" in page
        assert "git init &amp;&amp;" not in page

    def test_no_button_when_nothing_was_refused(self):
        page = render("tasks", FakeCtx())
        assert "name='init_repo'" not in page, "没被挡住就不许多一个按钮"

    def test_first_screen_answers_which_agent_and_where_the_key_goes(self):
        page = render("tasks", FakeCtx())
        for role in ("supervisor", "executor", "reviewer"):
            assert role in page
        assert "codex_" in page, "要报出真正绑的 profile 名，不是只说\"已配置\""
        assert "API key" in page and "/settings" in page
        assert "不持有密钥" in page

    def test_a_profile_that_resolves_to_nothing_is_named_not_blamed(
            self, empty_cfg):
        (empty_cfg / "config" / "harness.yaml").write_text(
            "nope:\n  command: \"definitely-not-a-cli-xyz\"\n"
            "  prompt_mode: stdin\n  supports_cli: true\n"
            "  supports_file_write: true\n",
            encoding="utf-8")
        (empty_cfg / "config" / "agents.yaml").write_text(
            "supervisor:\n  provider: generic_cli\n  transport: subprocess\n"
            "  harness_profile: nope\n"
            "executor:\n  provider: generic_cli\n  transport: subprocess\n"
            "  harness_profile: nope\n"
            "reviewer:\n  provider: generic_cli\n  transport: subprocess\n"
            "  harness_profile: nope\n", encoding="utf-8")
        page = render("tasks", FakeCtx(config_dir=str(empty_cfg / "config")))
        assert "还没法开工" in page and "没找到" in page
        assert "definitely-not-a-cli-xyz" in page
        assert "harness.yaml" in page, "要给出下一步能做的具体一件"
