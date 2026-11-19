"""Budget-labelled search give-ups must be visible in the routing summary.

Issue #9601 audit row 5 (PCBJam "no hint" failure mode): a net whose A* was
cut off by its wall-clock deadline or iteration cap used to be reported as a
bare ``No path found`` with generic suggestions, indistinguishable from a
geometrically unroutable net.  The negotiated loop already records each net's
last C++ ``FAILURE_*`` code on the pathfinder (``search_failures_by_net``,
#6369); ``show_routing_summary`` now surfaces the budget-labelled codes
(``timeout``, ``iteration_limit``, ...) with a budget remedy that names the
flag matching the cut-off: ``--per-net-timeout`` for wall-clock give-ups,
``--per-net-iterations`` / ``--max-search-iterations`` for iteration-limit
give-ups, plus a coarser ``--grid`` hint.  The grid hint must be a value the
CLI accepts: the route flow refuses ``--grid`` above the effective clearance
(``rules.trace_clearance``), so the hint is bounded by it and omitted when no
legal coarser grid exists.

A partially connected net can also be a budget give-up -- its committed pads
routed while the remaining pad-to-pad search was cut off -- so it shows the
budget reason next to its pad counts and counts toward the SEARCH BUDGET
suggestion.  A fully connected net never does, even though the failure store
can retain a code from an earlier attempt that later succeeded.

A budget stop does NOT establish the geometry either way (the same blocked
endpoints can return ``iteration_limit`` under a small budget and ``no_path``
under a large one), so the budget lines must say the search stopped before it
could establish whether a path exists -- never that the net is geometrically
free.

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
    # The clearance the CLI's ``--grid`` refusal compares against (the route
    # flow builds DesignRules with ``trace_clearance=args.clearance``); the
    # grid hint is bounded by it.
    router.grid.rules.trace_clearance = 0.15
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


def _run_summary(router, net_map=None, nets_to_route=1, nets_to_route_ids=None) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        show_routing_summary(
            router,
            net_map=net_map if net_map is not None else {"LONG_HAUL": 1},
            nets_to_route=nets_to_route,
            nets_to_route_ids=nets_to_route_ids if nets_to_route_ids is not None else {1},
        )
    return buf.getvalue()


class TestBudgetSearchFailureHint:
    def test_timeout_code_is_surfaced_with_budget_remedy(self):
        router = _make_router_mock({1: 3})  # FAILURE_TIMEOUT
        out = _run_summary(router)
        assert "timeout" in out, f"budget label missing from:\n{out}"
        assert "SEARCH BUDGET" in out
        assert "--per-net-timeout" in out
        # The wall-clock flag is reserved for wall-clock give-ups: a timeout
        # must not advertise the iteration caps.
        assert "--per-net-iterations" not in out
        assert "--max-search-iterations" not in out
        # Coarser-grid remedy computed from the live grid resolution.
        assert "--grid 0.1" in out

    def test_iteration_limit_code_is_surfaced(self):
        router = _make_router_mock({1: 2})  # FAILURE_ITERATION_LIMIT
        out = _run_summary(router)
        assert "iteration_limit" in out
        assert "SEARCH BUDGET" in out

    def test_iteration_limit_names_the_iteration_flags(self):
        # An iteration-limit give-up is bounded by the per-net cap and the
        # backstop (effective per-net cap = min of the two); raising only one
        # never relieves a binding other one.  The wall-clock flag is for
        # timeouts and must not be the advice here.
        router = _make_router_mock({1: 2})  # FAILURE_ITERATION_LIMIT
        out = _run_summary(router)
        assert "--per-net-iterations" in out, f"per-net cap missing from:\n{out}"
        assert "--max-search-iterations" in out, f"backstop missing from:\n{out}"
        assert "--per-net-timeout" not in out

    def test_mixed_budget_labels_name_both_families(self):
        # A run with one net cut off by wall clock and another by the
        # iteration cap advertises both remedies.
        router = _make_router_mock({1: 3, 2: 2})  # FAILURE_TIMEOUT + FAILURE_ITERATION_LIMIT
        out = _run_summary(
            router,
            net_map={"LONG_HAUL": 1, "ITER_HAUL": 2},
            nets_to_route=2,
            nets_to_route_ids={1, 2},
        )
        assert "--per-net-timeout" in out
        assert "--per-net-iterations" in out
        assert "--max-search-iterations" in out

    def test_grid_hint_is_bounded_by_the_effective_clearance(self):
        # The suggested grid must be one the CLI accepts: ``--grid`` above
        # the effective clearance (rules.trace_clearance) is refused with
        # "Grid resolution ... exceeds clearance ...", so a doubled
        # resolution is capped at the clearance.
        router = _make_router_mock({1: 2})  # FAILURE_ITERATION_LIMIT
        router.grid.resolution = 0.1  # doubling would give 0.2 > 0.15
        out = _run_summary(router)
        assert "--grid 0.15" in out, f"clearance-bounded hint missing from:\n{out}"
        assert "--grid 0.2" not in out, f"hint exceeds clearance:\n{out}"

    def test_grid_hint_omitted_when_no_legal_coarser_grid(self):
        # When even the clearance-capped candidate is not coarser than the
        # current resolution, there is no legal coarser grid: the hint is
        # omitted entirely rather than suggesting a value the CLI refuses.
        router = _make_router_mock({1: 2})  # FAILURE_ITERATION_LIMIT
        router.grid.resolution = 0.15  # == clearance: no legal coarser grid
        out = _run_summary(router)
        assert "SEARCH BUDGET" in out, f"budget block missing from:\n{out}"
        assert "--grid" not in out, f"illegal grid hint in:\n{out}"

    def test_partially_connected_budget_net_shows_reason_and_suggestion(self, monkeypatch):
        # A net whose committed pads routed but whose remaining pad-to-pad
        # search was cut off keeps its pad counts visible, shows the budget
        # reason on the partially-connected line, and counts toward the
        # SEARCH BUDGET suggestion (no unrouted net is needed for it).
        import kicad_tools.router.observability as observability

        router = _make_router_mock({1: 2})  # FAILURE_ITERATION_LIMIT
        router.routes = [MagicMock(net=1, vias=[], segments=[])]
        router.nets = {1: ["U1.1", "U1.2", "U1.3"]}
        router.pads = {key: MagicMock() for key in router.nets[1]}
        info = {
            "total_pads": 3,
            "connected_pads": 2,
            "connected": False,
            "stranded_pads": ["U1.3"],
        }
        monkeypatch.setattr(
            observability,
            "validate_net_connectivity",
            lambda routes, pads, tolerance=0.01: {1: info},
        )
        out = _run_summary(router, net_map={"DATA": 1}, nets_to_route=1, nets_to_route_ids={1})
        assert "2/3 pads connected" in out, f"pad counts lost from:\n{out}"
        assert "iteration_limit" in out, f"budget reason missing from partial line:\n{out}"
        assert "SEARCH BUDGET" in out, f"no SEARCH BUDGET entry for partial net:\n{out}"
        assert "1 net affected" in out, f"partial net not counted:\n{out}"
        assert "--per-net-iterations" in out, f"iteration remedy missing from:\n{out}"

    def test_fully_connected_net_with_stale_failure_code_is_excluded(self, monkeypatch):
        # The failure store can retain a code from an earlier attempt that
        # later succeeded; a fully connected net must not surface it.
        import kicad_tools.router.observability as observability

        router = _make_router_mock({1: 2})  # stale FAILURE_ITERATION_LIMIT
        router.routes = [MagicMock(net=1, vias=[], segments=[])]
        router.nets = {1: ["U1.1", "U1.2", "U1.3"]}
        router.pads = {key: MagicMock() for key in router.nets[1]}
        info = {
            "total_pads": 3,
            "connected_pads": 3,
            "connected": True,
            "stranded_pads": [],
        }
        monkeypatch.setattr(
            observability,
            "validate_net_connectivity",
            lambda routes, pads, tolerance=0.01: {1: info},
        )
        out = _run_summary(router, net_map={"DATA": 1}, nets_to_route=1, nets_to_route_ids={1})
        assert "SEARCH BUDGET" not in out, f"stale code surfaced for a connected net:\n{out}"
        assert "iteration_limit" not in out, f"stale label rendered:\n{out}"

    def test_budget_stop_does_not_rule_out_geometry(self):
        # A budget cut-off leaves the geometry undetermined: the same blocked
        # endpoints can return iteration_limit under a small budget and
        # no_path under a large one.  Neither budget line may claim the net
        # is (or is not) geometrically blocked.
        for code in (2, 3):  # FAILURE_ITERATION_LIMIT, FAILURE_TIMEOUT
            router = _make_router_mock({1: code})
            out = _run_summary(router)
            assert "geometrically blocked" not in out, f"code {code} rules out geometry:\n{out}"
            assert "geometric blocker" not in out, f"code {code} rules out geometry:\n{out}"
            assert "establish whether a path exists" in out, f"code {code} wording missing:\n{out}"

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
