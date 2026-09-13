"""Electrical invariants and timing-budget failures for the real SDRAM demo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "boards/07-matchgroup-test/real_design"


def load(name):
    spec = importlib.util.spec_from_file_location(f"board07_real_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reviewed_project_keeps_clearance_and_via_floors_when_emitting_factory_rules():
    import json

    from kicad_tools.manufacturers import get_profile
    from kicad_tools.manufacturers.project_generator import merge_project_rules

    project = json.loads((ROOT / "authored-source/sdram_demo.kicad_pro").read_text())
    assert project["text_variables"]["KCT_PRESERVE_BOARD_RULES"] == "1"
    merge_project_rules(project, get_profile("jlcpcb").get_design_rules(layers=6))
    rules = project["board"]["design_settings"]["rules"]
    default = next(c for c in project["net_settings"]["classes"] if c["name"] == "Default")
    assert rules["min_clearance"] >= 0.15
    assert rules["min_via_diameter"] >= 0.5
    assert rules["min_through_hole_diameter"] >= 0.2
    assert default["clearance"] >= 0.15
    assert default["via_diameter"] >= 0.5
    assert default["via_drill"] >= 0.2


def test_clock_relative_budget_cannot_hide_behind_individually_matched_groups():
    validate = load("validate")
    constraints = {
        "groups": {"data": ["DQ0", "DQ1"], "command": ["SDCLK", "SDNWE"]},
        "group_skew_mm": 5,
        "clock_reference": "SDCLK",
        "clock_relative_tolerance_mm": 10,
        "max_trace_length_mm": 120,
    }
    errors = validate.timing_errors({"DQ0": 50, "DQ1": 52, "SDCLK": 30, "SDNWE": 31}, constraints)
    assert len(errors) == 2
    assert all("clock-relative" in error for error in errors)
    assert (
        validate.timing_errors({"DQ0": 35, "DQ1": 37, "SDCLK": 30, "SDNWE": 31}, constraints) == []
    )


def test_absent_clock_or_data_never_passes_timing():
    validate = load("validate")
    constraints = {
        "groups": {"bus": ["DQ0", "SDCLK"]},
        "group_skew_mm": 5,
        "clock_reference": "SDCLK",
        "clock_relative_tolerance_mm": 10,
        "max_trace_length_mm": 120,
    }
    assert validate.timing_errors({"DQ0": 20}, constraints) == ["Missing routed length: SDCLK"]


def test_inner_layer_transition_does_not_count_unused_through_barrel_as_series_length():
    from types import SimpleNamespace

    import pytest

    validate = load("validate")
    via = SimpleNamespace(position=(1, 0), size=0.5)
    segments = [
        SimpleNamespace(start=(0, 0), end=(1, 0), layer="F.Cu", width=0.18),
        SimpleNamespace(start=(1, 0), end=(2, 0), layer="In2.Cu", width=0.18),
        SimpleNamespace(start=(1, 2), end=(2, 2), layer="B.Cu", width=0.18),
    ]
    elevations = {"F.Cu": 0.0175, "In2.Cu": 0.3042, "B.Cu": 1.575}
    assert validate.via_travel_mm(via, segments, elevations) == pytest.approx(0.2867)
    segments.append(SimpleNamespace(start=(1, 0), end=(1, 2), layer="B.Cu", width=0.18))
    assert validate.via_travel_mm(via, segments, elevations) == pytest.approx(1.5575)


def test_clock_load_uses_stricter_driver_limit_and_counts_unused_via_barrels():
    validate = load("validate")
    clock = validate.external_load("SDCLK", 10.0, 2)
    assert clock["estimated_external_load_pf"] == 15.5
    assert clock["external_limit_pf"] == 15.0
    assert clock["estimated_external_load_pf"] > clock["external_limit_pf"]
    assert clock["via_basis"] == "1 pF/via allowance (geometry unavailable)"
    address = validate.external_load("A0", 10.0, 2)
    assert address["estimated_external_load_pf"] < address["external_limit_pf"]
    data = validate.external_load("DQ0", 10.0, 2)
    assert data["receiver_allowance_pf"] == 10.0
    assert data["receiver_basis"] == "engineering reserve"


def test_dq_load_evaluates_both_receiver_directions_explicitly():
    """#5134: DQ must not fold both directions into a single opaque number."""
    validate = load("validate")
    data = validate.external_load("DQ0", 10.0, 2)
    directions = data["directions"]
    assert set(directions) == {"SDRAM drives, MCU receives", "MCU drives, SDRAM receives"}
    mcu_receives = directions["SDRAM drives, MCU receives"]
    sdram_receives = directions["MCU drives, SDRAM receives"]
    # The two directions use different, independently sourced receiver
    # allowances -- they would diverge today if the two directions were ever
    # given asymmetric net-level limits instead of sharing one 30 pF budget.
    assert mcu_receives["receiver_allowance_pf"] == 10.0
    assert mcu_receives["receiver_basis"] == "engineering reserve"
    assert sdram_receives["receiver_allowance_pf"] == 6.5
    assert sdram_receives["receiver_basis"] == "ISSI maximum"
    assert mcu_receives["estimated_external_load_pf"] > sdram_receives["estimated_external_load_pf"]
    # The net-level fields still report the governing (worse) direction, so
    # existing single-number consumers keep working unchanged.
    assert data["worst_direction"] == "SDRAM drives, MCU receives"
    assert data["estimated_external_load_pf"] == mcu_receives["estimated_external_load_pf"]
    # A single-direction net (clock, address/control) has exactly one entry.
    clock = validate.external_load("SDCLK", 10.0, 2)
    assert list(clock["directions"]) == ["MCU drives, SDRAM receives (clock)"]
    address = validate.external_load("A0", 10.0, 2)
    assert list(address["directions"]) == ["MCU drives, SDRAM receives"]


