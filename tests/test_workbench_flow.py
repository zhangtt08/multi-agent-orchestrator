"""工作流那一页的守卫：每一格都要有出处，读它不许改动现场。

这一页存在的理由是把"人工核查那一秒"换成看得见的工作流，所以它最坏的失败
不是不好看，而是**把没有的东西印成像判据的样子**，或者看一眼就把队列库改了。
"""
from __future__ import annotations

import json
import sqlite3
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

import pytest

from mao.core.models import (AttemptRecord, ExecutionStatus, ReviewStatus,
                             TaskState)
from mao.core.models import Task
from mao.scheduler import (RuntimeStatus, SystemClock, TaskRepository,
                           TaskSubmissionService)
from tools import delivery_view as dv
from tools import workbench_flow as flow
from tools import workbench_ui as ui

FABRICATED = ("98%", "T-1040", "Online", "+33%", "5/5", "12 files",
              "All systems healthy", "from last hour")


def _repo(base: Path) -> TaskRepository:
    """同一个队列库的句柄。测试里反复开合，判据不许因为句柄不同而变。"""
    return TaskRepository(base / "runtime_scheduler" / "queue.db",
                          clock=SystemClock())


def _submission(repo: TaskRepository) -> TaskSubmissionService:
    return TaskSubmissionService(repo, clock=SystemClock())


def _runner():
    from tools.scheduler_cli import SchedulerRunner

    return SchedulerRunner("config")


@dataclass
class FakeCtx:
    config_dir: str = "config"
    real_roles: bool = False
    default_strategy: str = "COPY"
    last_form: Dict[str, str] = field(default_factory=dict)
    last_go: Dict[str, str] = field(default_factory=dict)
    last_plan: Dict[str, str] = field(default_factory=dict)
    runner: object = field(default_factory=_runner)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """一份真配置 + 真队列库 + 一份真形状的 attempt 现场。"""
    c = tmp_path / "config"
    c.mkdir()
    (c / "settings.yaml").write_text(textwrap.dedent("""
        runtime_dir: runtime
        scheduler:
          enabled: true
          db_path: ./runtime_scheduler/queue.db
          attempts_root: runtime
        checkpoint:
          enabled: true
          db_path: runtime/checkpoints.db
    """), encoding="utf-8")
    (c / "agents.yaml").write_text(
        "supervisor:\n  provider: mock_supervisor\n"
        "executor:\n  provider: mock_executor_a\n"
        "reviewer:\n  provider: mock_supervisor\n", encoding="utf-8")
    (c / "harness.yaml").write_text("# 空\n", encoding="utf-8")
    monkeypatch.setattr(dv, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "ROOTISH", tmp_path)
    import tools.scheduler_cli as sc
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    # build_repo_from_config 那一路的 db_path 是按进程 cwd 解析的。不 chdir 的
    # 话，写入动作会打到仓库里那台真实队列库上 —— 测试就成了在生产队列里排话。
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _dump(model) -> Dict:
    return json.loads(model.model_dump_json())


