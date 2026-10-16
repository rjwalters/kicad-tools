"""Per-net-class ``allow_plane_layers`` opt-in (Issue #6876, ruling on #6824).

``--reserve-plane-layers`` (on by default) sets ``DesignRules.allowed_layers``
to the non-PLANE layers, so no class could reach a reserved plane.  A class
now opts in to named plane layers through
``NetClassRouting.allow_plane_layers``; every layer-permission path consults
the one shared predicate :func:`~kicad_tools.router.rules.layer_permitted`.

Each path is pinned here with an opted-in case that fails on main and a
control without the field (unchanged behaviour).  The lattice engine has its
own module, ``tests/router/lattice/test_allow_plane_layers_lattice.py``.

Fixture stack: ``four_layer_sig_gnd_pwr_sig`` (F=0, In1=1 GND, In2=2 PWR,
B=3) with ``allowed_layers=["F.Cu", "B.Cu"]``.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layer_advisories import plane_layer_signal_violations
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pair_layer_filter import pair_permitted_layers
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Obstacle, Pad, Route, Segment
from kicad_tools.router.rules import (
    DesignRules,
    NetClassRouting,
    allowed_plane_layer_indices,
    layer_permitted,
)

IN1, IN2, B_CU = 1, 2, 3
RESERVED = ["F.Cu", "B.Cu"]
STACK = LayerStack.four_layer_sig_gnd_pwr_sig
NAMES = {0: "F.Cu", 1: "In1.Cu", 2: "In2.Cu", 3: "B.Cu"}

BACKENDS = [
    pytest.param(
        "cpp",
        id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="router_cpp not built"),
    ),
    pytest.param("python", id="python"),
]


def _opted(name: str = "USB", layers: list | None = None, **kw) -> NetClassRouting:
    return NetClassRouting(name=name, allow_plane_layers=[IN2] if layers is None else layers, **kw)


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


def test_layer_permitted_predicate() -> None:
    rules = DesignRules(allowed_layers=RESERVED)
    plain = NetClassRouting(name="Plain")
    opted = _opted()
    assert layer_permitted(rules, None, 0, "F.Cu")
    assert not layer_permitted(rules, None, IN2, "In2.Cu")
    assert not layer_permitted(rules, plain, IN2, "In2.Cu")
    assert layer_permitted(rules, opted, IN2, "In2.Cu")
    # Only the named plane: In1.Cu stays reserved for the opted class.
    assert not layer_permitted(rules, opted, IN1, "In1.Cu")
    # No gate -> everything permitted.
    assert layer_permitted(DesignRules(), plain, IN1, "In1.Cu")
    # In-process names resolve too.
    assert layer_permitted(rules, _opted(layers=["In2.Cu"]), IN2, "In2.Cu")
    assert allowed_plane_layer_indices(_opted(layers=["In2.Cu"])) == frozenset({IN2})


# ---------------------------------------------------------------------------
# Grid pathfinder + C++ backend (single-ended)
# ---------------------------------------------------------------------------


def _tht(x: float, net: int, name: str, ref: str) -> Pad:
    return Pad(
        x=x, y=5.0, width=1.0, height=1.0, net=net, net_name=name, layer=Layer.F_CU,
        ref=ref, pin="1", through_hole=True, drill=0.6,
    )  # fmt: skip


def _walled_grid(rules: DesignRules, pads: list[Pad]) -> RoutingGrid:
    grid = RoutingGrid(width=20.0, height=10.0, rules=rules, layer_stack=STACK())
    for pad in pads:
        grid.add_pad(pad)
    # Seal BOTH outer layers at x=10: the only crossing is on an inner layer.
    for layer in (Layer.F_CU, Layer.B_CU):
        grid.add_obstacle(Obstacle(x=10.0, y=5.0, width=2.0, height=16.0, layer=layer))
    return grid


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.25,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
        allowed_layers=list(RESERVED),
    )


def _single_ended(backend: str, net_class_map: dict[str, NetClassRouting]):
    rules = _rules()
    pads = [_tht(3.0, 1, "SIG", "J1"), _tht(17.0, 1, "SIG", "J2")]
    grid = _walled_grid(rules, pads)
    if backend == "cpp":
        from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

        pf = CppPathfinder(
            CppGrid.from_routing_grid(grid), rules, diagonal_routing=True,
            net_class_map=net_class_map,
        )  # fmt: skip
    else:
        pf = Router(grid, rules, net_class_map=net_class_map)
    return pf, pf.route(pads[0], pads[1])


@pytest.mark.parametrize("backend", BACKENDS)
def test_single_ended_control_without_opt_in_declines(backend: str) -> None:
    _pf, route = _single_ended(backend, {"SIG": NetClassRouting(name="Plain")})
    if route is not None:
        assert not {s.layer for s in route.segments} & {Layer.IN1_CU, Layer.IN2_CU}


@pytest.mark.parametrize("backend", BACKENDS)
def test_single_ended_opt_in_routes_on_in2(backend: str) -> None:
    """Fails on main: the reserved In2.Cu was unreachable for every class."""
    _pf, route = _single_ended(backend, {"SIG": _opted()})
    assert route is not None, f"[{backend}] opted-in net must cross on In2.Cu"
    layers = {s.layer for s in route.segments}
    assert Layer.IN2_CU in layers, layers
    assert Layer.IN1_CU not in layers, f"[{backend}] In1.Cu was not opted in: {layers}"


@pytest.mark.parametrize("backend", BACKENDS)
def test_hard_avoid_wins_over_opt_in(backend: str) -> None:
    nc = _opted(avoid_layers=[IN2], target_ampacity=1.0)
    _pf, route = _single_ended(backend, {"SIG": nc})
    if route is not None:
        assert Layer.IN2_CU not in {s.layer for s in route.segments}


@pytest.mark.skipif(not is_cpp_available(), reason="router_cpp not built")
def test_cpp_routable_layers_restored_for_next_net() -> None:
    """The per-call widening is undone: the next (plain) net stays off In2.Cu."""
    from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder

    rules = _rules()
    pads = [
        _tht(3.0, 1, "SIG", "J1"),
        _tht(17.0, 1, "SIG", "J2"),
        Pad(x=3.0, y=8.5, width=1.0, height=1.0, net=2, net_name="OTHER", layer=Layer.F_CU,
            ref="J3", pin="1", through_hole=True, drill=0.6),
        Pad(x=17.0, y=8.5, width=1.0, height=1.0, net=2, net_name="OTHER", layer=Layer.F_CU,
            ref="J4", pin="1", through_hole=True, drill=0.6),
    ]  # fmt: skip
    grid = _walled_grid(rules, pads)
    pf = CppPathfinder(
        CppGrid.from_routing_grid(grid), rules, diagonal_routing=True,
        net_class_map={"SIG": _opted(), "OTHER": NetClassRouting(name="Plain")},
    )  # fmt: skip
    assert pf._routable_layers == [0, B_CU]
    first = pf.route(pads[0], pads[1])
    assert first is not None and Layer.IN2_CU in {s.layer for s in first.segments}
    assert pf._routable_layers == [0, B_CU], "widening must be restored"
    second = pf.route(pads[2], pads[3])
    if second is not None:
        assert not {s.layer for s in second.segments} & {Layer.IN1_CU, Layer.IN2_CU}


# ---------------------------------------------------------------------------
# Coupled search + tie step filter (both nets must opt in)
# ---------------------------------------------------------------------------


def test_pair_filter_requires_both_nets_to_opt_in() -> None:
    rules = DesignRules(allowed_layers=RESERVED)
    base = [0, 1, 2, 3]
    plain = NetClassRouting(name="Plain")
    both = pair_permitted_layers(base, NAMES.__getitem__, rules, (_opted(), _opted()))
    assert both == [0, IN2, B_CU]
    one = pair_permitted_layers(base, NAMES.__getitem__, rules, (_opted(), plain))
    assert one == [0, B_CU]
    classless = pair_permitted_layers(base, NAMES.__getitem__, rules, (_opted(), None))
    assert classless == [0, B_CU]
    # Control: no opt-in anywhere is unchanged.
    assert pair_permitted_layers(base, NAMES.__getitem__, rules, (plain, plain)) == [0, B_CU]


def _coupled_pads() -> tuple[Pad, Pad, Pad, Pad]:
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


def _route_coupled(use_cpp: bool, net_class_map: dict[str, NetClassRouting]):
    rules = DesignRules(
        trace_width=0.2, trace_clearance=0.1, via_drill=0.2, via_diameter=0.3,
        via_clearance=0.05, grid_resolution=0.1, min_hole_to_hole=0.5,
        allowed_layers=list(RESERVED),
    )  # fmt: skip
    grid = RoutingGrid(width=6.0, height=3.0, rules=rules, layer_stack=STACK())
    grid.add_keepout(3.0, 0.0, 3.0, 3.0, layers=[Layer.F_CU, Layer.B_CU])
    pads = _coupled_pads()
    for item in pads:
        grid.add_pad(item)
    pf = CoupledPathfinder(
        grid=grid, rules=rules, target_spacing_cells=6, min_spacing_cells=6,
        net_class_map=net_class_map,
    )  # fmt: skip
    pf._use_cpp_coupled = use_cpp
    result = pf.route_coupled(*pads, max_iterations_budget=200_000)
    return pf, result


COUPLED_BACKENDS = [
    pytest.param(
        True, id="cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="router_cpp not built"),
    ),
    pytest.param(False, id="python"),
]  # fmt: skip


@pytest.mark.parametrize("use_cpp", COUPLED_BACKENDS)
def test_coupled_layers_include_in2_when_both_opt_in(use_cpp: bool) -> None:
    pf, _ = _route_coupled(use_cpp, {"D+": _opted(), "D-": _opted()})
    assert pf._coupled_routable_layers("D+", "D-", 0) == [0, IN2, B_CU]
    pf, _ = _route_coupled(use_cpp, {"D+": _opted(), "D-": NetClassRouting(name="Plain")})
    assert pf._coupled_routable_layers("D+", "D-", 0) == [0, B_CU]


@pytest.mark.parametrize("use_cpp", COUPLED_BACKENDS)
def test_coupled_pair_routes_on_in2_when_outer_layers_walled(use_cpp: bool) -> None:
    """Counterpart of ``test_reserved_planes_never_routed_when_outer_layers_walled``."""
    _pf, result = _route_coupled(use_cpp, {"D+": _opted(), "D-": _opted()})
    assert result is not None, "both nets opted in to In2.Cu: the pair can cross there"
    layers = {seg.layer for route in result for seg in route.segments}
    assert Layer.IN2_CU in layers, layers
    assert Layer.IN1_CU not in layers, layers


@pytest.mark.parametrize("use_cpp", COUPLED_BACKENDS)
def test_coupled_pair_with_one_side_opted_stays_off_plane(use_cpp: bool) -> None:
    _pf, result = _route_coupled(use_cpp, {"D+": _opted(), "D-": NetClassRouting(name="Plain")})
    if result is not None:
        layers = {seg.layer for route in result for seg in route.segments}
        assert not layers & {Layer.IN1_CU, Layer.IN2_CU}, layers


def test_lattice_pair_class_carries_only_the_common_opt_in() -> None:
    """The lattice ``CoupledConnection`` holds one class: intersect both nets'."""
    from kicad_tools.router.core import Autorouter

    p, n = _opted("P", [IN1, IN2]), _opted("N", [IN2])
    merged = Autorouter._pair_plane_opt_in_class(p, p, n)
    assert merged.allow_plane_layers == [IN2]
    assert merged.name == "P"
    assert Autorouter._pair_plane_opt_in_class(p, p, None).allow_plane_layers is None
    same = _opted()
    assert Autorouter._pair_plane_opt_in_class(same, same, same) is same
    plain = NetClassRouting(name="Plain")
    assert Autorouter._pair_plane_opt_in_class(plain, plain, plain) is plain


