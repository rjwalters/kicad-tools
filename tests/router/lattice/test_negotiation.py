"""Lattice-engine negotiation + acceptance tests (issue #4278).

The load-bearing acceptance criteria from the issue:

1. Board 02 charlieplex under REAL negotiation + REAL ``kicad-cli pcb drc
   --refill-zones``: 0 shorts (hard), >= 17/24 completion (hard floor),
   24/24 target.
2. ``lattice_builds == 1`` across the whole negotiation (static substrate).
4. Never-ship-a-short: a connection that cannot clear DECLINES.
6. N-layer via the real ``LayerStack``; via edges adjacent-layer only,
   emitted vias are through-vias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import run_geometric_drc
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

_REPO = Path(__file__).resolve().parents[3]
_CHARLIEPLEX = _REPO / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"


def _pads_by_net(pads: list[Pad]) -> dict[int, list[Pad]]:
    bynet: dict[int, list[Pad]] = {}
    for p in pads:
        if p.net > 0:
            bynet.setdefault(p.net, []).append(p)
    return bynet


def _connections(pads: list[Pad]) -> list[tuple[object, Pad, Pad, object]]:
    """Star-wise 2-pad connections keyed (net, seq) -- the dispatch topology."""
    conns: list[tuple[object, Pad, Pad, object]] = []
    for net, ps in _pads_by_net(pads).items():
        anchor = ps[0]
        for seq, other in enumerate(ps[1:]):
            conns.append(((net, seq), anchor, other, None))
    return conns


def _pad(
    x: float,
    y: float,
    net: int,
    *,
    ref: str,
    layer: Layer = Layer.F_CU,
    width: float = 1.0,
    height: float = 1.0,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=f"N{net}",
        layer=layer,
        ref=ref,
        pin="1",
    )


# ---------------------------------------------------------------------------
# Static substrate: the lattice is built ONCE per board (acceptance 2).
# ---------------------------------------------------------------------------


def test_lattice_builds_exactly_once_across_negotiation() -> None:
    from kicad_tools.router.io import load_pads_for_analysis

    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)
    assert len(conns) >= 3, "need several nets to prove once != per-net"

    pf = LatticePathfinder.from_board(text)
    _routes, stats = pf.route_netset(conns, max_iterations=4)

    # ONE lattice build for the whole board, across every net AND every
    # negotiation iteration -- the static-substrate invariant (mirrors the
    # mesh's triangulation_calls == 1 discipline).
    assert pf.lattice_builds == 1
    assert stats.lattice_builds == 1


def test_build_is_idempotent_single_lattice() -> None:
    text = _CHARLIEPLEX.read_text()
    pf = LatticePathfinder.from_board(text)
    lat1 = pf.build()
    lat2 = pf.build()
    assert lat1 is lat2
    assert pf.lattice_builds == 1


# ---------------------------------------------------------------------------
# Board 02 acceptance: completion floor + real kicad-cli DRC (acceptance 1).
# ---------------------------------------------------------------------------


def test_charlieplex_negotiation_meets_completion_floor() -> None:
    from kicad_tools.router.io import load_pads_for_analysis

    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)
    assert len(conns) == 24

    pf = LatticePathfinder.from_board(text)
    routes, stats = pf.route_netset(conns, max_iterations=8)
    assert stats.total == 24
    # Hard floor from the issue: >= 17/24 (strictly above everything the
    # navmesh line ever measured); the spike-parity target is 24/24.
    assert stats.routed >= 17, (
        f"completion {stats.routed}/24 below the hard floor; declines: {pf.failure_reasons}"
    )
    # Every shortfall must be a decline with a diagnosis, never a short.
    assert len(pf.failure_reasons) == 24 - stats.routed
    assert stats.lattice_builds == 1


def test_charlieplex_netset_is_drc_clean(tmp_path: Path) -> None:
    """The whole negotiated net set emits DRC-clean copper (0 shorts).

    ``kicad-cli pcb drc --refill-zones`` is the authoritative gate
    (``drc/geometric.py``) -- ``kct check`` alone is insufficient (standing
    process rule).
    """
    from kicad_tools.router.io import load_pads_for_analysis, merge_routes_into_pcb

    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)

    base_pcb = tmp_path / "base.kicad_pcb"
    base_pcb.write_text(text)
    base = run_geometric_drc(base_pcb)
    if not base.ran:
        pytest.skip(f"kicad-cli DRC unavailable: {base.reason}")

    pf = LatticePathfinder.from_board(text)
    routes, stats = pf.route_netset(conns, max_iterations=8)
    assert stats.routed >= 17
    sexp = "".join(r.to_sexp() for r in routes.values() if r.segments)

    out = tmp_path / "routed.kicad_pcb"
    out.write_text(merge_routes_into_pcb(text, sexp))
    res = run_geometric_drc(out)
    assert res.ran
    assert res.error_count <= base.error_count, (
        f"lattice net set introduced DRC errors: base={base.error_count} "
        f"routed={res.error_count} types={dict(res.by_type)}"
    )


# ---------------------------------------------------------------------------
# Never-ship-a-short (acceptance 4, #3906-modeled).
# ---------------------------------------------------------------------------


def test_committed_copper_forces_detour_or_decline_never_a_cross() -> None:
    """Two nets through one narrow slot: the second must detour around the
    first's committed copper or decline -- emitted copper never crosses."""
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    # Other-net wall pads pinch the middle of the board into a slot barely
    # wider than one trace pitch.
    walls = [
        _pad(15.0, 9.0, 9, ref="WT", width=1.0, height=6.0),
        _pad(15.0, 2.0, 9, ref="WB", width=1.0, height=4.0),
    ]
    a1, a2 = _pad(3.0, 5.5, 1, ref="A1"), _pad(27.0, 5.5, 1, ref="A2")
    b1, b2 = _pad(3.0, 6.5, 2, ref="B1"), _pad(27.0, 6.5, 2, ref="B2")
    pads = [a1, a2, b1, b2] + walls
    pf = LatticePathfinder(outline, pads, DesignRules(), LayerStack.two_layer())
    conns = [((1, 0), a1, a2, None), ((2, 0), b1, b2, None)]
    routes, stats = pf.route_netset(conns, max_iterations=6)
    assert stats.lattice_builds == 1
    assert stats.routed >= 1

    # THE invariant: whatever routed, no two different-net segments on the
    # same layer sit closer than the copper gap (a crossing would be 0).
    copper_gap = pf.rules.trace_width + pf.rules.trace_clearance
    flat: list[tuple[int, object, tuple, tuple]] = []
    for key, route in routes.items():
        for seg in route.segments:
            flat.append((route.net, seg.layer, (seg.x1, seg.y1), (seg.x2, seg.y2)))
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            n1, l1, p1, q1 = flat[i]
            n2, l2, p2, q2 = flat[j]
            if n1 == n2 or l1 != l2:
                continue
            d = seg_seg_dist(p1, q1, p2, q2)
            assert d >= copper_gap - 1e-6, (
                f"nets {n1}/{n2} shipped copper {d:.4f}mm apart (< {copper_gap})"
            )


