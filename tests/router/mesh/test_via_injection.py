"""2.5D via-injection acceptance for mesh-router P2.6 (issue #4276).

Portal-midpoint via sites over identically-meshed layers resolve transverse
crossings the in-layer lane allocator (P2.5) cannot: a net whose F.Cu geodesic
genuinely crosses committed copper dips a layer -- via down, cross under, via
up -- instead of declining.  These tests cover the load-bearing guarantees:

* board 02 completion rises **past P2.5's 7/24**, DRC-clean, zero same-layer
  shorts, triangulation still built once per board;
* the graph generalises to **N layers** (a 2-layer board AND a >=3-layer
  synthetic case where A* composes a multi-layer span from adjacent via hops);
* **never ship a short** -- a via whose stub cannot clear declines (``None``);
* **via-in-pad is OFF by default**, tier-gated by ``MfrLimits`` -- injected vias
  are free-space portal midpoints, never dropped onto pads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import run_geometric_drc
from kicad_tools.router.io import load_pads_for_analysis, merge_routes_into_pcb
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytest.importorskip("kicad_tools.router.router_cpp")

_REPO = Path(__file__).resolve().parents[3]
_CHARLIEPLEX = _REPO / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"

# P2.5's committed completion on the charlieplex 3x3 fixture (issue #4276).
_P25_BASELINE = 7


def _pad(x, y, net, ref, layer=Layer.F_CU, through_hole=False, w=1.0, h=1.0):
    return Pad(
        x=x,
        y=y,
        width=w,
        height=h,
        net=net,
        net_name=f"N{net}",
        layer=layer,
        ref=ref,
        pin="1",
        through_hole=through_hole,
    )


def _pads_by_net(pads):
    bynet: dict[int, list] = {}
    for p in pads:
        if p.net > 0:
            bynet.setdefault(p.net, []).append(p)
    return bynet


def _connections(pads):
    conns = []
    for net, ps in _pads_by_net(pads).items():
        anchor = ps[0]
        for seq, other in enumerate(ps[1:]):
            if anchor.layer == other.layer:
                conns.append(((net, seq), anchor, other, None))
    return conns


def _same_layer_shorts(routes) -> int:
    """Different-net segments intersecting ON THE SAME layer (real shorts)."""
    from kicad_tools.router.mesh.geometry import segments_intersect

    segs = []
    for key, route in routes.items():
        for s in route.segments:
            segs.append((key, s.layer, (s.x1, s.y1), (s.x2, s.y2)))
    count = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if (
                segs[i][0] != segs[j][0]
                and segs[i][1] == segs[j][1]
                and segments_intersect(segs[i][2], segs[i][3], segs[j][2], segs[j][3])
            ):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Board 02: completion rises past P2.5's 7/24, DRC-clean, once-per-board.
# ---------------------------------------------------------------------------


def test_via_injection_lifts_board02_past_p25(tmp_path) -> None:
    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)

    pf = MeshPathfinder.from_board(text)
    routes, stats = pf.route_netset(conns, max_iterations=8)

    # Completion strictly past the P2.5 ceiling, by dipping layers.
    assert stats.routed > _P25_BASELINE, (
        f"2.5D did not lift completion past P2.5: {stats.routed} <= {_P25_BASELINE}"
    )
    # At least one net routed by an actual layer dip (a via was emitted).
    assert any(r.vias for r in routes.values()), "expected >=1 net resolved by a via dip"
    # The static-mesh-once invariant survives via injection.
    assert stats.triangulation_calls == 1
    # No same-layer net-vs-net short (cross-layer XY overlaps are legal dips).
    assert _same_layer_shorts(routes) == 0


def test_via_injection_board02_is_drc_clean(tmp_path) -> None:
    """The injected multi-layer copper (segments + vias) passes kicad-cli DRC."""
    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)

    base_pcb = tmp_path / "base.kicad_pcb"
    base_pcb.write_text(text)
    base = run_geometric_drc(base_pcb)
    if not base.ran:
        pytest.skip(f"kicad-cli DRC unavailable: {base.reason}")

    pf = MeshPathfinder.from_board(text)
    routes, stats = pf.route_netset(conns, max_iterations=8)
    assert any(r.vias for r in routes.values())
    sexp = "".join(r.to_sexp() for r in routes.values() if r.segments)

    out = tmp_path / "routed.kicad_pcb"
    out.write_text(merge_routes_into_pcb(text, sexp))
    res = run_geometric_drc(out)
    assert res.ran
    # No NEW error-severity findings vs the unrouted baseline (via / hole /
    # annular rules exercised on real multi-layer copper).
    assert res.error_count <= base.error_count, (
        f"via injection introduced DRC errors: base={base.error_count} "
        f"routed={res.error_count} types={dict(res.by_type)}"
    )


def test_injected_vias_are_free_space_not_in_pad() -> None:
    """Every injected via sits at a free-space portal midpoint, never on a pad.

    Via-in-pad is OFF by default, so an injected via must clear every pad.  This
    is the geometric proof of the default-off policy on the real board.
    """
    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)
    pf = MeshPathfinder.from_board(text)
    routes, _stats = pf.route_netset(conns, max_iterations=8)

    vias = [v for r in routes.values() for v in r.vias]
    assert vias, "expected at least one injected via to test"
    for v in vias:
        for pad in pads:
            hx = pad.width / 2.0
            hy = pad.height / 2.0
            in_pad = abs(v.x - pad.x) <= hx and abs(v.y - pad.y) <= hy
            assert not in_pad, f"via at ({v.x:.2f},{v.y:.2f}) landed on pad {pad.ref}"


# ---------------------------------------------------------------------------
# N-layer generality: 2-layer AND a >=3-layer synthetic case.
# ---------------------------------------------------------------------------


def test_via_edges_compose_multilayer_span_on_4layer_board() -> None:
    """A 4-layer board: A* composes a multi-layer span from adjacent via hops.

    Start pad on F.Cu (index 0), end pad on B.Cu (index 3): reaching the goal
    layer requires the ``(triangle, layer)`` A* to compose adjacent-layer via
    hops (0->1->2->3), emitted as a single through-via.  Proves the mesh is
    replicated across the board's actual layer count, not hardcoded to 2.
    """
    stack = LayerStack.four_layer_all_signal()
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    start = _pad(5.0, 15.0, 1, "A1", layer=Layer.F_CU)
    end = _pad(35.0, 15.0, 1, "A2", layer=Layer.B_CU)
    pf = MeshPathfinder(outline, [start, end], DesignRules(), layer_stack=stack)

    res = pf._route_via_injection(start, end, None, {}, present_cost_factor=0.0)
    assert res is not None, "cross-layer net should route by composing via hops"
    route, _portals = res
    # A layer change was composed -> at least one through-via emitted.
    assert route.vias, "expected a via to compose the F.Cu -> B.Cu span"
    for v in route.vias:
        assert v.layers == (Layer.F_CU, Layer.B_CU), "default via is a through-via"
        assert v.in_pad is False, "free-space via is never a via-in-pad"
    # Copper landed on both the entry and exit layers.
    layers = {s.layer for s in route.segments}
    assert Layer.F_CU in layers and Layer.B_CU in layers
    # Static mesh: one triangulation across the whole N-layer graph.
    assert pf.triangulation_calls == 1


def test_two_layer_board_default_stack() -> None:
    """The 2-layer path (board 02's stack) still composes a dip end-to-end."""
    text = _CHARLIEPLEX.read_text()
    pf = MeshPathfinder.from_board(text)
    assert pf.layer_stack.num_layers == 2
    conns = _connections(load_pads_for_analysis(text))
    routes, _stats = pf.route_netset(conns, max_iterations=8)
    dipped = [r for r in routes.values() if r.vias]
    assert dipped, "expected a 2-layer dip"
    for r in dipped:
        layers = {s.layer for s in r.segments}
        assert layers == {Layer.F_CU, Layer.B_CU}


# ---------------------------------------------------------------------------
# Never ship a short: a via/stub that cannot clear declines (#3906).
# ---------------------------------------------------------------------------


def test_via_that_cannot_clear_declines_never_shorts() -> None:
    """A through-hole wall blocks BOTH layers: injection declines, never crosses.

    Net 1 must cross the board centre; a large other-net through-hole pad there
    is a keep-out on EVERY layer, so no dip can land clear.  The obstacle-aware
    placement + authoritative per-layer fit must decline (``None``) rather than
    ship copper over the blocker (the #3906 invariant, on the 2.5D path).
    """
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    start = _pad(2.0, 10.0, 1, "A1")
    end = _pad(38.0, 10.0, 1, "A2")
    # A tall PTH pad wall spanning the full height at mid-board -> no route on
    # any layer (through_hole=True blocks F.Cu AND B.Cu).
    wall = _pad(20.0, 10.0, 2, "W1", through_hole=True, w=2.0, h=20.0)
    pf = MeshPathfinder(outline, [start, end, wall], DesignRules())

    res = pf._route_via_injection(start, end, None, {}, present_cost_factor=0.0)
    assert res is None, "a dip that cannot clear the blocker must decline, not short"


def test_negotiated_netset_never_ships_same_layer_short() -> None:
    """Whole negotiated set (with injection) has zero same-layer cross-net shorts."""
    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)
    pf = MeshPathfinder.from_board(text)
    routes, _stats = pf.route_netset(conns, max_iterations=8)
    assert _same_layer_shorts(routes) == 0


