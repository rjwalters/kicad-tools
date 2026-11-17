"""BOM output drops virtual symbols in every format (issue #7615).

KiCad marks a power symbol with the ``(power)`` flag on its library
symbol, not with its library name. Project-library power symbols and
PWR_FLAGs (``myproj:GND``, ``myproj:PWR_FLAG``) must therefore be left out
of the BOM just like stock ``power:`` symbols, whatever their ``in_bom``
says, and ``kct bom`` must drop them in ``table``, ``csv``, ``json`` and
``jlcpcb`` alike.

Every variant is derived from the repo's own fixture
``tests/fixtures/sheet_qualified_lvs/root_sq.kicad_sch`` (resistors
``R10``-``R12`` plus two ``power:GND`` placements, ``#PWR01``/``#PWR02``)
by rewriting only the power symbol's library name, ``(power)`` token,
references and ``in_bom``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli import bom_cmd
from kicad_tools.schema.bom import extract_bom

FIXTURE = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs" / "root_sq.kicad_sch"

REAL_REFS = ("R10", "R11", "R12")
_REF_RE = re.compile(r"^#?[A-Z]+[0-9]+$")
FORMATS = ("table", "csv", "json", "jlcpcb")

# The fixture's embedded power:GND definition, verbatim (header lines only).
_LIB_HEADER = '\t\t(symbol "power:GND"\n\t\t\t(power)\n'


def _variant(
    tmp_path: Path,
    *,
    lib_id: str = "power:GND",
    ref_prefix: str = "#PWR",
    in_bom: bool = False,
    power_token: str | None = "(power)",
    lib_entry: str = "keep",
    name: str = "variant",
) -> Path:
    """Write a rewritten copy of the fixture and return its path.

    ``lib_entry``: ``"keep"`` renames the embedded definition to *lib_id*;
    ``"drop"`` renames it to an unrelated key so the placed symbols have
    no embedded definition; ``"extends"`` keeps the power definition as a
    base and adds a ``(extends ...)`` derived entry named *lib_id*.
    """
    text = FIXTURE.read_text()
    assert _LIB_HEADER in text, "fixture layout changed"

    power_line = f"\t\t\t{power_token}\n" if power_token else ""
    if lib_entry == "keep":
        header = f'\t\t(symbol "{lib_id}"\n{power_line}'
    elif lib_entry == "drop":
        header = f'\t\t(symbol "unused:Other"\n{power_line}'
    elif lib_entry == "extends":
        lib, _, base_name = lib_id.partition(":")
        base = f"{lib}:{base_name}_Base"
        derived = (
            f'\t\t(symbol "{lib_id}"\n'
            f'\t\t\t(extends "{base_name}_Base")\n'
            '\t\t\t(property "Reference" "#PWR" (at 0 0 0))\n'
            "\t\t)\n"
        )
        header = derived + f'\t\t(symbol "{base}"\n{power_line}'
    else:  # pragma: no cover - test helper misuse
        raise ValueError(lib_entry)
    text = text.replace(_LIB_HEADER, header, 1)

    text = text.replace('(lib_id "power:GND")', f'(lib_id "{lib_id}")')
    text = text.replace('"Reference" "#PWR01"', f'"Reference" "{ref_prefix}01"')
    text = text.replace('"Reference" "#PWR02"', f'"Reference" "{ref_prefix}02"')
    if in_bom:
        # Only the two placed power symbols carry a bare "(in_bom no)" line.
        text = text.replace("\t\t(in_bom no)\n", "\t\t(in_bom yes)\n")

    path = tmp_path / f"{name}.kicad_sch"
    path.write_text(text)
    return path


def _run(capsys, path: Path, fmt: str, *extra: str) -> str:
    capsys.readouterr()
    assert bom_cmd.main([str(path), "--format", fmt, *extra]) == 0
    return capsys.readouterr().out


def _refs_in(out: str, fmt: str) -> set[str]:
    """Every reference designator an output mentions."""
    if fmt == "json":
        data = json.loads(out)
        if "groups" in data:
            return {r for g in data["groups"] for r in g["references"]}
        return {i["reference"] for i in data["items"]}
    tokens = out.replace(",", " ").replace('"', " ").split()
    return {t for t in tokens if _REF_RE.match(t)}


# --------------------------------------------------------------------------
# kct bom: format x in_bom matrix for project-library power symbols / flags
# --------------------------------------------------------------------------

PROJECT_VARIANTS = {
    "projlib-power": {"lib_id": "myproj:GND", "ref_prefix": "#PWR"},
    "projlib-flag": {"lib_id": "myproj:PWR_FLAG", "ref_prefix": "#FLG"},
}


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("group", [False, True], ids=["ungrouped", "grouped"])
@pytest.mark.parametrize("in_bom", [False, True], ids=["in_bom-no", "in_bom-yes"])
@pytest.mark.parametrize("variant", sorted(PROJECT_VARIANTS))
def test_project_library_virtual_symbols_dropped(tmp_path, capsys, variant, in_bom, group, fmt):
    path = _variant(tmp_path, in_bom=in_bom, **PROJECT_VARIANTS[variant])
    extra = ("--group",) if group else ()
    out = _run(capsys, path, fmt, *extra)
    refs = _refs_in(out, fmt)
    assert not {r for r in refs if r.startswith("#")}, out
    assert set(REAL_REFS) <= refs, out


@pytest.mark.parametrize("fmt", FORMATS)
def test_stock_power_control_dropped(tmp_path, capsys, fmt):
    out = _run(capsys, FIXTURE, fmt)
    assert "#PWR" not in out
    assert set(REAL_REFS) <= _refs_in(out, fmt)


@pytest.mark.parametrize("fmt", FORMATS)
def test_kicad_tools_pwr_symbol_dropped(tmp_path, capsys, fmt):
    path = _variant(tmp_path, lib_id="kicad_tools_pwr:VIN", ref_prefix="#PWR")
    out = _run(capsys, path, fmt)
    assert "#PWR" not in out
    assert set(REAL_REFS) <= _refs_in(out, fmt)


@pytest.mark.parametrize("fmt", FORMATS)
def test_missing_lib_symbol_hash_reference_is_virtual(tmp_path, capsys, fmt):
    """No embedded definition: a ``#`` reference alone marks it virtual."""
    path = _variant(
        tmp_path, lib_id="myproj:PWR_FLAG", ref_prefix="#FLG", in_bom=True, lib_entry="drop"
    )
    out = _run(capsys, path, fmt)
    assert "#FLG" not in out


