"""Search-time HV pairwise clearance in the lattice engine (issue #4602).

Predicate-level coverage of the net-id-space pairwise projection
(:mod:`kicad_tools.router.lattice.pairwise`) and its consumption by
:class:`~kicad_tools.router.lattice.obstacles.CommittedCopper` and the
static pad keep-outs:

1. a MAPPED pair's gap is ``own_half + stored_half + max(own_clr,
   stored_clr, pairwise_required)`` -- the requirement depends on the pair;
2. an UNMAPPED pair is byte-identical to the scalar path;
3. a #4506 attach zone covering both nets waives the pairwise term (but
   never the scalar term) at the predicate level;
4. the spatial-query window inflates to the pairwise reach, so copper far
   beyond the scalar window is still found (the #4511 search-radius trap);
5. the per-pad pairwise keep-out blocks a mapped foreign pad at the
   requirement and honours the zone exemption;
6. the id-space projection builder resolves names/ids like the #4510 C++
   projection and returns ``None`` in every dormant case.
"""

from __future__ import annotations

from kicad_tools.router.lattice.obstacles import CommittedCopper, LatticeObstacleModel
from kicad_tools.router.lattice.pairwise import LatticePairwise, build_lattice_pairwise
from kicad_tools.router.lattice.quadtree import OctilinearLattice
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    build_pairwise_clearance_table,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

RULES = DesignRules()  # trace_width 0.2, trace_clearance 0.2 (defaults)
HALF = RULES.trace_width / 2.0
CLR = RULES.trace_clearance
REQ = 3.2  # 300 V / IEC 60664-1 / PD2 / IIIa derived requirement (mm)

HV, LV, OTHER = 1, 2, 3


def _projection(
    zones: tuple[tuple[float, float, float, float, frozenset[int]], ...] = (),
) -> LatticePairwise:
    return LatticePairwise(
        required_by_pair={(HV, LV): REQ},
        zones=zones,
        max_by_net={HV: REQ, LV: REQ},
    )


def _fresh(pairwise: LatticePairwise | None = None) -> CommittedCopper:
    return CommittedCopper(
        2,
        trace_half=HALF,
        clearance=CLR,
        via_radius=RULES.via_diameter / 2.0,
        via_via_gap=RULES.via_diameter + CLR,
        same_net_via_gap=RULES.via_drill + RULES.min_hole_to_hole,
        pairwise=pairwise,
    )


# ---------------------------------------------------------------------------
# 1 + 2: pair gap = max(own, stored, pairwise); unmapped pairs unchanged.
# ---------------------------------------------------------------------------


def test_mapped_pair_gap_widens_to_the_pairwise_requirement() -> None:
    committed = _fresh(_projection())
    committed.add_run(0, [(0.0, 0.0), (20.0, 0.0)], net=HV, half_width=HALF)

    pw_gap = HALF + HALF + REQ  # centreline gap for the mapped pair
    # Inside the requirement (but far beyond the scalar gap): blocked.
    y_inside = pw_gap - 0.1
    assert not committed.seg_clear((0.0, y_inside), (20.0, y_inside), 0, LV, HALF, CLR)
    assert not committed.node_clear((10.0, y_inside), 0, LV, HALF, CLR)
    # Just beyond the requirement: clear.
    y_outside = pw_gap + 0.05
    assert committed.seg_clear((0.0, y_outside), (20.0, y_outside), 0, LV, HALF, CLR)
    assert committed.node_clear((10.0, y_outside), 0, LV, HALF, CLR)


def test_unmapped_pair_keeps_the_scalar_gap() -> None:
    """A net with no pairwise mapping spaces exactly as the scalar path does."""
    for pairwise in (None, _projection()):
        committed = _fresh(pairwise)
        committed.add_run(0, [(0.0, 0.0), (20.0, 0.0)], net=HV, half_width=HALF)
        scalar_gap = HALF + HALF + CLR
        # OTHER participates in no pair: legal just beyond the scalar gap,
        # regardless of whether a projection is installed.
        y = scalar_gap + 0.05
        assert committed.seg_clear((0.0, y), (20.0, y), 0, OTHER, HALF, CLR)
        assert committed.node_clear((10.0, y), 0, OTHER, HALF, CLR)
        # And still blocked inside the scalar gap.
        assert not committed.seg_clear((0.0, 0.1), (20.0, 0.1), 0, OTHER, HALF, CLR)


