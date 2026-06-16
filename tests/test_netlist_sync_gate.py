"""Tests for the schematic/PCB drift wiring in route + check (issue #3154).

Covers:
- ``resolve_schematic_for_pcb`` discovery (sibling, project.kct, stage suffix,
  missing -> None).
- ``kct check --netlist-sync`` blocking gate (schematic-only -> exit 2,
  in-sync -> exit 0, PCB-only orphan -> exit 0 unless --strict, no schematic
  -> exit 1).
- Advisory drift banner on plain ``kct check`` and ``kct route`` (non-blocking).

Fixtures reuse the in-repo S-expression strings from ``test_pcb_sync_netlist``
(chorus-test is an external board and not available in-repo).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import main as check_main
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.sync.discover import resolve_schematic_for_pcb

# Reuse the in-repo S-expression fixtures from the sync-netlist test module.
# Ensure this directory is importable regardless of pytest collection order
# (the default prepend import mode only adds tests/ once a test in it loads).
sys.path.insert(0, str(Path(__file__).parent))

from test_pcb_sync_netlist import (  # noqa: E402
    MINIMAL_PCB_MATCHING,
    MINIMAL_SCHEMATIC,
    PCB_MISSING_R1,
    PCB_WITH_ORPHAN,
)


def _write_pair(directory: Path, basename: str, schematic: str, pcb: str) -> Path:
    """Write a matching-basename schematic+PCB pair so auto-discovery resolves.

    Returns the PCB path.
    """
    (directory / f"{basename}.kicad_sch").write_text(schematic)
    pcb_path = directory / f"{basename}.kicad_pcb"
    pcb_path.write_text(pcb)
    return pcb_path


# ---------------------------------------------------------------------------
# resolve_schematic_for_pcb
# ---------------------------------------------------------------------------


class TestResolveSchematicForPcb:
    def test_returns_sibling_schematic(self, tmp_path):
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, MINIMAL_PCB_MATCHING)
        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "board.kicad_sch"

    def test_returns_none_when_no_schematic(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)
        assert resolve_schematic_for_pcb(pcb) is None

    def test_strips_stage_suffix(self, tmp_path):
        # Schematic keeps the bare basename; the PCB carries a _routed suffix.
        (tmp_path / "board.kicad_sch").write_text(MINIMAL_SCHEMATIC)
        routed = tmp_path / "board_routed.kicad_pcb"
        routed.write_text(MINIMAL_PCB_MATCHING)
        resolved = resolve_schematic_for_pcb(routed)
        assert resolved == tmp_path / "board.kicad_sch"

    def test_honors_project_kct_artifacts_schematic(self, tmp_path):
        # project.kct points artifacts.schematic at a non-sibling name.
        (tmp_path / "custom_name.kicad_sch").write_text(MINIMAL_SCHEMATIC)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)
        (tmp_path / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "test"\n'
            "  artifacts:\n"
            '    schematic: "custom_name.kicad_sch"\n'
            '    pcb: "board.kicad_pcb"\n'
        )
        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "custom_name.kicad_sch"


# ---------------------------------------------------------------------------
# kct check --netlist-sync (blocking gate, AC #1 / AC #2)
# ---------------------------------------------------------------------------


class TestNetlistSyncGate:
    def test_schematic_only_drift_exits_nonzero(self, tmp_path, capsys):
        # PCB_MISSING_R1 has C1 only; schematic has R1+C1 -> R1 schematic-only.
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, PCB_MISSING_R1)
        rc = check_main([str(pcb), "--netlist-sync"])
        assert rc == 2
        out = capsys.readouterr().out
        # Names the schematic-only ref and the count delta.
        assert "R1" in out
        assert "1 schematic-only" in out
        assert "OUT OF SYNC" in out

    def test_in_sync_exits_zero(self, tmp_path, capsys):
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, MINIMAL_PCB_MATCHING)
        rc = check_main([str(pcb), "--netlist-sync"])
        assert rc == 0
        assert "IN SYNC" in capsys.readouterr().out

    def test_pcb_only_orphan_nonfatal_without_strict(self, tmp_path, capsys):
        # PCB_WITH_ORPHAN adds D1 (not in schematic) -> PCB-only orphan only.
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, PCB_WITH_ORPHAN)
        rc = check_main([str(pcb), "--netlist-sync"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "D1" in out
        assert "1 PCB-only" in out

    def test_pcb_only_orphan_fatal_with_strict(self, tmp_path):
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, PCB_WITH_ORPHAN)
        rc = check_main([str(pcb), "--netlist-sync", "--strict"])
        assert rc == 2

    def test_no_schematic_exits_one(self, tmp_path, capsys):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(MINIMAL_PCB_MATCHING)
        rc = check_main([str(pcb), "--netlist-sync"])
        assert rc == 1
        assert "none was found" in capsys.readouterr().err

    def test_explicit_schematic_override(self, tmp_path, capsys):
        # PCB next to a non-matching basename; point --schematic explicitly.
        sch = tmp_path / "other.kicad_sch"
        sch.write_text(MINIMAL_SCHEMATIC)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_MISSING_R1)
        rc = check_main([str(pcb), "--netlist-sync", "--schematic", str(sch)])
        assert rc == 2
        assert "R1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Advisory drift banner (AC #2 of the issue: route + plain check, non-blocking)
# ---------------------------------------------------------------------------


class TestAdvisoryBanner:
    def test_plain_check_prints_banner_without_changing_exit(self, tmp_path, capsys):
        # Drift present but DRC clean -> banner appears, exit stays 0.
        # The banner is routed to stderr (not stdout) so it does not pollute
        # ``--format json`` payloads consumed by the CI gate at
        # ``scripts/ci/check_routed_drc.py`` (see ``_emit_drift_banner``).
        #
        # Note (issue #3750): pass ``--drc-only`` so the new LVS meta
        # sub-check does not (correctly) flag the schematic/PCB pin
        # mismatch and flip the exit code to 2 -- that is the new default
        # contract.  This test is specifically asserting the *advisory
        # banner* contract, which is the pre-#3750 behaviour preserved
        # under ``--drc-only``.
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, PCB_MISSING_R1)
        rc = check_main([str(pcb), "--format", "summary", "--drc-only"])
        captured = capsys.readouterr()
        assert "PCB out of sync with schematic" in captured.err
        assert "1 schematic-only" in captured.err
        # Stdout stays clean -- only the DRC report (or JSON body) lands there.
        assert "PCB out of sync with schematic" not in captured.out
        # Banner alone does not flip the exit code (DRC found no errors).
        assert rc == 0

    def test_plain_check_no_banner_when_in_sync(self, tmp_path, capsys):
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, MINIMAL_PCB_MATCHING)
        check_main([str(pcb), "--format", "summary"])
        captured = capsys.readouterr()
        # Banner is suppressed entirely on both streams when in sync.
        assert "out of sync" not in captured.out.lower()
        assert "out of sync" not in captured.err.lower()

    def test_plain_check_no_banner_when_no_schematic(self, tmp_path, capsys):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(PCB_MISSING_R1)
        check_main([str(pcb), "--format", "summary"])
        captured = capsys.readouterr()
        assert "out of sync" not in captured.out.lower()
        assert "out of sync" not in captured.err.lower()

    def test_route_prints_banner(self, tmp_path, capsys):
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, PCB_MISSING_R1)
        rc = route_main([str(pcb), "--dry-run"])
        out = capsys.readouterr().out
        assert "PCB out of sync with schematic" in out
        # The banner is advisory; a successful dry-run still returns 0.
        assert rc == 0

    def test_route_no_sync_check_suppresses_banner(self, tmp_path, capsys):
        pcb = _write_pair(tmp_path, "board", MINIMAL_SCHEMATIC, PCB_MISSING_R1)
        route_main([str(pcb), "--dry-run", "--no-sync-check"])
        assert "out of sync" not in capsys.readouterr().out.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
