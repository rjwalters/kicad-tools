"""``mask_to_copper``: holes standing on a reinforce strip mid-run (#9444).

After #9419 the anchor-end openings of a recorded run read ``within_reinforce_run``,
but an untented layer-change ``via`` or an anchor-size ``gr_circle`` hole (and its
mask circles) sitting between the anchors had no ``owner_run``: attribution only
reached openings at a confirmed anchor or strip pieces. They are continuous with
the strip, so they belong to the run; copper that is not the run's does not.
"""

import pytest
from shapely.geometry import box

from kicad_tools.physics.wire_gauge import anchor_drill_for_awg, anchor_pad_for_drill
from kicad_tools.validate.mask_copper import (
    _MID_RUN_CIRCLE_FLASH_MM,
    MaskCopperPolicy,
    assess_attributed_geometry,
)
from tests.test_mask_copper_reinforce_9419 import A, B, _disk, _geometry, _record

POLICY = MaskCopperPolicy(0.1, "order", "process", "rev")

MID = (20.0, 10.0)


def _run_pieces():
    """B.Cu leg A-B with anchor vias and an end-hole circle on each mask side."""
    pieces = [
        ("segment", "PWR", {"B.Cu": box(10.0, 9.75, 30.0, 10.25)}),
        ("gr_line", "", {"B.Mask": box(A[0], 9.75, B[0], 10.25)}),
        # The net doubled on the far layer, as on the recorded boards (#9419).
        ("segment", "PWR", {"F.Cu": box(10.0, 9.75, 30.0, 10.25)}),
    ]
    for x, y in (A, B):
        pieces.append(
            (
                "via",
                "PWR",
                {
                    "F.Cu": _disk(x, y, 0.4),
                    "B.Cu": _disk(x, y, 0.4),
                    "F.Mask": _disk(x, y, 0.4),
                    "B.Mask": _disk(x, y, 0.4),
                },
            )
        )
    return pieces


def _mid_via(net="PWR", at=MID):
    x, y = at
    return (
        "via",
        net,
        {
            "F.Cu": _disk(x, y, 0.4),
            "B.Cu": _disk(x, y, 0.4),
            "F.Mask": _disk(x, y, 0.4),
            "B.Mask": _disk(x, y, 0.4),
        },
    )


def _mid_circle(at=MID):
    x, y = at
    return ("gr_circle", "", {"F.Mask": _disk(x, y, 0.7), "B.Mask": _disk(x, y, 0.7)})


def _run(pieces, **record):
    return assess_attributed_geometry(_geometry(pieces), POLICY, (), [_record(**record)])


def _near(result, point, radius=1.0):
    from shapely.geometry import Point

    return [m for m in result.measurements if Point(m.location_mm).distance(Point(point)) <= radius]


def test_mid_run_via_is_within_reinforce_run_on_both_sides():
    result = _run(_run_pieces() + [_mid_via()])
    mid = _near(result, MID)
    assert {m.mask_side for m in mid} == {"F.Mask", "B.Mask"}
    assert {m.disposition for m in mid} == {"within_reinforce_run"}
    assert {m.owner_run for m in mid} == {"HS_POWER"}
    assert result.passed
    assert not [m for m in result.measurements if m.disposition == "violation"]


def test_mid_run_gr_circle_hole_is_within_reinforce_run_on_both_sides():
    result = _run(_run_pieces() + [_mid_circle()])
    mid = _near(result, MID)
    assert {m.mask_side for m in mid} == {"F.Mask", "B.Mask"}
    assert {m.disposition for m in mid} == {"within_reinforce_run"}
    assert {m.owner_run for m in mid} == {"HS_POWER"}
    assert result.passed


