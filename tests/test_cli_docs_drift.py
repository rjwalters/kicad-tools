"""Docs-vs-parser drift guard for three ``docs/reference/cli.md`` sections (Issue #7070).

``cli.md`` is hand-written, so it drifts from ``kicad_tools.cli.parser``:
``optimize-traces`` documented a ``--length-match`` flag that was never
implemented, and listed 2 of its ~15 options; ``parts`` omitted ``import``;
``sch validate`` omitted ``--lib-path`` / ``--skip``.

For each guarded section this test checks, in both directions:

* every backticked ``--long-option`` (or subcommand) in the section exists on
  the real argparse tree, and
* every declared long option (or subcommand) appears in the section.

Scope is deliberately limited to these sections; a universal all-commands
check would need a large allowlist.  Add a section to ``SECTIONS`` to extend.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from kicad_tools.cli.parser import create_parser

CLI_MD = Path(__file__).resolve().parent.parent / "docs" / "reference" / "cli.md"

# Options argparse adds itself or that are global, never documented per command.
IGNORED_OPTIONS = {"--help"}

LONG_OPTION = re.compile(r"`(--[a-z0-9][a-z0-9-]*)`")
SUBCOMMAND_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|", re.MULTILINE)


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("parser has no subparsers")


def _find_parser(path: tuple[str, ...]) -> argparse.ArgumentParser:
    parser = create_parser()
    for name in path:
        parser = _subparsers_action(parser).choices[name]
    return parser


def _long_options(parser: argparse.ArgumentParser) -> set[str]:
    return {
        opt
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt not in IGNORED_OPTIONS
    }


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    return set(_subparsers_action(parser).choices)


def _section(heading: str) -> str:
    """Return the text under ``### `<heading>``` (or ####), up to the next heading or rule."""
    text = CLI_MD.read_text()
    match = re.search(
        rf"^#{{3,4}} `{re.escape(heading)}`\n(.*?)(?=^#{{2,4}} |^---$)", text, re.S | re.M
    )
    assert match, f"cli.md has no section headed `{heading}`"
    return match.group(1)


# (cli.md heading, parser path, check subcommands instead of options)
SECTIONS = [
    pytest.param("optimize-traces", ("optimize-traces",), False, id="optimize-traces"),
    pytest.param("parts", ("parts",), True, id="parts-subcommands"),
    pytest.param("parts import", ("parts", "import"), False, id="parts-import"),
    pytest.param("sch validate", ("sch", "validate"), False, id="sch-validate"),
]


def _documented_and_declared(heading: str, path: tuple[str, ...], subcommands: bool):
    body = _section(heading)
    parser = _find_parser(path)
    if subcommands:
        return set(SUBCOMMAND_ROW.findall(body)), _subcommands(parser)
    return set(LONG_OPTION.findall(body)), _long_options(parser)


@pytest.mark.parametrize(("heading", "path", "subcommands"), SECTIONS)
def test_documented_names_exist_on_parser(heading, path, subcommands):
    documented, declared = _documented_and_declared(heading, path, subcommands)
    phantom = sorted(documented - declared)
    assert not phantom, f"cli.md '{heading}' documents names the parser does not declare: {phantom}"


@pytest.mark.parametrize(("heading", "path", "subcommands"), SECTIONS)
def test_declared_names_are_documented(heading, path, subcommands):
    documented, declared = _documented_and_declared(heading, path, subcommands)
    missing = sorted(declared - documented)
    assert not missing, f"cli.md '{heading}' omits names the parser declares: {missing}"
