"""One shared power-symbol predicate across BOM, footprint gate, parser, annotation (#7637).

Variants are built from ``tests/fixtures/sheet_qualified_lvs/root_sq.kicad_sch``
with the same ``_variant`` builder as ``test_bom_virtual_parts`` (#7615).
"""

from __future__ import annotations

import pytest

from kicad_tools.cli.sch_fix_annotation import _extract_all_symbols, _is_power_symbol
from kicad_tools.cli.sch_footprint_common import iter_missing_footprint_symbols
from kicad_tools.schema.bom import extract_bom
from kicad_tools.schematic.models.elements import PowerSymbol
from kicad_tools.schematic.models.schematic import Schematic as ParserSchematic
from kicad_tools.sexp import parse_string
from kicad_tools.sexp.power import is_power_lib_symbol, is_power_symbol, lookup_from_nodes
from tests.test_bom_virtual_parts import REAL_REFS, _variant

PROJECT_VARIANTS = {
    "projlib-power": {"lib_id": "myproj:GND", "ref_prefix": "#PWR"},
    "projlib-flag": {"lib_id": "myproj:PWR_FLAG", "ref_prefix": "#FLG"},
}


def _missing_footprint_refs(path) -> set[str]:
    return {
        sym.reference
        for _node, sym, _sch in iter_missing_footprint_symbols(path, include_assigned=True)
    }


@pytest.mark.parametrize("variant", sorted(PROJECT_VARIANTS))
def test_project_library_power_not_flagged_missing_footprint(tmp_path, variant):
    path = _variant(tmp_path, **PROJECT_VARIANTS[variant])
    refs = _missing_footprint_refs(path)
    assert not {r for r in refs if r.startswith("#")}
    assert set(REAL_REFS) <= refs


def test_flag_without_hash_reference_uses_library_flag(tmp_path):
    """Only the ``(power)`` flag identifies it: no stock prefix, no ``#`` ref."""
    path = _variant(tmp_path, lib_id="myproj:PWR_FLAG", ref_prefix="FLG")
    assert not {r for r in _missing_footprint_refs(path) if r.startswith("FLG")}


def test_non_power_project_symbol_still_reported(tmp_path):
    path = _variant(tmp_path, lib_id="myproj:Thing", ref_prefix="X", power_token=None)
    assert {"X01", "X02"} <= _missing_footprint_refs(path)


@pytest.mark.parametrize("variant", sorted(PROJECT_VARIANTS))
def test_parser_routes_project_library_power_to_power_symbols(tmp_path, variant):
    path = _variant(tmp_path, **PROJECT_VARIANTS[variant])
    sch = ParserSchematic.load(path)
    assert len(sch.power_symbols) == 2
    assert {s.reference for s in sch.symbols} == set(REAL_REFS)


def test_parser_flag_with_plain_reference_routes_by_library_flag(tmp_path):
    """No ``#`` reference and no stock prefix: only the ``(power)`` flag decides."""
    path = _variant(tmp_path, lib_id="myproj:PWR_FLAG", ref_prefix="FLG")
    sch = ParserSchematic.load(path)
    assert len(sch.power_symbols) == 2


def test_parser_without_library_flag_or_prefix_is_not_power(tmp_path):
    path = _variant(tmp_path, lib_id="myproj:PWR_FLAG", ref_prefix="FLG")
    doc = parse_string(path.read_text())
    lookup = lookup_from_nodes(doc.get("lib_symbols").find_all("symbol"))
    placed = [c for c in doc.children if c.name == "symbol" and c.get("lib_id")]
    flagged = [c for c in placed if "myproj:PWR_FLAG" in str(c.get("lib_id").get_first_atom())]
    assert flagged
    assert not any(PowerSymbol.is_power_symbol(c) for c in flagged)
    assert all(
        PowerSymbol.is_power_symbol(c, {"myproj:PWR_FLAG": lookup("myproj:PWR_FLAG")})
        for c in flagged
    )


@pytest.mark.parametrize("variant", sorted(PROJECT_VARIANTS))
def test_fix_annotation_recognises_project_library_power(tmp_path, variant):
    path = _variant(tmp_path, **PROJECT_VARIANTS[variant])
    syms = _extract_all_symbols(path.read_text(), "root_sq")
    power = [s for s in syms if _is_power_symbol(s)]
    assert {s["reference"][:4] for s in power} == {PROJECT_VARIANTS[variant]["ref_prefix"]}
    assert len(power) == 2
    assert not [s for s in syms if s["reference"] in REAL_REFS and _is_power_symbol(s)]


def test_fix_annotation_stock_power_unchanged(tmp_path):
    syms = _extract_all_symbols(_variant(tmp_path).read_text(), "root_sq")
    assert sum(_is_power_symbol(s) for s in syms) == 2
    assert _is_power_symbol({"lib_id": "power:GND"})
    assert not _is_power_symbol({"lib_id": "Device:R"})