@pytest.mark.parametrize("fmt", FORMATS)
def test_missing_lib_symbol_plain_reference_is_kept(tmp_path, capsys, fmt):
    """No embedded definition and an ordinary reference: a real part."""
    path = _variant(tmp_path, lib_id="myproj:TP", ref_prefix="TP", in_bom=True, lib_entry="drop")
    out = _run(capsys, path, fmt)
    refs = _refs_in(out, fmt)
    assert {"TP01", "TP02"} <= refs, out


# --------------------------------------------------------------------------
# jlcpcb now receives the same filtered list as the other writers
# --------------------------------------------------------------------------


def test_jlcpcb_honours_exclude(tmp_path, capsys):
    out = _run(capsys, FIXTURE, "jlcpcb", "--exclude", "R1*")
    assert not set(REAL_REFS) & _refs_in(out, "jlcpcb"), out


def test_jlcpcb_honours_include_dnp(tmp_path, capsys):
    text = FIXTURE.read_text()
    # Mark R10 DNP: its placement is the first "(in_bom yes)" in the file.
    text = text.replace("\t\t(in_bom yes)\n", "\t\t(in_bom yes)\n\t\t(dnp yes)\n", 1)
    path = tmp_path / "dnp.kicad_sch"
    path.write_text(text)
    assert extract_bom(str(path)).items[0].dnp

    default = _refs_in(_run(capsys, path, "jlcpcb"), "jlcpcb")
    assert "R10" not in default and {"R11", "R12"} <= default

    with_dnp = _refs_in(_run(capsys, path, "jlcpcb", "--include-dnp"), "jlcpcb")
    assert set(REAL_REFS) <= with_dnp


# --------------------------------------------------------------------------
# BOMItem.is_power is read from the library (power) flag
# --------------------------------------------------------------------------


def _power_items(path: Path):
    return [i for i in extract_bom(str(path)).items if i.reference not in REAL_REFS]


@pytest.mark.parametrize("token", ["(power)", "(power local)", "(power global)"])
def test_power_flag_variants_set_is_power(tmp_path, token):
    # A non-'#' reference proves the flag, not the reference fallback, decides.
    path = _variant(tmp_path, lib_id="myproj:GND", ref_prefix="PS", in_bom=True, power_token=token)
    items = _power_items(path)
    assert [i.reference for i in items] == ["PS01", "PS02"]
    assert all(i.is_power and i.is_power_symbol and i.is_virtual for i in items)
    assert [i.reference for i in extract_bom(str(path)).filter().items] == list(REAL_REFS)


def test_extends_power_base_sets_is_power(tmp_path):
    path = _variant(
        tmp_path, lib_id="myproj:GNDA", ref_prefix="PS", in_bom=True, lib_entry="extends"
    )
    items = _power_items(path)
    assert [i.reference for i in items] == ["PS01", "PS02"]
    assert all(i.is_power for i in items)


def test_extends_non_power_base_is_not_power(tmp_path):
    path = _variant(
        tmp_path,
        lib_id="myproj:GNDA",
        ref_prefix="PS",
        in_bom=True,
        power_token=None,
        lib_entry="extends",
    )
    items = _power_items(path)
    assert items and not any(i.is_power or i.is_virtual for i in items)


def test_no_power_flag_is_not_power(tmp_path):
    path = _variant(tmp_path, lib_id="myproj:GND", ref_prefix="PS", in_bom=True, power_token=None)
    items = _power_items(path)
    assert items and not any(i.is_power or i.is_virtual for i in items)


def test_filter_drops_in_bom_yes_flag(tmp_path):
    """The reported case: an ``in_bom yes`` ``#FLG`` survived ``filter()``."""
    path = _variant(tmp_path, lib_id="myproj:PWR_FLAG", ref_prefix="#FLG", in_bom=True)
    assert [i.reference for i in extract_bom(str(path)).filter().items] == list(REAL_REFS)
