"""Net-class width/clearance plumbing through the lattice engine (issue #4271).

Phase A of the softstart rev-C large-board proof: the lattice dispatch used
to hardwire ``net_class=None`` for every connection, every committed segment
was recorded at the board-global half-width, and ``CommittedCopper.seg_clear``
discarded the stored per-segment half-width.  A 2.6 mm HV_HICUR net was
emitted at the default width AND spaced as default-width copper.

These tests pin the fixed invariants:

1. Committed copper blocks at its TRUE half-width (a wide net's copper is
   spaced as wide copper).
2. A class ``clearance`` (softstart HV = 0.3 mm) is honored from EITHER side
   of a pair (KiCad conditional-rule semantics).
3. Emission and spacing use the SAME class width end-to-end through
   ``route_netset``.
4. The dispatch threads ``router.net_class_map`` into the connections.
5. Default single-width behavior is unchanged when no class applies.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting


def _pad(
    x: float,
    y: float,
    net: int,
    *,
    ref: str,
    net_name: str | None = None,
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
        net_name=net_name or f"N{net}",
        layer=layer,
        ref=ref,
        pin="1",
    )


def _fresh(rules: DesignRules) -> CommittedCopper:
    return CommittedCopper(
        2,
        trace_half=rules.trace_width / 2.0,
        clearance=rules.trace_clearance,
        via_radius=rules.via_diameter / 2.0,
        via_via_gap=rules.via_diameter + rules.trace_clearance,
        same_net_via_gap=rules.via_drill + rules.min_hole_to_hole,
    )


# ---------------------------------------------------------------------------
# 1. Committed copper blocks at its TRUE half-width.
# ---------------------------------------------------------------------------


def test_committed_wide_run_blocks_at_true_half_width() -> None:
    """2.6 mm copper must be spaced as 2.6 mm copper, not the global default."""
    rules = DesignRules()  # trace_width 0.2, trace_clearance 0.15 (defaults)
    committed = _fresh(rules)
    wide_half = 1.3  # a 2.6 mm HV_HICUR run
    committed.add_run(0, [(0.0, 5.0), (20.0, 5.0)], net=1, half_width=wide_half)

    default_half = rules.trace_width / 2.0
    legacy_gap = rules.trace_width + rules.trace_clearance  # the old single gap
    true_gap = default_half + wide_half + rules.trace_clearance

    # A default-width segment at the LEGACY gap would have been accepted
    # pre-#4271 -- it must now be rejected (it overlaps the wide copper).
    y_legacy = 5.0 + legacy_gap + 0.05
    assert y_legacy < 5.0 + true_gap
    assert not committed.seg_clear((0.0, y_legacy), (20.0, y_legacy), 0, net=2)

    # At the TRUE gap it clears.
    y_true = 5.0 + true_gap + 0.01
    assert committed.seg_clear((0.0, y_true), (20.0, y_true), 0, net=2)

    # Same-net copper is never an obstacle.
    assert committed.seg_clear((0.0, y_legacy), (20.0, y_legacy), 0, net=1)


def test_node_clear_honors_stored_half_width() -> None:
    rules = DesignRules()
    committed = _fresh(rules)
    committed.add_run(0, [(0.0, 5.0), (20.0, 5.0)], net=1, half_width=1.3)
    default_half = rules.trace_width / 2.0
    true_gap = default_half + 1.3 + rules.trace_clearance
    assert not committed.node_clear((10.0, 5.0 + true_gap - 0.05), 0, net=2)
    assert committed.node_clear((10.0, 5.0 + true_gap + 0.05), 0, net=2)


def test_via_clear_honors_stored_half_width() -> None:
    """A via next to committed WIDE copper needs the wide gap, not the
    global ``via_copper_gap`` derived from the default trace width."""
    rules = DesignRules()
    committed = _fresh(rules)
    committed.add_run(0, [(0.0, 5.0), (20.0, 5.0)], net=1, half_width=1.3)
    via_r = rules.via_diameter / 2.0
    legacy_gap = via_r + rules.trace_clearance + rules.trace_width / 2.0
    true_gap = via_r + 1.3 + rules.trace_clearance
    y_legacy = 5.0 + legacy_gap + 0.05
    assert y_legacy < 5.0 + true_gap
    assert not committed.via_clear((10.0, y_legacy), net=2)
    assert committed.via_clear((10.0, 5.0 + true_gap + 0.05), net=2)


# ---------------------------------------------------------------------------
# 2. Class clearance honored from EITHER side of the pair.
# ---------------------------------------------------------------------------


def test_hv_clearance_honored_when_committed_side_carries_it() -> None:
    """An HV run committed with clearance=0.3 pushes DEFAULT nets to 0.3."""
    rules = DesignRules()
    committed = _fresh(rules)
    half = rules.trace_width / 2.0
    committed.add_run(0, [(0.0, 5.0), (20.0, 5.0)], net=1, half_width=half, clearance=0.3)
    gap_default_clr = 2 * half + rules.trace_clearance
    gap_hv_clr = 2 * half + 0.3
    y = 5.0 + gap_default_clr + 0.01  # clears the default gap only
    assert y < 5.0 + gap_hv_clr
    assert not committed.seg_clear((0.0, y), (20.0, y), 0, net=2)
    assert committed.seg_clear(
        (0.0, 5.0 + gap_hv_clr + 0.01), (20.0, 5.0 + gap_hv_clr + 0.01), 0, net=2
    )


def test_hv_clearance_honored_when_querying_side_carries_it() -> None:
    """A default run committed first: an HV query (clearance=0.3) must keep
    the HV gap away from it."""
    rules = DesignRules()
    committed = _fresh(rules)
    half = rules.trace_width / 2.0
    committed.add_run(0, [(0.0, 5.0), (20.0, 5.0)], net=1, half_width=half)
    gap_hv = 2 * half + 0.3
    y = 5.0 + gap_hv - 0.05
    assert not committed.seg_clear((0.0, y), (20.0, y), 0, net=2, half=half, clearance=0.3)
    y2 = 5.0 + gap_hv + 0.01
    assert committed.seg_clear((0.0, y2), (20.0, y2), 0, net=2, half=half, clearance=0.3)


# ---------------------------------------------------------------------------
# 3. End-to-end: emission AND spacing at the class width through route_netset.
# ---------------------------------------------------------------------------


def test_route_netset_emits_and_spaces_at_class_width() -> None:
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    a1, a2 = _pad(4.0, 8.0, 1, ref="A1"), _pad(36.0, 8.0, 1, ref="A2")
    b1, b2 = _pad(4.0, 12.0, 2, ref="B1"), _pad(36.0, 12.0, 2, ref="B2")
    rules = DesignRules()
    pf = LatticePathfinder(outline, [a1, a2, b1, b2], rules, LayerStack.two_layer())

    hv = NetClassRouting(name="HV_HICUR", trace_width=1.0, clearance=0.3)
    conns = [((1, 0), a1, a2, hv), ((2, 0), b1, b2, None)]
    routes, stats = pf.route_netset(conns, max_iterations=6)
    assert stats.lattice_builds == 1
    assert (1, 0) in routes and (2, 0) in routes, f"declines: {pf.failure_reasons}"

    # Emission at the class width; the default net stays at the global width.
    assert all(abs(s.width - 1.0) < 1e-9 for s in routes[(1, 0)].segments)
    assert all(abs(s.width - rules.trace_width) < 1e-9 for s in routes[(2, 0)].segments)

    # Spacing: no cross-net pair on the same layer closer than
    # own_half + other_half + max(HV clearance, global clearance).
    min_gap = 1.0 / 2.0 + rules.trace_width / 2.0 + max(0.3, rules.trace_clearance)
    for s1 in routes[(1, 0)].segments:
        for s2 in routes[(2, 0)].segments:
            if s1.layer != s2.layer:
                continue
            d = seg_seg_dist((s1.x1, s1.y1), (s1.x2, s1.y2), (s2.x1, s2.y1), (s2.x2, s2.y2))
            assert d >= min_gap - 1e-6, f"HV/default pair shipped {d:.4f}mm apart (< {min_gap:.4f})"


def test_default_connections_match_pre_class_behavior() -> None:
    """With ``net_class=None`` everywhere, geometry falls back to the global
    rules -- the pre-#4271 single-width behavior."""
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    a1, a2 = _pad(4.0, 8.0, 1, ref="A1"), _pad(36.0, 8.0, 1, ref="A2")
    rules = DesignRules()
    pf = LatticePathfinder(outline, [a1, a2], rules, LayerStack.two_layer())
    assert pf._conn_geometry(None) == (rules.trace_width / 2.0, rules.trace_clearance)
    routes, stats = pf.route_netset([((1, 0), a1, a2, None)], max_iterations=2)
    assert stats.routed == 1
    assert all(abs(s.width - rules.trace_width) < 1e-9 for s in routes[(1, 0)].segments)


