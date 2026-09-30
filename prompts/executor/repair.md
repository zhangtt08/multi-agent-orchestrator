# Executor — Repair (Round {round})

Your previous attempt was **rejected**. This is a targeted repair, not a second attempt at
the whole task.

## Task

task_id: `{task_id}`
round: {round} of {max_rounds}

## What the Reviewer found

reason:
{previous_reason}

root cause:
{previous_root_cause}

failed checks:
{failed_checks}

## Required fix

{next_prompt}

## What already passed — do not regress it

{passed_checks}

## Rules for this round

1. Change only what the required fix describes. Broad rewrites make the diff unreviewable
   and tend to reintroduce fixed problems.
2. Re-run the tests that cover both the failed checks and the passing ones, and report both
   results.
3. If you conclude the Reviewer's stated root cause is wrong, say so explicitly in
   `remaining_issues` with your reasoning. Do not quietly do something else.
4. Return the execution JSON only.
