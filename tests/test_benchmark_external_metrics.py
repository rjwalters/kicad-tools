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
    SCHEMA_URL,
    SCHEMA_VERSION,
    BackendInfo,
    BenchmarkReport,
    CompletionMetrics,
    CopperMetrics,
    KctCheckSummary,
    KicadCliDrcSummary,
    TimingMetrics,
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
            "completion",
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
