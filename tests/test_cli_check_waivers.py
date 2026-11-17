"""End-to-end CLI tests for ``kct check --waivers`` (Issue #4417).

Exercises the general ``.kct_waivers.json`` sidecar against a real board with an
overlapping-courtyard finding: a matching waiver flips the exit code, the
finding is reported WAIVED (non-blocking) yet keeps ``severity: error`` in the
JSON -- so the ``kct audit`` manufacturing gate (which re-parses that JSON and
ignores ``waived``) stays blocking by default.
"""

from __future__ import annotations

import json
import re

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.drc.report import parse_json_report
from kicad_tools.validate.violations import DRCResults, DRCViolation

pytest.importorskip("shapely")


def _board_file(tmp_path, *, refs_positions):
    """Write a minimal .kicad_pcb with overlapping F.CrtYd footprints."""
    fps = []
    for ref, (x, y) in refs_positions:
        fps.append(
            f"""  (footprint "TestFP" (layer "F.Cu")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
    (fp_rect (start -1 -1) (end 1 1) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
  )"""
        )
    content = (
        "(kicad_pcb (version 20240108) (generator test)\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n' + "\n".join(fps) + "\n)\n"
    )
    path = tmp_path / "board.kicad_pcb"
    path.write_text(content)
    return path


def _write_waiver(path, waivers):
    path.write_text(json.dumps({"version": 2, "waivers": waivers}))


