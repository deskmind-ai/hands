"""Which key code types a letter on the user's keyboard layout, for the shortcuts the driver presses (cmd+A, cmd+V).

A key code names a physical key, and an app matches a menu shortcut by the character that key types on the current
layout. Key code 0 is A on a US board and Q on a French one, so a hard-coded cmd+A could reach the app as cmd+Q and
quit it (deskmind#21). The layout is read in a child process: asking Text Input Sources anything registers the
asking process with LaunchServices, which leaves Dock tiles behind (see PeekabooDriver._key_event)."""
from __future__ import annotations

#: ANSI key codes, what macOS itself falls back to when a layout types no Latin letter (Russian, Greek, ...).
ANSI = {"a": 0, "v": 9}
RETURN = 36
TAB = 48

#: US ANSI key codes of the keys a chord names by their character: the fallback when the layout cannot be read.
US = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
      "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
      "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
      "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50}
#: Keys a chord names by name, the same on every layout.
NAMED = {"return": RETURN, "enter": RETURN, "tab": TAB, "space": 49, "delete": 51, "backspace": 51, "escape": 53,
         "esc": 53, "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123,
         "right": 124, "down": 125, "up": 126}
#: Modifier names -> CGEventFlags (shift, control, option, command).
MODIFIERS = {"shift": 1 << 17, "ctrl": 1 << 18, "control": 1 << 18, "option": 1 << 19, "alt": 1 << 19,
             "opt": 1 << 19, "cmd": 1 << 20, "command": 1 << 20}


def parse_chord(chord: str, table: dict[int, str] | None = None) -> tuple[int, int] | None:
    """("cmd+shift+n", layout table) -> (key code, CGEventFlags), or None when it names no single key. A character
    key is found on the user's layout (`table`, as for pick_keycodes), else at its US position."""
    flags, key = 0, None
    for part in (p.strip().lower() for p in chord.split("+")):
        if not part:
            continue
        if part in MODIFIERS:
            flags |= MODIFIERS[part]
        elif key is None:
            key = part
        else:
            return None
    if key is None:
        return None
    if key in NAMED:
        return NAMED[key], flags
    if len(key) != 1:
        return None
    codes = sorted(code for code, typed in (table or {}).items() if typed.lower() == key)
    if codes:
        return codes[0], flags
    return (US[key], flags) if key in US else None


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


#: Run with the driver's python: prints {"ascii": the current keyboard input source types ASCII by itself, "id": its
#: id (for the trace only)}. Asked of Text Input Sources by its ASCII-capable property, never by name; in a child for
#: the same reason as CHILD. Anything that fails prints nothing, which the caller reads as "not ASCII".
INPUT_SOURCE_CHILD = r'''
import ctypes, ctypes.util, json
c = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Carbon"))
cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
c.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
c.TISGetInputSourceProperty.restype = ctypes.c_void_p
c.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
cf.CFBooleanGetValue.restype, cf.CFBooleanGetValue.argtypes = ctypes.c_bool, [ctypes.c_void_p]
cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
src = c.TISCopyCurrentKeyboardInputSource()
v = c.TISGetInputSourceProperty(src, ctypes.c_void_p.in_dll(c, "kTISPropertyInputSourceIsASCIICapable").value)
i = c.TISGetInputSourceProperty(src, ctypes.c_void_p.in_dll(c, "kTISPropertyInputSourceID").value)
buf = ctypes.create_string_buffer(256)
print(json.dumps({"ascii": bool(v) and cf.CFBooleanGetValue(v),
                  "id": buf.value.decode() if i and cf.CFStringGetCString(i, buf, 256, 0x08000100) else ""}))
'''
