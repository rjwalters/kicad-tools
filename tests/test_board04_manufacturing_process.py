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


def test_c10_drill_repair_keeps_both_layer_connections(process, repaired):
    import math

    from kicad_tools.sexp import parse_string

    doc = parse_file(repaired)
    doc.add(
        parse_string(
            '(via (at 136.45 82.70) (size 0.6) (drill 0.15) (layers "F.Cu" "B.Cu") '
            '(tenting (front yes) (back yes)) (net "OSC_IN"))'
        )
    )
    for layer in ("F.Cu", "B.Cu"):
        doc.add(
            parse_string(
                f"(segment (start 136.45 82.70) (end 136.45 83.0) "
                f'(width 0.2) (layer "{layer}") (net "OSC_IN"))'
            )
        )
    repaired.write_text(serialize_sexp(doc))
    with pytest.raises(ValueError, match="Via drill too close"):
        process.validate_process(repaired, check_native=False)
    assert process.repair(repaired) == 1
    pcb = process.validate_process(repaired, check_native=False)
    tails = [
        s
        for s in pcb.segments
        if math.dist(s.start, (17.95, 15.20)) < 1e-6 and math.dist(s.end, (17.95, 15.15)) < 1e-6
    ]
    assert {s.layer for s in tails} == {"F.Cu", "B.Cu"}
    assert process.repair(repaired) == 0


@pytest.mark.parametrize("alternative", [False, True])
def test_ground_stitch_trim_preserves_required_connections(process, tmp_path, alternative):
    # Exact bad stitch geometry, with two named ground terminals. Removing
    # the stitch is permitted only when an independent copper path exists.
    text = """(kicad_pcb (version 20240108) (generator "test")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "GND")
      (footprint "test" (layer "F.Cu") (at 33.25 26.16)
        (property "Reference" "U2")
        (pad "23" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "GND")))
      (footprint "test" (layer "B.Cu") (at 32.01 26.16)
        (property "Reference" "J1")
        (pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "B.Cu") (net 1 "GND")))
      (via (at 32.01 26.16) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
        (net 1 "GND") (uuid "10000000-0000-4000-8000-000000000001"))
      (segment (start 33.25 26.16) (end 32.01 26.16) (width 0.2) (layer "F.Cu")
        (net 1 "GND") (uuid "10000000-0000-4000-8000-000000000002"))
    """
    if alternative:
        text += """
          (via (at 33.25 27.5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
            (net 1 "GND") (uuid "10000000-0000-4000-8000-000000000003"))
          (segment (start 33.25 26.16) (end 33.25 27.5) (width 0.2)
            (layer "F.Cu") (net 1 "GND"))
          (segment (start 33.25 27.5) (end 32.01 26.16) (width 0.2)
            (layer "B.Cu") (net 1 "GND"))
        """
    path = tmp_path / "ground.kicad_pcb"
    path.write_text(text + ")")
    before = path.read_bytes()
    if alternative:
        assert process.trim_redundant_gnd_stitch(path) == 1
        assert process.trim_redundant_gnd_stitch(path) == 0
        assert "10000000-0000-4000-8000-000000000003" in path.read_text()
    else:
        with pytest.raises(ValueError, match="required for pad connectivity"):
            process.trim_redundant_gnd_stitch(path)
        assert path.read_bytes() == before


