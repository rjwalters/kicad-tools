"""Lattice trace/via attach-zone waivers use the census rule (#8408).

The lattice used to waive a trace/via pair whenever the closest-gap midpoint sat
in an attach zone.  The #8277 gate waives only when BOTH nearest points lie in
one zone and the copper outside it still clears.  A trace lying outside the zone
with a via on the zone corner has its midpoint inside the zone but its trace-side
nearest point outside, so the gate refuses what the search used to commit.
"""

from __future__ import annotations

from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pairwise import LatticePairwise

HALF = 0.1
CLR = 0.2
VIA_R = 0.3
REQ = 1.3
# Zone box covers nets 1 and 2; its right/top corner is (10, 10).
ZONE = (0.0, 0.0, 10.0, 10.0, frozenset({1, 2}), None)


def _pw() -> LatticePairwise:
    return LatticePairwise(
        required_by_pair={(1, 2): REQ}, zones=(ZONE,), max_by_net={1: REQ, 2: REQ}
    )


def _copper(trace_y: float) -> CommittedCopper:
    cc = CommittedCopper(
        1,
        trace_half=HALF,
        clearance=CLR,
        via_radius=VIA_R,
        via_via_gap=0.8,
        same_net_via_gap=0.7,
        pairwise=_pw(),
    )
    # Net 2 trace entirely outside the zone (x > 10), via centred on the corner.
    cc.add_run(0, [(9.0, trace_y), (12.0, trace_y)], 2, HALF, CLR)
    return cc


def test_midpoint_in_zone_but_trace_nearest_point_outside_is_refused() -> None:
    pw = _pw()
    via = (9.5, 9.5)
    a, b = (9.0, 10.4), (12.0, 10.4)  # outside the zone (y > 10)
    # Midpoint (9.5, 9.95) is inside the box: the legacy probe waives it.
    assert pw.exempt_seg_pt(a, b, via, 1, 2)
    # Gap 0.9 - 0.3 - 0.1 = 0.5 < 1.3 and the trace-side witness is outside.
    assert not pw.exempt_seg_pt(a, b, via, 1, 2, half=HALF, radius=VIA_R, req=REQ)


def test_pair_wholly_in_zone_is_still_waived() -> None:
    pw = _pw()
    a, b = (2.0, 2.0), (4.0, 2.0)
    assert pw.exempt_seg_pt(a, b, (3.0, 5.0), 1, 2, half=HALF, radius=VIA_R, req=REQ)
    assert pw.exempt_pt_pt((3.0, 3.0), (3.0, 5.0), 1, 2, radius_p=VIA_R, radius_q=VIA_R, req=REQ)


def test_seg_seg_nearest_point_outside_zone_is_refused() -> None:
    pw = _pw()
    # Net-1 trace ends on the zone corner; net-2 trace lies wholly outside.
    a, b = (8.0, 10.0), (10.0, 10.0)
    c, d = (10.8, 10.0), (14.0, 10.0)
    assert not pw.exempt_seg_seg(a, b, c, d, 1, 2, 0, half_a=HALF, half_b=HALF, req=REQ)


def test_node_clear_refuses_what_midpoint_probe_waived() -> None:
    cc = _copper(10.4)
    # Net-1 node (disc of trace half-width) at (9.5, 9.5), net-2 trace outside
    # the zone: midpoint inside the zone, trace-side nearest point outside.
    assert not cc.node_clear((9.5, 9.5), 0, 1)