def test_via_barrel_capacitance_matches_hand_computed_slyt335_value():
    """Board07's actual .5/.8 mm pad/antipad, 1.6 mm, Er4.6 construction."""
    import pytest

    validate = load("validate")
    # load-review.md "Via allowance and implementation recommendation":
    # er4.6, .5mm pad, .8mm antipad (.15mm edge clearance) -> ~0.681 pF.
    capacitance = validate.via_barrel_capacitance_pf(
        pad_diameter_mm=0.5,
        antipad_diameter_mm=0.8,
        board_thickness_mm=1.6,
        epsilon_r=4.6,
    )
    assert capacitance == pytest.approx(0.681, abs=0.001)
    # The flat 1 pF/via allowance is conservative relative to the measured value.
    assert capacitance < 1.0


def test_via_barrel_capacitance_falls_back_to_none_on_degenerate_geometry():
    validate = load("validate")
    # Antipad at or inside the pad (no clearance gap) cannot be evaluated.
    assert (
        validate.via_barrel_capacitance_pf(
            pad_diameter_mm=0.5, antipad_diameter_mm=0.5, board_thickness_mm=1.6, epsilon_r=4.6
        )
        is None
    )
    assert (
        validate.via_barrel_capacitance_pf(
            pad_diameter_mm=0.0, antipad_diameter_mm=0.8, board_thickness_mm=1.6, epsilon_r=4.6
        )
        is None
    )
    assert (
        validate.via_barrel_capacitance_pf(
            pad_diameter_mm=0.5, antipad_diameter_mm=0.8, board_thickness_mm=1.6, epsilon_r=0.0
        )
        is None
    )


def test_stackup_average_epsilon_r_weights_by_dielectric_thickness():
    from types import SimpleNamespace as NS

    import pytest

    validate = load("validate")
    stack = NS(
        has_explicit_data=True,
        layers=[
            NS(is_copper=True, is_dielectric=False, thickness_mm=0.035, epsilon_r=0),
            NS(is_copper=False, is_dielectric=True, thickness_mm=1.0, epsilon_r=4.0),
            NS(is_copper=False, is_dielectric=True, thickness_mm=3.0, epsilon_r=5.0),
            NS(is_copper=True, is_dielectric=False, thickness_mm=0.035, epsilon_r=0),
        ],
    )
    # (1.0*4.0 + 3.0*5.0) / 4.0 = 4.75
    assert validate.stackup_average_epsilon_r(stack) == pytest.approx(4.75)
    assert validate.stackup_average_epsilon_r(NS(has_explicit_data=True, layers=[])) is None


