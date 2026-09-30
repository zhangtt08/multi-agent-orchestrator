# Supervisor — Plan Contract Repair

Your previous plan was **rejected by the plan validator**. This is not about
code — it is about the plan itself being unexecutable as written.

## Goal

{goal}

## Constraints

{constraints}

## Round budget

{round} of {max_rounds}.

## Validation errors (must all be fixed)

{validation_errors}

## Your previous plan

{previous_plan}

## Context

{context}

## Your task

Return the **same plan JSON shape**, corrected. Rules:

1. Fix **every** listed validation error. Do not fix only some of them.
2. Do not weaken the plan to make it pass. If a criterion was called out as
   empty or vague, make it concrete and checkable — do not delete it.
3. Verification commands must stay inside the workspace and must be on the
   framework's command allowlist (build/test/lint tools). If a command was
   rejected, replace it with an allowed equivalent rather than removing
   verification altogether.
4. Do not change the goal, and do not add new scope.
5. Return the JSON object only. No prose outside the JSON.
