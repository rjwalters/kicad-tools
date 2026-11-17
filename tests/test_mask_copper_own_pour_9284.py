"""``own_pour_thermal``: a pad tied to its own pour by thermal spokes (issue #9284).

The fab evidence (#8858, JLCDFM on board 01's Gerbers) lists no item for a pad
joined to its own same-net pour by thermal spokes at 0 mm mask expansion, with
the pour body a relief gap (0.2 and 0.3 mm) away. That shape alone reads
``own_pour_thermal`` at info. A solid connection, a pour not connected to the
pad, any pour copper exposed through a positive expansion, a pour body inside
the examined band, and different-net pour all keep their measured verdict.
Evidence: ``docs/research/mask-to-copper-own-pad-8858.md``.
"""

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from kicad_tools.validate.mask_copper import (
    MaskCopperAssessment,
    MaskCopperPolicy,
    MaskEscapeIntent,
    assess_attributed_geometry,
    assessment_results,
    source_binding,
)

# Interim JLCPCB values written literally (#8842), so a future threshold table
# cannot silently move what these tests assert.
POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)
OWNER = "00000000-0000-0000-0000-000000000001"
ZONE = "00000000-0000-0000-0000-000000000002"
ISLAND = "00000000-0000-0000-0000-000000000003"
PAD = box(0, 0, 1, 1)
OUTLINE = box(-3, -3, 4, 4)
SEVERITY_RANK = {None: 0, "info": 1, "warning": 2, "error": 3}
THERMAL, SOLID, NONE, THT_THERMAL, INHERITED = 1, 2, 0, 3, -1


def thermal_fill(gap=0.2, spoke=0.3, spokes=4, pad=PAD, outline=OUTLINE):
    """A pcbnew-shaped thermal relief: body a *gap* away, spokes through the pad.

    pcbnew's fill carries the spokes across the pad itself (board 01's GND
    fill does), so the spoke cross overlaps the pad.
    """
    cx, cy = pad.centroid.x, pad.centroid.y
    body = outline.difference(pad.buffer(gap, join_style="mitre"))
    arms = [
        box(cx - spoke / 2, cy, cx + spoke / 2, outline.bounds[3]),  # up
        box(cx - spoke / 2, outline.bounds[1], cx + spoke / 2, cy),  # down
        box(cx, cy - spoke / 2, outline.bounds[2], cy + spoke / 2),  # right
        box(outline.bounds[0], cy - spoke / 2, cx, cy + spoke / 2),  # left
    ][:spokes]
    reach = pad.buffer(gap + 0.05, join_style="mitre")
    return unary_union([body, *[a.intersection(reach) for a in arms]])


def pad_connection(pad=INHERITED, footprint=INHERITED, pth=False, gap=None, spoke=None):
    return {
        "pad": pad,
        "footprint": footprint,
        "plated_through": pth,
        "thermal_gap_mm": gap,
        "thermal_spoke_width_mm": spoke,
    }


def zone_connection(code=THERMAL, gap=0.2, spoke=0.3):
    return {"pad_connection": code, "thermal_gap_mm": gap, "thermal_spoke_width_mm": spoke}


def _geometry(
    fill,
    *,
    margin=0.0,
    net="GND",
    owner_net="GND",
    owner_kind="pad:roundrect",
    owner_conn="default",
    zone_conn="default",
    island=None,
    side="F",
):
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    mask, cu = f"{side}.Mask", f"{side}.Cu"
    opening = PAD.buffer(margin, join_style="mitre") if margin else PAD
    copper = [PAD, fill] + ([island] if island is not None else [])
    exported = ExportedMaskGeometry(
        "a" * 64, None, None, "10.0.6", {}, layers={mask: opening, cu: unary_union(copper)}
    )
    objects = {
        OWNER: {
            "kind": owner_kind,
            "net": owner_net,
            "pad_ref": "R2/2",
            "layers_geometry": {mask: opening, cu: PAD},
            "margin_mm": {mask: margin},
            "zone_connection": pad_connection() if owner_conn == "default" else owner_conn,
        },
        ZONE: {
            "kind": "zone",
            "net": net,
            "layers_geometry": {cu: fill},
            "margin_mm": {},
            "zone_connection": zone_connection() if zone_conn == "default" else zone_conn,
        },
    }
    if island is not None:
        objects[ISLAND] = {
            "kind": "zone",
            "net": net,
            "layers_geometry": {cu: island},
            "margin_mm": {},
            "zone_connection": zone_connection(),
        }
    return AttributedMaskGeometry(exported, objects)