def test_conn_geometry_clearance_floor_is_the_design_rules() -> None:
    """A class can only GROW clearance -- never shrink below the rules."""
    rules = DesignRules()
    pf = LatticePathfinder(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        [_pad(2.0, 5.0, 1, ref="A1"), _pad(8.0, 5.0, 1, ref="A2")],
        rules,
    )
    tight = NetClassRouting(name="tight", trace_width=0.3, clearance=0.01)
    half, clr = pf._conn_geometry(tight)
    assert half == 0.15
    assert clr == rules.trace_clearance  # floored, not 0.01


# ---------------------------------------------------------------------------
# 4. The dispatch threads router.net_class_map into the connections.
# ---------------------------------------------------------------------------


def _add_pad(router: Autorouter, pad: Pad) -> None:
    key = (pad.ref, pad.pin)
    router.pads[key] = pad
    router.nets.setdefault(pad.net, []).append(key)


def test_dispatch_threads_net_class_from_map() -> None:
    """``_negotiate_lattice_netset`` resolves each net's class by name (the
    ``--net-class-map`` sidecar merges into ``router.net_class_map``) instead
    of hardwiring ``None`` -- the load-bearing #4271 gap."""
    router = Autorouter(50, 40, strategy="lattice")
    _add_pad(router, _pad(10.0, 10.0, 1, ref="R1", net_name="FUSED_LINE"))
    _add_pad(router, _pad(30.0, 25.0, 1, ref="R2", net_name="FUSED_LINE"))
    router.net_class_map["FUSED_LINE"] = NetClassRouting(
        name="HV_HICUR", trace_width=1.2, clearance=0.3
    )

    routes = router.route_net(1)
    assert routes and routes[0].segments
    assert all(abs(s.width - 1.2) < 1e-9 for s in routes[0].segments)


def test_dispatch_unclassified_net_stays_at_global_width() -> None:
    router = Autorouter(50, 40, strategy="lattice")
    _add_pad(router, _pad(10.0, 10.0, 1, ref="R1", net_name="PLAIN"))
    _add_pad(router, _pad(30.0, 25.0, 1, ref="R2", net_name="PLAIN"))
    assert "PLAIN" not in router.net_class_map

    routes = router.route_net(1)
    assert routes and routes[0].segments
    assert all(abs(s.width - router.rules.trace_width) < 1e-9 for s in routes[0].segments)
