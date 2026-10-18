"""Issue #6866: the diff-pair tie step honours the pair layer filter.

The lattice tie step (``Autorouter._lattice_plan_pair_ties``, #6698) planned
tie copper on every layer both pads share, ignoring ``rules.allowed_layers``
(``--reserve-plane-layers``) and the nets' hard ``avoid_layers``.  On board
03 (seed 42) that put 1.70 mm of USB_D- on In1.Cu, the GND plane.  The tie
now uses :func:`pair_permitted_layers`, the filter the coupled search
(``CoupledPathfinder._coupled_routable_layers``, #6857) uses too.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pair_layer_filter import pair_permitted_layers
from kicad_tools.router.rules import DesignRules, NetClassRouting
from tests.router.lattice.test_coupled_pairs import _HS, _usbc_chain_pads

_NAMES = {0: "F.Cu", 1: "In1.Cu", 2: "In2.Cu", 3: "B.Cu"}


def _router(*, allowed: list[str] | None, classes: dict | None = None, strict: bool = False):
    from kicad_tools.router.core import Autorouter

    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
    rules.min_trace_width_floor = 0.1016
    rules.allowed_layers = allowed
    rules.strict_layers = strict
    router = Autorouter(
        30,
        12,
        strategy="lattice",
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
        net_class_map=classes or {"D+": _HS, "D-": _HS},
    )
    for key, pad in _usbc_chain_pads().items():
        router.pads[(pad.ref, key)] = pad
        router.nets.setdefault(pad.net, []).append((pad.ref, key))
        router.net_names[pad.net] = pad.net_name
    router._ensure_lattice_pathfinder()
    return router


def _tie_layers(router) -> dict[str, str]:
    return {
        route.net_name: route.segments[0].layer.kicad_name
        for _k, route, _e in router._lattice_pair_ties
    }


def test_ties_use_inner_layers_without_plane_reservation() -> None:
    # Baseline: with every layer permitted the crossing D- tie takes In1.Cu,
    # the first free layer after F.Cu -- the board 03 shape.
    router = _router(allowed=None)
    router._lattice_netset_connections()
    assert _tie_layers(router) == {"D+": "F.Cu", "D-": "In1.Cu"}


def test_tie_never_lands_on_a_reserved_plane(capsys) -> None:
    # ``--reserve-plane-layers``: only F.Cu / B.Cu carry signal copper.
    router = _router(allowed=["F.Cu", "B.Cu"])
    router._lattice_netset_connections()
    layers = _tie_layers(router)
    assert set(layers) == {"D+", "D-"}
    assert set(layers.values()) <= {"F.Cu", "B.Cu"}
    assert layers["D+"] != layers["D-"]
    assert "In1.Cu" not in capsys.readouterr().out


def test_tie_honours_hard_avoid_layers_of_the_partner_net() -> None:
    # D- hard-avoids F.Cu and In1.Cu (strict layers).  The pair filter is the
    # union of BOTH nets, so the D+ tie may not use them either: only In2.Cu
    # and B.Cu remain (before #6866: D+ on F.Cu, D- on In1.Cu).
    avoid = replace(_HS, avoid_layers=[0, 1])
    router = _router(allowed=None, classes={"D+": _HS, "D-": avoid}, strict=True)
    router._lattice_netset_connections()
    layers = _tie_layers(router)
    assert set(layers) == {"D+", "D-"}
    assert set(layers.values()) <= {"In2.Cu", "B.Cu"}


def test_tie_declines_when_no_layer_is_permitted(capsys, monkeypatch) -> None:
    from tests.router.lattice.test_coupled_pairs import _todays_connections

    # Every shared layer is hard-avoided by D-: no permitted tie exists, so
    # the pair keeps today's connections (all or nothing, #6697/#6698).
    avoid = replace(_HS, avoid_layers=[0, 1, 2, 3])
    router = _router(allowed=None, classes={"D+": _HS, "D-": avoid}, strict=True)
    today = _todays_connections(router, monkeypatch)
    _c, conns = router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert conns == today
    out = capsys.readouterr().out
    assert "Diff-pair ties D+/D-: not used (J1.B6->J1.A6 has no permitted tie layer" in out


def test_tie_declines_when_only_one_permitted_layer_fits_one_tie(capsys) -> None:
    # One permitted layer has room for only one of the two crossing ties.
    router = _router(allowed=["F.Cu"])
    router._lattice_netset_connections()
    assert router._lattice_pair_ties == []
    assert "Diff-pair ties D+/D-: not used (J1.B7->J1.A7 has no clearing tie)" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("allowed", "avoid", "strict", "expected"),
    [
        (None, (), False, [0, 1, 2, 3]),
        (["F.Cu", "B.Cu"], (), False, [0, 3]),
        (None, (3,), False, [0, 1, 2, 3]),  # soft avoid: not hard without strict
        (None, (3,), True, [0, 1, 2]),
        (["F.Cu", "B.Cu"], (3,), True, [0]),
    ],
)
def test_pair_permitted_layers(allowed, avoid, strict, expected) -> None:
    rules = DesignRules()
    rules.allowed_layers = allowed
    rules.strict_layers = strict
    p_class = NetClassRouting(name="P", avoid_layers=list(avoid))
    got = pair_permitted_layers([0, 1, 2, 3], _NAMES.__getitem__, rules, (None, p_class))
    assert got == expected
