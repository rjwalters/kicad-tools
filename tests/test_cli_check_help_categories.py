"""Drift test: ``--only``/``--skip`` help must list every CHECK_CATEGORIES name (#6896)."""

from __future__ import annotations

import re

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.cli.check_cmd import CHECK_CATEGORIES
from kicad_tools.cli.parser import create_parser

_MARKERS = {"--only": "Runonlyspecificchecks", "--skip": "Skipspecificchecks"}


def _flat(text: str) -> str:
    """Collapse argparse's line wrapping so names split at hyphens still match."""
    return "".join(text.split())


def _option_section(flat_help: str, flag: str) -> str:
    """Return the flattened help text following ``flag`` up to the next option."""
    start = flat_help.index(_MARKERS[flag])
    nxt = re.search(r"--[a-z]", flat_help[start + len(_MARKERS[flag]) :])
    end = start + len(_MARKERS[flag]) + nxt.start() if nxt else len(flat_help)
    return flat_help[start:end]


def _parser_help() -> str:
    parser = create_parser()
    sub = next(
        a for a in parser._actions if hasattr(a, "choices") and a.choices and "check" in a.choices
    )
    return sub.choices["check"].format_help()


def _check_cmd_help(capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit):
        check_cmd.main(["--help"])
    return capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--only", "--skip"])
@pytest.mark.parametrize("source", ["parser", "check_cmd"])
def test_help_lists_every_category(flag, source, capsys):
    text = _parser_help() if source == "parser" else _check_cmd_help(capsys)
    section = _option_section(_flat(text), flag)
    missing = [c for c in CHECK_CATEGORIES if c not in section]
    assert not missing, f"{source} {flag} help is missing categories: {missing}"
    assert "dimensions,edge,silkscreen" not in section


@pytest.mark.parametrize("flag", ["--only", "--skip"])
def test_unknown_category_lists_available(flag, capsys, tmp_path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    rc = check_cmd.main([str(pcb), flag, "bogus"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown check category: 'bogus'" in err
    assert "Available:" in err
