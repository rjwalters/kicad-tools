"""Staged search budgets inside the hard total routing deadline (issue #5266).

``--timeout`` is a HARD TOTAL: :func:`route_deadline._supervise` runs the whole
invocation under it out-of-process and terminates the process group when it
fires.  Before #5266 the same number also doubled as the per-stage cap handed
to inner router routines, so a recipe with a *staged* contract -- Board 07's
"600 s initial search, then two 600 s placement-delta probes, then
postprocessing" -- was inexpressible: raising ``--timeout`` let the initial
pass swallow the enlarged budget, and leaving it at 600 s meant the supervisor
killed the run during the first probe (the #5164 qualification failure).

``--search-timeout`` is the separate per-stage allocation.  These tests pin the
four properties the issue's acceptance criteria name:

1. Configured per-stage limits are enforced (and default to the pre-#5266
   behaviour when the flag is absent).
2. Exhausting the initial stage's allocation still leaves time for permitted
   later work -- which it did NOT under the old single-number contract.
3. The overall ``--timeout`` still interrupts, and ``--search-timeout`` can
   never enlarge or escape it.
4. Checkpoint serialization restores the stage label it interrupted, so a
   later timeout is not misattributed to ``serialization``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_07_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"


def _staged_args(**overrides):
    """Namespace with a Board-07-shaped staged budget contract."""
    args = SimpleNamespace(
        timeout=2400.0,
        search_timeout=600.0,
        placement_delta_feedback_timeout=600.0,
        placement_delta_feedback_budget=2,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    route_cmd._set_wall_clock_deadline(args)
    return args


def _advance(args, seconds: float) -> None:
    """Simulate ``seconds`` of elapsed wall clock without sleeping."""
    for field in ("_wall_clock_deadline", "_routing_deadline"):
        value = getattr(args, field, None)
        if value is not None:
            setattr(args, field, value - seconds)


# =============================================================================
# 1. Configured per-stage limits are enforced
# =============================================================================


class TestSearchStageCap:
    def test_search_timeout_caps_a_single_stage_below_the_total(self):
        args = _staged_args()
        # The stage cap is the 600 s allocation, NOT the 2400 s total.
        assert route_cmd._search_stage_cap(args) == 600.0
        assert route_cmd._budgeted_timeout(args) == pytest.approx(600.0, abs=1.0)

    def test_absent_search_timeout_preserves_pre_5266_behaviour(self):
        """Without the flag the cap is ``--timeout`` verbatim (bit-for-bit)."""
        args = _staged_args(search_timeout=None)
        assert route_cmd._search_stage_cap(args) == 2400.0
        assert route_cmd._budgeted_timeout(args) == pytest.approx(2400.0, abs=1.0)

    def test_cap_applies_without_any_total_deadline(self):
        """``--search-timeout`` alone still bounds each stage."""
        args = SimpleNamespace(timeout=None, search_timeout=600.0)
        route_cmd._set_wall_clock_deadline(args)
        assert args._wall_clock_deadline is None
        assert route_cmd._budgeted_timeout(args) == 600.0

    def test_legacy_unbounded_run_is_unchanged(self):
        args = SimpleNamespace(timeout=None, search_timeout=None)
        route_cmd._set_wall_clock_deadline(args)
        assert route_cmd._budgeted_timeout(args) is None

    def test_stage_cap_never_exceeds_what_the_total_has_left(self):
        """A stage allocation larger than the remaining total is clamped."""
        args = _staged_args(timeout=900.0, search_timeout=600.0)
        _advance(args, 800.0)  # only ~100 s of the hard total remains
        budgeted = route_cmd._budgeted_timeout(args)
        assert budgeted is not None
        assert budgeted <= 100.0

    def test_per_attempt_slice_honours_the_stage_cap(self):
        """Escalation attempts are bounded by the stage cap, not the total."""
        args = _staged_args()
        slice_0 = route_cmd._per_attempt_budgeted_timeout(args, attempt_index=0, max_attempts=2)
        assert slice_0 is not None
        # Fair slice of the 2400 s total would be 1200 s; the stage cap wins.
        assert slice_0 == pytest.approx(600.0, abs=1.0)


# =============================================================================
# 2. Initial-stage exhaustion still leaves time for permitted later work
# =============================================================================


class _StubRouter:
    """Minimal stand-in: the loop only needs a failed-net count before it runs."""

    def get_failed_nets(self):
        return [1, 2, 3]


def _run_delta_feedback(args, tmp_path):
    """Drive the delta-feedback entry point far enough to see its budget decision.

    The PCB path deliberately does not exist, so the loop bails at ``PCB.load``
    *after* announcing whether it intends to run.  That keeps the test bounded
    (no routing) while still exercising the real skip/clamp decision.
    """
    return route_cmd._run_placement_delta_feedback(
        _StubRouter(),
        tmp_path / "missing.kicad_pcb",
        tmp_path / "out.kicad_pcb",
        args,
        False,
    )


class TestInitialStageExhaustion:
    def test_exhausted_initial_stage_leaves_budget_for_probes(self, tmp_path, capsys):
        """600 s of initial search out of a 2400 s total -> probes still run."""
        args = _staged_args()
        # The stage cap is what makes the 600 s simulation below faithful: the
        # initial pass cannot spend more than its allocation.
        assert route_cmd._budgeted_timeout(args) == pytest.approx(600.0, abs=1.0)
        _advance(args, 600.0)  # the initial search stage spent its allocation

        assert not route_cmd._deadline_expired(args)
        remaining = route_cmd._remaining_budget(args)
        assert remaining is not None and remaining > 1700.0

        _run_delta_feedback(args, tmp_path)
        out = capsys.readouterr().out
        assert "attempting delta feedback" in out
        assert "Skipping" not in out

    def test_old_single_number_contract_starved_the_probes(self, tmp_path, capsys):
        """Regression witness: ``--timeout 600`` alone is the #5266 failure."""
        args = _staged_args(timeout=600.0, search_timeout=None)
        _advance(args, 600.0)  # the same initial search stage

        assert route_cmd._deadline_expired(args)
        assert _run_delta_feedback(args, tmp_path) is None
        out = capsys.readouterr().out
        assert "Skipping" in out
        assert "attempting delta feedback" not in out

    def test_probe_allocation_is_granted_in_full_when_it_fits(self):
        args = _staged_args()
        _advance(args, 600.0)
        assert route_cmd._delta_probe_timeout(args) == 600.0

    def test_probe_allocation_never_escapes_the_hard_total(self, capsys):
        """A 600 s probe with 120 s of total left is clamped, not honoured."""
        args = _staged_args()
        _advance(args, 2280.0)  # ~120 s of the hard total remains
        timeout = route_cmd._delta_probe_timeout(args, quiet=False)
        assert timeout is not None
        assert timeout <= 120.0
        assert "clamped to the total --timeout" in capsys.readouterr().out

    def test_shortfall_between_total_and_probe_contract_is_announced(self, capsys):
        """2 x 600 s of probes with 900 s left gets an advisory note."""
        args = _staged_args()
        _advance(args, 1500.0)  # ~900 s left; 2 x 600 does not fit
        assert route_cmd._delta_probe_timeout(args, quiet=False) == 600.0
        assert "exceeds the" in capsys.readouterr().out

    def test_probe_loop_skipped_when_total_is_exhausted(self, tmp_path, capsys):
        """An explicit allocation does not resurrect an expired total deadline."""
        args = _staged_args()
        _advance(args, 2400.0)
        assert route_cmd._deadline_expired(args)
        assert _run_delta_feedback(args, tmp_path) is None
        out = capsys.readouterr().out
        assert "Skipping" in out
        assert "hard total --timeout deadline is exhausted" in out


