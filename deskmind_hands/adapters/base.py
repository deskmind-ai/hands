"""What the harness requires of a model.

Every model gets the same information and the same tool vocabulary; only the
prompt template and the output dialect differ. That constraint is what makes a
model comparison mean anything -- the moment one adapter is handed an extra
channel the numbers stop being about the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..actions import Action
from ..drivers.base import Observation
from ..geometry import CoordSpace
from deskmind_bench.task import Task


@dataclass
class Turn:
    """One completed round, as the model will see it in its history."""

    action_json: dict
    result_ok: bool
    result_detail: str
    #: True/False once the next observation is in: did the screen actually change? ``ok`` does not answer that --
    #: clicking a control that is already in the requested state succeeds and moves nothing.
    changed: bool | None = None
    #: The action's effect as the driver knows it (drivers.base.EFFECTS); "" when not classified.
    effect: str = ""


@dataclass
class TurnContext:
    task: Task
    observation: Observation
    history: list[Turn]
    channels: frozenset[str]           # subset of {"screenshot", "ax", "dom"}
    dialogue: list[tuple[str, str]] = field(default_factory=list)  # (question, reply)
    notice: str | None = None          # e.g. "your last action was rejected as stale"
    #: Element ids that were acted on without changing the screen, since the last real change. A chooser adapter
    #: should stop offering them; a text adapter gets the same fact through ``notice``.
    ineffective: tuple[str, ...] = ()
    #: Which run and which step this decision is for (protocol: Request identity): the trace's own numbering, so a
    #: server's log and the run's trace can be joined.
    run_id: str = ""
    step: int = 0
    #: How many questions the planner itself has asked this run: what takes ASK away and counts against the budget.
    #: A harness approval is not one, and is not in `dialogue` either (G16).
    asked: int = 0


@dataclass
class Proposal:
    action: Action
    raw_text: str = ""
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    parse_error: str | None = None
    #: Why this step, decided, is not to be carried out as it stands (pipeline.policy): a write or a commit the model
    #: was not sure enough of. The loop looks again, then asks the user, else stops.
    unsure: str | None = None

    def to_json(self) -> dict:
        return {
            "action": self.action.to_json() if self.action else None,
            "latency_s": round(self.latency_s, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "parse_error": self.parse_error,
        }


class AdapterUnavailable(RuntimeError):
    """SDK missing, credentials absent, or the model cannot be loaded."""


@runtime_checkable
class Adapter(Protocol):
    name: str
    coord_space: CoordSpace

    def propose(self, ctx: TurnContext) -> Proposal: ...
    def usage(self) -> dict: ...
