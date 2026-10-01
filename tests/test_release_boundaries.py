"""发布边界守卫（v1.0）。

这一档测试锁的是"交到别人手上会不会立刻坏"，不是运行时功能：

```text
版本只有一处真相                 VERSION == mao.__version__ == --version
公共文件里没有本机绝对路径        换台机器就失效，还泄漏目录结构
公共文件里没有真实密钥
生产配置默认值保守                刚开程序就扇出真实调用是不可接受的
发布面被 git 跟踪                曾经整包源码处于未跟踪状态（迁移事故）
运行数据不进包                    runtime / DB / 模型 / venv / dist
工具文件至少能编译                argparse 签名漂移只有跑起来才发现
没有调试残留
```

历史文档（docs/history/PHASE*_REPORT.md 等）允许写本机路径 —— 那是审计现场。
守卫只覆盖会被别人拿去运行的那一层。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RELEASE_SURFACE = ("mao", "config", "tools", "examples", "main.py", "README.md")

# 真实用户目录形态：盘符 + Users/<名字>。`C:\Users\<某人>` 这种带尖括号的
# 占位说明是允许出现的 —— 它教人别写死路径，本身不是路径。
# 盘符前不能是字母：否则 URL 里的 `s:/` 会被当成 `C:/`。
MACHINE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\/]{1,2}[Uu]sers[\/]{1,2}([A-Za-z0-9._ -]{2,})")
SECRET_SHAPES = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{24,}"),
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _tracked_text_files() -> list[Path]:
    proc = _git("ls-files", "--", *RELEASE_SURFACE)
    if proc.returncode != 0:
        pytest.skip("需要 git 仓库形态（迁移轮的教训：没有 git 就证明不了被跟踪）")
    out = []
    for name in proc.stdout.splitlines():
        p = ROOT / name
        if p.is_file() and p.suffix in (".py", ".yaml", ".yml", ".md", ".txt",
                                        ".toml", ".json", ""):
            out.append(p)
    return out


class TestVersionBoundary:
    def test_version_file_matches_package(self):
        from mao import __version__

        declared = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        assert declared == __version__, (
            f"VERSION={declared} 与 mao.__version__={__version__} 不一致 —— "
            "版本有两个真相时，总有一个是过期的")
        assert re.fullmatch(r"\d+\.\d+\.\d+", declared), declared

    def test_readme_announces_the_current_version(self):
        """README 第一屏的数字也是对外声明。它曾停在 1.0.0 而 VERSION 已是 1.0.3。"""
        from mao import __version__

        head = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:6]
        found = [ln for ln in head if re.search(r"\*\*Version \d+\.\d+\.\d+\*\*", ln)]
        assert found, "README 第一屏没有版本声明了 —— 那对外靠什么知道是哪一版？"
        assert __version__ in found[0], (
            f"README 写的版本不是 {__version__}：对外文档与 VERSION 分叉了")

    def test_cli_prints_product_name_and_version(self):
        from mao import __version__

        proc = _git("rev-parse", "--is-inside-work-tree")
        out = subprocess.run([sys.executable, str(ROOT / "main.py"), "--version"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", cwd=str(ROOT))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == f"multi-agent-orchestrator {__version__}"
        _ = proc  # git 是否存在不影响本用例，只是顺手确认可用

    def test_pyproject_does_not_duplicate_version(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(r'^dynamic\s*=\s*\[\s*"version"\s*\]', text, re.M), (
            "pyproject 必须动态读取版本，不能复制一份可以漂走的字面量")


class TestNoMachinePaths:
    @pytest.mark.parametrize("suffix", [".py", ".yaml", ".md"])
    def test_release_surface_has_no_user_directories(self, suffix):
        offenders = []
        for path in _tracked_text_files():
            if path.suffix != suffix:
                continue
            for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for match in MACHINE_PATH.finditer(line):
                    if "<" in match.group(1):        # 占位说明，允许
                        continue
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
        assert not offenders, (
            "发布面出现本机绝对路径（换台机器就失效，还泄漏目录结构）：\n"
            + "\n".join(offenders[:12]))


class TestNoSecrets:
    def test_tracked_release_surface_has_no_secret_shapes(self):
        hits = []
        for path in _tracked_text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in SECRET_SHAPES:
                for match in pattern.finditer(text):
                    line = text[:match.start()].count("\n") + 1
                    hits.append(f"{path.relative_to(ROOT)}:{line}: "
                                f"{match.group(0)[:40]}…")
        assert not hits, "疑似真实凭据被写进发布面：\n" + "\n".join(hits[:10])

    def test_env_example_names_only_variables(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            # 允许"值本身不含本机路径"的通用默认（模型 id、镜像端点、开关）；
            # 禁止的是**某台机器的路径**和凭据 —— 那才是换机器就废的原因。
            # 盘符判定要求前面不是字母，否则 `https://` 里的 `s:/` 会被误当成
            # `C:/`（这一条我自己踩过一次）。
            value_text = value.strip()
            assert (not value_text
                    or (not re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", value_text)
                        and "Users" not in value_text)), (
                f".env.example 不该带真实本机路径：{line}")
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()), key


class TestProductionConfigInvariants:
    """`config/` 是"照它跑就是安全默认"的那份配置 —— 这条得被机器看着。"""

    @pytest.fixture(scope="class")
    def settings(self):
        from mao.core.config import load_config

        return load_config("config").settings

    def test_concurrency_is_conservative(self, settings):
        s = settings.scheduler
        assert s.enabled is True
        assert s.max_concurrent_tasks == 1, "普通用户默认并发应为 1"
        assert s.worker_pool_size in (0, 1)
        assert s.capacity.global_agent_calls <= 2
        assert s.capacity.provider_default == 1, "同一 provider 必须串行"

    def test_quota_guardrails_present(self, settings):
        assert settings.max_rounds <= 5
        assert settings.effective_agent_call_limit() <= 30
        assert settings.scheduler.default_max_attempts <= 3

    def test_capacity_keys_match_the_bound_profiles(self):
        """容量闸门按 harness profile 名键控。换执行者却忘改这里，键就指向一个
        不存在的 provider —— 不报错，只是静默退回 provider_default，
        于是"每 provider 串行"这条从配置上看着在、实际不在。"""
        from mao.core.config import load_config

        cfg = load_config("config")
        bound = {b["harness_profile"] for b in cfg.binding_map().values()}
        declared = set(cfg.settings.scheduler.capacity.providers)
        assert bound == declared, (
            f"绑定={sorted(bound)} 容量键={sorted(declared)} —— 两边必须同名")

    def test_checkpoint_durable_by_default(self, settings):
        cp = settings.checkpoint
        assert cp.enabled is True
        assert cp.auto_resume is True
        assert cp.validate_workspace is True
        assert cp.validate_artifact_hashes is True
        assert cp.workspace_mismatch_policy == "block", "指纹不匹配时不能自动覆盖用户改动"

    def test_workspace_isolation_is_default(self, settings):
        assert settings.scheduler.workspace.default_strategy == "GIT_WORKTREE"

    def test_memory_falls_back_instead_of_failing(self, settings):
        assert settings.memory.enabled is True
        assert settings.memory.required is False, (
            "记忆层坏了不该让整条流水线跑不动")
        assert settings.memory.semantic.enabled is True
        retrieval = settings.memory.retrieval
        mode = (retrieval.get("mode") if isinstance(retrieval, dict)
                else getattr(retrieval, "mode", None))
        assert mode == "hybrid"

    def test_no_machine_paths_in_config_files(self):
        for path in sorted((ROOT / "config").glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            assert not MACHINE_PATH.search(text), path
            assert "${" in text or path.name != "harness.yaml", (
                "harness.yaml 的可执行路径必须走环境变量占位")


class TestReleaseSurfaceTracked:
    def test_runtime_and_data_are_ignored(self):
        for target in ("runtime/x", "runtime_p10/x", "runtime_worktrees/rt-1",
                       "runtime_scheduler/queue.db", "memory/memory.db",
                       "dist/mao.zip", ".venv-ml/pyvenv.cfg",
                       "examples/calculator/__pycache__/x.pyc"):
            proc = _git("check-ignore", "-q", target)
            assert proc.returncode == 0, f"{target} 应该被忽略但没被忽略"

    def test_source_packages_are_tracked(self):
        for package in ("mao/core", "mao/agents", "mao/transports", "mao/harness",
                        "mao/memory", "mao/scheduler", "mao/workspaces",
                        "mao/checkpoints", "tools", "config", "examples"):
            proc = _git("ls-files", "--", package)
            assert proc.stdout.strip(), f"{package}/ 没有任何被跟踪的文件"

    def test_release_docs_exist(self):
        """发布面的文档入口必须真的存在，而且**只有一个当前入口**。

        2026-10-02 把仓库根那四份轮次文档（RELEASE_CHECKLIST / RELEASE_MANIFEST /
        DELIVERY_CHECKLIST / RELEASE_NOTES_v1.0.0）合并成 `docs/RELEASE.md` 一份：
        同一件事在四个地方各写一遍，每一处都会留着某一天的计数骗后人
        （AGENTS.md「同一判断在两个地方各写一遍，就是缺陷的形状」）。
        旧文原样进了 `docs/history/`，所以这里同时锁"归档还在"与"根目录不再有它们"。
        """
        for name in ("README.md", "AGENTS.md", "VERSION", ".env.example",
                     "requirements.txt",
                     "requirements-semantic.txt", "requirements-ml.txt",
                     "docs/RELEASE.md", "docs/USER_GUIDE.md",
                     "docs/OPERATOR_GUIDE.md", "docs/TROUBLESHOOTING.md",
                     "docs/ARCHITECTURE.md", "examples/task_single.json",
                     "examples/task_queue.json", "examples/config_minimal/settings.yaml"):
            assert (ROOT / name).is_file(), f"发布面缺 {name}"
        for archived in ("RELEASE_CHECKLIST.md", "RELEASE_MANIFEST.md",
                         "DELIVERY_CHECKLIST.md"):
            assert (ROOT / "docs" / "history" / archived).is_file(), \
                f"轮次记录应归档在 docs/history/：{archived}"
            assert not (ROOT / archived).exists(), \
                f"{archived} 又回到仓库根了 —— 根目录只留一份准确的 docs/RELEASE.md"

    def test_no_stale_phase_default_in_cli(self):
        """CLI 的默认配置目录只能是 config/ —— 曾经各处默认不一致，
        同一个产品的 memory / queue / checkpoint 视图指向三套 runtime。"""
        offenders = []
        for path in (ROOT / "main.py", ROOT / "tools" / "scheduler_cli.py",
                     ROOT / "tools" / "memory_cli.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "config_dir" in line and re.search(r'["\']config_p\d', line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, "CLI 里还有指向阶段性配置的默认值：\n" + "\n".join(offenders)


class TestToolFilesCompile:
    @pytest.mark.parametrize("path", sorted((ROOT / "tools").glob("*.py")),
                             ids=lambda p: p.name)
    def test_tool_compiles_and_has_docstring(self, path):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assert ast.get_docstring(tree), f"{path.name} 没有模块 docstring（工具没有说明=用户没有入口）"


class TestNoDebugResidue:
    def test_no_breakpoint_or_pdb(self):
        bad = []
        for path in list((ROOT / "mao").rglob("*.py")) + \
                list((ROOT / "tools").glob("*.py")) + [ROOT / "main.py"]:
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in ("pdb.set_trace(", "breakpoint(", "console = None  # DEBUG"):
                if token in text:
                    bad.append(f"{path.relative_to(ROOT)}: {token}")
        assert not bad, "调试残留：\n" + "\n".join(bad)

    def test_framework_code_does_not_print(self):
        """mao/ 里 print 只允许出现在 embedding worker 的 JSON-lines 协议里。

        理由写在 mao/core/logging_setup.py 顶部：框架代码里的 print 等于
        "这条信息我以后不打算查了"。
        """
        allowed = {ROOT / "mao" / "memory" / "embeddings" / "worker.py"}
        offenders = []
        for path in (ROOT / "mao").rglob("*.py"):
            if path in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert not offenders, "core 里出现 print（该走 logging）：\n" + "\n".join(offenders[:10])
