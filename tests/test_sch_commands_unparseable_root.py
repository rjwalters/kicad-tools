"""Schematic commands must fail on an unparseable root schematic (#7028).

Sibling audit of ``kct bom``: ``sch summary``, ``sch validate``,
``sch preflight`` and ``sch hierarchy`` used to exit 0 on an empty root file
(the failure was swallowed into a fallback summary or per-check warnings).
"""

import pytest

from kicad_tools.cli.sch_hierarchy import main as hierarchy_main
from kicad_tools.cli.sch_preflight import main as preflight_main
from kicad_tools.cli.sch_summary import run_summary
from kicad_tools.cli.sch_validate import main as validate_main
from kicad_tools.schema.hierarchy import build_hierarchy


@pytest.fixture
def empty_root(tmp_path):
    path = tmp_path / "empty.kicad_sch"
    path.write_text("")
    return path


def test_build_hierarchy_raises_on_unparseable_root(empty_root):
    with pytest.raises(ValueError):
        build_hierarchy(str(empty_root), strict_root=True)


def test_build_hierarchy_raises_on_missing_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_hierarchy(str(tmp_path / "nope.kicad_sch"), strict_root=True)


def test_summary_exits_nonzero(empty_root, capsys):
    rc = run_summary(empty_root)
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "Error" in captured.err


@pytest.mark.parametrize("main", [validate_main, preflight_main])
def test_validate_and_preflight_exit_nonzero(main, empty_root, capsys):
    with pytest.raises(SystemExit) as exc:
        main([str(empty_root)])
    captured = capsys.readouterr()
    assert exc.value.code != 0
    assert str(empty_root) in captured.err


def test_hierarchy_exits_nonzero(empty_root, capsys):
    with pytest.raises(SystemExit) as exc:
        hierarchy_main([str(empty_root), "tree"])
    assert exc.value.code != 0
    assert "Unexpected end of input" in capsys.readouterr().err
