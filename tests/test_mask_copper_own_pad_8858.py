"""Per-family verdicts for ``mask_to_copper`` same-net findings (issue #8858).

On boards 00-07 every opening is exactly its pad (``pad_to_mask_clearance 0``),
and the pinned KiCad 10.0.6 plot shows that each "own-pad entry", "own pour"
and "own via" error exposes **no copper beyond the pad's own land**: the
measured area was the conductor's copper lying *under* the pad. Those
measurements are now ``within_own_pad`` (info). Everything the fab reports as
danger, or that the evidence could not classify, keeps its old verdict; each
of those families is pinned here too, so a later change cannot loosen one by
accident. Evidence and the classification table: issue #8858 and
``docs/research/mask-to-copper-own-pad-8858.md``.
"""

import pytest
from shapely.geometry import Point, box

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


def _geometry(conductor, *, margin=0.0, kind="segment", net="N", drill=None, max_error=0.0):
    """One pad (opening = pad grown by *margin*) and one conductor."""
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    opening = PAD.buffer(margin, join_style="mitre") if margin else PAD
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.6",
        {},
        layers={"F.Mask": opening, "F.Cu": unary_union([PAD, conductor])},
    )
    conductor_item = {
        "kind": kind,
        "net": net,
        "layers_geometry": {"F.Cu": conductor},
        "margin_mm": {},
    }
    if drill is not None:
        conductor_item["drill_mm"] = drill
    geometry = AttributedMaskGeometry(
        exported,
        {
            OWNER: {
                "kind": "pad:roundrect",
                "net": "N",
                "pad_ref": "R1/1",
                "layers_geometry": {"F.Mask": opening, "F.Cu": PAD},
                "margin_mm": {"F.Mask": margin},
            },
            OTHER: conductor_item,
        },
    )
    if max_error:
        geometry.provenance["max_error_mm_per_construction"] = max_error
    return geometry


def _only(geometry, intents=()):
    result = assess_attributed_geometry(geometry, POLICY, intents)
    assert result.coverage == "complete"
    assert len(result.measurements) == 1
    return result, result.measurements[0]


def _severities(result):
    return [
        v.severity for v in assessment_results(result).violations if v.rule_id == "mask_to_copper"
    ]


# A track entering its own pad from the right: its copper under the pad is
# the pad's land; with a 0 mm expansion nothing beyond the pad is exposed.
ENTRY = box(0.5, 0.4, 2.0, 0.6)


def test_own_pad_entry_with_zero_expansion_is_within_own_pad_info():
    """Family A (boards 00-07): 5/108/288 errors on boards 01/02/03 before #8858."""
    result, m = _only(_geometry(ENTRY))
    assert m.disposition == "within_own_pad"
    assert m.same_net is True and m.own_pad_entry is True
    # The raw copper-in-opening area is still recorded; the exposure beyond
    # the pad is what is now reported, and it is zero.
    assert m.exposed_area_mm2 == pytest.approx(0.1, abs=1e-9)
    assert m.exposed_beyond_owner_mm2 == pytest.approx(0.0, abs=1e-12)
    assert result.passed
    assert _severities(result) == ["info"]
    message = next(
        v.message for v in assessment_results(result).violations if v.rule_id == "mask_to_copper"
    )
    assert "within_own_pad" in message
    assert "beyond owner pad copper 0 mm²" in message


def test_own_pad_entry_through_a_positive_expansion_stays_an_error():
    """A 0.1 mm expansion exposes 0.02 mm² of track beyond the pad: unchanged.

    The fab's handling of that unavoidable entry corridor is not established
    by the recorded JLCDFM evidence, so it keeps its pre-#8858 verdict.
    """
    result, m = _only(_geometry(ENTRY, margin=0.1))
    assert m.disposition == "violation"
    assert m.exposed_beyond_owner_mm2 == pytest.approx(0.02, abs=1e-9)
    assert not result.passed
    assert _severities(result) == ["error"]


def test_own_pour_under_its_pad_with_zero_expansion_is_within_own_pad():
    """Family B (boards 01/03): a solid-connected same-net pour under the pad."""
    pour = box(-2, -2, 3, 3)
    result, m = _only(_geometry(pour, kind="zone"))
    assert m.disposition == "within_own_pad"
    assert result.passed


