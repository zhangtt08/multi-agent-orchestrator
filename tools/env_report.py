"""env_report —— 环境体检的唯一实现（doctor 与 bootstrap 共用）。

为什么要有这个模块：`main.py doctor` 与 `tools/bootstrap.py` 一度各自检查一套
东西，同一件事（CLI 能不能定位、语义档缺了算不算失败）在两边给出不同答案。
一份产品不该有两个"你的环境怎么样"。这里只有一处判据：

```text
Core               解释器 / 依赖 / 配置 / 日志级别
Git                git 可执行（GIT_WORKTREE 隔离与框架取证的前提）
CLI Harnesses      三个角色的 Profile、可执行文件、能力闸门、权限策略
Scheduler          队列 DB 可用、并发与容量闸门、调用预算
Checkpoint         断点库可用、resume 与完整性策略
Memory             记忆库 + FTS5 + 实际检索模式（HYBRID / LEXICAL FALLBACK）
Embedding Runtime  独立 worker 握手（不加载模型，所以是秒级）
Workspace          工作区目录与工作树根可写、交付形态
```

判级纪律（这是产品行为，不是风格）：

* 可选组件缺失 ⇒ **WARN**：语义检索缺席时系统退化为词法检索并且仍然完整可用。
  只有"照这个配置跑不起来"才是 FAIL。
* 每个非 OK 项必须给一条可执行动作 —— 只输出 False 等于没说。
* 认证状态永远是 WARN/unknown，不是 FAIL：本框架的不变式是"只有成功的真实
  调用是正证据"，而 doctor 不许烧配额。要确认可用 `tools/smoke_real_harness.py`。

这里**不**安装、不下载、不删文件，也不调用真实 Agent CLI。
唯一允许的写操作是创建软件自己的数据区并初始化 schema（首次运行自动完成）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

GROUPS: Tuple[str, ...] = (
    "Core", "Git", "CLI Harnesses", "Scheduler", "Checkpoint",
    "Memory", "Embedding Runtime", "Workspace",
)

# preflight 的条目名 → 体检分组。CLI 相关判据一律沿用 preflight 本身，
# 因为"doctor 与实跑不一致"比"doctor 报错"更糟。
_PREFLIGHT_GROUP = {
    "python": "Core",
    "config": "Core",
    "runtime": "Core",
    "workspace": "Workspace",
    "agents": "CLI Harnesses",
    "cli_commands": "CLI Harnesses",
    "capabilities": "CLI Harnesses",
    "authentication": "CLI Harnesses",
    "policy": "CLI Harnesses",
}


@dataclass
class Item:
    group: str
    status: str
    name: str
    detail: str = ""
    action: str = ""

    def render(self, width: int) -> List[str]:
        pad = " " * max(0, width - len(self.name))
        line = f"  [{self.status:<4}] {self.name}{pad}"
        if self.detail:
            line += f"  {self.detail}"
        out = [line.rstrip()]
        if self.action and self.status != OK:
            out.append(f"{' ' * 10}→ {self.action}")
        return out


@dataclass
class EnvReport:
    config_dir: str = ""
    items: List[Item] = field(default_factory=list)

    def add(self, group: str, status: str, name: str, detail: str = "",
            action: str = "") -> Item:
        item = Item(group=group, status=status, name=name, detail=detail,
                    action=action)
        self.items.append(item)
        return item

    def rows(self, group: Optional[str] = None) -> List[Item]:
        return [i for i in self.items if group is None or i.group == group]

    def worst(self, group: Optional[str] = None) -> str:
        rows = self.rows(group)
        return max((i.status for i in rows), key=lambda s: _RANK[s], default=OK)

    def counts(self, rows: Optional[List[Item]] = None) -> str:
        rows = self.items if rows is None else rows
        parts = []
        for status in (OK, WARN, FAIL):
            n = sum(1 for i in rows if i.status == status)
            if n:
                parts.append(f"{n} {status}")
        return ", ".join(parts) or "empty"

    def render(self) -> str:
        width = max((len(i.name) for i in self.items), default=12)
        lines: List[str] = []
        for group in GROUPS:
            rows = self.rows(group)
            if not rows:
                continue
            rows.sort(key=lambda i: (-_RANK[i.status], i.name))
            lines.append(f"{group}  ({self.counts(rows)})")
            for item in rows:
                lines.extend(item.render(width))
            lines.append("")
        return "\n".join(lines).rstrip()

    def actions(self) -> List[str]:
        """所有非 OK 项的下一步动作（去重、保序）。"""
        seen, out = set(), []
        for item in self.items:
            if item.status != OK and item.action and item.action not in seen:
                seen.add(item.action)
                out.append(f"{item.name}: {item.action}")
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "config_dir": self.config_dir,
            "status": self.worst(),
            "counts": {s: sum(1 for i in self.items if i.status == s)
                       for s in (OK, WARN, FAIL)},
            "groups": {
                group: [
                    {"name": i.name, "status": i.status, "detail": i.detail,
                     "action": i.action} for i in self.rows(group)
                ]
                for group in GROUPS if self.rows(group)
            },
        }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def collect_core(report: EnvReport, config_dir: str) -> Optional[Any]:
    """Python / 依赖 / 配置。返回 Config；配置坏了返回 None（后续分组不再跑）。"""
    v = sys.version_info
    python_detail = f"{v.major}.{v.minor}.{v.micro}  ({sys.executable})"
    if (v.major, v.minor) < (3, 10):
        report.add("Core", FAIL, "python", python_detail,
                   "需要 Python 3.10+；建议 3.13")
    else:
        report.add("Core", OK, "python", python_detail)
    if os.name == "nt" and not os.environ.get("PYTHONIOENCODING"):
        report.add("Core", WARN, "console encoding", "PYTHONIOENCODING 未设置",
                   "Windows 控制台默认 GBK，中文日志会乱码：set PYTHONIOENCODING=utf-8")

    for mod, pip_name, required in (
            ("pydantic", "pydantic", True),
            ("yaml", "PyYAML", True),
            ("numpy", "numpy", False),
            ("faiss", "faiss-cpu", False)):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", getattr(m, "version", "?"))
            report.add("Core", OK, f"dep {pip_name}", str(ver))
        except ImportError:
            if required:
                report.add("Core", FAIL, f"dep {pip_name}", "未安装",
                           f"pip install -r requirements.txt（缺 {pip_name}）")
            else:
                report.add("Core", WARN, f"dep {pip_name}", "未安装",
                           "只影响进程内向量索引；不装则语义检索退化为词法："
                           "pip install -r requirements-semantic.txt")

    try:
        from mao.core.config import load_config

        config = load_config(config_dir)
    except Exception as exc:  # noqa: BLE001 - 面向使用者：给结论，不吐原始栈
        report.add("Core", FAIL, "config",
                   _friendly(str(exc)[:220]) or type(exc).__name__,
                   f"检查 {config_dir}/settings.yaml、agents.yaml、harness.yaml "
                   "的键名与取值（键写错时上面会指出是哪一个）")
        return None

    s = config.settings
    report.add("Core", OK, "config",
               f"{config_dir}  max_rounds={s.max_rounds}  "
               f"dry_run={s.dry_run}  runtime_dir={s.runtime_dir}")
    # 日志级别的真实情况（读代码判据，不是读配置里的装饰性字段）：
    # 控制台 INFO；每个任务目录下的 orchestrator.log 才是 DEBUG。
    # settings.verbose / debug_logging 目前不参与日志决策 —— 与其在这里
    # 猜一个级别吓用户，不如把两句话讲清楚。
    report.add("Core", OK, "logging",
               "控制台 INFO；runtime/<task>/logs/orchestrator.log 为 DEBUG（排障用）")
    return config


def _friendly(raw: str) -> str:
    """把 Pydantic/配置异常的原文压成人能执行的一句话（§36 错误信息产品化）。

    只做**降噪**，不猜测语义：取原文里最能定位问题的片段（字段名 + 一句
    message），并保留"看完整原文"的路径 —— 真看不懂时 `--verbose` 会给栈。
    """
    lines = [ln.strip() for ln in str(raw).replace("\n", " ").splitlines() if ln.strip()]
    text = " ".join(lines) if lines else str(raw)
    for sep in (": ", " - "):
        if sep in text:
            head, _, tail = text.partition(sep)
            if len(head) <= 90 and tail:
                return f"{head}: {tail[:120]}"
    return text[:200]


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
def collect_git(report: EnvReport, strategy: str) -> None:
    exe = shutil.which("git")
    if not exe:
        report.add("Git", FAIL, "git", "not found",
                   "安装 Git。GIT_WORKTREE 隔离与框架取证（git diff）都依赖它；"
                   "本工具不会替你安装")
        return
    probe = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    version = (probe.stdout or "").strip() or exe
    if strategy.upper() == "GIT_WORKTREE":
        report.add("Git", OK, "git", f"{version}")
        # 这是使用前提，不是本机缺陷：目录是不是 git 仓库要到提交任务时才确定，
        # 那时 GIT_WORKTREE 会给出明确错误（本程序不会替你 git init）。
        report.add("Git", OK, "worktree 前提",
                   "被调度的项目必须已经是 git 仓库；不是就用 COPY 策略")
    else:
        report.add("Git", OK, "git", f"{version}  (策略 {strategy}，git 非必需)")


# ---------------------------------------------------------------------------
# CLI Harnesses
# ---------------------------------------------------------------------------
def uses_real_cli(config: Any) -> bool:
    """是否有**角色**真的被绑到"要走命令行"的 Profile 上。

    只看结构，不看品牌；而且必须看绑定，不能只看 harness.yaml 里定义过什么：
    离线档里 `base_cli` 这种底座也写着 supports_cli，但没有任何角色用它，
    照定义判就会给一个纯 Mock 配置报"登录态未知"。
    """
    try:
        registry = config.profile_registry()
    except Exception:  # noqa: BLE001 - 判不出来就不该替用户隐藏结论
        return True
    for role in ("supervisor", "executor", "reviewer"):
        declared = getattr(getattr(config, role), "harness_profile", None)
        names = declared.values() if isinstance(declared, dict) else [declared]
        for name in names:
            if not name:
                continue
            try:
                profile = registry.resolve(str(name))
            except Exception:  # noqa: BLE001
                return True
            if getattr(profile, "supports_cli", False):
                return True
    return False


def collect_harness(report: EnvReport, config: Any) -> None:
    """复用 Orchestrator 的 preflight —— 与实跑同一个判据，不另写一套。"""
    try:
        from mao.bootstrap import build_orchestrator

        orch = build_orchestrator(
            config,
            runtime_root=Path(str(config.settings.runtime_dir)),
            echo=lambda _m: None,
        )
        preflight = orch.preflight()
    except Exception as exc:  # noqa: BLE001
        report.add("CLI Harnesses", FAIL, "preflight",
                   f"{type(exc).__name__}: {_friendly(str(exc))[:200]}",
                   "配置能加载但装配失败：先跑 python main.py providers 看 Profile")
        return

    for item in preflight.items:
        # 无任务的体检不该报"workspace not specified"：doctor 没有绑定工作区，
        # 真正该报的是工作区根目录能不能写 —— 那在 Workspace 分组里。
        if item.name == "workspace" and "not specified" in item.detail:
            continue
        # python / config 由 collect_core 报：它真的加载了配置、逐个查了依赖，
        # 比 preflight 的"能不能 import 到 Role"更严。同屏出现两条同名结论，
        # 用户会去猜哪条才算。
        if item.name in ("python", "config"):
            continue
        action = item.hint
        if item.name == "authentication":
            if not uses_real_cli(config):
                # 全是 Mock provider：没有"登录态"这件事，报出来只会让用户
                # 去查一个不存在的问题。
                continue
            # preflight 给的是"为什么不替你断定"。它那句"只能由一次真实调用证明"
            # 现在只对一半：codex 这类 CLI 有**本地**状态子命令，零调用（见
            # `tools/agent_probe.py`），所以下面另有一条按探测写的结论。
            action = ("本地能探的探完了就看下面那条 login；探不了的只能由一次"
                      "成功的真实调用证明：python tools/smoke_real_harness.py "
                      "--dry-run 看命令，加 --yes 才真跑（消耗配额）")
        report.add(_PREFLIGHT_GROUP.get(item.name, "CLI Harnesses"),
                   item.status, item.name, item.detail, action)
    if uses_real_cli(config):
        from tools import agent_probe

        facts = agent_probe.facts_for(config.binding_map(),
                                       config.profile_registry())
        bad = [r for r in facts if r["login"] == agent_probe.NOT_LOGGED_IN]
        if bad:
            report.add("CLI Harnesses", FAIL, "login",
                       "、".join(f"{r['role']}={r['profile']}" for r in bad)
                       + f" 用的 CLI 本机报：{bad[0]['login_detail']}",
                       "在终端跑一次 "
                       + Path(str(bad[0]['path'])).name.split('.')[0]
                       + " login（会开浏览器要授权，这一步只能本人做），"
                         "然后重跑 doctor")
        else:
            # 只有"没登录"才报结论，是半张答案：本机明明登录着，用户看到的却
            # 是上一句"cannot determine"，于是以为要在哪里填 API key。
            # 这个软件不存 key —— 凭据是各 CLI 自己的登录态（地雷 35 要第一屏答完）。
            logged = [r for r in facts
                      if r["login"] == agent_probe.LOGGED_IN]
            unknown = [r for r in facts
                       if r["login"] not in (agent_probe.LOGGED_IN,
                                             agent_probe.NOT_LOGGED_IN)]
            def _who(rows: list) -> str:
                return "、".join(
                    f"{r['role']}={Path(str(r['path'] or '')).name}"
                    f"（{r['login_detail']}）" for r in rows)

            if logged and not unknown:
                report.add("CLI Harnesses", OK, "login",
                           _who(logged) + " —— 这里不需要 API key，"
                           "凭据就是各 CLI 自己的登录态",
                           "可以直接开工：python tools/workbench.py"
                           "（第一个屏就是那一句提示词，角色绑在 "
                           "config/agents.yaml）")
            elif logged:
                report.add("CLI Harnesses", WARN, "login",
                           f"{_who(logged)} 已登录；"
                           f"{_who(unknown)} 本机探不了",
                           "探不了那一格不等于没登录；要确证只能由一次成功的"
                           "真实调用：python tools/smoke_real_harness.py "
                           "--dry-run 看命令，加 --yes 才真跑（消耗配额）")
            elif unknown:
                report.add("CLI Harnesses", WARN, "login",
                           _who(unknown),
                           "这些 CLI 没有本地状态子命令，只能由一次成功的真实"
                           "调用证明：python tools/smoke_real_harness.py "
                           "--dry-run 看命令，加 --yes 才真跑（消耗配额）")
    if not uses_real_cli(config):
        report.add("CLI Harnesses", OK, "harness",
                   "这份配置全是 Mock provider：不需要真实 CLI，零配额消耗")
    if config.settings.dry_run:
        report.add("CLI Harnesses", WARN, "execution mode", "dry_run=true",
                   "现在只会打印将要执行的命令，不会真的调用 Agent；"
                   "要实跑把 settings.dry_run 设为 false")
    elif uses_real_cli(config):
        report.add("CLI Harnesses", OK, "execution mode",
                   "真实调用（会消耗 CLI 配额）")
    else:
        report.add("CLI Harnesses", OK, "execution mode",
                   "Mock provider —— 不消耗任何配额")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def collect_scheduler(report: EnvReport, config: Any, create_runtime: bool) -> None:
    s = config.settings.scheduler
    if not s.enabled:
        report.add("Scheduler", WARN, "scheduler", "disabled",
                   "队列/调度不可用，只能单任务直跑：python main.py run")
        return
    report.add("Scheduler", OK, "concurrency",
               f"max_concurrent_tasks={s.max_concurrent_tasks} "
               f"worker_pool={s.worker_pool_size or '跟随并发'}")
    capacity = s.capacity
    report.add("Scheduler", OK, "capacity",
               f"global_agent_calls={capacity.global_agent_calls} "
               f"per_provider={capacity.provider_default}")
    limit = config.settings.effective_agent_call_limit()
    report.add("Scheduler", OK, "call budget",
               f"{limit} 次 Agent 调用/任务，max_rounds={config.settings.max_rounds}，"
               f"max_attempts={s.default_max_attempts}")
    report.add("Scheduler", OK, "retry",
               f"lease={s.lease_timeout_seconds}s heartbeat={s.heartbeat_seconds}s "
               f"aging={'on' if s.aging_enabled else 'off'}")

    db = ROOT / str(s.db_path)
    if not create_runtime:
        report.add("Scheduler", OK, "queue db", str(db))
        return
    try:
        from mao.scheduler import SystemClock, TaskRepository

        db.parent.mkdir(parents=True, exist_ok=True)
        repo = TaskRepository(db, clock=SystemClock())
        version = repo.schema_version()
        queued = len(repo.list())
        repo.close()
        report.add("Scheduler", OK, "queue db",
                   f"{db}  schema=v{version}  现有 {queued} 个任务")
    except Exception as exc:  # noqa: BLE001
        report.add("Scheduler", FAIL, "queue db",
                   f"{type(exc).__name__}: {_friendly(str(exc))[:160]}",
                   "scheduler.db_path 不可写或库损坏（修法见 docs/TROUBLESHOOTING.md）")


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
def collect_checkpoint(report: EnvReport, config: Any, create_runtime: bool) -> None:
    cp = config.settings.checkpoint
    if not cp.enabled:
        report.add("Checkpoint", WARN, "checkpoint", "disabled",
                   "关掉就没有断点续跑：进程崩溃后只能整任务重跑")
        return
    report.add("Checkpoint", OK, "resume",
               f"auto_resume={cp.auto_resume} "
               f"max_resume_epochs={cp.max_resume_epochs}")
    report.add("Checkpoint", OK, "integrity",
               f"validate_workspace={cp.validate_workspace} "
               f"validate_artifact_hashes={cp.validate_artifact_hashes}")
    report.add("Checkpoint", OK, "policies",
               f"incomplete={cp.execution_incomplete_policy} "
               f"workspace_mismatch={cp.workspace_mismatch_policy}")

    # 两种模式各自落在哪：单任务用 runtime_dir，调度模式用 attempts_root。
    # 生产配置里这两个常常是同一个目录，那就只报一次。
    single = ROOT / (str(cp.db_path) or Path(str(config.settings.runtime_dir)) / "checkpoints.db")
    scheduled = ROOT / str(config.settings.scheduler.attempts_root) / "checkpoints.db"
    paths = [single] if single == scheduled else [single, scheduled]
    where = str(single) if len(paths) == 1 else " / ".join(str(p) for p in paths)
    if not create_runtime:
        report.add("Checkpoint", OK, "checkpoint db", where)
        return
    try:
        from mao.checkpoints import SQLiteCheckpointStore

        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            SQLiteCheckpointStore(path, artifacts_root=path.parent).close()
        report.add("Checkpoint", OK, "checkpoint db", f"{where} 已就绪")
    except Exception as exc:  # noqa: BLE001
        report.add("Checkpoint", FAIL, "checkpoint db",
                   f"{type(exc).__name__}: {_friendly(str(exc))[:160]}",
                   "checkpoint.db_path / scheduler.attempts_root 不可用")


# ---------------------------------------------------------------------------
# Memory + Embedding Runtime
# ---------------------------------------------------------------------------
def _retrieval_mode(memory_cfg: Any) -> str:
    """取 memory.retrieval.mode。

    `retrieval` 在 Settings 模型里是**普通 dict**，不是子模型 —— 所以
    `getattr(retrieval, "mode", "lexical")` 永远拿不到值，会把 hybrid
    静默显示成 lexical。读配置要按它真实的类型读，不能靠 getattr 兜底猜。
    """
    retrieval = getattr(memory_cfg, "retrieval", None)
    if isinstance(retrieval, dict):
        return str(retrieval.get("mode") or "lexical")
    return str(getattr(retrieval, "mode", None) or "lexical")


def probe_embedding(semantic: Any) -> Dict[str, Any]:
    """构建 provider 并做握手探测（health 不加载模型，所以是秒级）。"""
    try:
        from mao.memory.embeddings import build_embedding_provider
        from mao.memory.vector_index import build_vector_index

        provider = build_embedding_provider(semantic)
    except Exception as exc:  # noqa: BLE001
        return {"healthy": False, "name": "-",
                "reason": f"provider 构造失败 {type(exc).__name__}"}
    try:
        healthy = bool(provider.health_check())
    except Exception as exc:  # noqa: BLE001
        provider.close()
        return {"healthy": False, "name": provider.name,
                "reason": f"{type(exc).__name__}: {str(exc)[:140]}"}
    # 探测结束就把 worker 关掉。以前这里不关：一次 doctor 就留下一个长驻
    # python 进程 + 它对向量索引/记忆库的句柄，后续命令可能被它锁住。
    out: Dict[str, Any] = {
        "healthy": healthy,
        "name": provider.name,
        "model_id": getattr(provider, "model_id", "") or "",
        "reason": "" if healthy else str(
            getattr(provider, "available_reason", "不可用"))[:160],
    }
    if not healthy:
        return out
    try:
        index = build_vector_index(semantic, provider)
        if index is not None and index.health_check():
            out["index"] = (True, index.backend_name, index.count())
        else:
            out["index"] = (False, "none", 0)
    except Exception as exc:  # noqa: BLE001
        out["index"] = (False, type(exc).__name__, 0)
    provider.close()
    return out


def effective_mode(config: Any) -> Tuple[str, str]:
    """按"真的能不能嵌入"决定检索模式，而不是照抄配置里的字符串。"""
    m = config.settings.memory
    configured = _retrieval_mode(m).upper()
    semantic = getattr(m, "semantic", None)
    if semantic is None or not getattr(semantic, "enabled", False):
        return "LEXICAL FALLBACK", "semantic disabled"
    probed = probe_embedding(semantic)
    if not probed["healthy"]:
        return "LEXICAL FALLBACK", probed["reason"] or "provider 不可用"
    usable = probed.get("index", (False, "none", 0))[0]
    if not usable:
        return "LEXICAL FALLBACK", "向量索引为空：python main.py memory index rebuild"
    return configured, "语义嵌入可用"


def collect_memory(report: EnvReport, config: Any, create_runtime: bool) -> None:
    m = config.settings.memory
    if not getattr(m, "enabled", False):
        report.add("Memory", OK, "memory", "disabled（没有长期记忆，其余功能不受影响）")
        return
    db = ROOT / str(m.path)
    try:
        if create_runtime:
            db.parent.mkdir(parents=True, exist_ok=True)
            from mao.memory.store import SQLiteMemoryStore

            store = SQLiteMemoryStore(str(db))
            close = getattr(store, "close", None)
            if callable(close):
                close()
        report.add("Memory", OK, "memory db", str(db))
    except Exception as exc:  # noqa: BLE001
        report.add("Memory", FAIL, "memory db",
                   f"{type(exc).__name__}: {_friendly(str(exc))[:160]}",
                   "memory.path 不可写或库损坏")

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        report.add("Memory", OK, "fts5", "available")
    except Exception:  # noqa: BLE001
        report.add("Memory", FAIL, "fts5", "不可用",
                   "词法检索是最后的退化路径，它也没有就等于记忆不可用："
                   "换用官方 Python 发行版（自带 FTS5）")
    finally:
        con.close()

    configured = _retrieval_mode(m).upper()
    mode, why = effective_mode(config)
    report.add("Memory", OK if mode == configured else WARN, "Memory Retrieval",
               f"{mode}  (config={configured}; {why})",
               "" if mode == configured else
               "语义档不可用 → 退化为词法检索（Runtime 仍完整可用）。"
               "恢复：python tools/setup_embeddings.py")
    report.add("Memory", OK, "outcome feedback",
               f"enabled={m.outcome_feedback.enabled} "
               f"role_aware={m.outcome_feedback.role_aware}"
               if getattr(m, "outcome_feedback", None) else "未配置")
    if getattr(m, "required", False):
        report.add("Memory", WARN, "memory.required", "true",
                   "语义缺席时任务会失败而不是降级 —— 除非确有必要，"
                   "建议把 memory.required 设回 false")


def collect_embedding_runtime(report: EnvReport, config: Any,
                              create_runtime: bool) -> None:
    m = config.settings.memory
    semantic = getattr(m, "semantic", None)
    if semantic is None or not getattr(semantic, "enabled", False):
        report.add("Embedding Runtime", OK, "semantic",
                   "disabled —— 检索为词法，不需要 ML 运行时")
        return
    from mao.harness.profiles import expand_env_placeholders

    def _ex(attr: str) -> str:
        value = expand_env_placeholders(str(getattr(semantic, attr, "") or ""))
        return "" if value.startswith("${") else value

    interp = _ex("worker_interpreter")
    if not interp:
        report.add("Embedding Runtime", WARN, "worker interpreter", "未配置",
                   "设 MEMORY_EMBEDDING_INTERPRETER 指向装了 torch==2.6.0+cpu + "
                   "sentence-transformers 的独立 venv，然后跑 "
                   "python tools/setup_embeddings.py（暂用词法检索，不影响可用性）")
    elif not Path(interp).is_file():
        report.add("Embedding Runtime", WARN, "worker interpreter",
                   f"不存在: {interp}",
                   "创建 ML venv：python tools/setup_embeddings.py")
    else:
        report.add("Embedding Runtime", OK, "worker interpreter", interp)

    model = _ex("model_path")
    hf_home = _ex("hf_home")
    if not model:
        report.add("Embedding Runtime", WARN, "bge-m3 model", "model_path 未配置",
                   "设 MEMORY_EMBEDDING_MODEL_PATH（例如 BAAI/bge-m3），"
                   "权重由 python tools/setup_embeddings.py 取")
    else:
        from tools.setup_embeddings import model_cache_dir

        cached = model_cache_dir(model, hf_home)
        if cached is None:
            report.add("Embedding Runtime", WARN, "bge-m3 model",
                       f"{model} 权重未缓存",
                       "python tools/setup_embeddings.py（约 2.2GB，只在显式运行时下载）")
        else:
            report.add("Embedding Runtime", OK, "bge-m3 model", str(cached))

    if not create_runtime:
        return
    probed = probe_embedding(semantic)
    if probed["healthy"]:
        index = probed.get("index") or (False, "none", 0)
        report.add("Embedding Runtime", OK, "worker health",
                   f"provider={probed['name']} 握手通过 "
                   f"(model={probed['model_id'] or '?'})")
        report.add("Embedding Runtime", OK if index[0] else WARN, "vector index",
                   f"backend={index[1]} entries={index[2]}",
                   "" if index[0] else
                   "没有可用的向量索引就没有语义检索："
                   "python main.py memory index rebuild")
    else:
        report.add("Embedding Runtime", WARN, "worker health",
                   probed["reason"] or "不可用",
                   "逐项定位：python main.py memory embeddings doctor；"
                   "期间检索为词法，任务不受影响")


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
def collect_workspace(report: EnvReport, config: Any, create_runtime: bool) -> None:
    s = config.settings
    strategy = str(s.scheduler.workspace.default_strategy or "DIRECT")
    report.add("Workspace", OK, "default strategy",
               f"{strategy}  (DIRECT=直接改原目录 / GIT_WORKTREE=隔离且并发安全 / "
               "COPY=非 git 项目)")
    root = ROOT / str(s.workspace_dir)
    try:
        if create_runtime:
            root.mkdir(parents=True, exist_ok=True)
        probe = root / ".mao-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        report.add("Workspace", OK, "workspace dir", f"{root} 可写")
    except Exception as exc:  # noqa: BLE001
        report.add("Workspace", FAIL, "workspace dir",
                   f"{root}: {type(exc).__name__} {_friendly(str(exc))[:120]}",
                   "该目录不可写：改 settings.workspace_dir 或修目录权限")

    if strategy.upper() == "GIT_WORKTREE":
        wt = ROOT / str(s.scheduler.workspace.worktree_root)
        try:
            if create_runtime:
                wt.mkdir(parents=True, exist_ok=True)
            existing = len([p for p in wt.glob("rt-*") if p.is_dir()]) if wt.exists() else 0
            report.add("Workspace", OK, "worktree root",
                       f"{wt}（现有 {existing} 个工作树）")
        except Exception as exc:  # noqa: BLE001
            report.add("Workspace", FAIL, "worktree root",
                       f"{wt}: {type(exc).__name__} {_friendly(str(exc))[:120]}",
                       "scheduler.workspace.worktree_root 不可创建")
    report.add("Workspace", OK, "delivery",
               "完成后交付 worktree + changes.patch + 框架证据；不自动合并回原仓库")


# ---------------------------------------------------------------------------
def collect(config_dir: str = "config", *, create_runtime: bool = True,
            include_harness: bool = True) -> EnvReport:
    """跑一遍体检。`create_runtime=False` 时只读不写（连数据区都不建）。"""
    report = EnvReport(config_dir=config_dir)
    config = collect_core(report, config_dir)
    if config is None:
        return report

    strategy = str(config.settings.scheduler.workspace.default_strategy or "DIRECT")
    collect_git(report, strategy)
    if include_harness:
        collect_harness(report, config)
    collect_scheduler(report, config, create_runtime)
    collect_checkpoint(report, config, create_runtime)
    collect_memory(report, config, create_runtime)
    collect_embedding_runtime(report, config, create_runtime)
    collect_workspace(report, config, create_runtime)
    return report


def static_mode(config: Any) -> str:
    """只查文件与环境变量、**不拉起 worker** 的检索模式判断。

    给调度器启动摘要用：那里每多一次进程拉起就多一秒，而且启动路径不该
    依赖 ML 运行时活着。判断结果与 effective_mode 一致的场合足够准。
    """
    m = config.settings.memory
    configured = _retrieval_mode(m).upper()
    semantic = getattr(m, "semantic", None)
    if semantic is None or not getattr(semantic, "enabled", False):
        return "LEXICAL FALLBACK"
    from mao.harness.profiles import expand_env_placeholders

    def _ex(attr: str) -> str:
        value = expand_env_placeholders(str(getattr(semantic, attr, "") or ""))
        return "" if value.startswith("${") else value

    interp = _ex("worker_interpreter")
    if not interp or not Path(interp).is_file():
        return "LEXICAL FALLBACK"
    model = _ex("model_path")
    if not model:
        return "LEXICAL FALLBACK"
    from tools.setup_embeddings import model_cache_dir

    if model_cache_dir(model, _ex("hf_home")) is None:
        return "LEXICAL FALLBACK"
    return configured


def summary_line(config: Any, config_dir: str = "config") -> str:
    """调度器启动时的一行配置摘要 —— 不含任何 secret。"""
    s = config.settings
    return (f"Config: {config_dir}  "
            f"Concurrency: {s.scheduler.max_concurrent_tasks}  "
            f"Capacity: {s.scheduler.capacity.global_agent_calls} calls / "
            f"{s.scheduler.capacity.provider_default} per provider  "
            f"Checkpoint: {'enabled' if s.checkpoint.enabled else 'disabled'}  "
            f"Memory: {static_mode(config).lower()}  "
            f"Workspace: {s.scheduler.workspace.default_strategy}")
