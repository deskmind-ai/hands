"""Per-app knowledge, loaded from a file on your machine -- never shipped as constants.

A few apps need facts the accessibility tree does not carry:

  - names for toolbar icons an Electron app exposes only as a bare "button" (grounded on the screenshot, see
    grounding.py);
  - the controls of an app with no accessibility tree at all, observed through OCR and grounded icons (vision.py);
  - an app whose content sits deep inside a web view, so the tree has to be read further down;
  - a chat app the agent may act in, confined to one conversation (the peekaboo driver's chat scope);
  - which keyboard chords a given app can receive in the background (adapters/systemone.py).

Those facts are specific to your apps, your locale and your accounts, so they live in a YAML file:

    $DESKMIND_APPS             a path to the file, if set
    ~/.config/deskmind/apps.yaml   otherwise

Every key is optional and every default is empty: with no file, no app gets special treatment and the generic
mechanisms (grounding, vision elements, flash-click) stay switched off. See docs/apps-config.md for the schema and
an example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("~/.config/deskmind/apps.yaml")


@dataclass
class AppsConfig:
    #: bundle id -> {name shown to the planner: grounding query}, for unnamed buttons.
    grounding: dict[str, dict[str, str]] = field(default_factory=dict)
    #: bundle ids whose accessibility tree lives deep inside a web view.
    deep_ax: set[str] = field(default_factory=set)
    #: bundle id -> {"vocab": {name: query}, "typable": [name], "home": str | None}, for apps with no tree.
    vision: dict[str, dict] = field(default_factory=dict)
    #: The one chat app the agent may act in, and the conversation it is confined to.
    chat_bundle: str | None = None
    chat_conversation: str | None = None
    chat_placeholders: set[str] = field(default_factory=set)
    #: app display name -> chords that can be delivered to it in the background ([] routes none).
    chords: dict[str, set[str]] = field(default_factory=dict)
    source: Path | None = None

    def vision_home(self, bundle: str) -> str | None:
        return (self.vision.get(bundle.lower()) or {}).get("home") or None


def _path() -> Path:
    return Path(os.environ.get("DESKMIND_APPS") or DEFAULT_PATH).expanduser()


def load(path: Path | None = None) -> AppsConfig:
    """Read the apps file. A missing file is the empty configuration; a malformed one is an error, not a guess."""
    p = Path(path).expanduser() if path else _path()
    if not p.is_file():
        return AppsConfig()
    import yaml
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: the apps file must be a mapping")
    known = ("grounding", "deep_ax", "vision", "chat", "chords")
    unknown = [str(k) for k in raw if k not in known]
    if unknown:
        raise ValueError(f"{p}: unknown keys: {', '.join(unknown)}; known keys: {', '.join(known)}")
    chat = raw.get("chat") or {}
    vision = {}
    for bundle, spec in (raw.get("vision") or {}).items():
        spec = spec or {}
        vision[str(bundle).lower()] = {
            "vocab": {str(k): str(v) for k, v in (spec.get("vocab") or {}).items()},
            "typable": [str(x) for x in (spec.get("typable") or [])],
            "home": spec.get("home"),
        }
    return AppsConfig(
        grounding={str(b).lower(): {str(k): str(v) for k, v in (vocab or {}).items()}
                   for b, vocab in (raw.get("grounding") or {}).items()},
        deep_ax={str(b).lower() for b in (raw.get("deep_ax") or [])},
        vision=vision,
        chat_bundle=(str(chat["bundle"]).lower() if chat.get("bundle") else None),
        chat_conversation=chat.get("conversation") or None,
        chat_placeholders={str(x) for x in (chat.get("placeholders") or [])},
        chords={str(app): {str(c) for c in (chords or [])} for app, chords in (raw.get("chords") or {}).items()},
        source=p,
    )


#: Loaded once per process; the driver and the adapters read the same configuration.
APPS = load()
