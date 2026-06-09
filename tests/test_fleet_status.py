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


# PCB with one plane-net (GND) stitching residual: 4 GND pads but only 3
# are connected via traces. The fourth GND pad has no trace -- exactly the
# situation that produces an advisory ``connectivity`` finding in CI but
# should NOT block routing-complete. The VCC signal net is fully routed.
ADVISORY_ONLY_PCB = """(kicad_pcb
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
    (end 40 40)
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

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "R3")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 30 20)
    (property "Reference" "R4")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 19.5 10) (end 29.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 29.5 10) (end 29.5 20) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10.5 10) (end 20.5 10) (width 0.25) (layer "F.Cu") (net 2))
  (segment (start 20.5 10) (end 30.5 10) (width 0.25) (layer "F.Cu") (net 2))
)
"""


# PCB with one signal-net incomplete: 3 SIG1 pads but only 2 are connected.
# The unconnected pad is NOT a plane/pour residual -- it is a genuine signal-
# net gap and MUST block routing-complete.
BLOCKING_SIGNAL_PCB = """(kicad_pcb
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
  (net 1 "SIG1")
  (net 2 "GND")

  (gr_rect
    (start 0 0)
    (end 40 40)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 30 10)
    (property "Reference" "R3")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10.5 10) (end 20.5 10) (width 0.25) (layer "F.Cu") (net 2))
  (segment (start 20.5 10) (end 30.5 10) (width 0.25) (layer "F.Cu") (net 2))
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
    pcb_text: str | None = None,
) -> Path:
    """Build ``boards_dir/<name>/output/`` with a minimal routed PCB and
    optional manufacturing artifacts.

    When ``pcb_text`` is provided it overrides the default
    ``CONNECTED_PCB`` / ``UNROUTED_PCB`` selection (useful for the
    advisory-only / blocking-signal fixtures used by the
    routing_complete tests).

    Returns the path to the routed PCB file.
    """
    board_dir = boards_dir / name
    output_dir = board_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if pcb_text is None:
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

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
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
        assert data["schema_version"] == "1.1"
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
            "blocking_incomplete_nets",
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
        assert not any(c in out for c in forbidden_chars), "Unicode glyphs leaked into table output"

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


class TestFleetStatusAdvisoryFilter:
    """Issue #3206 -- ``routing_complete`` must honor ``ADVISORY_RULE_IDS``.

    Plane/pour stitching residuals (advisory ``connectivity`` per
    ``DRCChecker.ADVISORY_RULE_IDS``) must not flip ``routing_complete``
    to NO; only genuine signal-net gaps should. Mirrors the filter
    pattern at ``scripts/ci/check_routed_drc.py:_count_blocking_errors``.

    Two layers of coverage:
      * Analyzer level: ``NetStatusResult.blocking_incomplete_count``
        zeroes out plane-net residuals but counts signal-net gaps.
      * Fleet-cmd level: ``RoutingStatus.routing_complete`` (and the JSON
        ship-ready verdict) honor the filtered count.
    """

    def test_analyzer_advisory_only_zero_blocking(self, tmp_path: Path):
        """Plane-net residual: raw incomplete>0 but blocking==0."""
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        pcb_path = tmp_path / "advisory_only.kicad_pcb"
        pcb_path.write_text(ADVISORY_ONLY_PCB)

        result = NetStatusAnalyzer(pcb_path).analyze()
        # Raw view (diagnostic) still shows the GND residual.
        assert result.incomplete_count == 1
        gnd = next(n for n in result.incomplete if n.net_name == "GND")
        assert gnd.is_advisory_incomplete is True
        # Filtered view (gating verdict) drops it.
        assert result.blocking_incomplete_count == 0
        assert "GND" in result.advisory_incomplete_names

    def test_analyzer_blocking_signal_counts(self, tmp_path: Path):
        """Signal-net gap: raw incomplete and blocking both nonzero."""
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        pcb_path = tmp_path / "blocking_signal.kicad_pcb"
        pcb_path.write_text(BLOCKING_SIGNAL_PCB)

        result = NetStatusAnalyzer(pcb_path).analyze()
        assert result.incomplete_count == 1
        sig = next(n for n in result.incomplete if n.net_name == "SIG1")
        assert sig.is_advisory_incomplete is False
        assert result.blocking_incomplete_count == 1
        assert "SIG1" not in result.advisory_incomplete_names

    def test_fleet_status_advisory_only_ships(self, tmp_path: Path, capsys):
        """Board with only a GND stitching residual must report ship-ready."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "advisory-only",
            pcb_text=ADVISORY_ONLY_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        # All boards ship-ready -> exit 0.
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        b = data["boards"][0]
        # Raw count preserved for diagnostic visibility.
        assert b["routing"]["incomplete_nets"] == 1
        # Blocking count drops it -> verdict is YES.
        assert b["routing"]["blocking_incomplete_nets"] == 0
        assert b["routing"]["routing_complete"] is True
        assert b["ship_ready"] is True
        assert b["blockers"] == []

    def test_fleet_status_blocking_signal_does_not_ship(self, tmp_path: Path, capsys):
        """Board with a genuine signal-net gap must still fail ship-ready."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "blocking-signal",
            pcb_text=BLOCKING_SIGNAL_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        # Not ship-ready -> exit 2.
        assert result == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        b = data["boards"][0]
        assert b["routing"]["incomplete_nets"] == 1
        assert b["routing"]["blocking_incomplete_nets"] == 1
        assert b["routing"]["routing_complete"] is False
        # Blocker message should use the filtered count -- one blocking
        # signal-net gap, not "0 nets" or the raw count.
        assert any("incomplete routing (1/" in blocker for blocker in b["blockers"]), b["blockers"]

    def test_fleet_status_advisory_table_shows_yes(self, tmp_path: Path, capsys):
        """The Ship? column reads YES even when the raw incomplete>0."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "advisory-only",
            pcb_text=ADVISORY_ONLY_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        result = main(["status", "--boards-dir", str(boards)])
        assert result == 0
        out = capsys.readouterr().out
        assert "advisory-only" in out
        # The advisory row must show YES, not a "incomplete routing" blocker.
        assert "YES" in out
        assert "incomplete routing" not in out

    def test_routing_status_dataclass_advisory_only(self):
        """Unit test on RoutingStatus directly: advisory-only -> YES."""
        from kicad_tools.cli.fleet_cmd import RoutingStatus

        rs = RoutingStatus(
            total_pads=8,
            connected_pads=7,
            total_nets=2,
            complete_nets=1,
            incomplete_nets=1,
            blocking_incomplete_nets=0,
            unrouted_nets=0,
        )
        # Raw incomplete shouldn't flip routing_complete to NO when the
        # blocking-filtered count is 0.
        assert rs.routing_complete is True

    def test_routing_status_dataclass_blocking(self):
        """Unit test on RoutingStatus directly: blocking -> NO."""
        from kicad_tools.cli.fleet_cmd import RoutingStatus

        rs = RoutingStatus(
            total_pads=6,
            connected_pads=5,
            total_nets=2,
            complete_nets=1,
            incomplete_nets=1,
            blocking_incomplete_nets=1,
            unrouted_nets=0,
        )
        assert rs.routing_complete is False

    def test_schema_version_bumped_to_1_1(self, tmp_path: Path, capsys):
        """JSON ``schema_version`` is bumped to 1.1 alongside the new field."""
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "advisory-only",
            pcb_text=ADVISORY_ONLY_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["schema_version"] == "1.1"
        # New field exposed on every board.
        assert "blocking_incomplete_nets" in data["boards"][0]["routing"]
        # Raw field preserved.
        assert "incomplete_nets" in data["boards"][0]["routing"]