def test_same_net_copper_is_never_pairwise_blocked() -> None:
    committed = _fresh(_projection())
    committed.add_run(0, [(0.0, 0.0), (20.0, 0.0)], net=HV, half_width=HALF)
    assert committed.seg_clear((0.0, 0.5), (20.0, 0.5), 0, HV, HALF, CLR)


# ---------------------------------------------------------------------------
# 3: attach-zone exemption at the predicate level (#4506 semantics).
# ---------------------------------------------------------------------------


def test_attach_zone_waives_the_pairwise_term_but_not_the_scalar_term() -> None:
    zone = (5.0, -5.0, 15.0, 5.0, frozenset({HV, LV}))
    committed = _fresh(_projection(zones=(zone,)))
    committed.add_run(0, [(5.0, 0.0), (15.0, 0.0)], net=HV, half_width=HALF)

    # Scalar-passing but requirement-violating copper INSIDE the zone: waived
    # (the closest-gap midpoint falls in the zone and both nets are members).
    assert committed.seg_clear((5.0, 1.0), (15.0, 1.0), 0, LV, HALF, CLR)
    # The scalar gap is NEVER waived by a zone.
    assert not committed.seg_clear((5.0, 0.1), (15.0, 0.1), 0, LV, HALF, CLR)
    # The same proximity OUTSIDE the zone still blocks.
    committed.add_run(0, [(30.0, 0.0), (40.0, 0.0)], net=HV, half_width=HALF)
    assert not committed.seg_clear((30.0, 1.0), (40.0, 1.0), 0, LV, HALF, CLR)


def test_zone_covering_only_one_net_does_not_exempt() -> None:
    zone = (5.0, -5.0, 15.0, 5.0, frozenset({HV, OTHER}))
    committed = _fresh(_projection(zones=(zone,)))
    committed.add_run(0, [(5.0, 0.0), (15.0, 0.0)], net=HV, half_width=HALF)
    assert not committed.seg_clear((5.0, 1.0), (15.0, 1.0), 0, LV, HALF, CLR)


# ---------------------------------------------------------------------------
# 4: the search-radius trap -- far HV copper must still be found.
# ---------------------------------------------------------------------------


def test_spatial_window_inflates_to_the_pairwise_reach() -> None:
    """Copper ~3 mm away is far beyond the scalar query window (own_half +
    own_clr + 0.5 ~= 0.8 mm) yet inside the pairwise requirement; the inflated
    window must surface it or the predicate silently passes (#4511 analog)."""
    committed = _fresh(_projection())
    committed.add_run(0, [(0.0, 0.0), (20.0, 0.0)], net=HV, half_width=HALF)
    y = 3.0  # >> scalar window, < 3.2 requirement
    assert not committed.seg_clear((0.0, y), (20.0, y), 0, LV, HALF, CLR)
    assert not committed.node_clear((10.0, y), 0, LV, HALF, CLR)


def test_via_predicates_honour_the_pairwise_requirement() -> None:
    committed = _fresh(_projection())
    committed.add_run(0, [(0.0, 0.0), (20.0, 0.0)], net=HV, half_width=HALF)
    via_r = RULES.via_diameter / 2.0
    # An LV via 3 mm from HV copper is inside the requirement: blocked.
    assert not committed.via_clear((10.0, via_r + HALF + 3.0), LV)
    # Beyond the requirement: clear.
    assert committed.via_clear((10.0, via_r + HALF + REQ + 0.05), LV)
    # And an LV trace near a committed HV via is symmetric.
    committed2 = _fresh(_projection())
    committed2.add_via((10.0, 0.0), HV)
    assert not committed2.seg_clear((0.0, 3.0), (20.0, 3.0), 0, LV, HALF, CLR)
    assert committed2.seg_clear(
        (0.0, via_r + HALF + REQ + 0.05), (20.0, via_r + HALF + REQ + 0.05), 0, LV, HALF, CLR
    )


# ---------------------------------------------------------------------------
# 5: per-pad pairwise keep-out.
# ---------------------------------------------------------------------------


def _pad(x: float, y: float, net: int, net_name: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=f"R{net}",
        pin="1",
    )


def _obstacles(pads: list[Pad]) -> LatticeObstacleModel:
    lattice = OctilinearLattice((0.0, 0.0, 40.0, 40.0), [], coarse=3.2)
    return LatticeObstacleModel(
        lattice,
        pads,
        [(0,) for _ in pads],
        2,
        agent_radius=HALF + CLR,
    )


