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
