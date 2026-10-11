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


class _FakeRunner:
    """`tools/app.py` 要的调度器替身。

    `running` 必须是**方法**（地雷 6、30）：写成布尔属性或 property，页面拿到的是
    bound method、恒真，于是"调度器在跑"这句话从此没有判据。
    """

    def __init__(self) -> None:
        self._up = False

    @property
    def log_path(self) -> Path:
        return REPO / "runtime_workbench" / "scheduler.log"

    def running(self) -> bool:
        return self._up

    def tail(self, lines: int = 40):
        return []


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

    def test_dotenv_is_not_committable(self, tmp_path):
        # 这一格的全部意义建立在"密钥不进版本库"上：gitignore 规则没了就是事故。
        # 判据不靠"这份工作树恰好是个 git 仓库"（地雷 38、46：跨机器搬运会把 .git
        # 整个剥掉，于是这条会红得与 .gitignore 无关）。现场自己造：把**仓库里那一份
        # .gitignore** 放进一个临时仓库，再问 git「.env 被忽略了吗」。
        repo = tmp_path / "repo"
        repo.mkdir()
        shutil.copy(REPO / ".gitignore", repo / ".gitignore")
        (repo / ".env").write_text("ANY_KEY=secret\n", encoding="utf-8")
        (repo / ".env.example").write_text("ANY_KEY=\n", encoding="utf-8")
        blank = tmp_path / "no-git-config"
        blank.write_text("", encoding="utf-8")
        # 全局与系统 gitignore 指到空文件：让"被忽略"这件事只能由仓库自己那份规则解释
        # （同地雷 47 把 GIT_CONFIG_* 隔离成唯一变量的那条）。
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(blank),
                   GIT_CONFIG_SYSTEM=str(blank))

        def run(*args):
            try:
                return subprocess.run(["git", *args], cwd=str(repo), env=env,
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace")
            except OSError as exc:               # 没有 git 的机器：说得出为什么证明不了
                pytest.fail(f"这一格要问 git「.env 被忽略了没有」，可这台机器上"
                            f"起不了 git（{type(exc).__name__}）—— 无法证明，不是放行")

        init = run("init", "-q", ".")
        assert init.returncode == 0, init.stderr
        assert run("check-ignore", "-q", ".env").returncode == 0,             ".env 没被 gitignore 挡住 = 密钥会进版本库"
        assert run("check-ignore", "-q", ".env.example").returncode != 0,             "样例档被一起挡了 = 装好的桌面版里没人知道有哪些键"


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
    """交给人的那份键清单，每个键都必须真的被代码或配置读一次。

    清单就是 `local_env.SUGGESTED`（`.env.example` 与旧的凭据格都从它出）；
    一页版删掉的是**那一格输入框**，不是这份清单的效力 —— 人照样按它填 .env。

    这条测试的由来是我自己犯的错：凭据格一开始列了 `ANTHROPIC_API_KEY` 与
    `OPENAI_API_KEY`，而这个程序**一次都没读过**它们 —— 额度来自 CLI 登录态。
    列一个不读的键比不列更糟：那会让人以为填了就生效。
    """

    #: 只数**会发出去的那一份代码**：archive/（旧界面与历史配置档）、runtime*/、
    #: .venv*、memory/、workspaces/、dist/、docs/ 都不算 —— 一个键被一份不再发布的
    #: 东西"读"了，等于没人读（同地雷 38 那句"要改的是让测试自己造它依赖的现场"，
    #: 这里依赖的是"哪些文件算本体"，判据就得写死在本体那一份清单上）。
    SHIPPED_FILES = ("main.py", "pyproject.toml", "pytest.ini")
    SHIPPED_DIRS = ("mao", "tools", "config", "examples", "prompts")
    SHIPPED_SUFFIXES = (".py", ".yaml", ".yml", ".toml", ".ini")

    def _read_keys(self):
        import re

        blob = []
        for rel in self.SHIPPED_FILES:
            f = REPO / rel
            if f.is_file():
                blob.append(f.read_text(encoding="utf-8", errors="replace"))
        for rel in self.SHIPPED_DIRS:
            for f in sorted((REPO / rel).rglob("*")):
                if not f.is_file() or f.suffix not in self.SHIPPED_SUFFIXES:
                    continue
                if "__pycache__" in f.parts:
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
        assert not missing, f"清单里列了、本体却没人读的键：{missing}"

    def test_external_consumed_keys_are_documented_and_few(self):
        """第三方库读的键要单独列、单独说明，不能混在"我们的键"里。"""
        assert len(le.EXTERNAL_CONSUMED) <= 6
        assert all(v.strip() for v in le.EXTERNAL_CONSUMED.values())
        assert set(le.EXTERNAL_CONSUMED) <= {k for k, _ in le.SUGGESTED}

    def test_api_keys_are_not_offered(self):
        names = {k for k, _ in le.SUGGESTED}
        assert "ANTHROPIC_API_KEY" not in names
        assert "OPENAI_API_KEY" not in names

    def test_the_page_says_where_the_quota_actually_comes_from(self, tmp_path,
                                                               monkeypatch):
        """那一页要答对两件事：这个程序不用 API key；额度来自 CLI 自己的登录态。

        原来打的是 `workbench_ui.settings` 里的凭据格 —— 那一格随旧界面一起删了
        （一页版没有 .env 编辑器，`test_workbench_ui.py` 也搬进 archive/old-ui-tests/，
        那个 `FakeCtx` 替身不再存在）。判据本身没变，搬到还活着的那一屏：
        『调用哪个 agent』那一格必须说"这个软件自己不存任何 key"、必须把每个角色
        能不能用交给**本机零调用探测**说出来、并给出一条只有本人能跑的登录命令；
        页面上不许再长出"填 key"的输入格。
        旧格里"值只显示长度与末四位、明文不进页面源码"那一半在这页上没有对应物 ——
        那条判据继续由 `test_the_plaintext_never_comes_back_out` 锁着，没有作废。
        """
        import json

        from tools import app as A

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        shutil.copy(REPO / "examples" / "config_minimal" / "settings.yaml",
                    cfg / "settings.yaml")
        cli = tmp_path / "cli"
        cli.mkdir()
        # ASCII + CRLF 的 .cmd，替真 codex 说话（中文 .bat 在这台机器上按 GBK 解码）。
        # 文件名必须就叫 codex.cmd —— 探测层按可执行文件名查本地状态子命令。
        (cli / "codex.cmd").write_bytes(
            b"@echo off\r\necho Not logged in\r\nexit /b 1\r\n")
        (cfg / "harness.yaml").write_text(json.dumps({
            "stub_codex": {"command": str(cli / "codex.cmd").replace("\\", "/"),
                           "prompt_mode": "stdin", "supports_cli": True}},
            indent=2), encoding="utf-8")
        (cfg / "agents.yaml").write_text(
            "".join(f"{role}:\n  provider: generic_cli\n  transport: subprocess\n"
                    f"  harness_profile: stub_codex\n"
                    for role in ("supervisor", "executor", "reviewer")),
            encoding="utf-8")

        monkeypatch.setattr(A, "ROOT", tmp_path)
        monkeypatch.setattr(A, "desktop_dir", lambda: tmp_path)
        # config_dir 传绝对路径：`load_config` 按进程 cwd 解析相对路径（地雷 36）
        app = A.App(config_dir=str(cfg), runner=_FakeRunner(), real_roles=True)
        page = A.page(app)
        assert "不存任何 key" in page,             "旧句「这个程序不用 API key」的等价说法；没了就是让人去填一个不生效的键"
        assert "<input type='password'" not in page,             "这一页不存密钥 —— 出现一个填 key 的框就是第二个凭据入口"
        assert "没登录" in page and "codex login" in page,             "额度那一头真正要动的地方是 CLI 的登录态，要说出命令"
        assert "只能你本人做" in page,             "这一步程序替不了，必须点名由谁做（地雷 35：做不了的才留给人）"
        assert "会消耗订阅额度" in page
