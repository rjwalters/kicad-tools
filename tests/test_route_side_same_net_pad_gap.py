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
``GapBand.error_edge_mm`` of the stackup's ``pad_spacing`` dfm entry that
``_resolve_mfr_pad_gap_edge`` reads (0.15 mm on jlcpcb ``4layer_1oz``,
0.13 mm on ``4layer_2oz``).  ``None``/0 keeps the guard OFF: an unresolvable
stackup never guesses an edge (AC 3).
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.cli.stitch_cmd import _resolve_mfr_pad_gap_edge
from kicad_tools.router.core import RoutingGrid
from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.oracle_completion import PourLinkCloser
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_clearance import same_net_pad_gap_acceptable

VIA = 0.6
DRILL = 0.3
R = VIA / 2  # 0.3

#: The issue's worked-example pad: 0.3 x 0.7 SMD rect centred at the origin.
PAD_W, PAD_H = 0.3, 0.7
PAD_TOP = PAD_H / 2  # 0.35
PAD_BBOX = (-PAD_W / 2, -PAD_H / 2, PAD_W / 2, PAD_H / 2)

EDGE_1OZ = 0.15  # jlcpcb 4layer_1oz pad_spacing danger (no warning band)
EDGE_2OZ = 0.13  # jlcpcb 4layer_2oz recorded warning band


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


def test_two_oz_edge_bites_below_one_oz_edge():
    # A 0.14 mm gap is an error on 1 oz (rejected) and only a warning on the
    # 2 oz stackup's 0.13 edge (accepted) -- a hardcoded literal would
    # betray one of the two stackups.
    y = PAD_TOP + R + 0.14
    assert not _router(EDGE_1OZ)._can_place_via(
        0.0, y, net=1, foreign_pads=[_own_pad()], clearance=0.05
    )
    assert _router(EDGE_2OZ)._can_place_via(
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
    from kicad_tools.router import oracle_completion

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
