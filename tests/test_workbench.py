"""判定层（tools/workbench.py）与一页版外壳（tools/app.py）的守卫测试。

被测的不是"网页好不好看"，是四条边界：
提交走的是不是产品那条路、错误是不是一句话说清、POST 是不是只认同源、
以及**不许因为一个网页就多引一个依赖**。

2026-10 之后网页只剩一页：workbench.py 里的 `serve` / `render_*` 全部搬进
`tools/app.py`，`workbench_ui.py` 与 `workbench_flow.py` 被删（归档在
`archive/old-ui/`，不可 import）。这一份测试跟着搬 —— **一条都没删**，
每条判据都改问新的那一个入口；只在旧页面上成立的那半句话（仪表盘里不许长第二份
表单、监听范围要印在页面上）随那一页一起退了，各自在 docstring 里写明它原来锁
的是什么、现在由哪一条量。
"""
from __future__ import annotations

import ast
import http.client
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from tools import app as A
from tools import workbench as wb

STDLIB_OK = {"__future__", "argparse", "html", "os", "subprocess", "sys",
             "threading", "time", "dataclasses", "datetime", "http.server",
             "pathlib", "typing", "urllib", "json", "sqlite3", "http"}
PROJECT_OK = {"mao", "tools"}

SHELLS = (Path(A.__file__), Path(wb.__file__))

SETTINGS = ("runtime_dir: runtime\n"
            "scheduler:\n  enabled: true\n"
            "  db_path: ./runtime/queue.db\n  attempts_root: runtime\n"
            "checkpoint:\n  enabled: true\n")


# ---------------------------------------------------------------------------
# 现场：一页版把"根"散在四个模块里，少按一处就会读到仓库自己那份 runtime_batch
# （地雷 36 的同一条），而默认落地目录在业主桌面上 —— 测试一次都不许碰。
# ---------------------------------------------------------------------------

def _confine(monkeypatch, tmp_path: Path) -> Path:
    import tools.batch_project as bp
    import tools.delivery_view as dv
    import tools.scheduler_cli as sc

    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path / "desktop")
    monkeypatch.setattr(wb, "ROOT", tmp_path)
    monkeypatch.setattr(dv, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "ROOT", tmp_path)     # 子进程日志的根在 scheduler_cli
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path / "runtime_batch")
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    return cfg


class FakeRunner:
    """形状必须和真 `SchedulerRunner` 一致：`running` 是**方法**。

    地雷 30 抓到两次：替身把 running 写成布尔属性，矩阵全绿，生产里子进程
    从来没起来过。这里用方法，并且另有一条拿真类当对象的哨兵。
    """

    def __init__(self, running=False):
        self.calls = []
        self._running = running

    @property
    def log_path(self) -> Path:
        return Path("runtime_workbench") / "scheduler.log"

    def running(self):
        return self._running

    def start(self):
        self.calls.append("start")
        self._running = True
        return True, "已启动 pid=1"

    def stop(self):
        self.calls.append("stop")
        self._running = False
        return False, "已停止。"

    def tail(self, lines: int = 40):
        return []


class FakeShip:
    """推进器的替身 —— 生产里它是真子进程，测试里不许真起。"""

    log_path = Path("runtime_workbench") / "ship.log"

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []
        self._running = False

    def running(self):
        return self._running

    def start(self):
        self.calls.append("ship-start")
        self._running = self.ok
        return self.ok, "已启动 ship pid=1" if self.ok else "起不来：权限不足"

    def stop(self):
        self._running = False
        return True, "已停止。"

    def tail(self, lines: int = 40):
        return []


def _make_app(tmp_path: Path, monkeypatch, real_roles: bool = False) -> A.App:
    cfg = _confine(monkeypatch, tmp_path)
    app = A.App(config_dir=str(cfg), runner=FakeRunner(),
                real_roles=real_roles)
    app.wb.ship_factory = lambda cd, p: FakeShip()
    return app