def _measure(geometry, conductor=ZONE, intents=()):
    result = assess_attributed_geometry(geometry, POLICY, intents)
    found = [m for m in result.measurements if m.conductor_uuid == conductor]
    assert len(found) <= 1
    return result, (found[0] if found else None)


def _severity(result, conductor=ZONE):
    found = [
        v.severity
        for v in assessment_results(result).violations
        if v.rule_id == "mask_to_copper" and conductor in v.items
    ]
    return found[0] if found else None


# --- the excused shape: thermal spokes, body beyond the examined band ------------------


@pytest.mark.parametrize("gap,spoke", [(0.2, 0.3), (0.3, 0.4)])
def test_spoke_connected_own_pour_is_info(gap, spoke):
    """Board 01's two tested relief gaps: R2.2 (0.2/0.3) and J1.2, J2.2 (0.3/0.4)."""
    geometry = _geometry(
        thermal_fill(gap, spoke),
        owner_conn=pad_connection(gap=gap, spoke=spoke) if gap == 0.2 else "default",
        zone_conn=zone_connection(gap=0.3, spoke=0.4),
    )
    result, m = _measure(geometry)
    assert m.disposition == "own_pour_thermal"
    assert m.pour_spokes == 4
    assert m.pour_relief_gap_mm == pytest.approx(gap)
    assert m.pour_spoke_width_mm == pytest.approx(spoke)
    assert m.beside_owner_excess_mm2 == pytest.approx(0.0, abs=1e-9)
    assert result.passed
    assert _severity(result) == "info"
    message = next(v.message for v in assessment_results(result).violations if ZONE in v.items)
    assert "own_pour_thermal" in message
    assert "4 thermal spokes" in message
    assert "within_own_pad" not in message


def test_two_spokes_also_qualify():
    result, m = _measure(_geometry(thermal_fill(spokes=2)))
    assert m.disposition == "own_pour_thermal"
    assert m.pour_spokes == 2


def test_back_side_is_classified_like_the_front():
    result, m = _measure(_geometry(thermal_fill(), side="B"))
    assert m.mask_side == "B.Mask"
    assert m.disposition == "own_pour_thermal"


@pytest.mark.parametrize(
    "owner_conn,zone_conn,pth",
    [
        # Footprint-level thermal override, zone solid by default.
        (pad_connection(footprint=THERMAL), zone_connection(code=SOLID), False),
        # Pad thermal wins over a solid footprint.
        (pad_connection(pad=THERMAL, footprint=SOLID), zone_connection(), False),
        # Thermal for through-hole pads, on a plated through-hole pad.
        (pad_connection(pth=True), zone_connection(code=THT_THERMAL), True),
    ],
)
def test_declared_thermal_precedence(owner_conn, zone_conn, pth):
    result, m = _measure(_geometry(thermal_fill(), owner_conn=owner_conn, zone_conn=zone_conn))
    assert m.disposition == "own_pour_thermal"


# --- everything the evidence does not cover keeps its verdict --------------------------


def test_solid_connected_own_pour_stays_an_error():
    """A solid pad-to-pour connection is untested by the fab: error."""
    fill = box(-3, -3, 4, 4)
    result, m = _measure(_geometry(fill, owner_conn=pad_connection(pad=SOLID)))
    assert m.disposition == "violation"
    assert m.pour_spokes is None
    assert _severity(result) == "error"
    assert not result.passed


def test_solid_geometry_declared_thermal_stays_an_error():
    """The declaration alone never excuses: the fill must show the spokes."""
    result, m = _measure(_geometry(box(-3, -3, 4, 4)))
    assert m.disposition == "violation"
    assert m.beside_owner_excess_mm2 > 0


