"""The hub stylesheet is a flat list of rules.

Regression: the CP6 rebase onto CP9 dropped the closing brace of `.tex-overleaf`, so every
later rule (the CP6 audio styles among them) compiled nested under it and never applied.
"""

from pathlib import Path

STYLES = Path(__file__).resolve().parents[1] / "hub" / "src" / "styles.css"


def _strip_comments_and_strings(text: str) -> str:
    out = []
    i = 0
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_braces_balance_and_only_at_rules_nest() -> None:
    text = _strip_comments_and_strings(STYLES.read_text(encoding="utf-8"))
    depth = 0
    stack: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "{":
            prelude = text[start:i].strip().split(";")[-1].strip()
            if depth > 0:
                assert stack[-1].startswith("@"), (
                    f"rule {prelude!r} is nested inside {stack[-1]!r}; a closing brace is missing"
                )
            stack.append(prelude)
            depth += 1
            start = i + 1
        elif ch == "}":
            assert depth > 0, "unbalanced closing brace"
            stack.pop()
            depth -= 1
            start = i + 1
        elif ch == ";":
            start = i + 1
    assert depth == 0, f"unclosed block: {stack[-1]!r}"
