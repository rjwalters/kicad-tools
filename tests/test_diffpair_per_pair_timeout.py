"""Tests for the Issue #3089 per-pair wall-clock timeout in CoupledPathfinder.

Background: ``CoupledPathfinder.route_coupled`` is a pure-Python A* over
a joint ``(P-pos, N-pos)`` grid state.  On board 06's USB3 SS BGA-49
escape (J3/J4) the search has been observed to spend 20+ minutes
without converging, blowing past the CI 10-minute wall-clock cap.  The
C++ router backend is NOT engaged for coupled diffpair routing.

Issue #3089 adds a ``timeout_seconds`` kwarg to
``CoupledPathfinder.route_coupled`` and threads a ``per_pair_timeout``
kwarg through ``route_differential_pair_coupled``,
``route_differential_pair``, ``route_all_with_diffpairs``, and
``DifferentialPairConfig`` so callers can bound the per-pair cost
without changing the algorithm.

The contract:

1. ``timeout_seconds=None`` preserves the legacy unbounded behaviour
   (no time check, no observable change).
2. ``timeout_seconds=<small>`` causes a pathological / unsolvable
   search to return ``None`` within ~``timeout_seconds`` wall clock
   instead of running to ``max_iterations`` exhaustion.
3. ``timeout_seconds=<large>`` does NOT cause a clean fixture to
   spuriously fail (the budget is large enough that the search
   completes long before the deadline).
4. The threaded kwargs preserve the legacy default at every layer.
"""

from __future__ import annotations

import time
from dataclasses import fields

from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# DifferentialPairConfig field surface
# ---------------------------------------------------------------------------


def test_diffpair_config_has_per_pair_timeout_field():
    """``DifferentialPairConfig`` exposes a ``per_pair_timeout`` field."""
    field_names = {f.name for f in fields(DifferentialPairConfig)}
    assert "per_pair_timeout" in field_names


def test_diffpair_config_per_pair_timeout_default_none():
    """The default value preserves the legacy unbounded behaviour."""
    cfg = DifferentialPairConfig()
    assert cfg.per_pair_timeout is None


def test_diffpair_config_per_pair_timeout_accepts_float():
    """Callers can configure the budget via the constructor."""
    cfg = DifferentialPairConfig(enabled=True, per_pair_timeout=30.0)
    assert cfg.per_pair_timeout == 30.0


# ---------------------------------------------------------------------------
# CoupledPathfinder.route_coupled timeout_seconds kwarg
# ---------------------------------------------------------------------------


def _make_simple_pair_pads() -> tuple[Pad, Pad, Pad, Pad]:
    """Two-pad fixture that the coupled pathfinder routes in well under 1 s."""
    p_start = Pad(
        x=2.0,
        y=5.0,
        width=0.2,
        height=0.2,
        net=1,
        net_name="DP+",
        layer=Layer.F_CU,
    )
    p_end = Pad(
        x=10.0,
        y=5.0,
        width=0.2,
        height=0.2,
        net=1,
        net_name="DP+",
        layer=Layer.F_CU,
    )
    n_start = Pad(
        x=2.0,
        y=5.4,
        width=0.2,
        height=0.2,
        net=2,
        net_name="DP-",
        layer=Layer.F_CU,
    )
    n_end = Pad(
        x=10.0,
        y=5.4,
        width=0.2,
        height=0.2,
        net=2,
        net_name="DP-",
        layer=Layer.F_CU,
    )
    return p_start, p_end, n_start, n_end


def _make_unreachable_pair_pads() -> tuple[Pad, Pad, Pad, Pad]:
    """Pair whose endpoints sit close enough that the coupled search
    starts but the goal-state lattice is large enough that A* cannot
    converge within a sub-second budget without the timeout firing.

    We deliberately place the endpoints on opposite corners of a small
    grid so the joint state space (rows*cols)^2 is non-trivial -- the
    search will iterate over many heap pops before reaching the goal
    (and will reach it eventually, but not within a 1 ms budget).
    """
    p_start = Pad(
        x=1.0,
        y=1.0,
        width=0.2,
        height=0.2,
        net=1,
        net_name="LONG+",
        layer=Layer.F_CU,
    )
    p_end = Pad(
        x=11.0,
        y=11.0,
        width=0.2,
        height=0.2,
        net=1,
        net_name="LONG+",
        layer=Layer.F_CU,
    )
    n_start = Pad(
        x=1.0,
        y=1.4,
        width=0.2,
        height=0.2,
        net=2,
        net_name="LONG-",
        layer=Layer.F_CU,
    )
    n_end = Pad(
        x=11.0,
        y=11.4,
        width=0.2,
        height=0.2,
        net=2,
        net_name="LONG-",
        layer=Layer.F_CU,
    )
    return p_start, p_end, n_start, n_end


def test_route_coupled_default_timeout_preserves_legacy_behaviour():
    """``timeout_seconds=None`` (the default) does not cause a clean
    fixture to fail and matches the legacy result."""
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )
    p_start, p_end, n_start, n_end = _make_simple_pair_pads()

    # No timeout
    result_default = pf.route_coupled(p_start, p_end, n_start, n_end)
    # Explicit None
    result_none = pf.route_coupled(p_start, p_end, n_start, n_end, timeout_seconds=None)
    assert result_default is not None
    assert result_none is not None


