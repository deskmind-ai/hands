<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/banner-dark.svg">
    <img src="assets/brand/banner-light.svg" alt="DeskMind 得心 — 得心，应手。" width="720">
  </picture>
</p>

<p align="center">
  <img src="assets/brand/hands-lockup-light.svg" height="40" alt="DeskMind Hands"><br>
  <b>DeskMind Hands</b> · Drives the real macOS desktop in the background, without taking your focus.<br>
  <a href="README.zh-CN.md">中文</a> · <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/data.md">Training data</a> · <a href="docs/apps-config.md">App config</a>
</p>

> Part of **[DeskMind](https://github.com/deskmind-ai/deskmind)**: start there for the app, the demo and the other components.

---

**DeskMind · 得心** — *得心，应手。* (from 得心应手: what the mind decides, the hand carries out) is a family of
open-source projects that let an agent **see** your screen, **decide** the next step, and **act** on the real desktop,
all locally.

This repository is the **Hands**. It is the harness that turns a planner's decision into an action on your Mac, and
records what happened. It is also where the Brain's training data comes from.

| | Repository | Role |
|---|---|---|
| 👁 | [deskmind-ai/eyes](https://github.com/deskmind-ai/eyes) | find the target on a screenshot |
| 🧠 | [deskmind-ai/brain](https://github.com/deskmind-ai/brain) | decide the next step, with calibrated confidence |
| ✋ | **deskmind-ai/hands** | drive the real macOS desktop |
| 📐 | [deskmind-ai/bench](https://github.com/deskmind-ai/bench) | sandbox desktop tasks and graders to reproduce our numbers |

## What it does

- **Works in the background.** Actions go to the target window through the accessibility API and
  [Peekaboo](https://github.com/steipete/Peekaboo)'s window-targeted input, not through the global keyboard and mouse.
  You keep typing in your own app while a task runs. The rare step that can only be done in the foreground is refused
  unless you allow it, and every foreground action is counted in the run record.
- **A projection layer makes Finder and TextEdit tractable.** Moving a file in Finder is select, ⌘C, navigate, ⌥⌘V:
  four decisions a planner loses its way in. Hands shows it as one synthetic control per file, *move 「a.log」 to ▾*,
  with the valid destinations as options. New folder is a name field and a button; save-as is a name, a place and a
  button; undo is a button. Each synthetic control is flagged, carried out through the app's scripting interface, and
  counted separately. Switch it off with `HANDS_PROJECTION=0` to measure a planner on the raw accessibility tree.
- **Typed questions, not free text.** The `systemone` adapter speaks `POST /v1/systemone`: it sends the page state and
  a set of typed questions (*which operation?*, *which element for CLICK?*, *which value to type?*, *is the goal
  met?*), each with enumerated options, and reads back a probability per option. The planner never writes an action,
  so nothing is parsed. Point it at [DeskMind Brain](https://github.com/deskmind-ai/brain) on your Mac, or at any
  server with the same API.
- **An oracle for DAgger labels.** Every generated training task carries its effect oracle, the shell commands that
  produce the correct end state. `tools/oracle_label.py` turns what is still left to do into the right answer for any
  state a student model reached, with no model in the loop. States the oracle cannot place go to people ([docs/data.md](docs/data.md)).
- **Eval-first.** Every task is YAML with a declarative grader. `verify-tasks` requires the oracle to pass and a
  do-nothing control to fail before a task counts. The graders and the diag suite live in
  [deskmind-bench](https://github.com/deskmind-ai/bench), the scorer of record.

## Quick start (mock desktop, no permissions)

The mock desktop is a real filesystem behind a simulated Finder and editor. It runs anywhere, including Linux CI.
deskmind-bench must be cloned next to hands (`git clone https://github.com/deskmind-ai/bench ../bench`, relative to
the hands checkout): both `pip install -e ../bench` and `uv sync` use it from there.

```bash
git clone https://github.com/deskmind-ai/bench && git clone https://github.com/deskmind-ai/hands
cd hands
python -m venv .venv && . .venv/bin/activate
pip install -e ../bench -e ".[render]"          # or: uv sync (uses ../bench)

python -m unittest discover -s tests              # contracts: coordinates, actions, graders, sandbox
deskmind-hands doctor                             # what this machine can run, and why not the rest
deskmind-hands verify-tasks --set smoke           # oracle passes, do-nothing fails, for every task
deskmind-hands run --set smoke --adapter oracle --repeats 1 --gate 100 --no-screenshots
```

With a local Brain server on port 8793 (see its README), the same smoke set runs with a real planner:

```bash
deskmind-hands run --set smoke --adapter systemone --systemone-url http://127.0.0.1:8793 --model brain-4b
```

## The real desktop

Real runs need macOS, Peekaboo on `PATH` (upstream 4.3.1 or later, which has the MCP `see` fix; tested on
macOS 27, zh-Hans system locale), and Screen
Recording plus Accessibility granted to Peekaboo's host app. Install the extras with `pip install -e ".[macos]"`.

```bash
HANDS_PEEKABOO_TRANSPORT=mcp deskmind-hands run --set train --task T-newfolder-01000 \
    --driver peekaboo --adapter systemone --systemone-url http://127.0.0.1:8793 --model brain-4b
```

Every run works in a sandbox under `runs/<run>/ws`, unpacked from a fixture. It closes the windows it opened and
restores your clipboard on the way out. To run the diag benchmark and compare with the reference numbers, use
[deskmind-bench](https://github.com/deskmind-ai/bench).

## General mode

`deskmind-hands do` runs a goal in your own words on the real desktop: no fixture and no grader. Name the apps the
goal may use as `name=bundle id`; `--in` confines file work to one folder and prints a change report afterwards.

```bash
HANDS_PEEKABOO_TRANSPORT=mcp deskmind-hands do "<goal>" --apps Safari=com.apple.Safari,TextEdit=com.apple.TextEdit \
    --adapter systemone --systemone-url http://127.0.0.1:8793 --model brain-4b
```

It asks for confirmation first (`--yes` skips it); `--ask stdin` lets the planner ask you questions, and
`--foreground-ok` allows the named apps a brief foreground flash when they ignore background input.

## The gym

`tools/gym` holds small sandbox web apps (`mail`, `music`, `settings`, and `mailmusic`, which spans two of them), each
generated from a seed and paired with an oracle that reads the app's true state. A local server serves them to
GymHost, a chromeless window around one page, so no real app or user data is touched. The runner drives a planner
through them and writes DAgger rows labelled by the oracle.

```bash
tools/gym/host/build.sh                           # builds tools/gym/host/build/GymHost.app (needs swiftc)
HANDS_PEEKABOO_TRANSPORT=mcp python -m tools.gym.run --app music --seeds 1-20 \
    --url http://127.0.0.1:8793 --model brain-4b --out data/gym/music.rows.jsonl
```

`--summary` writes one line per task (passed, approvals asked, final state); `--trap` and `--beta` control how often a
plausible wrong step or the oracle's own step is carried out.

## Layout

| Path | What lives there |
|---|---|
| `deskmind_hands/drivers/peekaboo.py` | the real-desktop driver and its projection layer |
| `deskmind_hands/drivers/mock.py` | the deterministic mock desktop |
| `deskmind_hands/adapters/systemone.py` | typed-question planner client (`/v1/systemone`) |
| `deskmind_hands/adapters/` | also `oracle`/`null` (free), `anthropic`, `openai`, `mlx`, `hybrid` |
| `deskmind_hands/runtime/loop.py` | the agent loop: budgets, cancellation, stale bindings, goal tracking |
| `deskmind_hands/geometry.py`, `actions.py` | the one place coordinates are converted; the one action vocabulary |
| `deskmind_hands/apps.py` | per-app vocabularies from your local config (empty by default) |
| `tasks/smoke`, `tasks/train`, `tasks/holdout` | 6 mock tasks; 200 generated training tasks (10 families); 42 held-out tasks: unseen phrasings of every family, plus two compositional families |
| `tools/` | task generation, state collection, oracle labelling, label packs, the gym |

## App-specific knowledge stays on your machine

Some apps need facts the accessibility tree does not carry: names for icon-only buttons, the controls of an app with
no tree at all, a chat app that may only be used in one conversation. None of that ships here. It loads from
`~/.config/deskmind/apps.yaml` (or `$DESKMIND_APPS`), and with no file every app is treated the same way. The generic
mechanisms stay: grounding unnamed buttons with a local model, vision elements from on-device OCR, and a guarded
foreground flash-click. See [docs/apps-config.md](docs/apps-config.md).

## Honest limits

- **macOS only** for real runs, and tested on one machine with a Chinese system locale. Labels of synthetic controls
  are in Chinese, as the planners were trained on them.
- **Finder and TextEdit** are the apps with projections. Elsewhere, Safari included, a planner sees the raw
  accessibility tree.
- **Some steps need the foreground.** Where an app accepts no background input at all, Hands refuses the step unless
  you allow a short foreground flash for that app.

## License

Apache-2.0, see `LICENSE` and `NOTICE`. The DeskMind name, 得心, the logo and Xiaofang are not covered by the code
licence. You may use them to refer to the project, but not in modified form or to imply endorsement.
