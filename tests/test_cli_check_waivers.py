"""End-to-end CLI tests for ``kct check --waivers`` (Issue #4417).

Exercises the general ``.kct_waivers.json`` sidecar against a real board with an
overlapping-courtyard finding: a matching waiver flips the exit code, the
finding is reported WAIVED (non-blocking) yet keeps ``severity: error`` in the
JSON -- so the ``kct audit`` manufacturing gate (which re-parses that JSON and
ignores ``waived``) stays blocking by default.
"""

from __future__ import annotations

import json

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.drc.report import parse_json_report

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
