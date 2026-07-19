"""#4318: the lattice engine must never emit 0.000mm segment-to-via copper.

A lattice octilinear segment whose *body* runs across a via center at 0.000mm
slipped through a complementary blind spot: the committed-copper predicates
only enforced clearance against *different-net* elements (so a same-net segment
landing on a via center was never rejected), and the DRC checker's co-location
suppression is *endpoint-only* (so a segment whose interior crosses a via center
was not exempt).  Net effect: an unfixable ``clearance_segment_via`` at 0.000mm
that no nudge post-pass could clean.

Two guards close the hole, both reusing ONE body-vs-endpoint predicate
(``seg_body_crosses_pt``) so the router accept gate and ``kct check`` agree:

1. ``CommittedCopper.seg_clear`` rejects a segment whose body crosses a
   *same-net* via center (different-net was already rejected by the gap check),
   while preserving the legitimate in-pad-escape endpoint-at-via-center
   invariant (#2706).
2. ``LatticePathfinder._self_drc_reason`` is a final gate over one net's OWN
   emitted geometry -- a segment crossing the net's own via is committed
   together with that via and is never cross-checked during A*.
"""

from __future__ import annotations

import math

from kicad_tools.router.lattice.obstacles import (
    _COLOCATION_EPSILON_MM,
    CommittedCopper,
    seg_body_crosses_pt,
)


def _committed(num_layers: int = 2) -> CommittedCopper:
    return CommittedCopper(
        num_layers,
        trace_half=0.1,
        clearance=0.2,
        via_radius=0.3,
        via_via_gap=0.65,
        same_net_via_gap=0.8,
    )


# ---------------------------------------------------------------------------
# seg_body_crosses_pt: the shared body-vs-endpoint predicate.
# ---------------------------------------------------------------------------


def test_body_crossing_detected_mid_span() -> None:
    # (0,0)->(2,0) with a point at its midpoint (1,0) is a body crossing.
    assert seg_body_crosses_pt((0.0, 0.0), (2.0, 0.0), (1.0, 0.0)) is True


def test_endpoint_coincidence_is_exempt() -> None:
    # A via tapped at either segment endpoint is the legal in-pad invariant.
    assert seg_body_crosses_pt((0.0, 0.0), (2.0, 0.0), (0.0, 0.0)) is False
    assert seg_body_crosses_pt((0.0, 0.0), (2.0, 0.0), (2.0, 0.0)) is False


def test_point_off_the_line_is_not_a_crossing() -> None:
    # Well clear of the segment: not a coincidence at all.
    assert seg_body_crosses_pt((0.0, 0.0), (2.0, 0.0), (1.0, 0.5)) is False


def test_near_endpoint_within_epsilon_is_exempt() -> None:
    # Just inside epsilon of an endpoint still counts as the endpoint (legal).
    p = (_COLOCATION_EPSILON_MM / 2.0, 0.0)
    assert seg_body_crosses_pt((0.0, 0.0), (2.0, 0.0), p) is False


# ---------------------------------------------------------------------------
# CommittedCopper.seg_clear: same-net body-crossing is now rejected.
# ---------------------------------------------------------------------------


def test_seg_clear_rejects_same_net_body_crossing_via() -> None:
    c = _committed()
    net = 7
    c.add_via((1.0, 0.0), net)  # same-net via at the segment's midpoint
    # A same-net segment whose BODY runs across that via center is malformed.
    assert c.seg_clear((0.0, 0.0), (2.0, 0.0), 0, net) is False


def test_seg_clear_allows_same_net_via_at_endpoint() -> None:
    c = _committed()
    net = 7
    c.add_via((0.0, 0.0), net)  # same-net via AT the segment endpoint (in-pad)
    # The legitimate in-pad-escape endpoint-at-via-center invariant (#2706).
    assert c.seg_clear((0.0, 0.0), (2.0, 0.0), 0, net) is True


def test_seg_clear_still_rejects_different_net_via() -> None:
    c = _committed()
    c.add_via((1.0, 0.0), 3)  # different-net via on the segment body
    # Different-net was already caught by the positive gap; still rejected.
    assert c.seg_clear((0.0, 0.0), (2.0, 0.0), 0, 7) is False


def test_seg_clear_allows_same_net_via_well_clear() -> None:
    c = _committed()
    net = 7
    c.add_via((1.0, 5.0), net)  # far from the segment
    assert c.seg_clear((0.0, 0.0), (2.0, 0.0), 0, net) is True


# ---------------------------------------------------------------------------
# _self_drc_reason: final gate over a net's OWN emitted geometry.
# ---------------------------------------------------------------------------


