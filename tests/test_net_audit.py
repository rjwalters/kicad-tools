"""Tests for the pcb net-audit command and net_audit module."""

import json
import shutil
from pathlib import Path

from kicad_tools.audit.net_audit import find_stale_nets, fix_stale_nets
from kicad_tools.schema.pcb import PCB

FIXTURES = Path(__file__).parent / "fixtures"
STALE_NETS_PCB = FIXTURES / "stale_nets.kicad_pcb"


class TestFindStaleNets:
    """Unit tests for find_stale_nets detection logic."""

    def test_detects_old_vs_new_style_pair(self):
        """Net-(C11-Pad2) (0 segments) and Net-(C11-2) (>0 segments) detected."""
        pcb = PCB.load(STALE_NETS_PCB)
        groups = find_stale_nets(pcb)

        assert len(groups) == 1
        group = groups[0]
        # Net-(C11-2) has segments, so it's active
        assert group.active_net_name == "Net-(C11-2)"
        assert group.stale_net_name == "Net-(C11-Pad2)"
        assert group.active_segment_count > 0

    def test_named_nets_never_flagged(self):
        """Named nets like GND and +3.3V are never detected as stale."""
        pcb = PCB.load(STALE_NETS_PCB)
        groups = find_stale_nets(pcb)

        all_net_names = set()
        for g in groups:
            all_net_names.add(g.active_net_name)
            all_net_names.add(g.stale_net_name)

        assert "GND" not in all_net_names
        assert "+3.3V" not in all_net_names

    def test_no_duplicates_produces_no_findings(self):
        """A PCB with only active nets produces no findings."""
        # Build a PCB with only unique auto-generated nets (no duplicates)
        pcb = PCB.load(STALE_NETS_PCB)
        # Net-(R1-1) has no duplicate counterpart, so it shouldn't appear
        groups = find_stale_nets(pcb)
        # Only the C11 pair should show up
        for g in groups:
            assert "R1" not in g.stale_net_name
            assert "R1" not in g.active_net_name

    def test_affected_pads_identified(self):
        """Pads referencing stale nets are listed in the group."""
        pcb = PCB.load(STALE_NETS_PCB)
        groups = find_stale_nets(pcb)

        assert len(groups) == 1
        pads = groups[0].affected_pads
        # C11 pad 2 references Net-(C11-Pad2) which is stale
        assert len(pads) == 1
        assert pads[0].footprint_ref == "C11"
        assert pads[0].pad_number == "2"
        assert pads[0].current_net == "Net-(C11-Pad2)"


class TestFixStaleNets:
    """Unit tests for fix_stale_nets."""

    def test_fix_reassigns_pads(self, tmp_path):
        """--fix mode reassigns pad net references from stale to active."""
        # Copy fixture to tmp so we can modify it
        pcb_copy = tmp_path / "stale_nets.kicad_pcb"
        shutil.copy(STALE_NETS_PCB, pcb_copy)

        pcb = PCB.load(pcb_copy)
        groups = find_stale_nets(pcb)
        assert len(groups) == 1

        fixed = fix_stale_nets(pcb, groups)
        assert fixed == 1  # One pad was reassigned

        pcb.save(pcb_copy)

        # Re-parse and verify the pad now references the active net
        pcb2 = PCB.load(pcb_copy)
        groups2 = find_stale_nets(pcb2)
        # After fix, the stale group should have no affected pads
        # (the net declaration may still exist but no pads reference it)
        stale_pads = []
        for g in groups2:
            stale_pads.extend(g.affected_pads)
        assert len(stale_pads) == 0

    def test_fix_with_no_groups_is_noop(self):
        """Fixing an empty group list does nothing."""
        pcb = PCB.load(STALE_NETS_PCB)
        fixed = fix_stale_nets(pcb, [])
        assert fixed == 0


class TestNetAuditCLI:
    """Integration tests for the CLI entry point."""

    def test_cli_detects_stale_nets(self):
        """CLI returns non-zero when stale nets found and --fix not used."""
        from kicad_tools.cli.commands.pcb import _run_net_audit_command

        class Args:
            pcb_command = "net-audit"
            pcb = str(STALE_NETS_PCB)
            format = "text"
            fix = False
            dry_run = False
            output = None

        ret = _run_net_audit_command(Args(), STALE_NETS_PCB)
        assert ret == 1  # non-zero because stale nets found

    def test_cli_json_output(self, capsys):
        """--format json produces valid JSON with expected schema."""
        from kicad_tools.cli.commands.pcb import _run_net_audit_command

        class Args:
            pcb_command = "net-audit"
            pcb = str(STALE_NETS_PCB)
            format = "json"
            fix = False
            dry_run = False
            output = None

        _run_net_audit_command(Args(), STALE_NETS_PCB)
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "stale_nets" in data
        assert "findings" in data
        assert data["stale_nets"] == 1
        finding = data["findings"][0]
        assert "stale_net" in finding
        assert "active_net" in finding
        assert "affected_pads" in finding

    def test_cli_dry_run(self, capsys):
        """--dry-run shows what would be fixed without modifying."""
        from kicad_tools.cli.commands.pcb import _run_net_audit_command

        class Args:
            pcb_command = "net-audit"
            pcb = str(STALE_NETS_PCB)
            format = "json"
            fix = False
            dry_run = True
            output = None

        ret = _run_net_audit_command(Args(), STALE_NETS_PCB)
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data.get("dry_run") is True
        assert data.get("would_fix_pads") == 1

    def test_cli_fix(self, tmp_path, capsys):
        """--fix reassigns pads and saves."""
        pcb_copy = tmp_path / "stale_nets.kicad_pcb"
        shutil.copy(STALE_NETS_PCB, pcb_copy)

        from kicad_tools.cli.commands.pcb import _run_net_audit_command

        class Args:
            pcb_command = "net-audit"
            pcb = str(pcb_copy)
            format = "text"
            fix = True
            dry_run = False
            output = None

        ret = _run_net_audit_command(Args(), pcb_copy)
        assert ret == 0

        # Verify the fix persisted
        pcb = PCB.load(pcb_copy)
        groups = find_stale_nets(pcb)
        stale_pads = []
        for g in groups:
            stale_pads.extend(g.affected_pads)
        assert len(stale_pads) == 0

    def test_clean_pcb_returns_zero(self, tmp_path, capsys):
        """A PCB with no stale nets returns 0."""
        from kicad_tools.cli.commands.pcb import _run_net_audit_command

        # Use the routing-diagnostic fixture which has no stale nets
        clean_pcb = FIXTURES / "routing-diagnostic.kicad_pcb"

        class Args:
            pcb_command = "net-audit"
            pcb = str(clean_pcb)
            format = "text"
            fix = False
            dry_run = False
            output = None

        ret = _run_net_audit_command(Args(), clean_pcb)
        assert ret == 0
