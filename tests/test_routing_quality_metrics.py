"""Tests for the advisory routing-quality metrics (issue #4623).

Two layers:

- Unit tests for the pure ``kicad_tools.analysis.routing_quality`` module
  on synthetic segment sets with known geometry (45°-only, staircase,
  zero-length, fragments, off-axis, empty board).
- CLI tests asserting the advisory wire-in in ``kct check``: the stanza
  appears in table/summary output, ``routing_quality`` appears in
  ``--format json`` and ``--output`` envelopes, is **absent** under
  ``--drc-only`` (legacy contract, #3750), and never affects the exit
  code -- including under ``--strict`` and when the computation crashes.

Deliberately a standalone file (not an extension of
``tests/test_check_cmd_coverage.py``) per the issue's parallel-wave
guidance.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_tools.analysis.routing_quality import (
    FRAGMENT_LENGTH_MM,
    STAIRCASE_STEP_MM,
    RoutingQualityMetrics,
    compute_routing_quality,
)
from kicad_tools.schema.pcb import Segment

# ---------------------------------------------------------------------------
# Unit-test scaffolding: real Segment dataclasses on a minimal PCB stub
# (compute_routing_quality only reads ``pcb.segments``).
# ---------------------------------------------------------------------------


class _StubPCB:
    def __init__(self, segments: list[Segment]):
        self.segments = segments


def _seg(
    start: tuple[float, float],
    end: tuple[float, float],
    net: int = 1,
    layer: str = "F.Cu",
    width: float = 0.2,
) -> Segment:
    return Segment(
        start=start,
        end=end,
        width=width,
        layer=layer,
        net_number=net,
        net_name=f"NET{net}",
    )


class TestComputeRoutingQuality:
    def test_empty_board_is_graceful(self):
        m = compute_routing_quality(_StubPCB([]))
        assert m.total_segments == 0
        assert m.nets_with_copper == 0
        assert m.segments_per_net == 0.0
        assert m.zero_length_count == 0
        assert m.median_length_mm == 0.0
        assert m.fragment_count == 0
        assert m.fragment_fraction == 0.0
        assert m.orthogonal_count == 0
        assert m.diagonal_45_count == 0
        assert m.off_axis_count == 0
        assert m.staircase_step_count == 0
        assert m.staircase_fraction == 0.0

    def test_pure_45_route(self):
        segs = [
            _seg((0.0, 0.0), (1.0, 1.0)),
            _seg((1.0, 1.0), (2.0, 2.0)),
            _seg((2.0, 2.0), (3.0, 1.0)),  # falling 45° also counts
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.total_segments == 3
        assert m.diagonal_45_count == 3
        assert m.orthogonal_count == 0
        assert m.off_axis_count == 0
        assert m.staircase_step_count == 0
        assert m.zero_length_count == 0
        assert m.fragment_count == 0
        assert m.median_length_mm == pytest.approx(math.sqrt(2.0))
        assert m.nets_with_copper == 1
        assert m.segments_per_net == pytest.approx(3.0)

    def test_staircase_route_counts_every_step(self):
        # Four alternating H/V legs of 0.4 mm, chained end-to-end on the
        # same net and layer -- every one is a staircase step.
        segs = [
            _seg((0.0, 0.0), (0.4, 0.0)),
            _seg((0.4, 0.0), (0.4, 0.4)),
            _seg((0.4, 0.4), (0.8, 0.4)),
            _seg((0.8, 0.4), (0.8, 0.8)),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.orthogonal_count == 4
        assert m.staircase_step_count == 4
        assert m.staircase_fraction == pytest.approx(1.0)
        # 0.4 mm legs are above the fragment threshold
        assert m.fragment_count == 0
        assert m.diagonal_45_count == 0
        assert m.off_axis_count == 0

    def test_staircase_requires_same_layer(self):
        segs = [
            _seg((0.0, 0.0), (0.4, 0.0), layer="F.Cu"),
            _seg((0.4, 0.0), (0.4, 0.4), layer="B.Cu"),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.staircase_step_count == 0

    def test_staircase_requires_same_net(self):
        segs = [
            _seg((0.0, 0.0), (0.4, 0.0), net=1),
            _seg((0.4, 0.0), (0.4, 0.4), net=2),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.staircase_step_count == 0

    def test_staircase_requires_both_legs_short(self):
        # Short H meeting a LONG perpendicular V: neither is a step.
        segs = [
            _seg((0.0, 0.0), (0.4, 0.0)),
            _seg((0.4, 0.0), (0.4, 5.0)),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.staircase_step_count == 0

    def test_staircase_requires_perpendicular_partner(self):
        # Two collinear short H segments sharing an endpoint: not a step.
        segs = [
            _seg((0.0, 0.0), (0.4, 0.0)),
            _seg((0.4, 0.0), (0.8, 0.0)),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.staircase_step_count == 0

    def test_zero_length_segments_counted_and_excluded_from_stats(self):
        segs = [
            _seg((1.0, 1.0), (1.0, 1.0)),  # exactly zero
            _seg((2.0, 2.0), (2.0 + 1e-7, 2.0)),  # within the 1e-6 epsilon
            _seg((0.0, 0.0), (1.0, 0.0)),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.total_segments == 3
        assert m.zero_length_count == 2
        # Stats over the single real segment only
        assert m.median_length_mm == pytest.approx(1.0)
        assert m.orthogonal_count == 1
        assert m.fragment_count == 0
        assert m.fragment_fraction == 0.0

    def test_fragments(self):
        segs = [
            _seg((0.0, 0.0), (0.1, 0.0)),  # 0.1 mm < FRAGMENT_LENGTH_MM
            _seg((0.0, 1.0), (1.0, 1.0)),
            _seg((0.0, 2.0), (1.0, 2.0)),
            _seg((0.0, 3.0), (1.0, 3.0)),
        ]
        assert FRAGMENT_LENGTH_MM > 0.1
        m = compute_routing_quality(_StubPCB(segs))
        assert m.fragment_count == 1
        assert m.fragment_fraction == pytest.approx(0.25)

    def test_off_axis(self):
        segs = [
            _seg((0.0, 0.0), (1.0, 0.5)),  # ~26.6°: neither 0/90 nor 45
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.off_axis_count == 1
        assert m.orthogonal_count == 0
        assert m.diagonal_45_count == 0

    def test_angle_tolerance_boundaries(self):
        # 0.4° off horizontal -> still orthogonal; 1° off -> off-axis.
        near = math.tan(math.radians(0.4))
        far = math.tan(math.radians(1.0))
        m = compute_routing_quality(
            _StubPCB(
                [
                    _seg((0.0, 0.0), (10.0, 10.0 * near)),
                    _seg((0.0, 5.0), (10.0, 5.0 + 10.0 * far)),
                ]
            )
        )
        assert m.orthogonal_count == 1
        assert m.off_axis_count == 1

    def test_net_zero_excluded_from_nets_with_copper(self):
        segs = [
            _seg((0.0, 0.0), (1.0, 0.0), net=0),
            _seg((0.0, 1.0), (1.0, 1.0), net=3),
            _seg((0.0, 2.0), (1.0, 2.0), net=3),
        ]
        m = compute_routing_quality(_StubPCB(segs))
        assert m.nets_with_copper == 1
        assert m.total_segments == 3
        assert m.segments_per_net == pytest.approx(3.0)

    def test_only_net_zero_copper_has_no_division_by_zero(self):
        m = compute_routing_quality(_StubPCB([_seg((0.0, 0.0), (1.0, 0.0), net=0)]))
        assert m.nets_with_copper == 0
        assert m.segments_per_net == 0.0

    def test_to_dict_round_trip(self):
        m = compute_routing_quality(
            _StubPCB(
                [
                    _seg((0.0, 0.0), (0.4, 0.0)),
                    _seg((0.4, 0.0), (0.4, 0.4)),
                    _seg((5.0, 5.0), (5.0, 5.0)),
                ]
            )
        )
        d = m.to_dict()
        assert d == {
            "total_segments": 3,
            "nets_with_copper": 1,
            "segments_per_net": 3.0,
            "zero_length_count": 1,
            "median_length_mm": pytest.approx(0.4),
            "fragment_count": 0,
            "fragment_fraction": 0.0,
            "orthogonal_count": 2,
            "diagonal_45_count": 0,
            "off_axis_count": 0,
            "staircase_step_count": 2,
            "staircase_fraction": 1.0,
        }
        # The payload must be JSON-serializable as-is.
        json.dumps(d)

    def test_thresholds_are_the_pinned_constants(self):
        # Pinned in issue #4623 to match the #4615 measurement basis.
        assert FRAGMENT_LENGTH_MM == 0.25
        assert STAIRCASE_STEP_MM == 0.6


# ---------------------------------------------------------------------------
# CLI wire-in tests.  Board template mirrors tests/conftest.py DRC_CLEAN_PCB
# (designed to pass all DRC checks) with a parameterizable copper block far
# from the footprint and the board edge.
# ---------------------------------------------------------------------------

_PCB_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 125 125)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
{segments})
"""

