"""``kct pcb diff OLD NEW`` -- geometric board diff (issue #6417).

Each side is a ``.kicad_pcb`` path or a ``REV:path`` git spec, resolved by
:func:`kicad_tools.cli.check_diff.resolve_side` (the same loader as
``kct check --diff``).  Matching lives in :mod:`kicad_tools.pcb.diff`; this
module is the glue: filters, text and JSON rendering, the SVG overlay and the
exit code (``0`` nothing differs, ``2`` something does, ``1`` a side could not
be loaded).
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from kicad_tools.cli.check_diff import DiffSideError, resolve_side
from kicad_tools.cli.format_options import stdout_to_stderr_when
from kicad_tools.pcb.diff import (
    STATUSES,
    DiffEntry,
    DiffItem,
    DiffResult,
    diff_pcbs,
    to_json,
)

__all__ = ["run_pcb_diff", "write_svg"]

#: Changed items listed in the text report before the ``... N more`` tail.
TEXT_ITEM_LIMIT = 50


def _side_file(spec: str) -> str:
    """The board path a side names: the spec itself, or a ``REV:path``'s path."""
    if Path(spec).exists():
        return spec
    _rev, sep, rel = spec.partition(":")
    return rel if sep and rel else spec


def run_pcb_diff(
    old_spec: str,
    new_spec: str,
    *,
    fmt: str = "text",
    nets: Sequence[str] = (),
    layers: Sequence[str] = (),
    svg: str | None = None,
) -> int:
    """Diff two boards and report; returns the process exit code."""
    from kicad_tools.schema.pcb import PCB

    boards: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="kct-pcb-diff-") as tmp:
        scratch = Path(tmp)
        for label, spec in (("old", old_spec), ("new", new_spec)):
            try:
                path = resolve_side(spec, scratch, label)
            except DiffSideError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            if path.is_dir():
                print(f"Error: {label}: {spec!r} is a directory, not a .kicad_pcb", file=sys.stderr)
                return 1
            # Loader warnings must not corrupt a ``--format json`` document.
            with stdout_to_stderr_when(fmt == "json"):
                try:
                    boards[label] = PCB.load(path)
                except Exception as e:  # noqa: BLE001 - any parse failure is a load error
                    print(f"Error: {label}: cannot load {spec}: {e}", file=sys.stderr)
                    return 1

    full = diff_pcbs(boards["old"], boards["new"])
    shown = full.filtered(nets, layers)

    if svg:
        try:
            write_svg(full, shown, Path(svg))
        except OSError as e:
            print(f"Error: cannot write {svg}: {e}", file=sys.stderr)
            return 1
        print(f"[INFO] wrote SVG overlay to {svg}", file=sys.stderr)

    sides = {
        "old": {"spec": old_spec, "file": _side_file(old_spec)},
        "new": {"spec": new_spec, "file": _side_file(new_spec)},
    }
    if fmt == "json":
        doc = to_json(
            shown,
            old=sides["old"],
            new=sides["new"],
            filters={"net": sorted(set(nets)), "layer": sorted(set(layers))},
        )
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        _print_text(shown, sides, nets, layers)

    return 2 if shown.has_changes else 0


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _fmt_pt(p: Sequence[float] | None) -> str:
    if p is None:
        return "-"
    return f"({p[0]:g}, {p[1]:g})"


def _describe(item: DiffItem | None) -> str:
    """A one-line geometric description of one side of an entry."""
    if item is None:
        return "-"
    g = item.geometry
    kind = item.kind
    if kind == "segment":
        return f"{_fmt_pt(g['start'])}->{_fmt_pt(g['end'])} w={g['width']:g}"
    if kind == "arc":
        return f"{_fmt_pt(g['start'])}~{_fmt_pt(g['mid'])}~{_fmt_pt(g['end'])} w={g['width']:g}"
    if kind == "via":
        return f"@{_fmt_pt(g['position'])} {g['size']:g}/{g['drill']:g} {'-'.join(g['layers'])}"
    if kind == "footprint":
        return f"@{_fmt_pt(g['position'])} rot={g['rotation']:g} {g['layer']} {g['lib']}"
    if kind == "pad":
        return f"@{_fmt_pt(g['position'])} {g['shape']} {g['size'][0]:g}x{g['size'][1]:g}"
    if kind == "zone":
        return f"{g['vertices']} pts prio={g['priority']} {','.join(g['layers'])}"
    if kind == "zone_fill":
        return f"{g['polygons']} polys, {g['vertices']} vertices"
    if kind == "graphic":
        return f"{g['type']} {g['layer']} {_fmt_pt(g['start'])}->{_fmt_pt(g['end'])}"
    return "?"


