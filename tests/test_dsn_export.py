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
    def exporter(self, tmp_path):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        # Copy: export() writes a revision sidecar beside the board (issue #6915).
        board = tmp_path / "board.kicad_pcb"
        board.write_bytes(VOLTAGE_DIVIDER_PCB.read_bytes())
        return KiCadToDSNExporter(str(board))

    def test_export_writes_revision_sidecar(self, exporter, tmp_path):
        import hashlib
        import json

        exporter.export(str(tmp_path / "out" / "test.dsn"))
        sidecar = json.loads((tmp_path / "board.dsn-revision.json").read_text())
        expected = hashlib.sha256(
            (tmp_path / "board.kicad_pcb").read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        assert sidecar == {"board_sha256": expected, "dsn": "test.dsn"}

    def test_export_string_writes_no_sidecar(self, exporter, tmp_path):
        exporter.export()
        assert not (tmp_path / "board.dsn-revision.json").exists()

    def test_revision_hash_is_crlf_insensitive(self, tmp_path):
        from kicad_tools.export.dsn import board_revision_hash

        lf = tmp_path / "lf.kicad_pcb"
        crlf = tmp_path / "crlf.kicad_pcb"
        lf.write_bytes(b"(kicad_pcb\n  (a)\n)\n")
        crlf.write_bytes(b"(kicad_pcb\r\n  (a)\r\n)\r\n")
        assert board_revision_hash(lf) == board_revision_hash(crlf)

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


class TestDSNLayerIndex:
    """Issue #7485: each structure layer carries its stack-order index."""

    def test_layer_index_follows_stack_order(self):
        exporter = KiCadToDSNExporter("unused.kicad_pcb")
        exporter._layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        structure = exporter._generate_structure()
        assert re.findall(r"\(index (\d+)\)", structure) == ["0", "1", "2", "3"]


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
    "board": {
        "design_settings": {"rules": {"min_copper_edge_clearance": 0.3, "min_hole_clearance": 0.25}}
    },
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
        assert any("min_hole_clearance 0.25 mm" in o for o in exporter.omissions)

    def test_edge_clearance_insets_boundary_and_is_not_omitted(self, tmp_path):
        exporter = KiCadToDSNExporter(_write_fixture(tmp_path))
        dsn = exporter.export()
        assert not any("min_copper_edge_clearance" in o for o in exporter.omissions)
        m = re.search(r"\(boundary\s*\(path pcb 0([^)]*)\)", dsn)
        pts = [float(v) for v in m.group(1).split()]
        xs, ys = pts[0::2], pts[1::2]
        # the 40 x 30 mm outline, inset 0.3 mm on every side (um)
        assert (min(xs), max(xs), min(ys), max(ys)) == (300.0, 39700.0, 300.0, 29700.0)

    def test_zero_edge_clearance_keeps_outline(self, tmp_path):
        pro = {**_FIXTURE_PRO, "board": {"design_settings": {"rules": {}}}}
        dsn = KiCadToDSNExporter(_write_fixture(tmp_path, pro=pro)).export()
        m = re.search(r"\(boundary\s*\(path pcb 0([^)]*)\)", dsn)
        pts = [float(v) for v in m.group(1).split()]
        assert (min(pts[0::2]), max(pts[0::2])) == (0.0, 40000.0)

    def test_pad_inside_edge_band_is_listed_not_an_error(self, tmp_path):
        import json

        pcb = _write_fixture(tmp_path)
        text = pcb.read_text(encoding="utf-8")
        # move the R_0603 footprint so its pads straddle the left edge band
        pcb.write_text(text.replace("(at 10 10)", "(at 0.1 10)", 1), encoding="utf-8")
        assert json.loads(pcb.with_suffix(".kicad_pro").read_text())
        exporter = KiCadToDSNExporter(pcb)
        exporter.export()
        assert any("copper-to-edge band" in o for o in exporter.omissions)


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
        assert any("min_hole_clearance" in o for o in payload["omissions"])

    def test_text_lists_omissions(self, tmp_path, capsys):
        pcb = _write_fixture(tmp_path)
        assert _run_pcb(["pcb", "export-dsn", str(pcb), "-o", str(tmp_path / "b.dsn")]) == 0
        out = capsys.readouterr().out
        assert "Not exported" in out and "min_hole_clearance" in out


class TestBoardOutline:
    """Edge.Cuts shapes become one valid boundary; cutouts become keepouts (#7482)."""

    @staticmethod
    def _export(tmp_path, edge_cuts, extra="", edge_clearance=None):
        import json

        pcb = tmp_path / "outline.kicad_pcb"
        pcb.write_text(
            '(kicad_pcb (version 20240108) (generator "t")\n'
            '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
            f"{edge_cuts}\n{extra}\n)\n",
            encoding="utf-8",
        )
        if edge_clearance is not None:
            rules = {"min_copper_edge_clearance": edge_clearance}
            pro = {"board": {"design_settings": {"rules": rules}}}
            pcb.with_suffix(".kicad_pro").write_text(json.dumps(pro), encoding="utf-8")
        exporter = KiCadToDSNExporter(pcb)
        return exporter, exporter.export()

    @staticmethod
    def _keepouts(dsn):
        """``(layer, Polygon)`` for every ``(keepout "" (polygon ...))``, in mm."""
        from shapely.geometry import Polygon

        out = []
        for layer, body in re.findall(r'\(keepout ""\s*\(polygon (\S+) 0 ([^)]*)\)', dsn):
            v = [float(x) / 1000.0 for x in body.split()]
            out.append((layer, Polygon(list(zip(v[0::2], v[1::2], strict=True)))))
        return out

    @staticmethod
    def _boundary(dsn):
        from shapely.geometry import Polygon

        m = re.search(r"\(boundary\s*\(path pcb 0([^)]*)\)", dsn)
        assert m, "no boundary exported"
        v = [float(x) / 1000.0 for x in m.group(1).split()]
        return Polygon(list(zip(v[0::2], v[1::2], strict=True)))

    @staticmethod
    def _line(a, b):
        return f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))'

    def test_out_of_order_lines(self, tmp_path):
        p = [(0, 0), (100, 0), (100, 60), (0, 60)]
        edges = "\n".join(
            [
                self._line(p[2], p[3]),
                self._line(p[1], p[0]),  # reversed
                self._line(p[0], p[3]),
                self._line(p[1], p[2]),
                self._line(p[0], p[1]),  # duplicate edge
            ]
        )
        _, dsn = self._export(tmp_path, edges)
        poly = self._boundary(dsn)
        assert poly.is_valid
        assert poly.area == pytest.approx(6000)

    def test_in_order_lines_have_no_duplicate_vertices(self, tmp_path):
        p = [(0, 0), (100, 0), (100, 60), (0, 60)]
        edges = "\n".join(self._line(p[i], p[(i + 1) % 4]) for i in range(4))
        _, dsn = self._export(tmp_path, edges)
        poly = self._boundary(dsn)
        assert poly.area == pytest.approx(6000)
        assert len(poly.exterior.coords) == 5  # 4 corners + closure

    def test_gr_poly(self, tmp_path):
        edges = '(gr_poly (pts (xy 0 0) (xy 100 0) (xy 100 60) (xy 0 60)) (layer "Edge.Cuts"))'
        _, dsn = self._export(tmp_path, edges)
        assert self._boundary(dsn).area == pytest.approx(6000)

    def test_gr_circle(self, tmp_path):
        edges = '(gr_circle (center 50 50) (end 80 50) (layer "Edge.Cuts"))'
        _, dsn = self._export(tmp_path, edges)
        poly = self._boundary(dsn)
        assert poly.is_valid
        assert poly.area == pytest.approx(3.14159265 * 900, rel=0.01)

    def test_arc_corner(self, tmp_path):
        import math

        s = math.sqrt(0.5)
        edges = "\n".join(
            [
                self._line((0, 0), (100, 0)),
                self._line((100, 0), (100, 50)),
                f"(gr_arc (start 100 50) (mid {90 + 10 * s} {50 + 10 * s}) (end 90 60) "
                '(layer "Edge.Cuts"))',
                self._line((90, 60), (0, 60)),
                self._line((0, 60), (0, 0)),
            ]
        )
        _, dsn = self._export(tmp_path, edges)
        assert self._boundary(dsn).area == pytest.approx(6000 - 100 + 25 * math.pi, abs=0.1)

    def test_rect_cutout_becomes_keepout(self, tmp_path):
        edges = (
            '(gr_rect (start 0 0) (end 100 60) (layer "Edge.Cuts"))\n'
            '(gr_rect (start 40 20) (end 60 40) (layer "Edge.Cuts"))'
        )
        exporter, dsn = self._export(tmp_path, edges)
        assert self._boundary(dsn).area == pytest.approx(6000)
        assert len(re.findall(r'\(keepout ""', dsn)) == 2  # F.Cu and B.Cu
        assert not any("Edge.Cuts" in o for o in exporter.omissions)

    def test_annular_board_hole_larger_than_copper(self, tmp_path):
        """An LED ring: the r=25 hole encloses more area than the copper annulus."""
        import math

        edges = (
            '(gr_circle (center 50 50) (end 80 50) (layer "Edge.Cuts"))\n'
            '(gr_circle (center 50 50) (end 75 50) (layer "Edge.Cuts"))'
        )
        exporter, dsn = self._export(tmp_path, edges)
        assert self._boundary(dsn).area == pytest.approx(math.pi * 900, rel=0.01)
        keepouts = self._keepouts(dsn)
        assert sorted(layer for layer, _ in keepouts) == ["B.Cu", "F.Cu"]
        for _, hole in keepouts:
            assert hole.area == pytest.approx(math.pi * 625, rel=0.01)
        assert not any("Edge.Cuts" in o for o in exporter.omissions)

    def test_frame_board_window_larger_than_copper(self, tmp_path):
        edges = (
            '(gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts"))\n'
            '(gr_rect (start 5 5) (end 95 95) (layer "Edge.Cuts"))'
        )
        exporter, dsn = self._export(tmp_path, edges)
        assert self._boundary(dsn).area == pytest.approx(10000)
        keepouts = self._keepouts(dsn)
        assert len(keepouts) == 2
        assert all(hole.area == pytest.approx(8100) for _, hole in keepouts)
        assert not any("Edge.Cuts" in o for o in exporter.omissions)

    def test_edge_clearance_grows_cutout_keepouts(self, tmp_path):
        """min_copper_edge_clearance applies at a cutout edge, not only the outer one."""
        from shapely.geometry import box

        edges = (
            '(gr_rect (start 0 0) (end 100 60) (layer "Edge.Cuts"))\n'
            '(gr_rect (start 40 20) (end 60 40) (layer "Edge.Cuts"))'
        )
        fp = (
            '(footprint "TP" (layer "F.Cu") (at 50 19.8)\n'
            '  (property "Reference" "TP1" (at 0 0) (layer "F.SilkS"))\n'
            '  (pad "1" smd rect (at 0 0) (size 0.2 0.2) (layers "F.Cu")))'
        )
        exporter, dsn = self._export(tmp_path, edges, fp, edge_clearance=0.5)
        assert self._boundary(dsn).equals(box(0.5, 0.5, 99.5, 59.5))
        keepouts = self._keepouts(dsn)
        assert len(keepouts) == 2
        for _, hole in keepouts:
            assert hole.symmetric_difference(box(39.5, 19.5, 60.5, 40.5)).area < 1e-6
        assert any("copper-to-edge band" in o and "TP1.1" in o for o in exporter.omissions)
        assert not any("cannot be inset" in o for o in exporter.omissions)

    def test_open_outline_is_omitted(self, tmp_path):
        edges = "\n".join([self._line((0, 0), (100, 0)), self._line((100, 0), (100, 60))])
        exporter, dsn = self._export(tmp_path, edges)
        assert "(boundary" not in dsn
        assert any("closed outline" in o for o in exporter.omissions)

    def test_footprint_edge_cuts_listed(self, tmp_path):
        fp = (
            '(footprint "X" (layer "F.Cu") (at 5 5)\n'
            '  (fp_line (start 0 0) (end 1 0) (layer "Edge.Cuts")))'
        )
        edges = '(gr_rect (start 0 0) (end 100 60) (layer "Edge.Cuts"))'
        exporter, _ = self._export(tmp_path, edges, fp)
        assert any("footprint-embedded Edge.Cuts" in o for o in exporter.omissions)

    def test_micron_gap_is_snapped(self, tmp_path):
        edges = "\n".join(
            [
                self._line((0, 0), (100, 0)),
                self._line((100.0004, 0), (100, 60)),
                self._line((100, 60.0004), (0, 60)),
                self._line((0, 60), (0, 0)),
            ]
        )
        _, dsn = self._export(tmp_path, edges)
        poly = self._boundary(dsn)
        assert poly.area == pytest.approx(6000, abs=0.1)
        assert len(poly.exterior.coords) == 5  # 4 corners + closure, gaps welded

    def test_charlieplex_routed_board_boundary_is_valid(self, tmp_path):
        import shutil

        src = (
            Path(__file__).parent.parent
            / "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb"
        )
        dst = tmp_path / src.name
        shutil.copy(src, dst)
        shutil.copy(src.with_suffix(".kicad_pro"), dst.with_suffix(".kicad_pro"))
        from shapely.geometry import box

        boundary = self._boundary(KiCadToDSNExporter(dst).export())
        assert boundary.is_valid
        # Edge.Cuts is the 50 x 55 mm rectangle (123.5, 77.5)-(173.5, 132.5); the
        # board's .kicad_pro min_copper_edge_clearance of 0.3 mm insets it.
        assert len(boundary.exterior.coords) == 5
        assert boundary.symmetric_difference(box(123.8, 77.8, 173.2, 132.2)).area < 1e-6


