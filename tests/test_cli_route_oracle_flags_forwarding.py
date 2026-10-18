"""``kct route`` accepts and forwards the #5785 oracle-loop flags.

``--oracle-rounds`` and ``--allow-stranded-pour-pads`` are advertised to
users in ``changelog.d/5785.upgrade.md`` as ``kct route`` options, so
``kct route`` must accept them and hand them to ``route_cmd``.  These tests
drive the real ``kct`` entry point (``kct route`` passes argv straight
through since issue #7017) and capture what ``route_cmd.main`` receives.
"""

import pytest


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018) (generator pcbnew))")
    return pcb


def _dispatch(argv):
    """Return (route namespace, argv route_cmd.main received)."""
    from tests.route_cli import kct_route

    assert argv[0] == "route"
    forwarded, args = kct_route(argv[1:])
    return args, forwarded


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