def _print_text(
    result: DiffResult,
    sides: dict[str, dict[str, str]],
    nets: Sequence[str],
    layers: Sequence[str],
) -> None:
    counts = result.counts()
    print(f"kct pcb diff: {sides['old']['spec']} -> {sides['new']['spec']}")
    if nets or layers:
        parts = []
        if nets:
            parts.append("net " + "|".join(nets))
        if layers:
            parts.append("layer " + "|".join(layers))
        print(f"filter: {' AND '.join(parts)}")
    print(
        f"removed {counts['removed']}  added {counts['added']}  "
        f"changed {counts['changed']}  unchanged {counts['unchanged']}"
    )

    by_kind: dict[str, dict[str, int]] = {}
    for e in result.entries:
        by_kind.setdefault(e.kind, dict.fromkeys(STATUSES, 0))[e.status] += 1
    if by_kind:
        print()
        print(f"  {'kind':<10} {'removed':>8} {'added':>8} {'changed':>8} {'unchanged':>10}")
        for kind in sorted(by_kind):
            c = by_kind[kind]
            print(
                f"  {kind:<10} {c['removed']:>8} {c['added']:>8} "
                f"{c['changed']:>8} {c['unchanged']:>10}"
            )

    rows: dict[tuple[str, str], dict[str, int]] = {}
    for e in result.entries:
        if e.status == "unchanged":
            continue
        rows.setdefault((e.net, e.layer), dict.fromkeys(STATUSES, 0))[e.status] += 1
    if rows:
        width = max(12, *(len(net or "-") for net, _ in rows))
        lwidth = max(14, *(len(layer or "-") for _, layer in rows))
        print()
        print(f"  {'net':<{width}} {'layer':<{lwidth}} {'removed':>8} {'added':>8} {'changed':>8}")
        for (net, layer), c in sorted(rows.items()):
            print(
                f"  {net or '-':<{width}} {layer or '-':<{lwidth}} "
                f"{c['removed']:>8} {c['added']:>8} {c['changed']:>8}"
            )

    changes = result.changes()
    if changes:
        print()
        for e in changes[:TEXT_ITEM_LIMIT]:
            head = f"  {e.status:<8} {e.kind:<9} {e.net or '-'} {e.layer or '-'} [{e.key}]"
            if e.status == "changed":
                print(f"{head}\n      {_describe(e.before)}\n   -> {_describe(e.after)}")
            elif e.status == "removed":
                print(f"{head}  {_describe(e.before)}")
            else:
                print(f"{head}  {_describe(e.after)}")
        if len(changes) > TEXT_ITEM_LIMIT:
            print(f"  ... {len(changes) - TEXT_ITEM_LIMIT} more (use --format json)")


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

_SVG_STYLE = """
.removed { color: #d62728; }
.added { color: #2ca02c; }
.changed { color: #ff9f1c; }
.unchanged { color: #9a9a9a; opacity: 0.35; }
.before { stroke-dasharray: 0.4 0.25; opacity: 0.8; }
.edge-cuts { color: #222222; opacity: 1; }
.fill { fill-opacity: 0.35; }
"""


