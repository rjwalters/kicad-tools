"""Issue #8485: bound repeat Python fallbacks that failed without a proof.

The #6098 / #8370 memos only record a Python A* that DRAINED its open set.  A
search ended by the per-net wall-clock deadline recorded nothing, so the same
request re-ran at the full per-net budget every negotiated iteration (100-650 s
per net on a 97-net HV board, ~30% of a 6 h stage).  Now any failed fallback is
recorded per pad-pair request: a repeat runs on a reduced budget, and once the
stage's fallback budget is spent repeats are skipped.  First attempts of
never-tried requests always keep their full budget.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router import cpp_backend
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend unavailable")


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _pathfinder() -> CppPathfinder:
    grid = RoutingGrid(width=10.0, height=6.0, rules=_rules(), layer_stack=LayerStack.two_layer())
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, _rules(), diagonal_routing=True)
    pf.set_routable_layers(cpp_grid.get_routable_indices())
    return pf


def _pads(net_name: str = "HV", y: float = 3.0) -> tuple[Pad, Pad]:
    common = {"width": 0.6, "height": 0.6, "net": 1, "net_name": net_name, "layer": Layer.F_CU}
    return Pad(x=2.0, y=y, **common), Pad(x=8.0, y=y, **common)


def _code(name: str = "FAILURE_PAIRWISE_BLOCKED") -> int:
    return int(getattr(cpp_backend.router_cpp, name))


def _fallback(pf: CppPathfinder, pads=None, timeout: float | None = 100.0, code=None):
    start, end = pads or _pads()
    return pf._try_python_fallback(
        start,
        end,
        reason="open set drained with pairwise refusals",
        cpp_failure_reason=_code() if code is None else code,
        negotiated_mode=True,
        per_net_timeout=timeout,
    )


def _deadline_hit_stub():
    """``Router.route`` stub: fails as if the per-net deadline fired (not drained)."""

    def _route(self, *args, **kwargs):
        self.last_route_exhausted = False
        self.last_route_deadline_hit = True
        return None

    return mock.patch.object(Router, "route", autospec=True, side_effect=_route)


@pytest.mark.parametrize("name", ["FAILURE_NO_PATH", "FAILURE_PAIRWISE_BLOCKED"])
def test_deadline_hit_failure_repeat_gets_reduced_budget(name: str) -> None:
    pf = _pathfinder()
    with _deadline_hit_stub() as spy:
        assert _fallback(pf, code=_code(name)) is None
        # Pretend the first attempt ran 200 s into its deadline.
        (key,) = pf._python_failed_attempts
        pf._python_failed_attempts[key] = 200.0
        assert _fallback(pf, code=_code(name)) is None
        assert _fallback(pf, code=_code(name)) is None
    budgets = [c.kwargs["per_net_timeout"] for c in spy.call_args_list]
    assert budgets[0] == pytest.approx(100.0)
    expected = max(
        cpp_backend.REPEAT_FALLBACK_BUDGET_FLOOR_S,
        cpp_backend.REPEAT_FALLBACK_BUDGET_FRACTION * 200.0,
    )
    assert budgets[1:] == [pytest.approx(expected)] * 2
    # The first attempt's seconds are kept, so the repeat budget does not decay.
    assert pf._python_failed_attempts[key] == 200.0
    stats = pf.fallback_stats
    assert stats["python_fallback_reduced_repeats"] == 2
    assert stats["python_fallback_reduced_seconds_saved"] == pytest.approx(400.0, abs=1.0)
    assert stats["python_fallback_seconds_saved"] >= stats["python_fallback_reduced_seconds_saved"]
    # Not a proof: the drained-repeat skip did not fire.
    assert stats["python_fallback_repeat_skips"] == 0


def test_floor_applies_to_short_first_attempt() -> None:
    pf = _pathfinder()
    with _deadline_hit_stub() as spy:
        _fallback(pf)
        _fallback(pf)
    assert spy.call_args_list[1].kwargs["per_net_timeout"] == pytest.approx(
        cpp_backend.REPEAT_FALLBACK_BUDGET_FLOOR_S
    )


def test_untried_request_keeps_full_budget() -> None:
    pf = _pathfinder()
    with _deadline_hit_stub() as spy:
        _fallback(pf, pads=_pads(y=3.0))
        _fallback(pf, pads=_pads(y=4.0))
    assert [c.kwargs["per_net_timeout"] for c in spy.call_args_list] == [
        pytest.approx(100.0),
        pytest.approx(100.0),
    ]
    assert pf.fallback_stats["python_fallback_reduced_repeats"] == 0


def test_no_wall_budget_is_unchanged() -> None:
    """``per_net_timeout=None`` (no wall budget, no tuned cap): nothing to reduce."""
    pf = _pathfinder()
    with _deadline_hit_stub() as spy:
        _fallback(pf, timeout=None)
        _fallback(pf, timeout=None)
    assert [c.kwargs["per_net_timeout"] for c in spy.call_args_list] == [None, None]
    assert pf.fallback_stats["python_fallback_reduced_repeats"] == 0


def test_successful_fallback_clears_record() -> None:
    pf = _pathfinder()
    with _deadline_hit_stub():
        _fallback(pf)
    assert pf._python_failed_attempts
    # Real search on an open grid routes the request and drops the record.
    assert _fallback(pf) is not None
    assert pf._python_failed_attempts == {}


def test_stage_budget_exhaustion_skips_repeat_but_not_new_request() -> None:
    pf = _pathfinder()
    pf.set_python_fallback_stage_budget(10.0)
    with _deadline_hit_stub() as spy:
        _fallback(pf, pads=_pads(y=3.0))
        pf._python_fallback_stage_spent = 10.0  # stage budget spent
        assert _fallback(pf, pads=_pads(y=3.0)) is None  # repeat: skipped
        assert spy.call_count == 1
        _fallback(pf, pads=_pads(y=4.0))  # first attempt of a new request: runs
        assert spy.call_count == 2
    stats = pf.fallback_stats
    assert stats["python_fallback_stage_budget_skips"] == 1
    assert stats["python_fallback_stage_budget_s"] == pytest.approx(10.0)
    assert stats["python_fallback_stage_spent_s"] >= 10.0


def test_setting_stage_budget_resets_spend_and_none_uncaps() -> None:
    pf = _pathfinder()
    pf.set_python_fallback_stage_budget(10.0)
    pf._python_fallback_stage_spent = 50.0
    pf.set_python_fallback_stage_budget(None)
    stats = pf.fallback_stats
    assert stats["python_fallback_stage_budget_s"] is None
    assert stats["python_fallback_stage_spent_s"] == 0.0
    with _deadline_hit_stub() as spy:
        _fallback(pf)
        _fallback(pf)
    assert spy.call_count == 2
    assert pf.fallback_stats["python_fallback_stage_budget_skips"] == 0


def test_env_opt_out_restores_full_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD_ROUTER_SKIP_REPEAT_FALLBACK", "0")
    pf = _pathfinder()
    pf.set_python_fallback_stage_budget(1.0)
    with _deadline_hit_stub() as spy:
        _fallback(pf)
        pf._python_fallback_stage_spent = 5.0
        _fallback(pf)
    assert [c.kwargs["per_net_timeout"] for c in spy.call_args_list] == [
        pytest.approx(100.0),
        pytest.approx(100.0),
    ]
    stats = pf.fallback_stats
    assert stats["python_fallback_reduced_repeats"] == 0
    assert stats["python_fallback_stage_budget_skips"] == 0


def test_relief_mode_untouched() -> None:
    pf = _pathfinder()
    pf.set_relief_mode(True)
    pf.set_python_fallback_stage_budget(1.0)
    with _deadline_hit_stub() as spy:
        _fallback(pf)
        _fallback(pf)
    assert [c.kwargs["per_net_timeout"] for c in spy.call_args_list] == [
        pytest.approx(100.0),
        pytest.approx(100.0),
    ]
    assert pf._python_failed_attempts == {}
    assert pf.fallback_stats["python_fallback_stage_spent_s"] == 0.0


def test_deterministic_budget_reduces_iterations_not_wall_clock() -> None:
    """Under a tuned per-net iteration cap the repeat bound stays load-independent."""
    pf = _pathfinder()
    pf._per_net_iteration_cap_active = True
    pf._effective_search_iterations = 1_000_000
    pf.set_python_fallback_stage_budget(1.0)
    seen: list[tuple[int | None, float | None]] = []

    def _route(self, *args, **kwargs):
        seen.append((self._max_iterations_override, kwargs["per_net_timeout"]))
        self.last_route_exhausted = False
        return None

    with mock.patch.object(Router, "route", autospec=True, side_effect=_route):
        _fallback(pf)
        pf._python_fallback_stage_spent = 5.0  # wall-time stage cap must not apply
        _fallback(pf)
    assert seen[0] == (1_000_000, pytest.approx(100.0))
    assert seen[1] == (100_000, pytest.approx(100.0))
    stats = pf.fallback_stats
    assert stats["python_fallback_reduced_repeats"] == 1
    assert stats["python_fallback_stage_budget_skips"] == 0


def test_negotiated_stage_sets_fallback_budget_on_public_board() -> None:
    """Integration: a timed negotiated stage on board 01 installs the stage cap."""
    from pathlib import Path

    from kicad_tools.router.io import load_pcb_for_routing

    pcb = (
        Path(__file__).resolve().parents[2]
        / "boards"
        / "01-voltage-divider"
        / "output"
        / "voltage_divider.kicad_pcb"
    )
    router, _net_map = load_pcb_for_routing(str(pcb), validate_drc=False, force_python=False)
    assert isinstance(router.router, CppPathfinder)
    router.route_all_negotiated(max_iterations=2, timeout=60.0)
    stats = router.router.fallback_stats
    assert stats["python_fallback_stage_budget_s"] == pytest.approx(
        cpp_backend.STAGE_FALLBACK_BUDGET_FRACTION * 60.0
    )
    assert stats["python_fallback_stage_spent_s"] >= 0.0
    assert stats["python_fallback_stage_budget_skips"] == 0
