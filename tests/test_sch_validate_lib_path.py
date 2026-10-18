"""``--lib-path`` fallback and ``kct sch validate`` flag forwarding (Issue #7065)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kicad_tools.cli import main
from kicad_tools.cli.sch_validate import (
    _resolve_lib_symbols_for_design,
    build_lib_manager,
    check_lib_symbols_mismatch,
    check_symbol_footprint_pin_mismatch,
    validate_schematic,
)
from tests._sch_sexp_builders import make_lib_symbol, make_symbol_instance

LIB_ID = "MyLib:MyIC"
PINS = [(str(i), f"P{i}", "passive") for i in range(1, 6)]


def _fixture(tmp_path: Path, *, embed: bool) -> tuple[str, str]:
    """Schematic with U1 (MyLib:MyIC, 5 pins) on a 3-pad footprint + the lib dir."""
    libdir = tmp_path / "libs"
    libdir.mkdir()
    short = make_lib_symbol("MyIC", PINS)
    (libdir / "MyLib.kicad_sym").write_text(
        f'(kicad_symbol_lib (version 20231120) (generator "t")\n{short}\n)\n'
    )
    inst = make_symbol_instance("U1", LIB_ID, PINS, footprint="Package_TO_SOT_SMD:SOT-23-3")
    embedded = make_lib_symbol(LIB_ID, PINS) if embed else ""
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(
        f"""(kicad_sch (version 20231120) (generator "t") (uuid "u") (paper "A4")
    (lib_symbols {embedded})
    {inst}
    (sheet_instances (path "/" (page "1")))
)"""
    )
    return str(sch), str(libdir)


def test_resolver_needs_lib_path(tmp_path):
    sch, libdir = _fixture(tmp_path, embed=False)
    _, without = _resolve_lib_symbols_for_design(sch)
    assert LIB_ID not in without
    _, with_lib = _resolve_lib_symbols_for_design(sch, build_lib_manager([libdir]))
    assert LIB_ID in with_lib
    assert len(with_lib[LIB_ID].pins) == 5


def test_pin_footprint_mismatch_needs_lib_path(tmp_path):
    sch, libdir = _fixture(tmp_path, embed=False)
    assert check_symbol_footprint_pin_mismatch(sch) == []
    issues = check_symbol_footprint_pin_mismatch(sch, build_lib_manager([libdir]))
    assert len(issues) == 1
    assert "5 pins" in issues[0].message


def test_embedded_stays_authoritative(tmp_path):
    sch, libdir = _fixture(tmp_path, embed=True)
    assert len(check_symbol_footprint_pin_mismatch(sch)) == 1
    assert check_lib_symbols_mismatch(sch, build_lib_manager([libdir])) == []


def test_lib_symbols_mismatch_finding_kept_with_hint(tmp_path):
    sch, libdir = _fixture(tmp_path, embed=False)
    plain = check_lib_symbols_mismatch(sch)
    assert len(plain) == 1 and "resolvable" not in plain[0].message
    hinted = check_lib_symbols_mismatch(sch, build_lib_manager([libdir]))
    assert len(hinted) == 1
    assert hinted[0].severity == plain[0].severity
    assert "(resolvable from --lib-path)" in hinted[0].message


def test_validate_schematic_threads_lib_paths(tmp_path):
    sch, libdir = _fixture(tmp_path, embed=False)
    skip = {"erc"}
    base = validate_schematic(sch, skip_checks=skip)
    with_lib = validate_schematic(sch, [libdir], skip_checks=skip)
    assert not [i for i in base.issues if i.category == "pin_footprint_mismatch"]
    assert [i for i in with_lib.issues if i.category == "pin_footprint_mismatch"]


def test_missing_lib_dir_warns_not_crashes(tmp_path, capsys):
    sch, _ = _fixture(tmp_path, embed=False)
    result = validate_schematic(sch, [str(tmp_path / "nope")], skip_checks={"erc"})
    assert result.checks_run
    assert "not a directory" in capsys.readouterr().err


def test_kct_forwards_lib_path_and_skip(tmp_path):
    sch, libdir = _fixture(tmp_path, embed=False)
    with patch("kicad_tools.cli.sch_validate.main", return_value=0) as m:
        rc = main(["sch", "validate", sch, "--lib-path", libdir, "--skip", "erc", "--skip", "x"])
    assert rc == 0
    argv = m.call_args[0][0]
    assert argv.count("--lib-path") == 1 and libdir in argv
    assert argv.count("--skip") == 2 and "erc" in argv and "x" in argv
