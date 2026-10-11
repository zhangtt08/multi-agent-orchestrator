"""一个输入框那条路的测试：`workbench.go_from_form` 与它上面那一格 `/go`。

业主的原话是"跟平时使用 agent 一样，输入框中输入提示词 agent 自动切分即可进入
程序开始工作，而不是仍然这样死板固定"。所以这一格要锁的是：
- 输入边界在调用任何 agent 之前挡掉；
- 真实档：先切分，切成了才入队，入队了才启动调度器 —— 顺序不能反；
- Mock 档：不假装切分了（内置 Mock 答不出项目档），并明说走的是单任务；
- 任何一步失败都不留半成品，也不悄悄启动调度器。

2026-10 之后这一条路只剩一个入口：`tools/app.py` 的 `/go`。旧版那份
"从已切好的清单开工"（`start_plan_from_plan`，页面上的第二张表单）没有了，
它锁的三条判据跟着搬到 `A.start_go` / `A.serve` 上量 —— 判据一条没减。
"""
from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import app as A
from tools import workbench as wb


class FakeRunner:
    """替身的形状要跟真 `SchedulerRunner` 一致：`running` 是**方法**（地雷 30）。

    真类还有 `tail()` 与 `log_path` —— 缺了它们，页面那一格会走
    `except Exception` 那条兜底支，测试就测不到"日志尾巴读的是磁盘那一份"。
    """

    def __init__(self, running=False):
        self._running = running
        self.calls = []

    @property
    def log_path(self):
        return Path("runtime_workbench") / "scheduler.log"

    def running(self):
        return self._running

    def tail(self, lines: int = 40):
        return []

    def start(self):
        self.calls.append("start")
        self._running = True
        return True, "已启动 pid=1"


class FakeShip(FakeRunner):
    """推进器的替身 —— 生产里它是真子进程，测试里不许真起。"""

    def __init__(self, ok=True):
        super().__init__()
        self.ok = ok

    def start(self):
        self.calls.append("ship-start")
        self._running = self.ok
        return self.ok, "已启动 ship pid=1" if self.ok else "起不来：权限不足"


def make_ctx(real=True, running=False):
    return SimpleNamespace(config_dir="examples/config_minimal",
                           real_roles=real, runner=FakeRunner(running),
                           ship=None, ship_factory=lambda cd, p: FakeShip(),
                           last_go={}, last_form={}, last_plan={})


GOOD = {"prompt": "交付一个可打开的中文演示站点，分三步各自可验收",
        "workspace": "."}

SETTINGS = ("runtime_dir: runtime\n"
            "scheduler:\n  enabled: true\n"
            "  db_path: ./runtime/queue.db\n  attempts_root: runtime\n"
            "checkpoint:\n  enabled: true\n")


