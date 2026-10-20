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
#: The installed browsers, [{bundle, names}], from the same scan; read once per process.
_INSTALLED: list[dict] | None = None


def _scan() -> tuple[set[str], list[dict]]:
    """The apps that pass the structural check, as lowercased bundle ids and as [{bundle, names}]: every name the
    user may call one by -- its localized display name, CFBundleName (localized and not) and its .app file name --
    each once (case aside), merged across copies of one bundle id."""
    found: set[str] = set()
    by_bundle: dict[str, dict] = {}
    try:
        from AppKit import NSBundle, NSFileManager, NSURL, NSWorkspace
        for u in NSWorkspace.sharedWorkspace().URLsForApplicationsToOpenURL_(NSURL.URLWithString_("https://example.com/")) or []:
            b = NSBundle.bundleWithURL_(u)
            if b is None or not b.bundleIdentifier():
                continue
            schemes = {str(x).lower() for t in (b.objectForInfoDictionaryKey_("CFBundleURLTypes") or [])
                       for x in (t.get("CFBundleURLSchemes") or [])}
            if not {"http", "https", "file"} <= schemes:
                continue
            bid = str(b.bundleIdentifier())
            found.add(bid.lower())
            entry = by_bundle.setdefault(bid.lower(), {"bundle": bid, "names": []})
            loc = b.localizedInfoDictionary() or {}
            path = str(u.path() or "")
            cands = [NSFileManager.defaultManager().displayNameAtPath_(path) if path else None,
                     loc.get("CFBundleDisplayName"), loc.get("CFBundleName"),
                     b.objectForInfoDictionaryKey_("CFBundleDisplayName"), b.objectForInfoDictionaryKey_("CFBundleName"),
                     str(u.lastPathComponent() or "")]
            for n in cands:
                n = str(n or "").strip()
                if n.lower().endswith(".app"):
                    n = n[:-4]
                if n and n.casefold() not in {x.casefold() for x in entry["names"]}:
                    entry["names"].append(n)
    except Exception:  # noqa: BLE001 -- not macOS: none known
        pass
    return found, sorted(by_bundle.values(), key=lambda e: e["bundle"].lower())


def is_browser(bundle: str) -> bool:
    global _BROWSERS, _INSTALLED
    if _BROWSERS is None:
        found, installed = _scan()
        _BROWSERS = found
        if _INSTALLED is None:
            _INSTALLED = installed
    return (bundle or "").lower() in _BROWSERS


def installed_browsers() -> list[dict]:
    """[{bundle, names}] of the browsers installed on this Mac, whether running or not: what a name the user or the
    model uses ("Chrome") may mean, when no window of it is open. Read once per process."""
    global _BROWSERS, _INSTALLED
    if _INSTALLED is None:
        found, installed = _scan()
        _INSTALLED = installed
        if _BROWSERS is None:
            _BROWSERS = found
    return [{"bundle": e["bundle"], "names": list(e["names"])} for e in _INSTALLED]


class BrowserSet(frozenset):
    """The known browsers' bundle ids (lowercased), where `in` also holds for any app that registers https:."""

    def __contains__(self, bundle) -> bool:
        return frozenset.__contains__(self, bundle) or (isinstance(bundle, str) and is_browser(bundle))