def _circle_through(
    a: Sequence[float], b: Sequence[float], c: Sequence[float]
) -> tuple[float, float, float] | None:
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _arc_path(start: Sequence[float], mid: Sequence[float], end: Sequence[float]) -> str:
    circle = _circle_through(start, mid, end)
    if circle is None:
        return f"M {start[0]:g} {start[1]:g} L {mid[0]:g} {mid[1]:g} L {end[0]:g} {end[1]:g}"
    _ux, _uy, r = circle
    # Sweep: sign of the cross product of (mid-start) x (end-start).
    cross = (mid[0] - start[0]) * (end[1] - start[1]) - (mid[1] - start[1]) * (end[0] - start[0])
    sweep = 1 if cross > 0 else 0
    # Large arc: mid lies on the far side of the chord from the centre.
    chord = math.hypot(end[0] - start[0], end[1] - start[1])
    sagitta_mid = math.hypot(mid[0] - (start[0] + end[0]) / 2, mid[1] - (start[1] + end[1]) / 2)
    large = 1 if sagitta_mid > r and chord > 0 else 0
    return f"M {start[0]:g} {start[1]:g} A {r:g} {r:g} 0 {large} {sweep} {end[0]:g} {end[1]:g}"


def _points_attr(points: Sequence[Sequence[float]]) -> str:
    return " ".join(f"{x:g},{y:g}" for x, y in points)


def _shapes(item: DiffItem) -> list[tuple[str, dict[str, Any], list[tuple[float, float]]]]:
    """SVG ``(tag, attrs, bbox points)`` for one item; empty when not drawn."""
    g = item.geometry
    kind = item.kind
    if kind == "segment":
        s, e, w = g["start"], g["end"], g["width"]
        attrs: dict[str, Any] = {
            "x1": s[0], "y1": s[1], "x2": e[0], "y2": e[1],
            "stroke": "currentColor", "stroke-width": w, "stroke-linecap": "round",
        }  # fmt: skip
        return [("line", attrs, [s, e])]
    if kind == "arc":
        attrs = {
            "d": _arc_path(g["start"], g["mid"], g["end"]),
            "fill": "none", "stroke": "currentColor",
            "stroke-width": g["width"], "stroke-linecap": "round",
        }  # fmt: skip
        return [("path", attrs, [g["start"], g["mid"], g["end"]])]
    if kind == "via":
        (x, y), r = g["position"], g["size"] / 2
        return [
            (
                "circle",
                {"cx": x, "cy": y, "r": r, "fill": "currentColor"},
                [(x - r, y - r), (x + r, y + r)],
            )
        ]
    if kind == "pad":
        (x, y), (w, h) = g["position"], g["size"]
        attrs = {
            "x": x - w / 2, "y": y - h / 2, "width": w, "height": h,
            "fill": "currentColor", "fill-opacity": 0.6,
        }  # fmt: skip
        if g["rotation"]:
            attrs["transform"] = f"rotate({-g['rotation']:g} {x:g} {y:g})"
        if g["shape"] in ("circle", "oval"):
            attrs["rx"] = min(w, h) / 2
        r = math.hypot(w, h) / 2
        return [("rect", attrs, [(x - r, y - r), (x + r, y + r)])]
    if kind == "zone":
        outline = [tuple(p) for p in g.get("_outline", [])]
        if len(outline) < 2:
            return []
        attrs = {
            "points": _points_attr(outline), "fill": "none",
            "stroke": "currentColor", "stroke-width": 0.1,
        }  # fmt: skip
        return [("polygon", attrs, outline)]
    if kind == "zone_fill":
        out = []
        for _layer, ring in g.get("_fills", []):
            pts = [tuple(p) for p in ring]
            if len(pts) >= 3:
                attrs = {"points": _points_attr(pts), "fill": "currentColor", "class": "fill"}
                out.append(("polygon", attrs, pts))
        return out
    if kind == "graphic" and g["layer"] == "Edge.Cuts":
        w = max(g["width"], 0.1)
        base = {"fill": "none", "stroke": "currentColor", "stroke-width": w}
        t = g["type"]
        if t == "line":
            s, e = g["start"], g["end"]
            return [("line", {"x1": s[0], "y1": s[1], "x2": e[0], "y2": e[1], **base}, [s, e])]
        if t == "rect":
            (x1, y1), (x2, y2) = g["start"], g["end"]
            attrs = {
                "x": min(x1, x2),
                "y": min(y1, y2),
                "width": abs(x2 - x1),
                "height": abs(y2 - y1),
                **base,
            }
            return [("rect", attrs, [(x1, y1), (x2, y2)])]
        if t == "circle" and g["center"] is not None:
            (cx, cy), (ex, ey) = g["center"], g["end"]
            r = math.hypot(ex - cx, ey - cy)
            return [
                (
                    "circle",
                    {"cx": cx, "cy": cy, "r": r, **base},
                    [(cx - r, cy - r), (cx + r, cy + r)],
                )
            ]
        if t == "arc" and g["mid"] is not None:
            return [
                (
                    "path",
                    {"d": _arc_path(g["start"], g["mid"], g["end"]), **base},
                    [g["start"], g["mid"], g["end"]],
                )
            ]
        if t == "poly" and len(g["points"]) >= 2:
            pts = [tuple(p) for p in g["points"]]
            return [("polygon", {"points": _points_attr(pts), **base}, pts)]
    return []


