# Reviewer — Review Round {round}

## Goal

{goal}

## Acceptance criteria

{acceptance_criteria}

## Executor report (self-reported — verify, do not trust)

summary:
{execution_summary}

status: `{execution_status}`

changed files:
{changed_files}

commands run:
{commands_run}

tests reported:
{tests}

remaining issues the Executor itself admitted:
{remaining_issues}

## Evidence supplied

build: {evidence_build}
tests: {evidence_tests}
lint: {evidence_lint}
browser: {evidence_browser}

git diff (may be truncated):
{git_diff}

## Framework verification outputs (authoritative — the orchestrator ran these commands)

Per-command real output captured by the orchestrator. This is your primary
evidence for per-item test outcomes (which tests ran, passed, failed, xfailed):

{verification_outputs}

## Source snapshots (collected by the orchestrator, truncated per file)

Changed files first, then the workspace source files the acceptance criteria
refer to (implementation/tests). Use them to verify semantics directly instead
of trusting the Executor's prose:

{source_snapshots}

## Your task

1. Walk every acceptance criterion. For each, decide satisfied / not satisfied and cite the
   evidence you used. Cite the per-command outputs and source snapshots above — they are
   framework-collected and authoritative; the Executor's prose is not.
2. Where the Executor claims success without evidence, mark the criterion as **not
   satisfied** and say what evidence was missing. Note: if a snapshot or output is
   genuinely missing from the sections above, that is evidence the orchestrator failed to
   supply — but check carefully before claiming so; truncation markers mean the content
   was captured and clipped, not lost.
3. If you are failing the round, write `root_cause` as a mechanism ("handler is bound to the
   wrong element"), not a restatement of the symptom ("ESC is broken"). Set `next_prompt` to
   a concrete instruction for the next attempt.
4. `next_prompt` must describe **code changes only** — never ask the Executor to run
   commands or produce test output. The Executor has no shell access by design; the
   orchestrator runs the verification suite and supplies the results. The `pytest` outcome
   above **is** the `test_result` evidence.
5. Return the review JSON only.
