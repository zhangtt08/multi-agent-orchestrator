"""CLI 登录态探测与"花钱之前先问登录"这一道闸门。

现场（2026-09-30）：业主双击打开桌面版，输入「创建一个1111文档」，页面上什么都
没成 —— 三个角色全绑 codex，而 `codex login status` 回的是 "Not logged in"。
这个项目此前一直写着"登录态只能由一次成功的真实调用证明"，于是软件宁可让他
点一次、烧一次调用、再在执行那步失败，也没有一句"你先登录"。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools import agent_probe as ap
from tools import workbench as wb

STUB_NOT_LOGGED_IN = "@echo off\r\necho Not logged in\r\nexit /b 1\r\n"
STUB_LOGGED_IN = "@echo off\r\necho Logged in as somebody\r\nexit /b 0\r\n"


def stub(tmp_path, name, body):
    """ASCII + CRLF 的 .cmd —— 这条机器上 .bat/.cmd 用中文会按 GBK 解码成乱码命令。"""
    p = tmp_path / name
    p.write_bytes(body.encode("ascii"))
    return p


class TestProbe:
    def test_a_cli_that_says_not_logged_in_is_not_logged_in(self, tmp_path):
        exe = stub(tmp_path, "codex.cmd", STUB_NOT_LOGGED_IN)
        state, detail = ap.probe(str(exe), cache={})
        assert state == ap.NOT_LOGGED_IN
        assert "Not logged in" in detail

    def test_a_logged_in_cli_says_so(self, tmp_path):
        exe = stub(tmp_path, "codex.cmd", STUB_LOGGED_IN)
        assert ap.probe(str(exe), cache={})[0] == ap.LOGGED_IN

    def test_claude_is_now_probed_by_its_own_zero_call_status_command(self, tmp_path):
        """`claude auth status --json` 是本机实测过的本地状态子命令。

        这一条锁两件事：它**要**被探（不然 Claude Code 明明登录着，界面只能说
        "探测不了"，于是业主以为要在哪里填 API key）；而它回 `loggedIn: false`
        时**不许**因为退出码 0 就被算成已登录 —— 后者是 `_NEGATIVE` 那条判据。
        """
        logged = stub(tmp_path, "claude.cmd",
                      '@echo off\r\necho {"loggedIn": true, "authMethod": "oauth_token"}\r\n'
                      "exit /b 0\r\n")
        assert ap.probe(str(logged), cache={})[0] == ap.LOGGED_IN
        out = stub(tmp_path, "claude.cmd",
                   '@echo off\r\necho {"loggedIn": false}\r\nexit /b 0\r\n')
        state, detail = ap.probe(str(out), cache={})
        assert state == ap.NOT_LOGGED_IN, detail

    def test_env_authenticated_cli_is_judged_by_the_env_it_documents(self, tmp_path,
                                                                     monkeypatch):
        """Gemini 的文档写的是环境变量认证 —— 那就按那个判，而不是猜它有子命令。"""
        exe = stub(tmp_path, "gemini.cmd", STUB_LOGGED_IN)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert ap.probe(str(exe), cache={})[0] == ap.NOT_LOGGED_IN
        monkeypatch.setenv("GEMINI_API_KEY", "some-value")
        assert ap.probe(str(exe), cache={})[0] == ap.LOGGED_IN

    def test_harness_ships_profiles_for_more_than_two_clis(self):
        """"不能只限定于 claude code 和 codex"这一条要有对应的档，否则普查里
        第三家永远是"没实测过驱动方式"。"""
        from tools import role_wiring as rw

        names = set(rw.profiles(Path(__file__).resolve().parents[1], "config"))
        assert {"opencode_executor", "gemini_executor", "qwen_executor"} <= names
        # 只读角色没有实测过的开关，所以**不许**给这三家编一个 reviewer 档
        assert not {n for n in names if n.startswith(("opencode_", "gemini_", "qwen_"))
                    and n.endswith(("_reviewer", "_supervisor"))}

    def test_a_cli_without_a_status_subcommand_is_never_guessed(self, tmp_path):
        """退出码 0 不等于"已登录" —— 探测不了就写探测不了。

        这一格原来用 `claude.cmd` 当"没有本地状态子命令"的那一个；从 2026-10-08 起
        它不再成立了：`claude auth status --json` 是本机实测过的零调用状态子命令
        （不起模型、不联网授权），所以 claude 现在是**探得到**的那一类。判据没变，
        换的是"探不了"这个事实的替身 —— 现在用 kimi，它的文档里没有这种子命令。
        """
        exe = stub(tmp_path, "kimi.cmd", STUB_LOGGED_IN)
        state, detail = ap.probe(str(exe), cache={})
        assert state == ap.UNKNOWN
        assert "探测" in detail or "证明" in detail

    def test_a_missing_executable_is_unknown_not_a_login_failure(self, tmp_path):
        state, detail = ap.probe(str(tmp_path / "codex.exe"), cache={})
        assert state == ap.UNKNOWN
        assert "谈不上登录态" in detail

    def test_the_verdict_is_cached_so_a_page_render_does_not_fork_a_cli(
            self, tmp_path):
        exe = tmp_path / "codex.cmd"
        counter = tmp_path / "hits.txt"
        exe.write_bytes(
            f"@echo off\r\necho hit >> \"{counter}\"\r\n"
            f"echo Not logged in\r\nexit /b 1\r\n".encode("ascii"))
        cache = {}
        ap.probe(str(exe), cache=cache)
        ap.probe(str(exe), cache=cache)
        assert counter.read_text(encoding="utf-8").strip().count("hit") == 1
        ap.probe(str(exe), ttl=0, cache=cache)      # 过期就再探一次
        assert counter.read_text(encoding="utf-8").strip().count("hit") == 2


def _config_with_codex(tmp_path, exe: Path) -> str:
    """一份真的能加载的 config：三个角色都绑到那个 stub 上。

    用真实 YAML 而不是替身：这道闸门的判据是"配置里的 profile 解析到哪个 exe，
    那个 exe 怎么说"，替身会把"解析"这一环整个跳过。
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    src = Path(__file__).resolve().parent.parent / "examples/config_minimal"
    shutil.copy(src / "settings.yaml", cfg / "settings.yaml")
    (cfg / "harness.yaml").write_text(json.dumps({
        "stub_codex": {"command": str(exe).replace("\\", "/"),
                       "prompt_mode": "stdin", "supports_cli": True}},
        indent=2), encoding="utf-8")
    (cfg / "agents.yaml").write_text(
        "".join(f"{r}:\n  provider: generic_cli\n  transport: subprocess\n"
                f"  harness_profile: stub_codex\n"
                for r in ("supervisor", "executor", "reviewer")),
        encoding="utf-8")
    return str(cfg)