# ---------------------------------------------------------------------------
# Issue #7481: pin placement under the Specctra rule
# ---------------------------------------------------------------------------


def _specctra_pins(dsn: str) -> dict[tuple[str, str], tuple[float, float]]:
    """Place every pin with ``place + R_ccw(theta) . (mirror-x if back) . offset``."""
    import math

    images: dict[str, dict[str, tuple[float, float]]] = {}
    cur = None
    for line in dsn.splitlines():
        m = re.match(r'\s*\(image "?([^"\s]+)"?\s*$', line)
        if m:
            cur = m[1]
            images[cur] = {}
            continue
        m = re.match(r'\s*\(pin "?[^"\s]+"? "?([^"\s]+)"? (\S+) (\S+)\)', line)
        if m and cur:
            images[cur][m[1]] = (float(m[2]), float(m[3]))
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for img, ref, x, y, side, rot in re.findall(
        r'\(component "?([^"\s]+)"?\s*\n\s*\(place "?([^"\s]+)"? (\S+) (\S+) (front|back) (\S+)\)',
        dsn,
    ):
        a = math.radians(float(rot))
        for num, (ox, oy) in images[img].items():
            if side == "back":
                ox = -ox
            out[(ref, num)] = (
                float(x) + ox * math.cos(a) - oy * math.sin(a),
                float(y) + ox * math.sin(a) + oy * math.cos(a),
            )
    return out


