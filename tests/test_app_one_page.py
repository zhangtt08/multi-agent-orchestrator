"""一页版工作台（tools/app.py）的守卫。

这些判据是从被替换掉的那几页里**搬过来**的，不是新发明的：
 - 抄图不抄数（地雷 21）：界面上每个数字都要有来源，缺就写"没有记录"；
 - 渲染前后队列库字节相同、缺库不许被顺手创建（地雷 21）；
 - `running` 是方法不是 property（地雷 6、30）：这一格用真类当哨兵；
 - 落地页就是打字那一格（地雷 35 的那次实测）；
 - workbench 引用不存在的符号（地雷 37）：跨文件引用守卫换成 app→workbench；
 - 网页处理函数不许让异常逃出去（地雷 28）：每个 POST 分支都要留下一句话。
"""
from __future__ import annotations

import http.client
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from tools import app as A
from tools import rehearsal as RH                                    # noqa: E402
from tools import workbench as wb                              # noqa: E402

SETTINGS = ("runtime_dir: runtime\n"
            "scheduler:\n  enabled: true\n"
            "  db_path: ./runtime/queue.db\n  attempts_root: runtime\n"
            "checkpoint:\n  enabled: true\n")


class FakeRunner:
    """形状必须和真 `SchedulerRunner` 一致：`running` 是**方法**。

    地雷 30 抓到两次：替身把 running 写成布尔属性，矩阵全绿，生产里子进程从来没起来。
    """

    def __init__(self, up: bool = False) -> None:
        self._up = up

    @property
    def log_path(self) -> Path:
        return ROOT / "runtime_workbench" / "scheduler.log"

    def running(self) -> bool:
        return self._up

    def tail(self, lines: int = 40):
        return []


def make_app(tmp_path: Path, up: bool = False) -> A.App:
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    (cfg / "agents.yaml").write_text(
        "supervisor:\n  provider: mock_supervisor\n", encoding="utf-8")
    app = A.App(config_dir=str(cfg), runner=FakeRunner(up), real_roles=False)
    app.wb.forced_workspace = ""
    return app


def write_batch(app: A.App, milestone_status: str = "failed") -> Path:
    """造一份现场：项目档 + 批次状态，落在 app 的 config 之外（runtime_batch 是仓库根）。"""
    import json

    name = "guard-batch"
    # `check_spec` 的机械判据里有"落地目录必须是真目录"，夹具就得把它造出来，
    # 否则页面读到的是"读不动"而不是这一批的现场（地雷 38：测试不许靠环境过关）。
    (ROOT / "runtime_batch" / "guard-ws").mkdir(parents=True, exist_ok=True)
    planned = ROOT / "runtime_batch" / "planned"
    planned.mkdir(parents=True, exist_ok=True)
    spec_path = planned / f"{name}.project.json"
    spec_path.write_text(json.dumps({
        "name": name, "workspace": str(ROOT / "runtime_batch" / "guard-ws"),
        "strategy": "COPY", "config_dir": app.config_dir, "max_rounds": 2,
        "constraints": "", "mode": "auto",
        "final_acceptance": {"name": "全套测试", "command": ["pytest", "-q"]},
        "owner_goal": "做一份上线说明",
        "milestones": [{"id": "m1", "goal": "写一份能照着做的上线说明，分步骤列清楚",
                        "acceptance": "pytest -q",
                        "constraints": "", "depends_on": []},
                       {"id": "m2", "goal": "给说明补上每一步的界面截图与图注",
                        "acceptance": "pytest -q",
                        "constraints": "", "depends_on": []}],
    }, ensure_ascii=False), encoding="utf-8")
    state_path = ROOT / "runtime_batch" / f"{name}.json"
    state_path.write_text(json.dumps({
        "name": name, "workspace": str(ROOT / "runtime_batch" / "guard-ws"),
        "created_at": "2026-10-08T00:00:00", "owner_goal": "做一份上线说明",
        "directives": [{"text": "标题要用中文", "at": "2026-10-08T00:00:00"}],
        "milestones": {"m1": {"status": "done", "runtime_task_id": "rt-aaaa",
                              "patch_lines": 12},
                       "m2": {"status": milestone_status, "detail": "changes.patch 0 行 [框架]",
                              "runtime_task_id": "rt-bbbb"}},
        "final": {"status": "not-run"},
    }, ensure_ascii=False), encoding="utf-8")
    app.project = str(spec_path)
    return state_path


@pytest.fixture()
def batch(tmp_path):
    app = make_app(tmp_path)
    state = write_batch(app)
    try:
        yield app, state
    finally:
        import shutil

        spec = Path(app.project)
        spec.unlink(missing_ok=True)
        state.unlink(missing_ok=True)
        for leftover in (ROOT / "runtime_batch" / "guard-batch",
                         ROOT / "runtime_batch" / "guard-ws"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)


@pytest.fixture(autouse=True)
def _never_write_the_owners_desktop(tmp_path, monkeypatch):
    """默认落地目录在业主桌面上 —— 测试一次都不许往那儿建文件夹。"""
    monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path)


