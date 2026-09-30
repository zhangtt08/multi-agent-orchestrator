"""release_check —— 一条命令回答"这份东西能不能交到别人手上"。

    python tools/release_check.py                  # 默认：不烧额度、不下模型
    python tools/release_check.py --semantic       # 加跑真实 BGE-M3 语义用例
    python tools/release_check.py --real-harness   # 加跑一次真实 Agent（消耗额度）
    python tools/release_check.py --package        # 加做干净 staging + zip + 审计
    python tools/release_check.py --quick          # 跳过两项慢检查（迭代时用）

检查项与它真实的判据：

```text
Repository        git 可用、源码被跟踪、无"被跟踪同时被忽略"、工作树干净
Config            三份配置都能加载；生产配置过一遍体检且没有 FAIL
CLI               --version / 各子命令 --help / doctor / providers 不 traceback
Smoke             tools/smoke_test.py 的 8 步端到端（全在临时目录，零额度）
DB lifecycle      临时库里：建库 -> 提交 -> 关进程 -> 重开 -> 状态还在
Fresh clone       tracked-files-only 检出能独立 import 并跑最小流程
Unit tests        全量 pytest，以 JUnit 为权威计数
baseline count    逐文件计数（与全量 JUnit 必须一致）
Semantic          真实 BGE-M3（未请求则 NOT REQUESTED）
Real harness      真实 Agent（未请求则引用最近一次验收证据，不谎称刚跑过）
Packaging         staging 不含运行数据/密钥/模型，且从 staging 里能跑通
```

纪律（§33）：本脚本**不改业务源码**。它只写临时目录、`--junitxml` 用的临时文件，
以及 `dist/`（已被 .gitignore 排除）。

退出码：0 = 全部 PASS（WARN 允许）；1 = 有 FAIL；2 = 脚本自己跑不下去。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL, SKIP, INFO = "PASS", "WARN", "FAIL", "SKIP", "INFO"
_RANK = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2, SKIP: 0}

# 发布包里禁止出现的形态（§59/§94）
FORBIDDEN_IN_PACKAGE = (
    ".git/", "__pycache__/", ".pytest_cache/", ".basetemp_run/",
    "runtime/", ".db", ".faiss", ".log", ".tmp", ".pyc",
    "runtime_p", "runtime_worktrees/", "runtime_scheduler/",
    "runtime_checkpoints/", "memory/", "dist/", ".venv", "envs/",
    "migration_backup/", ".env", "sitecustomize",
)
# 允许的同名源码（守卫的例外：mao/memory 是源码包，不是运行数据目录）
ALLOWED_LOOKALIKES = ("mao/memory/", "mao/workspaces/", "mao/checkpoints/",
                      "mao/scheduler/", "mao/core/", "mao/harness/",
                      "mao/agents/", "mao/transports/", "examples/config_minimal/")


class Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = PASS
        self.lines: List[str] = []

    def note(self, text: str) -> None:
        self.lines.append(text)

    def fail(self, text: str) -> None:
        self.status = FAIL
        self.lines.append(text)

    def warn(self, text: str) -> None:
        if self.status != FAIL:
            self.status = WARN
        self.lines.append(text)


def _run(cmd: List[str], *, timeout: float = 1800.0, cwd: Path = ROOT,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        full_env.update(env)
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, env=full_env)


def _tail(text: str, limit: int = 6) -> List[str]:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-limit:]


# ---------------------------------------------------------------------------
def check_repository(py: str) -> Check:
    c = Check("Repository")
    probe = _run(["git", "--version"])
    if probe.returncode != 0:
        c.fail("git 不可用 —— 无法证明源码被跟踪（迁移轮的直接教训）")
        return c
    tracked = _run(["git", "ls-files"])
    if tracked.returncode != 0:
        c.fail(f"不是 git 仓库：{tail_message(tracked)}")
        return c
    names = [n for n in tracked.stdout.splitlines() if n.strip()]
    c.note(f"{len(names)} 个受跟踪文件")
    offenders = _run(["git", "ls-files", "-i", "-c", "--exclude-standard"])
    both = [ln for ln in offenders.stdout.splitlines() if ln.strip()]
    if both:
        c.fail(f"{len(both)} 个文件既被跟踪又命中 ignore 规则：{both[:5]}")
    dirty = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in dirty.stdout.splitlines() if ln.strip()]
    if changed:
        c.warn(f"工作树不干净（{len(changed)} 项）：提交后再跑一次才是发布态")
        c.note("  " + "; ".join(ch[:40] for ch in changed[:6]))
    for required in ("mao/core", "mao/memory", "mao/workspaces", "mao/checkpoints",
                     "config", "tools", "examples", "tests"):
        hit = [n for n in names if n.startswith(required.replace("/", "\\") + "\\")
               or n.startswith(required + "/")]
        if not hit:
            c.fail(f"{required}/ 没有任何被跟踪的文件")
    return c


def tail_message(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-1][:160]


def check_config(py: str) -> Check:
    c = Check("Config")
    for name in ("config", "config_offline", "examples/config_minimal"):
        try:
            out = _run([py, "-c",
                        "import sys; sys.path.insert(0, r'%s');"
                        "from mao.core.config import load_config;"
                        "cfg = load_config(%r);"
                        "print('max_rounds', cfg.settings.max_rounds,"
                        " 'scheduler', cfg.settings.scheduler.enabled,"
                        " 'checkpoint', cfg.settings.checkpoint.enabled)"
                        % (str(ROOT), name)], timeout=120)
        except subprocess.TimeoutExpired:
            c.fail(f"{name}: 加载超时")
            continue
        if out.returncode != 0:
            c.fail(f"{name}: 加载失败 —— {tail_message(out)}")
        else:
            c.note(f"{name}: {out.stdout.strip()}")
    # 生产配置必须能过体检，并且体检里没有 FAIL
    from tools.env_report import FAIL as R_FAIL
    from tools.env_report import collect

    report = collect("config", create_runtime=True)
    for item in report.items:
        if item.status == R_FAIL and item.name != "login":
            c.fail(f"生产配置体检 FAIL: {item.group}/{item.name}: {item.detail}")
        elif item.status == R_FAIL:
            # `login` 是唯一一条**本机前提**而不是发布物缺陷的 FAIL：CLI 没登录
            # 必须让 doctor 喊出来（业主 2026-09-30 就是被这件事挡住了，而软件
            # 一句没说），但它不该让"这份东西对不对"的检查变红。
            c.note("  login FAIL = 这台机器的 agent CLI 没登录，不是发布物的缺陷。"
                   "真跑之前先照那条结论里的命令登录一次。")
    counts = [f"{sum(1 for i in report.items if i.status == s)} {s}"
              for s in ("OK", WARN, FAIL)]
    c.note("doctor 分组体检（config/）: " + ", ".join(counts))
    if report.worst() == WARN:
        c.note("  WARN 全部是可选组件缺席（语义档/登录态），属预期降级")
    return c


def check_cli(py: str) -> Check:
    c = Check("CLI")
    main = str(ROOT / "main.py")
    # doctor 允许的非零退出码只有一个来源：这台机器的 agent CLI 没登录
    # （与 check_configs 里那条例外同源）。除 login 之外的 FAIL 必须让它回 0。
    from tools.env_report import FAIL as R_FAIL
    from tools.env_report import collect as _collect

    doc_rc = 0 if not [i for i in _collect("config", create_runtime=False).items
                       if i.status == R_FAIL and i.name != "login"] else 1
    invocations = [
        ([main, "--version"], 0), ([main, "--help"], 0),
        ([main, "doctor"], doc_rc), ([main, "doctor", "--doctor-json"], doc_rc),
        ([main, "doctor", "--help"], 0),
        ([main, "providers"], 0), ([main, "--discover"], 0),
        ([main, "queue", "--help"], 0), ([main, "queue", "submit", "--help"], 0),
        ([main, "scheduler", "--help"], 0), ([main, "scheduler", "status"], 0),
        ([main, "checkpoint", "--help"], 0), ([main, "memory", "--help"], 0),
        ([main, "memory", "list"], 0),
        ([str(ROOT / "tools" / "bootstrap.py")], 0),
        ([str(ROOT / "tools" / "smoke_test.py"), "--help"], 0),
        ([str(ROOT / "tools" / "smoke_real_harness.py"), "--dry-run"], 0),
        ([str(ROOT / "tools" / "setup_embeddings.py"), "--check"], 0),
        # 预期错误必须说人话：非零退出 + 一句能照做的说明，且不能抛栈
        ([main, "queue", "submit", "--goal", "x",
          "--workspace", str(ROOT / "no-such-dir")], 2),
        ([main, "queue", "show", "rt-doesnotexist"], 1),
    ]
    for argv, expected_rc in invocations:
        try:
            out = _run([py] + argv, timeout=600)
        except subprocess.TimeoutExpired:
            c.fail(f"{' '.join(argv)}: 超时")
            continue
        text = (out.stdout or "") + (out.stderr or "")
        label = " ".join(a.replace(str(ROOT) + os.sep, "") for a in argv[1:])
        if "Traceback (most recent call last)" in text:
            c.fail(f"{label} 抛栈 —— 预期错误必须是人话")
            c.note("  " + " | ".join(_tail(text, 3)))
            continue
        if out.returncode != expected_rc:
            c.fail(f"{label}: 退出码 {out.returncode}，期望 {expected_rc}")
            c.note("  " + " | ".join(_tail(text, 2)))
        elif expected_rc != 0 and not (
                "error" in text.lower() or "未找到" in text
                or "reject" in text.lower()):
            c.warn(f"{label}: 非零退出但没有可读说明")
    return c


def check_smoke(py: str) -> Check:
    c = Check("Smoke")
    try:
        out = _run([py, str(ROOT / "tools" / "smoke_test.py")], timeout=900)
    except subprocess.TimeoutExpired:
        c.fail("smoke_test 超时")
        return c
    if out.returncode != 0:
        c.fail(f"smoke_test rc={out.returncode}")
    for line in _tail(out.stdout, 4):
        c.note(line)
    leaked = [p for p in (ROOT / "runtime_worktrees").glob("rt-*")
              if p.is_dir() and (time_stamp_newer(p))]
    if leaked:
        c.warn(f"仓库工作树根目录里有本次新产生的目录（自检应只用临时目录）：{leaked[:3]}")
    return c


def time_stamp_newer(path: Path) -> bool:
    """本次运行期间创建的目录 —— 用启动时间比较，避免把历史产物误判成泄漏。"""
    return path.stat().st_mtime >= START_TS - 1


def check_db_lifecycle(py: str) -> Check:
    """建库 → 提交 → 关掉 → 重开 → 状态还在。首次运行必须自动初始化。"""
    c = Check("DB lifecycle")
    tmp = Path(tempfile.mkdtemp(prefix="mao-release-db-"))
    try:
        script = """
