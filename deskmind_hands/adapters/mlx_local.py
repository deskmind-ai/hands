"""Local vision model adapter, via MLX.

The two settings that decide whether a local model looks capable or hopeless are
its output dialect and its coordinate convention, and both are properties of the
checkpoint rather than of the family. Getting either wrong produces output that
reads exactly like weak grounding, so both are established by measurement --
``tools/identify_coords.py`` -- and recorded here with the date and the evidence.

Measured on 2026-09-06, mlx-community/UI-TARS-1.5-7B-4bit on an M2 Max:

  coordinate convention   absolute pixels of the image it was shown
                          3/4 synthetic targets hit; 0/4 for 0-1000 and 0-1
                          (the published plan assumed 0-1000, which would have
                          put every click roughly 1.5x too far right and down)
  dialect                 answers `click(x, y)` when asked plainly, and emits
                          `<|box_start|>(x,y)<|box_end|>` unprompted
  latency                 5.4-6.4 s per turn, comparable to the cloud endpoint's
                          median on the same machine
"""

from __future__ import annotations

import io
import json
import re
import time

from ..actions import Action, ActionError, ActionKind, Binding, normalise_keys
from ..geometry import CoordSpace, ImageTransform, Point, Size, to_logical
from .base import AdapterUnavailable, Proposal, TurnContext

#: Verified per checkpoint, never assumed from the model family. A wrong entry
#: here costs tens of points of apparent accuracy and looks like a weak model.
CONVENTIONS = {
    # measured 2026-09-06, 4/4 probes, see module docstring
    "ui-tars": CoordSpace.MODEL_IMAGE,
    # unverified on this machine; the published figure is absolute pixels
    "holo": CoordSpace.MODEL_IMAGE,
    "fara": CoordSpace.MODEL_IMAGE,
    "norm1000": CoordSpace.NORM_1000,
}

MAX_ELEMENTS = 40   # smaller than the cloud adapter's: a 7B model drowns in a long list

SYSTEM = """You are a GUI agent operating a macOS desktop. You are given a task,
a screenshot, and a list of interface elements. Perform the next single action.

## Output Format
Thought: one short sentence
Action: <one action from the space below>

## Action Space
click(element_id='elem_12')          click a listed element, preferred
click(x, y)                          click a pixel of the screenshot
type(content='...', replace=true)    write text; replace=true overwrites the field
hotkey(key='cmd s')                  press a chord
scroll(direction='down', element_id='elem_3')
wait(seconds=1)
finished(summary='...')              only after seeing evidence the task is done
failed(reason='...')

## Rules
- Prefer element_id over pixels. Only fall back to pixels when the target is
  absent from the list.
- Keyboard input goes to whatever holds focus. Click the field first.
- Do not modify anything the task did not name.
"""

_ACTION_RE = re.compile(r"Action:\s*(.+)", re.IGNORECASE)
_BOX_RE = re.compile(r"<\|box_start\|>\s*\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)")
_CALL_RE = re.compile(r"(\w+)\s*\((.*)\)\s*$", re.DOTALL)
_KW_RE = re.compile(r"(\w+)\s*=\s*'([^']*)'|(\w+)\s*=\s*\"([^\"]*)\"|(\w+)\s*=\s*([\w.]+)")


