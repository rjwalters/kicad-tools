"""Tests for the sch tidy command and the shared field_geometry module.

Covers bbox computation (graphics, pin fallback, rotation/mirror), the
field-offset metric, default field positions, the tidy command itself
(--refs, --threshold, --dry-run, hidden/power/unresolvable skips), and the
cosmetic-only invariance guarantees (structural diff, BOM, netlist).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.sch_tidy import (
    main as tidy_main,
)
from kicad_tools.cli.sch_tidy import (
    tidy_fields,
)
from kicad_tools.schema import Schematic
from kicad_tools.schema.bom import extract_bom_from_schematic
from kicad_tools.schema.field_geometry import (
    DEFAULT_FIELD_CLEARANCE_MM,
    default_field_positions,
    field_offset_mm,
    placed_body_bbox,
)
from kicad_tools.schema.library import LibrarySymbol
from kicad_tools.sexp.parser import SExp, parse_string

REAL_FIXTURE = (
    Path(__file__).parent.parent / "boards" / "00-simple-led" / "output" / "simple_led.kicad_sch"
)

# ---------------------------------------------------------------------------
# Library symbol fixtures (parsed via LibrarySymbol.from_sexp)
# ---------------------------------------------------------------------------

RESISTOR_LIB_SYMBOL = """\
(symbol "Device:R"
  (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (symbol "R_0_1"
    (rectangle (start -1.016 -2.54) (end 1.016 2.54)
      (stroke (width 0.254) (type default)) (fill (type none)))
  )
  (symbol "R_1_1"
    (pin passive line (at 0 3.81 270) (length 1.27) (name "1" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin passive line (at 0 -3.81 90) (length 1.27) (name "2" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )
)
"""

PIN_ONLY_LIB_SYMBOL = """\
(symbol "power:GND"
  (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (symbol "GND_1_1"
    (pin power_in line (at 0 0 270) (length 0) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
  )
)
"""


def _lib_symbol(text: str) -> LibrarySymbol:
    return LibrarySymbol.from_sexp(parse_string(text))


# ---------------------------------------------------------------------------
# Schematic fixtures
# ---------------------------------------------------------------------------


def _resistor_instance(
    ref: str,
    uuid: str,
    x: float,
    y: float,
    rot: int = 0,
    mirror: str = "",
    ref_at: tuple[float, float, float] | None = None,
    val_at: tuple[float, float, float] | None = None,
    hide_ref: bool = False,
) -> str:
    """Build a placed Device:R symbol instance sexp string."""
    rx, ry, ra = ref_at if ref_at else (x + 30, y + 30, 0)
    vx, vy, va = val_at if val_at else (x - 30, y - 30, 0)
    mirror_str = f" (mirror {mirror})" if mirror else ""
    hide_str = " hide" if hide_ref else ""
    return f"""\
  (symbol (lib_id "Device:R") (at {x} {y} {rot}){mirror_str} (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "{uuid}")
    (property "Reference" "{ref}" (at {rx} {ry} {ra}) (effects (font (size 1.27 1.27)){hide_str}))
    (property "Value" "10k" (at {vx} {vy} {va}) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))
    (pin "1" (uuid "{uuid}-p1"))
    (pin "2" (uuid "{uuid}-p2"))
  )
"""


def _schematic(symbols: str, extra_lib_symbols: str = "") -> str:
    return f"""\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
{RESISTOR_LIB_SYMBOL}{PIN_ONLY_LIB_SYMBOL}{extra_lib_symbols}  )
{symbols})
"""


ROTATION_FIXTURE = _schematic(
    _resistor_instance("R1", "aaaaaaaa-0000-0000-0000-000000000001", 50, 50, rot=0)
    + _resistor_instance("R2", "aaaaaaaa-0000-0000-0000-000000000002", 100, 50, rot=90)
    + _resistor_instance("R3", "aaaaaaaa-0000-0000-0000-000000000003", 150, 50, rot=180)
    + _resistor_instance("R4", "aaaaaaaa-0000-0000-0000-000000000004", 50, 100, rot=270)
    + _resistor_instance("R5", "aaaaaaaa-0000-0000-0000-000000000005", 100, 100, rot=90, mirror="y")
)

POWER_FIXTURE = _schematic(
    _resistor_instance("R1", "bbbbbbbb-0000-0000-0000-000000000001", 50, 50)
    + """\
  (symbol (lib_id "power:GND") (at 100 100 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "bbbbbbbb-0000-0000-0000-000000000002")
    (property "Reference" "#PWR01" (at 130 130 0) (effects (font (size 1.27 1.27))))
    (property "Value" "GND" (at 130 135 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "bbbbbbbb-0000-0000-0000-000000000002-p1"))
  )
"""
)

UNRESOLVED_FIXTURE = _schematic(
    _resistor_instance("R1", "cccccccc-0000-0000-0000-000000000001", 50, 50)
    + """\
  (symbol (lib_id "Missing:Symbol") (at 100 100 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "cccccccc-0000-0000-0000-000000000002")
    (property "Reference" "U1" (at 130 130 0) (effects (font (size 1.27 1.27))))
    (property "Value" "MYSTERY" (at 130 135 0) (effects (font (size 1.27 1.27))))
  )
"""
)

MULTI_UNIT_LIB_SYMBOL = """\
(symbol "Lib:Dual"
  (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (property "Value" "Dual" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (symbol "Dual_1_1"
    (pin input line (at -2.54 2.54 0) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
    (pin output line (at 2.54 -2.54 180) (length 1.27) (name "Y" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
  )
  (symbol "Dual_2_1"
    (pin input line (at -5.08 5.08 0) (length 1.27) (name "B" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
    (pin output line (at 5.08 -5.08 180) (length 1.27) (name "Z" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
  )
)
"""

MULTI_UNIT_FIXTURE = _schematic(
    """\
  (symbol (lib_id "Lib:Dual") (at 50 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "dddddddd-0000-0000-0000-000000000001")
    (property "Reference" "U1" (at 90 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Dual" (at 90 95 0) (effects (font (size 1.27 1.27))))
  )
  (symbol (lib_id "Lib:Dual") (at 120 50 0) (unit 2)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "dddddddd-0000-0000-0000-000000000002")
    (property "Reference" "U1" (at 20 20 0) (effects (font (size 1.27 1.27))))
    (property "Value" "Dual" (at 20 25 0) (effects (font (size 1.27 1.27))))
  )
""",
    extra_lib_symbols=MULTI_UNIT_LIB_SYMBOL,
)


def _load(content: str) -> Schematic:
    return Schematic(parse_string(content))


def _write(tmp_path: Path, content: str, name: str = "test.kicad_sch") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _symbol_bbox(sch: Schematic, ref: str, unit: int = 1):
    """Recompute the placed body bbox for a symbol in a schematic."""
    for inst in sch.symbols:
        if inst.reference == ref and inst.unit == unit:
            lib_sym = sch.get_lib_symbol_resolved(inst.lib_id)
            assert lib_sym is not None
            return placed_body_bbox(
                lib_sym, inst.position, rotation=inst.rotation, mirror=inst.mirror, unit=inst.unit
            )
    raise AssertionError(f"symbol {ref} not found")


def _field_at(
    sch: Schematic, ref: str, field_name: str, unit: int = 1
) -> tuple[float, float, float]:
    for sym_sexp in sch.sexp.find_children("symbol"):
        ref_val = None
        unit_val = 1
        if u := sym_sexp.find_child("unit"):
            unit_val = u.get_int(0) or 1
        for prop in sym_sexp.find_children("property"):
            if prop.get_string(0) == "Reference":
                ref_val = prop.get_string(1)
        if ref_val != ref or unit_val != unit:
            continue
        for prop in sym_sexp.find_children("property"):
            if prop.get_string(0) == field_name:
                at = prop.find_child("at")
                assert at is not None
                return (at.get_float(0) or 0.0, at.get_float(1) or 0.0, at.get_float(2) or 0.0)
    raise AssertionError(f"field {field_name} of {ref} not found")


# ---------------------------------------------------------------------------
# field_geometry: placed_body_bbox
# ---------------------------------------------------------------------------


class TestPlacedBodyBbox:
    def test_bbox_from_rectangle_graphics(self):
        lib = _lib_symbol(RESISTOR_LIB_SYMBOL)
        bbox = placed_body_bbox(lib, (100.0, 50.0))
        assert bbox == pytest.approx((98.984, 47.46, 101.016, 52.54))

    def test_bbox_rotation_90_swaps_extents(self):
        lib = _lib_symbol(RESISTOR_LIB_SYMBOL)
        bbox0 = placed_body_bbox(lib, (100.0, 50.0), rotation=0)
        bbox90 = placed_body_bbox(lib, (100.0, 50.0), rotation=90)
        w0, h0 = bbox0[2] - bbox0[0], bbox0[3] - bbox0[1]
        w90, h90 = bbox90[2] - bbox90[0], bbox90[3] - bbox90[1]
        assert (w90, h90) == pytest.approx((h0, w0))

    def test_bbox_rotation_180_matches_symmetric_body(self):
        lib = _lib_symbol(RESISTOR_LIB_SYMBOL)
        bbox0 = placed_body_bbox(lib, (100.0, 50.0), rotation=0)
        bbox180 = placed_body_bbox(lib, (100.0, 50.0), rotation=180)
        assert bbox180 == pytest.approx(bbox0)

    def test_bbox_mirror_asymmetric_polyline(self):
        text = """\
(symbol "Lib:Tri"
  (symbol "Tri_0_1"
    (polyline (pts (xy 0 0) (xy 2.54 0) (xy 2.54 1.27) (xy 0 0))
      (stroke (width 0.254) (type default)) (fill (type none)))
  )
)
"""
        lib = _lib_symbol(text)
        bbox = placed_body_bbox(lib, (0.0, 0.0))
        assert bbox[0] == pytest.approx(0.0)
        assert bbox[2] == pytest.approx(2.54)
        # mirror "x" negates library x
        bbox_m = placed_body_bbox(lib, (0.0, 0.0), mirror="x")
        assert bbox_m[0] == pytest.approx(-2.54)
        assert bbox_m[2] == pytest.approx(0.0)

    def test_bbox_from_circle_graphics(self):
        text = """\
(symbol "Lib:C"
  (symbol "C_0_1"
    (circle (center 1 1) (radius 2)
      (stroke (width 0.254) (type default)) (fill (type none)))
  )
)
"""
        lib = _lib_symbol(text)
        bbox = placed_body_bbox(lib, (10.0, 10.0))
        # library (x, y) -> sheet (x, -y): center (1, -1), radius 2
        assert bbox == pytest.approx((9.0, 7.0, 13.0, 11.0))

    def test_bbox_pin_fallback_without_graphics(self):
        lib = _lib_symbol(PIN_ONLY_LIB_SYMBOL)
        assert not lib.graphics
        bbox = placed_body_bbox(lib, (100.0, 100.0))
        # single pin at library origin -> degenerate bbox at position
        assert bbox == pytest.approx((100.0, 100.0, 100.0, 100.0))

    def test_bbox_empty_symbol_is_degenerate_at_position(self):
        lib = _lib_symbol('(symbol "Lib:Empty")')
        assert placed_body_bbox(lib, (5.0, 7.0)) == (5.0, 7.0, 5.0, 7.0)

    def test_bbox_multi_unit_uses_unit_pins(self):
        lib = _lib_symbol(MULTI_UNIT_LIB_SYMBOL)
        assert lib.units == 2
        bbox1 = placed_body_bbox(lib, (0.0, 0.0), unit=1)
        bbox2 = placed_body_bbox(lib, (0.0, 0.0), unit=2)
        assert bbox1 == pytest.approx((-2.54, -2.54, 2.54, 2.54))
        assert bbox2 == pytest.approx((-5.08, -5.08, 5.08, 5.08))


# ---------------------------------------------------------------------------
# field_geometry: field_offset_mm
# ---------------------------------------------------------------------------


class TestFieldOffset:
    BBOX = (10.0, 20.0, 30.0, 40.0)

    def test_inside_is_zero(self):
        assert field_offset_mm((20.0, 30.0), self.BBOX) == 0.0

    def test_on_edge_is_zero(self):
        assert field_offset_mm((10.0, 30.0), self.BBOX) == 0.0

    def test_axis_distance(self):
        assert field_offset_mm((5.0, 30.0), self.BBOX) == pytest.approx(5.0)
        assert field_offset_mm((20.0, 45.0), self.BBOX) == pytest.approx(5.0)

    def test_corner_distance(self):
        assert field_offset_mm((7.0, 16.0), self.BBOX) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# field_geometry: default_field_positions
# ---------------------------------------------------------------------------


class TestDefaultFieldPositions:
    def test_reference_above_value_below(self):
        bbox = (98.984, 47.46, 101.016, 52.54)
        positions = default_field_positions(bbox)
        rx, ry, ra = positions["Reference"]
        vx, vy, va = positions["Value"]
        assert ry < bbox[1]  # above (sheet Y-down)
        assert vy > bbox[3]  # below
        assert ra == 0.0
        assert va == 0.0
        # grid aligned
        for coord in (rx, ry, vx, vy):
            assert coord / 1.27 == pytest.approx(round(coord / 1.27))

    def test_positions_outside_but_close_for_all_rotations(self):
        lib = _lib_symbol(RESISTOR_LIB_SYMBOL)
        for rot in (0, 90, 180, 270):
            for mirror in ("", "y"):
                bbox = placed_body_bbox(lib, (100.0, 50.0), rotation=rot, mirror=mirror)
                for _name, (x, y, _angle) in default_field_positions(bbox).items():
                    offset = field_offset_mm((x, y), bbox)
                    assert 0.0 < offset <= 3.0, (rot, mirror, _name, offset)

    def test_custom_clearance(self):
        bbox = (0.0, 0.0, 2.54, 2.54)
        positions = default_field_positions(bbox, clearance=2.54)
        assert positions["Reference"][1] == pytest.approx(-2.54)
        assert positions["Value"][1] == pytest.approx(5.08)

    def test_clearance_survives_grid_snap(self):
        # Off-grid bbox edges: snap moves by <= 0.635, clearance is 1.27
        bbox = (10.1, 20.3, 30.7, 40.9)
        positions = default_field_positions(bbox)
        assert positions["Reference"][1] < bbox[1]
        assert positions["Value"][1] > bbox[3]
        assert DEFAULT_FIELD_CLEARANCE_MM == 1.27


# ---------------------------------------------------------------------------
# tidy: core behaviour
# ---------------------------------------------------------------------------


class TestTidyCore:
    def test_tidy_all_rotations_and_mirror(self):
        sch = _load(ROTATION_FIXTURE)
        result = tidy_fields(sch)
        assert len(result.symbols) == 5
        assert result.fields_changed == 10
        assert not result.warnings

        for ref in ("R1", "R2", "R3", "R4", "R5"):
            bbox = _symbol_bbox(sch, ref)
            for field_name in ("Reference", "Value"):
                x, y, angle = _field_at(sch, ref, field_name)
                offset = field_offset_mm((x, y), bbox)
                assert 0.0 < offset <= 3.0, (ref, field_name, offset)
                assert angle == 0.0

    def test_tidy_round_trips_and_reparses(self, tmp_path):
        path = _write(tmp_path, ROTATION_FIXTURE)
        sch = Schematic.load(path)
        tidy_fields(sch)
        sch.save()

        reloaded = Schematic.load(path)
        assert len(reloaded.symbols) == 5
        assert {s.reference for s in reloaded.symbols} == {"R1", "R2", "R3", "R4", "R5"}

    def test_tidy_is_idempotent(self, tmp_path):
        path = _write(tmp_path, ROTATION_FIXTURE)
        sch = Schematic.load(path)
        tidy_fields(sch)
        sch.save()
        first = path.read_text()

        sch2 = Schematic.load(path)
        result = tidy_fields(sch2)
        assert result.fields_changed == 0
        sch2.save()
        assert path.read_text() == first

    def test_angle_reset_to_horizontal(self):
        content = _schematic(
            _resistor_instance(
                "R1",
                "eeeeeeee-0000-0000-0000-000000000001",
                50,
                50,
                ref_at=(80, 80, 90),
                val_at=(20, 20, 45),
            )
        )
        sch = _load(content)
        tidy_fields(sch)
        assert _field_at(sch, "R1", "Reference")[2] == 0.0
        assert _field_at(sch, "R1", "Value")[2] == 0.0

    def test_refs_scoping_leaves_others_byte_identical(self):
        sch = _load(ROTATION_FIXTURE)
        before = {ref: _serialize_symbol(sch, ref) for ref in ("R1", "R2", "R3", "R4", "R5")}
        result = tidy_fields(sch, refs=["R2"])
        assert [p.reference for p in result.symbols] == ["R2"]
        after = {ref: _serialize_symbol(sch, ref) for ref in ("R1", "R2", "R3", "R4", "R5")}
        assert after["R2"] != before["R2"]
        for ref in ("R1", "R3", "R4", "R5"):
            assert after[ref] == before[ref], ref

    def test_threshold_skips_nearby_fields(self):
        # Fields ~42mm away (default fixture offsets) vs a close pair
        content = _schematic(
            _resistor_instance("R1", "ffffffff-0000-0000-0000-000000000001", 50, 50)
            + _resistor_instance(
                "R2",
                "ffffffff-0000-0000-0000-000000000002",
                100,
                50,
                ref_at=(100, 45, 0),
                val_at=(100, 55, 0),
            )
        )
        sch = _load(content)
        result = tidy_fields(sch, threshold=15.0)
        assert [p.reference for p in result.symbols] == ["R1"]

    def test_power_symbols_skipped_by_default(self):
        sch = _load(POWER_FIXTURE)
        result = tidy_fields(sch)
        assert [p.reference for p in result.symbols] == ["R1"]

    def test_power_symbol_tidied_when_named_in_refs(self):
        sch = _load(POWER_FIXTURE)
        result = tidy_fields(sch, refs=["#PWR01"])
        assert [p.reference for p in result.symbols] == ["#PWR01"]
        # Reference field of the GND lib symbol is visible in this fixture's
        # placed instance; both fields should now be near the (degenerate) bbox.
        x, y, _ = _field_at(sch, "#PWR01", "Value")
        assert field_offset_mm((x, y), _symbol_bbox(sch, "#PWR01")) <= 3.0

    def test_hidden_field_not_moved(self):
        content = _schematic(
            _resistor_instance("R1", "11111111-0000-0000-0000-000000000001", 50, 50, hide_ref=True)
        )
        sch = _load(content)
        result = tidy_fields(sch)
        assert result.fields_changed == 1  # only Value
        assert result.symbols[0].changes[0].field == "Value"
        assert _field_at(sch, "R1", "Reference")[:2] == (80.0, 80.0)

    def test_unresolvable_lib_id_warns_and_skips(self):
        sch = _load(UNRESOLVED_FIXTURE)
        result = tidy_fields(sch)
        assert [p.reference for p in result.symbols] == ["R1"]
        assert len(result.warnings) == 1
        assert "U1" in result.warnings[0]
        assert "Missing:Symbol" in result.warnings[0]
        # untouched
        assert _field_at(sch, "U1", "Reference")[:2] == (130.0, 130.0)

    def test_multi_unit_each_unit_placed_near_its_instance(self):
        sch = _load(MULTI_UNIT_FIXTURE)
        result = tidy_fields(sch)
        assert len(result.symbols) == 2
        assert {p.unit for p in result.symbols} == {1, 2}

        x1, _, _ = _field_at(sch, "U1", "Reference", unit=1)
        x2, _, _ = _field_at(sch, "U1", "Reference", unit=2)
        assert x1 == pytest.approx(50.0, abs=1.5)
        assert x2 == pytest.approx(120.0, abs=1.5)


def _serialize_symbol(sch: Schematic, ref: str) -> str:
    for sym_sexp in sch.sexp.find_children("symbol"):
        for prop in sym_sexp.find_children("property"):
            if prop.get_string(0) == "Reference" and prop.get_string(1) == ref:
                return sym_sexp.to_string()
    raise AssertionError(f"symbol {ref} not found")


# ---------------------------------------------------------------------------
# tidy: cosmetic-only invariance
# ---------------------------------------------------------------------------


def _normalize_field_ats(root: SExp) -> None:
    """Zero the (at ...) of Reference/Value properties on placed symbols."""
    for sym_sexp in root.find_children("symbol"):
        for prop in sym_sexp.find_children("property"):
            if prop.get_string(0) in ("Reference", "Value"):
                if at := prop.find_child("at"):
                    at.set_value(0, 0)
                    at.set_value(1, 0)
                    at.set_value(2, 0)


class TestInvariance:
    def test_structural_diff_only_field_ats_change(self, tmp_path):
        path = _write(tmp_path, ROTATION_FIXTURE)
        before_tree = parse_string(path.read_text())

        sch = Schematic.load(path)
        result = tidy_fields(sch)
        assert result.fields_changed > 0
        sch.save()

        after_tree = parse_string(path.read_text())

        _normalize_field_ats(before_tree)
        _normalize_field_ats(after_tree)
        assert before_tree.to_string() == after_tree.to_string()

    @pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="board fixture not available")
    def test_structural_diff_on_real_fixture(self, tmp_path):
        path = tmp_path / "simple_led.kicad_sch"
        shutil.copy(REAL_FIXTURE, path)
        before_tree = parse_string(path.read_text())

        sch = Schematic.load(path)
        result = tidy_fields(sch)
        assert result.fields_changed > 0
        sch.save()

        after_tree = parse_string(path.read_text())
        _normalize_field_ats(before_tree)
        _normalize_field_ats(after_tree)
        assert before_tree.to_string() == after_tree.to_string()

    @pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="board fixture not available")
    def test_bom_invariance_on_real_fixture(self, tmp_path):
        path = tmp_path / "simple_led.kicad_sch"
        shutil.copy(REAL_FIXTURE, path)

        bom_before = extract_bom_from_schematic(Schematic.load(path))

        sch = Schematic.load(path)
        tidy_fields(sch)
        sch.save()

        bom_after = extract_bom_from_schematic(Schematic.load(path))
        assert bom_after == bom_before

    @pytest.mark.skipif(not REAL_FIXTURE.exists(), reason="board fixture not available")
    def test_netlist_invariance_on_real_fixture(self, tmp_path):
        from kicad_tools.cli.export_netlist import export_netlist, load_netlist
        from kicad_tools.cli.runner import find_kicad_cli

        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            pytest.skip("kicad-cli not available")

        path = tmp_path / "simple_led.kicad_sch"
        shutil.copy(REAL_FIXTURE, path)

        def _netlist_signature(net_path: Path):
            netlist = load_netlist(net_path)
            components = sorted((c.reference, c.value, c.footprint) for c in netlist.components)
            nets = sorted(
                (net.name, tuple(sorted((n.reference, n.pin) for n in net.nodes)))
                for net in netlist.nets
            )
            return components, nets

        before_out = tmp_path / "before.net"
        ok, err = export_netlist(path, before_out, kicad_cli)
        assert ok, err
        before = _netlist_signature(before_out)

        sch = Schematic.load(path)
        result = tidy_fields(sch)
        assert result.fields_changed > 0
        sch.save()

        after_out = tmp_path / "after.net"
        ok, err = export_netlist(path, after_out, kicad_cli)
        assert ok, err
        after = _netlist_signature(after_out)

        assert after == before


# ---------------------------------------------------------------------------
# tidy: CLI entry point
# ---------------------------------------------------------------------------


class TestTidyCli:
    def test_dry_run_leaves_file_byte_identical(self, tmp_path, capsys):
        path = _write(tmp_path, ROTATION_FIXTURE)
        original = path.read_bytes()

        rc = tidy_main([str(path), "--dry-run"])
        assert rc == 0
        assert path.read_bytes() == original

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "R1" in out
        assert "->" in out  # before/after offsets
        assert "would change" in out

    def test_dry_run_json(self, tmp_path, capsys):
        path = _write(tmp_path, ROTATION_FIXTURE)
        original = path.read_bytes()

        rc = tidy_main([str(path), "--dry-run", "--format", "json"])
        assert rc == 0
        assert path.read_bytes() == original

        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is True
        assert data["modified"] is False
        assert data["symbols_changed"] == 5
        assert data["fields_changed"] == 10
        change = data["symbols"][0]["changes"][0]
        assert {"field", "old_position", "new_position", "old_offset", "new_offset"} <= set(change)

    def test_apply_writes_file(self, tmp_path, capsys):
        path = _write(tmp_path, ROTATION_FIXTURE)
        original = path.read_bytes()

        rc = tidy_main([str(path)])
        assert rc == 0
        assert path.read_bytes() != original
        assert "changed" in capsys.readouterr().out

        sch = Schematic.load(path)
        bbox = _symbol_bbox(sch, "R1")
        x, y, _ = _field_at(sch, "R1", "Reference")
        assert field_offset_mm((x, y), bbox) <= 3.0

    def test_apply_json_output(self, tmp_path, capsys):
        path = _write(tmp_path, ROTATION_FIXTURE)
        rc = tidy_main([str(path), "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["dry_run"] is False
        assert data["modified"] is True
        assert data["fields_changed"] == 10

    def test_backup_created(self, tmp_path):
        path = _write(tmp_path, ROTATION_FIXTURE)
        original = path.read_bytes()

        rc = tidy_main([str(path), "--backup"])
        assert rc == 0

        backups = list(tmp_path.glob("*.backup-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    def test_refs_cli(self, tmp_path, capsys):
        path = _write(tmp_path, ROTATION_FIXTURE)
        rc = tidy_main([str(path), "--refs", "R1,R3", "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert {s["reference"] for s in data["symbols"]} == {"R1", "R3"}

    def test_threshold_cli(self, tmp_path, capsys):
        path = _write(tmp_path, ROTATION_FIXTURE)
        rc = tidy_main([str(path), "--threshold", "100", "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fields_changed"] == 0
        assert data["modified"] is False

    def test_missing_file_errors(self, tmp_path, capsys):
        rc = tidy_main([str(tmp_path / "missing.kicad_sch")])
        assert rc == 1
        assert "not found" in capsys.readouterr().err.lower()

    def test_kct_dispatch(self, tmp_path, capsys):
        """The `kct sch tidy` dispatch path reaches the command."""
        from kicad_tools.cli import main as kct_main

        path = _write(tmp_path, ROTATION_FIXTURE)
        rc = kct_main(["sch", "tidy", str(path), "--dry-run", "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fields_changed"] == 10
