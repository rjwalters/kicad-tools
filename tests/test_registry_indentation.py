"""Regression tests for #6218: registry must be indentation-agnostic."""

import pytest

from kicad_tools.schematic.exceptions import LibraryNotFoundError, SymbolNotFoundError
from kicad_tools.schematic.registry import LibraryIndex, SymbolRegistry, scan_toplevel_symbols

BODY = """(symbol "{name}"{extra}
{i}(property "Reference" "C" (at 0 0 0))
{i}(property "Description" "has ) paren in string ( {name}")
{i}(symbol "{name}_0_1"
{i}{i}(rectangle (start -1 -1) (end 1 1))
{i})
{i}(symbol "{name}_1_1"
{i}{i}(pin passive line (at 0 3 270) (length 2) (name "~" (effects)) (number "1" (effects)))
{i}{i}(pin passive line (at 0 -3 90) (length 2) (name "~" (effects)) (number "2" (effects)))
{i})
)"""


def _lib(indent: str) -> str:
    def ind(block: str) -> str:
        return "\n".join(indent + ln if ln else ln for ln in block.split("\n"))

    parts = [
        # An unrelated extends symbol first-ish: must never leak into others.
        ind(BODY.format(name="Filter_EMI_LL", extra="", i=indent)),
        ind(BODY.format(name="C_Small", extra="", i=indent)),
        ind(
            '(symbol "Child"\n'
            f'{indent}(extends "C_Small")\n'
            f'{indent}(property "Reference" "X" (at 0 0 0))\n)'
        ),
        ind(
            '(symbol "Other"\n'
            f'{indent}(extends "Filter_EMI_LL")\n'
            f'{indent}(property "Reference" "Y" (at 0 0 0))\n)'
        ),
    ]
    return "(kicad_symbol_lib (version 20211014)\n" + "\n".join(parts) + "\n)\n"


@pytest.fixture(params=["\t", "  "], ids=["tab-kicad8", "space-kicad7"])
def registry(request, tmp_path):
    (tmp_path / "Device.kicad_sym").write_text(_lib(request.param))
    return SymbolRegistry(lib_paths=[tmp_path])


def test_index_only_top_level(registry):
    idx = registry._get_library_index("Device")
    assert set(idx.symbols) == {"Filter_EMI_LL", "C_Small", "Child", "Other"}


def test_plain_symbol_resolves_exactly(registry):
    sym = registry.get("Device:C_Small")
    assert sym.name == "C_Small"
    assert sym.raw_sexp.startswith('(symbol "C_Small"')
    assert "Filter_EMI_LL" not in sym.raw_sexp
    assert [p.number for p in sym.pins] == ["1", "2"]
    assert sym.description.startswith("has ) paren")


def test_unit_subsymbol_not_confused_with_parent(registry):
    sym = registry.get("Device:C_Small")
    assert sym.raw_sexp.count('(symbol "C_Small_0_1"') == 1
    with pytest.raises(SymbolNotFoundError):
        registry.get("Device:C_Small_0_1")


def test_extends_uses_own_parent(registry):
    child = registry.get("Device:Child")
    assert '(symbol "C_Small"' in child.raw_sexp
    assert "Filter_EMI_LL" not in child.raw_sexp
    assert len(child.pins) == 2
    other = registry.get("Device:Other")
    assert '(symbol "Filter_EMI_LL"' in other.raw_sexp
    assert "C_Small" not in other.raw_sexp


def test_missing_symbol_raises_not_wrong_symbol(registry):
    with pytest.raises(SymbolNotFoundError):
        registry.get("Device:C_Smal")


def test_missing_library_is_library_not_found(tmp_path):
    reg = SymbolRegistry(lib_paths=[tmp_path])
    with pytest.raises(LibraryNotFoundError):
        reg.get("Nope:X")
    with pytest.raises(FileNotFoundError):
        reg.get("Nope:X")


def test_unbalanced_library_fails_loudly(tmp_path):
    f = tmp_path / "Bad.kicad_sym"
    f.write_text('(kicad_symbol_lib\n  (symbol "A"\n    (property "R" "x")\n')
    with pytest.raises(ValueError, match="Cannot parse"):
        LibraryIndex.from_file(f)


def test_scan_ignores_parens_in_strings():
    spans = scan_toplevel_symbols('(lib (symbol "A" (p ")(" )) (symbol "B"))')
    assert set(spans) == {"A", "B"}


def test_fast_and_exact_scanners_agree():
    from kicad_tools.schematic.registry import _scan_toplevel_symbols_exact

    for indent in ("\t", "  "):
        lib = _lib(indent)
        assert scan_toplevel_symbols(lib) == _scan_toplevel_symbols_exact(lib)
    tricky = '(lib (symbol "A" (p "(symbol \\"X\\" (")) (symbol "B" (q ")")))'
    assert scan_toplevel_symbols(tricky) == _scan_toplevel_symbols_exact(tricky)