def _filled_antipad_board(radius=0.4, *, topology="circle"):
    """Real PCB parser with native-style bridged filled-polygon holes."""
    import math

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_string

    header = "(kicad_pcb (version 20241229) (generator test) "
    header += '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) '
    header += '(8 "In3.Cu" signal) (10 "In4.Cu" signal) (31 "B.Cu" signal)) '
    header += '(net 1 "SIG") (net 2 "GND") (net 3 "+3V3") '
    header += '(via (at 0 0) (size .5) (drill .2) (layers "F.Cu" "B.Cu") (net 1)) '
    for layer in ("In1.Cu", "In4.Cu"):
        ring = [
            (radius * math.cos(-i * math.tau / 128), radius * math.sin(-i * math.tau / 128))
            for i in range(129)
        ]
        if topology == "slot":
            ring = [(x * 2, y) for x, y in ring]
        outer = [(2, 0), (2, 2), (-2, 2), (-2, -2), (2, -2), (2, 0)]
        points = outer + [ring[0]] + ring[1:] + [ring[0], outer[0]]
        if topology == "edge":
            points = [(radius, -2), (2, -2), (2, 2), (radius, 2)]
        pts = " ".join(f"(xy {x} {y})" for x, y in points)
        fill = f'(filled_polygon (layer "{layer}") (pts {pts}))'
        if topology == "unfilled":
            fill = ""
        net_id, net_name = (2, "GND") if layer == "In1.Cu" else (3, "+3V3")
        header += f'(zone (net {net_id}) (net_name "{net_name}") (layer "{layer}") {fill}) '
    return PCB(parse_string(header + ")"))


def test_measured_antipads_follow_filled_copper_not_project_rule(tmp_path):
    import json

    import pytest

    validate = load("validate")
    project = tmp_path / "board.kicad_pro"
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.15}}}})
    )
    original = project.read_bytes()
    values = []
    for radius in (0.4, 0.377, 0.5):
        board = _filled_antipad_board(radius)
        planes = validate.board_reference_copper(board)
        values.append(validate.net_via_capacitance_pf(board.vias, planes, 1.6, 4.6))
        assert validate.measured_antipad_diameter_mm(board.vias[0], planes) == pytest.approx(
            2 * radius, abs=0.001
        )
    assert values[0] == pytest.approx(0.681, abs=0.002)
    assert values[1] > values[0] > values[2]
    assert project.read_bytes() == original


def test_net_via_capacitance_rejects_unknown_topology_for_whole_net():
    validate = load("validate")
    for topology in ("slot", "edge", "unfilled"):
        board = _filled_antipad_board(topology=topology)
        assert (
            validate.net_via_capacitance_pf(
                board.vias, validate.board_reference_copper(board), 1.6, 4.6
            )
            is None
        )
    board = _filled_antipad_board()
    planes = validate.board_reference_copper(board)
    assert validate.net_via_capacitance_pf(board.vias, planes, 1.6, None) is None
    assert validate.net_via_capacitance_pf([], planes, 1.6, 4.6) is None
    board.vias[0].via_type = "blind"
    assert validate.net_via_capacitance_pf(board.vias, planes, 1.6, 4.6) is None


def test_stackup_parser_defaults_and_partial_material_do_not_become_measurements():
    from kicad_tools.physics import Stackup
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_string

    validate = load("validate")
    header = '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) '
    default = Stackup.from_pcb(PCB(parse_string(header + ")")))
    assert not default.has_explicit_data
    assert validate.stackup_average_epsilon_r(default) is None
    for er in ("", "(epsilon_r nan)", "(epsilon_r 0)"):
        setup = '(setup (stackup (layer "F.Cu" (type "copper") (thickness .035)) '
        setup += '(layer "dielectric 1" (type "core") (thickness .7) (epsilon_r 4.6)) '
        setup += f'(layer "dielectric 2" (type "prepreg") (thickness .83) {er}) '
        setup += '(layer "B.Cu" (type "copper") (thickness .035))))'
        stack = Stackup.from_pcb(PCB(parse_string(header + setup + ")")))
        assert stack.has_explicit_data
        assert validate.stackup_average_epsilon_r(stack) is None
    # The reproduced near-limit clock load must retain its failing fallback.
    load_result = validate.external_load("SDCLK", 10.8, 1)
    assert load_result["estimated_external_load_pf"] > load_result["external_limit_pf"]


