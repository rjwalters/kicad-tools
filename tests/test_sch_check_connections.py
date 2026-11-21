"""Tests for the sch connections command.

Covers wire-graph BFS connectivity, power symbol connections,
no-connect marker handling, and JSON/table output.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.sch_check_connections import (
    check_symbol_connections,
)
from kicad_tools.cli.sch_check_connections import (
    main as connections_main,
)
from kicad_tools.schema import Schematic

# ---------------------------------------------------------------------------
# Minimal schematic: resistor with both pins connected via wires + labels
# ---------------------------------------------------------------------------

CONNECTED_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "aaaa-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
  (wire (pts (xy 100 53.81) (xy 100 60))
    (stroke (width 0) (type default)) (uuid "w4"))
  (label "VIN" (at 100 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-vin"))
  (label "GND" (at 100 60 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-gnd"))
)
"""

# ---------------------------------------------------------------------------
# Schematic with power symbol (GND) connecting to a resistor pin
# ---------------------------------------------------------------------------

POWER_SYMBOL_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000002")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
    (symbol "power:GND"
      (power)
      (symbol "GND_0_1"
        (polyline
          (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54)
               (xy -1.27 -1.27) (xy 0 -1.27))
          (stroke (width 0) (type default))
          (fill (type none))
        )
      )
      (symbol "GND_1_1"
        (pin power_in line
          (at 0 0 270)
          (length 0)
          (name "GND")
          (number "1")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "aaaa-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
  (symbol
    (lib_id "power:GND")
    (at 100 60 0)
    (unit 1)
    (in_bom no)
    (on_board no)
    (dnp no)
    (uuid "gggg-gggg")
    (property "Reference" "#PWR01" (at 100 66.04 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "GND" (at 100 63.5 0)
      (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pg1"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
  (wire (pts (xy 100 53.81) (xy 100 60))
    (stroke (width 0) (type default)) (uuid "w2"))
  (label "VIN" (at 100 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-vin"))
)
"""

# ---------------------------------------------------------------------------
# Schematic with unconnected pin (no wire, no label, no power)
# ---------------------------------------------------------------------------

UNCONNECTED_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000003")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "aaaa-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
  (label "VIN" (at 100 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-vin"))
)
"""

# ---------------------------------------------------------------------------
# Schematic with no-connect marker on an unconnected pin
# ---------------------------------------------------------------------------

NO_CONNECT_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000004")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "aaaa-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
  (label "VIN" (at 100 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-vin"))
  (no_connect (at 100 53.81) (uuid "nc-1"))
)
"""


def _write_sch(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.kicad_sch"
    p.write_text(content)
    return p


def _load_from_string(text: str) -> Schematic:
    """Parse a schematic from a string."""
    from kicad_tools.sexp import parse_string

    sexp = parse_string(text)
    return Schematic(sexp)


# ===========================================================================
# Tests
# ===========================================================================


class TestWireConnected:
    """Pins connected via wire endpoints should report connected=True."""

    def test_both_pins_connected_via_wires_and_labels(self):
        sch = _load_from_string(CONNECTED_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        assert pin_map["1"].connected is True
        assert pin_map["2"].connected is True

    def test_connection_type_is_wire(self):
        sch = _load_from_string(CONNECTED_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        assert pin_map["1"].connection_type == "wire"
        assert pin_map["2"].connection_type == "wire"


class TestPowerSymbolConnection:
    """Pins connected via power symbols (GND, +3V3) should report connected=True."""

    def test_pin_connected_via_power_symbol(self):
        sch = _load_from_string(POWER_SYMBOL_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        # Pin 1 is connected via label "VIN"
        assert pin_map["1"].connected is True
        # Pin 2 is connected via power symbol GND
        assert pin_map["2"].connected is True

    def test_power_symbol_not_in_results(self):
        """Power symbols themselves should not appear as checked components."""
        sch = _load_from_string(POWER_SYMBOL_SCHEMATIC)
        results = check_symbol_connections(sch)

        refs = {r.reference for r in results}
        assert "#PWR01" not in refs


class TestUnconnectedPin:
    """Pins with no wire/label/power/no-connect should report connected=False."""

    def test_unconnected_pin_detected(self):
        sch = _load_from_string(UNCONNECTED_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        # Pin 1 is connected via wire + label
        assert pin_map["1"].connected is True
        # Pin 2 has no wire at all
        assert pin_map["2"].connected is False

    def test_unconnected_pin_has_empty_connection_type(self):
        sch = _load_from_string(UNCONNECTED_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        assert pin_map["2"].connection_type == ""


class TestNoConnectMarker:
    """Pins with no-connect markers should report connected=True with type 'no_connect'."""

    def test_no_connect_pin_shows_connected(self):
        sch = _load_from_string(NO_CONNECT_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        # Pin 2 has a no-connect marker at its position
        assert pin_map["2"].connected is True

    def test_no_connect_pin_has_correct_type(self):
        sch = _load_from_string(NO_CONNECT_SCHEMATIC)
        results = check_symbol_connections(sch)

        pin_map = {r.pin_number: r for r in results if r.reference == "R1"}
        assert pin_map["2"].connection_type == "no_connect"

    def test_no_connect_pin_excluded_from_unconnected_filter(self):
        """When --verbose is not set, no-connect pins should not appear."""
        sch = _load_from_string(NO_CONNECT_SCHEMATIC)
        results = check_symbol_connections(sch)

        # Filter like main() does when --verbose is not set
        unconnected = [r for r in results if not r.connected]
        pin_numbers = {r.pin_number for r in unconnected if r.reference == "R1"}
        assert "2" not in pin_numbers


class TestNoConnectParsing:
    """The Schematic.no_connects property should parse (no_connect ...) s-expressions."""

    def test_no_connects_parsed(self):
        sch = _load_from_string(NO_CONNECT_SCHEMATIC)
        assert len(sch.no_connects) == 1
        assert sch.no_connects[0].position == (100.0, 53.81)

    def test_schematic_without_no_connects(self):
        sch = _load_from_string(CONNECTED_SCHEMATIC)
        assert len(sch.no_connects) == 0


class TestJsonOutput:
    """JSON output should include connection_type field."""

    def test_json_includes_connection_type(self, tmp_path, capsys):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(NO_CONNECT_SCHEMATIC)

        connections_main([str(sch_file), "--format", "json", "--verbose"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Find pin 2 of R1 -- should have connection_type "no_connect"
        nc_pins = [p for p in data["pins"] if p["reference"] == "R1" and p["pin_number"] == "2"]
        assert len(nc_pins) == 1
        assert nc_pins[0]["connection_type"] == "no_connect"
        assert nc_pins[0]["connected"] is True

    def test_json_summary_counts(self, tmp_path, capsys):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(CONNECTED_SCHEMATIC)

        connections_main([str(sch_file), "--format", "json", "--verbose"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        summary = data["summary"]
        assert summary["total_pins"] == 2
        assert summary["connected"] == 2
        assert summary["unconnected"] == 0


class TestFilterPattern:
    """The --filter option should restrict results to matching references."""

    def test_filter_matches(self, tmp_path, capsys):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(CONNECTED_SCHEMATIC)

        connections_main([str(sch_file), "--format", "json", "--verbose", "--filter", "R*"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        refs = {p["reference"] for p in data["pins"]}
        assert refs == {"R1"}

    def test_filter_no_match(self, tmp_path, capsys):
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(CONNECTED_SCHEMATIC)

        connections_main([str(sch_file), "--format", "json", "--verbose", "--filter", "U*"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["summary"]["total_pins"] == 0


# ---------------------------------------------------------------------------
# Issue #9833: undotted T-connections and mid-wire attachments warn
# ---------------------------------------------------------------------------

# R1 pin 1 wires down to (100, 40), the interior of the bus wire
# (80, 40)-(140, 40): an undotted T that kicad-cli leaves open.
T_CONNECTION_NO_DOT = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000011")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "tconn-r1")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "1k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1")) (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 80 40) (xy 140 40))
    (stroke (width 0) (type default)) (uuid "w-bus"))
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w-r1"))
%(EXTRA)s
)
"""

_JUNCTION_AT_T = '  (junction (at 100 40) (diameter 0) (color 0 0 0 0) (uuid "j-t"))\n'
_LABEL_AT_T = '  (label "BUS" (at 100 40 0)\n    (effects (font (size 1.27 1.27)) (justify left bottom))\n    (uuid "lbl-bus"))\n'

# A pin lying on a wire's interior with no junction: R1 pin 1 at
# (100, 46.19) on the long wire (100, 30)-(100, 60).
PIN_MID_WIRE_NO_DOT = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000012")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "pmw2-r1")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "1k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1")) (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 100 30) (xy 100 60))
    (stroke (width 0) (type default)) (uuid "w1"))
%(EXTRA)s
)
"""

_JUNCTION_AT_PIN = '  (junction (at 100 46.19) (diameter 0) (color 0 0 0 0) (uuid "j-pin"))\n'

# A power:GND symbol on the bus interior at (120, 40).
POWER_MID_WIRE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000013")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "pmw3-r1")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "1k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1")) (pin "2" (uuid "p2"))
  )
  (symbol
    (lib_id "power:GND") (at 120 40 0) (unit 1)
    (in_bom no) (on_board no) (dnp no) (uuid "pmw3-gnd")
    (property "Reference" "#PWR01" (at 120 46.04 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "GND" (at 120 43.5 0)
      (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pg1"))
  )
  (wire (pts (xy 80 40) (xy 140 40))
    (stroke (width 0) (type default)) (uuid "w-bus"))
%(EXTRA)s
)
"""


class TestMidWireAttachmentWarnings:
    """Issue #9833: undotted Ts and mid-wire pins/power symbols warn."""

    def test_undotted_t_warns(self):
        from kicad_tools.cli.sch_check_connections import check_mid_wire_attachments

        sch = _load_from_string(T_CONNECTION_NO_DOT % {"EXTRA": ""})
        warnings = check_mid_wire_attachments(sch)
        kinds = {w.kind for w in warnings}
        assert "undotted_t" in kinds
        t_warn = next(w for w in warnings if w.kind == "undotted_t")
        assert t_warn.position == (100.0, 40.0)

    def test_dotted_t_does_not_warn(self):
        from kicad_tools.cli.sch_check_connections import check_mid_wire_attachments

        sch = _load_from_string(T_CONNECTION_NO_DOT % {"EXTRA": _JUNCTION_AT_T})
        warnings = check_mid_wire_attachments(sch)
        assert all(w.kind != "undotted_t" for w in warnings)

    def test_label_at_t_does_not_warn(self):
        from kicad_tools.cli.sch_check_connections import check_mid_wire_attachments

        sch = _load_from_string(T_CONNECTION_NO_DOT % {"EXTRA": _LABEL_AT_T})
        warnings = check_mid_wire_attachments(sch)
        assert all(w.kind != "undotted_t" for w in warnings)

    def test_pin_mid_wire_warns_and_pin_is_unconnected(self):
        from kicad_tools.cli.sch_check_connections import check_mid_wire_attachments

        sch = _load_from_string(PIN_MID_WIRE_NO_DOT % {"EXTRA": ""})

        warnings = check_mid_wire_attachments(sch)
        kinds = {w.kind for w in warnings}
        assert "pin_mid_wire" in kinds

        # The pin itself is reported unconnected: kicad-cli leaves it open
        results = check_symbol_connections(sch)
        pin1 = next(r for r in results if r.reference == "R1" and r.pin_number == "1")
        assert pin1.connected is False

    def test_pin_mid_wire_with_junction_does_not_warn(self):
        from kicad_tools.cli.sch_check_connections import check_mid_wire_attachments

        sch = _load_from_string(PIN_MID_WIRE_NO_DOT % {"EXTRA": _JUNCTION_AT_PIN})
        warnings = check_mid_wire_attachments(sch)
        # Pin 1's junction anchors it; pin 2 (also mid-wire, no dot) still warns
        assert all(w.position != (100.0, 46.2) for w in warnings)
        assert any(w.position == (100.0, 53.8) and w.kind == "pin_mid_wire" for w in warnings)

    def test_power_mid_wire_warns(self):
        from kicad_tools.cli.sch_check_connections import check_mid_wire_attachments

        sch = _load_from_string(POWER_MID_WIRE % {"EXTRA": ""})
        warnings = check_mid_wire_attachments(sch)
        kinds = {w.kind for w in warnings}
        assert "power_mid_wire" in kinds

    def test_json_output_includes_warnings(self, tmp_path, capsys):
        sch_file = tmp_path / "tconn.kicad_sch"
        sch_file.write_text(T_CONNECTION_NO_DOT % {"EXTRA": ""})

        connections_main([str(sch_file), "--format", "json", "--verbose"])
        data = json.loads(capsys.readouterr().out)

        assert "warnings" in data
        kinds = {w["kind"] for w in data["warnings"]}
        assert "undotted_t" in kinds
        warn = next(w for w in data["warnings"] if w["kind"] == "undotted_t")
        assert warn["position"] == [100.0, 40.0]

    def test_table_output_includes_warnings(self, tmp_path, capsys):
        sch_file = tmp_path / "tconn.kicad_sch"
        sch_file.write_text(T_CONNECTION_NO_DOT % {"EXTRA": ""})

        connections_main([str(sch_file), "--verbose"])
        out = capsys.readouterr().out

        assert "Warnings:" in out
        assert "undotted_t" in out
        assert "no junction dot" in out

    def test_table_all_connected_still_shows_warnings(self, tmp_path, capsys):
        """Warnings print even when the default (non-verbose) filter is on."""
        sch_file = tmp_path / "tconn.kicad_sch"
        sch_file.write_text(T_CONNECTION_NO_DOT % {"EXTRA": ""})

        connections_main([str(sch_file)])
        out = capsys.readouterr().out

        assert "Warnings:" in out
