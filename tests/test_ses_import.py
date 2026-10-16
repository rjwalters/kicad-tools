"""Tests for SES (Specctra Session) import functionality."""

from pathlib import Path

import pytest

from kicad_tools.export.ses import (
    SESToKiCadImporter,
    _build_net_map,
    _extract_balanced,
    _merge_sexp_into_pcb,
)

# Path to the voltage divider test board
VOLTAGE_DIVIDER_PCB = (
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)


# Minimal SES content for testing
MINIMAL_SES = """\
(session voltage_divider
  (base_design voltage_divider)
  (resolution um 1)
  (routes
    (resolution um 1)
    (parser
      (host_cad "Freerouting")
      (host_version "1.9.0")
    )
    (network_out
    )
    (wire
      (path F.Cu 250.0 105000.0 111230.0 105800.0 110400.0)
      (net VIN)
    )
    (wire
      (path F.Cu 250.0 105800.0 110400.0 114000.0 108000.0)
      (net VIN)
    )
    (wire
      (path F.Cu 250.0 116000.0 108000.0 125000.0 111230.0)
      (net VOUT)
    )
    (via
      "Via[0-1]_Pad800_um" 115000.0 112000.0
      (net GND)
    )
  )
)
"""


class TestExtractBalanced:
    """Test the balanced parenthesis extractor."""

    def test_simple(self):
        text = "(hello world)"
        assert _extract_balanced(text, 0) == "(hello world)"

    def test_nested(self):
        text = "(a (b c) d)"
        assert _extract_balanced(text, 0) == "(a (b c) d)"

    def test_with_string(self):
        text = '(net "my net (special)")'
        assert _extract_balanced(text, 0) == '(net "my net (special)")'

    def test_at_offset(self):
        text = "prefix (inner) suffix"
        assert _extract_balanced(text, 7) == "(inner)"

    def test_not_at_paren(self):
        assert _extract_balanced("hello", 0) is None

    def test_out_of_range(self):
        assert _extract_balanced("(a)", 10) is None

    def test_unbalanced(self):
        assert _extract_balanced("(a (b)", 0) is None


