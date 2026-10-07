# What the planner is shown: changes on `next`

Every change on `next` that alters what a model is shown -- the state (`page.text`, elements, recent actions, notes),
the questions, their options or their order -- is listed here, newest last, as it merges (deskmind#65). The 0.6 cut
reads this list: the replay baseline is re-run over it, and a retrain decides which of these its data must cover.
A change to the protocol's schema is not listed here; it bumps the protocol version (`2.0-next.N`) instead.

Each entry: the PR, what changes for the model, and how much of the recorded corpus it moves
(`tools/harness_replay.py corpus`, on the machine that has the runs).

| PR | What the model sees differently | Corpus |
|---|---|---|
| hands#29 | `page.text` and the value options no longer carry the values of position and level controls (scroll bars, sliders, splitters, progress and level indicators), nor a label that only repeats the value. Their names stay; a stepper's value stays. (deskmind#63) | 1,062 of 4,819 runs (TextEdit, Finder windows); only position numbers removed |
