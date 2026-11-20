"""Layout-quality metrics over a parsed PCB (issue #9594).

One pass over a board's copper producing the numbers a layout reviewer
asks for first: the track-vs-ground via split, per-net corner counts,
total track length, outline area, and differential-pair skew. Consumed by
``kct board-metrics`` (``board.json``'s ``layout_quality`` block) and by
the fleet-router benchmark's per-case fingerprint.

Method credit: the PCBJam blind-PCB study
(https://www.pcbjam.com/blog/fable-blind-pcb-layout/) popularised this
metric set. Its scoring scripts (``scoring/lab-metrics.cjs`` and
siblings, MIT licence) informed the *definitions* only; this
implementation was written independently from the method description in
issue #9594, so no code is reused and no attribution obligation applies.

Corner classes
--------------

Corners are counted per net over same-net, same-layer joins of straight
segments, reusing :class:`~kicad_tools.explain.checks.acid_trap.
AcidTrapCheck`'s junction/angle logic -- the same logic the
``sharp_trace_corner`` DRC rule runs (no second copy). The angle is the
interior angle between the two legs at the shared endpoint:

* ``turn_back`` -- angle below :data:`TURN_BACK_MAX_ANGLE_DEG` (10 deg).
  Overlapping copper or a dead-end stub, usually router debris after
  rip-up. Passes DRC; it is what you clean up before ordering.
* ``acute`` -- from :data:`TURN_BACK_MAX_ANGLE_DEG` up to (excluding)
  ``MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG`` (89 deg): the acid-trap
  range ``sharp_trace_corner`` already warns on.
* ``right_angle`` -- 89..91 deg inclusive (``ANGLE_TOLERANCE_DEG`` around
  90): a rounded right angle. The study found router output carried 2-5x
  the right angles of hand or scripted copper.

Obtuse bends (a 45-deg chamfer has a 135-deg interior angle) and
straight pass-throughs (180 deg) are not corners and are not counted.

Vertices sitting under same-net pad/via copper are joints, not bends,
and are skipped exactly as :mod:`kicad_tools.validate.rules.
sharp_trace_corner` skips them. Stacked duplicate segments count once
(the issue #9037 handling). Arcs contribute length but no corners: a
curved track has no discrete bend vertex, and chord angles would
misreport smooth copper.

Via classification
------------------

A via is a *ground via* when its net name classifies as
``NetClass.GROUND`` under the router's name patterns
(:func:`~kicad_tools.router.net_class.classify_from_name`: GND, GNDA,
GNDD, AGND, DGND, PGND, SGND, VSS, GROUND, CGND, CHASSIS, EARTH, SHIELD,
and the ``_GND``/``_VSS``/``_AGND``/``_DGND`` suffixes). Every other via
-- including net-less vias -- is a *track via*: copper carrying a signal
or supply to another layer, where a high count means the routing ran out
of room.

Pair and match-group skew
------------------------

Per differential pair, skew comes from the existing
:class:`~kicad_tools.analysis.trace_length.TraceLengthAnalyzer`
(``find_differential_pairs`` + ``analyze_diff_pair``); it is exposed
here, not reimplemented. Match-group skew needs the router's
net-class map (``validate.match_group_skew.derive_group_skew_data``),
which neither consumer has on its own: callers that know a map (the
board's declared ``artifacts.net_class_map_sidecar``, the benchmark's
frozen ``routing_sidecars``) pass it in and the block grows a
``match_group_skew`` entry; without one the key is simply absent.

Not a check
-----------

These are metrics, not findings: nothing here is, or feeds, a ``kct
check`` rule. ``sharp_trace_corner`` already warns on acute corners and
turn-backs and its 90 deg threshold is unverified against fab guidance
(see its docstring); no new rule and no new threshold are introduced
here (recorded decision on issue #9594).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "TURN_BACK_MAX_ANGLE_DEG",
    "LayoutQualityReport",
    "analyze_layout_quality",
]

#: A same-net join whose interior angle is below this is a turn-back
#: (overlapping copper / dead-end stub), not merely an acute corner.
#: Mirrors the PCBJam study's turn-back definition; not a fab-derived
#: threshold and deliberately not a ``kct check`` limit.
TURN_BACK_MAX_ANGLE_DEG = 10.0

_CORNER_CLASSES = ("right_angle", "acute", "turn_back")

# Vertex keys round coordinates to 2 decimals, the same granularity the
# sharp-trace-corner rule uses to group findings at one vertex.
_VERTEX_DECIMALS = 2
# Junction-candidate lattice for ``_count_corners`` (issue #9658), in
# cells per mm.  The cell size (1/100 mm) equals the per-axis 0.01 mm
# tolerance ``AcidTrapCheck._find_junction`` joins endpoints with, so two
# endpoints it would pair can never land more than one lattice cell apart
# on an axis and each occupied cell only needs its 3x3 neighbourhood to
# enumerate every candidate pair.  Keep the cell size <= that tolerance
# (i.e. this value >= 100); the bucketing is a pure candidate filter and
# ``_find_junction`` still decides every junction.
_JUNCTION_LATTICE_PER_MM = 100
# Endpoint-join tolerance for Edge.Cuts polygonization, in mm. KiCad writes
# coordinates at 1e-6 mm (1 nm) precision, so the two copies of one shared
# corner -- a board graphic's written value and a rotated footprint endpoint
# (rotation, then the board-origin subtraction) -- can differ by roughly one
# file unit in either axis. Fixed-grid rounding cannot join them: a pair
# straddling a rounding boundary (``7.071068`` vs ``7.071069``) lands in
# different cells however coarse the grid. Endpoints within this distance are
# therefore clustered and moved to one shared point (<= a few nm, ~1e-5 mm^2
# on a 100 mm board), far below any real feature of a board outline.
_OUTLINE_JOIN_TOLERANCE_MM = 2e-6


def _cluster_endpoints(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    tol: float = _OUTLINE_JOIN_TOLERANCE_MM,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Move endpoints that lie within ``tol`` of each other onto one point.

    Union-find over a ``tol``-sized spatial hash (each point checks its 3x3
    neighbouring cells), so it is near-linear. Each cluster collapses onto its
    first-seen member, which makes the result deterministic and keeps
    exactly-equal endpoints equal.
    """
    points = [pt for seg in segments for pt in seg]
    parent = list(range(len(points)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple[int, int], list[int]] = {}
    for idx, (x, y) in enumerate(points):
        cx, cy = math.floor(x / tol), math.floor(y / tol)
        for nx in (cx - 1, cx, cx + 1):
            for ny in (cy - 1, cy, cy + 1):
                for other in cells.get((nx, ny), ()):
                    ox_, oy_ = points[other]
                    if math.hypot(x - ox_, y - oy_) <= tol:
                        ra, rb = find(idx), find(other)
                        if ra != rb:
                            parent[max(ra, rb)] = min(ra, rb)
        cells.setdefault((cx, cy), []).append(idx)
    snapped = [(points[find(i)][0] + 0.0, points[find(i)][1] + 0.0) for i in range(len(points))]
    return [(snapped[2 * i], snapped[2 * i + 1]) for i in range(len(segments))]


def _classify_angle(angle: float | None) -> str | None:
    """Bucket one interior angle into a corner class (or ``None``).

    The acute/right-angle window is the shared acid-trap logic's own
    (``MIN_TRACE_ANGLE_DEG``/``ANGLE_TOLERANCE_DEG``), imported rather
    than copied. Imports stay function-local: ``kicad_tools.explain``
    pulls in the whole checks package, which must not import this
    package back at module load.
    """
    from kicad_tools.explain.checks.acid_trap import ANGLE_TOLERANCE_DEG, MIN_TRACE_ANGLE_DEG

    if angle is None:
        return None
    if angle < TURN_BACK_MAX_ANGLE_DEG:
        return "turn_back"
    if angle < MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG:
        return "acute"
    if angle <= MIN_TRACE_ANGLE_DEG + ANGLE_TOLERANCE_DEG:
        return "right_angle"
    return None  # obtuse bend or straight pass-through: not a corner


def _net_raw_name(pcb: PCB, net_number: int, seg_net_name: str) -> str:
    """The net name as pad/via copper records it (burial matching)."""
    if seg_net_name:
        return seg_net_name
    net = pcb.get_net(net_number)
    return net.name if net else ""


def _net_display_name(pcb: PCB, net_number: int, raw_name: str) -> str:
    if raw_name:
        return raw_name
    return f"Net {net_number}"


def _count_corners(pcb: PCB) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Count same-net joins by corner class; totals and per-net tallies.

    Groups segments by (net, layer), collects the legs meeting at each
    junction vertex (``AcidTrapCheck._find_junction``), skips vertices
    buried under same-net pad/via copper, dedupes stacked duplicate
    segments, and classifies every distinct-leg pair angle.

    Junction candidates are bucketed per endpoint on the
    :data:`_JUNCTION_LATTICE_PER_MM` lattice instead of comparing every
    same-group pair (issue #9658): the walk below still visits candidate
    pairs in ``(i, j)`` index order and still lets ``_find_junction``
    decide every junction, so the vertices, their legs and the published
    counts are exactly what the all-pairs search produced -- at roughly
    linear cost per group instead of quadratic.
    """
    from kicad_tools.explain.checks.acid_trap import AcidTrapCheck
    from kicad_tools.validate.rules.sharp_trace_corner import _copper_discs, _is_buried

    check = AcidTrapCheck()
    discs = _copper_discs(pcb)

    # KiCad-10 name-only boards give every segment net_number 0, so the
    # net name is part of the group key: grouping by number alone would
    # merge distinct nets into one population.
    groups: dict[tuple[int, str, str], list[Any]] = {}
    for seg in pcb.segments:
        groups.setdefault((seg.net_number, seg.net_name, seg.layer), []).append(seg)

    # One vertex per (group, rounded junction): every same-group segment
    # pair sharing an endpoint contributes both legs to it.
    vertices: dict[tuple, tuple[tuple[float, float], list[Any]]] = {}
    for key, segments in groups.items():
        # Bucket endpoint indices on the lattice: two endpoints within
        # _find_junction's per-axis 0.01 mm tolerance land in cells at
        # most one apart on each axis, so pairing each occupied cell
        # with its 3x3 neighbourhood reaches every junction candidate.
        cells: dict[tuple[int, int], list[int]] = {}
        for idx, seg in enumerate(segments):
            for point in (seg.start, seg.end):
                cells.setdefault(
                    (
                        round(point[0] * _JUNCTION_LATTICE_PER_MM),
                        round(point[1] * _JUNCTION_LATTICE_PER_MM),
                    ),
                    [],
                ).append(idx)
        candidates: set[tuple[int, int]] = set()
        for (cx, cy), idxs in cells.items():
            for i, a in enumerate(idxs):
                for b in idxs[i + 1 :]:
                    candidates.add((a, b) if a < b else (b, a))
            # The four forward neighbours cover every unordered pair of
            # cells within Chebyshev distance 1 exactly once.
            for dx, dy in ((-1, 1), (0, 1), (1, 1), (1, 0)):
                for a in idxs:
                    for b in cells.get((cx + dx, cy + dy), ()):
                        candidates.add((a, b) if a < b else (b, a))
        # (i, j) order over the candidates reproduces the all-pairs
        # walk's pair order, so first-junction wins and leg order match
        # the previous implementation exactly.
        for i, j in sorted(candidates):
            seg1, seg2 = segments[i], segments[j]
            junction = check._find_junction(seg1, seg2)
            if junction is None:
                continue
            vkey = (
                key,
                round(junction[0], _VERTEX_DECIMALS),
                round(junction[1], _VERTEX_DECIMALS),
            )
            entry = vertices.setdefault(vkey, (junction, []))
            legs = entry[1]
            if seg1 not in legs:
                legs.append(seg1)
            if seg2 not in legs:
                legs.append(seg2)

    totals: dict[str, int] = dict.fromkeys(_CORNER_CLASSES, 0)
    per_net: dict[str, dict[str, int]] = {}
    for vkey, (junction, legs) in vertices.items():
        net_number, seg_net_name, layer = vkey[0]
        raw_name = _net_raw_name(pcb, net_number, seg_net_name)
        # A vertex under same-net pad/via copper is a joint (pad-centre
        # fan-in), not a bend -- same exemption as sharp_trace_corner.
        if _is_buried(discs, raw_name, layer, junction):
            continue
        # Stacked duplicate segments (an art artifact) count once, so a
        # duplicated bend is not double-counted (issue #9037 handling).
        distinct: list[Any] = []
        seen_edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
        for seg in legs:
            pa = (round(seg.start[0], _VERTEX_DECIMALS), round(seg.start[1], _VERTEX_DECIMALS))
            pb = (round(seg.end[0], _VERTEX_DECIMALS), round(seg.end[1], _VERTEX_DECIMALS))
            edge = (pa, pb) if pa <= pb else (pb, pa)
            if edge not in seen_edges:
                seen_edges.add(edge)
                distinct.append(seg)
        if len(distinct) < 2:
            continue
        display = _net_display_name(pcb, net_number, raw_name)
        for i, seg1 in enumerate(distinct):
            for seg2 in distinct[i + 1 :]:
                cls = _classify_angle(check._calculate_angle(seg1, seg2, junction))
                if cls is None:
                    continue
                totals[cls] += 1
                per_net.setdefault(display, dict.fromkeys(_CORNER_CLASSES, 0))[cls] += 1
    return totals, per_net


def _via_split(pcb: PCB) -> tuple[int, int]:
    """``(track, ground)`` via counts under the documented net-name rule."""
    from kicad_tools.router.net_class import NetClass, classify_from_name

    track = 0
    ground = 0
    for via in pcb.vias:
        if classify_from_name(via.net_name or "") is NetClass.GROUND:
            ground += 1
        else:
            track += 1
    return track, ground


def _total_track_length_mm(pcb: PCB) -> float:
    """Summed straight-segment centreline length plus analytic arc length."""
    total = 0.0
    for seg in pcb.segments:
        total += math.dist(seg.start, seg.end)
    for arc in pcb.arcs:
        total += arc.length
    return total


def _outline_area_mm2(pcb: PCB) -> float | None:
    """Area enclosed by the Edge.Cuts outline, or ``None`` when unsupported.

    Every Edge.Cuts contour is measured -- top-level graphics *and*
    footprint-owned ones (a board-edge cutout often lives as ``fp_rect``
    graphics inside a connector footprint). Straight elements (lines,
    rects, polys) contribute exact segments -- a radius-bearing rect or a
    poly whose ``pts`` mixes in ``arc`` entries keeps its curves through
    the same certified tessellators; other curved elements are
    tessellated
    to the certified accuracy of :mod:`kicad_tools.core.board_outline` /
    :mod:`kicad_tools.core.outline_tessellation` (arcs/cubics within
    ``DEFAULT_MAX_ERROR_MM`` chords, circles within ~1e-5 mm), so the
    published area is exact on straight-edge boards and bounded by
    curve length times that error otherwise.

    ``polygonize_full`` returns the faces of the line arrangement: the
    outer face *with its holes punched out*, each cutout face, and any
    nested island face, so the material area is the sum of faces at even
    nesting depth (outer and islands in cutouts), skipping odd-depth
    cutout faces. Nesting depth is the same probe
    ``router.mesh.pathfinder.interior_edge_loops`` uses (issue #6822):
    each face's *exterior* ring (a punched hole would defeat containment
    on the face itself), largest first, counting how many of the larger
    exterior rings contain the ring's representative point.

    Geometry this module cannot measure returns ``None`` rather than an
    inaccurate area -- the same policy as the development board-size
    metadata: ``fp_curve`` Edge.Cuts graphics (not parsed into the
    footprint model) and anything the certified tessellators refuse
    (malformed or uncertifiable coordinates). Open, crossing, malformed
    or absent outlines return ``None`` too.
    """
    from shapely.errors import ShapelyError  # type: ignore[import-untyped]
    from shapely.geometry import MultiLineString, Polygon  # type: ignore[import-untyped]
    from shapely.ops import polygonize_full  # type: ignore[import-untyped]

    try:
        segments = _edge_cuts_segments(pcb)
    except ValueError:
        return None
    if not segments:
        return None
    # Cluster endpoints (see _OUTLINE_JOIN_TOLERANCE_MM) so shared corners
    # computed through different float paths compare equal; exact-coordinate
    # polygonization would otherwise see an open contour.
    snapped = _cluster_endpoints(segments)
    segments = [seg for seg in snapped if seg[0] != seg[1]]
    if not segments:
        return None
    try:
        lines = MultiLineString(segments)
        if not lines.is_simple:  # crossings and overlapping edges
            return None
        polygons, cuts, dangles, invalid = polygonize_full(lines)
        if polygons.is_empty or not all(g.is_empty for g in (cuts, dangles, invalid)):
            return None
        faces = [face for face in polygons.geoms if face.area > 0]
        rings = sorted(
            ((face, Polygon(face.exterior)) for face in faces),
            key=lambda pair: pair[1].area,
            reverse=True,
        )
        area = 0.0
        for idx, (face, ring) in enumerate(rings):
            probe = ring.representative_point()
            depth = sum(1 for _, other in rings[:idx] if other.contains(probe))
            if depth % 2 == 0:
                area += face.area
        return area if math.isfinite(area) and area > 0 else None
    except (ValueError, TypeError, OverflowError, ShapelyError):
        return None


def _authored_radius_mm(node: Any) -> float:
    """A ``gr_rect``/``fp_rect`` ``(radius ...)`` token; 0.0 when absent.

    Absent, non-numeric or non-positive tokens mean square corners, the
    same lenience ``panel.outline``'s rect parsing applies.
    """
    child = node.find_child("radius")
    if child is None:
        return 0.0
    value = child.get_float(0)
    if value is None or not math.isfinite(value) or value <= 0:
        return 0.0
    return float(value)


def _pts_contains_arc(node: Any) -> bool:
    """Whether a ``gr_poly``/``fp_poly`` ``pts`` mixes in ``arc`` entries."""
    pts = node.find_child("pts")
    return pts is not None and any(child.name == "arc" for child in pts.iter_children())


def _node_point(node: Any, tag: str) -> tuple[float, float]:
    """An authored ``(tag x y)`` coordinate pair of a graphic node."""
    child = node.find_child(tag)
    if child is None:
        raise ValueError(f"Malformed Edge.Cuts {node.tag}: missing {tag} coordinate")
    x, y = child.get_float(0), child.get_float(1)
    if x is None or y is None or not math.isfinite(x) or not math.isfinite(y):
        raise ValueError(f"Malformed Edge.Cuts {node.tag}: invalid {tag} coordinate")
    return (x, y)


def _rounded_rect_boundary(
    start: tuple[float, float],
    end: tuple[float, float],
    radius: float,
    map_point: Callable[[tuple[float, float]], tuple[float, float]],
) -> list[tuple[float, float]]:
    """Tessellated closed boundary of a radius-bearing rectangle.

    Each corner is a 90-degree arc given the 45-degree midpoint form
    ``k = r * (1 - sqrt(0.5))`` -- the same corner split
    ``panel.outline._rect_primitives`` emits for the fab outline -- so a
    rounded rectangle measures here exactly what the panel flow routes.
    The radius is clamped to half the shorter side like the panel flow
    (a larger authored radius is a stadium, not an error), and a
    non-positive one falls back to square corners. ``map_point`` places
    the authored sheet/footprint coordinates into board space; being a
    rigid transform it preserves every circle, so tessellating after it
    is exact.
    """
    from kicad_tools.core.outline_tessellation import tessellate_arc

    x0, x1 = sorted((start[0], end[0]))
    y0, y1 = sorted((start[1], end[1]))
    r = max(0.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    chain: list[tuple[float, float]] = []

    def extend(points: list[tuple[float, float]]) -> None:
        for point in points:
            if not chain or math.dist(chain[-1], point) > 1e-9:
                chain.append(point)

    if r <= 1e-9:
        extend([map_point(corner) for corner in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))])
        return chain
    k = r * (1.0 - math.sqrt(0.5))
    pieces: list[tuple[bool, tuple[tuple[float, float], ...]]] = [
        (False, ((x0 + r, y0), (x1 - r, y0))),
        (True, ((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r))),
        (False, ((x1, y0 + r), (x1, y1 - r))),
        (True, ((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1))),
        (False, ((x1 - r, y1), (x0 + r, y1))),
        (True, ((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r))),
        (False, ((x0, y1 - r), (x0, y0 + r))),
        (True, ((x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0))),
    ]
    for is_arc, authored in pieces:
        mapped = [map_point(point) for point in authored]
        extend(tessellate_arc(mapped[0], mapped[1], mapped[2]) if is_arc else mapped)
    if math.dist(chain[0], chain[-1]) <= 1e-9:
        chain.pop()  # explicit closure: the caller's wraparound re-adds it
    return chain


def _poly_boundary_chain(
    node: Any,
    map_point: Callable[[tuple[float, float]], tuple[float, float]],
) -> list[tuple[float, float]]:
    """Tessellated closed boundary of a ``gr_poly``/``fp_poly``.

    ``pts`` may mix ``xy`` vertices with three-point ``arc`` entries (the
    KiCad 8 polygon encoding); the footprint model keeps only the ``xy``
    run, which silently squares every arc off. Entries are joined in
    authored order, arcs tessellated with the certified tessellator, and
    any gap bridged straight -- the joining rule
    ``panel.outline._poly_primitives`` applies.
    """
    from kicad_tools.core.outline_tessellation import tessellate_arc

    pts = node.find_child("pts")
    if pts is None:
        raise ValueError(f"Malformed Edge.Cuts {node.tag}: missing points")
    chain: list[tuple[float, float]] = []
    for child in pts.iter_children():
        points: list[tuple[float, float]]
        if child.name == "xy":
            x, y = child.get_float(0), child.get_float(1)
            if x is None or y is None or not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"Malformed Edge.Cuts {node.tag}: invalid xy coordinate")
            points = [map_point((x, y))]
        elif child.name == "arc":
            mapped = [_node_point(child, tag) for tag in ("start", "mid", "end")]
            points = tessellate_arc(
                map_point(mapped[0]), map_point(mapped[1]), map_point(mapped[2])
            )
        else:
            continue
        for point in points:
            if not chain or math.dist(chain[-1], point) > 1e-9:
                chain.append(point)
    if len(chain) < 3:
        raise ValueError(f"Malformed Edge.Cuts {node.tag}: missing points")
    if math.dist(chain[0], chain[-1]) <= 1e-9:
        chain.pop()  # explicit closure: the caller's wraparound re-adds it
    return chain


def _edge_cuts_segments(pcb: PCB) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Straight-segment chains for every Edge.Cuts contour, board-relative.

    Raises:
        ValueError: Any Edge.Cuts geometry this module cannot measure
            (``fp_curve``, malformed or uncertifiable contours).
    """
    from kicad_tools.core.board_outline import board_outline_segments
    from kicad_tools.core.geometry import rotate_pad_offset
    from kicad_tools.core.outline_tessellation import tessellate_arc
    from kicad_tools.sexp import SExp

    # Top-level graphics: the certified routing-outline builder measures
    # every gr_* kind (straight elements exactly, arcs/cubics/circles to
    # a certified Hausdorff bound) and refuses what it cannot certify --
    # except the two curved encodings it flattens: a radius-bearing
    # gr_rect loses its rounded corners and a gr_poly whose pts mixes in
    # arc entries loses every arc to the xy run alone, so both used to
    # publish the square-corner area (review finding on #9614). Those two
    # kinds are measured below with the same certified tessellators;
    # every other kind stays delegated. Footprint blocks are excluded
    # from the delegated root entirely: main's outline_graphics re-emits
    # their Edge.Cuts graphics as board-coordinate gr_* nodes (squaring
    # radius corners and dropping pts arcs the same way), so delegating
    # them would double-count the footprint loop below and undo its
    # curves; both spellings of the block tag, like the schema's own
    # footprint predicate. Coordinates arrive sheet-absolute; footprints
    # below are already board-relative, so shift by the detected board
    # origin to match.
    ox, oy = getattr(pcb, "_board_origin", (0.0, 0.0))

    def sheet_to_board(point: tuple[float, float]) -> tuple[float, float]:
        return (point[0] - ox, point[1] - oy)

    def is_edge_cuts(node: Any) -> bool:
        layer = node.find_child("layer")
        return layer is not None and layer.get_string(0) == "Edge.Cuts"

    deferred: list[Any] = []
    rest: list[Any] = []
    for node in pcb._sexp.iter_children():
        if (
            not node.is_atom
            and node.tag in ("gr_rect", "gr_poly")
            and is_edge_cuts(node)
            and (
                (node.tag == "gr_rect" and _authored_radius_mm(node) > 0.0)
                or (node.tag == "gr_poly" and _pts_contains_arc(node))
            )
        ):
            deferred.append(node)
        elif not node.is_atom and node.tag in ("footprint", "module"):
            continue  # measured by the footprint loop below, never delegated
        else:
            rest.append(node)
    segments = [
        (sheet_to_board(a), sheet_to_board(b))
        for a, b in board_outline_segments(SExp(children=rest))
    ]
    for node in deferred:
        if node.tag == "gr_rect":
            chain = _rounded_rect_boundary(
                _node_point(node, "start"),
                _node_point(node, "end"),
                _authored_radius_mm(node),
                sheet_to_board,
            )
        else:
            chain = _poly_boundary_chain(node, sheet_to_board)
        segments.extend(zip(chain, chain[1:] + chain[:1], strict=True))

    def fp_point(fp: Any, local: tuple[float, float] | None) -> tuple[float, float]:
        if local is None:
            raise ValueError("Edge.Cuts footprint graphic: missing coordinates")
        dx, dy = rotate_pad_offset(local[0], local[1], fp.rotation)
        return (fp.position[0] + dx, fp.position[1] + dy)

    def add_chain(chain: list[tuple[float, float]]) -> None:
        segments.extend(zip(chain, chain[1:] + chain[:1], strict=True))

    for fp in pcb.footprints:
        # fp_curve is not parsed into the footprint model at all, so an
        # Edge.Cuts one would silently vanish: refuse the whole area.
        # This guard depends on _sexp_node being linked — PCB's
        # constructor runs _link_footprint_sexp_nodes(), which every
        # production parse path goes through; without the link the guard
        # is skipped and an fp_curve cutout would vanish instead of
        # forcing None.
        fp_node = getattr(fp, "_sexp_node", None)
        raw_rects: list[Any] = []
        raw_polys: list[Any] = []
        if fp_node is not None:
            for child in fp_node.iter_children():
                if child.tag == "fp_curve" and is_edge_cuts(child):
                    raise ValueError("Edge.Cuts fp_curve is not modelled")
            # Rect corner radii and pts arc entries are not parsed into
            # the footprint model either, so rects and polys are measured
            # from the authored node to keep their curves (the model
            # branch below would square them off). find_all matches the
            # model's own collection exactly.
            for child in fp_node.find_all("fp_rect"):
                if is_edge_cuts(child):
                    raw_rects.append(child)
            for child in fp_node.find_all("fp_poly"):
                if is_edge_cuts(child):
                    raw_polys.append(child)

        def fp_map(point: tuple[float, float]) -> tuple[float, float]:
            return fp_point(fp, point)

        for graphic in fp.graphics:
            if graphic.layer != "Edge.Cuts":
                continue
            kind = graphic.graphic_type
            if kind in ("rect", "poly") and fp_node is not None:
                # Measured from the authored node above/below instead.
                continue
            if kind == "line":
                start, end = fp_point(fp, graphic.start), fp_point(fp, graphic.end)
                segments.append((start, end))
            elif kind == "rect":
                # Transform all four local corners, exactly like the poly
                # branch: synthesizing the other two in board space would
                # rebuild an axis-aligned box from two rotated points and
                # shrink a cutout at any non-multiple-of-90 rotation.
                (lx1, ly1), (lx2, ly2) = graphic.start, graphic.end
                add_chain(
                    [
                        fp_point(fp, corner)
                        for corner in ((lx1, ly1), (lx2, ly1), (lx2, ly2), (lx1, ly2))
                    ]
                )
            elif kind == "arc":
                chain = tessellate_arc(
                    fp_point(fp, graphic.start),
                    fp_point(fp, graphic.mid),
                    fp_point(fp, graphic.end),
                )
                # Equal-length slices like board_outline_segments' gr_arc
                # branch: zip(chain, chain[1:], strict=True) is always
                # unequal (the tail is one shorter) and raised for every
                # tessellated footprint arc, dropping the whole area.
                segments.extend(zip(chain[:-1], chain[1:], strict=True))
            elif kind == "poly":
                points = [fp_point(fp, point) for point in graphic.points]
                if len(points) < 3:
                    raise ValueError("Edge.Cuts fp_poly: missing points")
                add_chain(points)
            elif kind == "circle":
                from kicad_tools.core.board_outline import circle_tessellation_points

                if graphic.center is None or not graphic.radius:
                    raise ValueError("Edge.Cuts fp_circle: missing centre or radius")
                add_chain(circle_tessellation_points(fp_point(fp, graphic.center), graphic.radius))
            else:  # pragma: no cover - the parser only yields the kinds above
                raise ValueError(f"Edge.Cuts footprint graphic not modelled: {kind}")
        for child in raw_rects:
            add_chain(
                _rounded_rect_boundary(
                    _node_point(child, "start"),
                    _node_point(child, "end"),
                    _authored_radius_mm(child),
                    fp_map,
                )
            )
        for child in raw_polys:
            add_chain(_poly_boundary_chain(child, fp_map))
    return segments


def _diff_pair_skews(pcb: PCB) -> list[dict[str, Any]]:
    """Per differential pair, the existing trace-length skew analysis."""
    from kicad_tools.analysis.trace_length import TraceLengthAnalyzer

    analyzer = TraceLengthAnalyzer()
    out: list[dict[str, Any]] = []
    for net_p, net_n in analyzer.find_differential_pairs(pcb):
        report = analyzer.analyze_diff_pair(pcb, net_p, net_n)
        out.append(
            {
                "net_positive": net_p,
                "net_negative": net_n,
                "length_positive_mm": round(report.report_p.total_length_mm, 3),
                "length_negative_mm": round(report.report_n.total_length_mm, 3),
                "skew_mm": round(report.skew_mm, 3),
            }
        )
    return out


def _match_group_skews(pcb: PCB, net_class_map: Any) -> dict[str, float]:
    """Per declared match group, the existing validate-side skew analysis.

    ``derive_group_skew_data`` already degrades to ``{}`` when the map is
    empty, and reports only groups whose members are all routed.
    """
    from kicad_tools.validate.match_group_skew import derive_group_skew_data

    skews, _groups, _thresholds = derive_group_skew_data(
        pcb, net_class_map, num_copper_layers=len(pcb.copper_layers)
    )
    return {name: round(skew, 3) for name, skew in sorted(skews.items())}


@dataclass
class LayoutQualityReport:
    """Layout-quality metrics for one parsed PCB.

    Attributes:
        track_vias: Vias on nets that do not classify as ground (the
            routing-exhaustion signal; includes net-less vias).
        ground_vias: Vias on ground-classified nets (stitching/plane ties).
        right_angle_corners: Same-net joins with an interior angle of
            89-91 deg, board-wide total.
        acute_corners: Same-net joins with an interior angle of 10..89 deg
            (the acid-trap population ``sharp_trace_corner`` warns on).
        turn_back_corners: Same-net joins with an interior angle under
            10 deg (overlaps / dead-end stubs).
        corners_per_net: Per-net breakdown of the three corner classes.
        total_track_length_mm: Straight segments plus analytic arcs.
        outline_area_mm2: Edge.Cuts polygon area; ``None`` when the
            outline is absent or unsupported.
        diff_pair_skews: One entry per detected differential pair.
        match_group_skews: Per declared match group (requires the
            caller-supplied net-class map), the routed skew in mm.
    """

    track_vias: int = 0
    ground_vias: int = 0
    right_angle_corners: int = 0
    acute_corners: int = 0
    turn_back_corners: int = 0
    corners_per_net: dict[str, dict[str, int]] = field(default_factory=dict)
    total_track_length_mm: float = 0.0
    outline_area_mm2: float | None = None
    diff_pair_skews: list[dict[str, Any]] = field(default_factory=list)
    match_group_skews: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Dictionary for JSON serialization (additive-board.json block)."""
        result: dict[str, Any] = {
            "via": {
                "track": self.track_vias,
                "ground": self.ground_vias,
                "total": self.track_vias + self.ground_vias,
            },
            "corners": {
                "right_angle": self.right_angle_corners,
                "acute": self.acute_corners,
                "turn_back": self.turn_back_corners,
            },
            "total_track_length_mm": round(self.total_track_length_mm, 3),
        }
        if self.corners_per_net:
            result["corners"]["per_net"] = {
                net: dict(self.corners_per_net[net]) for net in sorted(self.corners_per_net)
            }
        if self.outline_area_mm2 is not None:
            result["outline_area_mm2"] = round(self.outline_area_mm2, 3)
        if self.diff_pair_skews:
            result["diff_pair_skew"] = self.diff_pair_skews
        if self.match_group_skews:
            result["match_group_skew"] = dict(self.match_group_skews)
        return result


def analyze_layout_quality(pcb: PCB, net_class_map: Any = None) -> LayoutQualityReport:
    """Compute the layout-quality metric set for one parsed PCB.

    Pure over the board: same input, same output (all floats are rounded
    in :meth:`LayoutQualityReport.to_dict`), which is what the
    benchmark's rerun-stability assertion relies on.

    Args:
        pcb: A loaded :class:`~kicad_tools.schema.pcb.PCB`.
        net_class_map: Optional ``{net_name: NetClassRouting}`` map (the
            router sidecar convention, loaded with
            :func:`~kicad_tools.router.rules.net_class_map_from_path`).
            When supplied, declared match groups report their routed
            skew (``match_group_skew``); without it the key is absent.

    Returns:
        The populated :class:`LayoutQualityReport`.
    """
    totals, per_net = _count_corners(pcb)
    track_vias, ground_vias = _via_split(pcb)
    return LayoutQualityReport(
        track_vias=track_vias,
        ground_vias=ground_vias,
        right_angle_corners=totals["right_angle"],
        acute_corners=totals["acute"],
        turn_back_corners=totals["turn_back"],
        corners_per_net=per_net,
        total_track_length_mm=_total_track_length_mm(pcb),
        outline_area_mm2=_outline_area_mm2(pcb),
        diff_pair_skews=_diff_pair_skews(pcb),
        match_group_skews=_match_group_skews(pcb, net_class_map),
    )
