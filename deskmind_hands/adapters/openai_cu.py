"""OpenAI computer-use adapter.

The action vocabulary and the ``computer_call_output`` screenshot shape below are
taken from the published guide. Two things were NOT pinned down there and are
flagged as UNCONFIRMED: the exact required fields on the tool declaration
(display size / environment) and the acknowledgement format for pending safety
checks. Both are checked at construction time and again on the first response, so
a wrong guess fails loudly during deployment acceptance instead of quietly
mis-driving the desktop for a whole benchmark run.

Reference: https://developers.openai.com/api/docs/guides/tools-computer-use
"""

from __future__ import annotations

import base64
import os
import io
import time
from typing import Any

from ..actions import Action, ActionError, ActionKind, Binding, normalise_keys
from ..geometry import CoordSpace, ImageTransform, Point, Size, to_logical
from .base import AdapterUnavailable, Proposal, TurnContext

#: The guide shows `{"type": "computer"}` but does not state the required sizing
#: fields. We send what the standard shape expects and let the API reject it
#: rather than silently assume it is optional.
UNCONFIRMED = (
    "tool declaration fields (display_width/display_height/environment) and the "
    "pending_safety_checks acknowledgement format are unverified against the live API"
)

MAX_LONG_EDGE = 2048


class OpenAIAdapter:
    name = "openai"
    coord_space = CoordSpace.LOGICAL_POINTS

    def __init__(self, model: str | None = None, *, environment: str = "mac",
                 acknowledge_safety_checks: bool = False) -> None:
        try:
            import openai
        except ImportError as exc:
            raise AdapterUnavailable("pip install openai") from exc
        self._openai = openai
        try:
            self._client = openai.OpenAI()
        except Exception as exc:
            raise AdapterUnavailable(f"no OpenAI credential: {exc}") from exc
        self.model = model or os.environ.get("OPENAI_CU_MODEL") or ""
        if not self.model:
            raise AdapterUnavailable("name a model: --model or OPENAI_CU_MODEL")
        self.environment = environment
        # Auto-acknowledging a safety check on a benchmark run would make the
        # product's approval behaviour untestable, so it is off unless asked for.
        self.ack_safety = acknowledge_safety_checks
        self._prev_id: str | None = None
        self._pending_call: dict | None = None
        self._pending_safety: list = []
        self._transform: ImageTransform | None = None
        self._usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0,
                       "safety_checks": 0, "unconfirmed": UNCONFIRMED}
        self._started = False

    def _encode(self, png: bytes, source_px: Size) -> tuple[str, ImageTransform]:
        tx = ImageTransform.fit(source_px, MAX_LONG_EDGE)
        data = png
        if tx.target.as_tuple() != source_px.as_tuple():
            from PIL import Image
            img = Image.open(io.BytesIO(png)).resize(tx.target.as_tuple(), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        return "data:image/png;base64," + base64.standard_b64encode(data).decode(), tx

    def _to_action(self, act: dict, obs) -> Action:
        kind_name = act.get("type")
        binding = Binding(observation_id=obs.id, app=obs.focused_app)
        common = {"coord_space": CoordSpace.LOGICAL_POINTS, "binding": binding, "raw": act}

        def pt(xk: str = "x", yk: str = "y") -> Point:
            if xk not in act or yk not in act:
                raise ActionError(f"{kind_name} missing {xk}/{yk}")
            return to_logical(Point(float(act[xk]), float(act[yk])),
                              CoordSpace.MODEL_IMAGE, obs.geometry, self._transform)

        if kind_name == "screenshot":
            return Action(kind=ActionKind.SCREENSHOT, **common)
        if kind_name == "click":
            button = act.get("button", "left")
            kind = ActionKind.RIGHT_CLICK if button == "right" else ActionKind.CLICK
            return Action(kind=kind, point=pt(), **common)
        if kind_name == "double_click":
            return Action(kind=ActionKind.DOUBLE_CLICK, point=pt(), **common)
        if kind_name == "move":
            return Action(kind=ActionKind.WAIT, duration_s=0.01,
                          text="mouse_move is not modelled", **common)
        if kind_name == "type":
            return Action(kind=ActionKind.TYPE_TEXT, text=act.get("text", ""), **common)
        if kind_name == "keypress":
            keys = act.get("keys") or []
            return Action(kind=ActionKind.KEY, keys=normalise_keys(keys), **common)
        if kind_name == "scroll":
            return Action(kind=ActionKind.SCROLL,
                          scroll_dx=int(act.get("scroll_x", 0)),
                          scroll_dy=int(act.get("scroll_y", 0)),
                          point=pt() if "x" in act else None, **common)
        if kind_name == "drag":
            path = act.get("path") or []
            if len(path) < 2:
                raise ActionError("drag path needs at least two points")
            a, b = path[0], path[-1]
            return Action(kind=ActionKind.DRAG,
                          point=to_logical(Point(float(a["x"]), float(a["y"])),
                                           CoordSpace.MODEL_IMAGE, obs.geometry, self._transform),
                          to_point=to_logical(Point(float(b["x"]), float(b["y"])),
                                              CoordSpace.MODEL_IMAGE, obs.geometry, self._transform),
                          **common)
        if kind_name == "wait":
            return Action(kind=ActionKind.WAIT, duration_s=float(act.get("duration", 1)), **common)
        raise ActionError(f"unknown computer action type {kind_name!r}")

    def propose(self, ctx: TurnContext) -> Proposal:
        obs = ctx.observation
        if obs.screenshot_png is None:
            raise AdapterUnavailable("this adapter needs the screenshot channel")
        image_url, self._transform = self._encode(obs.screenshot_png, obs.geometry.pixels)

        tool = {"type": "computer", "environment": self.environment,
                "display_width": obs.geometry.logical.w,
                "display_height": obs.geometry.logical.h}

        if not self._started:
            payload: Any = [{"role": "user", "content": [
                {"type": "input_text", "text": f"Task:\n{ctx.task.goal}"},
                {"type": "input_image", "image_url": image_url}]}]
            self._started = True
        else:
            if self._pending_call is None:
                raise AdapterUnavailable("no pending computer_call to answer")
            out: dict[str, Any] = {
                "type": "computer_call_output",
                "call_id": self._pending_call["call_id"],
                "output": {"type": "computer_screenshot", "image_url": image_url,
                           "detail": "original"},
            }
            if self._pending_safety:
                if not self.ack_safety:
                    raise AdapterUnavailable(
                        "model raised a pending safety check; acknowledging it automatically "
                        "would invalidate the approval measurement -- rerun with "
                        "acknowledge_safety_checks=True only if the protocol says so")
                out["acknowledged_safety_checks"] = self._pending_safety
                self._usage["safety_checks"] += len(self._pending_safety)
                self._pending_safety = []
            payload = [out]

        t0 = time.perf_counter()
        try:
            resp = self._client.responses.create(
                model=self.model, tools=[tool], input=payload,
                previous_response_id=self._prev_id, truncation="auto")
        except Exception as exc:
            raise AdapterUnavailable(f"openai api error: {exc}") from exc
        latency = time.perf_counter() - t0

        self._prev_id = resp.id
        self._usage["requests"] += 1
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        self._usage["input_tokens"] += in_tok
        self._usage["output_tokens"] += out_tok

        calls = [o for o in resp.output if getattr(o, "type", "") == "computer_call"]
        text = getattr(resp, "output_text", "") or ""

        if not calls:
            upper = text.upper()
            kind = ActionKind.DONE if "TASK_COMPLETE" in upper else (
                ActionKind.GIVE_UP if "TASK_FAILED" in upper else ActionKind.DONE)
            return Proposal(action=Action(kind=kind, text=text[:500]), raw_text=text,
                            latency_s=latency, input_tokens=in_tok, output_tokens=out_tok)

        call = calls[0]
        self._pending_call = {"call_id": call.call_id}
        self._pending_safety = list(getattr(call, "pending_safety_checks", []) or [])
        act = call.action if isinstance(call.action, dict) else call.action.model_dump()
        try:
            action = self._to_action(act, obs)
        except (ActionError, ValueError, KeyError) as exc:
            return Proposal(action=Action(kind=ActionKind.SCREENSHOT), raw_text=repr(act),
                            latency_s=latency, input_tokens=in_tok, output_tokens=out_tok,
                            parse_error=str(exc))
        return Proposal(action=action, raw_text=text or repr(act), latency_s=latency,
                        input_tokens=in_tok, output_tokens=out_tok)

    def usage(self) -> dict:
        return dict(self._usage)
