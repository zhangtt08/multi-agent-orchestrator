# Supervisor — Initial Plan

## Round

{round}

## Goal

{goal}

## Constraints

{constraints}

## Workspace

{workspace_path}

{workspace_summary}

You are read-only: you may open and read any of these files, but you must not
change anything. Use this listing to decide what to inspect; do not assume the
bug's location without looking.

## Executor capabilities (plan within these — you are not told which product it is)

{executor_capabilities}

Plan only for what the Executor can actually do. If `supports_file_write` is
`no`, do not plan file edits. If shell access is limited, do not plan for the
Executor to run arbitrary commands — the orchestrator runs the verification
commands itself.

## Context

{context}

## Round budget

You have **{max_rounds}** rounds total (this is round {round}). If the work is repetitive
enough that a second attempt would be identical, it will be rejected as a retry rather than
a repair. Prefer a plan that can be verified early over a plan that is impressive on paper.

## Your task

Produce the plan JSON described in your system prompt.

Requirements for this specific call:

1. Inspect the workspace first. Identify the actual defect before prescribing a fix.
   Do not guess from the goal text alone.
2. Derive 3–5 acceptance criteria that are independently checkable, and for each one name
   the evidence type that would prove it (`test_result`, `git_diff`, `browser_test`,
   `build_result`, `lint_result`, `changed_files`). Criteria must be concrete
   ("multiply(3, 4) == 12"), never vague ("code works correctly").
3. Write `executor_prompt` as a self-contained brief: the goal, the numbered steps, the
   relevant context, what to run, and the JSON contract the Executor must return. Do not
   reference this conversation.
4. Declare `verification_commands` the orchestrator should run to verify the result.
   They must be allowed build/test/lint commands operating inside the workspace
   (for example running the project's test suite). The orchestrator will reject
   anything outside its policy.
5. List risks that specifically concern *verification* — where could a superficially
   passing result still be wrong? Those risks become the Reviewer's focus.
