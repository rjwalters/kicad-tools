"""Tests for the Phase-1 current-sense / analog layout lint (#4328).

Covers the analyzer (parallel-run + gap, FAIL/PASS rule, classification,
overrides, edge cases) and the CLI subcommand (text/json output, exit codes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.analysis import CurrentSenseAnalyzer, CurrentSenseResult
from kicad_tools.analysis.current_sense import (
    DEFAULT_MAX_PARALLEL_MM,
    DEFAULT_MIN_GAP_MM,
)
from kicad_tools.cli.analyze_cmd import main as analyze_main
from kicad_tools.schema.pcb import PCB

# ---------------------------------------------------------------------------
# Synthetic board fixtures
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_rect (start 0 0) (end 50 50) (stroke (width 0.1)) (layer "Edge.Cuts"))
"""

# ISENSE (-> ANALOG) runs parallel to PHASE_A (-> HIGH_CURRENT_SIGNAL) for 20mm.
# Center-to-center 0.3mm, widths 0.2mm each => edge gap 0.1mm. 20 >= 10 AND
# 0.1 <= 0.5 => FAIL.
FAIL_PCB = (
    _HEADER
    + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (net 3 "GND")
  (segment (start 0 0) (end 20 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 0.3) (end 20 0.3) (width 0.2) (layer "F.Cu") (net 2))
)
"""
)

# Same nets but PHASE_A is 5mm away => edge gap 4.8mm > 0.5mm => PASS.
PASS_PCB = (
    _HEADER
    + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (net 3 "GND")
  (segment (start 0 0) (end 20 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 5.0) (end 20 5.0) (width 0.2) (layer "F.Cu") (net 2))
)
"""
)

# No sense nets at all: only high-current + ground.
NO_SENSE_PCB = (
    _HEADER
    + """
  (net 0 "")
  (net 1 "PHASE_A")
  (net 2 "GND")
  (segment (start 0 0) (end 20 0) (width 0.2) (layer "F.Cu") (net 1))
)
"""
)

# Sense net with a high-current neighbour on a DIFFERENT layer only => PASS
# (Phase 1 is same-layer only).
DIFFERENT_LAYER_PCB = (
    _HEADER
    + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (segment (start 0 0) (end 20 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 0.3) (end 20 0.3) (width 0.2) (layer "B.Cu") (net 2))
)
"""
)

# Crossing (perpendicular) segments: not a parallel run => PASS, no blocker.
CROSSING_PCB = (
    _HEADER
    + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (segment (start 0 10) (end 20 10) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 10 0) (end 10 20) (width 0.2) (layer "F.Cu") (net 2))
)
"""
)

# The rev-C scenario: neither name is auto-classified (OC_TRIP_N is not ANALOG,
# GATE_NEG_A is not HIGH_CURRENT). They must be tagged via explicit overrides.
OVERRIDE_PCB = (
    _HEADER
    + """
  (net 0 "")
  (net 1 "/OC_TRIP_N")
  (net 2 "/GATE_NEG_A")
  (segment (start 0 0) (end 20 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 0.3) (end 20 0.3) (width 0.2) (layer "F.Cu") (net 2))
)
"""
)


def _write(tmp_path: Path, text: str, name: str = "board.kicad_pcb") -> Path:
    pcb_file = tmp_path / name
    pcb_file.write_text(text)
    return pcb_file


def _load(tmp_path: Path, text: str) -> PCB:
    return PCB.load(str(_write(tmp_path, text)))


# ---------------------------------------------------------------------------
# Analyzer: geometry + FAIL/PASS rule
# ---------------------------------------------------------------------------