def test_external_load_uses_measured_via_capacitance_when_provided():
    import pytest

    validate = load("validate")
    measured = validate.external_load("A0", 10.0, 2, via_capacitance_pf=1.362)
    assert measured["via_allowance_pf"] == pytest.approx(1.362)
    assert measured["via_basis"] == "TI SLYT335 geometry (measured pad/antipad/stackup)"
    assert measured["estimated_external_load_pf"] == pytest.approx(10.0 + 1.362 + 3.8)
    fallback = validate.external_load("A0", 10.0, 2, via_capacitance_pf=None)
    assert fallback["via_allowance_pf"] == 2.0
    assert fallback["via_basis"] == "1 pF/via allowance (geometry unavailable)"


def test_preexport_gate_never_accepts_missing_design_checks_or_existing_bad_bundle():
    import copy

    validate = load("validate")
    report = {
        "summary": {"errors": 0, "warnings": 0},
        "meta_checks": {
            **{key: {"status": "PASSED"} for key in ("drc", "erc", "lvs")},
            "manifest": {"status": "NOT RUN"},
        },
    }
    assert validate.kct_design_gate_passes(report, 2, manifest_present=False)
    assert not validate.kct_design_gate_passes(report, 2, manifest_present=True)
    assert not validate.kct_design_gate_passes(report, 1, manifest_present=False)
    for key in ("drc", "erc", "lvs", "manifest"):
        failed = copy.deepcopy(report)
        failed["meta_checks"][key]["status"] = "FAILED"
        assert not validate.kct_design_gate_passes(failed, 2, manifest_present=False)
        assert not validate.kct_design_gate_passes(failed, 0, manifest_present=True)
    report["summary"]["warnings"] = 1
    assert not validate.kct_design_gate_passes(report, 2, manifest_present=False)


def test_real_memory_and_core_regulator_have_separate_supply_domains():
    generate = load("generate")
    parts = {part.ref: part for part in generate.build_parts()}
    # ISSI's chip select, clock enable and byte masks must reach the correct
    # FMC bank2 pins; wrong bank1 wiring cannot be rescued by length matching.
    assert parts["U2"].pins["19"] == "SDNE1"
    assert parts["U2"].pins["37"] == "SDCKE1"
    assert parts["U2"].pins["15"] == "LDQM"
    assert parts["U2"].pins["39"] == "UDQM"
    assert "36" not in parts["U2"].pins and "40" not in parts["U2"].pins
    for pin, name in (("71", "VCAP1"), ("106", "VCAP2")):
        assert parts["U1"].pins[pin] == name
        attached = [p for p in parts.values() if name in p.pins.values()]
        assert len(attached) == 2
        capacitor = next(p for p in attached if p.ref.startswith("C"))
        assert capacitor.value == "2.2uF"
        assert capacitor.pins == {"1": name, "2": "GND"}
    assert all(p.mpn and p.lcsc for p in parts.values())
    for ref in ("C29", "C30"):
        assert parts[ref].value == "10nF"
        assert parts[ref].pins == {"1": "+3V3", "2": "GND"}
        assert parts[ref].mpn == "CL10B103KB8NNNC"


def test_generated_outline_reaches_router_without_silent_hat_fallback(tmp_path):
    from kicad_tools.router import DesignRules
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.schema.pcb import PCB

    generate = load("generate")
    generate.generate(tmp_path)
    path = tmp_path / "sdram_demo.kicad_pcb"
    router, _ = load_pcb_for_routing(
        path,
        rules=DesignRules(grid_resolution=0.5, trace_clearance=1.0),
        validate_drc=False,
    )
    assert (router.grid.width, router.grid.height) == (100, 80)
    # Verify that assigned library pads survive serialization as actual nets.
    board = PCB.load(path)
    for ref, number in (("U2", "38"), ("U1", "93")):
        pad = next(p for p in board.get_footprint(ref).pads if p.number == number)
        assert pad.net_name == "SDCLK"


def test_routing_port_substitution_cannot_pass_physical_geometry_gate(tmp_path):
    from kicad_tools.schema.pcb import PCB

    generate, validate = load("generate"), load("validate")
    generate.generate(tmp_path)
    source = PCB.load(tmp_path / "sdram_demo.kicad_pcb")
    candidate = PCB.load(tmp_path / "sdram_demo.kicad_pcb")
    pad = next(p for p in candidate.get_footprint("U1").pads if p.net_name == "DQ1")
    original_net = pad.net_name
    pad.position = (pad.position[0] + 1.25, pad.position[1])
    assert pad.net_name == original_net
    assert validate.physical_pad_geometry(candidate) != validate.physical_pad_geometry(source)


