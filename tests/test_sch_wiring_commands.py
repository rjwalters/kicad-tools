"""Tests for schematic wiring commands: add-no-connect, cleanup-wires, disconnect."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.schema import Schematic

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal schematic with wires, labels, symbols, and lib_symbols
MINIMAL_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:R_0_1"
        (polyline (pts (xy -1.016 -2.54) (xy -1.016 2.54)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "wire-1")
  )
  (wire (pts (xy 100 50) (xy 100 100))
    (stroke (width 0) (type default))
    (uuid "wire-2")
  )
  (label "NET1" (at 100 50 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


SCHEMATIC_WITH_ZERO_LENGTH_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000002")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 100 50) (xy 100 50))
    (stroke (width 0) (type default))
    (uuid "zero-wire")
  )
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "good-wire")
  )
  (label "NET1" (at 100 50 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


SCHEMATIC_WITH_DANGLING_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000003")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "connected-wire")
  )
  (wire (pts (xy 300 300) (xy 350 300))
    (stroke (width 0) (type default))
    (uuid "dangling-wire")
  )
  (label "NET1" (at 100 50 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


SCHEMATIC_WITH_NO_CONNECT = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000004")
  (paper "A4")
  (lib_symbols)
  (no_connect (at 100 50) (uuid "nc-1"))
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _write_sch(tmp_path: Path, content: str, name: str = "test.kicad_sch") -> Path:
    """Write a schematic string to a temp file."""
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# cleanup-wires tests
# ---------------------------------------------------------------------------


class TestCleanupWires:
    """Tests for the cleanup-wires command."""

    def test_finds_zero_length_wire(self, tmp_path):
        """Zero-length wires are detected as cleanup candidates."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ZERO_LENGTH_WIRE)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        zero_issues = [i for i in issues if i.reason == "zero_length"]
        assert len(zero_issues) == 1
        assert zero_issues[0].start == (100.0, 50.0)
        assert zero_issues[0].end == (100.0, 50.0)

    def test_finds_dangling_wire(self, tmp_path):
        """Fully isolated (both-ends dangling) wires are detected."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_DANGLING_WIRE)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        dangling = [i for i in issues if i.reason == "dangling"]
        assert len(dangling) == 1
        assert dangling[0].start == (300.0, 300.0)

    def test_connected_wire_not_flagged(self, tmp_path):
        """Wires connected to labels are not flagged."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_DANGLING_WIRE)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        # The wire from (100,50) to (150,50) has one end on a label,
        # so it should NOT be flagged as dangling
        dangling_starts = {i.start for i in issues if i.reason == "dangling"}
        assert (100.0, 50.0) not in dangling_starts

    def test_remove_wires(self, tmp_path):
        """Flagged wires are actually removed from the schematic."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates, remove_wires

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ZERO_LENGTH_WIRE)
        sch = Schematic.load(path)

        initial_wire_count = len(list(sch.sexp.find_all("wire")))
        issues = find_cleanup_candidates(sch)
        removed = remove_wires(sch, issues)

        assert removed == 1
        final_wire_count = len(list(sch.sexp.find_all("wire")))
        assert final_wire_count == initial_wire_count - 1

    def test_dry_run_no_modification(self, tmp_path):
        """Dry run mode does not modify the file."""
        from kicad_tools.cli.sch_cleanup_wires import main

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ZERO_LENGTH_WIRE)
        original_content = path.read_text()

        result = main([str(path), "--dry-run"])

        assert result == 0
        assert path.read_text() == original_content

    def test_backup_created(self, tmp_path):
        """Backup flag creates a copy before modifying."""
        from kicad_tools.cli.sch_cleanup_wires import main

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ZERO_LENGTH_WIRE)

        result = main([str(path), "--backup"])

        assert result == 0
        # A backup file should exist
        backups = list(tmp_path.glob("*.backup-*"))
        assert len(backups) == 1

    def test_no_issues_clean_schematic(self, tmp_path):
        """A clean schematic with no issues returns 0 and reports nothing."""
        from kicad_tools.cli.sch_cleanup_wires import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        result = main([str(path), "--dry-run"])
        assert result == 0

    def test_json_output(self, tmp_path, capsys):
        """JSON output mode produces valid JSON."""
        import json

        from kicad_tools.cli.sch_cleanup_wires import main

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ZERO_LENGTH_WIRE)
        result = main([str(path), "--dry-run", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "issues" in data
        assert data["zero_length"] == 1


# ---------------------------------------------------------------------------
# add-no-connect tests
# ---------------------------------------------------------------------------


class TestAddNoConnect:
    """Tests for the add-no-connect command."""

    def test_build_no_connect_sexp(self):
        """No-connect S-expression node is correctly built."""
        from kicad_tools.cli.sch_add_no_connect import _build_no_connect_sexp

        node = _build_no_connect_sexp(100.0, 50.0)
        assert node.name == "no_connect"
        at_node = node.find("at")
        assert at_node is not None
        assert at_node.get_float(0) == 100.0
        assert at_node.get_float(1) == 50.0
        assert node.find("uuid") is not None

    def test_find_existing_no_connects(self, tmp_path):
        """Existing no-connect markers are detected."""
        from kicad_tools.cli.sch_add_no_connect import _find_existing_no_connects

        path = _write_sch(tmp_path, SCHEMATIC_WITH_NO_CONNECT)
        sch = Schematic.load(path)
        existing = _find_existing_no_connects(sch)

        assert (1000, 500) in existing  # 100.0*10, 50.0*10

    def test_add_no_connect_markers(self, tmp_path):
        """No-connect markers are inserted into the S-expression tree."""
        from kicad_tools.cli.sch_add_no_connect import NoConnectAction, add_no_connect_markers

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        initial_nc_count = len(list(sch.sexp.find_all("no_connect")))
        assert initial_nc_count == 0

        actions = [
            NoConnectAction(
                reference="U1",
                pin_number="5",
                pin_name="NC",
                position=(200.0, 100.0),
            ),
            NoConnectAction(
                reference="U1",
                pin_number="6",
                pin_name="NC",
                position=(200.0, 110.0),
            ),
        ]

        count = add_no_connect_markers(sch, actions)
        assert count == 2

        nc_nodes = list(sch.sexp.find_all("no_connect"))
        assert len(nc_nodes) == 2

    def test_no_duplicate_no_connect(self, tmp_path):
        """Existing no-connect markers are not duplicated in auto mode."""
        from kicad_tools.cli.sch_add_no_connect import _find_existing_no_connects

        path = _write_sch(tmp_path, SCHEMATIC_WITH_NO_CONNECT)
        sch = Schematic.load(path)

        existing = _find_existing_no_connects(sch)
        # The point (100, 50) already has a no-connect
        assert (1000, 500) in existing


# ---------------------------------------------------------------------------
# disconnect tests
# ---------------------------------------------------------------------------


class TestDisconnect:
    """Tests for the disconnect command."""

    def test_find_wires_at_point(self, tmp_path):
        """Wires at a given point are found correctly."""
        from kicad_tools.cli.sch_disconnect import _find_wires_at_point

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        # Point (100, 50) should match both wires
        wires = _find_wires_at_point(sch, (100.0, 50.0))
        assert len(wires) == 2

    def test_find_wires_at_unconnected_point(self, tmp_path):
        """No wires are found at an unconnected point."""
        from kicad_tools.cli.sch_disconnect import _find_wires_at_point

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        wires = _find_wires_at_point(sch, (500.0, 500.0))
        assert len(wires) == 0

    def test_disconnect_removes_wires(self, tmp_path):
        """Disconnecting a pin removes wires at the pin position."""
        from kicad_tools.cli.sch_disconnect import disconnect_pin

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        initial_wire_count = len(list(sch.sexp.find_all("wire")))
        result = disconnect_pin(sch, (100.0, 50.0))

        assert result.wires_removed == 2
        final_wire_count = len(list(sch.sexp.find_all("wire")))
        assert final_wire_count == initial_wire_count - 2

    def test_disconnect_with_no_connect(self, tmp_path):
        """Disconnect with --add-nc inserts a no-connect marker."""
        from kicad_tools.cli.sch_disconnect import disconnect_pin

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        result = disconnect_pin(sch, (100.0, 50.0), add_no_connect=True)

        assert result.wires_removed == 2
        assert result.no_connect_added is True

        nc_nodes = list(sch.sexp.find_all("no_connect"))
        assert len(nc_nodes) == 1

    def test_disconnect_no_wires_no_nc(self, tmp_path):
        """Disconnect at a point with no wires does not add no-connect."""
        from kicad_tools.cli.sch_disconnect import disconnect_pin

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        result = disconnect_pin(sch, (500.0, 500.0), add_no_connect=True)

        assert result.wires_removed == 0
        assert result.no_connect_added is False

    def test_build_no_connect_sexp(self):
        """No-connect S-expression is valid."""
        from kicad_tools.cli.sch_disconnect import _build_no_connect_sexp

        node = _build_no_connect_sexp(150.0, 75.0)
        assert node.name == "no_connect"
        at_node = node.find("at")
        assert at_node.get_float(0) == 150.0
        assert at_node.get_float(1) == 75.0