class TestAnalyzerGeometry:
    def test_parallel_close_fails(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, FAIL_PCB)
        results = CurrentSenseAnalyzer().analyze(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.sense_net == "ISENSE"
        assert r.nearest_hicur_net == "PHASE_A"
        assert r.layer == "F.Cu"
        assert r.max_parallel_mm == pytest.approx(20.0, abs=0.01)
        assert r.min_gap_mm == pytest.approx(0.1, abs=0.001)
        assert r.status == "FAIL"
        # margin = gap - threshold = 0.1 - 0.5 = -0.4 (negative => violated)
        assert r.margin_mm == pytest.approx(-0.4, abs=0.001)

    def test_well_separated_passes(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, PASS_PCB)
        results = CurrentSenseAnalyzer().analyze(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.sense_net == "ISENSE"
        # Still the nearest blocker, but gap keeps it PASS.
        assert r.nearest_hicur_net == "PHASE_A"
        assert r.max_parallel_mm == pytest.approx(20.0, abs=0.01)
        assert r.min_gap_mm == pytest.approx(4.8, abs=0.001)
        assert r.status == "PASS"
        assert r.margin_mm == pytest.approx(4.3, abs=0.001)

    def test_short_run_at_close_gap_passes(self, tmp_path: Path) -> None:
        # Close (0.1mm) but only a 3mm parallel run => PASS under the AND rule.
        text = (
            _HEADER
            + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (segment (start 0 0) (end 3 0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 0.3) (end 3 0.3) (width 0.2) (layer "F.Cu") (net 2))
)
"""
        )
        pcb = _load(tmp_path, text)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.min_gap_mm == pytest.approx(0.1, abs=0.001)
        assert r.max_parallel_mm == pytest.approx(3.0, abs=0.01)
        assert r.status == "PASS"  # short run: not enough coupling length

    def test_custom_thresholds(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, PASS_PCB)
        # Loosen the gap threshold above 4.8mm so the 20mm run now FAILs.
        r = CurrentSenseAnalyzer(max_parallel_mm=5.0, min_gap_mm=5.0).analyze(pcb)[0]
        assert r.status == "FAIL"

    def test_long_run_farther_blocker_fails_not_just_nearest(self, tmp_path: Path) -> None:
        # Regression for #4336 (the false-PASS the judge identified). Two
        # same-layer high-current blockers:
        #   PHASE_A: gap 0.10mm, 3mm run   -> nearest-by-gap, PASSes alone.
        #   PHASE_B: gap 0.15mm, 20mm run  -> FAILs the AND rule alone.
        # Phase-1 collapsed to the nearest-by-gap blocker (PHASE_A) and thus
        # falsely PASSed. The net must now FAIL and point at PHASE_B.
        text = (
            _HEADER
            + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (net 3 "PHASE_B")
  (segment (start 0 1.0) (end 20 1.0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 1.3) (end 3 1.3) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 0 0.65) (end 20 0.65) (width 0.2) (layer "F.Cu") (net 3))
)
"""
        )
        pcb = _load(tmp_path, text)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.sense_net == "ISENSE"
        assert r.status == "FAIL"
        # Worst offender is the long-run (but slightly farther) blocker.
        assert r.nearest_hicur_net == "PHASE_B"
        assert r.max_parallel_mm == pytest.approx(20.0, abs=0.01)
        assert r.min_gap_mm == pytest.approx(0.15, abs=0.001)
        assert r.layer == "F.Cu"
        # margin = 0.15 - 0.5 = -0.35 (violated).
        assert r.margin_mm == pytest.approx(-0.35, abs=0.001)

    def test_nearest_blocker_also_long_still_points_at_it(self, tmp_path: Path) -> None:
        # No regression: when the nearest-by-gap blocker itself has a long run,
        # it is (the/a) failing offender and remains the reported net.
        pcb = _load(tmp_path, FAIL_PCB)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.status == "FAIL"
        assert r.nearest_hicur_net == "PHASE_A"
        assert r.max_parallel_mm == pytest.approx(20.0, abs=0.01)

    def test_worst_offender_prefers_larger_parallel_run(self, tmp_path: Path) -> None:
        # Both blockers FAIL the AND rule; the worst offender is the one with
        # the larger parallel run (strongest coupling), even though the other
        # is nearer by gap.
        text = (
            _HEADER
            + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (net 3 "PHASE_B")
  (segment (start 0 1.0) (end 30 1.0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 1.3) (end 15 1.3) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 0 0.65) (end 30 0.65) (width 0.2) (layer "F.Cu") (net 3))
)
"""
        )
        pcb = _load(tmp_path, text)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.status == "FAIL"
        assert r.nearest_hicur_net == "PHASE_B"  # 30mm run > PHASE_A's 15mm
        assert r.max_parallel_mm == pytest.approx(30.0, abs=0.01)

    def test_worst_offender_tiebreak_smallest_gap(self, tmp_path: Path) -> None:
        # Both blockers FAIL with equal parallel runs; tiebreak selects the
        # smaller-gap blocker.
        text = (
            _HEADER
            + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (net 3 "PHASE_B")
  (segment (start 0 1.0) (end 20 1.0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 1.3) (end 20 1.3) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 0 0.65) (end 20 0.65) (width 0.2) (layer "F.Cu") (net 3))
)
"""
        )
        pcb = _load(tmp_path, text)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.status == "FAIL"
        assert r.nearest_hicur_net == "PHASE_A"  # gap 0.10 < PHASE_B's 0.15
        assert r.min_gap_mm == pytest.approx(0.1, abs=0.001)

    def test_multi_blocker_all_pass_reports_nearest_by_gap(self, tmp_path: Path) -> None:
        # When NO blocker fails, the row is byte-identical to phase 1: it
        # reports the nearest-by-gap blocker (PHASE_A here), not the other.
        text = (
            _HEADER
            + """
  (net 0 "")
  (net 1 "ISENSE")
  (net 2 "PHASE_A")
  (net 3 "PHASE_B")
  (segment (start 0 1.0) (end 20 1.0) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 0 1.3) (end 3 1.3) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 0 0.65) (end 5 0.65) (width 0.2) (layer "F.Cu") (net 3))
)
"""
        )
        pcb = _load(tmp_path, text)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.status == "PASS"
        # Nearest-by-gap wins on PASS (0.10 < 0.15).
        assert r.nearest_hicur_net == "PHASE_A"
        assert r.min_gap_mm == pytest.approx(0.1, abs=0.001)
        assert r.max_parallel_mm == pytest.approx(3.0, abs=0.01)
        # The whole census row matches the phase-1 nearest-by-gap contract.
        assert r.to_dict() == {
            "sense_net": "ISENSE",
            "nearest_hicur_net": "PHASE_A",
            "layer": "F.Cu",
            "max_parallel_mm": 3.0,
            "min_gap_mm": 0.1,
            "status": "PASS",
            "margin": -0.4,
        }


