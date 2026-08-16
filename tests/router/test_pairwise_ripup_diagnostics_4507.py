"""Cross-domain (HV) rip-up diagnostics for the search-time pairwise kernels.

Issue #4507 (epic #4431 Phase 2).  Phase 2b (#4511) gave the C++ A* search hard
cross-domain blocking, but the refusal was *silent*: unlike the #2476
stored-via check, ``cross_domain_trace_blocked`` / ``cross_domain_via_blocked``
named no blocker, so a net that failed because a low-voltage neighbour crowded
its widened HV keepout came back as a bare ``FAILURE_NO_PATH``.  The negotiated
strategy has no rip-up target for that reason code, so it blanket-retried --
precisely the thrash Phase 2 exists to end.

This module pins the new diagnostic end to end:

* the kernels record ``(foreign net, refused candidate)`` -- and record
  *nothing* when the refusal never happens (dormant / same-domain / net 0) or
  when a rated footprint's attach zone (#4506) waives the widening;
* a drained search reports ``FAILURE_PAIRWISE_BLOCKED`` with the blocker in
  ``RouteResult.blocking_via_net``, while TIMEOUT / ITERATION_LIMIT (budget
  artifacts, per #2610) and the stored-via diagnostic keep precedence;
* ``NegotiatedRouter`` feeds the new reason into the existing targeted rip-up
  queue; and
* without a voltage map nothing above can fire (byte-identical dormancy).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairwise_clearance import (
    build_cpp_domain_matrix,
    build_pairwise_clearance_table,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

DRU = 0.2
TRACE_WIDTH = 0.2
RESOLUTION = 0.1

HV_NET = 1  # /AC_LINE  @ 150 V
LV_NET = 2  # /GND      @ 0 V
HV_TAP_NET = 3  # /AC_LINE_TAP -- same domain as HV_NET
NET_NAMES = {"/AC_LINE": HV_NET, "/GND": LV_NET, "/AC_LINE_TAP": HV_TAP_NET}
VOLTAGES = {"/AC_LINE": 150.0, "/GND": 0.0, "/AC_LINE_TAP": 150.0}

# IEC 60664-1, PD2, material group IIIa @ 150 V -> 1.6 mm.  At 0.1 mm
# resolution the scalar trace radius is 3 cells and the widened HV<->LV radius
# is ceil((0.1 + 1.6) / 0.1) = 17 cells, so a foreign cell 8 cells away sits in
# the annulus: inside the widening, outside the scalar disc.
SCALAR_RADIUS = 3
ANNULUS_DY = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cpp_grid(cols: int = 200, rows: int = 200):
    from kicad_tools.router import router_cpp

    return router_cpp.Grid3D(cols, rows, 2, RESOLUTION, 0.0, 0.0)


def _cpp_rules():
    from kicad_tools.router import router_cpp

    rules = router_cpp.DesignRules()
    rules.trace_width = TRACE_WIDTH
    rules.trace_clearance = DRU
    rules.via_diameter = 0.6
    rules.via_clearance = DRU
    rules.grid_resolution = RESOLUTION
    rules.cost_straight = 1.0
    return rules


def _pathfinder(grid):
    from kicad_tools.router import router_cpp

    pf = router_cpp.Pathfinder(grid, _cpp_rules(), True)
    pf.set_search_pair_widths(TRACE_WIDTH / 2.0, 0.3)
    return pf


def _install_domains(grid) -> None:
    domains = build_cpp_domain_matrix(build_pairwise_clearance_table(VOLTAGES, dru=DRU), NET_NAMES)
    assert domains is not None
    grid.set_pairwise_domains(domains.net_to_domain, domains.matrix)


def _cpp_zone(min_x, min_y, max_x, max_y, net_ids):
    from kicad_tools.router import router_cpp

    zone = router_cpp.AttachZone()
    zone.min_x, zone.min_y, zone.max_x, zone.max_y = min_x, min_y, max_x, max_y
    zone.net_ids = list(net_ids)
    return zone


# ---------------------------------------------------------------------------
# Kernel-level recording
# ---------------------------------------------------------------------------


@requires_cpp
def test_trace_kernel_records_the_foreign_blocker() -> None:
    """A cross-domain refusal names the LV net and the refused candidate."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 100 + ANNULUS_DY, 0, LV_NET, False, False)

    assert pf.pairwise_block_count == 0
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is True

    assert pf.pairwise_block_count == 1
    assert pf.last_pairwise_block_net == LV_NET
    # The #2476 convention reports the REFUSED CANDIDATE, not the blocker.
    assert pf.last_pairwise_block_x == pytest.approx(10.0)
    assert pf.last_pairwise_block_y == pytest.approx(10.0)


