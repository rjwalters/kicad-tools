"""Authored reference labels must not alias distinct physical terminals (#5418)."""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.schema.pcb import PCB
from tests.test_routing_placement_disposition import board_text


def repeated_reference_board(*, invalid, style):
    text = board_text(invalid=invalid)
    for ref in ("X1", "R1"):
        original = f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))'
        if style == "missing":
            replacement = ""
        else:
            label = "" if style == "empty" else 'DUP\\"label' if style == "escaped" else "DUP"
            replacement = f'(property "Reference" "{label}" (at 0 0) (layer "F.SilkS"))'
        text = text.replace(original, replacement)
    return text


@pytest.mark.parametrize("style", ["missing", "empty", "duplicate", "escaped"])
def test_only_the_physically_offboard_footprint_excludes_its_nets(tmp_path, style):
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(repeated_reference_board(invalid=True, style=style))
    original = board.read_bytes()
    result = analyze_routing_placement(board, coupled_groups=[("BAD", "PARTNER")])
    assert result.direct_invalid_nets == {"BAD"}
    assert result.coupled_invalid_nets == {"PARTNER"}
    assert "GOOD" in result.eligible_nets
    assert board.read_bytes() == original


@pytest.mark.parametrize("style", ["missing", "empty", "duplicate", "escaped"])
def test_every_topology_entry_resolves_to_its_own_physical_net(tmp_path, style):
    board = tmp_path / "valid.kicad_pcb"
    board.write_text(repeated_reference_board(invalid=False, style=style))
    original = board.read_bytes()
    router, nets = load_pcb_for_routing(str(board), force_python=True)
    assert len(router.all_pads) == 8
    assert len(router.pads) == 8
    for net, terminals in router.nets.items():
        assert all(router.pads[key].net == net for key in terminals)
    assert set(router.nets[nets["BAD"]]).isdisjoint(router.nets[nets["GOOD"]])
    assert board.read_bytes() == original


@pytest.mark.parametrize("style", ["missing", "empty", "duplicate", "escaped"])
def test_default_mixed_cli_routes_only_valid_physical_nets(
    tmp_path, style, options=(), expected_exit=2
):
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(repeated_reference_board(invalid=True, style=style))
    original = board.read_bytes()
    output, report = tmp_path / "routed.kicad_pcb", tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(board),
            "-o",
            str(output),
            "--timeout",
            "30",
            "--complete-report",
            str(report),
            *options,
        ],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert board.read_bytes() == original
    before, after = PCB.load(board), PCB.load(output)
    assert any(s.net_name == "GOOD" and s.start != s.end for s in after.segments)

    def membership(pcb):
        return Counter((f.reference, p.number, p.net_name) for f in pcb.footprints for p in f.pads)

    def bad_copper(pcb):
        return Counter(
            (s.start, s.end, s.width, s.layer) for s in pcb.segments if s.net_name == "BAD"
        )

    assert membership(after) == membership(before)
    assert bad_copper(after) == bad_copper(before)
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["direct_invalid_nets"] == ["BAD"]


@pytest.mark.parametrize("style", ["missing", "empty", "duplicate", "escaped"])
def test_ambiguous_netlist_selector_rejected_by_preflight_and_loader(tmp_path, style):
    board = tmp_path / "valid.kicad_pcb"
    board.write_text(repeated_reference_board(invalid=False, style=style))
    ref = PCB.load(board).footprints[0].reference
    overrides = {f"{ref}.1": "RENAMED"}
    for operation in (analyze_routing_placement, load_pcb_for_routing):
        with pytest.raises(ValueError, match="Ambiguous netlist terminal"):
            operation(board, netlist=overrides)


@pytest.mark.parametrize("style", ["missing", "empty", "duplicate", "escaped"])
@pytest.mark.parametrize("move_protected", [False, True])
def test_repair_guard_tracks_physical_footprint_with_duplicate_labels(
    tmp_path, style, move_protected
):
    from kicad_tools.cli.route_fixed_repair import repair_fixed_copper

    board = tmp_path / "partial.kicad_pcb"
    original = repeated_reference_board(invalid=True, style=style)
    board.write_text(original)
    disposition = analyze_routing_placement(board)

    def repair(candidate):
        text = candidate.read_text()
        old, new = (
            ("(at 125 105)", "(at 124 105)") if move_protected else ("(at 105 107)", "(at 106 107)")
        )
        assert old in text
        candidate.write_text(text.replace(old, new))
        return 0

    code, error = repair_fixed_copper(board, disposition, repair)
    if move_protected:
        assert code == 3 and error
        assert board.read_text() == original
    else:
        assert code == 0 and error is None
        assert board.read_text() != original


