"""Equivalence pins for the coupled route-halo via memo (Issue #5696).

Background
----------
PR #5695 extended the dynamic route-halo physical refinement to the coupled
differential-pair search.  On the **pure-Python coupled fallback** (reached when
``KCT_COUPLED_CPP=0``, when ``CppCoupledPathfinder`` construction raises, and
for the v1-deferred ``allow_swap_via`` / ``manhattan_sum`` cases) the new check
cost ~14x on the via predicate for every refinable cell probed, because
``RouteHaloRefiner.via_clear`` re-ran the shapely distance sweep inside
``RouteHaloGeometry.clear`` on every call.

Issue #5696 memoises the *geometric half* of that predicate.  The memo is keyed
on ``(gx, gy, net)`` and tokened on ``RouteHaloRefiner._memo_token``, which is
deliberately **wider** than the token the per-net sibling
``Router._via_halo_clear_cached`` uses (``test_router_via_halo_memo_5617.py``):

1. The sibling tokens on ``halo.state_version`` alone.  That is sound there
   only because ``Router.clear_via_cache()`` drops the memo at the start of
   every route / A* search, which pins the rule configuration for the memo's
   lifetime.
2. ``RouteHaloRefiner`` has **no** such per-route reset hook, and its ``rules``
   object is mutated in place between probes of the same cell -- by callers and
   by ``tests/test_coupled_route_halo_clearance.py``'s HV-widening controls.  A
   ``state_version``-only token would therefore serve the pre-mutation verdict,
   and in the "was blocked, now cached clear" direction that is a silent DRC
   violation with no error signal.
3. The ``cells_known`` guard stays **outside** the memo.  It reads the grid's
   occupancy planes directly, several of which (``_static_blocked``,
   ``_pad_blocked``, ``_is_obstacle``, ``_reserved_for_nets``) can be written
   without bumping ``grid.occupancy_generation`` -- so a memo that swallowed it
   would hide a hard constraint.

Point 2 is the case ``test_router_via_halo_memo_5617.py`` does not cover and is
the whole point of this module; ``test_memo_drops_on_a_rules_mutation`` and
``test_memo_drops_on_a_pairwise_table_mutation`` are its witnesses.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable
from kicad_tools.router.primitives import Layer, Route, Via
from kicad_tools.router.route_halo_geometry import RouteHaloRefiner
from kicad_tools.router.rules import DesignRules, NetClassRouting

#: Mirrors ``tests/test_coupled_route_halo_clearance.py`` -- a cell covered by
#: net 2's conservative via halo whose real copper is far enough away that a
#: net-1 via there is legal.
LEGAL = (55, 56)
LAYER = 2

#: A window straddling the stored via, so the sweeps below see BOTH verdicts.
_SWEEP = [(gx, gy) for gx in range(54, 68) for gy in range(54, 68)]


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )


def _context(
    *, armed: bool = True, net_class_map: dict[str, NetClassRouting] | None = None
) -> tuple[RoutingGrid, CoupledPathfinder]:
    rules = _rules()
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


def _uncached(refiner: RouteHaloRefiner, cells, layer: int, gx: int, gy: int, net: int) -> bool:
    """The exact pre-memo body of ``via_clear``.

    Written out here rather than reusing the shipped method so a regression
    that quietly changed the composition -- e.g. folding ``cells_known`` into
    the memoised half -- cannot make this module agree with itself.
    """
    halo = refiner._halo
    if halo is None or not refiner.armed:
        return False
    if not refiner.cells_known(cells, layer):
        return False
    return refiner._via_geometry_clear_uncached(halo, gx, gy, net)


# ---------------------------------------------------------------------------
# The window is meaningful
# ---------------------------------------------------------------------------


def test_probe_window_is_not_degenerate():
    """Guard: the sweeps below are only meaningful if they see both verdicts."""
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    halo = grid._route_halo
    geometry = {refiner._via_geometry_clear_uncached(halo, gx, gy, 1) for gx, gy in _SWEEP}
    assert geometry == {True, False}
    # ...and the ``cells_known`` guard is exercised in both directions too, so
    # the parity sweep covers the composed predicate, not just its tail.
    known = {refiner.cells_known([(gx, gy)], LAYER) for gx, gy in _SWEEP}
    assert known == {True, False}


# ---------------------------------------------------------------------------
# (a) memoised verdict == uncached verdict
# ---------------------------------------------------------------------------


def test_memo_matches_uncached_verdict_everywhere():
    """Every memoised verdict equals the freshly-computed one."""
    _, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    for gx, gy in _SWEEP:
        cells = [(gx, gy)]
        expected = _uncached(refiner, cells, LAYER, gx, gy, 1)
        # First call populates, second is served from the memo; both must agree
        # with the uncached computation.
        assert refiner.via_clear(cells, LAYER, gx, gy, 1) is expected
        assert refiner.via_clear(cells, LAYER, gx, gy, 1) is expected


def test_memo_is_layer_independent_and_computes_once():
    """The geometric half ignores ``layer`` -- so the key may too.

    This is the property that lets ``CoupledPathfinder._is_via_blocked`` hoist
    one ``via_clear`` verdict across its per-layer loop, and the reason the key
    is ``(gx, gy, net)`` rather than ``(gx, gy, net, layer)``.
    """
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    layers = range(grid.num_layers)

    # Property: identical verdicts across every layer that clears the
    # ``cells_known`` guard, computed WITHOUT the memo.
    sampled = 0
    for gx, gy in _SWEEP:
        answers = set()
        for layer in layers:
            cells = [(gx, gy)]
            if not refiner.cells_known(cells, layer):
                continue
            sampled += 1
            answers.add(_uncached(refiner, cells, layer, gx, gy, 1))
        assert len(answers) <= 1, f"via verdict became layer-dependent at ({gx}, {gy})"
    assert sampled > len(_SWEEP), "every probe must be sampled on more than one layer"

    calls: list[tuple[int, int, int]] = []
    original = refiner._via_geometry_clear_uncached

    def counting(halo_arg, gx, gy, net):
        calls.append((gx, gy, net))
        return original(halo_arg, gx, gy, net)

    refiner._via_geometry_clear_uncached = counting  # type: ignore[method-assign]
    for layer in layers:
        refiner.via_clear([LEGAL], layer, *LEGAL, 1)
    assert len(calls) == 1


def test_is_via_blocked_verdicts_unchanged_by_the_memo():
    """End-to-end: the coupled predicate answers the same with and without it."""
    _, reference_pathfinder = _context()
    reference_refiner = reference_pathfinder._halo_refiner

    def uncached_geometry(halo, gx, gy, net):
        return reference_refiner._via_geometry_clear_uncached(halo, gx, gy, net)

    # Bypass the memo entirely on the reference instance.
    reference_refiner._via_geometry_clear = uncached_geometry  # type: ignore[method-assign]
    reference = {(gx, gy): reference_pathfinder._is_via_blocked(gx, gy, 1) for gx, gy in _SWEEP}
    assert set(reference.values()) == {True, False}, "sweep must see both verdicts"
    assert not reference_refiner._via_memo, "reference instance must not have memoised"

    _, memoised = _context()
    for gx, gy in _SWEEP:
        assert memoised._is_via_blocked(gx, gy, 1) is reference[(gx, gy)]
    assert memoised._halo_refiner._via_memo, "memoised instance must have memoised"


# ---------------------------------------------------------------------------
# (b) the memo drops on a halo geometry change
# ---------------------------------------------------------------------------


def test_memo_drops_on_halo_geometry_change():
    """A foreign via landing under the probe flips the verdict (#5410's ripup)."""
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner

    before = refiner.via_clear([LEGAL], LAYER, *LEGAL, 1)
    assert before is True

    x, y = grid.grid_to_world(59, 58)
    overlapping = Route(net=3, net_name="N3")
    overlapping.vias.append(Via(x, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 3, "N3"))
    grid.mark_route(overlapping)

    after = _uncached(refiner, [LEGAL], LAYER, *LEGAL, 1)
    assert after is not before, "fixture no longer exercises an invalidation"
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is after

    # ...and back again on unmark, so BOTH transitions are pinned.
    grid.unmark_route(overlapping)
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is before


def test_memo_drops_on_a_record_without_an_occupancy_bump():
    """``record()`` alone bumps ``state_version``; the token must follow it."""
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    halo = grid._route_halo

    refiner.via_clear([LEGAL], LAYER, *LEGAL, 1)
    assert refiner._via_memo
    token_before = refiner._memo_token(halo)

    halo.record((0, 1, 0, 1, 1, 2, 2, 0.2), 3, True)
    assert refiner._memo_token(halo) != token_before

    refiner.via_clear([LEGAL], LAYER, *LEGAL, 1)
    assert refiner._via_memo_token == refiner._memo_token(halo)


# ---------------------------------------------------------------------------
# (c) the memo drops on a ``rules`` mutation -- the case #5617 does not cover
# ---------------------------------------------------------------------------


def test_memo_drops_on_a_rules_mutation():
    """``rules.via_clearance`` changes between two probes of the SAME cell.

    No ``record()``, no occupancy change: ``halo.state_version`` is identical
    across the mutation, so a ``state_version``-only token would serve the
    pre-mutation ``True`` and silently admit a via that now violates clearance.
    """
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    halo = grid._route_halo

    version_before = halo.state_version
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is True

    pathfinder.rules.via_clearance = 0.3
    assert halo.state_version == version_before, "fixture must not change the geometry"

    assert _uncached(refiner, [LEGAL], LAYER, *LEGAL, 1) is False
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is False

    # And back -- an invalidation that only ever tightens would still be a bug.
    pathfinder.rules.via_clearance = 0.2
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is True


def test_memo_drops_on_a_pairwise_table_mutation():
    """The HV-widening control of ``test_coupled_route_halo_clearance.py:211``.

    That test probes ``LEGAL`` twice through ``CoupledPathfinder._is_via_blocked``
    across a ``rules.pairwise_clearance`` reassignment.  It is the regression
    witness for this memo and must keep passing unmodified; this is the same
    transition asserted directly on the refiner, in both directions.
    """
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    halo = grid._route_halo

    version_before = halo.state_version
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is True
    assert not pathfinder._is_via_blocked(*LEGAL, 1)

    pathfinder.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=0.15, net_voltages={"N1": 0, "N2": 300}, required_by_pair={("N1", "N2"): 0.4}
    )
    assert halo.state_version == version_before, "fixture must not change the geometry"

    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is False
    assert pathfinder._is_via_blocked(*LEGAL, 1)

    pathfinder.rules.pairwise_clearance = None
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is True