class FakeRunner:
    """形状必须和真 `SchedulerRunner` 一致：`running` 是**方法**（地雷 6、30）。

    替身把它写成布尔属性时矩阵全绿、生产里子进程从来没起来过 —— 这条地雷抓到两次。
    """

    def __init__(self) -> None:
        self._up = False

    @property
    def log_path(self) -> Path:
        return Path("scheduler.log")

    def running(self) -> bool:
        return self._up

    def tail(self, lines: int = 40):
        return []


def _three_verdict_config(tmp_path):
    """三个角色各绑一个 stub：登录着 / 没登录 / 探测不了。

    文件名必须就叫 `codex.cmd` —— 登录态探测是按**可执行文件名**查本地状态子命令的，
    把它叫 `codex_logged_in.cmd` 就落到"探测不了"那一档，"可用"这一格于是测不到。
    所以两个 codex 各自放一个子目录。
    """
    cfg = tmp_path / "cfg3"
    cfg.mkdir()
    src = Path(__file__).resolve().parent.parent / "examples/config_minimal"
    shutil.copy(src / "settings.yaml", cfg / "settings.yaml")

    def mk(sub, name, body):
        d = tmp_path / sub
        d.mkdir()
        return stub(d, name, body)

    exes = {"supervisor": mk("cli-yes", "codex.cmd", STUB_LOGGED_IN),
            "executor": mk("cli-no", "codex.cmd", STUB_NOT_LOGGED_IN),
            # "探测不了"那一个的替身：claude 从 2026-10-08 起有本地状态子命令
            # （`auth status --json`，本机实测零调用），不能再拿它当"探不了"。
            "reviewer": mk("cli-unknown", "kimi.cmd", STUB_LOGGED_IN)}
    (cfg / "harness.yaml").write_text(json.dumps({
        f"stub_{role}": {"command": str(exe).replace("\\", "/"),
                         "prompt_mode": "stdin", "supports_cli": True}
        for role, exe in exes.items()}, indent=2), encoding="utf-8")
    (cfg / "agents.yaml").write_text(
        "".join(f"{role}:\n  provider: generic_cli\n  transport: subprocess\n"
                f"  harness_profile: stub_{role}\n" for role in exes),
        encoding="utf-8")
    return str(cfg), exes