def make_app(tmp_path, monkeypatch, real=True) -> A.App:
    """一页版那一格（tools/app.py）。四个"根"都要按住。

    `A.ROOT` 决定项目档/交付说明/落地清单读哪一层，`bp.STATE_DIR` 决定批次状态
    读写哪一层，`dv.ROOT` 决定队列库在哪，`A.desktop_dir` 决定程序替人挑的落地
    目录在哪 —— 少按任何一处，测试就会读到仓库自己那份 runtime_batch（地雷 36
    的同一条）或在业主桌面上建目录（规则 3）。
    """
    import tools.batch_project as bp
    import tools.delivery_view as dv
    import tools.scheduler_cli as sc

    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path / "desktop")
    monkeypatch.setattr(wb, "ROOT", tmp_path)
    monkeypatch.setattr(dv, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    monkeypatch.setattr(bp, "STATE_DIR", tmp_path / "runtime_batch")
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    app = A.App(config_dir=str(cfg), runner=FakeRunner(), real_roles=real)
    app.wb.ship_factory = lambda cd, p: FakeShip()
    return app


def _spec_for(workspace: Path, name: str = "site",
              ids: tuple = ("m1", "m2")) -> str:
    """一份能过 `check_spec` 机械判据的清单（落地目录必须是真目录）。"""
    return json.dumps({
        "name": name, "workspace": str(workspace), "strategy": "COPY",
        "config_dir": "examples/config_minimal", "mode": "auto",
        "final_acceptance": {"name": "全套测试", "command": ["pytest", "-q"]},
        "milestones": [
            {"id": ids[0], "goal": "在工作区根新建 index.html，首页要有标题与说明区块",
             "acceptance": "pytest -q", "constraints": "", "depends_on": []},
            {"id": ids[1], "goal": "再加一份 about.html 并与首页共用同一套样式",
             "acceptance": "pytest -q", "constraints": "", "depends_on": []}],
    }, ensure_ascii=False)


def _patch_plan(monkeypatch, tmp_path, payload: str, fname: str = "site"):
    """把"切分产出项目档"接好，但让**真** `load_spec` / `load_state` 读它。

    旧版把 load_spec 换成替身，于是这一格从来没真的过过 check_spec —— 一页版的
    项目档是页面后面所有格子的身份（地雷 50），所以这里让真校验器跑一遍。
    """
    from tools import batch_project as bp

    target = tmp_path / "runtime_batch" / "planned" / f"{fname}.project.json"
    monkeypatch.setattr(wb, "_planned_target", lambda ws: target)

    def fake_plan(t, goal, **kw):
        Path(t).parent.mkdir(parents=True, exist_ok=True)
        Path(t).write_text(payload, encoding="utf-8")
        return 0

    monkeypatch.setattr(bp, "plan", fake_plan)
    order: list = []
    monkeypatch.setattr(bp, "submit_next",
                        lambda spec, state, **kw: order.append("queue") or 0)

    def ship_factory(_cd, _p):
        order.append("ship-start")
        return FakeShip()

    return order, target, ship_factory


def _serve(app: A.App):
    httpd = A.serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _call(port: int, method: str, path: str, body: str = ""):
    """POST 要同时带本机 Host 与同源的 Origin —— app.py 缺 Origin 就回 403。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode("utf-8", "replace")


@pytest.fixture()
def ws_repo(tmp_path):
    """这些测试原来靠"进程 cwd 恰好是一个 git 仓库"过关 —— 那是环境，不是判据。

    `git archive` 出来的发布档里没有 `.git`，于是仓库闸门那一条把 5 个测试全挡在
    门外，红的理由跟它们各自测的东西毫无关系。给自己造一个仓库。
    """
    return str(make_repo(tmp_path / "ws"))


def test_empty_prompt_is_the_only_thing_that_stops_before_any_call(monkeypatch):
    """2026-09-30 放宽：短不再是拒绝理由，"什么都没有"才是。

    原来这一格要求至少 10 个字，业主的输入是「创建一个1111文档」这类短句，
    被拒之后他的原话是"怎么填都不行"。前提只剩两条（有内容、有落地目录），
    短句照走，但回执里要说清"验收 agent 只能按字面理解"。
    """
    ctx = make_ctx()
    boom = lambda *a, **k: pytest.fail("输入没过关就不该调用任何东西")
    monkeypatch.setattr(wb, "submit_task", boom)
    from tools import batch_project as bp
    monkeypatch.setattr(bp, "plan", boom)
    text, bad = wb.go_from_form(ctx, {"prompt": "   ", "workspace": "."})
    assert bad and "写一句话" in text


def test_short_prompt_now_runs_and_says_why_it_might_miss(monkeypatch, tmp_path,
                                                          ws_repo):
    ctx = make_ctx()
    _patch_split(monkeypatch, tmp_path, ONE)
    text, bad = wb.go_from_form(ctx, {"prompt": "做个站",
                                      "workspace": ws_repo})
    assert bad is False, text
    assert "只有 3 个字" in text and "按字面理解" in text
    assert "切成 1 格" in text          # 放宽不等于打折：这一格照样开工


def test_missing_workspace_is_refused_with_the_real_reason():
    ctx = make_ctx()
    text, bad = wb.go_from_form(ctx, {"prompt": "交付一个可打开的中文演示站点",
                                      "workspace": "   "})
    assert bad and "落地目录" in text and "不会被替你分配" in text


def test_nonexistent_workspace_is_refused(tmp_path):
    ctx = make_ctx()
    text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=str(tmp_path / "nope")))
    assert bad and "不存在" in text


def test_mock_tier_says_it_did_not_split(monkeypatch):
    ctx = make_ctx(real=False)
    seen = {}

    def fake_submit(config_dir, fields):
        seen["goal"] = fields["goal"]
        return "rt-mock-1", ""

    monkeypatch.setattr(wb, "submit_task", fake_submit)
    text, bad = wb.go_from_form(ctx, GOOD)
    assert not bad, text
    assert "没有自动切分" in text and "单任务" in text
    assert seen["goal"] == GOOD["prompt"]
    assert ctx.runner.calls == ["start"]      # 入队成功之后才动调度器


def test_mock_tier_keeps_the_rejection_reason(monkeypatch):
    ctx = make_ctx(real=False)
    monkeypatch.setattr(wb, "submit_task",
                        lambda c, f: ("", "workspace 那一栏要填一个目录"))
    text, bad = wb.go_from_form(ctx, GOOD)
    assert bad and "workspace" in text
    assert ctx.runner.calls == []             # 被拒不启动调度器


def _patch_split(monkeypatch, tmp_path, milestones_json: str):
    """把"切分产出一个项目档"这条路径接好，只换里程碑条数。"""
    from tools import batch_project as bp
    real_load_spec = bp.load_spec          # patch 之后再调 bp.load_spec 就是递归
    (tmp_path / "site.project.json").write_text(milestones_json, encoding="utf-8")
    monkeypatch.setattr(wb, "_planned_target", lambda ws: tmp_path / "x.json")

    def fake_plan(target, goal, **kw):
        (tmp_path / "x.json").write_text(milestones_json, encoding="utf-8")
        return 0

    monkeypatch.setattr(bp, "plan", fake_plan)
    monkeypatch.setattr(bp, "load_spec",
                        lambda p: real_load_spec(tmp_path / "site.project.json"))
    monkeypatch.setattr(bp, "load_state", lambda spec: {"milestones": {}}
                        )
    order = []
    monkeypatch.setattr(bp, "submit_next",
                        lambda spec, state, **kw: order.append("queue") or 0)
    return order


ONE = ('{"name":"site","workspace":".","strategy":"COPY",'
       '"config_dir":"examples/config_minimal",'
       '"milestones":[{"id":"m1","goal":"新建 index.html 与 styles.css，首页要有 title 与 hero 区块",'
       '"acceptance":"pytest -q"}]}')

TWO = ('{"name":"site","workspace":".","strategy":"COPY",'
       '"config_dir":"examples/config_minimal",'
       '"milestones":[{"id":"m1","goal":"新建 index.html 与 styles.css，首页要有 title 与 hero 区块",'
       '"acceptance":"pytest -q"},'
       '{"id":"m2","goal":"加 about.html 并与首页共用同一套样式，导航能互跳",'
       '"acceptance":"pytest -q"}]}')


def test_real_tier_queues_then_starts(monkeypatch, tmp_path, ws_repo):
    ctx = make_ctx()
    order = _patch_split(monkeypatch, tmp_path, ONE)
    text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=ws_repo))
    assert not bad, text
    order.append("start" if ctx.runner.calls else "nostart")
    assert order == ["queue", "start"]             # 顺序不能反：先入队再开工
    assert "切成 1 格" in text and "m1" in text
    assert "这一格" in text and "推进器" in text
    # v1.8 的形状：不再有人工门，回执要说清推进器接手了整批
    assert "合入还要你说一句" not in text
    assert "总验收通过就是交付" in text


def test_the_ship_subprocess_is_the_one_command_that_drives_the_batch():
    """推进器的 argv 就是"整批无人值守"这件事本身 —— 跑偏了要能被发现。

    `--no-serve` 这一条是**反着的**断言，2026-09-30 被彩排档实跑推翻：面板起的
    `scheduler run` 一 drain 就退出，m1 做完之后它已经不在了，于是 m2 入队却没人领，
    批次以「提交后 182s 仍停在 QUEUED —— 没有调度器在领这条任务」收场。
    判据不在"面板说过调度器运行中"，在"每一格都被领走"。
    """
    from pathlib import Path

    argv = wb.make_ship("examples/config_minimal",
                        Path("runtime_batch/site.project.json")).cmd_factory(
        "examples/config_minimal")
    assert "ship" in argv and "--no-serve" not in argv
    assert "--project" in argv
    assert any("batch_project.py" in a for a in argv)
    # 推进器不带 `scheduler run` 那种命令行：serve 是 ship 自己的事（drive→serve=True）
    assert argv.count("scheduler") == 0


def test_a_ship_that_cannot_start_is_reported_not_hidden(monkeypatch, tmp_path,
                                                       ws_repo):
    ctx = make_ctx()
    ctx.ship_factory = lambda cd, p: FakeShip(ok=False)
    _patch_split(monkeypatch, tmp_path, TWO)
    text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=ws_repo))
    assert bad is True
    assert "推进器起不来" in text and "后面的格子不会自己开始" in text


def test_a_multi_slice_receipt_says_the_batch_drives_itself(
        monkeypatch, tmp_path, ws_repo):
    """"会自动跑完"以前是最不该说的满话；现在它是事实，反过来才要被抓出来。"""
    ctx = make_ctx()
    _patch_split(monkeypatch, tmp_path, TWO)
    text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=ws_repo))
    assert not bad, text
    assert "切成 2 格" in text and "第一格 m1" in text
    assert "推进器会自己一格一格走完" in text and "中间不问你" in text
    assert "只有你合入上一格" not in text


def test_human_mode_still_promises_the_manual_gate(monkeypatch, tmp_path,
                                                   ws_repo):
    """mode: human 那一档还在，回执就不许说成无人值守。"""
    ctx = make_ctx()
    human = TWO.replace('{"name":"site"', '{"name":"site"', 1)
    _patch_split(monkeypatch, tmp_path, human)
    from tools import batch_project as bp

    real_load_spec = bp.load_spec
    spec_with_mode = real_load_spec(tmp_path / "site.project.json")
    spec_with_mode["mode"] = "human"
    monkeypatch.setattr(bp, "load_spec", lambda p: spec_with_mode)
    text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=ws_repo))
    assert not bad, text
    assert "mode: human" in text and "等你 accept" in text
    assert "中间不问你" not in text


def test_real_tier_stops_when_plan_refuses(monkeypatch, tmp_path, ws_repo):
    from tools import batch_project as bp

    ctx = make_ctx()
    monkeypatch.setattr(wb, "_planned_target", lambda ws: tmp_path / "x.json")

    def refuse(target, goal, **kw):
        kw["echo"]("没有生成项目档：acceptance 必须是机器命令")
        return 2

    def never(*a, **k):
        raise AssertionError("切分没成功就不该入队")

    monkeypatch.setattr(bp, "plan", refuse)
    monkeypatch.setattr(bp, "submit_next", never)
    text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=ws_repo))
    assert bad and "acceptance 必须是机器命令" in text
    assert "什么都没开始" in text
    assert ctx.runner.calls == []


# ---------------------------------------------------------------------------
# 2026-09-29 业主实跑炸出来的两条：
#   落地目录不是 git 仓库 → batch_project.head() 抛 BatchError → /go 那条连接
#   被 socketserver 打断，页面上什么都看不到，只有 launcher.log 里一段 traceback；
#   而切完之后的"下一步"只有一条命令，界面里没有门。
# ---------------------------------------------------------------------------

def git_in(repo, *args):
    import subprocess
    proc = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def make_repo(path, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    git_in(path, "init", "-q")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git_in(path, "add", "-A")
    if commit:
        git_in(path, "commit", "-q", "-m", "基线")
    return path


class TestWorkspaceMustBeItsOwnRepo:
    """落地目录要 git，但**这一步不许再是业主的活**。

    2026-09-30 实跑：输入「创建一个1111文档」+ 一个普通桌面目录，页面上给的下一步
    是 `git init && git add -A && git commit`。对一个用界面的人来说那不是限制，
    是死路（他的原话："怎么填都不行"）。现在前提还在（批次确实要基线），
    但满足前提的动作搬进了按钮。
    """

    def test_plain_folder_offers_to_build_the_repo_instead_of_homework(
            self, tmp_path, monkeypatch):
        from tools import batch_project as bp
        ctx = make_ctx()
        plain = tmp_path / "项目文件"
        plain.mkdir()
        monkeypatch.setattr(bp, "plan",
                            lambda *a, **k: pytest.fail("不该花一次 Supervisor 调用"))
        text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=str(plain)))
        assert bad
        assert "建仓库并开工" in text, "要给的是按钮，不是终端作业"
        assert "git init &&" not in text
        assert "没有调用任何 agent" in text
        assert ctx.runner.calls == []
        assert ctx.go_init_hint.get("prompt") == GOOD["prompt"]
        assert not (plain / ".git").exists(), "没点按钮之前不许动这个目录"

    def test_clicking_the_button_builds_the_repo_and_starts_the_run(
            self, tmp_path, monkeypatch):
        from tools import batch_project as bp
        ctx = make_ctx()
        plain = tmp_path / "新项目"
        plain.mkdir()
        spec = ('{"name":"doc","workspace":"'
                + str(plain).replace("\\", "/")
                + '","strategy":"COPY","config_dir":"examples/config_minimal",'
                  '"milestones":[{"id":"m1",'
                  '"goal":"在工作区根新建 说明.md，写清这一格交付了什么",'
                  '"acceptance":"test -f 说明.md"}]}')
        real_load_spec = bp.load_spec
        monkeypatch.setattr(wb, "_planned_target", lambda ws: tmp_path / "x.json")
        monkeypatch.setattr(bp, "plan",
                            lambda target, goal, **kw: (target.write_text(
                                spec, encoding="utf-8"), 0)[1])
        monkeypatch.setattr(bp, "load_spec",
                            lambda p: real_load_spec(tmp_path / "x.json"))
        monkeypatch.setattr(bp, "load_state", lambda s: {"milestones": {}})
        monkeypatch.setattr(bp, "submit_next", lambda *a, **k: 0)

        text, bad = wb.go_from_form(
            ctx, dict(GOOD, workspace=str(plain), init_repo="1"))
        assert bad is False, text
        assert "建好 git 仓库" in text and "切成 1 格" in text
        assert (plain / ".git" / "HEAD").is_file()
        assert wb.git_workspace_problem(str(plain)) == ""   # 第二次不用再建

    def test_an_empty_repo_gets_a_baseline_commit(self, tmp_path):
        empty = make_repo(tmp_path / "empty", commit=False)
        assert "还没有任何提交" in wb.git_workspace_problem(str(empty))
        ok, msg = wb.init_repo_here(str(empty))
        assert ok, msg
        assert wb.git_workspace_problem(str(empty)) == ""

    def test_a_totally_empty_folder_still_gets_a_baseline(self, tmp_path):
        """`git add -A` 在空目录里什么都没有 —— 基线提交必须 --allow-empty。"""
        blank = tmp_path / "完全空的目录"
        blank.mkdir()
        ok, msg = wb.init_repo_here(str(blank))
        assert ok, msg
        assert wb.git_workspace_problem(str(blank)) == ""

    def test_subdirectory_of_another_repo_is_refused_without_a_button(
            self, tmp_path):
        parent = make_repo(tmp_path / "parent")
        child = parent / "sub"
        child.mkdir()
        kind, msg = wb.repo_problem(str(child))
        assert kind == "foreign-repo"
        assert "子目录" in msg and str(parent.resolve()) in msg
        ctx = make_ctx()
        text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=str(child)))
        assert bad and ctx.go_init_hint == {}, "别人的仓库不许替人 init"

    def test_a_real_repo_passes(self, tmp_path):
        good = make_repo(tmp_path / "good")
        assert wb.git_workspace_problem(str(good)) == ""
        assert wb.repo_problem(str(good)) == ("", "")

    def test_a_missing_landing_dir_is_named_and_never_raises(self, tmp_path):
        """业主手上真有一份这样的清单：昨天切出来的，落地目录今天不在了。

        `subprocess.run(cwd=<不存在的目录>)` 是直接抛 NotADirectoryError 的 ——
        让它从网页处理函数里逃出去，代价是这条连接被掐断、页面上什么都看不到
        （地雷 28），而不是一个 500。
        """
        gone = tmp_path / "项目文件"
        assert wb.repo_problem(str(gone))[0] == "missing-dir"
        assert wb.repo_problem("")[0] == "missing-dir"
        assert "已经不在了" in wb.repo_problem(str(gone))[1]
        assert wb.init_repo_here(str(gone))[0] is False

    def test_the_one_press_builds_the_repo_and_starts_the_run(
            self, tmp_path, monkeypatch):
        """旧版这一条打的是"从已切好的清单开工"那一格的第二颗按钮
        （`start_plan_from_plan`）。一页版没有第二次点击 —— `start_go` 自己带上
        `init_repo`，于是同一件事的判据变成：**同一下**就把仓库建出来并开工
        （地雷 39：程序替人做了一步，后面依赖这一步的地方要一起兜住）。
        """
        app = make_app(tmp_path, monkeypatch)
        plain = tmp_path / "planned-ws"
        plain.mkdir()
        order, target, ship_factory = _patch_plan(
            monkeypatch, tmp_path, _spec_for(plain))
        app.wb.ship_factory = ship_factory

        text, bad, project = A.start_go(
            app, {"prompt": GOOD["prompt"], "workspace": str(plain)})
        assert bad is False, text
        assert "建好 git 仓库" in text and "切成 2 格" in text
        assert (plain / ".git" / "HEAD").is_file()
        assert wb.git_workspace_problem(str(plain)) == ""   # 第二次不用再建
        assert project == str(target), "这一批的身份没交回页面 = 下一格没人认得它"
        assert order == ["queue", "ship-start"], order

    def test_queue_step_crash_becomes_a_receipt_not_a_dead_connection(
            self, monkeypatch, tmp_path):
        from tools import batch_project as bp
        ctx = make_ctx()
        real_load_spec = bp.load_spec
        repo = make_repo(tmp_path / "ws")        # 仓库合格，才轮得到测入队那一步
        one = ('{"name":"site","workspace":"' + str(repo).replace("\\", "/") + \
               '","strategy":"COPY",'
               '"config_dir":"examples/config_minimal",'
               '"milestones":[{"id":"m1","goal":"新建 index.html 并保证标题是中文站点名",'
               '"acceptance":"pytest -q"}]}')
        (tmp_path / "p.json").write_text(one, encoding="utf-8")
        monkeypatch.setattr(wb, "_planned_target", lambda ws: tmp_path / "x.json")
        monkeypatch.setattr(bp, "plan",
                            lambda target, goal, **kw: (target.write_text(
                                one, encoding="utf-8"), 0)[1])
        monkeypatch.setattr(bp, "load_spec", lambda p: real_load_spec(tmp_path / "p.json"))
        monkeypatch.setattr(bp, "load_state", lambda spec: {"milestones": {}})

        def boom(*a, **k):
            raise bp.BatchError("读不到 workspace 的 HEAD：fatal: not a git repository")

        monkeypatch.setattr(bp, "submit_next", boom)
        text, bad = wb.go_from_form(ctx, dict(GOOD, workspace=str(repo)))
        assert bad and "第一格没能入队" in text and "HEAD" in text
        assert ctx.runner.calls == []          # 入队失败绝不启动调度器


class TestTheGoRouteKeepsTheJudgment:
    """/go 是"从一句话开工"这一条路的唯一入口（旧版第二张表单 `start_plan_from_plan`
    搬到这里）。判据还是那三条：先入队再动调度器、这一批的身份要交回页面、
    仓库不合格就不许花钱。
    """

    def test_it_queues_the_first_slice_then_starts(self, monkeypatch, tmp_path):
        """过一遍真 HTTP：按下『开工』之后，页面必须**认得出**这一批。

        旧版这条打在 start_plan_from_plan 上（那一格会把项目档记进 ctx）。一页版
        记的是 `app.project`，而"现在到哪了"那一格读的就是它 —— 没记进去就是
        跑完了没人看得见（地雷 29、42 的同一形状）。
        """
        app = make_app(tmp_path, monkeypatch)
        repo = make_repo(tmp_path / "ws")
        order, _target, ship_factory = _patch_plan(
            monkeypatch, tmp_path,
            _spec_for(repo, name="ztt", ids=("m1-one", "m2-two")), fname="ztt")
        app.wb.ship_factory = ship_factory
        httpd = _serve(app)
        try:
            port = httpd.server_address[1]
            code, _body = _call(port, "POST", "/go",
                                "prompt=" + urllib.parse.quote(GOOD["prompt"])
                                + "&workspace=" + urllib.parse.quote(str(repo)))
            assert code == 303, f"开工直接回了 {code}，没有走 303"
            assert app.project.endswith("ztt.project.json"), app.project
            assert order == ["queue", "ship-start"], order
            assert app.runner.calls == ["start"], "先入队才动调度器 —— 顺序不能反"
            _c, page = _call(port, "GET", "/")
            assert "m1-one" in page and "推进器在跑" in page
            assert "中间不问你" in page and "只有你合入上一格" not in page
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_missing_project_file_is_a_receipt(self, tmp_path, monkeypatch):
        """读不到项目档要说得出为什么，也不许把异常抛到浏览器（地雷 28、44）。"""
        app = make_app(tmp_path, monkeypatch)
        app.project = str(tmp_path / "runtime_batch" / "ztt.project.json")
        text = A.status_block(app)
        assert "项目档不在了" in text
        assert "还没有开始过任何一批" not in text, "把读不到说成没干过 = 页面在撒谎"

    def test_non_git_workspace_blocks_before_queueing(self, monkeypatch, tmp_path):
        """仓库这一格仍然先问再花钱 —— 而且别人的仓库连"替你建"都不许做（地雷 35
        留下的唯一一道人的门槛）。"""
        from tools import batch_project as bp

        app = make_app(tmp_path, monkeypatch)
        parent = make_repo(tmp_path / "parent")
        child = parent / "sub"
        child.mkdir()
        monkeypatch.setattr(bp, "plan",
                            lambda *a, **k: pytest.fail("仓库不合格就不该切分（那一次要花钱）"))
        monkeypatch.setattr(bp, "submit_next",
                            lambda *a, **k: pytest.fail("仓库不合格就不该入队"))
        text, bad, project = A.start_go(
            app, {"prompt": GOOD["prompt"], "workspace": str(child)})
        assert bad and project == ""
        assert "还没有调用任何 agent" in text and "子目录" in text
        assert "git init &&" not in text, "不再把终端作业当下一步"
        assert not (child / ".git").exists(), "别人的仓库不许替人 init"
        assert app.runner.calls == []
