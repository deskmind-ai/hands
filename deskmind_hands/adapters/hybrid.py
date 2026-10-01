"""Cloud plans, local model looks: the screenshot never leaves the machine.

The measured split between the two models is what makes this shape worth
building rather than merely arguing about. On the mock desktop, UI-TARS-1.5-7B
4-bit used an element id for all 24 of its clicks, never fell back to guessing
coordinates, and produced one unparseable turn in 24 -- but scored 0/6 strict,
losing every task to a no-progress loop. It reads a screen; it does not finish a
job. Claude scores 83% on the same tasks and needs the screen described to it.

So each model does the half it is good at:

  cloud   plans from *text only* -- the accessibility element list and the
          history of what happened. It never receives an image.
  local   answers visual questions about the current screenshot when the cloud
          asks one, because the thing the cloud needs is often not in the
          accessibility tree at all: an invoice rendered as a picture, an
          unlabelled control, a value drawn rather than exposed.

Privacy stops being a claim and becomes a structural property: this adapter
cannot send a screenshot to the cloud, and ``assert_no_images_sent`` proves it
after every run. What crosses the network is the element list and whatever the
local model reported -- an invoice total, not the invoice.
"""

from __future__ import annotations

import time
from typing import Any

from ..actions import Action, ActionKind
from ..geometry import CoordSpace
from .base import AdapterUnavailable, Proposal, TurnContext

LOOK_TOOL = {
    "name": "look_at_screen",
    "description": (
        "Ask a question about what is currently on screen. A local vision model "
        "answers from the screenshot, which never leaves this machine. Use it "
        "whenever what you need is not in the element list -- text rendered as a "
        "picture, a document's contents, an unlabelled control's position."),
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string",
                                    "description": "one specific question, e.g. "
                                                   "'what is the invoice number?'"}},
        "required": ["question"],
    },
}


