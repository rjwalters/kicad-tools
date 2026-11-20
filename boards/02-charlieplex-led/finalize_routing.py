"""Apply the reviewed revision-B escape corrections, then require native DRC.

The seed-42 all-net route places two escape vias toward the adjacent pad,
shorting C1 and R5. These are explicit layout edits, not a DRC waiver. Their
connected segment endpoints move with them. A changed route still has to pass
native DRC; unmatched coordinates never silently certify a different board.
"""

import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kicad_tools.router.quantize import quantize_pcb_file
from kicad_tools.sexp import parse_file, serialize_sexp


def _relocate_escapes(pcb):
    """Repair reviewed escape variants, moving only their own net endpoints."""
    moves = {
        (154.385, 117.5): (156.8, 117.5),  # C1 GND escape, outside pad courtyard
        # Keep the complete drill/annulus clear of SMT copper (issue #5012).
        (133.1, 113.5): (133.4, 112.8),  # LINE_A
        (148.1, 85.5): (148.4, 85.5),  # NODE_A
        (158.9, 85.5): (158.6, 85.5),  # NODE_A
        (138.0, 85.9): (138.4, 85.9),  # NODE_B
        (148.9, 105.5): (148.6, 105.5),  # NODE_C
        (138.1, 105.5): (138.4, 105.5),  # NODE_C
        (149.8, 105.9): (150.2, 106.2),  # NODE_C
        (158.9, 95.5): (158.6, 95.5),  # NODE_D
        (148.1, 95.5): (148.4, 95.5),  # NODE_D
        (157.1, 105.9): (156.7, 105.9),  # NODE_D
    }
    # Linux seed-42 routing chooses adjacent escape grid cells. The
    # observed variants need reviewed destinations. Net qualification
    # prevents an unrelated endpoint at the same coordinate from moving.
    net_names = {net.get_int(0): net.get_string(1) for net in pcb.find_children("net")}
    net_qualified_moves = {
        # Authoritative-grid seed-42 capacitor escapes (#5270). Native DRC,
        # process eligibility and copper LVS validate these off-pad positions.
        ("GND", (155.1, 117.9)): (154.6, 118.4),  # C1-2; clear LINE_C and RESET
        ("VCC", (129.9, 119.5)): (128.6, 119.5),  # C2-1; outside the SMT drill area
        # Exact seed-42 variants exposed by the repaired edge parser (#5257).
        ("GND", (155.1, 117.5)): (156.8, 117.5),
        ("VCC", (139.7, 117.3)): (139.2, 115.5),
        # Directed Steiner searches produce these seed-42 escapes.
        ("NODE_A", (148.0, 84.9)): (148.0, 84.5),
        ("NODE_D", (157.9, 106.0)): (158.3, 106.0),
        ("GND", (155.2, 117.4)): (156.5, 117.4),
        ("VCC", (140.665, 117.3)): (139.2, 115.5),
        ("VCC", (140.665, 117.2)): (139.2, 115.5),
        ("RESET", (141.1, 118.1)): (140.9, 118.5),
        # CI reports board-relative (17.8, 40.8); origin is (123.5, 77.5).
        ("RESET", (141.3, 118.3)): (140.9, 118.5),
        # The authoritative four-line outline (#4978) produces alternate
        # bottom-row LED escape cells. Move their attached endpoints with
        # the via, using the same off-pad x offsets as the upper rows.
        ("NODE_B", (148.1, 105.5)): (148.4, 105.5),  # D8-1
        ("NODE_B", (158.9, 105.5)): (158.6, 105.5),  # D9-2
        ("NODE_C", (138.1, 106.0)): (138.4, 106.0),  # D7-1
    }
    # Only a present via activates a relocation. Quantization can create a
    # track-only waypoint at an old escape cell; moving it on a second pass
    # would distort an already repaired route.
    active_moves = {}
    for via in pcb.find_all("via"):
        net = via.find("net")
        net_name = net_names.get(net.get_int(0), net.get_string(0)) if net else None
        point = via.find("at")
        old = tuple(round(point.get_float(i), 3) for i in range(2))
        destination = net_qualified_moves.get((net_name, old), moves.get(old))
        if destination is not None:
            active_moves[net_name, old] = destination
    for item in pcb.find_all("segment") + pcb.find_all("via"):
        net = item.find("net")
        net_name = net_names.get(net.get_int(0), net.get_string(0)) if net else None
        for key in ("at",) if item.name == "via" else ("start", "end"):
            point = item.find(key)
            old = tuple(round(point.get_float(i), 3) for i in range(2))
            destination = active_moves.get((net_name, old))
            if destination is not None:
                for i, value in enumerate(destination):
                    point.set_value(i, value)


