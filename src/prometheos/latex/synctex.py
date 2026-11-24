"""SyncTeX lookups between workspace source lines and PDF positions (decision 0012 item 9).

The stored SyncTeX map is the engine's ``.synctex`` text (version 1), decompressed, with
the staging directory removed from its ``Input:`` lines so paths are workspace-relative.
Positions are PDF points measured from the top-left corner of the page, the coordinate
system PDF.js viewers use after scaling.

Record forms (TeX scaled points, times ``Unit``):
``[tag,line:x,y:W,H,D`` and ``(tag,line:x,y:W,H,D`` open vertical/horizontal boxes,
``h``/``v`` are void boxes with the same fields, ``x``, ``g``, ``$`` are points
``tag,line:x,y`` and ``k`` is a kern ``tag,line:x,y:W``. ``{n`` and ``}n`` delimit page n.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SP_PER_BP = 65781.76  # 65536 sp per TeX point * 72.27 / 72
MAX_RECORDS = 2_000_000

_BOX = re.compile(r"^([\[(hv])(\d+),(-?\d+):(-?\d+),(-?\d+):(-?\d+),(-?\d+),(-?\d+)$")
_POINT = re.compile(r"^([xg$])(\d+),(-?\d+):(-?\d+),(-?\d+)$")
_KERN = re.compile(r"^(k)(\d+),(-?\d+):(-?\d+),(-?\d+):(-?\d+)$")


@dataclass(frozen=True)
class Node:
    page: int
    kind: str
    tag: int
    line: int
    x: float
    y: float
    width: float = 0.0
    height: float = 0.0
    depth: float = 0.0

    @property
    def is_box(self) -> bool:
        return self.kind in "[(hv"

    def contains(self, x: float, y: float) -> bool:
        return (
            self.is_box
            and self.x <= x <= self.x + self.width
            and self.y - self.height <= y <= self.y + self.depth
        )

    def area(self) -> float:
        return max(self.width, 0.0) * max(self.height + self.depth, 0.0)


@dataclass
class SyncMap:
    inputs: dict[int, str] = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)
    pages: int = 0


def normalize_inputs(text: str, staging_root: str) -> str:
    """Rewrite ``Input:tag:path`` lines to workspace-relative paths (or ``outside:name``)."""
    from prometheos.latex.diagnostics import workspace_path

    out = []
    for line in text.split("\n"):
        if line.startswith("Input:"):
            _, tag, name = line.split(":", 2)
            if name.strip():
                relative = workspace_path(name, staging_root)
                base = name.replace("\\", "/").split("/")[-1]
                line = f"Input:{tag}:{relative if relative else 'outside:' + base}"
        out.append(line)
    return "\n".join(out)


def parse(text: str) -> SyncMap:
    result = SyncMap()
    unit = 1.0
    magnification = 1000.0
    x_offset = y_offset = 0.0
    page = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Input:"):
            _, tag, name = line.split(":", 2)
            result.inputs[int(tag)] = name
            continue
        if line.startswith("Unit:"):
            unit = float(line[5:])
            continue
        if line.startswith("Magnification:"):
            magnification = float(line[14:])
            continue
        if line.startswith("X Offset:"):
            x_offset = float(line[9:])
            continue
        if line.startswith("Y Offset:"):
            y_offset = float(line[9:])
            continue
        if line[0] == "{" and line[1:].isdigit():
            page = int(line[1:])
            result.pages = max(result.pages, page)
            continue
        if page == 0 or len(result.nodes) >= MAX_RECORDS:
            continue
        scale = unit * magnification / 1000.0 / SP_PER_BP
        box = _BOX.match(line)
        if box:
            kind, tag, src, x, y, w, h, d = box.groups()
            result.nodes.append(
                Node(
                    page,
                    kind,
                    int(tag),
                    int(src),
                    int(x) * scale + x_offset,
                    int(y) * scale + y_offset,
                    int(w) * scale,
                    int(h) * scale,
                    int(d) * scale,
                )
            )
            continue
        point = _POINT.match(line) or _KERN.match(line)
        if point:
            groups = point.groups()
            width = int(groups[5]) * scale if len(groups) > 5 else 0.0
            result.nodes.append(
                Node(
                    page,
                    groups[0],
                    int(groups[1]),
                    int(groups[2]),
                    int(groups[3]) * scale + x_offset,
                    int(groups[4]) * scale + y_offset,
                    width,
                )
            )
    return result


def _norm(name: str) -> str:
    value = name.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.casefold()


def _tag_for(sync: SyncMap, file: str) -> int | None:
    wanted = _norm(file)
    for tag, name in sync.inputs.items():
        if _norm(name) == wanted:
            return tag
    return None


def forward(sync: SyncMap, file: str, line: int) -> dict[str, Any] | None:
    """Source line to PDF: page and highlight boxes (nearest following line if needed)."""
    tag = _tag_for(sync, file)
    if tag is None:
        return None
    lines = sorted({n.line for n in sync.nodes if n.tag == tag and n.line > 0})
    if not lines:
        return None
    candidates = [v for v in lines if v >= line] or [lines[-1]]
    chosen = candidates[0]
    hits = [n for n in sync.nodes if n.tag == tag and n.line == chosen]
    page = min(n.page for n in hits)
    on_page = [n for n in hits if n.page == page]
    # One band per typeset baseline carrying this source line: from the leftmost to the
    # rightmost node on that baseline, as tall as its boxes (or a text line).
    bands: dict[float, dict[str, float]] = {}
    for node in on_page:
        if node.kind == "[" or (node.is_box and node.width <= 0 and node.kind != "h"):
            continue
        key = round(node.y, 1)
        right = node.x + max(node.width, 0.0)
        above = node.height if node.is_box and node.height > 0 else 8.0
        below = node.depth if node.is_box and node.depth > 0 else 2.5
        band = bands.setdefault(
            key, {"left": node.x, "right": right, "above": above, "below": below}
        )
        band["left"] = min(band["left"], node.x)
        band["right"] = max(band["right"], right)
        band["above"] = max(band["above"], above)
        band["below"] = max(band["below"], below)
    rects = [
        {
            "x": round(b["left"], 2),
            "y": round(y - b["above"], 2),
            "width": round(max(b["right"] - b["left"], 12.0), 2),
            "height": round(b["above"] + b["below"], 2),
        }
        for y, b in sorted(bands.items())
    ]
    return {"page": page, "line": chosen, "file": sync.inputs[tag], "rects": rects[:50]}


def inverse(sync: SyncMap, page: int, x: float, y: float) -> dict[str, Any] | None:
    """PDF position to source: the innermost box under the point, then its nearest node."""
    nodes = [n for n in sync.nodes if n.page == page and n.line > 0 and n.tag in sync.inputs]
    if not nodes:
        return None
    containing = [n for n in nodes if n.contains(x, y)]
    if containing:
        box = min(containing, key=lambda n: n.area())
        inside = [
            n
            for n in nodes
            if not n.is_box
            and box.x <= n.x <= box.x + box.width
            and box.y - box.height <= n.y <= box.y + box.depth
        ]
        best = min(inside, key=lambda n: abs(n.x - x) + abs(n.y - y)) if inside else box
    else:
        best = min(nodes, key=lambda n: abs(n.x - x) + abs(n.y - y) * 2)
    name = sync.inputs[best.tag]
    if name.startswith("outside:"):
        return None
    return {"file": name, "line": best.line}