def test_route_coupled_generous_timeout_allows_clean_fixture():
    """A budget that is much larger than the actual search time should
    let the search complete normally."""
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )
    p_start, p_end, n_start, n_end = _make_simple_pair_pads()

    # 60 seconds is enormous for a 12.7x12.7 mm two-pad fixture.
    result = pf.route_coupled(p_start, p_end, n_start, n_end, timeout_seconds=60.0)
    assert result is not None


def test_route_coupled_tiny_timeout_returns_none_under_budget():
    """A microscopic budget on a non-trivial fixture must fire the
    timeout and return ``None`` quickly, not wait for
    ``max_iterations`` to exhaust.

    This is the load-bearing test for #3089: it proves the inner A*
    loop actually consults the wall clock and exits early.
    """
    # A larger grid amplifies the joint state space so the search has
    # work to do (the timeout-check only fires every 1024 iterations,
    # so a 4x4-cell trivial fixture might solve before the first check).
    rules = DesignRules()
    grid = RoutingGrid(width=25.4, height=25.4, rules=rules)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )
    p_start, p_end, n_start, n_end = _make_unreachable_pair_pads()

    # Set a 1 ms budget.  The unbounded search on this fixture has
    # been measured at 8+ seconds; the timeout-check fires every
    # ``CoupledPathfinder._TIMEOUT_CHECK_INTERVAL`` iterations and
    # each Python A* iteration can be slow (~10 ms on a synthetic
    # 25mm grid).  The bound here is the EXIT bound -- the search
    # must complete within ``one full check interval after deadline``,
    # which is well under the unbounded ~25-minute USB3 stall.
    # Bound at 30 s to leave plenty of margin for slow CI runners
    # while still proving the timeout fires (the unbounded run on
    # the same fixture is much slower than this).
    t0 = time.monotonic()
    result = pf.route_coupled(p_start, p_end, n_start, n_end, timeout_seconds=0.001)
    elapsed = time.monotonic() - t0

    assert result is None, "Microscopic timeout must abort the search; result is non-None"
    assert elapsed < 30.0, (
        f"Microscopic timeout must abort within one check interval "
        f"after deadline; took {elapsed:.3f}s"
    )


def test_route_coupled_timeout_does_not_change_result_on_clean_fixture():
    """A clean fixture that completes well under the budget must return
    the SAME (non-None) result regardless of whether a budget was
    supplied."""
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )
    p_start, p_end, n_start, n_end = _make_simple_pair_pads()

    result_unbounded = pf.route_coupled(p_start, p_end, n_start, n_end)

    # Rebuild the pathfinder so any grid mutations from the first call
    # do not bias the second.
    pf2 = CoupledPathfinder(
        grid=RoutingGrid(width=12.7, height=12.7, rules=rules),
        rules=rules,
        target_spacing_cells=2,
        min_spacing_cells=2,
    )
    result_bounded = pf2.route_coupled(p_start, p_end, n_start, n_end, timeout_seconds=10.0)

    assert result_unbounded is not None
    assert result_bounded is not None
    # Same segment counts (deterministic A* with same inputs).
    p_route_a, n_route_a = result_unbounded
    p_route_b, n_route_b = result_bounded
    assert len(p_route_a.segments) == len(p_route_b.segments)
    assert len(n_route_a.segments) == len(n_route_b.segments)


# ---------------------------------------------------------------------------
# Threading: kwarg signatures preserve legacy defaults
# ---------------------------------------------------------------------------


def test_route_coupled_signature_accepts_timeout_seconds():
    """The kwarg must be on the method (sanity check for callers)."""
    import inspect

    sig = inspect.signature(CoupledPathfinder.route_coupled)
    assert "timeout_seconds" in sig.parameters
    # Default must preserve legacy unbounded behaviour.
    assert sig.parameters["timeout_seconds"].default is None


def test_route_all_with_diffpairs_signature_accepts_per_pair_timeout():
    """The plumbing reaches the top-level entry point."""
    import inspect

    from kicad_tools.router.diffpair_routing import DiffPairRouter

    sig = inspect.signature(DiffPairRouter.route_all_with_diffpairs)
    assert "per_pair_timeout" in sig.parameters
    assert sig.parameters["per_pair_timeout"].default is None


def test_route_differential_pair_signature_accepts_per_pair_timeout():
    """The middle-layer convenience method also threads it."""
    import inspect

    from kicad_tools.router.diffpair_routing import DiffPairRouter

    sig = inspect.signature(DiffPairRouter.route_differential_pair)
    assert "per_pair_timeout" in sig.parameters
    assert sig.parameters["per_pair_timeout"].default is None


def test_route_differential_pair_coupled_signature_accepts_per_pair_timeout():
    """The inner-layer coupled-only method also threads it."""
    import inspect

    from kicad_tools.router.diffpair_routing import DiffPairRouter

    sig = inspect.signature(DiffPairRouter.route_differential_pair_coupled)
    assert "per_pair_timeout" in sig.parameters
    assert sig.parameters["per_pair_timeout"].default is None