def _element(tag: str, attrs: dict[str, Any], classes: str, title: str) -> str:
    cls = " ".join(filter(None, [classes, attrs.pop("class", "")]))
    parts = [f"<{tag} class={quoteattr(cls)}"]
    for k, v in attrs.items():
        parts.append(f" {k}={quoteattr(f'{v:g}' if isinstance(v, float) else str(v))}")
    return "".join(parts) + f"><title>{_xml_text(title)}</title></{tag}>"


def _xml_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_svg(full: DiffResult, shown: DiffResult, out: Path) -> None:
    """Write a self-contained overlay of ``full``, highlighting ``shown``.

    Entries outside ``shown`` (filtered away) are drawn dimmed like
    unchanged ones.  A changed item is drawn twice: its old geometry dashed
    as ``removed before`` and its new geometry as ``changed``.
    """
    highlighted = {id(e) for e in shown.entries}
    layers: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    bbox_pts: list[tuple[float, float]] = []

    def draw(item: DiffItem, status: str, entry: DiffEntry, extra: str = "") -> None:
        kind_class = "edge-cuts" if item.kind == "graphic" else item.kind.replace("_", "-")
        if status == "unchanged":
            classes = f"{kind_class} unchanged"
        else:
            classes = f"{kind_class} {status} {extra}".strip()
        title = f"{entry.status} {entry.kind} {entry.net or '-'} {entry.layer} [{entry.key}]"
        for tag, attrs, pts in _shapes(item):
            bbox_pts.extend((float(x), float(y)) for x, y in pts)
            if item.kind == "graphic":
                z = 4
            elif status == "unchanged":
                z = 1 if item.kind == "zone_fill" else 2
            else:
                z = 3
            layers[z].append(_element(tag, dict(attrs), classes, title))

    for e in full.entries:
        status = e.status if id(e) in highlighted else "unchanged"
        if status == "unchanged":
            draw(e.item, "unchanged", e)
        elif status == "changed":
            assert e.before is not None and e.after is not None
            draw(e.before, "removed", e, "before")
            draw(e.after, "changed", e)
        elif status == "removed":
            assert e.before is not None
            draw(e.before, "removed", e)
        else:
            assert e.after is not None
            draw(e.after, "added", e)

    if bbox_pts:
        xs = [p[0] for p in bbox_pts]
        ys = [p[1] for p in bbox_pts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    else:
        x0, y0, x1, y1 = 0.0, 0.0, 10.0, 10.0
    pad = max(1.0, 0.02 * max(x1 - x0, y1 - y0))
    x0, y0, w, h = x0 - pad, y0 - pad, (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
    scale = 8.0  # px per mm for the default rendered size

    body = "\n".join(line for z in sorted(layers) for line in layers[z])
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:g} {y0:g} {w:g} {h:g}" '
        f'width="{w * scale:.0f}" height="{h * scale:.0f}">\n'
        f"<style>{_SVG_STYLE}</style>\n"
        f'<rect class="background" x="{x0:g}" y="{y0:g}" width="{w:g}" height="{h:g}" fill="white"/>\n'
        f"{body}\n</svg>\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