def _serve(app: A.App):
    httpd = A.serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _call(port: int, method: str, path: str, body: str = "", host: str = "",
          origin: str = "same"):
    """POST 必须同时带本机 Host 与同源的 Origin —— app.py 缺 Origin 就回 403。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin == "same":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    elif origin:
        headers["Origin"] = origin
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.getheaders())


def _form(**fields) -> str:
    return urllib.parse.urlencode(fields, encoding="utf-8")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    return _make_app(tmp_path, monkeypatch)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """起一个真服务器（一页版的 `A.serve`），但把提交与调度都换成可控的替身。"""
    started: list[list[str]] = []
    submitted: list[str] = []

    def fake_submit(config_dir, fields):
        goal = str(fields.get("goal", "")).strip()
        submitted.append(goal)
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

    cfg = _confine(monkeypatch, tmp_path)
    monkeypatch.setattr(wb, "submit_task", fake_submit)
    runner = wb.SchedulerRunner(str(cfg), cmd_factory=fake_cmd)
    built = A.App(config_dir=str(cfg), runner=runner, real_roles=False)
    built.wb.ship_factory = lambda cd, p: FakeShip()
    httpd = _serve(built)
    port = httpd.server_address[1]
    yield port, built, httpd, started, submitted
    built.runner.stop()
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------------------
# 起得来：判据不在"仓库里能起"，在"从一个陌生 cwd 能起"（地雷 26）
# ---------------------------------------------------------------------------

class TestBootsFromAnyCwd:
    """桌面版以脚本方式起入口，那时 `sys.path[0]` 是 tools/ 本身，不是仓库根。

    `from tools import …` 一旦排在 sys.path.insert 之前，双击打开的窗口就是
    ModuleNotFoundError: No module named 'tools' —— 网页从没起来过，
    而 pytest 里永远看不见这一条：pytest 已经把仓库根放进 sys.path 了。
    """

    def _run(self, script: Path, tmp_path: Path, *args: str):
        return subprocess.run([sys.executable, str(script.resolve()), *args],
                              cwd=str(tmp_path), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def test_help_from_a_foreign_cwd(self, tmp_path):
        proc = self._run(Path(A.__file__), tmp_path, "--help")
        assert proc.returncode == 0, proc.stderr
        assert "No module named" not in proc.stderr
        assert "usage" in proc.stdout.lower()

    def test_the_judgment_layer_still_boots_as_a_script(self, tmp_path):
        """workbench.py 只剩判定层，但它仍然要能被"以脚本方式"起起来。

        旧版这里是 `tools/workbench.py --help`（那时它就是入口）。网页搬走之后
        这个模块不再打印用法，可 `from tools import local_env, role_wiring`
        那一行的顺序判据一个字没变 —— 起不来就是地雷 26 的同一场事故。
        """
        proc = self._run(Path(wb.__file__), tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "No module named" not in (proc.stdout + proc.stderr)

    def test_nothing_opens_this_page_to_the_network(self, tmp_path):
        """只绑 127.0.0.1、不提供改这个的开关 —— 这是这个产品的边界，不是默认值。"""
        proc = self._run(Path(A.__file__), tmp_path, "--help")
        assert "--host" not in proc.stdout
        assert A.HOST == "127.0.0.1"


class TestNoNewDependency:
    def test_only_stdlib_and_project_are_imported(self):
        """产品宣称"零服务依赖"。工作台多引一个 web 框架，这句话就作废了。"""
        for shell in SHELLS:
            tree = ast.parse(shell.read_text(encoding="utf-8"))
            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    used.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    used.add(node.module.split(".")[0])
            bad = {m for m in used
                   if m not in STDLIB_OK and not any(
                       m == p or m.startswith(p + ".") for p in PROJECT_OK)}
            assert not bad, f"{shell.name} 引入了新依赖：{sorted(bad)}"

    def test_the_archived_pages_are_not_imported_anymore(self):
        """`workbench_ui` / `workbench_flow` 已经删了，只留在 archive/old-ui/。

        留一条机械守卫：任何一份还活着的壳里出现这个名字，双击打开的就是一个
        连网页都没有的窗口（ModuleNotFoundError 发生在 import 期，页面上看不到）。
        """
        for shell in SHELLS:
            text = shell.read_text(encoding="utf-8")
            assert "workbench_ui" not in text, shell.name
            assert "workbench_flow" not in text, shell.name


class TestPureHelpers:
    def test_same_origin_accepts_only_the_pages_own_host(self):
        assert wb.same_origin("http://127.0.0.1:8765", "127.0.0.1:8765", 8765)
        assert wb.same_origin("http://localhost:8765", "localhost:8765", 8765)
        assert not wb.same_origin("", "127.0.0.1:8765", 8765)
        assert not wb.same_origin("http://evil.example", "127.0.0.1:8765", 8765)
        # 端口不同就不是同源：8765 的页面不能往 8766 提交
        assert not wb.same_origin("http://127.0.0.1:1", "127.0.0.1:8765", 8765)
        # DNS rebinding 的现场形状：Host 与 Origin 同为外域、彼此自洽。
        # 旧判据是"Origin 等于请求自带的 Host"，这一条会放行 —— 而 /go 要花额度。
        assert not wb.same_origin("http://mao.example", "mao.example", 8765)
        assert not wb.same_origin("http://mao.example:8765", "mao.example:8765", 8765)
        # Host 判据用的是启动时绑定的端口，与请求自带的那一份无关
        assert wb.host_problem("localhost:8765", 8765) == ""
        assert wb.host_problem("[::1]:8765", 8765) == ""
        assert wb.host_problem("mao.example:8765", 8765)
        assert wb.host_problem("127.0.0.1:8766", 8765)
        assert wb.host_problem("0.0.0.0:8765", 8765)
        assert wb.host_problem("", 8765) == "缺少 Host 头"

    def test_run_key_is_confined_to_id_characters(self):
        for bad in ("", "../etc", "rt 1", "a" * 65, "rt-x?y=1"):
            assert wb.safe_key(bad) is False, bad
        assert wb.safe_key("rt-12ddf644a36a") is True

    def test_scheduler_command_is_an_argv_list_not_a_string(self):
        argv = wb.scheduler_command("config", python_exe="py")
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[:1] == ["py"] and "scheduler" in argv
        assert argv[-2:] == ["--config-dir", "config"]

    def test_the_real_runner_class_still_exposes_running_as_a_method(self):
        """哨兵用真类，不用替身（地雷 30 第二次就是在替身上翻车的）。"""
        from tools.scheduler_cli import SchedulerRunner

        assert not isinstance(SchedulerRunner.running, property)
        assert callable(SchedulerRunner.running)

    def test_a_queue_that_was_never_created_is_named_as_such(self, tmp_path,
                                                             monkeypatch):
        """"还没建" 与 "调度层未启用" 是两回事，说错会让人去改配置。

        旧版这一句住在 `_db_label`（渲染队列库那一行）。一页版把它交给
        `delivery_view.queue_db_path`：库不存在就给 None，页面那一格因此写
        "没有队列库"。**读一页不许把空库建出来**（地雷 21）—— 那等于在别人的
        配置里凭空造一台。
        """
        import tools.delivery_view as dv

        cfg = _confine(monkeypatch, tmp_path)
        (cfg / "settings.yaml").write_text(
            "scheduler:\n  db_path: ./runtime_none/queue.db\n", encoding="utf-8")
        db = tmp_path / "runtime_none" / "queue.db"
        assert dv.queue_db_path(str(cfg)) is None
        assert not db.exists(), "观测层一上来就把队列库建出来了"
        assert dv.queue_rows(str(cfg)) == [], "查不到行要说【没有记录】，不许折成异常"


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
        而本机那就是这个仓库本身 —— 那扇门上必须挂住（地雷 19）。"""
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

    def test_the_form_never_hardcodes_a_strategy_that_gets_rejected(
            self, tmp_path, app, monkeypatch):
        """写死 GIT_WORKTREE = 给没有 git 仓库的人预设一条必然被拒的提交。

        旧入口是 `render_form(default_strategy=...)`；一页版把这一栏整个去掉了，
        于是提交带出去的是空策略 —— 判定层必须继续把空值读成"用本 config 的
        默认策略"，而不是"用户写错了"。两半都在这里量。
        """
        form = A.go_form(app)
        assert "GIT_WORKTREE" not in form
        assert "<select" not in form, "页面又长出第二套策略选择就是漂移的开始"

        calls: list[str] = []
        monkey = _stub_repo(calls)
        try:
            monkey.enter("config")
            rt, err = wb.submit_task("config", {
                "goal": "把 multiply 的返回值改成 a * b",
                "workspace": str(tmp_path), "strategy": ""})
        finally:
            monkey.exit()
        assert err == "" and rt == "rt-stub1", err
        assert "submit" in calls

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


