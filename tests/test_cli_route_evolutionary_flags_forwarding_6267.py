"""``kct route --pop-size/--generations`` reach the inner route command (#6267).

The outer parser accepted both flags but ``cli/commands/routing.py`` never
forwarded them, so every ``kct route --strategy evolutionary`` ran the default
20 x 10 GA -- 200 full ``route_all()`` evaluations -- no matter what the user
(or a test trying to keep the GA tiny, #5901) asked for.  That is why the
"tiny GA" evolutionary CLI tests still blew the 30 s route deadline.
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
        return mock_main.call_args[0][0]


def test_pop_size_and_generations_are_forwarded(pcb_file):
    sub_argv = _dispatch(
        ["route", str(pcb_file), "--strategy", "evolutionary", "--pop-size", "4",
         "--generations", "2", "--quiet"]
    )  # fmt: skip
    assert sub_argv[sub_argv.index("--pop-size") + 1] == "4"
    assert sub_argv[sub_argv.index("--generations") + 1] == "2"


def test_defaults_forward_nothing(pcb_file):
    sub_argv = _dispatch(["route", str(pcb_file), "--quiet"])
    assert "--pop-size" not in sub_argv
    assert "--generations" not in sub_argv


def test_forwarded_values_reach_the_inner_parser(pcb_file):
    from kicad_tools.cli.route_cmd import _route_parser

    sub_argv = _dispatch(["route", str(pcb_file), "--pop-size", "3", "--generations", "1"])
    parsed = _route_parser().parse_args(sub_argv)
    assert (parsed.pop_size, parsed.generations) == (3, 1)
