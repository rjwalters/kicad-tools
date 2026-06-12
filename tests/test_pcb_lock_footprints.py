"""Tests for pcb lock-footprints / unlock-footprints commands.

Covers acceptance criteria for issue #2977:
1. ``--refs J1,J2`` sets ``(locked yes)`` on the selected footprints.
2. ``unlock-footprints --refs J1,J2`` clears the locks.
3. ``--all-perimeter`` locks footprints whose bbox touches the board edge.
4. Idempotent: locking an already-locked footprint is a no-op.
5. Round-tripping through ``PCB.load`` / ``.save`` preserves the flag.
"""

from __future__ import annotations

import json
from pathlib import Path

# Minimal PCB with an Edge.Cuts outline (10mm x 10mm), two
# perimeter-edge footprints (J1 at corner, J2 inboard), and a central
# footprint (U1) that should NOT be perimeter.
MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 10 0) (end 10 10) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 10 10) (end 0 10) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 10) (end 0 0) (layer "Edge.Cuts") (width 0.05))
  (net 0 "")
  (net 1 "GND")
  (footprint "Connector_JST:JST_XH_B2B"
    (layer "F.Cu")
    (uuid "fp-j1")
    (at 0.5 5)
    (property "Reference" "J1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x02" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 -1.25) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0 1.25) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Connector_JST:JST_XH_B3B"
    (layer "F.Cu")
    (uuid "fp-j2")
    (at 5 0.5)
    (property "Reference" "J2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x03" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at -1.25 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
    (pad "3" smd rect (at 1.25 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Package_DIP:DIP-8"
    (layer "F.Cu")
    (uuid "fp-u1")
    (at 5 5)
    (property "Reference" "U1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "DIP8" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at -1 -1) (size 1 1) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 1 1) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)
"""


def _write_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(MINIMAL_PCB)
    return pcb


class TestRunLockFootprints:
    """Tests for run_lock_footprints function."""

    def test_locks_refs(self, tmp_path):
        """--refs J1,J2 sets locked=True on both."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb, refs=["J1", "J2"])
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is True
        assert board.get_footprint("J2").locked is True
        # U1 was not touched
        assert board.get_footprint("U1").locked is False

    def test_unlock_refs(self, tmp_path):
        """unlock=True clears locked attribute on selected refs."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        # First lock them
        assert run_lock_footprints(pcb, refs=["J1", "J2"]) == 0
        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is True
        assert board.get_footprint("J2").locked is True

        # Then unlock
        rc = run_lock_footprints(pcb, refs=["J1", "J2"], unlock=True)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is False
        assert board.get_footprint("J2").locked is False

    def test_idempotent_lock(self, tmp_path):
        """Locking an already-locked footprint is a no-op (rc=0)."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        assert run_lock_footprints(pcb, refs=["J1"]) == 0
        # Second lock — still rc=0, still locked
        assert run_lock_footprints(pcb, refs=["J1"]) == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is True

    def test_idempotent_unlock_when_unlocked(self, tmp_path):
        """Unlocking an unlocked footprint is a no-op (rc=0)."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb, refs=["J1"], unlock=True)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is False

    def test_all_perimeter(self, tmp_path):
        """--all-perimeter selects footprints touching the board edge."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb, all_perimeter=True)
        assert rc == 0

        board = PCB.load(pcb)
        # J1 (at x=0.5, hits left edge) and J2 (at y=0.5, hits top edge)
        # both have bboxes within 2mm of the board edge.
        assert board.get_footprint("J1").locked is True
        assert board.get_footprint("J2").locked is True
        # U1 is at the center (5,5) → bbox well inside the board.
        assert board.get_footprint("U1").locked is False

    def test_all_perimeter_with_custom_margin(self, tmp_path):
        """Wider margin can pull in central footprints; narrow excludes."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        # 0.1mm tolerance — only footprints whose bbox literally touches
        # the edge.  J1 bbox: x in [-0.5, 0.5] → touches x=0; J2 bbox
        # y in [-0.5, 0.5] → touches y=0.
        rc = run_lock_footprints(
            pcb, all_perimeter=True, perimeter_margin=0.1
        )
        assert rc == 0
        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is True
        assert board.get_footprint("J2").locked is True
        assert board.get_footprint("U1").locked is False

    def test_missing_ref_returns_error(self, tmp_path):
        """Unknown ref produces rc=1 and no PCB modification."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb, refs=["J1", "DOES_NOT_EXIST"])
        assert rc == 1

        # Nothing was saved
        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is False

    def test_missing_args_returns_error(self, tmp_path):
        """Neither refs nor all_perimeter → rc=1."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb)
        assert rc == 1

    def test_mutually_exclusive_args_returns_error(self, tmp_path):
        """--refs and --all-perimeter together → rc=1."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb, refs=["J1"], all_perimeter=True)
        assert rc == 1

    def test_dry_run_does_not_write(self, tmp_path):
        """--dry-run reports changes but does not save."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        original = pcb.read_text()
        rc = run_lock_footprints(pcb, refs=["J1"], dry_run=True)
        assert rc == 0
        assert pcb.read_text() == original

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is False

    def test_output_path(self, tmp_path):
        """-o writes to alternative path, leaves input untouched."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        original = pcb.read_text()
        out = tmp_path / "locked.kicad_pcb"
        rc = run_lock_footprints(pcb, refs=["J1"], output_path=out)
        assert rc == 0
        assert out.exists()
        assert pcb.read_text() == original

        board = PCB.load(out)
        assert board.get_footprint("J1").locked is True

    def test_json_output(self, tmp_path, capsys):
        """--format json emits a parseable JSON report."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints

        pcb = _write_pcb(tmp_path)
        rc = run_lock_footprints(pcb, refs=["J1"], output_format="json")
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["operation"] == "lock"
        assert data["n_changed"] == 1
        assert data["written"] is True
        assert any(c["reference"] == "J1" and c["now_locked"] for c in data["changes"])

    def test_load_save_roundtrip_preserves_lock(self, tmp_path):
        """PCB.load -> .save preserves the locked flag set by this command."""
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        assert run_lock_footprints(pcb, refs=["J1"]) == 0

        # Round-trip once more
        board = PCB.load(pcb)
        out = tmp_path / "rt.kicad_pcb"
        board.save(out)

        board2 = PCB.load(out)
        assert board2.get_footprint("J1").locked is True
        assert board2.get_footprint("J2").locked is False


class TestCliEntryPoint:
    """Tests exercising the kct pcb lock-footprints CLI dispatch."""

    def test_cli_lock_via_parser(self, tmp_path):
        """End-to-end: parser -> dispatcher -> run_lock_footprints."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.cli.parser import create_parser
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        parser = create_parser()
        args = parser.parse_args([
            "pcb", "lock-footprints", str(pcb), "--refs", "J1,J2"
        ])
        rc = run_pcb_command(args)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is True
        assert board.get_footprint("J2").locked is True

    def test_cli_unlock_via_parser(self, tmp_path):
        """End-to-end unlock via parser."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.cli.parser import create_parser
        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        # Pre-lock J1
        assert run_lock_footprints(pcb, refs=["J1"]) == 0

        parser = create_parser()
        args = parser.parse_args([
            "pcb", "unlock-footprints", str(pcb), "--refs", "J1"
        ])
        rc = run_pcb_command(args)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is False

    def test_cli_refs_with_whitespace(self, tmp_path):
        """--refs splits on commas and trims whitespace."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.cli.parser import create_parser
        from kicad_tools.schema.pcb import PCB

        pcb = _write_pcb(tmp_path)
        parser = create_parser()
        args = parser.parse_args([
            "pcb", "lock-footprints", str(pcb), "--refs", " J1 , J2 "
        ])
        rc = run_pcb_command(args)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.get_footprint("J1").locked is True
        assert board.get_footprint("J2").locked is True


class TestSExpRoundtrip:
    """Verify the (locked) attribute survives KiCad s-expression round-trip."""

    def test_locked_attribute_in_sexp(self, tmp_path):
        """After lock, the .kicad_pcb file contains a top-level ``(locked yes)``.

        Issue #3457: the lock must be the MODERN top-level ``(locked yes)``
        form, NEVER the legacy KiCad-6 in-attr token (``(attr smd locked)``)
        -- KiCad 10's kicad-cli rejects the legacy form with "Failed to
        load board", silently breaking zone fill / DRC / gerber export.
        """
        import re

        from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints

        pcb = _write_pcb(tmp_path)
        assert run_lock_footprints(pcb, refs=["J1"]) == 0

        text = pcb.read_text()
        assert "locked" in text
        # Find J1 footprint block and confirm the lock landed there.
        # (Naive but sufficient for the fixture.)
        j1_start = text.find('"fp-j1"')
        assert j1_start >= 0
        # Search forward until the matching close-paren of J1's footprint.
        i = text.rfind("(footprint", 0, j1_start)
        assert i >= 0
        block_start = i
        depth = 0
        for j in range(block_start, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    block_end = j
                    break
        else:
            block_end = len(text)
        block = text[block_start:block_end]
        assert "(locked yes)" in block
        # The legacy in-attr token must never be emitted (issue #3457).
        assert not re.search(r"\(attr\s[^()]*\blocked\b", block), (
            "lock-footprints emitted the legacy in-attr 'locked' token; "
            "KiCad 10's kicad-cli rejects '(attr ... locked)'."
        )