# ---------------------------------------------------------------------------
# Mesh engine
# ---------------------------------------------------------------------------


def test_mesh_blocked_layers_exclude_opted_in_layer() -> None:
    from kicad_tools.router.mesh.pathfinder import MeshPathfinder

    rules = DesignRules(allowed_layers=list(RESERVED))
    pf = MeshPathfinder.__new__(MeshPathfinder)
    pf.rules = rules
    pf.layer_stack = STACK()
    assert pf._blocked_layers(None) == frozenset({IN1, IN2})
    assert pf._blocked_layers(NetClassRouting(name="Plain")) == frozenset({IN1, IN2})
    assert pf._blocked_layers(_opted()) == frozenset({IN1})
    # Hard avoid still wins.
    hard = _opted(avoid_layers=[IN2], target_ampacity=1.0)
    assert pf._blocked_layers(hard) == frozenset({IN1, IN2})


# ---------------------------------------------------------------------------
# Post-route reference-plane warning
# ---------------------------------------------------------------------------


def _route_on(net: str, layer: Layer, net_id: int) -> Route:
    seg = Segment(x1=0.0, y1=0.0, x2=1.0, y2=0.0, width=0.2, layer=layer, net=net_id,
                  net_name=net)  # fmt: skip
    return Route(net=net_id, net_name=net, segments=[seg])


def test_plane_warning_skips_opted_in_pairs_only() -> None:
    stack = STACK()
    routes = [
        _route_on("USB_D+", Layer.IN2_CU, 1),  # opted in -> suppressed
        _route_on("USB_D+", Layer.IN1_CU, 1),  # opted net, other plane -> warns
        _route_on("LED", Layer.IN2_CU, 2),  # plain net on the plane -> warns
    ]
    ncm = {"USB_D+": _opted(), "LED": NetClassRouting(name="Plain")}
    got = {(v.net_name, v.layer_name) for v in plane_layer_signal_violations(routes, stack, ncm)}
    assert got == {("USB_D+", "In1.Cu"), ("LED", "In2.Cu")}
    # Without the map everything is reported (unchanged default).
    assert len(plane_layer_signal_violations(routes, stack)) == 3


def test_route_cmd_audit_passes_the_net_class_map(capsys) -> None:
    from kicad_tools.cli.route_cmd import _audit_plane_layer_reservation

    class _FakeRouter:
        routes = [_route_on("USB_D+", Layer.IN2_CU, 1)]
        net_class_map = {"USB_D+": _opted()}

    assert _audit_plane_layer_reservation(_FakeRouter(), STACK()) == []
