"""Tests for the external-benchmark metrics module (issue #4934).

The fixture below is a deliberately small *routed* board whose every
headline number is known by construction:

* ``SIG1`` — two pads joined by one 10 mm F.Cu trace: 1 of 1 connection.
* ``SIG2`` — three pads; R1.2 and R2.2 joined through a via (4.5 mm on
  F.Cu + 5.5 mm on B.Cu), R3.1 left stranded: 1 of 2 connections. The
  stranded pad sits alone, so the net contributes exactly ONE open
  connection — the case that separates a ratsnest-style count from a
  naive unconnected-pad count.
* ``SIG3`` — a single pad: 0 connections required.
* One copper ``(arc …)`` track: a radius-1 mm semicircle, ``pi`` mm long.
  ``PCB`` does not model copper arcs, so this is the regression guard for
  wirelength measured off ``pcb.segments`` alone.

Totals: 2 of 3 connections routed, 1 via, ``20 + pi`` mm of copper.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.benchmark.external import (
    ARTIFACT_SOURCE_FALLBACK_INPUT,
    ARTIFACT_SOURCE_ROUTER_OUTPUT,
    ROUTE_OUTCOME_COMPLETED,
    ROUTE_OUTCOME_FAILED,
    ROUTE_OUTCOME_PARTIAL,
    ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING,
    ROUTE_OUTCOME_TIMEOUT,
    ROUTE_OUTCOME_UNKNOWN,
    SCHEMA_URL,
    SCHEMA_VERSION,
    BackendInfo,
    BenchmarkReport,
    CompletionMetrics,
    CopperMetrics,
    DiffPairCompletion,
    KctCheckSummary,
    KicadCliDrcSummary,
    RouteOutcome,
    TimingMetrics,
    build_route_outcome,
    build_timing,
    collect_report,
    measure_completion,
    measure_copper,
    measure_diff_pairs,
    probe_backend,
    render_markdown,
    render_report_markdown,
    run_kct_check,
    run_kicad_cli_drc,
)

ROUTED_FIXTURE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (net 3 "SIG3")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 0) (end 50 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 20) (end 0 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.05))

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG2"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" thru_hole circle (at 0.5 0) (size 0.9 0.9) (drill 0.4)
      (layers "*.Cu") (net 2 "SIG2"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "R3")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG2"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG3"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10.5 10) (end 15 10) (width 0.25) (layer "F.Cu") (net 2))
  (segment (start 15 10) (end 20.5 10) (width 0.25) (layer "B.Cu") (net 2))
  (via (at 15 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
  (arc (start 40 10) (mid 41 9) (end 42 10) (width 0.25) (layer "F.Cu") (net 3))
)
"""


DIFF_PAIR_FIXTURE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "USB_P")
  (net 2 "USB_N")

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "J1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "USB_P"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "USB_N"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "U1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "USB_P"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "USB_N"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""


EXPECTED_WIRELENGTH_MM = 20 + math.pi


@pytest.fixture
def routed_board(tmp_path: Path) -> Path:
    path = tmp_path / "routed.kicad_pcb"
    path.write_text(ROUTED_FIXTURE, encoding="utf-8")
    return path


@pytest.fixture
def diff_pair_board(tmp_path: Path) -> Path:
    path = tmp_path / "diffpair.kicad_pcb"
    path.write_text(DIFF_PAIR_FIXTURE, encoding="utf-8")
    return path


def _cpp_backend() -> BackendInfo:
    return BackendInfo(backend="cpp", available=True, version="1.0.0", build_version=21)


def _python_backend() -> BackendInfo:
    return BackendInfo(
        backend="python",
        available=False,
        unavailable_reason="C++ router extension not built",
    )


# ---------------------------------------------------------------------------
# Copper: wirelength + via count, measured from the board FILE
# ---------------------------------------------------------------------------


