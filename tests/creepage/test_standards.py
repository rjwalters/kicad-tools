"""Unit tests for the IEC standard-table lookup (Issue #4332, phase 2).

Covers:

* Hand-verified creepage data points across PD1/PD2/PD3 and material groups
  I/II/IIIa (exact expected mm per the encoded IEC 60664-1 Table F.4 /
  IEC 62368-1 Table 17).
* Boundary / step-up behaviour: exactly-on-row returns that row; between rows
  steps UP (never interpolated); above the highest row fails loud.
* Undocumented (voltage, PD, material-group) combinations raise the loud
  ``StandardLookupError`` -- never a silent guessed number.
* Clearance derivation: peak-voltage keying + pollution-degree floor +
  structured provenance.
* Internal consistency (monotonic in voltage, across material groups, and
  across pollution degrees) as a transcription self-check.
* Precedence in ``CreepagePair`` (stricter of manual --min / derived governs,
  and the reported governing bound is correct in both directions).
"""

from __future__ import annotations

import pytest

from kicad_tools.creepage.engine import CreepagePair
from kicad_tools.creepage.standards import (
    MATERIAL_GROUPS,
    RMS_TO_PEAK,
    StandardLookupError,
    get_standard,
    normalize_material_group,
)

STDS = ("iec60664", "iec62368")


# ---------------------------------------------------------------------------
# Hand-verified creepage data points (IEC 60664-1 Table F.4 / 62368-1 Table 17)
# ---------------------------------------------------------------------------

# (working_voltage_rms, pollution_degree, material_group) -> expected mm
_HAND_VERIFIED = {
    # PD2, group I
    (200, 2, "I"): 1.0,
    (250, 2, "I"): 1.25,
    (1000, 2, "I"): 5.0,
    # PD2, group II
    (100, 2, "II"): 1.0,
    (200, 2, "II"): 1.4,
    (1000, 2, "II"): 7.1,
    # PD2, group IIIa
    (100, 2, "IIIa"): 1.4,
    (200, 2, "IIIa"): 2.0,
    (250, 2, "IIIa"): 2.5,
    (1000, 2, "IIIa"): 10.0,
    # PD3, group I / II / IIIa
    (200, 3, "I"): 2.5,
    (200, 3, "II"): 2.8,
    (200, 3, "IIIa"): 3.2,
    (1000, 3, "IIIa"): 16.0,
    # PD1 is material-group independent
    (100, 1, "I"): 0.25,
    (400, 1, "IIIa"): 1.0,
}


@pytest.mark.parametrize("standard_id", STDS)
@pytest.mark.parametrize(("key", "expected"), list(_HAND_VERIFIED.items()))
def test_hand_verified_creepage(standard_id, key, expected):
    voltage, pd, group = key
    std = get_standard(standard_id)
    value, prov = std.required_creepage(voltage, pd, group)
    assert value == pytest.approx(expected)
    assert prov["quantity"] == "creepage"
    assert prov["pollution_degree"] == pd
    assert prov["voltage_row_used_v"] >= voltage


def test_pd1_is_material_group_independent():
    std = get_standard("iec60664")
    for group in ("I", "II", "IIIa"):
        value, prov = std.required_creepage(160, 1, group)
        assert value == pytest.approx(0.32)  # PD1, 160 V row
        assert "PD1" in prov["material_group"]


def test_two_standards_are_harmonised():
    a = get_standard("iec60664")
    b = get_standard("iec62368")
    for pd in (1, 2, 3):
        for group in ("I", "II", "IIIa"):
            if pd == 1 and group != "I":
                continue
            for v in (100, 200, 630, 1000):
                va, _ = a.required_creepage(v, pd, group)
                vb, _ = b.required_creepage(v, pd, group)
                assert va == vb, f"mismatch at {v}V PD{pd} {group}"


# ---------------------------------------------------------------------------
# Boundary / step-up behaviour
# ---------------------------------------------------------------------------