class TestFleetStatusAdvisoryDRCFilter:
    """Issue #3363 -- extend advisory filter to clearance-refused signal nets.

    PR #3215 aligned ``RoutingStatus.routing_complete`` with
    ``DRCChecker.ADVISORY_RULE_IDS`` for plane/pour stitching residuals.
    Issue #3363 closes the second category surfaced by PR #3286: signal
    nets the router correctly refused at clearance (e.g. board 04's NRST
    after the post-#3225/#3227/#3232/#3248/#3250 clearance kernel closed
    the marginal U2.7/U2.8 corridor).

    Concrete case: board 04's NRST is a signal net with both pads NOT
    on a copper zone, so ``NetStatus.is_advisory_incomplete`` is False
    (correctly -- it's not a plane/pour residual). But the CI strict
    gate (``scripts/ci/check_routed_drc.py``) classifies the
    ``connectivity`` rule violation as advisory, so the gate passes.
    Without this fix the fleet command would still emit a misleading
    ``incomplete routing (1/12 nets)`` blocker for boards the CI gate
    considers ship-ready.

    Implementation (Option A from the issue body): when a sidecar
    ``drc_report.json`` exists AND classifies every error as advisory
    ``connectivity``, the ``incomplete routing`` blocker is suppressed.
    Without a sidecar the pre-fix behaviour persists (mirrors the
    issue-#2932 backwards-compat rule for ``_detect_drc``).
    """

    @staticmethod
    def _write_drc_report(
        routed_pcb: Path,
        *,
        connectivity_errors: int = 0,
        blocking_errors: int = 0,
        include_violations: bool = True,
    ) -> None:
        """Drop a synthetic ``drc_report.json`` next to ``routed_pcb``.

        ``connectivity_errors`` are emitted with ``rule_id="connectivity"``
        (advisory per ``ADVISORY_RULE_IDS``). ``blocking_errors`` use
        ``rule_id="clearance"`` so they count as non-advisory blocking
        violations. When ``include_violations`` is False the ``violations``
        array is omitted, forcing the fall-back to ``summary.errors``
        (legacy report format).
        """
        violations: list[dict] = []
        for i in range(connectivity_errors):
            violations.append(
                {
                    "rule_id": "connectivity",
                    "severity": "error",
                    "message": f"Net 'X{i}' is partially routed: 1 of 2 pads stranded",
                }
            )
        for i in range(blocking_errors):
            violations.append(
                {
                    "rule_id": "clearance",
                    "severity": "error",
                    "message": f"Clearance violation #{i}",
                }
            )
        report: dict = {
            "file": str(routed_pcb),
            "manufacturer": "jlcpcb",
            "summary": {
                "errors": connectivity_errors + blocking_errors,
                "warnings": 0,
                "infos": 0,
                "passed": (connectivity_errors + blocking_errors) == 0,
            },
        }
        if include_violations:
            report["violations"] = violations
        (routed_pcb.parent / "drc_report.json").write_text(json.dumps(report))

    def test_advisory_only_drc_suppresses_incomplete_blocker(
        self, tmp_path: Path, capsys
    ):
        """Issue #3363 acceptance: signal-net incomplete + advisory DRC -> YES.

        Mirrors board 04's post-#3286 state: NRST is a signal net the
        router refused, but the only DRC error is the advisory
        ``connectivity`` finding. The board must ship-ready.
        """
        boards = tmp_path / "boards"
        # Use BLOCKING_SIGNAL_PCB which has a signal-net (SIG1) gap that
        # would normally drive `incomplete routing` in the fleet status.
        routed_pcb = make_fake_board(
            boards,
            "nrst-style",
            pcb_text=BLOCKING_SIGNAL_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # The CI gate sees the same SIG1 net as an advisory connectivity
        # finding. Drop a matching DRC report.
        self._write_drc_report(routed_pcb, connectivity_errors=1, blocking_errors=0)

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        assert result == 0  # All ship-ready.

        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        # Raw view still reflects the signal-net gap for human triage.
        assert b["routing"]["blocking_incomplete_nets"] == 1
        # But the verdict is YES because the CI strict gate would pass.
        assert b["ship_ready"] is True
        assert b["blockers"] == []
        # DRC split surfaces the advisory category to JSON consumers.
        assert b["drc"]["blocking_errors"] == 0
        assert b["drc"]["advisory_errors_by_rule"] == {"connectivity": 1}
        assert b["drc"]["advisory_only"] is True
        assert b["drc"]["over_tolerance"] is False

    def test_no_drc_report_preserves_incomplete_blocker(
        self, tmp_path: Path, capsys
    ):
        """Without a ``drc_report.json`` the pre-fix behaviour persists.

        Mirrors the issue-#2932 backwards-compat rule for ``_detect_drc``:
        boards that have not yet had ``kct check`` run must keep their
        pre-fix classification. A signal-net gap without a DRC report on
        disk continues to drive ``incomplete routing``.
        """
        boards = tmp_path / "boards"
        make_fake_board(
            boards,
            "no-drc-report",
            pcb_text=BLOCKING_SIGNAL_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # NO drc_report.json written.

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        assert result == 2  # Not ship-ready.

        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is False
        assert any("incomplete routing" in blocker for blocker in b["blockers"]), b[
            "blockers"
        ]
        # No DRC report -> advisory_only stays False.
        assert b["drc"]["report_exists"] is False
        assert b["drc"]["advisory_only"] is False

    def test_blocking_drc_keeps_incomplete_blocker(self, tmp_path: Path, capsys):
        """Mixed DRC (advisory + blocking) does NOT suppress the blocker.

        When the CI strict gate would itself report blocking errors
        (e.g. board 06's ``creepage`` violations), the fleet command
        must NOT suppress the incomplete-routing blocker -- the board
        is not ship-ready and both reasons should remain visible.
        """
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "mixed-drc",
            pcb_text=BLOCKING_SIGNAL_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # Mix: 1 advisory connectivity + 1 blocking clearance.
        self._write_drc_report(routed_pcb, connectivity_errors=1, blocking_errors=1)

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        assert result == 2  # Not ship-ready.

        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is False
        # Both the incomplete-routing AND the DRC blocker should be present.
        assert any(
            "incomplete routing" in blocker for blocker in b["blockers"]
        ), b["blockers"]
        assert any("DRC errors" in blocker for blocker in b["blockers"]), b["blockers"]
        # advisory_only is False because there's a blocking error.
        assert b["drc"]["advisory_only"] is False
        assert b["drc"]["blocking_errors"] == 1

    def test_legacy_summary_only_report_keeps_strict_gate(
        self, tmp_path: Path, capsys
    ):
        """Older reports without a ``violations`` array stay strict.

        Pre-#3363 ``drc_report.json`` files only carried ``summary.errors``.
        Without per-rule breakdown the gate cannot distinguish blocking
        from advisory errors, so the safer default is to treat the raw
        count as blocking (preserves the pre-fix verdict).
        """
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "legacy-drc",
            pcb_text=BLOCKING_SIGNAL_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # Legacy: summary.errors=1, no violations array.
        self._write_drc_report(
            routed_pcb,
            connectivity_errors=1,
            blocking_errors=0,
            include_violations=False,
        )

        # Without a violations array we cannot prove all errors are
        # advisory, so the incomplete-routing blocker stays AND the DRC
        # blocker fires on the raw error count (strict fall-back).
        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is False
        # Raw error count is 1, tolerance is 0 -> over_tolerance.
        assert b["drc"]["report_exists"] is True
        assert b["drc"]["over_tolerance"] is True
        # advisory_only is False because the violations array is missing.
        assert b["drc"]["advisory_only"] is False

    def test_clean_drc_report_does_not_flip_incomplete_to_ship(
        self, tmp_path: Path, capsys
    ):
        """A 0-error DRC report does NOT suppress incomplete-routing.

        Defensive: ``advisory_only`` requires at least one advisory
        error to fire. A clean DRC report with zero errors should NOT
        suppress an incomplete-routing blocker -- the absence of DRC
        errors says nothing about routing completeness on its own.
        """
        boards = tmp_path / "boards"
        routed_pcb = make_fake_board(
            boards,
            "clean-drc-incomplete-routing",
            pcb_text=BLOCKING_SIGNAL_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )
        # 0 errors total -> not advisory_only.
        self._write_drc_report(routed_pcb, connectivity_errors=0, blocking_errors=0)

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        # Without an advisory-rule signal the incomplete-routing blocker
        # stays, so this is NOT ship-ready.
        assert result == 2
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert any(
            "incomplete routing" in blocker for blocker in b["blockers"]
        ), b["blockers"]
        assert b["drc"]["advisory_only"] is False

    def test_drc_status_to_dict_exposes_new_fields(self, tmp_path: Path):
        """``DRCStatus.to_dict()`` exposes the new advisory split fields."""
        from kicad_tools.cli.fleet_cmd import DRCStatus

        drc = DRCStatus(
            report_exists=True,
            errors=2,
            blocking_errors=0,
            advisory_errors_by_rule={"connectivity": 2},
        )
        d = drc.to_dict()
        assert d["blocking_errors"] == 0
        assert d["advisory_errors_by_rule"] == {"connectivity": 2}
        assert d["advisory_only"] is True
        # over_tolerance uses blocking_errors when present.
        assert d["over_tolerance"] is False

    def test_drc_status_over_tolerance_uses_blocking_count(self):
        """``over_tolerance`` ignores advisory errors per CI gate semantics.

        Mirrors ``scripts/ci/check_routed_drc.py``: 5 advisory connectivity
        errors with 0 blocking errors and tolerance 0 still passes the
        strict gate, because the gate only counts non-advisory rules.
        """
        from kicad_tools.cli.fleet_cmd import DRCStatus

        drc = DRCStatus(
            report_exists=True,
            errors=5,
            tolerance=0,
            blocking_errors=0,
            advisory_errors_by_rule={"connectivity": 5},
        )
        # Raw errors > tolerance, but blocking_errors == 0 -> not over.
        assert drc.over_tolerance is False

        # Mixed: 2 blocking + 3 advisory, tolerance 1 -> over (2 > 1).
        drc2 = DRCStatus(
            report_exists=True,
            errors=5,
            tolerance=1,
            blocking_errors=2,
            advisory_errors_by_rule={"connectivity": 3},
        )
        assert drc2.over_tolerance is True


# ---------------------------------------------------------------------------
# Issue #3280: schematic-vs-PCB drift detection
# ---------------------------------------------------------------------------


# A minimal schematic with three named labels (matches the EXTRA_NET_PCB
# below for the no-drift case and EXTRA_NET_DRIFT_PCB for the drift case).
THREE_NET_SCH = """(kicad_sch
  (version 20240108)
  (generator "test")
  (paper "A4")
  (label "VCC" (at 10 10 0))
  (label "GND" (at 10 20 0))
  (label "SIG1" (at 10 30 0))
)
"""


# A PCB whose named-net set matches THREE_NET_SCH exactly (VCC, GND, SIG1).
# No drift -> blocker text is the normal "incomplete routing" form.
MATCHING_NETS_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (layer "Edge.Cuts"))
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
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG1"))
  )
)
"""


# A PCB with EXTRA nets (USB_CC1, USB_CC2, VBUS) that do not appear in
# THREE_NET_SCH. This mirrors the real board-03 condition (routed PCB has
# more nets than schematic) and intentionally exceeds the issue #3302
# ``_DRIFT_ADDED_ONLY_TOLERANCE`` headroom so a real drift still fires.
EXTRA_NET_DRIFT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")
  (net 4 "USB_CC1")
  (net 5 "USB_CC2")
  (net 6 "VBUS")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (layer "Edge.Cuts"))
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
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 4 "USB_CC1"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 25 10)
    (property "Reference" "R3")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 5 "USB_CC2"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 6 "VBUS"))
  )
)
"""