class TestMeasureCopper:
    def test_via_count_from_board_file(self, routed_board: Path) -> None:
        assert measure_copper(routed_board).via_count == 1

    def test_wirelength_includes_copper_arc_tracks(self, routed_board: Path) -> None:
        copper = measure_copper(routed_board)
        assert copper.segment_count == 3
        assert copper.arc_count == 1
        assert copper.wirelength_mm == pytest.approx(EXPECTED_WIRELENGTH_MM, abs=1e-6)

    def test_arc_is_invisible_to_pcb_segments(self, routed_board: Path) -> None:
        """Regression guard: the schema PCB does not model copper arcs.

        If it ever does, ``measure_copper`` must stop adding them
        separately or the arc would be double-counted.
        """
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(routed_board))
        assert len(pcb.segments) == 3
        straight_only = sum(math.dist(s.start, s.end) for s in pcb.segments)
        assert straight_only == pytest.approx(20.0, abs=1e-6)

    def test_wirelength_split_by_layer(self, routed_board: Path) -> None:
        by_layer = measure_copper(routed_board).wirelength_by_layer_mm
        assert set(by_layer) == {"F.Cu", "B.Cu"}
        assert by_layer["B.Cu"] == pytest.approx(5.5, abs=1e-6)
        assert by_layer["F.Cu"] == pytest.approx(14.5 + math.pi, abs=1e-6)

    def test_to_dict_rounds_and_sorts(self, routed_board: Path) -> None:
        data = measure_copper(routed_board).to_dict()
        assert data["wirelength_mm"] == round(EXPECTED_WIRELENGTH_MM, 2)
        assert list(data["wirelength_by_layer_mm"]) == ["B.Cu", "F.Cu"]


# ---------------------------------------------------------------------------
# Completion counted in ratsnest connections (DeepPCB's unit)
# ---------------------------------------------------------------------------


class TestMeasureCompletion:
    def test_connections_routed_over_total(self, routed_board: Path) -> None:
        completion = measure_completion(routed_board)
        # SIG1 1/1, SIG2 1/2, SIG3 0/0.
        assert (completion.connections_routed, completion.connections_total) == (2, 3)
        assert completion.completion_pct == pytest.approx(200 / 3, abs=1e-6)

    def test_net_rollup_alongside_connection_counts(self, routed_board: Path) -> None:
        completion = measure_completion(routed_board)
        assert completion.nets_total == 3
        assert completion.nets_complete == 2  # SIG1 + single-pad SIG3
        assert completion.nets_incomplete == 1  # SIG2

    def test_island_count_drives_open_connection_count(self, routed_board: Path) -> None:
        """A stranded island is ONE open connection, not one per pad."""
        result = NetStatusAnalyzer(str(routed_board)).analyze()
        sig2 = result.get_net("SIG2")
        assert sig2 is not None
        assert sig2.island_count == 2
        assert sig2.total_connections == 2
        assert sig2.open_connections == 1
        assert sig2.routed_connections == 1

    def test_single_pad_net_has_one_island_and_no_connections(self, routed_board: Path) -> None:
        result = NetStatusAnalyzer(str(routed_board)).analyze()
        sig3 = result.get_net("SIG3")
        assert sig3 is not None
        assert sig3.island_count == 1
        assert sig3.total_connections == 0
        assert sig3.open_connections == 0

    def test_net_status_result_aggregates(self, routed_board: Path) -> None:
        result = NetStatusAnalyzer(str(routed_board)).analyze()
        assert result.total_connections == 3
        assert result.routed_connections == 2
        assert result.open_connections == 1
        assert result.connection_completion_percentage == pytest.approx(200 / 3, abs=1e-6)

    def test_net_status_dicts_expose_connection_fields(self, routed_board: Path) -> None:
        result = NetStatusAnalyzer(str(routed_board)).analyze()
        data = result.to_dict()
        assert data["total_connections"] == 3
        assert data["routed_connections"] == 2
        assert data["open_connections"] == 1
        sig2 = next(n for n in data["nets"] if n["net_name"] == "SIG2")
        assert sig2["island_count"] == 2
        assert sig2["open_connections"] == 1

    def test_empty_board_is_not_a_routing_failure(self) -> None:
        completion = CompletionMetrics(
            connections_routed=0,
            connections_total=0,
            nets_total=0,
            nets_complete=0,
            nets_incomplete=0,
            nets_unrouted=0,
            nets_blocking_incomplete=0,
        )
        assert completion.completion_pct == 100.0


