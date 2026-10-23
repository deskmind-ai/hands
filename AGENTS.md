# AGENTS.md — DeskMind Hands

Instructions for coding agents (Codex, Claude Code) and reviewers working in this repo. Read
[CONTRIBUTING.md](CONTRIBUTING.md) first: set-up, the two drivers, the projection layer and the sandbox rule.

## Running the tests

- `python -m unittest discover -s tests -v`, plus the two smoke checks in CONTRIBUTING.md. Tests are mock-only: no
  `--driver peekaboo`, no real app, no network, no model key.
- With pytest, put a fake `peekaboo` on PATH that exits at once, and pass `--timeout`. Without pyobjc,
  `executor._windows` falls back to calling the real `peekaboo` command, and it can hang the run.
- Missing Pillow gives three known errors (`test_annotate_trace.py`, `StepPictures`, `RareHan`). Install the `render`
  extra to clear them.

## Rules for this repo

- **User input must never be counted less than main counts it.** This is input attribution (`inputwatch.py`, the
  executor's epoch and `user_active`). A key the user typed into a task's app must stale the task. Main counts every
  key with no app target (target 0 or the Dock).
  - The only intended exception is a plain Command chord typed in the user's own app between steps (the rc.13 stale
    loop).
  - Any change here keeps `MainIsTheFloor` (a table of scenarios run against main's `note()`) and `EvidenceProperty`
    (random interleavings, judged by ground truth) passing. New scenarios go into that table.
  - A key may be left uncounted only on positive evidence. Over-counting is an effect, not a bug.
- **Front record.** Polled reads prove which app was in front only at some instant inside each read. A span counts as
  seen only when consecutive reads are at most `FRONT_GAP_S` apart. Anything else is a gap, and keys in a gap count.
- **Never set a process-wide AX messaging timeout.** It changes every accessibility call in the process, including the
  guards.
- **Focus give-back changes go in their own PR.** That is `_give_front_back` and when the front is given back. Never
  bundle them with other work. DeskMind takes the foreground only on the user's click.
- **Register before acting.** Anything that can bring an app to the front (a step, a flash, staging, a launch,
  `_open_window`) registers its interval with the input watch before it acts, and closes the interval in `finally`.
- **Re-check the shared input watch.** There is one per process, and it is re-checked on every reuse: permission in
  both directions, the tap still enabled, and the poller alive. If any check fails it is rebuilt.

## How we change code here (shared across deskmind-ai repos)

These rules come from the review rounds of hands#62 and deskmind#107 (10-22/23). In those rounds the same family of bug
was patched one case at a time for many rounds, until the fix was redesigned around where the fact really lives.

### Before writing a fix

- **Write the invariant first.** State, in one sentence in the PR description, what must always hold. For example:
  "a key the user typed into a task's app is never counted less than main counts it", or "every process the helper
  runs ends up on the current key". Then turn it into a test that checks it across many cases: a table against the
  old behaviour, or random interleavings. Don't rely only on one test per reported scenario.
- **List the states.** For anything with a lifecycle (a process, a watch, a request, a session), write down every
  state it passes through, from created to registered, started, stored, ending, closing and gone. For each reader of
  that thing, say what it sees in each state. Most of the bugs in those rounds were one state a reader forgot.
- **Stop patching after the second bug of a family.** When a review finds a second case of the same kind of bug, stop
  adding cases. Find the fact's source of truth and derive from it:
  - The side that owns a fact reports it. The other side doesn't keep its own record of what it thinks happened.
  - Derive related values from a single read. Example: a process's environment and the digest reported for it come
    from the same read of the file.
  - Register first, then read. An item becomes visible to readers before it reads the data that can change under it.
  - Record something as done only after the other side confirmed it. A failed or unanswered request stays owed and
    is retried.
- **Classify every finding before working on it.**
  - A **bug** is any of: a gate breaks (a false completion, an unconsented change or send, no stop or rollback); a
    core function no longer works; a crash or hang; a regression; two checks that contradict each other. Bugs are
    always fixed.
  - An **effect** is friction: an extra card, an extra stale, slowness, wording. Effects are rated and recorded in
    deskmind#90 for later.
  - A state that can stay wrong forever, like a session left on a revoked key, is a bug. That holds even if the first
    example you find looks harmless.

### Adding a mechanism is where new bugs come from

When a fix adds a cache, a lock, a wait, a shared object or a retry, go through this list:

- **Cache.** Only cache success, never a failure. Something must invalidate the entry, for example a reused pid needs
  its start time checked. When an entry is reused, recheck that it still matches what holds now (permissions, an
  enabled tap, a live thread).
- **Shared or process-wide object.** Check whether state leaks from one user to the next, and check its health on
  every reuse. Never set a process-wide setting to fix one caller.
- **Wait.** A wait is always bounded. Never wait for work that is queued on the queue you are running on.
- **Lock.** Never hold a lock across a callback, IO or a run loop. Keep one lock order.
- **Teardown.** A thread that arrives late must find out that it was given up and return. Stopping twice is harmless.
- **Retry.** A retry is bounded or backed off. A record of success is written only after the whole job succeeded.

### Tests

- A new regression test must fail on the previous head. Say so in the PR or the commit.
- Fakes model the real lifecycle and the real failure replies. That includes the window between spawn and start, a
  lost or refused request, and a service that checks credentials. A fake that skips a state hides the bug in that
  state.
- A property test's oracle comes from the simulated ground truth. It must not repeat the implementation's own
  algorithm.
- For changes that involve threads or concurrency, run the suite several times in parallel under load. Use a race
  detector if one exists for the language.

### PRs and review rounds

- **One PR, one invariant.** When a second concern shows up, such as a new ownership question or a new sync problem,
  split it into its own PR.
- **One round fixes the whole family.** Fix every finding of a review round together. Before pushing, grep for other
  places with the same pattern and fix them in the same round.
- **Our re-reviews look only at what changed since the last reviewed head.** The external review covers the whole PR.
  Don't run two reviews on different heads at the same time.
- **Merging into main needs the `reviewed` label.** Merge with `--merge` at the reviewed head
  (`--match-head-commit`), after posting a review record on the PR.
