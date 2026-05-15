"""Tests for the DRC-aware ship-ready logic in ``kct fleet status`` (#2932).

These tests live in a separate module so the original
``test_fleet_status.py`` suite stays focused on routing + manufacturing
detection, and the DRC integration can grow its own coverage matrix.

Acceptance criteria covered:

* clean board (drc_report.json with 0 errors) -> ship-ready YES
* board with DRC errors beyond tolerance -> ship-ready NO with a
  ``DRC errors: N`` blocker
* board WITHOUT drc_report.json -> classification unchanged
  (backwards-compat: a previously-YES board stays YES)
* per-board tolerance allowlist (``.github/routed-drc-tolerance.yml``)
  is honored: errors within tolerance do NOT block ship-ready
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.fleet_cmd import main

# Reuse the synthetic-PCB fixtures + ``make_fake_board`` builder from
# the sibling test module to keep board-construction logic in one place.
from tests.test_fleet_status import make_fake_board

# ---------------------------------------------------------------------------
# DRC report builder
# ---------------------------------------------------------------------------


def _write_drc_report(routed_pcb: Path, *, errors: int) -> Path:
    """Write a minimal ``drc_report.json`` next to ``routed_pcb``.

    Only the ``summary.errors`` key is consulted by the fleet surveyor;
    we provide a realistic shell otherwise so future schema readers
    don't trip on missing fields.
    """
    report = {
        "file": str(routed_pcb),
        "manufacturer": "jlcpcb-tier1",
        "layers": 2,
        "summary": {
            "errors": errors,
            "warnings": 0,
            "infos": 0,
            "rules_checked": 25,
            "rules_checked_by_rule": {},
            "passed": errors == 0,
        },
        "violations": [
            {
                "rule_id": "clearance_pad_segment",
                "type": "clearance_pad_segment",
                "severity": "error",
                "message": "synthetic DRC error",
            }
            for _ in range(errors)
        ],
    }
    report_path = routed_pcb.parent / "drc_report.json"
    report_path.write_text(json.dumps(report))
    return report_path


def _write_tolerance_file(path: Path, mapping: dict[str, int]) -> Path:
    """Write a minimal ``.github/routed-drc-tolerance.yml``-shaped file.

    Avoids importing ``yaml`` here; the surveyor's loader uses pyyaml
    but plain hand-rolled YAML is fine for these fixtures.
    """
    lines = ["tolerances:"]
    for k, v in mapping.items():
        lines.append(f"  {k}: {v}")
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Acceptance-criterion tests
# ---------------------------------------------------------------------------


class TestFleetStatusDRC:
    def test_clean_board_with_zero_errors_is_ship_ready(
        self, tmp_path: Path, capsys
    ):
        """Board has all artifacts + drc_report.json with 0 errors -> YES."""
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "a-clean",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(routed_pcb, errors=0)

        # Empty tolerance file -> strict 0-error gate applies.
        tol = _write_tolerance_file(tmp_path / "tol.yml", {})

        rc = main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
                "--format",
                "json",
            ]
        )
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is True
        assert b["blockers"] == []
        assert b["drc"]["report_exists"] is True
        assert b["drc"]["errors"] == 0
        assert b["drc"]["over_tolerance"] is False

    def test_board_with_drc_errors_blocks_ship_ready(
        self, tmp_path: Path, capsys
    ):
        """Board with 182 DRC errors (mirrors board-02) reports NO with reason."""
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "b-broken",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(routed_pcb, errors=182)

        # No tolerance file at all -> strict 0-error gate -> blocker.
        tol = tmp_path / "absent-tolerance.yml"
        # Intentionally do not create tol; loader treats absent as empty.
        assert not tol.exists()

        rc = main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
                "--format",
                "json",
            ]
        )
        assert rc == 2

        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is False
        assert b["drc"]["report_exists"] is True
        assert b["drc"]["errors"] == 182
        assert b["drc"]["over_tolerance"] is True
        # The blocker text must cite the DRC count.
        assert any(
            "DRC errors" in blocker and "182" in blocker
            for blocker in b["blockers"]
        ), f"missing DRC-cited blocker in {b['blockers']!r}"

    def test_board_without_drc_report_keeps_pre_fix_classification(
        self, tmp_path: Path, capsys
    ):
        """No drc_report.json -> board is treated as DRC-unknown.

        Backwards-compat invariant from issue #2932: do NOT block
        ship-ready when the DRC stage has not run. A fully-routed board
        with all artifacts must still report YES.
        """
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "c-no-drc",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # No drc_report.json next to routed_pcb.
        assert not (routed_pcb.parent / "drc_report.json").exists()

        tol = _write_tolerance_file(tmp_path / "tol.yml", {})

        rc = main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
                "--format",
                "json",
            ]
        )
        # All artifacts present + no DRC blocker added -> ship-ready.
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is True
        assert b["blockers"] == []
        assert b["drc"]["report_exists"] is False
        # over_tolerance is False because the report wasn't seen.
        assert b["drc"]["over_tolerance"] is False


class TestFleetStatusDRCTolerance:
    """The per-board allowlist must let grandfathered boards ship."""

    def test_errors_within_tolerance_do_not_block(self, tmp_path: Path, capsys):
        """Tolerance of 53, actual 38 -> still ship-ready."""
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "05-bldc-motor-controller",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(routed_pcb, errors=38)

        # Tolerance keyed by suffix that matches the routed PCB path.
        # The suffix lookup in ``_drc_tolerance_for`` accepts both
        # repo-relative and absolute matches.
        suffix = (
            "05-bldc-motor-controller/output/"
            "05_bldc_motor_controller_routed.kicad_pcb"
        )
        tol = _write_tolerance_file(tmp_path / "tol.yml", {suffix: 53})

        rc = main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
                "--format",
                "json",
            ]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is True
        assert b["drc"]["errors"] == 38
        assert b["drc"]["tolerance"] == 53
        assert b["drc"]["over_tolerance"] is False

    def test_errors_above_tolerance_block(self, tmp_path: Path, capsys):
        """Tolerance of 53, actual 60 -> blocker cites the count + allowance."""
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "05-bldc-motor-controller",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(routed_pcb, errors=60)
        suffix = (
            "05-bldc-motor-controller/output/"
            "05_bldc_motor_controller_routed.kicad_pcb"
        )
        tol = _write_tolerance_file(tmp_path / "tol.yml", {suffix: 53})

        rc = main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
                "--format",
                "json",
            ]
        )
        assert rc == 2
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is False
        # Blocker message must include the actual count AND the allowance
        # so reviewers can see how far over budget the board is.
        msg = " ".join(b["blockers"])
        assert "DRC errors" in msg
        assert "60" in msg
        assert "53" in msg


class TestFleetStatusDRCTableRendering:
    """The DRC column must render without breaking the existing table."""

    def test_drc_column_present_in_header(self, tmp_path: Path, capsys):
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "a-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        main(["status", "--boards-dir", str(boards)])
        out = capsys.readouterr().out
        # Column heading.
        assert "DRC" in out
        # Existing columns still present (no regression).
        for col in ("Board", "Pads", "Mfr", "Stale", "Ship?"):
            assert col in out

    def test_drc_dash_for_missing_report(self, tmp_path: Path, capsys):
        """No drc_report.json -> the DRC cell renders as ``-``."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "a-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        main(["status", "--boards-dir", str(boards)])
        out = capsys.readouterr().out
        # The header row of the DRC column is "DRC"; the data row must
        # contain "-" in the cell. We assert the literal cell appears
        # in the row that names the board.
        for line in out.splitlines():
            if line.startswith("a-board"):
                # Crude but sufficient: a "-" token must appear in the
                # column-aligned data line.
                assert " - " in line or line.endswith(" -")
                return
        raise AssertionError("a-board row not found in table output")

    def test_drc_cell_shows_count_with_bang_when_over_tolerance(
        self, tmp_path: Path, capsys
    ):
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "a-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(routed_pcb, errors=17)
        tol = _write_tolerance_file(tmp_path / "tol.yml", {})
        main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
            ]
        )
        out = capsys.readouterr().out
        # Either "17!" (over tolerance) appears, or the row exists with
        # a ship-NO and "DRC errors" reason text.
        assert "17!" in out
        assert "DRC errors" in out