def test_pairwise_pad_blocked_blocks_a_mapped_pad_at_the_requirement() -> None:
    obstacles = _obstacles([_pad(20.0, 20.0, HV, "HV_LINE")])
    pw = _projection()
    # Segment passing 3 mm from the pad edge (pad half-extent 0.5): the rect
    # is grown to 0.5 + own_half + 3.2, so a pass at y=23.5 (3.0 mm off the
    # edge) is blocked while y=24.0 (3.5 mm off the edge... still inside
    # 0.5+0.1+3.2=3.8 from centre) -- use centre distances explicitly.
    grown_reach = 0.5 + HALF + REQ  # pad half-extent + own_half + requirement
    y_block = 20.0 + grown_reach - 0.1
    y_clear = 20.0 + grown_reach + 0.1
    assert obstacles.pairwise_pad_blocked((0.0, y_block), (40.0, y_block), 0, LV, HALF, 0.0, pw)
    assert not obstacles.pairwise_pad_blocked((0.0, y_clear), (40.0, y_clear), 0, LV, HALF, 0.0, pw)
    # Unmapped querying net: never pairwise-blocked.
    assert not obstacles.pairwise_pad_blocked(
        (0.0, y_block), (40.0, y_block), 0, OTHER, HALF, 0.0, pw
    )
    # Wrong layer: the pad only occupies layer 0.
    assert not obstacles.pairwise_pad_blocked((0.0, y_block), (40.0, y_block), 1, LV, HALF, 0.0, pw)
    # ``layer=None`` (the through-via probe) checks every layer.
    assert obstacles.pairwise_pad_blocked((0.0, y_block), (40.0, y_block), None, LV, HALF, 0.0, pw)


def test_pairwise_pad_blocked_honours_the_attach_zone() -> None:
    obstacles = _obstacles([_pad(20.0, 20.0, HV, "HV_LINE")])
    zone = (15.0, 15.0, 25.0, 25.0, frozenset({HV, LV}))
    pw = _projection(zones=(zone,))
    y = 22.0  # inside the pairwise keep-out AND inside the zone
    assert not obstacles.pairwise_pad_blocked((15.0, y), (25.0, y), 0, LV, HALF, 0.0, pw)


# ---------------------------------------------------------------------------
# 6: the id-space projection builder.
# ---------------------------------------------------------------------------


def test_build_lattice_pairwise_projects_names_to_ids() -> None:
    table = build_pairwise_clearance_table({"/HV_LINE": 300.0, "/LV_SENSE": 0.0}, dru=CLR)
    zones = (
        AttachZone(10.0, 10.0, 20.0, 20.0, frozenset({"HV_LINE", "LV_SENSE"})),
        # A single-resolvable-net zone cannot exempt any pair and is dropped.
        AttachZone(0.0, 0.0, 5.0, 5.0, frozenset({"HV_LINE", "ABSENT"})),
    )
    pw = build_lattice_pairwise(table, zones, {"/HV_LINE": HV, "/LV_SENSE": LV})
    assert pw is not None
    assert pw.required(HV, LV) == pw.required(LV, HV) == 3.2
    assert pw.required(HV, OTHER) == 0.0
    assert pw.max_required_for(HV) == pw.max_required_for(LV) == 3.2
    assert pw.max_required_for(OTHER) == 0.0
    assert len(pw.zones) == 1
    assert pw.exempt(15.0, 15.0, HV, LV)
    assert not pw.exempt(15.0, 15.0, HV, OTHER)
    assert not pw.exempt(50.0, 50.0, HV, LV)


def test_build_lattice_pairwise_dormant_cases_return_none() -> None:
    table = build_pairwise_clearance_table({"/HV_LINE": 300.0, "/LV_SENSE": 0.0}, dru=CLR)
    # No table at all.
    assert build_lattice_pairwise(None, (), {"/HV_LINE": HV}) is None
    # No net above the HV threshold -> empty matrix.
    low = build_pairwise_clearance_table({"/A": 3.3, "/B": 0.0}, dru=CLR)
    assert build_lattice_pairwise(low, (), {"/A": 1, "/B": 2}) is None
    # A mapped pair whose nets do not resolve to board net ids.
    assert build_lattice_pairwise(table, (), {"/UNRELATED": 7}) is None