@pytest.mark.parametrize(
    "owner_conn,zone_conn",
    [
        (pad_connection(pad=SOLID), zone_connection()),
        (pad_connection(footprint=SOLID), zone_connection()),
        (pad_connection(), zone_connection(code=SOLID)),
        (pad_connection(), zone_connection(code=NONE)),
        # Thermal-for-through-hole on an SMD pad is not a thermal declaration.
        (pad_connection(pth=False), zone_connection(code=THT_THERMAL)),
        # No declared connection data at all (older worker output): fail closed.
        (None, zone_connection()),
        (pad_connection(), None),
    ],
)
def test_spoke_geometry_without_a_thermal_declaration_stays_an_error(owner_conn, zone_conn):
    result, m = _measure(_geometry(thermal_fill(), owner_conn=owner_conn, zone_conn=zone_conn))
    assert m.disposition == "violation"
    assert _severity(result) == "error"


def test_unconnected_same_net_pour_beside_the_opening_stays_an_error():
    """A same-net pour that does not touch the pad (island, other zone)."""
    island = box(-3, -3, 4, 4).difference(PAD.buffer(0.05, join_style="mitre"))
    result, m = _measure(_geometry(island))
    assert m.disposition == "violation"
    assert m.relation == "disjoint"
    assert _severity(result) == "error"


def test_spoke_connected_pour_with_an_island_in_the_band_stays_an_error():
    """The same zone, spoke-connected, but with extra copper beside the opening."""
    fill = unary_union([thermal_fill(), box(0.1, 1.03, 0.3, 1.2)])
    result, m = _measure(_geometry(fill))
    assert m.disposition == "violation"
    assert m.beside_owner_excess_mm2 > 0


def test_second_same_net_zone_beside_the_opening_stays_an_error():
    """A separate same-net zone in the band keeps its own verdict."""
    island = box(0.1, 1.04, 0.3, 1.3)
    result, m = _measure(_geometry(thermal_fill(), island=island), conductor=ISLAND)
    assert m.disposition == "violation"
    assert _severity(result, ISLAND) == "error"
    assert not result.passed


@pytest.mark.parametrize("margin", [0.015, 0.05, 0.1])
def test_positive_expansion_with_spokes_in_the_ring_stays_an_error(margin):
    result, m = _measure(_geometry(thermal_fill(), margin=margin))
    assert m.disposition == "violation"
    assert m.exposed_beyond_owner_mm2 > 0
    assert _severity(result) == "error"


def test_different_net_pour_is_unchanged():
    result, m = _measure(_geometry(thermal_fill(), net="VOUT"))
    assert m.same_net is False
    assert m.disposition == "violation"
    assert _severity(result) == "error"


def test_different_net_pour_in_the_warning_band_stays_a_warning():
    """Board 01 R2.1: a different-net pour 0.1275 mm from the opening (fab: warning)."""
    pour = box(-3, -3, 4, 4).difference(PAD.buffer(0.1275, join_style="mitre"))
    result, m = _measure(_geometry(pour, net="VOUT"))
    assert m.disposition == "warning"
    assert m.clearance_mm == pytest.approx(0.1275, abs=1e-6)
    assert _severity(result) == "warning"


@pytest.mark.parametrize("gap", [0.149, 0.14, 0.12, 0.1, 0.09, 0.05])
def test_pour_body_inside_the_examined_band_stays_an_error(gap):
    """Untested: the fab result covers relief gaps of 0.2 and 0.3 mm only.

    A body just inside the band adds little area to each spoke piece, so this
    pins that it is caught by its contact along the band's outer edge.
    """
    result, m = _measure(_geometry(thermal_fill(gap=gap)))
    assert m.disposition == "violation"
    assert m.beside_owner_excess_mm2 > 0
    assert _severity(result) == "error"


def test_wider_spoke_than_declared_stays_an_error():
    result, m = _measure(_geometry(thermal_fill(spoke=0.6), zone_conn=zone_connection(spoke=0.3)))
    assert m.disposition == "violation"


def test_more_than_four_spokes_is_not_a_thermal_relief():
    fill = unary_union(
        [thermal_fill()]
        + [box(x, -0.3, x + 0.05, 1.3).difference(PAD) for x in (0.1, 0.25, 0.75, 0.85)]
    )
    result, m = _measure(_geometry(fill))
    assert m.disposition == "violation"


