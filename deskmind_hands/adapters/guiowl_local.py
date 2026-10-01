"""Local grounder adapter: GUI-Owl-1.5-4B plus an RL grounding fine-tune, via MLX.

A different checkpoint from ``mlx_local``'s UI-TARS, and different in the two ways that decide whether a local model
looks capable: it speaks Qwen3-VL's ``computer_use`` tool call rather than UI-TARS' ``click(x, y)`` dialect, and it
answers in 0-1000 normalised coordinates of the image it was shown. Both are properties of the checkpoint, measured
rather than assumed (ScreenSpot-Pro full set, 2026-09-20, on the screenshots this adapter sends):

  grounding, single pass      67.7   open-weight state of the art at this size
  grounding, two-pass zoom    77.5   KV-Ground-4B 76.4, GUI-Owl-1.5-4B base 76.2
  ScreenSpot-v2               95.0

Resolution, not prompting, dominates both accuracy and latency here. Downscaling a 4K screen to 1M pixels puts 94%
of targets below one visual token and costs 35 points (33.0 vs 67.7); a coarse pass at 2M pixels followed by a
second pass on a 0.5 crop restored to full detail measured 80.0 on a 100-sample subset. Prefill is ~2.3 s per
million pixels on an M4 Pro, so the two passes cost about twice one pass, not four times.

Set ``zoom: 0`` to compare against the single-pass configuration.
"""

from __future__ import annotations

import io
import json
import re
import time

from ..actions import Action, ActionError, ActionKind, Binding, normalise_keys
from ..geometry import CoordSpace, Point, Size
from .base import AdapterUnavailable, Proposal, TurnContext

#: The model's own tool, trimmed to the actions this harness executes. The full GUI-Owl agent prompt is 974 tokens
#: and measured no better on one Pro screenshot (identical click, 9.4 s vs 5.8 s), so the short one is the default.
SYSTEM = (
    '# Tools\n\nYou may call one function to control the computer.\n<tools>\n'
    '{"type": "function", "function": {"name": "computer_use", "description": "Control a computer with mouse and '
    'keyboard. The screen resolution is 1000x1000. Click element centers.", "parameters": {"properties": '
    '{"action": {"enum": ["left_click", "double_click", "right_click", "type", "key", "scroll", "wait", '
    '"terminate"], "type": "string"}, "coordinate": {"description": "(x, y) in 0-1000.", "type": "array"}, '
    '"text": {"description": "for type.", "type": "string"}, "keys": {"description": "for key.", "type": "array"}, '
    '"pixels": {"description": "for scroll, positive scrolls up.", "type": "number"}, '
    '"status": {"enum": ["success", "failure"], "type": "string"}}, "required": ["action"], "type": "object"}}}\n'
    '</tools>\n\nReply with only one <tool_call>{"name": "computer_use", "arguments": {...}}</tool_call>. '
    'When the task is already complete, use action=terminate with status=success.'
)

MAX_ELEMENTS = 40
_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_ARGS_RE = re.compile(r'"arguments"\s*:\s*(\{.*?\})\s*\}?\s*$', re.S)


def _arguments(text: str) -> dict:
    """The tool call's arguments. The model sometimes omits the "name" key, which makes the wrapper invalid JSON."""
    m = _TOOL_RE.search(text) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise ActionError(f"no tool call in {text[:80]!r}")
    body = m.group(1)
    try:
        obj = json.loads(body)
        return obj.get("arguments", obj)
    except json.JSONDecodeError:
        pass
    inner = _ARGS_RE.search(body)
    if inner:
        try:
            return json.loads(inner.group(1))
        except json.JSONDecodeError:
            pass
    raise ActionError(f"unparseable tool call: {body[:80]!r}")