def _relocate_pad_edge(path):
    """Slide vias out of the mask-dam zone beside SMD pads (issue #7296).

    Issue #8979: also out of the fab-view same-net pad slit band, at the
    edge ``kct check --mfr jlcpcb`` grades this 2-layer stackup's same-net
    ``physical_copper_gap`` against (resolved from the profile table, never a
    literal).  The grid router still seats some escape vias in that band
    (C++ pathfinder, tracked by #8994).
    """
    from kicad_tools.cli.relocate_in_pad_vias import relocate_pad_edge_vias
    from kicad_tools.cli.stitch_cmd import _resolve_mfr_pad_gap_edge
    from kicad_tools.manufacturers import get_profile
    from kicad_tools.schema.pcb import PCB

    band = _resolve_mfr_pad_gap_edge("jlcpcb", 2)
    if band is None:
        raise ValueError("Board02 could not resolve the jlcpcb 2-layer pad slit band")
    pcb = PCB.load(path)
    result = relocate_pad_edge_vias(
        pcb, get_profile("jlcpcb").get_design_rules(layers=2), same_net_pad_gap_mm=band
    )
    if result.skipped or result.unresolvable:
        raise ValueError(f"Board02 pad-edge vias left in the dam zone: {result}")
    if result.changed:
        pcb.save(path)


#: Centre-line distance a pad escape may bend from the pad it serves.  Closer,
#: the bend's copper sits under the mask opening beside the pad
#: (``mask_to_copper`` own-pad-entry / pass-through errors, issue #9467).
_ESCAPE_BEND_MM = 0.4
#: Clearance a re-routed escape keeps from other-net copper (netclass floor +
#: margin); native DRC re-verifies everything this module writes.
_ESCAPE_CLEARANCE_MM = 0.16
#: Power-width tracks carry the 0.2 mm ``Power`` netclass floor, so their
#: escape obstacle buffer is wider than the signal-track one (#9467).
_ESCAPE_POWER_CLEARANCE_MM = 0.25
#: Same-net copper within this distance of a mask opening, without an
#: endpoint inside the owning pad, is graded ``mask_to_copper`` (the
#: checker's warning clearance, ``mask_copper`` policy) -- corridor legs are
#: nudged beyond it plus margin (#9467).
_ESCAPE_WARN_BAND_MM = 0.15
_ESCAPE_NUDGE_MARGIN_MM = 0.075
#: A via ring closer than this to its own pad is slid out (ring overlap).
_RING_GAP_MM = 0.05
#: Ring-to-pad gap a slid via is given.
_RING_SLIDE_GAP_MM = 0.3
_TOL = 0.01


def _bbox_distance(pt, bbox):
    """Distance from ``pt`` to an axis-aligned ``(x0, y0, x1, y1)`` box (0 inside)."""
    x0, y0, x1, y1 = bbox
    return math.hypot(max(x0 - pt[0], 0.0, pt[0] - x1), max(y0 - pt[1], 0.0, pt[1] - y1))


def _near(a, b, tol=_TOL):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _pad_layers(pad):
    layers = set()
    for name in pad.layers:
        if name in ("F.Cu", "B.Cu"):
            layers.add(name)
        elif name == "*.Cu":
            layers.update(("F.Cu", "B.Cu"))
    return sorted(layers)


def _escape_candidates(pad_xy, target):
    """Routes from a pad centre to ``target``: axis-aligned first leg, then 90 or 45 deg.

    Legality (bend distance, clearance) is judged by the caller.  Candidates are
    point lists ordered by preference on a length tie: the 45-degree finish
    first, then the two L routes.
    """
    px, py = pad_xy
    tx, ty = target
    dx, dy = tx - px, ty - py
    out = []
    if abs(dx) < _TOL or abs(dy) < _TOL:
        return [[(px, py), (tx, ty)]]
    if abs(dy) > abs(dx):
        out.append([(px, py), (px, ty - math.copysign(abs(dx), dy)), (tx, ty)])
    elif abs(dx) > abs(dy):
        out.append([(px, py), (tx - math.copysign(abs(dy), dx), py), (tx, ty)])
    out.append([(px, py), (px, ty), (tx, ty)])
    out.append([(px, py), (tx, py), (tx, ty)])
    return out


def _route_length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))


def _turn_degrees(a, b, c):
    """Heading change at ``b`` going a -> b -> c (0 = straight)."""
    h1 = math.atan2(b[1] - a[1], b[0] - a[0])
    h2 = math.atan2(c[1] - b[1], c[0] - b[0])
    turn = abs(math.degrees(h2 - h1)) % 360
    return 360 - turn if turn > 180 else turn


class _EscapeContext:
    """Copper of one PCB snapshot, indexed for escape re-routing."""

    def __init__(self, pcb):
        from shapely.geometry import Point

        from kicad_tools.validate.rules.via_pad_geometry import pad_absolute_bbox

        self.pcb = pcb
        self.pads = []  # (ref, pad, bbox, centre, layers)
        for fp in pcb.footprints:
            for pad in fp.pads:
                x0, y0, x1, y1 = pad_absolute_bbox(pad, fp)
                self.pads.append(
                    (fp.reference, pad, (x0, y0, x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2))
                )
        self.point = Point

    def obstacles(self, layer, net_number):
        """Shapely shapes of every other-net copper item that exists on ``layer``."""
        from shapely.geometry import LineString, box

        point = self.point
        line_string = LineString
        shapes = []
        for seg in self.pcb.segments:
            if seg.layer == layer and seg.net_number != net_number:
                shapes.append(line_string([seg.start, seg.end]).buffer(seg.width / 2))
        for via in self.pcb.vias:
            if via.net_number != net_number:
                shapes.append(point(via.position).buffer(via.size / 2))
        for _ref, pad, bbox, _centre in self.pads:
            if pad.net_number != net_number and layer in _pad_layers(pad):
                shapes.append(box(*bbox))
        return shapes


