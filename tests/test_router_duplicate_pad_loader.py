"""Load physical duplicate lands without changing authored pin names (#5480)."""

import json
import subprocess

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.router.io import load_pcb_for_routing


def duplicate_land_board(*, policy="no", pin="SH", second_layer="F.Cu", reverse=False):
    pads = [
        f'(pad "{pin}" smd rect (at {x} 0) (size 1 1) (layers "{layer}") (net 1 "SIGNAL"))'
        for x, layer in ((0, "F.Cu"), (10, second_layer))
    ]
    if reverse:
        pads.reverse()
    jumper = "" if policy is None else f"(duplicate_pad_numbers_are_jumpers {policy})"
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "SIGNAL")
      (footprint "Test:Shield" (layer "F.Cu") (at 10 10)
        (property "Reference" "J1" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        {jumper} {" ".join(pads)})
      (footprint "Test:Terminal" (layer "F.Cu") (at 20 15)
        (property "Reference" "U1" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL")))
      (gr_rect (start 0 0) (end 30 25) (stroke (width 0.05) (type solid))
        (fill none) (layer "Edge.Cuts")))"""


@pytest.mark.parametrize("policy", [None, "no", "yes"])
@pytest.mark.parametrize("pin", ["1", "SH"])
@pytest.mark.parametrize("second_layer", ["F.Cu", "B.Cu"])
@pytest.mark.parametrize("reverse", [False, True])
def test_loader_retains_lands_and_only_explicit_jumpers_share_targets(
    tmp_path, policy, pin, second_layer, reverse
):
    path = tmp_path / "duplicates.kicad_pcb"
    path.write_text(
        duplicate_land_board(policy=policy, pin=pin, second_layer=second_layer, reverse=reverse)
    )
    original = path.read_bytes()
    router, nets = load_pcb_for_routing(str(path), force_python=True)
    assert len(router.all_pads) == 3
    lands = [pad for pad in router.all_pads if pad.ref == "J1"]
    assert len(lands) == 2
    assert {pad.pin for pad in lands} == {pin}
    assert {(pad.x, pad.y) for pad in lands} == {(10, 10), (20, 10)}
    assert len(router.nets[nets["SIGNAL"]]) == (2 if policy == "yes" else 3)
    assert all(router.pads[key].net == nets["SIGNAL"] for key in router.nets[nets["SIGNAL"]])
    assert path.read_bytes() == original


def test_loader_policy_is_a_footprint_field_not_property_text(tmp_path):
    path = tmp_path / "property.kicad_pcb"
    text = duplicate_land_board(policy=None).replace(
        '(footprint "Test:Shield" (layer "F.Cu")',
        '(footprint "Test:Shield" (layer "F.Cu") '
        '(property "Comment" "(duplicate_pad_numbers_are_jumpers yes)")',
    )
    path.write_text(text)
    router, nets = load_pcb_for_routing(str(path), force_python=True)
    assert len(router.nets[nets["SIGNAL"]]) == 3


@pytest.mark.parametrize("policy", [None, "no", "yes"])
@pytest.mark.parametrize("second_layer", ["F.Cu", "B.Cu"])
def test_native_duplicate_land_jumper_policy(tmp_path, policy, second_layer):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available")
    path = tmp_path / "native.kicad_pcb"
    path.write_text(duplicate_land_board(policy=policy, second_layer=second_layer))
    report = tmp_path / "native.json"
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    missing = json.loads(report.read_text())["unconnected_items"]
    assert len(missing) == (1 if policy == "yes" else 2)
    router, nets = load_pcb_for_routing(str(path), force_python=True)
    assert len(router.nets[nets["SIGNAL"]]) - 1 == len(missing)


@pytest.mark.parametrize("policy", [None, False, True])
@pytest.mark.parametrize("entry_point", ["adaptive", "route_pcb"])
def test_component_dictionary_entry_points_preserve_jumper_policy(monkeypatch, policy, entry_point):
    from kicad_tools.router.adaptive import AdaptiveAutorouter
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.io import route_pcb
    from kicad_tools.router.layers import LayerStack

    component = {
        "ref": "J1",
        "x": 10,
        "y": 10,
        "pads": [{"number": "SH", "x": x, "y": 0, "net": "SIGNAL"} for x in (0, 10)],
    }
    if policy is not None:
        component["duplicate_pad_numbers_are_jumpers"] = policy
    if entry_point == "adaptive":
        adaptive = AdaptiveAutorouter(30, 25, [component], {"SIGNAL": 1}, verbose=False)
        router = adaptive._create_autorouter(LayerStack.two_layer())
    else:
        captured = []

        def capture_route_all(router, *args, **kwargs):
            captured.append(router)
            return []

        monkeypatch.setattr(Autorouter, "route_all", capture_route_all)
        route_pcb(30, 25, [component], {"SIGNAL": 1})
        (router,) = captured
    assert len(router.all_pads) == 2
    assert len(router.nets[1]) == (1 if policy else 2)
    assert {pad.pin for pad in router.all_pads} == {"SH"}