def test_memo_drops_on_a_net_name_map_or_net_class_change():
    """``set_net_name_to_id`` and the net-class map are token members too.

    The candidate ``Via``'s diameter comes from ``nc.via_size`` and every
    pairwise lookup is by net NAME, so neither is pinned by the ``(gx, gy,
    net)`` key.
    """
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    halo = grid._route_halo
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is True

    # Re-arming with a different map must not be served from the memo.
    token_before = refiner._memo_token(halo)
    refiner.set_net_name_to_id({"N1": 1, "N2": 2, "N3": 3})
    assert refiner._memo_token(halo) != token_before

    # A fat via class blocks the same cell the default 0.6 mm via cleared --
    # ``nc.via_size`` is read by ``via_clear`` and is not in the key.
    token_before = refiner._memo_token(halo)
    refiner.net_class_map = {
        "N1": NetClassRouting(name="FAT", trace_width=0.2, clearance=0.15, via_size=1.2)
    }
    assert refiner._memo_token(halo) != token_before
    assert _uncached(refiner, [LEGAL], LAYER, *LEGAL, 1) is False
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is False


# ---------------------------------------------------------------------------
# The guard the memo must never swallow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["static", "pad", "obstacle", "reserved"])
def test_a_memo_hit_still_honours_hard_blockage(kind):
    """Hard flags are written WITHOUT bumping ``occupancy_generation``.

    ``cells_known`` therefore stays outside the memo: the token cannot see
    these writes, and a memo that had swallowed the guard would keep answering
    "clear" over pad metal, a static halo, a keepout or a reservation.
    """
    grid, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    x, y = LEGAL

    # Populate the memo with the permissive verdict FIRST.
    assert refiner.via_clear([LEGAL], LAYER, x, y, 1) is True
    assert refiner._via_memo

    if kind == "static":
        grid._static_blocked[LAYER, y, x] = True
    elif kind == "pad":
        grid._pad_blocked[LAYER, y, x] = True
    elif kind == "obstacle":
        grid._is_obstacle[LAYER, y, x] = True
    else:
        grid._reserved_for_nets[(LAYER, y, x)] = frozenset({7})

    assert refiner.via_clear([LEGAL], LAYER, x, y, 1) is False
    assert pathfinder._is_via_blocked(*LEGAL, 1)