def test_exact_row_returns_that_row():
    std = get_standard("iec60664")
    value, prov = std.required_creepage(200, 2, "IIIa")
    assert prov["voltage_row_used_v"] == 200.0
    assert value == pytest.approx(2.0)


def test_between_rows_steps_up_not_interpolated():
    std = get_standard("iec60664")
    # 201 V sits between the 200 and 250 rows -> must step UP to 250 (2.5 mm),
    # NOT interpolate to ~2.01 mm.
    value, prov = std.required_creepage(201, 2, "IIIa")
    assert prov["voltage_row_used_v"] == 250.0
    assert value == pytest.approx(2.5)


def test_below_lowest_row_steps_up_to_lowest():
    std = get_standard("iec60664")
    value, prov = std.required_creepage(12, 2, "IIIa")
    assert prov["voltage_row_used_v"] == 50.0  # lowest tabulated row
    assert value == pytest.approx(1.2)


def test_above_highest_row_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="exceeds the highest tabulated row"):
        std.required_creepage(1500, 2, "IIIa")


def test_nonpositive_voltage_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError):
        std.required_creepage(0, 2, "IIIa")
    with pytest.raises(StandardLookupError):
        std.required_creepage(-100, 2, "IIIa")


# ---------------------------------------------------------------------------
# Undocumented combinations fail loud (never a silent number)
# ---------------------------------------------------------------------------


def test_pd3_iiib_is_undocumented_and_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="material group"):
        std.required_creepage(200, 3, "IIIb")


def test_unknown_pollution_degree_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="pollution degree"):
        std.required_creepage(200, 4, "IIIa")


def test_unknown_material_group_fails_loud():
    with pytest.raises(StandardLookupError, match="unknown material group"):
        normalize_material_group("IV")


def test_unknown_standard_fails_loud():
    with pytest.raises(StandardLookupError, match="unknown standard"):
        get_standard("iec99999")


def test_material_group_aliases():
    assert normalize_material_group("iiia") == "IIIa"
    assert normalize_material_group("3b") == "IIIb"
    assert normalize_material_group(" II ") == "II"


# ---------------------------------------------------------------------------
# Softstart rev-C envelope (~170 Vpk / PD2 -> ~1.5-2.5 mm)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("standard_id", STDS)
def test_softstart_revc_envelope(standard_id):
    std = get_standard(standard_id)
    value, _ = std.required_creepage(170, 2, "IIIa")
    assert 1.5 <= value <= 2.5, f"{standard_id}: {value} mm outside EE_REVIEW envelope"


# ---------------------------------------------------------------------------
# Clearance derivation + provenance
# ---------------------------------------------------------------------------


def test_clearance_floored_by_pollution_degree():
    std = get_standard("iec60664")
    # 170 Vpk is below the lowest peak row -> the PD floor governs.
    value, prov = std.required_clearance(170 * RMS_TO_PEAK, 2)
    assert value == pytest.approx(0.2)  # PD2 floor
    assert prov["governing_component"] == "pollution-degree floor"
    assert prov["altitude_assumption"] == "<= 2000 m"
    assert prov["quantity"] == "clearance"


def test_clearance_pd3_floor_is_higher():
    std = get_standard("iec60664")
    value, _ = std.required_clearance(200 * RMS_TO_PEAK, 3)
    assert value == pytest.approx(0.8)  # PD3 floor


def test_clearance_high_voltage_uses_table_row():
    std = get_standard("iec60664")
    value, prov = std.required_clearance(5000, 1)
    assert prov["governing_component"] == "peak-voltage table row"
    assert value >= 0.8


def test_clearance_above_highest_peak_row_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError):
        std.required_clearance(20000, 2)