class GuiOwlLocalAdapter:
    """Vision-only local adapter. Sees the screenshot and the element list; answers with one tool call."""

    name = "guiowl"

    def __init__(self, model: str | None = None, *, coarse_pixels: int = 2_007_040,
                 crop_pixels: int = 2_007_040, zoom: float = 0.5, max_tokens: int = 96) -> None:
        try:
            from mlx_vlm import generate, load
            from mlx_vlm.prompt_utils import apply_chat_template
        except ImportError as exc:
            raise AdapterUnavailable("mlx-vlm is not importable; run from the project venv") from exc
        self.model_id = model or "~/models/guiowl-ours-mlx4"
        from pathlib import Path

        self.model_id = str(Path(self.model_id).expanduser())
        self.coarse_pixels, self.crop_pixels, self.zoom = coarse_pixels, crop_pixels, zoom
        self.max_tokens = max_tokens
        self.coord_space = CoordSpace.NORM_1000  # of the full screenshot: the zoom pass is mapped back here
        self._generate, self._apply = generate, apply_chat_template
        try:
            self._model, self._processor = load(self.model_id)
        except Exception as exc:
            raise AdapterUnavailable(f"cannot load {self.model_id}: {exc}") from exc
        self._config = getattr(self._model, "config", None)
        self._usage = {"requests": 0, "zoom_passes": 0, "parse_failures": 0, "slowest_turn_s": 0.0}

    # -- images -----------------------------------------------------------

    @staticmethod
    def _budget(img, max_pixels: int):
        w, h = img.size
        if w * h <= max_pixels:
            return img
        from PIL import Image

        s = (max_pixels / (w * h)) ** 0.5
        return img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)

    def _crop(self, full, point: tuple[float, float]):
        """Crop `zoom` of W/H around a 0-1 point, restored to the full size so the target regains its detail."""
        from PIL import Image

        W, H = full.size
        w, h = int(W * self.zoom), int(H * self.zoom)
        x, y = point[0] * W, point[1] * H
        box = (max(0, int(x - w // 2)), max(0, int(y - h // 2)), min(W, int(x + w // 2)), min(H, int(y + h // 2)))
        crop = full.crop(box).resize((W, H), Image.LANCZOS)
        return self._budget(crop, self.crop_pixels), box, (W, H)

    # -- prompt -----------------------------------------------------------

    def _elements_text(self, obs) -> str:
        rows = []
        for e in obs.elements:
            if not (e.label or e.settable):
                continue
            rows.append(f"  {e.id}  {(e.label or e.ax_role)[:30]}{' [text field]' if e.settable else ''}")
            if len(rows) >= MAX_ELEMENTS:
                break
        return "Elements:\n" + ("\n".join(rows) if rows else "  (none)")

    def _prompt(self, ctx: TurnContext) -> str:
        obs = ctx.observation
        parts = [f"Task: {ctx.task.goal}"]
        if "ax" in ctx.channels:
            parts.append(self._elements_text(obs))
        # What is focused decides whether this model types or clicks first: told the field is focused it emits
        # `type`, told nothing it clicks the field again and the run stalls.
        focused = next((e for e in obs.elements if getattr(e, "focused", False)), None)
        if focused is not None:
            parts.append(f"Focused right now: {focused.label or focused.ax_role}")
        done = [f"{i}. {t.action_json.get('kind')} -> {'ok' if t.result_ok else 'FAILED'}"
                for i, t in enumerate(ctx.history[-5:], 1)]
        if done:
            parts.append("Already done (do not repeat):\n" + "\n".join(done) + "\nDo the next step.")
        if ctx.notice:
            parts.append(f"Note: {ctx.notice}")
        if ctx.dialogue and ctx.dialogue[-1][1]:
            parts.append(f"User said: {ctx.dialogue[-1][1]}")
        return "\n\n".join(parts)

    def _ask(self, image, prompt: str) -> str:
        chat = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        formatted = self._apply(self._processor, self._config, chat, num_images=1)
        raw = self._generate(self._model, self._processor, formatted, [image],
                             max_tokens=self.max_tokens, verbose=False)
        return raw if isinstance(raw, str) else getattr(raw, "text", str(raw))

    # -- parsing ----------------------------------------------------------

    def _to_action(self, args: dict, obs, point: tuple[float, float] | None) -> Action:
        kind = str(args.get("action", "")).lower()
        b = Binding(observation_id=obs.id, app=obs.focused_app)
        pt = Point(point[0] * 1000, point[1] * 1000) if point else None
        clicks = {"left_click": ActionKind.CLICK, "click": ActionKind.CLICK,
                  "double_click": ActionKind.DOUBLE_CLICK, "doubleclick": ActionKind.DOUBLE_CLICK,
                  "right_click": ActionKind.RIGHT_CLICK, "middle_click": ActionKind.CLICK,
                  "mouse_move": ActionKind.CLICK}  # the checkpoint shortens names when the prompt is short
        if kind in clicks:
            if pt is None:
                raise ActionError(f"{kind} without a coordinate")
            return Action(kind=clicks[kind], point=pt, coord_space=self.coord_space, binding=b, raw=args)
        if kind == "type":
            return Action(kind=ActionKind.TYPE_TEXT, text=args.get("text", ""), foreground=True,
                          binding=b, raw=args)
        if kind in ("key", "hotkey"):
            keys = args.get("keys") or []
            return Action(kind=ActionKind.KEY, keys=normalise_keys("+".join(keys) if isinstance(keys, list) else keys),
                          binding=b, raw=args)
        if kind in ("scroll", "hscroll"):
            amount = int(float(args.get("pixels", 0)) / 100) or -3
            return Action(kind=ActionKind.SCROLL, scroll_dy=-amount if kind == "scroll" else 0,
                          scroll_dx=amount if kind == "hscroll" else 0, binding=b, raw=args)
        if kind == "wait":
            return Action(kind=ActionKind.WAIT, duration_s=min(float(args.get("time", 1)), 10), binding=b, raw=args)
        if kind == "terminate":
            done = str(args.get("status", "success")).lower() == "success"
            return Action(kind=ActionKind.DONE if done else ActionKind.GIVE_UP, text=str(args.get("status", "")),
                          binding=b, raw=args)
        if kind in ("answer", "interact"):
            return Action(kind=ActionKind.ASK_USER, text=args.get("text", ""), binding=b, raw=args)
        raise ActionError(f"unknown action {kind!r}")

    # -- main entry point -------------------------------------------------

    def propose(self, ctx: TurnContext) -> Proposal:
        obs = ctx.observation
        if obs.screenshot_png is None:
            raise AdapterUnavailable("this adapter needs the screenshot channel")
        from PIL import Image

        full = Image.open(io.BytesIO(obs.screenshot_png)).convert("RGB")
        prompt = self._prompt(ctx)
        t0 = time.perf_counter()
        try:
            text = self._ask(self._budget(full, self.coarse_pixels), prompt)
            args = _arguments(text)
            point = None
            if args.get("coordinate"):
                c = args["coordinate"]
                point = (float(c[0]) / 1000, float(c[1]) / 1000)
                if self.zoom and 0 <= point[0] <= 1 and 0 <= point[1] <= 1:
                    crop, box, (W, H) = self._crop(full, point)
                    text2 = self._ask(crop, prompt)
                    self._usage["zoom_passes"] += 1
                    args2 = _arguments(text2)
                    if args2.get("coordinate"):  # map the refined point back onto the full screenshot
                        c2 = args2["coordinate"]
                        point = ((box[0] + float(c2[0]) / 1000 * (box[2] - box[0])) / W,
                                 (box[1] + float(c2[1]) / 1000 * (box[3] - box[1])) / H)
                        args, text = args2, text2
            action = self._to_action(args, obs, point)
        except (ActionError, ValueError, KeyError, IndexError, TypeError) as exc:
            self._usage["parse_failures"] += 1
            return Proposal(action=Action(kind=ActionKind.SCREENSHOT), raw_text=locals().get("text", ""),
                            latency_s=time.perf_counter() - t0, parse_error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # model or MLX failure: the run should stop, not score a wrong action
            raise AdapterUnavailable(f"mlx generation failed: {exc}") from exc
        latency = time.perf_counter() - t0
        self._usage["requests"] += 1
        self._usage["slowest_turn_s"] = max(self._usage["slowest_turn_s"], latency)
        return Proposal(action=action, raw_text=text, latency_s=latency)

    def usage(self) -> dict:
        return dict(self._usage)
