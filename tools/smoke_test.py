"""smoke_test —— 秒级端到端自检，不碰任何真实 CLI、零配额。

    python tools/smoke_test.py

它验证的是"这套装配在这台机器上能跑通一次完整生命周期"：

```text
配置加载  →  runtime 目录/DB 自动初始化  →  队列提交
        →  调度器真实 tick（inline，Mock Agent）
        →  任务到达终态
        →  checkpoint 链存在且可校验
        →  恢复点选择可用（不重跑 Agent）
        →  产物与历史可被用户找到
```

刻意全在临时目录里跑：不写仓库的 runtime/，不碰任何已冻结的验收证据。
默认用离线 Mock 档（`config_p10_offline`）；`--config-dir config` 会真的去调
Codex/Claude 并烧配额，所以这里不允许——要验真实链路请用
`tools/smoke_real_harness.py`，它需要你显式确认。

退出码：0 全通过；1 任一环节失败；2 自检本身没跑起来。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_GOAL = (
    "修复 calculator.py 中 multiply() 的 bug：multiply(a, b) 必须返回 a * b。"
    "只允许修改 calculator.py；禁止修改 test_calculator.py。"
    "验收命令：pytest test_calculator.py::test_multiply -q"
)
TEMPLATE = ROOT / "tools" / "phase10_demo_source"


class Step:
    """一个检查步骤：记录名字、结果、以及失败时用户能做什么。"""

    def __init__(self, name: str):
        self.name = name
        self.ok = False
        self.detail = ""
        self.action = ""

    def fail(self, detail: str, action: str = "") -> "Step":
        self.detail = detail
        self.action = action
        return self

    def pass_(self, detail: str = "") -> "Step":
        self.ok = True
        self.detail = detail
        return self


def _make_workspace(dest: Path) -> tuple[Path, list[str]]:
    """造一个真实 git 小仓库作为任务源工作区。

    不自动 git init 用户的目录 —— 这里 init 的是**我们自己造的**临时目录，
    性质不同：它是自检的道具。
    """
    notes: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("calculator.py", "test_calculator.py", ".gitignore"):
        src = TEMPLATE / name
        if src.is_file():
            shutil.copyfile(src, dest / name)
        else:                                    # 模板缺文件也要如实报出来
            notes.append(f"模板缺文件 {name}")
    git = shutil.which("git")
    if not git:
        return dest, notes + ["git not found"]

    def run(*args: str) -> str:
        p = subprocess_run([git, "-c", "user.email=smoke@example.com",
                            "-c", "user.name=smoke", *args], cwd=str(dest))
        if p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {p.stderr[:200]}")
        return p.stdout or ""

    run("init", "-q", "-b", "master")
    run("add", "-A")
    run("commit", "-q", "-m", "baseline")
    notes.append("HEAD=" + run("rev-parse", "HEAD").strip()[:8])
    return dest, notes


def subprocess_run(cmd: list[str], cwd: str) -> "object":
    import subprocess

    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_test")
    parser.add_argument("--keep", action="store_true",
                        help="保留临时目录并打印路径（排查用）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(None if argv is None else argv[1:])

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    steps: list[Step] = []
    tmp = Path(tempfile.mkdtemp(prefix="mao-smoke-"))
    try:
        # ---- 1. 配置加载（强制离线档：绝不碰真实 CLI）--------------------
        s = Step("config load")
        try:
            from mao.core.config import load_config

            config = load_config("config_p10_offline")
            if config.settings.dry_run:
                s.fail("dry_run=true 会让自检看不到真实子进程行为")
            else:
                s.pass_(f"config_p10_offline  checkpoint="
                       f"{config.settings.checkpoint.enabled}")
        except Exception as exc:  # noqa: BLE001
            s.fail(f"{type(exc).__name__}: {exc}",
                   "检查 config_p10_offline/ 三份文件是否完整")
        steps.append(s)
        if not s.ok:
            return report(steps, tmp, args)

        st = config.settings
        # 全部落点改到临时目录：自检不留垃圾，也不碰已冻结的证据
        st.runtime_dir = str(tmp / "runtime")
        st.workspace_dir = str(tmp / "workspaces")
        st.memory.path = str(tmp / "runtime" / "memory.db")
        st.scheduler.db_path = str(tmp / "runtime" / "queue.db")
        st.scheduler.attempts_root = str(tmp / "runtime")
        st.checkpoint.db_path = str(tmp / "runtime" / "checkpoints.db")
        st.scheduler.max_concurrent_tasks = 1
        st.scheduler.worker_pool_size = 0          # inline：可观察、可复现
        st.scheduler.lease_timeout_seconds = 6
        st.scheduler.heartbeat_seconds = 2
        # worktree 根目录也在 scheduler.workspace 下面 —— 不覆盖就会写到**仓库自己的**
        # runtime_worktrees/。上一版就漏了这一条，自检跑完在仓库里留下 3 个 worktree，
        # 而它自己的 docstring 还写着"刻意全在临时目录里跑"。
        st.scheduler.workspace.worktree_root = str(tmp / "worktrees")

        # ---- 2. 工作区与 git -------------------------------------------
        s = Step("workspace + git")
        try:
            ws, notes = _make_workspace(tmp / "src")
            s.pass_("; ".join(n for n in notes if n))
        except Exception as exc:  # noqa: BLE001
            ws = tmp / "src"
            s.fail(str(exc)[:200], "自检需要一个真实 git 仓库作道具：装 Git")
        steps.append(s)
        if not s.ok:
            return report(steps, tmp, args)

        # ---- 3. 队列提交 ------------------------------------------------
        s = Step("queue submit")
        rt_id = task_id = ""
        try:
            from mao.core.models import Task
            from tools.scheduler_cli import (build_repo_from_config,
                                             build_submission_service)

            repo = build_repo_from_config(config)
            try:
                service = build_submission_service(config, repo)
                task = Task(goal=DEMO_GOAL, workspace_path=str(ws),
                            max_rounds=2,
                            constraints=["不得修改测试文件"],
                            context={"project": "calculator-demo",
                                     "symptom": "multiply(2, 3) returns 5",
                                     # Mock Adapter 按剧本走；不给剧本它就用默认
                                     # 剧情（一直 FAIL），那测的是剧本不是链路。
                                     "acceptance_script": "immediate_pass"})
                rt = service.submit(task, config_dir="config_p10_offline")
                rt_id, task_id = rt.runtime_task_id, rt.task_id
                s.pass_(f"{rt_id} status={rt.status.value} "
                        f"strategy={getattr(rt, 'workspace_strategy', '')}")
            finally:
                repo.close()
        except Exception as exc:  # noqa: BLE001
            s.fail(f"{type(exc).__name__}: {exc}", "队列/DB 初始化失败")
        steps.append(s)
        if not s.ok:
            return report(steps, tmp, args)

        # ---- 4. 调度器真实 tick ----------------------------------------
        s = Step("scheduler run")
        try:
            from mao.scheduler import RuntimeStatus
            from tools.scheduler_cli import (build_repo_from_config,
                                             build_scheduler_from_config)

            repo = build_repo_from_config(config)
            try:
                sched = build_scheduler_from_config(
                    config, repo, config_dir="config_p10_offline")
                # echo 的默认值本身就是 no-op；显式传 None 会炸在 echo(...) 上。
                sched.run(max_ticks=60, poll_seconds=0.2,
                          with_heartbeat=False,
                          echo=(print if args.verbose else (lambda _m: None)))
                row = repo.get(rt_id)
                status = row.status
                final_err = row.last_error or ""
            finally:
                repo.close()
            if status == RuntimeStatus.COMPLETED:
                s.pass_(f"COMPLETED  attempt={getattr(row, 'attempt', '?')}")
            else:
                s.fail(f"status={status.value} last_error={final_err[:160]}",
                       "任务没到终态：看 runtime 目录下的 history.jsonl")
        except Exception as exc:  # noqa: BLE001
            s.fail(f"{type(exc).__name__}: {exc}", "调度器没能跑起来")
        steps.append(s)

        # ---- 5. checkpoint 链 + 完整性 --------------------------------
        s = Step("checkpoint chain")
        try:
            from mao.checkpoints import ResumeManager, SQLiteCheckpointStore

            store = SQLiteCheckpointStore(Path(st.checkpoint.db_path),
                                          artifacts_root=Path(st.scheduler.attempts_root))
            try:
                recs = [r for r in store.list_for_task(task_id)
                        if r.status.value == "COMMITTED"]
                stages = [r.stage.value for r in recs]
                bad = [r.checkpoint_id for r in recs
                       if store.verify_integrity(r) is not None]
                if not recs:
                    s.fail("没有任何 COMMITTED checkpoint",
                           "checkpoint.enabled 是否为 true？")
                elif bad:
                    s.fail(f"完整性校验失败：{bad}", "artifact 快照或前驱链被破坏")
                else:
                    s.pass_(f"{len(recs)} 条全 COMMITTED 且校验通过：{stages}")
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            s.fail(f"{type(exc).__name__}: {exc}")
        steps.append(s)

        # ---- 6. 恢复点选择（不重跑 Agent）-----------------------------
        s = Step("resume point")
        try:
            from mao.checkpoints import ResumeManager, SQLiteCheckpointStore

            store = SQLiteCheckpointStore(Path(st.checkpoint.db_path),
                                          artifacts_root=Path(st.scheduler.attempts_root))
            try:
                # 用真实执行工作区去问，而不是传 None —— 传 None 只会得到
                # WORKSPACE_MISMATCH，那测的是"我没给路径"，不是恢复逻辑。
                row_ws = ""
                from mao.scheduler import TaskRepository

                _repo = TaskRepository(Path(st.scheduler.db_path),
                                       clock=__import__("mao.scheduler",
                                                        fromlist=["SystemClock"]
                                                        ).SystemClock())
                try:
                    _row = _repo.get(rt_id)
                    row_ws = (_row.execution_workspace_path or _row.workspace_path
                              or "") if _row else ""
                finally:
                    _repo.close()

                ev = ResumeManager(store, config=st.checkpoint).find_resume_point(
                    task_id=task_id, runtime_task_id=rt_id,
                    attempt=1, workspace_path=row_ws or None)
                kind = getattr(ev.failure_kind, "value", None)
                nxt = getattr(ev.resume_point, "next_stage", "") if ev.ok else ""
                # 真判据：任务已 COMPLETED，就不该再给出一个"还要继续跑"的恢复点；
                # 而选择器必须给出**可解释**的结论（ok 或明确 failure_kind），
                # 不能既 ok=False 又说不出原因。
                ws_alive = bool(row_ws) and Path(row_ws).is_dir()
                detail_extra = f" workspace={'alive' if ws_alive else 'gone'}"
                if ev.ok and nxt not in ("TERMINAL", "TERMINAL_BLOCKED"):
                    s.fail(f"终态任务却给出可继续的恢复点 next_stage={nxt}{detail_extra}",
                           "ResumeManager 把已完成任务判成待恢复 = resume 语义坏了")
                elif not ev.ok and not kind:
                    s.fail("选择器返回不可恢复却没给原因",
                           "失败必须可归因（NO_CHECKPOINT / WORKSPACE_MISMATCH / ...）")
                else:
                    s.pass_(f"ok={ev.ok} next={nxt or '-'} failure={kind or '-'}"
                           + detail_extra)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            s.fail(f"{type(exc).__name__}: {exc}",
                   "恢复点选择器异常：ResumeManager 是唯一选点者，不能猜")
        steps.append(s)

        # ---- 7. 用户能不能找到产物 -------------------------------------
        s = Step("result artifacts")
        attempt_dir = Path(st.scheduler.attempts_root) / rt_id / "attempt1"
        found = sorted(p.name for p in attempt_dir.rglob("*")
                       if p.is_file()) if attempt_dir.is_dir() else []
        want = ("state.json", "history.jsonl")
        missing = [n for n in want if not any(n in f for f in found)]
        if missing:
            s.fail(f"缺 {missing}（attempt 目录：{attempt_dir}）",
                   "用户按文档找不到任务历史")
        else:
            s.pass_(f"{attempt_dir}  含 {len(found)} 个产物文件")
        steps.append(s)

        # ---- 8. 语义缺失时的降级（不该让 Runtime 不可用）--------------
        # 语义档不可用时整个 Runtime 必须还能跑（§50）。这里真构造一次：
        # 用生产配置（memory + semantic 都开），但不设 MEMORY_* 环境变量，
        # 于是 embedding provider 必然不可用 —— 期望是"构造成功并降级"，
        # 而不是抛异常、也不是返回 None 把记忆整层悄悄关掉。
        s = Step("graceful degrade")
        try:
            from mao.core.config import load_config as _load
            from mao.memory import build_memory_layer

            saved = {k: __import__("os").environ.pop(k, None)
                     for k in ("MEMORY_EMBEDDING_MODEL_PATH",
                               "MEMORY_EMBEDDING_INTERPRETER",
                               "MEMORY_HF_HOME")}
            try:
                prod = _load("config")
                prod.settings.memory.path = str(tmp / "runtime" / "memory2.db")
                prod.settings.memory.semantic.index_dir = str(
                    tmp / "runtime" / "vector_index")
                layer = build_memory_layer(prod)
            finally:
                for k, v in saved.items():
                    if v is not None:
                        __import__("os").environ[k] = v
            if layer is None:
                s.fail("语义不可用时 build_memory_layer 返回 None",
                       "记忆层应在语义不可用时降级为词法，而不是整层消失")
            else:
                hits, used_mode = layer.retrieve_details(
                    role="executor", query="multiply bug", top_k=3)
                s.pass_(f"semantic 不可用时仍构造成功，实际检索模式={used_mode}，"
                        f"返回 {len(hits)} 条（空结果可接受，抛异常不可）")
        except Exception as exc:  # noqa: BLE001
            s.fail(f"{type(exc).__name__}: {exc}",
                   "语义档缺失不该让 Runtime 不可用：这是降级路径的硬要求")
        steps.append(s)

        return report(steps, tmp, args)
    finally:
        if args.keep:
            print(f"\n[keep] 临时目录：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def report(steps: list[Step], tmp: Path, args) -> int:
    width = max((len(s.name) for s in steps), default=10)
    print()
    print("=" * 74)
    print(" smoke test —— Multi-Agent Orchestrator（离线 Mock，零配额）")
    print("=" * 74)
    failed = 0
    for s in steps:
        mark = "PASS" if s.ok else "FAIL"
        print(f"  [{mark}] {s.name.ljust(width)}  {s.detail}")
        if not s.ok:
            failed += 1
            if s.action:
                print(f"         → {s.action}")
    print("-" * 74)
    if failed:
        print(f"{failed}/{len(steps)} 步失败 —— 这不是可发布的状态。")
        return 1
    print(f"{len(steps)}/{len(steps)} 步通过 —— 配置、DB 初始化、队列、调度、"
          "checkpoint、恢复点、产物可见性、降级路径全部工作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
