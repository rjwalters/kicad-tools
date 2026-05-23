"""Tests for the ``kct fleet ship-ready`` CLI command (issue #3099).

The ``ship-ready`` subcommand is the warn-only nightly CI gate. These
tests exercise:

* The default warn-only exit code (always 0 even when boards fail).
* The ``--strict`` mode flip (exit 2 on failure).
* JSON output schema (consumed by the nightly workflow's artifact step).
* ERC report detection (presence/absence behavior).
* DRC + manufacturing + ERC blocker aggregation.

The fake-board builder reuses the synthetic PCB fixtures from
``test_fleet_status``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from kicad_tools.cli.fleet_cmd import main

# Re-import the synthetic-board builder from the sibling test module so we
# stay in lockstep with its fixture coverage and avoid duplication.
from tests.test_fleet_status import make_fake_board

# ---------------------------------------------------------------------------
# ERC report helpers
# ---------------------------------------------------------------------------


def _write_erc_report(
    board_dir: Path,
    *,
    errors: int = 0,
    warnings: int = 0,
    location: str = "output",
) -> Path:
    """Write an ``erc_report.json`` for a fake board.

    Uses the same shape as :class:`kicad_tools.erc.report.ERCReport`'s
    summary block so the detector's first parsing branch matches.
    """
    if location == "output":
        path = board_dir / "output" / "erc_report.json"
    else:
        path = board_dir / "erc_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": f"{board_dir.name}.kicad_sch",
        "summary": {
            "errors": errors,
            "warnings": warnings,
        },
        "violations": [],
    }
    path.write_text(json.dumps(payload))
    return path


def _write_drc_report(
    board_dir: Path,
    routed_pcb_name: str,
    *,
    errors: int = 0,
) -> Path:
    """Write a ``drc_report.json`` next to the routed PCB."""
    path = board_dir / "output" / "drc_report.json"
    payload = {
        "source": routed_pcb_name,
        "summary": {
            "errors": errors,
            "warnings": 0,
        },
    }
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShipReadyWarnOnly:
    """Default ``warn-only`` semantics: always exit 0."""

    def test_warn_only_exit_zero_on_all_pass(self, tmp_path: Path, capsys):
        """3 ship-ready boards -> warn-only exit 0."""
        boards = tmp_path / "boards"
        for name in ["a-board", "b-board", "c-board"]:
            make_fake_board(
                boards,
                name,
                routed_complete=True,
                has_gerbers=True,
                has_bom=True,
                has_cpl=True,
                has_manifest=True,
            )

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
            ]
        )
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["total"] == 3
        assert data["summary"]["passed"] == 3
        assert data["summary"]["failed"] == 0
        assert data["summary"]["warn_only"] is True
        assert data["command"] == "ship-ready"

    def test_warn_only_exit_zero_even_when_boards_fail(self, tmp_path: Path, capsys):
        """Boards FAILING must NOT escalate the warn-only exit code."""
        boards = tmp_path / "boards"
        # One PASS, one FAIL (missing manufacturing).
        make_fake_board(
            boards,
            "good-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(
            boards,
            "bad-board",
            routed_complete=False,  # incomplete routing -> blocker
        )

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
            ]
        )
        # Warn-only: ALWAYS exit 0 even on failure.
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["total"] == 2
        assert data["summary"]["passed"] == 1
        assert data["summary"]["failed"] == 1
        # FAIL board is identified by name.
        failing = [b for b in data["boards"] if not b["passed"]]
        assert len(failing) == 1
        assert failing[0]["name"] == "bad-board"
        assert failing[0]["blockers"]  # at least one reason listed

    def test_warn_only_exit_zero_on_empty_boards_dir(self, tmp_path: Path, capsys):
        """No boards discovered -> still exit 0 in warn-only mode."""
        boards = tmp_path / "boards"
        boards.mkdir()
        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
            ]
        )
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["total"] == 0
        assert data["summary"]["passed"] == 0


class TestShipReadyStrict:
    """``--strict`` mode opts into non-zero exit semantics."""

    def test_strict_exit_zero_on_all_pass(self, tmp_path: Path, capsys):
        """All boards PASS -> strict mode still exits 0."""
        boards = tmp_path / "boards"
        for name in ["a", "b"]:
            make_fake_board(
                boards,
                name,
                routed_complete=True,
                has_gerbers=True,
                has_bom=True,
                has_cpl=True,
                has_manifest=True,
            )

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        assert data["summary"]["warn_only"] is False
        assert data["summary"]["passed"] == 2

    def test_strict_exit_two_on_any_fail(self, tmp_path: Path, capsys):
        """One board FAILing under --strict -> exit 2."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "good",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(
            boards,
            "bad",
            routed_complete=False,
        )
        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        assert result == 2

    def test_strict_exit_two_on_empty_boards_dir(self, tmp_path: Path, capsys):
        """No boards -> strict mode exits 2 (matches ``fleet status``)."""
        boards = tmp_path / "boards"
        boards.mkdir()
        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        assert result == 2