class TestGateBeforeMoney:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        ap.uncached()
        yield
        ap.uncached()

    def test_the_blocker_names_the_roles_and_who_must_act(self, tmp_path):
        cfg = _config_with_codex(tmp_path,
                                 stub(tmp_path, "codex.cmd",
                                      STUB_NOT_LOGGED_IN))
        text = wb.login_blocker(cfg)
        assert "没登录" in text and "supervisor" in text
        assert "codex login" in text, "要给那一条只有本人能跑的命令"
        assert "只能你本人" in text

    def test_no_blocker_once_the_cli_is_logged_in(self, tmp_path):
        cfg = _config_with_codex(tmp_path,
                                 stub(tmp_path, "codex.cmd", STUB_LOGGED_IN))
        assert wb.login_blocker(cfg) == ""

    def test_the_gate_runs_before_the_supervisor_call(self, tmp_path,
                                                     monkeypatch):
        from tools import batch_project as bp
        cfg = _config_with_codex(tmp_path,
                                 stub(tmp_path, "codex.cmd",
                                      STUB_NOT_LOGGED_IN))
        ctx = wb.Workbench(config_dir=cfg, runner=None, real_roles=True)
        monkeypatch.setattr(bp, "plan",
                            lambda *a, **k: pytest.fail("没登录就不该花这一次调用"))
        text, bad = wb.go_from_form(ctx, {
            "prompt": "创建一个1111文档", "workspace": str(tmp_path)})
        assert bad and "没登录" in text
        assert "还没有调用任何 agent" in text

    def test_an_unprobeable_cli_does_not_block_the_run(self, tmp_path,
                                                      monkeypatch):
        """探测不了 ≠ 未登录。把这条判据写成"宁可拦"就是又一道墙。"""
        cfg = _config_with_codex(tmp_path,
                                 stub(tmp_path, "kimi.cmd", STUB_LOGGED_IN))
        assert wb.login_blocker(cfg) == ""

    def test_the_strip_shows_the_three_verdicts_not_a_lie(self, tmp_path,
                                                          monkeypatch):
        """三种结论都要露面，而"探测不了"永远不许被渲染成"看起来没问题"。

        这一条原来打的是 `workbench_ui.agent_strip`（那一页已删，界面只剩
        `tools/app.py` 这一页）。判据一个字没松，只是搬到页面上那一格：
        三个角色在同一页上各自给出 **可用 / 没登录 / 探测不了**，
        未登录那一行还要给出只有本人能跑的那一条命令；页面上不出现"在线"。
        """
        from tools import app as A

        cfg, exes = _three_verdict_config(tmp_path)
        facts = {f["role"]: f for f in ap.role_facts(cfg)}
        assert [facts[r]["login"] for r in
                ("supervisor", "executor", "reviewer")] == \
            [ap.LOGGED_IN, ap.NOT_LOGGED_IN, ap.UNKNOWN]
        # 页面那一列写的是发现层解析到的那一份路径 —— 判据只有这一份来源（地雷 46）
        assert Path(facts["executor"]["path"]).resolve() == \
            exes["executor"].resolve()

        monkeypatch.setattr(A, "ROOT", tmp_path)
        monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path)
        app = A.App(config_dir=cfg, runner=FakeRunner(), real_roles=True)
        strip = A.agent_strip(app)
        page = A.page(app)
        assert strip in page,             "这一格要真的在那一页上；只在函数里绿一次不算界面答了问题"
        assert "可用" in page and "没登录" in page and "探测不了" in page
        assert "可执行文件" in page,             "「调用哪个 agent」必须答在第一屏，一行都要说清用的是哪一份 CLI"
        assert "codex login" in page and "只能你本人做" in page,             "未登录要说得出那一条只有本人能跑的命令，而不是只说一句「不行」"
        for lie in ("在线", "Online", "已登录", "Active 3", "98%"):
            assert lie not in page, f"界面上出现了 {lie} —— 登录态永远最多是 WARN（地雷 21）"
