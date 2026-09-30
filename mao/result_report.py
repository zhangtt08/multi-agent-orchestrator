"""RESULT.md —— 一次任务的可读交付说明（写在 attempt 的 artifacts/ 里）。

为什么要它：跑完之后用户要在磁盘上找到答案 —— 状态是什么、动了哪些文件、
框架自己验了什么、Reviewer 怎么判的、补丁在哪、怎么落到我的项目里。
这些信息本来就在产物里，但要读四个 JSON 才能拼出来。

边界（这是它存在的同时必须承认的限制）：

* **不自动合并、不自动 apply。** 文件里只*说明*怎么应用，程序一次也不会替你做。
* 明确区分"框架采集"与"Agent 自述"：`changed_files` 有两处来源，
  框架那份（git status / evidence）才是判据，Agent 报的那份只是它的说法。
* 本模块**只读已有产物 + 写一个 md**。任何异常都被吞掉并返回 None ——
  一份说明书不该改变任务结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_TEMPLATE_NOTE = (
    "本文件由框架在任务结束时根据**已落盘产物**生成；它不改变任务结果，"
    "也不代表任何自动合并已经发生。"
)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _latest_task_dir(attempt_dir: Path) -> Optional[Path]:
    """attempt 目录下只有一个 task_<id>/ 子目录；找不到就返回 None。"""
    candidates = sorted(p for p in attempt_dir.glob("task_*") if p.is_dir())
    return candidates[-1] if candidates else None


def _bullets(items: List[Any], limit: int = 20) -> str:
    if not items:
        return "  （无）\n"
    lines = [f"  - {str(i)[:160]}" for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"  - …另有 {len(items) - limit} 项")
    return "\n".join(lines) + "\n"


def _check_lines(checks: List[Any]) -> str:
    """验收项是结构化对象 —— 直接 str() 会糊成一行字典，读不出结论。"""
    rows = []
    for item in checks:
        if isinstance(item, dict):
            flag = "✓" if item.get("satisfied") else "✗"
            rows.append(f"  {flag} {item.get('criterion_id', item.get('id', '?'))}: "
                        f"{str(item.get('description') or '')[:90]}"
                        f" — {str(item.get('detail') or '')[:120]}")
        else:
            rows.append(f"  - {str(item)[:160]}")
    return "\n".join(rows[:20]) + ("\n  （另有若干项）" if len(rows) > 20 else "")


def render(task: Dict[str, Any], state: Dict[str, Any],
           review: Dict[str, Any], execution: Dict[str, Any],
           workspace_result: Dict[str, Any], *, outcome: str,
           error: str, patch_path: str, evidence_dir: Path) -> str:
    """把已落盘的信息排成一页 RESULT.md 文本。"""
    framework_files = list(workspace_result.get("changed_files") or [])
    claimed_files = list((execution or {}).get("changed_files") or [])
    evidence = (execution or {}).get("evidence") or {}
    verification = {
        "test_result": evidence.get("test_result") or "（框架未采集到测试结果）",
        "build_result": evidence.get("build_result") or "",
        "git_diff_stat": evidence.get("git_diff_stat") or {},
    }
    rel = []
    for name in ("plan.json", "execution.json", "review.json",
                 "state.json", "history.jsonl"):
        if (evidence_dir / name).exists():
            rel.append(name)

    lines: List[str] = ["# RESULT", ""]
    lines += [f"- Task           : {task.get('task_id', '')} / "
              f"{state.get('task_id', '')}",
              f"- Goal           : {str(task.get('goal', ''))[:300]}",
              f"- Runtime status : **{outcome or state.get('current_state', '')}**",
              f"- Rounds         : {state.get('current_round', '?')} / "
              f"max_rounds={state.get('max_rounds', '?')}",
              ""]
    if error:
        lines += [f"- Last error     : {str(error)[:400]}", ""]

    attempts = state.get("attempts") or []
    if isinstance(attempts, list) and attempts:
        lines += [f"## 每一轮做了什么（共 {len(attempts)} 次）", ""]
        for row in attempts:
            if not isinstance(row, dict):
                lines.append(f"  - {str(row)[:160]}")
                continue
            lines.append(
                f"  - round {row.get('round', '?')}: 执行={row.get('execution_status', '?')} "
                f"复审={row.get('review_status', '?')} — "
                f"{str(row.get('review_reason') or row.get('summary') or '')[:160]}")
        lines.append("")

    lines += ["## 工作区",
              f"- 策略           : {workspace_result.get('workspace_strategy', '')}",
              f"- 执行工作区     : {workspace_result.get('execution_workspace_path', '')}",
              f"- base revision  : {workspace_result.get('base_revision', '') or '（无）'}",
              "",
              "## 改动的文件",
              _bullets(framework_files) if framework_files else "  （框架未记录到改动）\n",
              f"  框架那份（git 采集）是判据；Agent 自述那份在下面，二者不必一致。"]
    if claimed_files and claimed_files != framework_files:
        lines += ["", "  Agent 自述的改动文件：", _bullets(claimed_files)]

    lines += ["", "## 框架验证（机械采集，不是模型判断）",
              f"- 测试结果       : {str(verification['test_result'])[:500]}", ""]
    if verification["build_result"]:
        lines += [f"- 构建结果       : {str(verification['build_result'])[:300]}", ""]
    diff_stat = verification["git_diff_stat"]
    if diff_stat:
        lines += [f"- diff 规模       : "
                  + ", ".join(f"{k}={v}" for k, v in list(diff_stat.items())[:8]), ""]

    if review:
        lines += ["## Reviewer 判定",
                  f"- status         : {review.get('status', '')}",
                  f"- reason         : {str(review.get('reason', ''))[:600]}",
                  f"- round          : {review.get('round', '')}", ""]
        passed = list(review.get("passed_checks") or [])
        failed = list(review.get("failed_checks") or [])
        if passed:
            lines += ["通过项：", _check_lines(passed), ""]
        if failed:
            lines += ["未通过项：", _check_lines(failed), ""]

    patch_note = (patch_path if patch_path
                  else "（本次没有 changes.patch —— GIT_WORKTREE 策略才会产出）")
    lines += ["", "## 交付物",
              f"- 补丁           : {patch_note}",
              f"- 证据目录       : {evidence_dir}",
              f"- 其它产物       : {', '.join(rel) or '（无）'}",
              "",
              "## 怎么应用（程序不会替你执行）", ""]
    if patch_path and Path(patch_path).exists():
        lines += ["源项目路径用 `python main.py queue show <rt-id>` 里的 workspace 一行确认，"
                  "然后：**先看再决定**",
                  "",
                  "```bash",
                  f"git -C <你的项目目录> apply --stat \"{patch_path}\"",
                  f"git -C <你的项目目录> apply \"{patch_path}\"",
                  "```",
                  "",
                  "或者直接去那份执行工作区里看改完的文件，自己挑。"]
    else:
        lines += ["本次没有 `changes.patch`。执行工作区目录里就是改完的文件，"
                  "自己去取；需要补丁形态请用 `GIT_WORKTREE` 策略重新提交。"]
    lines += ["", "---", f"_{_TEMPLATE_NOTE}_", ""]
    return "\n".join(lines)


def write_result_md(runtime_attempt_dir: Any, *, outcome: str = "",
                    error: str = "") -> Optional[Path]:
    """在 `<attempt>/artifacts/RESULT.md` 生成说明书。失败返回 None，不抛。"""
    try:
        attempt = Path(runtime_attempt_dir)
        task_dir = _latest_task_dir(attempt)
        if task_dir is None:
            return None
        artifacts = attempt / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)

        task = _load_json(task_dir / "task.json") or {}
        state = _load_json(task_dir / "state.json") or {}
        review = _load_json(task_dir / "review.json") or {}
        execution = _load_json(task_dir / "execution.json") or {}
        workspace_result = _load_json(artifacts / "workspace_result.json") or {}
        patch = artifacts / "changes.patch"
        content = render(task, state, review, execution, workspace_result,
                         outcome=outcome, error=error,
                         patch_path=str(patch) if patch.exists() else "",
                         evidence_dir=task_dir)
        out = artifacts / "RESULT.md"
        out.write_text(content, encoding="utf-8")
        return out
    except Exception:  # noqa: BLE001 - 说明书失败不能改变任务结局
        return None
