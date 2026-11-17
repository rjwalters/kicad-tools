"""The test session's per-board fp-lib-table subset (Issue #5240).

``tests/kicad_fp_table_subset.py`` points each ``kicad-cli pcb drc`` launch at
a config home whose ``fp-lib-table`` keeps only the libraries the board names.
These tests pin the parts that make that safe: what it rewrites, what it
leaves alone, and -- on a real kicad-cli with the pinned tables -- that DRC
reports the same findings either way.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import kicad_fp_table_subset as subset

REPO = Path(__file__).resolve().parent.parent
BOARD01 = REPO / "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb"

TABLE = """(fp_lib_table
\t(version 7)
\t(lib (name "Resistor_SMD") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/Resistor_SMD.pretty") (options "") (descr "R"))
\t(lib (name "Connector_PinHeader_2.54mm") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/Connector_PinHeader_2.54mm.pretty") (options "") (descr "J"))
\t(lib (name "Audio_Module") (type "KiCad") (uri "${KICAD10_FOOTPRINT_DIR}/Audio_Module.pretty") (options "") (descr "A"))
)
"""


def test_board_nicknames_reads_footprint_lib_ids() -> None:
    text = (
        '(kicad_pcb (footprint "Resistor_SMD:R_0603" (layer "F.Cu"))\n'
        '(footprint "Test:Pad" (layer "F.Cu")) (footprint "NoLibrary" (layer "F.Cu")))'
    )
    assert subset.board_nicknames(text) == frozenset({"Resistor_SMD", "Test"})


def test_legacy_module_boards_are_left_alone() -> None:
    assert subset.board_nicknames("(kicad_pcb (module Resistor_SMD:R_0603 (layer F.Cu)))") is None


def test_filter_keeps_header_and_named_entries_only() -> None:
    out = subset.filter_fp_lib_table(TABLE, frozenset({"Resistor_SMD", "Missing"}))
    assert out is not None
    assert out.startswith("(fp_lib_table\n\t(version 7)\n")
    assert out.rstrip().endswith(")")
    assert '(name "Resistor_SMD")' in out
    assert "Audio_Module" not in out and "Connector_PinHeader" not in out
    # Every kept line is byte-identical to the source line.
    assert set(out.splitlines()) <= set(TABLE.splitlines())


@pytest.mark.parametrize(
    "entry",
    [
        '\t(lib (name "KiCad") (type "Table") (uri "/x/fp-lib-table") (options "") (descr ""))\n',
        '\t(lib (name "Split") (type "KiCad")\n\t\t(uri "/x") (options "") (descr ""))\n',
    ],
    ids=["nested-table", "multi-line-entry"],
)
def test_filter_refuses_tables_it_cannot_rewrite_exactly(entry: str) -> None:
    assert (
        subset.filter_fp_lib_table(
            TABLE.replace("(fp_lib_table\n", "(fp_lib_table\n" + entry), frozenset()
        )
        is None
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["kicad-cli", "pcb", "drc", "--format", "json", "-o", "r.json", "/b/x.kicad_pcb"],
            "/b/x.kicad_pcb",
        ),
        (
            ["/usr/bin/kicad-cli", "pcb", "drc", "/b/x.kicad_pcb", "--output", "/b/y.kicad_pcb"],
            "/b/x.kicad_pcb",
        ),
        (["kicad-cli", "pcb", "export", "gerbers", "/b/x.kicad_pcb"], None),
        (["kicad-cli", "sch", "erc", "/b/x.kicad_sch"], None),
        (["kicad-cli", "version"], None),
        (["docker", "run", "img", "kicad-cli", "pcb", "drc", "/b/x.kicad_pcb"], None),
        (["kicad-cli", "pcb", "drc", "/b/a.kicad_pcb", "/b/b.kicad_pcb"], None),
        ("kicad-cli pcb drc /b/x.kicad_pcb", None),
    ],
)
def test_only_single_board_drc_launches_are_candidates(argv, expected) -> None:
    board = subset._drc_board(argv, None)
    assert (str(board) if board else None) == expected


def test_relative_board_resolves_against_cwd(tmp_path: Path) -> None:
    board = subset._drc_board(["kicad-cli", "pcb", "drc", "x.kicad_pcb"], tmp_path)
    assert board == tmp_path / "x.kicad_pcb"


@pytest.fixture
def fake_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A session config home holding TABLE plus other settings files."""
    home = tmp_path / "home"
    (home / "10.0" / "colors").mkdir(parents=True)
    (home / "10.0" / "fp-lib-table").write_text(TABLE)
    (home / "10.0" / "sym-lib-table").write_text("(sym_lib_table (version 7))\n")
    (home / "10.0" / "kicad_common.json").write_text('{"environment": {"vars": {"X": "1"}}}')
    monkeypatch.setenv(subset.CONFIG_HOME_ENV, str(home))
    monkeypatch.delenv(subset.DISABLE_ENV, raising=False)
    monkeypatch.setattr(subset, "_session_home", [str(home)])
    monkeypatch.setattr(subset, "_cache", {})
    monkeypatch.setattr(subset, "_root", [tmp_path / "subsets"])
    (tmp_path / "subsets").mkdir()
    board = tmp_path / "board.kicad_pcb"
    board.write_text('(kicad_pcb (footprint "Resistor_SMD:R_0603" (layer "F.Cu")))')
    return home


def _argv(tmp_path: Path) -> list[str]:
    return [
        "kicad-cli",
        "pcb",
        "drc",
        "-o",
        str(tmp_path / "r.json"),
        str(tmp_path / "board.kicad_pcb"),
    ]


def test_drc_launch_gets_a_copied_home_with_the_cut_table(
    tmp_path: Path, fake_session: Path
) -> None:
    env = subset.subset_env(_argv(tmp_path), None, None)
    assert env is not None
    home = Path(env[subset.CONFIG_HOME_ENV])
    assert home != fake_session
    table = (home / "10.0" / "fp-lib-table").read_text()
    assert '(name "Resistor_SMD")' in table and "Audio_Module" not in table
    for name in ("sym-lib-table", "kicad_common.json"):
        assert (home / "10.0" / name).read_bytes() == (fake_session / "10.0" / name).read_bytes()
    assert (home / "10.0" / "colors").is_dir()
    # The session's own table is never touched, and the copy is reused.
    assert (fake_session / "10.0" / "fp-lib-table").read_text() == TABLE
    again = subset.subset_env(_argv(tmp_path), None, None)
    assert again is not None and again[subset.CONFIG_HOME_ENV] == str(home)
    # Everything else in the environment is passed through.
    assert {k: v for k, v in env.items() if k != subset.CONFIG_HOME_ENV} == {
        k: v for k, v in os.environ.items() if k != subset.CONFIG_HOME_ENV
    }


def test_a_launch_with_its_own_config_home_is_left_alone(
    tmp_path: Path, fake_session: Path
) -> None:
    own = {**os.environ, subset.CONFIG_HOME_ENV: str(tmp_path / "empty")}
    assert subset.subset_env(_argv(tmp_path), own, None) is None


def test_opt_out_leaves_every_launch_alone(
    tmp_path: Path, fake_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(subset.DISABLE_ENV, "1")
    assert subset.subset_env(_argv(tmp_path), None, None) is None


def test_unreadable_board_is_left_alone(tmp_path: Path, fake_session: Path) -> None:
    (tmp_path / "board.kicad_pcb").unlink()
    assert subset.subset_env(_argv(tmp_path), None, None) is None


def _drc_findings(report: Path) -> list[str]:
    """Report content minus run metadata and KiCad-generated UUIDs."""

    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items() if k != "uuid"}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    data = json.loads(report.read_text())
    return sorted(
        json.dumps(scrub(item), sort_keys=True)
        for key in ("violations", "unconnected_items", "schematic_parity")
        for item in data.get(key, [])
    )


def test_real_drc_reports_the_same_findings_with_the_cut_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the session's real kicad-cli and tables, the cut table changes nothing.

    The board mixes stock footprints (their libraries are kept, so the
    footprint-vs-library comparison still runs) with a footprint from a
    library no table has (``lib_footprint_issues`` either way).
    """
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    text = BOARD01.read_text()
    extra = (
        '\t(footprint "NotInAnyTable:Pad" (layer "F.Cu") (at 5 5)\n'
        '\t\t(property "Reference" "X1" (at 0 -1 0) (layer "F.SilkS"))\n'
        '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n\t)\n'
    )
    text = text.rstrip()[:-1] + extra + ")\n"
    boards = {}
    for arm in ("full", "cut"):
        directory = tmp_path / arm
        directory.mkdir()
        boards[arm] = directory / BOARD01.name
        boards[arm].write_text(text)
        shutil.copy(BOARD01.with_suffix(".kicad_dru"), directory)
    argv = [str(cli), "pcb", "drc", "--format", "json", "--severity-all", "-o"]
    if subset.subset_env([*argv, "r.json", str(boards["cut"])], None, None) is None:
        pytest.skip("this session's config home has no flat fp-lib-table to cut")

    monkeypatch.setenv(subset.DISABLE_ENV, "1")
    subprocess.run(
        [*argv, str(tmp_path / "full.json"), str(boards["full"])], capture_output=True, timeout=300
    )
    monkeypatch.delenv(subset.DISABLE_ENV)
    subprocess.run(
        [*argv, str(tmp_path / "cut.json"), str(boards["cut"])], capture_output=True, timeout=300
    )

    full = _drc_findings(tmp_path / "full.json")
    assert any('"lib_footprint_issues"' in f and "NotInAnyTable" in f for f in full), full
    assert _drc_findings(tmp_path / "cut.json") == full