# ---------------------------------------------------------------------------
# 网页外壳：一页版的四个 POST 与那一页的渲染
# ---------------------------------------------------------------------------

class TestHttpShell:
    def test_the_landing_page_is_the_place_where_you_type(self, server):
        """输入面只有一处，而落地页就是那一处（地雷 35 的那次实测）。

        旧版这条锁的是"`/` 落到任务那一格、仪表盘里没有第二份表单"。一页版没有
        第二页了，所以判据收成两条更硬的：落地页真的有可以打字的那一栏，
        而**整页只有一份**通向 /go 的表单。
        "监听范围写在人落地的那一页上"那半条随仪表盘一起退了 —— 现在它由
        `test_nothing_opens_this_page_to_the_network` 按机械事实量（真绑的地址）。
        """
        port, _b, httpd, _s, _sub = server
        status, page, _h = _call(port, "GET", "/")
        assert status == 200
        assert "<textarea name='prompt'" in page, "输入需求的地方不见了"
        assert "action='/go'" in page
        assert page.count("action='/go'") == 1, "长出第二份提交入口就是漂移的开始"
        assert A.HOST == "127.0.0.1" and httpd.server_address[0] == "127.0.0.1"

    def test_post_without_origin_is_refused(self, server):
        port, _b, _h, _s, submitted = server
        code, page, _hdr = _call(port, "POST", "/go", _form(prompt="做个东西"),
                                 origin="")
        assert code == 403
        assert "Origin" in page
        assert submitted == [], "被来源守卫拒了却已经提交 = 拒的是措辞不是动作"

    def test_a_cross_site_post_never_reaches_the_money_path(self, server):
        port, _b, _h, _s, submitted = server
        code, page, _ = _call(port, "POST", "/go", _form(prompt="做个东西"),
                              origin="http://evil.example")
        assert code == 403, f"/go 被跨站表单驱动了：{code}"
        assert "本机" in page
        assert submitted == []

    def test_an_id_shaped_path_no_longer_reaches_anything(self, server):
        """旧版这条打的是 `/run/<rt-id>` 那一页：非法 id 必须在门口拒掉（400），
        不许被拼进查询路径。一页版没有"按 id 开一页"这条路 —— 判据跟着换成
        **那条路径根本不存在**：带 `..%2F` 的串只拿回 404 的那一页，
        不动队列、不花钱。`safe_key` 本身仍由
        `TestPureHelpers::test_run_key_is_confined_to_id_characters` 量着。
        """
        port, _b, _h, _s, submitted = server
        status, page, _hdrs = _call(port, "GET", "/run/..%2F..%2Fetc")
        assert status == 404, "非法 id 那一条路径还在被某个处理函数接走"
        assert "<textarea name='prompt'" in page, "404 也要把人放回打字那一格"
        assert submitted == []

    def test_go_answers_303_so_a_refresh_costs_nothing(self, server):
        """303 而不是 200：刷新一个提交页不该再提交一次（会再花一次额度）。"""
        port, _b, _h, _s, submitted = server
        ws = str(Path(server[1].config_dir))
        code, _body, hdr = _call(port, "POST", "/go",
                                 _form(prompt="把 multiply 改成返回 a * b",
                                       workspace=ws))
        assert code == 303, "提交后直接回了 200，没有走 303"
        assert hdr["Location"] == "/", "跳转目标不是那一页"
        _s2, page2, _h2 = _call(port, "GET", "/")
        assert "需求至少要写" not in page2
        assert len(submitted) == 1, "跟着 303 回到那一页又提交了一次"

    def test_a_landing_dir_that_cannot_be_created_comes_back_as_one_sentence(
            self, server, tmp_path):
        """错误没有回到页面上，用户只看到刷新（地雷 44：要说得出差在哪一格）。

        旧版这条打的是 `workspace 路径不存在` —— 一页版把"不存在"改成了"替你建"，
        所以还站得住的判据是**建不出来**那一种：回话必须带上撞上的那个路径。
        （这一支的输入回填产品没做：`start_go` 在 mkdir 失败那一支直接 return，
        `wb.last_go` 因此是空的。"拒绝不许丢工作"仍由下面
        `test_rejected_submit_keeps_what_was_typed` 量在走到判定层的那一条路上；
        这一处缺陷按规则报给业主，不在测试里替它抹平，也不把它写成断言。）
        """
        port, _b, _h, _s, _sub = server
        file_as_dir = tmp_path / "f.txt"
        file_as_dir.write_text("x", encoding="utf-8")
        blocked = str(file_as_dir / "sub")
        code, _body, _ = _call(port, "POST", "/go",
                               _form(prompt="把 multiply 改成返回 a * b",
                                     workspace=blocked))
        assert code == 303
        _s, page, _h = _call(port, "GET", "/")
        assert "落地目录建不出来" in page, page[:400]
        assert "f.txt" in page, "回话说不出撞的是哪一格"
        assert "class='notice" in page

    def test_a_blank_landing_dir_is_chosen_for_you_and_never_on_the_desktop(
            self, server, tmp_path):
        """一页版不再要求人懂文件系统：留空就由程序挑一个（地雷 35）。

        旧版这一条打的是 `plan_from_form` 那句"落地目录"拒绝 —— 那句拒绝现在只在
        判定层成立（tests/test_workbench_onebox.py 仍锁着）。页面这一层成立的是
        另一句：替你选一个，而选出来的必须是**这一次测试的**那一个 ——
        `app.ROOT` 与 `app.desktop_dir` 没被按住就会在业主桌面上建目录（规则 3）。
        """
        port, _b, _h, _s, submitted = server
        code, _body, _ = _call(port, "POST", "/go",
                               _form(prompt="把 multiply 改成返回 a * b"))
        assert code == 303
        assert submitted == ["把 multiply 改成返回 a * b"]
        home = tmp_path / "desktop" / "MAO-交付"
        made = sorted(p for p in home.iterdir() if p.is_dir())
        assert len(made) == 1, f"程序替人选的落地目录不对：{made}"
        assert "multiply" in made[0].name, made[0].name
        assert str(tmp_path) in str(made[0]), "跑到了业主真桌面上"

    def test_go_starts_a_real_child_process_and_keeps_output_on_disk(
            self, server):
        """判据不在"页面说过调度器运行中"，在"子进程真起来了、输出在磁盘上"（地雷 30）。"""
        port, built, _h, started, _sub = server
        code, _b, _ = _call(port, "POST", "/go",
                            _form(prompt="把 multiply 改成返回 a * b",
                                  workspace=str(built.config_dir)))
        assert code == 303
        assert built.runner.running(), "点了开工但子进程不在"
        assert started and started[0][0] == sys.executable
        deadline = time.time() + 10
        while time.time() < deadline and not built.runner.tail():
            time.sleep(0.2)
        assert "tick" in "".join(built.runner.tail()), "子进程输出没落到日志文件里"
        # 尾巴读的是磁盘上那一份，不是内存字段（地雷 16 的同一形状）。
        # 注意 `log_tail` 在页面上挂在"现在到哪了"那一格里，没有批次时那一格
        # 整块不出现 —— 所以这里直接量那个渲染函数。
        assert "tick" in A.log_tail(built), "留在磁盘上的输出页面读不到 = 白留"

    def test_double_start_does_not_spawn_a_second_worker(self, server):
        port, built, _h, started, _sub = server
        for _ in range(2):
            _call(port, "POST", "/go",
                  _form(prompt="把 multiply 改成返回 a * b",
                        workspace=str(built.config_dir)))
            time.sleep(0.1)
        assert len(started) == 1, "重复点开工多开了一个 worker（地雷 18）"

    def test_rejected_submit_keeps_what_was_typed(self, server):
        """拒绝不该丢工作：刚写完的那句要原样回到输入面。

        一页版没有"跳回表单"那一步（就这一页），所以回填活到**下一次成功**为止；
        旧版那句"再看一次输入面就该是空表单"因此改成"成功之后不该还挂着上一句"。
        """
        port, built, _h, _s, _sub = server
        ws = str(built.config_dir)
        _c, _b, _ = _call(port, "POST", "/go", _form(prompt="太短了",
                                                     workspace=ws))
        _c, page, _ = _call(port, "GET", "/")
        assert "太短了" in page, "拒绝把用户刚写的需求清空了"
        assert ws in page
        _c, _b, _ = _call(port, "POST", "/go",
                          _form(prompt="把 multiply 改成返回 a * b", workspace=ws))
        _c, again, _ = _call(port, "GET", "/")
        assert "太短了" not in again

    def test_goal_is_escaped_when_echoed_back(self, server):
        port, built, _h, _s, _sub = server
        ws = str(built.config_dir)
        _c, _body, _ = _call(port, "POST", "/go",
                             _form(prompt="<b>'太短", workspace=ws))
        _c, page, _h = _call(port, "GET", "/")
        assert "&lt;b&gt;" in page, "回填没转义"
        assert "&#x27;" in page, "引号没转义 = 能从属性里逃出去"
        assert "<b>'" not in page

    def test_the_echo_cannot_break_out_of_the_attribute(self, app):
        """回填的值是**用户输入**，进的是单引号属性；quote=True 不是可选项。"""
        app.wb.last_go = {"prompt": "<b>'</b>", "workspace": "C:\\x' onmouseover="}
        out = A.go_form(app)
        assert "<b>'</b>" not in out and "' onmouseover=" not in out
        assert "&#x27;" in out

    def test_an_exception_inside_a_post_branch_leaves_a_sentence(self, server,
                                                                monkeypatch):
        """网页处理函数让异常逃出去不是"返回 500"：连接被掐断，页面上什么都
        看不到（地雷 28）。这条在 /go 上量：切分那一步真会抛网络错。"""
        port, built, _h, _s, _sub = server

        def boom(ctx, fields):
            raise RuntimeError("Connection to api.openai.com refused")

        monkeypatch.setattr(wb, "go_from_form", boom)
        code, body, _ = _call(port, "POST", "/go",
                              _form(prompt="把 multiply 改成返回 a * b",
                                    workspace=str(built.config_dir)))
        assert code in (303, 200), f"直接回了 {code}：{body[:160]}"
        _c, page, _h = _call(port, "GET", "/")
        assert "RuntimeError" in page and "api.openai.com" in page


