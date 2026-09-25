"""Equivalence pins for the vectorised coupled via raster (Issue #5720).

Background
----------
PR #5719 (#5696) memoised the *geometric* half of ``RouteHaloRefiner.via_clear``
and cut it from 16.977 to 2.029 us/call, but the composed predicate
``CoupledPathfinder._is_via_blocked`` only improved 168.971 -> 131.911 us/call:
the residual is the **raster** work the predicate does itself, per cell, in
Python:

1. ``num_layers x (2 * via_extra_cells + 1)^2`` ``_is_cell_blocked`` calls to
   build a ``blocked_cells`` list (47.3 us of 131.2 on the fixture below),
2. the same cells again through ``RouteHaloRefiner.cells_known`` ->
   ``RouteHaloGeometry.cell_known`` (37.3 us),
3. a ``num_layers x (2 * drill_cells + 1)^2``
   ``grid.cell_at(...).pad_blocked`` drill sweep that allocated a ``_CellView``
   per cell (30.6 us) -- the exact pattern #5617 had already removed from
   ``cell_known``.

Issue #5720 replaces all three with occupancy-plane slices over the whole
candidate column.  That is a pure-performance change, so what needs pinning is
**equivalence**, not behaviour:

* ``_reference_is_via_blocked`` below is the pre-#5720 per-cell loop, written
  out verbatim, and the sweeps assert the shipped predicate agrees with it
  cell for cell.  It is spelled out here rather than reached through a shipped
  helper so a regression cannot make this module agree with itself.
* ``RouteHaloGeometry.window_known`` is pinned against per-cell
  ``cell_known``, and ``RouteHaloRefiner.cells_known_mask`` against per-layer
  ``cells_known``.
* The guard must stay **per-layer**.  #5410/#5696 established that the via
  *geometry* verdict is layer-independent (a through via spans the whole
  stack); the raster is not.  ``test_hard_blockage_on_one_layer_still_rejects``
  and ``test_reservation_on_one_layer_still_rejects`` are the witnesses -- they
  fail if the column-wide mask is ever collapsed across layers.
* No caching was added here, so the four hard-blockage flags that are written
  *without* bumping ``grid.occupancy_generation`` must still be observed on the
  very next call.  ``test_hard_blockage_without_an_occupancy_bump_still_rejects``
  pins that from ``_is_via_blocked``'s side (the memo's own side is pinned by
  ``test_coupled_route_halo_memo_5696.py``).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

#: Mirrors ``tests/test_coupled_route_halo_clearance.py`` -- a cell covered by
#: net 2's conservative via halo whose real copper is far enough away that a
#: net-1 via there is legal.
LEGAL = (55, 56)
#: One cell closer: the illegal control.
ILLEGAL_VIA = (56, 56)
LAYER = 2

#: A window straddling the stored via, so the sweeps see BOTH verdicts.
_SWEEP = [(gx, gy) for gx in range(54, 68) for gy in range(54, 68)]


def _rules(*, via_drill: float = 0.3) -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=via_drill,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )


#: ``via_drill`` values that make the drill footprint and the via envelope
#: differ in size.  On the default rules both are 5x5, which would let a
#: dropped ``_pad_blocked`` conjunct in ``window_known`` -- or a ``min`` where
#: the bounds test needs a ``max`` -- hide behind the other footprint's check.
#: 0.3 mm -> ``drill_cells`` 2 == ``via_extra_cells``; 0.1 mm -> 1 < 2.
_DRILLS = [0.3, 0.1]
#: The narrow-drill variant, named for the tests that specifically need
#: ``drill_cells < via_extra_cells``.
_NARROW_DRILL = 0.1


def _context(
    *,
    armed: bool = True,
    net_class_map: dict[str, NetClassRouting] | None = None,
    via_drill: float = 0.3,
) -> tuple[RoutingGrid, CoupledPathfinder]:
    rules = _rules(via_drill=via_drill)
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    x, y = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)
    pathfinder = CoupledPathfinder(grid, rules, target_spacing_cells=3, net_class_map=net_class_map)
    if armed:
        pathfinder.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, pathfinder


def _reference_is_via_blocked(pathfinder: CoupledPathfinder, gx: int, gy: int, net: int) -> bool:
    """The exact pre-#5720 body of ``CoupledPathfinder._is_via_blocked``.

    Three per-cell Python sweeps, in their original order, reached through the
    still-shipped ``_is_cell_blocked`` / ``cells_known`` / ``via_clear`` /
    ``cell_at`` entry points.
    """
    grid = pathfinder.grid
    refiner = pathfinder._halo_refiner
    drill_cells = max(0, int(math.ceil((pathfinder.rules.via_drill / 2) / grid.resolution)))
    extra = pathfinder._via_extra_cells
    via_geometry_clear: bool | None = None
    for layer in range(grid.num_layers):
        blocked_cells: list[tuple[int, int]] = []
        for dy in range(-extra, extra + 1):
            for dx in range(-extra, extra + 1):
                if pathfinder._is_cell_blocked(gx + dx, gy + dy, layer, net):
                    blocked_cells.append((gx + dx, gy + dy))
        if blocked_cells:
            if not refiner.cells_known(blocked_cells, layer):
                return True
            if via_geometry_clear is None:
                via_geometry_clear = refiner.via_clear(blocked_cells, layer, gx, gy, net)
            if not via_geometry_clear:
                return True
        for dy in range(-drill_cells, drill_cells + 1):
            for dx in range(-drill_cells, drill_cells + 1):
                cgx, cgy = gx + dx, gy + dy
                if not (0 <= cgx < grid.cols and 0 <= cgy < grid.rows):
                    return True
                if grid.cell_at(layer, cgy, cgx).pad_blocked:
                    return True
    return False


def _a_blocked_envelope_cell(pathfinder: CoupledPathfinder, gx: int, gy: int, net: int):
    """A cell inside ``(gx, gy)``'s via envelope that the raster blocks."""
    extra = pathfinder._via_extra_cells
    for dy in range(-extra, extra + 1):
        for dx in range(-extra, extra + 1):
            if pathfinder._is_cell_blocked(gx + dx, gy + dy, 0, net):
                return gx + dx, gy + dy
    raise AssertionError("fixture has no blocked cell in the envelope")


