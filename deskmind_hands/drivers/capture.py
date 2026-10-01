"""A window's pixels to a PNG, in-process with ScreenCaptureKit, `screencapture -l` when that is not possible.

`screencapture -x -o -l <id>` spawns a process and writes through the whole screenshot service for every capture;
the driver takes one per vision observation and one before and after every checked click (see
PeekabooDriver._window_digest). ScreenCaptureKit captures the same window from this process: desktop-independent
(occluded or on another display is fine), at the window's backing scale, no shadow -- what `screencapture -o -l`
writes. The shareable-content list is reused for 2 s, as trycua/cua's capture does.

Needs pyobjc-framework-ScreenCaptureKit (macOS 14+) and Screen Recording for the process. Without either, or when
a capture fails, `screencapture` is used, as before.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

_CONTENT_TTL = 2.0
_content: tuple[float, object] | None = None
_lock = threading.Lock()


def _sck():
    try:
        import ScreenCaptureKit
        return ScreenCaptureKit if hasattr(ScreenCaptureKit, "SCScreenshotManager") else None
    except ImportError:
        return None


def _wait(start, timeout: float):
    """Call an async ScreenCaptureKit API (`start(handler)`) and wait for its (result, error)."""
    done, box = threading.Event(), {}

    def handler(result, error):
        box["r"], box["e"] = result, error
        done.set()

    start(handler)
    if not done.wait(timeout) or box.get("e") is not None:
        return None
    return box.get("r")


def _window(sck, window_id: int, timeout: float):
    global _content
    with _lock:
        cached = _content
    content = cached[1] if cached and time.monotonic() - cached[0] < _CONTENT_TTL else None
    for attempt in (0, 1):
        if content is None:
            content = _wait(lambda h: sck.SCShareableContent
                            .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                                False, False, h), timeout)
            if content is None:
                return None
            with _lock:
                _content = (time.monotonic(), content)
        w = next((w for w in content.windows() if int(w.windowID()) == int(window_id)), None)
        if w is not None:
            return w
        content = None   # a window newer than the cached list: ask again once
    return None


def capture_sck(window_id: int, path: Path, timeout: float = 5.0) -> bool:
    """The window at its backing scale, without shadow, to `path` (PNG). False if it could not be done."""
    sck = _sck()
    if sck is None:
        return False
    try:
        import Quartz
        win = _window(sck, window_id, timeout)
        if win is None:
            return False
        flt = sck.SCContentFilter.alloc().initWithDesktopIndependentWindow_(win)
        scale = float(flt.pointPixelScale()) if hasattr(flt, "pointPixelScale") else 2.0
        frame = win.frame()
        cfg = sck.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(max(1, int(round(frame.size.width * scale))))
        cfg.setHeight_(max(1, int(round(frame.size.height * scale))))
        cfg.setShowsCursor_(False)
        if hasattr(cfg, "setIgnoreShadowsSingleWindow_"):
            cfg.setIgnoreShadowsSingleWindow_(True)
        image = _wait(lambda h: sck.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            flt, cfg, h), timeout)
        if image is None:
            return False
        url = Quartz.CFURLCreateWithFileSystemPath(None, str(path), Quartz.kCFURLPOSIXPathStyle, False)
        dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        if dest is None:
            return False
        Quartz.CGImageDestinationAddImage(dest, image, None)
        return bool(Quartz.CGImageDestinationFinalize(dest)) and Path(path).exists()
    except Exception:   # a private framework's surprise must not cost the step: the fallback takes it
        return False


def capture_screencapture(window_id: int, path: Path, timeout: float = 15.0) -> bool:
    p = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(window_id), str(path)],
                       capture_output=True, timeout=timeout)
    return p.returncode == 0 and Path(path).exists()


def capture_window(window_id: int | str, path: Path) -> bool:
    """`window_id`'s pixels to `path`: ScreenCaptureKit first, `screencapture -l` if that fails."""
    wid = int(window_id)
    Path(path).unlink(missing_ok=True)
    return capture_sck(wid, Path(path)) or capture_screencapture(wid, Path(path))
