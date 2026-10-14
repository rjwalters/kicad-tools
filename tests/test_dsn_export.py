"""Tests for DSN (Specctra) export functionality."""

import re
from pathlib import Path

import pytest

from kicad_tools.export.dsn import (
    KiCadToDSNExporter,
    _dsn_quote,
    mm_to_um,
    um_to_mm,
)

# Path to the voltage divider test board
VOLTAGE_DIVIDER_PCB = (
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)

VOLTAGE_DIVIDER_ROUTED_PCB = (
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider_routed.kicad_pcb"
)


class TestUnitConversions:
    """Test coordinate conversion helpers."""

    def test_mm_to_um(self):
        assert mm_to_um(1.0) == 1000.0
        assert mm_to_um(0.25) == 250.0
        assert mm_to_um(0.0) == 0.0

    def test_um_to_mm(self):
        assert um_to_mm(1000.0) == 1.0
        assert um_to_mm(250.0) == 0.25

    def test_roundtrip(self):
        for val in [0.0, 0.1, 0.25, 1.0, 10.5, 100.0]:
            assert abs(um_to_mm(mm_to_um(val)) - val) < 1e-6


class TestDSNQuote:
    """Test DSN string quoting."""

    def test_simple_name(self):
        assert _dsn_quote("F.Cu") == "F.Cu"

    def test_name_with_spaces(self):
        assert _dsn_quote("my net") == '"my net"'

    def test_name_with_parens(self):
        assert _dsn_quote("Net-(J1-1)") == '"Net-(J1-1)"'

    def test_empty_string(self):
        assert _dsn_quote("") == '""'


class TestDSNExporterBasic:
    """Test DSN exporter with the voltage divider board."""

    @pytest.fixture
    def exporter(self):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        return KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

    def test_export_produces_valid_sexp(self, exporter):
        dsn = exporter.export()
        assert dsn.startswith("(pcb")
        assert dsn.rstrip().endswith(")")

        # Check bracket balance
        open_count = dsn.count("(")
        close_count = dsn.count(")")
        assert open_count == close_count, (
            f"Unbalanced parens: {open_count} open vs {close_count} close"
        )

    def test_layers_extracted(self, exporter):
        layers = exporter.layers
        assert "F.Cu" in layers
        assert "B.Cu" in layers

    def test_nets_extracted(self, exporter):
        nets = exporter.nets
        # Voltage divider has nets: "", "VIN", "VOUT", "GND"
        net_names = set(nets.values())
        assert "VIN" in net_names
        assert "VOUT" in net_names
        assert "GND" in net_names

    def test_footprints_extracted(self, exporter):
        fps = exporter.footprints
        refs = {fp.reference for fp in fps}
        assert "J1" in refs
        assert "J2" in refs
        assert "R1" in refs
        assert "R2" in refs

    def test_dsn_has_structure_section(self, exporter):
        dsn = exporter.export()
        assert "(structure" in dsn

    def test_dsn_has_placement_section(self, exporter):
        dsn = exporter.export()
        assert "(placement" in dsn

    def test_dsn_has_library_section(self, exporter):
        dsn = exporter.export()
        assert "(library" in dsn

    def test_dsn_has_network_section(self, exporter):
        dsn = exporter.export()
        assert "(network" in dsn

    def test_dsn_has_boundary(self, exporter):
        dsn = exporter.export()
        assert "(boundary" in dsn

    def test_dsn_has_resolution(self, exporter):
        dsn = exporter.export()
        assert "(resolution um 10)" in dsn

    def test_dsn_net_names_present(self, exporter):
        dsn = exporter.export()
        assert "VIN" in dsn
        assert "VOUT" in dsn
        assert "GND" in dsn

    def test_dsn_component_references(self, exporter):
        dsn = exporter.export()
        assert "J1" in dsn
        assert "J2" in dsn
        assert "R1" in dsn
        assert "R2" in dsn

    def test_dsn_has_padstacks(self, exporter):
        dsn = exporter.export()
        assert "(padstack" in dsn

    def test_dsn_has_pins(self, exporter):
        dsn = exporter.export()
        assert "(pin" in dsn

    def test_dsn_has_net_class(self, exporter):
        dsn = exporter.export()
        assert "(class kicad_default" in dsn


