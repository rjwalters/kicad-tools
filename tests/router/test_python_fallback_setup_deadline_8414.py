"""The pure-Python search charges its pre-search mask setup to its budget (#8414).

Profiled on a 4-layer mains board, the post-negotiation sweep's
``route_net(<wide power return net>, per_net_timeout=10)`` took 128 s. 127 s of
that was the pure-Python fallback building HV pairwise masks *before*
``Router._route_impl`` started its deadline clock. The search itself took
0.04 s, so ``last_route_deadline_hit`` stayed ``False`` and no caller saw a
deadline stop.

The pairwise masks are built only after ``Autorouter._prepare_routing`` has
pushed the net-name map into the pathfinder. Called without it, the HV state
stays dormant and the bug does not reproduce (10.9 s in the profile). So these
tests go through ``_prepare_routing`` and assert the HV state is armed first.
The dilation is stalled with a sleep so that a small fixture reproduces the
board's setup cost without a 15.9 M-cell grid.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules

HV_NAME = "HV"
LV_NAMES = [f"LV{i}" for i in range(10)]  # ten foreign domains to widen against
BUDGET_S = 10.0
STALL_S = 1.5  # per dilation; 10 domains + 1 band = 16.5 s of setup unfixed


def _hv_autorouter() -> Autorouter:
    voltages = {HV_NAME: 400.0, **dict.fromkeys(LV_NAMES, 0.0)}
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    rules.pairwise_clearance = build_pairwise_clearance_table(voltages, dru=0.2)
    router = Autorouter(width=30.0, height=20.0, rules=rules, force_python=True)

    def pad(num: int, x: float, y: float, net: int, name: str) -> dict:
        return {
            "number": str(num),
            "x": x,
            "y": y,
            "width": 0.6,
            "height": 0.6,
            "net": net,
            "net_name": name,
        }

    router.add_component("J1", [pad(1, 3.0, 10.0, 1, HV_NAME)])
    router.add_component("J2", [pad(1, 27.0, 10.0, 1, HV_NAME)])
    # LV copper along the top edge: far enough that the widened HV route along
    # y=10 stays legal, close enough to the border to exercise zero padding.
    router.add_component(
        "U1",
        [pad(i + 1, 3.0 + 2.5 * i, 1.0, i + 2, name) for i, name in enumerate(LV_NAMES)],
    )
    router._prepare_routing()
    return router


def _armed_search_router(router: Autorouter) -> Router:
    search = router.router
    assert isinstance(search, Router)
    table = router.rules.pairwise_clearance
    state = search._pairwise_search_state(table)
    assert state is not None, "_prepare_routing must arm the HV pairwise rules"
    hv_dom = state.net_to_domain[1]
    widening = [d for d, req in enumerate(state.matrix[hv_dom]) if d != hv_dom and req > 0.0]
    assert len(widening) == len(LV_NAMES)
    return search


def _stall_dilation(search: Router, calls: list[int]):
    inner = search._dilate_zero_padded

    def stalled(mask: np.ndarray, radius: int) -> np.ndarray:
        calls.append(radius)
        time.sleep(STALL_S)
        return inner(mask, radius)

    search._dilate_zero_padded = stalled  # type: ignore[method-assign]
    return inner


def test_fallback_route_net_returns_within_its_budget() -> None:
    """Acceptance: a 10 s fallback call returns within 1.2x of its budget."""
    router = _hv_autorouter()
    search = _armed_search_router(router)
    calls: list[int] = []
    inner = _stall_dilation(search, calls)

    t0 = time.monotonic()
    routes = router.route_net(1, per_net_timeout=BUDGET_S)
    elapsed = time.monotonic() - t0

    assert elapsed <= 1.2 * BUDGET_S, f"route_net took {elapsed:.1f}s on a {BUDGET_S}s budget"
    assert routes == []
    # The search stopped on the wall clock in setup, not by draining.
    assert search.last_route_deadline_hit is True
    assert search.last_route_exhausted is False
    # The setup was cut short: not every domain was dilated.
    assert 0 < len(calls) < len(LV_NAMES) + 1

    # Only complete masks were cached: every entry matches a fresh dilation.
    cache = search._pairwise_extra_cache
    assert 0 < len(cache) < len(LV_NAMES)
    state = search._pairwise_search_state(router.rules.pairwise_clearance)
    assert state is not None
    for (foreign_dom, pair_r), cached in cache.items():
        ids = [n for n, d in enumerate(state.net_to_domain) if d == foreign_dom]
        foreign = search.grid._blocked & np.isin(search.grid._net, ids)
        expected = inner(foreign, pair_r) if foreign.any() else None
        if expected is None:
            assert cached is None
        else:
            assert cached is not None and cached.tobytes() == expected.tobytes()


def test_direct_search_reports_a_setup_deadline_stop() -> None:
    """``Router.route`` itself: ``None`` plus ``last_route_deadline_hit``."""
    router = _hv_autorouter()
    search = _armed_search_router(router)
    calls: list[int] = []
    _stall_dilation(search, calls)
    start, end = (router.pads[key] for key in router.nets[1])

    t0 = time.monotonic()
    result = search.route(start, end, per_net_timeout=2.0)
    elapsed = time.monotonic() - t0

    assert result is None
    assert search.last_route_deadline_hit is True
    assert search.last_route_deadline_iterations == 0
    assert elapsed <= 2.0 + STALL_S + 0.5
    assert len(calls) <= 3


@pytest.mark.parametrize("per_net_timeout", [None, BUDGET_S])
def test_unstalled_fallback_still_routes_the_hv_net(per_net_timeout: float | None) -> None:
    """With cheap masks the same armed call routes, and is no deadline stop."""
    router = _hv_autorouter()
    search = _armed_search_router(router)

    routes = router.route_net(1, per_net_timeout=per_net_timeout)

    assert routes, "the HV net must route on the armed fixture"
    assert search.last_route_deadline_hit is False
    assert len(search._pairwise_extra_cache) == len(LV_NAMES)
