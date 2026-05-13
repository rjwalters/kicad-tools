"""Tests for the `kct fleet status` CLI command."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from kicad_tools.cli.fleet_cmd import main

# ---------------------------------------------------------------------------
# Synthetic PCB fixtures
# ---------------------------------------------------------------------------

# PCB with fully connected nets (2 nets, 4 pads, all connected).
CONNECTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")

  (gr_rect
    (start 0 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10.5 10) (end 20.5 10) (width 0.25) (layer "F.Cu") (net 2))
)
"""


# PCB with unrouted nets (no traces).
UNROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")

  (gr_rect
    (start 0 0)
    (end 40 40)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02"
    (layer "F.Cu")
    (at 10 20)
    (property "Reference" "J1")
    (pad "1" thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 1 "SIG1"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 2 "SIG2"))
  )

  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02"
    (layer "F.Cu")
    (at 30 20)
    (property "Reference" "J2")
    (pad "1" thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 1 "SIG1"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") (net 2 "SIG2"))
  )
)
"""


# ---------------------------------------------------------------------------
# Fake-board builder
# ---------------------------------------------------------------------------


def make_fake_board(
    boards_dir: Path,
    name: str,
    *,
    routed_complete: bool = True,
    has_gerbers: bool = False,
    has_bom: bool = False,
    has_cpl: bool = False,
    has_manifest: bool = False,
    bom_suffix: str = "jlcpcb",
    cpl_suffix: str = "jlcpcb",
    artifacts_older_than_routed: bool = False,
) -> Path:
    """Build ``boards_dir/<name>/output/`` with a minimal routed PCB and
    optional manufacturing artifacts.

    Returns the path to the routed PCB file.
    """
    board_dir = boards_dir / name
    output_dir = board_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    pcb_text = CONNECTED_PCB if routed_complete else UNROUTED_PCB
    routed_pcb = output_dir / f"{name.replace('-', '_')}_routed.kicad_pcb"
    routed_pcb.write_text(pcb_text)

    mfg_dir = output_dir / "manufacturing"
    if any([has_gerbers, has_bom, has_cpl, has_manifest]):
        mfg_dir.mkdir(parents=True, exist_ok=True)

    if has_gerbers:
        (mfg_dir / "gerbers.zip").write_bytes(b"PK\x03\x04fake")
    if has_bom:
        (mfg_dir / f"bom_{bom_suffix}.csv").write_text("ref,value\nR1,10k\n")
    if has_cpl:
        (mfg_dir / f"cpl_{cpl_suffix}.csv").write_text("ref,x,y\nR1,0,0\n")
    if has_manifest:
        manifest = {
            "version": "1.0",
            "manufacturer": bom_suffix,
            "files": {
                f"bom_{bom_suffix}.csv": {"sha256": "0" * 64, "size": 12},
                f"cpl_{cpl_suffix}.csv": {"sha256": "0" * 64, "size": 13},
                "gerbers.zip": {"sha256": "0" * 64, "size": 14},
            },
        }
        (mfg_dir / "manifest.json").write_text(json.dumps(manifest))

    if artifacts_older_than_routed and has_manifest:
        # Make manifest older than routed PCB so the surveyor flags STALE.
        manifest_path = mfg_dir / "manifest.json"
        routed_mtime = routed_pcb.stat().st_mtime
        old_time = routed_mtime - 3600.0
        os.utime(manifest_path, (old_time, old_time))

    return routed_pcb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFleetStatusBasics:
    def test_fleet_status_all_ship_ready(self, tmp_path: Path, capsys):
        """3 boards, all 100% routed + all artifacts present + fresh -> exit 0."""
        boards = tmp_path / "boards"
        for i, name in enumerate(["a-board", "b-board", "c-board"]):
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
                "status",
                "--boards-dir",
                str(boards),
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 3
        assert data["summary"]["ship_ready"] == 3
        for board in data["boards"]:
            assert board["ship_ready"] is True
            assert board["blockers"] == []

    def test_fleet_status_mixed_completion(self, tmp_path: Path, capsys):
        """5 boards mixed: 1 shippable, 2 incomplete, 1 missing mfr, 1 stale."""
        boards = tmp_path / "boards"

        make_fake_board(
            boards,
            "a-ship",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(
            boards,
            "b-incomplete-1",
            routed_complete=False,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(
            boards,
            "c-incomplete-2",
            routed_complete=False,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(
            boards,
            "d-no-mfr",
            routed_complete=True,
        )
        make_fake_board(
            boards,
            "e-stale",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
            artifacts_older_than_routed=True,
        )

        result = main(
            ["status", "--boards-dir", str(boards), "--format", "json"]
        )
        assert result == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        by_name = {b["name"]: b for b in data["boards"]}

        assert by_name["a-ship"]["ship_ready"] is True
        assert by_name["a-ship"]["blockers"] == []

        assert by_name["b-incomplete-1"]["ship_ready"] is False
        assert any("incomplete routing" in b for b in by_name["b-incomplete-1"]["blockers"])

        assert by_name["c-incomplete-2"]["ship_ready"] is False
        assert any("incomplete routing" in b for b in by_name["c-incomplete-2"]["blockers"])

        assert by_name["d-no-mfr"]["ship_ready"] is False
        assert "no manufacturing/ dir" in by_name["d-no-mfr"]["blockers"]

        assert by_name["e-stale"]["ship_ready"] is False
        assert "artifacts stale" in by_name["e-stale"]["blockers"]

    def test_fleet_status_json_schema(self, tmp_path: Path, capsys):
        """Verify JSON schema: schema_version, ISO 8601 timestamps, board keys."""
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

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Top-level keys
        assert data["schema_version"] == "1.0"
        for key in ("schema_version", "surveyed_at", "boards_dir", "summary", "boards"):
            assert key in data

        # ISO 8601 surveyed_at
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            data["surveyed_at"],
        )

        # Board dict keys
        b = data["boards"][0]
        expected_board_keys = {
            "name",
            "routed_pcb",
            "routed_mtime",
            "routing",
            "manufacturing",
            "ship_ready",
            "blockers",
        }
        assert expected_board_keys.issubset(b.keys())

        # Routing sub-keys
        expected_routing_keys = {
            "total_pads",
            "connected_pads",
            "completion_pct",
            "total_nets",
            "complete_nets",
            "incomplete_nets",
            "unrouted_nets",
            "routing_complete",
        }
        assert expected_routing_keys.issubset(b["routing"].keys())

        # Manufacturing sub-keys
        expected_mfg_keys = {
            "dir_exists",
            "has_gerbers",
            "has_bom",
            "has_cpl",
            "has_manifest",
            "manifest_mtime",
            "stale",
        }
        assert expected_mfg_keys.issubset(b["manufacturing"].keys())

    def test_fleet_status_table_format(self, tmp_path: Path, capsys):
        """Table headers, summary footer, no Unicode glyphs."""
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
        make_fake_board(boards, "b-incomplete", routed_complete=False)

        main(["status", "--boards-dir", str(boards)])
        captured = capsys.readouterr()
        out = captured.out

        # Header row
        assert "Board" in out
        assert "Pads" in out
        assert "Mfr" in out
        assert "Stale" in out
        assert "Ship?" in out

        # Summary footer line
        assert "boards surveyed" in out
        assert "ship-ready" in out
        assert "incomplete" in out
        assert "artifacts stale" in out

        # Plain ASCII only (no Unicode glyphs, no checkmarks/x-marks).
        forbidden_chars = "✓✗✅❌─━│"
        assert not any(c in out for c in forbidden_chars), (
            "Unicode glyphs leaked into table output"
        )

        # YES / NO ship status appears.
        assert "YES" in out
        assert "NO" in out

    def test_fleet_status_empty_boards_dir(self, tmp_path: Path, capsys):
        """Empty boards/ -> 'No boards found' table, JSON boards=[]."""
        empty = tmp_path / "boards"
        empty.mkdir()

        # Table format.
        result = main(["status", "--boards-dir", str(empty)])
        captured = capsys.readouterr()
        assert "No boards found" in captured.out
        # Exit code 2: no boards found counts as not-ship-ready (mirrors
        # net-status semantics).
        assert result == 2

        # JSON format.
        result = main(["status", "--boards-dir", str(empty), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["boards"] == []
        assert data["summary"]["total"] == 0
        assert result == 2

    def test_fleet_status_custom_boards_dir(self, tmp_path: Path, capsys):
        """--boards-dir picks up an alternate location."""
        alt = tmp_path / "alt-fleet"
        make_fake_board(
            alt,
            "x-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        main(["status", "--boards-dir", str(alt), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        assert data["boards"][0]["name"] == "x-board"

    def test_fleet_status_ship_only_filter(self, tmp_path: Path, capsys):
        """--ship-only hides non-ship-ready rows in table; JSON unaffected."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "a-ship",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        make_fake_board(boards, "b-incomplete", routed_complete=False)

        # Table with --ship-only.
        main(["status", "--boards-dir", str(boards), "--ship-only"])
        captured = capsys.readouterr()
        assert "a-ship" in captured.out
        assert "b-incomplete" not in captured.out

        # JSON output always lists all boards.
        main(["status", "--boards-dir", str(boards), "--ship-only", "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        names = {b["name"] for b in data["boards"]}
        assert names == {"a-ship", "b-incomplete"}

    def test_fleet_status_stale_detection(self, tmp_path: Path, capsys):
        """os.utime to age the manifest below the routed PCB -> stale + blocker."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "x-board",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
            artifacts_older_than_routed=True,
        )
        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        assert result == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        b = data["boards"][0]
        assert b["manufacturing"]["stale"] is True
        assert "artifacts stale" in b["blockers"]

    def test_fleet_status_pattern_override(self, tmp_path: Path, capsys):
        """--pattern '*.kicad_pcb' picks up unrouted PCBs too."""
        boards = tmp_path / "boards"
        board_dir = boards / "p-board" / "output"
        board_dir.mkdir(parents=True)
        # Only an unrouted PCB present (no *_routed.kicad_pcb).
        (board_dir / "p_board.kicad_pcb").write_text(UNROUTED_PCB)

        # Default pattern: should not pick it up.
        main(["status", "--boards-dir", str(boards), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 0

        # Overridden pattern: should pick it up.
        main(
            [
                "status",
                "--boards-dir",
                str(boards),
                "--pattern",
                "*.kicad_pcb",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["total"] == 1
        assert data["boards"][0]["name"] == "p-board"


class TestFleetStatusManufacturerVariation:
    """Verify manufacturer-name variation tolerance for BOM/CPL detection."""

    @pytest.mark.parametrize("mfr", ["jlcpcb", "pcbway", "seeed"])
    def test_bom_cpl_suffix_variation(self, tmp_path: Path, capsys, mfr: str):
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            f"vendor-{mfr}",
            routed_complete=True,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
            bom_suffix=mfr,
            cpl_suffix=mfr,
        )
        main(["status", "--boards-dir", str(boards), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        b = data["boards"][0]
        assert b["manufacturing"]["has_bom"] is True
        assert b["manufacturing"]["has_cpl"] is True


class TestFleetStatusIntegration:
    """Integration smoke test against the actual repo boards/ dir."""

    @pytest.mark.slow
    def test_fleet_status_real_boards_dir(self, capsys):
        # Locate repo root: tests/test_fleet_status.py -> ../boards
        repo_boards = Path(__file__).resolve().parent.parent / "boards"
        if not repo_boards.is_dir():
            pytest.skip("repo boards/ directory not present in this checkout")

        # Should run without exception and produce some output.
        rc = main(
            [
                "status",
                "--boards-dir",
                str(repo_boards),
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "schema_version" in data
        assert "boards" in data
        # Exit code: 0 if all ship, 2 otherwise; both are valid for a smoke test.
        assert rc in (0, 2)
