# Evaluating the harness

hands is the harness between a small planner and the desktop: it observes, turns what it sees into typed choices, carries out the one chosen and checks what happened. The planner can only choose among the options hands offers. When the right option is missing, or the observation is wrong or the run is stopped for the wrong reason, no planner can succeed. On 09-30, more than half of the failures had one of these causes rather than the model.

"Better" for the harness therefore has to be measured separately from the model, before a change reaches a model run. There are four layers, cheapest first. A change is accepted only when it passes all four.

## Layer 1: unit and contract tests

`tests/`. Each test pins one behaviour of one function, e.g. "two Lisa Wong orders on screen offer ASK" or "an unsaved document is never closed". These tests show that the cases someone thought of work. They say nothing about cases nobody thought of.

    uv run --with pytest --with pillow python -m pytest -q tests

## Layer 2: replay snapshots

`tests/replay/<case>/` holds recorded `hands do` runs: `manifest.json` and `trace.jsonl`, without screenshots and with the home directory removed. `expected.json` holds what replaying the run gives: its outcome, any divergences, and every request the adapter built, normalized. `deskmind_hands/replay.py` runs the real loop and the real System One adapter against:

- a driver that hands back the recorded observations and results, and
- a planner that answers each request with the recorded choice.

No desktop and no model are involved, so a replay gives the same answer every time.

- **A changed request** means a change to what the planner is shown. If the change is intended, accept it with `--update`. The diff is then a change to the model's input, and the person training the planner needs to know about it.
- **A divergence** means the recorded choice is no longer among the options. For a run that succeeded, a divergence is a regression in reachability.

The replay does not cover the driver, because the observations are the recorded ones. Changes to what the driver sees are covered by layer 3.

    python tools/harness_replay.py add <run dir> --name <case>
    python tools/harness_replay.py check [--update] [case ...]

Runs recorded before 09-30 have no window titles or window lists. They can be replayed, but their FOCUS_WINDOW options come out empty. Record new cases with the current recorder.

## Layer 3: reachability and oracle drives on real apps

`tools/gym/run.py --oracle-only` runs gym tasks against the real apps on the virtual display with no model:

- The family's oracle answers every request, and its answer is carried out.
- When the oracle has no answer among the offered options, the step is recorded as `unreachable`. The run then gives up.

Two numbers come out of this:

- **Reachability**: of the steps the oracle took, the share where its answer was among the options.
- **Oracle pass rate**: this should be 100%. A task that fails with the oracle driving fails because of the harness, the driver, the app or the grader, never because of a model.

    HANDS_GYM_DISPLAY=<id> python -m tools.gym.run --app expense --split 2 --seeds 1-20 \
        --oracle-only --url http://127.0.0.1:1 --model none --out /tmp/oracle.rows.jsonl --summary /tmp/oracle.jsonl

Build the gitignored products before running this from a fresh clone or worktree:

- `swiftc -O tools/native/ocr.swift -o tools/native/ocr`
- `bash tools/gym/host/build.sh`

Without the OCR reader, no text is read from pixels. Without GymHost, every seed prints "GymHost not ready (the page's accessibility tree did not come up); failed as environment". Both look like model or harness failures when they aren't.

Before a seed's first observation, the runner waits (polling) until GymHost is ready: every window at least 400 x 300 pt, and the task's list rows (the mail inbox's messages) present as rows in its accessibility tree. A seed whose host never gets there is not run. With `--summary`, it is written with `"cause": "environment"` and the reason, and no DAgger rows are written for it. On a virtual display the window once came up 1 pt wide and the mail rows lost their role, so the oracle labelled nothing (deskmind#64). Every summary line also records the host's windows (`host_windows`: size, element and row counts).

Two controls come from OSWorld Verified and cua-bench:

- **A null run must fail**: the grader must not pass a task where nothing was done.
- **Fault injection**: the user starts typing mid-run, a window changes while it is being captured, a ghost window appears. The harness must go on correctly.

## Layer 4: the same model, paired A/B

Fix the planner (e.g. G18b) and the task set: bench diag, the D series and gym held-out seeds. Then:

1. Run the old and the new harness in alternation, N times each.
2. Compare task by task.
3. Attribute every failure to one of: harness, model, environment, grader.

If any pair of runs is incomplete, no headline number is reported (the same rule as pi's paired evals).

## The gate

A harness change is "better" only if all of these hold:

1. Layers 1 and 2 pass. Every replay diff is intended and has been reported.
2. In layer 3, reachability does not drop and every oracle drive passes.
3. In layer 4, the overall pass rate does not drop and no task gets worse.

If any of these fails, the change is worse, or not yet shown to be better.

## Lessons from earlier runs

A failure that is the model's, not the harness's, is not fixed with a check for that one task. It becomes a lesson:
one or two sentences of advice for a kind of task, kept in the user's lessons store and recalled by the goal's words
(`deskmind_hands/lessons.py`; `tools/lessons.py add | list | recall | retire`). A lesson names no file, row or window
that only its run had.

Shown to the planner only with `HANDS_LESSONS=1`, as the state's `lessons` section; G18b and earlier never saw one.
What a run recalled is in its trace (`"t": "lessons"`), whether or not requests are logged. A lesson is judged like a harness change: a layer-4 A/B with
the store on and off, on the task that taught it and on the rest of the set, so a lesson that helps one task and
misleads others is caught.

Lessons are written by hand for now. Next: distil them from a failed run's trace with a stronger model, reviewed
before they are added (`--by model:NAME`).

## Reading a run

`python tools/annotate_trace.py runs/do-…` draws each step on the screenshot it was decided on: a box around the
element acted on and, above the picture, the step, what it did and whether it was refused. The images go to the run's
`annotated/` folder with an `index.md`. A run made with `--no-screenshots` has nothing to draw on.