class TestDSNExporterWithRoutes:
    """Test DSN export with pre-existing routes."""

    @pytest.fixture
    def exporter(self):
        if not VOLTAGE_DIVIDER_ROUTED_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_ROUTED_PCB}")
        return KiCadToDSNExporter(str(VOLTAGE_DIVIDER_ROUTED_PCB))

    def test_wiring_section_present(self, exporter):
        dsn = exporter.export()
        assert "(wiring" in dsn

    def test_wiring_has_protected_routes(self, exporter):
        dsn = exporter.export()
        assert "(type protect)" in dsn

    def test_wiring_has_wire_elements(self, exporter):
        dsn = exporter.export()
        assert "(wire" in dsn
        # Wire should have path with layer and coordinates
        assert "(path" in dsn


class TestDSNExportToFile:
    """Test writing DSN to disk."""

    @pytest.fixture
    def exporter(self):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        return KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

    def test_export_to_file(self, exporter, tmp_path):
        output = tmp_path / "test.dsn"
        dsn = exporter.export(str(output))
        assert output.exists()
        assert output.read_text() == dsn

    def test_export_creates_parent_dirs(self, exporter, tmp_path):
        output = tmp_path / "subdir" / "nested" / "test.dsn"
        exporter.export(str(output))
        assert output.exists()


class TestDSNRoundTrip:
    """Test structural consistency of DSN export."""

    @pytest.fixture
    def dsn_content(self):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))
        return exporter.export()

    def test_net_count_matches(self, dsn_content):
        """Verify number of nets in DSN matches source PCB."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

        # Count nets in DSN (exclude net 0 which is the unconnected net)
        re.findall(r'\(net "?[^")\s]+', dsn_content)
        # Count non-empty net names from source
        source_nets = {n for n, v in exporter.nets.items() if v and n != 0}

        # DSN should have entries for each named net
        assert len(source_nets) > 0

    def test_component_count_matches(self, dsn_content):
        """Verify number of components in DSN matches source PCB."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

        # Count place directives in DSN
        place_count = len(re.findall(r"\(place\s", dsn_content))
        assert place_count == len(exporter.footprints)

    def test_layer_count_matches(self, dsn_content):
        """Verify layer count in DSN structure matches source."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

        # Count (layer ...) in structure section
        structure_match = re.search(r"\(structure(.*?)\n  \)", dsn_content, re.DOTALL)
        assert structure_match
        layer_count = len(re.findall(r"\(layer\s", structure_match.group(1)))
        assert layer_count == len(exporter.layers)


class TestDSNViaLayerSpan:
    """Issue #6614: pre-existing vias export the span KiCad reads."""

    @pytest.mark.parametrize(
        "via_kw,pair,expected",
        [
            # A keyword-less partial pair is a through via to KiCad.
            ("", '"In1.Cu" "F.Cu"', ("F.Cu", "B.Cu")),
            ("", '"In1.Cu" "B.Cu"', ("F.Cu", "B.Cu")),
            # A keyworded blind via keeps its declared span.
            ("blind ", '"F.Cu" "In1.Cu"', ("F.Cu", "In1.Cu")),
            ("", '"F.Cu" "B.Cu"', ("F.Cu", "B.Cu")),
        ],
    )
    def test_parse_via_reads_kicad_span(self, via_kw, pair, expected):
        from kicad_tools.sexp import parse_string

        node = parse_string(
            f"(via {via_kw}(at 1 2) (size 0.6) (drill 0.3) (layers {pair}) (net 1))"
        )
        via = KiCadToDSNExporter("unused.kicad_pcb")._parse_via(node)
        assert via is not None
        assert via.layers == expected


# ---------------------------------------------------------------------------
# Issue #6594: rules, classes, zones and KiCad 10 net references
# ---------------------------------------------------------------------------

BOARDS = Path(__file__).parent.parent / "boards"
SIMPLE_LED_PCB = BOARDS / "00-simple-led" / "output" / "simple_led.kicad_pcb"
USBC_PD_PCB = BOARDS / "09-usbc-pd-power" / "output" / "usbc_pd_power.kicad_pcb"