# ---------------------------------------------------------------------------
# The window is meaningful
# ---------------------------------------------------------------------------


def test_probe_window_is_not_degenerate():
    """Guard: the parity sweeps are only meaningful if they see both verdicts."""
    _, pathfinder = _context()
    verdicts = {pathfinder._is_via_blocked(gx, gy, 1) for gx, gy in _SWEEP}
    assert verdicts == {True, False}
    assert pathfinder._is_via_blocked(*LEGAL, 1) is False
    assert pathfinder._is_via_blocked(*ILLEGAL_VIA, 1) is True


def test_the_fixture_exercises_more_than_one_envelope_cell():
    """Guard: a 1-cell envelope would not distinguish the mask from a scalar."""
    _, pathfinder = _context()
    assert pathfinder._via_extra_cells >= 1
    extra = pathfinder._via_extra_cells
    gx, gy = LEGAL
    blocked = sum(
        pathfinder._is_cell_blocked(gx + dx, gy + dy, LAYER, 1)
        for dy in range(-extra, extra + 1)
        for dx in range(-extra, extra + 1)
    )
    assert 1 < blocked < (2 * extra + 1) ** 2


# ---------------------------------------------------------------------------
# (a) the vectorised predicate == the per-cell reference
# ---------------------------------------------------------------------------


def test_is_via_blocked_matches_the_per_cell_reference_everywhere():
    """Every verdict equals the pre-#5720 triple-sweep implementation's."""
    _, pathfinder = _context()
    for gx, gy in _SWEEP:
        expected = _reference_is_via_blocked(pathfinder, gx, gy, 1)
        assert pathfinder._is_via_blocked(gx, gy, 1) is expected, (gx, gy)


def test_the_two_rule_variants_size_the_footprints_differently():
    """Guard: ``_DRILLS`` must actually separate the two footprints."""
    sizes = []
    for via_drill in _DRILLS:
        grid, pathfinder = _context(via_drill=via_drill)
        drill_cells = max(0, int(math.ceil((pathfinder.rules.via_drill / 2) / grid.resolution)))
        sizes.append((drill_cells, pathfinder._via_extra_cells))
    assert sizes[0][0] == sizes[0][1], sizes
    assert sizes[1][0] < sizes[1][1], sizes