def _assert_pins_on_pads(exporter: KiCadToDSNExporter) -> int:
    from kicad_tools.core.geometry import rotate_pad_offset

    placed = _specctra_pins(exporter.export())
    checked = 0
    for fp in exporter._footprints:
        counts: dict[str, int] = {}
        for pad in fp.pads:
            counts[pad.number] = counts.get(pad.number, 0) + 1
        for pad in fp.pads:
            if not pad.number or counts[pad.number] > 1:
                continue  # unnumbered / repeated pad numbers share one DSN pin key
            dx, dy = rotate_pad_offset(pad.x, pad.y, fp.rotation)
            ex, ey = mm_to_um(fp.x + dx), mm_to_um(fp.y + dy)
            got = placed[(fp.reference, pad.number)]
            assert got == pytest.approx((ex, ey), abs=1.0), (
                fp.reference,
                pad.number,
                fp.layer,
                fp.rotation,
            )
            checked += 1
    return checked


def _fp_block(ref, lib, x, y, rot, layer):
    pad_layer = layer
    return f"""  (footprint "{lib}"
    (layer "{layer}")
    (at {x} {y} {rot})
    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -2 0.5) (size 0.8 0.9) (layers "{pad_layer}") (net "A"))
    (pad "2" smd rect (at 2 0.5) (size 0.8 0.9) (layers "{pad_layer}") (net "B"))
    (pad "3" smd rect (at 1 -1.5) (size 0.8 0.9) (layers "{pad_layer}"))
  )
"""


