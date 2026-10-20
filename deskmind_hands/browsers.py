"""Which apps are web browsers: by what they register, not by a list of names.

A browser is an app the system offers for opening https: links (NSWorkspace URLsForApplicationsToOpenURL) that
also declares the http, https and file URL schemes in its Info.plist (CFBundleURLTypes) -- it shows web pages and
local pages. "Opens https:" alone takes in a terminal and a coding app that hand links on (10-20, iTerm2 and Codex
on the review's Mac; neither declares file:). Read once per process. `BrowserSet` keeps the driver's known list for
what it already did by it, and answers "in" for any app found this way as well (hands #54 review: the list was a
guard, and a browser not on it passed it).
"""
from __future__ import annotations

_BROWSERS: set[str] | None = None


def is_browser(bundle: str) -> bool:
    global _BROWSERS
    if _BROWSERS is None:
        found: set[str] = set()
        try:
            from AppKit import NSBundle, NSURL, NSWorkspace
            for u in NSWorkspace.sharedWorkspace().URLsForApplicationsToOpenURL_(NSURL.URLWithString_("https://example.com/")) or []:
                b = NSBundle.bundleWithURL_(u)
                if b is None or not b.bundleIdentifier():
                    continue
                schemes = {str(x).lower() for t in (b.objectForInfoDictionaryKey_("CFBundleURLTypes") or [])
                           for x in (t.get("CFBundleURLSchemes") or [])}
                if {"http", "https", "file"} <= schemes:
                    found.add(str(b.bundleIdentifier()).lower())
        except Exception:  # noqa: BLE001 -- not macOS: none known
            pass
        _BROWSERS = found
    return (bundle or "").lower() in _BROWSERS


class BrowserSet(frozenset):
    """The known browsers' bundle ids (lowercased), where `in` also holds for any app that registers https:."""

    def __contains__(self, bundle) -> bool:
        return frozenset.__contains__(self, bundle) or (isinstance(bundle, str) and is_browser(bundle))
