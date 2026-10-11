"""cc-switch 识别 + 本机普查的守卫。

这两格最容易犯的错有两个，都在这里被钉住：
1. **把凭据抄进返回值**（那就会出现在页面上）—— 用一条"密文不许出现"的哨兵量；
2. **靠这台机器过关**（地雷 38）—— 夹具自己造一份 HOME、自己的 cc-switch 库、
   自己的 PATH，判据不读真实的 `C:\\Users\\...`。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from tools import cc_switch as cs                                 # noqa: E402
from tools import cli_census as census                             # noqa: E402
from tools import harness_authoring as ha                           # noqa: E402

SECRET_VALUE = "sk-DO-NOT-LEAK-1234567890"


class Runner:
    """`running` 必须是**方法**（地雷 30 那条），这里只用它的形状。"""

    def running(self) -> bool:
        return False


def make_switch_home(home: Path, rows, endpoints, visible=None) -> Path:
    d = home / ".cc-switch"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "cc-switch.db")
    con.execute("create table providers (id text, app_type text, name text,"
                " settings_config text, category text, provider_type text,"
                " is_current text)")
    con.executemany("insert into providers values (?,?,?,?,?,?,?)", rows)
    con.execute("create table provider_endpoints (id text, provider_id text,"
                " app_type text, url text, added_at text)")
    con.executemany("insert into provider_endpoints values (?,?,?,?,?)", endpoints)
    con.commit()
    con.close()
    (d / "settings.json").write_text(json.dumps(
        {"visibleApps": visible or {}, "currentProviderClaude": "p-claude"}),
        encoding="utf-8")
    return d / "cc-switch.db"


@pytest.fixture()
def switch_home(tmp_path):
    """一份和真 cc-switch 同形状的库：claude 接的是第三方端点 + 一个真凭据。"""
    cfg = json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.example/anthropic",
                              "ANTHROPIC_MODEL": "glm-4.6",
                              "ANTHROPIC_AUTH_TOKEN": SECRET_VALUE}})
    codex_cfg = json.dumps({"auth": {"OPENAI_API_KEY": SECRET_VALUE,
                                     "auth_mode": "apikey"}, "config": ""})
    rows = [
        ("p-claude", "claude", "DeepSeek Example", cfg, "third_party", "", "1"),
        ("p-claude-official", "claude", "Claude Official", '{"env":{}}', "official", "", "0"),
        ("p-codex", "codex", "Zhipu GLM", codex_cfg, "third_party", "", "1"),
        ("p-gemini", "gemini", "Google Official", '{"env":{},"config":{}}', "official", "", "0"),
    ]
    endpoints = [("1", "p-claude", "claude", "https://api.deepseek.example/anthropic", "0"),
                 ("2", "p-codex", "codex", "https://open.bigmodel.example/api/paas/v4", "0")]
    make_switch_home(tmp_path, rows, endpoints,
                     {"claude": True, "codex": True, "zcode": True, "opencode": False})
    return tmp_path


class TestCcSwitchReader:
    def test_it_recognizes_the_provider_the_user_already_configured(self, switch_home):
        data = cs.read(str(switch_home))
        assert data["installed"] is True, data
        claude = data["apps"]["claude"]["current"]
        assert claude["name"] == "DeepSeek Example"
        assert "https://api.deepseek.example/anthropic" in claude["endpoints"]
        assert claude["config"]["env"]["values"]["ANTHROPIC_MODEL"] == "glm-4.6"

    def test_the_secret_value_never_comes_back_out(self, switch_home):
        """识别端点是目的，抄走凭据不是 —— 这一条是密文哨兵，不是样式检查。"""
        data = cs.read(str(switch_home))
        blob = json.dumps(data, ensure_ascii=False)
        assert SECRET_VALUE not in blob, "返回值里带着明文密钥，界面就会把它印出来"
        assert "ANTHROPIC_AUTH_TOKEN" in blob, "键名要露面：人才知道那一格叫什么"
        env = data["apps"]["claude"]["current"]["config"]["env"]
        assert env["hidden_keys"] == ["ANTHROPIC_AUTH_TOKEN"]
        assert "OPENAI_API_KEY" in data["apps"]["codex"]["current"]["config"]["auth"]["hidden_keys"]

    def test_no_cc_switch_is_a_fact_not_a_fake_list(self, tmp_path):
        data = cs.read(str(tmp_path / "nothing-here"))
        assert data["installed"] is False
        assert "没有 cc-switch" in data["reason"]
        assert data["apps"] == {}

    def test_an_unreadable_db_says_why_instead_of_blaming_the_user(self, tmp_path):
        d = tmp_path / ".cc-switch"
        d.mkdir(parents=True)
        (d / "cc-switch.db").write_bytes(b"this is not a sqlite file at all")
        data = cs.read(str(tmp_path))
        assert data["installed"] is False
        assert data["reason"], "读不动却给不出原因 —— 那就是一堵墙，不是判据"

    def test_lines_are_plain_sentences_about_the_current_provider(self, switch_home):
        lines = "\n".join(cs.lines(str(switch_home)))
        assert "DeepSeek Example" in lines and "https://api.deepseek.example/anthropic" in lines
        assert SECRET_VALUE not in lines
        assert "没有哪一家是当前" in lines            # gemini 那一家不许被硬挑一个


class TestCensus:
    def test_switch_app_types_become_candidates_even_when_we_never_heard_of_them(
            self, switch_home, monkeypatch):
        """候选名单不是写死的两个名字：zcode 这种我们没在 KNOWN_INSTALL_GLOBS 里
        配过的 app_type，也要进候选（命中与否由发现层量）。"""
        names = census.cli_names(["zcode", "codex", "claude"])
        assert "zcode" in names and "codex" in names and "claude" in names

    def test_visible_apps_widen_the_candidate_list(self, switch_home, monkeypatch):
        data = census.census(str(switch_home))
        keys = {r["key"] for r in data["rows"]}
        assert "zcode" in keys, "visibleApps 里有的 app 没进普查 —— 清单还是写死的"

    def test_app_markers_are_read_from_the_sandbox_home_not_the_real_one(
            self, tmp_path, monkeypatch):
        (tmp_path / ".zcode").mkdir()
        local = tmp_path / "Local"
        (local / "Programs" / "WorkBuddy").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        data = census.census(str(tmp_path))
        found = {r["key"]: r["path"] for r in data["rows"] if r["kind"] == "app"}
        assert "zcode" in found and str(tmp_path) in found["zcode"]
        assert "workbuddy" in found, "WorkBuddy 在沙箱 LOCALAPPDATA 里却没被认到"
        assert found["workbuddy"].startswith(str(local)), found["workbuddy"]

    def test_bindable_means_a_profile_resolves_to_that_exact_executable(self):
        rows = [{"key": "codex", "path": r"C:\x\codex.exe", "found": True},
                {"key": "claude", "path": r"C:\y\claude.cmd", "found": True}]
        profiles = [{"profile": "codex_executor", "path": r"C:\x\codex.exe"},
                    {"profile": "real_executor", "path": r"C:\y\claude.cmd"},
                    {"profile": "no_such_cli", "path": ""}]
        out = census.bindable_profiles(rows, profiles)
        assert out == {"codex": ["codex_executor"], "claude": ["real_executor"]}, \
            "绑不绑得上按解析到的那一份 exe 判，不按档名里有没有 'codex' 猜"

    def test_a_detected_cli_without_a_profile_is_reported_as_not_drivable(
            self, tmp_path, monkeypatch):
        """检测到 ≠ 能驱动。这一条不许被"顺手给个按钮"抹平：按没实测过的参数起一次
        就是一次白烧的额度。"""
        data = census.census(str(tmp_path))
        rows = [r for r in data["rows"] if r["kind"] == "cli" and r["found"]]
        bound = census.bindable_profiles(rows, [])
        assert bound == {}, "没有任何档时不许凭空说谁可绑"


class TestHarnessAuthoring:
    """把"本机这个 CLI"写成档那一格。MAO 不猜参数：契约不齐就不落盘。"""

    def make(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "examples/config_minimal/settings.yaml",
                    cfg / "settings.yaml")
        (cfg / "harness.yaml").write_text(
            "stub_executor:\n  description: \"已有的那一档\"\n"
            f"  command: '{sys.executable}'\n  prompt_mode: stdin\n"
            "  supports_cli: true\n  supports_file_write: true\n",
            encoding="utf-8")
        (cfg / "agents.yaml").write_text(
            "supervisor:\n  provider: generic_cli\n  harness_profile: stub_executor\n"
            "executor:\n  provider: generic_cli\n  harness_profile: stub_executor\n"
            "reviewer:\n  provider: generic_cli\n  harness_profile: stub_executor\n",
            encoding="utf-8")
        return cfg

    def fields(self, **kw):
        base = {"name": "third_cli_executor", "command": sys.executable,
                "extra_args": "run --auto", "prompt_mode": "stdin",
                "writable": "yes", "allowed_exit_codes": "0"}
        base.update(kw)
        return base

    def test_missing_contract_is_refused_before_the_file_moves(self, tmp_path):
        cfg = self.make(tmp_path)
        target = cfg / "harness.yaml"
        before = target.read_bytes()
        ok, msg = ha.write_profile(tmp_path, "config", "third_cli_executor",
                                   self.fields(extra_args=""))
        assert not ok and "非交互参数" in msg
        assert target.read_bytes() == before, "没给参数也落了盘 —— 那是一次未知调用"

    def test_a_command_this_machine_cannot_find_is_refused(self, tmp_path):
        cfg = self.make(tmp_path)
        before = (cfg / "harness.yaml").read_bytes()
        ok, msg = ha.write_profile(tmp_path, "config", "ghost_cli_executor",
                                   self.fields(command="definitely-not-installed-xyz"))
        assert not ok and "找不到" in msg
        assert (cfg / "harness.yaml").read_bytes() == before

    def test_a_written_profile_loads_and_can_be_bound(self, tmp_path):
        """写完的判据不是"文件里多了一段字"，而是那份配置**还能加载**、
        并且真的能绑到角色上 —— 这一条把"检测到 → 加档 → 换档"那个圈闭合。"""
        from tools import role_wiring as rw

        self.make(tmp_path)
        ok, msg = ha.write_profile(tmp_path, "config", "third_cli_executor", self.fields())
        assert ok, msg
        assert "third_cli_executor" in rw.profiles(tmp_path, "config")
        bound, note = rw.set_binding("executor", "third_cli_executor", tmp_path, "config")
        assert bound, note
        assert "静默" in note            # 新档没有同名容量键，这一句必须跟着说

    def test_it_never_overwrites_a_profile_that_already_exists(self, tmp_path):
        cfg = self.make(tmp_path)
        before = (cfg / "harness.yaml").read_bytes()
        ok, msg = ha.write_profile(tmp_path, "config", "stub_executor", self.fields())
        assert not ok and "已经有" in msg
        assert (cfg / "harness.yaml").read_bytes() == before

    def test_a_profile_that_cannot_load_is_rolled_back_byte_for_byte(
            self, tmp_path, monkeypatch):
        """回滚这一条只能靠"加载真的失败"来量，所以这里让加载失败 ——
        注意这是**唯一**被控制的接缝，文件写入与字节比对都是真的。"""
        cfg = self.make(tmp_path)
        target = cfg / "harness.yaml"
        before = target.read_bytes()

        import mao.core.config as mc

        def boom(*a, **k):
            raise RuntimeError("装配不起来（夹具造的）")

        monkeypatch.setattr(mc, "load_config", boom)
        ok, msg = ha.write_profile(tmp_path, "config", "third_cli_executor", self.fields())
        assert not ok and "回滚" in msg, msg
        assert target.read_bytes() == before, "加载失败却把半档留在文件里"

    def test_the_card_offers_authoring_only_for_detected_unbound_clis(self,
                                                      tmp_path, monkeypatch):
        from tools import app as A

        cfg = self.make(tmp_path)
        app = A.App(config_dir=str(cfg), runner=Runner(), real_roles=True)
        # 假的那个 CLI 用另一个真的文件：夹具里那几档解析到的是 sys.executable，
        # 拿同一个路径当"没档的那一个"就会被 bindable_profiles 认成已绑，
        # 于是这一格什么都没得渲染 —— 判据就测不到了。
        other = tmp_path / "thirdcli.exe"
        other.write_bytes(b"stub")
        fake = {"rows": [{"kind": "cli", "key": "thirdcli", "cn": "ThirdCLI",
                          "found": True, "path": str(other), "source": "PATH",
                          "switch_current": "", "switch_endpoints": []}],
                "switch_installed": False, "switch_reason": "本机没有 cc-switch",
                "switch_currents": {}, "problem": ""}
        monkeypatch.setattr(census, "census", lambda home=None: fake)
        form = A.authoring_form(app)
        assert "ThirdCLI" in form and "name='extra_args'" in form
        assert "不猜参数" in form, "那一格必须说清参数由谁给"