def test_mid_run_via_with_circle_on_far_side_run():
    """Run recorded on F.Cu: the via is on the strip side, the far side is B.Mask."""
    pieces = [
        ("segment", "PWR", {"F.Cu": box(10.0, 9.75, 30.0, 10.25)}),
        ("gr_line", "", {"F.Mask": box(A[0], 9.75, B[0], 10.25)}),
    ]
    for x, y in (A, B):
        pieces.append(
            (
                "via",
                "PWR",
                {
                    "F.Cu": _disk(x, y, 0.4),
                    "B.Cu": _disk(x, y, 0.4),
                    "F.Mask": _disk(x, y, 0.4),
                    "B.Mask": _disk(x, y, 0.4),
                },
            )
        )
    pieces += [_mid_via(), _mid_circle()]
    result = _run(pieces, layer="F.Cu")
    assert result.passed
    assert {m.disposition for m in _near(result, MID)} == {"within_reinforce_run"}


def test_different_net_track_beside_a_mid_run_via_is_still_an_error():
    pieces = _run_pieces() + [
        _mid_via(),
        ("segment", "OTHER", {"F.Cu": box(19.0, 10.45, 21.0, 10.55)}),
    ]
    result = _run(pieces)
    errors = [m for m in result.measurements if m.disposition == "violation"]
    assert errors
    assert {m.conductor_net for m in errors} == {"OTHER"}
    assert not result.passed


def test_same_net_via_away_from_the_run_is_not_attributed():
    far_via = (20.0, 30.0)
    pieces = _run_pieces() + [
        ("segment", "PWR", {"B.Cu": box(15.0, 29.75, 25.0, 30.25)}),
        _mid_via(at=far_via),
    ]
    result = _run(pieces)
    away = _near(result, far_via)
    assert away
    assert all(m.owner_run is None for m in away)
    assert any(m.disposition != "within_reinforce_run" for m in away)


def test_different_net_via_on_the_strip_is_not_attributed_to_the_run():
    pieces = _run_pieces() + [_mid_via(net="OTHER", at=(24.0, 10.0))]
    result = _run(pieces)
    assert not result.passed
    assert all(
        m.owner_run is None
        for m in result.measurements
        if m.conductor_net == "OTHER" and m.mask_side == "F.Mask"
    )


def test_same_net_via_beside_the_run_is_not_attributed():
    """Centre 0.6 mm off the centreline: its opening touches the strip edge only."""
    result = _run(_run_pieces() + [_mid_via(at=(20.0, 10.6))])
    # As on main: the via's own opening is measured, not excused.
    assert not result.passed
    assert any(m.disposition == "violation" and m.owner_run is None for m in result.measurements)


def test_same_net_via_just_short_of_the_strip_is_not_attributed():
    result = _run(_run_pieces() + [_mid_via(at=(20.0, 10.69))])
    assert not result.passed
    assert any(m.disposition == "violation" and m.owner_run is None for m in result.measurements)


def test_oversize_gr_circle_centred_off_the_strip_is_not_attributed():
    """Refused by the centre-on-strip gate (not the size cap: see the next test)."""
    big = ("gr_circle", "", {"F.Mask": _disk(20.0, 13.0, 3.0), "B.Mask": _disk(20.0, 13.0, 3.0)})
    result = _run(_run_pieces() + [big])
    assert all(m.owner_run is None for m in _near(result, (20.0, 13.0), radius=0.3))


def test_gr_circle_beside_the_strip_is_not_attributed():
    result = _run(_run_pieces() + [_mid_circle(at=(20.0, 10.9))])
    assert all(m.owner_run is None for m in _near(result, (20.0, 10.9), radius=0.3))


def test_run_track_in_a_different_net_via_opening_stays_an_error():
    """A different-net via on the strip must not hand the run's own copper an owner."""
    pieces = _run_pieces() + [_mid_via(net="OTHER", at=(24.0, 10.0))]
    result = _run(pieces)
    inside = [
        m
        for m in _near(result, (24.0, 10.0), radius=0.5)
        if m.conductor_net == "PWR" and m.mask_side == "F.Mask"
    ]
    assert inside
    assert {m.disposition for m in inside} == {"violation"}
    assert all(m.owner_run is None for m in inside)