@pytest.mark.parametrize("style", ["missing", "empty", "duplicate", "escaped"])
def test_classifier_preserves_every_physical_component(tmp_path, style):
    from kicad_tools.router.stuck_classifier import _iter_board_pads

    board = tmp_path / "valid.kicad_pcb"
    board.write_text(repeated_reference_board(invalid=False, style=style))
    pcb = PCB.load(board)
    pads = list(_iter_board_pads(pcb))
    assert len(pads) == sum(len(fp.pads) for fp in pcb.footprints)
    assert len({key for key, *_ in pads}) == len(pcb.footprints)


@pytest.mark.parametrize("ref", ["", "DUP"])
def test_adaptive_reconstruction_retains_full_population_keys(ref):
    from kicad_tools.router.adaptive import AdaptiveAutorouter
    from kicad_tools.router.layers import LayerStack

    components = [
        {
            "ref": ref,
            "x": x,
            "y": 5,
            "pads": [
                {"number": "1", "x": 0, "y": 0, "net": net},
                {"number": "2", "x": 0, "y": 1, "net": "SHARED"},
            ],
        }
        for x, net in ((3, "A"), (9, "B"))
    ]
    adaptive = AdaptiveAutorouter(15, 15, components, {"A": 1, "B": 2, "SHARED": 3}, verbose=False)
    previous = None
    for stack in (LayerStack.two_layer(), LayerStack.four_layer_sig_gnd_pwr_sig()):
        router = adaptive._create_autorouter(stack)
        assert len(router.all_pads) == len(router.pads) == 4
        assert len(router.nets[3]) == 2
        assert {pad.ref for pad in router.all_pads} == {ref}
        for net, keys in router.nets.items():
            assert all(router.pads[key].net == net for key in keys)
        if previous is not None:
            assert router.nets == previous
        previous = router.nets
    assert all("component_id" not in comp for comp in components)


@pytest.mark.parametrize("pin", ["1", ""])
def test_loader_and_reset_keep_same_pin_arrays_as_physical_obstacles(tmp_path, pin):
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Segment

    board = tmp_path / "array.kicad_pcb"
    text = repeated_reference_board(invalid=False, style="duplicate")
    original_pad = '(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "BAD"))'
    replacement = original_pad.replace('"1" smd', f'"{pin}" smd')
    text = text.replace(original_pad, replacement + replacement.replace("(at 0 0)", "(at 1 0)"), 1)
    board.write_text(text)
    router, nets = load_pcb_for_routing(board, force_python=True)
    assert len(router.all_pads) == 9 and len(router.pads) == 8
    assert len(router.nets[nets["BAD"]]) == 3
    shapes = [(p.key, p.x, p.y, p.net) for p in router.all_pads]
    router._reset_for_new_trial()
    assert [(p.key, p.x, p.y, p.net) for p in router.all_pads] == shapes
    assert len(router.grid._pads) == 9
    for pad in router.all_pads:
        segment = Segment(pad.x - 0.1, pad.y, pad.x + 0.1, pad.y, 0.1, Layer.F_CU, 99)
        assert not router.grid.validate_segment_clearance(segment, exclude_net=99)[0]


@pytest.mark.parametrize(
    "options",
    [
        ["--no-auto-layers", "--strategy", "monte-carlo"],
        ["--no-auto-layers", "--strategy", "evolutionary"],
        ["--no-auto-layers", "--strategy", "basic", "--route-engine", "mesh"],
        ["--no-auto-layers", "--strategy", "basic", "--route-engine", "lattice"],
        ["--no-auto-layers", "--adaptive-rules"],
        ["--adaptive-rules"],
        ["--region", "0,0,20,12"],
        ["--complete"],
    ],
)
def test_duplicate_reference_mixed_cli_modes(tmp_path, options):
    # Single-pass modes also report the fixture's inherited BAD/PLANE DRC errors.
    expected_exit = 2 if "--adaptive-rules" in options or "--region" in options else 3
    test_default_mixed_cli_routes_only_valid_physical_nets(
        tmp_path, "duplicate", options, expected_exit
    )