@pytest.mark.parametrize("owner_kind", ["pad:custom", "pad:chamfered_rect", "pad:padstack"])
def test_unverified_pad_shapes_fail_closed(owner_kind):
    result, m = _measure(_geometry(thermal_fill(), owner_kind=owner_kind))
    assert m.disposition == "violation"


def test_solder_mask_defined_pad_fails_closed():
    result, m = _measure(_geometry(thermal_fill(), margin=-0.05))
    assert m.disposition == "violation"


# --- monotone severity -------------------------------------------------------------------


def test_relief_gap_sweep_severity_never_drops_as_the_body_closes_in():
    """Pour body moved from 0.30 mm to touching the pad, spokes unchanged.

    Info only while the body is beyond the whole examined band (0.15 mm);
    from there inward it is an error, never a warning or info again.
    """
    observed = []
    for step in range(31):
        gap = round(0.30 - step * 0.01, 2)
        result, m = _measure(_geometry(thermal_fill(gap=gap)))
        observed.append((gap, m.disposition, _severity(result)))
    ranks = [SEVERITY_RANK[s] for _, _, s in observed]
    assert ranks == sorted(ranks), observed
    for gap, disposition, severity in observed:
        if gap >= 0.16:
            assert (disposition, severity) == ("own_pour_thermal", "info"), (gap, observed)
        if gap <= 0.15:
            assert severity == "error", (gap, observed)


def test_spoke_width_sweep_severity_never_drops_as_spokes_widen():
    """Actual spokes widened past the declared 0.3 mm: info, then error for good."""
    observed = []
    for step in range(13):
        actual = round(0.20 + step * 0.05, 2)
        result, m = _measure(_geometry(thermal_fill(spoke=actual)))
        observed.append((actual, _severity(result)))
    ranks = [SEVERITY_RANK[s] for _, s in observed]
    assert ranks == sorted(ranks), observed
    assert dict(observed)[0.3] == "info"
    assert dict(observed)[0.5] == "error"


def test_expansion_sweep_severity_never_drops_as_the_ring_widens():
    observed = []
    for margin in (0.0, 0.01, 0.02, 0.05, 0.1, 0.2):
        result, m = _measure(_geometry(thermal_fill(), margin=margin))
        observed.append((margin, _severity(result)))
    ranks = [SEVERITY_RANK[s] for _, s in observed]
    assert ranks == sorted(ranks), observed
    assert observed[0][1] == "info"
    assert all(s == "error" for _, s in observed[1:])


# --- bookkeeping ----------------------------------------------------------------------


def test_declared_intent_still_reads_intentional():
    geometry = _geometry(box(-3, -3, 4, 4), owner_conn=pad_connection(pad=SOLID))
    intent = MaskEscapeIntent(source_binding(geometry), OWNER, ZONE, "F.Mask", "reviewed pour")
    result, m = _measure(geometry, intents=[intent])
    assert m.disposition == "intentional"


def test_incomplete_coverage_never_produces_an_error_or_a_pass():
    geometry = _geometry(thermal_fill())
    geometry.errors.append("F.Mask: unattributed native mask region")
    result = assess_attributed_geometry(geometry, POLICY)
    assert result.coverage == "incomplete"
    assert not result.passed


def test_own_pour_thermal_payload_round_trips():
    result, _ = _measure(_geometry(thermal_fill()))
    data = result.to_dict()
    entry = next(m for m in data["measurements"] if m["conductor_uuid"] == ZONE)
    assert entry["disposition"] == "own_pour_thermal"
    assert entry["pour_spokes"] == 4
    loaded = MaskCopperAssessment.from_dict(data)
    assert loaded.measurements == result.measurements
    assert loaded.passed


def test_payload_without_the_pour_fields_still_loads():
    result, _ = _measure(_geometry(thermal_fill()))
    data = result.to_dict()
    for entry in data["measurements"]:
        for key in ("pour_spokes", "pour_relief_gap_mm", "pour_spoke_width_mm"):
            del entry[key]
    loaded = MaskCopperAssessment.from_dict(data)
    assert all(m.pour_spokes is None for m in loaded.measurements)
