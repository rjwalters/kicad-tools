"""Wide-bar and custom-pad extensions of the ``within_own_pad`` class (#9750).

Two same-net shapes a clean layout cannot avoid were measured errors:

- a power bar wider than its own through-hole pad: its copper covers the
  pad's whole perimeter band, so the band area under mask is the pad's own
  perimeter, not a second trace -- the one-crossing allowance
  (``1.45 x width x clearance``) models a track *narrower* than the pad and
  fails a square or large-round pad purely by shape (round 2.4 mm passed
  with 0.70 < 0.72 mm², rect 2.4 and circle 3.6 failed);
- any track entering a custom pad (the stock ``SOT-89-3`` tab): the owner
  kind fell outside the plain-shape tuple, so every connection errored.

Both now read ``within_own_pad`` (info): the band allowance becomes the ring
itself when the copper covers it, and ``pad:custom`` joins the entry class
with conditions 2-4 measured against the pad's real copper outline. The
reproduction board is not committed, so every case below is synthetic
geometry at the issue's scale (issue body: 5.5 mm bars, 2.4/3.6 mm pads,
0.5 mm tab entry).
"""

import pytest
from shapely.geometry import LineString, Point, box

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    assess_attributed_geometry,
    assessment_results,
)

# Interim JLCPCB values written literally (issue #8842), matching the #8858
# suite so the allowance arithmetic there carries over unchanged.
POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)
OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
SEVERITY_RANK = {None: 0, "info": 1, "warning": 2, "error": 3}

RECT_24 = box(-1.2, -1.2, 1.2, 1.2)
CIRCLE_24 = Point(0, 0).buffer(1.2, quad_segs=64)
CIRCLE_36 = Point(0, 0).buffer(1.8, quad_segs=64)
# A custom pad's real copper outline, at SOT-89 tab scale: a rounded
# rectangle standing in for the plotted polygon (the stock footprint itself
# is GPL content and is not copied; the class only sees the outline).
TAB = box(-1.7, -0.95, 1.7, 0.95).buffer(0.2, quad_segs=16)


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
    owner_kind="pad:rect",
    side="F",
    pad=RECT_24,
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
                "pad_ref": "U1/2",
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


# --- the wide bar: copper covers the pad's whole perimeter band -----------------------


@pytest.mark.parametrize(
    "pad,owner_kind",
    [(RECT_24, "pad:rect"), (CIRCLE_24, "pad:circle"), (CIRCLE_36, "pad:circle")],
    ids=["rect-2.4", "circle-2.4", "circle-3.6"],
)
def test_wide_bar_on_own_pad_is_within_own_pad(pad, owner_kind):
    """The issue's three reproductions: a 5.5 mm bar ending at the pad centre.

    Rect 2.4 x 2.4 and circle 3.6 measured errors (their perimeter band
    0.89 / 1.04 mm² exceeded the one-crossing allowance 0.72 mm²) while the
    circle 2.4 passed at 0.70 mm² -- nothing about the layout differed. With
    the band allowance being the covered ring itself, all three agree.
    """
    result, m = _only(_segment((-4.0, 0.0), (0.0, 0.0), 5.5, pad=pad, owner_kind=owner_kind))
    assert m.disposition == "within_own_pad"
    assert m.within_own_pad_failed_condition is None
    assert m.beside_owner_excess_mm2 == pytest.approx(0.0, abs=1e-9)
    assert _severity(result) == "info"
    assert result.passed


def test_wide_bar_exactly_as_wide_as_the_pad_is_within_own_pad():
    """A 2.4 mm bar on the 2.4 mm pad: the ring's far side stays bare, so the
    copper beside the opening is one flush entry strip (0.22 mm²) inside the
    one-crossing budget for a 2.4 mm track (0.31 mm²) -- info on the plain
    allowance already, ring cover not needed."""
    result, m = _only(_segment((-4.0, 0.0), (0.0, 0.0), 2.4, pad=RECT_24))
    assert m.disposition == "within_own_pad"
    assert _severity(result) == "info"


def test_wide_bar_with_positive_margin_still_errors_condition_3():
    """An opening larger than the pad exposes the bar in the expansion ring;
    the wide-bar allowance never excuses copper beyond the pad's own land."""
    result, m = _only(_segment((-4.0, 0.0), (0.0, 0.0), 5.5, pad=RECT_24, margin=0.05))
    assert m.disposition == "violation"
    assert m.within_own_pad_failed_condition == 3
    assert _severity(result) == "error"


def test_wide_bar_passing_through_the_pad_stays_an_error():
    """No authored end inside the pad: not an entry, whatever the width."""
    result, m = _only(_segment((-4.0, 0.0), (4.0, 0.0), 5.5, pad=RECT_24))
    assert m.disposition == "violation"
    assert m.within_own_pad_failed_condition == 4
    assert _severity(result) == "error"
    assert not result.passed