def test_unroutable_connection_declines_with_reason() -> None:
    """A pad fully boxed in by other-net keep-outs must DECLINE (None).

    The walls overlap at the corners (no diagonal pinhole), so no path out
    of the box exists on the pad's layer and the box is sealed against a
    dip too (the walls block both attach layers of any via inside).
    """
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    target = _pad(10.0, 10.0, 1, ref="U1", width=0.6, height=0.6)
    mate = _pad(3.0, 3.0, 1, ref="U2")
    # Sealed ring: two full-width horizontal bars + two vertical bars whose
    # keep-outs overlap the horizontal ones at every corner, duplicated on
    # BOTH layers so the boxed pad cannot dip out either.
    walls = []
    for layer in (Layer.F_CU, Layer.B_CU):
        walls += [
            _pad(10.0, 11.2, 2, ref=f"WN{layer}", width=4.0, height=0.8, layer=layer),
            _pad(10.0, 8.8, 2, ref=f"WS{layer}", width=4.0, height=0.8, layer=layer),
            _pad(11.2, 10.0, 2, ref=f"WE{layer}", width=0.8, height=4.0, layer=layer),
            _pad(8.8, 10.0, 2, ref=f"WW{layer}", width=0.8, height=4.0, layer=layer),
        ]
    pf = LatticePathfinder(outline, [target, mate] + walls, DesignRules())
    assert pf.route(target, mate) is None

    routes, stats = pf.route_netset([((1, 0), target, mate, None)], max_iterations=2)
    assert stats.routed == 0
    assert routes == {}
    # Declined with a diagnosis -- never emitted crossing the walls.
    assert pf.failure_reasons[(1, 0)] in ("no-path", "pad-escape-start", "pad-escape-end")