# ---------------------------------------------------------------------------
# Classification + overrides
# ---------------------------------------------------------------------------


class TestClassification:
    def test_auto_classification(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, FAIL_PCB)
        sense, hicur = CurrentSenseAnalyzer().classify_nets(pcb)
        assert "ISENSE" in sense  # SENSE -> ANALOG
        assert "PHASE_A" in hicur  # PHASE_A -> HIGH_CURRENT_SIGNAL
        assert "ISENSE" not in hicur

    def test_override_tags_rev_c_nets(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, OVERRIDE_PCB)
        # Without overrides the rev-C nets are not recognised.
        assert CurrentSenseAnalyzer().analyze(pcb) == []
        # With overrides, /OC_TRIP_N becomes a sense net whose nearest blocker
        # is /GATE_NEG_A and it FAILs.
        analyzer = CurrentSenseAnalyzer(
            sense_nets=["/OC_TRIP_N"],
            hicur_nets=["/GATE_NEG_A"],
        )
        results = analyzer.analyze(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.sense_net == "/OC_TRIP_N"
        assert r.nearest_hicur_net == "/GATE_NEG_A"
        assert r.status == "FAIL"

    def test_net_in_both_sets_is_sense_only(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, FAIL_PCB)
        analyzer = CurrentSenseAnalyzer(sense_nets=["PHASE_A"])
        sense, hicur = analyzer.classify_nets(pcb)
        assert "PHASE_A" in sense
        assert "PHASE_A" not in hicur


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_no_sense_nets_returns_empty(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, NO_SENSE_PCB)
        assert CurrentSenseAnalyzer().analyze(pcb) == []

    def test_different_layer_not_coupled(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, DIFFERENT_LAYER_PCB)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.nearest_hicur_net is None
        assert r.min_gap_mm is None
        assert r.max_parallel_mm == 0.0
        assert r.status == "PASS"
        assert r.margin_mm is None

    def test_crossing_segments_not_parallel(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, CROSSING_PCB)
        r = CurrentSenseAnalyzer().analyze(pcb)[0]
        assert r.nearest_hicur_net is None
        assert r.status == "PASS"

    def test_defaults(self) -> None:
        a = CurrentSenseAnalyzer()
        assert a.max_parallel_mm == DEFAULT_MAX_PARALLEL_MM
        assert a.min_gap_mm == DEFAULT_MIN_GAP_MM


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


class TestResultDict:
    def test_to_dict_keys(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, FAIL_PCB)
        d = CurrentSenseAnalyzer().analyze(pcb)[0].to_dict()
        assert set(d) == {
            "sense_net",
            "nearest_hicur_net",
            "layer",
            "max_parallel_mm",
            "min_gap_mm",
            "status",
            "margin",
        }
        assert d["status"] == "FAIL"

    def test_to_dict_no_neighbor_is_json_safe(self, tmp_path: Path) -> None:
        pcb = _load(tmp_path, DIFFERENT_LAYER_PCB)
        d = CurrentSenseAnalyzer().analyze(pcb)[0].to_dict()
        # No inf/NaN that would break json.dumps.
        assert d["min_gap_mm"] is None
        assert d["margin"] is None
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_fail_exit_code(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, FAIL_PCB)
        rc = analyze_main(["current-sense", str(pcb)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "ISENSE" in out
        assert "FAIL" in out

    def test_cli_pass_exit_code(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, PASS_PCB)
        rc = analyze_main(["current-sense", str(pcb)])
        assert rc == 0

    def test_cli_no_sense_clean_exit(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, NO_SENSE_PCB)
        rc = analyze_main(["current-sense", str(pcb)])
        assert rc == 0
        assert "No sense nets" in capsys.readouterr().out

    def test_cli_json_shape(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, FAIL_PCB)
        rc = analyze_main(["current-sense", str(pcb), "--format", "json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"census", "thresholds", "summary"}
        assert payload["summary"] == {"total": 1, "fail": 1, "pass": 0}
        assert payload["thresholds"]["max_parallel_mm"] == 10.0
        assert payload["thresholds"]["min_gap_mm"] == 0.5
        row = payload["census"][0]
        assert row["sense_net"] == "ISENSE"
        assert row["nearest_hicur_net"] == "PHASE_A"
        assert row["status"] == "FAIL"

    def test_cli_overrides(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, OVERRIDE_PCB)
        rc = analyze_main(
            [
                "current-sense",
                str(pcb),
                "--sense-net",
                "/OC_TRIP_N",
                "--hicur-net",
                "/GATE_NEG_A",
                "--format",
                "json",
            ]
        )
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        row = payload["census"][0]
        assert row["sense_net"] == "/OC_TRIP_N"
        assert row["nearest_hicur_net"] == "/GATE_NEG_A"

    def test_cli_custom_thresholds(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, PASS_PCB)
        rc = analyze_main(["current-sense", str(pcb), "--max-parallel", "5", "--min-gap", "5"])
        assert rc == 1

    def test_cli_missing_file(self, tmp_path: Path) -> None:
        rc = analyze_main(["current-sense", str(tmp_path / "nope.kicad_pcb")])
        assert rc == 1

    def test_cli_quiet(self, tmp_path: Path, capsys) -> None:
        pcb = _write(tmp_path, NO_SENSE_PCB)
        rc = analyze_main(["current-sense", str(pcb), "--quiet"])
        assert rc == 0
        # Quiet suppresses the informational "no sense nets" chrome.
        assert capsys.readouterr().out.strip() == ""


def test_result_dataclass_direct() -> None:
    r = CurrentSenseResult(
        sense_net="S",
        nearest_hicur_net=None,
        layer=None,
        max_parallel_mm=0.0,
        min_gap_mm=None,
        status="PASS",
        margin_mm=None,
    )
    assert r.to_dict()["status"] == "PASS"