def make_run(tmp_path, status: str = "COMPLETED", rounds=2,
             with_criteria: bool = True):
    """提交一条真队列行，并写出两轮的落盘现场（模型序列化，形状不走样）。"""
    repo = _repo(tmp_path)
    rt = _submission(repo).submit(
        Task(goal="做一个能离线打开的中文演示站，不要动 tests/"))
    repo.update_status(rt.runtime_task_id, RuntimeStatus.RUNNING)
    repo.update_status(rt.runtime_task_id, getattr(RuntimeStatus, status))

    task_dir = (tmp_path / "runtime" / rt.runtime_task_id / "attempt1"
                / f"task_{rt.task_id}")
    (task_dir / "logs").mkdir(parents=True, exist_ok=True)
    (task_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    attempts = [AttemptRecord(round=i,
                              execution_status=ExecutionStatus.SUCCESS,
                              review_status=(ReviewStatus.FAIL if i < rounds
                                             else ReviewStatus.PASS),
                              review_reason=("首页还是英文，标题要对齐需求"
                                             if i < rounds else "满足需求"),
                              summary=f"第 {i} 轮改了 {i} 个文件")
                for i in range(1, rounds + 1)]
    (task_dir / "task.json").write_text(json.dumps({
        "task_id": rt.task_id,
        "goal": "做一个能离线打开的中文演示站，不要动 tests/",
        "constraints": [], "max_rounds": 3,
        "workspace_path": str(tmp_path)}, ensure_ascii=False), encoding="utf-8")
    (task_dir / "state.json").write_text(json.dumps(_dump(
        _State(task_id=rt.task_id, current_round=rounds, max_rounds=3,
               current_state=TaskState.COMPLETED, attempts=attempts)),
        ensure_ascii=False), encoding="utf-8")
    (task_dir / "plan.json").write_text(json.dumps({
        "plan_id": "PLAN-1", "round": rounds,
        "executor_prompt": "把首页改成中文，标题用需求原话，不要动 tests/",
        "acceptance_criteria": ([{"criterion_id": "C1",
                                  "description": "首页能离线打开",
                                  "verification_type": "COMMAND",
                                  "command": "pytest tests/test_m1.py -q"}]
                                 if with_criteria else [])},
        ensure_ascii=False), encoding="utf-8")
    (task_dir / "execution.json").write_text(json.dumps({
        "status": "success", "summary": "改了 index.html 与 zh.py",
        "changed_files": ["index.html", "zh.py"],
        "commands_run": [{"command": "pytest tests/test_m1.py -q",
                          "exit_code": 0, "output_excerpt": "1 passed"}],
        "tests": [], "remaining_issues": [], "errors": [],
        "evidence": {}}, ensure_ascii=False), encoding="utf-8")
    (task_dir / "review.json").write_text(json.dumps({
        "status": "pass", "round": rounds, "reason": "满足需求",
        "root_cause": "", "next_prompt": "",
        # 判据字段叫 satisfied，不叫 passed —— delivery_view 按 satisfied 数
        "passed_checks": [{"criterion_id": "C1", "satisfied": True,
                           "evidence": "退出码 0"}],
        "failed_checks": []}, ensure_ascii=False), encoding="utf-8")
    calls = [
        {"role": "executor", "round": 1, "harness": "codex",
         "provider": "codex_cli", "exit_code": 0, "duration_ms": 1234,
         "response_valid": True},
        {"role": "reviewer", "round": 1, "harness": "qoder",
         "provider": "qoder_cli", "exit_code": 0, "duration_ms": 456,
         "response_valid": True, "error_type": ""},
        {"role": "executor", "round": 2, "harness": "codex",
         "provider": "codex_cli", "exit_code": 0, "duration_ms": 999,
         "response_valid": False, "error_type": "contract_invalid"},
    ]
    (task_dir / "logs" / "agent_calls.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in calls) + "\n",
        encoding="utf-8")
    # changes.patch 落在 attempt 目录的 artifacts 下（不是 task 目录）——
    # delivery_view 就是按这个位置找它的。
    (task_dir.parent / "artifacts").mkdir(parents=True, exist_ok=True)
    # 框架取证的那一份：delivery_view 的改动文件数与执行工作区都从它读
    # "verification" 是**框架自己跑过**的那一份（`evidence.py` 写进
    # workspace_result.json 的同一个块），不是 execution.commands_run 那句自述
    # —— 地雷 45。缺了它，这一格就没有"框架实测退出码"这件事，页面只能判
    # "未确认"；测试要造的是它依赖的那份现场，不是把断言改松。
    (task_dir.parent / "artifacts" / "workspace_result.json").write_text(
        json.dumps({"changed_files": ["index.html", "zh.py"],
                    "execution_workspace_path": str(task_dir),
                    "verification": [{
                        "name": "acceptance-1",
                        "command_display": "pytest tests/test_m1.py -q",
                        "exit_code": 0}]},
                   ensure_ascii=False), encoding="utf-8")
    (task_dir.parent / "artifacts" / "changes.patch").write_text(
        "--- a/index.html\n+++ b/index.html\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8")
    repo.close()
    return rt, task_dir


def _State(**kw):
    """只用来序列化 —— 模型带默认值，构造不出半个字段就写不全现场。"""
    from mao.core.models import State

    return State.model_construct(**kw)


class TestFlowRendersRealEvidence:
    def test_goal_rounds_and_verdicts_are_on_the_page(self, cfg):
        rt, _ = make_run(cfg)
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "做一个能离线打开的中文演示站" in page
        assert "第 1 轮" in page and "第 2 轮" in page
        assert "2 / 3" in page                     # current_round / max_rounds
        assert "首页还是英文，标题要对齐需求" in page
        assert "改了 index.html 与 zh.py" in page or "index.html" in page
        assert "pytest tests/test_m1.py -q" in page
        assert "exit=0" in page
        # 补丁行数是真的从 changes.patch 数出来的，不是 0 也不是编的
        assert "5 行" in page

    def test_delivery_tile_follows_the_evidence_not_the_mood(self, cfg):
        """同一句需求，有验收判据判"已交付"，没有判据就判"未确认"。

        这条是那一卡的判别力测试：卡片若写死任何一种结论，这里必红。
        判据读 collect() 的字段，不读整页字符串 —— 页面上别处也可能出现
        "已交付"这三个字（冲突说明里就会引用它）。
        """
        full, _ = make_run(cfg)
        data = flow.collect(full.runtime_task_id, "config")
        assert data["delivery_label"] == "已交付"
        assert data["delivery_label"] in flow.render(data)

        bare, _ = make_run(cfg, with_criteria=False)
        bdata = flow.collect(bare.runtime_task_id, "config")
        assert bdata["delivery_label"] == "未确认", bdata["conflicts"]
        assert bdata["delivery_label"] in flow.render(bdata)

    def test_the_page_shows_why_not_just_the_label(self, cfg):
        """结论下面要跟着逐条判据 —— 只剩一个"未确认"就等于把人换成了一个字。"""
        rt, _ = make_run(cfg)
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "交付判据（逐条，含来源）" in page
        assert "队列终态 = COMPLETED" in page
        assert "Reviewer 判定：pass" in page
        assert "框架实际跑了 1 条验证命令" in page

    def test_no_invented_values_leak_in(self, cfg):
        rt, _ = make_run(cfg)
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        leaked = [f for f in FABRICATED if f in page]
        assert not leaked, leaked

    def test_cost_is_never_displayed(self, cfg):
        """成本从来没被采集过 —— 这一页不许出现"花了多少钱/多少 token"。"""
        rt, _ = make_run(cfg)
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "成本从来没被采集过" in page
        for word in ("token", "费用", "$"):
            assert word not in page.replace("成本", ""), word

    def test_invalid_response_is_shown_as_invalid(self, cfg):
        rt, _ = make_run(cfg)
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "不合格" in page and "contract_invalid" in page

    def test_older_rounds_do_not_claim_the_latest_artifacts(self, cfg):
        """只有最新一轮能挂上那三份落盘产物；早轮写"最新一份"就是假话。"""
        rt, _ = make_run(cfg)
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        first, rest = page.split("第 1 轮", 1)
        assert "最新一份落盘产物" not in rest.split("第 2 轮")[0]
        assert "最新一份落盘产物" in rest

    def test_missing_run_is_reported_and_no_db_is_created(self, cfg):
        data = flow.collect("rt-does-not-exist", "config")
        assert data["ok"] is False
        page = flow.render(data)
        assert "看不到这条运行" in page
        assert not (cfg / "runtime_scheduler" / "queue.db").exists()


class TestDirectivesOnThePage:
    def test_queued_directive_shows_as_queued(self, cfg):
        rt, _ = make_run(cfg, status="RUNNING")
        repo = _repo(cfg)
        repo.add_directive(rt.runtime_task_id, "只要中文界面")
        repo.close()
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "只要中文界面" in page and "还在排队" in page
        assert "第 1 轮已生效" not in page

    def test_applied_directive_shows_which_round_used_it(self, cfg):
        rt, _ = make_run(cfg, status="RUNNING")
        repo = _repo(cfg)
        did = repo.add_directive(rt.runtime_task_id, "先别动 tests/")
        repo.take_directives(rt.runtime_task_id, round_no=2)
        repo.close()
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "先别动 tests/" in page
        assert "第 2 轮已生效" in page
        assert "还在排队" not in page

    def test_steer_refuses_a_terminal_run_and_queues_nothing(self, cfg):
        from tools.workbench import steer_task

        rt, _ = make_run(cfg, status="COMPLETED")
        text, bad = steer_task("config", rt.runtime_task_id, "再补一句")
        assert bad is True and "COMPLETED" in text
        repo = _repo(cfg)
        assert repo.pending_directives(rt.runtime_task_id) == []
        repo.close()

    def test_steer_on_unknown_id_does_not_create_a_directive(self, cfg):
        from tools.workbench import steer_task

        make_run(cfg)
        text, bad = steer_task("config", "rt-none", "随便一句")
        assert bad is True and "没有这条运行" in text

    def test_control_actions_map_to_the_real_transitions(self, cfg):
        from tools.workbench import control_task

        rt, _ = make_run(cfg, status="RUNNING")
        text, bad = control_task("config", rt.runtime_task_id, "pause")
        assert bad is False and "下一个轮次边界" in text
        repo = _repo(cfg)
        assert repo.get(rt.runtime_task_id).pause_requested is True
        repo.close()

        text, bad = control_task("config", rt.runtime_task_id, "resume")
        assert bad is True, text          # RUNNING 不是 PAUSED，恢复要拒

    def test_terminal_run_disables_the_three_actions(self, cfg):
        rt, _ = make_run(cfg, status="FAILED")
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert page.count("disabled") == 3
        assert "已是终态 FAILED" in page


class TestHttpRoutes:
    """真 HTTP 上过一遍：按钮提交的表单与服务器的回执都要是真的。

    浏览器上的失败形态是"连接被掐断、什么都看不到"（AGENTS.md 地雷 28），
    所以这一组不 replaced by 函数级调用。
    """

    @pytest.fixture()
    def server(self, cfg):
        import threading
        import tools.workbench as wb

        monkey = pytest.MonkeyPatch()
        monkey.setattr(wb, "ROOT", cfg)
        ctx = wb.Workbench(config_dir="config", runner=_runner(),
                           real_roles=False, db_rel="./runtime_scheduler/queue.db")
        httpd = wb.serve(ctx, 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
        httpd.server_close()
        monkey.undo()

    def _post(self, url, form, origin=""):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=form.encode("utf-8"),
                                     headers={"Origin": origin} if origin else {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode("utf-8"), resp.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), url

    def test_the_root_lands_on_the_place_where_you_type(self, server):
        """业主原话："现在的软件上手根本不知道从哪里做起"。

        根路径以前落在仪表盘 —— 那一页没有提示词框、没有落地目录、也没有
        『建仓库并开工』，第一眼是"进行中 0"。现在打开就是能打字那一格，
        而仪表盘没有因此失联（导航那一格仍在 `/ui`）。
        """
        import re

        import urllib.request

        with urllib.request.urlopen(server + "/") as r:
            body = r.read().decode("utf-8")
            assert r.geturl().endswith("/ui/tasks"), r.geturl()
        # 判据是"那一页真的有一个可以打字的地方"，不是某个标签的措辞。
        assert re.search(r"<textarea[^>]*name=['\"]goal['\"]", body), body[:400]

        with urllib.request.urlopen(server + "/ui") as r:
            dash = r.read().decode("utf-8")
        assert "仪表盘" in dash


    def test_flow_page_serves_over_http(self, server, cfg):
        import urllib.request

        rt, _ = make_run(cfg, status="RUNNING")
        with urllib.request.urlopen(server + f"/ui/flow/{rt.runtime_task_id}") as r:
            page = r.read().decode("utf-8")
        assert r.status == 200
        assert "两个 agent 的往返" in page
        assert "做一个能离线打开的中文演示站" in page

    def test_bare_flow_link_lands_on_the_newest_run(self, server, cfg):
        """表单里那句"去看工作流"没有 id —— 它必须落到最近那一跑，不是 400。"""
        import urllib.request

        rt, _ = make_run(cfg, status="RUNNING")
        with urllib.request.urlopen(server + "/ui/flow") as r:
            page = r.read().decode("utf-8")
        assert rt.runtime_task_id in r.geturl()
        assert "两个 agent 的往返" in page

    def test_bare_flow_link_says_so_when_there_is_nothing_to_show(self, server,
                                                                  cfg):
        import urllib.request

        with urllib.request.urlopen(server + "/ui/flow/") as r:
            page = r.read().decode("utf-8")
        assert "还没有任何运行" in r.geturl() or "还没有任何运行" in page

    def test_batch_steer_button_reaches_the_state_file(self, server, cfg,
                                                       monkeypatch):
        """批次卡上那个框：一句话要同时留下批次层面的记录与正在跑那一格的收件箱。"""
        import json as _json

        from tools import batch_project as bp

        rt, _ = make_run(cfg, status="RUNNING")
        state_dir = cfg / "runtime_batch"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(bp, "STATE_DIR", state_dir)
        name = "面板批次"
        (state_dir / f"{name}.json").write_text(_json.dumps(
            {"name": name, "workspace": str(cfg), "milestones": {
                "m1": {"status": "running", "runtime_task_id":
                       rt.runtime_task_id}}}, ensure_ascii=False),
            encoding="utf-8")
        code, _body, url = self._post(
            server + "/batch-steer",
            f"state={name}.json&say=dark+theme+please", origin=server)
        assert code == 200 and "notice=" in url and "bad=1" not in url
        saved = _json.loads((state_dir / f"{name}.json").read_text(
            encoding="utf-8"))
        assert [d["text"] for d in saved.get("directives") or []] == \
               ["dark theme please"]
        repo = _repo(cfg)
        try:
            assert [d["text"] for d in
                    repo.pending_directives(rt.runtime_task_id)] == \
                   ["dark theme please"]
        finally:
            repo.close()

    def test_flow_page_rejects_unsafe_id(self, server):
        import urllib.error
        import urllib.request

        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(server + "/ui/flow/..%2F..%2Fetc")
        assert exc.value.code == 400

    def test_steer_form_queues_the_directive_for_real(self, server, cfg):
        rt, _ = make_run(cfg, status="RUNNING")
        code, _body, url = self._post(
            server + "/steer",
            f"runtime_task_id={rt.runtime_task_id}&config=config"
            f"&text={rt.runtime_task_id}",
            origin=server)
        assert code == 200 and "notice=" in url
        repo = _repo(cfg)
        pending = repo.pending_directives(rt.runtime_task_id)
        repo.close()
        assert [d["text"] for d in pending] == [rt.runtime_task_id]

    def test_steer_without_origin_is_refused_and_writes_nothing(self, server, cfg):
        rt, _ = make_run(cfg, status="RUNNING")
        code, page, _ = self._post(
            server + "/steer",
            f"runtime_task_id={rt.runtime_task_id}&text=随便改点什么")
        assert code == 403 and "非同源" in page
        repo = _repo(cfg)
        assert repo.pending_directives(rt.runtime_task_id) == []
        repo.close()

    def test_steer_for_another_config_is_refused(self, server, cfg):
        rt, _ = make_run(cfg, status="RUNNING")
        code, _body, url = self._post(
            server + "/steer",
            f"runtime_task_id={rt.runtime_task_id}&config=config_p9&text=换台配置",
            origin=server)
        assert code == 200 and "bad=1" in url
        repo = _repo(cfg)
        assert repo.pending_directives(rt.runtime_task_id) == []
        repo.close()


class TestReadOnlyRendering:
    def test_rendering_the_flow_page_changes_no_bytes(self, cfg):
        rt, _ = make_run(cfg)
        db = cfg / "runtime_scheduler" / "queue.db"
        repo = _repo(cfg)
        repo.add_directive(rt.runtime_task_id, "顺手排一句")
        repo.close()
        before = db.read_bytes()
        for _ in range(2):
            flow.render(flow.collect(rt.runtime_task_id, "config"))
            ui.tasks(FakeCtx())
        assert db.read_bytes() == before

    def test_stage_ladder_only_counts_committed(self, cfg):
        rt, _ = make_run(cfg)
        db = cfg / "runtime" / "checkpoints.db"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE checkpoint_records (checkpoint_id TEXT PRIMARY KEY,"
            " task_id TEXT, runtime_task_id TEXT, attempt INTEGER, "
            "round_no INTEGER, stage TEXT, status TEXT, created_at TEXT, "
            "committed_at TEXT)")
        con.execute("INSERT INTO checkpoint_records (checkpoint_id, task_id,"
                    " runtime_task_id, round_no, stage, status, created_at)"
                    " VALUES ('CP1', ?, ?, 1, 'EXECUTION_COMPLETED',"
                    " 'COMMITTED', '2026-09-29T01:00:00')",
                    (rt.task_id, rt.runtime_task_id))
        con.execute("INSERT INTO checkpoint_records (checkpoint_id, task_id,"
                    " runtime_task_id, round_no, stage, status, created_at)"
                    " VALUES ('CP2', ?, ?, 1, 'REVIEW_COMPLETED',"
                    " 'PREPARING', '2026-09-29T01:01:00')",
                    (rt.task_id, rt.runtime_task_id))
        con.commit()
        con.close()

        stages = flow.committed_stages("config", rt.task_id,
                                       rt.runtime_task_id)
        assert [s["stage"] for s in stages] == ["EXECUTION_COMPLETED"]
        page = flow.render(flow.collect(rt.runtime_task_id, "config"))
        assert "执行 Agent 交回结果<small>到达</small>" in page
        # PREPARING 的那一条不许被画成到达 —— 否则进度条把"写到一半"说成"做到了"
        assert "验收 Agent 判完<small>未到达</small>" in page
        assert "共 1 条" in page