class HybridAdapter:
    name = "hybrid"
    coord_space = CoordSpace.LOGICAL_POINTS

    def __init__(self, planner, grounder, *, max_looks_per_turn: int = 1,
                 mode: str = "describe", describe_px: int = 78_000) -> None:
        """
        mode="ask"       the planner requests a look, the local model answers,
                         the planner decides. Two cloud round trips per visual
                         step, and each round trip costs ~2.8s of fixed overhead
                         on this endpoint regardless of its contents -- so this
                         mode is structurally slower than sending the image and
                         cannot be tuned out of it.
        mode="describe"  the local model describes every screen up front and the
                         description rides along with the observation. One cloud
                         round trip, same as cloud-only. This is the only shape
                         that is not a latency loss by construction.

        describe_px      pixel budget for the local pass. Local latency is
                         391ms per 100k pixels and accuracy held at 3/3 down to
                         78k, so the default is 78k: we were feeding it four
                         times the pixels it needed.
        """
        if mode not in ("ask", "describe"):
            raise AdapterUnavailable(f"unknown hybrid mode {mode!r}")
        self.planner = planner
        self.grounder = grounder
        self.mode = mode
        self.describe_px = describe_px
        self.max_looks = max_looks_per_turn
        self._usage = {
            "planner_requests": 0, "grounder_requests": 0,
            "look_questions": 0, "images_sent_to_cloud": 0,
            "planner_latency_s": 0.0, "grounder_latency_s": 0.0, "cost_usd": 0.0,
        }
        # The planner must not be able to see an image even by accident.
        # mode is already set above: _extend_tools depends on it.
        self._blind_planner()

    def _blind_planner(self) -> None:
        """Replace the planner's image encoder with one that refuses.

        Belt and braces: the turn context handed to the planner already has its
        screenshot stripped, but a future edit to the cloud adapter could add an
        image block back without anyone noticing. This makes that a crash rather
        than a silent privacy regression.
        """
        adapter = self.planner
        usage = self._usage
        original = getattr(adapter, "_image_block", None)
        if original is None:
            self._extend_tools()
            return

        def guarded(obs):
            block = original(obs)
            if block is not None:
                # Normally unreachable: the planner is handed observations with
                # the pixels already removed. If a future edit puts an image back
                # this crashes rather than quietly becoming a privacy regression.
                usage["images_sent_to_cloud"] += 1
                raise AdapterUnavailable(
                    "the planner tried to send an image; in this configuration "
                    "the screenshot never leaves the machine")
            return None

        adapter._image_block = guarded  # type: ignore[method-assign]
        self._extend_tools()

    def _extend_tools(self) -> None:
        """Offer the look tool only in the mode that can answer it.

        In describe mode the screen has already been read and the text rides
        along with the observation, so there is nothing for this tool to do --
        and leaving it visible is not harmless. The planner called it, got back
        a no-op, called it again, and each of those empty round trips cost a
        cloud turn: 14 to 45 turns for a task the cloud alone does in 7, at 14x
        the price. The first describe-mode measurement was of this bug.
        """
        from . import anthropic_cu
        present = [t for t in anthropic_cu.CUSTOM_TOOLS if t.get("name") == LOOK_TOOL["name"]]
        if self.mode == "ask" and not present:
            anthropic_cu.CUSTOM_TOOLS.append(LOOK_TOOL)
        elif self.mode == "describe" and present:
            for t in present:
                anthropic_cu.CUSTOM_TOOLS.remove(t)

    # -- the local model as an eye -----------------------------------------

    def _look(self, question: str, ctx: TurnContext) -> str:
        """Put one visual question to the local model and return its answer."""
        obs = ctx.observation
        if obs.screenshot_png is None:
            return "no screenshot is available for this observation"
        t0 = time.perf_counter()
        try:
            img = self.grounder._image(obs.screenshot_png, obs.geometry.pixels)
            prompt = (f"{question}\n\nAnswer with the value only, no explanation. "
                      "If it is not visible, say NOT VISIBLE.")
            formatted = self.grounder._apply(
                self.grounder._processor, self.grounder._config, prompt, num_images=1)
            raw = self.grounder._generate(
                self.grounder._model, self.grounder._processor, formatted, [img],
                max_tokens=96, verbose=False)
        except Exception as exc:  # noqa: BLE001 - a blind eye is a result, not a crash
            return f"the local model could not answer: {type(exc).__name__}: {exc}"
        self._usage["grounder_requests"] += 1
        self._usage["look_questions"] += 1
        self._usage["grounder_latency_s"] += time.perf_counter() - t0
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        return text.strip()[:400]

    # -- main entry point ---------------------------------------------------

    DESCRIBE_PROMPT = ("列出画面中所有可读的文本，每行一条，不要解释。"
                       " List every readable piece of text on screen, one per line.")

    def _describe(self, ctx: TurnContext) -> str:
        """One local pass over the screen, before the planner is asked anything."""
        obs = ctx.observation
        if obs.screenshot_png is None:
            return ""
        t0 = time.perf_counter()
        try:
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(obs.screenshot_png)).convert("RGB")
            # Scale to the pixel budget rather than the model's default: the
            # vision encoder is the whole cost and accuracy did not need more.
            w, h = img.size
            if w * h > self.describe_px:
                r = (self.describe_px / (w * h)) ** 0.5
                img = img.resize((max(64, int(w * r)), max(64, int(h * r))), Image.LANCZOS)
            formatted = self.grounder._apply(
                self.grounder._processor, self.grounder._config,
                self.DESCRIBE_PROMPT, num_images=1)
            raw = self.grounder._generate(
                self.grounder._model, self.grounder._processor, formatted, [img],
                max_tokens=160, verbose=False)
        except Exception as exc:  # noqa: BLE001
            return f"(local vision unavailable: {type(exc).__name__})"
        self._usage["grounder_requests"] += 1
        self._usage["grounder_latency_s"] += time.perf_counter() - t0
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        return text.strip()[:1200]

    def propose(self, ctx: TurnContext) -> Proposal:
        if self.mode == "describe":
            described = self._describe(ctx)
            blind = self._without_screenshot(ctx)
            note = ("On-screen text, read locally (the image itself stays on this "
                    f"machine):\n{described}")
            blind = self._with_notice(
                blind, f"{blind.notice}\n\n{note}" if blind.notice else note)
            t0 = time.perf_counter()
            proposal = self.planner.propose(blind)
            self._usage["planner_requests"] += 1
            self._usage["planner_latency_s"] += time.perf_counter() - t0
            self._usage["cost_usd"] += proposal.cost_usd or 0.0
            return proposal

        looks = 0
        while True:
            blind = self._without_screenshot(ctx)
            t0 = time.perf_counter()
            proposal = self.planner.propose(blind)
            self._usage["planner_requests"] += 1
            self._usage["planner_latency_s"] += time.perf_counter() - t0
            self._usage["cost_usd"] += proposal.cost_usd or 0.0

            raw = proposal.action.raw if isinstance(proposal.action.raw, dict) else {}
            if raw.get("_member") != LOOK_TOOL["name"]:
                return proposal

            # The planner asked to see. Answer locally and let it decide again.
            question = raw.get("question", "")
            answer = self._look(question, ctx)
            looks += 1
            self.planner._answer_look(answer)          # feeds it back as a tool result
            if looks > self.max_looks:
                # A planner that only ever asks to look would never act. Hand
                # back the no-op and let the loop take a fresh observation.
                return proposal
            ctx = self._with_notice(
                ctx, f"Local vision answered: {answer}. You have "
                     f"{self.max_looks - looks} look(s) left this turn; act on what you have.")

    @staticmethod
    def _without_screenshot(ctx: TurnContext) -> TurnContext:
        """Hand the planner an observation with the pixels removed."""
        from dataclasses import replace
        from ..drivers.base import Observation
        obs = ctx.observation
        blind_obs = Observation(
            id=obs.id, geometry=obs.geometry, transform=obs.transform,
            screenshot_png=None, screenshot_path=None, elements=obs.elements,
            focused_app=obs.focused_app, layout_version=obs.layout_version, ts=obs.ts)
        return replace(ctx, observation=blind_obs)

    @staticmethod
    def _with_notice(ctx: TurnContext, notice: str) -> TurnContext:
        from dataclasses import replace
        return replace(ctx, notice=notice)

    def assert_no_images_sent(self) -> None:
        """The privacy claim, checked rather than asserted."""
        if self._usage["images_sent_to_cloud"]:
            raise AssertionError(
                f"{self._usage['images_sent_to_cloud']} image(s) reached the cloud "
                "planner; this configuration guarantees none do")

    def usage(self) -> dict:
        u = dict(self._usage)
        u["planner"] = self.planner.usage()
        u["grounder"] = self.grounder.usage()
        return u
