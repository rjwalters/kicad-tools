"""Move a free via in a PCB and keep its attached copper connected.

Selects exactly one via (by position, net and/or UUID), relocates it, and
translates the coincident trace-segment endpoints on the same net by the via's
delta so routed copper follows the via instead of being stranded.

Coordinate convention matches ``pcb move-footprint``: (x, y) values are
**board-relative** by default (measured from the Edge.Cuts top-left origin);
pass ``absolute=True`` for raw KiCad page coordinates.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

#: Rule classes ``--check`` evaluates, reported in the JSON ``check`` block.
CHECK_RULE_CLASSES = ("clearance", "hole_clearance", "hole_to_hole")


def run_move_via(
    pcb_path: Path,
    at: tuple[float, float] | None = None,
    to: tuple[float, float] | None = None,
    by: tuple[float, float] | None = None,
    net: str | None = None,
    uuid: str | None = None,
    tolerance: float = 0.05,
    dry_run: bool = False,
    check: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
    absolute: bool = False,
    manufacturer: str = "jlcpcb",
    layers: int = 4,
) -> int:
    """Move one via and drag the copper attached to it.

    Returns:
        Exit code (0 for success, 1 for errors; nothing is written on error).
    """
    from kicad_tools.schema.pcb import PCB

    if at is None and uuid is None:
        return _fail("Either --at or --uuid is required to select a via", output_format)
    if (to is None) == (by is None):
        return _fail("Exactly one of --to or --by is required", output_format)

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        return _fail(f"Failed to load PCB: {e}", output_format)

    ox, oy = pcb.board_origin
    shift = (ox, oy) if absolute else (0.0, 0.0)

    # Select candidates.
    candidates = list(pcb.vias)
    if uuid is not None:
        candidates = [v for v in candidates if v.uuid == uuid]
    if at is not None:
        ax, ay = at[0] - shift[0], at[1] - shift[1]
        tol_sq = tolerance * tolerance
        candidates = [
            v for v in candidates if (v.position[0] - ax) ** 2 + (v.position[1] - ay) ** 2 <= tol_sq
        ]
    if net is not None:
        candidates = [v for v in candidates if net in (v.net_name, str(v.net_number))]

    if not candidates:
        return _fail("No via matched the selection", output_format)
    if len(candidates) > 1:
        listing = "; ".join(
            f"uuid={v.uuid or '?'} at ({v.position[0] + shift[0]:.4f}, "
            f"{v.position[1] + shift[1]:.4f}) net={v.net_name or v.net_number}"
            for v in candidates
        )
        return _fail(
            f"Ambiguous selection: {len(candidates)} vias matched ({listing}); "
            "narrow with --net, --uuid or a smaller --tolerance",
            output_format,
        )
    via = candidates[0]

    old_pos = via.position
    if to is not None:
        new_pos = (to[0] - shift[0], to[1] - shift[1])
    else:
        new_pos = (old_pos[0] + by[0], old_pos[1] + by[1])  # type: ignore[index]
    delta = (new_pos[0] - old_pos[0], new_pos[1] - old_pos[1])
    net_label = via.net_name or via.net_number
    touched = {n for n in (via.net_name, str(via.net_number)) if n and n != "0"}

    before: Counter | None = None
    if check:
        before = _violation_counter(pcb, touched, manufacturer, layers, via, pcb_path)

    # Drag attached endpoints first (matched against the OLD via position).
    net_filter = via.net_number or None
    dragged = 0
    if delta != (0.0, 0.0):
        dragged = pcb.drag_trace_endpoints(
            old_pos, delta, tolerance=tolerance, net_number=net_filter
        )
        if not pcb.relocate_via(via, new_pos):
            return _fail("Could not locate the via's S-expression node", output_format)

    result: dict = {
        "pcb": str(pcb_path),
        "dry_run": dry_run,
        "coordinate_space": "absolute" if absolute else "board-relative",
        "board_origin": [ox, oy],
        "via": {
            "uuid": via.uuid,
            "net": net_label,
            "old_position": [old_pos[0] + shift[0], old_pos[1] + shift[1]],
            "new_position": [new_pos[0] + shift[0], new_pos[1] + shift[1]],
        },
        "endpoints_dragged": dragged,
        "moved": False,
    }

    if check and before is not None:
        after = _violation_counter(pcb, touched, manufacturer, layers, via, pcb_path)
        new = after - before
        result["check"] = {
            "new_violations": sum(new.values()),
            "rules_checked": list(CHECK_RULE_CLASSES),
        }
        if new:
            result["check"]["details"] = [
                {"rule_id": k[0], "layer": k[1], "nets": list(k[2]), "items": list(k[3])}
                for k in new
            ]
            return _fail(
                f"--check: move introduces {sum(new.values())} new clearance "
                "violation(s) (copper clearance, hole_clearance, hole_to_hole) "
                "on the touched net(s); nothing written",
                output_format,
                extra=result,
            )

    if not dry_run:
        save_path = output_path or pcb_path
        result["output"] = str(save_path)
        try:
            pcb.save(save_path)
        except Exception as e:
            return _fail(f"Failed to save PCB: {e}", output_format)
        result["moved"] = True

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        v = result["via"]
        print("PCB Move Via (dry run)" if dry_run else "PCB Move Via")
        print(f"  PCB: {pcb_path}")
        print(f"  Via {v['uuid'] or '(no uuid)'} (net {v['net']}):")
        print(
            f"    Position [{result['coordinate_space']}]: "
            f"({v['old_position'][0]}, {v['old_position'][1]}) -> "
            f"({v['new_position'][0]}, {v['new_position'][1]})"
        )
        print(f"    Endpoints dragged: {dragged}")
        if check:
            print(f"    Check ({', '.join(CHECK_RULE_CLASSES)}): no new violations")
        print(f"  {'Would move via' if dry_run else 'Saved to: ' + result['output']}")
    return 0


def _violation_counter(
    pcb, touched: set[str], manufacturer: str, layers: int, via, pcb_path: Path
) -> Counter:
    """Violations for the moved via on ``touched`` nets, as a location-free multiset.

    Locations shift with the move, so violations are compared by
    (rule, layer, nets, items) rather than by count or position.  Covers
    manufacturer copper clearance plus the moved via's ``hole_clearance`` and
    ``hole_to_hole`` against the board's own ``.kicad_pro`` / ``.kicad_dru``.
    """
    from kicad_tools.validate.checker import DRCChecker

    results = DRCChecker(pcb, manufacturer=manufacturer, layers=layers).check_clearances()
    counter: Counter = Counter()
    for viol in results:
        if viol.nets and not touched.intersection(viol.nets):
            continue
        counter[
            (viol.rule_id, viol.layer, tuple(sorted(viol.nets)), tuple(sorted(viol.items)))
        ] += 1
    counter.update(_via_hole_violations(pcb, via, pcb_path))
    return counter


def _via_hole_violations(pcb, via, pcb_path: Path) -> Counter:
    """``hole_clearance`` / ``hole_to_hole`` breaches of ``via``'s drill.

    Hole clearance is measured from the drill edge to foreign copper (tracks,
    pads, vias, zone fills) on every copper layer the barrel spans, connected
    or not.  Hole-to-hole is measured to every other drill, any net.  The
    moved via is keyed by uuid so its own identity is stable across the move.
    """
    from shapely.geometry import LineString, Point

    from kicad_tools._shapely import require_shapely
    from kicad_tools.core.layers import via_spans_layer
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules, ItemProps
    from kicad_tools.validate.rules.base import DRC_TOLERANCE
    from kicad_tools.validate.rules.clearance import (
        ClearanceRule,
        _collect_zone_fills,
        _element_to_shapely_geom,
    )
    from kicad_tools.validate.rules.factory_clearance import _hole_geometry

    counter: Counter = Counter()
    if via.drill <= 0:
        return counter
    require_shapely("move-via hole checks")
    rules = BoardClearanceRules.from_board(pcb_path)
    hole = Point(*via.position).buffer(via.drill / 2, quad_segs=64)
    me = ItemProps("Via", via.net_name, plated=True)
    me_id = f"via {via.uuid or 'moved'} hole"
    kinds = {"segment": "Track", "pad": "Pad", "via": "Via"}
    fills = _collect_zone_fills(pcb, include_unassigned=True)

    def foreign(net: int) -> bool:
        return not (net != 0 and net == via.net_number)

    for layer in pcb.copper_layers:
        if not via_spans_layer(via.layers, layer.name):
            continue
        candidates = []
        for elem in ClearanceRule()._collect_elements(pcb, layer.name):
            if elem.element_type == "via" and elem.geometry[:2] == tuple(via.position):
                continue  # the moved via itself
            if elem.element_type == "segment":
                x1, y1, x2, y2, width = elem.geometry
                geom = LineString(((x1, y1), (x2, y2))).buffer(width / 2, quad_segs=64)
            else:
                geom = _element_to_shapely_geom(elem)
            if geom is not None and foreign(elem.net_number):
                candidates.append((geom, kinds[elem.element_type], elem.net_name, elem.reference))
        candidates.extend(
            (f.polygon, "Zone", f.net_name, f"Zone [{f.net_name}]")
            for f in fills.get(layer.name, [])
            if foreign(f.net_number)
        )
        for geom, kind, net_name, ref in candidates:
            required = rules.hole_clearance(me, ItemProps(kind, net_name), layer.name)
            if hole.distance(geom) + DRC_TOLERANCE < required:
                nets = tuple(sorted({via.net_name, net_name}))
                counter[("hole_clearance", layer.name, nets, tuple(sorted((me_id, ref))))] += 1

    drills = [
        (
            ItemProps("Via", v.net_name, plated=True),
            Point(*v.position).buffer(v.drill / 2, quad_segs=64),
            v.net_name,
            f"via {v.uuid or v.position} hole",
        )
        for v in pcb.vias
        if v is not via and v.drill > 0
    ]
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.type == "thru_hole" and (geom := _hole_geometry(pad, fp)) is not None:
                props = ItemProps("Pad", pad.net_name, plated=True, pad_type="Through-hole")
                drills.append((props, geom, pad.net_name, f"{fp.reference}-{pad.number} hole"))
    for props, geom, net_name, ref in drills:
        if hole.distance(geom) + DRC_TOLERANCE < rules.hole_to_hole(me, props):
            nets = tuple(sorted({via.net_name, net_name}))
            counter[("hole_to_hole", "*", nets, tuple(sorted((me_id, ref))))] += 1
    return counter


def _fail(message: str, output_format: str, extra: dict | None = None) -> int:
    if output_format == "json":
        payload = dict(extra or {})
        payload["error"] = message
        print(json.dumps(payload, indent=2))
    else:
        print(f"Error: {message}", file=sys.stderr)
    return 1
