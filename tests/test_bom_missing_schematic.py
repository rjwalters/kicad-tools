"""kct bom must fail loudly on a missing or unparseable root schematic (#7028)."""

import pytest

from kicad_tools.cli.bom_cmd import main
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.schema.bom import extract_bom


@pytest.mark.parametrize("fmt", ["table", "csv", "json", "jlcpcb"])
def test_missing_schematic_exits_nonzero(tmp_path, capsys, fmt):
    missing = tmp_path / "m.kicad_sch"
    rc = main([str(missing), "--format", fmt])
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert f"Error: File not found: {missing}" in captured.err


@pytest.mark.parametrize("extra", [["--validate"], ["--group"]])
def test_missing_schematic_with_flags(tmp_path, capsys, extra):
    rc = main([str(tmp_path / "m.kicad_sch"), *extra])
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "File not found" in captured.err


def test_unparseable_root_exits_nonzero(tmp_path, capsys):
    empty = tmp_path / "empty.kicad_sch"
    empty.write_text("")
    rc = main([str(empty)])
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert str(empty) in captured.err


def test_extract_bom_raises_on_missing_root(tmp_path):
    with pytest.raises(KiCadFileNotFoundError):
        extract_bom(str(tmp_path / "nope.kicad_sch"))
