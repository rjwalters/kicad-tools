"""Worker trials retain electrical terminals and every physical pad shape."""

import pickle
from dataclasses import asdict

import pytest

from kicad_tools.router import Autorouter
from kicad_tools.router.algorithms.evolutionary import _run_evolutionary_trial
from kicad_tools.router.core import _run_monte_carlo_trial
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment


def _payload(router):
    config = pickle.loads(pickle.dumps(router._serialize_for_parallel()))
    config.update(
        trial_num=0, chrom_idx=0, seed=0, base_order=[], net_order=[], use_negotiated=False
    )
    return config


def _run_worker(monkeypatch, config, mode, inspect):
    def observe(router, *_args, **_kwargs):
        inspect(router)
        return []

    monkeypatch.setattr(Autorouter, "route_all", observe)
    monkeypatch.setattr(Autorouter, "route_all_negotiated", observe)
    monkeypatch.setattr(Autorouter, "_evaluate_solution", lambda self, routes: 0)
    if mode == "evolutionary":
        _run_evolutionary_trial(config)
    else:
        config["use_negotiated"] = mode == "negotiated"
        _run_monte_carlo_trial(config)


@pytest.mark.parametrize("mode", ["basic", "negotiated", "evolutionary"])
@pytest.mark.parametrize("ref", ["", "DUP", 'DUP\\return"pin'])
@pytest.mark.parametrize("pin", ["1", ""])
def test_worker_keeps_physical_array_and_distinct_component_topology(monkeypatch, mode, ref, pin):
    parent = Autorouter(20, 20, force_python=True, physics_enabled=False)
    parent.add_component(
        ref,
        [{"number": pin, "x": x, "y": 5, "width": 1, "height": 1, "net": 1} for x in (3, 6)],
        component_id="physical:first",
    )
    parent.add_component(
        ref,
        [{"number": pin, "x": 12, "y": 5, "width": 1, "height": 1, "net": 2}],
        component_id="physical:second",
    )
    parent.all_pads[0].drill_size = (0.2, 0.4)
    parent.all_pads[0].drill_rotation = 37
    parent.all_pads[0].footprint_name = "Package:PhysicalArray"
    config = _payload(parent)
    seen = []

    def inspect(worker):
        seen.append(worker)
        assert len(worker.all_pads) == len(worker.grid._pads) == 3
        assert [asdict(p) for p in worker.all_pads] == [asdict(p) for p in parent.all_pads]
        assert set(worker.pads) == set(parent.pads)
        assert len(worker.pads) == 3
        assert worker.nets == parent.nets
        assert len(worker.nets[1]) == 2
        for net, keys in worker.nets.items():
            assert all(worker.pads[key].net == net for key in keys)
        # Each same-pin physical shape remains a foreign-copper obstacle.
        # A topology-only reconstruction loses the first x=3 shape.
        for x in (3, 6, 12):
            candidate = Segment(x - 0.1, 5, x + 0.1, 5, 0.1, Layer.F_CU, 3)
            assert not worker.grid.validate_segment_clearance(candidate, exclude_net=3)[0]
        assert [worker.grid._pad_pin_pitch[id(p)] for p in worker.all_pads] == [
            parent.grid._pad_pin_pitch[id(p)] for p in parent.all_pads
        ]

    _run_worker(monkeypatch, config, mode, inspect)
    assert len(seen) == 1


@pytest.mark.parametrize("mode", ["basic", "negotiated", "evolutionary"])
def test_legacy_worker_payload_keeps_reference_lookup(monkeypatch, mode):
    parent = Autorouter(20, 20, force_python=True, physics_enabled=False)
    parent.add_component("J1", [{"number": "1", "x": 3, "y": 5, "net": 1}])
    config = _payload(parent)
    for item in config["pads_data"]:
        item.pop("component_id")
        item.pop("pin_pitch")
    # Legacy repeated topology targets remain one electrical terminal.
    config["nets"]["1"] = [["J1", "1"], ["J1", "1"]]
    seen = []

    def inspect(worker):
        seen.append(worker)
        assert worker.pads[("J1", "1")].ref == "J1"
        assert worker.nets == {1: [("J1", "1")]}
        assert len(worker.all_pads) == 1

    _run_worker(monkeypatch, config, mode, inspect)
    assert len(seen) == 1


@pytest.mark.parametrize("mode", ["basic", "negotiated", "evolutionary"])
def test_worker_rejects_one_physical_terminal_with_conflicting_nets(monkeypatch, mode):
    parent = Autorouter(20, 20, force_python=True, physics_enabled=False)
    parent.add_component("J1", [{"number": "1", "x": 3, "y": 5, "net": 1}])
    config = _payload(parent)
    config["pads_data"].append({**config["pads_data"][0], "net": 2, "x": 6})
    with pytest.raises(ValueError, match="Physical terminal.*conflicting nets"):
        _run_worker(monkeypatch, config, mode, lambda worker: pytest.fail("routing started"))