def test_clearance_provenance_carries_full_citation():
    std = get_standard("iec62368")
    _, prov = std.required_clearance(1000, 2)
    for key in (
        "standard",
        "edition",
        "table_id",
        "clause",
        "voltage_axis",
        "pollution_degree",
        "altitude_assumption",
        "disclaimer",
    ):
        assert key in prov


# ---------------------------------------------------------------------------
# Internal consistency (transcription self-check)
# ---------------------------------------------------------------------------


def test_creepage_monotonic_in_voltage():
    std = get_standard("iec60664")
    rows = std.creepage_voltage_rows
    for pd in (1, 2, 3):
        groups = ("I",) if pd == 1 else ("I", "II", "IIIa")
        for group in groups:
            vals = [std.required_creepage(v, pd, group)[0] for v in rows]
            assert vals == sorted(vals), f"non-monotone PD{pd} {group}"


def test_creepage_monotonic_across_material_groups():
    std = get_standard("iec60664")
    for pd in (2, 3):
        for v in std.creepage_voltage_rows:
            i = std.required_creepage(v, pd, "I")[0]
            ii = std.required_creepage(v, pd, "II")[0]
            iiia = std.required_creepage(v, pd, "IIIa")[0]
            assert i <= ii <= iiia, f"non-monotone groups at {v}V PD{pd}"


def test_creepage_monotonic_across_pollution_degrees():
    std = get_standard("iec60664")
    for v in std.creepage_voltage_rows:
        pd1 = std.required_creepage(v, 1, "I")[0]
        pd2 = std.required_creepage(v, 2, "IIIa")[0]
        pd3 = std.required_creepage(v, 3, "IIIa")[0]
        assert pd1 < pd2 < pd3, f"non-monotone PD ladder at {v}V"


def test_all_material_groups_constant_defined():
    assert MATERIAL_GROUPS == ("I", "II", "IIIa", "IIIb")


# ---------------------------------------------------------------------------
# Precedence: stricter of manual --min / derived governs (CreepagePair)
# ---------------------------------------------------------------------------


def _pair(min_mm=None, required=None, creepage=5.0, clearance=5.0, req_clearance=None):
    return CreepagePair(
        net_a="HV",
        net_b="GND",
        kind="conductor",
        layer="F.Cu",
        clearance_mm=clearance,
        creepage_mm=creepage,
        min_mm=min_mm,
        required_creepage_mm=required,
        required_clearance_mm=req_clearance,
    )


def test_precedence_derived_stricter_governs():
    p = _pair(min_mm=1.0, required=3.0, creepage=2.5)
    assert p.governing_creepage_mm == pytest.approx(3.0)
    assert p.governing_bound == "derived"
    assert not p.creepage_passed  # 2.5 < 3.0


def test_precedence_manual_stricter_governs():
    p = _pair(min_mm=4.0, required=2.0, creepage=3.0)
    assert p.governing_creepage_mm == pytest.approx(4.0)
    assert p.governing_bound == "manual (--min)"
    assert not p.creepage_passed  # 3.0 < 4.0


def test_precedence_manual_only():
    p = _pair(min_mm=2.0, required=None, creepage=3.0)
    assert p.governing_creepage_mm == pytest.approx(2.0)
    assert p.governing_bound == "manual (--min)"
    assert p.passed


def test_precedence_derived_only():
    p = _pair(min_mm=None, required=2.0, creepage=3.0)
    assert p.governing_creepage_mm == pytest.approx(2.0)
    assert p.governing_bound == "derived"
    assert p.passed


def test_passed_gates_on_both_creepage_and_clearance():
    # Creepage clears, but clearance does not -> overall FAIL.
    p = _pair(required=2.0, creepage=3.0, clearance=0.1, req_clearance=0.2)
    assert p.creepage_passed
    assert not p.clearance_passed
    assert not p.passed
    # Both clear -> PASS.
    ok = _pair(required=2.0, creepage=3.0, clearance=0.5, req_clearance=0.2)
    assert ok.passed
