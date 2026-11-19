"""Budget-labelled search give-ups must be visible in the routing summary.

Issue #9601 audit row 5 (PCBJam "no hint" failure mode): a net whose A* was
cut off by its wall-clock deadline or iteration cap used to be reported as a
bare ``No path found`` with generic suggestions, indistinguishable from a
geometrically unroutable net.  The negotiated loop already records each net's
last C++ ``FAILURE_*`` code on the pathfinder (``search_failures_by_net``,
#6369); ``show_routing_summary`` now surfaces the budget-labelled codes
(``timeout``, ``iteration_limit``, ...) with the budget remedy line
(coarser ``--grid`` / larger ``--per-net-timeout``).

Geometric codes (``no_path``) keep the legacy rendering: "No path found" and
no SEARCH BUDGET suggestion.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kicad_tools.router.output import show_routing_summary


def _make_router_mock(search_failures_by_net: dict[int, int] | None):
    """Minimal mock satisfying show_routing_summary (pattern of
    tests/test_single_pad_net_count.py)."""
    router = MagicMock()
    router.nets = {}
    router.routes = []
    router.routing_failures = []
    router.grid.num_layers = 2
    router.grid.resolution = 0.05
    router.pads = {}
    # ``rules`` is only consulted for attribute reads in some branches; give
    # it concrete values so any arithmetic stays a float.
    router.rules.trace_width = 0.2
    router.rules.trace_clearance = 0.15
    if search_failures_by_net is None:
        # Simulate a router without the store (non-grid engine / old mock).
        del router.router  # MagicMock auto-attrs removed
    else:
        router.router.search_failures_by_net = search_failures_by_net
    return router


def _run_summary(router) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        show_routing_summary(
            router,
            net_map={"LONG_HAUL": 1},
            nets_to_route=1,
            nets_to_route_ids={1},
        )
    return buf.getvalue()


class TestBudgetSearchFailureHint:
    def test_timeout_code_is_surfaced_with_budget_remedy(self):
        router = _make_router_mock({1: 3})  # FAILURE_TIMEOUT
        out = _run_summary(router)
        assert "timeout" in out, f"budget label missing from:\n{out}"
        assert "SEARCH BUDGET" in out
        assert "--per-net-timeout" in out
        # Coarser-grid remedy computed from the live grid resolution.
        assert "--grid 0.1" in out

    def test_iteration_limit_code_is_surfaced(self):
        router = _make_router_mock({1: 2})  # FAILURE_ITERATION_LIMIT
        out = _run_summary(router)
        assert "iteration_limit" in out
        assert "SEARCH BUDGET" in out

    def test_geometric_no_path_keeps_legacy_rendering(self):
        router = _make_router_mock({1: 1})  # FAILURE_NO_PATH
        out = _run_summary(router)
        assert "No path found" in out
        assert "SEARCH BUDGET" not in out
        assert "per-net-timeout" not in out

    def test_router_without_failure_store_is_unchanged(self):
        router = _make_router_mock(None)
        out = _run_summary(router)
        assert "No path found" in out
        assert "SEARCH BUDGET" not in out
