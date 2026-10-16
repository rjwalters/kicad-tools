"""Coupled diff-pair search honours layer restrictions (Issue #6857).

The single-ended router removes two kinds of layer from a net's routable
set: layers outside ``DesignRules.allowed_layers`` (``--reserve-plane-layers``,
on by default) and the net class's HARD ``avoid_layers``.  The coupled
joint-state search (Python and C++) used the bare
``grid.get_routable_indices()`` and so could commit pair copper on a
reference-plane layer (Board07 MIPI_DAT0 on In1.Cu at a 700k budget).

Every scenario runs on both the C++ backend (skipped when the extension is
absent) and the forced-Python backend.  Each restricted case is paired with
an unrestricted control on the same fixture that DOES use the forbidden
layer, so the restriction is shown to bite.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder, DiffPairRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route
from kicad_tools.router.rules import DesignRules, NetClassRouting

BACKENDS = [
    pytest.param(
        True,
        id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="router_cpp not built"),
    ),
    pytest.param(False, id="python"),
]

_PLANE_LAYERS = (Layer.IN1_CU, Layer.IN2_CU)


def _rules(**overrides) -> DesignRules:
    # The #6499 fixture's fine-pitch rules: a 0.6 mm pair can drop vias.
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.5,
        **overrides,
    )


def _pads() -> tuple[Pad, Pad, Pad, Pad]:
    """SMD sources on F.Cu, through-hole sinks (reachable on any layer)."""

    def pad(x: float, y: float, net: int, name: str, ref: str, th: bool) -> Pad:
        return Pad(
            x=x, y=y, width=0.2, height=0.2, net=net, net_name=name, layer=Layer.F_CU,
            ref=ref, pin="1", through_hole=th, drill=0.1 if th else 0.0,
        )  # fmt: skip

    return (
        pad(1.0, 1.2, 1, "D+", "A", False),
        pad(5.0, 1.2, 1, "D+", "B", True),
        pad(1.0, 1.8, 2, "D-", "C", False),
        pad(5.0, 1.8, 2, "D-", "D", True),
    )


def _route(
    use_cpp: bool,
    *,
    wall_layers: list[Layer],
    allowed_layers: list[str] | None = None,
    strict_layers: bool = False,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> tuple[RoutingGrid, tuple[Route, Route] | None]:
    """Route the pair across a full-height wall on ``wall_layers``."""
    rules = _rules(allowed_layers=allowed_layers, strict_layers=strict_layers)
    grid = RoutingGrid(
        width=6.0,
        height=3.0,
        rules=rules,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    # A wall from top to bottom edge at x=3mm: the pair cannot stay on any
    # of these layers end to end, so it must via onto another one.
    grid.add_keepout(3.0, 0.0, 3.0, 3.0, layers=wall_layers)
    pads = _pads()
    for item in pads:
        grid.add_pad(item)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=6,
        min_spacing_cells=6,
        net_class_map=net_class_map,
    )
    pf._use_cpp_coupled = use_cpp
    result = pf.route_coupled(*pads, max_iterations_budget=200_000)
    assert pf.last_coupled_backend == ("cpp" if use_cpp else "python")
    return grid, result


def _segment_layers(result: tuple[Route, Route], net_name: str | None = None) -> set[Layer]:
    """Layers carrying pair copper (a through via spans every layer anyway)."""
    return {
        seg.layer
        for route in result
        if net_name is None or route.net_name == net_name
        for seg in route.segments
    }


# ---------------------------------------------------------------------------
# rules.allowed_layers (plane-layer reservation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_control_unrestricted_pair_uses_inner_layer(use_cpp: bool) -> None:
    """Without ``allowed_layers`` the walled pair crosses on an inner layer."""
    _, result = _route(use_cpp, wall_layers=[Layer.F_CU, Layer.B_CU])
    assert result is not None, "the unrestricted pair must route via an inner layer"
    assert _segment_layers(result) & set(_PLANE_LAYERS), (
        "control fixture must use In1/In2 so the restricted test bites"
    )


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_reserved_planes_never_routed_when_outer_layers_walled(use_cpp: bool) -> None:
    """With F/B walled and planes reserved, the pair fails rather than use a plane."""
    _, result = _route(
        use_cpp, wall_layers=[Layer.F_CU, Layer.B_CU], allowed_layers=["F.Cu", "B.Cu"]
    )
    if result is not None:
        segs = _segment_layers(result)
        assert not segs & set(_PLANE_LAYERS), f"coupled pair routed on a plane: {segs}"


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_reserved_planes_pair_crosses_on_b_cu(use_cpp: bool) -> None:
    """F.Cu walled, planes reserved: the pair must cross on B.Cu only."""
    _, result = _route(use_cpp, wall_layers=[Layer.F_CU], allowed_layers=["F.Cu", "B.Cu"])
    assert result is not None, "the pair can legally cross on B.Cu"
    segs = _segment_layers(result)
    assert not segs & set(_PLANE_LAYERS), f"coupled pair routed on a plane: {segs}"
    assert Layer.B_CU in segs


# ---------------------------------------------------------------------------
# Per-net HARD avoid_layers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_control_only_b_cu_open_is_used(use_cpp: bool) -> None:
    """Control: with F/In1/In2 walled the pair crosses on B.Cu."""
    _, result = _route(use_cpp, wall_layers=[Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU])
    assert result is not None
    assert Layer.B_CU in _segment_layers(result)


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_hard_avoid_layers_on_p_net_blocks_b_cu(use_cpp: bool) -> None:
    """P's class hard-avoids B.Cu (strict_layers): the pair never uses B.Cu."""
    b_cu_index = 3  # four_layer_sig_gnd_pwr_sig: F=0, In1=1, In2=2, B=3
    net_class = NetClassRouting(name="HS", avoid_layers=[b_cu_index])
    _, result = _route(
        use_cpp,
        wall_layers=[Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU],
        strict_layers=True,
        net_class_map={"D+": net_class},
    )
    if result is not None:
        segs = _segment_layers(result)
        assert Layer.B_CU not in segs, f"hard-avoided B.Cu used: {segs}"


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_hard_avoid_layers_pair_routes_on_remaining_layer(use_cpp: bool) -> None:
    """With B.Cu hard-avoided and In1 open, the pair crosses on In1, not B.Cu."""
    net_class = NetClassRouting(name="HS", avoid_layers=[3])
    _, result = _route(
        use_cpp,
        wall_layers=[Layer.F_CU, Layer.IN2_CU],
        strict_layers=True,
        net_class_map={"D+": net_class},
    )
    assert result is not None
    segs = _segment_layers(result)
    assert Layer.B_CU not in segs
    assert Layer.IN1_CU in segs


