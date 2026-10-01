"""What becomes of each error the harness meets, decided in one place.

Every failure message a driver, the OCR reader or the loop produces goes through `classify`. The result is one of:

- **TRANSIENT**: read or try again, a bounded number of times, and do not count it as a step. A window that changed
  while it was being captured, an accessibility tree still being built, an OCR reader that timed out.
- **USER_BUSY**: the user was using their Mac and the step waited for them; nothing was done (see ExecResult.deferred).
- **REJECTED**: the step could not be done as asked. The planner is told why and chooses again.
- **ENVIRONMENT**: the machine, not the harness. The run ends, and the failure is scored as the environment's: a
  locked screen, a missing permission, two apps registered under one bundle id.
- **HARNESS**: anything else that stops the run. A bug in the harness, reported as one.

These rules used to live as string checks at every place an error was handled, each place knowing a different subset.
A run then died as a "harness bug" on a transient read that another place would have retried (09-30), and 22 gym runs
were scored harness bugs when the Mac had two GymHost apps registered. The table below is the one list; the test
suite pins every rule against the real messages it was written for (tests/test_errors.py). Codex CLI keeps its
error taxonomy the same way (protocol/src/error.rs, retry_delay).
"""
from __future__ import annotations

import re
from enum import Enum


class Kind(Enum):
    TRANSIENT = "transient"
    USER_BUSY = "user_busy"
    REJECTED = "rejected"
    ENVIRONMENT = "environment"
    HARNESS = "harness"


#: (pattern, kind): the first match decides. Patterns are matched case-insensitively against the whole message.
RULES: list[tuple[str, Kind]] = [
    # The user, not a fault (drivers/base.py USER_BUSY).
    (r"^the user is active", Kind.USER_BUSY),
    # The machine.
    (r"session is locked|screen capture is unavailable while the macos gui session is locked", Kind.ENVIRONMENT),
    (r"multiple apps match", Kind.ENVIRONMENT),
    (r"not (?:been )?granted|permission (?:is )?(?:denied|missing)|not authori[sz]ed|accessibility (?:access|permission)",
     Kind.ENVIRONMENT),
    # A read to make again.
    (r"changed during capture|no longer matched its process-generation lane", Kind.TRANSIENT),
    (r"ax tree incomplete|accessibility_enumeration_incomplete|tree (?:stays |is )?incomplete", Kind.TRANSIENT),
    (r"timeoutexpired|timed out", Kind.TRANSIENT),
]

_COMPILED = [(re.compile(p, re.I), k) for p, k in RULES]


def classify(message: str, *, default: Kind = Kind.HARNESS) -> Kind:
    """The kind of `message`. `default` is what an unmatched message is: HARNESS for an exception that stopped the
    run, REJECTED for a step result the planner should hear about."""
    text = message or ""
    for pattern, kind in _COMPILED:
        if pattern.search(text):
            return kind
    return default


def transient(message: str) -> bool:
    return classify(message) is Kind.TRANSIENT
