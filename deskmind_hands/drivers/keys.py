"""Which key code types a letter on the user's keyboard layout, for the shortcuts the driver presses (cmd+A, cmd+V).

A key code names a physical key, and an app matches a menu shortcut by the character that key types on the current
layout. Key code 0 is A on a US board and Q on a French one, so a hard-coded cmd+A could reach the app as cmd+Q and
quit it (deskmind#21). The layout is read in a child process: asking Text Input Sources anything registers the
asking process with LaunchServices, which leaves Dock tiles behind (see PeekabooDriver._key_event)."""
from __future__ import annotations

#: ANSI key codes, what macOS itself falls back to when a layout types no Latin letter (Russian, Greek, ...).
ANSI = {"a": 0, "v": 9}
RETURN = 36


def pick_keycodes(table: dict[int, str], letters: str) -> dict[str, int]:
    """For each letter, the key code that types it with cmd held, from `table` (key code -> what it types then).

    The lowest such code when several do; the ANSI code when none does, or when the table is empty (the layout
    could not be read)."""
    out = {}
    for ch in letters:
        codes = sorted(code for code, typed in table.items() if typed.lower() == ch)
        out[ch] = codes[0] if codes else ANSI[ch]
    return out


#: Run with the driver's python: prints {"table": {code: typed with cmd}, "events": {"<code>:<1|0>": base64}} for
#: every key code, the events made by CGEventCreateKeyboardEvent so they carry what the layout gives them. The table
#: is the current keyboard layout's, or the one named by an input source id in argv[1] (tests read French that way
#: without switching the user's keyboard).
CHILD = r'''
import base64, ctypes, ctypes.util, json, sys
import Quartz as Q
table = {}
try:
    c = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Carbon"))
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    c.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
    c.TISGetInputSourceProperty.restype = ctypes.c_void_p
    c.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    c.LMGetKbdType.restype = ctypes.c_uint8
    source = c.TISCopyCurrentKeyboardLayoutInputSource()
    if len(sys.argv) > 1:
        from Foundation import NSDictionary
        import objc
        c.TISCreateInputSourceList.restype = ctypes.c_void_p
        c.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        key_id = objc.objc_object(c_void_p=ctypes.c_void_p.in_dll(c, "kTISPropertyInputSourceID").value)
        want = NSDictionary.dictionaryWithObject_forKey_(sys.argv[1], key_id)
        found = c.TISCreateInputSourceList(objc.pyobjc_id(want), True)
        source = cf.CFArrayGetValueAtIndex(found, 0) if found and cf.CFArrayGetCount(found) else None
    data = c.TISGetInputSourceProperty(source, ctypes.c_void_p.in_dll(c, "kTISPropertyUnicodeKeyLayoutData")) \
        if source else None
    layout = cf.CFDataGetBytePtr(data) if data else None
    c.UCKeyTranslate.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
                                 ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16)]
    kbd = c.LMGetKbdType()
    for code in range(128) if layout else ():
        dead, n, buf = ctypes.c_uint32(0), ctypes.c_ulong(0), (ctypes.c_uint16 * 4)()
        # kUCKeyActionDown, cmd held ((cmdKey >> 8) & 0xFF), no dead keys
        if c.UCKeyTranslate(layout, code, 0, 1, kbd, 1, ctypes.byref(dead), 4, ctypes.byref(n), buf) == 0 and n.value:
            table[code] = "".join(chr(buf[i]) for i in range(n.value))
except Exception:
    table = {}
events = {f"{k}:{int(d)}": base64.b64encode(bytes(Q.CGEventCreateData(None, Q.CGEventCreateKeyboardEvent(None, k, d))))
          .decode() for k in range(128) for d in (True, False)}
print(json.dumps({"table": table, "events": events}))
'''
