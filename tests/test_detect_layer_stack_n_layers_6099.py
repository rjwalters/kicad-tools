"""8+-layer boards are routed on their real copper stack (Issue #6099).

``detect_layer_stack`` used to collapse every copper count other than 2, 4 and
6 to a 2-layer stack, silently: ``kct route`` routed an 8-layer board on
F.Cu/B.Cu only, keepouts on ``In3.Cu`` resolved to nothing, and the
``route-auto --strategy hierarchical`` layer cap (#6059) became 2.

These tests pin the N-layer path end to end -- detection (both KiCad layer
numberings), the router enum, keepout resolution, routing on the deepest inner
layer, the ``--auto-layers`` ladder and the hierarchical cap -- and pin that
2-, 4- and 6-layer detection still returns the historical presets exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.core.types import (
    CopperLayer,
    copper_layers_in_span,
    copper_span,
    copper_span_contains,
)
from kicad_tools.router.io import detect_layer_stack
from kicad_tools.router.layers import LayerStack, LayerType
from kicad_tools.router.rule_area_resolve import resolve_layer_spec
from tests.test_grid_keepout_rule_areas_6008 import (
    BACKENDS,
    WALL_X0,
    WALL_X1,
    _board,
    _load,
    _route,
    _segments,
    _wall,
)


def _copper_rows(n: int, *, kicad10: bool) -> list[str]:
    """``(layers ...)`` copper rows for an ``n``-layer board, in KiCad's row order.

    ``kicad10`` uses the KiCad 10 file format's non-contiguous ordinals
    (F.Cu=0, B.Cu=2, In1.Cu=4, In2.Cu=6, ...); otherwise the legacy F.Cu=0,
    InN.Cu=N, B.Cu=31 that KiCad 9 (``20241229``) and older still write.
    """
    inner = range(1, n - 1)
    if kicad10:
        return [
            '    (0 "F.Cu" signal)',
            *(f'    ({2 + 2 * i} "In{i}.Cu" signal)' for i in inner),
            '    (2 "B.Cu" signal)',
        ]
    return [
        '    (0 "F.Cu" signal)',
        *(f'    ({i} "In{i}.Cu" signal)' for i in inner),
        '    (31 "B.Cu" signal)',
    ]


# KiCad 10 renumbered the non-copper layers too: Edge.Cuts is 25, and every
# even ordinal is copper (44 would be In20.Cu).
_EDGE_CUTS = {True: '(25 "Edge.Cuts" user)', False: '(44 "Edge.Cuts" user)'}


def _layer_table(n: int, *, kicad10: bool = True, extra: str = "") -> str:
    rows = "\n".join(_copper_rows(n, kicad10=kicad10))
    return f"(kicad_pcb\n  (layers\n{rows}\n    {_EDGE_CUTS[kicad10]}\n  )\n{extra})\n"


def _with_copper(text: str, n: int, *, kicad10: bool = True) -> str:
    """Swap the 2-layer table of a #6008 test board for an ``n``-layer one.

    The KiCad 10 variant also bumps the file version and renumbers Edge.Cuts:
    KiCad 10 ordinals in an older-format file are a different board to KiCad
    (kicad-cli's zone-fill round trip would move In6.Cu copper to B.Cu).
    """
    two = '    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)'
    assert two in text
    text = text.replace(two, "\n".join(_copper_rows(n, kicad10=kicad10)))
    if kicad10:
        assert "(version 20240108)" in text and _EDGE_CUTS[False] in text
        text = text.replace("(version 20240108)", "(version 20260206)")
        text = text.replace(_EDGE_CUTS[False], _EDGE_CUTS[True])
    return text


def _names(n: int) -> list[str]:
    return ["F.Cu", *(f"In{i}.Cu" for i in range(1, n - 1)), "B.Cu"]


def _zone(layer: str, net: str) -> str:
    return f'  (zone (net 1) (net_name "{net}") (layer "{layer}") (hatch edge 0.5))\n'


# ---------------------------------------------------------------------------
# The router copper-layer enum reaches In30.Cu
# ---------------------------------------------------------------------------


def test_copper_layer_enum_keeps_historical_values() -> None:
    assert [(m.name, m.value) for m in list(CopperLayer)[:6]] == [
        ("F_CU", 0),
        ("IN1_CU", 1),
        ("IN2_CU", 2),
        ("IN3_CU", 3),
        ("IN4_CU", 4),
        ("B_CU", 5),
    ]


def test_copper_layer_enum_covers_kicad_maximum() -> None:
    assert len(CopperLayer) == 32
    assert {m.kicad_name for m in CopperLayer} == set(_names(32))
    for name in _names(32):
        assert CopperLayer.from_kicad_name(name).kicad_name == name
    with pytest.raises(ValueError):
        CopperLayer.from_kicad_name("In31.Cu")


def test_stack_order_is_physical_order() -> None:
    ordered = sorted(CopperLayer, key=lambda m: m.stack_order)
    assert [m.kicad_name for m in ordered] == _names(32)
    # It agrees with ``value`` on the six historical members.
    six = [m for m in CopperLayer if m.value <= 5]
    assert sorted(six, key=lambda m: m.value) == sorted(six, key=lambda m: m.stack_order)


def test_via_span_helpers_use_physical_order() -> None:
    f, b = CopperLayer.F_CU, CopperLayer.B_CU
    in5, in6 = CopperLayer.IN5_CU, CopperLayer.IN6_CU
    # Merging F..In5 with In5..B is a through via, not F..In5 (value order).
    assert copper_span(f, in5, in5, b) == (f, b)
    assert copper_span_contains(f, b, in6)
    assert copper_span_contains(b, in5, in6)
    assert not copper_span_contains(f, CopperLayer.IN4_CU, in5)
    assert copper_layers_in_span(CopperLayer.IN4_CU, in6) == {CopperLayer.IN4_CU, in5, in6}


# ---------------------------------------------------------------------------
# detect_layer_stack
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [8, 10, 12, 32])
@pytest.mark.parametrize("kicad10", [True, False], ids=["kicad10-ordinals", "legacy-ordinals"])
def test_detects_n_layer_stack_in_physical_order(n: int, kicad10: bool) -> None:
    stack = detect_layer_stack(_layer_table(n, kicad10=kicad10))
    assert stack.num_layers == n
    assert [layer.name for layer in stack.layers] == _names(n)
    assert [layer.index for layer in stack.layers] == list(range(n))
    assert [layer.is_outer for layer in stack.layers] == [True] + [False] * (n - 2) + [True]
    # No zones and no ``power`` layers: nothing to call a plane.
    assert all(layer.layer_type == LayerType.SIGNAL for layer in stack.layers)
    assert stack.get_routable_indices() == list(range(n))
    # Every layer maps onto the router enum (no silent F.Cu fallback).
    assert [layer.layer_enum.kicad_name for layer in stack.layers] == _names(n)
    assert [stack.index_to_layer_enum(i).kicad_name for i in range(n)] == _names(n)


def test_row_order_does_not_matter() -> None:
    """Layers are ordered by name, not by row or ordinal."""
    rows = _copper_rows(8, kicad10=True)
    shuffled = [rows[0], rows[-1], *reversed(rows[1:-1])]
    text = "(kicad_pcb\n  (layers\n" + "\n".join(shuffled) + "\n  )\n)\n"
    assert [layer.name for layer in detect_layer_stack(text).layers] == _names(8)


def test_zones_and_power_layers_become_planes() -> None:
    text = _layer_table(8, extra=_zone("In1.Cu", "GND") + _zone("In6.Cu", "+3V3"))
    text = text.replace('"In3.Cu" signal', '"In3.Cu" power')
    stack = detect_layer_stack(text)
    kinds = {layer.name: layer.layer_type for layer in stack.layers}
    assert [n for n, t in kinds.items() if t == LayerType.PLANE] == ["In1.Cu", "In3.Cu", "In6.Cu"]
    planes = {layer.name: layer.plane_net for layer in stack.plane_layers}
    assert planes == {"In1.Cu": "GND", "In3.Cu": "", "In6.Cu": "+3V3"}
    refs = {layer.name: layer.reference_plane for layer in stack.layers}
    assert refs["F.Cu"] == "In1.Cu"
    assert refs["In2.Cu"] == "In1.Cu"  # the plane above wins a tie
    assert refs["In4.Cu"] == "In3.Cu"
    assert refs["In5.Cu"] == "In6.Cu"
    assert refs["B.Cu"] == "In6.Cu"


def test_outer_zone_never_makes_a_plane() -> None:
    stack = detect_layer_stack(_layer_table(8, extra=_zone("F.Cu", "GND")))
    assert stack.plane_layers == []


@pytest.mark.parametrize(
    ("n", "preset"),
    [
        (2, LayerStack.two_layer),
        (4, LayerStack.four_layer_sig_sig_gnd_pwr),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig),
    ],
)
@pytest.mark.parametrize("kicad10", [True, False], ids=["kicad10-ordinals", "legacy-ordinals"])
def test_two_four_six_layers_keep_their_presets(n: int, preset, kicad10: bool) -> None:
    stack = detect_layer_stack(_layer_table(n, kicad10=kicad10))
    expected = preset()
    assert (stack.name, stack.description, stack.layers) == (
        expected.name,
        expected.description,
        expected.layers,
    )


def test_four_layer_with_inner_zone_unchanged() -> None:
    stack = detect_layer_stack(_layer_table(4, extra=_zone("In1.Cu", "GND")))
    assert stack.name == "4-Layer (auto-detected)"
    assert [(layer.name, layer.layer_type, layer.plane_net) for layer in stack.layers] == [
        ("F.Cu", LayerType.SIGNAL, ""),
        ("In1.Cu", LayerType.PLANE, "GND"),
        # Historical quirk kept byte-identical: a zone-less In2.Cu gets "".
        ("In2.Cu", LayerType.PLANE, ""),
        ("B.Cu", LayerType.SIGNAL, ""),
    ]


def test_unrecognised_copper_names_warn(caplog) -> None:
    text = _layer_table(8).replace('"In6.Cu"', '"Weird.Cu"')
    stack = detect_layer_stack(text)
    assert stack.num_layers == 7
    assert "Weird.Cu" in caplog.text
    assert "odd number of copper layers" in caplog.text


# ---------------------------------------------------------------------------
# Keepout rule areas resolve against the real stack
# ---------------------------------------------------------------------------


def test_keepout_on_in3_resolves_on_eight_layers() -> None:
    stack = detect_layer_stack(_layer_table(8))
    assert resolve_layer_spec(["In3.Cu"], stack) == frozenset({3})
    assert resolve_layer_spec(["In6.Cu"], stack) == frozenset({6})
    assert resolve_layer_spec(["*.In.Cu"], stack) == frozenset(range(1, 7))
    assert resolve_layer_spec(["F&B.Cu"], stack) == frozenset({0, 7})


# ---------------------------------------------------------------------------
# kct route: the Autorouter routes on the deepest inner layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force_python", BACKENDS)
@pytest.mark.parametrize("n", [8, 10])
def test_router_routes_through_the_only_open_inner_layer(
    tmp_path: Path, n: int, force_python: bool
) -> None:
    """Every layer but the deepest inner one is walled: the route must use it."""
    if force_python and n > 8:
        pytest.skip("pure-Python A* on 10 layers is slow; 8 layers covers the path")
    deepest = f"In{n - 2}.Cu"
    walled = " ".join(f'"{name}"' for name in _names(n) if name != deepest)
    text = _with_copper(_board(_wall(layers=walled, vias="allowed")), n)

    router = _load(tmp_path, text, force_python)
    assert router.layer_stack.num_layers == n
    assert router.grid.num_layers == n
    assert [layer.name for layer in router.layer_stack.layers] == _names(n)

    routes = _route(router)
    crossing = [
        s for s in _segments(routes) if max(s.x1, s.x2) > WALL_X0 and min(s.x1, s.x2) < WALL_X1
    ]
    assert crossing, "the net must be routed across the wall"
    assert {s.layer.kicad_name for s in crossing} == {deepest}


@pytest.mark.parametrize("kicad10", [True, False], ids=["kicad10-ordinals", "legacy-ordinals"])
def test_kct_route_writes_copper_on_the_deepest_inner_layer(tmp_path: Path, kicad10: bool) -> None:
    """End to end through the CLI writer (and kicad-cli's zone-fill round trip, when present)."""
    from kicad_tools.cli import main as kct_main
    from kicad_tools.schema.pcb import PCB

    walled = " ".join(f'"{name}"' for name in _names(8) if name != "In6.Cu")
    src = tmp_path / "board.kicad_pcb"
    src.write_text(_with_copper(_board(_wall(layers=walled, vias="allowed")), 8, kicad10=kicad10))
    out = tmp_path / "out.kicad_pcb"
    rc = kct_main(
        [
            "route",
            str(src),
            "-o",
            str(out),
            "--grid",
            "0.1",
            "--clearance",
            "0.15",
            "--no-auto-layers",
        ]
    )
    assert rc == 0
    crossing = [
        s
        for s in PCB.load(str(out)).segments
        if max(s.start[0], s.end[0]) > WALL_X0 - 100 and min(s.start[0], s.end[0]) < WALL_X1 - 100
    ]
    assert crossing
    assert {s.layer for s in crossing} == {"In6.Cu"}


# ---------------------------------------------------------------------------
# kct route --auto-layers ladder
# ---------------------------------------------------------------------------


def _ladder() -> list[tuple[int, LayerStack]]:
    return [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]


def test_auto_layers_ladder_ends_on_the_board_stack(tmp_path: Path) -> None:
    from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_layer_table(8))
    ladder = _filter_layer_configs_for_pcb(_ladder(), pcb, 8, quiet=True)
    assert [n for n, _ in ladder] == [8]
    assert [layer.name for layer in ladder[0][1].layers] == _names(8)


def test_auto_layers_cap_below_board_count_still_warns(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_layer_table(8))
    ladder = _filter_layer_configs_for_pcb(_ladder(), pcb, 6)
    assert max(n for n, _ in ladder) == 6
    assert "below the PCB's declared copper count (8)" in capsys.readouterr().out


@pytest.mark.parametrize("n", [2, 4, 6])
def test_auto_layers_ladder_unchanged_for_preset_counts(tmp_path: Path, n: int) -> None:
    from kicad_tools.cli.route_cmd import _filter_layer_configs_for_pcb

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_layer_table(n))
    ladder = _filter_layer_configs_for_pcb(_ladder(), pcb, 6, quiet=True)
    assert [(k, s.name) for k, s in ladder] == [(k, s.name) for k, s in _ladder() if k >= n]


