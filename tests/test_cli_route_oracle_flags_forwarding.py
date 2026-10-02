"""``kct route`` accepts and forwards the #5785 oracle-loop flags.

``--oracle-rounds`` and ``--allow-stranded-pour-pads`` are advertised to
users in ``changelog.d/5785.upgrade.md`` as ``kct route`` options, so the
outer dispatcher (``cli/parser.py`` -> ``cli/commands/routing.py``) must
accept them and hand them to the inner ``route_cmd`` parser.  These tests
drive the real outer parser and capture the inner argv.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018) (generator pcbnew))")
    return pcb


def _dispatch(argv):
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(argv)
    with patch("kicad_tools.cli.route_cmd.main") as mock_main:
        mock_main.return_value = 0
        run_route_command(args)
        return args, mock_main.call_args[0][0]


def _value_after(argv, flag):
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize("rounds", ["0", "5"])
def test_oracle_rounds_accepted_and_forwarded(pcb_file, rounds):
    args, sub_argv = _dispatch(["route", str(pcb_file), "--oracle-rounds", rounds, "--quiet"])
    assert args.oracle_rounds == int(rounds)
    assert _value_after(sub_argv, "--oracle-rounds") == rounds


def test_allow_stranded_pour_pads_accepted_and_forwarded(pcb_file):
    args, sub_argv = _dispatch(["route", str(pcb_file), "--allow-stranded-pour-pads", "--quiet"])
    assert args.allow_stranded_pour_pads is True
    assert "--allow-stranded-pour-pads" in sub_argv


def test_unset_flags_forward_nothing(pcb_file):
    """Unset => inner defaults (env var / 3 rounds, strict verdict) apply."""
    args, sub_argv = _dispatch(["route", str(pcb_file), "--quiet"])
    assert args.oracle_rounds is None
    assert args.allow_stranded_pour_pads is False
    assert "--oracle-rounds" not in sub_argv
    assert "--allow-stranded-pour-pads" not in sub_argv


def test_forwarded_argv_parses_on_inner_parser(pcb_file):
    """The forwarded tokens round-trip through the real inner parser."""
    from kicad_tools.cli.route_cmd import _route_parser

    _, sub_argv = _dispatch(
        ["route", str(pcb_file), "--oracle-rounds", "0", "--allow-stranded-pour-pads", "--quiet"]
    )
    inner = _route_parser().parse_args(sub_argv)
    assert inner.oracle_rounds == 0
    assert inner.allow_stranded_pour_pads is True