@pytest.mark.parametrize("disconnect", [False, True])
def test_changed_escape_requires_complete_physical_net(process, repaired, disconnect):
    doc = parse_file(repaired)
    old = (146.45, 89.25)  # Reviewed U2.6 outside-pad via in sheet coordinates.
    new = (146.50, 89.25)
    moved = 0
    for via in doc.find_children("via"):
        at = via.find_child("at")
        if abs(at.get_float(0) - old[0]) < 1e-6 and abs(at.get_float(1) - old[1]) < 1e-6:
            at.set_atom(0, new[0])
            moved += 1
    assert moved == 1
    for segment in doc.find_children("segment"):
        for key in ("start", "end"):
            point = segment.find_child(key)
            if abs(point.get_float(0) - old[0]) < 1e-6 and abs(point.get_float(1) - old[1]) < 1e-6:
                point.set_atom(0, new[0])
    if disconnect:
        doc.children = [
            n
            for n in doc.children
            if not (
                n.name == "segment"
                and n.find_child("net")
                and n.find_child("net").get_string(0) == "OSC_OUT"
            )
        ]
    repaired.write_text(serialize_sexp(doc))
    before = repaired.read_bytes()
    if disconnect:
        with pytest.raises(ValueError, match="Missing reviewed escape U2.6"):
            process.repair(repaired)
        assert repaired.read_bytes() == before
    else:
        assert process.repair(repaired) == 0
        process.validate_process(repaired, check_native=False)


@pytest.mark.parametrize("fault", [None, "open", "wrong_stack", "undersized_drill"])
def test_paid_route_gate_checks_actual_copper_and_process(repaired, fault):
    spec = importlib.util.spec_from_file_location("board04_recipe", BOARD / "generate_design.py")
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)
    doc = parse_file(repaired)
    if fault == "open":
        doc.children = [
            n
            for n in doc.children
            if not (
                n.name == "segment"
                and n.find_child("net")
                and n.find_child("net").get_string(0) == "OSC_OUT"
            )
        ]
    elif fault == "wrong_stack":
        from kicad_tools.sexp import parse_string

        doc.find_child("layers").add(parse_string('(2 "In1.Cu" signal)'))
    elif fault == "undersized_drill":
        doc.find_children("via")[0].find_child("drill").set_atom(0, 0.10)
    repaired.write_text(serialize_sexp(doc))
    assert recipe.paid_process_route_is_complete(repaired) is (fault is None)


def test_offpad_repair_rejects_blocked_bond_without_writing(process, repaired):
    """A reviewed destination is unsafe when a fresh route occupies it (#5439)."""
    import math

    from kicad_tools.sexp import parse_string

    doc = parse_file(repaired)
    old = (153.663, 87.25)
    preferred = (155.05, 87.25)
    via = next(
        v
        for v in doc.find_children("via")
        if math.dist(
            (v.find_child("at").get_float(0), v.find_child("at").get_float(1)),
            preferred,
        )
        < 1e-6
    )
    via.find_child("at").set_atom(0, old[0])
    via.find_child("at").set_atom(1, old[1])
    # Remove the previous repair's tail to reconstruct its source escape.
    for segment in list(doc.find_children("segment")):
        start, end = segment.find_child("start"), segment.find_child("end")
        if (
            math.dist((start.get_float(0), start.get_float(1)), old) < 1e-6
            and math.dist((end.get_float(0), end.get_float(1)), preferred) < 1e-6
        ):
            doc.children.remove(segment)
    doc.add(
        parse_string(
            "(segment (start 154.65 87.05) (end 155.45 87.05) "
            '(width 0.2) (layer "B.Cu") (net "SWCLK") '
            '(uuid "00000000-0000-4000-8000-000000005439"))'
        )
    )
    repaired.write_text(serialize_sexp(doc))
    original = repaired.read_bytes()
    with pytest.raises(ValueError, match="No clearance-safe off-pad bond for U2.35"):
        process.repair(repaired)
    assert repaired.read_bytes() == original


@pytest.mark.parametrize("existing_options", [False, True])
def test_late_process_failure_does_not_publish_board_or_options(
    process, repaired, existing_options
):
    """Invalid drill dimensions are detected after all repair/trim work."""
    doc = parse_file(repaired)
    doc.find_children("via")[0].find_child("size").set_atom(0, 0.29)
    repaired.write_text(serialize_sexp(doc))
    options = repaired.parent / "manufacturing-requirements.json"
    if existing_options:
        options.write_text('{"preserve": "original options"}\n')
    else:
        options.unlink()
    original = repaired.read_bytes()
    previous = options.read_bytes() if options.exists() else None
    with pytest.raises(ValueError, match="Via violates reviewed drilling dimensions"):
        process.repair(repaired)
    assert repaired.read_bytes() == original
    assert (options.read_bytes() if options.exists() else None) == previous


