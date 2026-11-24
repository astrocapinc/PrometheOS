"""Showing text that others can write (commit subjects, branch and file names, remote URLs)
without its invisible characters, so what the owner reads is what it is (CP9 integration
review run 2, pre-check rounds 2-3).

Invisible means a control, format (bidi, zero-width), surrogate, private-use or unassigned
character, or a separator other than a plain space.
"""

from __future__ import annotations

import unicodedata

INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})


def invisible(character: str) -> bool:
    return character != " " and unicodedata.category(character) in INVISIBLE


def visible(text: str) -> str:
    """The text with every invisible character written as an escape such as ``\\u{202e}``."""
    return "".join(f"\\u{{{ord(c):x}}}" if invisible(c) else c for c in text)


def json_for_terminal(document: str) -> str:
    """JSON text with every invisible character written as a JSON ``\\uXXXX`` escape. JSON
    holds such characters only inside strings (its own layout uses newlines and plain
    spaces), so a reader decodes exactly the same value, while a terminal shows no
    control, bidi or zero-width character."""
    out: list[str] = []
    for c in document:
        if c == "\n" or not invisible(c):
            out.append(c)
            continue
        code = ord(c)
        if code > 0xFFFF:
            code -= 0x10000
            out.append(f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}")
        else:
            out.append(f"\\u{code:04x}")
    return "".join(out)