class TestShipReadyERCDetection:
    """Verify ERC reports drive a blocker only when the report exists."""

    def test_erc_missing_does_not_block(self, tmp_path: Path, capsys):
        """No erc_report.json -> warn-only, no ERC blocker."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "no-erc-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        # No ERC report present -> board still PASS.
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["passed"] is True
        assert board["erc"]["report_exists"] is False

    def test_erc_zero_errors_does_not_block(self, tmp_path: Path, capsys):
        """erc_report.json with 0 errors -> PASS."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "erc-clean",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_erc_report(boards / "erc-clean", errors=0, warnings=2)

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["passed"] is True
        assert board["erc"]["report_exists"] is True
        assert board["erc"]["errors"] == 0
        assert board["erc"]["warnings"] == 2

    def test_erc_with_errors_blocks_under_strict(self, tmp_path: Path, capsys):
        """erc_report.json with errors -> blocker, strict exits 2."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "erc-broken",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_erc_report(boards / "erc-broken", errors=3)

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        assert result == 2
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["passed"] is False
        assert board["erc"]["errors"] == 3
        assert any("ERC errors: 3" in b for b in board["blockers"])

    def test_erc_kicad_native_shape_parsed(self, tmp_path: Path, capsys):
        """KiCad-native shape (``sheets[].violations[].severity``) is parsed."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "kicad-shape",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        report_path = boards / "kicad-shape" / "output" / "erc_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "source": "kicad-shape.kicad_sch",
                    "sheets": [
                        {
                            "path": "/",
                            "violations": [
                                {"severity": "error", "type": "pin_no_connection"},
                                {"severity": "error", "type": "pin_no_connection"},
                                {"severity": "warning", "type": "label_dangling"},
                            ],
                        }
                    ],
                }
            )
        )

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
            ]
        )
        assert result == 0  # warn-only
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["erc"]["report_exists"] is True
        assert board["erc"]["errors"] == 2
        assert board["erc"]["warnings"] == 1

    def test_erc_malformed_report_treated_as_missing(self, tmp_path: Path, capsys):
        """Malformed JSON in erc_report.json -> behaves like missing."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "bad-erc",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        report_path = boards / "bad-erc" / "output" / "erc_report.json"
        report_path.write_text("{not valid json")

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
            ]
        )
        # Malformed report is silently treated as missing -> board PASS.
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["passed"] is True
        assert board["erc"]["report_exists"] is False


class TestShipReadyDRCAggregation:
    """Verify DRC tolerance is honored alongside ERC + manufacturing."""

    def test_drc_over_tolerance_blocks_with_zero_default(self, tmp_path: Path, capsys):
        """DRC errors > 0 with no allowlist entry -> blocker."""
        boards = tmp_path / "boards"
        routed = make_fake_board(
            boards,
            "drc-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(boards / "drc-board", routed.name, errors=2)

        # Use a non-existent tolerance file so default is 0.
        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
                "--strict",
                "--drc-tolerance-file",
                str(tmp_path / "does-not-exist.yml"),
            ]
        )
        assert result == 2
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["passed"] is False
        assert any("DRC errors" in b for b in board["blockers"])

    def test_drc_under_tolerance_does_not_block(self, tmp_path: Path, capsys):
        """DRC errors within an allowlist tolerance -> board PASS."""
        boards = tmp_path / "boards"
        routed = make_fake_board(
            boards,
            "tolerated-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        _write_drc_report(boards / "tolerated-board", routed.name, errors=2)

        # Allowlist that permits up to 5 errors for this board.
        tolerance_file = tmp_path / "tol.yml"
        tolerance_file.write_text(f"tolerances:\n  {routed.relative_to(tmp_path).as_posix()}: 5\n")

        # Re-route the board under the tmp_path-relative key by chdir.
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = main(
                [
                    "ship-ready",
                    "--boards-dir",
                    str(boards.relative_to(tmp_path)),
                    "--format",
                    "json",
                    "--strict",
                    "--drc-tolerance-file",
                    str(tolerance_file.relative_to(tmp_path)),
                ]
            )
        finally:
            os.chdir(cwd)

        assert result == 0
        data = json.loads(capsys.readouterr().out)
        board = data["boards"][0]
        assert board["passed"] is True


class TestShipReadyTableOutput:
    """Verify the table format is human-readable for nightly summaries."""

    def test_table_lists_pass_and_fail(self, tmp_path: Path, capsys):
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "good-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(boards, "bad-board", routed_complete=False)

        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "table",
            ]
        )
        assert result == 0  # warn-only default
        out = capsys.readouterr().out
        assert "good-board" in out
        assert "bad-board" in out
        assert "PASS" in out
        assert "FAIL" in out
        # Footer mode marker.
        assert "warn-only mode" in out
        # GitHub Actions warning annotation surfaces failure count.
        assert "::warning::ship-ready" in out

    def test_table_no_boards_message(self, tmp_path: Path, capsys):
        boards = tmp_path / "boards"
        boards.mkdir()
        result = main(
            [
                "ship-ready",
                "--boards-dir",
                str(boards),
                "--format",
                "table",
            ]
        )
        assert result == 0
        out = capsys.readouterr().out
        assert "No boards found" in out


class TestShipReadyDispatcher:
    """Verify the ``kct fleet ship-ready`` dispatcher forwards all args."""

    def test_dispatcher_forwards_strict(self, tmp_path: Path, capsys):
        """``run_fleet_command`` re-serializes args correctly for ship-ready."""
        from argparse import Namespace

        from kicad_tools.cli.commands.fleet import run_fleet_command

        boards = tmp_path / "boards"
        make_fake_board(boards, "x", routed_complete=False)

        args = Namespace(
            fleet_command="ship-ready",
            fleet_ship_boards_dir=str(boards),
            fleet_ship_format="json",
            fleet_ship_pattern="*_routed.kicad_pcb",
            fleet_ship_drc_tolerance_file=str(tmp_path / "nope.yml"),
            fleet_ship_strict=True,
        )
        result = run_fleet_command(args)
        # The board has incomplete routing -> strict exit 2.
        assert result == 2

    def test_dispatcher_default_warn_only(self, tmp_path: Path, capsys):
        from argparse import Namespace

        from kicad_tools.cli.commands.fleet import run_fleet_command

        boards = tmp_path / "boards"
        make_fake_board(boards, "x", routed_complete=False)

        args = Namespace(
            fleet_command="ship-ready",
            fleet_ship_boards_dir=str(boards),
            fleet_ship_format="json",
            fleet_ship_pattern="*_routed.kicad_pcb",
            fleet_ship_drc_tolerance_file=str(tmp_path / "nope.yml"),
            fleet_ship_strict=False,
        )
        result = run_fleet_command(args)
        # Warn-only -> exit 0 despite the FAIL board.
        assert result == 0