def _pathfinder():
    """A LatticePathfinder instance is not needed to drive _self_drc_reason:
    it is a pure method over a _RouteResult.  Build a minimal stand-in via the
    class' unbound method to avoid constructing a full board lattice."""
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder

    return LatticePathfinder.__new__(LatticePathfinder)


def _route_result(runs, via_points):
    from kicad_tools.router.lattice.pathfinder import _RouteResult

    # ``route`` and ``resources`` are unused by _self_drc_reason.
    return _RouteResult(route=None, runs=runs, via_points=via_points, resources=())


def test_self_drc_declines_segment_crossing_own_via() -> None:
    pf = _pathfinder()
    # One run on layer 0 from (0,0) to (2,0); the net's own via sits mid-body.
    runs = [(0, [(0.0, 0.0), (2.0, 0.0)], [0.2])]
    result = _route_result(runs, via_points=[(1.0, 0.0)])
    assert pf._self_drc_reason(result) == "self-drc-segment-via-coincident"


def test_self_drc_accepts_via_at_run_endpoint() -> None:
    pf = _pathfinder()
    # A layer transition: the via sits at the run's endpoint (the normal case).
    runs = [(0, [(0.0, 0.0), (2.0, 0.0)], [0.2])]
    result = _route_result(runs, via_points=[(2.0, 0.0)])
    assert pf._self_drc_reason(result) is None


def test_self_drc_accepts_via_free_route() -> None:
    pf = _pathfinder()
    runs = [(0, [(0.0, 0.0), (2.0, 0.0)], [0.2])]
    result = _route_result(runs, via_points=[])
    assert pf._self_drc_reason(result) is None


# ---------------------------------------------------------------------------
# Router-vs-checker agreement (#4318 AC T1.3): the DRC checker flags a
# different-net body-crossing that the lattice predicate also rejects, and the
# checker (correctly) exempts the endpoint coincidence the predicate exempts.
# ---------------------------------------------------------------------------


_HEADER_2L = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG1")
  (net 2 "SIG2")
"""


def _checker_flags_segment_via(seg_ep, via_center, seg_net, via_net, tmp_path) -> bool:
    """Run the real DRC ClearanceRule over one segment + one via and report
    whether a ``clearance_segment_via`` error is produced.  Exercises the
    checker's own edge-to-edge predicate + endpoint-only co-location
    suppression (the same behavior ``kct check`` uses)."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    (x1, y1), (x2, y2) = seg_ep
    content = (
        _HEADER_2L
        + f"  (via (at {via_center[0]} {via_center[1]}) (size 0.6) (drill 0.3)"
        + f'   (layers "F.Cu" "B.Cu") (net {via_net} "SIG{via_net}") (uuid "via-1"))\n'
        + f"  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2)"
        + f'   (layer "F.Cu") (net {seg_net} "SIG{seg_net}") (uuid "seg-1"))\n'
        + ")\n"
    )
    pcb_path = tmp_path / "seg_via.kicad_pcb"
    pcb_path.write_text(content)
    pcb = PCB.load(pcb_path)
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    results = checker.check_clearances()
    return any(v.rule_id == "clearance_segment_via" for v in results.violations)


def test_checker_and_router_agree_on_body_crossing(tmp_path) -> None:
    # Different-net body crossing: both the checker AND the lattice predicate
    # reject it -- no router-vs-checker disagreement.
    seg = ((100.0, 100.0), (102.0, 100.0))
    via = (101.0, 100.0)
    assert _checker_flags_segment_via(seg, via, 1, 2, tmp_path) is True

    c = _committed()
    c.add_via(via, 2)
    assert c.seg_clear(seg[0], seg[1], 0, 1) is False


def test_checker_and_router_agree_on_endpoint_exemption(tmp_path) -> None:
    # Endpoint coincidence (in-pad invariant, #2706): the checker suppresses it
    # AND the lattice predicate permits it (same-net) -- both agree it is legal.
    seg = ((100.0, 100.0), (102.0, 100.0))
    via = (100.0, 100.0)
    assert _checker_flags_segment_via(seg, via, 1, 2, tmp_path) is False

    c = _committed()
    c.add_via(via, 1)  # same-net endpoint tap
    assert c.seg_clear(seg[0], seg[1], 0, 1) is True


def test_colocation_epsilon_matches_checker() -> None:
    # The lattice epsilon must equal the DRC checker's co-location epsilon so
    # the accept gate and kct check agree by construction.
    from kicad_tools.validate.rules import clearance as clearance_mod

    assert math.isclose(_COLOCATION_EPSILON_MM, clearance_mod._COLOCATION_EPSILON_MM)
