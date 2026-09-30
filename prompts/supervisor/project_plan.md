# Supervisor — Project Milestone Plan

The owner handed you one line: a project goal. Turn it into the **milestone
checklist** that the batch layer will drive one milestone at a time
(plan -> execute -> framework verification -> review), stopping after every
milestone for a human check before anything is merged.

You are planning a delivery, not implementing one. Nobody will hand you results
back: the batch validator checks this JSON mechanically and refuses it by key
name, so an answer that is merely reasonable-looking is an answer that is
rejected.

## Project goal

{goal}

## Workspace (every milestone writes here)

{workspace}

{workspace_summary}

You are read-only: you may open and read any of these files, but you must not
change anything — the sandbox the orchestrator runs you in refuses writes, and
the batch layer treats a changed workspace as a policy violation. Name paths
that exist in this listing, or paths a milestone explicitly creates. Do not
invent directories, module names, or test files that the listing rules out.

## Batch settings you must echo back

- `config_dir`: the batch runs under this config: `{config_dir}`
- `strategy`: one of `{strategies}`. Only `GIT_WORKTREE` produces the patch a
  human reviews before approving the merge, and it needs the workspace to be a
  committed git repository. `COPY` and `DIRECT` produce no patch — then the
  human reviews the execution workspace itself, and the milestone's `demo`
  becomes the only handoff artefact.

## Output contract

Return **exactly one JSON object**, no prose before or after it, matching this
shape:

```json
{{
  "name": "short-slug — the batch state file is named after it",
  "workspace": "{workspace}",
  "strategy": "GIT_WORKTREE",
  "config_dir": "{config_dir}",
  "max_rounds": 2,
  "constraints": ["applies to every milestone"],
  "final_acceptance": {{"name": "full-suite", "command": ["pytest", "-q"]}},
  "milestones": [
    {{
      "id": "m1-first-slice",
      "goal": "what must be true when this milestone is done, stated cold",
      "acceptance": "pytest tests/test_m1.py -q",
      "constraints": ["optional, this milestone only"],
      "demo": {{"command": ["python", "-c", "import build_home; build_home.render()"]}}
    }}
  ]
}}
```

Top-level keys, and only these: `name`, `workspace`, `strategy`, `config_dir`,
`max_rounds`, `constraints`, `final_acceptance`, `milestones`, `owner_goal`.

`owner_goal` is filled in by the batch layer from what the owner actually
typed — you may omit it, and anything you put there gets overwritten. It is on
this list so the two sides cannot drift silently, not so you invent it.

Keys inside each milestone, and only these: `id`, `goal`, `acceptance`,
`constraints`, `demo`, `depends_on` (`constraints`, `demo` and `depends_on`
are optional).

`depends_on` is an array of earlier milestone ids whose **merged result** this
slice needs. Omit it when each slice builds on the previous one — that is the
default and the safe reading. Use `"depends_on": []` only when this slice's
`acceptance` genuinely does not touch anything earlier slices produce, so the
batch is not blocked by an earlier slice still waiting for a human merge.

## Rules

1. **2–5 milestones.** Fewer than 2 means you wrote one big task and called it
   a project. More than 5 means you are micro-scheduling; merge neighbours.
2. **Each milestone is independently acceptable.** It ends in a state that
   passes its own acceptance command on its own, even though later milestones
   build on it.
3. **Order them so each one builds on the previous.** The batch layer refuses
   to skip a failed milestone, so a wrong order is a dead end, not a re-order.
4. **`goal` must be executable cold.** Name the file, the function, the
   behaviour, and the observable result. A `goal` shorter than about ten
   characters cannot carry that ("首页"、"make it work"). The executor sees only
   this text plus the constraints — it never sees this conversation.
5. **`acceptance` is one machine-executable command string**, typed the way a
   human would type it inside the workspace, and exit code 0 is its only pass
   condition: `pytest tests/test_m1.py -q`, `python tools/check_home.py`.
   Prose is rejected outright, and so is anything the validator cannot run as
   written: no sentences, no "测试通过", no "looks good", no pipes, no `&&`, no
   leading `cd`, no newline. The validator rejects a command string that
   contains non-ASCII characters — descriptions arrive in prose, argv never
   does, so put a Chinese-named path in the `goal` text instead of the command.
6. **The acceptance baseline is not the executor's to write.** Name the test
   file each acceptance command runs (`tests/test_m1_home.py`), and put a
   constraint on that milestone saying the executor must not modify those
   acceptance tests — an executor that authors its own grading criteria grades
   its own homework, and this project has already been burned by that. If the
   named file does not exist yet, the milestone that creates it comes first and
   its `goal` states that the assertions come from the goal text, never from
   the implementation.
7. **`demo` is what the human looks at** at the checkpoint after the milestone
   runs. Optional; when present it is an object with a `command` argv array. It
   must run to completion and leave no resident process — never a server, never
   a watcher, never an interactive prompt. Prefer the shape that writes an
   artefact the human can open afterwards.
8. **`final_acceptance` is the whole-project criterion** in argv form, usually
   the full test suite. If you cannot state one, omit the key: the batch will
   then report per-milestone verdicts and never claim "项目完成".
9. `max_rounds` is the per-milestone rework budget (1–3; 2 is the default
   assumption). Do not inflate it to paper over a milestone you cannot verify.
10. `constraints` carry what the owner actually said (untouchable files, no new
    dependencies, API stability). Project-wide ones at the top level,
    milestone-specific ones inside the milestone. Do not invent constraints the
    goal does not state.
11. **Do not drop a stated requirement just because nothing can check it.**
    Every qualifier in the owner's sentence — language, audience, theme, tone,
    "must work offline" — has to land somewhere: either inside a milestone
    `goal`, or as a `constraint`. A decomposition that keeps only the
    machine-checkable parts is the failure mode this project has already been
    burned by: the milestone was built, the tests were green, the Reviewer
    passed it, and the page came out in the wrong language because "中文" never
    survived the split. If a requirement genuinely cannot be checked by any
    command, still carry it in the `goal`, and say in `risk_notes`-style prose
    inside that goal that a human has to judge it at the demo checkpoint.

## Self-check before you answer

- Would `acceptance` run on this machine as written, inside the workspace?
- Does every milestone end in something a human can look at before merging?
- Did I use only paths from the listing above, plus ones a milestone creates?
- Is the JSON the only thing I returned, with no key outside the two lists?