# ---------------------------------------------------------------------------
# Environment validity: timing must be REFUSED on the Python fallback
# ---------------------------------------------------------------------------


class TestTimingValidity:
    def test_timing_accepted_under_cpp_backend(self) -> None:
        timing = build_timing(142.7, _cpp_backend())
        assert timing.valid is True
        assert timing.wall_clock_s == pytest.approx(142.7)
        assert timing.refusal_reason is None

    def test_timing_refused_under_python_fallback(self) -> None:
        timing = build_timing(142.7, _python_backend())
        assert timing.valid is False
        # The number is DROPPED, not merely flagged, so no renderer can
        # accidentally publish a fallback runtime.
        assert timing.wall_clock_s is None
        assert timing.refusal_reason is not None
        assert "C++ router backend was not active" in timing.refusal_reason

    def test_timing_refused_when_probe_reports_cpp_unavailable(self) -> None:
        backend = BackendInfo(
            backend="cpp",
            available=False,
            unavailable_reason="backend probe did not run",
        )
        assert backend.timing_valid is False
        assert build_timing(1.0, backend).wall_clock_s is None

    def test_timing_refused_when_nothing_was_timed(self) -> None:
        timing = build_timing(None, _cpp_backend())
        assert timing.valid is False
        assert timing.wall_clock_s is None
        assert "no routing pass was timed" in (timing.refusal_reason or "")

    def test_refused_timing_serializes_as_null(self) -> None:
        data = build_timing(9.9, _python_backend()).to_dict()
        assert data["wall_clock_s"] is None
        assert data["valid"] is False

    def test_probe_backend_reports_a_known_backend(self) -> None:
        backend = probe_backend()
        assert backend.backend in {"cpp", "python", "unknown"}
        assert isinstance(backend.available, bool)

    def test_measured_phase_labels_a_non_completed_attempt(self) -> None:
        """A real, backend-eligible timing on a failed attempt is still
        published (it IS real elapsed time), but is labeled so it can never
        be read as a completed-routing performance number (#5280).
        """
        outcome = RouteOutcome(
            outcome=ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING,
            artifact_source=ARTIFACT_SOURCE_FALLBACK_INPUT,
            exit_code=2,
        )
        timing = build_timing(4.4, _cpp_backend(), route_outcome=outcome)
        assert timing.valid is True
        assert timing.wall_clock_s == pytest.approx(4.4)
        assert timing.measured_phase == ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING

    def test_measured_phase_defaults_unknown_without_outcome(self) -> None:
        timing = build_timing(1.0, _cpp_backend())
        assert timing.measured_phase == ROUTE_OUTCOME_UNKNOWN


# ---------------------------------------------------------------------------
# Route outcome + artifact provenance (issue #5280, Epic #5278 Phase 1)
# ---------------------------------------------------------------------------