@requires_cpp
def test_via_kernel_records_the_foreign_blocker() -> None:
    """The via sibling records through the same channel."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    # 12 cells away: outside the scalar via disc, inside the widened radius
    # ceil((0.3 + 1.6) / 0.1) = 19 cells.
    grid.mark_blocked(100, 112, 1, LV_NET, False, False)

    assert pf.cross_domain_via_blocked(100, 100, HV_NET) is True
    assert pf.pairwise_block_count == 1
    assert pf.last_pairwise_block_net == LV_NET


@requires_cpp
@pytest.mark.parametrize("foreign_net", [HV_TAP_NET, 0])
def test_no_widening_records_nothing(foreign_net: int) -> None:
    """Same-domain copper and net 0 never widen, so they never record."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 100 + ANNULUS_DY, 0, foreign_net, False, False)

    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False
    assert pf.pairwise_block_count == 0
    assert pf.last_pairwise_block_net == 0


@requires_cpp
def test_dormant_kernel_records_nothing() -> None:
    """No installed matrix -> the kernel short-circuits before any recording."""
    grid = _cpp_grid()
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 100 + ANNULUS_DY, 0, LV_NET, False, False)

    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False
    assert pf.cross_domain_via_blocked(100, 100, HV_NET) is False
    assert pf.pairwise_block_count == 0


@requires_cpp
def test_attach_zone_waiver_records_nothing() -> None:
    """A waived (#4506) pair is not a blocker -- ripping it up would not help."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET])])
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 100 + ANNULUS_DY, 0, LV_NET, False, False)

    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False
    assert pf.pairwise_block_count == 0


@requires_cpp
def test_clear_resets_the_diagnostics() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 100 + ANNULUS_DY, 0, LV_NET, False, False)
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is True

    pf.clear_pairwise_block_diagnostics()
    assert pf.pairwise_block_count == 0
    assert pf.last_pairwise_block_net == 0
    assert pf.last_pairwise_block_x == pytest.approx(0.0)
    assert pf.last_pairwise_block_y == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Search-level failure reason
# ---------------------------------------------------------------------------


def _curtain_grid(with_pairwise: bool):
    """An LV curtain across the board with a 1.0 mm slot at mid-height.

    The slot is wide enough for the HV trace at the DRU floor (0.3 mm each
    side) and far too narrow for the 1.6 mm creepage requirement, so the SAME
    geometry routes when the matrix is dormant and drains when it is not.
    """
    grid = _cpp_grid(cols=120, rows=120)
    if with_pairwise:
        _install_domains(grid)
    for layer in (0, 1):
        for y in range(120):
            if 55 <= y <= 65:  # 1.0 mm slot centred on y = 6.0 mm
                continue
            grid.mark_blocked(60, y, layer, LV_NET, False, False)
    return grid


def _route_across_curtain(grid, **kwargs):
    pf = _pathfinder(grid)
    return pf.route(
        start_x=2.0,
        start_y=6.0,
        start_layer=0,
        end_x=10.0,
        end_y=6.0,
        end_layer=0,
        net=HV_NET,
        trace_radius_cells=SCALAR_RADIUS,
        via_radius_cells=SCALAR_RADIUS,
        **kwargs,
    )


@requires_cpp
def test_drained_search_names_the_cross_domain_blocker() -> None:
    """The headline: a pairwise-blocked drain is actionable, not a bare NO_PATH."""
    from kicad_tools.router import router_cpp

    # Baseline: the same slot is routable while the matrix is dormant, so the
    # failure below is caused by the pairwise widening and nothing else.
    open_result = _route_across_curtain(_curtain_grid(with_pairwise=False))
    assert open_result.success is True
    assert open_result.failure_reason == router_cpp.FAILURE_NONE

    blocked = _route_across_curtain(_curtain_grid(with_pairwise=True))
    assert blocked.success is False
    assert blocked.failure_reason == router_cpp.FAILURE_PAIRWISE_BLOCKED
    assert blocked.blocking_via_net == LV_NET
    # The reported location is a refused candidate on the board, not (0, 0).
    assert 0.0 < blocked.failure_x < 12.0


@requires_cpp
def test_no_matrix_drain_stays_no_path() -> None:
    """Backward compat: dormant pairwise -> the pre-#4507 reason code."""
    from kicad_tools.router import router_cpp

    grid = _cpp_grid(cols=120, rows=120)
    for layer in (0, 1):
        for y in range(120):
            grid.mark_blocked(60, y, layer, LV_NET, False, False)  # solid curtain

    result = _route_across_curtain(grid)
    assert result.success is False
    assert result.failure_reason == router_cpp.FAILURE_NO_PATH
    assert result.blocking_via_net == 0


