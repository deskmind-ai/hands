# Contracts

Fixtures here are contracts between hands and harness: both repos test against the same file, so a field added,
renamed or dropped on one side fails the other's tests.

- `opened_window.json`: the exact key set of an `open_window` reply's `opened_window` (a window a launch brought up
  for an app this launch's own read said was not running). `was_running` is always `false` when the field is present;
  harness counts a window as opened by the task only with `was_running === false`, and treats it missing as unknown,
  the user's.

Synthetic names only: no run data.
