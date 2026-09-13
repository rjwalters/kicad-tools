"""Device, terminal and hierarchy identity must not manufacture a stress PASS."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.analysis.component_stress import (
    ComponentStressAnalyzer,
    OperatingStateManifest,
    PinRoleCache,
    resolve_pin_roles,
)
from kicad_tools.operations.netlist import build_netlist_from_schematic
from kicad_tools.schema.schematic import Schematic

FIXTURES = Path(__file__).parent / "fixtures" / "component_stress"
SCH = FIXTURES / "mosfets.kicad_sch"


def _analyze(path):
    return ComponentStressAnalyzer(OperatingStateManifest.load(FIXTURES / "states.yaml")).analyze(
        path
    )


def _symbol():
    sch = Schematic.load(SCH)
    sym = next(s for s in sch.symbols if s.reference == "Q1A")
    return sym, sch.get_lib_symbol_resolved(sym.lib_id)


@pytest.mark.parametrize(
    "fields",
    [
        {"Pin_D": "3", "Pin_G": "3", "Pin_S": "3"},
        {"Pin_D": "3"},
        {"Pin_D": "5", "Pin_G": "4", "Pin_S": "6"},
        {"Pin_D": "", "Pin_G": "", "Pin_S": ""},
        {"Pin_D": "2", "Pin_Drain": "3", "Pin_G": "1", "Pin_S": "3"},
        {"Pin_D": "2", "pin_d": "3", "Pin_G": "1", "Pin_S": "3"},
    ],
)
def test_invalid_explicit_roles_do_not_fall_back(tmp_path, fields):
    text = SCH.read_text()
    properties = " ".join(f'(property "{name}" "{value}")' for name, value in fields.items())
    text = text.replace(
        '(property "MPN" "IRFB4110PBF"', properties + '\n(property "MPN" "IRFB4110PBF"'
    )
    path = tmp_path / "invalid_roles.kicad_sch"
    path.write_text(text)
    rows = [r for r in _analyze(path) if r.reference == "Q1A"]
    assert len(rows) == 14
    assert all(r.status == "UNRESOLVED" and "pin roles" in r.reason for r in rows)


def test_valid_explicit_roles_can_override_library_names():
    sym, lib = _symbol()
    for role, number in {"D": "1", "G": "2", "S": "3"}.items():
        sym.properties[f"Pin_{role}"] = SimpleNamespace(value=number)
    assert resolve_pin_roles(sym, lib).roles == {"D": "1", "G": "2", "S": "3"}


def test_changed_library_pinout_invalidates_cache():
    sym, _ = _symbol()
    old = SimpleNamespace(
        pins=[SimpleNamespace(number=str(i), name=n) for i, n in enumerate("GDS", 1)]
    )
    new = SimpleNamespace(
        pins=[SimpleNamespace(number=str(i), name=n) for i, n in enumerate("DGS", 1)]
    )
    cache = PinRoleCache()
    assert cache.resolve(sym, old).roles["D"] == "2"
    assert cache.resolve(sym, new).roles["D"] == "1"
    assert cache.invalidations == 1
    assert cache.hits == 0


def test_new_conflicting_explicit_alias_invalidates_cache():
    sym, lib = _symbol()
    for role, number in {"D": "2", "G": "1", "S": "3"}.items():
        sym.properties[f"Pin_{role}"] = SimpleNamespace(value=number)
    cache = PinRoleCache()
    assert cache.resolve(sym, lib).roles["D"] == "2"
    sym.properties["Pin_Drain"] = SimpleNamespace(value="3")
    assert cache.resolve(sym, lib) is None
    assert cache.invalidations == 1


@pytest.mark.parametrize("names", ["GDD", "DGS"])
def test_ambiguous_or_duplicate_library_pins_cannot_use_suffix(names):
    sym, _ = _symbol()
    numbers = ["1", "2", "3"] if names == "GDD" else ["1", "1", "3"]
    lib = SimpleNamespace(
        pins=[SimpleNamespace(number=i, name=n) for i, n in zip(numbers, names, strict=True)]
    )
    assert resolve_pin_roles(sym, lib) is None


def test_missing_mpn_is_unresolved_even_with_sourced_sufficient_rating(tmp_path):
    path = tmp_path / "missing_mpn.kicad_sch"
    path.write_text(
        SCH.read_text()
        .replace('"MPN" "IRFB4110PBF"', '"Other" "IRFB4110PBF"')
        .replace('"Vds_max" "100V"', '"Vds_max" "600V"')
    )
    rows = [r for r in _analyze(path) if r.reference == "Q1A"]
    assert len(rows) == 14
    assert all(r.status == "UNRESOLVED" and "MPN" in r.reason for r in rows)


@pytest.mark.parametrize("lib_id", ["Transistor_FET:IRFB4110", "Vendor:Custom4110"])
def test_vendor_mosfet_without_rating_fields_remains_in_census(tmp_path, lib_id):
    path = tmp_path / "vendor.kicad_sch"
    path.write_text(
        SCH.read_text()
        .replace("Device:Q_NMOS_GDS", lib_id)
        .replace('"Vds_max"', '"UnusedVds"')
        .replace('"Vgs_max"', '"UnusedVgs"')
    )
    rows = [r for r in _analyze(path) if r.reference == "Q1A"]
    assert len(rows) == 14
    assert all(r.status == "UNRESOLVED" for r in rows)


def _root(tmp_path, filenames):
    path = tmp_path / "root.kicad_sch"
    sheets = " ".join(
        f'(sheet (at 10 10) (size 20 20) (uuid "22222222-2222-2222-2222-{i:012d}") (property "Sheetname" "Child{i}" (at 10 10 0)) (property "Sheetfile" "{name}" (at 10 10 0)))'
        for i, name in enumerate(filenames)
    )
    path.write_text(
        f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "33333333-3333-3333-3333-333333333333") (paper "A4") (lib_symbols) {sheets})'
    )
    return path


def test_child_sheet_mosfets_have_same_stress_as_flat_schematic(tmp_path):
    (tmp_path / "child.kicad_sch").write_text(SCH.read_text())
    root = _root(tmp_path, ["child.kicad_sch"])
    assert {c.sheet_path for c in build_netlist_from_schematic(root).components} == {
        "/child.kicad_sch/"
    }
    nested = _analyze(root)
    flat = _analyze(SCH)
    assert [r.to_dict() for r in nested] == [r.to_dict() for r in flat]
    q1 = next(
        r for r in nested if (r.reference, r.state, r.check) == ("Q1A", "mains_negative", "vds")
    )
    assert q1.status == "FAIL"
    assert q1.stress_v == pytest.approx(259.3)


@pytest.mark.parametrize(
    "filenames",
    [
        ["missing.kicad_sch"],
        ["child.kicad_sch", "child.kicad_sch"],
        ["child.kicad_sch", "other.kicad_sch"],
    ],
)
def test_incomplete_or_ambiguous_hierarchy_is_unresolved(tmp_path, filenames):
    for name in ("child.kicad_sch", "other.kicad_sch"):
        (tmp_path / name).write_text(SCH.read_text())
    rows = _analyze(_root(tmp_path, filenames))
    assert rows
    assert all(r.status == "UNRESOLVED" for r in rows)
    assert all(r.check == "analysis" for r in rows)


@pytest.mark.parametrize("global_labels", [False, True])
def test_sibling_net_names_require_proven_global_binding(tmp_path, global_labels):
    text = SCH.read_text()
    if global_labels:
        text = text.replace("(label ", "(global_label ")
    (tmp_path / "child.kicad_sch").write_text(text)
    other = text
    for old, new in zip(range(1, 5), range(5, 9), strict=True):
        other = other.replace(f"Q{old}A", f"Q{new}A")
    (tmp_path / "other.kicad_sch").write_text(other)
    root = _root(tmp_path, ["child.kicad_sch", "other.kicad_sch"])
    rows = _analyze(root)
    if not global_labels:
        assert rows and all(r.status == "UNRESOLVED" and r.check == "analysis" for r in rows)
        assert "sheet-local label" in rows[0].reason
    else:
        assert len(rows) == 112
        for ref in ("Q1A", "Q5A"):
            row = next(
                r for r in rows if (r.reference, r.state, r.check) == (ref, "mains_negative", "vds")
            )
            assert row.status == "FAIL"
            assert row.stress_v == pytest.approx(259.3)


def test_conflicting_pin_net_bindings_are_rejected():
    nets = [
        SimpleNamespace(name=name, nodes=[SimpleNamespace(reference="Q1", pin="1")])
        for name in ("SAFE", "UNSAFE")
    ]
    with pytest.raises(ValueError, match="multiple nets"):
        ComponentStressAnalyzer._pin_net_map(SimpleNamespace(nets=nets), "Q1")


def test_conflicting_pin_net_binding_is_visible_unresolved(monkeypatch):
    netlist = build_netlist_from_schematic(SCH)
    netlist.nets.append(
        SimpleNamespace(name="CONFLICT", nodes=[SimpleNamespace(reference="Q1A", pin="2")])
    )
    monkeypatch.setattr(
        "kicad_tools.analysis.component_stress.build_netlist_from_schematic", lambda path: netlist
    )
    rows = [r for r in _analyze(SCH) if r.reference == "Q1A"]
    assert len(rows) == 14
    assert all(r.status == "UNRESOLVED" and "multiple nets" in r.reason for r in rows)


def test_distinct_net_names_cannot_share_a_tolerant_manifest_alias(monkeypatch):
    netlist = build_netlist_from_schematic(SCH)
    netlist.nets.append(
        SimpleNamespace(name="bank_pos", nodes=[SimpleNamespace(reference="Q1A", pin="9")])
    )
    monkeypatch.setattr(
        "kicad_tools.analysis.component_stress.build_netlist_from_schematic", lambda path: netlist
    )
    rows = _analyze(SCH)
    assert rows and all(r.status == "UNRESOLVED" for r in rows)
    assert "ambiguous operating-state aliases" in rows[0].reason


@pytest.mark.parametrize(
    "alias,value,status",
    [
        ("VDSS", "100V", "UNRESOLVED"),
        ("vDS_MAX", "100V", "UNRESOLVED"),
        ("VDSS", "invalid", "UNRESOLVED"),
        ("VDSS", "600", "PASS"),
        ("VDSS", "0.6kV", "PASS"),
    ],
)
def test_all_rating_aliases_must_agree(tmp_path, alias, value, status):
    text = SCH.read_text().replace('"Vds_max" "100V"', '"Vds_max" "600V"')
    text = text.replace(
        '(property "MPN" "IRFB4110PBF"',
        f'(property "{alias}" "{value}")\n(property "MPN" "IRFB4110PBF"',
    )
    path = tmp_path / "rating_alias.kicad_sch"
    path.write_text(text)
    row = next(
        r
        for r in _analyze(path)
        if (r.reference, r.state, r.check) == ("Q1A", "mains_negative", "vds")
    )
    assert row.status == status
    assert row.stress_v == pytest.approx(259.3)


@pytest.mark.parametrize(
    "alias,value,remove_mpn,status",
    [
        ("Manufacturer Part Number", "OTHER", False, "UNRESOLVED"),
        ("mpn", "OTHER", False, "UNRESOLVED"),
        ("Manufacturer Part Number", "IRFB4110PBF", False, "FAIL"),
        ("LCSC", "C12345", False, "FAIL"),
        ("LCSC", "C12345", True, "UNRESOLVED"),
    ],
)
def test_exact_mpn_aliases_and_supplier_codes(tmp_path, alias, value, remove_mpn, status):
    text = SCH.read_text().replace(
        '(property "MPN" "IRFB4110PBF"',
        f'(property "{alias}" "{value}")\n(property "MPN" "IRFB4110PBF"',
    )
    if remove_mpn:
        text = text.replace('"MPN" "IRFB4110PBF"', '"Other" "IRFB4110PBF"')
    path = tmp_path / "mpn_alias.kicad_sch"
    path.write_text(text)
    row = next(
        r
        for r in _analyze(path)
        if (r.reference, r.state, r.check) == ("Q1A", "mains_negative", "vds")
    )
    assert row.status == status
    if status == "UNRESOLVED":
        assert "MPN" in row.reason