def _board_with(tmp_path, blocks):
    text = (
        '(kicad_pcb\n  (version 20260206)\n  (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
        '  (gr_rect (start 0 0) (end 80 60) (layer "Edge.Cuts"))\n' + "".join(blocks) + ")\n"
    )
    path = tmp_path / "rot.kicad_pcb"
    path.write_text(text)
    return KiCadToDSNExporter(str(path))


class TestDSNPinPlacement:
    def test_all_sides_and_rotations_land_on_kicad_pads(self, tmp_path):
        blocks = [
            _fp_block("F0", "Lib_A", 10, 10, 0, "F.Cu"),
            _fp_block("F90", "Lib_A", 20, 10, 90, "F.Cu"),
            _fp_block("FM90", "Lib_A", 30, 10, -90, "F.Cu"),
            _fp_block("F180", "Lib_A", 40, 10, 180, "F.Cu"),
            _fp_block("F45", "Lib_A", 50, 10, 45.5, "F.Cu"),
            _fp_block("B0", "Lib_A", 10, 30, 0, "B.Cu"),
            _fp_block("B90", "Lib_A", 20, 30, 90, "B.Cu"),
            _fp_block("B270", "Lib_A", 30, 30, 270, "B.Cu"),
            _fp_block("BM90", "Lib_A", 40, 30, -90, "B.Cu"),
        ]
        assert _assert_pins_on_pads(_board_with(tmp_path, blocks)) == 27

    def test_rotation_written_negated_and_normalised(self, tmp_path):
        dsn = _board_with(
            tmp_path,
            [
                _fp_block("P", "Lib_A", 10, 10, 90, "F.Cu"),
                _fp_block("Q", "Lib_A", 20, 10, -90, "F.Cu"),
            ],
        ).export()
        assert "(place P 10000.0 10000.0 front 270)" in dsn
        assert "(place Q 20000.0 10000.0 front 90)" in dsn

    def test_library_on_both_sides_gets_two_images(self, tmp_path):
        dsn = _board_with(
            tmp_path,
            [
                _fp_block("F1", "Lib_A", 10, 10, 0, "F.Cu"),
                _fp_block("B1", "Lib_A", 20, 10, 0, "B.Cu"),
                _fp_block("B2", "Lib_A", 30, 10, 0, "B.Cu"),
            ],
        ).export()
        images = re.findall(r'\(image "?([^"\s]+)"?\s*$', dsn, re.M)
        assert sorted(images) == ["Lib_A", "Lib_A__back"]
        # Back image pre-negates x; every back pad stays on B.Cu.
        back = re.search(r'\(image "?Lib_A__back"?\s*\n(.*?)\n    \)', dsn, re.S).group(1)
        assert re.search(r'"?1"? 2000\.0 500\.0', back)
        assert "Rect[B]" in back

    @pytest.mark.parametrize(
        "board",
        [
            "03-usb-joystick",
            "06-diffpair-test",
            "07-matchgroup-test",
            "09-usbc-pd-power",
        ],
    )
    def test_fleet_boards_pins_land_on_pads(self, board):
        pcbs = sorted((BOARDS / board / "output").glob("*.kicad_pcb"))
        pcbs = [p for p in pcbs if "routed" not in p.name] or pcbs
        if not pcbs:
            pytest.skip(f"no committed PCB for {board}")
        assert _assert_pins_on_pads(KiCadToDSNExporter(str(pcbs[0]))) > 0


