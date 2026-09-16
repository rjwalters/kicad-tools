"""Routing must connect physical lands, including repeated authored pin names."""

import json
import subprocess

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.cli.route_cmd import _write_routed_pcb
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules
from tests.test_net_status_duplicate_pads import _board


def terminal_board(pin="SH", reverse=False, second_layer="F.Cu"):
    board = (
        _board(reverse=reverse, second_layer=second_layer)
        .replace('"SH"', f'"{pin}"')
        .replace("GND", "SIGNAL")
        .rstrip()
    )
    return (
        board[:-1]
        + """(footprint "Test:Terminal" (layer "F.Cu") (at 20 15)
      (property "Reference" "U1" (at 0 -2) (layer "F.SilkS")
        (effects (font (size 1 1) (thickness .15))))
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))))"""
    )


@pytest.mark.parametrize("second_layer", [Layer.F_CU, Layer.B_CU])
@pytest.mark.parametrize("pin", ["1", "SH"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("strategy", ["basic", "negotiated", "lattice"])
def test_every_disconnected_land_is_routed(tmp_path, pin, reverse, strategy, second_layer):
    router = Autorouter(
        width=30,
        height=20,
        rules=DesignRules(grid_resolution=0.25),
        strategy="lattice" if strategy == "lattice" else "grid",
        force_python=True,
    )
    xs = [20, 10] if reverse else [10, 20]
    router.add_component(
        "J1",
        [
            {
                "number": pin,
                "x": x,
                "y": 10,
                "net": 1,
                "net_name": "SIGNAL",
                "width": 1,
                "height": 1,
                "layer": second_layer if x == 20 else Layer.F_CU,
            }
            for x in xs
        ],
    )
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 20,
                "y": 15,
                "net": 1,
                "net_name": "SIGNAL",
                "width": 1,
                "height": 1,
            }
        ],
    )
    source, output = tmp_path / "source.kicad_pcb", tmp_path / "routed.kicad_pcb"
    source.write_text(terminal_board(pin, reverse, second_layer.kicad_name))
    if strategy == "negotiated":
        router.route_all_negotiated(
            max_iterations=2, timeout=5, per_net_timeout=2, adaptive=False, perturbation=False
        )
    else:
        router.route_all(timeout=5, per_net_timeout=2)
    _write_routed_pcb(source, output, router.to_sexp())
    status = NetStatusAnalyzer(output).analyze().get_net("SIGNAL")
    assert status.total_pads == 3
    assert status.island_count == 1, status.to_dict()
    assert router.get_statistics()["nets_fully_connected"] == 1
    cli = find_kicad_cli()
    if cli:
        report = tmp_path / "native.json"
        subprocess.run(
            [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(output)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        assert not json.loads(report.read_text())["unconnected_items"]


def test_land_identity_stable_under_reordering_and_pin_suffix_collision():
    pads = [
        {"number": pin, "x": x, "y": 5, "net": 1}
        for pin, x in [("1", 3), ("1", 6), ("1#land:1", 9)]
    ]
    identities = []
    for population in (pads, list(reversed(pads))):
        router = Autorouter(20, 20, force_python=True)
        router.add_component("J1", population)
        identities.append({p.x: p.key for p in router.all_pads})
        assert len(router.nets[1]) == len(router.pads) == 3
        router.add_component("J1", list(reversed(population)))
        assert len(router.all_pads) == 3
    assert identities[0] == identities[1]


def test_coincident_lands_on_different_layers_are_distinct_targets():
    router = Autorouter(20, 20, force_python=True)
    router.add_component(
        "J1",
        [
            {"number": "SH", "x": 5, "y": 5, "net": 1, "layer": layer}
            for layer in (Layer.F_CU, Layer.B_CU)
        ],
    )
    assert len(router.nets[1]) == 2
    assert {p.layer for p in router.pads.values()} == {Layer.F_CU, Layer.B_CU}


@pytest.mark.parametrize("second_layer", [Layer.F_CU, Layer.B_CU])
def test_missing_land_remains_in_completion_accounting(second_layer):
    from kicad_tools.router.primitives import Route, Segment

    router = Autorouter(30, 20, force_python=True)
    router.add_component(
        "J1",
        [
            {"number": "SH", "x": x, "y": 10, "net": 1, "net_name": "SIGNAL", "layer": layer}
            for x, layer in ((10, second_layer), (20, Layer.F_CU))
        ],
    )
    router.add_component("U1", [{"number": "1", "x": 20, "y": 15, "net": 1, "net_name": "SIGNAL"}])
    # This is the previously misreported complete result: only the second
    # land connects to the external terminal, and the first land is stranded.
    router.routes = [Route(1, "SIGNAL", [Segment(20, 10, 20, 15, 0.25, Layer.F_CU, 1)])]
    stats = router.get_statistics()
    assert stats["nets_fully_connected"] == 0
    assert stats["nets_partial"] == 1


@pytest.mark.parametrize("through_hole", [False, True])
def test_overlapping_copper_lands_do_not_require_redundant_connections(through_hole):
    from kicad_tools.router.primitives import Route, Segment

    router = Autorouter(30, 20, force_python=True)
    router.add_component(
        "J1",
        [
            {
                "number": "SH",
                "x": x,
                "y": 10,
                "net": 1,
                "width": 2,
                "height": 2,
                "through_hole": through_hole,
            }
            for x in (10, 10.5)
        ],
    )
    router.add_component("U1", [{"number": "1", "x": 20, "y": 10, "net": 1}])
    router.routes = [Route(1, "SIGNAL", [Segment(10.5, 10, 20, 10, 0.25, Layer.F_CU, 1)])]
    assert router.get_statistics()["nets_fully_connected"] == 1


def test_legacy_extension_cannot_overwrite_an_existing_land():
    router = Autorouter(20, 20, force_python=True)
    router.add_component("J1", [{"number": "1", "x": x, "y": 5, "net": 1} for x in (3, 6)])
    snapshot = dict(router.pads)
    with pytest.raises(ValueError, match="conflicts with an existing land"):
        router.add_component("J1", [{"number": "1#land:1", "x": 9, "y": 5, "net": 2}])
    assert router.pads == snapshot
    assert set(router.nets) == {1}


def test_virtual_pair_pad_preserves_physical_terminal():
    router = Autorouter(20, 20, force_python=True)
    router.add_component("J1", [{"number": "SH", "x": x, "y": 5, "net": 1} for x in (3, 6)])
    physical = next(pad for pad in router.all_pads if pad.terminal_id)
    virtual = router._diffpair._virtual_pad_at(physical, 10, 10, 0)
    assert virtual.key == physical.key
    assert virtual.pin == physical.pin == "SH"
