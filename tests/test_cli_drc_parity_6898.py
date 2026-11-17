"""Issue #6898: ``kct drc`` must expose the run-DRC options of ``kicad-drc``.

Mirrors ``TestExportParserParity`` (#6881/#6882) and adds a forwarding test so
an option added to the outer parser but not forwarded (the #3159/#4691 drift
trap) fails here.
"""

import argparse
from unittest.mock import patch

import pytest

from kicad_tools.cli.drc_cmd import build_parser
from kicad_tools.cli.parser import create_parser

# Standalone-only: superseded by `kct mfr rules` / `kct mfr compare`; kicad-drc
# prints a deprecation pointer instead of kct drc duplicating them.
STANDALONE_ONLY = {"--rules", "--compare"}


def _unified_drc_subparser(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["drc"]
    raise AssertionError("no subparsers on unified kct parser")


def _option_actions(parser):
    out = {}
    for action in parser._actions:
        if action.option_strings and action.dest != "help":
            for opt in action.option_strings:
                out[opt] = action
    return out


class TestDrcParserParity:
    def test_every_standalone_option_exists_in_unified_parser(self):
        standalone = _option_actions(build_parser())
        unified = _option_actions(_unified_drc_subparser(create_parser()))
        missing = sorted(set(standalone) - set(unified) - STANDALONE_ONLY)
        assert not missing, f"unified `kct drc` is missing options: {missing}"

    def test_allowlist_is_not_stale(self):
        unified = _option_actions(_unified_drc_subparser(create_parser()))
        assert not (STANDALONE_ONLY & set(unified))

    def test_choices_match_or_unified_is_superset(self):
        standalone = _option_actions(build_parser())
        unified = _option_actions(_unified_drc_subparser(create_parser()))
        for opt, action in standalone.items():
            if opt in STANDALONE_ONLY or action.choices is None:
                continue
            assert set(action.choices) <= set(unified[opt].choices), opt

    def test_dest_and_type_match(self):
        standalone = _option_actions(build_parser())
        unified = _option_actions(_unified_drc_subparser(create_parser()))
        for opt, action in standalone.items():
            if opt in STANDALONE_ONLY:
                continue
            assert unified[opt].dest == action.dest, opt
            assert unified[opt].type == action.type, opt
            assert type(unified[opt]) is type(action), opt


class TestDrcForwarding:
    def test_new_flags_reach_drc_cmd(self, tmp_path):
        from kicad_tools.cli import main

        report = tmp_path / "r.json"
        report.write_text("{}")
        out = tmp_path / "out.json"
        filt = tmp_path / "f.toml"
        captured = {}

        def fake(argv):
            captured["argv"] = argv
            return 0

        with patch("kicad_tools.cli.drc_cmd.main", side_effect=fake):
            rc = main(
                [
                    "drc",
                    str(report),
                    "--strict",
                    "-c",
                    "routing",
                    "-v",
                    "-s",
                    "--filter-config",
                    str(filt),
                    "--keep-report",
                    "-o",
                    str(out),
                    "-m",
                    "jlcpcb",
                ]
            )
        assert rc == 0
        argv = captured["argv"]
        assert "--strict" in argv and "--verbose" in argv and "--suggest" in argv
        assert "--keep-report" in argv
        assert argv[argv.index("--category") + 1] == "routing"
        assert argv[argv.index("--filter-config") + 1] == str(filt)
        assert argv[argv.index("--output") + 1] == str(out)
        # And the forwarded argv parses on the standalone parser.
        ns = build_parser().parse_args(argv)
        assert ns.strict and ns.verbose and ns.suggest and ns.keep_report

    def test_output_omitted_not_forwarded(self, tmp_path):
        from kicad_tools.cli import main

        captured = {}
        with patch(
            "kicad_tools.cli.drc_cmd.main",
            side_effect=lambda a: captured.setdefault("argv", a) and 0,
        ):
            main(["drc", "x.json"])
        assert "--output" not in captured["argv"]


class TestDeprecationPointer:
    @pytest.mark.parametrize(
        ("flag", "pointer"),
        [("--compare", "kct mfr compare"), ("--rules", "kct mfr rules")],
    )
    def test_standalone_flag_prints_pointer(self, flag, pointer, capsys):
        from kicad_tools.cli.drc_cmd import main

        assert main([flag]) == 0
        err = capsys.readouterr().err
        assert pointer in err
