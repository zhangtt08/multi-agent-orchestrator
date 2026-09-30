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

    def test_a_cli_without_a_status_subcommand_is_never_guessed(self, tmp_path):
        """claude 退出码 0 不等于"已登录" —— 探测不了就写探测不了。"""
        exe = stub(tmp_path, "claude.cmd", STUB_LOGGED_IN)
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
                                 stub(tmp_path, "claude.cmd", STUB_LOGGED_IN))
        assert wb.login_blocker(cfg) == ""

    def test_the_strip_shows_the_three_verdicts_not_a_lie(self, tmp_path):
        from tools import workbench_ui as ui
        cfg = _config_with_codex(tmp_path,
                                 stub(tmp_path, "codex.cmd",
                                      STUB_NOT_LOGGED_IN))
        page = "".join(ui.agent_strip(
            type("C", (), {"config_dir": cfg})()))
        assert "未登录" in page and "就差登录这一步" in page
        assert "codex login" in page
        assert "在线" not in page