class TestOnePage:
    def test_the_landing_page_is_the_place_where_you_type(self, batch):
        app, _ = batch
        page = A.page(app)
        assert "<textarea name='prompt'" in page,             "第一屏必须能打字；否则就是又一页看不懂的面板"

    def test_there_is_exactly_one_page(self, batch):
        app, _ = batch
        page = A.page(app)
        assert "<a href" not in page,             "页面上出现链接就是第二页；一页版不许有第二页"
        for section in ("说一句话", "调用哪个 agent", "现在到哪了", "中途改需求", "交付结果"):
            assert section in page

    def test_before_any_batch_the_page_asks_only_two_things(self, tmp_path,
                                                            monkeypatch):
        """没开工就不摆"中途改需求/交付结果"那两格 —— 没有输入对象的输入框是噪声。"""
        monkeypatch.setattr(A, "ROOT", tmp_path)   # 别把仓库里留下的那一批读进来
        app = make_app(tmp_path)
        page = A.page(app)
        assert "说一句话" in page and "调用哪个 agent" in page
        assert "中途改需求" not in page
        assert "交付结果" not in page

    def test_the_desktop_shell_is_told_the_truth_about_stopping(self, tmp_path,
                                                                monkeypatch):
        """exe 那一档里没有终端可以按 Ctrl+C —— 话要按外壳实际是谁来说。"""
        app = make_app(tmp_path)
        monkeypatch.setenv("MAO_DESKTOP", "1")
        page = A.page(app)
        assert "Ctrl+C" not in page,             "桌面外壳里写 Ctrl+C = 教人做一件做不到的事"
        assert "runtime_desktop" in page and "关掉这个窗口 = 停手" in page
        monkeypatch.delenv("MAO_DESKTOP")
        assert "Ctrl+C" in A.page(make_app(tmp_path)),             (
            "浏览器那一档仍然有 Ctrl+C，两档各说各的")

    def test_the_rehearsal_tier_never_claims_a_cost_on_the_same_page(self, tmp_path):
        """假 agent 的三个 profile 也 `supports_cli: true`，所以 `real_roles` 在彩排档
        同样为真 —— 档位那一行必须按**这一档是什么**说，不能同一页既说不花钱又说花钱。"""
        app = make_app(tmp_path)
        app.rehearsal = True
        app.real_roles = True
        page = A.page(app)
        assert "不消耗订阅额度" in page
        assert "会消耗订阅额度" not in page,             "同一页给两句相反的代价说明 = 没有说明"

    def test_a_rehearsal_start_does_not_build_a_folder_on_the_desktop(
            self, tmp_path, monkeypatch):
        """彩排档的落地目录被定死在临时目录里；那一格不该在业主桌面上留下空文件夹。

        `go_from_form` 用替身：这一条判据要量的是 start_go 的**目录处理**，
        不是配置能不能加载（夹具那份 config 只有 settings/agents，没有 harness）。
        """
        app = make_app(tmp_path)
        app.rehearsal = True
        forced = tmp_path / "rehearsal-ws"
        forced.mkdir()
        app.wb.forced_workspace = str(forced)
        desk = tmp_path / "Desktop"
        desk.mkdir()
        stage = tmp_path / "stages" / "ws-2"
        monkeypatch.setattr(A, "desktop_dir", lambda: desk)
        monkeypatch.setattr(RH, "stage_dir", lambda: stage)
        got = {}

        def fake_go(ctx, fields):
            got.update(fields)
            return ("已经按这张清单开工", False)
        monkeypatch.setattr(A.wb, "go_from_form", fake_go)
        text, bad, project = A.start_go(app, {"prompt": "做一个能看照片的网页",
                                              "workspace": ""})
        assert not bad
        assert got.get("workspace") == str(stage),             (
            f"彩排档把落地目录交成了 {got.get('workspace')!r}，而不是演练档自己那一格")
        assert not str(got.get("workspace") or "").startswith(str(desk)), (
            "彩排档的落地目录跑到了业主桌面上")
        assert not (desk / "MAO-交付").exists(),             (
            "彩排档在业主桌面上建了一个永远为空的文件夹 —— 那一格会被替换掉，"
            "提前建它就是在替人制造垃圾")

    def test_the_second_press_does_not_inherit_the_first_stage(
            self, tmp_path, monkeypatch):
        """连按两次『开工』：第二次必须换一格。

        实测（2026-10-08）：`forced_workspace` 是开机时算的一次，第一跑把那一格
        变成了仓库、内容也合了进去；沿用它的第二跑，假 agent 写的是**同一份固定
        内容**，于是 `changes.patch` 0 行、闸门拒绝 —— 免费那一档（业主第一个按的
        东西）报出一个"未确认"，而那件事没有任何人做错。地雷 42 的同一条形状：
        "开机那一刻算出来的状态"不能代替"这一次动作的状态"。
        """
        app = make_app(tmp_path)
        app.rehearsal = True
        first = tmp_path / "stages" / "ws"
        first.mkdir(parents=True)
        app.wb.forced_workspace = str(first)
        second = tmp_path / "stages" / "ws-2"
        monkeypatch.setattr(RH, "stage_dir", lambda: second)
        got = {}

        def fake_go(ctx, fields):
            got.update(fields)
            return ("已经按这张清单开工", False)
        monkeypatch.setattr(A.wb, "go_from_form", fake_go)
        A.start_go(app, {"prompt": "再演一遍", "workspace": ""})
        assert got.get("workspace") == str(second)
        assert app.wb.forced_workspace == str(second),             (
            "页面上印的还是上一跑的目录 —— 人按了第二次却看到第一次的路径")

    def test_the_rehearsal_press_carries_the_repo_prerequisite(self, tmp_path,
                                                               monkeypatch):
        """彩排档也带 `init_repo`：演练格按设计**不是**仓库（按钮要能演到），
        少这一句就等于免费那一档按一次只得到一道追问。
        """
        app = make_app(tmp_path)
        app.rehearsal = True
        app.wb.forced_workspace = str(tmp_path / "ws")
        stage = tmp_path / "ws-new"
        monkeypatch.setattr(RH, "stage_dir", lambda: stage)
        got = {}

        def fake_go(ctx, fields):
            got.update(fields)
            return ("已经按这张清单开工", False)
        monkeypatch.setattr(A.wb, "go_from_form", fake_go)
        A.start_go(app, {"prompt": "做一个能看照片的网页", "workspace": ""})
        assert got.get("init_repo") == "1"

    def test_the_rehearsal_tier_says_it_costs_nothing(self, tmp_path):
        app = make_app(tmp_path)
        app.rehearsal = True
        assert "不消耗任何订阅额度" in A.page(app)

    def test_the_two_panes_show_what_each_side_got_and_its_heartbeat(self, batch):
        """任务一开跑，这一页就同时给出两边：验收那边收到什么、执行那边收到什么。

        判据不许是"页面上出现了某个词"，要盯实质：简报**原文**、它的字数与 SHA、
        租约的两种结论（有人心跳 / 租约过期）各自只在对应现场出现一次。
        """
        import json

        app, state_path = batch
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["milestones"]["m2"]["status"] = "running"
        st["milestones"]["m2"]["prompt"] = (
            "请在本项目根创建 hello.md，内容一行：hello from opencode。"
            "验收项：文件存在且内容一致。")
        st["milestones"]["m2"]["prompt_sha256"] = "deadbeef1234deadbeef"
        st["milestones"]["m2"]["runtime_task_id"] = "rt-bbbb"
        state_path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        card = A.pane_card(app)
        assert "两边窗口" in card
        assert card in A.page(app),             "只在函数里绿一次不算界面答了问题 —— 拔掉这一格测试必须红"
        assert "m2" in card and "正在跑" in card
        assert "请在本项目根创建 hello.md" in card, "发过去的简报必须露原文，不能只给状态"
        assert "deadbeef" in card and "字" in card
        assert "验收 agent 这边" in card and "执行 agent 这边" in card

    def test_a_stale_lease_says_so_instead_of_looking_like_progress(self, batch):
        """「卡了还是出问题了一目了然」：租约过期这一条要自己说出来，
        不能和"正在跑"共用一句话（地雷 42 的判据形状）。"""
        import json

        app, state_path = batch
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st["milestones"]["m2"]["status"] = "running"
        st["milestones"]["m2"]["prompt"] = "把说明写完"
        state_path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

        def fake_rows(ids):
            app.missing_rows = []
            return {"rt-bbbb": {"status": "RUNNING", "lease_state": "stale",
                                "stage": "EXECUTING", "patch_lines": 0,
                                "last_error": "", "terminal": False}}
        app.queue_rows = fake_rows            # 只换读队列这一层，判据仍在框架那侧
        card = A.pane_card(app)
        assert "租约过期" in card and "没了" in card, card[:400]
        assert "心跳正常" not in card

    def test_the_pane_card_stays_out_of_the_way_when_nothing_is_live(self, batch):
        """没有活着的格子时这格不出现 —— 它不是常驻的第四段说明书。"""
        app, _ = batch
        assert A.pane_card(app) == ""

    def test_the_page_does_not_drown_the_owner_in_words(self, tmp_path, monkeypatch):
        """业主这条要求是"文字太多了，提示没必要这么多" —— 那就把它变成一个数。

        一个"精简"的要求如果只写在文档里，下一轮加功能时一定会涨回去
        （这一格从 114 字涨到 942 字就是这么发生的）。所以这里钉两件事：
        整页说明文字有上限；同一句解释不许在几个角色/几个 app 上重复多遍。
        观测量的是**渲染出来的那一页**，不是源码里的字符串常量。
        """
        import re

        app = make_app(tmp_path)
        roles = [{"role": r, "profile": f"p_{r}", "description": "档",
                  "path": "C:\\x\\cli.exe", "reason": "", "login": "logged-in",
                  "login_detail": "", "env_keys": [], "needs": [], "writable": True}
                 for r in ("supervisor", "executor", "reviewer")]
        profiles = [{"profile": "p_supervisor", "description": "档", "path": "C:\\x\\a.exe",
                     "reason": "", "login": "logged-in", "login_detail": "", "env_keys": [],
                     "needs": [], "writable": True},
                    {"profile": "p_executor", "description": "档", "path": "C:\\x\\b.exe",
                     "reason": "", "login": "logged-in", "login_detail": "", "env_keys": [],
                     "needs": [], "writable": True},
                    {"profile": "p_reviewer", "description": "档", "path": "C:\\x\\c.exe",
                     "reason": "", "login": "logged-in", "login_detail": "", "env_keys": [],
                     "needs": [], "writable": True}]
        monkeypatch.setattr(A.agent_probe, "catalog",
                            lambda config_dir: {"roles": roles, "profiles": profiles})
        monkeypatch.setattr(A.agent_probe, "role_facts", lambda config_dir: roles)
        monkeypatch.setattr(A.agent_probe, "cli_own_endpoint_facts", lambda: [])
        monkeypatch.setattr(A.cli_census, "census", lambda home=None: {
            "rows": [], "switch_installed": False, "switch_reason": "本机没有 cc-switch",
            "switch_currents": {}, "problem": ""})
        monkeypatch.setattr(A.rw, "bindings",
                            lambda root, cd: {r: f"p_{r}" for r in A.rw.ROLES})
        monkeypatch.setattr(A.rw, "capacity_keys", lambda root, cd: {"p_supervisor": 1})
        page = A.page(app)
        # 只数"说给人看的话"：去掉控件文本之外的中文字符
        text = re.sub(r"<[^>]+>", " ", page)
        chars = len(re.findall(r"[一-鿿]", text))
        assert chars <= 1350, f"整页说明文字 {chars} 字 —— 业主说的是字太多，不是字不够"
        for phrase in ("这一类要它有命令行入口或 API 端点才能被驱动",
                       "按没实测过的参数起一次就是一次白烧的额度"):
            assert text.count(phrase) <= 1, (
                f"同一句解释重复了 {text.count(phrase)} 遍：{phrase} —— 合并成一句脚注")
        assert "这台机器上还有什么" in page, (
            "字要少，但那一列不能因为嫌啰嗦被删掉 —— 它是「能不能用」的答案")

    def test_the_real_tier_says_it_costs_quota(self, tmp_path):
        app = make_app(tmp_path)
        app.real_roles = True
        assert "会消耗订阅额度" in A.page(app)