class TestBuildRouteOutcome:
    def test_completed_on_exit_zero_with_full_completion(self) -> None:
        outcome = build_route_outcome(
            exit_code=0, output_exists=True, completion_pct=100.0, connections_total=2
        )
        assert outcome.outcome == ROUTE_OUTCOME_COMPLETED
        assert outcome.artifact_source == ARTIFACT_SOURCE_ROUTER_OUTPUT
        assert outcome.reason is None

    def test_completed_when_nothing_was_required(self) -> None:
        """``connections_total == 0`` is "nothing to route", not a failure."""
        outcome = build_route_outcome(
            exit_code=0, output_exists=True, completion_pct=100.0, connections_total=0
        )
        assert outcome.outcome == ROUTE_OUTCOME_COMPLETED

    def test_partial_on_exit_zero_below_full_completion(self) -> None:
        outcome = build_route_outcome(
            exit_code=0, output_exists=True, completion_pct=32.6, connections_total=460
        )
        assert outcome.outcome == ROUTE_OUTCOME_PARTIAL
        assert outcome.artifact_source == ARTIFACT_SOURCE_ROUTER_OUTPUT
        assert "not a failure" in (outcome.reason or "")

    def test_partial_output_preserved_despite_nonzero_exit(self) -> None:
        """Real, newly-produced partial output must never be mislabeled as
        a clean success just because the exit was non-zero -- but it must
        also not be discarded as a failure when it IS real progress.
        """
        outcome = build_route_outcome(exit_code=2, output_exists=True)
        assert outcome.outcome == ROUTE_OUTCOME_PARTIAL
        assert outcome.artifact_source == ARTIFACT_SOURCE_ROUTER_OUTPUT
        assert "partial progress" in (outcome.reason or "")

    def test_failed_on_nonzero_exit_no_output(self) -> None:
        outcome = build_route_outcome(exit_code=1, output_exists=False)
        assert outcome.outcome == ROUTE_OUTCOME_FAILED
        assert outcome.artifact_source == ARTIFACT_SOURCE_FALLBACK_INPUT
        assert outcome.exit_code == 1

    def test_failed_on_exception_without_output(self) -> None:
        outcome = build_route_outcome(
            exit_code=None, output_exists=False, exception=RuntimeError("boom")
        )
        assert outcome.outcome == ROUTE_OUTCOME_FAILED
        assert outcome.artifact_source == ARTIFACT_SOURCE_FALLBACK_INPUT
        assert "boom" in (outcome.reason or "")

    def test_failed_on_exception_preserves_partial_output(self) -> None:
        outcome = build_route_outcome(
            exit_code=None, output_exists=True, exception=RuntimeError("boom")
        )
        assert outcome.outcome == ROUTE_OUTCOME_FAILED
        assert outcome.artifact_source == ARTIFACT_SOURCE_ROUTER_OUTPUT

    def test_timeout_takes_priority_over_exit_code(self) -> None:
        outcome = build_route_outcome(exit_code=None, output_exists=False, timed_out=True)
        assert outcome.outcome == ROUTE_OUTCOME_TIMEOUT
        assert outcome.artifact_source == ARTIFACT_SOURCE_FALLBACK_INPUT

    def test_unknown_when_no_attempt_evidence(self) -> None:
        outcome = build_route_outcome(exit_code=None, output_exists=False)
        assert outcome.outcome == ROUTE_OUTCOME_UNKNOWN
        assert "no route attempt was recorded" in (outcome.reason or "")

    def test_unknown_when_exit_zero_contradicts_missing_output(self) -> None:
        """Exit 0 (claimed success) with no output file is a contradiction
        the harness cannot resolve -- unknown, never assumed successful.
        """
        outcome = build_route_outcome(exit_code=0, output_exists=False)
        assert outcome.outcome == ROUTE_OUTCOME_UNKNOWN
        assert outcome.artifact_source == ARTIFACT_SOURCE_FALLBACK_INPUT

    def test_to_dict_round_trips(self) -> None:
        outcome = RouteOutcome(
            outcome=ROUTE_OUTCOME_COMPLETED,
            artifact_source=ARTIFACT_SOURCE_ROUTER_OUTPUT,
            exit_code=0,
        )
        assert outcome.to_dict() == {
            "outcome": "completed",
            "artifact_source": "router_output",
            "exit_code": 0,
            "reason": None,
        }