def test_helper_returns_filtered_set() -> None:
    """Filtering everything away yields ``[]`` (Issue #6860: caller declines)."""
    rules = _rules(allowed_layers=["F.Cu"], strict_layers=True)
    grid = RoutingGrid(
        width=5.0, height=5.0, rules=rules, layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=2,
        net_class_map={"D+": NetClassRouting(name="HS", avoid_layers=[0])},
    )
    assert pf._coupled_routable_layers("D+", "D-", 0) == []
    rules.allowed_layers = ["F.Cu", "B.Cu"]
    assert pf._coupled_routable_layers("D+", "D-", 0) == [3]
    rules.allowed_layers = None
    rules.strict_layers = False
    assert pf._coupled_routable_layers("D+", "D-", 0) == [0, 1, 2, 3]


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_through_hole_start_leaves_on_a_permitted_layer(use_cpp: bool) -> None:
    """THT start + THT goal, F.Cu hard-avoided: the pair never runs on F.Cu."""
    rules = _rules(strict_layers=True)
    grid = RoutingGrid(
        width=6.0, height=3.0, rules=rules, layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig()
    )

    def pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
        return Pad(
            x=x, y=y, width=0.2, height=0.2, net=net, net_name=name, layer=Layer.F_CU,
            ref=ref, pin="1", through_hole=True, drill=0.1,
        )  # fmt: skip

    pads = (
        pad(1.0, 1.2, 1, "D+", "A"),
        pad(5.0, 1.2, 1, "D+", "B"),
        pad(1.0, 1.8, 2, "D-", "C"),
        pad(5.0, 1.8, 2, "D-", "D"),
    )
    for item in pads:
        grid.add_pad(item)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=6,
        min_spacing_cells=6,
        net_class_map={"D+": NetClassRouting(name="HS", avoid_layers=[0])},
    )
    pf._use_cpp_coupled = use_cpp
    result = pf.route_coupled(*pads, max_iterations_budget=200_000)
    assert result is not None
    segs = _segment_layers(result)
    assert segs and Layer.F_CU not in segs, segs


# ---------------------------------------------------------------------------
# Issue #6860: decline like the single-ended router
# ---------------------------------------------------------------------------


def _smd_pads(end_layer: Layer = Layer.F_CU) -> tuple[Pad, Pad, Pad, Pad]:
    """SMD sources on F.Cu, SMD sinks on ``end_layer``."""

    def pad(x: float, y: float, net: int, name: str, ref: str, layer: Layer) -> Pad:
        return Pad(
            x=x, y=y, width=0.2, height=0.2, net=net, net_name=name, layer=layer,
            ref=ref, pin="1",
        )  # fmt: skip

    return (
        pad(1.0, 1.2, 1, "D+", "A", Layer.F_CU),
        pad(5.0, 1.2, 1, "D+", "B", end_layer),
        pad(1.0, 1.8, 2, "D-", "C", Layer.F_CU),
        pad(5.0, 1.8, 2, "D-", "D", end_layer),
    )


