"""The window's document, read generically: the parsing and the once-only ask of an app that does not answer."""
from __future__ import annotations

from deskmind_hands import axdocument as ad


def test_document_values_become_posix_paths():
    assert ad._posix("file:///Users/me/a%20b.txt") == "/Users/me/a b.txt"
    assert ad._posix("/Users/me/a.txt") == "/Users/me/a.txt"
    assert ad._posix("Macintosh HD:Users:me:a.txt") == "/Users/me/a.txt"
    assert ad._posix("") is None and ad._posix("https://example.com/") is None


def test_the_window_is_picked_by_title_then_position():
    rows = ("notes.txt\tfile:///ws/one/notes.txt\t10\t20\ttrue\n"
            "notes.txt\tfile:///ws/two/notes.txt\t400\t20\tfalse\n"
            "未命名\t\t0\t0\ttrue\n"
            "ghost\t\t0\t0\t\n")
    assert ad._pick(rows, "notes.txt", [400, 20, 500, 300]) == ("/ws/two/notes.txt", False)
    assert ad._pick(rows, "notes.txt", None) == ("/ws/one/notes.txt", True)
    assert ad._pick(rows, "未命名", None) == (None, True)     # never saved: no path, unsaved changes
    assert ad._pick(rows, "ghost", None) is None              # neither known: not a document window
    assert ad._pick(rows, "other", None) is None
    # accessibility rows carry no modified column: the path alone
    assert ad._pick("a.txt\tfile:///ws/a.txt\t1\t2\n", "a.txt", None) == ("/ws/a.txt", None)


def test_an_app_whose_scripting_hangs_is_asked_once(monkeypatch):
    calls = []

    def osa(script):
        calls.append(script)
        return "" if "System Events" in script else None   # AX: nothing; scripting: timed out
    monkeypatch.setattr(ad, "_osa", osa)
    ad._NO_SCRIPTING.discard("com.example.hung")
    assert ad.window_document(1, "com.example.hung", "x", None) is None
    assert ad.window_document(1, "com.example.hung", "x", None) is None
    assert sum("application id" in s for s in calls) == 1     # scripting asked the first time only
    assert sum("System Events" in s for s in calls) == 2      # accessibility is cheap and asked each time
    ad._NO_SCRIPTING.discard("com.example.hung")


def test_an_app_that_answers_with_nothing_is_asked_again(monkeypatch):
    calls = []

    def osa(script):
        calls.append(script)
        return ""
    monkeypatch.setattr(ad, "_osa", osa)
    for _ in range(2):
        assert ad.window_document(1, "com.example.doc", "未命名", None) is None
    assert sum("application id" in s for s in calls) == 2
