#!/usr/bin/env python
"""fake_cli_agent.py —— 一个"长得像真实 CLI Agent"的独立程序。

为什么必须是真的独立进程
------------------------
第一阶段的 Mock 是**进程内**的 Python 对象，它绕过了这条链路：

    Orchestrator -> Adapter -> Transport -> 外部进程 -> stdout -> JSON -> 框架

而这条链路恰恰是第二阶段最需要验证的东西。进程内 Mock 永远测不出：
  - argv 到底拼对了没有
  - stdin 到底喂进去了没有
  - 临时 Prompt 文件到底写没写、内容对不对
  - stdout 里的日志噪声会不会干扰 JSON 解析
  - 非零退出码 / 超时 / 命令不存在会怎样

所以这个脚本**通过真实 subprocess 被调用**，用真实 stdout 说话。

它模拟的角色行为（§19）
-----------------------
  Executor: round 1 留下一个故意的验收错误 -> round 2 修一半 -> round 3 修好
  Reviewer: FAIL -> FAIL -> PASS
  Supervisor: 直接给出 Plan（含 verification_commands）

三种 Prompt 投喂方式都支持（§18）
---------------------------------
  stdin    : 默认，从标准输入读
  argument : --prompt "..."
  file     : --prompt-file path/to/prompt.md

行为开关通过环境变量控制（便于测试矩阵）
---------------------------------------
  FAKE_AGENT_MODE      正常 / noisy / garbage / fenced / badjson / sleep / fail
  FAKE_AGENT_ROLE      强制角色（默认从 Prompt 里推断）
  FAKE_AGENT_ROUND     强制轮次（默认从 Prompt 里推断）
  FAKE_AGENT_EXIT      强制退出码
  FAKE_AGENT_DELAY     人为延迟（秒），用于超时测试
  FAKE_AGENT_OUTPUT_FILE  把 JSON 也写一份到文件（测 Mode D）
  FAKE_AGENT_SESSION_ID   回显的会话 id

刻意不做的事
------------
不 import 框架的任何代码。它必须像一个"外部程序"那样独立存在 ——
如果它 import 了 mao，那这个测试就退化成进程内调用了，白测。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ACCEPTANCE_ERROR = "escape key handler is registered on the wrong element"
SECOND_ERROR = "route rollback runs after the modal unmounts"

# 角色识别模式表。
#
# 为什么不用"出现某个关键词就算这个角色"这种朴素做法？
# 因为真实 Prompt 是交叉引用的：Executor 的 Prompt 里会写着
# "Brief from the Supervisor"。用关键词扫描会把 Executor 的 Prompt
# 判成 Supervisor —— 这个 bug 在开发过程中真实发生过，而且只有把
# 假 Agent 放到独立进程里跑才会暴露（进程内 Mock 根本不经这条路径）。
#
# 所以这里匹配的是**结构化标记**，按特异性从高到低排列：
#   role: executor            框架注入的显式角色字段
#   # Executor — ...          渲染模板的标题行
#   You are the executor.     显式身份句
# 并且要求"Executor 命中 且 Supervisor 未命中"才算强证据。
ROLE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("executor", (
        r"role[\"']?\s*[:=]\s*[\"']?executor",
        r"^#\s*executor\b",
        r"you are (?:the |an? )?executor",
        r"executor[\"']?\s*[:=]",
    )),
    ("reviewer", (
        r"role[\"']?\s*[:=]\s*[\"']?reviewer",
        r"^#\s*reviewer\b",
        r"you are (?:the |an? )?reviewer",
        r"reviewer[\"']?\s*[:=]",
    )),
    ("supervisor", (
        r"role[\"']?\s*[:=]\s*[\"']?supervisor",
        r"^#\s*supervisor\b",
        r"you are (?:the |an? )?supervisor",
        r"supervisor[\"']?\s*[:=]",
    )),
)


# ---------------------------------------------------------------------------
# Prompt 读取：三种模式
# ---------------------------------------------------------------------------
def read_prompt(args: argparse.Namespace) -> str:
    """按调用方式取得 Prompt 文本。顺序即优先级。"""
    if args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            print(f"[fake-agent] cannot read prompt file: {exc}", file=sys.stderr)
            raise SystemExit(4)

    if args.prompt is not None:
        return args.prompt

    # 默认：stdin
    try:
        return sys.stdin.read()
    except Exception:  # noqa: BLE001
        return ""


def detect_role(prompt: str, override: str | None) -> str:
    """从 Prompt 中识别本次调用期望的角色。

    先看显式覆盖，再按 ROLE_MARKERS 的结构化模式逐角色打分，
    最后按"证据最具体者胜出"决定。识别不出时保守地当作 executor ——
    因为在无法判断时，执行角色的产出契约最宽松。

    `--role` 这个显式参数永远优先。框架侧若担心识别歧义，
    最稳的做法就是在 Profile 的 extra_args 里固定传 --role。
    """
    if override:
        return override.lower()
    if not prompt:
        return "executor"

    scores: dict[str, int] = {}
    for role, patterns in ROLE_MARKERS:
        hits = 0
        for pattern in patterns:
            if re.search(pattern, prompt, re.IGNORECASE | re.MULTILINE):
                hits += 1
        if hits:
            scores[role] = hits

    if not scores:
        return "executor"

    best = max(scores.values())
    winners = [role for role, score in scores.items() if score == best]

    if len(winners) == 1:
        return winners[0]

    # 平票时的兜底：按"标题行说了算什么"裁决。
    # 渲染模板的第一行标题是最可靠的信号。
    head = prompt.lstrip().splitlines()[:4]
    head_text = "\n".join(head)
    for role, _patterns in ROLE_MARKERS:
        if re.search(rf"^#\s*{role}\b", head_text, re.IGNORECASE | re.MULTILINE):
            return role

    # 仍然平票：选"标题里最先出现"的那个
    positions = {}
    for role in winners:
        match = re.search(rf"\b{role}\b", head_text, re.IGNORECASE)
        positions[role] = match.start() if match else 10**6
    return min(positions, key=positions.get)


def detect_round(prompt: str, override: str | None) -> int:
    """从 Prompt 里抓轮次。

    支持框架实际渲染出的多种说法：
        "round: 1" / "current round: 2"   (键值行)
        "## Round\\n\\n1"                  (小节标题 + 独立数值行)
        "Round 2" / "ROUND 2"             (banner)
        "preparing round 3"
        "轮次: 2" / "第 2 轮"
    抓不到就默认 1。

    刻意允许 0：Supervisor 的初始规划就是 round 0，
    强行夹到 1 会让"第几轮"这件事在日志里失真。
    """
    if override is not None:
        try:
            return max(0, int(override))
        except ValueError:
            pass
    if not prompt:
        return 1

    patterns = (
        # "## Round" 换行后跟一个纯数字行 —— 渲染模板的常见形态
        r"^#+\s*round\s*$\s*^\s*(\d+)\s*$",
        r"current[_\s]*round[\"']?\s*[:=]\s*(\d+)",
        r"\bround\b[\s:=\-]*(\d+)",
        r"第\s*(\d+)\s*轮",
        r"轮次[\"']?\s*[:=]\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE | re.MULTILINE)
        if match:
            return max(0, int(match.group(1)))
    return 1


# ---------------------------------------------------------------------------
# 各角色的响应构造
# ---------------------------------------------------------------------------
def project_plan_response(prompt: str) -> dict:
    """项目档形状 —— 面板那条路（一句话 → 切分）要的是这个，不是单条任务的 Plan。

    为什么要分两支：`batch_project.plan` 走的是同一个 supervisor 角色，但它问的是
    "把这句拆成 2-5 格"，回答要过 `check_spec` 那套机械判据（goal 够长、acceptance
    是一条命令、final_acceptance 是 argv）。给一个任务 Plan 会直接被拒，
    于是"零配额演一遍面板那条路"根本起不来。
    """
    return {
        "name": "esc-flow",
        "strategy": "GIT_WORKTREE",
        "max_rounds": 3,
        "constraints": ["不得修改 tests"],
        "final_acceptance": {"name": "selfcheck",
                             "command": [sys.executable, "-c",
                                         "print('final acceptance ok')"]},
        "milestones": [
            {"id": "m1",
             "goal": "补上 useEscapeKey 与 closeFlow，让 ESC 关闭时路由一起收",
             "acceptance": "pytest -q"},
            {"id": "m2",
             "goal": "把关闭流程写进 README，说明 ESC 与点击遮罩的差别",
             "acceptance": "pytest -q"}],
    }


def supervisor_response(prompt: str) -> dict:
    """Supervisor：给方案 + 给框架代跑的验收命令。"""
    return {
        "task_id": _task_id(prompt),
        "goal": _goal(prompt),
        "executor_prompt": (
            "Fix the ESC-to-close navigation flow. Attach the keydown handler at "
            "the document level, and make sure history/route rollback completes "
            "before the modal unmount resolves."
        ),
        "tasks": [
            {"title": "Move ESC handler to document level",
             "detail": "attach on mount, detach on unmount", "requires": []},
            {"title": "Sequence route rollback before unmount",
             "detail": "await the rollback promise", "requires": ["task_1"]},
        ],
        "constraints": ["do not change the public props of the modal"],
        "acceptance_criteria": [
            {"criterion_id": "ac_1", "description": "ESC closes the modal",
             "required_evidence": ["test_result"]},
            {"criterion_id": "ac_2",
             "description": "route rollback completes before unmount",
             "required_evidence": ["test_result"]},
        ],
        "verification_commands": [
            {"name": "selfcheck", "command": [sys.executable, "-c", "print('ok')"],
             "required": True},
        ],
        "risk_notes": ["focus management regressions"],
        "round": detect_round(prompt, None),
    }


def executor_response(prompt: str, round_no: int, include_error: bool) -> dict:
    """Executor：三轮渐进修复。

    round 1 -> 故意留下验收错误（remaining_issues 非空）
    round 2 -> 修一半（还有一个问题）
    round 3 -> 完成
    """
    if round_no <= 1:
        changed = ["src/components/Modal.tsx"]
        remaining = [ACCEPTANCE_ERROR]
        summary = "moved ESC handling but the handler is still scoped to the modal element"
        evidence_diff = (
            "--- a/src/components/Modal.tsx\n"
            "+++ b/src/components/Modal.tsx\n"
            "@@\n-  useEffect(() => window.addEventListener('keydown', onEsc), [])\n"
            "+  useEffect(() => modalRef.current?.addEventListener('keydown', onEsc), [])\n"
        )
    elif round_no == 2:
        changed = ["src/components/Modal.tsx", "src/hooks/useEscapeKey.ts"]
        remaining = [SECOND_ERROR]
        summary = "handler moved to document level, but route rollback still races unmount"
        evidence_diff = (
            "--- a/src/hooks/useEscapeKey.ts\n"
            "+++ b/src/hooks/useEscapeKey.ts\n"
            "@@\n+document.addEventListener('keydown', onEsc)\n"
            "-modalRef.current?.addEventListener('keydown', onEsc)\n"
        )
    else:
        changed = [
            "src/components/Modal.tsx",
            "src/hooks/useEscapeKey.ts",
            "src/navigation/closeFlow.ts",
        ]
        remaining = []
        summary = "ESC handling and route rollback now complete in the correct order"
        evidence_diff = (
            "--- a/src/navigation/closeFlow.ts\n"
            "+++ b/src/navigation/closeFlow.ts\n"
            "@@\n"
            "+await rollbackRoute()\n"
            "+closeModal()\n"
        )

    payload = {
        "task_id": _task_id(prompt),
        "round": round_no,
        "status": "success" if not remaining else "failed",
        "summary": summary,
        "changed_files": changed,
        "commands_run": [
            {"command": "npm run test:navigation", "exit_code": 1 if remaining else 0,
             "output_excerpt": "28 passed" if not remaining else "2 failed"},
        ],
        "tests": ["npm run test:navigation: 28 passed" if not remaining
                  else "npm run test:navigation: 2 failed"],
        "errors": [] if not remaining else [remaining[0]],
        "artifacts": [],
        "remaining_issues": remaining,
        "evidence": {
            "build_result": "webpack: compiled successfully",
            "test_result": "28 passed" if not remaining else "2 failed",
            "lint_result": "eslint: 0 problems",
            "git_diff": evidence_diff,
            "git_diff_stat": {"files": len(changed), "added": 12, "deleted": 3},
            "changed_files": changed,
            "artifacts": [],
        },
    }
    # 用于测"Executor 自吹但框架证据打脸"的场景
    if include_error and round_no > 1:
        payload["evidence"]["build_result"] = "build passed"
    return payload


def reviewer_response(prompt: str, round_no: int, force_pass: bool) -> dict:
    """Reviewer：FAIL -> FAIL -> PASS（默认三轮节奏）。"""
    if force_pass or round_no >= 3:
        return {
            "task_id": _task_id(prompt),
            "round": round_no,
            "status": "pass",
            "passed_checks": [
                {"criterion_id": "ac_1", "description": "ESC closes the modal",
                 "satisfied": True, "detail": "verified via framework evidence"},
                {"criterion_id": "ac_2",
                 "description": "route rollback completes before unmount",
                 "satisfied": True, "detail": "diff shows await before close"},
            ],
            "failed_checks": [],
            "reason": "all acceptance criteria satisfied",
            "root_cause": None,
            "next_prompt": None,
            "evidence": {"test_result": "28 passed",
                         "build_result": "compiled successfully"},
            "reviewer": "fake-cli",
        }

    if round_no <= 1:
        failed = {"criterion_id": "ac_1", "description": "ESC closes the modal",
                  "satisfied": False,
                  "detail": "handler is still attached to the modal element"}
        reason = "ESC does not close the modal because the handler never fires"
        root = ACCEPTANCE_ERROR
    else:
        failed = {"criterion_id": "ac_2",
                  "description": "route rollback completes before unmount",
                  "satisfied": False,
                  "detail": "rollback still races the unmount"}
        reason = "ESC closes the modal but the route is left half-updated"
        root = SECOND_ERROR

    return {
        "task_id": _task_id(prompt),
        "round": round_no,
        "status": "fail",
        "passed_checks": [],
        "failed_checks": [failed],
        "reason": reason,
        "root_cause": root,
        "next_prompt": (
            f"Address the failed criterion: {failed['description']}. "
            "Re-run the navigation tests and report per-criterion results."
        ),
        "evidence": {"test_result": "2 failed"},
        "reviewer": "fake-cli",
    }


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _task_id(prompt: str) -> str:
    match = re.search(r"task[_-]?id[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]+)", prompt or "",
                      re.IGNORECASE)
    return match.group(1) if match else "task_fake"


def _goal(prompt: str) -> str:
    match = re.search(r"(?:goal|目标)[\"']?\s*[:=]\s*[\"']?([^\n\"']+)", prompt or "",
                      re.IGNORECASE)
    return match.group(1).strip() if match else "fake goal"


# ---------------------------------------------------------------------------
# 输出形态：模拟真实 Harness 的各种"脏输出"
# ---------------------------------------------------------------------------
def emit(payload: dict, mode: str) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2)

    if mode == "clean":
        print(body)
        return

    if mode == "fenced":
        print("[fake-agent] analysing repository ...")
        print("```json")
        print(body)
        print("```")
        print("[fake-agent] done")
        return

    if mode == "noisy":
        print("[fake-agent] booting")
        print("[fake-agent] reading 14 files")
        print("[fake-agent] thinking ...")
        print(body)
        print("[fake-agent] completed in 1.2s")
        return

    if mode == "garbage":
        print("[fake-agent] I could not produce a structured result.")
        print("Everything looks fine to me, trust me.")
        return

    if mode == "badjson":
        # 几乎合法：只有尾随逗号
        broken = body.replace('"reviewer": "fake-cli"', '"reviewer": "fake-cli",')
        broken = broken[: broken.rfind("}")] + ",}"
        print("[fake-agent] here is the result:")
        print(broken)
        return

    if mode == "empty":
        return

    print(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_cli_agent",
        description="A stand-in CLI agent used to exercise the real subprocess path.",
    )
    parser.add_argument("--prompt", default=None,
                        help="prompt passed as an argument (argument mode)")
    parser.add_argument("--prompt-file", dest="prompt_file", default=None,
                        help="path to a prompt file (file mode)")
    parser.add_argument("--session-id", dest="session_id", default=None)
    parser.add_argument("--role", default=None,
                        help="force the role instead of inferring it from the prompt")
    parser.add_argument("--round", dest="round_no", default=None,
                        help="force the round number instead of inferring it")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print("fake-cli-agent 2.0.0")
        return 0

    env = os.environ

    # 人为延迟：用于超时测试
    delay = _to_float(env.get("FAKE_AGENT_DELAY"))
    if delay:
        time.sleep(delay)

    prompt = read_prompt(args)
    role = detect_role(prompt, args.role or env.get("FAKE_AGENT_ROLE"))
    round_no = detect_round(prompt, args.round_no or env.get("FAKE_AGENT_ROUND"))
    mode = (env.get("FAKE_AGENT_MODE") or "clean").lower()
    force_pass = _truthy(env.get("FAKE_AGENT_FORCE_PASS"))
    include_error = _truthy(env.get("FAKE_AGENT_EXECUTOR_SELF_REPORT"))

    if role == "supervisor":
        payload = (project_plan_response(prompt) if '"milestones"' in prompt
                   else supervisor_response(prompt))
    elif role == "reviewer":
        payload = reviewer_response(prompt, round_no, force_pass)
    else:
        payload = executor_response(prompt, round_no, include_error)

    # session id 回显：让框架侧能验证会话透传
    session = env.get("FAKE_AGENT_SESSION_ID") or args.session_id
    if session:
        payload["session_id"] = session

    # Mode D：把结果同时写到 Profile 指定的文件
    out_file = env.get("FAKE_AGENT_OUTPUT_FILE")
    if out_file:
        try:
            with open(out_file, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except OSError as exc:
            print(f"[fake-agent] cannot write output file: {exc}", file=sys.stderr)

    # 结构化：日志走 stderr，结果走 stdout —— 这也是真实 Harness 的常见形态
    print(f"[fake-agent] role={role} round={round_no} mode={mode}", file=sys.stderr)

    emit(payload, mode)

    exit_code = env.get("FAKE_AGENT_EXIT")
    if exit_code is not None:
        try:
            return int(exit_code)
        except ValueError:
            pass
    return 0


def _to_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
