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

from kicad_tools.pcb.reinforce import LEG_COPY, STRATEGY_PARALLEL, STRATEGY_WIRE
from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm

SIDECAR_VERSION = 1
SIDECAR_SUFFIX = ".reinforcement.json"


def sidecar_path(board: Path) -> Path:
    """Sidecar location for a board: ``<board>.reinforcement.json``."""
    return board.with_name(board.name + SIDECAR_SUFFIX)


def _pt(x: float, y: float) -> dict[str, float]:
    return {"x": round(x, 4), "y": round(y, 4)}


def build_wire_records(outcome: Any, origin_x: float, origin_y: float) -> list[dict[str, Any]]:
    """Wire records from a ``ReinforceResult``'s legs (empty in spaced mode).

    Legs the minimum-wire-length filter skipped are omitted entirely (Issue
    #8915): the owner does not want a wire there, so it is not on the list.
    A leg reinforced by a parallel copy instead (Issue #8916) is recorded
    whatever its length, with ``"strategy": "parallel"`` and the copy's
    copper, so ``--clear`` / ``--replace`` can remove it and
    ``reinforcement_incomplete`` stops asking it for a wire. The leg that IS
    such a copy (status ``copy``) gets no record of its own.
    """
    records: list[dict[str, Any]] = []
    bare = round(bare_copper_diameter_mm(outcome.wire_gauge_awg), 4)
    for leg in outcome.legs:
        if not leg.polyline:
            continue
        parallel = getattr(leg, "strategy", STRATEGY_WIRE) == STRATEGY_PARALLEL
        if parallel and leg.status == LEG_COPY:
            continue
        # A leg below the minimum buttress-wire length gets no wire at all
        # (Issue #8915), so it gets no record either -- the sidecar is the
        # owner's cut-and-bend list. Every other unanchored leg still earns a
        # record: it names copper that WANTS a wire and says why it has none.
        # (The record would also be unreachable for ``drop_wires``, which
        # matches on anchors, and this one has none.) A short leg that got
        # parallel copper instead (#8916) IS reinforced, so it is recorded.
        copy = getattr(leg, "copy", None)
        if getattr(leg, "below_min_wire_length", False) and copy is None:
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
                # Additive (#8803): the --max-end-shift used; None = default.
                "max_end_shift_mm": getattr(outcome, "max_end_shift_mm", None),
                "bare_diameter_mm": bare,
                # Filled by #8743 (solder-mask opening along the run).
                "mask_opening": None,
                "status": leg.status,
                "reason": leg.reason,
            }
        )
        if parallel:
            records[-1].update(_parallel_fields(leg, copy, origin_x, origin_y))
    return sort_wires(records)


def _parallel_fields(leg: Any, copy: Any, origin_x: float, origin_y: float) -> dict[str, Any]:
    """The extra keys of a parallel-copy record (Issue #8916).

    Absent ``strategy`` means ``wire``, so pre-#8916 sidecars read unchanged
    and the version stays :data:`SIDECAR_VERSION`.
    """
    fields: dict[str, Any] = {
        "strategy": STRATEGY_PARALLEL,
        "copy_status": leg.copy_status,
        "copy_layer": copy.layer if copy is not None else None,
        "polyline": [_pt(x + origin_x, y + origin_y) for x, y in leg.polyline],
        "copy_segments": [],
        "stitch_vias": [],
        "stitch_via_size_mm": None,
        "stitch_via_drill_mm": None,
    }
    if copy is not None:
        fields["copy_segments"] = [
            {
                "start": _pt(a[0] + origin_x, a[1] + origin_y),
                "end": _pt(b[0] + origin_x, b[1] + origin_y),
                "width_mm": round(w, 4),
            }
            for a, b, w in copy.segments
        ]
        fields["stitch_vias"] = [_pt(x + origin_x, y + origin_y) for x, y in copy.stitch_vias]
        fields["stitch_via_size_mm"] = round(copy.via_size_mm, 4)
        fields["stitch_via_drill_mm"] = round(copy.via_drill_mm, 4)
    return fields