def _is_node_hard(ctx, node, own_pad, layer):
    """A vertex a track chain must not be re-routed through (via, other pad)."""
    if any(_near(v.position, node) for v in ctx.pcb.vias):
        return True
    for _ref, pad, _bbox, centre in ctx.pads:
        if pad is not own_pad and _near(centre, node) and layer in _pad_layers(pad):
            return True
    return False


def _slide_ring_vias(pcb):
    """Slide same-net vias whose ring overlaps (or all but touches) their own pad.

    The ring then sits under the pad's mask opening (``mask_to_copper`` via
    pass-through).  The via moves radially away from the pad until the ring is
    ``_RING_SLIDE_GAP_MM`` clear; its tracks follow it and are re-squared by
    ``_square_pad_escapes``.  A slide that would crowd other-net copper is
    skipped and reported.
    """
    ctx = _EscapeContext(pcb)
    moved = 0
    for ref, pad, bbox, _centre in ctx.pads:
        if not pad.net_number or pad.type == "np_thru_hole":
            continue
        for via in list(pcb.vias):
            if via.net_number != pad.net_number:
                continue
            radius = via.size / 2
            cx, cy = via.position
            qx = min(max(cx, bbox[0]), bbox[2])
            qy = min(max(cy, bbox[1]), bbox[3])
            dist = math.hypot(cx - qx, cy - qy)
            if dist < _TOL or dist - radius >= _RING_GAP_MM:
                continue
            reach = radius + _RING_SLIDE_GAP_MM
            new = (qx + (cx - qx) / dist * reach, qy + (cy - qy) / dist * reach)
            new = (round(new[0], 4), round(new[1], 4))
            ring = ctx.point(new).buffer(radius + _ESCAPE_CLEARANCE_MM)
            blocked = any(
                ring.intersects(shape)
                for layer in ("F.Cu", "B.Cu")
                for shape in ctx.obstacles(layer, via.net_number)
            )
            if blocked:
                print(f"Board02: via at {via.position} left beside {ref}.{pad.number}: no room")
                continue
            attached = [
                seg
                for seg in pcb.segments
                if seg.net_number == via.net_number
                and (_near(seg.start, via.position) or _near(seg.end, via.position))
            ]
            for seg in attached:
                start = new if _near(seg.start, via.position) else seg.start
                end = new if _near(seg.end, via.position) else seg.end
                pcb.remove_segments([seg])
                pcb.add_trace(
                    start, end, width=seg.width, layer=seg.layer, net=seg.net_number, dedupe=False
                )
            pcb.relocate_via(via, new)
            moved += 1
            ctx = _EscapeContext(pcb)
    return moved


def _square_pad_escapes(pcb):
    """Re-route pad escapes that bend inside the pad's mask-opening zone.

    ``mask_to_copper`` (issue #8858) grades copper that stays under the mask
    beside a pad opening.  A track that leaves its pad straight and clean is
    excused; a stair-stepped or diagonal exit whose bends sit within
    ``_ESCAPE_BEND_MM`` of the pad is not (issue #9467).  The seed-42 route
    leaves exactly those dog-legs where ``_relocate_escapes`` moved a via, so
    the repair belongs to the recipe: every chain of plain track vertices that
    starts at a pad centre and bends inside the zone is replaced by an
    axis-aligned exit plus a 90- or 45-degree finish to the first vertex
    beyond the zone (or to the via it ends at).  Chains with no legal
    replacement are left untouched and reported -- never waived.
    """
    from shapely.geometry import LineString  # type: ignore[import-untyped]

    rerouted = 0
    unresolved = []
    for ref, pad_num in _escape_pads(pcb):
        ctx = _EscapeContext(pcb)
        entry = next(
            (e for e in ctx.pads if e[0] == ref and e[1].number == pad_num),
            None,
        )
        if entry is None:
            continue
        _ref, pad, bbox, centre = entry
        if not pad.net_number:
            continue
        for layer in _pad_layers(pad):
            same = [s for s in pcb.segments if s.layer == layer and s.net_number == pad.net_number]
            for spoke in [s for s in same if _near(s.start, centre) or _near(s.end, centre)]:
                if spoke not in pcb.segments:
                    continue
                result = _reroute_spoke(pcb, ctx, pad, bbox, centre, layer, spoke, LineString)
                if result is True:
                    rerouted += 1
                    ctx = _EscapeContext(pcb)
                elif result is not None:
                    unresolved.append((ref, pad_num, result))
    for ref, pad_num, why in unresolved:
        print(f"Board02: pad {ref}.{pad_num} escape left as routed: {why}")
    return rerouted


