"""Issue #6475: ``--deterministic-rescue`` also removes the other reach-deciding clocks.

Three wall clocks stayed live in the grid negotiated loop under
``--deterministic-budget --deterministic-rescue --timeout``: the 60 s
oscillation-escape budget (#2415), the remaining-budget per-iteration per-net
cap (#3989), and checkpoint timing feeding the seeded global RNG (#3272).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import _make_checkpoint_callback
from kicad_tools.core.types import CopperLayer
from kicad_tools.router import primitives
from kicad_tools.router.core import Autorouter, IterationMetrics
from kicad_tools.router.primitives import Route, Segment

_PCB = '(kicad_pcb (version 20240108) (generator "t") (net 0 "") (net 1 "A"))\n'


class _Stub:
    _active_expansion_cap = Autorouter._active_expansion_cap
    _wall_clock_free_arm = Autorouter._wall_clock_free_arm
    _escape_budget = Autorouter._escape_budget
    _wall_clock_arm_line = Autorouter._wall_clock_arm_line

    def __init__(self, per_net_iterations: int = 0, max_search_iterations: int = 0) -> None:
        self._per_net_iterations = per_net_iterations
        self._max_search_iterations = max_search_iterations


class TestEscapeBudgetArms:
    def test_deterministic_arm_has_no_escape_budget(self):
        stub = _Stub(per_net_iterations=1000)
        assert stub._escape_budget(360.0, 0.0, True) is None
        assert stub._wall_clock_free_arm(True)

    def test_flag_off_keeps_the_60s_budget(self):
        stub = _Stub(per_net_iterations=1000)
        import time

        budget = stub._escape_budget(10_000.0, time.time(), False)
        assert budget == pytest.approx(60.0)

    def test_flag_off_budget_is_quarter_of_remaining(self):
        import time

        budget = _Stub()._escape_budget(100.0, time.time(), False)
        assert budget is not None and budget == pytest.approx(25.0, abs=0.5)

    def test_flag_without_cap_keeps_wall_clock(self):
        """No expansion cap -> no machine-independent bound; wall clock stays."""
        import time

        stub = _Stub()
        assert not stub._wall_clock_free_arm(True)
        assert stub._escape_budget(10_000.0, time.time(), True) == pytest.approx(60.0)

    def test_expired_stage_clamps_to_zero(self):
        import time

        assert _Stub()._escape_budget(1.0, time.time() - 100, False) == 0.0

    def test_no_timeout_is_unbounded_either_way(self):
        assert _Stub()._escape_budget(None, 0.0, False) is None

    def test_banner_names_each_arm(self):
        on = _Stub(max_search_iterations=5)._wall_clock_arm_line(True)
        off = _Stub(max_search_iterations=5)._wall_clock_arm_line(False)
        assert "Escape budget / per-iteration per-net cap: iteration-bounded" in on
        assert "Escape budget / per-iteration per-net cap: wall clock" in off


def test_negotiated_loop_uses_the_helpers():
    import inspect

    src = inspect.getsource(Autorouter.route_all_negotiated)
    assert src.count("self._escape_budget(") == 2
    assert "min(60.0, remaining * 0.25)" not in src
    assert "self._wall_clock_free_arm(deterministic_rescue)" in src
    assert "self._wall_clock_arm_line(deterministic_rescue)" in src


def _route(net: int) -> Route:
    return Route(
        net=net,
        net_name=f"N{net}",
        segments=[
            Segment(x1=0.0, y1=0.0, x2=1.0, y2=1.0, width=0.2, layer=CopperLayer.F_CU, net=net)
        ],
    )


class TestCheckpointIsAnRngObserver:
    @pytest.fixture(autouse=True)
    def _uuids(self):
        primitives.enable_deterministic_uuids(True)
        yield
        primitives.reset_deterministic_uuids()

    def test_checkpoint_write_leaves_rng_state_unchanged(self, tmp_path: Path):
        pcb = tmp_path / "in.kicad_pcb"
        pcb.write_text(_PCB)
        cb = _make_checkpoint_callback(pcb, tmp_path / "out.kicad_pcb", 0.0001, quiet=True)
        assert cb is not None
        random.seed(42)
        before = random.getstate()
        cb([_route(1)], IterationMetrics(iteration=1, routed_count=1, overflow=0))
        assert (tmp_path / "out.kicad_pcb").exists()
        assert random.getstate() == before

    def test_draws_after_checkpoint_match_a_run_without_one(self, tmp_path: Path):
        pcb = tmp_path / "in.kicad_pcb"
        pcb.write_text(_PCB)
        cb = _make_checkpoint_callback(pcb, tmp_path / "out.kicad_pcb", 0.0001, quiet=True)
        random.seed(7)
        expected = [random.random() for _ in range(3)]
        random.seed(7)
        cb([_route(1)], IterationMetrics(iteration=1, routed_count=1, overflow=0))
        assert [random.random() for _ in range(3)] == expected

    def test_completed_hook_also_restores_state(self, tmp_path: Path):
        pcb = tmp_path / "in.kicad_pcb"
        pcb.write_text(_PCB)
        cb = _make_checkpoint_callback(pcb, tmp_path / "out.kicad_pcb", 30.0, quiet=True)
        random.seed(9)
        before = random.getstate()
        cb.checkpoint_completed(
            [_route(1)], IterationMetrics(iteration=1, routed_count=1, overflow=0)
        )
        assert random.getstate() == before