# Four alternating H/V 0.4 mm legs: 100% staircase steps ("terrible" metrics).
_STAIRCASE_SEGMENTS = """  (segment (start 110 110) (end 110.4 110) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110.4 110) (end 110.4 110.4) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000021"))
  (segment (start 110.4 110.4) (end 110.8 110.4) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000022"))
  (segment (start 110.8 110.4) (end 110.8 110.8) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000023"))
"""

_CLEAN_45_SEGMENTS = """  (segment (start 110 110) (end 112 112) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
"""


@pytest.fixture
def staircase_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "staircase_4623.kicad_pcb"
    p.write_text(_PCB_TEMPLATE.format(segments=_STAIRCASE_SEGMENTS))
    return p


@pytest.fixture
def clean_45_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "clean45_4623.kicad_pcb"
    p.write_text(_PCB_TEMPLATE.format(segments=_CLEAN_45_SEGMENTS))
    return p


class TestCheckCmdAdvisoryWireIn:
    def test_table_output_has_stanza(self, staircase_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main([str(staircase_pcb), "--allow-incomplete"])
        out = capsys.readouterr().out
        assert result == 0
        assert "Routing quality (advisory):" in out
        assert "Staircase steps" in out
        assert "Zero-length:" in out

    def test_summary_output_has_stanza(self, staircase_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main([str(staircase_pcb), "--format", "summary", "--allow-incomplete"])
        out = capsys.readouterr().out
        assert result == 0
        assert "Routing quality (advisory):" in out

    def test_terrible_metrics_never_change_exit_code(
        self, staircase_pcb: Path, clean_45_pcb: Path, capsys
    ):
        """AC: advisory-only -- all-staircase copper still exits 0 when
        DRC/meta pass, with and without --strict."""
        from kicad_tools.cli.check_cmd import main

        assert main([str(clean_45_pcb), "--allow-incomplete"]) == 0
        assert main([str(staircase_pcb), "--allow-incomplete"]) == 0
        assert main([str(staircase_pcb), "--allow-incomplete", "--strict"]) == 0
        capsys.readouterr()

    def test_json_output_has_routing_quality(self, staircase_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main([str(staircase_pcb), "--format", "json", "--allow-incomplete"])
        data = json.loads(capsys.readouterr().out)
        assert result == 0
        rq = data["routing_quality"]
        assert rq["total_segments"] == 4
        assert rq["orthogonal_count"] == 4
        assert rq["staircase_step_count"] == 4
        assert rq["zero_length_count"] == 0
        assert rq["nets_with_copper"] == 1
        # Verdict fields are untouched by the advisory metrics
        assert data["summary"]["passed"] is True

    def test_output_file_has_routing_quality(self, staircase_pcb: Path, tmp_path: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        report = tmp_path / "report_4623.json"
        result = main([str(staircase_pcb), "--output", str(report), "--allow-incomplete"])
        capsys.readouterr()
        assert result == 0
        data = json.loads(report.read_text())
        assert data["routing_quality"]["staircase_step_count"] == 4

    def test_drc_only_stdout_has_no_stanza(self, staircase_pcb: Path, capsys):
        """AC: legacy --drc-only contract (#3750) -- no advisory stanza."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(staircase_pcb), "--drc-only"])
        out = capsys.readouterr().out
        assert result == 0
        assert "Routing quality" not in out

    def test_drc_only_json_omits_routing_quality(self, staircase_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main([str(staircase_pcb), "--format", "json", "--drc-only"])
        data = json.loads(capsys.readouterr().out)
        assert result == 0
        assert "routing_quality" not in data
        # Sanity: the sibling meta-mode key is omitted too (same convention)
        assert "meta_checks" not in data

    def test_drc_only_report_file_omits_routing_quality(
        self, staircase_pcb: Path, tmp_path: Path, capsys
    ):
        from kicad_tools.cli.check_cmd import main

        report = tmp_path / "report_drconly_4623.json"
        result = main([str(staircase_pcb), "--output", str(report), "--drc-only"])
        capsys.readouterr()
        assert result == 0
        data = json.loads(report.read_text())
        assert "routing_quality" not in data
        assert "meta_checks" not in data

    def test_metrics_crash_degrades_to_warning(self, staircase_pcb: Path, capsys, monkeypatch):
        """AC: a metrics crash degrades to a stderr warning, never a
        failed check."""
        from kicad_tools.cli import check_cmd

        def _boom(pcb):
            raise RuntimeError("synthetic metrics failure 4623")

        monkeypatch.setattr(check_cmd, "compute_routing_quality", _boom)
        result = check_cmd.main([str(staircase_pcb), "--allow-incomplete"])
        captured = capsys.readouterr()
        assert result == 0
        assert "Routing quality (advisory):" not in captured.out
        assert "routing-quality metrics unavailable" in captured.err

    def test_stanza_prints_after_meta_stanza(self, staircase_pcb: Path, capsys):
        """The advisory stanza follows the meta-check rollup in table mode."""
        from kicad_tools.cli.check_cmd import main

        main([str(staircase_pcb), "--allow-incomplete"])
        out = capsys.readouterr().out
        assert out.index("Overall:") < out.index("Routing quality (advisory):")

    def test_stanza_helper_formats_empty_board(self, capsys):
        """Zero-copper board: stanza renders with no division by zero."""
        from kicad_tools.cli.check_cmd import print_routing_quality_stanza

        metrics = RoutingQualityMetrics(
            total_segments=0,
            nets_with_copper=0,
            segments_per_net=0.0,
            zero_length_count=0,
            median_length_mm=0.0,
            fragment_count=0,
            fragment_fraction=0.0,
            orthogonal_count=0,
            diagonal_45_count=0,
            off_axis_count=0,
            staircase_step_count=0,
            staircase_fraction=0.0,
        )
        print_routing_quality_stanza(metrics)
        out = capsys.readouterr().out
        assert "Routing quality (advisory):" in out
        assert "0 across 0 net(s)" in out
