"""Tests for pcb move-footprint command (pcb_move_footprint module)."""

import json

import pytest

MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Connector_JST:JST_XH_B2B"
    (layer "F.Cu")
    (uuid "fp-j2")
    (at 100 100)
    (property "Reference" "J2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x02" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole roundrect (at -1.25 0) (size 1.7 1.7) (layers "*.Cu") (net 1 "GND"))
    (pad "2" thru_hole roundrect (at 1.25 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
  )
  (footprint "Connector_JST:JST_XH_B3B"
    (layer "F.Cu")
    (uuid "fp-j3")
    (at 120 100 90)
    (property "Reference" "J3" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x03" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole roundrect (at -2.5 0) (size 1.7 1.7) (layers "*.Cu") (net 1 "GND"))
    (pad "2" thru_hole roundrect (at 0 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
    (pad "3" thru_hole roundrect (at 2.5 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
  )
)
"""

# Board with a non-zero origin: the Edge.Cuts rect starts at (116, 77), so
# PCB.board_origin == (116, 77).  The footprint's raw (at ...) is at the
# sheet-absolute coordinate (126, 87) which is board-relative (10, 10).
NONZERO_ORIGIN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_rect
    (start 116 77)
    (end 181 133)
    (layer "Edge.Cuts")
    (width 0.1)
    (uuid "edge-rect")
  )
  (footprint "Connector_JST:JST_XH_B2B"
    (layer "F.Cu")
    (uuid "fp-j2")
    (at 126 87)
    (property "Reference" "J2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x02" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole roundrect (at -1.25 0) (size 1.7 1.7) (layers "*.Cu") (net 1 "GND"))
    (pad "2" thru_hole roundrect (at 1.25 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
  )
  (footprint "Connector_JST:JST_XH_B3B"
    (layer "F.Cu")
    (uuid "fp-j3")
    (at 140 87 90)
    (property "Reference" "J3" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "Conn_01x03" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" thru_hole roundrect (at -2.5 0) (size 1.7 1.7) (layers "*.Cu") (net 1 "GND"))
    (pad "2" thru_hole roundrect (at 0 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
    (pad "3" thru_hole roundrect (at 2.5 0) (size 1.7 1.7) (layers "*.Cu") (net 0 ""))
  )
)
"""


def _at_node_values(pcb_path, uuid):
    """Return the raw footprint-level (at X Y) written into the S-expression.

    In every fixture here the footprint-level ``(at ...)`` node immediately
    follows the footprint ``(uuid ...)`` line, so we read the first ``(at ...)``
    after that uuid.
    """
    import re

    text = pcb_path.read_text()
    idx = text.index(f'(uuid "{uuid}")')
    after = text[idx:]
    m = re.search(r"\(at ([-\d.]+) ([-\d.]+)(?: [-\d.]+)?\)", after)
    assert m is not None, f"no (at ...) found after uuid {uuid}"
    return float(m.group(1)), float(m.group(2))


class TestRunMoveFootprint:
    """Tests for run_move_footprint function."""

    def test_moves_footprint_position(self, tmp_path):
        """Successfully moves a footprint to new coordinates."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25))
        assert rc == 0

        board = PCB.load(pcb)
        fp = board.get_footprint("J2")
        assert fp is not None
        assert fp.position[0] == pytest.approx(132.5, abs=0.01)
        assert fp.position[1] == pytest.approx(98.25, abs=0.01)

    def test_moves_footprint_with_rotation(self, tmp_path):
        """Moves a footprint and sets new rotation."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25), rotation=90.0)
        assert rc == 0

        board = PCB.load(pcb)
        fp = board.get_footprint("J2")
        assert fp is not None
        assert fp.position[0] == pytest.approx(132.5, abs=0.01)
        assert fp.position[1] == pytest.approx(98.25, abs=0.01)
        assert fp.rotation == pytest.approx(90.0, abs=0.01)

    def test_batch_mode(self, tmp_path):
        """Batch mode moves multiple footprints."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        batch = {
            "J2": {"x": 132.5, "y": 98.25},
            "J3": {"x": 140.0, "y": 98.25},
        }
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 0

        board = PCB.load(pcb)
        j2 = board.get_footprint("J2")
        j3 = board.get_footprint("J3")
        assert j2 is not None
        assert j3 is not None
        assert j2.position[0] == pytest.approx(132.5, abs=0.01)
        assert j3.position[0] == pytest.approx(140.0, abs=0.01)

    def test_batch_mode_with_rotation(self, tmp_path):
        """Batch mode supports per-footprint rotation."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        batch = {
            "J2": {"x": 132.5, "y": 98.25, "rotation": 180.0},
        }
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 0

        board = PCB.load(pcb)
        fp = board.get_footprint("J2")
        assert fp is not None
        assert fp.rotation == pytest.approx(180.0, abs=0.01)

    def test_dry_run_does_not_modify(self, tmp_path):
        """dry_run=True leaves file unchanged."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)
        original = pcb.read_text()

        rc = run_move_footprint(pcb, reference="J2", to=(200.0, 200.0), dry_run=True)
        assert rc == 0
        assert pcb.read_text() == original

    def test_nonexistent_reference_returns_1(self, tmp_path):
        """Moving a non-existent reference returns error."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="Z99", to=(100.0, 100.0))
        assert rc == 1

    def test_batch_partial_failure_is_atomic(self, tmp_path):
        """If any reference in batch is invalid, no footprints are moved."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        batch = {
            "J2": {"x": 200.0, "y": 200.0},
            "Z99": {"x": 300.0, "y": 300.0},
        }
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 1

        # J2 should not have moved
        board = PCB.load(pcb)
        j2 = board.get_footprint("J2")
        assert j2 is not None
        assert j2.position[0] == pytest.approx(100.0, abs=0.01)

    def test_output_path(self, tmp_path):
        """Writes to output path instead of overwriting input."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        out = tmp_path / "output.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(150.0, 150.0), output_path=out)
        assert rc == 0

        # Original should be unchanged
        board_orig = PCB.load(pcb)
        j2_orig = board_orig.get_footprint("J2")
        assert j2_orig is not None
        assert j2_orig.position[0] == pytest.approx(100.0, abs=0.01)

        # Output should have J2 moved
        board_out = PCB.load(out)
        j2_out = board_out.get_footprint("J2")
        assert j2_out is not None
        assert j2_out.position[0] == pytest.approx(150.0, abs=0.01)

    def test_json_output(self, tmp_path, capsys):
        """JSON output contains expected fields."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25), output_format="json")
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["moved"] is True
        assert len(data["moves"]) == 1
        assert data["moves"][0]["reference"] == "J2"
        assert data["moves"][0]["new_position"] == [132.5, 98.25]
        assert data["moves"][0]["old_position"] == [100.0, 100.0]

    def test_text_output(self, tmp_path, capsys):
        """Text output contains key information."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(132.5, 98.25), output_format="text")
        assert rc == 0

        captured = capsys.readouterr()
        assert "J2" in captured.out
        assert "Moved" in captured.out

    def test_round_trip_integrity(self, tmp_path):
        """Load, move, save, reload -- verify only target footprint changed."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        # Record J3's original position
        board_before = PCB.load(pcb)
        j3_before = board_before.get_footprint("J3")
        assert j3_before is not None
        j3_pos_before = j3_before.position
        j3_rot_before = j3_before.rotation

        # Move only J2
        rc = run_move_footprint(pcb, reference="J2", to=(150.0, 75.0))
        assert rc == 0

        # Reload and verify
        board_after = PCB.load(pcb)
        j2 = board_after.get_footprint("J2")
        j3 = board_after.get_footprint("J3")
        assert j2 is not None
        assert j3 is not None
        assert j2.position[0] == pytest.approx(150.0, abs=0.01)
        assert j2.position[1] == pytest.approx(75.0, abs=0.01)
        # J3 should be unchanged
        assert j3.position[0] == pytest.approx(j3_pos_before[0], abs=0.01)
        assert j3.position[1] == pytest.approx(j3_pos_before[1], abs=0.01)
        assert j3.rotation == pytest.approx(j3_rot_before, abs=0.01)


# A routed board: two 2-pad passives with traces landing exactly on pads.
#   R1 @ (100, 100): pad 1 -> (99, 100) net 1, pad 2 -> (101, 100) net 2
#   R3 @ (100, 120): pad 1 -> (99, 120) net 3 -- copper is NOT coincident
#     (seg-d starts at 95, 120), used to exercise the zero-match warning.
# seg-c terminates on R2's pad (129, 100) and must never move when R1 moves.
ROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
  (net 3 "N3")
  (footprint "R"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "N1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2 "N2"))
  )
  (footprint "R"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 130 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "N1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
  (footprint "R"
    (layer "F.Cu")
    (uuid "fp-r3")
    (at 100 120)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 3 "N3"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
  (segment (start 99 100) (end 90 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-a"))
  (segment (start 101 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-b"))
  (segment (start 129 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-c"))
  (segment (start 95 120) (end 90 120) (width 0.25) (layer "F.Cu") (net 3) (uuid "seg-d"))
)
"""

# Same routed board translated onto a non-zero board origin (116, 77).  In the
# tree, copper is sheet-absolute; after load, board_origin is subtracted so the
# in-memory view matches ROUTED_PCB.  R1's pad 1 is board-relative (99, 100).
ROUTED_NONZERO_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
  (gr_rect
    (start 116 77)
    (end 300 260)
    (layer "Edge.Cuts")
    (width 0.1)
    (uuid "edge-rect")
  )
  (footprint "R"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 216 177)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "N1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 2 "N2"))
  )
  (segment (start 215 177) (end 206 177) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-a"))
  (segment (start 217 177) (end 226 177) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg-b"))
)
"""

# KiCad-10 name-only dialect (#4416): pads and segments carry (net "N1") with
# no numeric id, so their net_number parses as 0.
ROUTED_NAME_ONLY_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
  (footprint "R"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "N1"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "N2"))
  )
  (segment (start 99 100) (end 90 100) (width 0.25) (layer "F.Cu") (net "N1") (uuid "seg-a"))
  (segment (start 101 100) (end 110 100) (width 0.25) (layer "F.Cu") (net "N2") (uuid "seg-b"))
)
"""


def _seg(pcb, uuid):
    """Return the Segment with the given uuid, or None."""
    for s in pcb.segments:
        if s.uuid == uuid:
            return s
    return None


class TestDragEndpoints:
    """Tests for move-footprint --drag-endpoints."""

    def test_drag_persists_after_reload(self, tmp_path):
        """Move + drag, save, RELOAD from disk: endpoints followed the pads.

        Reloading is the critical assertion -- Segment has no __setattr__ ->
        S-expression sync, so an in-memory-only check would pass even if the
        tree was never updated.
        """
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(pcb, reference="R1", to=(105.0, 100.0), drag_endpoints=True)
        assert rc == 0

        board = PCB.load(pcb)
        # R1 pad 1 was (99, 100) -> delta (5, 0): seg-a start follows.
        seg_a = _seg(board, "seg-a")
        assert seg_a is not None
        assert seg_a.start == pytest.approx((104.0, 100.0))
        # Far end of seg-a is untouched.
        assert seg_a.end == pytest.approx((90.0, 100.0))
        # R1 pad 2 was (101, 100): seg-b start follows.
        seg_b = _seg(board, "seg-b")
        assert seg_b is not None
        assert seg_b.start == pytest.approx((106.0, 100.0))
        assert seg_b.end == pytest.approx((110.0, 100.0))
        # UUIDs preserved by the in-place tree-sync strategy.
        assert {"seg-a", "seg-b", "seg-c", "seg-d"} <= {s.uuid for s in board.segments}

    def test_drag_leaves_other_nets_and_footprints_untouched(self, tmp_path):
        """seg-c (on R2's pad) is not moved when R1 moves."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(pcb, reference="R1", to=(105.0, 100.0), drag_endpoints=True)
        assert rc == 0

        board = PCB.load(pcb)
        seg_c = _seg(board, "seg-c")
        assert seg_c is not None
        assert seg_c.start == pytest.approx((129.0, 100.0))
        assert seg_c.end == pytest.approx((120.0, 100.0))

    def test_no_drag_flag_strands_traces(self, tmp_path):
        """Without --drag-endpoints the traces stay put (regression guard)."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(pcb, reference="R1", to=(105.0, 100.0))
        assert rc == 0

        board = PCB.load(pcb)
        seg_a = _seg(board, "seg-a")
        assert seg_a is not None
        # Endpoint unchanged -> stranded (the gap this feature fills).
        assert seg_a.start == pytest.approx((99.0, 100.0))

    def test_zero_match_warning_json(self, tmp_path, capsys):
        """Per-pad zero_match_warning surfaces in JSON output."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(
            pcb,
            reference="R3",
            to=(105.0, 120.0),
            drag_endpoints=True,
            output_format="json",
        )
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        drag = data["moves"][0]["drag"]
        # Only pad 1 (net 3) is reported; pad 2 (net 0) is unconnected + skipped.
        assert len(drag) == 1
        assert drag[0]["pad"] == "1"
        assert drag[0]["endpoints_dragged"] == 0
        assert drag[0]["zero_match_warning"] is True

    def test_zero_match_warning_text(self, tmp_path, capsys):
        """Zero-match warning is printed in text mode."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(pcb, reference="R3", to=(105.0, 120.0), drag_endpoints=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "WARNING" in out

    def test_per_pad_counts_in_json(self, tmp_path, capsys):
        """JSON reports per-pad drag counts and a total."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(
            pcb,
            reference="R1",
            to=(105.0, 100.0),
            drag_endpoints=True,
            output_format="json",
        )
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        assert data["drag_endpoints"] is True
        assert data["drag_tolerance"] == 0.05
        assert data["endpoints_dragged"] == 2
        counts = {d["pad"]: d["endpoints_dragged"] for d in data["moves"][0]["drag"]}
        assert counts == {"1": 1, "2": 1}

    def test_nonzero_origin_drag_persists(self, tmp_path):
        """Drag applies the board-origin offset when writing the tree."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_NONZERO_PCB)

        # R1 board-relative (100, 100) -> (105, 100): delta (5, 0).
        rc = run_move_footprint(pcb, reference="R1", to=(105.0, 100.0), drag_endpoints=True)
        assert rc == 0

        board = PCB.load(pcb)
        assert board.board_origin == pytest.approx((116.0, 77.0))
        seg_a = _seg(board, "seg-a")
        assert seg_a is not None
        # Board-relative endpoint follows the pad.
        assert seg_a.start == pytest.approx((104.0, 100.0))

        # And the raw tree stores sheet-absolute (104 + 116, 100 + 77).
        text = pcb.read_text()
        assert "(start 220 177)" in text

    def test_name_only_dialect_drag(self, tmp_path):
        """Name-only nets (net_number 0) drag via geometric fallback (#4416)."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_NAME_ONLY_PCB)

        rc = run_move_footprint(pcb, reference="R1", to=(105.0, 100.0), drag_endpoints=True)
        assert rc == 0

        board = PCB.load(pcb)
        seg_a = _seg(board, "seg-a")
        seg_b = _seg(board, "seg-b")
        assert seg_a is not None and seg_b is not None
        assert seg_a.start == pytest.approx((104.0, 100.0))
        assert seg_b.start == pytest.approx((106.0, 100.0))
        # The name-only dialect is preserved on save.
        assert '(net "N1")' in pcb.read_text()

    def test_map_batch_composes_with_drag(self, tmp_path):
        """--map batch mode applies the drag per moved footprint."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        batch = {
            "R1": {"x": 105.0, "y": 100.0},
            "R2": {"x": 135.0, "y": 100.0},
        }
        rc = run_move_footprint(pcb, batch_map=batch, drag_endpoints=True)
        assert rc == 0

        board = PCB.load(pcb)
        # R1 pad 1 -> seg-a start moved by (5, 0).
        assert _seg(board, "seg-a").start == pytest.approx((104.0, 100.0))
        # R2 pad 1 (129, 100) -> seg-c start moved by (5, 0).
        assert _seg(board, "seg-c").start == pytest.approx((134.0, 100.0))

    def test_dry_run_reports_drag_without_writing(self, tmp_path, capsys):
        """--dry-run reports intended drags but leaves the file untouched."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)
        original = pcb.read_text()

        rc = run_move_footprint(
            pcb,
            reference="R1",
            to=(105.0, 100.0),
            drag_endpoints=True,
            dry_run=True,
            output_format="json",
        )
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["endpoints_dragged"] == 2
        # File must be byte-identical -- nothing was written.
        assert pcb.read_text() == original

    def test_rotation_skips_drag_with_warning(self, tmp_path, capsys):
        """Combining --drag-endpoints with a rotation change skips the drag."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(ROUTED_PCB)

        rc = run_move_footprint(
            pcb,
            reference="R1",
            to=(105.0, 100.0),
            rotation=90.0,
            drag_endpoints=True,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "SKIPPED" in out

        board = PCB.load(pcb)
        # Footprint moved + rotated, but copper was NOT dragged.
        fp = board.get_footprint("R1")
        assert fp is not None
        assert fp.rotation == pytest.approx(90.0)
        seg_a = _seg(board, "seg-a")
        assert seg_a is not None
        assert seg_a.start == pytest.approx((99.0, 100.0))


class TestMoveFootprintCLIParser:
    """Tests for the move-footprint CLI parser."""

    def test_parser_has_move_footprint_subcommand(self):
        """Parser supports 'pcb move-footprint' subcommand."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "132.5",
                "98.25",
                "test.kicad_pcb",
            ]
        )
        assert args.pcb_command == "move-footprint"
        assert args.ref == "J2"
        assert args.to == [132.5, 98.25]

    def test_parser_rotation_flag(self):
        """Parser accepts --rotation flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "132.5",
                "98.25",
                "--rotation",
                "90",
                "test.kicad_pcb",
            ]
        )
        assert args.rotation == 90.0

    def test_parser_dry_run_flag(self):
        """Parser accepts --dry-run flag."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "132.5",
                "98.25",
                "--dry-run",
                "test.kicad_pcb",
            ]
        )
        assert args.dry_run is True

    def test_parser_map_flag(self):
        """Parser accepts --map flag for batch mode."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--map",
                '{"J2": {"x": 132.5, "y": 98.25}}',
                "test.kicad_pcb",
            ]
        )
        assert args.batch_map == '{"J2": {"x": 132.5, "y": 98.25}}'

    def test_dispatcher_integration(self, tmp_path):
        """Dispatcher correctly routes to move-footprint handler."""
        from kicad_tools.cli.commands.pcb import _run_move_footprint_command

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(MINIMAL_PCB)

        class Args:
            ref = "J2"
            to = [132.5, 98.25]
            rotation = None
            batch_map = None
            output = None
            dry_run = True
            format = "text"

        rc = _run_move_footprint_command(Args(), pcb)
        assert rc == 0


