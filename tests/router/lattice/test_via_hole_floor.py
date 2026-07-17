"""Lattice via hole-to-hole legality (issue #4291).

Two gaps produced 16 ``hole_to_hole`` DRC warnings on the softstart P4 run
of record (#4271 / PR #4289), all involving lattice via holes:

1. ``CommittedCopper.via_clear`` sized the CROSS-net via-via gap from copper
   alone (``via_diameter + clearance`` centre-to-centre) with no drill
   floor: at the softstart rules (0.6/0.3 via, 0.127-0.15 clearance) two
   0.3 mm drills could sit 0.45 mm hole-edge-to-edge, under the 0.5 mm
   ``rules.min_hole_to_hole`` floor the same-net branch already enforced.
   Fix: cross-net gap = ``max(via_via_gap, same_net_via_gap)``.

2. ``LatticePathfinder._via_ok`` queried ``pads_near`` with a copper-derived
   window (~0.8 mm) while the hole-to-hole check against a PTH/NPTH pad can
   reject out to ``via_drill/2 + pad_drill/2 + min_hole_to_hole`` (1.3 mm
   for softstart's 1.3 mm terminal drills) -- pads whose keep-out rect falls
   outside the query's bucket range were silently unchecked.  Fix: the
   window now covers the board's largest drill (computed once in
   ``__init__``).
"""

from __future__ import annotations

import math

from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# Softstart-shaped rules (softstart_revc.kicad_dru): via 0.6/0.3, clearance
# under the 0.35 hole-floor delta so the copper gap (0.6 + 0.15 = 0.75 cc)
# sits BELOW the hole floor (0.3 + 0.5 = 0.8 cc) -- the regime where fix 1
# changes the verdict.
_SOFTSTART_RULES = {
    "trace_width": 0.2,
    "trace_clearance": 0.15,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "min_hole_to_hole": 0.5,
}


def _committed(**overrides: float) -> CommittedCopper:
    kwargs: dict[str, float] = {
        "trace_half": 0.1,
        "clearance": 0.15,
        "via_radius": 0.3,
        "via_via_gap": 0.75,  # via_diameter + clearance (copper cc gap)
        "same_net_via_gap": 0.8,  # via_drill + min_hole_to_hole (hole cc floor)
    }
    kwargs.update(overrides)
    return CommittedCopper(2, **kwargs)


# ---------------------------------------------------------------------------
# Fix 1: CommittedCopper.via_clear cross-net hole floor.
# ---------------------------------------------------------------------------


def test_cross_net_via_pair_under_hole_floor_is_illegal() -> None:
    """0.77 mm cc clears the copper gap (0.75) but leaves only 0.47 mm
    hole-edge-to-edge for two 0.3 mm drills -- under the 0.5 mm floor.
    Pre-#4291 this was accepted (the run-of-record warning geometry)."""
    cc = _committed()
    cc.add_via((5.0, 5.0), net=1)
    assert not cc.via_clear((5.77, 5.0), net=2)


def test_cross_net_via_pair_at_hole_floor_is_legal() -> None:
    """0.82 mm cc = 0.52 mm hole gap: legal (stays legal after the fix)."""
    cc = _committed()
    cc.add_via((5.0, 5.0), net=1)
    assert cc.via_clear((5.82, 5.0), net=2)


def test_cross_net_copper_gap_still_governs_when_larger() -> None:
    """When the copper gap exceeds the hole floor (the JLC-default regime:
    0.7 + 0.2 = 0.9 cc copper vs 0.35 + 0.5 = 0.85 cc holes) the fix must
    not LOOSEN anything: 0.87 cc clears the holes but not the copper."""
    cc = _committed(via_via_gap=0.9, via_radius=0.35, same_net_via_gap=0.85)
    cc.add_via((5.0, 5.0), net=1)
    assert not cc.via_clear((5.87, 5.0), net=2)
    assert cc.via_clear((5.91, 5.0), net=2)


def test_same_net_via_gap_unchanged() -> None:
    """The same-net branch keeps its hole floor exactly as before."""
    cc = _committed()
    cc.add_via((5.0, 5.0), net=1)
    assert not cc.via_clear((5.77, 5.0), net=1)
    assert cc.via_clear((5.82, 5.0), net=1)


