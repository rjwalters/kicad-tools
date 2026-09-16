"""Physical access survives routing lifecycle and uses real plane identities."""

import hashlib

import pytest

from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.plane_access import PlaneAccessPolicy, PlaneAccessTarget
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules
from kicad_tools.zones.pour_escape import EscapeRules


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "access.kicad_pcb"
    path.write_text("""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (2 "In1.Cu" power) (4 "In2.Cu" signal)
              (31 "B.Cu" signal) (44 "Edge.Cuts" user)
      )
      (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "POWER")
      (footprint "test" (layer "F.Cu") (at 10 10)
        (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
        (pad "1" smd rect (at 0 0) (size .45 .45) (layers "F.Cu") (net 1 "GND"))
        (pad "2" smd rect (at 3 0) (size .45 .45) (layers "F.Cu") (net 2 "SIG"))
        (pad "3" smd rect (at 0 3) (size .45 .45) (layers "F.Cu") (net 3 "POWER")))
      (gr_rect (start 0 0) (end 20 20) (stroke (width .05) (type default))
        (fill none) (layer "Edge.Cuts")))""")
    return path


def load(path, **kwargs):
    return load_pcb_for_routing(
        str(path),
        skip_nets=["GND", "POWER"],
        force_python=True,
        rules=DesignRules(grid_resolution=0.05),
        plane_access_policy=PlaneAccessPolicy(
            (PlaneAccessTarget("GND", "In1.Cu"), PlaneAccessTarget("POWER", "In1.Cu")),
            EscapeRules(),
        ),
        **kwargs,
    )


def test_real_identities_fixed_lifecycle_and_same_site_output(board):
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    router, names = load(board)
    fixed = router._plane_access_routes
    assert len(fixed) == 2
    assert {r.net for r in fixed} == {names["GND"], names["POWER"]}
    assert names["GND"] not in router.nets and names["POWER"] not in router.nets
    assert {p.net for p in router.pads.values() if p.net_name in {"GND", "POWER"}} == {1, 3}
    via = fixed[0].vias[0]
    assert via.diameter == 0.5 and via.drill == 0.3
    assert (via.x, via.y) != (10, 10)
    assert all(s.width == 0.2 for r in fixed for s in r.segments)
    for layer in router.grid.layer_stack.layers:
        assert not router.grid.fixed_fills.segment_clear(
            (via.x - 1, via.y), (via.x + 1, via.y), layer.index, 0.1, 0.15
        )
    loser = Route(2, "SIG", [Segment(2, 2, 3, 2, 0.15, Layer.F_CU, 2, "SIG")])
    router.routes.append(loser)
    router._mark_route(loser)
    router.restore_route_snapshot([])
    assert all(any(item is route for item in router.grid.routes) for route in fixed)
    text = router.to_sexp(skip_cleanup=True)
    assert text.count("(via") == 2
    assert f"(at {via.x:.4f} {via.y:.4f})" in text
    assert '(net "GND")' in router.to_sexp(skip_cleanup=True, name_only=True)
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("damage", ["geometry", "route", "fills", "collision"])
def test_export_fails_closed_after_access_damage(board, damage):
    from kicad_tools.router.fixed_copper import FixedFillObstacles

    router, _ = load(board)
    fixed = router._plane_access_routes[0]
    if damage == "geometry":
        fixed.vias[0].x += 0.1
    elif damage == "route":
        router.existing_routes.remove(fixed)
    elif damage == "fills":
        router.grid.install_fixed_fills(FixedFillObstacles())
    else:
        via = fixed.vias[0]
        router.routes.append(
            Route(
                2,
                "SIG",
                [Segment(via.x - 1, via.y, via.x + 1, via.y, 0.15, Layer.IN1_CU, 2, "SIG")],
            )
        )
    with pytest.raises(ValueError, match="[Ff]ixed plane access"):
        router.to_sexp(skip_cleanup=True)


