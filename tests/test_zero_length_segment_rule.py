"""Tests for the ``zero_length_segment`` DRC rule (issue #4651).

Zero-length segments are always routing artifacts; the advisory
routing-quality metrics (#4623) count them (``zero_length_count``) but
until #4651 nothing reported them as findings.  This file covers:

- the rule unit behaviour on a synthetic board (per-segment warning with
  net + layer + position, clean board emits nothing);
- detection parity: the rule's finding count always equals the advisory
  stanza's ``zero_length_count`` on the same board;
- waivability through the central ``.kct_waivers.json`` mechanism
  (#4417), both by ``items`` (one specific segment) and by ``nets``
  (every artifact on a net);
- severity contract: warning by default (exit 0), fatal under
  ``--strict`` (the #4651 fleet pre-check found board-05 carrying five
  zero-length segments, so error-by-default would break a shipping
  board).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.analysis.routing_quality import compute_routing_quality
from kicad_tools.schema.pcb import PCB

# Mirrors the #4623 template in tests/test_routing_quality_metrics.py:
# DRC-clean apart from the parameterizable segment block.
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

# Two zero-length artifacts (nets GND and +3.3V) plus one normal segment.
_ZERO_LENGTH_SEGMENTS = """  (segment (start 110 110) (end 110 110) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 112 118) (end 112 118) (width 0.25) (layer "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
  (segment (start 120 120) (end 122 122) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000022"))
"""

_CLEAN_SEGMENTS = """  (segment (start 110 110) (end 112 112) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
"""


@pytest.fixture
def zero_length_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "zero_length_4651.kicad_pcb"
    p.write_text(_PCB_TEMPLATE.format(segments=_ZERO_LENGTH_SEGMENTS))
    return p


@pytest.fixture
def clean_pcb(tmp_path: Path) -> Path:
    p = tmp_path / "clean_4651.kicad_pcb"
    p.write_text(_PCB_TEMPLATE.format(segments=_CLEAN_SEGMENTS))
    return p


def _run_check_json(board: Path, capsys, *extra: str) -> tuple[int, dict]:
    from kicad_tools.cli.check_cmd import main

    rc = main(
        [str(board), "--only", "zero_length_segment", "--format", "json", "--drc-only", *extra]
    )
    return rc, json.loads(capsys.readouterr().out)


def _zl_violations(data: dict) -> list[dict]:
    return [v for v in data["violations"] if v["rule_id"] == "zero_length_segment"]


class TestZeroLengthSegmentRule:
    def test_emits_one_warning_per_segment(self, zero_length_pcb: Path, capsys):
        rc, data = _run_check_json(zero_length_pcb, capsys)
        violations = _zl_violations(data)
        # Warning severity: found but non-blocking by default.
        assert rc == 0
        assert len(violations) == 2
        for v in violations:
            assert v["severity"] == "warning"
            assert v["location"] is not None
            assert len(v["items"]) == 1
            assert len(v["nets"]) == 1
        by_net = {v["nets"][0]: v for v in violations}
        assert set(by_net) == {"GND", "+3.3V"}
        assert by_net["GND"]["layer"] == "F.Cu"
        assert by_net["+3.3V"]["layer"] == "B.Cu"
        # The items id carries the layer and the reported (sheet-absolute)
        # position so a waiver can target one specific segment.
        assert by_net["GND"]["items"][0].startswith("F.Cu@(")
        gx, gy = by_net["GND"]["location"]
        assert gx == pytest.approx(110.0)
        assert gy == pytest.approx(110.0)

    def test_clean_board_emits_nothing(self, clean_pcb: Path, capsys):
        rc, data = _run_check_json(clean_pcb, capsys)
        assert rc == 0
        assert _zl_violations(data) == []
        assert data["summary"]["warnings"] == 0

    def test_strict_makes_warnings_fatal(self, zero_length_pcb: Path, capsys):
        rc, data = _run_check_json(zero_length_pcb, capsys, "--strict")
        assert rc == 2
        assert len(_zl_violations(data)) == 2

    def test_count_agrees_with_advisory_metrics(self, zero_length_pcb: Path, capsys):
        """AC: stanza zero_length_count == rule violation count (same board)."""
        rc, data = _run_check_json(zero_length_pcb, capsys)
        assert rc == 0
        metrics = compute_routing_quality(PCB.load(zero_length_pcb))
        assert metrics.zero_length_count == len(_zl_violations(data)) == 2

    def test_items_id_matches_reported_location(self, zero_length_pcb: Path, capsys):
        """The waiver-matching items id and the JSON location agree."""
        _, data = _run_check_json(zero_length_pcb, capsys)
        for v in _zl_violations(data):
            layer_part, coord_part = v["items"][0].split("@")
            assert layer_part == v["layer"]
            x, y = (float(t) for t in coord_part.strip("()").split(","))
            assert x == pytest.approx(v["location"][0], abs=1e-3)
            assert y == pytest.approx(v["location"][1], abs=1e-3)


class TestZeroLengthSegmentWaivers:
    """The rule flows through the central .kct_waivers.json mechanism (#4417)."""

    def test_items_waiver_waives_one_segment(self, zero_length_pcb: Path, capsys):
        # First run: harvest the exact items id from the JSON output (the
        # workflow a waiver author follows).
        _, data = _run_check_json(zero_length_pcb, capsys)
        gnd = next(v for v in _zl_violations(data) if v["nets"] == ["GND"])

        waiver = zero_length_pcb.parent / "w_items.json"
        waiver.write_text(
            json.dumps(
                {
                    "version": 2,
                    "waivers": [
                        {
                            "rule": "zero_length_segment",
                            "items": gnd["items"],
                            "reason": "known router artifact, cleanup tracked",
                            "issue": "kicad-tools#4651",
                        }
                    ],
                }
            )
        )
        rc, data = _run_check_json(zero_length_pcb, capsys, "--waivers", str(waiver))
        assert rc == 0
        violations = _zl_violations(data)
        waived = [v for v in violations if v["waived"]]
        active = [v for v in violations if not v["waived"]]
        assert len(waived) == 1
        assert waived[0]["nets"] == ["GND"]
        assert waived[0]["status"] == "waived"
        assert waived[0]["waiver_issue"] == "kicad-tools#4651"
        assert len(active) == 1
        assert active[0]["nets"] == ["+3.3V"]

    def test_nets_waiver_with_strict_flips_exit(self, zero_length_pcb: Path, capsys):
        """Waiving both artifacts (by nets) relieves the --strict gate."""
        waiver = zero_length_pcb.parent / "w_nets.json"
        waiver.write_text(
            json.dumps(
                {
                    "version": 2,
                    "waivers": [
                        {
                            "rule": "zero_length_segment",
                            "nets": ["GND"],
                            "reason": "known artifact on GND stitching",
                            "issue": "kicad-tools#4651",
                        },
                        {
                            "rule": "zero_length_segment",
                            "nets": ["+3.3V"],
                            "reason": "known artifact on 3V3 fanout",
                            "issue": "kicad-tools#4651",
                        },
                    ],
                }
            )
        )
        # Unwaived: --strict fails on the two warnings.
        rc, _ = _run_check_json(zero_length_pcb, capsys, "--strict")
        assert rc == 2
        # Waived: both findings report WAIVED and the gate passes.
        rc, data = _run_check_json(zero_length_pcb, capsys, "--strict", "--waivers", str(waiver))
        assert rc == 0
        assert all(v["waived"] for v in _zl_violations(data))
        assert data["summary"]["waived"] == 2
        assert data["summary"]["warnings"] == 0


class TestRegistration:
    def test_rule_runs_in_default_check_all_set(self, zero_length_pcb: Path, capsys):
        """No --only: the rule is part of the standard category set."""
        from kicad_tools.cli.check_cmd import main

        rc = main([str(zero_length_pcb), "--format", "json", "--drc-only"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert len(_zl_violations(data)) == 2

    def test_checker_check_all_includes_rule(self, zero_length_pcb: Path):
        from kicad_tools.validate import DRCChecker

        checker = DRCChecker(PCB.load(zero_length_pcb), manufacturer="jlcpcb", layers=2)
        results = checker.check_all()
        zl = [v for v in results.violations if v.rule_id == "zero_length_segment"]
        assert len(zl) == 2

    def test_skip_category_suppresses_rule(self, zero_length_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        rc = main(
            [
                str(zero_length_pcb),
                "--skip",
                "zero_length_segment",
                "--format",
                "json",
                "--drc-only",
            ]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert _zl_violations(data) == []
