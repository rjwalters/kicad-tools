"""Issue #3501: correction-pass rollback must not unmark usage it never marked.

Defect chain (found judging PR #3500 / Issue #3413):

1. ``Autorouter._post_route_clearance_correction`` commits pass-placed
   copper with ``grid.mark_route(route)`` ONLY -- deliberately not
   ``mark_route_usage()``, per the Issue #1694 width-aware-envelope note.
2. The Issue #3413 all-or-nothing rollback unwound that copper with BOTH
   ``unmark_route(route)`` AND ``unmark_route_usage(route)``.
3. Since usage was never incremented for pass-placed copper, the
   ``unmark_route_usage`` call decremented per-cell ``usage_count``
   contributed by OTHER nets sharing those cells (clamped at 0 per cell,
   so counts were stolen rather than underflowed).

The same asymmetry existed in the Issue #1783 retry path, which used
``rip_up_nets`` (rip both marks) on copper that only ever had
``mark_route``.

These tests pin the exact-tracking fix (#3478 transactional discipline):

* a pass-placed route is unwound with ``unmark_route`` only, via the
  ``pass_marked_route_ids`` ledger,
* aggregate usage counts are conserved across a rolled-back correction
  pass (mark / reroute / rollback leaves ``_usage_count`` bit-identical),
* a net that fails BEFORE any copper is marked produces no extra
  ``unmark_route_usage`` call and no tripwire warning,
* the ``_unwind_correction_pass_net`` tripwire warns when asked to
  unwind a route the pass never marked (and still never touches usage).
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.2,
    )


@pytest.fixture
def router(rules: DesignRules) -> Autorouter:
    return Autorouter(width=50.0, height=40.0, rules=rules)


def _route(net: int, x1: float, y1: float, x2: float, y2: float) -> Route:
    r = Route(net=net, net_name=f"NET{net}")
    r.segments.append(Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net))
    return r


def _seg_violation(net_a: int, net_b: int):
    """Minimal seg-seg violation record (duck-typed like io.validate_routes)."""
    return type("V", (), {"net": net_a, "obstacle_net": net_b, "obstacle_type": "segment"})()


def _usage_snapshot(router: Autorouter) -> np.ndarray:
    return np.array(router.grid._usage_count, copy=True)


class TestUsageConservationAcrossRollback:
    """mark / reroute / rollback must leave per-cell usage bit-identical."""

    def test_rollback_conserves_usage_counts(self, router: Autorouter, capsys) -> None:
        """A re-landed route crossing a bystander's usage cells must not
        steal the bystander's counts when the pass rolls back."""
        # Pre-pass committed state, as the negotiated loop leaves it:
        # both marks (copper + usage).
        orig1 = _route(1, 5.0, 10.0, 15.0, 10.0)
        orig2 = _route(2, 5.0, 20.0, 15.0, 20.0)
        for orig in (orig1, orig2):
            router.grid.mark_route(orig)
            router.grid.mark_route_usage(orig)
            router.routes.append(orig)

        # Bystander net 3: usage marked at y=15 (NOT part of the pass).
        bystander = _route(3, 5.0, 15.0, 15.0, 15.0)
        router.grid.mark_route_usage(bystander)

        # The pass re-lands net 1 on a vertical run crossing the
        # bystander's centerline at (10.0, 15.0).
        route_new = _route(1, 10.0, 5.0, 10.0, 25.0)

        gx, gy = router.grid.world_to_grid(10.0, 15.0)
        layer_idx = router.grid.layer_to_index(Layer.F_CU.value)
        assert int(router.grid._usage_count[layer_idx, gy, gx]) == 1

        net_routes = {1: [orig1], 2: [orig2]}
        snapshot = _usage_snapshot(router)

        call_count = [0]

        def mock_validate(router_obj):
            call_count[0] += 1
            # One violation to start the pass; clean afterwards so the
            # #1783 retry path stays out of the way.
            return [_seg_violation(1, 2)] if call_count[0] == 1 else []

        def mock_route_net(net, pf, per_net_timeout=None):
            # Net 1 re-lands; net 2 fails => all-or-nothing rollback.
            return [route_new] if net == 1 else []

        with (
            patch("kicad_tools.router.io.validate_routes", mock_validate),
            patch.object(router, "_route_net_negotiated", mock_route_net),
        ):
            router._post_route_clearance_correction(net_routes=net_routes, present_factor=0.5)

        # Rollback restored the pre-pass state verbatim.
        assert net_routes[1] == [orig1]
        assert net_routes[2] == [orig2]
        assert orig1 in router.routes
        assert orig2 in router.routes
        assert route_new not in router.routes

        # The bystander's usage at the crossing cell survived.
        assert int(router.grid._usage_count[layer_idx, gy, gx]) == 1, (
            "rollback stole the bystander net's usage_count at the "
            "crossing cell (unmark_route_usage on never-usage-marked copper)"
        )
        # Aggregate conservation: bit-identical usage before vs after.
        assert np.array_equal(_usage_snapshot(router), snapshot), (
            "usage counts were not conserved across a rolled-back correction pass"
        )

        # The exact-tracking ledger balanced: no tripwire warnings.
        out = capsys.readouterr().out
        assert "WARNING (Issue #3501)" not in out

    def test_failure_before_marking_no_unmark_no_warning(self, router: Autorouter, capsys) -> None:
        """A net whose reroute fails before ANY copper is marked must not
        produce extra unmark_route_usage calls nor tripwire warnings."""
        orig1 = _route(1, 5.0, 10.0, 15.0, 10.0)
        router.grid.mark_route(orig1)
        router.grid.mark_route_usage(orig1)
        router.routes.append(orig1)

        net_routes = {1: [orig1]}
        snapshot = _usage_snapshot(router)

        call_count = [0]

        def mock_validate(router_obj):
            call_count[0] += 1
            return [_seg_violation(1, 2)] if call_count[0] == 1 else []

        def mock_route_net(net, pf, per_net_timeout=None):
            return []  # fails before marking anything

        unmark_usage_calls: list[Route] = []
        real_unmark_usage = router.grid.unmark_route_usage

        def tracking_unmark_usage(route, net_cells=None):
            unmark_usage_calls.append(route)
            return real_unmark_usage(route, net_cells)

        with (
            patch("kicad_tools.router.io.validate_routes", mock_validate),
            patch.object(router, "_route_net_negotiated", mock_route_net),
            patch.object(router.grid, "unmark_route_usage", tracking_unmark_usage),
        ):
            router._post_route_clearance_correction(net_routes=net_routes, present_factor=0.5)

        # Exactly ONE legitimate unmark: rip_up_nets removing the
        # original (which HAD usage marked).  The rollback unwind adds
        # nothing because the pass never marked any copper.
        assert unmark_usage_calls == [orig1], (
            f"expected only rip_up_nets to unmark usage for the original "
            f"route; saw {len(unmark_usage_calls)} call(s)"
        )

        # Pre-pass state restored, usage conserved, no tripwire.
        assert net_routes[1] == [orig1]
        assert orig1 in router.routes
        assert np.array_equal(_usage_snapshot(router), snapshot)
        out = capsys.readouterr().out
        assert "WARNING (Issue #3501)" not in out