def _big_circle_on_the_strip():
    """6 mm mask circle centred ON the strip: only the size cap can refuse it."""
    return ("gr_circle", "", {"F.Mask": _disk(20.0, 10.0, 3.0), "B.Mask": _disk(20.0, 10.0, 3.0)})


def _off_run_track():
    """Same-net copper 2.5 mm off the run, under the big circle."""
    return (
        "segment",
        "PWR",
        {"B.Cu": box(18.0, 12.25, 22.0, 12.75), "F.Cu": box(18.0, 12.25, 22.0, 12.75)},
    )


def test_oversize_gr_circle_centred_on_the_strip_is_not_attributed():
    """The size cap (drill + flash allowance) keeps an oversize circle from excusing copper (#9444)."""
    result = _run(_run_pieces() + [_big_circle_on_the_strip(), _off_run_track()])
    assert not result.passed
    assert all(m.owner_run is None for m in _near(result, (20.0, 12.5), radius=0.5))
    assert any(
        m.disposition == "violation" and m.owner_run is None
        for m in _near(result, (20.0, 12.5), radius=0.5)
    )


def test_gr_circle_is_not_attributed_when_the_record_has_no_usable_gauge():
    """No gauge, no size bound: a mid-run circle is never attributed."""
    result = _run(_run_pieces() + [_mid_circle()], wire_gauge_awg=None)
    mid = _near(result, MID, radius=0.3)
    # The strip's own B.Mask piece stays owned; the circle's openings do not.
    assert any(m.disposition == "violation" and m.owner_run is None for m in mid)
    assert all(m.owner_run is None for m in mid if m.mask_side == "F.Mask")


def test_far_side_strip_opening_does_not_excuse_the_near_side_same_net_copper():
    """A run on B.Cu whose strip carries mask on BOTH sides (#9444 review).

    The far-side (F.Mask) strip opening must not be claimed by the run: the
    doubled same-net F.Cu track under it stays an error with no owner. Pins the
    ``not far`` candidate guard in ``mask_copper``.
    """
    pieces = _run_pieces()
    pieces[1] = (
        "gr_line",
        "",
        {
            "B.Mask": box(A[0], 9.75, B[0], 10.25),
            "F.Mask": box(A[0], 9.75, B[0], 10.25),
        },
    )
    result = _run(pieces + [_mid_via(), _mid_circle()])
    assert not result.passed
    far = [
        m for m in result.measurements if m.mask_side == "F.Mask" and m.disposition == "violation"
    ]
    assert far
    assert any(m.owner_run is None for m in far)


def test_far_side_chain_grows_only_through_strip_pieces():
    """A non-strip board circle on the run's mask side must not extend the chain.

    A same-net via 3.25 mm off the strip sits inside a ``gr_circle`` that overlaps
    the strip on the run's own mask side. Chaining through that circle would
    attribute the same-net ``F.Cu`` track in the via's opening to the run; it
    stands nowhere near the run and stays an error. Pins the ``kind == "gr_line"``
    filter on the far-side chain in ``mask_copper`` (#9444 review).
    """
    off_run = (20.0, 13.5)
    pieces = _run_pieces() + [
        ("gr_circle", "", {"B.Mask": _disk(20.0, 12.5, 2.5)}),
        _mid_via(at=off_run),
        ("segment", "PWR", {"F.Cu": box(19.0, 13.25, 21.0, 13.75)}),
    ]
    result = _run(pieces)
    far = _near(result, off_run, radius=0.6)
    far = [m for m in far if m.mask_side == "F.Mask"]
    assert far
    assert {m.disposition for m in far} == {"violation"}
    assert all(m.owner_run is None for m in far)


# --- The size envelope itself (#9490) ---------------------------------------
#
# The 6 mm circle above is far above any plausible envelope; these pin the
# cap's boundary (sizes derived from the checker's constants, so they track
# it) and its value (pinned to the producer's anchor pad, so it cannot drift
# silently).


def _cap_mm():
    """Mid-run circle cap for the record's gauge (16 AWG)."""
    return anchor_drill_for_awg(16) + 2 * _MID_RUN_CIRCLE_FLASH_MM


