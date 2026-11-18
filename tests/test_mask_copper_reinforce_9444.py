"""``mask_to_copper``: holes standing on a reinforce strip mid-run (#9444).

After #9419 the anchor-end openings of a recorded run read ``within_reinforce_run``,
but an untented layer-change ``via`` or an anchor-size ``gr_circle`` hole (and its
mask circles) sitting between the anchors had no ``owner_run``: attribution only
reached openings at a confirmed anchor or strip pieces. They are continuous with
the strip, so they belong to the run; copper that is not the run's does not.
"""

from shapely.geometry import box

from kicad_tools.validate.mask_copper import MaskCopperPolicy, assess_attributed_geometry
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
