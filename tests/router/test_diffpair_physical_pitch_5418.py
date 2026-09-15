"""Differential routing consumes pitches by physical component identity."""

import pytest

from kicad_tools.router import Autorouter, diffpair_routing
from kicad_tools.router.diffpair import (
    DifferentialPair,
    DifferentialPairRules,
    DifferentialPairType,
    DifferentialSignal,
)
from kicad_tools.router.grid import RoutingGrid


@pytest.fixture(params=["DUP", ""])
def physical_router(request):
    router = Autorouter(20, 15, force_python=True, physics_enabled=False)
    for identity, x, pitch, nets in [
        ("left", 5, 0.5, (1, 2)),
        ("right", 12, 0.8, (1, 2)),
        ("foreign", 8, 0.4, (3, 4)),
    ]:
        router.add_component(
            request.param,
            [
                {
                    "number": str(i + 1),
                    "x": x,
                    "y": 5 + i * pitch,
                    "width": 0.2,
                    "height": 0.2,
                    "net": net,
                }
                for i, net in enumerate(nets)
            ],
            component_id=identity,
        )
    assert router.component_pitches == pytest.approx({"left": 0.5, "right": 0.8, "foreign": 0.4})
    return router


def test_escape_channels_retain_each_physical_pitch(physical_router):
    channels = physical_router._diffpair._escape_channel_registry(frozenset())
    assert len(channels) == 4
    for channel in channels:
        pitch = 0.5 if channel.x == 5 else 0.8
        assert channel.reach == pytest.approx(
            diffpair_routing._ESCAPE_CHANNEL_REACH_PITCHES * pitch
        )


def test_fine_grid_receives_pair_and_foreign_physical_pitches(physical_router, monkeypatch):
    router = physical_router
    pair = DifferentialPair(
        name="USB_D",
        positive=DifferentialSignal("USB_D+", 1, "USB_D", "P", "plus_minus"),
        negative=DifferentialSignal("USB_D-", 2, "USB_D", "N", "plus_minus"),
        pair_type=DifferentialPairType.USB2,
        rules=DifferentialPairRules.for_type(DifferentialPairType.USB2),
    )
    monkeypatch.setattr(
        router._diffpair,
        "_get_pair_pads",
        lambda _: (
            [router.pads[key] for key in router.nets[1]],
            [router.pads[key] for key in router.nets[2]],
        ),
    )
    observed = {}
    add_pad = RoutingGrid.add_pad

    def capture(grid, pad, *args, **kwargs):
        observed[pad.key] = kwargs.get("pin_pitch")
        return add_pad(grid, pad, *args, **kwargs)

    monkeypatch.setattr(RoutingGrid, "add_pad", capture)
    # Exercise real grid construction without spending a coupled-search budget.
    monkeypatch.setattr(diffpair_routing.CoupledPathfinder, "route_coupled", lambda *a, **k: None)
    router._diffpair._route_pair_on_fine_grid(pair, None, 1, None)
    assert observed.keys() == router.pads.keys()
    for key, pitch in observed.items():
        assert pitch == pytest.approx(router.component_pitches[router.pads[key].component_key])
