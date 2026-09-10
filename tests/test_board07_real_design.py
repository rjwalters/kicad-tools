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
    address = validate.external_load("A0", 10.0, 2)
    assert address["estimated_external_load_pf"] < address["external_limit_pf"]
    data = validate.external_load("DQ0", 10.0, 2)
    assert data["receiver_allowance_pf"] == 10.0
    assert data["receiver_basis"] == "engineering reserve"


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
