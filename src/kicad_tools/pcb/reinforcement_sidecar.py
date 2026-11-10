"""Buttress-wire list sidecar for ``kct pcb reinforce`` (Issue #8744).

Slice 3 of #8741. Each non-dry-run ``reinforce`` pass in ``ends`` mode
records the wires the board owner must cut and bend in
``<board>.reinforcement.json`` next to the board, one entry per leg
(:class:`kicad_tools.pcb.reinforce.LegSummary`). Re-running replaces its own
wires, keeps other nets' entries and never duplicates; output is sorted and
free of timestamps so it diffs cleanly.

Coordinates are sheet-absolute (like refusals, Issue #8493).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm

SIDECAR_VERSION = 1
SIDECAR_SUFFIX = ".reinforcement.json"


def sidecar_path(board: Path) -> Path:
    """Sidecar location for a board: ``<board>.reinforcement.json``."""
    return board.with_name(board.name + SIDECAR_SUFFIX)


def _pt(x: float, y: float) -> dict[str, float]:
    return {"x": round(x, 4), "y": round(y, 4)}


def build_wire_records(outcome: Any, origin_x: float, origin_y: float) -> list[dict[str, Any]]:
    """Wire records from a ``ReinforceResult``'s legs (empty in spaced mode)."""
    records: list[dict[str, Any]] = []
    bare = round(bare_copper_diameter_mm(outcome.wire_gauge_awg), 4)
    for leg in outcome.legs:
        if not leg.polyline:
            continue
        sx, sy = leg.polyline[0]
        ex, ey = leg.polyline[-1]
        start = _pt(sx + origin_x, sy + origin_y)
        end = _pt(ex + origin_x, ey + origin_y)
        anchors = [_pt(a.x + origin_x, a.y + origin_y) for a in leg.anchors]
        straight = None
        if len(anchors) == 2:
            straight = round(
                math.hypot(anchors[1]["x"] - anchors[0]["x"], anchors[1]["y"] - anchors[0]["y"]),
                4,
            )
        records.append(
            {
                "net": leg.net,
                "layer": leg.layer,
                "kind": leg.kind,
                "path": leg.paths[0] if leg.paths else None,
                "paths": sorted(leg.paths),
                "leg_start": start,
                "leg_end": end,
                "anchors": anchors,
                "length_mm": round(leg.length_mm, 4),
                "straight_line_mm": straight,
                "wire_gauge_awg": outcome.wire_gauge_awg,
                "bare_diameter_mm": bare,
                # Filled by #8743 (solder-mask opening along the run).
                "mask_opening": None,
                "status": leg.status,
                "reason": leg.reason,
            }
        )
    return sort_wires(records)


def wire_key(wire: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a wire: net + layer + leg start/end (end order ignored)."""
    ends = sorted(
        [
            (wire["leg_start"]["x"], wire["leg_start"]["y"]),
            (wire["leg_end"]["x"], wire["leg_end"]["y"]),
        ]
    )
    return (wire["net"], wire["layer"], tuple(ends))


def sort_wires(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(wires, key=wire_key)


def merge_wires(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace wires whose key matches a new one; keep the rest; sort."""
    merged = {wire_key(w): w for w in existing}
    for w in new:
        merged[wire_key(w)] = w
    return sort_wires(list(merged.values()))


def load_wires(path: Path) -> list[dict[str, Any]]:
    """Read a sidecar's wires. Raises ``OSError``/``ValueError`` if unreadable."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        raise ValueError(f"{path}: not a version {SIDECAR_VERSION} reinforcement sidecar")
    wires = data.get("wires")
    if not isinstance(wires, list):
        raise ValueError(f"{path}: missing 'wires' list")
    return wires


def write_sidecar(board: Path, new_wires: list[dict[str, Any]]) -> Path:
    """Merge ``new_wires`` into the board's sidecar and write it deterministically."""
    path = sidecar_path(board)
    existing = load_wires(path) if path.is_file() else []
    wires = merge_wires(existing, new_wires)
    text = json.dumps({"version": SIDECAR_VERSION, "wires": wires}, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def format_wire_list(wires: list[dict[str, Any]]) -> str:
    """Human-readable wire list."""
    if not wires:
        return "No wires recorded."
    lines = [f"{len(wires)} wire(s):"]
    for i, w in enumerate(wires):
        anchors = ", ".join(f"({a['x']:.2f}, {a['y']:.2f})" for a in w["anchors"]) or "none"
        lines.append(
            f"  {i}. {w['net']} {w['layer']} {w['length_mm']:.1f} mm "
            f"{w['wire_gauge_awg']} AWG [{w['status']}] anchors: {anchors}"
        )
        if w.get("path"):
            lines.append(f"     path: {w['path']}")
    return "\n".join(lines)
