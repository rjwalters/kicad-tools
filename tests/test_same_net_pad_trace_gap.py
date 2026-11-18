"""Issue #8976: the same-net pad-vs-trace slit-band rule, generalised.

``same_net_pad_gap_acceptable`` (#8957) holds a via annulus out of the
same-net SMD pad slit band.  Issue #8976 measured this issue's third
producer population -- same-net pad-vs-trace pairs with no via involved --
on the board 06 / board 07 seed-42 regenerations: the pad's OWN escape
trace skirts its pad inside the fab's ``pad_spacing`` band (14 findings on
board 07, 13 on board 06, every offending leg within 0.017-0.146 mm of its
pad), because both router engines exempt same-net copper from their
leg-legality predicates.

This module gates the generalised predicate
(:func:`~kicad_tools.router.via_clearance.same_net_segment_pad_gap_acceptable`)
that the router-side fix will enforce: a same-net pad and trace must either
touch/overlap (one copper union) or sit at or beyond the fab's *detection*
edge -- never in between.  The edge is the resolved ``pad_spacing``
``GapBand.detection_edge_mm``, never ``error_edge_mm``: on a stackup whose
warning tier sits below the danger tier (jlcpcb ``4layer_2oz``: warning
0.13 below danger 0.15) the detection edge is wider, and enforcing the
error edge would leave ``[error_edge, detection_edge)`` gaps graded as
warnings rather than removed.
"""

from __future__ import annotations

import math

from kicad_tools.router.via_clearance import (
    same_net_pad_gap_acceptable,
    same_net_segment_pad_gap_acceptable,
)

EDGE = 0.15  # jlcpcb pad_spacing detection edge on both fixtures (warning_mm None)

#: The #8957 worked-example pad: 0.3 x 0.7 SMD rect centred at the origin.
PAD_BBOX = (-0.15, -0.35, 0.15, 0.35)

W = 0.225  # the board 07 escape trace width
HALF = W / 2

# The measured board 07 shapes (sheet-frame y values normalised to x=0):
# J1-style vertical hug at 0.0176 mm, U3-style horizontal hug at 0.1139 mm.
HUG_GAP_J1 = 0.0176
HUG_GAP_U3 = 0.1139