def test_bom_agrees_with_shared_predicate(tmp_path):
    path = _variant(tmp_path, **PROJECT_VARIANTS["projlib-flag"])
    items = extract_bom(path).items
    assert {i.reference for i in items if not i.is_virtual} == set(REAL_REFS)


def test_extends_chain_and_missing_entry():
    doc = parse_string(
        '(lib_symbols (symbol "p:Base" (power)) (symbol "p:Derived" (extends "Base")) '
        '(symbol "p:Plain"))'
    )
    lookup = lookup_from_nodes(doc.find_all("symbol"))
    assert is_power_lib_symbol(lookup, "p:Derived")
    assert not is_power_lib_symbol(lookup, "p:Plain")
    assert not is_power_lib_symbol(lookup, "p:Missing")
    # Missing entry: ``#`` reference fallback; ordinary refs stay real parts.
    assert is_power_symbol("p:Missing", "#FLG01", lookup=lookup)
    assert not is_power_symbol("p:Missing", "R1", lookup=lookup)
    assert is_power_symbol("power:GND", "")


# --- PWR_FLAG from a project library keeps its rails distinct (#7637) ------

FLAG_LIB_IDS = ("power:PWR_FLAG", "myproj:PWR_FLAG")


def _flag_variant(tmp_path, lib_id: str):
    """Both placed power symbols become ``lib_id`` flags whose Value is
    ``PWR_FLAG``, exactly as KiCad writes a placed flag. One sits on R10.2,
    the other on R12.2: two different rails."""
    path = _variant(tmp_path, lib_id=lib_id, ref_prefix="#FLG")
    text = path.read_text()
    assert text.count('(property "Value" "GND"') == 2
    path.write_text(text.replace('(property "Value" "GND"', '(property "Value" "PWR_FLAG"'))
    return path


@pytest.mark.parametrize("lib_id", FLAG_LIB_IDS)
def test_power_flag_any_library_has_no_net_name(tmp_path, lib_id):
    sch = ParserSchematic.load(_flag_variant(tmp_path, lib_id))
    assert len(sch.power_symbols) == 2
    for pwr in sch.power_symbols:
        assert pwr.value_text == "PWR_FLAG"
        assert pwr.is_power_flag
        assert pwr.net_name is None


@pytest.mark.parametrize("lib_id", FLAG_LIB_IDS)
def test_power_flag_any_library_does_not_merge_rails(tmp_path, lib_id):
    """A project-library flag must not become a global ``PWR_FLAG`` net that
    merges every flagged rail (the regression the shared predicate exposed)."""
    nets = ParserSchematic.load(_flag_variant(tmp_path, lib_id)).extract_netlist()
    assert "PWR_FLAG" not in nets
    owner = {str(pin): name for name, pins in nets.items() for pin in pins}
    assert owner.get("R10.2") is not None
    assert owner.get("R12.2") is not None
    assert owner["R10.2"] != owner["R12.2"]


def test_is_power_flag_matches_symbol_name_only():
    def flag(lib_id: str) -> bool:
        return PowerSymbol(lib_id=lib_id, x=0, y=0).is_power_flag

    assert flag("power:PWR_FLAG")
    assert flag("PWR_FLAG")
    assert flag("myproj:PWR_FLAG")
    assert not flag("power:GND")
    assert not flag("myproj:PWR_FLAG_ALT")


# --- ``#`` reference only decides when no library entry exists -------------


def test_hash_reference_with_non_power_library_entry_is_not_power(tmp_path):
    """A ``#``-referenced graphic symbol (``#LOGO1``) whose embedded entry
    has no ``(power)`` flag stays an ordinary symbol in the parser."""
    path = _variant(tmp_path, lib_id="Graphic:Logo", ref_prefix="#LOGO", power_token=None)
    sch = ParserSchematic.load(path)
    assert sch.power_symbols == []
    assert {"#LOGO01", "#LOGO02"} <= {s.reference for s in sch.symbols}


def test_hash_reference_without_library_entry_is_power(tmp_path):
    path = _variant(tmp_path, lib_id="myproj:GND", ref_prefix="#PWR", lib_entry="drop")
    assert len(ParserSchematic.load(path).power_symbols) == 2


def test_hash_rule_needs_missing_entry():
    doc = parse_string('(lib_symbols (symbol "Graphic:Logo"))')
    lookup = lookup_from_nodes(doc.find_all("symbol"))
    assert not is_power_symbol("Graphic:Logo", "#LOGO1", lookup=lookup)
    assert is_power_symbol("Graphic:Missing", "#LOGO1", lookup=lookup)
    # No lookup: presence is unknown, the ``#`` convention still applies.
    assert is_power_symbol("Graphic:Logo", "#LOGO1")
