"""Per-family verdicts for ``mask_to_copper`` same-net findings (issue #8858).

The ``within_own_pad`` class (info) is granted for what the copper *does*, not
merely for touching the pad: a straight track that enters its own pad once,
at 45 degrees or steeper, with nothing exposed beyond the pad and nothing
under mask beside the opening beyond that one crossing. Everything the fab
reported as danger, and every shape the evidence does not decide, keeps its
measured verdict and is pinned here. Evidence and the classification table:
issue #8858 and ``docs/research/mask-to-copper-own-pad-8858.md``.
"""

import math

import pytest
from shapely.geometry import LineString, Point, box

from kicad_tools.validate.mask_copper import (
    MaskCopperAssessment,
    MaskCopperPolicy,
    MaskEscapeIntent,
    assess_attributed_geometry,
    assessment_results,
    source_binding,
)

# Interim JLCPCB values written literally (issue #8842), so a future threshold
# table cannot silently move what these tests assert.
POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)
OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
PAD = box(0, 0, 1, 1)
SEVERITY_RANK = {None: 0, "info": 1, "warning": 2, "error": 3}


def track(a, b, width=0.2):
    """A straight segment: native-like capsule copper plus its authored source."""
    copper = LineString([a, b]).buffer(width / 2, quad_segs=64)
    return copper, {"width_mm": width, "endpoints_mm": (tuple(a), tuple(b))}


def _geometry(
    conductor,
    *,
    margin=0.0,
    kind="segment",
    net="N",
    owner_net="N",
    owner_kind="pad:roundrect",
    side="F",
    pad=PAD,
    extra=None,
    max_error=0.0,
):
    """One pad (opening = pad grown by *margin*) and one conductor on *side*."""
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    mask, cu = f"{side}.Mask", f"{side}.Cu"
    opening = pad.buffer(margin, join_style="mitre") if margin else pad
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.6",
        {},
        layers={mask: opening, cu: unary_union([pad, conductor])},
    )
    conductor_item = {
        "kind": kind,
        "net": net,
        "layers_geometry": {cu: conductor},
        "margin_mm": {},
        **(extra or {}),
    }
    geometry = AttributedMaskGeometry(
        exported,
        {
            OWNER: {
                "kind": owner_kind,
                "net": owner_net,
                "pad_ref": "R1/1",
                "layers_geometry": {mask: opening, cu: pad},
                "margin_mm": {mask: margin},
            },
            OTHER: conductor_item,
        },
    )
    if max_error:
        geometry.provenance["max_error_mm_per_construction"] = max_error
    return geometry


def _segment(a, b, width=0.2, **kw):
    copper, extra = track(a, b, width)
    return _geometry(copper, extra=extra, **kw)


def _only(geometry, intents=()):
    result = assess_attributed_geometry(geometry, POLICY, intents)
    assert result.coverage == "complete"
    found = [m for m in result.measurements if m.conductor_uuid == OTHER]
    assert len(found) <= 1
    return result, (found[0] if found else None)


def _severity(result):
    found = [
        v.severity
        for v in assessment_results(result).violations
        if v.rule_id == "mask_to_copper" and OTHER in v.items
    ]
    return found[0] if found else None


# --- the excused family -------------------------------------------------------------


def test_perpendicular_entry_at_zero_expansion_is_within_own_pad_info():
    """Family A, perpendicular: the shape JLCDFM did not flag (#8914, consumer)."""
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5)))
    assert m.disposition == "within_own_pad"
    assert m.exposed_beyond_owner_mm2 == pytest.approx(0.0, abs=1e-12)
    assert m.beside_owner_excess_mm2 == pytest.approx(0.0, abs=1e-9)
    assert result.passed
    assert _severity(result) == "info"
    message = next(v.message for v in assessment_results(result).violations if OTHER in v.items)
    assert "within_own_pad" in message
    # Issue #9269: the deciding beside-pad excess is printed, so a reader can
    # see why a non-zero "beyond owner pad copper" is excused.
    assert "beside owner excess" in message


