"""Authored pad clearance must survive fine-pitch exceptions in strict mode."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from kicad_tools.router.cache import CacheKey, SubProblemSignature
from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


def fixture(strict, foreign_net=2):
    rules = DesignRules(
        grid_resolution=0.05, trace_width=0.15, trace_clearance=0.15, strict_pad_clearance=strict
    )
    grid = RoutingGrid(width=10, height=10, rules=rules)
    start = Pad(
        x=5,
        y=4.5,
        width=1.475,
        height=0.3,
        net=1,
        net_name="SIGNAL",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    foreign = replace(
        start, y=5, net=foreign_net, net_name="NEIGHBOR" if foreign_net else "", pin="2"
    )
    end = replace(start, x=8, ref="J1", width=0.6, height=0.6)
    for pad in (start, foreign, end):
        grid.add_pad(pad)
    # 0.125mm edge clearance: legal copper separation, below authored0.15mm.
    bad = Segment(5, 5.35, 6, 5.35, 0.15, Layer.F_CU, net=1)
    return rules, grid, start, end, bad


@pytest.mark.parametrize("foreign_net", [0, 2])
@pytest.mark.parametrize("strict", [False, True])
def test_python_and_cpp_reject_same_component_subclearance_in_strict_mode(strict, foreign_net):
    rules, grid, start, end, bad = fixture(strict, foreign_net)
    valid, clearance, _ = grid.validate_segment_clearance(
        bad, exclude_net=1, exclude_refs={"U1", "J1"}
    )
    assert valid is (not strict)
    if strict:
        assert clearance == pytest.approx(0.125)
        deficit, _ = grid.worst_segment_pad_deficit(bad, 1, exclude_refs={"U1", "J1"})
        assert deficit == pytest.approx(0.025)
    if is_cpp_available():
        backend = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        violation = backend._validate_route_clearance(
            Route(net=1, net_name="SIGNAL", segments=[bad]), start, end, 5
        )
        assert (violation is not None) is strict


@pytest.mark.parametrize("backend_name", ["python", "cpp"])
def test_strict_fine_pitch_pad_still_escapes_along_its_long_axis(backend_name):
    rules, grid, start, end, _ = fixture(True)
    if backend_name == "cpp":
        if not is_cpp_available():
            pytest.skip("C++ backend unavailable")
        backend = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
    else:
        from kicad_tools.router.pathfinder import Router

        backend = Router(grid, rules)
    result = backend.route(start, end, per_net_timeout=5)
    assert result and result.segments
    for segment in result.segments:
        valid, _, _ = grid.validate_segment_clearance(
            segment, exclude_net=1, exclude_refs={"U1", "J1"}
        )
        assert valid


def test_strict_floor_cannot_be_undercut_by_fine_pitch_or_component_overrides():
    rules = DesignRules(
        trace_clearance=0.15,
        strict_pad_clearance=True,
        fine_pitch_clearance=0.08,
        component_clearances={"U1": 0.10},
    )
    assert rules.get_clearance_for_component("U1", 0.5) == 0.15
    rules.component_clearances["U1"] = 0.20
    assert rules.get_clearance_for_component("U1", 0.5) == 0.20
    relaxed = replace(rules, strict_pad_clearance=False)
    assert CacheKey.compute("pcb", rules, 0.05) != CacheKey.compute("pcb", relaxed, 0.05)
    _, _, start, end, _ = fixture(True)
    assert SubProblemSignature.compute([start, end], rules) != SubProblemSignature.compute(
        [start, end], relaxed
    )


def test_strict_option_survives_main_cli_dispatch(tmp_path):
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb)")
    args = create_parser().parse_args(["route", str(path), "--strict-pad-clearance", "--dry-run"])
    with patch("kicad_tools.cli.route_cmd.main", return_value=0) as main:
        run_route_command(args)
    assert "--strict-pad-clearance" in main.call_args.args[0]


@pytest.mark.parametrize("backend_name", ["python", "cpp"])
def test_off_grid_qfp_escape_keeps_trace_radius_inside_seed_pad(backend_name):
    rules = DesignRules(
        grid_resolution=0.05, trace_width=0.15, trace_clearance=0.15, strict_pad_clearance=True
    )
    grid = RoutingGrid(width=10, height=10, rules=rules)
    start = Pad(
        x=5.0125,
        y=5.25,
        width=1.475,
        height=0.3,
        net=1,
        net_name="SIGNAL",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    neighbor = replace(start, y=4.75, net=2, net_name="NEIGHBOR", pin="2")
    end = replace(start, x=8, y=3, width=0.6, height=0.6, ref="J1")
    for pad in (start, neighbor, end):
        grid.add_pad(pad)
    if backend_name == "cpp":
        if not is_cpp_available():
            pytest.skip("C++ backend unavailable")
        backend = CppPathfinder(CppGrid.from_routing_grid(grid), rules)
        rejected = []
        validate = backend._validate_route_clearance

        def record_validation(*args, **kwargs):
            violation = validate(*args, **kwargs)
            if violation is not None:
                rejected.append(violation)
            return violation

        backend._validate_route_clearance = record_validation
    else:
        from kicad_tools.router.pathfinder import Router

        backend = Router(grid, rules)
    result = backend.route(start, end, per_net_timeout=5)
    assert result and result.segments
    if backend_name == "cpp":
        # Before the inset, all five native resumes repeat the same illegal
        # pad-edge tail before the small fixture eventually falls back.
        assert rejected == []
    for segment in result.segments:
        valid, _, _ = grid.validate_segment_clearance(
            segment, exclude_net=1, exclude_refs={"U1", "J1"}
        )
        assert valid