# ---------------------------------------------------------------------------
# N-layer (acceptance 6): real LayerStack, adjacent-layer via edges,
# through-via emission.
# ---------------------------------------------------------------------------


def test_cross_layer_route_on_four_layer_stack_uses_through_vias() -> None:
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]
    stack = LayerStack.four_layer_all_signal()
    a = _pad(3.0, 5.0, 1, ref="A1", layer=Layer.F_CU)
    b = _pad(17.0, 5.0, 1, ref="A2", layer=Layer.B_CU)
    pf = LatticePathfinder(outline, [a, b], DesignRules(), stack)
    assert pf.num_layers == 4

    route = pf.route(a, b)
    assert route is not None
    assert route.vias, "cross-layer connection needs at least one via"
    for via in route.vias:
        # Only through-vias are ever generated (blind/buried N/A by
        # construction): every via spans the full stack.
        assert via.layers == (Layer.F_CU, Layer.B_CU)
    layers_used = {seg.layer for seg in route.segments}
    assert Layer.F_CU in layers_used and Layer.B_CU in layers_used


def test_via_hops_join_adjacent_layers_only() -> None:
    """The (node, layer) A* exposes via hops to L +/- 1 only; a 4-layer
    F.Cu -> B.Cu transition therefore traverses every intermediate layer
    state (and still emits a single deduplicated through-via per site)."""
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]
    stack = LayerStack.four_layer_all_signal()
    a = _pad(3.0, 5.0, 1, ref="A1", layer=Layer.F_CU)
    b = _pad(17.0, 5.0, 1, ref="A2", layer=Layer.B_CU)
    pf = LatticePathfinder(outline, [a, b], DesignRules(), stack)

    result, reason = pf._route_impl(
        a, b, None, committed=pf._fresh_committed(), history={}, present=0.0
    )
    assert result is not None, reason
    via_resources = [r for r in result.resources if r[0] == "v"]
    assert via_resources, "expected via resources on a cross-layer route"
    # 3 adjacent-layer hops share one node key -> ONE emitted through-via.
    distinct_sites = {(round(p[0], 4), round(p[1], 4)) for p in result.via_points}
    assert len(result.via_points) >= 3, "adjacent-layer hops: F->In1->In2->B"
    assert len(result.route.vias) == len(distinct_sites)


def test_two_layer_dip_resolves_a_blocked_crossing() -> None:
    """A wall blocking F.Cu entirely forces a dip to B.Cu and back."""
    outline = [(0.0, 0.0), (24.0, 0.0), (24.0, 10.0), (0.0, 10.0)]
    wall = _pad(12.0, 5.0, 9, ref="W", width=1.0, height=10.0, layer=Layer.F_CU)
    a = _pad(3.0, 5.0, 1, ref="A1")
    b = _pad(21.0, 5.0, 1, ref="A2")
    pf = LatticePathfinder(outline, [a, b, wall], DesignRules())
    route = pf.route(a, b)
    assert route is not None
    assert len(route.vias) >= 2, "expected a dip (down + up)"
    layers_used = {seg.layer for seg in route.segments}
    assert layers_used == {Layer.F_CU, Layer.B_CU}
