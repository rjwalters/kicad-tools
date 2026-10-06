"""``kct route --deterministic-rescue`` wiring (Issue #5870).

Background
----------

``tests/test_pour_fill_determinism_5578.py::test_board03_pour_fill_is_reproducible``
failed once with board 03's ``('GND', 'B.Cu')`` pour 21.59 mm^2 different
between two ``--seed 42 --deterministic-budget`` routes run while the machine
was busy.  Measured with retained stage snapshots (pre-fill, post-fill,
post-oracle) on fresh processes, idle vs bounded CPU load:

* The first divergent stage is ROUTING, not the fill.  Board 03's initial
  pass stalls on ``USB_CC1`` and the stall-relief rescue rips three blockers
  and re-lands them.  Those victim re-land sub-searches are bounded by
  ``RELIEF_SUBSEARCH_BUDGET_S`` (10 s wall clock) even under
  ``--deterministic-budget`` -- the deterministic arm (#4536) is opt-in and
  ``kct route`` never opted in (#4730).
* Lightly loaded, the re-lands finished under 10 s and every run produced
  the same 334 segment/via nodes.  Under CPU load the same re-lands needed
  10-31 s; with the 10 s cap they were cut at ~10.3-10.9 s, landed
  different copper, and two runs fell to 22/24 nets and escalated to the
  4-layer all-signal stack.  The fill around different copper differs by
  tens of mm^2, which is the reported failure.
* Lifting only that wall clock (or opting into the iteration-bounded arm)
  under the same load reproduced the unloaded route node-for-node.
* The native fill + thermal remediation were reproducible on identical
  input (four replays of one pre-fill board, under load, gave identical
  union geometry).  The later pour-oracle stage (#5785) is not reproducible
  even unloaded, because ``kicad-cli`` DRC's ``unconnected_items`` vary run
  to run.  That is a separate mechanism, tracked in #5934.

``--deterministic-rescue`` exposes the existing per-call opt-in on the CLI so
a route that must be load-independent can ask for it.  It stays OFF by
default: #4730/#4770 measured that the iteration-bounded rescue costs board
07 two nets on the negotiated path, and that decision is unchanged here.

These tests pin the wiring; the slow board-03 test exercises the behavior.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.cli.route_cmd import (
    _deterministic_rescue_kwargs,
    _normalize_deterministic_budget,
    _route_parser,
)

#: Every ``Autorouter`` entry point that can reach a relief rescue and takes
#: ``deterministic_rescue`` (see ``tests/test_relief_subsearch_budget.py``).
_RESCUE_ENTRY_POINTS = frozenset(
    {
        "route_all_negotiated",
        "route_all_two_phase",
        "route_with_escape",
        "route_with_escape_and_diffpairs",
    }
)


def test_kwargs_are_empty_without_the_flag():
    """Default runs pass no kwarg at all -- byte-identical to before #5870."""
    assert _deterministic_rescue_kwargs(SimpleNamespace()) == {}
    assert _deterministic_rescue_kwargs(SimpleNamespace(deterministic_rescue=False)) == {}


def test_kwargs_opt_in_with_the_flag():
    assert _deterministic_rescue_kwargs(SimpleNamespace(deterministic_rescue=True)) == {
        "deterministic_rescue": True
    }


def test_inner_parser_declares_the_flag_off_by_default():
    parser = _route_parser()
    assert parser.parse_args(["board.kicad_pcb"]).deterministic_rescue is False
    assert parser.parse_args(["board.kicad_pcb", "--deterministic-rescue"]).deterministic_rescue


def test_outer_parser_declares_the_flag():
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["route", "board.kicad_pcb", "--deterministic-rescue"])
    assert args.deterministic_rescue is True


@pytest.mark.parametrize("requested", [True, False])
def test_outer_command_forwards_the_flag(requested):
    from kicad_tools.cli.commands.routing import run_route_command

    args = SimpleNamespace(
        pcb="test.kicad_pcb",
        output=None,
        strategy="negotiated",
        skip_nets=None,
        grid="auto",
        trace_width=0.2,
        clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        mc_trials=10,
        iterations=15,
        verbose=False,
        dry_run=True,
        quiet=True,
        power_nets=None,
        deterministic_budget=True,
        deterministic_rescue=requested,
    )
    with patch("kicad_tools.cli.route_cmd.main") as mock_main:
        mock_main.return_value = 0
        run_route_command(args)
        sub_argv = mock_main.call_args[0][0]
    assert ("--deterministic-rescue" in sub_argv) is requested


def _entry_point_calls() -> list[ast.Call]:
    tree = ast.parse(Path(inspect.getsourcefile(route_cmd)).read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _RESCUE_ENTRY_POINTS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "router"
    ]


def test_every_routing_call_site_threads_the_opt_in():
    """A call site that forgets the splat silently ignores ``--deterministic-rescue``.

    Board 03 reaches the rescue through ``route_with_escape`` inside the
    layer-escalation loop; other boards reach it through the negotiated and
    two-phase dispatchers.  Pin all of them, so a new dispatch branch cannot
    re-open the load-dependent path for a run that asked for the opposite.
    """
    calls = _entry_point_calls()
    assert len(calls) >= 4, "the route_cmd dispatch sites were not found"
    missing = []
    for call in calls:
        splats = [
            kw.value
            for kw in call.keywords
            if kw.arg is None
            and isinstance(kw.value, ast.Call)
            and isinstance(kw.value.func, ast.Name)
            and kw.value.func.id == "_deterministic_rescue_kwargs"
        ]
        if not splats:
            missing.append(f"line {call.lineno}: router.{call.func.attr}(...)")
    assert not missing, (
        "routing call site(s) do not forward --deterministic-rescue "
        "(add **_deterministic_rescue_kwargs(args)):\n" + "\n".join(missing)
    )


def _budget_args(**overrides) -> SimpleNamespace:
    args = SimpleNamespace(
        deterministic_budget=True,
        per_net_timeout=30.0,
        max_search_iterations=0,
        per_net_iterations=0,
        timeout=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_deterministic_budget_banner_names_the_remaining_wall_clock(capsys):
    """``--deterministic-budget`` alone must not claim full load-independence silently."""
    _normalize_deterministic_budget(_budget_args(), quiet=False)
    out = capsys.readouterr().out
    assert "relief-rescue" in out
    assert "--deterministic-rescue" in out


def test_banner_note_absent_when_rescue_is_deterministic(capsys):
    _normalize_deterministic_budget(_budget_args(deterministic_rescue=True), quiet=False)
    assert "--deterministic-rescue" not in capsys.readouterr().out