def _escape_pads(pcb):
    """``(ref, pad number)`` of every netted pad, in a deterministic order."""
    return sorted(
        (fp.reference, pad.number)
        for fp in pcb.footprints
        for pad in fp.pads
        if pad.net_number and pad.type != "np_thru_hole"
    )


def _reroute_spoke(pcb, ctx, pad, bbox, centre, layer, spoke, line_string):
    """Re-route one track chain leaving ``centre``; ``True`` if changed, ``None`` if clean."""
    net = pad.net_number
    same = [s for s in pcb.segments if s.layer == layer and s.net_number == net]

    def other_end(seg, node):
        return seg.end if _near(seg.start, node) else seg.start

    chain = [centre]
    used = [spoke]
    current = spoke
    node = other_end(spoke, centre)
    while True:
        chain.append(node)
        reaches = _bbox_distance(node, bbox) >= _ESCAPE_BEND_MM - 1e-6
        if reaches or _is_node_hard(ctx, node, pad, layer) or len(chain) > 8:
            break
        onward = [s for s in same if s is not current and s not in used and _touches(s, node)]
        if len(onward) != 1:
            break
        current = onward[0]
        used.append(current)
        node = other_end(current, node)
    if len(chain) < 3:
        return None
    interior = chain[1:-1]
    if all(_bbox_distance(v, bbox) >= _ESCAPE_BEND_MM - 1e-6 for v in interior):
        return None  # already clean
    vertex = chain[-1]
    last_from = chain[-2]
    span = math.dist(last_from, vertex)
    # A short last leg is re-joined at its far vertex; a long one (a track that
    # runs on beyond the zone) is split at the first point that lets the exit
    # bend clear of the pad, so the unchanged remainder keeps its geometry.
    options = [(vertex, None)]
    if span > 1.0 and _bbox_distance(vertex, bbox) >= _ESCAPE_BEND_MM - 1e-6:
        options = []
        for i in range(1, int(span / 0.05)):
            f = i * 0.05 / span
            probe = (
                round(last_from[0] + (vertex[0] - last_from[0]) * f, 4),
                round(last_from[1] + (vertex[1] - last_from[1]) * f, 4),
            )
            if _bbox_distance(probe, bbox) >= _ESCAPE_BEND_MM - 1e-6:
                options.append((probe, (probe, vertex)))
        options.append((vertex, None))
    obstacles = ctx.obstacles(layer, net)
    width = spoke.width
    best = None
    for target, remainder in options:
        onward_ref = remainder[1] if remainder else None
        if onward_ref is None and not _is_node_hard(ctx, target, pad, layer):
            after = [s for s in same if s not in used and _touches(s, target)]
            if len(after) == 1:
                onward_ref = other_end(after[0], target)
        for route in _escape_candidates(centre, target):
            if any(_bbox_distance(v, bbox) < _ESCAPE_BEND_MM - 1e-6 for v in route[1:-1]):
                continue
            if onward_ref is not None and _turn_degrees(route[-2], target, onward_ref) > 91:
                continue
            shape = line_string(route).buffer(width / 2 + _ESCAPE_CLEARANCE_MM)
            if any(shape.intersects(o) for o in obstacles):
                continue
            length = _route_length(route)
            if best is None or length < best[0] - 1e-9:
                best = (length, route, remainder)
        if best is not None:
            break  # nearest legal split wins; later splits only lengthen the exit
    if best is None:
        return f"no clear axis-aligned exit to {tuple(round(c, 3) for c in vertex)}"
    remainder = best[2]
    route = best[1]
    pcb.remove_segments(used)
    pcb.add_trace(
        route[0],
        route[-1],
        width=width,
        layer=layer,
        net=net,
        waypoints=[tuple(round(c, 4) for c in v) for v in route[1:-1]] or None,
        dedupe=False,
    )
    if remainder is not None:
        pcb.add_trace(remainder[0], remainder[1], width=width, layer=layer, net=net, dedupe=False)
    return True


def _touches(seg, node):
    return _near(seg.start, node) or _near(seg.end, node)