# =============================================================================
# 3. The overall --timeout still interrupts; --search-timeout cannot enlarge it
# =============================================================================


class TestHardTotalDeadline:
    def test_search_timeout_does_not_enlarge_the_supervised_budget(self, tmp_path, monkeypatch):
        """The supervisor budget comes from ``--timeout`` alone."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240108))")
        captured: dict[str, float] = {}

        def _fake_supervise(command, budget, control, **kwargs):
            captured["budget"] = budget
            return 0

        monkeypatch.setattr(route_deadline, "_supervise", _fake_supervise)
        assert route_deadline.run([str(pcb), "--timeout", "5", "--search-timeout", "600"]) == 0
        assert captured["budget"] == 5.0

    def test_total_deadline_still_terminates_a_stuck_worker(self, tmp_path):
        """Bounded real-subprocess proof that the hard deadline still fires."""
        source = tmp_path / "input.kicad_pcb"
        source.write_text("(kicad_pcb (version 20240108))")
        output = tmp_path / "output.kicad_pcb"
        control = tmp_path / "control.json"
        control.write_text(
            json.dumps(
                {
                    "input": str(source),
                    "output": str(output),
                    "stage": "routing",
                    "snapshot_saved": False,
                }
            )
        )
        started = time.monotonic()
        assert (
            route_deadline._supervise(
                [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)\n"],
                2.0,
                control,
                save_seconds=1,
            )
            == route_deadline.TIMEOUT_EXIT
        )
        assert time.monotonic() - started < 20
        report = json.loads(output.with_suffix(".timeout.json").read_text())
        assert report["status"] == "partial"
        assert report["reason"] == "timeout"
        # Partial/unverified semantics are untouched by #5266.
        assert report["manufacturing_ready"] is False
        assert report["stage"] == "routing"


# =============================================================================
# 4. Checkpoint serialization restores the stage it interrupted
# =============================================================================


@pytest.fixture
def control_file(tmp_path, monkeypatch):
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"stage": "startup"}))
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(path))
    return path


class _StubRoute:
    def to_sexp(self, name_only: bool = False) -> str:
        return '(segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu") (net 1))'


class TestCheckpointStageRestore:
    def test_write_routed_pcb_still_stamps_serialization(self, tmp_path, control_file):
        source = tmp_path / "in.kicad_pcb"
        source.write_text("(kicad_pcb (version 20240108))")
        route_deadline.record_stage("routing")
        route_cmd._write_routed_pcb(source, tmp_path / "out.kicad_pcb", "", is_checkpoint=True)
        # Without a restore this is what the rest of the run would report.
        assert json.loads(control_file.read_text())["stage"] == "serialization"

    def test_checkpoint_callback_restores_the_routing_stage(self, tmp_path, control_file):
        source = tmp_path / "in.kicad_pcb"
        source.write_text("(kicad_pcb (version 20240108))")
        output = tmp_path / "out.kicad_pcb"
        route_deadline.record_stage("routing")

        callback = route_cmd._make_checkpoint_callback(source, output, 30.0, quiet=True)
        assert callback is not None
        callback([_StubRoute()], SimpleNamespace(iteration=1, routed_count=1, overflow=0))

        assert output.exists()
        assert json.loads(control_file.read_text())["stage"] == "routing"

    def test_restore_preserves_an_interrupt_recorded_mid_write(self, control_file):
        """A timeout *inside* the write is still reported as serialization."""
        route_deadline.record_stage("routing")
        with route_deadline.restore_stage():
            route_deadline.record_stage("serialization")
            # What ``_deadline_signal`` stamps when SIGTERM lands here.
            route_deadline.record_stage("serialization", interrupted_stage="serialization")

        state = json.loads(control_file.read_text())
        assert state["stage"] == "routing"
        # ``_supervise`` prefers ``interrupted_stage`` -- the real timeout label.
        assert state["interrupted_stage"] == "serialization"

    def test_restore_stage_is_inert_without_a_supervisor(self, monkeypatch):
        monkeypatch.delenv(route_deadline.CONTROL_ENV, raising=False)
        assert route_deadline.current_stage() is None
        with route_deadline.restore_stage() as previous:
            assert previous is None
        assert os.environ.get(route_deadline.CONTROL_ENV) is None


# =============================================================================
# Board 07's staged contract must fit inside its own hard total
# =============================================================================


def _load_board_07_recipe():
    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    sys.modules["generate_pcb"] = _load(
        "board_07_budget_generate_pcb", BOARD_07_DIR / "generate_pcb.py"
    )
    sys.modules["generate_schematic"] = _load(
        "board_07_budget_generate_schematic", BOARD_07_DIR / "generate_schematic.py"
    )
    return _load("board_07_budget_generate_design", BOARD_07_DIR / "generate_design.py")


class TestBoard07BudgetContract:
    """The recipe's own numbers must satisfy the contract #5266 exists to enable."""

    def test_total_covers_every_stage_the_recipe_permits(self):
        mod = _load_board_07_recipe()
        search = mod.ROUTE_SEARCH_TIMEOUT_S
        probes = mod.PLACEMENT_DELTA_FEEDBACK_BUDGET * mod.PLACEMENT_DELTA_FEEDBACK_TIMEOUT_S
        total = mod._route_total_timeout_s()

        assert search == 600, "the initial search stage keeps its measured 600 s allocation"
        assert mod.PLACEMENT_DELTA_FEEDBACK_TIMEOUT_S == 600, (
            "probes must get the SAME wall clock as the initial pass"
        )
        # The whole point: 600 + 2x600 fits, with room left for postprocessing.
        assert total >= search + probes
        assert total - (search + probes) >= 300
        # ... and the total still fits the existing 90-minute CI allowance.
        assert total <= 90 * 60

    def test_recipe_passes_both_budgets_to_the_cli(self):
        source = (BOARD_07_DIR / "generate_design.py").read_text()
        assert '"--search-timeout"' in source
        assert "str(ROUTE_SEARCH_TIMEOUT_S)" in source
        assert "str(_route_total_timeout_s())" in source
        # The old single-number contract must not come back.
        assert '"--timeout",\n        "600",' not in source
