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


def test_explicit_coupled_class_blocks_partner_before_filtering(tmp_path):
    from kicad_tools.router.rules import NetClassRouting

    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    args = route_cmd._route_parser().parse_args([str(source), "--nets", "PARTNER"])
    args._loaded_net_class_map = {
        "BAD": NetClassRouting(name="Pair", diffpair_partner="PARTNER", coupled_routing=True)
    }
    route_placement.prepare(args, source)
    result = args._placement_disposition
    assert result.coupled_invalid_nets == frozenset({"PARTNER"})
    assert result.requested_invalid_nets == frozenset({"PARTNER"})
    assert not result.eligible_nets
    router, _ = load_pcb_for_routing(str(source), placement_disposition=result, force_python=True)
    assert all(p.net == 0 for p in router.pads.values() if p.ref in {"P1", "P2"})


def test_disabled_coupled_class_does_not_exclude_suffix_partner(tmp_path):
    from kicad_tools.router.rules import NetClassRouting

    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text().replace('"BAD"', '"USB_D+"').replace('"PARTNER"', '"USB_D-"'))
    args = route_cmd._route_parser().parse_args([str(source), "--differential-pairs"])
    args._loaded_net_class_map = {
        name: NetClassRouting(name="Scalar", coupled_routing=False) for name in ("USB_D+", "USB_D-")
    }
    route_placement.prepare(args, source)
    assert not args._placement_disposition.coupled_invalid_nets
    assert "USB_D-" in args._placement_disposition.eligible_nets


def test_automatic_plane_skip_cannot_hide_requested_invalid_placement(tmp_path):
    source = tmp_path / "mixed.kicad_pcb"
    source.write_text(board_text())
    args = route_cmd._route_parser().parse_args([str(source)])
    route_placement.prepare(args, source)
    result = route_placement.for_attempt(args, ["BAD", "PLANE"])
    assert result.requested_invalid_nets == frozenset({"BAD"})
    assert result.plane_excluded_nets == frozenset({"PLANE"})