@pytest.mark.parametrize("via_drill", _DRILLS)
def test_is_via_blocked_matches_the_reference_at_and_beyond_the_grid_edge(via_drill):
    """The hoisted bounds test stands in for both footprints' rejections."""
    grid, pathfinder = _context(via_drill=via_drill)
    reach = max(
        pathfinder._via_extra_cells,
        max(0, int(math.ceil((pathfinder.rules.via_drill / 2) / grid.resolution))),
    )
    assert reach >= 1
    assert grid.cols == grid.rows, "the shared edge list below assumes a square grid"
    edges = [
        0,
        1,
        reach - 1,
        reach,
        reach + 1,
        # The exact coordinates where one footprint's far edge lands ON the
        # first out-of-bounds index -- an inclusive/exclusive slip here is
        # invisible to numpy, which clips an over-long slice silently.
        grid.cols - reach - 2,
        grid.cols - reach - 1,
        grid.cols - reach,
        grid.cols - reach + 1,
        grid.cols - 1,
        grid.cols,
    ]
    probes = [(gx, gy) for gx in edges for gy in edges]
    # The interesting half: at least one probe must be rejected purely by the
    # bounds test, and at least one must survive it.
    rejected = [p for p in probes if pathfinder._is_via_blocked(*p, 1)]
    assert rejected and len(rejected) < len(probes)
    for gx, gy in probes:
        expected = _reference_is_via_blocked(pathfinder, gx, gy, 1)
        assert pathfinder._is_via_blocked(gx, gy, 1) is expected, (gx, gy)


def test_is_via_blocked_matches_the_reference_for_the_halo_owner_net():
    """Own-net passability (#3508) is part of the mask, not of the guard."""
    _, pathfinder = _context()
    for gx, gy in _SWEEP:
        expected = _reference_is_via_blocked(pathfinder, gx, gy, 2)
        assert pathfinder._is_via_blocked(gx, gy, 2) is expected, (gx, gy)


def test_is_via_blocked_matches_the_reference_when_dormant():
    """An unarmed refiner fails closed on both implementations (#5410 B2)."""
    _, pathfinder = _context(armed=False)
    assert not pathfinder._halo_refiner.armed
    assert pathfinder._is_via_blocked(*LEGAL, 1) is True
    for gx, gy in _SWEEP:
        expected = _reference_is_via_blocked(pathfinder, gx, gy, 1)
        assert pathfinder._is_via_blocked(gx, gy, 1) is expected, (gx, gy)


# ---------------------------------------------------------------------------
# (b) window_known == cell_known, cells_known_mask == cells_known
# ---------------------------------------------------------------------------


def _seed_every_cell_known_conjunct(grid: RoutingGrid, x0: int, y0: int, x1: int, y1: int) -> None:
    """Make every rejecting conjunct of ``cell_known`` load-bearing in a window.

    Four flags are planted on cells the halo currently DOES vouch for (so each
    one has to flip a ``True`` to a ``False``), and two synthetic blockages are
    planted on cells the halo does NOT cover (so ``net > 0`` and the
    ``_cells == _net`` ownership compare each have to flip a ``False`` to a
    ``True``).  All six are raw plane writes: none bumps
    ``occupancy_generation``, which is exactly the regime #5696 keeps the guard
    out of its memo for.
    """
    halo = grid._route_halo
    halo._refresh()
    cells = [
        (layer, x, y)
        for layer in range(grid.num_layers)
        for y in range(y0, y1)
        for x in range(x0, x1)
    ]
    vouched = [c for c in cells if halo.cell_known(c[1], c[2], c[0])]
    unmarked = [
        (layer, x, y)
        for layer, x, y in cells
        if halo._cells[layer, y, x] == 0 and not grid._static_blocked[layer, y, x]
    ]
    assert len(vouched) >= 4 and len(unmarked) >= 2

    (pl, px, py), (ol, ox, oy), (sl, sx, sy), (rl, rx, ry) = vouched[:4]
    grid._pad_blocked[pl, py, px] = True
    grid._is_obstacle[ol, oy, ox] = True
    grid._static_blocked[sl, sy, sx] = True
    grid._reserved_for_nets[(rl, ry, rx)] = frozenset({7})

    # Blocked with no owner at all, and blocked by a net that owns no halo mark
    # here -- the two ways a blocked cell can still be unverifiable.
    (zl, zx, zy), (fl, fx, fy) = unmarked[:2]
    grid._blocked[zl, zy, zx] = True
    grid._net[zl, zy, zx] = 0
    grid._blocked[fl, fy, fx] = True
    grid._net[fl, fy, fx] = 3