class TestNumbersAreNotDecorations:
    def test_no_invented_counters_appear(self, batch):
        app, _ = batch
        page = A.page(app)
        for fake in ("Active 3", "Completed 18", "98%", "T-1040", "5/5 tests",
                     "12 files", "在线", "Online"):
            assert fake not in page, f"界面上出现了 {fake} —— 这一格没有对应数据源"

    def test_a_task_the_queue_db_does_not_know_is_reported_as_such(self, batch):
        app, _ = batch
        page = A.page(app)
        # 这两条运行确实不在替身配置的队列库里；页面要说清读的是哪个库，
        # 不许把"查不到"渲染成"没干活"，也不许静默留空。
        assert "查不到" in page and ("queue.db" in page or "没有队列库" in page),             "缺证据必须说得出在读哪一份（读不到库也要明说）"

    def test_an_unreadable_project_says_why_instead_of_claiming_nothing_ran(
            self, batch):
        app, _ = batch
        bad = Path(app.project)
        keep = bad.read_text(encoding="utf-8")
        bad.write_text(keep.replace("写一份能照着做的上线说明，分步骤列清楚", "短"),
                       encoding="utf-8")
        try:
            page = A.page(app)
        finally:
            bad.write_text(keep, encoding="utf-8")
        assert "还没有开始过任何一批" not in page,             (
            "项目档读不动时页面不许说「还没开始过」—— 那是把缺陷说成空白")
        assert "这一批现在开不了" in page and "goal" in page

    def test_rendering_never_creates_a_missing_queue(self, batch):
        app, _ = batch
        db = Path(app.config_dir) / "runtime" / "queue.db"
        assert not db.exists()
        A.page(app)
        assert not db.exists(),             "读一页就把队列库建出来 = 在别人的配置里凭空造一台"

    def test_rendering_does_not_rewrite_the_state_file(self, batch):
        app, state_path = batch
        before = state_path.read_bytes()
        A.page(app)
        assert state_path.read_bytes() == before,             "只读的观测不许改现场（证据是现场不是缓存）"


