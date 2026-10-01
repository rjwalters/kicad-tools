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


def no_connect_mount_board(*, policy=None, second_net="unconnected-(J5-PadMP)_1"):
    """Mirror srj18_dual_gmsl_serializer_adapter's J5: two "MP" lands (#5873)."""
    jumper = "" if policy is None else f"(duplicate_pad_numbers_are_jumpers {policy})"
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "SIGNAL") (net 2 "unconnected-(J5-PadMP)")
      (net 3 "{second_net}")
      (footprint "Test:Connector" (layer "F.Cu") (at 10 10)
        (property "Reference" "J5" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        {jumper}
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))
        (pad "MP" smd roundrect (at -2.8 -1.755 180) (size 1.2 1.8)
          (layers "F.Cu") (roundrect_rratio 0.25)
          (net 2 "unconnected-(J5-PadMP)") (pintype "passive+no_connect"))
        (pad "MP" smd roundrect (at 2.8 -1.755 180) (size 1.2 1.8)
          (layers "F.Cu") (roundrect_rratio 0.25)
          (net 3 "{second_net}") (pintype "passive+no_connect")))
      (footprint "Test:Terminal" (layer "F.Cu") (at 20 15)
        (property "Reference" "U1" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL")))
      (gr_rect (start 0 0) (end 30 25) (stroke (width 0.05) (type solid))
        (fill none) (layer "Edge.Cuts")))"""


@pytest.mark.parametrize("policy", [None, "no"])
def test_loader_accepts_same_number_lands_with_distinct_no_connect_nets(tmp_path, policy):
    path = tmp_path / "no_connect_mp.kicad_pcb"
    path.write_text(no_connect_mount_board(policy=policy))
    router, nets = load_pcb_for_routing(str(path), force_python=True)
    lands = [pad for pad in router.all_pads if pad.ref == "J5" and pad.pin == "MP"]
    assert len(lands) == 2
    # Two independent terminals: distinct keys, each keeping its own net.
    assert len({pad.key for pad in lands}) == 2
    assert {pad.net_name for pad in lands} == {
        "unconnected-(J5-PadMP)",
        "unconnected-(J5-PadMP)_1",
    }
    assert len({pad.net for pad in lands}) == 2
    for pad in lands:
        assert router.pads[pad.key] is pad
    # The signal net on the same footprint is unaffected.
    assert len(router.nets[nets["SIGNAL"]]) == 2


def test_loader_still_rejects_jumpered_lands_with_distinct_no_connect_nets(tmp_path):
    path = tmp_path / "jumpered_no_connect_mp.kicad_pcb"
    path.write_text(no_connect_mount_board(policy="yes"))
    with pytest.raises(ValueError, match=r"J5\.MP has conflicting nets"):
        load_pcb_for_routing(str(path), force_python=True)


def test_loader_still_rejects_no_connect_land_colliding_with_signal_net(tmp_path):
    path = tmp_path / "mixed_mp.kicad_pcb"
    path.write_text(no_connect_mount_board(second_net="SHIELD"))
    with pytest.raises(ValueError, match=r"J5\.MP has conflicting nets"):
        load_pcb_for_routing(str(path), force_python=True)


@pytest.mark.parametrize(
    "names",
    [
        ("SIG_A", "SIG_B"),
        ("unconnected-(J1-PadMP)", "SIG_B"),
        ("SIG_A", "unconnected-(J1-PadMP)"),
        ("", "unconnected-(J1-PadMP)"),
    ],
)
@pytest.mark.parametrize("jumpers", [False, True])
def test_add_component_rejects_genuine_same_number_net_conflicts(names, jumpers):
    from kicad_tools.router.core import Autorouter

    router = Autorouter(20, 20, force_python=True, physics_enabled=False)
    pads = [
        {"number": "MP", "x": x, "y": 5, "net": net, "net_name": name}
        for x, net, name in ((3, 1, names[0]), (9, 2, names[1]))
    ]
    with pytest.raises(ValueError, match=r"J1\.MP has conflicting nets"):
        router.add_component("J1", pads, duplicate_pad_numbers_are_jumpers=jumpers)


@pytest.mark.parametrize("mode", ["basic", "negotiated", "evolutionary"])
def test_split_no_connect_lands_survive_worker_reconstruction(monkeypatch, mode):
    """The worker-side key guard sees distinct terminal ids, not a conflict."""
    import pickle

    from kicad_tools.router.algorithms.evolutionary import _run_evolutionary_trial
    from kicad_tools.router.core import Autorouter, _run_monte_carlo_trial

    parent = Autorouter(20, 20, force_python=True, physics_enabled=False)
    parent.add_component(
        "J5",
        [
            {"number": "MP", "x": 3, "y": 5, "net": 1, "net_name": "unconnected-(J5-PadMP)"},
            {"number": "MP", "x": 9, "y": 5, "net": 2, "net_name": "unconnected-(J5-PadMP)_1"},
        ],
    )
    config = pickle.loads(pickle.dumps(parent._serialize_for_parallel()))
    config.update(
        trial_num=0, chrom_idx=0, seed=0, base_order=[], net_order=[], use_negotiated=False
    )
    seen = []

    def observe(router, *_args, **_kwargs):
        seen.append(router)
        return []

    monkeypatch.setattr(Autorouter, "route_all", observe)
    monkeypatch.setattr(Autorouter, "route_all_negotiated", observe)
    monkeypatch.setattr(Autorouter, "_evaluate_solution", lambda self, routes: 0)
    if mode == "evolutionary":
        _run_evolutionary_trial(config)
    else:
        config["use_negotiated"] = mode == "negotiated"
        _run_monte_carlo_trial(config)
    (worker,) = seen
    assert {pad.net for pad in worker.all_pads} == {1, 2}
    assert len(worker.pads) == 2
