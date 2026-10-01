"""Collision checking for trace optimization.

Epic #5509 Phase 4a (#5854): consumer group 15 -- the post-route trace
optimizer's two ``CollisionChecker`` implementations -- asks the shared
exact-geometry clearance kernel
(:mod:`kicad_tools.router.clearance_kernel`, through the one shared
primitive-to-shape translation in
:mod:`kicad_tools.router.clearance_shapes`) for every clearance verdict it
reaches.  Before this phase the module carried its own
``distance - half_a - half_b >= required`` arithmetic for routed copper and
judged *pad* copper straight off the raster, so a shortcut the search and the
commit gates would both accept could still be refused -- or, in the raster's
own conservative direction, a legal shortened path was rejected because the
candidate's clearance envelope touched a pad's clearance envelope, roughly
twice the real requirement.

What did **not** change (deliberately, per the epic's scope guards):

* **No rule value moves.**  Every requirement handed to the kernel is the one
  the grid already resolved for that pair -- ``rules.trace_clearance`` for
  routed copper, ``max(trace_clearance, via_clearance)`` for a via, and for a
  pad the per-component ``rules.get_clearance_for_component(pad.ref,
  pin_pitch)`` its own raster halo was built from (PR #5874 review; the same
  resolution ``RoutingGrid.worst_segment_pad_deficit`` and
  ``DiffPairRouter._kernel_pad_deficit`` use).  *Which* value a pair resolves
  to is Phase 2's axis, not this phase's.
* **The raster stays the broad phase.**  Bresenham-plus-buffer still selects
  the cells worth looking at; it no longer gets to pronounce a verdict on a
  cell whose occupancy can be re-measured from registered geometry
  (``grid.routes`` for route copper since #5625, ``grid.pads`` for pad copper
  since this phase).  A cell blocked by something that registers no geometry
  at all -- a keepout, an obstacle, a region bound, the board-edge band, all
  reported by :meth:`RoutingGrid.raster_only_blocked_cell` -- keeps its
  conservative reject, because there is nothing to re-measure it from.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Protocol

from ..clearance_shapes import KSegment, pad_shape, segment_shape, shapes_clear, via_shape
from ..layers import Layer
from ..primitives import Segment, pad_half_extents

if TYPE_CHECKING:
    from ..grid import RoutingGrid
    from ..primitives import Pad, Via


def _iter_dilated_line_cells(
    gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
) -> Iterator[tuple[int, int]]:
    """Yield grid cells within ``clearance`` cells (Chebyshev/square) of the
    Bresenham rasterization of the line from ``(gx1, gy1)`` to ``(gx2, gy2)``.

    Issue #5240: this is an exact, much cheaper replacement for the
    "recompute the full ``(2*clearance+1)^2`` window at every rasterized
    point, union the results" pattern that both ``GridCollisionChecker``
    and ``VectorCollisionChecker`` used (independently) for their
    trace-vs-obstacle clearance sweeps.  Profiling a board-06 re-route
    (``kct``-native backend, rtree installed) showed
    ``GridCollisionChecker._get_path_cells`` as the single hottest leaf
    frame in the post-route trace-optimization pass -- board 06's 0.05 mm
    grid resolution and ~0.325 mm trace half-width + clearance envelope
    give ``clearance_cells`` ~= 7, so the naive per-step window is a
    (2*7+1)^2 = 225-cell Python-level ``set`` insertion burst for EVERY
    single rasterized point along every optimized segment.

    Because consecutive Bresenham points move by at most one cell in x
    and/or y, step *i*'s window and step *i+1*'s window overlap in all but
    a thin strip: for a step whose center moves by ``(dx, dy)`` with
    ``dx, dy in {-1, 0, 1}`` (not both zero), the set difference
    ``new_window \\ old_window`` is *exactly*:

    * the full-height column at the new leading x edge (``new_cx + dx *
      clearance``), when ``dx != 0``; and/or
    * the full-width row at the new leading y edge (``new_cy + dy *
      clearance``), when ``dy != 0``.

    (Proof: a point ``(x, y)`` in the new window is also in the old
    window iff ``x`` and ``y`` are both within the old window's bounds;
    substituting the old window's bounds in terms of the new center and
    step shows this holds iff ``x != new_cx + dx * clearance`` AND
    ``y != new_cy + dy * clearance`` -- i.e. the complement, the new
    window minus the old one, is exactly the union of that column and
    that row.)  This drops the per-step cost from O(clearance^2) to
    O(clearance), independent of how the caller consumes the cells.

    The first point has no predecessor, so its full window is emitted
    unconditionally.  A diagonal step's column and row share one corner
    cell, which this generator yields twice -- harmless for a ``set``-
    deduplicating consumer, and a single redundant recheck (not a
    redundant O(clearance^2) rescan) for an early-exit consumer.  No
    bounds clipping against the grid is performed here (callers already
    clip, matching the pre-existing contract of both call sites).
    """
    dx = abs(gx2 - gx1)
    dy = abs(gy2 - gy1)
    sx = 1 if gx1 < gx2 else -1
    sy = 1 if gy1 < gy2 else -1
    err = dx - dy

    gx, gy = gx1, gy1
    c = clearance

    # First point: emit the full (2c+1) x (2c+1) window -- there is no
    # predecessor window to diff against.
    for cy in range(-c, c + 1):
        for cx in range(-c, c + 1):
            yield (gx + cx, gy + cy)

    while not (gx == gx2 and gy == gy2):
        e2 = 2 * err
        step_x = 0
        step_y = 0
        if e2 > -dy:
            err -= dy
            gx += sx
            step_x = sx
        if e2 < dx:
            err += dx
            gy += sy
            step_y = sy

        # Emitted using the ALREADY-UPDATED (gx, gy) -- see the proof
        # above: the leading edge is expressed in terms of the new
        # center.
        if step_x:
            lead_x = gx + step_x * c
            for cy in range(-c, c + 1):
                yield (lead_x, gy + cy)
        if step_y:
            lead_y = gy + step_y * c
            for cx in range(-c, c + 1):
                yield (gx + cx, lead_y)


def _candidate_shape(x1: float, y1: float, x2: float, y2: float, width: float) -> KSegment:
    """The path the optimizer is proposing, as kernel copper.

    Built once per ``path_is_clear`` call and reused for every surviving
    candidate, the way Phase 3b's ``RouteHaloGeometry.clear`` builds its own
    (issue #5661).

    The shape carries no layer on purpose.  Every caller in this module has
    *already* established that the counterpart shares the candidate's layer --
    the R-tree is indexed per layer, ``_routed_copper_clear`` compares
    ``seg.layer.value``, ``_pad_copper_clear`` compares the pad's layer index,
    and vias span every layer -- so leaving the candidate on
    ``ALL_LAYERS`` lets the kernel's layer gate pass unconditionally rather
    than re-deciding a filter the caller owns.
    """
    return KSegment(x1=x1, y1=y1, x2=x2, y2=y2, width=width)


def _path_clear_of_segment(candidate: KSegment, other: Segment, min_clearance: float) -> bool:
    """Exact edge-to-edge clearance between a candidate path and one segment.

    Issue #5625: the single narrow phase both checkers in this module use, so
    ``VectorCollisionChecker``'s "drop-in, ~10x faster replacement" claim is
    true by construction -- the two classes differ only in how they find
    candidates (R-tree query vs. raster walk), never in the arithmetic that
    decides them.

    Epic #5509 Phase 4a (#5854): that arithmetic is now the shared clearance
    kernel's.  The pre-#5854 body subtracted both half widths from a private
    ``segment_to_segment_distance`` and compared with a bare ``>=``; the
    kernel subtracts the same two half widths and compares under its own
    ``CLEARANCE_EPSILON_MM``, so search-time, commit-time and this post-route
    pass can no longer disagree about the same pair by a rounding epsilon.
    """
    return shapes_clear(candidate, segment_shape(other), min_clearance)


def _path_clear_of_via(candidate: KSegment, via: Via, via_clearance: float) -> bool:
    """Exact edge-to-edge clearance between a candidate path and one via.

    The via half of :func:`_path_clear_of_segment`'s contract, on the same
    kernel.  ``via_clearance`` is whatever the caller resolved -- this phase
    moves the *geometry* onto the kernel and leaves rule selection alone.
    """
    return shapes_clear(candidate, via_shape(via), via_clearance)


def _path_clear_of_pad(candidate: KSegment, pad: Pad, min_clearance: float) -> bool:
    """Exact edge-to-edge clearance between a candidate path and one pad.

    New in Epic #5509 Phase 4a (#5854).  There was no pad narrow phase before:
    both checkers decided pad copper off the raster, where a pad's blocked
    footprint is its metal grown by the pad's own clearance halo and the
    candidate is grown again by ``width / 2 + trace_clearance`` -- about twice
    the requirement, quantised outwards.  :func:`~..clearance_shapes.pad_shape`
    is the same exact pad model (the Minkowski core port of
    ``validate/rules/clearance.py``'s ``_pad_polygon``) the migrated diff-pair
    and mesh consumers measure a foreign pad with, so a roundrect's corner gap
    is measured rather than approximated by a bounding box.
    """
    return shapes_clear(candidate, pad_shape(pad), min_clearance)


def _via_spans_layer(grid: RoutingGrid, via: Any, layer_idx: int) -> bool:
    """Return True if ``via`` blocks copper on ``layer_idx``.

    Through-hole vias (the common case in kicad-tools today) declare
    ``layers=(F.Cu, B.Cu)`` and physically block every layer in between as
    well.  Blind / buried vias declare a sub-range.  This helper maps the
    start / end layer enum values to grid layer indices and returns ``True``
    iff ``layer_idx`` falls in the inclusive range.

    When the layer mapping cannot be resolved (unexpected Layer enum value,
    etc.) the helper returns ``True`` to preserve the conservative "assume
    blocking" behaviour of ``grid.validate_segment_clearance``, which iterates
    every via without layer filtering.

    Issue #5625: lifted out of ``VectorCollisionChecker._via_on_layer`` (which
    now delegates here) so the grid checker's own exact pass applies the same
    layer-span rule rather than a second transcription of it.
    """
    try:
        start_idx = grid.layer_to_index(via.layers[0].value)
        end_idx = grid.layer_to_index(via.layers[1].value)
    except Exception:
        return True
    lo, hi = (start_idx, end_idx) if start_idx <= end_idx else (end_idx, start_idx)
    return lo <= layer_idx <= hi


def _soft_cell_is_accountable_route_copper(
    grid: RoutingGrid, gx: int, gy: int, layer_idx: int
) -> bool:
    """May this softly-blocked cell be re-decided from exact copper?

    Issue #5625.  ``RoutingGrid._mark_segment`` / ``_mark_via`` dilate every
    committed object by ``width / 2 + trace_clearance`` (plus the #1666 safety
    cell) before painting it, so a raster cell being occupied by a foreign net
    does **not** mean the candidate path is within clearance of that net's
    copper -- it means the path's own clearance envelope reached that net's
    clearance envelope, which is roughly twice the real requirement.  The
    raster is therefore a broad phase, and the verdict belongs to the exact
    measurement the vector checker already performs.

    A raster cell may only be re-decided when its occupancy is fully
    accounted for by copper that is *registered* and can therefore be
    re-measured.  That is exactly the authorisation
    :meth:`RouteHaloGeometry.cell_known` was built for in Epic #5509 Phase 3c
    (issue #5617): it refuses a cell that is a hard obstacle, pad metal,
    statically blocked, reserved for another net, or whose conservative mark
    does not match a route halo currently registered in ``grid.routes``.
    :meth:`RoutingGrid.raster_only_blocked_cell` (#5662) is required on top of
    it: obstacles, keepouts, region bounds and the board-edge band register no
    geometry anywhere, so the raster mark is their only record and a cell they
    touched must keep its conservative verdict however clear the measurable
    copper is.

    Both predicates are compared with ``is True`` rather than truth-tested on
    purpose: a grid that cannot answer them at all -- a ``MagicMock`` test
    double, an older grid object without the planes -- must fall through to
    the conservative branch instead of silently authorising a refinement on a
    truthy auto-generated attribute.
    """
    halo = getattr(grid, "_route_halo", None)
    if halo is None:
        return False
    raster_only = getattr(grid, "raster_only_blocked_cell", None)
    if raster_only is not None and raster_only(gx, gy, layer_idx) is True:
        return False
    return halo.cell_known(gx, gy, layer_idx) is True


def _obstacle_cell_is_accountable_pad_copper(
    grid: RoutingGrid, gx: int, gy: int, layer_idx: int
) -> bool:
    """May this hard-blocked cell be re-decided from exact pad copper?

    Epic #5509 Phase 4a (#5854), and the pad-side twin of
    :func:`_soft_cell_is_accountable_route_copper`.  ``_add_pad_unsafe``
    paints a pad's metal *and* its clearance halo into the ``is_obstacle`` /
    ``pad_blocked`` planes, so a candidate path touching such a cell does not
    mean the path is within clearance of pad metal -- it means the path's own
    clearance envelope reached the pad's, roughly twice the real requirement.
    The raster is a broad phase there too, and the verdict belongs to
    :func:`_pad_copper_clear`'s exact measurement.

    A cell may only be re-decided when its blockage is fully accounted for by
    copper that is *registered* and can therefore be re-measured.  Three
    conditions, together:

    1. :meth:`RoutingGrid.raster_only_blocked_cell` (#5662) says ``False`` --
       no obstacle, keepout, region bound or board-edge band touched this
       cell.  None of those register geometry anywhere, so for them the raster
       mark is the only record and the conservative verdict has to stand.
    2. ``grid.pads`` is a **non-empty** tuple.  An empty registry accounts for
       nothing, so a hard-blocked cell on a grid that has registered no pads
       at all -- hand-painted fixture state, a grid whose arrays were released
       -- keeps its pre-#5854 reject rather than being laundered into "clear"
       by a walk over zero pads.
    3. :meth:`RoutingGrid.pad_marked_cell` (#5662) says a registered pad's own
       marking pass wrote this cell -- the *exclusive* attribution step, and
       the condition PR #5874's review added.  Condition 1 alone leaves more
       than ``_add_pad_unsafe`` behind: ``_apply_stitch_via_halo``
       (``grid.py``, issue #2842) marks ``_is_obstacle`` out to
       ``rules.stitch_via_halo_radius()`` (~0.425 mm) around every
       ``pad.net == 0`` plane pad, and ``_apply_narrow_channel_halo`` (#2878)
       re-blocks a same-component channel the relaxation pass had opened.
       Neither of those is pad *copper*: the stitch
       halo is space reserved for the via ``kct stitch`` drops later, which
       ``_pad_copper_clear`` cannot re-measure from pad metal, so refining it
       away would launder a reservation into "clear" and surface as a stitch
       failure rather than as an optimizer test.  ``pad_marked_cell``
       reproduces only ``_add_pad_unsafe``'s own halo rectangle, so a cell
       marked solely by one of those later passes fails attribution and keeps
       its conservative reject.

    This is the authorisation ``DiffPairRouter._pad_attributable_cell`` already
    uses for the identical question on the diff-pair span path (Phase 3c,
    hardened by the PR #5676 review) -- ``pad_marked_cell`` is "necessary but
    not sufficient" on its own, and ``raster_only_blocked_cell`` is what makes
    it sufficient.  That precedent additionally refuses corridor reservations
    held for another net; they are deliberately *not* checked here, because
    ``reserve_corridor_cells`` writes only ``_reserved_for_nets`` and never the
    ``is_obstacle`` / ``pad_blocked`` planes these branches read, so a
    reservation can never be the reason a cell reached this predicate (and this
    module has never consulted reservations, before or after #5854).

    Written to fail closed.  Both grid predicates are compared with ``is``
    against the answer that authorises a refinement rather than truth-tested,
    and ``grid.pads`` must be a real tuple, so a grid that cannot answer -- a
    ``MagicMock`` test double, an older grid object without the plane or the
    method -- keeps its pre-#5854 reject instead of authorising a refinement
    against a geometry registry that is not there.
    """
    raster_only = getattr(grid, "raster_only_blocked_cell", None)
    if raster_only is None:
        return False
    if raster_only(gx, gy, layer_idx) is not False:
        return False
    pads = getattr(grid, "pads", None)
    if not (isinstance(pads, tuple) and pads):
        return False
    pad_marked = getattr(grid, "pad_marked_cell", None)
    if pad_marked is None:
        return False
    return pad_marked(gx, gy, layer_idx) is True


def _stitch_via_reservation(grid: RoutingGrid, pad: Pad) -> float:
    """The #2842 stitch-via space a plane-net pad keeps, as a copper gap.

    PR #5874 review (blocking finding 2).  ``kct stitch`` drops one via per
    plane-net pad (``pad.net == 0`` -- including the pour nets #2757 rewrites
    to 0) **on the pad centre** to bond the plane to the pin, so foreign copper
    has to stay ``rules.stitch_via_halo_radius()`` (``via_diameter / 2 +
    clearance``, ~0.425 mm with the stitcher's default via) away from that
    centre or the via has nowhere to land.  ``RoutingGrid._apply_stitch_via_halo``
    reserves exactly that, as ``ext = max(0, halo_from_center - half_extent)``
    beyond the pad's metal edge per axis -- much more than the trace-only halo
    a fine-pitch pad gets (~0.05 mm), which was the board-04 U2.8 / U2.23 /
    U2.35 stitch failure.

    Phase 4a's exact pad gate measures metal, not reservations, so without this
    floor the optimizer would re-measure a plane pad's gap against
    ``trace_clearance`` alone and hand back the space the raster was holding --
    a regression that surfaces in ``kct stitch``, not in this module's tests
    (measured on an isolated 0.3 mm plane pad at ``trace_clearance`` 0.2 mm:
    ``main`` refuses a candidate whose copper sits 0.40 mm from the pad centre,
    the unfloored kernel gate accepts it, and the via needs 0.425 mm).  Note
    that the raster's *own* record of this reservation is invisible to both
    checkers -- ``_apply_stitch_via_halo`` leaves an unclaimed halo cell
    ``blocked`` with ``net == 0`` and neither ``is_obstacle`` nor
    ``pad_blocked``, which none of the branches above read -- so what main
    protected the reservation with was the quantised dilation of the pad's
    *metal*, and re-measuring that metal exactly is what hands it back.  The
    requirement returned here is
    the *physical* one -- ``halo_from_center`` measured to foreign **copper**,
    i.e. ``ext`` from the metal edge -- where the raster quantises it to whole
    cells and applies it to the trace *centre*.

    ``max`` over the two axes (equivalently ``halo_from_center -
    min(half_w, half_h)``) because the kernel reports a direction-free gap: the
    raster reserves ``ext_y`` for a candidate approaching along x and ``ext_x``
    for one approaching along y, and taking the larger can only refuse more.
    An elongated pad whose long axis already exceeds the halo keeps the short
    axis's reservation, which is the axis #2842 is about.

    Returns ``0.0`` -- i.e. no floor, requirement unchanged -- whenever the
    grid's own gate for the halo is off (``rules.stitch_via_halo`` false, no
    ``stitch_via_halo_radius``, a non-plane pad), mirroring
    ``_add_pad_unsafe``'s call-site condition exactly.
    """
    if pad.net != 0:
        return 0.0
    rules = getattr(grid, "rules", None)
    if not getattr(rules, "stitch_via_halo", False):
        return 0.0
    radius = getattr(rules, "stitch_via_halo_radius", None)
    if radius is None:
        return 0.0
    try:
        halo_from_center = float(radius())
    except Exception:  # pragma: no cover - defensive (test doubles)
        return 0.0
    # ``_add_pad_unsafe``'s effective extents, including its drill-only /
    # bare through-hole fallbacks.
    half_w, half_h = pad_half_extents(pad)
    if pad.through_hole and not (pad.width > 0 and pad.height > 0):
        side = (pad.drill + 0.7) if pad.drill > 0 else 1.7
        half_w = half_h = side / 2.0
    return max(0.0, halo_from_center - min(half_w, half_h))


def _pad_required_clearance(grid: RoutingGrid, pad: Pad) -> float:
    """The clearance requirement for one foreign pad, resolved per component.

    PR #5874 review (Epic #5509 Phase 4a, #5854).  Both in-repo authorities on
    segment-vs-pad clearance resolve this per pad rather than from the flat
    ``rules.trace_clearance``:

    * :meth:`RoutingGrid.worst_segment_pad_deficit` -- the #3545 finalization
      backstop, and the validator's own pad quadrant; and
    * ``DiffPairRouter._kernel_pad_deficit`` -- Phase 3c's already-migrated
      kernel consumer, which states explicitly that it mirrors
      ``worst_segment_pad_deficit``'s *rule* logic and replaces only its
      geometry.

    Both call ``rules.get_clearance_for_component(pad.ref, pin_pitch)``, which
    applies a per-component ``component_clearances`` override and the
    fine-pitch relaxation (with #2867's narrow-channel guard).  Using it here
    is also what keeps this narrow phase consistent with the broad phase it
    refines: ``RoutingGrid._clearance_for_pin_pitch`` (``grid.py``) builds the
    pad's raster halo from exactly this call under
    ``rules.strict_pad_clearance`` -- so a flat ``trace_clearance`` would
    *accept* a path the raster was enforcing an override against, and the
    #3545 backstop cannot catch it because it runs before the optimizer
    (``route_cmd.py``).

    The pitch is the one the grid recorded when the pad was added
    (``_pad_pin_pitch``, keyed by ``id(pad)`` -- the same lookup
    :meth:`RoutingGrid.pad_marked_cell` and ``find_pad_ref_at`` use), so the
    requirement measured here is the requirement the pad's own halo was
    derived from.  A pad registered without a pitch resolves with
    ``pin_pitch=None``, i.e. the override or ``trace_clearance`` -- never a
    pitch guessed from a different source, which could only relax the
    requirement below what the raster enforced.

    A plane-net pad additionally keeps the #2842 stitch-via reservation as a
    floor (:func:`_stitch_via_reservation`) -- the raster was holding that space
    for the via ``kct stitch`` drops later, and pad metal is not something the
    kernel can re-measure it from.

    Fails **safe** back to ``rules.trace_clearance`` (this module's pre-#5854
    value) when the grid or rules object cannot answer -- a ``MagicMock`` test
    double, an older rules object without the resolver -- rather than letting a
    non-numeric answer reach the kernel's arithmetic.
    """
    fallback = grid.rules.trace_clearance
    resolver = getattr(getattr(grid, "rules", None), "get_clearance_for_component", None)
    if resolver is None:
        return fallback
    pitches = getattr(grid, "_pad_pin_pitch", None)
    pin_pitch = pitches.get(id(pad)) if isinstance(pitches, dict) else None
    try:
        required = resolver(pad.ref, pin_pitch)
    except Exception:  # pragma: no cover - defensive (test doubles)
        return fallback
    if isinstance(required, bool) or not isinstance(required, (int, float)):
        return fallback
    return max(float(required), _stitch_via_reservation(grid, pad))


def _pad_copper_clear(
    grid: RoutingGrid,
    candidate: KSegment,
    layer_idx: int,
    exclude_net: int,
) -> bool:
    """Exact clearance of a candidate path against every registered pad.

    Epic #5509 Phase 4a (#5854): the pad narrow phase both checkers in this
    module reach once the raster has flagged a pad-accountable cell.
    ``grid.pads`` is the read-only snapshot Phase 3c (#5662) added for exactly
    this -- migrated consumers need the pad registry to shape it, and reaching
    into ``grid._pads`` is how two consumers end up holding a mutable list.

    Two filters, both transcriptions of what the raster branch already did
    rather than new policy:

    * **Own-net pads are skipped**, matching the ``cell.net == exclude_net``
      carve-out that keeps a route's own destination pad passable.  The filter
      is applied per PAD, the way :func:`_routed_copper_clear` applies its own
      per object.  A pad on a skipped pour net keeps ``net == 0`` (issue
      #2757's rewrite in ``load_pcb_for_routing``) and is therefore foreign to
      every real net, so it still gates -- which is the whole point of that
      issue.
    * **An SMD pad on another layer is skipped**; a through-hole pad is copper
      on every layer and is not.  Same rule ``_add_pad_unsafe`` marks with.

    The requirement is resolved **per pad** by
    :func:`_pad_required_clearance` -- ``rules.get_clearance_for_component``,
    exactly as both in-repo authorities on segment-vs-pad clearance do
    (``RoutingGrid.worst_segment_pad_deficit``, the #3545 finalization
    backstop, and ``DiffPairRouter._kernel_pad_deficit``, the already-migrated
    Phase 3c consumer).  A flat ``rules.trace_clearance`` here would *drop* a
    per-component override the raster halo was built from (PR #5874 review:
    with ``trace_clearance=0.2`` and ``component_clearances={"U1": 0.6}`` both
    checkers accepted a 0.3 mm gap this module refused before Phase 4a), which
    is a rule-value regression, not the de-quantisation this phase is for.
    Resolving the same value the halo used keeps scope guard #1 intact: the
    requirement is the one the grid already resolved for that pad, and only the
    *geometry* moved onto the kernel.
    """
    for pad in grid.pads:
        if pad.net == exclude_net:
            continue
        if not pad.through_hole:
            try:
                if grid.layer_to_index(pad.layer.value) != layer_idx:
                    continue
            except Exception:
                # Unresolvable layer: keep the conservative "assume it
                # blocks" reading ``_via_spans_layer`` already documents.
                pass
        if not _path_clear_of_pad(candidate, pad, _pad_required_clearance(grid, pad)):
            return False
    return True


def _routed_copper_clear(
    grid: RoutingGrid,
    candidate: KSegment,
    layer: Layer,
    layer_idx: int,
    exclude_net: int,
) -> bool:
    """Exact clearance of a candidate path against every committed route.

    Issue #5625: the grid checker's narrow phase.  Same question, same
    arithmetic and same rule values as ``VectorCollisionChecker``'s R-tree
    narrow phase -- only the broad phase differs, because this class is
    selected precisely when no R-tree is available, so candidates come from a
    linear walk of ``grid.routes`` instead.
    """
    min_clearance = grid.rules.trace_clearance
    via_clearance = max(min_clearance, grid.rules.via_clearance)
    layer_value = layer.value

    # Own-net copper is filtered per OBJECT rather than per route, matching
    # the vector checker's R-tree narrow phase (``other_seg.net`` /
    # ``via.net``): a route whose own ``net`` disagrees with an object it
    # carries must not hide that object from this scan.
    for route in grid.routes:
        for seg in route.segments:
            if seg.net == exclude_net or seg.layer.value != layer_value:
                continue
            if not _path_clear_of_segment(candidate, seg, min_clearance):
                return False
        for via in route.vias:
            if via.net == exclude_net:
                continue
            if not _via_spans_layer(grid, via, layer_idx):
                continue
            if not _path_clear_of_via(candidate, via, via_clearance):
                return False
    return True


class CollisionChecker(Protocol):
    """Protocol for checking if a path is clear of obstacles.

    Implementations can use different strategies:
    - Grid-based: Use RoutingGrid obstacle data
    - Segment intersection: Check for crossings with other nets
    - R-tree: Spatial indexing for efficient segment clearance queries
      (implemented in RoutingGrid via per-layer rtree.index.Index, Issue #1249)

    The collision checker should return True if the path is clear,
    False if it would cross obstacles or other nets.
    """

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path from (x1, y1) to (x2, y2) is clear of obstacles.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks (own net).

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        ...


class GridCollisionChecker:
    """Collision checker using the routing grid.

    Uses the RoutingGrid's obstacle data to check if paths are clear.
    This reuses the same collision detection logic as the autorouter.

    When ``ignore_overflow=True``, cells that are blocked by route
    occupation (another net passing through) but are *not* hard obstacles
    (pads, keepouts) are treated as clear.  This prevents the trace
    optimizer from fragmenting routes that pass through cells with minor
    overflow from negotiated routing.  Hard obstacles are always respected
    regardless of this flag.
    """

    def __init__(self, grid: RoutingGrid, ignore_overflow: bool = False):
        """Initialize with a routing grid.

        Args:
            grid: The routing grid with obstacle and net data.
            ignore_overflow: When True, treat cells blocked by route
                occupation (not hard obstacles) as clear.  This is used
                after negotiated routing with residual overflow so that
                the optimizer preserves connectivity instead of
                destroying segments that pass through overused cells.
        """
        self.grid = grid
        self.ignore_overflow = ignore_overflow

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using grid-based collision detection.

        Uses Bresenham's line algorithm to check all grid cells along the path,
        including a buffer for trace width and clearance.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Convert to grid coordinates
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        # Calculate clearance buffer in grid cells
        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Get layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False  # Invalid layer

        # Check all cells along the path using Bresenham's algorithm
        cells_to_check = self._get_path_cells(gx1, gy1, gx2, gy2, clearance_cells)

        # Issue #5625: set when the walk meets a foreign-net cell whose
        # occupancy is fully accounted for by registered route copper.  Such a
        # cell is a broad-phase hit, not a verdict (the raster already carries
        # the committed object's own clearance dilation, so the two envelopes
        # meeting is about twice the real requirement) -- the exact narrow
        # phase below decides it, exactly as ``VectorCollisionChecker`` does.
        needs_exact_route_check = False
        # Epic #5509 Phase 4a (#5854): the same deferral for a cell whose
        # HARD blockage is accountable to a registered pad.  A pad's raster
        # footprint is its metal plus its own clearance halo, so the branches
        # below used to reject a candidate whose copper was a comfortable
        # distance from the metal; ``_pad_copper_clear`` measures it instead.
        needs_exact_pad_check = False
        candidate = _candidate_shape(x1, y1, x2, y2, width)

        for gx, gy in cells_to_check:
            if not (0 <= gx < self.grid.cols and 0 <= gy < self.grid.rows):
                continue  # Out of bounds - skip but don't fail

            # Issue #5240: ``cell_at`` is a documented drop-in for the
            # legacy ``grid.grid[layer][y][x]`` chain (see
            # ``RoutingGrid.cell_at``'s docstring) -- one allocation
            # instead of three chained ``__getitem__`` calls.
            cell = self.grid.cell_at(layer_idx, gy, gx)

            # Check if blocked by another net
            if cell.blocked:
                # Issue #2963: own-net obstacle cells (destination pad
                # metal marked by PR #2928's first-touch) must remain
                # passable for the route's own net.  Foreign-net
                # obstacles still hard-reject.
                if cell.is_obstacle and cell.net != exclude_net:
                    # Epic #5509 Phase 4a (#5854): a pad's halo lands in this
                    # plane too, so defer to the exact pad measurement when
                    # the cell's blockage is accountable to a registered pad.
                    # A keepout / obstacle / region bound / board-edge cell
                    # registers no geometry and keeps the reject below.
                    if _obstacle_cell_is_accountable_pad_copper(self.grid, gx, gy, layer_idx):
                        needs_exact_pad_check = True
                        continue
                    return False  # Hard obstacle (pad, keepout) -- always block

                # Issue #2757: A pad on a skipped pour net (e.g. GND, +3V3)
                # has ``pad_blocked=True`` but ``is_obstacle=False`` and
                # ``cell.net=0`` because ``load_pcb_for_routing`` rewrites
                # skip-net pad nets to 0 (so they aren't routable) but still
                # registers the pad as a copper obstacle in the grid.  Before
                # this fix the optimizer's chamfer / collinear-merge passes
                # walked straight through those cells, producing post-route
                # ``clearance_pad_segment`` violations on every BGA/QFN edge
                # the new diagonal grazed (15 violations on board 06).
                # Treating pad-metal cells as hard obstacles -- except where
                # the cell already belongs to the optimised route's own net
                # (e.g. the route's destination pad) -- closes that hole
                # without affecting normal own-net pad anchoring.
                if cell.pad_blocked and cell.net != exclude_net:
                    # Epic #5509 Phase 4a (#5854): same deferral as the
                    # ``is_obstacle`` branch above -- this plane IS the pad
                    # registry's raster shadow, so when the grid can name the
                    # pads the exact measurement decides it.
                    if _obstacle_cell_is_accountable_pad_copper(self.grid, gx, gy, layer_idx):
                        needs_exact_pad_check = True
                        continue
                    return False

                # Cell is occupied by another net's route (soft block).
                # When ignore_overflow is set, skip this check so the
                # optimizer does not fragment routes through overused cells.
                #
                # Issue #3433: the tolerance is scoped to cells that are
                # GENUINELY overused (``usage_count > 1`` -- the same
                # predicate ``get_total_overflow`` uses).  The original
                # #2303 blanket skip made the checker blind to EVERY
                # foreign-net trace whenever the router finished with
                # ANY residual overflow: on board 04 (overflow=2 in an
                # unrelated OSC corridor) the staircase-compression pass
                # straightened SWO's B.Cu zigzag into a long diagonal
                # running straight across SWCLK's clearance-respecting
                # run, committing -0.200 mm full overlaps that no later
                # pass can repair.  The bug is environment-sensitive:
                # machines with the ``rtree`` package use
                # ``VectorCollisionChecker``, whose exact narrow phase
                # never honored ``ignore_overflow`` for foreign
                # segments -- only this grid fallback was blind.
                if cell.net != 0 and cell.net != exclude_net:
                    if self.ignore_overflow and cell.usage_count > 1:
                        continue  # Genuinely overused cell -- tolerated.

                    # Issue #5625: ``VectorCollisionChecker`` answers this
                    # same question by measuring the foreign copper exactly,
                    # and the two checkers disagreed on 16 of the 53
                    # path probes in Epic #5509's seeded corpus (seeds 0-19)
                    # because this branch answered it off the raster instead.
                    # Defer to the same exact measurement whenever the cell's
                    # occupancy is accountable to registered copper; a cell
                    # that is not (a keepout, an obstacle, a region bound, the
                    # board-edge band -- none of which register geometry to
                    # re-measure) keeps the conservative reject below.
                    if _soft_cell_is_accountable_route_copper(self.grid, gx, gy, layer_idx):
                        needs_exact_route_check = True
                        continue
                    return False  # Blocked by another net

        if needs_exact_route_check and not _routed_copper_clear(
            self.grid, candidate, layer, layer_idx, exclude_net
        ):
            return False

        if needs_exact_pad_check and not _pad_copper_clear(
            self.grid, candidate, layer_idx, exclude_net
        ):
            return False

        return True

    def _get_path_cells(
        self, gx1: int, gy1: int, gx2: int, gy2: int, clearance: int
    ) -> list[tuple[int, int]]:
        """Get all grid cells along a path with clearance buffer.

        Uses Bresenham's line algorithm with clearance expansion.

        Args:
            gx1, gy1: Start grid coordinates.
            gx2, gy2: End grid coordinates.
            clearance: Clearance buffer in grid cells.

        Returns:
            List of (gx, gy) grid coordinates to check.
        """
        # Issue #5240: delegate to the shared incremental dilation helper
        # (see its docstring for the exact-equivalence proof) instead of
        # recomputing the full clearance window at every rasterized point.
        # Same final cell SET as the prior per-point ``set.add`` loop --
        # only the amount of redundant work to reach it changed.
        return list({*_iter_dilated_line_cells(gx1, gy1, gx2, gy2, clearance)})


class VectorCollisionChecker:
    """Collision checker using exact vector math and R-tree spatial indexing.

    Instead of discretizing the path onto a grid (O(L * W) cells), this
    checker performs broad-phase candidate filtering via the R-tree spatial
    index already maintained by ``RoutingGrid``, then applies exact
    segment-to-segment distance calculations for narrow-phase clearance
    checks.

    This is typically 10x+ faster than ``GridCollisionChecker`` for boards
    with many routed nets, because the R-tree query returns only nearby
    candidates and the analytical distance check is O(1) per candidate pair.

    Falls back to ``GridCollisionChecker`` when the R-tree is unavailable
    (e.g. rtree package not installed) or the segment count is below the
    R-tree activation threshold.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        ignore_overflow: bool = False,
    ):
        """Initialize with a routing grid that has R-tree spatial index data.

        Args:
            grid: The routing grid with R-tree index and obstacle data.
            ignore_overflow: When True, treat cells blocked by route
                occupation (not hard obstacles) as clear.
        """
        self.grid = grid
        self.ignore_overflow = ignore_overflow
        # Lazy-initialized fallback for when R-tree is unavailable on a layer
        self._grid_fallback: GridCollisionChecker | None = None

    def _get_grid_fallback(self) -> GridCollisionChecker:
        """Get or create the grid-based fallback checker."""
        if self._grid_fallback is None:
            self._grid_fallback = GridCollisionChecker(
                self.grid, ignore_overflow=self.ignore_overflow
            )
        return self._grid_fallback

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear using R-tree + exact vector math.

        Broad phase: queries the per-layer R-tree for candidate segments
        whose bounding boxes overlap the query path's envelope (expanded
        by half-width + trace clearance).

        Narrow phase: computes exact segment-to-segment distance for each
        candidate and checks edge-to-edge clearance against the design
        rule minimum.

        Falls back to ``GridCollisionChecker`` when R-tree data is not
        available for the requested layer.

        Args:
            x1, y1: Start point coordinates.
            x2, y2: End point coordinates.
            layer: The layer the path is on.
            width: The trace width.
            exclude_net: Net ID to exclude from collision checks.

        Returns:
            True if the path is clear, False if it would cross obstacles.
        """
        # Resolve layer index
        try:
            layer_idx = self.grid.layer_to_index(layer.value)
        except Exception:
            return False

        # Check if R-tree is available and populated for this layer
        if not self.grid._rtree_available or layer_idx not in self.grid._seg_rtree:
            return self._get_grid_fallback().path_is_clear(
                x1, y1, x2, y2, layer, width, exclude_net
            )

        min_clearance = self.grid.rules.trace_clearance
        half_width = width / 2
        search_radius = half_width + min_clearance
        # Epic #5509 Phase 4a (#5854): one kernel shape for the candidate,
        # built once and reused by every narrow-phase query below.
        candidate = _candidate_shape(x1, y1, x2, y2, width)

        # Broad phase: query R-tree with expanded envelope
        query_envelope = (
            min(x1, x2) - search_radius,
            min(y1, y2) - search_radius,
            max(x1, x2) + search_radius,
            max(y1, y2) + search_radius,
        )
        candidate_ids = list(self.grid._seg_rtree[layer_idx].intersection(query_envelope))
        layer_items: dict[int, Any] = self.grid._seg_rtree_items.get(layer_idx, {})

        # Narrow phase: exact distance check for each candidate
        for cand_id in candidate_ids:
            other_seg: Segment | None = layer_items.get(cand_id)
            if other_seg is None:
                continue

            # Skip own-net segments
            if other_seg.net == exclude_net:
                continue

            # Exact edge-to-edge clearance.  Issue #5625: the arithmetic moved
            # to a module-level helper the grid checker's own exact pass calls
            # too, so the two implementations of this protocol can no longer
            # drift apart on the number they compare.  Epic #5509 Phase 4a
            # (#5854): that helper now asks the shared clearance kernel.
            if not _path_clear_of_segment(candidate, other_seg, min_clearance):
                return False

        # Issue #2955 / #2960: Check against foreign-net vias.
        #
        # The segment R-tree above does not index vias, so without an
        # explicit via check the optimizer's ``compress_staircase`` /
        # ``convert_45_corners`` passes happily replace a
        # clearance-respecting zigzag with a diagonal that grazes or
        # punches a foreign-net through-hole via.  The canonical
        # board-03 failure was XTAL1's B.Cu trace rewritten to a single
        # off-grid segment running 0.14 mm from XTAL2's via at
        # (125.6, 128.3), producing ``clearance_segment_segment`` /
        # ``clearance_segment_via`` post-route DRC pairs.
        #
        # ``GridCollisionChecker`` is implicitly safe against this
        # because ``_mark_via`` paints ``cell.net = via.net`` on every
        # blocked cell in the via's clearance envelope, so the
        # Bresenham walk hits the via at the soft-block branch
        # (``cell.net != exclude_net``).  The vector path needs an
        # explicit check.
        #
        # PR #2958 (issue #2955) added a double-nested linear scan over
        # ``grid.routes × route.vias`` here, which the optimizer
        # invokes thousands of times per net.  On boards 06/07 this
        # produced a fleet-wide ~3x slowdown (issue #2960).
        #
        # Issue #2960 replaces that scan with an R-tree query against
        # the via index maintained by ``RoutingGrid`` (mirrored to
        # ``self.routes`` mutations in ``mark_route`` / ``unmark_route``).
        # The broad phase returns only vias whose AABB overlaps the
        # path's query envelope; the narrow phase keeps the existing
        # ``_via_on_layer`` + point-to-segment distance contract.
        #
        # Through-hole vias span ``layers[0]`` -> ``layers[1]`` inclusive
        # of everything in between (KiCad does not enumerate inner
        # layers in the S-expression).  ``validate_segment_clearance``
        # (grid.py) uses the same "check every via on every layer"
        # simplification -- it's conservative-safe (at most a handful
        # of false-positive rejections on multi-layer boards with
        # blind/buried vias, which kicad-tools does not currently
        # emit).
        via_clearance = max(min_clearance, self.grid.rules.via_clearance)
        via_search_radius = half_width + via_clearance
        via_rtree = getattr(self.grid, "_via_rtree", None)
        via_items: dict[int, Any] = getattr(self.grid, "_via_rtree_items", {})
        if via_rtree is not None and via_items:
            # Broad-phase query envelope: path AABB inflated by
            # half_width + via_clearance.  Each indexed via envelope is
            # already inflated by ``via_radius + max_clearance + max_trace_half_width``
            # (see ``RoutingGrid._compute_via_rtree_inflation``), so the
            # union of the two envelopes is a conservative superset of
            # the actual clearance check region for any via.
            query_envelope = (
                min(x1, x2) - via_search_radius,
                min(y1, y2) - via_search_radius,
                max(x1, x2) + via_search_radius,
                max(y1, y2) + via_search_radius,
            )
            for via_id in via_rtree.intersection(query_envelope):
                via = via_items.get(via_id)
                if via is None:
                    continue
                # Skip own-net vias (matches the pre-fix per-route filter).
                if via.net == exclude_net:
                    continue
                # Layer filter mirrors the linear-scan version.
                if not self._via_on_layer(via, layer_idx):
                    continue
                if not _path_clear_of_via(candidate, via, via_clearance):
                    return False
        else:
            # Fallback: index not built (e.g. mock grids in unit tests,
            # or rtree unavailable).  Use the original linear scan so
            # correctness from PR #2958 is preserved unconditionally.
            for route in self.grid.routes:
                if route.net == exclude_net:
                    continue
                for via in route.vias:
                    if not self._via_on_layer(via, layer_idx):
                        continue
                    if not _path_clear_of_via(candidate, via, via_clearance):
                        return False

        # Also check hard obstacles (pads, keepouts) via the grid
        # The R-tree only indexes routed segments, not static obstacles,
        # so we use the grid's obstacle layer for pad/keepout checks.
        if not self._check_obstacles_clear(x1, y1, x2, y2, layer_idx, width, exclude_net):
            return False

        return True

    def _via_on_layer(self, via: Any, layer_idx: int) -> bool:
        """Return True if ``via`` blocks copper on ``layer_idx``.

        Through-hole vias (the common case in kicad-tools today) declare
        ``layers=(F.Cu, B.Cu)`` and physically block every layer in between
        as well.  Blind / buried vias declare a sub-range.  This helper maps
        the start / end layer enum values to grid layer indices and returns
        ``True`` iff ``layer_idx`` falls in the inclusive range.

        When the layer mapping cannot be resolved (unexpected Layer enum
        value, etc.) the helper returns ``True`` to preserve the conservative
        "assume blocking" behaviour of ``grid.validate_segment_clearance``
        which iterates every via without layer filtering.

        Issue #5625: the body now lives in the module-level
        :func:`_via_spans_layer` so the grid checker's exact pass applies the
        identical span rule; this method is kept as the (unchanged) bound
        name its call sites and tests already use.
        """
        return _via_spans_layer(self.grid, via, layer_idx)

    def _check_obstacles_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer_idx: int,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check that a path does not cross hard obstacles (pads, keepouts).

        Samples the path at grid resolution and checks each cell for hard
        obstacles.  This is lighter than a full Bresenham sweep because we
        only check obstacle status, not soft net occupation.

        Epic #5509 Phase 4a (#5854): that sample is now a **broad phase** for
        pad copper.  A cell whose blockage is accountable to a registered pad
        (:func:`_obstacle_cell_is_accountable_pad_copper`) no longer pronounces
        a verdict -- it schedules :func:`_pad_copper_clear`, which measures the
        candidate against the pad's exact outline through the shared clearance
        kernel.  This is where group 15's ``pad-seg`` over-rejection came from:
        the raster carries the pad's own clearance halo *and* dilates the
        candidate by ``width / 2 + trace_clearance`` on top of it, so a path a
        comfortable 0.247 mm from pad metal was refused against a 0.20 mm
        requirement.  A cell that registers no geometry (keepout, obstacle,
        region bound, board-edge band) keeps its conservative reject, exactly
        as the #5625 route-copper deferral does.

        Args:
            x1, y1: Start point coordinates (world).
            x2, y2: End point coordinates (world).
            layer_idx: Layer index.
            width: Trace width.
            exclude_net: Net ID to exclude.

        Returns:
            True if clear of hard obstacles.
        """
        gx1, gy1 = self.grid.world_to_grid(x1, y1)
        gx2, gy2 = self.grid.world_to_grid(x2, y2)

        total_clearance = width / 2 + self.grid.rules.trace_clearance
        clearance_cells = int(total_clearance / self.grid.resolution) + 1

        # Issue #5240: walk only the cells the incremental dilation helper
        # emits (see its docstring for the exact-equivalence proof) instead
        # of recomputing the full clearance window at every rasterized
        # point.  This is the same traversal -- with a possible duplicate
        # revisit of one corner cell per diagonal step, which is harmless
        # here (an early-exit obstacle scan, not a set builder) -- as the
        # prior nested-range loop, just without the O(clearance) *redundant*
        # cells per step that were already covered by the previous step's
        # window.
        #
        # Issue #5240: read the four backing planes directly instead of
        # materialising a ``_CellView`` per cell via ``cell_at(...)`` and then
        # reading four Python ``property`` descriptors off it.
        #
        # ``_CellView`` exists so mutating call sites (``cell.blocked = True``
        # and friends) and one-off single-field reads get a clean object API.
        # This loop is neither: it reads all four fields for
        # O(segment_length * clearance_cells) cells on every optimizer
        # candidate, so the wrapper's per-cell allocation plus four attribute
        # lookups dominate the actual NumPy indexing.  Reading the same planes
        # ``_CellView`` itself indexes (``grid._blocked`` etc.) is the idiom
        # already used for the analogous hot predicates in
        # ``pathfinder.py::_is_diagonal_blocked`` and
        # ``cpp_backend.py::from_routing_grid``.  Slicing out the 2D per-layer
        # views once, rather than 3-tuple-indexing the 3D array per cell, also
        # drops the leading-axis lookup from every iteration.
        #
        # Measured with ``rtree`` installed (``uv sync --extra dev``, matching
        # every CI job) -- without it ``make_collision_checker`` never selects
        # this class, so the scan under measurement is never reached:
        #
        # * ``scripts/research/profile_route_obstacle_scan.py`` on a real
        #   board-02 ``kct route``: this method's inclusive cost fell from
        #   8.655 s to 2.870 s over an identical 6,394 calls (1353.6 -> 448.8
        #   us/call), i.e. 6.1% -> 2.1% of route wall clock.
        # * ``scripts/research/bench_collision_obstacle_scan.py`` in isolation
        #   on a board-06-shaped 0.05 mm grid: 759.1 -> 267.3 us/probe, 2.84x,
        #   with all 400 probe verdicts identical.
        #
        # Exact-output controls: the routed board-02 PCB is byte-identical
        # across the two arms once random UUIDs are normalised, and
        # ``tests/router/test_collision_obstacle_scan_parity_5240.py`` pins
        # this loop against a transcription of the ``cell_at`` version it
        # replaces (the mock grids in ``tests/test_router_collision.py``
        # cannot tell the two code paths apart).
        blocked_layer = self.grid._blocked[layer_idx]
        is_obstacle_layer = self.grid._is_obstacle[layer_idx]
        pad_blocked_layer = self.grid._pad_blocked[layer_idx]
        net_layer = self.grid._net[layer_idx]
        cols = self.grid.cols
        rows = self.grid.rows

        # Epic #5509 Phase 4a (#5854): set when the walk meets a hard-blocked
        # cell the pad registry can account for -- the exact narrow phase
        # below decides it.
        needs_exact_pad_check = False

        for check_x, check_y in _iter_dilated_line_cells(gx1, gy1, gx2, gy2, clearance_cells):
            if not (0 <= check_x < cols and 0 <= check_y < rows):
                continue
            if not blocked_layer[check_y, check_x]:
                continue
            is_obstacle = bool(is_obstacle_layer[check_y, check_x])
            pad_blocked = bool(pad_blocked_layer[check_y, check_x])
            if is_obstacle or pad_blocked:
                # Hard obstacle (cross-net pad) OR pad-copper cell
                # (Issue #2757: pads on skipped pour nets have
                # pad_blocked=True but is_obstacle=False because
                # their net was rewritten to 0 by
                # load_pcb_for_routing; treat them as obstacles
                # too so the optimizer doesn't chamfer through
                # BGA GND / power pads).
                cell_net = int(net_layer[check_y, check_x])
                if cell_net != 0 and cell_net == exclude_net:
                    continue  # Own-net pad is OK
                if pad_blocked and cell_net == exclude_net:
                    continue  # Own-net pad-metal cell (net match)
                if _obstacle_cell_is_accountable_pad_copper(self.grid, check_x, check_y, layer_idx):
                    needs_exact_pad_check = True
                    continue
                return False

        if needs_exact_pad_check:
            candidate = _candidate_shape(x1, y1, x2, y2, width)
            if not _pad_copper_clear(self.grid, candidate, layer_idx, exclude_net):
                return False

        return True


def make_collision_checker(
    grid: RoutingGrid,
    ignore_overflow: bool = False,
) -> GridCollisionChecker | VectorCollisionChecker:
    """Select the best collision checker for the given grid.

    Returns a ``VectorCollisionChecker`` when the grid has an R-tree
    spatial index available and populated, otherwise falls back to
    ``GridCollisionChecker``.

    Args:
        grid: The routing grid with obstacle and net data.
        ignore_overflow: When True, treat cells blocked by route
            occupation (not hard obstacles) as clear.

    Returns:
        The most efficient collision checker for the given grid state.
    """
    if grid._rtree_available and grid._seg_rtree_count > 0:
        return VectorCollisionChecker(grid, ignore_overflow=ignore_overflow)
    return GridCollisionChecker(grid, ignore_overflow=ignore_overflow)
