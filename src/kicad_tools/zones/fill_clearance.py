"""Post-fill clearance correction for copper zones.

Background (Issue #3711)
------------------------
``kct zones fill`` produces the committed ``filled_polygon`` copper for
each zone by running ``kicad-cli pcb drc`` (which refills all zones as a
side effect — see :func:`kicad_tools.cli.runner._run_fill_zones_via_drc`).
On boards whose footprints/zones were serialized by *kicad-tools* rather
than the KiCad GUI, the resulting fill can leave the antipad around a
**foreign-net** through-hole pad too small — the fill copper grazes or
overlaps the pad by a fraction of a millimetre.  ``kct check``'s
:class:`~kicad_tools.validate.rules.clearance.ViaZoneClearanceRule` then
reports ``clearance_pad_zone`` / ``clearance_via_zone`` errors that block
the routed-PCB DRC gate.

This module applies a deterministic, pure-Python geometric correction
*after* the fill: for every zone's ``filled_polygon`` it subtracts an
antipad around each pad/via that belongs to a **different** net,
guaranteeing at least the zone clearance between the fill copper and any
foreign-net copper.  Same-net pads/vias are never subtracted, so thermal
relief / solid connections to the zone's own net are preserved.

The correction operates directly on the parsed S-expression document so it
needs neither kicad-cli nor the C++ router — only optional ``shapely`` for
the polygon difference.  When ``shapely`` is unavailable the correction is
skipped (the caller logs a hint) rather than producing a wrong fill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from kicad_tools.core.layers import via_spans_layer
from kicad_tools.sexp import SExp


class _Geometry(Protocol):
    """Minimal structural type for the shapely geometry methods used here.

    shapely ships no type stubs, so the concrete classes are ``Any`` to
    mypy.  This Protocol captures only the surface we touch (``buffer`` and
    ``intersects``) so ``_Obstacle.shape`` type-checks without a hard
    dependency on shapely's (untyped) classes.
    """

    def buffer(self, distance: float, *args: Any, **kwargs: Any) -> Any: ...

    def intersects(self, other: Any) -> bool: ...


@dataclass(frozen=True)
class _Obstacle:
    """A foreign-net copper obstacle to subtract from a fill polygon."""

    net_key: str
    layers: tuple[str, ...]
    # Sheet-absolute footprint of the copper, as a shapely geometry.
    shape: _Geometry


def _build_net_name_map(doc: SExp) -> dict[int, str]:
    """Map net numbers to net names from the board's ``(net N "name")`` table.

    ``find_all`` is recursive, so it also returns the *name-less* per-element
    nodes (``(net 9)`` on a via, ``(net 27)`` on a track).  Those carry no
    name and must NOT clobber the real ``(net 9 "GATE_AL")`` declaration from
    the top-level net table — so only entries that actually supply a non-empty
    name are recorded.
    """
    mapping: dict[int, str] = {}
    for net in doc.find_all("net"):
        num = net.get_int(0)
        if num is None:
            continue
        name = net.get_string(1)
        if name:
            mapping[num] = name
    return mapping


def _net_key(
    net_node: SExp | None,
    name_map: dict[int, str],
) -> str | None:
    """Return a canonical net identity (``name`` when known, else ``#N``).

    Handles both the ``(net N "name")`` element form and the name-only
    ``(net "name")`` zone form KiCad-tools emits, so a zone declared by
    name and a pad declared by number resolve to the same key whenever the
    number is present in the board's net table.
    """
    if net_node is None:
        return None
    num = net_node.get_int(0)
    if num is not None:
        if num == 0:
            return None  # unassigned copper — not clearance-checked
        name = net_node.get_string(1)
        if name:
            return name
        mapped = name_map.get(num)
        return mapped if mapped else f"#{num}"
    # Name-only form, e.g. (net "VCC").
    name = net_node.get_string(0)
    if not name:
        return None
    return name


def _build_box(shapely_mod, cx: float, cy: float, w: float, h: float):
    """Axis-aligned box centred at (cx, cy)."""
    return shapely_mod.box(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def _collect_obstacles(
    doc: SExp,
    shapely_mod,
    name_map: dict[int, str],
) -> list[_Obstacle]:
    """Gather every net-assigned pad and via as a sheet-absolute obstacle.

    Pads are modelled by their axis-aligned bounding box (matching the
    ``ViaZoneClearanceRule`` pad-box convention) so the subtracted antipad
    is at least as large as the box the DRC check measures against.  Vias
    are modelled by their circular barrel.  Net-0 (unassigned) copper is
    skipped — it does not participate in clearance checks.
    """
    from shapely.geometry import Point  # type: ignore[import-untyped]

    obstacles: list[_Obstacle] = []

    # --- Pads (inside footprints) ---
    for fp in doc.find_all("footprint"):
        fp_at = fp.find("at")
        if fp_at is None:
            continue
        fp_x = fp_at.get_float(0) or 0.0
        fp_y = fp_at.get_float(1) or 0.0
        fp_rot = fp_at.get_float(2) or 0.0
        rot_rad = math.radians(fp_rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.find_all("pad"):
            net_key = _net_key(pad.find("net"), name_map)
            if net_key is None:
                continue

            pad_at = pad.find("at")
            if pad_at is None:
                continue
            local_x = pad_at.get_float(0) or 0.0
            local_y = pad_at.get_float(1) or 0.0
            pad_rot = pad_at.get_float(2) or 0.0

            size_node = pad.find("size")
            if size_node is None:
                continue
            w = size_node.get_float(0) or 0.0
            h = size_node.get_float(1)
            if h is None:
                h = w
            if w <= 0 or h <= 0:
                continue

            # Footprint-local -> sheet-absolute (CCW-positive convention,
            # matching PCB.get_pad_position used throughout the codebase).
            abs_x = fp_x + (local_x * cos_r - local_y * sin_r)
            abs_y = fp_y + (local_x * sin_r + local_y * cos_r)

            # Total pad rotation in board frame.
            total_rot = (fp_rot + pad_rot) % 360.0
            box_w, box_h = _axis_aligned_box_dims(w, h, total_rot)

            shape = _build_box(shapely_mod, abs_x, abs_y, box_w, box_h)

            layers_node = pad.find("layers")
            pad_layers = (
                [
                    layers_node.get_string(i) or ""
                    for i in range(len(layers_node.values))
                    if isinstance(layers_node.values[i], str)
                ]
                if layers_node is not None
                else []
            )

            obstacles.append(_Obstacle(net_key=net_key, layers=tuple(pad_layers), shape=shape))

    # --- Vias (top level) ---
    for via in doc.find_all("via"):
        net_key = _net_key(via.find("net"), name_map)
        if net_key is None:
            continue
        at = via.find("at")
        size_node = via.find("size")
        if at is None or size_node is None:
            continue
        cx = at.get_float(0) or 0.0
        cy = at.get_float(1) or 0.0
        diameter = size_node.get_float(0) or 0.0
        if diameter <= 0:
            continue
        layers_node = via.find("layers")
        via_layers = (
            [
                layers_node.get_string(i) or ""
                for i in range(len(layers_node.values))
                if isinstance(layers_node.values[i], str)
            ]
            if layers_node is not None
            else ["F.Cu", "B.Cu"]
        )
        shape = Point(cx, cy).buffer(diameter / 2.0)
        obstacles.append(_Obstacle(net_key=net_key, layers=tuple(via_layers), shape=shape))

    return obstacles


def _axis_aligned_box_dims(w: float, h: float, rotation_deg: float) -> tuple[float, float]:
    """Axis-aligned bounding-box dimensions of a rotated rectangle.

    Mirrors ``_transform_pad_dimensions`` in the clearance DRC rule so the
    antipad we carve is never smaller than the box the check measures.
    """
    rot = rotation_deg % 360.0
    if abs(rot - 90) < 1e-3 or abs(rot - 270) < 1e-3:
        return h, w
    if abs(rot) < 1e-3 or abs(rot - 180) < 1e-3:
        return w, h
    rad = math.radians(rot)
    cos_a = abs(math.cos(rad))
    sin_a = abs(math.sin(rad))
    return (w * cos_a + h * sin_a, w * sin_a + h * cos_a)


def _obstacle_on_layer(obs: _Obstacle, layer: str) -> bool:
    """Whether an obstacle's copper exists on ``layer``."""
    # Vias use the via-span semantics; pads use the pad-layer wildcard.
    if "*.Cu" in obs.layers:
        return True
    if layer in obs.layers:
        return True
    # Through/blind vias span intermediate copper layers.
    return via_spans_layer(list(obs.layers), layer)


def _ring_to_xy_node(ring: list[tuple[float, float]]) -> SExp:
    """Build a ``(pts (xy ...) ...)`` node from a coordinate ring.

    Uses full coordinate precision (not the 2-decimal :func:`builders.xy`)
    so the corrected fill geometry round-trips without quantization error.
    """
    pts = SExp.list("pts")
    for x, y in ring:
        pts.append(SExp.list("xy", round(x, 6), round(y, 6)))
    return pts


def _strip_close(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop the trailing closing vertex from a coordinate ring."""
    out = [(float(x), float(y)) for x, y in coords]
    if len(out) > 1 and out[0] == out[-1]:
        out = out[:-1]
    return out


# Width of the zero-area "slit" used to vent a hole out to the polygon
# exterior so the result is simply-connected (KiCad ``filled_polygon``
# rings cannot carry holes).  1e-4 mm is far below fab resolution and the
# DRC's tolerance, so it changes neither copper nor clearance, but it is
# wide enough to survive coordinate rounding to 6 decimals.
_SLIT_WIDTH_MM = 1e-4

# Area tolerance (mm^2) for the rewrite-safety gates.  Slits and 6-decimal
# coordinate rounding perturb areas by ~1e-6 mm^2; anything below this is
# numerical noise, not real copper.
_AREA_EPS = 1e-4


def _vent_holes(poly):
    """Return a list of hole-free shapely Polygons equivalent to ``poly``.

    A KiCad ``filled_polygon`` ring cannot represent interior holes
    directly, and hand-stitched seams are fragile (a seam between distant
    vertices can cross copper and re-add a spurious lobe).  Instead, every
    hole is vented out to the polygon exterior with a hair-thin slit so the
    result is simply-connected.  All slits are built up front (one per
    interior) and subtracted in a single pass to avoid the re-scan
    instability of an iterative approach.  Each slit's endpoints are nudged
    slightly past the hole and exterior boundaries so the cut reliably
    separates.

    Returns the list of resulting hole-free Polygons.
    """
    from shapely.geometry import LineString
    from shapely.ops import nearest_points  # type: ignore[import-untyped]

    polys = _iter_polygons(poly)
    if not any(p.interiors for p in polys):
        return [p for p in polys if not p.is_empty and p.area > 0]

    slits = []
    for part in polys:
        for interior in part.interiors:
            p_hole, p_ext = nearest_points(LineString(interior.coords), part.exterior)
            dx = p_ext.x - p_hole.x
            dy = p_ext.y - p_hole.y
            length = (dx * dx + dy * dy) ** 0.5
            if length < 1e-12:
                # Hole boundary touches the exterior already; a tiny stub
                # still vents it.
                ux, uy = 0.0, 1.0
            else:
                ux, uy = dx / length, dy / length
            pad = _SLIT_WIDTH_MM * 2.0
            seg = LineString(
                [
                    (p_hole.x - ux * pad, p_hole.y - uy * pad),
                    (p_ext.x + ux * pad, p_ext.y + uy * pad),
                ]
            )
            slits.append(seg.buffer(_SLIT_WIDTH_MM, cap_style=2))

    if not slits:
        return [p for p in polys if not p.is_empty and p.area > 0]

    import shapely  # type: ignore[import-untyped]

    vented = poly.difference(shapely.unary_union(slits))
    return [p for p in _iter_polygons(vented) if not p.is_empty and p.area > 0]


def _iter_polygons(geom):
    """Yield every Polygon component of an arbitrary shapely geometry."""
    gt = geom.geom_type
    if gt == "Polygon":
        return [geom]
    if gt in ("MultiPolygon", "GeometryCollection"):
        out = []
        for g in geom.geoms:
            out.extend(_iter_polygons(g))
        return out
    return []


def _result_polygons(result) -> list:
    """Return the list of non-empty Polygon parts from a difference result."""
    return [p for p in _iter_polygons(result) if not p.is_empty and p.area > 0]


def _collect_same_net_anchors(
    doc: SExp,
    shapely_mod,
    name_map: dict[int, str],
    zone_net: str,
    fill_layer: str,
) -> list:
    """Collect copper anchors that tie a zone net to the rest of its net.

    Returns the sheet-absolute shapely shapes of every pad, via, and track
    *belonging to ``zone_net``* that sits on ``fill_layer``.  A
    ``filled_polygon`` part is only electrically part of the pour when it
    overlaps at least one of these anchors — KiCad's island-removal
    (``island_removal_mode 0``) discards any fill region that does not.

    The antipad subtraction in :func:`apply_foreign_pad_clearance` can split
    a pour into several parts; carrying the anchor list lets us drop the
    disconnected fragments (the ``isolated_copper`` islands seen on board-02
    after the #3712 carve) instead of emitting them as copper.
    """
    from shapely.geometry import LineString

    anchors: list = []

    # Same-net pads (boxes) and vias (barrels) — reuse the obstacle geometry.
    for obs in _collect_obstacles(doc, shapely_mod, name_map):
        if obs.net_key != zone_net:
            continue
        if not _obstacle_on_layer(obs, fill_layer):
            continue
        anchors.append(obs.shape)

    # Same-net track segments on this copper layer.
    for seg in doc.find_all("segment"):
        if _net_key(seg.find("net"), name_map) != zone_net:
            continue
        layer_node = seg.find("layer")
        if layer_node is None or (layer_node.get_string(0) or "") != fill_layer:
            continue
        start = seg.find("start")
        end = seg.find("end")
        if start is None or end is None:
            continue
        sx = start.get_float(0) or 0.0
        sy = start.get_float(1) or 0.0
        ex = end.get_float(0) or 0.0
        ey = end.get_float(1) or 0.0
        width_node = seg.find("width")
        width = (width_node.get_float(0) if width_node is not None else None) or 0.0
        line = LineString([(sx, sy), (ex, ey)])
        anchors.append(line.buffer(max(width, 0.0) / 2.0) if width > 0 else line)

    return anchors


def _keep_connected_rings(rings: list, anchors: list, polygon_cls) -> list:
    """Drop fill rings not electrically connected to the zone net.

    A ring is kept when its polygon intersects at least one same-net anchor
    (pad, via, or track).  When no anchors are known (e.g. a pour with no
    copper connection on this layer) every ring is kept — removing copper
    with no evidence it is stranded would be more dangerous than leaving it.

    This mirrors KiCad's ``island_removal_mode 0`` (remove all isolated
    islands): the antipad subtraction plus hole-venting can shed stranded
    sliver fragments; only the rings tied to the net's copper are real.
    """
    if not anchors:
        return rings
    kept = []
    for ring in rings:
        poly = polygon_cls(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        if any(poly.intersects(a) for a in anchors):
            kept.append(ring)
    # Never delete the whole fill: if the connectivity test rejects every
    # ring (anchor geometry just misses every fill edge), fall back to the
    # largest ring by area so the correction can only ever improve a board.
    if kept:
        return kept
    return [max(rings, key=lambda r: abs(polygon_cls(r).buffer(0).area))]


# Pad-connection mode keywords KiCad accepts in ``(connect_pads MODE ...)``.
# When the mode token is *absent* the zone uses thermal relief for ALL pads
# (the legacy default), which starves small SMD pads of the 2 spokes the
# geometric DRC requires (issue #3727).
_CONNECT_PAD_MODES = frozenset({"thru_hole_only", "yes", "no"})


def normalize_zone_pad_connection(
    doc: SExp,
    mode: str = "yes",
) -> int:
    """Upgrade legacy thermal-for-all zones to a stronger pad connection.

    KiCad's ``(connect_pads ...)`` carries an optional leading *mode* token:

    * **absent** -- thermal relief for every pad.  Small SMD pads cannot
      host the 2 thermal spokes KiCad's geometric DRC requires, and some
      through-hole power pads sit where only one spoke can form, so a pour
      that uses this default reports ``starved_thermal`` errors (issue
      #3727, across boards 03/04/05/softstart).
    * ``yes`` -- a **solid** full-copper connection for every pad (the
      default applied here).  A solid connection is strictly stronger than
      a 2-spoke thermal relief, so it eliminates ``starved_thermal``
      honestly -- it does not lower the required spoke count -- and gives
      the lowest-impedance power/ground delivery on a reflow-assembled
      board.
    * ``thru_hole_only`` -- thermal relief for THT pads, solid for SMD.
    * ``no`` -- no copper connection.

    This function adds the ``mode`` token to every copper zone whose
    ``connect_pads`` node currently lacks one.  Zones that already declare a
    mode (set deliberately by an upstream generator) are left untouched, as
    are keepout zones (which have no ``connect_pads``).  The document is
    mutated in place; the number of zones changed is returned so callers can
    skip a needless rewrite when nothing changed.

    Run this *before* the kicad-cli fill so the refilled copper reflects the
    new connection mode.
    """
    if mode not in _CONNECT_PAD_MODES:
        raise ValueError(
            f"Unsupported pad-connection mode {mode!r}; "
            f"expected one of {sorted(_CONNECT_PAD_MODES)}"
        )

    changed = 0
    for zone in doc.find_all("zone"):
        connect_pads = zone.find("connect_pads")
        if connect_pads is None:
            continue
        # An existing mode token is a bare atom child (e.g. "thru_hole_only");
        # the clearance sub-list is a named child.  Skip zones that already
        # carry any leading mode token so deliberate settings are preserved.
        has_mode = any(
            child.is_atom and isinstance(child.value, str) for child in connect_pads.children
        )
        if has_mode:
            continue
        # Insert the mode token as the first child, before (clearance ...).
        connect_pads.children.insert(0, SExp(value=mode))
        changed += 1
    return changed


def apply_foreign_pad_clearance(
    doc: SExp,
    default_clearance: float = 0.3,
) -> int:
    """Subtract foreign-net antipads from every zone ``filled_polygon``.

    For each zone, every ``filled_polygon`` on a copper layer is shrunk so
    that no pad/via belonging to a *different* net intrudes within the
    zone's clearance.  Each obstacle is buffered by
    ``clearance + min_thickness / 2`` before subtraction: clearance keeps
    the foreign copper away, and the extra half-thickness accounts for the
    fact that KiCad fill copper has finite width (the polygon edge is the
    centre-line minus half the minimum thickness in the worst case).

    The document is mutated in place.  Returns the number of
    ``filled_polygon`` nodes that were modified.

    When ``shapely`` is not importable the function is a no-op and returns
    ``0`` (the fill is left as kicad-cli produced it).
    """
    try:
        import shapely
        from shapely import make_valid
        from shapely.geometry import Polygon
    except ImportError:
        return 0

    name_map = _build_net_name_map(doc)
    obstacles = _collect_obstacles(doc, shapely, name_map)
    if not obstacles:
        return 0

    modified = 0

    for zone in doc.find_all("zone"):
        zone_net = _net_key(zone.find("net"), name_map)
        # Keepout / unassigned zones carry net 0 and no fill copper to
        # protect; skip them.
        if zone_net is None:
            continue

        # Zone clearance / thickness for the antipad buffer.
        clearance = default_clearance
        connect_pads = zone.find("connect_pads")
        if connect_pads is not None:
            cl = connect_pads.find("clearance")
            if cl is not None:
                cl_val = cl.get_float(0)
                if cl_val is not None:
                    clearance = cl_val
        min_thickness = 0.25
        mt = zone.find("min_thickness")
        if mt is not None:
            mt_val = mt.get_float(0)
            if mt_val is not None:
                min_thickness = mt_val

        buffer_dist = clearance + min_thickness / 2.0

        for filled in zone.find_all("filled_polygon"):
            layer_node = filled.find("layer")
            fill_layer = ""
            if layer_node is not None:
                fill_layer = layer_node.get_string(0) or ""
            else:
                zone_layer = zone.find("layer")
                if zone_layer is not None:
                    fill_layer = zone_layer.get_string(0) or ""

            pts_node = filled.find("pts")
            if pts_node is None:
                continue
            ring = [
                (xy.get_float(0) or 0.0, xy.get_float(1) or 0.0) for xy in pts_node.find_all("xy")
            ]
            if len(ring) < 3:
                continue

            fill_poly = Polygon(ring)
            if not fill_poly.is_valid:
                # KiCad encodes thermal/pad cut-outs as a self-touching
                # single ring; make_valid reconstructs the holed polygon
                # without dropping copper lobes (buffer(0) can).  This
                # matches the DRC's _repair_fill_polygon so our subtraction
                # operates on the same geometry the check measures.
                fill_poly = make_valid(fill_poly)
            if fill_poly.is_empty:
                continue

            # Union the foreign-net antipads that actually touch this fill.
            # Buffering each obstacle by buffer_dist guarantees the carved
            # gap is at least the zone clearance.  Skip obstacles whose
            # buffered footprint does not reach the fill so untouched fills
            # are left byte-for-byte unchanged (no spurious geometry churn).
            cutters = []
            for obs in obstacles:
                if obs.net_key == zone_net:
                    continue
                if not _obstacle_on_layer(obs, fill_layer):
                    continue
                buffered = obs.shape.buffer(buffer_dist)
                if buffered.intersects(fill_poly):
                    cutters.append(buffered)
            if not cutters:
                continue

            cut_union = shapely.unary_union(cutters)
            # Only rewrite a fill whose copper actually intrudes within
            # clearance of a foreign obstacle.  ``buffer_dist`` already
            # encodes the clearance, so a positive-area intersection of the
            # raw fill with the buffered cutters is exactly "this fill has a
            # real violation to fix".  Fills that merely sit *near* an
            # obstacle (the intersects() pre-filter above) but keep adequate
            # clearance are left byte-for-byte unchanged.
            if fill_poly.intersection(cut_union).area <= _AREA_EPS:
                continue

            result = fill_poly.difference(cut_union)
            parts = _result_polygons(result)
            if not parts:
                # Subtracting everything would leave no copper; leave the
                # original fill untouched rather than delete it (the zone
                # was intentionally placed and an empty fill is worse than
                # a tight one — this should not happen for real boards).
                continue

            # The difference keeps the original thermal/pad holes AND adds
            # the new foreign-net antipads as holes.  KiCad fill rings can't
            # carry holes, so vent every hole out to the exterior with a
            # hair-thin slit, producing simply-connected polygons; emit one
            # filled_polygon per resulting region, all on the original layer.
            rings: list[list[tuple[float, float]]] = []
            for part in parts:
                for vented in _vent_holes(part):
                    rings.append(_strip_close(list(vented.exterior.coords)))
            rings = [r for r in rings if len(r) >= 3]
            if not rings:
                continue

            # Island removal (matches KiCad ``island_removal_mode 0``).
            # Subtracting the foreign antipads — and venting the resulting
            # holes out to the exterior — can shed thin sliver lobes that are
            # no longer electrically tied to the pour.  Emitting them produces
            # ``isolated_copper`` warnings (the board-06 split-fill regression
            # class).  Keep only rings that overlap a same-net pad/via/track so
            # the rewritten pour stays a single connected copper component.
            if len(rings) > 1:
                anchors = _collect_same_net_anchors(doc, shapely, name_map, zone_net, fill_layer)
                rings = _keep_connected_rings(rings, anchors, Polygon)

            # Safety gate: reconstruct exactly what the DRC will read from
            # the rewritten rings (via the same _repair_fill_polygon path)
            # and accept the rewrite ONLY when it (a) removes the foreign
            # overlap and (b) adds no copper the original fill did not have
            # (no spurious lobe from a degenerate vent).  If the re-encode
            # is not faithful, leave the original fill untouched so the
            # correction can only ever improve a board, never regress it.
            recon = _reconstruct_fill(rings, make_valid)
            if recon is None or recon.is_empty:
                continue
            if recon.intersection(cut_union).area > _AREA_EPS:
                continue  # still overlaps a foreign antipad -> reject
            if recon.difference(fill_poly).area > _AREA_EPS:
                continue  # gained copper outside the original -> reject

            _replace_pts(filled, rings[0])
            modified += 1

            for extra_ring in rings[1:]:
                clone = SExp.list("filled_polygon")
                if layer_node is not None:
                    clone.append(SExp.list("layer", fill_layer))
                clone.append(_ring_to_xy_node(extra_ring))
                zone.append(clone)
                modified += 1

    return modified


def _reconstruct_fill(rings, make_valid_fn):
    """Reconstruct the geometry the DRC will read from rewritten rings.

    Mirrors ``_collect_zone_fills`` + ``_repair_fill_polygon``: each ring is
    parsed with ``Polygon`` and repaired with ``make_valid`` when invalid,
    then all parts are unioned.  Returns ``None`` if nothing usable results.
    """
    from shapely import unary_union
    from shapely.geometry import Polygon

    polys: list[Any] = []
    for ring in rings:
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = make_valid_fn(poly)
        polys.extend(p for p in _iter_polygons(poly) if not p.is_empty)
    if not polys:
        return None
    return unary_union(polys)


def _replace_pts(filled: SExp, ring: list[tuple[float, float]]) -> None:
    """Replace the ``(pts ...)`` child of a ``filled_polygon`` node."""
    new_pts = _ring_to_xy_node(ring)
    for i, child in enumerate(filled.children):
        if child.name == "pts":
            filled.children[i] = new_pts
            return
    filled.append(new_pts)
