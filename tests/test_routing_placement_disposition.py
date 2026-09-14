"""Placement exclusions are net-wide obstacles, with an explicit export handoff."""

import pytest

from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.io import load_pcb_for_routing


def board_text(*, name_only=False, invalid=True):
    nets = {1: "BAD", 2: "GOOD", 3: "PARTNER", 4: "PLANE"}
    table = "" if name_only else "\n".join(f'(net {i} "{n}")' for i, n in nets.items())
    parts = []
    for ref, x, y, net in [
        ("X1", 125 if invalid else 110, 105, 1),
        ("X2", 105, 103, 1),
        ("X3", 108, 103, 1),
        ("R1", 105, 107, 2),
        ("R2", 110, 107, 2),
        ("P1", 105, 109, 3),
        ("P2", 110, 109, 3),
        ("G1", 115, 105, 4),
    ]:
        net_expr = f'(net "{nets[net]}")' if name_only else f'(net {net} "{nets[net]}")'
        parts.append(f'''(footprint "test" (layer "F.Cu") (at {x} {y})
          (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
          (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") {net_expr}))''')
    copper_net = '(net "BAD")' if name_only else "(net 1)"
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) {table}
      (gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
      {" ".join(parts)}
      (segment (start 105 103) (end 106 103) (width 0.2) (layer "F.Cu") {copper_net})
      (via (at 106 103) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {copper_net}))"""


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "mixed.kicad_pcb"
    path.write_text(board_text())
    return path


def test_population_and_coupled_closure(board):
    result = analyze_routing_placement(
        board, user_excluded_nets={"PLANE"}, coupled_groups=[("BAD", "PARTNER")]
    )
    assert result.invalid_references == frozenset({"X1"})
    assert result.direct_invalid_nets == frozenset({"BAD"})
    assert result.coupled_invalid_nets == frozenset({"PARTNER"})
    assert result.requested_nets == frozenset({"BAD", "GOOD", "PARTNER"})
    assert result.eligible_nets == frozenset({"GOOD"})
    assert result.requested_invalid_nets == frozenset({"BAD", "PARTNER"})
    assert result.user_excluded_nets == frozenset({"PLANE"})


def test_explicit_request_and_plane_exclusion(board):
    result = analyze_routing_placement(
        board, requested_nets={"GOOD"}, plane_excluded_nets={"PLANE"}
    )
    assert result.requested_nets == result.eligible_nets == frozenset({"GOOD"})
    assert not result.requested_invalid_nets
    assert result.invalid_nets == frozenset({"BAD"})
    assert result.unrequested_nets == frozenset({"BAD", "PARTNER"})
    assert result.plane_excluded_nets == frozenset({"PLANE"})


@pytest.mark.parametrize("name_only", [False, True])
def test_loader_preserves_geometry_and_export_handoff(tmp_path, name_only):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(board_text(name_only=name_only))
    original = path.read_bytes()
    result = analyze_routing_placement(path, coupled_groups=[("BAD", "PARTNER")])
    router, nets = load_pcb_for_routing(str(path), placement_disposition=result, force_python=True)
    assert nets["BAD"] > 0
    assert nets["BAD"] not in router.nets
    assert nets["PARTNER"] not in router.nets
    assert nets["GOOD"] in router.nets
    assert router.pads[("X2", "1")].net == 0
    assert router.pads[("X3", "1")].net == 0
    assert router.placement_disposition == result
    assert len(router.placement_preserved_routes) == 1
    preserved = router.placement_preserved_routes[0]
    assert preserved.net == nets["BAD"]
    assert preserved.net_name == "BAD"
    assert len(preserved.segments) == len(preserved.vias) == 1
    assert router.existing_routes[0].net == 0
    assert router.existing_routes[0].segments[0].net == 0
    assert router.existing_routes[0].vias[0].net == 0
    assert "(segment" in router.placement_preserved_copper
    assert "(via" in router.placement_preserved_copper
    for x, y, layer in [(108, 103, 0), (105.6, 103, 0), (106, 103, 1)]:
        gx, gy = router.grid.world_to_grid(x, y)
        assert router.grid.is_blocked_for_net(gx, gy, layer, nets["GOOD"])
    gx, gy = router.grid.world_to_grid(110, 110)
    assert not router.grid.is_blocked_for_net(gx, gy, 0, nets["GOOD"])
    assert not router.routes
    assert path.read_bytes() == original


def test_effective_netlist_identity_preserves_authored_copper(board):
    mapping = {"X1.1": "NEW", "R1.1": "NEW", "R2.1": "NEW"}
    result = analyze_routing_placement(board, netlist=mapping)
    assert result.direct_invalid_nets == frozenset({"NEW"})
    assert "BAD" in result.preserve_copper_nets
    router, nets = load_pcb_for_routing(
        str(board), netlist=mapping, placement_disposition=result, force_python=True
    )
    assert nets["NEW"] not in router.nets
    assert router.pads[("R1", "1")].net == 0
    assert router.placement_preserved_routes[0].net_name == "BAD"


def test_override_and_valid_board(board):
    override = analyze_routing_placement(board, allow_offboard=True)
    assert not override.invalid_nets
    assert override.eligible_nets == override.requested_nets
    board.write_text(board_text(invalid=False))
    assert not analyze_routing_placement(board).invalid_references


def test_unavailable_check_does_not_invent_invalid_nets(board, monkeypatch):
    from kicad_tools.placement import PlacementAnalyzer

    def unavailable(*args, **kwargs):
        raise RuntimeError("check unavailable")

    monkeypatch.setattr(PlacementAnalyzer, "find_conflicts", unavailable)
    result = analyze_routing_placement(board, requested_nets={"GOOD"})
    assert not result.check_available
    assert not result.invalid_nets
    assert result.eligible_nets == frozenset({"GOOD"})


def test_transitive_coupling_before_requested_filter(board):
    result = analyze_routing_placement(
        board,
        requested_nets={"GOOD"},
        user_excluded_nets={"BAD"},
        coupled_groups=[("BAD", "PARTNER"), ("PARTNER", "GOOD")],
    )
    assert result.coupled_invalid_nets == frozenset({"PARTNER", "GOOD"})
    assert result.requested_invalid_nets == frozenset({"GOOD"})
    assert not result.eligible_nets


def test_warning_only_courtyard_overhang(board):
    text = board_text(invalid=False)
    text = text.replace(
        '(property "Reference" "X1"',
        "(fp_rect (start -15 -1) (end 1 1) (stroke (width 0.05) (type default)) "
        '(fill none) (layer "F.CrtYd")) (property "Reference" "X1"',
    )
    board.write_text(text)
    from kicad_tools.placement import ConflictSeverity, ConflictType, PlacementAnalyzer

    findings = PlacementAnalyzer().find_conflicts(board)
    assert any(
        c.type == ConflictType.OFF_BOARD and c.severity == ConflictSeverity.WARNING
        for c in findings
    )
    result = analyze_routing_placement(board)
    assert not result.invalid_nets
    assert result.eligible_nets == result.requested_nets


def test_no_outline_remains_permissive(board):
    text = board_text().replace(
        '(gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))',
        "",
    )
    board.write_text(text)
    assert not analyze_routing_placement(board).invalid_nets


def test_loader_without_disposition_retains_old_behavior(board):
    router, nets = load_pcb_for_routing(str(board), force_python=True)
    assert nets["BAD"] in router.nets
    assert router.placement_disposition is None
    assert not router.placement_preserved_routes
    assert not router.existing_routes


def test_affected_arc_is_retained_as_physical_copper(board):
    text = board.read_text()
    board.write_text(
        text[:-1]
        + '\n(arc (start 105 103) (mid 106 104) (end 107 103) (width 0.2) (layer "F.Cu") (net 1)))'
    )
    result = analyze_routing_placement(board)
    router, _ = load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)
    assert router.placement_preserved_arcs
    assert router.grid.fixed_fills
    assert not router.grid.fixed_fills.segment_clear((106, 104), (106, 104), 0, 0.1, 0.2)
    assert router.grid.fixed_fills.segment_clear((106, 103), (106, 103), 0, 0.1, 0.2)


def test_export_handoff_retains_authored_blocks_exactly_once(board):
    from kicad_tools.cli.route_cmd import _write_routed_pcb

    result = analyze_routing_placement(board)
    router, _ = load_pcb_for_routing(
        str(board), placement_disposition=result, force_python=True, load_existing_routes=True
    )
    original = board.read_bytes()
    out = board.with_stem("exported")
    # Simulate the dependent consumer's replace-copper boundary using the
    # explicit raw handoff; final CLI integration additionally emits new routes.
    _write_routed_pcb(board, out, router.placement_preserved_copper)
    assert out.read_text().count("(segment ") == 1
    assert out.read_text().count("(via ") == 1
    for block in router.placement_preserved_copper.splitlines():
        assert block in out.read_text()
    assert board.read_bytes() == original
    assert len(router.existing_routes) == 1


def test_independent_valid_net_still_produces_real_copper(board):
    result = analyze_routing_placement(board)
    router, nets = load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)
    assert not router.route_net(nets["BAD"])
    routes = router.route_net(nets["GOOD"])
    assert routes
    assert any(route.segments for route in routes)
    assert {route.net for route in routes} == {nets["GOOD"]}


def test_excluded_custom_pad_is_not_silently_dropped(board):
    board.write_text(board.read_text().replace("smd rect", "smd custom", 1))
    result = analyze_routing_placement(board)
    with pytest.raises(ValueError, match="custom"):
        load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)


def test_loader_rejects_different_effective_netlist(board):
    result = analyze_routing_placement(board, netlist={"X1.1": "NEW"})
    with pytest.raises(ValueError, match="pad/net identities do not match"):
        load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)


def test_affected_filled_zone_is_retained_as_physical_obstacle(board):
    board.write_text(
        board.read_text()[:-1]
        + """
      (zone (net 1) (net_name "BAD") (layer "F.Cu")
        (polygon (pts (xy 104 102) (xy 107 102) (xy 107 104)))
        (filled_polygon (layer "F.Cu") (pts (xy 104 102) (xy 107 102) (xy 107 104)))))"""
    )
    result = analyze_routing_placement(board)
    router, _ = load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)
    assert router.grid.fixed_fills
    assert len(router.placement_preserved_zones) == 1
    assert not router.grid.fixed_fills.segment_clear((106, 102.5), (106, 102.5), 0, 0.1, 0.2)


def test_malformed_affected_copper_is_rejected(board):
    board.write_text(board.read_text().replace("(width 0.2)", ""))
    result = analyze_routing_placement(board)
    with pytest.raises(ValueError, match="unsupported or malformed segment"):
        load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)


@pytest.mark.parametrize("net_name", ["BAD)", "BAD(", 'BAD\\"(quoted)', "BAD(segment ", "BAD(via "])
@pytest.mark.parametrize("name_only", [False, True])
def test_quoted_net_punctuation_survives_copper_handoff(board, net_name, name_only):
    from kicad_tools.cli.route_cmd import _write_routed_pcb
    from kicad_tools.schema.pcb import PCB

    board.write_text(board_text(name_only=name_only).replace("BAD", net_name))
    original = board.read_bytes()
    result = analyze_routing_placement(board)
    router, _ = load_pcb_for_routing(str(board), placement_disposition=result, force_python=True)
    assert len(router.placement_preserved_routes) == 1
    output = board.with_stem("quoted_export")
    _write_routed_pcb(board, output, router.placement_preserved_copper)
    parsed = PCB.load(output)
    assert len(parsed.segments) == 1
    assert len(parsed.vias) == 1
    assert board.read_bytes() == original
    for block in router.placement_preserved_copper.splitlines():
        assert block in original.decode()


def _empty_number_board(board):
    """Two mechanical lands share a footprint with ordinary numbered pads."""
    text = board_text(invalid=False)
    original = '(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "BAD"))'
    replacement = """(pad 1 smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "BAD"))
      (pad "A1" smd rect (at 0 1) (size 0.6 0.6) (layers "F.Cu") (net 1 "BAD"))
      (pad "" smd rect (at 1 0) (size 0.6 0.6) (layers "F.Cu"))
      (pad "" smd rect (at 2 0) (size 0.6 0.6) (layers "F.Cu"))"""
    assert original in text
    board.write_text(text.replace(original, replacement, 1))
    return board


@pytest.mark.parametrize("override", [False, True])
def test_loader_empty_pad_numbers_match_schema_identity_multiset(board, override):
    from kicad_tools.schema.pcb import PCB

    _empty_number_board(board)
    before = board.read_bytes()
    mapping = {"X1.": "MECHANICAL"} if override else None
    disposition = analyze_routing_placement(board, netlist=mapping)
    schema_numbers = [
        p.number for fp in PCB.load(board).footprints if fp.reference == "X1" for p in fp.pads
    ]
    assert schema_numbers == ["1", "A1", "", ""]
    identity = ("X1", "", "", "MECHANICAL" if override else "")
    assert disposition.pad_net_identities.count(identity) == 2
    router, net_map = load_pcb_for_routing(
        str(board), netlist=mapping, placement_disposition=disposition, force_python=True
    )
    assert ("X1", "") in router.pads
    assert ("X1", '""') not in router.pads
    assert ("X1", None) not in router.pads
    assert ("X1", "1") in router.pads and ("X1", "A1") in router.pads
    assert router.pads[("X1", "")].net == (net_map["MECHANICAL"] if override else 0)
    assert router.placement_disposition.pad_net_identities.count(identity) == 2
    for x in (111, 112):
        gx, gy = router.grid.world_to_grid(x, 105)
        assert router.grid.is_blocked_for_net(gx, gy, 0, net_map["GOOD"])
    assert board.read_bytes() == before


@pytest.mark.parametrize("mismatch", ["netlist", "remove_occurrence"])
def test_empty_number_fix_keeps_identity_mismatch_rejection(board, mismatch):
    _empty_number_board(board)
    disposition = analyze_routing_placement(board)
    mapping = None
    if mismatch == "netlist":
        mapping = {"X1.": "MECHANICAL"}
    else:
        board.write_text(
            board.read_text().replace(
                '(pad "" smd rect (at 2 0) (size 0.6 0.6) (layers "F.Cu"))', ""
            )
        )
    with pytest.raises(ValueError, match="pad/net identities do not match"):
        load_pcb_for_routing(
            str(board), netlist=mapping, placement_disposition=disposition, force_python=True
        )