class TestBuildNetMap:
    """Test the net name-to-number mapping builder."""

    def test_basic_net_map(self):
        content = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VIN")
  (net 2 "VOUT")
  (net 3 "GND")
)"""
        net_map = _build_net_map(content)
        assert net_map["VIN"] == 1
        assert net_map["VOUT"] == 2
        assert net_map["GND"] == 3
        assert "" not in net_map  # empty net name excluded

    def test_net_with_special_chars(self):
        content = '  (net 5 "Net-(J1-1)")\n'
        net_map = _build_net_map(content)
        assert net_map["Net-(J1-1)"] == 5


class TestMergeSexpIntoPcb:
    """Test the PCB merge helper."""

    def test_basic_merge(self):
        pcb = '(kicad_pcb\n  (net 0 "")\n)\n'
        route = '  (segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1))'
        merged = _merge_sexp_into_pcb(pcb, route)
        assert "(segment" in merged
        assert merged.rstrip().endswith(")")

    def test_empty_routes(self):
        pcb = '(kicad_pcb\n  (net 0 "")\n)\n'
        merged = _merge_sexp_into_pcb(pcb, "")
        assert merged == pcb

    def test_preserves_pcb_content(self):
        pcb = '(kicad_pcb\n  (net 0 "")\n  (net 1 "VIN")\n)\n'
        route = '  (segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1))'
        merged = _merge_sexp_into_pcb(pcb, route)
        assert '(net 1 "VIN")' in merged


class TestSESParser:
    """Test SES file parsing."""

    @pytest.fixture
    def importer(self, tmp_path):
        ses_file = tmp_path / "test.ses"
        ses_file.write_text(MINIMAL_SES)
        imp = SESToKiCadImporter(str(ses_file))
        imp.parse()
        return imp

    def test_wires_parsed(self, importer):
        assert len(importer.wires) == 3

    def test_vias_parsed(self, importer):
        assert len(importer.vias) == 1

    def test_wire_net_name(self, importer):
        net_names = {w.net_name for w in importer.wires}
        assert "VIN" in net_names
        assert "VOUT" in net_names

    def test_wire_layer(self, importer):
        for wire in importer.wires:
            assert wire.layer == "F.Cu"

    def test_wire_points(self, importer):
        vin_wires = [w for w in importer.wires if w.net_name == "VIN"]
        assert len(vin_wires) == 2
        # First VIN wire: two points
        assert len(vin_wires[0].points) == 2

    def test_wire_width(self, importer):
        for wire in importer.wires:
            assert wire.width == 250.0

    def test_via_net_name(self, importer):
        assert importer.vias[0].net_name == "GND"

    def test_via_coordinates(self, importer):
        via = importer.vias[0]
        assert via.x == 115000.0
        assert via.y == 112000.0

    def test_via_padstack(self, importer):
        via = importer.vias[0]
        assert "Via" in via.padstack_name


class TestSESMerge:
    """Test merging SES routes into a PCB file."""

    @pytest.fixture
    def ses_file(self, tmp_path):
        f = tmp_path / "test.ses"
        f.write_text(MINIMAL_SES)
        return f

    def test_merge_into_pcb(self, ses_file, tmp_path):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        assert output.exists()
        content = output.read_text()

        # Original content preserved
        assert "(kicad_pcb" in content
        assert '(net 1 "VIN")' in content

        # New routes added
        assert "(segment" in content
        assert "(via" in content

    def test_merge_coordinate_conversion(self, ses_file, tmp_path):
        """Verify SES um coordinates are correctly converted to mm."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        # 105000.0 um = 105.0 mm
        assert "105.0" in result

    def test_merge_net_mapping(self, ses_file, tmp_path):
        """Verify SES net names are mapped to correct KiCad net numbers."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        # VIN = net 1, VOUT = net 2, GND = net 3
        assert "(net 1)" in result  # VIN segments
        assert "(net 2)" in result  # VOUT segments
        assert "(net 3)" in result  # GND via

    def test_merge_produces_valid_sexp(self, ses_file, tmp_path):
        """Verify merged PCB has balanced parentheses."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        open_count = result.count("(")
        close_count = result.count(")")
        assert open_count == close_count, f"Unbalanced: {open_count} open vs {close_count} close"


class TestSESEmptyRoutes:
    """Test behavior with SES files containing no routes."""

    def test_empty_ses(self, tmp_path):
        ses_file = tmp_path / "empty.ses"
        ses_file.write_text("(session test\n  (routes\n  )\n)")
        importer = SESToKiCadImporter(str(ses_file))
        importer.parse()
        assert len(importer.wires) == 0
        assert len(importer.vias) == 0

    def test_merge_empty_ses(self, tmp_path):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        ses_file = tmp_path / "empty.ses"
        ses_file.write_text("(session test\n  (routes\n  )\n)")
        output = tmp_path / "out.kicad_pcb"

        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        # Should be identical to original, and still written (issue #6593)
        original = VOLTAGE_DIVIDER_PCB.read_text()
        assert result == original
        assert output.exists()
        assert output.read_text() == original


# ---------------------------------------------------------------------------
# Issue #6593: standard (Freerouting/fastroute-shaped) sessions
# ---------------------------------------------------------------------------

SIMPLE_LED_PCB = (
    Path(__file__).parent.parent / "boards" / "00-simple-led" / "output" / "simple_led.kicad_pcb"
)

# Hand-written from the Specctra session format: wires and vias nest under
# ``network_out > net``, coordinates are in 1/10 um, a ``string_quote`` parser
# block is present and the via padstack is described in ``library_out``.
STANDARD_SES = """\
(session "b00.ses"
  (base_design "b00.dsn")
  (placement
    (resolution um 10)
    (component "Conn"
      (place J1 1410000 875000 front 0)
    )
  )
  (was_is
  )
  (routes
    (resolution um 10)
    (parser
      (string_quote ")
      (space_in_quoted_tokens on)
      (host_cad "KiCad's Pcbnew")
      (host_version "kicad-tools")
    )
    (library_out
      (padstack "Via[0-1]_800:400_um"
        (shape (circle F.Cu 8000 0 0))
        (shape (circle B.Cu 8000 0 0))
        (attach off)
      )
    )
    (network_out
      (net GND
        (wire
          (path F.Cu 2500
            1410000 887700
            1450000 887700
          )
        )
        (wire (path B.Cu 2500 1450000 887700 1450000 900000 1500000 900000))
        (via "Via[0-1]_800:400_um" 1450000 887700)
      )
      (net VCC
        (wire (path B.Cu 2500 1410000 862300 1500000 862300))
      )
    )
  )
)
"""


def _session(body: str, resolution: str = "um 10") -> str:
    return f"(session s\n  (routes\n    (resolution {resolution})\n{body}\n  )\n)\n"


def _write(tmp_path, text: str, name: str = "s.ses") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _segments(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("(segment")]


def _vias(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("(via")]


NAME_ONLY_PCB = """\
(kicad_pcb
  (version 20250114)
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
  )
  (footprint "R" (at 10 10)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "+3V3"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net "Net-(R1-Pad2)"))
  )
)
"""

FOUR_LAYER_PCB = """\
(kicad_pcb
  (version 20240108)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" power)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
)
"""


class TestStandardSessionParse:
    @pytest.fixture
    def importer(self, tmp_path):
        imp = SESToKiCadImporter(_write(tmp_path, STANDARD_SES))
        imp.parse()
        return imp

    def test_nested_wires_inherit_net(self, importer):
        assert [w.net_name for w in importer.wires] == ["GND", "GND", "VCC"]

    def test_nested_via_inherits_net(self, importer):
        assert len(importer.vias) == 1
        via = importer.vias[0]
        assert (via.net_name, via.padstack_name) == ("GND", "Via[0-1]_800:400_um")
        assert (via.x, via.y) == (1450000.0, 887700.0)

    def test_raw_units_kept_on_dataclasses(self, importer):
        wire = importer.wires[0]
        assert wire.width == 2500.0
        assert wire.points == [(1410000.0, 887700.0), (1450000.0, 887700.0)]
        assert len(importer.wires[1].points) == 3

    def test_library_out_padstack(self, importer):
        padstack = importer.padstacks["Via[0-1]_800:400_um"]
        assert padstack.layers == ("F.Cu", "B.Cu")
        assert padstack.diameter == 8000.0

    def test_nothing_skipped(self, importer):
        assert importer.skipped == []


class TestResolutionScale:
    @pytest.mark.parametrize(
        ("resolution", "expected"),
        [("um 10", 0.0001), ("um 1", 0.001), ("mil 10", 0.00254), ("mm 1000", 0.001)],
    )
    def test_mm_per_unit(self, tmp_path, resolution, expected):
        imp = SESToKiCadImporter(_write(tmp_path, _session("", resolution)))
        imp.parse()
        assert imp.mm_per_unit == pytest.approx(expected)

    def test_unknown_unit_raises(self, tmp_path):
        imp = SESToKiCadImporter(_write(tmp_path, _session("", "furlong 1")))
        with pytest.raises(ValueError, match="furlong"):
            imp.parse()

    def test_resolution_falls_back_to_placement(self, tmp_path):
        text = "(session s (placement (resolution mil 1)) (routes))"
        imp = SESToKiCadImporter(_write(tmp_path, text))
        imp.parse()
        assert imp.mm_per_unit == pytest.approx(0.0254)

    def test_mil_scale_merges_in_mm(self, tmp_path):
        if not SIMPLE_LED_PCB.exists():
            pytest.skip("board 00 not found")
        body = "    (network_out (net GND (wire (path F.Cu 100 55512 34949 57087 34949))))"
        imp = SESToKiCadImporter(_write(tmp_path, _session(body, "mil 10")))
        out = imp.merge_into(SIMPLE_LED_PCB, tmp_path / "o.kicad_pcb")
        (seg,) = _segments(out)
        assert "(start 141.0005 88.7705)" in seg
        assert "(width 0.2540)" in seg


class TestStringQuoteAndLegacy:
    def test_string_quote_block_does_not_drop_wires(self, tmp_path):
        body = (
            '    (parser (string_quote ") (space_in_quoted_tokens on) '
            '(host_cad "KiCad\'s Pcbnew"))\n'
            "    (wire (path F.Cu 2500 1410000 887700 1450000 887700) (net GND))"
        )
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        imp.parse()
        assert [w.net_name for w in imp.wires] == ["GND"]

    def test_legacy_inline_net_still_parses(self, tmp_path):
        body = (
            "    (network_out)\n"
            "    (wire (path F.Cu 2500 1 2 3 4) (net VIN))\n"
            '    (via "Via[0-1]_x" 5 6 (net "Net-(J1-1)"))'
        )
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        imp.parse()
        assert [w.net_name for w in imp.wires] == ["VIN"]
        assert [v.net_name for v in imp.vias] == ["Net-(J1-1)"]

    def test_unsupported_wire_shape_reported(self, tmp_path):
        body = "    (network_out (net GND (wire (qarc F.Cu 100 0 0 10 10 5 5))))"
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        imp.parse()
        assert imp.wires == []
        (skip,) = imp.skipped
        assert skip.kind == "wire" and "qarc" in skip.reason


class TestStandardSessionMerge:
    def test_merge_board00_mm_coordinates(self, tmp_path):
        if not SIMPLE_LED_PCB.exists():
            pytest.skip("board 00 not found")
        out_path = tmp_path / "merged.kicad_pcb"
        imp = SESToKiCadImporter(_write(tmp_path, STANDARD_SES))
        out = imp.merge_into(SIMPLE_LED_PCB, out_path)

        assert out_path.read_text() == out
        assert imp.skipped == []
        assert (imp.imported_wires, imp.imported_vias) == (3, 1)
        segments = _segments(out)
        assert len(segments) == 4  # 1 + 2 + 1 segments
        assert "(start 141.0000 88.7700) (end 145.0000 88.7700) (width 0.2500)" in segments[0]
        import re

        for seg in segments:
            xs = [float(v) for v in re.findall(r"\((?:start|end) ([-\d.]+)", seg)]
            ys = [float(v) for v in re.findall(r"\((?:start|end) [-\d.]+ ([-\d.]+)", seg)]
            assert all(136 <= x <= 161 for x in xs), seg
            assert all(77.5 <= y <= 97.5 for y in ys), seg
        net_map = _build_net_map(SIMPLE_LED_PCB.read_text())
        assert f"(net {net_map['GND']})" in segments[0]
        assert f"(net {net_map['VCC']})" in segments[3]

        (via,) = _vias(out)
        assert '(layers "F.Cu" "B.Cu")' in via
        assert "(size 0.8000)" in via
        assert "(at 145.0000 88.7700)" in via

    def test_name_only_board_emits_named_nets(self, tmp_path):
        pcb = _write(tmp_path, NAME_ONLY_PCB, "named.kicad_pcb")
        body = (
            "    (network_out\n"
            '      (net "+3V3" (wire (path F.Cu 2000 100000 100000 120000 100000)))\n'
            '      (net "Net-(R1-Pad2)" (wire (path F.Cu 2000 100000 120000 120000 120000)))\n'
            "    )"
        )
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        out = imp.merge_into(pcb, tmp_path / "o.kicad_pcb")
        assert imp.skipped == []
        segs = _segments(out)
        assert '(net "+3V3")' in segs[0]
        assert '(net "Net-(R1-Pad2)")' in segs[1]
        from kicad_tools.sexp import parse_string

        parse_string(out)  # still a well-formed board

    def test_blind_via_span_from_library_out(self, tmp_path):
        pcb = _write(tmp_path, FOUR_LAYER_PCB, "four.kicad_pcb")
        body = (
            "    (library_out (padstack blind12\n"
            "      (shape (circle In2.Cu 4000)) (shape (circle In1.Cu 4000))))\n"
            "    (network_out (net GND (via blind12 100000 100000)))"
        )
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        out = imp.merge_into(pcb, tmp_path / "o.kicad_pcb")
        (via,) = _vias(out)
        assert '(layers "In1.Cu" "In2.Cu")' in via
        assert via.lstrip().startswith("(via buried ")
        assert "(size 0.4000)" in via

    def test_via_index_range_fallback(self, tmp_path):
        pcb = _write(tmp_path, FOUR_LAYER_PCB, "four.kicad_pcb")
        body = (
            '    (network_out (net GND (via "Via[0-3]_Pad600_um" 100000 100000)'
            ' (via "Via[0-1]_Pad600_um" 110000 100000)))'
        )
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        out = imp.merge_into(pcb, tmp_path / "o.kicad_pcb")
        (through,) = _vias(out)
        assert '(layers "F.Cu" "B.Cu")' in through
        # Via[0-1] is not a full span on a 4-layer board; the exporter never
        # writes partial spans, so it is skipped rather than guessed (#6602).
        (skip,) = imp.skipped
        assert skip.kind == "via" and "Via[0-1]" in skip.reason

    @pytest.mark.parametrize("kind", ["ten_layer", "mixed_inner"])
    def test_via_fallback_never_buried_when_dsn_drops_layers(self, tmp_path, kind):
        """Round trip: the exporter's Via[0-n] name must not come back blind/buried."""
        from kicad_tools.export.dsn import KiCadToDSNExporter

        if kind == "ten_layer":
            names = ["F.Cu", *(f"In{i}.Cu" for i in range(1, 9)), "B.Cu"]
            types = ["signal"] * 10
        else:
            names = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            types = ["signal", "signal", "mixed", "signal"]
        decl = "\n".join(
            f'    ({31 if n == "B.Cu" else i} "{n}" {t})'
            for i, (n, t) in enumerate(zip(names, types, strict=True))
        )
        pcb_text = (
            "(kicad_pcb\n  (version 20240108)\n  (layers\n"
            f'{decl}\n    (44 "Edge.Cuts" user)\n  )\n'
            '  (net 0 "")\n  (net 1 "GND")\n)\n'
        )
        pcb = _write(tmp_path, pcb_text, "board.kicad_pcb")
        exporter = KiCadToDSNExporter(pcb)
        exporter._load()
        exporter._extract_layers()
        assert len(exporter._layers) < len(names)  # exporter keeps fewer layers
        via_name = exporter._via_padstack_name_for(0.6)

        body = f'    (network_out (net GND (via "{via_name}" 100000 100000)))'
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        out = imp.merge_into(pcb, tmp_path / "o.kicad_pcb")
        vias = _vias(out)
        if vias:
            (via,) = vias
            assert '(layers "F.Cu" "B.Cu")' in via
            assert not via.lstrip().startswith(("(via blind", "(via buried"))
        else:
            (skip,) = imp.skipped
            assert skip.kind == "via" and via_name in skip.reason

    def test_unknown_padstack_is_skipped_not_defaulted(self, tmp_path):
        pcb = _write(tmp_path, FOUR_LAYER_PCB, "four.kicad_pcb")
        body = "    (network_out (net GND (via mystery 100000 100000)))"
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        out = imp.merge_into(pcb, tmp_path / "o.kicad_pcb")
        assert _vias(out) == []
        (skip,) = imp.skipped
        assert skip.kind == "via" and "mystery" in skip.reason

    def test_unknown_net_is_skipped(self, tmp_path):
        pcb = _write(tmp_path, FOUR_LAYER_PCB, "four.kicad_pcb")
        body = "    (network_out (net NOPE (wire (path F.Cu 2000 0 0 10000 0))))"
        imp = SESToKiCadImporter(_write(tmp_path, _session(body)))
        out_path = tmp_path / "o.kicad_pcb"
        imp.merge_into(pcb, out_path)
        assert out_path.read_text() == FOUR_LAYER_PCB
        (skip,) = imp.skipped
        assert (skip.kind, skip.net_name) == ("wire", "NOPE")
        assert imp.imported_wires == 0


class TestImportSesCli:
    def _run(self, argv, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.cli.parser import create_parser

        rc = run_pcb_command(create_parser().parse_args(argv))
        return rc, capsys.readouterr()

    def test_clean_import_exits_zero(self, tmp_path, capsys):
        if not SIMPLE_LED_PCB.exists():
            pytest.skip("board 00 not found")
        ses = _write(tmp_path, STANDARD_SES)
        out = tmp_path / "o.kicad_pcb"
        rc, cap = self._run(
            ["pcb", "import-ses", str(SIMPLE_LED_PCB), str(ses), "-o", str(out)], capsys
        )
        assert rc == 0, cap.err
        assert "Wires: 3/3" in cap.out
        assert out.exists()

    def test_unknown_net_exits_one_and_names_it(self, tmp_path, capsys):
        pcb = _write(tmp_path, FOUR_LAYER_PCB, "four.kicad_pcb")
        body = (
            "    (network_out (net GND (wire (path F.Cu 2000 0 0 10000 0)))"
            " (net NOPE (wire (path F.Cu 2000 0 0 10000 0))))"
        )
        ses = _write(tmp_path, _session(body))
        out = tmp_path / "o.kicad_pcb"
        rc, cap = self._run(["pcb", "import-ses", str(pcb), str(ses), "-o", str(out)], capsys)
        assert rc == 1
        assert "NOPE" in cap.out
        assert out.exists()

        rc, cap = self._run(
            ["pcb", "import-ses", str(pcb), str(ses), "-o", str(out), "--format", "json"],
            capsys,
        )
        import json

        payload = json.loads(cap.out)
        assert rc == 1
        assert payload["success"] is False
        assert payload["wires"] == 1
        assert any("NOPE" in s for s in payload["skipped"])

    def test_empty_session_exits_one(self, tmp_path, capsys):
        pcb = _write(tmp_path, FOUR_LAYER_PCB, "four.kicad_pcb")
        ses = _write(tmp_path, "(session s (routes (resolution um 10)))")
        out = tmp_path / "o.kicad_pcb"
        rc, cap = self._run(["pcb", "import-ses", str(pcb), str(ses), "-o", str(out)], capsys)
        assert rc == 1
        assert "no wires or vias imported" in cap.err
        assert out.read_text() == FOUR_LAYER_PCB
