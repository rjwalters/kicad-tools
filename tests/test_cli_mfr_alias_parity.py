"""Issue #6946: standalone ``--mfr`` parsers must accept every alias ``kct`` advertises.

``kct drc/check/audit/reason/repair-clearance --mfr`` offer
``get_all_manufacturer_names()`` (canonical IDs + aliases such as ``jlc``,
``osh``) but the standalone parsers they forward to only accepted canonical IDs,
so ``kct drc board.kicad_pcb -m jlc`` died with ``invalid choice``.
"""

import argparse
import contextlib
import importlib

import pytest

from kicad_tools.cli.drc_cmd import build_parser
from kicad_tools.cli.parser import create_parser

# (unified subcommand, standalone module)
FORWARDED = [
    ("drc", "kicad_tools.cli.drc_cmd"),
    ("check", "kicad_tools.cli.check_cmd"),
    ("audit", "kicad_tools.cli.audit_cmd"),
    ("reason", "kicad_tools.cli.reason_cmd"),
    ("repair-clearance", "kicad_tools.cli.repair_clearance_cmd"),
]
ALIASES = ["jlc", "osh"]


def _mfr_action(parser):
    for action in parser._actions:
        if {"--mfr", "-m"} & set(action.option_strings):
            return action
    return None


def _unified_subparser(name):
    for action in create_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("no subparsers on unified kct parser")


def test_drc_standalone_choices_cover_unified_choices():
    standalone = _mfr_action(build_parser())
    unified = _mfr_action(_unified_subparser("drc"))
    assert set(unified.choices) <= set(standalone.choices)


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize(("subcommand", "module"), FORWARDED)
def test_standalone_main_does_not_reject_alias(subcommand, module, alias, tmp_path, capsys):
    main = importlib.import_module(module).main
    missing = str(tmp_path / "missing.kicad_pcb")
    with contextlib.suppress(SystemExit):
        main([missing, "-m", alias])
    err = capsys.readouterr().err
    assert "invalid choice" not in err, f"{subcommand} rejected --mfr {alias}: {err}"