def test_window_known_matches_cell_known_cell_for_cell():
    grid, _ = _context()
    halo = grid._route_halo
    x0, y0, x1, y1 = 54, 54, 68, 68
    _seed_every_cell_known_conjunct(grid, x0, y0, x1, y1)
    known = halo.window_known(x0, y0, x1, y1)
    assert known is not None
    assert known.shape == (grid.num_layers, y1 - y0, x1 - x0)
    # Guard: the window must contain both answers.
    assert bool(known.any()) and not bool(known.all())
    for layer in range(grid.num_layers):
        for y in range(y0, y1):
            for x in range(x0, x1):
                assert bool(known[layer, y - y0, x - x0]) is halo.cell_known(x, y, layer), (
                    layer,
                    x,
                    y,
                )


@pytest.mark.parametrize(
    "window",
    [
        (-1, 54, 60, 60),
        (54, -1, 60, 60),
        (150, 54, 1000, 60),
        (54, 150, 60, 1000),
        (54, 54, 54, 60),
    ],
)
def test_window_known_is_none_outside_the_grid(window):
    """``cell_known`` answers False for every cell of such a window."""
    grid, _ = _context()
    halo = grid._route_halo
    assert halo.window_known(*window) is None


def test_window_known_does_not_perturb_the_memo_token():
    """An extra ``_refresh`` cannot bump ``state_version`` twice (#5696)."""
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    halo = grid._route_halo
    before = refiner._memo_token(halo)
    for _ in range(3):
        assert pathfinder._is_via_blocked(*LEGAL, 1) is False
        assert halo.window_known(54, 54, 60, 60) is not None
    assert refiner._memo_token(halo) == before


def test_cells_known_mask_matches_cells_known_per_layer():
    import numpy as np

    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    extra = pathfinder._via_extra_cells
    for gx, gy in _SWEEP:
        x0, y0 = gx - extra, gy - extra
        side = 2 * extra + 1
        mask = np.zeros((grid.num_layers, side, side), dtype=np.bool_)
        per_layer_cells: dict[int, list[tuple[int, int]]] = {}
        for layer in range(grid.num_layers):
            cells = []
            for dy in range(side):
                for dx in range(side):
                    if pathfinder._is_cell_blocked(x0 + dx, y0 + dy, layer, 1):
                        mask[layer, dy, dx] = True
                        cells.append((x0 + dx, y0 + dy))
            per_layer_cells[layer] = cells
        if not mask.any():
            continue
        expected = all(
            refiner.cells_known(cells, layer) for layer, cells in per_layer_cells.items() if cells
        )
        assert refiner.cells_known_mask(mask, x0, y0) is expected, (gx, gy)


def test_cells_known_mask_fails_closed_when_dormant():
    import numpy as np

    grid, pathfinder = _context(armed=False)
    refiner = pathfinder._halo_refiner
    mask = np.zeros((grid.num_layers, 3, 3), dtype=np.bool_)
    assert refiner.cells_known_mask(mask, 54, 54) is False
    mask[LAYER, 1, 1] = True
    assert refiner.cells_known_mask(mask, 54, 54) is False


# ---------------------------------------------------------------------------
# (c) the guard stays PER-LAYER (the raster is not layer-independent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["static", "obstacle", "reserved"])
def test_hard_blockage_on_one_layer_still_rejects(kind):
    """A flag on ONE layer must reject, even though other layers stay clean.

    Collapsing the column-wide mask across layers (``any(axis=0)``) would let
    the clean layers vouch for the dirty one.  ``pad`` is excluded only because
    on this fixture the drill footprint and the via envelope coincide, so the
    via-in-pad test already rejects it before the guard runs -- it is covered by
    ``test_hard_blockage_without_an_occupancy_bump_still_rejects``.
    """
    grid, pathfinder = _context()
    assert pathfinder._is_via_blocked(*LEGAL, 1) is False
    x, y = _a_blocked_envelope_cell(pathfinder, *LEGAL, 1)
    dirty_layer = 1
    generation = grid.occupancy_generation

    if kind == "static":
        grid._static_blocked[dirty_layer, y, x] = True
    elif kind == "obstacle":
        grid._is_obstacle[dirty_layer, y, x] = True
    else:
        grid._reserved_for_nets[(dirty_layer, y, x)] = frozenset({7})

    assert grid.occupancy_generation == generation, "control must not bump occupancy"
    # The other layers are untouched and still verifiable.
    assert grid._route_halo.cell_known(x, y, dirty_layer) is False
    assert grid._route_halo.cell_known(x, y, LAYER) is True
    assert pathfinder._is_via_blocked(*LEGAL, 1) is True
    assert _reference_is_via_blocked(pathfinder, *LEGAL, 1) is True


