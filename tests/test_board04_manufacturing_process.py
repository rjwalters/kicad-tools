"""Real regulator pinout and paid through-drill process must stay consistent."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.lvs import compare_netlists
from kicad_tools.sexp import parse_file, serialize_sexp

BOARD = Path(__file__).resolve().parents[1] / "boards/04-stm32-devboard"


@pytest.fixture(scope="module")
def process():
    spec = importlib.util.spec_from_file_location(
        "board04_process", BOARD / "manufacturing_process.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repaired(tmp_path):
    for name in [
        "stm32_devboard_routed.kicad_pcb",
        "stm32_devboard_routed.kicad_pro",
        "stm32_devboard_routed.kicad_dru",
        "manufacturing-requirements.json",
    ]:
        shutil.copy2(BOARD / "output" / name, tmp_path / name)
    return tmp_path / "stm32_devboard_routed.kicad_pcb"


def test_actual_regulator_pinout_and_schematic_agree():
    pcb = BOARD / "output/stm32_devboard_routed.kicad_pcb"
    doc = parse_file(pcb)
    for fp in doc.find_children("footprint"):
        props = {p.get_string(0): p.get_string(1) for p in fp.find_children("property")}
        if props.get("Reference") == "U1":
            assert props["Value"] == "MCP1825S-3302E/DB"
            pins = {
                p.get_string(0): p.find_child("net").get_string(0) for p in fp.find_children("pad")
            }
            assert pins == {"1": "+5V", "2": "GND", "3": "+3.3V"}
            break
    else:
        pytest.fail("Missing regulator")
    result = compare_netlists(BOARD / "output/stm32_devboard.kicad_sch", pcb)
    assert result.clean, result.mismatches


def test_repaired_process_is_idempotent_and_shared_profile_unchanged(process, repaired):
    from kicad_tools.manufacturers import get_profile

    before = get_profile("jlcpcb-tier1").get_design_rules(2)
    assert process.repair(repaired) == 0
    process.apply_native_floors(repaired)
    checker = process.make_checker(repaired)
    assert checker.design_rules.min_via_drill_mm == 0.15
    assert checker.design_rules.min_annular_ring_mm == 0.075
    assert not checker.design_rules.via_in_pad_supported
    assert get_profile("jlcpcb-tier1").get_design_rules(2) == before


def test_inset_route_c11_via_repair_clears_land_and_keeps_both_layer_tails(process, repaired):
    import math

    from kicad_tools.sexp import parse_string

    doc = parse_file(repaired)
    # Fresh inset-seed route: a 0.30 mm drill sits only 0.05 mm from C11.1.
    # Coordinates are in the source sheet frame (board origin 118.5, 67.5).
    doc.add(
        parse_string(
            '(via (at 143.2 83.25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(tenting (front yes) (back yes)) (net "OSC_OUT"))'
        )
    )
    for layer in ("F.Cu", "B.Cu"):
        doc.add(
            parse_string(
                "(segment (start 143.2 83.25) (end 143.3 83.15) "
                f'(width 0.2) (layer "{layer}") (net "OSC_OUT"))'
            )
        )
    repaired.write_text(serialize_sexp(doc))
    with pytest.raises(ValueError, match="Via drill too close"):
        process.validate_process(repaired, check_native=False)

    assert process.repair(repaired) == 1
    pcb = process.validate_process(repaired, check_native=False)
    assert any(math.dist(v.position, (24.8, 15.75)) < 1e-6 for v in pcb.vias)
    tails = [
        s
        for s in pcb.segments
        if math.dist(s.start, (24.7, 15.75)) < 1e-6 and math.dist(s.end, (24.8, 15.75)) < 1e-6
    ]
    assert {s.layer for s in tails} == {"F.Cu", "B.Cu"}
    assert all(s.net_name == "OSC_OUT" for s in tails)
    assert process.repair(repaired) == 0


@pytest.mark.parametrize("change", ["micro", "partial_hole", "native_floor", "regulator"])
def test_process_rejects_invalid_geometry_or_rules(process, repaired, change):
    doc = parse_file(repaired)
    if change == "native_floor":
        p = repaired.with_suffix(".kicad_pro")
        data = json.loads(p.read_text())
        data["board"]["design_settings"]["rules"]["min_via_hole"] = 0.3
        p.write_text(json.dumps(data))
    elif change == "micro":
        from kicad_tools.sexp import parse_string

        via = doc.find_children("via")[0]
        via.children.insert(0, parse_string("(via micro)").children[0])
        repaired.write_text(serialize_sexp(doc))
    elif change == "partial_hole":
        via = next(
            v
            for v in doc.find_children("via")
            if abs(v.find_child("at").get_float(0) - 128.74) < 0.001
        )
        via.find_child("at").set_atom(1, 79.11)  # Original U1 tab edge cut, not fully contained.
        repaired.write_text(serialize_sexp(doc))
    else:
        for fp in doc.find_children("footprint"):
            props = {p.get_string(0): p for p in fp.find_children("property")}
            if props.get("Reference") and props["Reference"].get_string(1) == "U1":
                props["Value"].set_atom(1, "AMS1117-3.3")
        repaired.write_text(serialize_sexp(doc))
    with pytest.raises(ValueError):
        process.make_checker(repaired)


@pytest.mark.parametrize("bonded", [False, True])
def test_obsolete_back_escape_trim_requires_free_endpoint(process, repaired, bonded):
    from kicad_tools.sexp import parse_string
    from kicad_tools.validate.rules.dangling_copper import DanglingCopperRule

    doc = parse_file(repaired)
    # Linux router variant retains this B.Cu copy of the SMT pad escape.
    tail_id = "00000000-0000-4000-8000-000000005044"
    doc.add(
        parse_string(
            f"(segment (start 145.3375 89.75) (end 145.8375 89.75) "
            f'(width 0.15) (layer "B.Cu") (net "NRST") (uuid "{tail_id}"))'
        )
    )
    if bonded:
        # A second branch terminates the endpoint: this is real route copper,
        # even though its position matches the historical dangling remnant.
        doc.add(
            parse_string(
                "(segment (start 145.3375 89.75) (end 149.8875 93.8) "
                '(width 0.15) (layer "B.Cu") (net "NRST") '
                '(uuid "00000000-0000-4000-8000-000000005045"))'
            )
        )
    repaired.write_text(serialize_sexp(doc))
    before = process.validate_process(repaired)
    findings = DanglingCopperRule().check(before, process.process_rules()).violations
    assert any(v.rule_id == "track_dangling" and "NRST" in v.nets for v in findings) == (not bonded)
    assert process.trim_obsolete_nrst_tail(repaired) == (0 if bonded else 1)
    after = process.validate_process(repaired)
    assert any(s.uuid == tail_id for s in after.segments) == bonded
    assert not any(
        v.rule_id == "track_dangling" and "NRST" in v.nets
        for v in DanglingCopperRule().check(after, process.process_rules()).violations
    )
    assert process.trim_obsolete_nrst_tail(repaired) == 0