def test_wide_bar_translation_sweep_severity_never_drops_as_beside_copper_rises():
    """The bar's centreline walks off the pad in y.

    Centred (|offset| < half the pad) the authored end is inside and the
    copper covers the ring: info with zero excess. Past the pad edge the end
    leaves the pad, the entry budget is gone and the whole covered ring
    (~0.89 mm²) is excess: error. Severity must be a non-decreasing function
    of the copper under mask beside the opening.
    """
    points = []
    for step in range(0, 26):
        offset = step * 0.1  # 0.0 .. 2.5 mm; the pad edge is at 1.2
        result, m = _only(_segment((-4.0, offset), (0.0, offset), 5.5, pad=RECT_24))
        sev = _severity(result)
        excess = m.beside_owner_excess_mm2 if m and m.beside_owner_excess_mm2 is not None else None
        points.append((offset, sev, excess, m.disposition if m else None))
    # Info exactly when the end is strictly inside: everything through 1.1.
    for offset, sev, _, disp in points:
        if offset <= 1.1:
            assert disp == "within_own_pad" and sev == "info", (offset, disp, sev)
    # Off the pad (end at or past the edge): error with real excess -- the
    # full ring (~0.89 mm²) while the bar still covers it, a shrinking slice
    # of it once the bar's flank uncovers the ring's far side.
    for offset, sev, excess, _ in points:
        if offset >= 1.2:
            assert sev == "error" and excess > 0.1, (offset, sev, excess)
    # Walking off the pad, severity never drops; and any position with real
    # excess is at least as severe as any with zero excess (numerical zero is
    # <= 1e-9 mm², as in the #8858 sweep).
    ranks = [SEVERITY_RANK[s] for _, s, _, _ in points if s]
    assert ranks == sorted(ranks)
    zero = [SEVERITY_RANK[s] for _, s, e, _ in points if s and (e is None or e <= 1e-9)]
    positive = [SEVERITY_RANK[s] for _, s, e, _ in points if s and e is not None and e > 1e-9]
    assert positive and zero
    assert min(positive) >= max(zero)


# --- the custom pad: entry measured against the real copper outline -------------------


def test_straight_entry_into_a_custom_pad_tab_is_within_own_pad():
    """The SOT-89 tab shape: a 0.5 mm track leaving the tab straight.

    Every connection to a custom pad used to fail condition 1 (owner is not
    a plain pad shape); with ``pad:custom`` admitted, conditions 2-4 run
    against the tab's real outline and a clean perpendicular entry is info.
    """
    result, m = _only(_segment((0.0, 0.0), (5.0, 0.0), 0.5, pad=TAB, owner_kind="pad:custom"))
    assert m.disposition == "within_own_pad"
    assert m.within_own_pad_failed_condition is None
    assert m.beside_owner_excess_mm2 == pytest.approx(0.0, abs=1e-9)
    assert _severity(result) == "info"
    assert result.passed


def test_custom_pad_with_copper_exposed_beyond_its_outline_stays_an_error():
    """A positive pad margin opens a ring beyond the tab's copper; the track
    crossing it is exposed beyond the pad's own land (condition 3)."""
    result, m = _only(
        _segment((0.0, 0.0), (5.0, 0.0), 0.5, pad=TAB, owner_kind="pad:custom", margin=0.05)
    )
    assert m.disposition == "violation"
    assert m.within_own_pad_failed_condition == 3
    assert _severity(result) == "error"


def test_custom_pad_without_a_net_still_never_excuses():
    result, m = _only(
        _segment((0.0, 0.0), (5.0, 0.0), 0.5, pad=TAB, owner_kind="pad:custom", owner_net="")
    )
    assert m.same_net is None
    assert m.disposition == "violation"


def test_padstack_and_chamfered_custom_pads_keep_their_measured_verdict():
    """Only the bare ``pad:custom`` kind joins the class; the suffixed kinds
    (padstack, chamfer -- ``mask_export_geometry`` appends them) stay out."""
    for owner_kind in ("pad:custom:padstack", "pad:custom:chamfer"):
        result, m = _only(_segment((0.0, 0.0), (5.0, 0.0), 0.5, pad=TAB, owner_kind=owner_kind))
        assert m.disposition == "violation"
        assert m.within_own_pad_failed_condition == 1


def test_own_pour_thermal_still_rejects_a_custom_pad():
    """The pour-thermal class keeps the plain-shape tuple (#9284 boundary):
    a custom-pad owner never becomes an own-pour downgrade path."""
    from kicad_tools.validate.mask_copper import _WITHIN_OWN_PAD_OWNER_KINDS

    assert "pad:custom" not in _WITHIN_OWN_PAD_OWNER_KINDS