class TestNonZeroBoardOrigin:
    """Sanity checks on the fixture's board origin."""

    def test_board_origin_is_nonzero(self, tmp_path):
        """NONZERO_ORIGIN_PCB has board_origin (116, 77)."""
        from kicad_tools.schema.pcb import PCB

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        board = PCB.load(pcb)
        ox, oy = board.board_origin
        assert ox == pytest.approx(116.0, abs=0.01)
        assert oy == pytest.approx(77.0, abs=0.01)
        # J2's (at 126 87) is board-relative (10, 10) after origin detection.
        j2 = board.get_footprint("J2")
        assert j2 is not None
        assert j2.position[0] == pytest.approx(10.0, abs=0.01)
        assert j2.position[1] == pytest.approx(10.0, abs=0.01)


class TestAbsoluteCoordinates:
    """Tests for the --absolute coordinate mode."""

    def test_default_is_board_relative_nonzero_origin(self, tmp_path):
        """Default (no flag): --to X Y writes (at X+ox Y+oy)."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(0.0, 0.0))
        assert rc == 0

        # board-relative (0, 0) -> sheet-absolute (116, 77)
        ax, ay = _at_node_values(pcb, "fp-j2")
        assert ax == pytest.approx(116.0, abs=0.01)
        assert ay == pytest.approx(77.0, abs=0.01)

    def test_absolute_single_move_nonzero_origin(self, tmp_path):
        """--absolute --to X Y writes (at X Y) regardless of board origin."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        rc = run_move_footprint(pcb, reference="J2", to=(116.0, 77.0), absolute=True)
        assert rc == 0

        # absolute (116, 77) lands exactly at sheet-absolute (116, 77)
        ax, ay = _at_node_values(pcb, "fp-j2")
        assert ax == pytest.approx(116.0, abs=0.01)
        assert ay == pytest.approx(77.0, abs=0.01)

    def test_absolute_and_relative_agree_on_zero_origin(self, tmp_path):
        """On a board with origin (0,0) both modes write the same (at ...)."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        rel = tmp_path / "rel.kicad_pcb"
        absf = tmp_path / "abs.kicad_pcb"
        rel.write_text(MINIMAL_PCB)
        absf.write_text(MINIMAL_PCB)

        assert run_move_footprint(rel, reference="J2", to=(132.5, 98.25)) == 0
        assert run_move_footprint(absf, reference="J2", to=(132.5, 98.25), absolute=True) == 0

        assert _at_node_values(rel, "fp-j2") == _at_node_values(absf, "fp-j2")

    def test_absolute_batch_mode_nonzero_origin(self, tmp_path):
        """--absolute applies to every entry in batch --map mode."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        batch = {
            "J2": {"x": 130.0, "y": 90.0},
            "J3": {"x": 150.0, "y": 95.0},
        }
        rc = run_move_footprint(pcb, batch_map=batch, absolute=True)
        assert rc == 0

        j2x, j2y = _at_node_values(pcb, "fp-j2")
        j3x, j3y = _at_node_values(pcb, "fp-j3")
        assert (j2x, j2y) == pytest.approx((130.0, 90.0), abs=0.01)
        assert (j3x, j3y) == pytest.approx((150.0, 95.0), abs=0.01)

    def test_default_batch_mode_nonzero_origin(self, tmp_path):
        """Default batch --map mode is board-relative (adds origin)."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        batch = {"J2": {"x": 0.0, "y": 0.0}}
        rc = run_move_footprint(pcb, batch_map=batch)
        assert rc == 0

        ax, ay = _at_node_values(pcb, "fp-j2")
        assert (ax, ay) == pytest.approx((116.0, 77.0), abs=0.01)

    def test_json_reports_coordinate_space(self, tmp_path, capsys):
        """JSON output labels the active coordinate space and board origin."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        rc = run_move_footprint(
            pcb,
            reference="J2",
            to=(120.0, 80.0),
            absolute=True,
            output_format="json",
            dry_run=True,
        )
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        assert data["coordinate_space"] == "absolute"
        assert data["board_origin"] == [116.0, 77.0]
        # new_position reported in requested (absolute) space
        assert data["moves"][0]["new_position"] == [120.0, 80.0]

    def test_dry_run_text_labels_mode(self, tmp_path, capsys):
        """Text dry-run output labels the coordinate mode."""
        from kicad_tools.cli.pcb_move_footprint import run_move_footprint

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        # absolute mode
        run_move_footprint(pcb, reference="J2", to=(120.0, 80.0), absolute=True, dry_run=True)
        out = capsys.readouterr().out
        assert "absolute" in out

        # board-relative mode
        run_move_footprint(pcb, reference="J2", to=(5.0, 5.0), dry_run=True)
        out = capsys.readouterr().out
        assert "board-relative" in out


class TestAbsoluteParserAndDispatch:
    """Parser + dispatcher coverage for --absolute."""

    def test_parser_absolute_flag(self):
        """Parser accepts --absolute and stores it on args."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "116",
                "77",
                "--absolute",
                "test.kicad_pcb",
            ]
        )
        assert args.absolute is True

    def test_parser_absolute_defaults_false(self):
        """--absolute defaults to False when omitted."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "move-footprint",
                "--ref",
                "J2",
                "--to",
                "10",
                "10",
                "test.kicad_pcb",
            ]
        )
        assert args.absolute is False

    def test_dispatcher_plumbs_absolute(self, tmp_path):
        """Dispatcher forwards args.absolute into run_move_footprint."""
        from kicad_tools.cli.commands.pcb import _run_move_footprint_command

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text(NONZERO_ORIGIN_PCB)

        class Args:
            ref = "J2"
            to = [116.0, 77.0]
            rotation = None
            batch_map = None
            output = None
            dry_run = False
            format = "text"
            absolute = True

        rc = _run_move_footprint_command(Args(), pcb)
        assert rc == 0

        ax, ay = _at_node_values(pcb, "fp-j2")
        assert (ax, ay) == pytest.approx((116.0, 77.0), abs=0.01)