class TestCliWaivers:
    def test_matching_waiver_flips_exit_and_json_status(self, tmp_path, capsys):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        waiver = tmp_path / "w.json"
        _write_waiver(
            waiver,
            [
                {
                    "rule": "courtyards_overlap",
                    "items": ["U1", "C1"],
                    "reason": "EE-mandated tight decoupling",
                    "issue": "chorus#18",
                }
            ],
        )
        rc = check_cmd.main(
            [
                str(board),
                "--only",
                "courtyard_overlap",
                "--waivers",
                str(waiver),
                "--format",
                "json",
                "--drc-only",
            ]
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert rc == 0
        assert data["summary"]["errors"] == 0
        assert data["summary"]["waived"] == 1
        assert data["summary"]["passed"] is True
        waived_v = [v for v in data["violations"] if v.get("waived")]
        assert len(waived_v) == 1
        # Load-bearing manufacturing-gate safety: status flips to "waived" but
        # the underlying severity stays "error".
        assert waived_v[0]["status"] == "waived"
        assert waived_v[0]["severity"] == "error"
        assert waived_v[0]["waiver_reason"] == "EE-mandated tight decoupling"
        assert waived_v[0]["waiver_issue"] == "chorus#18"

    def test_manufacturing_gate_still_blocks_on_waived(self, tmp_path):
        """The audit re-parser reads severity and ignores waived -> stays blocking."""
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        waiver = tmp_path / "w.json"
        _write_waiver(
            waiver,
            [
                {
                    "rule": "courtyards_overlap",
                    "items": ["U1", "C1"],
                    "reason": "intentional",
                    "issue": "x#1",
                }
            ],
        )
        out_json = tmp_path / "out.json"
        check_cmd.main(
            [
                str(board),
                "--only",
                "courtyard_overlap",
                "--waivers",
                str(waiver),
                "--output",
                str(out_json),
                "--drc-only",
            ]
        )
        report = parse_json_report(out_json.read_text())
        # Waiver relieved the kct-check gate, but the audit-facing report still
        # counts the finding as an error (severity-keyed).
        assert report.error_count == 1

    def test_no_waiver_still_fails(self, tmp_path, capsys):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        rc = check_cmd.main(
            [str(board), "--only", "courtyard_overlap", "--format", "json", "--drc-only"]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert data["summary"]["errors"] == 1
        assert data["summary"]["passed"] is False

    def test_explicit_malformed_waiver_hard_error(self, tmp_path):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        rc = check_cmd.main([str(board), "--only", "courtyard_overlap", "--waivers", str(bad)])
        assert rc == 1

    def test_explicit_missing_waiver_hard_error(self, tmp_path):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        rc = check_cmd.main(
            [
                str(board),
                "--only",
                "courtyard_overlap",
                "--waivers",
                str(tmp_path / "does-not-exist.json"),
            ]
        )
        assert rc == 1

    def test_auto_discovered_malformed_degrades(self, tmp_path, capsys):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        # Auto-discovered sidecar next to the board is malformed -> warn + continue.
        (tmp_path / ".kct_waivers.json").write_text("{not json")
        rc = check_cmd.main(
            [str(board), "--only", "courtyard_overlap", "--format", "json", "--drc-only"]
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Degraded to zero waivers: the overlap still fails.
        assert rc == 2
        assert data["summary"]["errors"] == 1
        assert "ignoring malformed waivers sidecar" in captured.err

    def test_unused_waiver_advisory(self, tmp_path, capsys):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        waiver = tmp_path / "w.json"
        _write_waiver(
            waiver,
            [
                {
                    "rule": "courtyards_overlap",
                    "items": ["U1", "C1"],
                    "reason": "matches",
                    "issue": "x#1",
                },
                {
                    "rule": "courtyards_overlap",
                    "items": ["X9", "Y9"],
                    "reason": "stale",
                    "issue": "x#2",
                },
            ],
        )
        rc = check_cmd.main(
            [
                str(board),
                "--only",
                "courtyard_overlap",
                "--waivers",
                str(waiver),
                "--format",
                "json",
                "--drc-only",
            ]
        )
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert data["summary"]["waived"] == 1
        unused = [v for v in data["violations"] if v["rule_id"] == "waiver_unused"]
        assert len(unused) == 1
        assert unused[0]["severity"] == "info"
        assert "x#2" in unused[0]["message"]


class TestFormatSummaryWaived:
    """Issue #4696: ``--format summary`` must not fold waived errors into

    the ``Warnings`` column -- the per-rule table and the ``TOTAL`` row need
    a dedicated ``Waived`` column, mirroring ``output_table``, so the
    table's ``Warnings`` total agrees with the ``DRC:`` prose line.
    """

    def test_waived_error_gets_own_column_and_totals_agree_with_prose(self, tmp_path, capsys):
        board = _board_file(tmp_path, refs_positions=[("U1", (10.0, 10.0)), ("C1", (11.0, 10.0))])
        waiver = tmp_path / "w.json"
        _write_waiver(
            waiver,
            [
                {
                    "rule": "courtyards_overlap",
                    "items": ["U1", "C1"],
                    "reason": "EE-mandated tight decoupling",
                    "issue": "chorus#18",
                }
            ],
        )
        check_cmd.main(
            [
                str(board),
                "--only",
                "courtyard_overlap",
                "--waivers",
                str(waiver),
                "--format",
                "summary",
            ]
        )
        out = capsys.readouterr().out
        # The exit code is driven by the meta-check rollup (no schematic
        # discovered next to this synthetic fixture -> INCOMPLETE); this
        # test is only about the summary-format DRC accounting, not the
        # exit code, so it doesn't assert `rc` here.

        lines = out.splitlines()
        header = next(line for line in lines if line.startswith("Rule ID"))
        assert "Waived" in header

        rule_row = next(line for line in lines if line.startswith("courtyards_overlap"))
        # Columns: rule_id, errors, warnings, infos, waived.
        _, errors, warnings, infos, waived = rule_row.split()
        assert (errors, warnings, infos, waived) == ("0", "0", "0", "1")

        total_row = next(line for line in lines if line.startswith("TOTAL"))
        _, total_errors, total_warnings, total_infos, total_waived = total_row.split()
        assert (total_errors, total_warnings, total_infos, total_waived) == ("0", "0", "0", "1")

        # The prose "DRC:" line (from the meta-check stanza) is computed
        # independently via `is_warning`, which also excludes waived
        # findings -- the two totals must agree.
        prose_match = re.search(
            r"DRC:\s+\S+\s+\(\d+ rules checked, (\d+) error\(s\), (\d+) warning\(s\)\)",
            out,
        )
        assert prose_match is not None
        prose_errors, prose_warnings = prose_match.groups()
        assert prose_errors == total_errors
        assert prose_warnings == total_warnings

    def test_output_summary_waived_warning_and_info_excluded_from_own_severity(
        self, tmp_path, capsys
    ):
        """Direct unit test: waived warnings/infos must not leak into the

        ``Warnings``/``Infos`` columns either -- the ``is_waived``-first
        bucket order fixes all three severities at once (not just error).
        """
        violations = [
            DRCViolation(
                rule_id="clearance_trace_trace",
                severity="warning",
                message="waived warning",
                waived=True,
                waiver_reason="test",
                waiver_issue="x#1",
            ),
            DRCViolation(
                rule_id="clearance_trace_trace",
                severity="warning",
                message="live warning",
            ),
            DRCViolation(
                rule_id="silkscreen_overlap",
                severity="info",
                message="waived info",
                waived=True,
                waiver_reason="test",
                waiver_issue="x#2",
            ),
        ]
        results = DRCResults(violations=violations, rules_checked=2)
        check_cmd.output_summary(violations, results, tmp_path / "board.kicad_pcb")
        out = capsys.readouterr().out

        clearance_row = next(
            line for line in out.splitlines() if line.startswith("clearance_trace_trace")
        )
        _, errors, warnings, infos, waived = clearance_row.split()
        assert (errors, warnings, infos, waived) == ("0", "1", "0", "1")

        silkscreen_row = next(
            line for line in out.splitlines() if line.startswith("silkscreen_overlap")
        )
        _, s_errors, s_warnings, s_infos, s_waived = silkscreen_row.split()
        assert (s_errors, s_warnings, s_infos, s_waived) == ("0", "0", "0", "1")

        total_row = next(line for line in out.splitlines() if line.startswith("TOTAL"))
        _, t_errors, t_warnings, t_infos, t_waived = total_row.split()
        assert (t_errors, t_warnings, t_infos, t_waived) == ("0", "1", "0", "2")