def test_message_omits_beside_owner_excess_when_not_evaluated():
    """Issue #9269: no deciding value was measured, so none is printed."""
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), net="M"))
    assert m.disposition == "violation"
    assert m.beside_owner_excess_mm2 is None
    message = next(v.message for v in assessment_results(result).violations if OTHER in v.items)
    assert "beside owner excess" not in message


def test_45_degree_entry_is_within_own_pad():
    result, m = _only(_segment((0.6, 0.4), (1.6, 1.4)))
    assert m.disposition == "within_own_pad"
    assert result.passed


def test_track_lying_wholly_on_its_pad_is_within_own_pad():
    result, m = _only(_segment((0.3, 0.5), (0.7, 0.5)))
    assert m.disposition == "within_own_pad"


def test_back_side_entry_is_classified_like_the_front():
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), side="B"))
    assert m.mask_side == "B.Mask"
    assert m.disposition == "within_own_pad"


def test_sub_budget_ring_between_plot_and_polygon_is_tolerated_once():
    """A ring narrower than u (the consumer's <5 um circle-vs-polygon ring)."""
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), margin=0.004, max_error=0.005))
    assert m.disposition == "within_own_pad"


# --- review blocker 1: copper beside the pad keeps its verdict ------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        # Parallel twin of family D, moved 0.1 mm closer: overlaps the top edge
        # by 0.05 mm along the whole pad (fab #8914 items 1-2 shape).
        ((-1.0, 1.05), (2.0, 1.05)),
        ((-1.0, 1.08), (2.0, 1.08)),
        # Exit that runs along the top edge (endpoint outside the pad).
        ((0.5, 1.05), (3.0, 1.05)),
        # Exit from inside the pad, lying along the edge (straddles it).
        ((0.5, 0.95), (3.0, 0.95)),
        # Shallow 5-degree exit from inside the pad.
        ((0.5, 0.9), (0.5 + 3 * math.cos(math.radians(5)), 0.9 + 3 * math.sin(math.radians(5)))),
        # Straight pass-through: no end on the pad; the evidence does not decide it.
        ((-1.0, 0.5), (2.0, 0.5)),
    ],
)
def test_copper_beside_or_through_the_pad_stays_an_error(a, b):
    result, m = _only(_segment(a, b))
    assert m.disposition in ("violation", "uncertain")
    assert _severity(result) == "error"
    assert not result.passed


def test_translation_sweep_severity_never_drops_as_beside_copper_rises():
    """A short track moved from beside the pad, through straddling, onto it.

    The fab-danger measure is copper under mask beside the opening beyond one
    clean crossing. Severity must be a non-decreasing function of it.
    """
    points = []
    for step in range(0, 31):
        y = 1.2 - step * 0.02  # centreline from 0.2 mm above the edge to 0.6 mm inside
        geometry = _segment((0.3, y), (0.7, y))
        result, m = _only(geometry)
        sev = _severity(result)
        excess = m.beside_owner_excess_mm2 if m and m.beside_owner_excess_mm2 is not None else None
        points.append((y, sev, excess, m.disposition if m else None))
    # Info exactly when the track lies on the pad with nothing beside it.
    for y, sev, excess, disp in points:
        if disp == "within_own_pad":
            assert excess == pytest.approx(0.0, abs=1e-9)
            assert y + 0.1 <= 1.0 + 1e-9
    # Straddling and beside positions are errors (or out of reach).
    for y, sev, _, _ in points:
        if 1.0 - 0.1 < y < 1.0 + 0.1 + 0.09:
            assert sev == "error", (y, sev)
    # Ordering: among positions that produce a finding, any position with
    # positive excess is at least as severe as any with zero excess.
    zero = [SEVERITY_RANK[s] for _, s, e, _ in points if s and e == 0]
    positive = [SEVERITY_RANK[s] for _, s, e, _ in points if s and e and e > 1e-9]
    assert positive and zero
    assert min(positive) >= max(zero)