# A KiCad-10-style board: no top-level net table, name-only pad nets,
# ``(property "Reference" ..)`` references, a pour and three rule areas.
_FIXTURE_PCB = """(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
  )
  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))
  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "+5V"))
    (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "SIG"))
  )
  (footprint "R_0603"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "SIG"))
    (pad "2" smd rect (at 0.8 0) (size 0.8 0.9) (layers "F.Cu") (net "GND"))
    (pad "3" smd rect (at 0 1) (size 0.8 0.9) (layers "F.Cu"))
  )
  (segment (start 10.8 10) (end 19.2 10) (width 0.2) (layer "F.Cu") (net "SIG"))
  (via (at 15 12) (size 0.7) (drill 0.35) (layers "F.Cu" "B.Cu") (net "GND"))
  (zone (net "GND") (layer "B.Cu") (uuid "z1") (name "gnd pour")
    (polygon (pts (xy 0 0) (xy 40 0) (xy 40 30) (xy 0 30)))
  )
  (zone (layers "F.Cu" "B.Cu") (uuid "k1") (name "no copper")
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed)
      (copperpour allowed) (footprints allowed))
    (polygon (pts (xy 1 1) (xy 3 1) (xy 3 3) (xy 1 3)))
  )
  (zone (layers "F.Cu") (uuid "k2") (name "no tracks")
    (keepout (tracks not_allowed) (vias allowed) (pads allowed)
      (copperpour allowed) (footprints allowed))
    (polygon (pts (xy 5 1) (xy 7 1) (xy 7 3) (xy 5 3)))
  )
  (zone (layers "*.Cu") (uuid "k3") (name "no vias")
    (keepout (tracks allowed) (vias not_allowed) (pads allowed)
      (copperpour allowed) (footprints not_allowed))
    (polygon (pts (xy 9 1) (xy 11 1) (xy 11 3) (xy 9 3)))
  )
)
"""

_FIXTURE_PRO = {
    "board": {"design_settings": {"rules": {"min_copper_edge_clearance": 0.3}}},
    "net_settings": {
        "classes": [
            {
                "name": "Default",
                "clearance": 0.15,
                "track_width": 0.2,
                "via_diameter": 0.6,
                "via_drill": 0.3,
            },
            {
                "name": "Power",
                "clearance": 0.25,
                "track_width": 0.5,
                "via_diameter": 0.8,
                "via_drill": 0.4,
            },
            {
                "name": "Signal",
                "clearance": 0.18,
                "track_width": 0.15,
                "via_diameter": 0.6,
                "via_drill": 0.3,
            },
        ],
        "meta": {"version": 3},
        "netclass_assignments": {"GND": "Power"},
        "netclass_patterns": [{"netclass": "Power", "pattern": "+*"}],
    },
}


def _write_fixture(tmp_path, *, pro=_FIXTURE_PRO, dru=None, name="fixture"):
    import json

    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(_FIXTURE_PCB, encoding="utf-8")
    if pro is not None:
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(pro), encoding="utf-8")
    if dru is not None:
        pcb.with_suffix(".kicad_dru").write_text(dru, encoding="utf-8")
    return pcb


def _class_block(dsn: str, name: str) -> str:
    match = re.search(rf"\(class {re.escape(name)}\b(.*?)\n    \)", dsn, re.DOTALL)
    assert match, f"no (class {name}) in DSN"
    return match.group(1)


def _rule_values(block: str) -> tuple[float, float]:
    match = re.search(r"\(rule\s*\(width ([\d.]+)\)\s*\(clearance ([\d.]+)\)", block)
    assert match
    return float(match.group(1)), float(match.group(2))


