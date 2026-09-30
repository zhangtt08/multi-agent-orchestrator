"""凭据层与角色绑定层 —— 业主指出"没有填 api key 的机制，也没有其他 agent 的
接入机制，后续调配不方便"，这两格就是答案；测试锁的是边界与那条静默退回的地雷。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import local_env as le        # noqa: E402
from tools import role_wiring as rw      # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """一份 config 的副本 + 干净的进程环境，别把真仓库的 agents.yaml 改了。"""
    shutil.copytree(REPO / "config", tmp_path / "config")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MAO_TEST_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


class TestLocalEnv:
    def test_a_key_that_is_already_exported_wins(self, sandbox, monkeypatch):
        monkeypatch.setenv("MAO_TEST_KEY", "from-shell")
        (sandbox / ".env").write_text("MAO_TEST_KEY=from-file\n", encoding="utf-8")
        assert le.load_local_env(sandbox) == 0
        assert os.environ["MAO_TEST_KEY"] == "from-shell"

    def test_a_missing_key_gets_loaded(self, sandbox):
        (sandbox / ".env").write_text('MAO_TEST_KEY="from-file"\n', encoding="utf-8")
        le.load_local_env(sandbox)
        assert os.environ["MAO_TEST_KEY"] == "from-file"

    def test_junk_lines_are_ignored(self, sandbox):
        (sandbox / ".env").write_text(
            "# comment\nlowercase_no\nNO_VALUE_AT_ALL=\nGOOD_KEY=ok\n",
            encoding="utf-8")
        le.load_local_env(sandbox)
        assert os.environ.get("GOOD_KEY") == "ok"
        assert "lowercase_no" not in os.environ

    def test_bad_key_names_are_refused(self, sandbox):
        for bad in ("bad-key", "lower", "", "9STARTS_WITH_DIGIT", "A B"):
            ok, msg = le.set_key(bad, "v", sandbox)
            assert not ok, bad

    def test_the_plaintext_never_comes_back_out(self, sandbox):
        secret = "sk-super-secret-value-8891"
        ok, msg = le.set_key("ANTHROPIC_API_KEY", secret, sandbox)
        assert ok
        assert secret not in msg
        fp = le.fingerprint("ANTHROPIC_API_KEY", sandbox)
        assert secret not in fp and "8891" in fp and "长度" in fp
        assert (sandbox / ".env").read_text(encoding="utf-8").strip() == \
            f"ANTHROPIC_API_KEY={secret}"

    def test_empty_value_deletes_the_line(self, sandbox):
        le.set_key("OPENAI_API_KEY", "abc", sandbox)
        le.set_key("OPENAI_API_KEY", "", sandbox)
        assert "OPENAI_API_KEY" not in (sandbox / ".env").read_text(
            encoding="utf-8")
        assert "OPENAI_API_KEY" not in os.environ

    def test_dotenv_is_not_committable(self):
        # 这一格的全部意义建立在"密钥不进版本库"上：gitignore 规则没了就是事故
        r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO,
                           capture_output=True)
        assert r.returncode == 0


class TestRoleWiring:
    def test_real_config_is_readable(self):
        assert "codex_executor" in rw.profiles(REPO, "config")
        assert rw.bindings(REPO, "config")["executor"] == "codex_executor"

    def test_switching_profiles_keeps_the_comments(self, sandbox):
        before = (REPO / "config" / "agents.yaml").read_text(encoding="utf-8")
        ok, msg = rw.set_binding("executor", "real_executor", sandbox, "config")
        assert ok, msg
        after = (sandbox / "config" / "agents.yaml").read_text(encoding="utf-8")
        assert len(before.splitlines()) == len(after.splitlines())
        assert "workspace-write 沙箱" in after        # 注释没被 yaml 往返抹掉
        assert rw.bindings(sandbox, "config")["executor"] == "real_executor"

    def test_the_capacity_coupling_is_said_out_loud(self, sandbox):
        """换档最大的坑：agents.yaml 改了、settings.yaml 的 capacity.providers
        没跟着加同名键 —— 调度器不报错，只静默退回默认值。"""
        ok, notes = rw.check("executor", "real_executor", sandbox, "config")
        assert ok                                   # profile 本身是存在的
        assert any("静默" in n for n in notes), notes

    def test_unknown_profile_is_refused_before_writing(self, sandbox):
        ok, notes = rw.check("executor", "no_such_profile", sandbox, "config")
        assert not ok
        assert any("不在" in n for n in notes)

    def test_only_the_three_roles_can_be_touched(self, sandbox):
        ok, msg = rw.set_binding("scheduler", "codex_executor", sandbox, "config")
        assert not ok and "角色只能是" in msg
        assert rw.bindings(sandbox, "config")["executor"] == "codex_executor"

    def test_a_role_without_the_line_is_not_guessed(self, tmp_path):
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "agents.yaml").write_text("executor:\n  provider: generic_cli\n",
                                         encoding="utf-8")
        ok, msg = rw.set_binding("executor", "real_executor", tmp_path, "config")
        assert not ok and "不敢替你猜位置" in msg


class TestSuggestedKeysAreReal:
    """界面上列出的每个键，都必须真的被代码或配置读一次。

    这条测试的由来是我自己犯的错：凭据格一开始列了 `ANTHROPIC_API_KEY` 与
    `OPENAI_API_KEY`，而这个程序**一次都没读过**它们 —— 额度来自 CLI 登录态。
    列一个不读的键比不列更糟：那会让人以为填了就生效。
    """

    def _read_keys(self):
        import re
        import subprocess

        tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        blob = []
        for p in tracked:
            if not p.endswith((".py", ".yaml", ".yml", ".toml", ".ini")):
                continue
            f = REPO / p
            if not f.is_file():
                continue
            blob.append(f.read_text(encoding="utf-8", errors="replace"))
        text = "\n".join(blob)
        found = set(re.findall(
            r'environ(?:\.get)?[\[\(]\s*["\']([A-Z][A-Z0-9_]{2,})'
            r'|getenv\(\s*["\']([A-Z][A-Z0-9_]{2,})'
            r'|\$\{([A-Z][A-Z0-9_]{2,})\}', text))
        flat = set()
        for tup in found:
            flat.update([x for x in tup if x])
        return flat

    def test_every_suggested_key_is_actually_read(self):
        read = self._read_keys()
        missing = [k for k, _ in le.SUGGESTED
                   if k not in read and k not in le.EXTERNAL_CONSUMED]
        assert not missing, f"界面上列了但没人读的键：{missing}"

    def test_external_consumed_keys_are_documented_and_few(self):
        """第三方库读的键要单独列、单独说明，不能混在"我们的键"里。"""
        assert len(le.EXTERNAL_CONSUMED) <= 6
        assert all(v.strip() for v in le.EXTERNAL_CONSUMED.values())
        assert set(le.EXTERNAL_CONSUMED) <= {k for k, _ in le.SUGGESTED}

    def test_api_keys_are_not_offered(self):
        names = {k for k, _ in le.SUGGESTED}
        assert "ANTHROPIC_API_KEY" not in names
        assert "OPENAI_API_KEY" not in names

    def test_the_panel_says_where_the_quota_actually_comes_from(self):
        import sys
        sys.path.insert(0, str(REPO / "tests"))
        from tools import workbench_ui as ui
        from test_workbench_ui import FakeCtx
        page = ui.settings(FakeCtx(real_roles=True))
        assert "这个程序不用 API key" in page
        assert "CLI 的登录态" in page
