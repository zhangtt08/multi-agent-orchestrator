"""§三 Harness Discovery 测试。

这一批测试**不依赖任何真实 CLI**：它们验证 discover 层的
"清单结构 / 探测逻辑 / 品牌隔离 / 不做安装"这些可离线断言的性质。

真实 CLI 的存在性由 `@pytest.mark.real_harness` 的测试覆盖（默认不跑）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from mao.harness.discovery import (
    KNOWN_HARNESSES,
    HarnessCandidate,
    candidate_names,
    find_candidate,
    probe_candidate,
    probe_raw_command,
)

DISCOVERY_DIR = Path(__file__).resolve().parent.parent / "mao" / "harness" / "discovery"


# ---------------------------------------------------------------------------
# 清单结构
# ---------------------------------------------------------------------------
class TestCandidateCatalog:
    def test_catalog_not_empty(self):
        assert len(KNOWN_HARNESSES) >= 10

    def test_keys_unique(self):
        keys = [c.key for c in KNOWN_HARNESSES]
        assert len(keys) == len(set(keys))

    def test_every_candidate_has_commands(self):
        for c in KNOWN_HARNESSES:
            assert c.commands, f"{c.key} 没有任何待试命令名"

    def test_every_candidate_has_safe_version_args(self):
        """version_args 必须是"打印版本就退出"型的参数。

        这里做一个保守的白名单检查：不允许出现明显会改本地状态
        或进入交互的参数。--version / version / -V 都是安全形态。
        """
        forbidden = {"install", "login", "auth", "add", "remove", "delete",
                     "uninstall", "update", "upgrade", "config", "setup"}
        for c in KNOWN_HARNESSES:
            for arg in c.version_args:
                bare = arg.lstrip("-").lower()
                assert bare not in forbidden, (
                    f"{c.key} 的 version_args 含可能的副作用参数: {arg}"
                )

    def test_candidate_names_matches_catalog(self):
        assert candidate_names() == [c.key for c in KNOWN_HARNESSES]

    def test_find_candidate_hit_and_miss(self):
        assert find_candidate("claude") is not None
        assert find_candidate("CLAUDE") is not None   # 大小写不敏感
        assert find_candidate("definitely-not-a-real-cli") is None


# ---------------------------------------------------------------------------
# 探测逻辑（用注入的候选，不依赖本机真实安装情况）
# ---------------------------------------------------------------------------
class TestProbeLogic:
    def test_probe_missing_command_returns_not_found(self):
        candidate = HarnessCandidate(
            key="__nope__",
            commands=["definitely-not-installed-xyz-123"],
        )
        result = probe_candidate(candidate)
        assert result.found is False
        assert result.path is None
        assert result.version is None
        assert result.command is None

    def test_probe_found_command_gets_version(self):
        """用本机一定存在的命令来验证"命中 + 取版本"这条路径。

        Windows 上没有 `sh`，但有 `cmd`；跨平台更稳的是直接用 Python 自己。
        这里用 `sys.executable --version` —— 它必然存在且必然有版本输出。
        """
        import sys
        candidate = HarnessCandidate(
            key="__python__",
            commands=[sys.executable],
            version_args=["--version"],
        )
        result = probe_candidate(candidate)
        assert result.found is True
        assert result.path is not None
        assert result.version is not None
        assert result.version.startswith("3.")

    def test_probe_is_never_raising(self):
        """探测一个不存在的绝对路径不应抛异常。"""
        result = probe_raw_command("C:/definitely/not/here/nope.exe")
        assert result.found is False

    def test_probe_all_reports_searched_commands(self):
        from mao.harness.discovery import probe_all
        candidates = [
            HarnessCandidate(key="a", commands=["definitely-absent-aaa"]),
            HarnessCandidate(key="b", commands=["definitely-absent-bbb"]),
        ]
        report = probe_all(candidates)
        assert len(report.results) == 2
        assert report.any_found is False
        assert "definitely-absent-aaa" in report.searched

    def test_report_render_contains_each_key(self):
        from mao.harness.discovery import probe_all
        candidates = [HarnessCandidate(key="zzz", commands=["definitely-absent-zzz"])]
        report = probe_all(candidates)
        rendered = report.render()
        assert "zzz" in rendered
        # §2 之后的三态展示：未命中显示 MISSING（原来是 NOT FOUND）
        assert "MISSING" in rendered

    def test_report_render_uses_three_state_labels(self):
        """§2 要求能同时区分 PATH FOUND / CONFIGURED PATH FOUND / MISSING。"""
        import sys

        from mao.harness.discovery import probe_all, probe_configured

        missing = probe_all([
            HarnessCandidate(key="__absent__", commands=["definitely-absent-xyz"]),
        ])
        rendered = missing.render()
        assert "MISSING" in rendered

        # 配置声明 + 本机一定存在的解释器 -> CONFIGURED PATH FOUND
        from mao.harness.discovery import ConfiguredCommand

        configured = probe_configured([
            ConfiguredCommand(name="__present__", command=sys.executable,
                              version_args=["--version"]),
        ])
        assert "CONFIGURED PATH FOUND" in configured.render()
        assert configured.found()[0].source == "configured"


# ---------------------------------------------------------------------------
# 品牌隔离：探测层可以知道品牌，但 core 与 discovery 的依赖方向必须正确
# ---------------------------------------------------------------------------
class TestDiscoveryIsolation:
    def test_discovery_does_not_import_core(self):
        """discovery 属于集成层，允许知道品牌；但它不该反向依赖 core 的业务逻辑。"""
        for py in DISCOVERY_DIR.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("mao.core"), (
                        f"{py.name} 不该 import mao.core（依赖方向反了）"
                    )
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("mao.core")

    def test_discovery_has_no_install_capability(self):
        """§三：Discovery 不负责安装。源码里不允许出现安装动作。"""
        forbidden = ("pip install", "npm install", "npm i -g", "winget install",
                     "choco install", "apt install", "brew install")
        for py in DISCOVERY_DIR.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{py.name} 含安装动作: {token}"

    def test_probe_uses_run_once_primitive(self):
        """全仓唯一 spawn 点纪律：探测也必须走 transports.run_once()。"""
        source = inspect.getsource(
            __import__("mao.harness.discovery.probe", fromlist=["probe"])
        )
        assert "run_once" in source
        assert "subprocess" not in source.replace("subprocess.run", "")