def test_blockage_on_a_non_zero_layer_only_still_rejects():
    """The envelope mask must cover the whole stack, not just layer 0.

    The fixture's only raster blockage is a THROUGH via's halo, which looks
    identical on all four layers -- so a layer-0-only envelope would agree with
    the reference everywhere until some copper exists on one inner layer alone.
    This plants exactly that: an unverifiable blocked cell on layer 1, at a cell
    layer 0 leaves entirely free.
    """
    grid, pathfinder = _context()
    extra = pathfinder._via_extra_cells
    gx, gy = LEGAL
    free = [
        (gx + dx, gy + dy)
        for dy in range(-extra, extra + 1)
        for dx in range(-extra, extra + 1)
        if not any(
            pathfinder._is_cell_blocked(gx + dx, gy + dy, layer, 1)
            for layer in range(grid.num_layers)
        )
    ]
    assert free, "fixture has no free cell in the envelope"
    x, y = free[0]
    assert pathfinder._is_via_blocked(gx, gy, 1) is False

    grid._blocked[1, y, x] = True
    grid._net[1, y, x] = 3
    assert pathfinder._is_cell_blocked(x, y, 0, 1) is False
    assert pathfinder._is_cell_blocked(x, y, 1, 1) is True
    assert grid._route_halo.cell_known(x, y, 1) is False
    assert pathfinder._is_via_blocked(gx, gy, 1) is True
    assert _reference_is_via_blocked(pathfinder, gx, gy, 1) is True


def test_via_clear_verified_fails_closed_when_dormant():
    """The unguarded entry point still refuses to answer without a net map.

    ``_is_via_blocked`` never reaches it while dormant (``cells_known_mask``
    rejects first), so this is the only pin on its own dormancy gate -- and the
    gate is what keeps the precondition contract from being the *only* thing
    standing between a future caller and a relaxation with no net names
    resolved (#5410 B2).
    """
    _, pathfinder = _context(armed=False)
    refiner = pathfinder._halo_refiner
    assert not refiner.armed
    assert refiner.via_clear_verified(*LEGAL, 1) is False
    assert not refiner._via_memo

    pathfinder.set_net_name_to_id({"N1": 1, "N2": 2})
    assert refiner.via_clear_verified(*LEGAL, 1) is True


def test_reservation_on_one_layer_still_rejects():
    """``_reserved_for_nets`` is keyed on ``(layer, y, x)``, not on ``(y, x)``."""
    grid, pathfinder = _context()
    x, y = _a_blocked_envelope_cell(pathfinder, *LEGAL, 1)
    # A reservation on a DIFFERENT cell of a different layer must not reject.
    grid._reserved_for_nets[(0, y + 40, x + 40)] = frozenset({7})
    assert pathfinder._is_via_blocked(*LEGAL, 1) is False
    grid._reserved_for_nets[(3, y, x)] = frozenset({7})
    assert pathfinder._is_via_blocked(*LEGAL, 1) is True


@pytest.mark.parametrize("kind", ["static", "pad", "obstacle", "reserved"])
def test_hard_blockage_without_an_occupancy_bump_still_rejects(kind):
    """No caching tokened on ``state_version`` was added by #5720.

    All four flags are written without bumping ``grid.occupancy_generation``,
    so ``halo.state_version`` (and therefore the #5696 memo token) cannot see
    them.  Every one must still be observed on the very next call.
    """
    grid, pathfinder = _context()
    x, y = LEGAL
    # Populate the #5696 memo with the permissive verdict first.
    assert pathfinder._is_via_blocked(x, y, 1) is False
    generation = grid.occupancy_generation
    version = grid._route_halo.state_version

    if kind == "static":
        grid._static_blocked[LAYER, y, x] = True
    elif kind == "pad":
        grid._pad_blocked[LAYER, y, x] = True
    elif kind == "obstacle":
        grid._is_obstacle[LAYER, y, x] = True
    else:
        grid._reserved_for_nets[(LAYER, y, x)] = frozenset({7})

    assert grid.occupancy_generation == generation
    assert grid._route_halo.state_version == version
    assert pathfinder._is_via_blocked(x, y, 1) is True
    assert _reference_is_via_blocked(pathfinder, x, y, 1) is True