def test_external_load_gate_and_report_scope(tmp_path, monkeypatch):
    """Exercise check() decisions with synthetic measurements, not native evidence."""
    import json
    from types import SimpleNamespace as NS

    import pytest

    validate = load("validate")
    source = tmp_path / "source"
    source.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_sch"):
        (source / f"sdram_demo{suffix}").write_text("synthetic test source")
    (source / "circuit.json").write_text('{"pin_nets": {}}')
    segment = NS(layer="In3.Cu", width=0.2, start=(0, 0), end=(105, 0), uuid="trace", net_name="A0")
    board = NS(
        footprints=[],
        zones=[],
        vias=[],
        _sexp=NS(find_children=lambda _: []),
        segments=[segment],
        get_net_by_name=lambda _: NS(number=1),
        segments_in_net=lambda _: [segment],
        vias_in_net=lambda _: [],
    )
    monkeypatch.setattr(validate.PCB, "load", lambda _: board)
    monkeypatch.setattr(
        validate, "NetStatusAnalyzer", lambda *a, **k: NS(analyze=lambda: NS(nets=[]))
    )
    monkeypatch.setattr(
        validate,
        "TraceLengthAnalyzer",
        lambda: NS(analyze_net=lambda *a: NS(segment_count=1, total_length_mm=105, via_count=2)),
    )
    stack = NS(
        has_explicit_data=False,
        layers=[NS(name="In3.Cu", is_signal_layer=True)],
        is_outer_layer=lambda _: False,
    )
    monkeypatch.setattr(validate.Stackup, "from_pcb", lambda _: stack)
    monkeypatch.setattr(validate, "copper_elevations", lambda _: {})
    monkeypatch.setattr(
        validate,
        "TransmissionLine",
        lambda _: NS(stripline=lambda **k: NS(z0=50, phase_velocity=1e8)),
    )
    monkeypatch.setattr(validate, "timing_errors", lambda *a: [])
    monkeypatch.setattr(validate, "write_lvs_report", lambda *a, **k: (True, True))

    def emit_rules(path, *args, **kwargs):
        path.with_suffix(".kicad_dru").write_text("synthetic test rules")

    monkeypatch.setattr(validate, "write_drc_constraints", emit_rules)

    def fake_run(args, **kwargs):
        if args[0] == "kct":
            output = Path(args[args.index("--output") + 1])
            report = {
                "summary": {"errors": 0, "warnings": 0},
                "meta_checks": {
                    **{key: {"status": "PASSED"} for key in ("drc", "erc", "lvs")},
                    "manifest": {"status": "NOT RUN"},
                },
            }
        elif args[0] == "kicad-cli":
            output = Path(args[args.index("-o") + 1])
            report = {"violations": [], "unconnected_items": [], "sheets": []}
        else:
            output = Path(args[-1])
            report = {"findings": []}
        output.write_text(json.dumps(report))
        return NS(returncode=2 if args[0] == "kct" else 0)

    monkeypatch.setattr(validate.subprocess, "run", fake_run)
    for name, trace_pf, expected in (
        ("A0", 21, True),
        ("A0", 27, False),
        ("SDCLK", 10, False),
        ("DQ0", 21, False),
        ("DQ0", 13, False),  # 25pF passes 30pF, but 21.5pF fails 20pF.
    ):
        (source / "sdram_constraints.json").write_text(json.dumps({"groups": {"bus": [name]}}))
        segment.end = (trace_pf * 5, 0)  # C'=0.2 pF/mm from the stubbed line model.
        segment.net_name = name
        segment.layer = "In2.Cu" if name.startswith("DQ") else "In3.Cu"
        stack.layers = [NS(name=segment.layer, is_signal_layer=True)]
        if name == "DQ0" and trace_pf == 13:
            monkeypatch.setattr(
                validate,
                "DQ_DRIVER_LIMITS",
                {
                    "SDRAM drives, MCU receives": (30.0, "synthetic engineering budget"),
                    "MCU drives, SDRAM receives": (20.0, "synthetic engineering budget"),
                },
            )
        evidence = tmp_path / f"{name}-{trace_pf}"
        assert validate.check(source / "sdram_demo.kicad_pcb", source, evidence) is expected
        report = json.loads((evidence / "validation.json").read_text())
        assert report["trace_capacitance_pf"][name] == pytest.approx(trace_pf)
        estimate = report["external_load_estimates"][name]
        assert estimate["via_allowance_pf"] == 2
        assert estimate["passes"] is expected
        if name == "DQ0" and trace_pf == 13:
            directions = estimate["directions"]
            assert directions["SDRAM drives, MCU receives"]["passes"] is True
            assert directions["MCU drives, SDRAM receives"]["passes"] is False
            assert estimate["worst_direction"] == "MCU drives, SDRAM receives"
            assert estimate["margin_pf"] == pytest.approx(-1.5)
            assert estimate["limit_basis"] == "synthetic engineering budget"
        # No via geometry is reachable from this synthetic board (an empty
        # via list can never match the stubbed via_count=2), so every net
        # must fall back to the flat allowance rather than silently using a
        # partial/mismatched measurement.
        assert estimate["via_basis"] == "1 pF/via allowance (geometry unavailable)"
        assert (estimate["estimated_external_load_pf"] <= estimate["external_limit_pf"]) is expected
        if expected:
            assert estimate["estimated_external_load_pf"] == pytest.approx(26.8)
            assert report["errors"] == []
        else:
            assert len(report["errors"]) == 1
            assert "external load" in report["errors"][0]
        assert "trace capacitance" not in " ".join(report["errors"])
        scope = report["capacitance_scope"]
        assert "receiver" in scope and "full-via" in scope
        assert "engineering reserve" in scope
        assert "geometry-derived" in scope.lower() and "MCU" in scope and "#5134" in scope
        assert report["hardware_tested"] is False
        assert report["manufacturing_complete"] is False