class TestFleetStatusDRCBackwardsCompat:
    """The DRC integration must not regress the existing tests' fixtures."""

    def test_mixed_with_and_without_drc(self, tmp_path: Path, capsys):
        """Two boards: one with a DRC report exceeding tolerance, one without.

        The board with the DRC report must drop to NO; the board
        without one must keep YES.
        """
        boards = tmp_path / "boards"
        clean_pcb = make_fake_board(
            boards,
            "a-no-drc",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # Deliberately omit drc_report.json for a-no-drc.
        assert not (clean_pcb.parent / "drc_report.json").exists()

        bad_pcb = make_fake_board(
            boards,
            "b-drc-bad",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(bad_pcb, errors=12)
        tol = _write_tolerance_file(tmp_path / "tol.yml", {})

        rc = main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--drc-tolerance-file",
                str(tol),
                "--format",
                "json",
            ]
        )
        assert rc == 2  # at least one board not ship-ready
        data = json.loads(capsys.readouterr().out)
        by_name = {b["name"]: b for b in data["boards"]}

        assert by_name["a-no-drc"]["ship_ready"] is True
        assert by_name["a-no-drc"]["blockers"] == []

        assert by_name["b-drc-bad"]["ship_ready"] is False
        assert any(
            "DRC errors" in blk for blk in by_name["b-drc-bad"]["blockers"]
        )