def test_shared_net_terminals_on_duplicate_footprints_stay_distinct(tmp_path):
    from kicad_tools.schema.physical_identity import footprint_keys

    board = tmp_path / "shared.kicad_pcb"
    text = board_text(invalid=False)
    for ref in ("X1", "X2", "X3", "R1", "R2", "P1", "P2", "G1"):
        text = text.replace(f'(property "Reference" "{ref}"', '(property "Reference" "DUP"')
    board.write_text(text)
    pcb = PCB.load(board)
    keys = footprint_keys(pcb.footprints)
    router, nets = load_pcb_for_routing(board, force_python=True)
    assert len(router.pads) == 8
    assert len(router.nets[nets["BAD"]]) == 3
    assert len(router.nets[nets["GOOD"]]) == 2
    for fp, key in zip(pcb.footprints, keys, strict=True):
        for pad in fp.pads:
            terminal = router.pads[(key, pad.number)]
            assert terminal.net_name == pad.net_name
            assert (terminal.x, terminal.y) == (
                fp.position[0] + pcb.board_origin[0],
                fp.position[1] + pcb.board_origin[1],
            )
    hierarchy = router._create_hierarchical_router()
    assert hierarchy.all_pads == router.all_pads


@pytest.mark.parametrize("style", ["empty", "duplicate"])
@pytest.mark.parametrize("auto_fix", [False, True])
def test_duplicate_identity_survives_real_native_fill_and_repair(
    tmp_path, monkeypatch, capsys, style, auto_fix
):
    from kicad_tools.schema.physical_identity import footprint_keys
    from tests import test_placement_native_fill as native

    if native.NATIVE_PYTHON is None:
        pytest.skip("KiCad Python runtime unavailable")
    monkeypatch.setattr(
        native, "board_text", lambda: repeated_reference_board(invalid=True, style=style)
    )
    native.test_real_cli_routes_and_fills_without_changing_excluded_zone(
        tmp_path, auto_fix, "BAD", capsys
    )
    before = PCB.load(tmp_path / "source.kicad_pcb")
    after = PCB.load(tmp_path / "routed.kicad_pcb")
    assert footprint_keys(before.footprints) == footprint_keys(after.footprints)
    assert [(fp.reference, fp.position) for fp in before.footprints] == [
        (fp.reference, fp.position) for fp in after.footprints
    ]


def test_single_anonymous_component_retains_pitch_geometry():
    from kicad_tools.router import Autorouter

    router = Autorouter(10, 10, force_python=True, physics_enabled=False)
    router.add_component(
        "",
        [
            {"number": "1", "x": 3, "y": 3, "net": 1},
            {"number": "2", "x": 3, "y": 3.5, "net": 2},
        ],
        component_id="",
    )
    assert router.grid.compute_component_pitches()[""] == pytest.approx(0.5)


@pytest.mark.parametrize("explicit", [False, True])
def test_component_registration_keeps_collision_guard(explicit):
    from kicad_tools.router import Autorouter

    router = Autorouter(10, 10, force_python=True, physics_enabled=False)
    first = {"number": "1", "x": 3, "y": 3, "net": 1}
    second = {"number": "2", "x": 3, "y": 4, "net": 2}
    kwargs = {"component_id": "U1"} if explicit else {}
    router.add_component("U1", [first], **kwargs)
    router.add_component("U1", [first], **kwargs)
    assert len(router.all_pads) == 1
    if explicit:
        for extension_kwargs in ({}, kwargs):
            with pytest.raises(ValueError, match="already exists"):
                router.add_component("U1", [second], **extension_kwargs)
    else:
        router.add_component("U1", [second])
        assert len(router.all_pads) == 2
        assert router.nets == {1: [("U1", "1")], 2: [("U1", "2")]}
    with pytest.raises(ValueError, match="already exists"):
        router.add_component("U1", [{**first, "net": 3}], **kwargs)


def test_explicit_identity_promotes_legacy_registration():
    from kicad_tools.router import Autorouter

    router = Autorouter(10, 10, force_python=True, physics_enabled=False)
    first = {"number": "1", "x": 3, "y": 3, "net": 1}
    router.add_component("U1", [first])
    router.add_component("U1", [first], component_id="U1")
    with pytest.raises(ValueError, match="already exists"):
        router.add_component("U1", [{"number": "2", "x": 3, "y": 4, "net": 2}])
    assert len(router.all_pads) == 1