# ---------------------------------------------------------------------------
# 批次那一格：一页版把它变成"现在到哪了"。只读投影，推进要回命令行动作层签字。
# ---------------------------------------------------------------------------

def _write_batch(tmp_path: Path, app: A.App, m2_status: str = "awaiting-merge"):
    import json

    ws = tmp_path / "runtime_batch" / "site-ws"
    ws.mkdir(parents=True, exist_ok=True)
    planned = tmp_path / "runtime_batch" / "planned"
    planned.mkdir(parents=True, exist_ok=True)
    spec_path = planned / "site.project.json"
    spec_path.write_text(json.dumps({
        "name": "site", "workspace": str(ws), "strategy": "COPY",
        "config_dir": app.config_dir, "max_rounds": 2, "constraints": "",
        "mode": "auto", "owner_goal": "做一个中文演示站点",
        "final_acceptance": {"name": "全套测试", "command": ["pytest", "-q"]},
        "milestones": [
            {"id": "m1", "goal": "新建 index.html 与 styles.css，首页要有 title",
             "acceptance": "pytest -q", "constraints": "", "depends_on": []},
            {"id": "m2", "goal": "加 about.html 并与首页共用同一套样式",
             "acceptance": "pytest -q", "constraints": "", "depends_on": []}],
    }, ensure_ascii=False), encoding="utf-8")
    state_path = tmp_path / "runtime_batch" / "site.json"
    state_path.write_text(json.dumps({
        "name": "site", "workspace": str(ws),
        "created_at": "2026-10-08T00:00:00", "owner_goal": "做一个中文演示站点",
        "directives": [],
        "milestones": {"m1": {"status": "done", "runtime_task_id": "rt-aaaa",
                              "patch_lines": 12},
                       "m2": {"status": m2_status,
                              "runtime_task_id": "rt-bbbb",
                              "detail": "changes.patch 27 行 [框架]"}},
        "final": {"status": "not-run"},
    }, ensure_ascii=False), encoding="utf-8")
    app.project = str(spec_path)
    return spec_path, state_path