# ---------------------------------------------------------------------------
# Issue #7483: NPTH holes and non-copper pads must not become pins
# ---------------------------------------------------------------------------


def _npth_pcb(tmp_path, *, copper_layers=("F.Cu", "B.Cu"), drill="3.2", back=False):
    layers = "\n".join(f'    ({i} "{name}" signal)' for i, name in enumerate(copper_layers))
    fp_layer = "B.Cu" if back else "F.Cu"
    text = f"""(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (layers
{layers}
    (25 "Edge.Cuts" user)
  )
  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))
  (footprint "MountingHole:MountingHole_3.2mm_M3"
    (layer "{fp_layer}")
    (at 10 10)
    (property "Reference" "H1" (at 0 0) (layer "F.SilkS"))
    (pad "" np_thru_hole circle (at 1 2) (size 3.2 3.2) (drill {drill}) (layers "*.Cu" "*.Mask"))
  )
  (footprint "Pkg:Thermal"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "A"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Paste"))
    (pad "3" smd rect (at 4 0) (size 1 1) (layers "F.Mask"))
  )
)
"""
    pcb = tmp_path / "npth.kicad_pcb"
    pcb.write_text(text, encoding="utf-8")
    return pcb


def _library(dsn: str) -> str:
    return dsn[dsn.index("(library") : dsn.index("(network")]