# ---------------------------------------------------------------------------
# Fix 2: _via_ok pad query window covers the largest drill on the board.
# ---------------------------------------------------------------------------

_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]


def _pf(pads: list[Pad], **knobs: float) -> LatticePathfinder:
    return LatticePathfinder(_OUTLINE, pads, DesignRules(**_SOFTSTART_RULES), **knobs)


def _assert_hole_floor_everywhere(pf: LatticePathfinder, pad: Pad) -> None:
    """Property check on the REAL lattice: every node whose centre distance
    to ``pad`` is under the hole-to-hole minimum must be via-illegal."""
    lattice = pf.build()
    committed = pf._fresh_committed()
    min_cc = pf.rules.via_drill / 2.0 + pad.drill / 2.0 + pf.rules.min_hole_to_hole
    other_net = pad.net + 1000
    checked = 0
    for key, pt in lattice.nodes.items():
        if math.dist(pt, (pad.x, pad.y)) < min_cc - 1e-9:
            checked += 1
            assert not pf._via_ok(key, other_net, committed), (
                f"via allowed at {pt} only {math.dist(pt, (pad.x, pad.y)):.3f} mm "
                f"from a {pad.drill} mm drill (hole floor needs {min_cc:.3f} mm cc)"
            )
    assert checked > 0, "no lattice nodes inside the hole-floor annulus; fixture broken"


def test_via_ok_hole_floor_against_large_drill_pth_pad() -> None:
    """Softstart's failure shape: a 1.3 mm-drill PTH terminal pad.  The
    rejection radius (1.3 mm cc) exceeds the old copper-derived query
    window (~0.8 mm)."""
    pad = Pad(
        x=10.0, y=5.0, width=2.0, height=2.0, net=7, net_name="TERM", through_hole=True, drill=1.3
    )
    _assert_hole_floor_everywhere(_pf([pad]), pad)


def test_via_ok_hole_floor_bucket_separated_narrow_npth() -> None:
    """The geometry that genuinely escaped the old window: a pad whose
    COPPER keep-out rect is much smaller than its drill (NPTH slot-style;
    ``drill > 0`` with ``through_hole=False``, the #4271 loader shape) and
    whose rect sits in a different 4 mm lookup bucket than the old query
    box.  The pad-lookup buckets accidentally cover fat pads (their
    keep-out rect reaches the query's bucket range), but a 3.0 mm drill
    with 0.4 mm copper rejects out to 2.15 mm cc while its rect only spans
    +/-0.45 mm -- the old ~0.8 mm window's bucket range never touched the
    pad's, so the hole-to-hole branch simply never ran.

    Concretely (bucket boundary at x = 4.0): pad rect x in [4.0, 4.9]
    (bucket 1); the coarse=0.8 lattice has a node at (2.4, 2.4) whose old
    query box [1.6, 3.2] stays in bucket 0 -- disjoint -- at 2.05 mm cc,
    under the 2.15 mm hole floor."""
    pad = Pad(
        x=4.45, y=2.4, width=0.4, height=0.4, net=7, net_name="NPTH", through_hole=False, drill=3.0
    )
    _assert_hole_floor_everywhere(_pf([pad], coarse=0.8), pad)


def test_via_ok_legal_beyond_hole_floor() -> None:
    """Nodes past the hole floor (and outside every keep-out) stay legal --
    the enlarged window must not over-reject."""
    pad = Pad(
        x=10.0, y=5.0, width=2.0, height=2.0, net=7, net_name="TERM", through_hole=True, drill=1.3
    )
    pf = _pf([pad])
    lattice = pf.build()
    committed = pf._fresh_committed()
    min_cc = pf.rules.via_drill / 2.0 + pad.drill / 2.0 + pf.rules.min_hole_to_hole
    legal = [
        key
        for key, pt in lattice.nodes.items()
        if 1.5 * min_cc < math.dist(pt, (10.0, 5.0)) < 3.0 * min_cc
    ]
    assert legal, "no probe nodes in the legal annulus; fixture broken"
    assert all(pf._via_ok(key, pad.net + 1000, committed) for key in legal)