@pytest.fixture()
def batch(tmp_path, monkeypatch):
    built = _make_app(tmp_path, monkeypatch)
    paths = _write_batch(tmp_path, built)
    return built, tmp_path, paths


class TestBatchSection:
    def test_empty_state_offers_an_action_not_a_command_line(self, app):
        """旧版这一格给的是 `batch_project.py run …`。业主那句"上手根本不知道
        从哪里做起"（地雷 35）之后，判据翻了个方向：**页面上不许出现终端作业**。

        放宽的不是判据 —— "下一步由人说" 换成了 "下一步由按钮做"，
        所以这一条量的还是同一件事，只是量的是现在那一条路。
        """
        text = A.status_block(app)
        assert "还没有开始过" in text
        assert "batch_project.py" not in text, "把人支回终端就是又一道墙"
        assert "开工" in A.go_form(app), "空状态必须给一个动作"

    def test_awaiting_merge_is_shown_as_the_human_gate(self, batch):
        built, _tmp, _paths = batch
        text = A.page(built)
        assert "site" in text
        assert "在等合入" in text, "跑完≠交付：这一格还是人的门"
        assert "accept" in text, "要说出这一格等人做什么，不许只写一个状态词"
        assert "项目完成" not in text

    def test_a_run_this_config_never_had_names_the_store_it_reads(self, batch):
        """按 rt-id 查不到时要报库路径（地雷 24），库还没建就明说"没有队列库"。

        旧版这一句在 `_db_label` 里；一页版把它交给 status_block 那一行 ——
        判据没变：不许把"这台配置里没有它的记录"渲染成"没干活"。
        """
        import tools.delivery_view as dv

        built, tmp_path, _paths = batch
        text = A.page(built)
        assert "rt-aaaa" in text and "查不到" in text
        assert "没有队列库" in text, "库不存在要说明，不许留空让人自己猜"
        assert dv.queue_db_path(built.config_dir) is None
        assert not (tmp_path / "runtime" / "queue.db").exists()

    def test_a_milestone_that_stopped_gets_a_button_not_a_homework(
            self, tmp_path, monkeypatch):
        """地雷 35 的同一格：停下来那一格的下一步必须是按钮。"""
        built = _make_app(tmp_path, monkeypatch)
        _write_batch(tmp_path, built, m2_status="failed")
        text = A.page(built)
        assert "这一格停下来并写了原因" in text
        assert "再跑这一格" in text
        assert "run --retry" not in text, "页面上不许多写一条 CLI 命令"


