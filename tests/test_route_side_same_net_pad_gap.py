"""Issue #8974: route-side vias keep their annulus out of the same-net pad slit band.

#8957 / PR #8963 taught the stitcher CLI's ``--avoid-pad-overlap`` ladder to
keep a via's copper ANNULUS out of the fab-view ``(0, edge)`` slit band beside
its own SMD pad.  The route side still had two gaps this issue closes, both
by engaging the SAME merged predicate -- never a second copy:

1. ``escape.py``'s ``_can_place_via`` skipped same-net pads structurally
   (Issue #2951) with no band check at all -- PR #8963's own docstring note
   handed the case forward.
2. The oracle weld's ``run_stitch`` call never passed
   ``same_net_pad_gap_mm``, so the merged guard was off on the weld path.

The band edge is the caller-resolved
``DesignRules.same_net_pad_gap_band_edge_mm`` -- the same
``GapBand.detection_edge_mm`` of the stackup's ``pad_spacing`` dfm entry that
``_resolve_mfr_pad_gap_edge`` reads (0.15 mm on jlcpcb ``4layer_1oz`` and,
since #9016, also on ``4layer_2oz``, whose 0.13 mm warning value sits below
its danger value).  ``None``/0 keeps the guard OFF: an unresolvable
stackup never guesses an edge (AC 3).
"""

from __future__ import annotations

import argparse
import math

import pytest

shapely = pytest.importorskip("shapely")

from shapely.geometry import Point, box  # noqa: E402

from kicad_tools.cli.stitch_cmd import _resolve_mfr_pad_gap_edge  # noqa: E402
from kicad_tools.router.core import RoutingGrid  # noqa: E402
from kicad_tools.router.escape import EscapeRouter  # noqa: E402
from kicad_tools.router.layers import Layer  # noqa: E402
from kicad_tools.router.link_router import (  # noqa: E402
    LinkRouteRules,
    LinkTerminal,
    _BoardModel,
    _Item,
    _via_site_blocker,
    route_link,
)
from kicad_tools.router.oracle_completion import PourLinkCloser  # noqa: E402
from kicad_tools.router.primitives import Pad, pad_half_extents  # noqa: E402
from kicad_tools.router.rules import DesignRules  # noqa: E402
from kicad_tools.router.via_clearance import (  # noqa: E402
    same_net_pad_gap_acceptable,
    same_net_pad_gap_band_ring,
)

VIA = 0.6
DRILL = 0.3
R = VIA / 2  # 0.3

#: The issue's worked-example pad: 0.3 x 0.7 SMD rect centred at the origin.
PAD_W, PAD_H = 0.3, 0.7
PAD_TOP = PAD_H / 2  # 0.35
PAD_BBOX = (-PAD_W / 2, -PAD_H / 2, PAD_W / 2, PAD_H / 2)

EDGE_1OZ = 0.15  # jlcpcb 4layer_1oz pad_spacing danger (no warning band)
#: jlcpcb 4layer_2oz resolves to the same 0.15 mm detection edge since #9016
#: (its recorded 0.13 mm warning value sits below the danger value).
EDGE_2OZ = 0.15
#: A narrower caller-resolved edge, used only to show the guard follows the
#: scalar it is handed rather than a literal.
EDGE_NARROW = 0.13


def _rules(edge: float | None) -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=DRILL,
        via_diameter=VIA,
        via_clearance=0.15,
        grid_resolution=0.1,
        same_net_pad_gap_band_edge_mm=edge,
    )


def _router(edge: float | None) -> EscapeRouter:
    rules = _rules(edge)
    grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
    return EscapeRouter(grid, rules, component_holes=())


def _own_pad() -> Pad:
    return Pad(x=0.0, y=0.0, width=PAD_W, height=PAD_H, net=1, net_name="GND")


def _foreign_pad() -> Pad:
    return Pad(x=2.0, y=0.0, width=0.3, height=0.7, net=2, net_name="VCC")


def _own_tht_pad() -> Pad:
    return Pad(
        x=0.0,
        y=0.0,
        width=PAD_W,
        height=PAD_H,
        net=1,
        net_name="GND",
        through_hole=True,
        drill=0.3,
    )


def _annulus_gap(via_y: float) -> float:
    return via_y - PAD_TOP - R


# -- _can_place_via: the band rejects / the ladder advances -------------------