def test_partial_and_replanning_requests_fail_closed(board):
    with pytest.raises(ValueError, match="complete board"):
        load(board, region=(0, 0, 10, 10))
    router, _ = load(board)
    board.write_text(
        board.read_text().rstrip().removesuffix(")") + router.to_sexp(skip_cleanup=True) + ")"
    )
    with pytest.raises(ValueError, match="fixed plane access metadata"):
        load(board)


def test_unreachable_access_returns_no_output(board):
    from shapely.geometry import box

    with pytest.raises(ValueError, match="Empty plane access region"):
        load_pcb_for_routing(
            str(board),
            skip_nets=["GND"],
            force_python=True,
            plane_access_policy=PlaneAccessPolicy(
                (PlaneAccessTarget("GND", "In1.Cu", box(50, 50, 51, 51)),), EscapeRules()
            ),
        )


def test_native_access_geometry_survives_mirror_rebuild(board):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("Native routing backend unavailable")
    router, _ = load(board)
    via = router._plane_access_routes[0].vias[0]
    fills = router.grid.fixed_fills
    for _ in range(2):
        native = CppGrid.from_routing_grid(router.grid)
        for layer in router.grid.layer_stack.layers:
            for delta in (0, 0.49, 0.60, 0.74, 0.76, 1.0):
                a = (via.x + delta, via.y - 0.1)
                b = (via.x + delta, via.y + 0.1)
                assert native._impl.fixed_fill_clear(
                    *a, *b, layer.index, 0.1, 0.15
                ) == fills.segment_clear(a, b, layer.index, 0.1, 0.15)
        router.restore_route_snapshot([])
        assert router.to_sexp(skip_cleanup=True).count("(via") == 2


def test_trial_reset_preserves_access_geometry(board):
    router, _ = load(board)
    points = [(v.x, v.y) for r in router._plane_access_routes for v in r.vias]
    router._reset_for_new_trial()
    assert router.to_sexp(skip_cleanup=True).count("(via") == len(points)
    for x, y in points:
        assert not router.grid.fixed_fills.via_clear((x, y), (0, 1, 2, 3), 0.25, 0.15)


def test_shared_helper_respects_nonrectangular_region():
    from shapely.geometry import box

    from kicad_tools.zones.pour_escape import find_escape

    region = box(-2, -2, 2, 2).difference(box(-0.3, -0.3, 0.3, 0.3))
    result = find_escape(
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [],
        [(box(-2, -2, 2, 2), {"In1.Cu"}, "plane")],
        (-2, -2, 2, 2),
        EscapeRules(),
        allowed_region=region,
    )
    assert result is None


def test_name_only_board_preserves_distinct_plane_nets(board):
    import re

    text = board.read_text()
    text = re.sub(r'\(net [0-9]+ "([^"]*)"\)', r'(net "\1")', text)
    board.write_text(text)
    router, names = load(board)
    assert len({r.net for r in router._plane_access_routes}) == 2
    assert all(r.net == names[r.net_name] for r in router._plane_access_routes)
    emitted = router.to_sexp(skip_cleanup=True, name_only=True)
    assert '(net "GND")' in emitted and '(net "POWER")' in emitted


def test_absent_plane_layer_rejected(board):
    with pytest.raises(ValueError, match="absent from routing stack"):
        load_pcb_for_routing(
            str(board),
            skip_nets=["GND"],
            force_python=True,
            plane_access_policy=PlaneAccessPolicy(
                (PlaneAccessTarget("GND", "In3.Cu"),), EscapeRules()
            ),
        )


@pytest.mark.parametrize("load_existing", [False, True])
def test_serialized_reload_cannot_drop_access_policy(board, load_existing):
    router, _ = load(board)
    emitted = router.to_sexp(skip_cleanup=True)
    board.write_text(board.read_text().rstrip().removesuffix(")") + emitted + ")")
    assert '(group "kct:fixed-plane-access:v1"' in board.read_text()
    with pytest.raises(ValueError, match="fixed plane access metadata"):
        load_pcb_for_routing(str(board), load_existing_routes=load_existing, force_python=True)


