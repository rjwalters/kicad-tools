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
                CppPathfinder.route(finder, pad, pad)
        else:
            result = CppPathfinder.route(finder, pad, pad)
            assert result is (completed if outcome == "success" else None)
        assert state["penalty"] == 0
    assert len(finder._per_call_timings) == (2 if timing else 0)
