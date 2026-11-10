"""Place global fiducials on a PCB.

``kct pcb add-fiducials`` searches each requested board side for copper-free
sites near the board corners, outside every courtyard and at least
``--edge-margin`` from the board edge, then inserts stock ``Fiducial``
footprints flagged ``board_only`` / ``exclude_from_bom`` /
``exclude_from_pos_files`` so BOM, CPL and schematic parity stay clean.

Zone fills are not treated as obstacles (a fiducial inside a pour gets a
clearance void from the pour); keepout rule areas that forbid footprints,
pads or copper are.  The whole run is all-or-nothing: if any requested side
lacks a free site, nothing is written.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

DEFAULT_FOOTPRINT = "Fiducial_1mm_Mask2mm"
DEFAULT_LIBRARY = "Fiducial"
#: Copper/courtyard-free radius around a fiducial centre (mm), applied once:
#: 1 mm mask-opening radius plus 1 mm bare-board ring.
DEFAULT_KEEPOUT_MM = 2.0
DEFAULT_EDGE_MARGIN_MM = 3.0
_GRID_STEP_MM = 0.5
#: Minimum spacing between two fiducials on one side (mm).
_MIN_SPACING_MM = 5.0
#: Corner search order: three of four corners is an L (asymmetric, non-collinear).
_CORNER_ORDER = ("top-left", "bottom-right", "bottom-left", "top-right", "top-mid", "bottom-mid")


def _collinear(points: list[tuple[float, float]]) -> bool:
    """True when every point lies on one line (needs >= 3 points to be False)."""
    if len(points) < 3:
        return True
    x0, y0 = points[0]
    for i in range(1, len(points)):
        for j in range(i + 1, len(points)):
            cross = (points[i][0] - x0) * (points[j][1] - y0) - (points[i][1] - y0) * (
                points[j][0] - x0
            )
            if abs(cross) > 1.0:  # mm^2; a 2 mm x 1 mm triangle is enough
                return False
    return True


def _side_layers(side: str) -> tuple[str, str]:
    return ("F.Cu", "F") if side == "front" else ("B.Cu", "B")


def stamp_child_uuids(pcb, refs) -> int:
    """Give uuid-less properties/graphics of footprints ``refs`` a minted UUID.

    Library footprints carry no per-item identities, and ``add_footprint`` only
    stamps the footprint and its pads.  Stamping the rest keeps the saved board
    in canonical-UUID form, so a later UUID remap is a pure rename (#8532).
    """
    from kicad_tools.sexp import SExp

    wanted, count = set(refs), 0
    for fp_node in pcb._sexp.find_all("footprint"):
        ref_prop = next(
            (c for c in fp_node.find_all("property") if c.get_string(0) == "Reference"), None
        )
        ref = ref_prop.get_string(1) if ref_prop is not None else None
        if ref not in wanted:
            continue
        fp_uuid = fp_node.find_child("uuid")
        key = fp_uuid.get_first_atom() if fp_uuid is not None else ref
        for i, child in enumerate(fp_node.children):
            if child.is_atom or child.name is None:
                continue
            if child.name != "property" and not child.name.startswith("fp_"):
                continue
            if child.find_child("uuid") is None:
                child.append(SExp.list("uuid", pcb.mint_uuid(child.name, key, i)))
                count += 1
    return count


def _is_fiducial(fp) -> bool:
    return str(getattr(fp, "name", "")).startswith("Fiducial:")


def _build_obstacles(pcb, side: str, keepout: float):
    """Return shapely geometries to avoid on ``side``.

    Copper and courtyards are *not* inflated here: the clearance is applied once,
    as the radius (``keepout``) of the probe disc in :func:`find_sites`, so the
    copper-free distance around a fiducial centre is ``keepout``.
    """
    from shapely.geometry import LineString, Point, Polygon, box  # type: ignore[import-untyped]

    from kicad_tools.geometry.courtyard import _courtyard_polygon
    from kicad_tools.validate.rules.via_pad_geometry import pad_absolute_bbox

    cu, short = _side_layers(side)
    geoms = []
    for fp in pcb.footprints:
        if _is_fiducial(fp) and fp.layer == cu:
            # Existing fiducials are obstacles on their real side (courtyard-exempt).
            geoms.append(Point(fp.position).buffer(0.5))
        for s in ("F", "B"):
            poly = _courtyard_polygon(fp, s, Polygon)
            if poly is not None:
                geoms.append(poly)  # courtyards: no extra keepout, just no overlap
        for pad in fp.pads:
            layers = pad.layers or []
            if cu in layers or "*.Cu" in layers or pad.type in ("thru_hole", "np_thru_hole"):
                x0, y0, x1, y1 = pad_absolute_bbox(pad, fp)
                geoms.append(box(x0, y0, x1, y1))
    for seg in pcb.segments:
        if seg.layer == cu:
            geoms.append(LineString([seg.start, seg.end]).buffer(seg.width / 2))
    for via in pcb.vias:
        geoms.append(Point(via.position).buffer(via.size / 2))
    for zone in pcb.zones:
        ko = getattr(zone, "keepout", None)
        if ko is None or len(zone.polygon) < 3:
            continue
        zone_layers = set(zone.layers or [zone.layer])
        if not ({cu, "F&B.Cu", "*.Cu"} & zone_layers):
            continue
        if not (ko.copperpour_allowed and ko.tracks_allowed and ko.footprints_allowed):
            geoms.append(Polygon(zone.polygon).buffer(0))
    return geoms


def _corner_targets(bounds, margin: float) -> dict[str, tuple[float, float]]:
    x0, y0, x1, y1 = bounds
    xm = (x0 + x1) / 2
    return {
        "top-left": (x0 + margin, y0 + margin),
        "top-right": (x1 - margin, y0 + margin),
        "bottom-left": (x0 + margin, y1 - margin),
        "bottom-right": (x1 - margin, y1 - margin),
        "top-mid": (xm, y0 + margin),
        "bottom-mid": (xm, y1 - margin),
    }


def find_sites(pcb, side: str, count: int, keepout: float, edge_margin: float):
    """Find ``count`` free fiducial sites on ``side`` (board-relative mm, the frame PCB.load uses).

    Raises:
        ValueError: if fewer than ``count`` sites exist or they are collinear.
    """
    from shapely.geometry import Point, Polygon
    from shapely.strtree import STRtree  # type: ignore[import-untyped]

    # PCB.load exposes outline, footprints, tracks and vias in one board-relative frame.
    outline = pcb.get_board_outline()
    if len(outline) < 3:
        raise ValueError("board has no closed Edge.Cuts outline to place fiducials inside")
    board = Polygon(outline).buffer(0)
    inner = board.buffer(-edge_margin)
    if inner.is_empty:
        raise ValueError(f"board is too small for an edge margin of {edge_margin} mm")
    obstacles = _build_obstacles(pcb, side, keepout)
    tree = STRtree(obstacles) if obstacles else None

    x0, y0, x1, y1 = board.bounds
    targets = _corner_targets((x0, y0, x1, y1), edge_margin)

    def free(x: float, y: float) -> bool:
        pt = Point(x, y)
        if not inner.contains(pt):
            return False
        if tree is None:
            return True
        disc = pt.buffer(keepout)
        return not any(disc.intersects(obstacles[i]) for i in tree.query(disc))

    # Candidate grid, computed once.
    cands = []
    nx = int((x1 - x0) / _GRID_STEP_MM) + 1
    ny = int((y1 - y0) / _GRID_STEP_MM) + 1
    for i in range(nx + 1):
        for j in range(ny + 1):
            x, y = x0 + i * _GRID_STEP_MM, y0 + j * _GRID_STEP_MM
            if free(x, y):
                cands.append((x, y))

    chosen: list[tuple[float, float]] = []
    for name in _CORNER_ORDER:
        if len(chosen) >= count:
            break
        tx, ty = targets[name]
        for x, y in sorted(cands, key=lambda p: math.hypot(p[0] - tx, p[1] - ty)):
            if all(math.hypot(x - cx, y - cy) >= _MIN_SPACING_MM for cx, cy in chosen):
                chosen.append((x, y))
                break
    if len(chosen) < count:
        raise ValueError(
            f"only {len(chosen)} of {count} free fiducial sites found on the {side} side "
            f"(keepout {keepout} mm, edge margin {edge_margin} mm)"
        )
    if count >= 3 and _collinear(chosen):
        raise ValueError(
            f"fiducial sites on the {side} side are collinear; no non-collinear set found"
        )
    return chosen


def _existing_fiducials(pcb, side: str) -> int:
    layer = _side_layers(side)[0]
    return sum(1 for fp in pcb.footprints if _is_fiducial(fp) and fp.layer == layer)


def _populated_sides(pcb) -> list[str]:
    sides = []
    for side, layer in (("front", "F.Cu"), ("back", "B.Cu")):
        for fp in pcb.footprints:
            flags: frozenset[str] | set[str] = getattr(fp, "attr_flags", frozenset())
            if fp.layer == layer and not fp.exclude_from_bom and "board_only" not in flags:
                sides.append(side)
                break
    return sides


def _next_refs(pcb, n: int) -> list[str]:
    used = {fp.reference for fp in pcb.footprints}
    refs: list[str] = []
    i = 1
    while len(refs) < n:
        if f"FID{i}" not in used:
            refs.append(f"FID{i}")
        i += 1
    return refs


def run_add_fiducials(
    pcb_path: Path,
    sides: str = "front,back",
    count: int = 3,
    footprint: str = DEFAULT_FOOTPRINT,
    keepout: float = DEFAULT_KEEPOUT_MM,
    edge_margin: float = DEFAULT_EDGE_MARGIN_MM,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
) -> int:
    """Add global fiducials to a PCB. Returns 0 on success, 1 on error."""
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        _print_error(f"Failed to load PCB: {e}", output_format)
        return 1

    if count < 1:
        _print_error("--count must be at least 1", output_format)
        return 1
    if sides.strip() == "populated":
        wanted = _populated_sides(pcb)
        if not wanted:
            _print_error("no populated side found for --sides populated", output_format)
            return 1
    else:
        wanted = [s.strip() for s in sides.split(",") if s.strip()]
        bad = [s for s in wanted if s not in ("front", "back")]
        if bad or not wanted:
            _print_error(f"invalid --sides {sides!r}; use front,back or populated", output_format)
            return 1
        wanted = list(dict.fromkeys(wanted))

    # Phase 1: find every site before touching the board (no partial write).
    # Idempotent: a side that already has >= count global fiducials is satisfied.
    plan: dict[str, list[tuple[float, float]]] = {}
    satisfied: list[str] = []
    try:
        for side in wanted:
            need = count - _existing_fiducials(pcb, side)
            if need <= 0:
                satisfied.append(side)
                continue
            plan[side] = find_sites(pcb, side, need, keepout, edge_margin)
    except ValueError as e:
        _print_error(str(e), output_format)
        return 1
    if not plan:
        msg = f"already satisfied: {', '.join(satisfied)} side(s) have >= {count} fiducials; nothing written"
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "pcb": str(pcb_path),
                        "fiducials": [],
                        "written": False,
                        "already_satisfied": satisfied,
                        "message": msg,
                    },
                    indent=2,
                )
            )
        else:
            print(msg)
        return 0

    refs = _next_refs(pcb, sum(len(v) for v in plan.values()))
    placed: list[dict[str, Any]] = []
    lib_id = footprint if ":" in footprint else f"{DEFAULT_LIBRARY}:{footprint}"
    ref_iter = iter(refs)
    if not dry_run:
        try:
            for side, sites in plan.items():
                layer = _side_layers(side)[0]
                for x, y in sites:
                    ref = next(ref_iter)
                    fp = pcb.add_footprint(lib_id, ref, x, y, layer=layer, value="Fiducial")
                    fp.exclude_from_bom = True
                    fp.exclude_from_pos_files = True
                    if "board_only" not in fp._attr_unknown_tokens:
                        fp._attr_unknown_tokens = [*fp._attr_unknown_tokens, "board_only"]
                    placed.append({"reference": ref, "side": side, "x": x, "y": y})
            stamp_child_uuids(pcb, [p["reference"] for p in placed])
        except Exception as e:
            _print_error(f"Failed to add fiducial footprint {lib_id}: {e}", output_format)
            return 1
    else:
        for side, sites in plan.items():
            for x, y in sites:
                placed.append({"reference": next(ref_iter), "side": side, "x": x, "y": y})

    result: dict[str, Any] = {
        "pcb": str(pcb_path),
        "footprint": lib_id,
        "dry_run": dry_run,
        "fiducials": [{**p, "x": round(p["x"], 3), "y": round(p["y"], 3)} for p in placed],
        "written": False,
    }
    if not dry_run:
        save_path = output_path or pcb_path
        try:
            pcb.save(save_path)
        except Exception as e:
            _print_error(f"Failed to save PCB: {e}", output_format)
            return 1
        result["written"] = True
        result["output"] = str(save_path)

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print("PCB Add Fiducials (dry run)" if dry_run else "PCB Add Fiducials")
        print(f"  PCB: {pcb_path}")
        print(f"  Footprint: {lib_id}")
        for p in result["fiducials"]:
            print(f"  {p['reference']}: {p['side']} at ({p['x']}, {p['y']}) mm (board-relative)")
        print("  Dry run: nothing written" if dry_run else f"  Saved to: {result['output']}")
    return 0


def _print_error(message: str, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"error": message}, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