def test_access_rules_bind_both_projects_and_output_dru(tmp_path):
    import json

    from tests.test_board06_pour_escape import _kct_managed_dru

    source, output = [tmp_path / name for name in ("input.kicad_pro", "output.kicad_pro")]
    source.write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {"min_through_hole_diameter": 0.5, "min_clearance": 0.35}
                    }
                }
            }
        )
    )
    output.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_via_annular_width": 0.3}}}})
    )
    output.with_suffix(".kicad_dru").write_text(
        _kct_managed_dru(
            '(rule "Copper to Edge - jlcpcb-tier1"\n  (constraint edge_clearance (min 0.3mm)))',
            '(rule "Hole to Edge - jlcpcb-tier1"\n'
            "  (condition \"(A.Type == 'via' || A.Type == 'pad') && B.Layer == 'Edge.Cuts'\")\n"
            "  (constraint physical_hole_clearance (min 0.4mm)))",
        )
    )
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    rules = EscapeRules.from_projects(source, output)
    assert rules.clearance == 0.35 and rules.drill == 0.5 and rules.annulus == 0.3
    assert rules.diameter == pytest.approx(1.1)
    assert rules.edge_clearance == 0.3 and rules.hole_edge_clearance == 0.4
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}
    output.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "Unknown")')
    with pytest.raises(ValueError, match="custom DRC rules"):
        EscapeRules.from_projects(source, output)


@pytest.fixture
def cli_plan(board, tmp_path):
    import json
    from argparse import Namespace

    project = board.with_suffix(".kicad_pro")
    project.write_text("{}")
    output = tmp_path / "out.kicad_pcb"
    output.with_suffix(".kicad_pro").write_text("{}")
    plan = tmp_path / "access.json"
    plan.write_text(
        json.dumps(
            {
                "version": 1,
                "edge_clearance": 0.5,
                "source_project": str(project),
                "pour_nets": [["GND", "ground"], ["POWER", "power"]],
            }
        )
    )
    return Namespace(plane_access_plan=str(plan), output=str(output))


def test_cli_policy_keeps_component_proof_and_actual_regions(board, cli_plan):
    from shapely.geometry import Polygon

    from kicad_tools.cli.route_plane_access import policy_for_attempt
    from kicad_tools.placement.routing import analyze_routing_placement
    from kicad_tools.router.net_class import NetClass
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

    skipped = ["GND", "POWER"]
    policy = policy_for_attempt(cli_plan, board, skipped)
    disposition = analyze_routing_placement(board, user_excluded_nets=skipped)
    router, _ = load_pcb_for_routing(
        str(board),
        skip_nets=skipped,
        force_python=True,
        rules=DesignRules(grid_resolution=0.05),
        placement_disposition=disposition,
        plane_access_policy=policy,
    )
    assert router.placement_disposition is disposition
    assert len(router._plane_access_routes) == 2
    auto_create_zones_for_pour_nets(
        board, [("GND", NetClass.GROUND), ("POWER", NetClass.POWER)], edge_clearance=0.5
    )
    poured = PCB.load(board)
    for target in policy.targets:
        zone = next(z for z in poured.zones if z.net_name == target.net_name)
        assert target.region.equals(Polygon(zone.polygon))


@pytest.mark.parametrize(
    "flag",
    [
        "nets",
        "complete",
        "region",
        "allow_offboard",
        "preserve_existing",
        "auto_layers",
        "auto_mfr_tier",
        "adaptive_rules",
        "auto_pcb_size",
    ],
)
def test_cli_policy_rejects_partial_or_override_modes(board, cli_plan, flag):
    from kicad_tools.cli.route_plane_access import policy_for_attempt

    setattr(cli_plan, flag, True)
    with pytest.raises(ValueError, match="complete unrouted"):
        policy_for_attempt(cli_plan, board, ["GND", "POWER"])