# ---------------------------------------------------------------------------
# 切分那一步的入口（旧：`plan_from_form` 那一格；现在：/go 里的第一步）
# ---------------------------------------------------------------------------

class TestTheSplitEntry:
    """业主对 loop 的第一句是"用户输入提示词 → 验收 agent 切分"。

    判定仍在 `batch_project.plan`；这一格补的是入口，所以入口换了就跟着改问法。
    """

    def test_nothing_is_called_before_the_input_passes_the_boundary(
            self, tmp_path, monkeypatch, app):
        """旧版这条写的是"至少 10 个字"。业主 2026-09-30 那句"怎么填都不行"把
        字数门槛撤了（地雷 35），现在**只有空**才是前提，短句照走。

        判据剩下的那半条不变：输入没过边界之前一次调用都不许发生。
        """
        from tools import batch_project as bp

        boom = lambda *a, **k: pytest.fail("输入没过关就不该调用任何东西")
        monkeypatch.setattr(bp, "plan", boom)
        monkeypatch.setattr(wb, "submit_task", boom)
        text, bad, project = A.start_go(app, {"prompt": "   ", "workspace": ""})
        assert bad and "写一句话" in text
        assert project == ""
        assert not (tmp_path / "runtime_batch" / "planned").exists()

    def test_a_crash_inside_plan_becomes_a_notice_not_a_500(self, tmp_path,
                                                           monkeypatch):
        """`batch_project.plan` 只吞 BatchError。真实档最常见的失败是网络
        （第一次 m1 就是 api.openai.com 直连不通），那会一路抛到浏览器。
        网页不能因为一条坏输入而挂掉 —— 这条规矩 `submit_task` 早就立了。

        一页版里走这一条的是 /go（旧那份写的是切分那一格）。
        """
        from tools import batch_project as bp

        def boom(*a, **k):
            raise RuntimeError("Connection to api.openai.com refused")

        monkeypatch.setattr(bp, "plan", boom)
        # 仓库前提与项目档落点在这条测试里都不是被测性质：前者给"可以开工"那一档
        # （真判据与真按钮在 tests/test_workbench_onebox.py 里锁着），后者按到本
        # 测试目录 —— 这条用例一个字都不该写进仓库自己的 runtime_batch。
        monkeypatch.setattr(wb, "repo_problem", lambda ws: ("", ""))
        monkeypatch.setattr(wb, "login_blocker", lambda cd: "")
        monkeypatch.setattr(wb, "_planned_target", lambda ws: tmp_path / "x.json")
        ctx = wb.Workbench(config_dir="config", runner=FakeRunner(),
                           real_roles=True)
        ws = tmp_path / "site"
        ws.mkdir()
        text, bad = wb.go_from_form(
            ctx, {"prompt": "交付一个中文演示站点，分三步各自可验收",
                  "workspace": str(ws)})
        assert bad and "切分失败" in text and "RuntimeError" in text
        assert ctx.runner.calls == []
        assert not (tmp_path / "x.json").exists()

    def test_a_refused_split_writes_no_project_file(self, tmp_path, monkeypatch):
        """被拒的时候一个字都不写，并且把拒绝原因原样带回来，不是一句"失败"。

        旧版这一条打在 Mock 档的切分那一格（内置 Mock 答不出项目档）。一页版
        的 Mock 档根本不再假装切分（那句诚实由 onebox 的
        `test_mock_tier_says_it_did_not_split` 锁），于是"不写半成品"这一半
        搬到真实档那一条路上量 —— 判据本身没变。
        """
        from tools import batch_project as bp

        def refuse(target, goal, **kw):
            kw["echo"]("没有生成项目档：acceptance 必须是机器命令")
            return 2

        monkeypatch.setattr(bp, "plan", refuse)
        monkeypatch.setattr(bp, "submit_next",
                            lambda *a, **k: pytest.fail("切分没成功就不该入队"))
        monkeypatch.setattr(wb, "repo_problem", lambda ws: ("", ""))
        monkeypatch.setattr(wb, "login_blocker", lambda cd: "")
        monkeypatch.setattr(wb, "_planned_target",
                           lambda ws: tmp_path / "runtime_batch" / "planned"
                                      / "site.project.json")
        ctx = wb.Workbench(config_dir="config", runner=FakeRunner(),
                           real_roles=True)
        ws = tmp_path / "site"
        ws.mkdir()
        text, bad = wb.go_from_form(
            ctx, {"prompt": "交付一个中文演示站点，分三步各自可验收",
                  "workspace": str(ws)})
        assert bad and "acceptance 必须是机器命令" in text
        assert not list((tmp_path / "runtime_batch" / "planned")
                        .glob("*.project.json"))
        assert len(text) > 10, f"回执太短，用户看不出为什么被拒：{text!r}"

    def test_the_button_states_the_cost_of_each_tier(self, tmp_path, monkeypatch):
        """花钱的那一档必须把代价写在按下去的那一格上（旧：render_plan_form）。

        彩排档那一格单独量一条：它绑的三个角色是 `generic_cli` +
        `supports_cli: True` 的真子进程，`env_report.uses_real_cli` 因此给它回
        True —— 拿 `real_roles` 当"会不会花钱"的判据，就会在同一页上既说
        "不消耗任何订阅额度"又说"会消耗订阅额度"（地雷 45 的形状：同一个判断在
        两个地方各写一遍）。2026-10-08 产品把这一格改成按**档位**说话；
        这条断言把它钉住，免得下一次又退回按 real_roles 说。
        """
        real = A.go_form(_make_app(tmp_path / "r", monkeypatch, real_roles=True))
        free = A.go_form(_make_app(tmp_path / "f", monkeypatch, real_roles=False))
        assert "消耗订阅额度" in real
        assert "不花钱" in free
        assert "action='/go'" in real and "action='/go'" in free
        app = _make_app(tmp_path / "h", monkeypatch, real_roles=True)
        app.rehearsal = True
        assert "不消耗任何订阅额度" in A.agent_strip(app)
        form = A.go_form(app)
        assert "彩排档" in form and "真实 agent，会消耗订阅额度" not in form