def _make_board_with_schematic(
    boards_dir: Path,
    name: str,
    *,
    sch_text: str,
    pcb_text: str,
    has_gerbers: bool = True,
    has_bom: bool = True,
    has_cpl: bool = True,
    has_manifest: bool = True,
) -> tuple[Path, Path]:
    """Build a board fixture that includes a ``.kicad_sch`` alongside the
    routed PCB so source-drift detection (issue #3280) has something to
    compare against.

    Returns ``(routed_pcb_path, schematic_path)``.
    """
    routed_pcb = make_fake_board(
        boards_dir,
        name,
        pcb_text=pcb_text,
        has_gerbers=has_gerbers,
        has_bom=has_bom,
        has_cpl=has_cpl,
        has_manifest=has_manifest,
    )
    # Schematic lives alongside the routed PCB under output/ to match the
    # real-board layout used by all in-repo boards (see boards/0*-*/output).
    sch_path = routed_pcb.parent / f"{name.replace('-', '_')}.kicad_sch"
    sch_path.write_text(sch_text)
    return routed_pcb, sch_path


class TestFleetStatusSourceDrift:
    """Issue #3280 -- routed PCB stale relative to source schematic.

    When a board's committed ``_routed.kicad_pcb`` carries a different
    set of named nets than its ``.kicad_sch`` (because the schematic was
    regenerated but the PCB was not re-routed), the ``X/Y nets`` figure
    derived from the PCB is meaningless as a current-routing signal. The
    fleet-status command must suppress the misleading
    ``incomplete routing (X/Y nets)`` blocker and surface a clearer
    ``routed PCB stale (schematic drift)`` blocker instead.

    Real-world trigger: board 03 (`03-usb-joystick`) reported
    ``1/16 nets`` from a stale PCB while ``kct route`` against the
    current schematic produces 11/13.
    """

    def test_drift_suppresses_incomplete_routing_blocker(self, tmp_path: Path, capsys):
        """Drift detected -> no ``incomplete routing`` blocker."""
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "drift-board",
            sch_text=THREE_NET_SCH,
            pcb_text=EXTRA_NET_DRIFT_PCB,
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        # The misleading "incomplete routing (X/Y nets)" blocker must
        # NOT fire -- this is the core regression guard.
        assert not any("incomplete routing" in blocker for blocker in b["blockers"]), b["blockers"]
        # A drift-specific blocker fires instead, and surfaces the
        # schematic net count (3) so triage can see the actual target.
        assert any(
            "routed PCB stale" in blocker and "schematic drift" in blocker
            for blocker in b["blockers"]
        ), b["blockers"]
        # JSON exposes the structured drift signal.
        routing = b["routing"]
        assert routing["source_stale"] is True
        assert routing["schematic_net_count"] == 3
        assert routing["pcb_net_count"] == 6
        assert "USB_CC1" in routing.get("drift_added", [])

    def test_no_drift_preserves_routing_blocker(self, tmp_path: Path, capsys):
        """No drift -> existing ``incomplete routing`` / ship behavior wins.

        Guards that boards with a fresh schematic-matching routed PCB
        still report routing status via the original code path.
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "matched-board",
            sch_text=THREE_NET_SCH,
            pcb_text=MATCHING_NETS_PCB,
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        routing = b["routing"]
        assert routing["source_stale"] is False
        assert routing["schematic_net_count"] == 3
        assert routing["pcb_net_count"] == 3
        # No drift blocker.
        assert not any("routed PCB stale" in blocker for blocker in b["blockers"]), b["blockers"]

    def test_missing_schematic_does_not_gate(self, tmp_path: Path, capsys):
        """No ``.kicad_sch`` -> drift detection is a no-op (back-compat).

        Boards without a schematic alongside the routed PCB cannot be
        evaluated for drift; the surveyor must NOT synthesize a false
        ``routed PCB stale`` blocker in that case. This preserves the
        pre-fix behavior for legacy/fixture boards.
        """
        boards = tmp_path / "boards"
        # make_fake_board creates ONLY a routed PCB -- no schematic.
        make_fake_board(
            boards,
            "no-sch-board",
            pcb_text=MATCHING_NETS_PCB,
            has_gerbers=True,
            has_bom=True,
            has_cpl=True,
            has_manifest=True,
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        routing = b["routing"]

        assert routing["source_stale"] is False
        assert routing["schematic_net_count"] is None
        assert not any("routed PCB stale" in blocker for blocker in b["blockers"])

    def test_drift_blocker_message_format(self, tmp_path: Path, capsys):
        """Blocker text surfaces both the schematic and PCB net counts.

        Curators reading the blocker should know exactly how far apart
        the two sides have drifted without parsing JSON.
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "drift-board",
            sch_text=THREE_NET_SCH,
            pcb_text=EXTRA_NET_DRIFT_PCB,
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        drift_blockers = [blocker for blocker in b["blockers"] if "routed PCB stale" in blocker]
        assert len(drift_blockers) == 1, b["blockers"]
        # Format: "routed PCB stale (schematic drift: N nets in schematic, M in PCB)"
        assert "3 nets in schematic" in drift_blockers[0]
        assert "6 in PCB" in drift_blockers[0]

    def test_drift_ship_ready_false(self, tmp_path: Path, capsys):
        """Drift forces ship_ready=False even when routing looks complete.

        The dangerous false-negative this guards: a stale PCB whose
        nets happen to be all 100% connected would otherwise read as
        ship-ready, hiding the schematic drift entirely. The drift
        blocker MUST gate ship-readiness.
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "drift-board",
            sch_text=THREE_NET_SCH,
            pcb_text=EXTRA_NET_DRIFT_PCB,
        )

        result = main(["status", "--boards-dir", str(boards), "--format", "json"])
        # Not ship-ready -> exit 2.
        assert result == 2
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]
        assert b["ship_ready"] is False
        assert b["routing"]["routing_complete"] is False


# ---------------------------------------------------------------------------
# Issue #3302: power-rail alias + unlabeled-local-net tolerance
# ---------------------------------------------------------------------------


# Schematic that uses the stock ``power:+3V3`` symbol name (KiCad
# convention) plus a labeled signal net SIG1.
POWER_ALIAS_SCH = """(kicad_sch
  (version 20240108)
  (generator "test")
  (paper "A4")
  (label "+3V3" (at 10 10 0))
  (label "GND" (at 10 20 0))
  (label "SIG1" (at 10 30 0))
)
"""


# PCB that uses the kicad-tools netlist-sync convention (``+3.3V``)
# for the same rail. Functionally identical to POWER_ALIAS_SCH.
POWER_ALIAS_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "+3.3V")
  (net 2 "GND")
  (net 3 "SIG1")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "+3.3V"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 1 "+3.3V"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG1"))
  )
)
"""