def test_cli_policy_preserves_stronger_output_and_source_floors(board, cli_plan):
    import json
    from pathlib import Path

    from kicad_tools.cli.route_plane_access import policy_for_attempt

    board.with_suffix(".kicad_pro").write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.31}}}})
    )
    Path(cli_plan.output).with_suffix(".kicad_pro").write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_hole_to_hole": 0.7}}}})
    )
    policy = policy_for_attempt(cli_plan, board, ["GND", "POWER"])
    assert policy.rules.clearance == 0.31
    assert policy.rules.hole_gap == 0.7


@pytest.mark.parametrize("damage", ["invalid", "partial", "unavailable", "identities"])
def test_cli_access_rejects_damaged_component_proof(board, cli_plan, damage):
    from dataclasses import replace

    from kicad_tools.cli.route_plane_access import policy_for_attempt
    from kicad_tools.placement.routing import analyze_routing_placement

    skipped = ["GND", "POWER"]
    disposition = analyze_routing_placement(board, user_excluded_nets=skipped)
    if damage == "invalid":
        disposition = replace(disposition, direct_invalid_nets=frozenset({"SIG"}))
    elif damage == "partial":
        disposition = replace(disposition, unrequested_nets=frozenset({"SIG"}))
    elif damage == "unavailable":
        disposition = replace(disposition, check_available=False)
    else:
        disposition = replace(disposition, pad_net_identities=())
    with pytest.raises(ValueError):
        load_pcb_for_routing(
            str(board),
            skip_nets=skipped,
            force_python=True,
            rules=DesignRules(grid_resolution=0.05),
            placement_disposition=disposition,
            plane_access_policy=policy_for_attempt(cli_plan, board, skipped),
        )


def test_outer_cli_delivers_plan_and_placement_proof_to_loader(board, cli_plan, monkeypatch):
    from kicad_tools.cli import main, route_cmd
    from kicad_tools.router import io

    seen = []
    original = io.load_pcb_for_routing

    def capture(*args, **kwargs):
        assert kwargs["plane_access_policy"] is not None
        assert kwargs["placement_disposition"] is not None
        router, _ = original(*args, **kwargs)
        seen.append(len(router._plane_access_routes))
        raise RuntimeError("intentional stop after access installation")

    monkeypatch.setattr(io, "load_pcb_for_routing", capture)
    monkeypatch.setattr(route_cmd, "main", route_cmd._in_process_main)
    result = main(
        [
            "route",
            str(board),
            "--output",
            cli_plan.output,
            "--no-auto-layers",
            "--layers",
            "4",
            "--skip-nets",
            "GND,POWER",
            "--no-auto-pour",
            "--plane-access-plan",
            cli_plan.plane_access_plan,
        ]
    )
    assert result == 1
    assert seen == [2]


def test_cli_plan_rejects_auto_pour_before_output_mutation(board, cli_plan, monkeypatch):
    from pathlib import Path

    from kicad_tools.cli import main, route_cmd

    before = board.read_bytes()
    monkeypatch.setattr(route_cmd, "main", route_cmd._in_process_main)
    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "route",
                str(board),
                "-o",
                cli_plan.output,
                "--no-auto-layers",
                "--layers",
                "4",
                "--skip-nets",
                "GND,POWER",
                "--plane-access-plan",
                cli_plan.plane_access_plan,
            ]
        )
    assert stopped.value.code == 2
    assert not Path(cli_plan.output).exists()
    assert board.read_bytes() == before


def test_cli_plan_rejects_unmodeled_output_rules(board, cli_plan):
    from pathlib import Path

    from kicad_tools.cli.route_plane_access import policy_for_attempt

    Path(cli_plan.output).with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "custom" (constraint clearance (min .8)))'
    )
    with pytest.raises(ValueError, match="custom DRC"):
        policy_for_attempt(cli_plan, board, ["GND", "POWER"])


def test_cli_plan_rejects_extra_skipped_signal(board, cli_plan):
    from kicad_tools.cli.route_plane_access import policy_for_attempt

    with pytest.raises(ValueError, match="all explicitly skipped"):
        policy_for_attempt(cli_plan, board, ["GND", "POWER", "SIG"])