def test_workbench_never_reaches_for_a_symbol_the_module_it_imports_does_not_have():
    """`bp.<名字>` / `dv.<名字>` 引用的必须真在那两个模块里（地雷 37）。

    这条守卫抓的不是风格，是一种**已经发生过两次**的事故：并行会话里整文件
    `git add tools/workbench.py`，把别人**还没提交**的半截改动一起带进版本库 ——
    于是 HEAD 里出现 `ui.workflow`，而那个函数只在一份未提交的 workbench_ui.py 里。
    页面路由一旦走到就抛 AttributeError，把连接掐断（地雷 28）。

    workbench_ui.py 已经随那一页删掉了；workbench.py 现在**惰性** import
    batch_project 与 delivery_view，同一件事的守卫跟着量这一对。
    跨文件引用能机械核对，就别靠人记得"这次暂存了什么"。
    """
    import importlib
    import re

    here = Path(__file__).resolve().parent.parent
    src = (here / "tools" / "workbench.py").read_text(encoding="utf-8")
    pairs = set(re.findall(r"from tools import ([a-z_]\w*) as (_?\w+)", src))
    assert pairs, "惰性 import 的形状变了，这条守卫要跟着改，不许直接删"
    for mod, alias in sorted(pairs):
        imported = importlib.import_module(f"tools.{mod}")
        used = set(re.findall(rf"\b{alias}\.([A-Za-z_]\w*)", src))
        missing = sorted(u for u in used if not hasattr(imported, u))
        assert not missing, (
            f"workbench.py 引用了 {mod} 里不存在的 {missing} —— "
            "十有八九是把并行会话的半截改动提交进来了")
