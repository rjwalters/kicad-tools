"""Placement populations follow the chosen attempt, not the last trial."""

from kicad_tools.cli import route_cmd, route_placement
from kicad_tools.router.io import load_pcb_for_routing
from tests.test_routing_placement_disposition import board_text


def test_plane_exclusions_are_attempt_local_and_selected_result_wins(tmp_path):
    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    args = route_cmd._route_parser().parse_args([str(source), "--skip-nets", "PARTNER"])
    route_placement.prepare(args, source)
    first = route_placement.for_attempt(args, ["PARTNER", "PLANE"])
    router, _ = load_pcb_for_routing(str(source), placement_disposition=first, force_python=True)
    assert first.plane_excluded_nets == frozenset({"PLANE"})
    assert first.user_excluded_nets == frozenset({"PARTNER"})
    assert first.requested_nets == frozenset({"BAD", "GOOD"})
    # A later rung can route a net previously assigned to a plane.
    later = route_placement.for_attempt(args, ["PARTNER"])
    assert not later.plane_excluded_nets
    assert "PLANE" in later.eligible_nets
    assert first.plane_excluded_nets == frozenset({"PLANE"})
    route_placement.select_result(args, router)
    assert args._placement_disposition == first


def test_route_only_inverted_skips_are_not_plane_intent(tmp_path):
    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    args = route_cmd._route_parser().parse_args([str(source), "--nets", "GOOD"])
    route_placement.prepare(args, source)
    result = route_placement.for_attempt(args, ["BAD", "PARTNER", "PLANE"])
    assert not result.plane_excluded_nets
    assert not result.user_excluded_nets
    assert result.unrequested_nets == frozenset({"BAD", "PARTNER", "PLANE"})
    assert result.requested_nets == result.eligible_nets == frozenset({"GOOD"})