def is_parallel(wire: dict[str, Any]) -> bool:
    """True for a parallel-copy record; an absent ``strategy`` is a wire."""
    return bool(wire.get("strategy", STRATEGY_WIRE) == STRATEGY_PARALLEL)


def wire_key(wire: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a wire: net + layer + leg start/end (end order ignored).

    A parallel copy (Issue #8916) is keyed on BOTH its layers, so a re-run
    that meets the leg from its copy's side replaces the record, not adds one.
    """
    ends = sorted(
        [
            (wire["leg_start"]["x"], wire["leg_start"]["y"]),
            (wire["leg_end"]["x"], wire["leg_end"]["y"]),
        ]
    )
    if is_parallel(wire):
        layers = "+".join(sorted({wire["layer"], wire.get("copy_layer") or ""} - {""}))
        return (wire["net"], f"parallel:{layers}", tuple(ends))
    return (wire["net"], wire["layer"], tuple(ends))


def sort_wires(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(wires, key=wire_key)


def merge_wires(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace wires whose key matches a new one; keep the rest; sort."""
    merged = {wire_key(w): w for w in existing}
    for w in new:
        merged[wire_key(w)] = w
    return sort_wires(list(merged.values()))


def _near(a: dict[str, float], b: tuple[float, float], tol: float = 0.05) -> bool:
    return abs(a["x"] - b[0]) <= tol and abs(a["y"] - b[1]) <= tol


def wires_for_net(wires: list[dict[str, Any]], net: str) -> list[dict[str, Any]]:
    return [w for w in wires if w.get("net") == net]


def wires_with_anchors(
    wires: list[dict[str, Any]], net: str, removed: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """Wires of ``net`` that have an anchor at one of the ``removed`` (sheet-absolute) points."""
    return [
        w
        for w in wires_for_net(wires, net)
        if any(_near(a, r) for a in w.get("anchors", []) for r in removed)
    ]


def drop_wires(
    path: Path,
    net: str,
    removed: list[tuple[float, float]],
    removed_copies: list[tuple[Any, ...]] | None = None,
) -> int:
    """Delete the sidecar wires of ``net`` anchored at a removed point; returns the count.

    ``removed_copies`` are the :func:`wire_key` s of parallel-copy records whose
    copper was removed (Issue #8916); those records go too. A no-op when the
    sidecar is absent. The file is rewritten deterministically.
    """
    if not path.is_file():
        return 0
    wires = load_wires(path)
    gone = wires_with_anchors(wires, net, removed)
    keys = set(removed_copies or ())
    gone += [w for w in wires_for_net(wires, net) if is_parallel(w) and wire_key(w) in keys]
    if not gone:
        return 0
    gone_ids = {id(w) for w in gone}
    kept = [w for w in wires if id(w) not in gone_ids]
    text = json.dumps(
        {"version": SIDECAR_VERSION, "wires": sort_wires(kept)}, indent=2, sort_keys=True
    )
    path.write_text(text + "\n", encoding="utf-8")
    return len(gone)


def load_wires(path: Path) -> list[dict[str, Any]]:
    """Read a sidecar's wires. Raises ``OSError``/``ValueError`` if unreadable."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != SIDECAR_VERSION:
        raise ValueError(f"{path}: not a version {SIDECAR_VERSION} reinforcement sidecar")
    wires = data.get("wires")
    if not isinstance(wires, list):
        raise ValueError(f"{path}: missing 'wires' list")
    return wires


def load_wires_if_present(board: Path) -> list[dict[str, Any]]:
    """Wires of the board's sidecar; empty when absent or unreadable (#8746)."""
    path = sidecar_path(board)
    if not path.is_file():
        return []
    try:
        return load_wires(path)
    except (OSError, ValueError):
        return []


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
        if is_parallel(w):
            lines.append(
                f"     parallel copy on {w.get('copy_layer') or '-'} "
                f"[{w.get('copy_status') or '-'}]: {len(w.get('copy_segments') or [])} "
                f"segment(s), {len(w.get('stitch_vias') or [])} stitching via(s)"
            )
        if w.get("path"):
            lines.append(f"     path: {w['path']}")
    return "\n".join(lines)