import sys
sys.path.insert(0, %r)
from pathlib import Path
from mao.core.models import Task
from mao.scheduler import (RuntimeStatus, SystemClock, TaskRepository,
                           TaskSubmissionService)
tmp = Path(%r)
repo = TaskRepository(tmp / "queue.db", clock=SystemClock())
assert repo.schema_version() >= 1
service = TaskSubmissionService(repo, clock=repo.clock, default_priority="NORMAL",
                                default_max_attempts=2, default_strategy="COPY")
rt = service.submit(Task(goal="release check persistence probe", max_rounds=1),
                    config_dir="release_check")
rt_id = rt.runtime_task_id
repo.close()
repo2 = TaskRepository(tmp / "queue.db", clock=SystemClock())
back = repo2.get(rt_id)
assert back is not None, "重开之后任务不见了"
assert back.status == RuntimeStatus.QUEUED, back.status
print("schema", repo2.schema_version(), "status", back.status.value,
      "attempt", back.attempt)
repo2.close()
from mao.checkpoints import SQLiteCheckpointStore
store = SQLiteCheckpointStore(tmp / "cp.db", artifacts_root=tmp)
store.close()
print("checkpoint db ok")
from mao.memory.store import SQLiteMemoryStore
mem = SQLiteMemoryStore(str(tmp / "memory.db"))
close = getattr(mem, "close", None)
callable(close) and close()
print("memory db ok")
""" % (str(ROOT), str(tmp))
        out = _run([py, "-c", script], timeout=300)
        if out.returncode != 0:
            c.fail(f"队列/checkpoint/memory 生命周期不成立 —— {tail_message(out)}")
        for line in _tail(out.stdout, 4):
            c.note(line)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return c


def check_fresh_clone(py: str) -> Check:
    c = Check("Fresh clone")
    out = _run([py, "-m", "pytest", "tests/test_repository_integrity.py",
                "-q", "-p", "no:cacheprovider", "--no-header"], timeout=1800)
    if out.returncode != 0:
        c.fail(f"完整性/fresh-clone 守卫未通过（rc={out.returncode}）")
    for line in _tail(out.stdout, 3):
        c.note(line)
    return c


def check_unit_tests(py: str, junit: Path) -> Check:
    from tools.baseline_count import parse_junit

    c = Check("Unit tests")
    out = _run([py, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
                "--no-header", f"--junitxml={junit}"], timeout=3600)
    summary = parse_junit(junit.read_text(encoding="utf-8")) if junit.exists() else None
    if summary is None:
        c.fail(f"拿不到 JUnit（rc={out.returncode}）—— 计数不能靠终端点数")
        for line in _tail(out.stdout, 5):
            c.note(line)
        return c
    text = (f"tests={summary['tests']} failures={summary['failures']} "
            f"errors={summary['errors']} skipped={summary['skipped']}")
    c.note(text)
    if summary["failures"] or summary["errors"]:
        c.fail(f"有红：{text}")
        for line in _tail(out.stdout, 8):
            if "FAILED" in line or "ERROR" in line:
                c.note("  " + line[:160])
    elif out.returncode != 0:
        # JUnit 说没有红，但 pytest 退出码非零 —— 两者不一致就是完整性问题
        c.fail(f"JUnit 全绿但 pytest rc={out.returncode}（退出码与计数不一致）")
    return c


def check_baseline_count(py: str) -> Check:
    c = Check("Baseline count")
    out = _run([py, str(ROOT / "tools" / "baseline_count.py")], timeout=3600)
    if out.returncode == 2:
        c.fail(f"INFRA：计数过程本身没跑完 —— rc=2")
    elif out.returncode == 1:
        c.fail("逐文件计数发现失败/错误的文件")
    for line in _tail(out.stdout, 5):
        c.note(line)
    return c


def check_semantic(py: str) -> Check:
    c = Check("Semantic")
    env_set = os.environ.get("MEMORY_EMBEDDING_INTERPRETER")
    if not env_set or not Path(env_set).is_file():
        c.fail("请求了 --semantic 但 MEMORY_EMBEDDING_INTERPRETER 不可用 —— "
               "先 python tools/setup_embeddings.py")
        return c
    out = _run([py, "-m", "pytest", "-m", "semantic_model", "-q",
                "-p", "no:cacheprovider", "--no-header"], timeout=3600)
    if out.returncode != 0:
        c.fail(f"真实 BGE-M3 用例未通过（rc={out.returncode}）")
    for line in _tail(out.stdout, 4):
        c.note(line)
    return c


def check_real_harness(py: str, requested: bool) -> Check:
    c = Check("Real harness")
    if requested:
        print("\n  ⚠ 即将进行**真实 Agent 调用**，消耗订阅额度（-m real_harness 全量）。")
        out = _run([py, "-m", "pytest", "-m", "real_harness", "-q",
                    "-p", "no:cacheprovider", "--no-header"], timeout=5400)
        if out.returncode != 0:
            c.fail(f"真实用例未通过（rc={out.returncode}）")
        for line in _tail(out.stdout, 5):
            c.note(line)
        return c
    evidence = ROOT / "runtime_p10" / "demo_evidence" / "rejudge_report.json"
    if not evidence.exists():
        c.warn("未请求 real harness，且找不到最近一次验收证据文件 —— 无法引用")
        return c
    try:
        data = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        c.warn(f"验收证据读不了：{exc}")
        return c
    verdict = data.get("verdict")
    if verdict != "PASS":
        c.fail(f"最近一次真实验收结论是 {verdict}，不是 PASS")
        return c
    c.note("VERIFIED_FROM_LATEST_ACCEPTANCE（不是本次跑的）")
    c.note(f"  证据: {evidence.relative_to(ROOT)}")
    c.note(f"  runtime_task={data.get('runtime_task_id')} "
           f"attempt={data.get('attempt')} epoch={data.get('resume_epoch')} "
           f"next={data.get('next_stage')} final={data.get('final_status')}")
    boundary = data.get("process_boundary") or {}
    # 进程边界成立的可观察判据（写在证据里，不靠回忆）：
    # 验证 stage 的 COMMIT 记录在 resume 判定之前，且首个 REVIEW COMMIT 在其之后
    proven = bool(boundary.get("verification_committed_before_resume")) \
        and bool(boundary.get("resume_before_first_review_commit"))
    c.note(f"  stage 顺序判据={'成立' if proven else '不成立'} "
           f"(verify_commit#{boundary.get('verification_commit_index')} < "
           f"resume#{boundary.get('resume_index')} < "
           f"first_review_commit#{boundary.get('first_review_commit_index')})")
    if not proven:
        c.fail("最近一次真实验收证据里没有成立的进程边界判据")
    c.note(f"  按轮调用数={data.get('calls_by_role_round')}")
    return c


# ---------------------------------------------------------------------------
def check_packaging(py: str) -> Check:
    """git archive → dist staging → 审计内容 → 从 staging 里真的跑一遍 → zip。

    为什么不用 shutil.copytree：整个目录复制会把运行数据、模型缓存、虚拟环境、
    以及"只对这台机器有效"的东西一起带走 —— 而那些正是发布物必须排除的。
    """
    from mao import __version__

    c = Check("Packaging")
    dist = ROOT / "dist"
    name = f"multi-agent-orchestrator-{__version__}"
    staging = dist / name
    if staging.exists():
        shutil.rmtree(staging)
    dist.mkdir(parents=True, exist_ok=True)

    archive = dist / f"{name}.tar"
    proc = _run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"])
    if proc.returncode != 0:
        c.fail(f"git archive 失败：{tail_message(proc)}")
        return c
    staging.mkdir(parents=True, exist_ok=True)
    import tarfile

    with tarfile.open(archive) as tar:
        try:
            tar.extractall(path=str(staging), filter="data")
        except TypeError:      # Python < 3.12 没有 filter 参数
            tar.extractall(path=str(staging))
    archive.unlink()

    files = sorted(p.relative_to(staging).as_posix()
                   for p in staging.rglob("*") if p.is_file())
    if not files:
        c.fail("staging 是空的")
        return c
    c.note(f"staging: {len(files)} 个文件（来自 git archive HEAD，非目录复制）")

    bad = []
    for rel in files:
        for token in FORBIDDEN_IN_PACKAGE:
            if token.endswith("/"):
                if rel.startswith(token) or f"/{token}" in f"/{rel}":
                    if not rel.startswith(ALLOWED_LOOKALIKES):
                        bad.append(f"{rel} ~ {token}")
                    break
            elif rel.endswith(token):
                bad.append(f"{rel} ~ {token}")
    if bad:
        c.fail("发布物里有不该出现的东西：\n  " + "\n  ".join(bad[:10]))

    required = ("README.md", "AGENTS.md", "VERSION", "requirements.txt", "main.py",
                "config/settings.yaml", "examples/task_single.json",
                "docs/USER_GUIDE.md", "tools/bootstrap.py",
                "mao/core/orchestrator.py", "tests/test_release_boundaries.py")
    missing = [r for r in required if not (staging / r).exists()]
    if missing:
        c.fail(f"发布物缺少必要文件：{missing}")
    if (staging / ".git").exists():
        c.warn("staging 里带了 .git —— 软件不该把 git 当运行依赖")

    # 从 staging 里跑：核心必须不依赖 .git 也能 import / doctor 核心 / 假跑
    checks = [
        ("import", "-c", "import sys; sys.path.insert(0, '.'); import mao, "
                         "mao.bootstrap, mao.scheduler, mao.checkpoints; "
                         "print('mao', mao.__version__)"),
    ]
    for label, flag, code in checks:
        out = _run([py, flag, code], cwd=staging, timeout=300)
        if out.returncode != 0:
            c.fail(f"staging 里 {label} 失败：{tail_message(out)}")
        else:
            c.note(f"  staging {label}: {out.stdout.strip()[:80]}")
    out = _run([py, "main.py", "doctor", "--config-dir", "examples/config_minimal"],
               cwd=staging, timeout=600)
    if out.returncode != 0 or "Traceback" in (out.stdout + out.stderr):
        c.fail("staging 里 doctor 跑不过 —— 这就是「另一台机器第一次运行」的替身")
    else:
        c.note("  staging doctor(examples/config_minimal): OK")
    out = _run([py, str(staging / "tools" / "smoke_test.py")],
               cwd=staging, timeout=900)
    if out.returncode != 0:
        c.fail("staging 里端到端自检未通过")
    else:
        c.note("  staging smoke_test: 8/8")

    zip_path = dist / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(staging / rel, f"{name}/{rel}")
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    c.note(f"  zip: {zip_path.relative_to(ROOT)}  {size_mb:.2f} MB  {len(files)} 条目")

    # 解压审计：zip 里不能多出 staging 没有的东西，也不能含禁用形态
    audit_dir = Path(tempfile.mkdtemp(prefix="mao-zip-audit-"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            zf.extractall(audit_dir)
        offenders = []
        for entry in names:
            rel = entry.split("/", 1)[1] if "/" in entry else entry
            if not rel:
                continue
            for token in FORBIDDEN_IN_PACKAGE:
                if token.endswith("/") and rel.startswith(token) \
                        and not rel.startswith(ALLOWED_LOOKALIKES):
                    offenders.append(f"{rel} ~ {token}")
                    break
                if not token.endswith("/") and rel.endswith(token):
                    offenders.append(f"{rel} ~ {token}")
                    break
        if offenders:
            c.fail("zip 内含禁用内容：" + "; ".join(offenders[:6]))
        unzipped = audit_dir / name
        out = _run([py, "-c", "import sys; sys.path.insert(0, '.'); import mao; "
                              "print(mao.__version__)"],
                   cwd=unzipped, timeout=300)
        if out.returncode != 0:
            c.fail("解压后的目录 import 不进来")
        else:
            zipped = [n for n in names if not n.endswith("/")]
            if len(zipped) != len(files):
                c.fail(f"zip 条目数 {len(zipped)} 与 staging {len(files)} 不一致")
            else:
                c.note(f"  解压审计: import ok，{len(zipped)} 条目与 staging 一致")
    finally:
        shutil.rmtree(audit_dir, ignore_errors=True)
    return c


# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="release_check",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--semantic", action="store_true",
                        help="额外运行真实 BGE-M3 语义用例（会加载模型，耗时数十秒）")
    parser.add_argument("--real-harness", action="store_true", dest="real_harness",
                        help="额外运行真实 Agent 用例：**消耗订阅额度**")
    parser.add_argument("--package", action="store_true",
                        help="额外生成 dist staging + zip 并审计（不碰业务源码）")
    parser.add_argument("--quick", action="store_true",
                        help="跳过全量测试与逐文件计数两项慢检查（迭代用）")
    parser.add_argument("--keep", action="store_true",
                        help="保留 dist/ 里的 staging 目录与 zip（默认保留 zip）")
    args = parser.parse_args(argv)

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    py = sys.executable
    from mao import __version__

    print("=" * 78)
    print(f" release check —— Multi-Agent Orchestrator {__version__}")
    print(f" python   : {py}")
    print(f" flags    : semantic={args.semantic} real_harness={args.real_harness} "
          f"package={args.package} quick={args.quick}")
    if args.real_harness:
        print(" ！真实 Agent 调用已获显式确认，会消耗额度")
    print("=" * 78)

    if args.quick:
        print("\n  ! --quick：本次**不**跑全量测试与逐文件计数，不能用作发布结论")

    junit = Path(tempfile.mkdtemp(prefix="mao-release-junit-")) / "full.xml"
    results: List[Check] = []
    for fn, kwargs in ((check_repository, {}), (check_config, {}), (check_cli, {}),
                       (check_smoke, {}), (check_db_lifecycle, {}),
                       (check_fresh_clone, {})):
        check = fn(py, **kwargs)
        results.append(check)
        _echo(check)
    if not args.quick:
        results.append(_echo(check_unit_tests(py, junit)))
        results.append(_echo(check_baseline_count(py)))
    if args.semantic:
        results.append(_echo(check_semantic(py)))
    else:
        skipped = Check("Semantic")
        skipped.status = SKIP
        skipped.note("NOT REQUESTED —— python tools/release_check.py --semantic")
        results.append(_echo(skipped))
    results.append(_echo(check_real_harness(py, args.real_harness)))
    if args.package:
        results.append(_echo(check_packaging(py)))

    print("\n" + "=" * 78)
    for check in results:
        print(f" {check.status:<5} {check.name}")
    failed = [c.name for c in results if c.status == FAIL]
    warned = [c.name for c in results if c.status == WARN]
    skipped = [c.name for c in results if c.status == SKIP]
    print("-" * 78)
    print(f" PASS={sum(1 for c in results if c.status == PASS)}  "
          f"WARN={len(warned)}  FAIL={len(failed)}  SKIP={len(skipped)}")
    if failed:
        print(f" 阻塞项：{', '.join(failed)}")
    if warned:
        print(f" 需判断：{', '.join(warned)}")
    if args.quick and not failed:
        print(" 注意：--quick 跳过了全量测试 —— 这个结论不足以支撑发布判定")
    print("=" * 78)
    return 1 if failed or (not args.quick and warned and _warn_blocks_release(warned)) else 0


def _warn_blocks_release(warned: List[str]) -> bool:
    """工作树不干净这一条 WARN 会挡住"发布态"结论，其余不会。"""
    return "Repository" in warned


def _echo(check: Check) -> Check:
    print(f"\n[{check.status}] {check.name}")
    for line in check.lines:
        print("  " + line)
    return check


START_TS = __import__("time").time()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