class TestCollectReportRouteOutcomeAndBaseline:
    def test_no_route_evidence_leaves_outcome_none(self, routed_board: Path) -> None:
        """A bare re-measurement (no ``route_*`` kwargs) must not fabricate
        an outcome -- absent evidence stays absent, not success-shaped.
        """
        report = collect_report(
            routed_board,
            board_id="fixture",
            protocol="zero-touch",
            run_kicad_cli=False,
            backend=_cpp_backend(),
        )
        assert report.route_outcome is None
        assert report.pre_route_completion is None
        assert report.newly_routed_connections is None

    def test_route_evidence_populates_outcome(self, routed_board: Path) -> None:
        report = collect_report(
            routed_board,
            board_id="fixture",
            protocol="zero-touch",
            run_kicad_cli=False,
            backend=_cpp_backend(),
            route_exit_code=0,
            route_output_exists=True,
        )
        assert report.route_outcome is not None
        assert report.route_outcome.outcome == ROUTE_OUTCOME_PARTIAL  # 2/3, not 100%

    def test_pre_route_completion_and_delta(self, routed_board: Path, tmp_path: Path) -> None:
        # A pre-route board with only SIG1 connected (1 of 3 connections):
        # SIG2's segments/via and SIG3's arc are absent, as if this were
        # ``routed_board`` before this attempt's routing pass ran.
        pre_route_text = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
  (net 3 "SIG3")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 0) (end 50 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 20) (end 0 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.05))

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG2"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" thru_hole circle (at 0.5 0) (size 0.9 0.9) (drill 0.4)
      (layers "*.Cu") (net 2 "SIG2"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "R3")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "SIG2"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG3"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""
        pre_route_path = tmp_path / "pre_route.kicad_pcb"
        pre_route_path.write_text(pre_route_text, encoding="utf-8")
        report = collect_report(
            routed_board,
            board_id="fixture",
            protocol="zero-touch",
            run_kicad_cli=False,
            backend=_cpp_backend(),
            route_exit_code=0,
            route_output_exists=True,
            pre_route_path=pre_route_path,
        )
        assert report.pre_route_completion is not None
        # Pre-route board: only SIG1's 1 connection is routed.
        assert report.pre_route_completion.connections_routed == 1
        assert report.completion.connections_routed == 2
        assert report.newly_routed_connections == 1

    def test_fallback_input_note_never_looks_like_success(self, routed_board: Path) -> None:
        report = collect_report(
            routed_board,
            board_id="fixture",
            protocol="zero-touch",
            run_kicad_cli=False,
            backend=_cpp_backend(),
            route_exit_code=1,
            route_output_exists=False,
        )
        assert report.route_outcome is not None
        assert report.route_outcome.outcome == ROUTE_OUTCOME_FAILED
        assert report.route_outcome.artifact_source == ARTIFACT_SOURCE_FALLBACK_INPUT


# ---------------------------------------------------------------------------
# Strict gates: kct check + the mandatory kicad-cli cross-gate
# ---------------------------------------------------------------------------


