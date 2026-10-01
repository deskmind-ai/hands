"""Model-free adapters. These are the cheapest and most important evals you own.

``OracleAdapter`` replays a known-good action sequence stored in the task file.
If the oracle does not score strict, the *task* is broken -- wrong fixture, wrong
grader, unreachable goal -- and no model result from it means anything. Running
the oracle over every task before every formal sweep is what stops you from
shipping a benchmark that measures your own bugs.

``NullAdapter`` does nothing and declares completion. If a task still scores
strict, the grader is vacuous. Every task must fail this one.
"""

from __future__ import annotations

from ..actions import Action, ActionError, ActionKind, from_json
from ..geometry import CoordSpace
from .base import Proposal, TurnContext


class OracleAdapter:
    name = "oracle"
    coord_space = CoordSpace.LOGICAL_POINTS

    def __init__(self) -> None:
        self._i = 0

    def propose(self, ctx: TurnContext) -> Proposal:
        steps = ctx.task.oracle
        if self._i >= len(steps):
            return Proposal(action=Action(kind=ActionKind.DONE), raw_text="oracle exhausted")
        spec = dict(steps[self._i])
        self._i += 1
        try:
            action = from_json(spec, default_space=CoordSpace.LOGICAL_POINTS)
        except ActionError as exc:
            return Proposal(action=Action(kind=ActionKind.GIVE_UP),
                            raw_text=str(spec), parse_error=f"bad oracle step: {exc}")
        action.binding = type(action.binding)(
            observation_id=ctx.observation.id,
            app=action.binding.app,
            window_id=action.binding.window_id,
            element_id=action.binding.element_id,
        )
        return Proposal(action=action, raw_text=repr(spec))

    def usage(self) -> dict:
        return {"steps_consumed": self._i}


class NullAdapter:
    """Negative control: claims success without acting."""

    name = "null"
    coord_space = CoordSpace.LOGICAL_POINTS

    def propose(self, ctx: TurnContext) -> Proposal:
        return Proposal(action=Action(kind=ActionKind.DONE), raw_text="claimed done without acting")

    def usage(self) -> dict:
        return {}


class ReplayAdapter:
    """Replays actions recorded in a previous run's trajectory."""

    name = "replay"
    coord_space = CoordSpace.LOGICAL_POINTS

    def __init__(self, actions: list[dict]) -> None:
        self._actions = actions
        self._i = 0

    def propose(self, ctx: TurnContext) -> Proposal:
        if self._i >= len(self._actions):
            return Proposal(action=Action(kind=ActionKind.DONE), raw_text="replay exhausted")
        spec = self._actions[self._i]
        self._i += 1
        return Proposal(action=from_json(spec, default_space=CoordSpace.LOGICAL_POINTS),
                        raw_text=repr(spec))

    def usage(self) -> dict:
        return {"steps_consumed": self._i}
