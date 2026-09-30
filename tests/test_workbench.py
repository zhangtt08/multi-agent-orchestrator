"""工作台外壳的守卫测试。

被测的不是"网页好不好看"，是四条边界：
提交走的是不是产品那条路、错误是不是一句话说清、POST 是不是只认同源、
以及**不许因为一个网页就多引一个依赖**。
"""
from __future__ import annotations

import ast
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from tools import workbench as wb

STDLIB_OK = {"__future__", "argparse", "html", "os", "subprocess", "sys",
             "threading", "time", "dataclasses", "datetime", "http.server",
             "pathlib", "typing", "urllib", "json", "sqlite3", "http"}
PROJECT_OK = {"mao", "tools"}


def _post(url: str, form: str, origin: str = "") -> tuple[int, str]:
    req = urllib.request.Request(url, data=form.encode("utf-8"), method="POST")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """起一个真服务器，但把提交与调度都换成可控的替身。"""
    started: list[list[str]] = []
    import tools.scheduler_cli as sc
    monkeypatch.setattr(wb, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "ROOT", tmp_path)     # 子进程日志的根在 scheduler_cli

    def fake_submit(config_dir, fields):
        goal = str(fields.get("goal", "")).strip()
        if len(goal) < 10:
            return "", "需求至少要写 10 个字"
        if str(fields.get("workspace", "")).strip() and not Path(
                str(fields["workspace"])).is_dir():
            return "", "workspace 路径不存在或不是目录：" + fields["workspace"]
        return "rt-fake0001", ""

    def fake_cmd(config_dir, python_exe=None):
        started.append([sys.executable, "-c",
                        "import time,sys;print('tick',flush=True);"
                        "time.sleep(30)"])
        return started[-1]

    monkeypatch.setattr(wb, "submit_task", fake_submit)
    ctx = wb.Workbench(
        config_dir="config",
        runner=wb.SchedulerRunner("config", cmd_factory=fake_cmd),
        real_roles=False, db_rel="./q.db")
    httpd = wb.serve(ctx, 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", ctx, httpd, started
    ctx.runner.stop()
    httpd.shutdown()
    httpd.server_close()


class TestBootsFromAnyCwd:
    """桌面版以脚本方式起 `python tools/workbench.py`，那时 sys.path[0] 是 tools/ 本身。

    `from tools import local_env, role_wiring` 一旦排在 sys.path.insert 之前，双击
    打开的窗口就是 ModuleNotFoundError: No module named 'tools' —— 网页从没起来过，
    而 pytest 里永远看不见这一条：pytest 已经把仓库根放进 sys.path 了。
    """

    def test_help_from_a_foreign_cwd(self, tmp_path):
        script = Path(wb.__file__).resolve()
        proc = subprocess.run([sys.executable, str(script), "--help"],
                              cwd=str(tmp_path), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        assert proc.returncode == 0, proc.stderr
        assert "No module named" not in proc.stderr
        assert "usage" in proc.stdout.lower()


class TestNoNewDependency:
    def test_only_stdlib_and_project_are_imported(self):
        """产品宣称"零服务依赖"。工作台多引一个 web 框架，这句话就作废了。"""
        tree = ast.parse(Path(wb.__file__).read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                used.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                used.add(node.module.split(".")[0])
        bad = {m for m in used
               if m not in STDLIB_OK and not any(
                   m == p or m.startswith(p + ".") for p in PROJECT_OK)}
        assert not bad, f"工作台引入了新依赖：{sorted(bad)}"


class TestPureHelpers:
    def test_same_origin_accepts_only_the_pages_own_host(self):
        assert wb.same_origin("http://127.0.0.1:8765", "127.0.0.1:8765")
        assert not wb.same_origin("", "127.0.0.1:8765")
        assert not wb.same_origin("http://evil.example", "127.0.0.1:8765")
        # 端口不同就不是同源：8765 的页面不能往 8766 提交
        assert not wb.same_origin("http://127.0.0.1:1", "127.0.0.1:8765")

    def test_run_key_is_confined_to_id_characters(self):
        for bad in ("", "../etc", "rt 1", "a" * 65, "rt-x?y=1"):
            assert wb.safe_key(bad) is False, bad
        assert wb.safe_key("rt-12ddf644a36a") is True

    def test_scheduler_command_is_an_argv_list_not_a_string(self):
        argv = wb.scheduler_command("config", python_exe="py")
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[:1] == ["py"] and "scheduler" in argv
        assert argv[-2:] == ["--config-dir", "config"]

    def test_empty_database_is_reported_as_not_yet_created(self):
        """"还没建" 与 "调度层未启用" 是两回事，说错会让人去改配置。"""
        label = wb._db_label("./runtime_x/queue.db")
        assert "还没建" in label
        assert wb._db_label("") .startswith("（")


class TestSubmitBoundary:
    def test_short_goal_is_rejected_before_any_queue_write(self):
        # 校验必须在装配 repo 之前：一次都没打开队列库，才谈得上"没写进去"
        calls: list[str] = []
        monkey = _stub_repo(calls)
        try:
            monkey.enter("config")
            rt, err = wb.submit_task("nope", {"goal": "改一下"})
        finally:
            monkey.exit()
        assert rt == "" and "10 个字" in err
        assert calls == [], "校验失败却已经把队列库打开了"

    def test_missing_workspace_names_the_path(self, tmp_path):
        monkey = _stub_repo([])
        try:
            monkey.enter("config")
            rt, err = wb.submit_task("config", {
                "goal": "把 multiply 的返回值改成 a * b",
                "workspace": str(tmp_path / "不存在")})
        finally:
            monkey.exit()
        assert rt == "" and "不存在" in err and "不是目录" in err

    def test_blank_workspace_is_refused_not_resolved_to_cwd(self):
        """留空 ≠ 没有工作区。Path('').resolve() 会落到调度器的当前目录，
        而本机那就是这个仓库本身 —— 那扇门上必须挂住。"""
        rt, err = wb.submit_task("config", {
            "goal": "把 multiply 的返回值改成 a * b", "workspace": "",
            "strategy": "COPY"})
        assert rt == ""
        assert "当前目录" in err and "必填" in err

    def test_the_word_for_none_is_not_treated_as_a_path(self):
        rt, err = wb.submit_task("config", {
            "goal": "把 multiply 的返回值改成 a * b", "workspace": "无"})
        assert rt == "" and "真的空着" in err

    def test_unknown_strategy_is_rejected(self):
        monkey = _stub_repo([])
        try:
            monkey.enter("config")
            rt, err = wb.submit_task("config", {
                "goal": "把 multiply 的返回值改成 a * b", "strategy": "MERGE"})
        finally:
            monkey.exit()
        assert rt == "" and "MERGE" not in err and "GIT_WORKTREE" in err


class _Stub:
    """把 load_config / build_repo / build_service 换成不碰磁盘的替身。"""

    def __init__(self, calls):
        self.calls = calls
        self.saved = {}

    def enter(self, config_dir):
        import mao.core.config as core_config
        import tools.scheduler_cli as cli

        calls = self.calls

        class FakeRepo:
            def close(self):
                calls.append("close")

        class FakeService:
            def submit(self, task, **kw):
                calls.append("submit")

                class RT:
                    runtime_task_id = "rt-stub1"
                return RT()

        self.saved = {
            "load": core_config.load_config,
            "repo": cli.build_repo_from_config,
            "svc": cli.build_submission_service,
        }
        core_config.load_config = lambda d, **k: object()
        cli.build_repo_from_config = lambda c: (calls.append("repo"),
                                                FakeRepo())[1]
        cli.build_submission_service = lambda c, r: FakeService()

    def exit(self):
        import mao.core.config as core_config
        import tools.scheduler_cli as cli

        core_config.load_config = self.saved["load"]
        cli.build_repo_from_config = self.saved["repo"]
        cli.build_submission_service = self.saved["svc"]


def _stub_repo(calls):
    return _Stub(calls)


class TestHttpShell:
    def test_input_box_lives_on_the_tasks_page(self, server):
        """输入面只有一处：任务那一格。根路径不是第二份表单，而是走过去的那扇门。

        这条原来锁的是"首页=仪表盘、首页上没有表单"—— 表单只许有一处，避免同一个
        判断各写一遍。业主那句"现在的软件上手根本不知道从哪里做起"改了**落地页**
        的选择：`/` 现在重定向到任务那一格。不变的是仍然只有一处表单，所以判据
        从"首页里不能有表单"改成"仪表盘里不能有第二份表单"—— 放宽的是路由，
        没有放宽那条真正要紧的约束。
        """
        base, ctx, httpd, _ = server
        with urllib.request.urlopen(base + "/ui/tasks") as resp:
            page = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "name='goal'" in page, "输入需求的地方不见了"
        assert "action='/submit'" in page

        with urllib.request.urlopen(base + "/") as landing:
            landed = landing.read().decode("utf-8")
            assert landing.geturl().endswith("/ui/tasks"), landing.geturl()
        assert "127.0.0.1" in landed, "监听范围要写在人落地的那一页上"

        with urllib.request.urlopen(base + "/ui") as dash:
            d = dash.read().decode("utf-8")
        assert "仪表盘" in d and "name='goal'" not in d, "仪表盘不许长出第二份表单"
        assert "127.0.0.1" in d, "监听范围也要写在仪表盘上"
        del ctx, httpd

    def test_post_without_origin_is_refused(self, server):
        base, _, _, _ = server
        code, page = _post(base + "/submit", "goal=把 multiply 改成返回 a * b")
        assert code == 403
        assert "非同源" in page

    def test_submit_redirects_to_the_run_page(self, server):
        """303 而不是 200：刷新一个提交页不该再提交一次（会再花一次额度）。"""
        base, _, _, _ = server

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(
            base + "/submit",
            data="goal=把 multiply 改成返回 a * b&strategy=COPY".encode(),
            method="POST", headers={"Origin": _origin(base)})
        try:
            opener.open(req)
            pytest.fail("提交后直接回了 200，没有走 303")
        except urllib.error.HTTPError as exc:
            assert exc.code == 303
            assert exc.headers["Location"] == "/run/rt-fake0001?" \
                "config=config", "跳转目标不是这条运行"

    def test_bad_workspace_comes_back_as_one_sentence(self, server):
        base, _, _, _ = server
        req = urllib.request.Request(
            base + "/submit",
            data=b"goal=%E6%8A%8A%20multiply%20%E6%94%B9%E6%88%90%20a%20%2A%20b"
                 b"&workspace=C%3A%5Cnope",
            method="POST", headers={"Origin": _origin(base)})
        with urllib.request.urlopen(req) as resp:
            page = resp.read().decode("utf-8")
        assert "workspace 路径不存在" in page, "错误没有回到页面上，用户只看到刷新"

    def test_unsafe_run_id_is_rejected_not_passed_down(self, server):
        base, _, _, _ = server
        try:
            urllib.request.urlopen(base + "/run/..%2F..%2Fetc")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            return
        pytest.fail("非法 id 被放进了查询路径")

    def test_start_uses_a_child_process_and_keeps_output_on_disk(self, server):
        base, ctx, _, started = server
        req = urllib.request.Request(
            base + "/scheduler", data=b"action=start", method="POST",
            headers={"Origin": _origin(base)})
        with urllib.request.urlopen(req):
            pass
        assert ctx.runner.running(), "点了启动但子进程不在"
        assert started and started[0][0] == sys.executable
        deadline = time.time() + 10
        while time.time() < deadline and not ctx.runner.tail():
            time.sleep(0.2)
        assert "tick" in "".join(ctx.runner.tail()), "子进程输出没落到日志文件里"
        code, page = _post(base + "/scheduler", "action=stop",
                           origin=_origin(base))
        assert code == 200
        assert not ctx.runner.running()

    def test_double_start_does_not_spawn_a_second_worker(self, server):
        base, ctx, _, started = server
        for _ in range(2):
            _post(base + "/scheduler", "action=start", origin=_origin(base))
            time.sleep(0.1)
        assert len(started) == 1


    def test_form_preselects_the_configs_own_strategy(self):
        """写死 GIT_WORKTREE = 给没有 git 仓库的人预设一条必然被拒的提交。"""
        page = wb.render_form("config", default_strategy="COPY")
        assert "<option value='COPY' selected>" in page
        assert "<option value='GIT_WORKTREE'>" in page
        assert "default_strategy = COPY" in page

    def test_rejected_submit_keeps_what_was_typed(self, server):
        base, _, _, _ = server
        with urllib.request.urlopen(urllib.request.Request(
                base + "/submit",
                data=("goal=" + urllib.parse.quote("太短了")
                      + "&constraints=不得改%20tests").encode(),
                method="POST", headers={"Origin": _origin(base)})) as resp:
            page = resp.read().decode("utf-8")     # 303 已被跟到 /
        assert "太短了" in page, "拒绝把用户刚写的需求清空了"
        assert "不得改 tests" in page
        # 回填是一次性的：再看一次输入面就该是空表单
        with urllib.request.urlopen(base + "/ui/tasks") as again:
            assert "太短了" not in again.read().decode("utf-8")

    def test_rejection_names_the_page_field_not_a_cli_flag(self, tmp_path):
        from mao.scheduler import SubmissionError

        calls: list[str] = []
        monkey = _stub_repo(calls)

        def raise_submission_error(config, repo):
            class S:
                def submit(self, task, **kw):
                    raise SubmissionError(
                        "GIT_WORKTREE 策略要求显式 --workspace（source repo）")
            return S()

        import tools.scheduler_cli as cli

        monkey.enter("config")
        saved = cli.build_submission_service
        cli.build_submission_service = raise_submission_error
        try:
            # workspace 给一个真实存在的目录：要测的是**提交层**那句 CLI 口吻的
            # 拒绝被翻译成人，而不是被边界上更早的那道校验挡住
            rt, err = wb.submit_task("config", {
                "goal": "把 multiply 的返回值改成 a * b",
                "workspace": str(tmp_path), "strategy": "GIT_WORKTREE"})
        finally:
            cli.build_submission_service = saved
            monkey.exit()
        assert rt == ""
        assert "workspace 那一栏" in err, "网页上把 CLI flag 当成字段名指路"

    def test_goal_is_escaped_when_echoed_back(self, server):
        base, _, _, _ = server
        form = ("goal=" + urllib.parse.quote("<b>太短</b>")
                + "&constraints=" + urllib.parse.quote("<script>alert(1)</script>"))
        req = urllib.request.Request(base + "/submit", data=form.encode(),
                                     method="POST",
                                     headers={"Origin": _origin(base)})
        with urllib.request.urlopen(req) as resp:
            page = resp.read().decode("utf-8")
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page, "回填没转义"
        assert "<script>alert(1)</script>" not in page


def _origin(base: str) -> str:
    return base


class TestBatchSection:
    """面板上的批次段是只读投影 —— 推进必须回命令行签字。"""

    def test_empty_state_points_at_a_starting_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        text = wb.render_batches()
        assert "batch_project.py run" in text and "还没有批次" in text

    def test_awaiting_merge_is_shown_as_the_human_gate(self, tmp_path, monkeypatch):
        import json

        d = tmp_path / "runtime_batch"
        d.mkdir()
        (d / "notes.json").write_text(json.dumps({
            "name": "notes", "workspace": "C:/x",
            "milestones": {"m1": {"status": "done"},
                           "m2": {"status": "awaiting-merge"}},
            "final": {"status": "not-run"}}, ensure_ascii=False),
            encoding="utf-8")
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        text = wb.render_batches()
        assert "notes" in text and "1/2" in text
        assert "等你同意合入" in text and "accept" in text and "--yes" in text
        assert "项目完成" not in text


class TestPlanFromThePanel:
    """业主对 loop 的第一句是"用户输入提示词 → 验收 agent 切分"，
    而 `plan` 此前只有命令行入口。这一格补的是入口，判定仍在 batch_project.plan。
    """

    def test_short_goal_is_refused_before_any_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        text, bad = wb.plan_from_form("examples/config_minimal", True,
                                      {"goal": "做个网站", "workspace": ""})
        assert bad and "10 个字" in text
        assert not (tmp_path / "runtime_batch" / "planned").exists()

    def test_missing_workspace_is_refused_and_says_why(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        text, bad = wb.plan_from_form(
            "examples/config_minimal", True,
            {"goal": "交付一个中文演示站点，分三步各自可验收", "workspace": ""})
        assert bad and "落地目录" in text

    def test_nonexistent_workspace_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        text, bad = wb.plan_from_form(
            "examples/config_minimal", True,
            {"goal": "交付一个中文演示站点，分三步各自可验收",
             "workspace": str(tmp_path / "nope")})
        assert bad and "不存在" in text

    def test_a_crash_inside_plan_becomes_a_notice_not_a_500(
            self, tmp_path, monkeypatch):
        """`batch_project.plan` 只吞 BatchError。真实档最常见的失败是网络
        （第一次 m1 就是 api.openai.com 直连不通），那会一路抛到浏览器。
        网页不能因为一条坏输入而挂掉 —— 这条规矩 `submit_task` 早就立了。"""
        from tools import batch_project as bp

        def boom(*a, **k):
            raise RuntimeError("Connection to api.openai.com refused")

        monkeypatch.setattr(bp, "plan", boom)
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        ws = tmp_path / "site"
        ws.mkdir()
        text, bad = wb.plan_from_form(
            "config", False,
            {"goal": "交付一个中文演示站点，分三步各自可验收", "workspace": str(ws)})
        assert bad and "切分失败" in text and "RuntimeError" in text

    def test_mock_tier_rejects_without_writing_a_project_file(
            self, tmp_path, monkeypatch):
        """Mock Supervisor 答不出项目档 —— 这是已知设计。要验的是：
        被拒的时候一个字都不写，且把拒绝原因原样带回输入面，不是一句"失败"。"""
        monkeypatch.setattr(wb, "ROOT", tmp_path)
        ws = tmp_path / "site"
        ws.mkdir()
        text, bad = wb.plan_from_form(
            "examples/config_minimal", True,
            {"goal": "交付一个中文演示站点，分三步各自可验收",
             "workspace": str(ws)})
        assert bad, "Mock 档不该产得出项目档"
        assert not list((tmp_path / "runtime_batch" / "planned")
                        .glob("*.project.json"))
        assert len(text) > 10, f"回执太短，用户看不出为什么被拒：{text!r}"

    def test_the_plan_form_states_the_cost_of_each_tier(self):
        real = wb.render_plan_form()
        mock = wb.render_plan_form(mock_tier=True)
        assert "消耗订阅额度" in real
        assert "只会给你一次【拒绝】" in mock
        assert "action='/plan'" in real


def test_workbench_never_reaches_for_a_ui_symbol_that_is_not_there():
    """`ui.<名字>` 引用的必须是 workbench_ui 里真有的顶层函数或属性。

    这条守卫抓的不是风格，是一种**已经发生过两次**的事故：并行会话里整文件
    `git add tools/workbench.py`，把别人**还没提交**的半截改动一起带进版本库 ——
    于是 HEAD 里出现 `ui.workflow`，而那个函数只在一份未提交的 workbench_ui.py 里。
    页面路由一旦走到就抛 AttributeError，把连接掐断（地雷 28）。
    跨文件引用能机械核对，就别靠人记得"这次暂存了什么"。
    """
    import re
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent
    src = (here / "tools" / "workbench.py").read_text(encoding="utf-8")
    ui_src = (here / "tools" / "workbench_ui.py").read_text(encoding="utf-8")
    defined = set(re.findall(r"^(?:def|class)\s+([A-Za-z_]\w*)", ui_src,
                             re.MULTILINE))
    defined |= set(re.findall(r"^([A-Za-z_]\w*)\s*[:=]", ui_src, re.MULTILINE))
    used = set(re.findall(r"\bui\.([A-Za-z_]\w*)", src))
    missing = sorted(used - defined)
    assert not missing, (
        f"workbench.py 引用了 workbench_ui 里不存在的 {missing} —— "
        "十有八九是把并行会话的半截改动提交进来了")