@pytest.fixture
def isolated_offpad_geometry(repaired):
    """Keep real Board04 pads/outline, with only the source GND escape."""
    from kicad_tools.schema.pcb import PCB

    doc = parse_file(repaired)
    source = next(
        node
        for node in doc.find_children("via")
        if abs(node.find_child("at").get_float(0) - 155.05) < 1e-6
        and abs(node.find_child("at").get_float(1) - 87.25) < 1e-6
    )
    source.find_child("at").set_atom(0, 153.663)
    doc.children = [
        node
        for node in doc.children
        if node.name not in {"segment", "arc", "via"} or node is source
    ]
    pcb = PCB(doc)
    for zone in pcb.zones:
        zone.filled_polygons.clear()
    return pcb, pcb.vias[0]


@pytest.mark.parametrize("layers", [["F.Cu"], ["F.Cu", "B.Cu"]])
def test_offpad_search_finds_legal_alternative(process, isolated_offpad_geometry, layers):
    from kicad_tools.cli import relocate_in_pad_vias as relocation
    from kicad_tools.schema.pcb import Segment

    pcb, via = isolated_offpad_geometry
    preferred = (36.55, 19.75)
    assert process.select_offpad_position(pcb, via, "U2.35", preferred, layers) == preferred
    foreign = next(number for number, net in pcb.nets.items() if net.name == "SWCLK")
    pcb.segments.append(
        Segment(
            start=(36.15, 19.55), end=(36.95, 19.55), width=0.2, layer="B.Cu", net_number=foreign
        )
    )
    target = process.select_offpad_position(pcb, via, "U2.35", preferred, layers)
    assert target != preferred
    assert relocation._check_stub_clearance(pcb, via, target, layers, 0.15, 0.127, 0.10) is None
    assert (
        relocation._check_clearance(
            pcb,
            via,
            *target,
            relocation._collect_smd_pads_by_net(pcb),
            relocation._collect_tht_pads(pcb),
            0.127,
            0.5,
            0.10,
        )
        is None
    )


def test_offpad_search_respects_via_keepout(process, isolated_offpad_geometry):
    from kicad_tools.schema.pcb import Zone, ZoneKeepout

    pcb, via = isolated_offpad_geometry
    pcb.zones.append(
        Zone(
            net_number=0,
            net_name="",
            layer="B.Cu",
            polygon=[(0, 0), (60, 0), (60, 50), (0, 50)],
            keepout=ZoneKeepout(vias_allowed=False),
        )
    )
    with pytest.raises(ValueError, match="No clearance-safe off-pad bond"):
        process.select_offpad_position(pcb, via, "U2.35", (36.55, 19.75), ["F.Cu"])


def test_alternative_repair_is_idempotent_before_refill(
    process, isolated_offpad_geometry, repaired, monkeypatch
):
    from kicad_tools.sexp import parse_string

    pcb, _ = isolated_offpad_geometry
    monkeypatch.setattr(process, "MOVES", [move for move in process.MOVES if move[0] == "U2.35"])
    monkeypatch.setattr(process, "INSET_ROUTE_MOVES", [])
    # This isolated source contains no NRST escape or its independent cleanup.
    monkeypatch.setattr(process, "trim_obsolete_nrst_tail", lambda _: 0)
    pcb._sexp.add(
        parse_string(
            "(segment (start 154.65 87.05) (end 155.45 87.05) "
            '(width 0.2) (layer "B.Cu") (net "SWCLK"))'
        )
    )
    repaired.write_text(serialize_sexp(pcb._sexp))
    assert process.repair(repaired) == 1
    first = repaired.read_bytes()
    assert process.repair(repaired) == 0
    assert repaired.read_bytes() == first