def _route_smd(
    use_cpp: bool,
    pads: tuple[Pad, Pad, Pad, Pad],
    *,
    allowed_layers: list[str] | None = None,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> tuple[CoupledPathfinder, tuple[Route, Route] | None]:
    """Route an open-field SMD pair (no walls) under the given restriction."""
    rules = _rules(allowed_layers=allowed_layers, strict_layers=True)
    grid = RoutingGrid(
        width=6.0, height=3.0, rules=rules, layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    for item in pads:
        grid.add_pad(item)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=6,
        min_spacing_cells=6,
        net_class_map=net_class_map,
    )
    pf._use_cpp_coupled = use_cpp
    return pf, pf.route_coupled(*pads, max_iterations_budget=200_000)


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_control_unrestricted_smd_pair_routes_on_f_cu(use_cpp: bool) -> None:
    """Control: the same SMD pair routes on F.Cu with no restriction."""
    pf, result = _route_smd(use_cpp, _smd_pads())
    assert result is not None
    assert Layer.F_CU in _segment_layers(result)
    assert pf.last_decline_reason is None


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_hard_avoid_f_cu_with_f_cu_smd_pads_declines(use_cpp: bool) -> None:
    """Hard-avoid F.Cu class, F.Cu SMD pads: never commit copper on F.Cu."""
    net_class = NetClassRouting(name="PWR", avoid_layers=[0])
    pf, result = _route_smd(use_cpp, _smd_pads(), net_class_map={"D+": net_class})
    if result is not None:
        segs = _segment_layers(result)
        assert Layer.F_CU not in segs, f"hard-avoided F.Cu used: {segs}"
    assert result is None
    assert pf.last_decline_reason is not None
    assert "SMD pad" in pf.last_decline_reason and "F.Cu" in pf.last_decline_reason


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_smd_end_pad_on_forbidden_layer_declines(use_cpp: bool) -> None:
    """An SMD END pad on a non-permitted layer declines the pair too."""
    pf, result = _route_smd(
        use_cpp, _smd_pads(end_layer=Layer.B_CU), allowed_layers=["F.Cu", "In1.Cu"]
    )
    assert result is None
    assert pf.last_decline_reason is not None
    assert "end" in pf.last_decline_reason and "B.Cu" in pf.last_decline_reason


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_empty_filtered_layer_set_declines(use_cpp: bool) -> None:
    """No permitted layer at all: decline instead of routing on the pad layer."""
    net_class = NetClassRouting(name="PWR", avoid_layers=[0])
    pf, result = _route_smd(
        use_cpp, _smd_pads(), allowed_layers=["F.Cu"], net_class_map={"D-": net_class}
    )
    if result is not None:
        segs = _segment_layers(result)
        assert Layer.F_CU not in segs, f"forbidden F.Cu used: {segs}"
    assert result is None
    assert pf.last_decline_reason is not None
    assert "no permitted" in pf.last_decline_reason


def _hard_avoid_f_cu_router() -> Autorouter:
    """Two-component pair whose class hard-avoids F.Cu; all pads SMD on F.Cu."""
    rules = DesignRules(
        trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1, strict_layers=True
    )
    nc = NetClassRouting(name="HS", coupled_routing=True, avoid_layers=[0])
    router = Autorouter(
        width=30.0, height=10.0, rules=rules, net_class_map={"USB_D+": nc, "USB_D-": nc}
    )
    for ref, x in (("U1", 5.0), ("J1", 25.0)):
        router.add_component(
            ref,
            [
                {"number": "1", "x": x, "y": 4.6, "width": 0.4, "height": 0.4,
                 "net": 1, "net_name": "USB_D+"},
                {"number": "2", "x": x, "y": 5.4, "width": 0.4, "height": 0.4,
                 "net": 2, "net_name": "USB_D-"},
            ],
        )  # fmt: skip
    return router


@pytest.mark.parametrize("use_cpp", BACKENDS)
def test_declined_pair_takes_the_fallback_without_f_cu_copper(
    use_cpp: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the declined pair falls back and still never lands on F.Cu."""
    monkeypatch.setattr(DiffPairRouter, "_shadow_route_pair", lambda self, *a, **k: None)
    real_init = CoupledPathfinder.__init__

    def init(self, *args, **kwargs):  # force the backend under test
        real_init(self, *args, **kwargs)
        self._use_cpp_coupled = use_cpp

    monkeypatch.setattr(CoupledPathfinder, "__init__", init)
    router = _hard_avoid_f_cu_router()
    router._diffpair.enable_pose_centerline = False
    [pair] = router._diffpair.detect_differential_pairs()
    routes, _ = router._diffpair.route_differential_pair_coupled(
        pair, per_pair_timeout=30.0, per_pair_max_iterations=20_000
    )
    out = capsys.readouterr().out
    segs = _segment_layers(routes)
    assert Layer.F_CU not in segs, f"hard-avoided F.Cu used: {segs}"
    assert not router._committed_coupled_nets
    assert "[coupled-declined]" in out and "F.Cu" in out, out