def test_rotation_sweep_severity_never_drops_toward_parallel():
    """An exit pivoted at the edge from perpendicular toward parallel."""
    tall = box(0, -2, 1, 3)
    previous = 0
    verdicts = []
    for degrees in range(0, 86, 5):
        phi = math.radians(degrees)  # 0 = perpendicular to the right edge
        a = (1 - 0.3 * math.cos(phi), 0.5 - 0.3 * math.sin(phi))
        b = (1 + 2 * math.cos(phi), 0.5 + 2 * math.sin(phi))
        result, m = _only(_segment(a, b, pad=tall))
        rank = SEVERITY_RANK[_severity(result)]
        assert rank >= previous, (degrees, _severity(result))
        previous = rank
        verdicts.append((degrees, m.disposition))
    # Perpendicular through 45 degrees from the normal: entries; beyond ~46: errors.
    assert all(d == "within_own_pad" for deg, d in verdicts if deg <= 45)
    assert all(d != "within_own_pad" for deg, d in verdicts if deg >= 50)


# --- review blocker 2: an expansion ring wider than u is never excused ----------------


@pytest.mark.parametrize("margin", [0.015, 0.03, 0.05, 0.1, 0.5])
def test_entry_through_a_positive_expansion_stays_an_error(margin):
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), margin=margin, max_error=0.005))
    assert m.disposition == "violation"
    assert m.exposed_beyond_owner_mm2 > 0
    assert _severity(result) == "error"


# --- fail-closed families ---------------------------------------------------------------


def test_own_pour_around_its_pad_stays_an_error():
    """Family B: pour copper under mask all around the opening (no fab evidence)."""
    result, m = _only(_geometry(box(-2, -2, 3, 3), kind="zone"))
    assert m.disposition == "violation"
    assert m.beside_owner_excess_mm2 > 0


def test_pour_inside_an_enlarged_opening_stays_an_error():
    result, m = _only(_geometry(box(-2, -2, 3, 3), kind="zone", margin=0.05))
    assert m.disposition == "violation"


@pytest.mark.parametrize(
    "center,drill",
    [
        ((1.25, 0.5), 0.3),  # family C: ring overlaps the pad, drill under mask
        ((1.28, 0.5), 0.3),
        ((0.5, 0.5), 0.3),  # via-in-pad
        ((1.1, 0.5), 0.3),  # drill straddles the opening edge
        ((1.25, 0.5), None),  # drill unknown
    ],
)
def test_vias_touching_their_pad_stay_errors(center, drill):
    """C cannot be separated from E (via just outside): both stay errors."""
    via = Point(*center).buffer(0.3, quad_segs=64)
    extra = {"drill_mm": drill} if drill else {}
    result, m = _only(_geometry(via, kind="via", extra=extra))
    assert m.disposition == "violation"
    assert _severity(result) == "error"


def test_via_just_outside_its_pad_is_an_error_too():
    via = Point(1.32, 0.5).buffer(0.3, quad_segs=64)
    result, m = _only(_geometry(via, kind="via", extra={"drill_mm": 0.3}))
    assert _severity(result) == "error"


@pytest.mark.parametrize(
    "owner_kind", ["pad:custom", "pad:rect:padstack", "pad:roundrect:chamfer", "pad:trapezoid"]
)
def test_unverified_pad_shapes_fail_closed(owner_kind):
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), owner_kind=owner_kind))
    assert m.disposition == "violation"


def test_solder_mask_defined_pad_fails_closed():
    """Opening smaller than the pad (negative margin)."""
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), margin=-0.05))
    assert m.disposition == "violation"


@pytest.mark.parametrize("owner_net", [""])
def test_pad_without_a_net_never_excuses(owner_net):
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), owner_net=owner_net))
    assert m.same_net is None
    assert m.disposition == "violation"