def test_candidate_inside_band_is_rejected():
    # The board 07 stamped pattern: 0.0891 mm of air between the annulus
    # edge and its own pad.  ``False`` IS the caller ladder's "try the next
    # site" signal -- the rejection must advance, never abort.
    router = _router(EDGE_1OZ)
    y = PAD_TOP + R + 0.0891
    assert 0.0 < _annulus_gap(y) < EDGE_1OZ
    assert not router._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_pad(), _foreign_pad()], clearance=0.05
    )


def test_candidate_overlapping_pad_is_accepted():
    # gap < 0: the annulus and the pad are one copper union -- no slit, the
    # guard must accept (drill-in-pad is other guards' business).
    router = _router(EDGE_1OZ)
    y = PAD_TOP + R - 0.075
    assert _annulus_gap(y) <= 0.0
    assert router._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_pad(), _foreign_pad()], clearance=0.05
    )


def test_candidate_beyond_edge_is_accepted():
    router = _router(EDGE_1OZ)
    y = PAD_TOP + R + EDGE_1OZ + 0.05
    assert _annulus_gap(y) >= EDGE_1OZ
    assert router._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_pad(), _foreign_pad()], clearance=0.05
    )


def test_guard_off_when_edge_unresolved():
    # AC 3: an unresolvable stackup keeps the guard OFF, not a guess.  Same
    # in-band seat as the rejection test, accepted because no edge resolved.
    y = PAD_TOP + R + 0.0891
    for edge in (None, 0.0):
        router = _router(edge)
        assert router._can_place_via(
            0.0, y, net=1, foreign_pads=[_own_pad(), _foreign_pad()], clearance=0.05
        )


def test_narrower_caller_edge_accepts_a_gap_the_wider_edge_rejects():
    # A 0.14 mm gap is inside a 0.15 mm band (rejected) and outside a
    # 0.13 mm one (accepted) -- the guard follows the caller-resolved scalar,
    # so a hardcoded literal would betray one of the two edges.
    y = PAD_TOP + R + 0.14
    assert not _router(EDGE_1OZ)._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_pad()], clearance=0.05
    )
    assert _router(EDGE_NARROW)._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_pad()], clearance=0.05
    )


def test_no_smd_pads_no_op():
    # A net with no SMD pads (here: the same-net pad is through-hole) must
    # not regress -- the predicate is about SMD lands.
    y = PAD_TOP + R + 0.0891
    router = _router(EDGE_1OZ)
    assert router._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_tht_pad(), _foreign_pad()], clearance=0.05
    )


def test_foreign_pads_alone_do_not_trip_the_band():
    # The band is a same-net constraint; a foreign-net pad never feeds it
    # (that is point_clear_of_copper's job).
    y = PAD_TOP + R + 0.0891
    router = _router(EDGE_1OZ)
    assert router._can_place_via(0.0, y, net=1, foreign_pads=[_foreign_pad()], clearance=0.05)


# -- Issue #9236: rotated pads use the rotation-aware AABB --------------------
#
# ``Pad.width``/``height`` are pre-rotation local dims plus a residual
# ``Pad.rotation`` (primitives.py).  A raw ``p.x +/- p.width/2`` box under-sizes
# a non-cardinal pad, so a via seated beside its long diagonal reads as
# "far enough" and escapes the slit band.  ``pad_half_extents`` is the repo's
# rotation-aware AABB helper.


def _rotated_pad(rotation: float, layer: Layer = Layer.F_CU, net: int = 1) -> Pad:
    return Pad(
        x=0.0,
        y=0.0,
        width=PAD_W,
        height=PAD_H,
        net=net,
        net_name="GND" if net == 1 else "VCC",
        layer=layer,
        rotation=rotation,
    )


@pytest.mark.parametrize(
    ("rotation", "layer"),
    [(45.0, Layer.F_CU), (30.0, Layer.B_CU)],
)
def test_rotated_same_net_pad_uses_rotation_aware_bbox(rotation, layer):
    pad = _rotated_pad(rotation, layer)
    hw, _hh = pad_half_extents(pad)
    # Seat beside the pad on +X: the raw 0.3 mm-wide box sees a gap well past
    # the 0.15 mm edge, the true rotated AABB sees a gap inside the band.
    x = 0.72
    raw_gap = x - PAD_W / 2 - R
    true_gap = x - hw - R
    assert raw_gap >= EDGE_1OZ
    assert 0.0 < true_gap < EDGE_1OZ
    router = _router(EDGE_1OZ)
    assert not router._can_place_via(x, 0.0, net=1, foreign_pads=[pad], clearance=0.05)