def test_unarmed_refiner_fails_closed_and_does_not_populate_the_memo():
    """Dormancy is decided before the memo is ever consulted (#5410 B2)."""
    _, pathfinder = _context(armed=False)
    refiner = pathfinder._halo_refiner
    assert not refiner.armed
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is False
    assert not refiner._via_memo
    assert refiner._via_memo_token is None

    pathfinder.set_net_name_to_id({"N1": 1, "N2": 2})
    assert refiner.via_clear([LEGAL], LAYER, *LEGAL, 1) is True


# ---------------------------------------------------------------------------
# Boundedness
# ---------------------------------------------------------------------------


def test_memo_cap_is_a_memory_backstop_only(monkeypatch):
    """Hitting the cap clears the memo; verdicts are unaffected."""
    _, pathfinder = _context()
    refiner = pathfinder._halo_refiner
    monkeypatch.setattr(RouteHaloRefiner, "_VIA_MEMO_MAX", 4, raising=True)

    inserted = 0
    for gx, gy in _SWEEP:
        cells = [(gx, gy)]
        expected = _uncached(refiner, cells, LAYER, gx, gy, 1)
        assert refiner.via_clear(cells, LAYER, gx, gy, 1) is expected
        if refiner.cells_known(cells, LAYER):
            inserted += 1
        assert len(refiner._via_memo) <= 4, f"cap breached after {inserted} inserts"
    assert inserted > 4, "sweep must actually push past the cap"