def test_via_in_pad_is_rejected_on_every_layer():
    """The drill sweep's layer-wise OR (#3508), now one plane slice."""
    grid, pathfinder = _context()
    x, y = LEGAL
    for layer in range(grid.num_layers):
        _, fresh = _context()
        fresh.grid._pad_blocked[layer, y, x] = True
        assert fresh._is_via_blocked(x, y, 1) is True
        assert _reference_is_via_blocked(fresh, x, y, 1) is True


#: Open field, far from net 2's halo: the via envelope is entirely unblocked
#: there, so the via-in-pad test is the ONLY thing that can reject.
_OPEN_FIELD = (20, 20)


def test_pad_metal_inside_the_envelope_but_outside_the_drill_still_rejects():
    """``window_known``'s ``_pad_blocked`` conjunct, with no drill sweep to hide behind.

    Needs ``drill_cells < via_extra_cells`` (the narrow-drill rules): on the
    default rules the two footprints coincide, so the via-in-pad test rejects
    such a cell first and the guard's own pad conjunct is never decisive.
    """
    grid, pathfinder = _context(via_drill=_NARROW_DRILL)
    drill_cells = max(0, int(math.ceil((pathfinder.rules.via_drill / 2) / grid.resolution)))
    extra = pathfinder._via_extra_cells
    assert drill_cells < extra
    gx, gy = LEGAL
    assert pathfinder._is_via_blocked(gx, gy, 1) is False

    ring = [
        (gx + dx, gy + dy)
        for dy in range(-extra, extra + 1)
        for dx in range(-extra, extra + 1)
        if max(abs(dx), abs(dy)) > drill_cells
        and pathfinder._is_cell_blocked(gx + dx, gy + dy, 0, 1)
    ]
    assert ring, "fixture has no blocked envelope cell outside the drill footprint"
    x, y = ring[0]
    grid._pad_blocked[0, y, x] = True
    assert grid._route_halo.cell_known(x, y, 0) is False
    assert pathfinder._is_via_blocked(gx, gy, 1) is True
    assert _reference_is_via_blocked(pathfinder, gx, gy, 1) is True


@pytest.mark.parametrize("own_net", [False, True])
def test_via_in_pad_is_the_only_rejecting_path_in_open_field(own_net):
    """#3508's second pass: no via-in-pad even on the via's OWN net's pad.

    In open field nothing enters the ``blocked_cells`` envelope, and an
    own-net pad cell is excluded from it by design (own-net passability), so a
    dropped drill sweep cannot be covered for by the ``cells_known`` guard.
    """
    grid, pathfinder = _context()
    x, y = _OPEN_FIELD
    assert pathfinder._is_via_blocked(x, y, 1) is False

    grid._pad_blocked[LAYER, y, x] = True
    if own_net:
        grid._blocked[LAYER, y, x] = True
        grid._net[LAYER, y, x] = 1
        assert pathfinder._is_cell_blocked(x, y, LAYER, 1) is False
    assert pathfinder._is_via_blocked(x, y, 1) is True
    assert _reference_is_via_blocked(pathfinder, x, y, 1) is True


def test_rules_mutation_is_still_observed():
    """The geometric half stays behind #5696's wide token, not a new one."""
    _, pathfinder = _context()
    assert pathfinder._is_via_blocked(*LEGAL, 1) is False
    pathfinder.rules.via_clearance = 0.6
    assert pathfinder._is_via_blocked(*LEGAL, 1) is True
    assert _reference_is_via_blocked(pathfinder, *LEGAL, 1) is True
    pathfinder.rules.via_clearance = 0.2
    assert pathfinder._is_via_blocked(*LEGAL, 1) is False