@requires_cpp
def test_iteration_limit_keeps_precedence_over_the_pairwise_blocker() -> None:
    """A budget artifact is never relabelled as a geometric HV blocker.

    ``--max-search-iterations`` exhaustion means "we ran out of budget", which
    calls for a bigger cap (#2610), not for ripping up a neighbouring net.
    """
    from kicad_tools.router import router_cpp

    result = _route_across_curtain(_curtain_grid(with_pairwise=True), max_search_iterations=5)
    assert result.success is False
    assert result.failure_reason == router_cpp.FAILURE_ITERATION_LIMIT


# ---------------------------------------------------------------------------
# cpp_backend / negotiated plumbing
# ---------------------------------------------------------------------------


@requires_cpp
def test_failure_reason_constant_is_exposed_and_mirrored() -> None:
    from kicad_tools.router import router_cpp
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    assert int(router_cpp.FAILURE_PAIRWISE_BLOCKED) == 7
    assert int(router_cpp.FAILURE_PAIRWISE_BLOCKED) == NegotiatedRouter._FAILURE_PAIRWISE_BLOCKED
    # 6 belongs to the mirrored ``violation_type`` vocabulary (drill spacing).
    assert int(router_cpp.FAILURE_PAIRWISE_BLOCKED) != int(router_cpp.FAILURE_VIA_VIA_BLOCKED)


@requires_cpp
def test_backend_surfaces_the_diagnostic_to_the_negotiated_strategy() -> None:
    """``get_last_failure_info`` carries the blocker through ``CppPathfinder``."""
    from kicad_tools.router import router_cpp

    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_diameter=0.6,
        via_clearance=DRU,
        grid_resolution=RESOLUTION,
    )
    rules.pairwise_clearance = build_pairwise_clearance_table({"HV": 150.0, "LV": 0.0}, dru=DRU)
    grid = RoutingGrid(width=12.0, height=12.0, rules=rules, layer_stack=LayerStack.two_layer())
    start = Pad(x=2.0, y=6.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    end = Pad(x=10.0, y=6.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    # LV curtain with a 1.0 mm slot at y = 6.0, on both layers.
    for layer in (Layer.F_CU, Layer.B_CU):
        grid.add_pad(Pad(x=6.0, y=3.0, width=0.6, height=5.0, net=2, net_name="LV", layer=layer))
        grid.add_pad(Pad(x=6.0, y=9.0, width=0.6, height=5.0, net=2, net_name="LV", layer=layer))
    nc = NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU)
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
    pf.set_net_name_to_id({"HV": 1, "LV": 2})
    # The 10-100x-slower pure-Python fallback is HV-aware too (#4791) and would
    # only re-derive the same verdict; the diagnostic is captured before it.
    pf._try_python_fallback = lambda *args, **kwargs: None

    assert pf.route(start, end, net_class=nc) is None
    info = pf.get_last_failure_info()
    assert info is not None
    assert info["failure_reason"] == int(router_cpp.FAILURE_PAIRWISE_BLOCKED)
    assert info["blocking_via_net"] == 2
    # The structured diagnostic carries the blocker's board net NAME too, so a
    # caller reporting the failure does not have to invert the id map itself.
    assert info["blocking_net_name"] == "LV"

    # And the human-readable form names the blocker for the router log.
    class _Result:
        failure_reason = int(router_cpp.FAILURE_PAIRWISE_BLOCKED)
        blocking_via_net = 2

    desc = pf._describe_cpp_failure(_Result())
    assert "pairwise" in desc
    assert "blocking net LV" in desc


def _bare_pathfinder() -> CppPathfinder:
    """A minimal ``CppPathfinder`` for describe-only diagnostics assertions."""
    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_diameter=0.6,
        via_clearance=DRU,
        grid_resolution=RESOLUTION,
    )
    grid = RoutingGrid(width=4.0, height=4.0, rules=rules, layer_stack=LayerStack.two_layer())
    return CppPathfinder(CppGrid.from_routing_grid(grid), rules)


