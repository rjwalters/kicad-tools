"""Tests for schematic wiring commands: add-no-connect, cleanup-wires, disconnect, remove-wire."""

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


SCHEMATIC_WITH_DUPLICATE_WIRES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000005")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 116.84 149.86) (xy 116.84 142.24))
    (stroke (width 0) (type default))
    (uuid "dup-wire-1")
  )
  (wire (pts (xy 116.84 142.24) (xy 116.84 149.86))
    (stroke (width 0) (type default))
    (uuid "dup-wire-2")
  )
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "unique-wire")
  )
  (label "NET1" (at 100 50 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-1")
  )
  (label "NET2" (at 116.84 149.86 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-2")
  )
  (label "NET3" (at 116.84 142.24 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-3")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


SCHEMATIC_WITH_SAME_ORDER_DUPLICATE_WIRES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000006")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "dup-same-1")
  )
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "dup-same-2")
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


# Schematic with a resistor at (100, 50) and wires connecting to its actual
# pin positions (pin 1 at y=53.81, pin 2 at y=46.19) -- NOT the symbol center.
# The old center-based heuristic would incorrectly flag these as dangling.
SCHEMATIC_WITH_PIN_CONNECTED_WIRES = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000020")
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
  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "sym-r1")
    (property "Reference" "R1" (at 100 50 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 50 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-r1-1"))
    (pin "2" (uuid "pin-r1-2"))
    (instances (project "test" (path "/" (reference "R1") (unit 1))))
  )
  (wire (pts (xy 100 53.81) (xy 100 60))
    (stroke (width 0) (type default))
    (uuid "wire-pin1")
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default))
    (uuid "wire-pin2")
  )
  (label "VCC" (at 100 60 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-vcc")
  )
  (label "GND" (at 100 40 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-gnd")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


# Schematic with a rotated resistor (90 degrees) and wires to its pins.
# At rotation=90, pin 1 (originally at 0, 3.81) maps to (3.81, 0) from center,
# and pin 2 (originally at 0, -3.81) maps to (-3.81, 0) from center.
# Symbol at (100, 50): pin 1 at (103.81, 50), pin 2 at (96.19, 50).
SCHEMATIC_WITH_ROTATED_SYMBOL = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000021")
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
  (symbol (lib_id "Device:R") (at 100 50 90) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "sym-r2")
    (property "Reference" "R1" (at 100 50 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 50 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-r2-1"))
    (pin "2" (uuid "pin-r2-2"))
    (instances (project "test" (path "/" (reference "R1") (unit 1))))
  )
  (wire (pts (xy 103.81 50) (xy 110 50))
    (stroke (width 0) (type default))
    (uuid "wire-rot-pin1")
  )
  (wire (pts (xy 96.19 50) (xy 90 50))
    (stroke (width 0) (type default))
    (uuid "wire-rot-pin2")
  )
  (label "VCC" (at 110 50 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-vcc")
  )
  (label "GND" (at 90 50 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-gnd")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


# Schematic with a symbol but no lib_symbols entry -- tests graceful fallback
SCHEMATIC_WITH_MISSING_LIB_SYMBOL = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000022")
  (paper "A4")
  (lib_symbols)
  (symbol (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "sym-missing")
    (property "Reference" "R1" (at 100 50 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 50 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pin-m1"))
    (pin "2" (uuid "pin-m2"))
    (instances (project "test" (path "/" (reference "R1") (unit 1))))
  )
  (wire (pts (xy 100 50) (xy 100 60))
    (stroke (width 0) (type default))
    (uuid "wire-center")
  )
  (label "NET1" (at 100 60 0)
    (effects (font (size 1.27 1.27)))
    (uuid "label-net1")
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

    def test_finds_duplicate_wires_reversed_endpoints(self, tmp_path):
        """Duplicate wires with reversed endpoint order are detected."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_DUPLICATE_WIRES)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        duplicates = [i for i in issues if i.reason == "duplicate"]
        assert len(duplicates) == 1
        # The second wire (reversed endpoints) should be the duplicate
        assert duplicates[0].start == (116.84, 142.24)
        assert duplicates[0].end == (116.84, 149.86)

    def test_finds_duplicate_wires_same_order(self, tmp_path):
        """Duplicate wires with identical endpoint order are detected."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_SAME_ORDER_DUPLICATE_WIRES)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        duplicates = [i for i in issues if i.reason == "duplicate"]
        assert len(duplicates) == 1

    def test_remove_duplicate_wires(self, tmp_path):
        """Duplicate wires are removed, keeping exactly one copy."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates, remove_wires

        path = _write_sch(tmp_path, SCHEMATIC_WITH_DUPLICATE_WIRES)
        sch = Schematic.load(path)

        initial_wire_count = len(list(sch.sexp.find_all("wire")))
        issues = find_cleanup_candidates(sch)
        removed = remove_wires(sch, issues)

        assert removed == 1
        final_wire_count = len(list(sch.sexp.find_all("wire")))
        assert final_wire_count == initial_wire_count - 1

    def test_duplicate_wires_dry_run_json(self, tmp_path, capsys):
        """JSON output includes duplicate wire count."""
        import json

        from kicad_tools.cli.sch_cleanup_wires import main

        path = _write_sch(tmp_path, SCHEMATIC_WITH_DUPLICATE_WIRES)
        result = main([str(path), "--dry-run", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["duplicate"] == 1
        assert any(i["reason"] == "duplicate" for i in data["issues"])

    def test_duplicate_wires_dry_run_text(self, tmp_path, capsys):
        """Text output reports duplicate wires in dry-run mode."""
        from kicad_tools.cli.sch_cleanup_wires import main

        path = _write_sch(tmp_path, SCHEMATIC_WITH_DUPLICATE_WIRES)
        result = main([str(path), "--dry-run"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Duplicate: 1" in captured.out
        assert "[duplicate]" in captured.out

    def test_wire_to_pin_not_flagged_as_dangling(self, tmp_path):
        """Wires connected to actual component pin positions are not flagged."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_PIN_CONNECTED_WIRES)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        dangling = [i for i in issues if i.reason == "dangling"]
        assert len(dangling) == 0, (
            f"Expected no dangling wires but found {len(dangling)}: "
            f"{[(d.start, d.end) for d in dangling]}"
        )

    def test_rotated_symbol_pin_wires_not_flagged(self, tmp_path):
        """Wires to pins on a 90-degree rotated symbol are not flagged."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ROTATED_SYMBOL)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        dangling = [i for i in issues if i.reason == "dangling"]
        assert len(dangling) == 0, (
            f"Expected no dangling wires but found {len(dangling)}: "
            f"{[(d.start, d.end) for d in dangling]}"
        )

    def test_missing_lib_symbol_falls_back_to_center(self, tmp_path):
        """When library symbol is missing, symbol center is used as fallback."""
        from kicad_tools.cli.sch_cleanup_wires import find_cleanup_candidates

        path = _write_sch(tmp_path, SCHEMATIC_WITH_MISSING_LIB_SYMBOL)
        sch = Schematic.load(path)
        issues = find_cleanup_candidates(sch)

        # Wire from (100, 50) to (100, 60): one end at symbol center (fallback),
        # other end at a label -- should NOT be flagged as dangling
        dangling = [i for i in issues if i.reason == "dangling"]
        assert len(dangling) == 0


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


# ---------------------------------------------------------------------------
# remove-wire tests
# ---------------------------------------------------------------------------

# Schematic with a junction at a 3-way intersection
SCHEMATIC_WITH_JUNCTION = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000010")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "wire-j1")
  )
  (wire (pts (xy 150 50) (xy 200 50))
    (stroke (width 0) (type default))
    (uuid "wire-j2")
  )
  (wire (pts (xy 150 50) (xy 150 100))
    (stroke (width 0) (type default))
    (uuid "wire-j3")
  )
  (junction (at 150 50) (diameter 0) (color 0 0 0 0)
    (uuid "junc-1")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Schematic with a junction at a 4-way intersection
SCHEMATIC_WITH_4WAY_JUNCTION = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000011")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 100 50) (xy 150 50))
    (stroke (width 0) (type default))
    (uuid "wire-4w1")
  )
  (wire (pts (xy 150 50) (xy 200 50))
    (stroke (width 0) (type default))
    (uuid "wire-4w2")
  )
  (wire (pts (xy 150 50) (xy 150 100))
    (stroke (width 0) (type default))
    (uuid "wire-4w3")
  )
  (wire (pts (xy 150 50) (xy 150 0))
    (stroke (width 0) (type default))
    (uuid "wire-4w4")
  )
  (junction (at 150 50) (diameter 0) (color 0 0 0 0)
    (uuid "junc-4w")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""

# Schematic with a zero-length wire for edge case testing
SCHEMATIC_WITH_ZERO_LENGTH_WIRE_REMOVE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000012")
  (paper "A4")
  (lib_symbols)
  (wire (pts (xy 100 50) (xy 100 50))
    (stroke (width 0) (type default))
    (uuid "zero-wire-rm")
  )
  (wire (pts (xy 200 50) (xy 250 50))
    (stroke (width 0) (type default))
    (uuid "normal-wire-rm")
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


class TestRemoveWire:
    """Tests for the remove-wire command."""

    def test_find_wire_by_endpoints_exact(self, tmp_path):
        """Wire is found by exact endpoint coordinates."""
        from kicad_tools.cli.sch_remove_wire import find_wire_by_endpoints

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        wire = find_wire_by_endpoints(sch, (100.0, 50.0), (150.0, 50.0))
        assert wire is not None

    def test_find_wire_order_insensitive(self, tmp_path):
        """Wire matching is order-insensitive (--from A --to B matches B->A)."""
        from kicad_tools.cli.sch_remove_wire import find_wire_by_endpoints

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        # Reversed order should still match
        wire = find_wire_by_endpoints(sch, (150.0, 50.0), (100.0, 50.0))
        assert wire is not None

    def test_find_nearest_wire(self, tmp_path):
        """Nearest wire is found by proximity to a point."""
        from kicad_tools.cli.sch_remove_wire import _wire_start_end, find_nearest_wire

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        # Point near (150, 50) should find the horizontal wire
        wire = find_nearest_wire(sch, (149.0, 50.0))
        assert wire is not None
        start, end = _wire_start_end(wire)
        # The nearest endpoint is (150, 50) which belongs to wire-1
        assert (start == (100.0, 50.0) and end == (150.0, 50.0)) or (
            start == (150.0, 50.0) and end == (100.0, 50.0)
        )

    def test_tolerance_matching(self, tmp_path):
        """Wire at (100, 50) matches query for (100.5, 50.5) within tolerance."""
        from kicad_tools.cli.sch_remove_wire import find_wire_by_endpoints

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        # Slightly off coordinates within default 1.27mm tolerance
        wire = find_wire_by_endpoints(sch, (100.5, 50.5), (150.5, 50.5))
        assert wire is not None

    def test_no_match_returns_none(self, tmp_path):
        """Query for non-existent coordinates returns None."""
        from kicad_tools.cli.sch_remove_wire import find_wire_by_endpoints

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        wire = find_wire_by_endpoints(sch, (999.0, 999.0), (888.0, 888.0))
        assert wire is None

    def test_remove_wire_decreases_count(self, tmp_path):
        """Removing a wire decreases wire count by 1."""
        from kicad_tools.cli.sch_remove_wire import (
            find_wire_by_endpoints,
            remove_wire_and_orphan_junctions,
        )

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch = Schematic.load(path)

        initial_count = len(list(sch.sexp.find_all("wire")))
        wire = find_wire_by_endpoints(sch, (100.0, 50.0), (150.0, 50.0))
        assert wire is not None

        removed, _ = remove_wire_and_orphan_junctions(sch, wire)
        assert removed is True
        assert len(list(sch.sexp.find_all("wire"))) == initial_count - 1

    def test_orphan_junction_cleanup(self, tmp_path):
        """Junction is removed when only 2 wires remain at that point."""
        from kicad_tools.cli.sch_remove_wire import (
            find_wire_by_endpoints,
            remove_wire_and_orphan_junctions,
        )

        path = _write_sch(tmp_path, SCHEMATIC_WITH_JUNCTION)
        sch = Schematic.load(path)

        # Initially 1 junction
        assert len(list(sch.sexp.find_all("junction"))) == 1

        # Remove one wire from the 3-way junction -> only 2 wires remain
        wire = find_wire_by_endpoints(sch, (150.0, 50.0), (150.0, 100.0))
        assert wire is not None

        _, junctions_removed = remove_wire_and_orphan_junctions(sch, wire)
        assert junctions_removed == 1
        assert len(list(sch.sexp.find_all("junction"))) == 0

    def test_junction_preserved_at_3plus_way(self, tmp_path):
        """Junction at 4-way intersection is NOT removed when one wire is deleted."""
        from kicad_tools.cli.sch_remove_wire import (
            find_wire_by_endpoints,
            remove_wire_and_orphan_junctions,
        )

        path = _write_sch(tmp_path, SCHEMATIC_WITH_4WAY_JUNCTION)
        sch = Schematic.load(path)

        assert len(list(sch.sexp.find_all("junction"))) == 1

        # Remove one wire from the 4-way junction -> 3 wires remain
        wire = find_wire_by_endpoints(sch, (150.0, 50.0), (150.0, 0.0))
        assert wire is not None

        _, junctions_removed = remove_wire_and_orphan_junctions(sch, wire)
        assert junctions_removed == 0
        assert len(list(sch.sexp.find_all("junction"))) == 1

    def test_dry_run_no_modification(self, tmp_path):
        """Dry run mode does not modify the file."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        original_content = path.read_text()

        result = main([str(path), "--from", "100", "50", "--to", "150", "50", "--dry-run"])

        assert result == 0
        assert path.read_text() == original_content

    def test_backup_created(self, tmp_path):
        """Backup flag creates a copy before modifying."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)

        result = main([str(path), "--from", "100", "50", "--to", "150", "50", "--backup"])

        assert result == 0
        backups = list(tmp_path.glob("*.backup-*"))
        assert len(backups) == 1

    def test_cli_round_trip_endpoints(self, tmp_path):
        """CLI with --from/--to removes a wire and decreases count by 1."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch_before = Schematic.load(path)
        initial_count = len(list(sch_before.sexp.find_all("wire")))

        result = main([str(path), "--from", "100", "50", "--to", "150", "50"])
        assert result == 0

        sch_after = Schematic.load(path)
        assert len(list(sch_after.sexp.find_all("wire"))) == initial_count - 1

    def test_cli_round_trip_near(self, tmp_path):
        """CLI with --near removes the nearest wire."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        sch_before = Schematic.load(path)
        initial_count = len(list(sch_before.sexp.find_all("wire")))

        result = main([str(path), "--near", "149", "50"])
        assert result == 0

        sch_after = Schematic.load(path)
        assert len(list(sch_after.sexp.find_all("wire"))) == initial_count - 1

    def test_zero_length_wire_match(self, tmp_path):
        """--from X Y --to X Y can match and remove a zero-length wire."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, SCHEMATIC_WITH_ZERO_LENGTH_WIRE_REMOVE)
        sch_before = Schematic.load(path)
        initial_count = len(list(sch_before.sexp.find_all("wire")))

        result = main([str(path), "--from", "100", "50", "--to", "100", "50"])
        assert result == 0

        sch_after = Schematic.load(path)
        assert len(list(sch_after.sexp.find_all("wire"))) == initial_count - 1

    def test_mutual_exclusivity_error(self, tmp_path):
        """Passing both --from/--to and --near produces an error."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)

        result = main([
            str(path), "--from", "100", "50", "--to", "150", "50", "--near", "125", "50"
        ])
        assert result == 1

    def test_no_match_returns_error(self, tmp_path):
        """Query for non-existent coordinates returns exit code 1."""
        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)

        result = main([str(path), "--from", "999", "999", "--to", "888", "888"])
        assert result == 1

    def test_json_output(self, tmp_path, capsys):
        """JSON output mode produces valid JSON."""
        import json

        from kicad_tools.cli.sch_remove_wire import main

        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        result = main([
            str(path), "--from", "100", "50", "--to", "150", "50",
            "--format", "json"
        ])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["removed"] is True
        assert "wire" in data
        assert data["wire"]["start"] == [100.0, 50.0]
        assert data["wire"]["end"] == [150.0, 50.0]