class MLXAdapter:
    name = "mlx"

    def __init__(self, model: str | None = None, *, convention: str = "ui-tars",
                 max_long_edge: int = 1512, max_tokens: int = 192) -> None:
        try:
            from mlx_vlm import generate, load
            from mlx_vlm.prompt_utils import apply_chat_template
        except ImportError as exc:
            raise AdapterUnavailable(
                "mlx-vlm is not importable; run from the project venv") from exc
        if convention not in CONVENTIONS:
            raise AdapterUnavailable(
                f"unknown coordinate convention {convention!r}; known: {sorted(CONVENTIONS)}. "
                "Establish it with tools/identify_coords.py rather than guessing.")
        self.coord_space = CONVENTIONS[convention]
        self.convention = convention
        self.model_id = model or "mlx-community/UI-TARS-1.5-7B-4bit"
        self.max_long_edge = max_long_edge
        self.max_tokens = max_tokens
        self._generate, self._apply = generate, apply_chat_template
        try:
            self._model, self._processor = load(self.model_id)
        except Exception as exc:
            raise AdapterUnavailable(f"cannot load {self.model_id}: {exc}") from exc
        self._config = getattr(self._model, "config", None)
        self._transform: ImageTransform | None = None
        self._usage = {"requests": 0, "parse_failures": 0, "convention": convention,
                       "slowest_turn_s": 0.0}

    # -- observation ------------------------------------------------------

    def _image(self, png: bytes, source_px: Size):
        from PIL import Image
        self._transform = ImageTransform.fit(source_px, self.max_long_edge)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if self._transform.target.as_tuple() != source_px.as_tuple():
            img = img.resize(self._transform.target.as_tuple(), Image.LANCZOS)
        return img

    def _elements_text(self, obs) -> str:
        rows = []
        for e in obs.elements:
            if not (e.label or e.settable):
                continue
            flag = " [text field]" if e.settable else ""
            rows.append(f"  {e.id}  {(e.label or e.ax_role)[:30]}{flag}")
            if len(rows) >= MAX_ELEMENTS:
                break
        return "Elements:\n" + ("\n".join(rows) if rows else "  (none)")

    # -- parsing ----------------------------------------------------------

    def _to_action(self, text: str, obs) -> Action:
        m = _ACTION_RE.search(text)
        body = (m.group(1) if m else text).strip().splitlines()[0].strip()
        b = Binding(observation_id=obs.id, app=obs.focused_app)

        call = _CALL_RE.search(body)
        if not call:
            raise ActionError(f"no action call in {body[:80]!r}")
        name, argstr = call.group(1).lower(), call.group(2)
        kw = {}
        for g in _KW_RE.finditer(argstr):
            k = g.group(1) or g.group(3) or g.group(5)
            v = g.group(2) if g.group(2) is not None else (
                g.group(4) if g.group(4) is not None else g.group(6))
            kw[k] = v

        def point() -> Point | None:
            box = _BOX_RE.search(argstr)
            nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", argstr)] if not box else \
                   [float(box.group(1)), float(box.group(2))]
            if len(nums) < 2:
                return None
            raw = Point(nums[0], nums[1])
            return to_logical(raw, self.coord_space, obs.geometry, self._transform)

        if name == "click":
            eid = kw.get("element_id") or kw.get("id")
            return Action(kind=ActionKind.CLICK, point=None if eid else point(),
                          coord_space=CoordSpace.LOGICAL_POINTS, foreground=True,
                          binding=Binding(observation_id=obs.id, app=obs.focused_app,
                                          element_id=eid), raw=body)
        if name in ("type", "type_text"):
            replace = str(kw.get("replace", "true")).lower() not in ("false", "0", "no")
            return Action(kind=ActionKind.TYPE_TEXT, text=kw.get("content", ""),
                          clear_first=replace, binding=b, raw=body)
        if name in ("hotkey", "press", "key"):
            keys = kw.get("key") or kw.get("keys") or ""
            return Action(kind=ActionKind.KEY, keys=normalise_keys(keys.replace(" ", "+")),
                          binding=b, raw=body)
        if name == "scroll":
            amt = int(float(kw.get("amount", 3)))
            d = kw.get("direction", "down")
            return Action(kind=ActionKind.SCROLL,
                          scroll_dx={"left": -amt, "right": amt}.get(d, 0),
                          scroll_dy={"up": -amt, "down": amt}.get(d, 0),
                          binding=Binding(observation_id=obs.id, app=obs.focused_app,
                                          element_id=kw.get("element_id")), raw=body)
        if name == "wait":
            return Action(kind=ActionKind.WAIT, duration_s=float(kw.get("seconds", 1)),
                          binding=b, raw=body)
        if name in ("finished", "done", "task_complete"):
            return Action(kind=ActionKind.DONE, text=kw.get("summary", ""), binding=b, raw=body)
        if name in ("failed", "give_up", "task_failed"):
            return Action(kind=ActionKind.GIVE_UP, text=kw.get("reason", ""), binding=b, raw=body)
        raise ActionError(f"unknown action {name!r}")

    # -- main entry point -------------------------------------------------

    def propose(self, ctx: TurnContext) -> Proposal:
        obs = ctx.observation
        if obs.screenshot_png is None:
            raise AdapterUnavailable("this adapter needs the screenshot channel")
        img = self._image(obs.screenshot_png, obs.geometry.pixels)

        history = "\n".join(
            f"  {t.action_json.get('kind')} -> {'ok' if t.result_ok else 'FAILED'}: "
            f"{t.result_detail[:70]}" for t in ctx.history[-4:])
        parts = [SYSTEM, f"\n## Task\n{ctx.task.goal}", f"\n{self._elements_text(obs)}"]
        if history:
            parts.append(f"\n## Recent results\n{history}")
        if ctx.notice:
            parts.append(f"\n## Note\n{ctx.notice}")
        if ctx.dialogue and ctx.dialogue[-1][1]:
            parts.append(f"\n## User said\n{ctx.dialogue[-1][1]}")
        prompt = "\n".join(parts)

        t0 = time.perf_counter()
        try:
            formatted = self._apply(self._processor, self._config, prompt, num_images=1)
            raw = self._generate(self._model, self._processor, formatted, [img],
                                 max_tokens=self.max_tokens, verbose=False)
        except Exception as exc:
            raise AdapterUnavailable(f"mlx generation failed: {exc}") from exc
        latency = time.perf_counter() - t0
        text = raw if isinstance(raw, str) else getattr(raw, "text", str(raw))
        self._usage["requests"] += 1
        self._usage["slowest_turn_s"] = max(self._usage["slowest_turn_s"], latency)

        try:
            action = self._to_action(text, obs)
        except (ActionError, ValueError, KeyError, IndexError, TypeError) as exc:
            self._usage["parse_failures"] += 1
            return Proposal(action=Action(kind=ActionKind.SCREENSHOT), raw_text=text,
                            latency_s=latency, parse_error=f"{type(exc).__name__}: {exc}")
        return Proposal(action=action, raw_text=text, latency_s=latency)

    def usage(self) -> dict:
        return dict(self._usage)