@pytest.mark.parametrize("rotation", [0.0, 90.0])
def test_cardinal_rotation_bbox_is_unchanged(rotation):
    # rotation 0 / 90 keep the pre-fix result: 0 is the raw box; 90 is
    # handled by the loader's axis swap (``rotation`` carries only the residual
    # 0 here), so the in-band / beyond-edge seats behave exactly as before.
    pad = _rotated_pad(rotation)
    router = _router(EDGE_1OZ)
    in_band = PAD_TOP + R + 0.0891
    beyond = PAD_TOP + R + EDGE_1OZ + 0.05
    if rotation == 90.0:
        # An unswapped 90-degree pad is a 0.7 x 0.3 box on the board.
        in_band = PAD_W / 2 + R + 0.0891
        beyond = PAD_W / 2 + R + EDGE_1OZ + 0.05
        assert not router._can_place_via(0.0, in_band, net=1, foreign_pads=[pad], clearance=0.05)
        assert router._can_place_via(0.0, beyond, net=1, foreign_pads=[pad], clearance=0.05)
        return
    assert not router._can_place_via(0.0, in_band, net=1, foreign_pads=[pad], clearance=0.05)
    assert router._can_place_via(0.0, beyond, net=1, foreign_pads=[pad], clearance=0.05)


def test_rotated_through_hole_same_net_pad_still_skipped():
    pad = _rotated_pad(45.0)
    pad.through_hole = True
    pad.drill = 0.3
    router = _router(EDGE_1OZ)
    assert router._can_place_via(0.72, 0.0, net=1, foreign_pads=[pad], clearance=0.05)


def test_rotated_foreign_pad_uses_rotation_aware_box():
    # Same raw-dims shape on the foreign-pad 5-tuple: a 45-degree foreign pad
    # whose raw 0.3 mm width leaves the via 0.05 mm clear but whose true AABB
    # half-width (0.354) puts the via annulus inside clearance.
    pad = _rotated_pad(45.0, net=2)
    router = _router(EDGE_1OZ)
    x = 0.72  # raw edge 0.15 -> 0.57 mm to via edge >> 0.2; true edge 0.354 -> 0.066
    assert not router._can_place_via(x, 0.0, net=1, foreign_pads=[pad], clearance=0.15)
    # With rotation 0 the same seat is clear -> proves rotation is the cause.
    assert router._can_place_via(
        x, 0.0, net=1, foreign_pads=[_rotated_pad(0.0, net=2)], clearance=0.15
    )


def test_via_clears_other_pads_uses_rotation_aware_box():
    router = _router(EDGE_1OZ)
    x = 0.72
    rotated = _rotated_pad(45.0, net=2)
    flat = _rotated_pad(0.0, net=2)
    assert not router._via_clears_other_pads(x, 0.0, VIA, 0.15, [rotated], same_net=1)
    assert router._via_clears_other_pads(x, 0.0, VIA, 0.15, [flat], same_net=1)


# -- the edge is stackup-derived, never a literal -----------------------------


def test_band_edge_resolves_from_jlcpcb_table():
    # The same resolver the stitcher uses feeds the DesignRules scalar.
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 1.0) == pytest.approx(EDGE_1OZ)
    assert _resolve_mfr_pad_gap_edge("jlcpcb", 4, 2.0) == pytest.approx(EDGE_2OZ)


def test_designrules_field_defaults_off():
    rules = DesignRules()
    assert rules.same_net_pad_gap_band_edge_mm is None


def test_designrules_post_init_does_not_resolve_the_edge():
    # AC 3 trap: ``manufacturer`` alone must NOT resolve the field (that is
    # what ``min_via_pad_edge_gap``'s __post_init__ does) -- DesignRules has
    # no stackup fields, so a post-init resolution would be stackup-blind.
    rules = DesignRules(manufacturer="jlcpcb")
    assert rules.same_net_pad_gap_band_edge_mm is None


# -- the oracle weld forwards the merged guard --------------------------------


def test_pour_link_closer_defaults_guard_off():
    closer = PourLinkCloser(via_size=VIA, via_drill=DRILL, clearance=0.2, trace_width=0.2)
    assert closer.same_net_pad_gap_mm == 0.0