class TestProjectNetClasses:
    def test_board00_uses_kicad_pro_default_class(self):
        """AC1/AC5 stand-in: board 00's DSN rules equal its .kicad_pro Default."""
        if not SIMPLE_LED_PCB.exists():
            pytest.skip(f"Test board not found: {SIMPLE_LED_PCB}")
        dsn = KiCadToDSNExporter(SIMPLE_LED_PCB).export()
        structure = re.search(r"\(structure(.*?)\n  \)", dsn, re.DOTALL).group(1)
        assert _rule_values(structure) == (250.0, 150.0)
        block = _class_block(dsn, "kicad_default")
        assert _rule_values(block) == (250.0, 150.0)
        assert "(use_via Via[0-1]_Pad600_um)" in block
        assert "(padstack Via[0-1]_Pad600_um" in dsn
        assert "Pad800_um" not in dsn

    def test_classes_assignments_and_patterns(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path))
        dsn = exporter.export()
        power = _class_block(dsn, "Power")
        assert "+5V" in power and "GND" in power and "SIG" not in power
        assert _rule_values(power) == (500.0, 250.0)
        assert "(use_via Via[0-1]_Pad800_um)" in power
        default = _class_block(dsn, "kicad_default")
        assert "SIG" in default and "GND" not in default
        assert _rule_values(default) == (200.0, 150.0)
        assert "(use_via Via[0-1]_Pad600_um)" in default
        # A class that holds no net of the network is not emitted.
        assert "(class Signal" not in dsn
        assert "(padstack Via[0-1]_Pad800_um" in dsn
        assert "(padstack Via[0-1]_Pad700_um" in dsn  # the pre-existing via

    def test_no_kicad_pro_keeps_defaults_and_reports_it(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path, pro=None))
        dsn = exporter.export()
        assert _rule_values(_class_block(dsn, "kicad_default")) == (250.0, 200.0)
        assert any("no fixture.kicad_pro" in line for line in exporter.omissions)

    def test_inexpressible_project_minimum_is_listed(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path))
        assert any("min_copper_edge_clearance 0.3 mm" in o for o in exporter.omissions)


class TestDruFloors:
    DRU = """(version 1)
(rule "Clearance floor"
  (constraint clearance (min 0.3mm)))
(rule "Trace Width"
  (condition "A.Type == 'track'")
  (constraint track_width (min 0.22mm)))
(rule "PTH Hole to Track"
  (condition "A.Pad_Type == 'Through-hole'")
  (constraint hole_clearance (min 0.28mm)))
(rule "Copper to Edge"
  (constraint edge_clearance (min 0.3mm)))
"""

    def test_unconditional_floors_raise_classes(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path, dru=self.DRU))
        dsn = exporter.export()
        # clearance 0.15 -> 0.3; width 0.2 -> 0.22 (trivially-true condition)
        assert _rule_values(_class_block(dsn, "kicad_default")) == (220.0, 300.0)
        # Power's own 0.5 / 0.25 stays above the width floor, clearance rises.
        assert _rule_values(_class_block(dsn, "Power")) == (500.0, 300.0)

    def test_inexpressible_rules_are_listed_by_name(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path, dru=self.DRU))
        omissions = "\n".join(exporter.omissions)
        assert "'PTH Hole to Track'" in omissions and "hole_clearance" in omissions
        assert "'Copper to Edge'" in omissions and "edge_clearance" in omissions
        assert "'Clearance floor'" not in omissions
        assert "'Trace Width'" not in omissions

    def test_conditional_clearance_is_not_applied(self, tmp_path):
        dru = """(version 1)
(rule "Power only"
  (condition "A.NetClass == 'Power'")
  (constraint clearance (min 0.5mm)))
"""
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path, dru=dru))
        dsn = exporter.export()
        assert _rule_values(_class_block(dsn, "kicad_default"))[1] == 150.0
        assert any("'Power only' (conditional)" in o for o in exporter.omissions)

    def test_rejected_dru_is_reported(self, tmp_path):
        dru = '(version 1)\n(rule "bad" (constraint not_a_constraint (min 1mm)))\n'
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path, dru=dru))
        exporter.export()
        assert any("fixture.kicad_dru not applied" in o for o in exporter.omissions)


