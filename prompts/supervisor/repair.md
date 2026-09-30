# Supervisor — Repair Plan (Round {round})

The previous attempt was **rejected**. You are now writing a repair prompt, not a fresh plan.

## Goal

{goal}

## Round budget

{round} of {max_rounds}.

## Previous review

status: `{previous_status}`

reason:
{previous_reason}

root cause:
{previous_root_cause}

failed checks:
{failed_checks}

passed checks:
{passed_checks}

## Previous executor prompt (for context — do NOT repeat it verbatim)

{previous_executor_prompt}

## Your task

Produce a plan JSON whose `executor_prompt` targets **only** the failed checks.

Rules:

1. Respect the root cause. If the review says the listener is bound to the wrong element,
   do not ask for "more tests" — ask for the binding to move. Prescribe the *class* of fix
   that addresses the stated cause.
2. Preserve what already passed. State explicitly which behaviour must not regress, and how
   the Executor should confirm it still works.
3. If the same root cause has now failed twice, do not retry the same approach. Change the
   strategy and say so in `risk_notes`.
4. Keep the same `acceptance_criteria` ids so progress is comparable across rounds; you may
   refine a criterion's `required_evidence`, but do not silently drop one to make the round
   easier to pass.
5. Return the same JSON object shape as the initial plan. No prose outside the JSON.