# PCB that adds TWO unlabeled-local synthesised nets (BOOT0, LED_K) on
# top of the matching schematic. This mirrors the board-04 condition:
# kicad-tools synthesises a net name during sync for short
# component-to-component segments that the schematic leaves unlabeled.
UNLABELED_LOCAL_NETS_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")
  (net 4 "BOOT0")
  (net 5 "LED_K")
  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)) (layer "Edge.Cuts"))
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
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 4 "BOOT0"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 25 10)
    (property "Reference" "R3")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 4 "BOOT0"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") (net 5 "LED_K"))
  )
)
"""


class TestFleetStatusDriftFalsePositiveFixes:
    """Issue #3302 -- false-positive drift on board 04 (and similar).

    Two distinct false-positive classes are tolerated here:

      1. Power-rail naming format mismatch (``+3V3`` vs ``+3.3V``):
         normalised by the canonical-form table in
         ``kicad_tools.schema.pcb.canonicalize_power_net``.

      2. Sub-threshold added-only diff: kicad-tools schematics routinely
         leave short component-to-component nets unlabeled, so the PCB
         net set is a strict superset of the schematic-label set even
         when the design is in sync. A residual added-only diff of at
         most ``_DRIFT_ADDED_ONLY_TOLERANCE`` (default 2) is attributed
         to unlabeled-local-net synthesis and not flagged.

    The regression we MUST preserve: PR #3289's board-03 protection
    where the PCB has more than two added nets relative to the
    schematic -- that still triggers ``routed PCB stale``.
    """

    def test_power_rail_alias_not_flagged(self, tmp_path: Path, capsys):
        """``+3V3`` (schematic) vs ``+3.3V`` (PCB) -> not source-stale.

        This is the board-04 root cause: KiCad's stock ``power:+3V3``
        symbol writes ``+3V3`` into the schematic-label set while the
        kicad-tools netlist-sync convention emits ``+3.3V`` into the
        PCB-net set. Both names refer to the same rail; the drift
        detector must canonicalise both sides before the diff.
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "alias-board",
            sch_text=POWER_ALIAS_SCH,
            pcb_text=POWER_ALIAS_PCB,
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        routing = b["routing"]
        assert routing["source_stale"] is False, (
            f"+3V3<->+3.3V should canonicalise; got {routing}"
        )
        # Raw counts are preserved for back-compat (issue #3302 AC).
        assert routing["schematic_net_count"] == 3
        assert routing["pcb_net_count"] == 3
        # No drift blocker.
        assert not any(
            "routed PCB stale" in blocker for blocker in b["blockers"]
        ), b["blockers"]

    def test_two_unlabeled_local_nets_not_flagged(self, tmp_path: Path, capsys):
        """<= 2 added unlabeled local nets in PCB -> not source-stale.

        Board 04's `LED_K` / `BOOT0` condition: short component-to-
        component segments are unlabeled in the schematic but
        synthesised in the PCB. The PCB-net set is a strict superset
        but the design is in sync.
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "unlabeled-board",
            sch_text=THREE_NET_SCH,  # VCC, GND, SIG1
            pcb_text=UNLABELED_LOCAL_NETS_PCB,  # ...+ BOOT0, LED_K
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        routing = b["routing"]
        assert routing["source_stale"] is False, (
            f"two added local nets should be tolerated; got {routing}"
        )
        # Raw counts are preserved for back-compat.
        assert routing["schematic_net_count"] == 3
        assert routing["pcb_net_count"] == 5
        # No drift blocker -- only routing/manufacturing gates remain.
        assert not any(
            "routed PCB stale" in blocker for blocker in b["blockers"]
        ), b["blockers"]

    def test_board03_style_three_adds_still_flagged(self, tmp_path: Path, capsys):
        """Three added non-alias nets -> still flagged (board 03 case).

        The regression guard for PR #3289: a real schematic drift
        where the PCB has three or more added nets (e.g. ``VBUS``,
        ``USB_CC1``, ``USB_CC2`` on board 03) is NOT masked by the
        sub-threshold tolerance.
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "real-drift-board",
            sch_text=THREE_NET_SCH,
            pcb_text=EXTRA_NET_DRIFT_PCB,  # adds USB_CC1, USB_CC2, VBUS
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        routing = b["routing"]
        assert routing["source_stale"] is True
        assert any(
            "routed PCB stale" in blocker for blocker in b["blockers"]
        ), b["blockers"]

    def test_removal_always_flagged(self, tmp_path: Path, capsys):
        """A removed schematic label is ALWAYS a real signal.

        The added-only tolerance only applies when there are zero
        removals. A net present in the schematic but missing from the
        PCB indicates the PCB was rebuilt against a stale schematic and
        must surface even if the count is sub-threshold.
        """
        # Schematic has one extra label (SIG_REMOVED) that the PCB lacks.
        sch_with_extra = """(kicad_sch
          (version 20240108)
          (generator "test")
          (paper "A4")
          (label "VCC" (at 10 10 0))
          (label "GND" (at 10 20 0))
          (label "SIG1" (at 10 30 0))
          (label "SIG_REMOVED" (at 10 40 0))
        )
        """
        boards = tmp_path / "boards"
        _make_board_with_schematic(
            boards,
            "removed-board",
            sch_text=sch_with_extra,
            pcb_text=MATCHING_NETS_PCB,  # VCC, GND, SIG1 only
        )

        main(["status", "--boards-dir", str(boards), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        b = data["boards"][0]

        routing = b["routing"]
        assert routing["source_stale"] is True
        assert "SIG_REMOVED" in routing.get("drift_removed", [])

    def test_canonicalize_power_net_unit(self):
        """Unit-level check on the canonicaliser used by the detector."""
        from kicad_tools.schema.pcb import canonicalize_power_net

        # Fractional rail forms canonicalise to ``+N.MV``.
        assert canonicalize_power_net("+3V3") == "+3.3V"
        assert canonicalize_power_net("+3.3V") == "+3.3V"
        assert canonicalize_power_net("+1V8") == "+1.8V"
        assert canonicalize_power_net("+1.8V") == "+1.8V"
        assert canonicalize_power_net("+2V5") == "+2.5V"
        assert canonicalize_power_net("-3V3") == "-3.3V"

        # Whole-volt forms canonicalise to ``+NV``.
        assert canonicalize_power_net("+5V") == "+5V"
        assert canonicalize_power_net("+5.0V") == "+5V"
        assert canonicalize_power_net("+12V") == "+12V"
        assert canonicalize_power_net("+12.0V") == "+12V"

        # Non-power names pass through unchanged.
        assert canonicalize_power_net("VBUS") == "VBUS"
        assert canonicalize_power_net("GND") == "GND"
        assert canonicalize_power_net("BOOT0") == "BOOT0"
        assert canonicalize_power_net("LED_K") == "LED_K"
        assert canonicalize_power_net("USB_CC1") == "USB_CC1"
        assert canonicalize_power_net("") == ""


# ---------------------------------------------------------------------------
# Drift detector extraction primitives -- issue #3370
# ---------------------------------------------------------------------------


class TestExtractSchematicNets:
    """Unit-level checks on :func:`_extract_schematic_nets`.

    Issue #3370: the drift detector must surface implicit globals
    published by ``power:`` symbol instances (e.g. ``+24V`` on board
    05's input power section), but must NOT surface ``PWR_FLAG`` -- a
    KiCad ERC convention symbol that doesn't represent its own net.
    """

    def _write_sch(self, tmp_path: Path, text: str) -> Path:
        sch = tmp_path / "test.kicad_sch"
        sch.write_text(text)
        return sch

    def test_label_extraction(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _extract_schematic_nets

        sch = self._write_sch(tmp_path, """(kicad_sch
          (label "SIG1" (at 10 10 0))
          (label "SIG2" (at 10 20 0))
          (global_label "GLOBAL_SIG" (at 10 30 0))
          (hierarchical_label "HIER_SIG" (at 10 40 0))
        )""")
        names = _extract_schematic_nets(sch)
        assert names == {"SIG1", "SIG2", "GLOBAL_SIG", "HIER_SIG"}

    def test_power_symbol_globals(self, tmp_path):
        """``power:+24V`` -> ``+24V`` implicit global."""
        from kicad_tools.cli.fleet_cmd import _extract_schematic_nets

        sch = self._write_sch(tmp_path, """(kicad_sch
          (symbol (lib_id "power:+24V") (at 10 10 0))
          (symbol (lib_id "power:GND") (at 10 20 0))
          (symbol (lib_id "power:+3V3") (at 10 30 0))
          (label "SIG1" (at 10 40 0))
        )""")
        names = _extract_schematic_nets(sch)
        assert names == {"+24V", "GND", "+3V3", "SIG1"}

    def test_pwr_flag_excluded(self, tmp_path):
        """``power:PWR_FLAG`` is an ERC marker, not a net publisher."""
        from kicad_tools.cli.fleet_cmd import _extract_schematic_nets

        sch = self._write_sch(tmp_path, """(kicad_sch
          (symbol (lib_id "power:PWR_FLAG") (at 10 10 0))
          (symbol (lib_id "power:VCC") (at 10 20 0))
          (label "SIG1" (at 10 30 0))
        )""")
        names = _extract_schematic_nets(sch)
        assert "PWR_FLAG" not in names
        assert names == {"VCC", "SIG1"}

    def test_net_placeholder_skipped(self, tmp_path):
        """``Net-(...)`` label placeholders are dropped."""
        from kicad_tools.cli.fleet_cmd import _extract_schematic_nets

        sch = self._write_sch(tmp_path, """(kicad_sch
          (label "Net-(R1-Pad2)" (at 10 10 0))
          (label "SIG1" (at 10 20 0))
        )""")
        names = _extract_schematic_nets(sch)
        assert names == {"SIG1"}

    def test_unreadable_returns_none(self, tmp_path):
        """Missing file yields ``None`` so the detector skips the gate."""
        from kicad_tools.cli.fleet_cmd import _extract_schematic_nets

        result = _extract_schematic_nets(tmp_path / "nonexistent.kicad_sch")
        assert result is None


class TestExtractPcbNamedNets:
    """Unit-level checks on :func:`_extract_pcb_named_nets`.

    Issue #3370: the function must drop PCB nets that exist only in
    routing artifacts (segments / vias / zones) with no pad
    attachment.  Those are routing leftovers from earlier sync rounds
    and don't represent meaningful schematic-vs-PCB drift.
    """

    def _write_pcb(self, tmp_path: Path, text: str) -> Path:
        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(text)
        return pcb

    def test_pad_attached_nets_kept(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _extract_pcb_named_nets

        pcb = self._write_pcb(tmp_path, """(kicad_pcb
          (net 0 "")
          (net 1 "VCC")
          (net 2 "GND")
          (footprint "R_0402"
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
            (pad "2" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
          )
        )""")
        names = _extract_pcb_named_nets(pcb)
        assert names == {"VCC", "GND"}

    def test_pad_orphan_nets_dropped(self, tmp_path):
        """Top-level ``(net N "X")`` with no pad reference is dropped.

        Mirrors board 05's ``PWR_LED`` / ``STATUS_LED`` leftover from
        an older sync round: the segments still reference the net but
        no pad does.  Without this filter, those names would surface
        as drift even though they only describe stale routing state.
        """
        from kicad_tools.cli.fleet_cmd import _extract_pcb_named_nets

        pcb = self._write_pcb(tmp_path, """(kicad_pcb
          (net 0 "")
          (net 1 "VCC")
          (net 2 "GND")
          (net 3 "STALE_SEG_NET")
          (footprint "R_0402"
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
            (pad "2" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
          )
          (segment (start 0 0) (end 1 1) (width 0.25) (layer "F.Cu") (net 3))
        )""")
        names = _extract_pcb_named_nets(pcb)
        assert "STALE_SEG_NET" not in names
        assert names == {"VCC", "GND"}

    def test_auto_generated_names_dropped(self, tmp_path):
        """``Net-(...)`` and ``unconnected-(...)`` names are dropped."""
        from kicad_tools.cli.fleet_cmd import _extract_pcb_named_nets

        pcb = self._write_pcb(tmp_path, """(kicad_pcb
          (net 0 "")
          (net 1 "VCC")
          (net 2 "Net-(R1-Pad2)")
          (net 3 "unconnected-(U1-NC-Pad5)")
          (footprint "R_0402"
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "VCC"))
            (pad "2" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "Net-(R1-Pad2)"))
          )
          (footprint "QFN-32"
            (pad "5" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "unconnected-(U1-NC-Pad5)"))
          )
        )""")
        names = _extract_pcb_named_nets(pcb)
        assert names == {"VCC"}

    def test_unreadable_returns_none(self, tmp_path):
        from kicad_tools.cli.fleet_cmd import _extract_pcb_named_nets

        result = _extract_pcb_named_nets(tmp_path / "nonexistent.kicad_pcb")
        assert result is None