def test_pour_link_closer_carries_the_edge():
    closer = PourLinkCloser(
        via_size=VIA,
        via_drill=DRILL,
        clearance=0.2,
        trace_width=0.2,
        same_net_pad_gap_mm=EDGE_1OZ,
    )
    assert closer.same_net_pad_gap_mm == pytest.approx(EDGE_1OZ)


def test_weld_forwards_band_edge_to_run_stitch(monkeypatch, tmp_path):
    """The weld's ``run_stitch`` call passes ``same_net_pad_gap_mm`` through.

    Issue #8974: the weld (``PourLinkCloser._weld_pads``) passes
    ``avoid_pad_overlap`` and ``pad_edge_gap`` today; the band edge must
    ride along or the merged stitcher guard stays off on the weld path.
    """

    import kicad_tools.cli.stitch_cmd as stitch_cmd

    seen: dict[str, object] = {}

    class _Result:
        vias_added = []
        pads_skipped = []
        traces_added = []
        already_connected = True

    def _fake_run_stitch(*args, **kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(stitch_cmd, "run_stitch", _fake_run_stitch)
    # ``_weld_pads`` imports run_stitch from the module at call time.
    closer = PourLinkCloser(
        via_size=VIA,
        via_drill=DRILL,
        clearance=0.2,
        trace_width=0.2,
        pad_edge_gap=0.1,
        same_net_pad_gap_mm=EDGE_1OZ,
    )
    from kicad_tools.router.oracle_completion import ClosureAttempt

    attempt = ClosureAttempt()
    closer._weld_pads(tmp_path / "board.kicad_pcb", {"GND": {"U1.1"}}, attempt)
    assert seen.get("same_net_pad_gap_mm") == pytest.approx(EDGE_1OZ)
    assert seen.get("pad_edge_gap") == pytest.approx(0.1)
    assert seen.get("avoid_pad_overlap") == closer.avoid_via_in_pad


# -- the link router rides the same predicate (no second band copy) -----------


def _link_rules(edge: float) -> LinkRouteRules:
    return LinkRouteRules(
        trace_width=0.2,
        clearance=0.2,
        via_size=VIA,
        via_drill=DRILL,
        margin=2.0,
        same_net_pad_gap_mm=edge,
    )


def _own_smd_item() -> _Item:
    return _Item(
        1,
        {"F.Cu": box(-PAD_W / 2, -PAD_H / 2, PAD_W / 2, PAD_H / 2)},
        is_pad=True,
    )


def _empty_link_model() -> _BoardModel:
    return _BoardModel(
        copper=["F.Cu", "B.Cu"],
        items=[_own_smd_item()],
        drills=[],
        pad_geoms=[],
        fills={},
        outline=None,
    )


def test_via_site_blocker_rejects_the_band():
    # ``_via_site_blocker`` delegates to the shared point predicate (bboxes),
    # never a local gap loop -- the same seat the escape router rejects.
    msg = _via_site_blocker(
        _empty_link_model(), 1, (0.0, PAD_TOP + R + 0.0891), _link_rules(EDGE_1OZ)
    )
    assert msg is not None and "slit band" in msg


def test_via_site_blocker_beyond_edge_is_clean():
    assert (
        _via_site_blocker(
            _empty_link_model(), 1, (0.0, PAD_TOP + R + EDGE_1OZ + 0.05), _link_rules(EDGE_1OZ)
        )
        is None
    )


def test_via_site_blocker_overlapping_own_pad_is_band_clean():
    # gap <= 0: one copper union, the band accepts (via-in-pad itself is
    # other guards' business -- ``model.pad_geoms`` is empty here).
    assert _via_site_blocker(_empty_link_model(), 1, (0.0, PAD_TOP), _link_rules(EDGE_1OZ)) is None


def test_via_site_blocker_guard_off_when_edge_unresolved():
    # The same in-band seat, accepted because the caller resolved no edge.
    assert (
        _via_site_blocker(_empty_link_model(), 1, (0.0, PAD_TOP + R + 0.0891), _link_rules(0.0))
        is None
    )


def test_route_link_via_lands_outside_the_band():
    # The A*'s via mask must not just work -- it must agree with the shared
    # predicate: every via the search actually places is predicate-clean.
    a = LinkTerminal("a", (10.0, 10.0), {"F.Cu": box(9.6, 9.6, 10.4, 10.4)})
    b = LinkTerminal("b", (14.0, 10.0), {"B.Cu": box(13.6, 9.6, 14.4, 10.4)})
    own_pad = _Item(1, {"F.Cu": box(9.6, 9.6, 10.4, 10.4)}, is_pad=True)
    model = _BoardModel(
        copper=["F.Cu", "B.Cu"],
        items=[own_pad],
        drills=[],
        pad_geoms=[],
        fills={},
        outline=None,
    )
    route = route_link(None, 1, "GND", a, b, _link_rules(EDGE_1OZ), model=model)
    assert route is not None and route.vias
    for via_x, via_y in route.vias:
        assert same_net_pad_gap_acceptable(via_x, via_y, VIA, [(9.6, 9.6, 10.4, 10.4)], EDGE_1OZ)


def test_band_ring_matches_the_point_predicate():
    # ``same_net_pad_gap_band_ring`` is the region form of the predicate:
    # every sampled seat the ring covers is a seat the predicate rejects,
    # and vice versa -- one band definition feeds the mask and the point
    # re-checks (#8974 review: link_router must not hold its own copy).
    ring = same_net_pad_gap_band_ring(PAD_BBOX, VIA, EDGE_1OZ)
    for gx in range(-20, 21):
        for gy in range(-45, 46):
            x, y = gx * 0.02, gy * 0.02
            rejected = not same_net_pad_gap_acceptable(x, y, VIA, [PAD_BBOX], EDGE_1OZ)
            assert ring.covers(Point(x, y)) is rejected
    assert same_net_pad_gap_band_ring(PAD_BBOX, VIA, 0.0).is_empty


# -- the predicate stays the single band definition ---------------------------


def test_predicate_semantics_unchanged():
    # The route side must call the SAME predicate the stitcher does; this
    # pins the shared contract so a fork would fail here first.
    y = PAD_TOP + R + 0.0891
    assert not same_net_pad_gap_acceptable(0.0, y, VIA, [PAD_BBOX], EDGE_1OZ)
    assert same_net_pad_gap_acceptable(0.0, PAD_TOP + R, VIA, [PAD_BBOX], EDGE_1OZ)
    assert same_net_pad_gap_acceptable(
        0.0, PAD_TOP + R + EDGE_1OZ + 1e-6, VIA, [PAD_BBOX], EDGE_1OZ
    )
    assert math.isclose(_annulus_gap(PAD_TOP + R + 0.0891), 0.0891, abs_tol=1e-9)


# -- the resolution point in route_cmd (no fab => guard off, no guess) --------


def _route_args(**over):
    """A minimal ``kct route`` args namespace for the resolution point.

    ``layers="2"`` keeps ``_effective_layer_count_for_rules`` off the board
    file, and the stackup probe swallows a missing board (Issue #6561's
    broad except), so ``pcb_path`` never has to exist.
    """
    ns = argparse.Namespace(
        manufacturer=None,
        copper=None,
        layers="2",
        trace_width=None,
        min_trace=None,
    )
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def test_route_cmd_resolution_is_none_without_a_manufacturer():
    # Finding 1 of the #8974 review: the old "jlcpcb" fallback turned the
    # band guard ON for every no-``--mfr`` run, against a fab the operator
    # never named.  With no manufacturer there is nothing to resolve from:
    # None, mirroring ``_resolve_route_clearance_floor`` and the stitcher's
    # ``args.mfr`` gate (AC 3).
    from kicad_tools.cli.route_cmd import _apply_manufacturer_trace_width_floor

    args = _route_args()
    assert _apply_manufacturer_trace_width_floor(args, "unused.kicad_pcb", quiet=True) == 0
    assert args._mfr_pad_gap_band_edge is None


def test_route_cmd_resolution_engages_with_a_manufacturer():
    # The guard still engages when the operator names a fab: jlcpcb
    # 2layer_1oz resolves a real edge from the profile table.
    from kicad_tools.cli.route_cmd import _apply_manufacturer_trace_width_floor

    args = _route_args(manufacturer="jlcpcb")
    assert _apply_manufacturer_trace_width_floor(args, "unused.kicad_pcb", quiet=True) == 0
    assert args._mfr_pad_gap_band_edge == pytest.approx(EDGE_1OZ)
