# Contributing to DeskMind Hands · [中文](CONTRIBUTING.zh-CN.md)

Thanks for helping. DeskMind Hands acts on someone's real desktop, so every change is judged the same way: does it let
a planner do more on the desktop, more reliably, while touching nothing outside the task? The org-wide guidelines
([deskmind-ai/.github](https://github.com/deskmind-ai/.github)) apply here too, including the
[code of conduct](https://github.com/deskmind-ai/.github/blob/main/CODE_OF_CONDUCT.md).

## Set up

```bash
git clone https://github.com/deskmind-ai/bench && git clone https://github.com/deskmind-ai/hands
cd hands
python -m venv .venv && . .venv/bin/activate
pip install -e ../bench -e ".[render]"     # the task schema and graders come from deskmind-bench
```

Then the three checks CI runs on Linux (`.github/workflows/evals.yml`). All three must pass before you open a PR:

```bash
python -m unittest discover -s tests -v                        # contracts, milliseconds
deskmind-hands verify-tasks --set smoke                        # task self-check: every oracle passes, doing nothing fails
deskmind-hands run --set smoke --adapter oracle --repeats 1 --gate 100 --no-screenshots   # smoke: the loop, end to end
```

**Tests are mock-only.** They run on the mock desktop with the `oracle` or `null` adapter: no Mac, no permissions, no
model, no credential. Never add a test that needs `--driver peekaboo`, a real app, the network or an API key. CI fails
if a model key is even present. `deskmind-hands doctor` shows what your machine can run.

## Two drivers, one interface

Both implement `Driver` in `deskmind_hands/drivers/base.py`: `observe()` returns elements, `execute()` performs one
action bound to the latest observation, and `state()` feeds the graders.

- **`drivers/mock.py`, platform-independent.** A simulated file window and editor over a real workspace directory.
  Actions change actual files, so the same grader scores a mock run and a real one. This is where tests live.
- **`drivers/peekaboo.py`, macOS only.** Reads the accessibility tree and a window screenshot through
  [Peekaboo](https://github.com/steipete/Peekaboo) and delivers input to the target window in the background. It
  needs Screen Recording and Accessibility permissions, so no test or CI job uses it. Changes here are checked by hand
  on a Mac; say in the PR which macOS and Peekaboo versions you used.

A driver reports what it cannot do (`ExecResult(unsupported=True)`) instead of faking it. A write it cannot read back
is `indeterminate`, not a success.

## The projection layer

In Finder and TextEdit, `peekaboo.py` adds **synthetic** controls to the observation: a *move 「file」 to ▾* dropdown
per file, a new-folder name field and button, save and save-as, undo (`_project_finder`, `_project_textedit`). Their
ids start with `syn:`, they carry `synthetic=True`, `_execute_projected` carries them out through the app's scripting
interface, and every use is counted in `state()` (`projected_actions`). `HANDS_PROJECTION=0` switches the layer off.

Rules for a projection: offer only destinations inside the workspace; verify the effect after the scripted call; keep
the labels short and in the language the planners were trained on; and never let a synthetic control reach something
the real GUI could not.

## Adding support for another app

1. **Survey it.** `tools/ax_survey.py` shows what its accessibility tree exposes. If the tree is usable, the planner may
   already cope with no new code. Check that first with a task.
2. **Project what is missing.** Add a branch for the bundle id in `observe()` and a `_project_<app>()` that returns
   `syn:<app>:...` elements, then handle those ids in `_execute_projected`. Use the app's scripting interface, confined
   to the workspace.
3. **Keep app vocabularies out of code.** Names for icon-only buttons, vision-only controls, background chords and a
   chat app's single allowed conversation belong in `~/.config/deskmind/apps.yaml`, read by `deskmind_hands/apps.py`
   ([docs/apps-config.md](docs/apps-config.md)). Nothing tied to one user's apps, locale or accounts is committed.
4. **Test on the mock.** Factor the projection so it can be tested without the app: a unit test that feeds it a
   workspace and a list of `Element`s and checks the synthetic controls and their effects. Where the mock can model
   the behaviour, add it to `drivers/mock.py` so the smoke set covers it.
5. **Add tasks.** A task set under `tasks/` with fixtures and an effect oracle, passing `verify-tasks`. Benchmark tasks
   for the new app are proposed in [deskmind-bench](https://github.com/deskmind-ai/bench).

A driver for another platform (Linux AT-SPI, Windows UI Automation) is a new class implementing `Driver`. Start
read-only: `observe()` producing the same `Element` shape, with `execute()` returning `unsupported`.

## The sandbox rule

Tasks and tests never touch the user's real files. Every run works in `runs/<run>/ws`, unpacked from a fixture, and
`env/workspace.py` refuses any path outside the run root. Task `stage` and `oracle_effect` commands use `$WS`, never
`~` or an absolute path. Tests use temporary directories. Nothing sends global keystrokes or clicks outside the target
window, and the user's clipboard is restored at the end of a run.

## Pull requests

- **One change per PR,** with a short description of what changed and how you checked it.
- **Harness changes need evidence.** A change to what the planner sees or how actions are carried out changes the
  harness version: bench numbers before and after it are not comparable. Paste a `deskmind-bench run` or
  `deskmind-bench score` result before and after, and say that the version changes. Maintainers bump it in
  deskmind-bench.
- **Keep the planner API stable.** The `systemone` adapter speaks `POST /v1/systemone` with `{state, questions}` and
  reads `{answers}`. Extend it compatibly.
- **Style.** Match the surrounding code: small functions, comments that explain why, bundle ids rather than app names,
  coordinates converted only in `geometry.py`, actions only from `actions.py`, no new core dependencies (PyYAML only).
- **Checklist:**
  - [ ] the three checks above pass on the mock desktop;
  - [ ] no test needs a Mac, a real app, the network or a key;
  - [ ] no app-specific vocabulary, personal path, account name or screenshot of your own apps in the diff;
  - [ ] real-desktop changes: macOS and Peekaboo versions, and before/after numbers in the description;
  - [ ] docs updated in English and 中文 (`docs/*.md` and `docs/*.zh-CN.md`).

## Reporting problems

- **Bugs:** give the command, the driver, the task id, and the run's `run.json` and `trace.jsonl` with anything
  personal removed. Screenshots only if they show nothing but the sandbox windows.
- **Security issues**, including Hands acting outside the task or its sandbox: don't open a public issue. See
  [SECURITY.md](https://github.com/deskmind-ai/.github/blob/main/SECURITY.md).

By contributing you agree that your contribution is licensed under Apache-2.0, the licence of this repository.