def test_max_layers_flag_accepts_eight() -> None:
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["route", "board.kicad_pcb", "--max-layers", "8"])
    assert args.max_layers == 8


# ---------------------------------------------------------------------------
# kct route-auto --strategy hierarchical: cap and ladder
# ---------------------------------------------------------------------------


def _hierarchical(tmp_path: Path, n: int):
    from kicad_tools.mcp.tools.routing import _build_pads_for_net
    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules
    from kicad_tools.schema.pcb import PCB

    text = _board()
    if n != 2:
        text = _with_copper(text, n)
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    pcb = PCB.load(str(path))
    orchestrator = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=DesignRules(),
        layer_stack=detect_layer_stack(text),
    )
    orchestrator._route_hierarchical("/SIG", None, _build_pads_for_net(pcb, 1, "/SIG"))
    assert orchestrator._hierarchical is not None
    return orchestrator._hierarchical


@pytest.mark.parametrize("n", [8, 10])
def test_hierarchical_cap_is_board_copper_count(tmp_path: Path, n: int) -> None:
    adaptive = _hierarchical(tmp_path, n)
    assert adaptive.max_layers == n
    ladder = adaptive._stacks_to_try()
    assert [s.num_layers for s in ladder] == [2, 4, 6, n]
    assert [layer.name for layer in ladder[-1].layers] == _names(n)


@pytest.mark.parametrize(("n", "expected"), [(2, [2]), (4, [2, 4]), (6, [2, 4, 6])])
def test_hierarchical_ladder_unchanged_for_preset_counts(
    tmp_path: Path, n: int, expected: list[int]
) -> None:
    adaptive = _hierarchical(tmp_path, n)
    assert adaptive.max_layers == n
    ladder = adaptive._stacks_to_try()
    assert [s.num_layers for s in ladder] == expected
    assert all(any(s is preset for preset in adaptive.LAYER_STACKS) for s in ladder)
