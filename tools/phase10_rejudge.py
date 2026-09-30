"""用已落盘的真实证据重跑判据（零 Agent 调用、零配额）。

原始 summary.json 是**那次跑自己写的记录**，本工具不改写它，只另存
`rejudge_report.json`：原始证据与复核结论同时在盘上，谁在什么时候说过什么
都能对得上。判据口径变更后需要重新评分时，用这个而不是再烧一次配额。

    python tools/phase10_rejudge.py [config_dir] [evidence_dir]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
import tools.phase10_checkpoint_demo as m

cfg = sys.argv[1] if len(sys.argv) > 1 else "config_p10"
ev_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
    "runtime_p10/demo_evidence")
saved = json.loads((ev_dir / "summary.json").read_text(encoding="utf-8"))
rt_id, task_id = saved["runtime_task_id"], saved["task_id"]
attempts_root = Path(m.load_profile(cfg).settings.scheduler.attempts_root)

evidence = m.collect(cfg, rt_id, attempts_root)
p1_trace = json.loads((ev_dir / "process1_trace.json")
                      .read_text(encoding="utf-8"))
p1_rc = int(p1_trace.get("rc", -1))
evidence["idempotency"] = m.audit_idempotency(cfg, task_id, rt_id)
problems = m.judge(evidence, None, None)
if p1_rc == 0:
    problems.append("§17 进程 1 rc=0")

b = evidence["process_boundary"]
runs = evidence["verification_runs"]
print("===== rejudge（只读已落盘证据）=====")
print("  config        :", cfg, " task:", rt_id, "/", task_id)
print("  process1 rc   :", p1_rc)
print("  boundary      :", b["crash_after_verification_commit"], b)
print("  attempt/epoch :", evidence["runtime_task"]["attempt"], "/",
      evidence["runtime_task"]["resume_epoch"])
print("  next_stage    :", evidence["runtime_task"]["recovery_decision_stage"])
print("  rounds        :", sorted({c["round"] for c in
                                   evidence["checkpoint_chain"]}))
print("  calls by role :", evidence["agent_calls"]["by_role"])
print("  calls by round:", evidence["agent_calls"]["by_role_round"])
print("  verify commits:", runs["verification_stage_commits"],
      " artifact-commands:",
      runs["commands_recorded_in_execution_artifact"])
print("  fingerprint   :", evidence["workspace_fingerprint"][
      "at_verification_checkpoint"][:16], "==",
      evidence["workspace_fingerprint"]["resume_time_recomputed"][:16])
print("  final status  :", evidence["runtime_task"]["status"])
print("  idempotency   : dup_lessons=",
      evidence["idempotency"].get("duplicate_lessons"),
      " dup_usage=", evidence["idempotency"].get("duplicate_usage_decisions"),
      " terminal_events=", evidence["idempotency"].get(
          "scheduler_terminal_events"))
report = {
    "rejudged_from_disk": True,
    "config_dir": cfg, "runtime_task_id": rt_id, "task_id": task_id,
    "process1_rc": p1_rc, "process_boundary": b,
    "attempt": evidence["runtime_task"]["attempt"],
    "resume_epoch": evidence["runtime_task"]["resume_epoch"],
    "next_stage": evidence["runtime_task"]["recovery_decision_stage"],
    "calls_by_role": evidence["agent_calls"]["by_role"],
    "calls_by_role_round": evidence["agent_calls"]["by_role_round"],
    "verification_runs": evidence["verification_runs"],
    "checkpoint_chain": evidence["checkpoint_chain"],
    "workspace_fingerprint": evidence["workspace_fingerprint"],
    "idempotency": evidence["idempotency"],
    "final_status": evidence["runtime_task"]["status"],
    "problems": problems,
    "verdict": "PASS" if not problems else "FAIL",
}
(ev_dir / "rejudge_report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print("  report        :", ev_dir / "rejudge_report.json")
if problems:
    print()
    print("FAIL:")
    for x in problems:
        print("  -", x)
    raise SystemExit(1)
print()
print("PASS：崩溃点、恢复身份、按轮归属的调用数、指纹、幂等与终态全部成立")
