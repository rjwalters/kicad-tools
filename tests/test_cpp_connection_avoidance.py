"""Clearance retry penalties must not leak into the next pad connection."""

from types import SimpleNamespace

import pytest

from kicad_tools.router.cpp_backend import CppPathfinder


@pytest.mark.parametrize("timing", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
def test_next_connection_starts_without_prior_clearance_penalties(timing, outcome):
    state = {"penalty": 0}
    completed = object()

    def search(*args, **kwargs):
        # A stale penalty would obstruct this otherwise independent edge.
        assert state["penalty"] == 0
        state["penalty"] = 100
        # Penalties must remain active within the connection's retry loop.
        assert state["penalty"] == 100
        if outcome == "exception":
            raise RuntimeError("search failed")
        return completed if outcome == "success" else None

    finder = SimpleNamespace(
        _per_call_timing_enabled=timing,
        _per_call_timings=[],
        _route_impl=search,
        clear_avoidance_costs=lambda: state.update(penalty=0),
    )
    pad = SimpleNamespace(net=6, net_name="USB2_D+")
    for _ in range(2):
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="search failed"):
                CppPathfinder.route(finder, pad, pad, clear_avoidance_after_connection=True)
        else:
            result = CppPathfinder.route(finder, pad, pad, clear_avoidance_after_connection=True)
            assert result is (completed if outcome == "success" else None)
        assert state["penalty"] == 0
    assert len(finder._per_call_timings) == (2 if timing else 0)


@pytest.mark.parametrize("timing", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
def test_connection_cleanup_clears_native_grid(monkeypatch, timing, outcome):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
    from kicad_tools.router.rules import DesignRules

    if not is_cpp_available():
        pytest.skip("C++ backend not available")
    grid = CppGrid(cols=20, rows=20, layers=2, resolution=0.127)
    finder = CppPathfinder(grid, DesignRules())
    finder.enable_per_call_timing(timing)
    completed = object()
    pad = SimpleNamespace(net=6, net_name="SENSE")
    forwarded = {
        "net_class": None,
        "negotiated_mode": True,
        "present_cost_factor": 2.0,
        "weight": 1.2,
        "start_layers": [0],
        "end_layers": [1],
        "per_net_timeout": 1.0,
        "extra_goal_cells": {(10, 10, 1)},
    }

    def search(start, end, **kwargs):
        assert start is pad and end is pad
        assert kwargs == forwarded
        assert grid._impl.at(10, 10, 0).avoidance_cost == 0
        for layer in range(2):
            grid._impl.boost_region_cost(10, 10, layer, 2, 100.0)
            assert grid._impl.at(10, 10, layer).avoidance_cost > 0
        if outcome == "exception":
            raise RuntimeError("search failed")
        return completed if outcome == "success" else None

    monkeypatch.setattr(finder, "_route_impl", search)
    for _ in range(2):
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="search failed"):
                finder.route(pad, pad, clear_avoidance_after_connection=True, **forwarded)
        else:
            assert finder.route(pad, pad, clear_avoidance_after_connection=True, **forwarded) is (
                completed if outcome == "success" else None
            )
        assert all(grid._impl.at(10, 10, layer).avoidance_cost == 0 for layer in range(2))
    records = finder.get_and_clear_per_call_timings()
    assert len(records) == (2 if timing else 0)
    for record in records:
        assert record["succeeded"] is (outcome == "success")
        assert record["per_net_timeout"] == 1.0


@pytest.mark.parametrize("timing", [False, True])
def test_default_preserves_penalties_until_caller_finishes_net(monkeypatch, timing):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available
    from kicad_tools.router.rules import DesignRules

    if not is_cpp_available():
        pytest.skip("C++ backend not available")
    grid = CppGrid(cols=20, rows=20, layers=2, resolution=0.127)
    finder = CppPathfinder(grid, DesignRules())
    finder.enable_per_call_timing(timing)
    observed = []
    pad = SimpleNamespace(net=6, net_name="SENSE")

    def search(*args, **kwargs):
        observed.append(grid._impl.at(10, 10, 0).avoidance_cost)
        grid._impl.boost_region_cost(10, 10, 0, 2, 100.0)
        return None

    monkeypatch.setattr(finder, "_route_impl", search)
    finder.route(pad, pad)
    finder.route(pad, pad)
    assert observed[0] == 0
    assert observed[1] > 0
    assert grid._impl.at(10, 10, 0).avoidance_cost > 0
    # Autorouter retains ownership of net-end cleanup on the default path.
    finder.clear_avoidance_costs()
    assert grid._impl.at(10, 10, 0).avoidance_cost == 0