def _circle_of_diameter(diameter):
    """``gr_circle`` opening of ``diameter`` mm centred ON the strip at MID."""
    radius = diameter / 2
    return (
        "gr_circle",
        "",
        {"F.Mask": _disk(MID[0], MID[1], radius), "B.Mask": _disk(MID[0], MID[1], radius)},
    )


def _near_run_track():
    """Same-net copper 0.5-0.9 mm off the run, inside an envelope-size circle.

    It sits outside the strip (y > 10.25), so the circle's opening is the only
    thing that can expose -- and, when attributed, excuse -- it.
    """
    return (
        "segment",
        "PWR",
        {"B.Cu": box(18.0, 10.5, 22.0, 10.9), "F.Cu": box(18.0, 10.5, 22.0, 10.9)},
    )


def test_gr_circle_just_under_the_cap_is_attributed():
    cap = _cap_mm()
    result = _run(_run_pieces() + [_circle_of_diameter(cap - 0.02), _near_run_track()])
    assert result.passed
    exposed = _near(result, (20.0, 10.7), radius=0.3)
    assert exposed
    assert {m.mask_side for m in exposed} == {"F.Mask", "B.Mask"}
    assert {m.disposition for m in exposed} == {"within_reinforce_run"}
    assert {m.owner_run for m in exposed} == {"HS_POWER"}


def test_gr_circle_exactly_at_the_cap_is_attributed_on_the_far_side_only():
    """The cap's inclusive edge is the far side; the strip side refuses it (#9490).

    At exactly ``drill + 2 x flash`` the circle attributes where it is the
    whole native opening (F.Mask here, the far side a doubled net is exposed
    on): the refusal is strictly-greater with a ``2 * _CENTRE_EPS_MM`` slack.
    On the strip's own side the opening's material is clipped to ``native``
    (circle union strip), which widens its hull span by the buffer dilation
    -- past that slack -- so the same circle stays unattributed there and the
    copper it exposes keeps its measured verdict.
    """
    result = _run(_run_pieces() + [_circle_of_diameter(_cap_mm()), _near_run_track()])
    exposed = _near(result, (20.0, 10.7), radius=0.3)
    far = [m for m in exposed if m.mask_side == "F.Mask"]
    near = [m for m in exposed if m.mask_side == "B.Mask"]
    assert far
    assert {m.disposition for m in far} == {"within_reinforce_run"}
    assert {m.owner_run for m in far} == {"HS_POWER"}
    assert near
    assert {m.disposition for m in near} == {"violation"}
    assert all(m.owner_run is None for m in near)
    assert not result.passed


def test_gr_circle_just_over_the_cap_is_not_attributed():
    cap = _cap_mm()
    result = _run(_run_pieces() + [_circle_of_diameter(cap + 0.02), _near_run_track()])
    assert not result.passed
    exposed = _near(result, (20.0, 10.7), radius=0.3)
    assert exposed
    assert {m.disposition for m in exposed} == {"violation"}
    assert all(m.owner_run is None for m in exposed)


def test_mid_run_circle_cap_is_one_ring_per_side_above_the_anchor_pad():
    """The envelope's value is pinned to the producer's anchor pad (#9490).

    ``pcb reinforce`` sizes the anchor pad at ``drill + 2 x min_annular_ring``
    (1.916 mm for 16 AWG at the fallback 0.25 mm ring); the checker's cap sits
    one more ring per side above it (2.416 mm). No producer emits mid-run
    circles, so the boundary tests above would silently track a bare bump of
    the flash constant -- this one goes red instead.
    """
    from kicad_tools.pcb.reinforce import _FALLBACK_DESIGN_RULES

    ring = _FALLBACK_DESIGN_RULES.min_annular_ring_mm
    pad = anchor_pad_for_drill(anchor_drill_for_awg(16), ring)
    assert anchor_drill_for_awg(16) + 2 * _MID_RUN_CIRCLE_FLASH_MM == pytest.approx(pad + 2 * ring)
