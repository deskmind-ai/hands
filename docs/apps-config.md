# App configuration · [中文](apps-config.zh-CN.md)

Per-app knowledge lives in a YAML file on your machine and is read once per process by `deskmind_hands/apps.py`:

1. the path in `$DESKMIND_APPS`, if set;
2. otherwise `~/.config/deskmind/apps.yaml`.

Every key is optional, and every default is empty. With no file, no app gets special treatment.

```yaml
# ~/.config/deskmind/apps.yaml

# Icon-only buttons (an Electron app's toolbar reads as a row of bare "button"s). For each app: the name the planner
# will see -> a description a grounding model can find on the screenshot.
grounding:
  com.example.chat:
    Send: send message button in the message input toolbar
    Emoji: emoji (smiley face) button in the message input toolbar
    Attach: attach / upload file (paperclip) button in the message input toolbar

# Apps whose accessibility tree lives deep inside a web view: read further down (depth 60 instead of 12).
deep_ax: [com.example.chat]

# Apps with no usable accessibility tree at all, observed through on-device OCR and grounded icons.
vision:
  com.example.player:
    vocab:
      Search: search input box at the top of the window
      Play/Pause: play / pause button in the player bar at the bottom
      Next: next track button in the player bar at the bottom
    typable: [Search]            # elements text is typed into
    home: Example Player         # OCR text of the logo that goes to the home page; clicked at the start of a run

# One chat app the agent may act in, confined to one conversation. Anything else on screen (other chats, their
# previews) is cut from the state before the planner sees it, and the run stops if the conversation is not open.
chat:
  bundle: com.example.chat
  conversation: My test group    # the exact title in the conversation header
  placeholders: ["Type a message"]  # what the empty message box reads back as

# Chords that can be delivered to an app in the background, by the app's display name. [] routes none, so the
# planner is never offered a chord that would land wherever the app's focus happens to be.
chords:
  Example Chat: []
  Example Player: []
```

## Related switches

| Variable | Effect |
|---|---|
| `HANDS_GROUNDER_URL` | the local grounding server (default `http://127.0.0.1:8010/ground`); the screenshot is passed by path and never leaves the machine |
| `HANDS_FLASH_APPS` | comma-separated bundle ids allowed a short foreground flash (vision apps) |
| `HANDS_CHAT_FLASH=1` | allow the flash-click for the configured chat app; at most one message is sent per run |
| `HANDS_PROJECTION=0` | switch off the projection layer |
| `HANDS_PEEKABOO_TRANSPORT` | `mcp` (one session per run, background) or `cli` |

The grounding server takes `{"image": <path>, "size": [w, h], "queries": {name: description}}` and answers
`{"points": {name: [x, y] | null}}` with coordinates relative to the image (0–1). Any local model that can do that
works; [DeskMind Eyes](https://github.com/deskmind-ai/eyes) is ours.

OCR for vision apps uses macOS Vision on the device. Build the helper once:

```bash
swiftc -O tools/native/ocr.swift -o tools/native/ocr
```
