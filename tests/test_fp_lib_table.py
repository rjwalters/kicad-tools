"""Unit tests for the ``fp-lib-table`` parser and var-expansion helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.footprints.fp_lib_table import (
    FpLibEntry,
    expand_kicad_vars,
    find_project_fp_lib_table,
    parse_fp_lib_table,
)

_CANONICAL = """(fp_lib_table
    (version 7)
    (lib (name "MyLib") (type "KiCad") (uri "${KIPRJMOD}/MyLib.pretty") (options "") (descr "Project local"))
    (lib (name "Resistor_SMD") (type "KiCad") (uri "${KICAD8_FOOTPRINT_DIR}/Resistor_SMD.pretty") (options "") (descr "Standard"))
    (lib (name "LegacyLib") (type "Legacy") (uri "${KIPRJMOD}/LegacyLib.mod") (options "") (descr "old"))
    (lib (name "GitHubLib") (type "Github") (uri "https://example.com/Lib.pretty") (options "") (descr "remote"))
)
"""


def test_parse_canonical_table(tmp_path: Path) -> None:
    table = tmp_path / "fp-lib-table"
    table.write_text(_CANONICAL, encoding="utf-8")
    entries = parse_fp_lib_table(table)
    by_name = {e.name: e for e in entries}
    assert set(by_name.keys()) == {"MyLib", "Resistor_SMD", "LegacyLib", "GitHubLib"}
    # KIPRJMOD resolves to the table's parent dir.
    assert by_name["MyLib"].resolved_path == tmp_path.resolve() / "MyLib.pretty"
    assert by_name["MyLib"].type == "KiCad"


def test_kiprjmod_resolves_against_table_parent(tmp_path: Path) -> None:
    sub = tmp_path / "project"
    sub.mkdir()
    table = sub / "fp-lib-table"
    table.write_text(
        '(fp_lib_table (lib (name "X") (type "KiCad") (uri "${KIPRJMOD}/X.pretty")'
        ' (options "") (descr "")))',
        encoding="utf-8",
    )
    entries = parse_fp_lib_table(table)
    assert len(entries) == 1
    assert entries[0].resolved_path == sub.resolve() / "X.pretty"


def test_kicad_env_var_resolves_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD8_FOOTPRINT_DIR", "/opt/kicad/footprints")
    table = tmp_path / "fp-lib-table"
    table.write_text(
        '(fp_lib_table (lib (name "R") (type "KiCad") (uri'
        ' "${KICAD8_FOOTPRINT_DIR}/Resistor_SMD.pretty") (options "") (descr "")))',
        encoding="utf-8",
    )
    entries = parse_fp_lib_table(table)
    assert entries[0].resolved_path == Path("/opt/kicad/footprints/Resistor_SMD.pretty")


def test_unresolved_var_yields_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Remove any inherited value so the lookup definitely fails.
    monkeypatch.delenv("KICAD9_FOOTPRINT_DIR", raising=False)
    table = tmp_path / "fp-lib-table"
    table.write_text(
        '(fp_lib_table (lib (name "R") (type "KiCad") (uri'
        ' "${KICAD9_FOOTPRINT_DIR}/Resistor_SMD.pretty") (options "") (descr "")))',
        encoding="utf-8",
    )
    entries = parse_fp_lib_table(table)
    assert len(entries) == 1
    assert entries[0].resolved_path is None
    # The raw URI is preserved for diagnostics.
    assert "${KICAD9_FOOTPRINT_DIR}" in entries[0].uri


def test_non_kicad_types_keep_resolved_none(tmp_path: Path) -> None:
    table = tmp_path / "fp-lib-table"
    table.write_text(_CANONICAL, encoding="utf-8")
    entries = parse_fp_lib_table(table)
    by_name = {e.name: e for e in entries}
    assert by_name["LegacyLib"].type == "Legacy"
    assert by_name["LegacyLib"].resolved_path is None
    assert by_name["GitHubLib"].type == "Github"
    assert by_name["GitHubLib"].resolved_path is None


def test_parse_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_fp_lib_table(tmp_path / "no-such-file") == []


def test_parse_malformed_file_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "fp-lib-table"
    bad.write_text("not an s-expression at all", encoding="utf-8")
    # Should not raise.
    parse_fp_lib_table(bad)


def test_find_project_fp_lib_table_in_project_root(tmp_path: Path) -> None:
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table)", encoding="utf-8")
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    found = find_project_fp_lib_table(sch)
    assert found == table


def test_find_project_fp_lib_table_for_subsheet(tmp_path: Path) -> None:
    """KIPRJMOD always resolves to the project root, even from a sub-sheet."""
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    table = tmp_path / "fp-lib-table"
    table.write_text("(fp_lib_table)", encoding="utf-8")
    sub = tmp_path / "sheets"
    sub.mkdir()
    sub_sheet = sub / "page2.kicad_sch"
    sub_sheet.write_text("(kicad_sch)", encoding="utf-8")
    found = find_project_fp_lib_table(sub_sheet)
    assert found == table


def test_find_project_fp_lib_table_missing(tmp_path: Path) -> None:
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    # No fp-lib-table sibling.
    sch = tmp_path / "proj.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    assert find_project_fp_lib_table(sch) is None


def test_expand_kicad_vars_with_file_scheme(tmp_path: Path) -> None:
    path = expand_kicad_vars("file://${KIPRJMOD}/Lib.pretty", kiprjmod=tmp_path)
    assert path == tmp_path / "Lib.pretty"


def test_expand_kicad_vars_pure_literal() -> None:
    # No variables: passes through untouched.
    assert expand_kicad_vars("/abs/path/Lib.pretty", kiprjmod=None) == Path("/abs/path/Lib.pretty")


def test_expand_kicad_vars_missing_kiprjmod_when_required() -> None:
    assert expand_kicad_vars("${KIPRJMOD}/X.pretty", kiprjmod=None) is None


def test_fp_lib_entry_dataclass_roundtrip() -> None:
    entry = FpLibEntry(name="X", type="KiCad", uri="${KIPRJMOD}/X.pretty", resolved_path=Path("/x"))
    assert entry.name == "X"
    assert entry.resolved_path == Path("/x")