def test_own_pour_through_a_positive_expansion_stays_an_error():
    """The pour visible in the expansion ring is exposed copper: unchanged (see #9085)."""
    pour = box(-2, -2, 3, 3)
    result, m = _only(_geometry(pour, kind="zone", margin=0.05))
    assert m.disposition == "violation"
    assert not result.passed


def test_own_via_beside_its_pad_with_drill_under_mask_is_within_own_pad():
    """Family C (boards 01/02/03): via annulus overlapping its pad, drill outside."""
    via = Point(1.25, 0.5).buffer(0.3, quad_segs=64)
    result, m = _only(_geometry(via, kind="via", drill=0.3))
    assert m.disposition == "within_own_pad"
    assert result.passed


@pytest.mark.parametrize(
    "center,drill",
    [
        # Via-in-pad: center (drill) inside the opening.
        ((0.5, 0.5), 0.3),
        # Drill straddles the opening edge: hole partly exposed.
        ((1.1, 0.5), 0.3),
        # Drill unknown: cannot prove the hole is tented.
        ((1.25, 0.5), None),
    ],
)
def test_via_whose_drill_may_be_exposed_is_not_reclassified(center, drill):
    via = Point(*center).buffer(0.3, quad_segs=64)
    result, m = _only(_geometry(via, kind="via", drill=drill))
    assert m.disposition == "violation"
    assert not result.passed


def test_different_net_copper_under_a_pad_is_never_own_land():
    """A different-net conductor overlapping a pad is a short, never the pad's land."""
    result, m = _only(_geometry(ENTRY, net="M"))
    assert m.same_net is False
    assert m.disposition == "violation"
    assert _severities(result) == ["error"]


@pytest.mark.parametrize("gap,expected", [(0.025, "violation"), (0.1, "warning")])
def test_same_net_track_beside_its_pad_keeps_the_fab_danger_band(gap, expected):
    """Families D/E: same-net copper near, not in, a 0-expansion opening.

    JLCDFM reported this shape as danger at 0-0.08 mm on the #8914 board (a
    track leaving its pad and jogging, or running parallel past it), so it
    stays measured at the 0.09 mm interim band. Same shape as #8932's pin,
    with a 0 mm expansion: the verdict there is kept.
    """
    track = box(1 + gap, -0.5, 1 + gap + 0.2, 1.5)
    result, m = _only(_geometry(track))
    assert m.disposition == expected
    assert m.exposed_beyond_owner_mm2 is None


def test_grazing_contact_within_the_construction_budget_is_not_reclassified():
    """Board 02's R1/2 case: overlap too shallow to prove a direct connection."""
    # 2 um deep overlap against a 5 um construction error per side.
    graze = box(0.998, 0.4, 2.0, 0.6)
    result, m = _only(_geometry(graze, max_error=0.005))
    assert m.relation == "overlap"
    assert m.disposition == "violation"
    assert not result.passed


def test_declared_intent_still_reads_intentional():
    """A reviewed MaskEscapeIntent keeps precedence over the automatic class."""
    geometry = _geometry(ENTRY)
    intent = MaskEscapeIntent(source_binding(geometry), OWNER, OTHER, "F.Mask", "reviewed escape")
    result, m = _only(geometry, [intent])
    assert result.intent_audit[0]["accepted"] is True
    assert m.disposition == "intentional"


def test_incomplete_coverage_does_not_reclassify_into_a_pass():
    geometry = _geometry(ENTRY)
    geometry.errors.append("F.Mask: unattributed native mask region")
    result = assess_attributed_geometry(geometry, POLICY)
    assert result.coverage == "incomplete"
    assert not result.passed


def test_within_own_pad_payload_round_trips():
    result, _ = _only(_geometry(ENTRY))
    data = result.to_dict()
    assert data["measurements"][0]["disposition"] == "within_own_pad"
    loaded = MaskCopperAssessment.from_dict(data)
    assert loaded.measurements == result.measurements
    assert loaded.passed


def test_payload_without_the_beyond_field_still_loads():
    result, _ = _only(_geometry(ENTRY, margin=0.1))
    data = result.to_dict()
    del data["measurements"][0]["exposed_beyond_owner_mm2"]
    loaded = MaskCopperAssessment.from_dict(data)
    assert loaded.measurements[0].exposed_beyond_owner_mm2 is None