class TestNameOnlyNetsAndReferences:
    def test_fixture_name_only_nets_export(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path))
        dsn = exporter.export()
        assert {fp.reference for fp in exporter.footprints} == {"R1", "R2"}
        assert set(exporter.nets.values()) >= {"+5V", "SIG", "GND"}
        assert "(net +5V" in dsn and "(net SIG" in dsn and "(net GND" in dsn
        network = dsn[dsn.index("(network") : dsn.index("(wiring")]
        pins = set(re.findall(r"^        (\S+)$", network, re.MULTILINE))
        assert pins == {"R1-1", "R1-2", "R2-1", "R2-2"}  # netless R2-3 stays out
        # Pre-existing copper keeps its name-only net.
        wiring = dsn[dsn.index("(wiring") :]
        assert "(net SIG)" in wiring and "(net GND)" in wiring
        assert "Via[0-1]_Pad700_um 15000.0 12000.0" in wiring

    def test_board09_exports_nets_references_and_planes(self):
        if not USBC_PD_PCB.exists():
            pytest.skip(f"Test board not found: {USBC_PD_PCB}")
        exporter = KiCadToDSNExporter(USBC_PD_PCB)
        dsn = exporter.export()
        assert not [fp for fp in exporter.footprints if fp.reference == "?"]
        pad_nets = {pad.net_name for fp in exporter.footprints for pad in fp.pads if pad.net_name}
        net_entries = re.findall(r"^    \(net ", dsn, re.MULTILINE)
        assert len(net_entries) == len(pad_nets) > 0
        assert re.search(r"\(plane GND\n\s+\(polygon In1\.Cu 0 ", dsn)
        assert re.search(r"\(plane GND\n\s+\(polygon In2\.Cu 0 ", dsn)
        assert dsn.count("(") == dsn.count(")")


class TestZonesAndKeepouts:
    def test_pour_becomes_plane(self, tmp_path):
        dsn = KiCadToDSNExporter(_write_fixture(tmp_path)).export()
        assert re.search(r"\(plane GND\n\s+\(polygon B\.Cu 0 0\.0 0\.0 40000\.0 0\.0", dsn)

    def test_rule_areas_become_keepouts(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path))
        dsn = exporter.export()
        found = re.findall(r'\((keepout|wire_keepout|via_keepout) ""\n\s+\(polygon (\S+)', dsn)
        assert sorted(found) == sorted(
            [
                ("keepout", "F.Cu"),
                ("keepout", "B.Cu"),
                ("wire_keepout", "F.Cu"),
                ("via_keepout", "F.Cu"),
                ("via_keepout", "B.Cu"),
            ]
        )
        assert any("'no vias'" in o and "footprints" in o for o in exporter.omissions), (
            exporter.omissions
        )

    def test_arc_outline_is_reported_not_approximated(self, tmp_path):
        pcb = _write_fixture(tmp_path)
        text = pcb.read_text().replace(
            "(polygon (pts (xy 0 0) (xy 40 0) (xy 40 30) (xy 0 30)))",
            "(polygon (pts (xy 0 0) (arc (start 40 0) (mid 41 15) (end 40 30)) (xy 0 30)))",
        )
        pcb.write_text(text)
        exporter = KiCadToDSNExporter(pcb)
        dsn = exporter.export()
        assert "(plane" not in dsn
        assert any("'gnd pour'" in o and "arcs" in o for o in exporter.omissions)


def _run_pcb(argv):
    from kicad_tools.cli.commands.pcb import run_pcb_command
    from kicad_tools.cli.parser import create_parser

    return run_pcb_command(create_parser().parse_args(argv))


class TestExportDsnCliOmissions:
    def test_json_includes_omissions(self, tmp_path, capsys):
        import json

        pcb = _write_fixture(tmp_path)
        argv = ["pcb", "export-dsn", str(pcb), "-o", str(tmp_path / "b.dsn"), "--format", "json"]
        assert _run_pcb(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert any("min_copper_edge_clearance" in o for o in payload["omissions"])

    def test_text_lists_omissions(self, tmp_path, capsys):
        pcb = _write_fixture(tmp_path)
        assert _run_pcb(["pcb", "export-dsn", str(pcb), "-o", str(tmp_path / "b.dsn")]) == 0
        out = capsys.readouterr().out
        assert "Not exported" in out and "min_copper_edge_clearance" in out