def test_committed_stackup_surface_defaults_do_not_mask_barrel_material():
    from kicad_tools.physics import Stackup
    from kicad_tools.schema.pcb import PCB

    validate = load("validate")
    board = PCB.load(ROOT / "reviewed-routing/sdram_demo.kicad_pcb")
    stack = Stackup.from_pcb(board)
    assert 4.16 < validate.stackup_average_epsilon_r(stack) < 4.6
    planes = validate.board_reference_copper(board)
    # Existing frozen fill, no native refill or route invoked by this test.
    assert any(validate.measured_antipad_diameter_mm(via, planes) is not None for via in board.vias)
    for layer in stack.layers:
        if layer.name == "dielectric 1":
            layer.thickness_mm = float("nan")
    assert validate.stackup_average_epsilon_r(stack) is None


def test_antipad_copper_island_and_one_unknown_via_disable_whole_net():
    import copy

    from shapely.geometry import Point
    from shapely.ops import unary_union

    validate = load("validate")
    board = _filled_antipad_board()
    planes = validate.board_reference_copper(board)
    other = copy.copy(board.vias[0])
    other.position = (5, 5)
    assert validate.net_via_capacitance_pf([board.vias[0], other], planes, 1.6, 4.6) is None
    planes["In1.Cu"] = unary_union([planes["In1.Cu"], Point(0.32, 0).buffer(0.02)])
    assert validate.net_via_capacitance_pf(board.vias, planes, 1.6, 4.6) is None


def test_via_formula_rejects_nonfinite_inputs():
    validate = load("validate")
    for index in range(4):
        for value in (float("nan"), float("inf")):
            inputs = [0.5, 0.8, 1.6, 4.6]
            inputs[index] = value
            assert validate.via_barrel_capacitance_pf(*inputs) is None


def test_dq_asymmetric_driver_budgets():
    validate = load("validate")
    report = validate.external_load(
        "DQ0",
        15.0,
        0,
        dq_driver_limits={
            "SDRAM drives, MCU receives": (30.0, "synthetic engineering budget"),
            "MCU drives, SDRAM receives": (20.0, "synthetic engineering budget"),
        },
    )
    assert report["directions"]["SDRAM drives, MCU receives"]["estimated_external_load_pf"] == 25
    assert report["directions"]["SDRAM drives, MCU receives"]["passes"] is True
    assert report["directions"]["MCU drives, SDRAM receives"]["estimated_external_load_pf"] == 21.5
    assert report["passes"] is False
    assert report["external_limit_pf"] == 20
    assert report["margin_pf"] == -1.5