# ---------------------------------------------------------------------------
# Via-in-pad OFF by default, tier-gated by MfrLimits.
# ---------------------------------------------------------------------------


def test_via_in_pad_off_by_default_and_tier_gated() -> None:
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    own = _pad(10.0, 10.0, 1, "A1")
    other = _pad(30.0, 10.0, 2, "B1")
    via_r = DesignRules().via_diameter / 2.0 + DesignRules().trace_clearance

    # Default / base jlcpcb: via-in-pad NOT supported -> a via on the net's own
    # pad is pruned (declined).
    for mfr in (None, "jlcpcb"):
        pf = MeshPathfinder(outline, [own, other], DesignRules(manufacturer=mfr))
        assert pf._via_in_pad_allowed is False
        assert pf._via_allowed_at((own.x, own.y), own.net, via_r, (0, 1), {}) is False
        # An other-net pad is a short on ANY tier -> never allowed.
        assert pf._via_allowed_at((other.x, other.y), own.net, via_r, (0, 1), {}) is False
        # Free space is fine.
        assert pf._via_allowed_at((20.0, 5.0), own.net, via_r, (0, 1), {}) is True

    # jlcpcb-tier1: via-in-pad supported -> a via on the OWN pad is permitted,
    # but an other-net pad is still a short.
    pf_t1 = MeshPathfinder(outline, [own, other], DesignRules(manufacturer="jlcpcb-tier1"))
    assert pf_t1._via_in_pad_allowed is True
    assert pf_t1._via_allowed_at((own.x, own.y), own.net, via_r, (0, 1), {}) is True
    assert pf_t1._via_allowed_at((other.x, other.y), own.net, via_r, (0, 1), {}) is False


def test_single_net_route_is_inert_no_vias() -> None:
    """The single-net ``route()`` contract is unchanged: no committed copper to
    dip around means no via machinery fires (P1/P2 behaviour preserved)."""
    text = _CHARLIEPLEX.read_text()
    pads = load_pads_for_analysis(text)
    pf = MeshPathfinder.from_board(text)
    for _net, ps in _pads_by_net(pads).items():
        if len(ps) == 2 and ps[0].layer == ps[1].layer:
            r = pf.route(ps[0], ps[1])
            if r is not None and r.segments:
                assert not r.vias, "single-net route must not inject vias"
                break