class TestUnwindHelperLedger:
    """Direct tests of Autorouter._unwind_correction_pass_net."""

    def test_ledgered_route_unwound_copper_only(self, router: Autorouter, capsys) -> None:
        """A pass-marked route is unmarked (copper) without touching usage."""
        bystander = _route(3, 5.0, 15.0, 15.0, 15.0)
        router.grid.mark_route_usage(bystander)

        route_new = _route(1, 10.0, 5.0, 10.0, 25.0)
        router.grid.mark_route(route_new)
        router.routes.append(route_new)
        ledger = {id(route_new)}
        net_routes = {1: [route_new]}
        snapshot = _usage_snapshot(router)

        router._unwind_correction_pass_net(1, net_routes, ledger)

        assert net_routes[1] == []
        assert route_new not in router.routes
        assert ledger == set(), "ledger must be consumed by the unwind"
        assert np.array_equal(_usage_snapshot(router), snapshot), (
            "unwind of pass-marked copper must never touch usage counts"
        )
        out = capsys.readouterr().out
        assert "WARNING (Issue #3501)" not in out

        # The copper itself WAS unmarked.
        gx, gy = router.grid.world_to_grid(10.0, 15.0)
        assert bool(router.grid._blocked[0, gy, gx]) is False

    def test_unledgered_route_trips_warning_and_preserves_usage(
        self, router: Autorouter, capsys
    ) -> None:
        """Unwinding a route the pass never marked warns loudly and still
        leaves usage counts untouched."""
        bystander = _route(3, 5.0, 15.0, 15.0, 15.0)
        router.grid.mark_route_usage(bystander)

        foreign = _route(1, 10.0, 5.0, 10.0, 25.0)
        router.grid.mark_route(foreign)
        router.routes.append(foreign)
        net_routes = {1: [foreign]}
        snapshot = _usage_snapshot(router)

        router._unwind_correction_pass_net(1, net_routes, set())

        out = capsys.readouterr().out
        assert "WARNING (Issue #3501)" in out, "unbalanced unwind must trip the #3501 warning"
        # Copper unmarked; usage untouched either way.
        assert net_routes[1] == []
        assert foreign not in router.routes
        assert np.array_equal(_usage_snapshot(router), snapshot)