def _line_distance(point, a, b):
    """Distance from ``point`` to the infinite line through ``a`` and ``b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return math.dist(point, a)
    return abs((point[0] - a[0]) * dy - (point[1] - a[1]) * dx) / length


def _bend_count(points):
    count = 0
    for a, b, c in zip(points, points[1:], points[2:]):
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-9:
            count += 1
    return count


def _escape_obstacle_clearance(width):
    """Obstacle buffer for an escape of this width (netclass-aware)."""
    if width >= 0.45:
        return _ESCAPE_POWER_CLEARANCE_MM
    return _ESCAPE_CLEARANCE_MM


def _strip_covered_spans(pcb):
    """Remove same-net track fully covered by a longer collinear same-net run.

    The seed-42 route leaves short spokes that lie inside a longer segment's
    copper beside a pad (``redundant_copper_covered``).  Readiness's prune
    step will not touch them -- #9524's pad guard keeps it away from pad
    centres -- so the recipe removes them itself: every removed span is
    copper-identical to what remains, and each endpoint keeps a
    copper connection (the board's T-taps and pad lands).
    """
    removed = 0
    changed = True
    while changed:
        changed = False
        for cand in list(pcb.segments):
            a, b = tuple(cand.start), tuple(cand.end)
            if math.dist(a, b) < 1e-6:
                continue
            for other in pcb.segments:
                if other is cand or other.net_number != cand.net_number:
                    continue
                if other.layer != cand.layer:
                    continue
                c, d = tuple(other.start), tuple(other.end)
                if math.dist(c, d) <= math.dist(a, b) + 1e-6:
                    continue
                if (
                    _line_distance(a, c, d) > 0.02
                    or _line_distance(b, c, d) > 0.02
                    or _line_distance(c, a, b) > 0.02
                ):
                    continue

                def _within(p):
                    return (
                        min(c[0], d[0]) - 0.02 <= p[0] <= max(c[0], d[0]) + 0.02
                        and min(c[1], d[1]) - 0.02 <= p[1] <= max(c[1], d[1]) + 0.02
                    )

                if not (_within(a) and _within(b)):
                    continue
                pcb.remove_segments([cand])
                removed += 1
                changed = True
                break
    return removed


def _minimal_candidate_routes(pad_xy, target):
    """One-bend (or straight) routes from a pad centre to ``target``.

    Ordered so the emitted escape survives the release cleanup chain
    (``kct readiness --generate`` runs optimize-traces with #9524's pad
    guard): straight and diagonal-first routes leave no corner for the
    optimizer to chamfer into a fresh ``redundant_copper_staircase``, the
    slanted Ls keep their corner off the 90/45 degree chamfer grid, and the
    plain Ls come last.
    """
    px, py = pad_xy
    tx, ty = target
    dx, dy = tx - px, ty - py
    d = min(abs(dx), abs(dy))
    sx = 1.0 if dx > 0 else -1.0
    sy = 1.0 if dy > 0 else -1.0
    out = [[(px, py), (tx, ty)]]
    out.append([(px, py), (px + sx * d, py + sy * d), (tx, ty)])
    out.append([(px, py), (tx - sx * d, ty - sy * d), (tx, ty)])
    if abs(dy) > abs(dx):
        for s in (0.6, 0.4, -0.4, -0.6):
            out.append([(px, py), (px, ty + s), (tx, ty)])
    else:
        for s in (0.6, 0.4, -0.4, -0.6):
            out.append([(px, py), (tx + s, py), (tx, ty)])
    out.append([(px, py), (px, ty), (tx, ty)])
    out.append([(px, py), (tx, py), (tx, ty)])
    return out


def _walk_escape_chain(pcb, ctx, pad, centre, layer, spoke):
    """Vertices and segments of one track chain leaving ``centre``.

    Stops at the first hard node (via, other pad centre) or branch, like
    ``_reroute_spoke``; shared with the minimalize and nudge passes.
    """
    same = [s for s in pcb.segments if s.layer == layer and s.net_number == pad.net_number]

    def other_end(seg, node):
        return tuple(seg.end) if _near(seg.start, node) else tuple(seg.start)

    chain = [centre]
    used = [spoke]
    current = spoke
    node = other_end(spoke, centre)
    while True:
        chain.append(node)
        if _is_node_hard(ctx, node, pad, layer) or len(chain) > 8:
            break
        onward = [s for s in same if s is not current and s not in used and _touches(s, node)]
        if len(onward) != 1:
            break
        current = onward[0]
        used.append(current)
        node = other_end(current, node)
    return chain, used


def _minimal_escapes(pcb):
    """Re-write pad escapes that still carry avoidable bends.

    ``_square_pad_escapes`` stops once every interior bend clears the pad
    zone, but the release cleanup (optimize-traces, pad-guarded since #9524)
    can no longer collapse the staircases that leaves behind, so
    ``redundant_copper_staircase`` fails the trace-quality gate.  This pass
    replaces each escape chain with the fewest-bend legal route to its first
    hard node; chains with no legal replacement are left as routed.
    """
    from shapely.geometry import LineString  # type: ignore[import-untyped]

    rerouted = 0
    for ref, pad_num in _escape_pads(pcb):
        ctx = _EscapeContext(pcb)
        entry = next((e for e in ctx.pads if e[0] == ref and e[1].number == pad_num), None)
        if entry is None:
            continue
        _ref, pad, bbox, centre = entry
        if not pad.net_number:
            continue
        for layer in _pad_layers(pad):
            same = [s for s in pcb.segments if s.layer == layer and s.net_number == pad.net_number]
            for spoke in [s for s in same if _near(s.start, centre) or _near(s.end, centre)]:
                if spoke not in pcb.segments:
                    continue
                chain, used = _walk_escape_chain(pcb, ctx, pad, centre, layer, spoke)
                if len(chain) < 3 or _bend_count(chain) <= 1:
                    continue
                target = chain[-1]
                obstacles = ctx.obstacles(layer, pad.net_number)
                width = spoke.width
                clearance = _escape_obstacle_clearance(width)
                best = None
                for route in _minimal_candidate_routes(centre, target):
                    if any(
                        _bbox_distance(v, bbox) < _ESCAPE_BEND_MM - 1e-6 for v in route[1:-1]
                    ):
                        continue
                    shape = LineString(route).buffer(width / 2 + clearance)
                    if any(shape.intersects(o) for o in obstacles):
                        continue
                    key = (_bend_count(route), round(_route_length(route), 3))
                    if best is None or key < best[0]:
                        best = (key, route)
                if best is None or _bend_count(best[1]) >= _bend_count(chain):
                    continue
                route = best[1]
                pcb.remove_segments(used)
                pcb.add_trace(
                    route[0],
                    route[-1],
                    width=width,
                    layer=layer,
                    net=pad.net_number,
                    waypoints=[tuple(round(c, 4) for c in v) for v in route[1:-1]] or None,
                    dedupe=False,
                )
                rerouted += 1
                ctx = _EscapeContext(pcb)
    return rerouted


def _trim_pass_throughs(pcb):
    """Split pad centres that only pass a track through.

    Two same-net spokes that leave one pad centre in opposite directions make
    that centre a degree-2 node of the checker's maximal-chain walk, so the
    chain runs *through* the pad and its detours read as one long staircase.
    The longer spoke is trimmed back onto the pad's inner rectangle: its
    copper connection is unchanged, but the maximal chain now ends on the pad
    instead of crossing it.
    """
    ctx = _EscapeContext(pcb)
    trimmed = 0
    for ref, pad, bbox, centre in ctx.pads:
        if not pad.net_number or pad.type == "np_thru_hole":
            continue
        for layer in _pad_layers(pad):
            same = [s for s in pcb.segments if s.layer == layer and s.net_number == pad.net_number]
            spokes = [s for s in same if _near(s.start, centre) or _near(s.end, centre)]
            if len(spokes) != 2:
                continue
            fars = [tuple(s.end) if _near(s.start, centre) else tuple(s.start) for s in spokes]
            if _near(fars[0], fars[1]):
                continue
            pick = 0 if math.dist(centre, fars[0]) >= math.dist(centre, fars[1]) else 1
            long_ = spokes[pick]
            far = fars[pick]
            dx, dy = far[0] - centre[0], far[1] - centre[1]
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            ux, uy = dx / length, dy / length
            half_w = (bbox[2] - bbox[0]) / 4.0
            half_h = (bbox[3] - bbox[1]) / 4.0
            t = min(half_w / max(abs(ux), 1e-9), half_h / max(abs(uy), 1e-9))
            new_end = (round(centre[0] + ux * t, 3), round(centre[1] + uy * t, 3))
            if math.dist(new_end, centre) >= length:
                continue
            pcb.remove_segments([long_])
            pcb.add_trace(
                far,
                new_end,
                width=long_.width,
                layer=layer,
                net=pad.net_number,
                dedupe=False,
            )
            trimmed += 1
            ctx = _EscapeContext(pcb)
    return trimmed


def _nudge_corridors(pcb):
    """Move escape legs that hug their own pad's mask opening.

    A corridor leg whose column passes inside the opening's warning band is
    exactly where optimize-traces' pull-tight lands its replacement bend
    (the bend sits just outside #9524's pad zone but the copper beside the
    opening still grades ``mask_to_copper``).  The leg slides -- with its two
    chain neighbours -- to the nearest column clear of both the band and
    other-net copper; a column *inside* the zone span is fine too, because
    the pad guard then blocks the pull outright.
    """
    from shapely.geometry import LineString  # type: ignore[import-untyped]

    nudged = 0
    for ref, pad_num in _escape_pads(pcb):
        ctx = _EscapeContext(pcb)
        entry = next((e for e in ctx.pads if e[0] == ref and e[1].number == pad_num), None)
        if entry is None:
            continue
        _ref, pad, bbox, centre = entry
        if not pad.net_number:
            continue
        ox0, oy0, ox1, oy1 = bbox[0] - 0.05, bbox[1] - 0.05, bbox[2] + 0.05, bbox[3] + 0.05
        for layer in _pad_layers(pad):
            same = [s for s in pcb.segments if s.layer == layer and s.net_number == pad.net_number]
            for spoke in [s for s in same if _near(s.start, centre) or _near(s.end, centre)]:
                chain, used = _walk_escape_chain(pcb, ctx, pad, centre, layer, spoke)
                if len(used) < 3:
                    continue
                obstacles = ctx.obstacles(layer, pad.net_number)
                for idx in range(1, len(used) - 1):
                    seg = used[idx]
                    a, b = tuple(seg.start), tuple(seg.end)
                    if math.dist(a, b) < 1.0:
                        continue
                    vertical = abs(a[0] - b[0]) < 1e-6
                    horizontal = abs(a[1] - b[1]) < 1e-6
                    if not (vertical or horizontal):
                        continue
                    need = _ESCAPE_WARN_BAND_MM + seg.width / 2 + _ESCAPE_NUDGE_MARGIN_MM
                    if vertical:
                        x = a[0]
                        if ox0 <= x <= ox1:
                            continue
                        near = (ox0 - x) if x < ox0 else (x - ox1)
                        if min(a[1], b[1]) - oy1 > 1.0 or oy0 - max(a[1], b[1]) > 1.0:
                            continue
                        cands = (
                            [round(ox0 - need, 3), round(ox0 + need, 3)]
                            if x < ox0
                            else [round(ox1 + need, 3), round(ox1 - need, 3)]
                        )
                    else:
                        y = a[1]
                        if oy0 <= y <= oy1:
                            continue
                        near = (oy0 - y) if y < oy0 else (y - oy1)
                        if min(a[0], b[0]) - ox1 > 1.0 or ox0 - max(a[0], b[0]) > 1.0:
                            continue
                        cands = (
                            [round(oy0 - need, 3), round(oy0 + need, 3)]
                            if y < oy0
                            else [round(oy1 + need, 3), round(oy1 - need, 3)]
                        )
                    if near > need:
                        continue
                    clearance = _escape_obstacle_clearance(seg.width)
                    applied = False
                    for cand in cands:
                        if vertical:
                            new_a, new_b = (cand, a[1]), (cand, b[1])
                        else:
                            new_a, new_b = (a[0], cand), (b[0], cand)
                        legs = [(seg, new_a, new_b)]
                        for nb in (used[idx - 1], used[idx + 1]):
                            na, nb2 = tuple(nb.start), tuple(nb.end)
                            if _near(na, a):
                                na = new_a
                            if _near(na, b):
                                na = new_b
                            if _near(nb2, a):
                                nb2 = new_a
                            if _near(nb2, b):
                                nb2 = new_b
                            legs.append((nb, na, nb2))
                        ok = True
                        for s2, p1, p2 in legs:
                            if math.dist(p1, p2) < 1e-6:
                                ok = False
                                break
                            shape = LineString([p1, p2]).buffer(s2.width / 2 + clearance)
                            if any(shape.intersects(o) for o in obstacles):
                                ok = False
                                break
                        if not ok:
                            continue
                        for s2, p1, p2 in legs:
                            pcb.remove_segments([s2])
                            pcb.add_trace(
                                p1,
                                p2,
                                width=s2.width,
                                layer=s2.layer,
                                net=s2.net_number,
                                dedupe=False,
                            )
                        applied = True
                        break
                    if applied:
                        nudged += 1
                        ctx = _EscapeContext(pcb)
                        break
    return nudged


def _stabilize_escapes(pcb):
    """Make the recipe's pad escapes survive the release cleanup chain.

    Runs after ``_square_pad_escapes``: strip covered spans, collapse
    avoidable bends, split pass-through pad centres, and nudge corridor legs
    off their pad's mask-opening band, so ``kct readiness --generate``'s
    pad-guarded cleanup (dedupe/prune/optimize-traces) finds nothing left to
    rewrite beside a pad (#9467).
    """
    stripped = _strip_covered_spans(pcb)
    minimalized = _minimal_escapes(pcb)
    trimmed = _trim_pass_throughs(pcb)
    nudged = _nudge_corridors(pcb)
    stripped += _strip_covered_spans(pcb)
    print(
        f"Board02: stabilized escapes: covered-stripped={stripped} "
        f"minimalized={minimalized} pass-throughs-trimmed={trimmed} nudged={nudged}"
    )
    return stripped + minimalized + trimmed + nudged


def _square_escapes_file(path):
    """Load ``path``, slide ring-overlapping vias, re-square pad escapes, save."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(path)
    slid = _slide_ring_vias(pcb)
    squared = _square_pad_escapes(pcb)
    stabilized = _stabilize_escapes(pcb)
    if slid or squared or stabilized:
        pcb.save(path)
    return slid, squared


def _net_vias(path):
    """``(net name, x, y)`` of every via, in the file's absolute frame."""
    doc = parse_file(path)
    names = {n.get_int(0): n.get_string(1) for n in doc.find_children("net")}
    vias = set()
    for via in doc.find_all("via"):
        at, net = via.find("at"), via.find("net")
        name = names.get(net.get_int(0), net.get_string(0)) if net is not None else None
        vias.add((name, round(at.get_float(0), 3), round(at.get_float(1), 3)))
    return vias


def _relocation_record(before, after, origin):
    """Pair each via finalization added with the nearest one it removed, per net.

    Issue #8979: ``output/readiness/drill-relocations.json`` is the reviewed
    list ``package_release.py``'s ``smt_drill_clearance`` check re-locates on
    the shipped board.  It used to be hand-recorded against one historical
    route, so any regeneration orphaned it; deriving it from the vias this
    recipe actually moved keeps it bound to the copper it describes.
    """
    removed = sorted(before - after, key=str)
    record = []
    for net, x, y in sorted(after - before, key=str):
        same = [v for v in removed if v[0] == net]
        if not same:
            raise ValueError(f"Board02 finalization added an unpaired {net} via at {(x, y)}")
        old = min(same, key=lambda v: (v[1] - x) ** 2 + (v[2] - y) ** 2)
        removed.remove(old)
        record.append(
            {
                "net": net,
                "from": [round(old[1] - origin[0], 3), round(old[2] - origin[1], 3)],
                "to": [round(x - origin[0], 3), round(y - origin[1], 3)],
                "absolute_from": [old[1], old[2]],
                "absolute_to": [x, y],
            }
        )
    return record


def _native_report(path):
    report = path.with_suffix(".native-drc.json")
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--refill-zones",
            "--format",
            "json",
            "--units",
            "mm",
            "-o",
            str(report),
            str(path),
        ],
        check=True,
        timeout=120,
    )
    return json.loads(report.read_text())


def _lvs_report(schematic, path):
    result = subprocess.run(
        [sys.executable, "-m", "kicad_tools.lvs.copper_lvs", str(schematic), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    data = json.loads(result.stdout)
    if result.returncode not in (0, 1):
        raise ValueError(f"Copper LVS failed: {result.stderr}")
    return data


def _geometry(pcb):
    from kicad_tools.core.board_outline import outline_graphics

    return (
        pcb.board_origin,
        pcb.board_size,
        tuple(serialize_sexp(n) for n in outline_graphics(pcb._sexp)),
        tuple(
            (
                fp.reference,
                fp.position,
                fp.rotation,
                fp.layer,
                tuple(
                    (
                        p.number,
                        p.position,
                        p.size,
                        p.rotation,
                        p.shape,
                        p.drill,
                        tuple(p.layers),
                        p.net_number,
                        p.net_name,
                    )
                    for p in fp.pads
                ),
            )
            for fp in pcb.footprints
        ),
    )


def _finalize_candidate(path, schematic):
    from native_via_repair import remove_reported_leaves, repair_report_vias

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    before = _geometry(PCB.load(path))
    routed_vias = _net_vias(path)
    baseline = _lvs_report(schematic, path)
    pcb = parse_file(path)
    _relocate_escapes(pcb)
    path.write_text(serialize_sexp(pcb))
    quantize_pcb_file(path)
    _relocate_pad_edge(path)
    _square_escapes_file(path)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "fix-silkscreen",
            str(path),
            "--mfr",
            "jlcpcb",
            "--format",
            "summary",
        ],
        check=True,
        timeout=120,
    )
    data = _native_report(path)
    if data["violations"]:
        pcb = PCB.load(path)
        if repair_report_vias(path, pcb, data):
            pcb.save(path)
            data = _native_report(path)
            pcb = PCB.load(path)
            if remove_reported_leaves(pcb, data):
                pcb.save(path)
                data = _native_report(path)
    if data["violations"] or data["unconnected_items"] or data["schematic_parity"]:
        print(json.dumps(data, indent=2), file=sys.stderr)
        return False
    pcb = PCB.load(path)
    if ViaInPadRule().check(pcb, None).violations:
        raise ValueError("Board02 via-in-pad gate failed")
    if _geometry(pcb) != before:
        raise ValueError("Board02 pad or outline geometry changed")
    lvs = _lvs_report(schematic, path)
    if (
        not lvs["clean"]
        or lvs["bound_pad_count"] <= 0
        or lvs["bound_pad_count"] != baseline["bound_pad_count"]
    ):
        raise ValueError(f"Board02 copper LVS gate failed: {lvs}")
    return _relocation_record(routed_vias, _net_vias(path), pcb.board_origin)


def finalize_routing(path: Path, schematic_path: Path | None = None) -> bool:
    """Commit only a fully qualified scratch result; failures retain original bytes."""
    path = Path(path).resolve()
    schematic = schematic_path or path.with_name(path.stem.removesuffix("_routed") + ".kicad_sch")
    try:
        with tempfile.TemporaryDirectory(prefix="board02-finalize-", dir=path.parent) as scratch:
            candidate = Path(scratch) / path.name
            shutil.copy2(path, candidate)
            for suffix in (".kicad_pro", ".kicad_dru"):
                sidecar = path.with_suffix(suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, candidate.with_suffix(suffix))
            relocations = _finalize_candidate(candidate, schematic)
            if relocations is False:
                return False
            # The scratch directory is on the same filesystem; replacement is atomic.
            candidate.replace(path)
            record = path.parent / "readiness" / "drill-relocations.json"
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps(relocations, indent=2) + "\n")
            return True
    except Exception as exc:
        print(f"Board02 finalization failed; original PCB retained: {exc}", file=sys.stderr)
        return False