class TestStrictGates:
    def test_kct_check_runs_and_summarizes(self, routed_board: Path) -> None:
        summary = run_kct_check(routed_board, layers=2)
        assert summary.ran is True
        assert isinstance(summary.error_count, int)
        assert isinstance(summary.warning_count, int)
        assert summary.passed == (summary.error_count == 0)

    def test_kct_check_failure_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        bogus = tmp_path / "missing.kicad_pcb"
        summary = run_kct_check(bogus)
        assert summary.ran is False
        assert summary.note is not None
        # "we could not check" must never look like "clean".
        assert summary.passed is None
        assert summary.error_count is None

    def test_kicad_cli_drc_not_run_reports_null_not_zero(
        self, routed_board: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kicad_tools.drc import geometric

        class _Skipped:
            ran = False
            note = "kicad-cli not found; geometric DRC skipped"
            error_count = 0
            by_type: dict[str, int] = {}

        monkeypatch.setattr(geometric, "run_geometric_drc", lambda *a, **k: _Skipped())
        summary = run_kicad_cli_drc(routed_board)
        assert summary.ran is False
        assert summary.violation_count is None
        assert "kicad-cli not found" in (summary.note or "")

    def test_kicad_cli_drc_reports_violation_count(
        self, routed_board: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kicad_tools.drc import geometric

        class _Ran:
            ran = True
            note = None
            error_count = 4
            by_type = {"clearance": 3, "shorting_items": 1}

        monkeypatch.setattr(geometric, "run_geometric_drc", lambda *a, **k: _Ran())
        summary = run_kicad_cli_drc(routed_board)
        assert summary.ran is True
        assert summary.violation_count == 4
        assert summary.by_type["shorting_items"] == 1


# ---------------------------------------------------------------------------
# Diff-pair completion, only where pairs are defined
# ---------------------------------------------------------------------------


class TestDiffPairs:
    def test_none_when_board_defines_no_pairs(self, routed_board: Path) -> None:
        assert measure_diff_pairs(routed_board) is None

    def test_auto_detected_pair_completion(self, diff_pair_board: Path) -> None:
        pairs = measure_diff_pairs(diff_pair_board)
        assert pairs is not None
        assert pairs.pairs_total == 1
        # USB_P is routed, USB_N is not: the pair is NOT complete.
        assert pairs.pairs_complete == 0
        row = pairs.pairs[0]
        assert row["positive_complete"] is True
        assert row["negative_complete"] is False
        assert row["complete"] is False

    def test_explicit_pairs_override_detection(self, diff_pair_board: Path) -> None:
        pairs = measure_diff_pairs(diff_pair_board, pairs=[("USB_P", "USB_N")])
        assert pairs is not None
        assert pairs.pairs_total == 1
        assert pairs.completion_pct == 0.0


# ---------------------------------------------------------------------------
# The report: JSON schema contract
# ---------------------------------------------------------------------------


def _report(routed_board: Path, **overrides) -> BenchmarkReport:
    kwargs: dict = {
        "board_id": "fixture",
        "protocol": "zero-touch",
        "board_commit": "deadbee",
        "board_source": "https://example.invalid/board",
        "wall_clock_s": 12.5,
        "layers": 2,
        "run_kicad_cli": False,
        "backend": _cpp_backend(),
    }
    kwargs.update(overrides)
    return collect_report(routed_board, **kwargs)


class TestBenchmarkReport:
    def test_top_level_schema_fields(self, routed_board: Path) -> None:
        data = _report(routed_board).to_dict()
        assert data["$schema"] == SCHEMA_URL
        assert data["schema_version"] == SCHEMA_VERSION
        assert set(data) == {
            "$schema",
            "schema_version",
            "generated_at",
            "board_id",
            "board_commit",
            "board_source",
            "board_file",
            "protocol",
            "tool_commit",
            "route_outcome",
            "completion",
            "pre_route_completion",
            "newly_routed_connections",
            "copper",
            "timing",
            "backend",
            "kct_check",
            "kicad_cli_drc",
            "diff_pairs",
            "notes",
        }

    def test_identity_fields_round_trip(self, routed_board: Path) -> None:
        data = _report(routed_board, notes=["annotated"]).to_dict()
        assert data["board_id"] == "fixture"
        assert data["board_commit"] == "deadbee"
        assert data["protocol"] == "zero-touch"
        assert data["board_file"] == "routed.kicad_pcb"
        assert data["notes"] == ["annotated"]

    def test_headline_metrics_match_the_fixture(self, routed_board: Path) -> None:
        data = _report(routed_board).to_dict()
        assert data["completion"]["connections_routed"] == 2
        assert data["completion"]["connections_total"] == 3
        assert data["copper"]["via_count"] == 1
        assert data["copper"]["wirelength_mm"] == round(EXPECTED_WIRELENGTH_MM, 2)
        assert data["timing"]["wall_clock_s"] == pytest.approx(12.5)
        assert data["backend"]["backend"] == "cpp"

    def test_timing_refused_when_backend_is_python(self, routed_board: Path) -> None:
        data = _report(routed_board, backend=_python_backend()).to_dict()
        assert data["timing"]["valid"] is False
        assert data["timing"]["wall_clock_s"] is None
        assert data["backend"]["available"] is False

    def test_skipped_cross_gate_is_recorded_explicitly(self, routed_board: Path) -> None:
        data = _report(routed_board).to_dict()
        assert data["kicad_cli_drc"]["ran"] is False
        assert data["kicad_cli_drc"]["violation_count"] is None
        assert "skipped by caller" in data["kicad_cli_drc"]["note"]

    def test_diff_pairs_null_when_board_defines_none(self, routed_board: Path) -> None:
        assert _report(routed_board).to_dict()["diff_pairs"] is None

    def test_to_json_is_valid_json(self, routed_board: Path) -> None:
        parsed = json.loads(_report(routed_board).to_json())
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_write_json_creates_parents(self, routed_board: Path, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir" / "report.json"
        written = _report(routed_board).write_json(out)
        assert written == out
        assert json.loads(out.read_text())["board_id"] == "fixture"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _synthetic_report(**overrides) -> BenchmarkReport:
    kwargs: dict = {
        "board_id": "strf",
        "protocol": "zero-touch",
        "board_commit": "a1b2c3d",
        "board_source": "https://example.invalid/strf",
        "completion": CompletionMetrics(
            connections_routed=98,
            connections_total=98,
            nets_total=42,
            nets_complete=42,
            nets_incomplete=0,
            nets_unrouted=0,
            nets_blocking_incomplete=0,
        ),
        "copper": CopperMetrics(
            via_count=68,
            wirelength_mm=1182.90,
            segment_count=731,
            arc_count=0,
        ),
        "timing": TimingMetrics(wall_clock_s=142.7, valid=True),
        "backend": _cpp_backend(),
        "kct_check": KctCheckSummary(ran=True, passed=True, error_count=0, warning_count=3),
        "kicad_cli_drc": KicadCliDrcSummary(ran=True, violation_count=0),
        "tool_commit": "9f3c21b",
    }
    kwargs.update(overrides)
    return BenchmarkReport(**kwargs)


class TestRenderMarkdown:
    def test_table_row_carries_numerator_and_denominator(self) -> None:
        text = render_markdown([_synthetic_report()])
        assert "| strf | zero-touch | 100.0% | 98 of 98 | 68 | 1182.90 |" in text
        assert "142.7 s" in text

    def test_refused_timing_renders_as_refused_with_footnote(self) -> None:
        report = _synthetic_report(
            timing=TimingMetrics(
                wall_clock_s=None,
                valid=False,
                refusal_reason="timing refused: the C++ router backend was not active",
            ),
            backend=_python_backend(),
        )
        text = render_markdown([report])
        assert "| refused |" in text
        assert "**Timing refusals**" in text
        assert "C++ router backend was not active" in text

    def test_unrun_cross_gate_is_not_rendered_as_clean(self) -> None:
        report = _synthetic_report(
            kicad_cli_drc=KicadCliDrcSummary(ran=False, note="kicad-cli not found"),
        )
        text = render_markdown([report])
        assert "| not run |" in text
        assert "DRC status is UNKNOWN, not clean" in text

    def test_notes_and_reproduction_are_rendered(self) -> None:
        report = _synthetic_report(notes=["3 nets left unrouted"])
        text = render_markdown([report])
        assert "3 nets left unrouted" in text
        assert "**Reproduction**" in text
        assert "https://example.invalid/strf @ `a1b2c3d`" in text

    def test_empty_report_set(self) -> None:
        assert "_No benchmark reports._" in render_markdown([])

    def test_single_report_wrapper_titles_by_board(self) -> None:
        assert render_report_markdown(_synthetic_report()).startswith("# Benchmark: strf")

    def test_real_report_renders(self, routed_board: Path) -> None:
        text = render_markdown([_report(routed_board)])
        assert "| fixture | zero-touch | 66.7% | 2 of 3 | 1 |" in text

    def test_completed_outcome_and_router_output_artifact_are_rendered(self) -> None:
        report = _synthetic_report(
            route_outcome=RouteOutcome(
                outcome=ROUTE_OUTCOME_COMPLETED,
                artifact_source=ARTIFACT_SOURCE_ROUTER_OUTPUT,
                exit_code=0,
            )
        )
        text = render_markdown([report])
        assert "| completed | router output |" in text
        # A completed attempt's timing is bare seconds -- no phase suffix.
        assert "142.7 s |" in text

    def test_stopped_before_routing_gets_fallback_footnote_and_timing_label(self) -> None:
        report = _synthetic_report(
            route_outcome=RouteOutcome(
                outcome=ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING,
                artifact_source=ARTIFACT_SOURCE_FALLBACK_INPUT,
                exit_code=9,
                reason="census gate refused before routing",
            ),
            timing=TimingMetrics(
                wall_clock_s=4.4, valid=True, measured_phase=ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING
            ),
        )
        text = render_markdown([report])
        assert "| stopped before routing | fallback input |" in text
        assert "4.4 s (stopped before routing)" in text
        assert "**Measured artifact is fallback input, not router output**" in text
        assert "census gate refused before routing" in text
        assert "Runtime measures the routing attempt" in text

    def test_legacy_report_without_route_outcome_is_flagged_not_success(self) -> None:
        report = _synthetic_report(route_outcome=None)
        text = render_markdown([report])
        assert "| unknown (legacy) | unknown |" in text
        assert "**Legacy reports (outcome/artifact provenance not tracked)**" in text
        assert "treat as unknown, never as success" in text


# ---------------------------------------------------------------------------
# Both committed legacy records (Aug 25 2026, pre-#5280) -- issue #5280's
# explicit "historical reports remain readable and explicitly historical"
# acceptance criterion.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_legacy_json(rel_path: str) -> dict:
    return json.loads((_repo_root() / rel_path).read_text())


def _reconstruct_legacy_report(data: dict) -> BenchmarkReport:
    """Rebuild a :class:`BenchmarkReport` from a committed schema-v1 JSON dict.

    Mirrors what a hypothetical "load a report back into Python" consumer
    would have to do -- ``route_outcome`` / ``pre_route_completion`` are
    deliberately NOT reconstructed here because the committed file predates
    them; this is the load-bearing point of the test.
    """
    completion_data = {k: v for k, v in data["completion"].items() if k != "completion_pct"}
    diff_pairs_data = data.get("diff_pairs")
    diff_pairs = None
    if diff_pairs_data is not None:
        diff_pairs = DiffPairCompletion(
            pairs_total=diff_pairs_data["pairs_total"],
            pairs_complete=diff_pairs_data["pairs_complete"],
            pairs=diff_pairs_data.get("pairs", []),
        )
    return BenchmarkReport(
        board_id=data["board_id"],
        protocol=data["protocol"],
        board_commit=data.get("board_commit"),
        board_source=data.get("board_source"),
        board_file=data.get("board_file"),
        tool_commit=data.get("tool_commit", "unknown"),
        generated_at=data.get("generated_at", ""),
        completion=CompletionMetrics(**completion_data),
        copper=CopperMetrics(**data["copper"]),
        timing=TimingMetrics(**data["timing"]),
        backend=BackendInfo(**data["backend"]),
        kct_check=KctCheckSummary(**data["kct_check"]),
        kicad_cli_drc=KicadCliDrcSummary(**data["kicad_cli_drc"]),
        diff_pairs=diff_pairs,
        notes=data.get("notes", []),
        # route_outcome / pre_route_completion intentionally omitted.
    )


LEGACY_REPORT_FILES = [
    "benchmarks/external/results/pocketbeagle.zero-touch.json",
    "benchmarks/external/results/beagleconnect_freedom.zero-touch.json",
]


class TestCommittedLegacyReports:
    """Both real committed reports (2026-08-25, generated by tool commit
    ``636fd368``) predate route-outcome/artifact-provenance tracking. Per
    this issue's "do not rewrite historical measurements" constraint they
    are never edited -- so the contract instead requires every consumer to
    treat their MISSING ``route_outcome``/``pre_route_completion`` as
    unknown, never as an implicit success.
    """

    @pytest.mark.parametrize("rel_path", LEGACY_REPORT_FILES)
    def test_legacy_json_on_disk_predates_outcome_tracking(self, rel_path: str) -> None:
        data = _load_legacy_json(rel_path)
        assert data["schema_version"] == SCHEMA_VERSION
        assert "route_outcome" not in data
        assert "pre_route_completion" not in data
        # Both boards actually refused to route zero-touch (see
        # benchmarks/external/results/README.md) -- captured via a
        # non-zero exit code note and the "no output file" note, which
        # predates this issue's structured route_outcome field.
        assert any("router exit code:" in n for n in data["notes"])
        assert any("no output file" in n for n in data["notes"])

    @pytest.mark.parametrize("rel_path", LEGACY_REPORT_FILES)
    def test_legacy_report_reconstructs_and_renders_as_unknown(self, rel_path: str) -> None:
        data = _load_legacy_json(rel_path)
        report = _reconstruct_legacy_report(data)
        assert report.route_outcome is None
        assert report.pre_route_completion is None
        assert report.newly_routed_connections is None

        text = render_markdown([report])
        assert "| unknown (legacy) | unknown |" in text
        assert "**Legacy reports (outcome/artifact provenance not tracked)**" in text
        # The real measured completion% must be rendered as-is -- NOT the
        # "0% complete" claim the original (pre-#5280) note text asserted,
        # which was itself wrong (see the issue body): both boards'
        # completion_pct is in the 30s, not 0.
        expected_pct = f"{data['completion']['completion_pct']:.1f}%"
        assert expected_pct in text
        assert "| 0.0% |" not in text