class TestNpthAndNonCopperPads:
    @pytest.mark.parametrize("layers", [("F.Cu", "B.Cu"), ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")])
    def test_npth_is_keepout_per_copper_layer(self, tmp_path, layers):
        dsn = KiCadToDSNExporter(_npth_pcb(tmp_path, copper_layers=layers)).export()
        lib = _library(dsn)
        image = lib[lib.index("(image MountingHole") : lib.index("(image Pkg")]
        assert "(pin" not in image
        keepouts = re.findall(r'\(keepout "" \(circle (\S+) 3200\.0 1000\.0 2000\.0\)\)', image)
        assert keepouts == list(layers)
        assert "*.Cu" not in dsn
        assert "Rect[B]Pad_3200" not in dsn

    def test_oval_drill_becomes_path_keepout(self, tmp_path):
        pcb = _npth_pcb(tmp_path)
        pcb.write_text(pcb.read_text().replace("(drill 3.2)", "(drill oval 3.0 1.0)"))
        dsn = KiCadToDSNExporter(pcb).export()
        paths = re.findall(r'\(keepout "" \(path (\S+) (\S+) (\S+) (\S+) (\S+) (\S+)\)\)', dsn)
        assert [p[0] for p in paths] == ["F.Cu", "B.Cu"]
        for _layer, width, x1, y1, x2, y2 in paths:
            assert float(width) == 1000.0
            assert (float(x1), float(y1), float(x2), float(y2)) == (0.0, 2000.0, 2000.0, 2000.0)

    def test_paste_and_mask_only_pads_have_no_pin(self, tmp_path):
        dsn = KiCadToDSNExporter(_npth_pcb(tmp_path)).export()
        image = _library(dsn)
        image = image[image.index("(image Pkg") :]
        image = image[: image.index("(padstack")]
        assert len(re.findall(r"\(pin ", image)) == 1
        assert "F.Paste" not in dsn and "F.Mask" not in dsn

    def test_fleet_shape_layers_are_declared(self):
        boards = sorted(BOARDS.glob("*/output/*.kicad_pcb"))
        if not boards:
            pytest.skip("no fleet boards found")
        for pcb in boards:
            dsn = KiCadToDSNExporter(pcb).export()
            structure = dsn[dsn.index("(structure") : dsn.index("(placement")]
            declared = set(re.findall(r"\(layer (\S+)", structure))
            used = set(re.findall(r"\(shape \((?:rect|circle|path|polygon) (\S+)", dsn))
            used |= set(re.findall(r'\(keepout "" \((?:circle|path|polygon) (\S+)', dsn))
            assert used <= declared, f"{pcb}: undeclared layers {used - declared}"


# ---------------------------------------------------------------------------
# Issue #7484: pad shape, rotation and custom-pad copper
# ---------------------------------------------------------------------------

_SHAPES_FP = """  (footprint "T:Shapes"
    (layer "{layer}")
    (at 20 20 {rot})
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd custom (at 0 0) (size 0.3 0.3) (layers "{cu}")
      (options (clearance outline) (anchor rect))
      (primitives
        (gr_poly (pts (xy 1 0) (xy 0.5 -0.75) (xy -0.5 -0.75) (xy -0.5 0.75) (xy 0.5 0.75))
          (width 0) (fill yes))))
    (pad "2" smd circle (at 3 0) (size 1.5 1.5) (layers "{cu}"))
    (pad "3" smd oval (at 6 0) (size 2.0 0.8) (layers "{cu}"))
    (pad "4" smd rect (at 9 0 {rot45}) (size 2.0 0.5) (layers "{cu}"))
    (pad "5" smd rect (at 12 0) (size 1.5 1.5) (layers "{cu}"))
  )
"""


def _shapes_dsn(tmp_path, back=False):
    layer, cu = ("B.Cu", "B.Cu") if back else ("F.Cu", "F.Cu")
    block = _SHAPES_FP.format(layer=layer, cu=cu, rot=0, rot45=45)
    dsn = _board_with(tmp_path, [block]).export()
    return dsn


def _padstack_shapes(dsn: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"\(padstack (\S+)\s+\(shape (\(.*\))\)\s+\(attach", dsn):
        out[m.group(1)] = m.group(2)
    return out


def _poly_area(nums):
    pts = list(zip(nums[0::2], nums[1::2], strict=True))
    return (
        abs(
            sum(
                x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True)
            )
        )
        / 2
    )


class TestPadShapes:
    def test_shapes_map_to_padstacks(self, tmp_path):
        shapes = _padstack_shapes(_shapes_dsn(tmp_path))
        by_kind = {}
        for name, shape in shapes.items():
            by_kind.setdefault(shape.split()[0].lstrip("("), []).append((name, shape))
        # circle d=1500, oval path width 800, no under-sized custom rect
        assert any("1500.0 0.0 0.0" in s for _, s in by_kind["circle"])
        assert any(s.split()[2] == "800.0" for _, s in by_kind["path"])
        polygons = []
        for _, s in by_kind["polygon"]:
            nums = [float(v) for v in s.rstrip(")").split()[3:]]
            polygons.append(nums)
        areas = sorted(_poly_area(n) for n in polygons)
        # rotated 2.0 x 0.5 (1.0 mm^2) and the custom pad's hull (>= 1.875 mm^2)
        assert areas[0] == pytest.approx(1.0e6, rel=1e-3)
        assert areas[-1] >= 1.875e6 - 1

    def test_rotated_rect_polygon_contains_rotated_rectangle(self, tmp_path):
        dsn = _shapes_dsn(tmp_path)
        polys = [
            [float(v) for v in s.rstrip(")").split()[3:]]
            for s in _padstack_shapes(dsn).values()
            if s.startswith("(polygon")
        ]
        rot = next(p for p in polys if _poly_area(p) < 1.5e6)
        from shapely.affinity import rotate
        from shapely.geometry import Polygon, box

        expected = rotate(box(-1000, -250, 1000, 250), -45, origin=(0, 0))
        assert Polygon(list(zip(rot[0::2], rot[1::2], strict=True))).buffer(1.0).contains(expected)

    def test_equal_size_circle_and_rect_do_not_share_a_padstack(self, tmp_path):
        names = list(_padstack_shapes(_shapes_dsn(tmp_path)))
        pad_names = [n for n in names if "Pad_1500.0x1500.0" in n]
        assert len(pad_names) == 2
        assert len(set(pad_names)) == 2

    def test_plain_rect_keeps_size_only_name(self, tmp_path):
        assert "Rect[T]Pad_1500.0x1500.0_um" in _shapes_dsn(tmp_path)

    def test_back_side_shapes_stay_on_back_copper(self, tmp_path):
        shapes = _padstack_shapes(_shapes_dsn(tmp_path, back=True))
        assert shapes
        assert all("B.Cu" in s for s in shapes.values())

    def test_back_side_custom_polygon_is_mirrored_like_pins(self, tmp_path):
        front = _padstack_shapes(_shapes_dsn(tmp_path))
        back = _padstack_shapes(_shapes_dsn(tmp_path, back=True))

        def custom(shapes):
            return max(
                (
                    [float(v) for v in s.rstrip(")").split()[3:]]
                    for s in shapes.values()
                    if s.startswith("(polygon")
                ),
                key=_poly_area,
            )

        f, b = custom(front), custom(back)
        assert sorted(zip(f[0::2], f[1::2], strict=True)) == sorted(
            zip((-x for x in b[0::2]), b[1::2], strict=True)
        )

    def test_pad_angle_is_absolute_so_footprint_rotation_cancels(self, tmp_path):
        block = """  (footprint "T:R"
    (layer "F.Cu")
    (at 20 20 90)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0 90) (size 1.0 0.5) (layers "F.Cu"))
  )
"""
        dsn = _board_with(tmp_path, [block]).export()
        assert "Rect[T]Pad_1000.0x500.0_um" in dsn

    def test_arbitrary_angle_polygon_not_axis_aligned_rect(self, tmp_path):
        block = _SHAPES_FP.format(layer="F.Cu", cu="F.Cu", rot=0, rot45=30)
        dsn = _board_with(tmp_path, [block]).export()
        assert "(polygon" in dsn