def test_different_net_copper_on_a_pad_stays_an_error():
    """Also the net-tie case: two nets meeting at a pad are never own land."""
    result, m = _only(_segment((0.5, 0.5), (2.0, 0.5), net="M"))
    assert m.same_net is False
    assert m.disposition == "violation"


def test_segment_without_authored_width_or_ends_fails_closed():
    copper, _ = track((0.5, 0.5), (2.0, 0.5))
    result, m = _only(_geometry(copper))
    assert m.disposition == "violation"


def test_arc_fails_closed():
    copper, extra = track((0.5, 0.5), (2.0, 0.5))
    result, m = _only(_geometry(copper, kind="arc", extra=extra))
    assert m.disposition == "violation"


def test_npth_pad_without_copper_never_excuses():
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    copper, extra = track((0.5, 0.5), (2.0, 0.5))
    exported = ExportedMaskGeometry(
        "a" * 64, None, None, "10.0.6", {}, layers={"F.Mask": PAD, "F.Cu": copper}
    )
    geometry = AttributedMaskGeometry(
        exported,
        {
            OWNER: {
                "kind": "pad:circle",
                "net": "",
                "layers_geometry": {"F.Mask": PAD},
                "margin_mm": {"F.Mask": 0.0},
            },
            OTHER: {
                "kind": "segment",
                "net": "N",
                "layers_geometry": {"F.Cu": copper},
                "margin_mm": {},
                **extra,
            },
        },
    )
    result, m = _only(geometry)
    assert m.disposition == "violation"


@pytest.mark.parametrize("gap,expected", [(0.025, "violation"), (0.1, "warning")])
def test_same_net_track_beside_its_pad_keeps_the_fab_danger_band(gap, expected):
    """Families D/E. Same shape as #8932's pin, at 0 mm expansion: kept."""
    result, m = _only(_segment((1 + gap + 0.1, -0.5), (1 + gap + 0.1, 1.5)))
    assert m.disposition == expected


def test_grazing_contact_within_the_construction_budget_is_not_reclassified():
    result, m = _only(_segment((1.098, 0.5), (2.0, 0.5), max_error=0.005))
    assert m.relation == "overlap"
    assert m.disposition == "violation"


# --- bookkeeping ----------------------------------------------------------------------


def test_declared_intent_still_reads_intentional():
    geometry = _segment((0.5, 0.5), (2.0, 0.5))
    intent = MaskEscapeIntent(source_binding(geometry), OWNER, OTHER, "F.Mask", "reviewed escape")
    result, m = _only(geometry, [intent])
    assert result.intent_audit[0]["accepted"] is True
    assert m.disposition == "intentional"


def test_incomplete_coverage_does_not_reclassify_into_a_pass():
    geometry = _segment((0.5, 0.5), (2.0, 0.5))
    geometry.errors.append("F.Mask: unattributed native mask region")
    result = assess_attributed_geometry(geometry, POLICY)
    assert result.coverage == "incomplete"
    assert not result.passed


def test_within_own_pad_payload_round_trips():
    result, _ = _only(_segment((0.5, 0.5), (2.0, 0.5)))
    data = result.to_dict()
    assert data["measurements"][0]["disposition"] == "within_own_pad"
    loaded = MaskCopperAssessment.from_dict(data)
    assert loaded.measurements == result.measurements
    assert loaded.passed


def test_payload_without_the_new_fields_still_loads():
    result, _ = _only(_segment((0.5, 0.5), (2.0, 0.5), margin=0.1))
    data = result.to_dict()
    for key in ("exposed_beyond_owner_mm2", "beside_owner_excess_mm2"):
        del data["measurements"][0][key]
    loaded = MaskCopperAssessment.from_dict(data)
    assert loaded.measurements[0].exposed_beyond_owner_mm2 is None
    assert loaded.measurements[0].beside_owner_excess_mm2 is None