@requires_cpp
def test_blocker_diagnostic_names_the_net_not_its_id() -> None:
    """The blocker is reported by NAME once the reverse map is threaded (#4507).

    The T4 softstart rev-C proof run observed ten nets drain the C++ open set
    on cross-domain refusals and report only ``blocking net id 51`` -- a
    number the operator has to look up before they can act on it.  Both
    blocker-naming reasons (the #2476 stored-via one and the #4507 pairwise
    one) resolve the id through ``set_net_name_to_id``.
    """
    from kicad_tools.router import router_cpp

    pf = _bare_pathfinder()
    pf.set_net_name_to_id(dict(NET_NAMES))

    class _Pairwise:
        failure_reason = int(router_cpp.FAILURE_PAIRWISE_BLOCKED)
        blocking_via_net = LV_NET

    class _ViaVia:
        failure_reason = int(router_cpp.FAILURE_VIA_VIA_BLOCKED)
        blocking_via_net = LV_NET

    for result in (_Pairwise(), _ViaVia()):
        desc = pf._describe_cpp_failure(result)
        assert "(blocking net /GND)" in desc
        assert "net id" not in desc


@requires_cpp
def test_blocker_diagnostic_falls_back_to_the_id_when_unresolvable() -> None:
    """An un-threaded (or incomplete) map keeps the pre-#4507 bare-id wording."""
    from kicad_tools.router import router_cpp

    class _Result:
        failure_reason = int(router_cpp.FAILURE_PAIRWISE_BLOCKED)
        blocking_via_net = 51

    # No reverse map at all -- e.g. a bare pathfinder outside the Autorouter.
    assert "(blocking net id 51)" in _bare_pathfinder()._describe_cpp_failure(_Result())

    # Map present but silent about this id (a net the Autorouter never
    # registered): still the id, never a misleading name.
    pf = _bare_pathfinder()
    pf.set_net_name_to_id(dict(NET_NAMES))
    assert "(blocking net id 51)" in pf._describe_cpp_failure(_Result())


@requires_cpp
def test_blocker_name_cache_follows_a_remapped_net_table() -> None:
    """Re-threading the map invalidates the memoised id -> name inverse."""
    from kicad_tools.router import router_cpp

    class _Result:
        failure_reason = int(router_cpp.FAILURE_PAIRWISE_BLOCKED)
        blocking_via_net = LV_NET

    pf = _bare_pathfinder()
    pf.set_net_name_to_id(dict(NET_NAMES))
    assert "(blocking net /GND)" in pf._describe_cpp_failure(_Result())

    pf.set_net_name_to_id({"/AC_LINE": HV_NET, "/PGND": LV_NET})
    assert "(blocking net /PGND)" in pf._describe_cpp_failure(_Result())


def test_negotiated_records_the_pairwise_blocker_for_ripup() -> None:
    """The new reason feeds the existing #2476 targeted-ripup queue."""
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_clearance=DRU,
        grid_resolution=RESOLUTION,
    )
    grid = RoutingGrid(width=10.0, height=10.0, rules=rules, layer_stack=LayerStack.two_layer())

    class FakeRouter:
        info: dict | None = None

        def get_last_failure_info(self):
            return self.info

    fake = FakeRouter()
    neg = NegotiatedRouter(grid, fake, rules, net_class_map={})

    fake.info = {
        "failure_reason": NegotiatedRouter._FAILURE_PAIRWISE_BLOCKED,
        "blocking_via_net": LV_NET,
        "failure_x": 1.0,
        "failure_y": 2.0,
    }
    neg._record_via_blocked_failure(failed_net=HV_NET)
    assert neg.get_and_clear_via_blocking_nets() == {(HV_NET, LV_NET)}

    # A plain NO_PATH still yields nothing to rip up.
    fake.info = {
        "failure_reason": NegotiatedRouter._FAILURE_NO_PATH,
        "blocking_via_net": LV_NET,
    }
    neg._record_via_blocked_failure(failed_net=HV_NET)
    assert neg.get_and_clear_via_blocking_nets() == set()


def test_negotiated_labels_the_new_reason() -> None:
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    label = NegotiatedRouter.describe_failure_reason(
        {"failure_reason": NegotiatedRouter._FAILURE_PAIRWISE_BLOCKED}
    )
    assert label == "pairwise_blocked"