class TestJudgmentShapes:
    def test_the_real_runner_class_is_the_sentinel_not_the_double(self):
        from tools.scheduler_cli import SchedulerRunner

        assert not isinstance(SchedulerRunner.running, property),             (
            "`running` 变成 property 就是地雷 30 的同一件事：调用点写成 `.running` "
            "拿到 bound method、恒真，页面从此说「调度器在跑」")
        assert callable(SchedulerRunner.running)

    def test_app_only_reaches_for_symbols_workbench_actually_has(self):
        """app.py 里每个 `wb.<名字>` 都必须在 workbench.py 真的定义过（地雷 37）。"""
        import ast
        import re

        used = set(re.findall(r"\bwb\.([A-Za-z_][A-Za-z0-9_]*)",
                              (ROOT / "tools" / "app.py").read_text(encoding="utf-8")))
        tree = ast.parse((ROOT / "tools" / "workbench.py").read_text(encoding="utf-8"))
        have = {n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        have |= {t.target.id for n in tree.body if isinstance(n, ast.ClassDef)
                 for t in n.body if isinstance(t, ast.AnnAssign)
                 and isinstance(t.target, ast.Name)}
        have |= {a.name for n in tree.body if isinstance(n, ast.ImportFrom)
                 for a in n.names}
        have |= {t.id for n in tree.body if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)}
        have |= {t.target.id for n in tree.body if isinstance(n, ast.AnnAssign)
                 and isinstance(n.target, ast.Name)}
        missing = sorted(u for u in used if u not in have)
        assert not missing, f"app.py 引用了 workbench 里不存在的 {missing}"

    def test_workbench_no_longer_holds_a_web_layer(self):
        """页面只有一处：workbench.py 不再自己起网页，也不再有第二套渲染。"""
        import ast

        tree = ast.parse((ROOT / "tools" / "workbench.py").read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assert not (names & {"serve", "main", "make_handler", "render_home",
                             "render_run", "render_batches", "render_form",
                             "render_go_form", "render_plan_form"})
        assert not (ROOT / "tools" / "workbench_ui.py").exists()
        assert not (ROOT / "tools" / "workbench_flow.py").exists()

    def test_the_retry_button_is_an_action_not_a_command_line_suggestion(self, batch):
        app, _ = batch
        page = A.page(app)
        assert "再跑这一格" in page, "停下来的那一格必须有按钮；写「去命令行 retry」就是地雷 35 的那道墙"
        assert "batch_project.py run --retry" not in page

    def test_a_real_retry_needs_the_press_that_spends_money(self, batch, monkeypatch):
        app, _ = batch
        app.real_roles = True
        calls = []
        monkeypatch.setattr(A.bp, "retry",
                            lambda *a, **k: calls.append(a) or 0)
        text, bad = A.retry_milestone(app, "m2", "0")
        assert not calls, "第一下只说明代价，不许真的重跑"
        assert "额度" in text and not bad
        text2, bad2 = A.retry_milestone(app, "m2", "1")
        assert calls and not bad2

    def test_a_milestone_waiting_for_merge_gets_the_merge_button(self, batch):
        """`mode: human` 那一档以前停在 awaiting-merge 只能回终端敲 accept ——
        这一格把动作搬回页面（地雷 35），语义不变：合入仍由人点头。"""
        import json
        app, state_path = batch
        doc = json.loads(state_path.read_text(encoding="utf-8"))
        doc["milestones"]["m2"]["status"] = "awaiting-merge"
        doc["milestones"]["m2"]["patch_lines"] = 27
        state_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        page = A.page(app)
        assert "把这一格合入那个文件夹（补丁 27 行）" in page
        assert "再跑这一格" not in page,             "等合入的那一格不是失败格，不该给重跑按钮"

    def test_the_merge_press_is_the_human_authorization_not_the_agent_one(self, batch,
                                                                         monkeypatch):
        import json
        app, state_path = batch
        doc = json.loads(state_path.read_text(encoding="utf-8"))
        doc["milestones"]["m2"]["status"] = "awaiting-merge"
        state_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        seen = {}

        def fake_accept(spec, state, confirmed=False, echo=None, ask=None,
                        authorized_by="human"):
            seen["confirmed"] = confirmed
            seen["authorized_by"] = authorized_by
            return 0
        monkeypatch.setattr(A.bp, "accept", fake_accept)
        text, bad = A.merge_milestone(app, "m2")
        assert seen == {"confirmed": True, "authorized_by": "human"},             (
                "人按的那一下记成人的授权；替推进器伪装成 agent-review 就是把门挪了")
        assert not bad
        # 已经合过的那一格不许再给一次合入 —— 那是替人多做一次决定
        text2, bad2 = A.merge_milestone(app, "m1")
        assert bad2 and "已合入交付" in text2

    def test_the_steer_box_writes_through_the_one_function_that_owns_it(self, batch,
                                                                        monkeypatch):
        """中途改需求只许走 `steer_batch`（它一次写两层：批次 directives + 当前运行的
        task_directives）。页面里自己拼一份就等于埋下"下一格又变回去了"那个洞（地雷 34）。"""
        app, _ = batch
        seen = {}
        monkeypatch.setattr(A.bp, "steer_batch",
                            lambda spec, state, text, echo=None:
                            seen.update(spec=spec, text=text) or 0)
        text, bad = A.say(app, "整站改成深色主题")
        assert not bad
        assert seen["text"] == "整站改成深色主题"
        assert seen["spec"]["name"] == "guard-batch",             (
            "传进来的必须是这一批的项目档，不是页面自己拼的字典")
        assert "下一格" in text, "生效时机要说出来，否则人会以为正在跑的调用被打断了"

    def test_a_rejected_start_keeps_what_the_owner_just_typed(self, tmp_path,
                                                              monkeypatch):
        """目录建不出来是被拒的一次，不是让人重敲一次的理由。"""
        monkeypatch.setattr(A, "ROOT", tmp_path)
        app = make_app(tmp_path)
        occupant = tmp_path / "occupant"
        occupant.write_text("x", encoding="utf-8")
        bad_ws = str(occupant / "sub")
        text, bad, project = A.start_go(app, {"prompt": "做一个能看照片的网页",
                                              "workspace": bad_ws})
        assert bad and project == ""
        assert app.wb.last_go.get("workspace") == bad_ws
        page = A.page(app)
        assert bad_ws in page and "做一个能看照片的网页" in page,             (
            f"被拒之后输入没了（回执：{text[:80]}）")

    def test_the_page_shows_the_directives_already_recorded(self, batch):
        app, _ = batch
        page = A.page(app)
        assert "中途补充" in page or "标题要用中文" in page,             (
            "记下来的话要在页面上露面 —— 只写进文件不露面，人不知道自己改成功没有")

    def test_retry_refuses_a_milestone_that_did_not_fail(self, batch, monkeypatch):
        app, _ = batch
        monkeypatch.setattr(A.bp, "retry", lambda *a, **k: 0)
        text, bad = A.retry_milestone(app, "m1", "1")
        assert bad and "已合入交付" in text


class TestHttpEdges:
    @staticmethod
    def _serve(app):
        httpd = A.serve(app, 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    @staticmethod
    def _call(port, method, path, body="", host=None, origin="same"):
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

    def test_a_cross_origin_post_is_refused_before_anything_happens(self, batch):
        """花钱的那两个入口不能由别的网页驱动（甲方验收第 1 条 BLOCKER）。"""
        app, _ = batch
        httpd = self._serve(app)
        try:
            port = httpd.server_address[1]
            for path in ("/go", "/retry"):
                status, text, _ = self._call(port, "POST", path, "prompt=x",
                                             origin="http://evil.example")
                assert status == 403, f"{path} 被跨站表单驱动了：{status}"
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_every_post_branch_leaves_a_sentence(self, batch):
        """坏输入只能换回一句话，不能换回断掉的连接（地雷 28）。"""
        app, _ = batch
        httpd = self._serve(app)
        try:
            port = httpd.server_address[1]
            cases = [("/control", "rt=&action=wat"),
                     ("/say", "text="),
                     ("/retry", "milestone=nope&confirm=1"),
                     ("/stop", "action=stop"),
                     ("/go", "prompt=&workspace="),
                     ("/agents", "supervisor=&executor=&reviewer="),
                     ("/endpoint", "profile=&MAO_X=1"),
                     ("/profile", "name=&command=&extra_args=&prompt_mode=stdin"),
                     ("/recheck", "x=1"),
                     ("/nonsense", "x=1")]
            for path, body in cases:
                if path == "/nonsense":
                    status, _t, _h = self._call(port, "POST", path, body)
                    assert status == 404, f"{path} 应落到业务层的 404，得到 {status}"
                    continue
                status, text, hdrs = self._call(port, "POST", path, body)
                assert status in (303, 200), f"{path} 直接回了 {status}"
                back = self._call(port, "GET", "/")
                assert "notice" in back[1], (
                    f"{path} 之后页面上没有回执 —— 动作没有留下一句话")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_foreign_host_is_refused(self, batch):
        app, _ = batch
        httpd = self._serve(app)
        try:
            port = httpd.server_address[1]
            status, text, _ = self._call(port, "GET", "/", host="evil.example")
            assert status == 400, "Host 守卫松了 —— 这条防的是 DNS 重绑定"
            assert "不是本机回环地址" in text, text[:160]
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unknown_path_still_serves_the_one_page(self, batch):
        app, _ = batch
        httpd = self._serve(app)
        try:
            port = httpd.server_address[1]
            status, text, _ = self._call(port, "GET", "/ui/flow/rt-x")
            assert status == 404
            assert "<textarea name='prompt'" in text,             "404 也要把人放回打字那一格，而不是白页"
        finally:
            httpd.shutdown()
            httpd.server_close()


PICKER_SETTINGS = """runtime_dir: runtime
scheduler:
  enabled: true
  db_path: ./runtime-picker/queue.db
  attempts_root: runtime-picker
  capacity:
    global_agent_calls: 4
    providers:
      found_profile: 2
    provider_default: 1
checkpoint:
  enabled: true
"""

PICKER_AGENTS = """# 这一行注释是文档的一部分：换档不许把它抹平
supervisor:
  provider: generic_cli
  transport: subprocess
  harness_profile: missing_profile
executor:
  provider: generic_cli
  transport: subprocess
  harness_profile: found_profile
reviewer:
  provider: generic_cli
  transport: subprocess
  harness_profile: found_profile
"""


class TestRolePicker:
    """『调用哪个 agent』那一格的换档：清单只从 profile_registry 出，写只走
    `role_wiring` 那一扇门。

    夹具自己写 harness.yaml，所以判据不依赖这台机器装了哪个 CLI
    （地雷 38：靠环境过关的用例在别的机器上就是假的）。一个档指向本机真的
    存在的那个 python，另一个指向一个不存在的命令 —— 两种结论都有对应的那一行。
    """

    def make(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "settings.yaml").write_text(PICKER_SETTINGS, encoding="utf-8")
        (cfg / "agents.yaml").write_text(PICKER_AGENTS, encoding="utf-8")
        (cfg / "harness.yaml").write_text(
            "found_profile:\n"
            "  description: \"本机有的那个档\"\n"
            # YAML 双引号里反斜杠是转义（`\U` 直接报错），Windows 路径必须单引号
            f"  command: '{sys.executable}'\n"
            "  supports_cli: true\n"
            "  supports_file_write: true\n"
            "readonly_profile:\n"
            "  description: \"Supervisor：只读且本机找得到的那个档\"\n"
            f"  command: '{sys.executable}'\n"
            "  supports_cli: true\n"
            "  supports_file_write: false\n"
            "missing_profile:\n"
            "  description: \"本机找不到 CLI 的那个档\"\n"
            "  command: \"definitely-not-installed-cli\"\n"
            "  supports_cli: true\n"
            "  environment:\n"
            "    ANTHROPIC_BASE_URL: \"${FAKE_BASE_URL}\"\n"
            "    ANTHROPIC_AUTH_TOKEN: \"${FAKE_TOKEN}\"\n",
            encoding="utf-8")
        app = A.App(config_dir=str(cfg), runner=FakeRunner(), real_roles=True)
        app.wb.forced_workspace = ""
        return app, cfg / "agents.yaml"

    def test_the_options_are_the_registry_itself_and_say_what_this_machine_has(self, tmp_path):
        app, _ = self.make(tmp_path)
        strip = A.agent_strip(app)
        for role in ("supervisor", "executor", "reviewer"):
            assert f"<select name='{role}'>" in strip, f"{role} 没有可换的那一列"
        assert "found_profile" in strip and "missing_profile" in strip
        assert "本机找不到 CLI" in strip,             "找不到 CLI 的那个档必须自己说清楚，否则人只能按下去才知道"
        assert sys.executable in strip,             "必须露出发现层解析到的那一份可执行文件，不是只报档名"
        assert "no_such_profile" not in strip

    def test_choosing_the_same_three_writes_nothing(self, tmp_path):
        app, agents = self.make(tmp_path)
        before = agents.read_bytes()
        text, bad = A.apply_roles(app, {"supervisor": "missing_profile",
                                        "executor": "found_profile",
                                        "reviewer": "found_profile"})
        assert not bad and "没改" in text, text
        assert agents.read_bytes() == before, "三格都没动却写了文件 —— 一次点击不该改磁盘"

    def test_one_press_switches_the_roles_and_keeps_the_comments(self, tmp_path):
        from tools import role_wiring as rw

        app, agents = self.make(tmp_path)
        before = agents.read_text(encoding="utf-8").splitlines()
        text, bad = A.apply_roles(app, {"supervisor": "readonly_profile",
                                        "executor": "found_profile",
                                        "reviewer": "missing_profile"})
        assert not bad, text
        after = agents.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before),             "行数变了 = 那次改写动的不只是 harness_profile 那一行"
        assert "# 这一行注释是文档的一部分：换档不许把它抹平" in "\n".join(after)
        bound = rw.bindings(tmp_path, "config")
        assert bound["supervisor"] == "readonly_profile"
        assert bound["reviewer"] == "missing_profile"
        assert "静默" in text, "capacity.providers 里没有这个键这件事必须一起说出来（记过的地雷）"

    def test_the_badge_is_recomputed_from_the_same_judgment_after_a_switch(self, tmp_path):
        """换档之后"这一档会不会花钱"必须重新量，而且量的是引擎那一条判据。"""
        from mao.core.config import load_config
        from tools.env_report import uses_real_cli

        app, _ = self.make(tmp_path)
        app.real_roles = False                    # 故意留一个和现场不符的旧结论
        # supervisor 那一格要的是**只读**档（`role_wiring.eligibility` 判），
        # 这一条测的是 badge 重算，不是资格 —— 所以用夹具里那个只读档。
        _text, bad = A.apply_roles(app, {"supervisor": "readonly_profile"})
        assert not bad
        assert app.real_roles == bool(uses_real_cli(load_config(app.config_dir))), \
            "badge 说的和引擎将要调的不是同一件事"

    def test_a_profile_that_is_not_in_harness_yaml_never_reaches_the_file(self, tmp_path):
        """表单只给注册表里的那些档，但手搓的 POST 不能把一个不存在的档写进
        agents.yaml —— 下一次开工就在 load_config 上炸，而那一句回执已经没了。"""
        app, agents = self.make(tmp_path)
        before = agents.read_bytes()
        text, bad = A.apply_roles(app, {"supervisor": "no_such_profile"})
        assert bad, text
        assert agents.read_bytes() == before,             f"不存在的档被写进去了：{text}"
        assert "没写" in text, f"拒了但要说出拒了：{text}"

    # ------------------------------------------------------------------
    # 2026-10-09 业主实测的那一条：会写文件的档被绑进只读那一格
    # ------------------------------------------------------------------
    def test_a_writable_profile_never_lands_in_a_read_only_role(self, tmp_path):
        app, agents = self.make(tmp_path)
        before = agents.read_bytes()
        text, bad = A.apply_roles(app, {"supervisor": "found_profile"})
        assert bad, "会改文件的档被写进了『拆目标』那一格 —— 这正是他按『开工』没动静的绑法"
        assert agents.read_bytes() == before, f"拒了却还是动了文件：{text}"
        assert "只许读" in text, f"拒了但说不出为什么：{text}"

    def test_the_per_role_dropdown_lists_only_profiles_that_fit_that_role(
            self, tmp_path):
        import re as _re

        app, _ = self.make(tmp_path)
        strip = A.agent_strip(app)
        blocks = {m.group(1): m.group(2) for m in _re.finditer(
            r"<select name='(\w+)'>(.*?)</select>", strip, _re.S)}
        assert set(blocks) >= {"supervisor", "executor", "reviewer"}
        assert "found_profile" not in blocks["supervisor"],             \
            "执行档出现在拆目标那一格里 —— 下拉框必须按资格筛，不是把十档全列出来"
        assert "found_profile" in blocks["executor"]
        assert "readonly_profile" not in blocks["executor"],             \
            "不改文件的档当不了执行 agent（干活那格要落盘）"
        assert "readonly_profile" in blocks["reviewer"]

    def test_one_radio_binds_all_three_roles_at_once(self, tmp_path):
        """选一家 = 三格自动配好：这是"这么多个 agent 该怎么选择"的直接答案。"""
        from tools import role_wiring as rw

        app, _ = self.make(tmp_path)
        groups = rw.vendors(tmp_path, "config")
        whole = [g for g in groups if g.get("complete")]
        assert whole, "夹具里那两个档都指向本机的 python，应该凑得齐一家"
        text, bad = A.apply_roles(app, {"vendor": whole[0]["vendor"]})
        assert not bad, text
        bound = rw.bindings(tmp_path, "config")
        assert bound["executor"] == "found_profile"
        assert bound["supervisor"] == "readonly_profile"
        assert bound["reviewer"] == "readonly_profile"

    def test_an_unknown_or_incomplete_vendor_writes_nothing(self, tmp_path):
        app, agents = self.make(tmp_path)
        before = agents.read_bytes()
        text, bad = A.apply_roles(app, {"vendor": "no-such-vendor"})
        assert bad and agents.read_bytes() == before, text
        assert "凑不齐三格" in text

    def test_the_shared_base_is_not_offered_for_any_role(self, tmp_path):
        """`base_real_cli` 那一类**空壳底座**不能当角色用 —— 业主实测把它绑到了
        执行 agent 那一格：它没有自己的参数，等于不带 `-p` 起一个交互式 CLI，
        挂住并白烧一次额度。判据是结构的两条（被 extends + 自己没参数），
        不看档名里有没有 "base"。
        """
        from tools import role_wiring as rw

        app, agents = self.make(tmp_path)
        cfg = tmp_path / "config"
        cfg_h = cfg / "harness.yaml"
        cfg_h.write_text(
            cfg_h.read_text(encoding="utf-8")
            + "shell_base:\n"
              "  description: \"共享底座\"\n"
              f"  command: '{sys.executable}'\n"
              "  supports_cli: true\n"
              "  supports_file_write: true\n"
              "derived_role:\n"
              "  extends: shell_base\n"
              "  description: \"Executor：从底座派生的那一档\"\n"
              "  extra_args: [\"-p\"]\n",
            encoding="utf-8", newline="\n")
        before = agents.read_bytes()
        text, bad = A.apply_roles(app, {"executor": "shell_base"})
        assert bad and agents.read_bytes() == before,             \
            f"空壳底座被写进了执行格：{text}"
        assert "共享底座" in text, f"拒了但说不出为什么：{text}"
        # 派生那一档（有自己的参数）照旧可选 —— 判据不能宽到把能用的一起挡掉
        ok, why = rw.eligibility("executor", "derived_role", tmp_path, "config")
        assert ok, why

    def test_a_rehearsal_tier_says_why_it_has_no_switching(self, tmp_path):
        app, _ = self.make(tmp_path)
        app.rehearsal = True
        text, bad = A.apply_roles(app, {"supervisor": "found_profile"})
        assert bad and "rehearsal" in text, text

    def test_the_printed_url_is_the_port_that_actualy_answers(self, tmp_path):
        """`--port 0` 时打印的那一个端口必须是真的绑上号的那一个。

        这是业主会照抄的一行字：报请求里的 0 就等于给一个打不开的地址。
        判据不是"打印里有数字"，而是"那个端口真的有人应答"。
        """
        import re
        import subprocess
        import urllib.request

        cfg = tmp_path / "cfg"
        shutil.copytree(ROOT / "config", cfg)
        proc = subprocess.Popen(
            [sys.executable, "-u", "tools/app.py", "--port", "0",
             "--config-dir", str(cfg)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace")
        try:
            port = None
            for _ in range(60):
                line = proc.stdout.readline()
                if not line:
                    break
                m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
                if m:
                    port = int(m.group(1))
                    break
            assert port, "没打印出端口"
            assert port != 0, f"打印的是请求里那个 0：{port}"
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=15) as r:
                assert r.status == 200, f"打印的那个端口没人应答：{port}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()

    def test_recheck_leaves_a_sentence_even_when_the_config_is_unreadable(self, tmp_path):
        app, _ = self.make(tmp_path)
        text, bad = A.recheck_login(app)
        assert not bad and "重新检测" in text, text
        broken = A.App(config_dir=str(tmp_path / "nope"), runner=FakeRunner(),
                       real_roles=False)
        text, bad = A.recheck_login(broken)
        assert bad and "没有记录" in text, text

    def test_the_endpoint_cell_offers_the_declared_vars_and_refuses_the_secret(self, tmp_path,
                                                                              monkeypatch):
        """要哪些本机变量 = 那一档自己写的 `${}`，界面不另列一份；
        名字像凭据的那一个**不给输入框**（这一页不收 key，是记过的边界）。"""
        monkeypatch.setattr(A, "ROOT", tmp_path)
        app, _ = self.make(tmp_path)
        strip = A.agent_strip(app)
        assert "FAKE_BASE_URL" in strip and "name='FAKE_BASE_URL'" in strip
        assert "name='FAKE_TOKEN'" not in strip,             "界面上长出了一个填凭据的输入格 —— 那一格不该存在"
        assert "界面不收" in strip and "environment.ANTHROPIC_AUTH_TOKEN" in strip
        # 业主问的是"claude 接 deepseek 怎么接"：那一档故意不映射凭据，于是页面
        # 必须把**那个 CLI 自己认的**变量名与它的现状报出来（只给名字与状态，
        # 永远不给输入框、永远不显示值）。
        assert "ANTHROPIC_AUTH_TOKEN" in strip and "这一页不收" in strip
        assert "name='ANTHROPIC_AUTH_TOKEN'" not in strip

    def test_apply_endpoint_writes_a_declared_var_and_refuses_a_secret_one(self, tmp_path,
                                                                          monkeypatch):
        import os

        from tools import local_env as le

        monkeypatch.setattr(A, "ROOT", tmp_path)
        monkeypatch.delenv("FAKE_BASE_URL", raising=False)
        app, _ = self.make(tmp_path)

        text, bad = A.apply_endpoint(app, {"profile": "missing_profile",
                                           "FAKE_BASE_URL": "https://example.test/v1"})
        assert not bad, text
        written = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "FAKE_BASE_URL=https://example.test/v1" in written, written

        text, bad = A.apply_endpoint(app, {"profile": "missing_profile",
                                           "FAKE_TOKEN": "sk-secret-abcdef"})
        assert bad and "不收凭据" in text, text
        assert "FAKE_TOKEN" not in (tmp_path / ".env").read_text(encoding="utf-8")
        assert not os.environ.get("FAKE_TOKEN"), "拒收了却还把值灌进进程环境"

    def test_apply_endpoint_refuses_a_var_that_profile_does_not_reference(self, tmp_path,
                                                                         monkeypatch):
        """写一个这一档没引用的变量 = 填了也不生效的那种缺陷，必须当场拒。"""
        monkeypatch.setattr(A, "ROOT", tmp_path)
        app, _ = self.make(tmp_path)
        text, bad = A.apply_endpoint(app, {"profile": "found_profile",
                                           "FAKE_BASE_URL": "https://example.test"})
        assert bad, text
        assert "没有引用" in text or "没有这一档" in text, text