def _vertical_leg_at_gap(gap: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """A vertical trace whose right edge sits ``gap`` right of the pad's left edge."""
    x = PAD_BBOX[0] - gap - HALF
    return (x, 0.0), (x, 0.7)


def _horizontal_leg_below(gap: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """A horizontal trace whose top edge sits ``gap`` below the pad's bottom edge."""
    y = PAD_BBOX[1] - gap - HALF
    return (-0.8, y), (0.2, y)


# -- the two-region rule for a pad/segment pair (AC unit gate) ---------------


def test_hug_inside_band_rejected():
    # The measured J1 pattern: 0.0176 mm of air between trace edge and pad.
    assert not same_net_segment_pad_gap_acceptable(
        *_vertical_leg_at_gap(HUG_GAP_J1), HALF, [PAD_BBOX], EDGE
    )


def test_hug_inside_band_rejected_at_measured_u3_gap():
    assert not same_net_segment_pad_gap_acceptable(
        *_horizontal_leg_below(HUG_GAP_U3), HALF, [PAD_BBOX], EDGE
    )


def test_touching_pair_accepted():
    # The trace's copper edge exactly on the pad edge: one copper union.
    assert same_net_segment_pad_gap_acceptable(*_vertical_leg_at_gap(0.0), HALF, [PAD_BBOX], EDGE)


def test_overlapping_pair_accepted():
    assert same_net_segment_pad_gap_acceptable(*_vertical_leg_at_gap(-0.05), HALF, [PAD_BBOX], EDGE)


def test_at_edge_accepted():
    # Exactly at the detection edge: not a fab finding (severity_for -> None).
    assert same_net_segment_pad_gap_acceptable(*_vertical_leg_at_gap(EDGE), HALF, [PAD_BBOX], EDGE)


def test_beyond_edge_accepted():
    assert same_net_segment_pad_gap_acceptable(
        *_vertical_leg_at_gap(EDGE + 0.01), HALF, [PAD_BBOX], EDGE
    )


def test_diagonal_crossing_into_pad_accepted():
    # A trace that legitimately terminates on the pad: the crossing leg is
    # one copper union, whatever the band says along its length.
    # Endpoint inside the pad, other end far outside, 45-degree diagonal.
    assert same_net_segment_pad_gap_acceptable((-1.0, -1.0), (0.05, 0.05), HALF, [PAD_BBOX], EDGE)


def test_diagonal_skirt_inside_band_rejected():
    # The measured J2 pattern: a 45-degree leg passing under the pad's
    # bottom edge inside the band (closest approach 0.0875 mm below the
    # copper edge, mirroring the measured 0.1317 mm corner skirts).
    assert not same_net_segment_pad_gap_acceptable(
        (-1.0, -1.55), (0.0, -0.55), HALF, [PAD_BBOX], EDGE
    )


def test_diagonal_corner_pass_outside_band_accepted():
    # The same 45-degree line kept 0.2121 mm clear of the pad's bottom-left
    # corner: copper gap 0.2121 - half = ~0.0996 -- still inside the band,
    # so rejected; a half-step further out clears it.
    c = -0.5  # y = x + c
    assert not same_net_segment_pad_gap_acceptable(
        (-1.0, -1.0 + c), (0.0, c), HALF, [PAD_BBOX], EDGE
    )
    c2 = -0.75  # corner distance 0.55/sqrt(2) ~ 0.389; gap ~0.276 > EDGE
    assert same_net_segment_pad_gap_acceptable((-1.0, -1.0 + c2), (0.0, c2), HALF, [PAD_BBOX], EDGE)


def test_same_net_trace_on_other_layer_not_tested():
    # No shared-layer copper gap: the caller passes only own-layer bboxes;
    # an empty list disables the test entirely.
    assert same_net_segment_pad_gap_acceptable(*_vertical_leg_at_gap(HUG_GAP_J1), HALF, [], EDGE)


def test_unresolvable_edge_disables_check():
    # Leave the check off rather than guessing an edge (min_gap_mm <= 0).
    assert same_net_segment_pad_gap_acceptable(
        *_vertical_leg_at_gap(HUG_GAP_J1), HALF, [PAD_BBOX], 0.0
    )
    assert same_net_segment_pad_gap_acceptable(
        *_vertical_leg_at_gap(HUG_GAP_J1), HALF, [PAD_BBOX], -1.0
    )


def test_zero_width_segment_rejected_in_band():
    # A zero-width centreline still measures a real gap against the box.
    x = PAD_BBOX[0] - 0.1
    assert not same_net_segment_pad_gap_acceptable((x, 0.0), (x, 0.7), 0.0, [PAD_BBOX], EDGE)


def test_beyond_band_far_leg_accepted():
    # A long leg far from the pad: distance dominated by the endpoint gap.
    assert same_net_segment_pad_gap_acceptable((-5.0, -5.0), (-3.0, -3.0), HALF, [PAD_BBOX], EDGE)


# -- the detection edge, not the error edge (issue #8976 correction) ---------


def test_warning_tier_stackup_requires_detection_edge():
    # jlcpcb 4layer_2oz pad_spacing: warning 0.13 below danger 0.15, so
    # error_edge_mm = 0.13 and detection_edge_mm = 0.15.  A 0.14 mm gap is
    # not a fab error but IS a fab warning -- the check must reject it.
    assert not same_net_segment_pad_gap_acceptable(
        *_vertical_leg_at_gap(0.14), HALF, [PAD_BBOX], 0.15
    )
    # ...while the same gap passes when (incorrectly) given the error edge,
    # which is exactly the under-constraint the correction calls out.
    assert same_net_segment_pad_gap_acceptable(*_vertical_leg_at_gap(0.14), HALF, [PAD_BBOX], 0.13)


# -- the via annulus form delegates with identical behaviour -----------------


def test_via_form_matches_point_degenerate_segment_form():
    via_size = 0.45
    radius = via_size / 2
    for y in (-1.0, 0.0, 0.2, 0.35, 0.5, 0.58, 0.6, 0.75, 1.0):
        for edge in (0.0, 0.13, 0.15):
            assert same_net_pad_gap_acceptable(
                0.0, y, via_size, [PAD_BBOX], edge
            ) == same_net_segment_pad_gap_acceptable((0.0, y), (0.0, y), radius, [PAD_BBOX], edge)


def test_via_form_band_semantics_unchanged():
    # The #8957 contract: reject inside the band, accept union and beyond.
    via = 0.45
    radius = via / 2
    pad_top = PAD_BBOX[3]
    # Seated 0.075 beyond the annulus edge: gap 0.075, inside the band.
    assert not same_net_pad_gap_acceptable(0.0, pad_top + radius + 0.075, via, [PAD_BBOX], EDGE)
    # Annulus edge exactly tangent to the pad (Issue #9124): a one-point
    # touch leaves a wedge, not a union -- rejected.
    assert not same_net_pad_gap_acceptable(0.0, pad_top + radius, via, [PAD_BBOX], EDGE)
    # Real overlap: one union.
    assert same_net_pad_gap_acceptable(0.0, pad_top + radius - 0.01, via, [PAD_BBOX], EDGE)
    # Gap exactly at the edge: not a fab finding.
    assert same_net_pad_gap_acceptable(0.0, pad_top + radius + EDGE, via, [PAD_BBOX], EDGE)


# -- measured-board sanity: the generalised predicate grades the regen slits --


def test_measured_board07_legs_graded_in_band():
    # The four measured board 07 shapes, reproduced at their measured gaps.
    for gap in (HUG_GAP_J1, HUG_GAP_U3, 0.0366, 0.1139):
        assert not same_net_segment_pad_gap_acceptable(
            *_vertical_leg_at_gap(gap), HALF, [PAD_BBOX], EDGE
        )
        assert not same_net_segment_pad_gap_acceptable(
            *_horizontal_leg_below(gap), HALF, [PAD_BBOX], EDGE
        )


def test_geometry_gaps_match_shapely_free_arithmetic():
    # The helper's gap arithmetic: verify a couple of exact values against
    # the same numbers the checker measured on the regenerations.
    a, b = _vertical_leg_at_gap(HUG_GAP_J1)
    x = a[0]
    # pad left edge - (x + half width)
    assert math.isclose(PAD_BBOX[0] - (x + HALF), HUG_GAP_J1, abs_tol=1e-12)
