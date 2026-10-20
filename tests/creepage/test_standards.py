"""Unit tests for the IEC standard-table lookup (Issue #4332, phase 2).

Covers:

* Hand-verified creepage data points across PD1/PD2/PD3 and material groups
  I/II/IIIa (exact expected mm per the encoded IEC 60664-1 Table F.5 /
  IEC 62368-1 Table 17).
* Boundary / step-up behaviour: exactly-on-row returns that row; between rows
  steps UP (never interpolated); above the highest row fails loud.
* Undocumented (voltage, PD, material-group) combinations raise the loud
  ``StandardLookupError`` -- never a silent guessed number.
* Clearance derivation (issue #7057): Table F.1 rated impulse voltage ->
  Table F.2, Table F.8 steady-state peak, pollution-degree floor, and
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
    _CREEPAGE_VOLTAGE_ROWS,
    _IMPULSE_PREFERRED_SERIES,
    _IMPULSE_WITHSTAND_V,
    DEFAULT_INSULATION,
    INSULATION_TYPES,
    MATERIAL_GROUPS,
    RMS_TO_PEAK,
    StandardLookupError,
    get_standard,
    normalize_material_group,
    reinforced_impulse_voltage,
)

STDS = ("iec60664", "iec62368")


# ---------------------------------------------------------------------------
# Hand-verified creepage data points (IEC 60664-1 Table F.5 / 62368-1 Table 17)
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
    value, prov = std.required_creepage(voltage, pd, group, insulation="basic")
    assert value == pytest.approx(expected)
    assert prov["quantity"] == "creepage"
    assert prov["pollution_degree"] == pd
    assert prov["voltage_row_used_v"] >= voltage


def test_pd1_is_material_group_independent():
    std = get_standard("iec60664")
    for group in ("I", "II", "IIIa"):
        value, prov = std.required_creepage(160, 1, group, insulation="basic")
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
                va, _ = a.required_creepage(v, pd, group, insulation="basic")
                vb, _ = b.required_creepage(v, pd, group, insulation="basic")
                assert va == vb, f"mismatch at {v}V PD{pd} {group}"


# ---------------------------------------------------------------------------
# Boundary / step-up behaviour
# ---------------------------------------------------------------------------


def test_exact_row_returns_that_row():
    std = get_standard("iec60664")
    value, prov = std.required_creepage(200, 2, "IIIa", insulation="basic")
    assert prov["voltage_row_used_v"] == 200.0
    assert value == pytest.approx(2.0)


def test_between_rows_steps_up_not_interpolated():
    std = get_standard("iec60664")
    # 201 V sits between the 200 and 250 rows -> must step UP to 250 (2.5 mm),
    # NOT interpolate to ~2.01 mm.
    value, prov = std.required_creepage(201, 2, "IIIa", insulation="basic")
    assert prov["voltage_row_used_v"] == 250.0
    assert value == pytest.approx(2.5)


def test_below_lowest_row_steps_up_to_lowest():
    std = get_standard("iec60664")
    # 12 V sits between the 10 and 12.5 V rows -> steps UP to the 12.5 V row.
    value, prov = std.required_creepage(12, 2, "IIIa", insulation="basic")
    assert prov["voltage_row_used_v"] == 12.5
    assert value == pytest.approx(0.42)


def test_below_lowest_tabulated_row_steps_up_to_10v():
    """Working voltages below the F.4 floor (10 V) step up to the 10 V row.

    The reported ΔV = 1.65 V regression (issue #4402): before the sub-50 V
    rows were added this stepped up to the 50 V row (1.2 mm) and no dense-board
    pad gap could satisfy it.  It now resolves to the 10 V row -> 0.40 mm.
    """
    std = get_standard("iec60664")
    for v in (1.65, 5.0, 9.9):
        value, prov = std.required_creepage(v, 2, "IIIa", insulation="basic")
        assert prov["voltage_row_used_v"] == 10.0, f"{v} V did not step up to 10 V row"
        assert value == pytest.approx(0.40)


def test_sub_50v_rows_exact_boundaries():
    """Exact-row and just-above-boundary step-up across the new sub-50 V rows."""
    std = get_standard("iec60664")
    # Exact-row hits.
    assert std.required_creepage(10, 2, "IIIa", insulation="basic")[0] == pytest.approx(0.40)
    assert std.required_creepage(12.5, 2, "IIIa", insulation="basic")[0] == pytest.approx(0.42)
    assert std.required_creepage(16, 2, "IIIa", insulation="basic")[0] == pytest.approx(0.45)
    assert std.required_creepage(20, 2, "IIIa", insulation="basic")[0] == pytest.approx(0.48)
    assert std.required_creepage(25, 2, "IIIa", insulation="basic")[0] == pytest.approx(0.50)
    assert std.required_creepage(32, 2, "IIIa", insulation="basic")[0] == pytest.approx(0.53)
    # 40 V is where the material groups first diverge.
    assert std.required_creepage(40, 2, "I", insulation="basic")[0] == pytest.approx(0.56)
    assert std.required_creepage(40, 2, "II", insulation="basic")[0] == pytest.approx(0.80)
    assert std.required_creepage(40, 2, "IIIa", insulation="basic")[0] == pytest.approx(1.10)
    assert std.required_creepage(40, 3, "IIIa", insulation="basic")[0] == pytest.approx(1.80)
    # Just above the 10 V row -> steps up to the 12.5 V row (no interpolation).
    value, prov = std.required_creepage(10.1, 2, "IIIa", insulation="basic")
    assert prov["voltage_row_used_v"] == 12.5
    assert value == pytest.approx(0.42)


def test_sub_50v_pd1_column():
    """PD1 sub-50 V values are material-group independent."""
    std = get_standard("iec60664")
    assert std.required_creepage(10, 1, "II", insulation="basic")[0] == pytest.approx(0.080)
    assert std.required_creepage(40, 1, "II", insulation="basic")[0] == pytest.approx(0.16)


def test_above_highest_row_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="exceeds the highest tabulated row"):
        std.required_creepage(1500, 2, "IIIa", insulation="basic")


def test_nonpositive_voltage_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError):
        std.required_creepage(0, 2, "IIIa", insulation="basic")
    with pytest.raises(StandardLookupError):
        std.required_creepage(-100, 2, "IIIa", insulation="basic")


# ---------------------------------------------------------------------------
# Undocumented combinations fail loud (never a silent number)
# ---------------------------------------------------------------------------


def test_pd3_iiib_is_undocumented_and_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="material group"):
        std.required_creepage(200, 3, "IIIb", insulation="basic")


def test_unknown_pollution_degree_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="pollution degree"):
        std.required_creepage(200, 4, "IIIa", insulation="basic")


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
    value, _ = std.required_creepage(170, 2, "IIIa", insulation="basic")
    assert 1.5 <= value <= 2.5, f"{standard_id}: {value} mm outside EE_REVIEW envelope"


# ---------------------------------------------------------------------------
# Clearance derivation + provenance
# ---------------------------------------------------------------------------


def test_clearance_floored_by_pollution_degree():
    std = get_standard("iec60664")
    # 48 V, OVC II -> 500 V impulse -> 0.04 mm; the PD2 floor governs.
    value, prov = std.required_clearance(48, 2, insulation="basic")
    assert value == pytest.approx(0.2)  # PD2 floor
    assert prov["governing_component"] == "pollution-degree floor"
    assert prov["altitude_assumption"] == "<= 2000 m"
    assert prov["quantity"] == "clearance"


def test_clearance_pd3_floor_is_higher():
    std = get_standard("iec60664")
    value, prov = std.required_clearance(48, 3, insulation="basic")
    assert value == pytest.approx(0.8)  # PD3 floor
    assert prov["governing_component"] == "pollution-degree floor"


def test_clearance_high_voltage_uses_impulse_row():
    std = get_standard("iec60664")
    value, prov = std.required_clearance(1000, 1, insulation="basic")
    assert prov["governing_component"] == "impulse"
    assert value == pytest.approx(5.5)


def test_clearance_above_highest_rated_voltage_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError):
        std.required_clearance(1000.1, 2, insulation="basic")
    with pytest.raises(StandardLookupError):
        std.required_clearance(20000, 2, insulation="basic")


# ---------------------------------------------------------------------------
# Issue #7057: clearance is keyed on the rated impulse withstand voltage
# (Table F.1 -> Table F.2) and the steady-state peak (Table F.8); the larger
# governs.  Every value below is cited in the curator's AC on #7057.
# ---------------------------------------------------------------------------

# (working_voltage_rms, pollution_degree, ovc) -> (mm, U_imp V, governing)
_CLEARANCE_REGRESSION = {
    # 230 V mains, OVC sweep, PD2.
    (230, 2, "I"): (0.5, 1500.0, "impulse"),
    (230, 2, "II"): (1.5, 2500.0, "impulse"),
    (230, 2, "III"): (3.0, 4000.0, "impulse"),
    (230, 2, "IV"): (5.5, 6000.0, "impulse"),
    # 120 V -> 150 V row; 48 V -> 50 V row (floor unchanged from pre-#7057).
    (120, 2, "II"): (0.5, 1500.0, "impulse"),
    (48, 2, "II"): (0.2, 500.0, "pollution-degree floor"),
    # Exactly on an F.1 row uses that row, not the next one.
    (300, 2, "II"): (1.5, 2500.0, "impulse"),
    # PD3 at 48 V: the 0.8 mm floor still governs.
    (48, 3, "II"): (0.8, 500.0, "pollution-degree floor"),
    # 1000 V rms: steady 1414 V pk -> 0.76 mm (F.8); impulse 6000 V -> 5.5 mm.
    (1000, 2, "II"): (5.5, 6000.0, "impulse"),
}


@pytest.mark.parametrize("standard_id", STDS)
@pytest.mark.parametrize(("key", "expected"), list(_CLEARANCE_REGRESSION.items()))
def test_clearance_regression_values(standard_id, key, expected):
    voltage, pd, ovc = key
    mm, u_imp, governing = expected
    value, prov = get_standard(standard_id).required_clearance(voltage, pd, ovc, insulation="basic")
    assert value == pytest.approx(mm)
    assert prov["impulse_withstand_voltage_v"] == u_imp
    assert prov["overvoltage_category"] == ovc
    assert prov["governing_component"] == governing
    assert prov["insulation"] == "basic"


def test_clearance_default_ovc_is_ii_and_230v_is_1p5mm():
    value, prov = get_standard("iec60664").required_clearance(230, 2, insulation="basic")
    assert value == pytest.approx(1.5)
    assert prov["overvoltage_category"] == "II"
    assert prov["impulse_withstand_voltage_v"] == 2500.0
    assert prov["governing_component"] == "impulse"
    assert prov["table_id"] == "Table F.2"
    assert prov["impulse_table_id"] == "Table F.1"
    assert prov["steady_state_table_id"] == "Table F.8"


def test_clearance_1000v_records_both_routes():
    _, prov = get_standard("iec60664").required_clearance(1000, 2, "II", insulation="basic")
    assert prov["peak_voltage_v"] == pytest.approx(1000 * RMS_TO_PEAK)
    assert prov["steady_state_row_used_v"] == 1500.0
    assert prov["steady_state_clearance_mm"] == pytest.approx(0.76)
    assert prov["impulse_clearance_mm"] == pytest.approx(5.5)


@pytest.mark.parametrize(
    ("impulse_v", "row", "mm"),
    [
        (2500.0, 2500.0, 1.5),
        (3100.0, 4000.0, 3.0),  # no 3200 V row; steps up to 4000 V
        (6100.0, 8000.0, 8.0),  # no 6300 V row; steps up to 8000 V
    ],
)
def test_table_f2_impulse_lookup(impulse_v, row, mm):
    value, row_used = get_standard("iec60664").impulse_clearance(impulse_v)
    assert row_used == row
    assert value == pytest.approx(mm)


def test_table_f2_has_only_preferred_series_rows():
    rows = get_standard("iec60664").clearance_impulse_rows
    assert 3200.0 not in rows
    assert 6300.0 not in rows
    assert 3000.0 in rows and 6000.0 in rows


@pytest.mark.parametrize(("peak_v", "mm"), [(1000.0, 0.26), (2000.0, 1.27), (5000.0, 5.7)])
def test_table_f8_steady_state_lookup(peak_v, mm):
    value, row_used = get_standard("iec60664").steady_state_clearance(peak_v)
    assert row_used == peak_v
    assert value == pytest.approx(mm)


def test_unknown_overvoltage_category_fails_loud():
    with pytest.raises(StandardLookupError):
        get_standard("iec60664").required_clearance(230, 2, "V", insulation="basic")


def test_iec62368_carries_unverified_harmonisation_note():
    _, prov = get_standard("iec62368").required_clearance(230, 2, insulation="basic")
    assert "UNVERIFIED" in prov["harmonisation_note"]


def test_creepage_pd2_group_ii_80v_is_0p95():
    value, prov = get_standard("iec60664").required_creepage(75, 2, "II", insulation="basic")
    assert value == pytest.approx(0.95)
    assert prov["voltage_row_used_v"] == 80.0
    assert prov["table_id"] == "Table F.5"


def test_clearance_provenance_carries_full_citation():
    std = get_standard("iec62368")
    _, prov = std.required_clearance(1000, 2, insulation="basic")
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
            vals = [std.required_creepage(v, pd, group, insulation="basic")[0] for v in rows]
            assert vals == sorted(vals), f"non-monotone PD{pd} {group}"


def test_creepage_monotonic_across_material_groups():
    std = get_standard("iec60664")
    for pd in (2, 3):
        for v in std.creepage_voltage_rows:
            i = std.required_creepage(v, pd, "I", insulation="basic")[0]
            ii = std.required_creepage(v, pd, "II", insulation="basic")[0]
            iiia = std.required_creepage(v, pd, "IIIa", insulation="basic")[0]
            assert i <= ii <= iiia, f"non-monotone groups at {v}V PD{pd}"


def test_creepage_monotonic_across_pollution_degrees():
    std = get_standard("iec60664")
    for v in std.creepage_voltage_rows:
        pd1 = std.required_creepage(v, 1, "I", insulation="basic")[0]
        pd2 = std.required_creepage(v, 2, "IIIa", insulation="basic")[0]
        pd3 = std.required_creepage(v, 3, "IIIa", insulation="basic")[0]
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


def test_clearance_tables_are_monotone():
    """Transcription self-check for Tables F.1 / F.2 / F.8 (issue #7057)."""
    std = get_standard("iec60664")
    for rows in (
        std.clearance_impulse_rows,
        std.clearance_impulse_values,
        std.clearance_steady_rows,
        std.clearance_steady_values,
        std.impulse_rated_voltage_rows,
    ):
        assert list(rows) == sorted(rows)
        assert len(set(rows)) == len(rows)
    columns = [std.impulse_withstand_values[c] for c in ("I", "II", "III", "IV")]
    for col in columns:
        assert list(col) == sorted(col)
        # Every U_imp value must be a Table F.2 row (no step-up between tables).
        assert all(v in std.clearance_impulse_rows for v in col)
    for lower, higher in zip(columns, columns[1:], strict=False):
        assert all(lo < hi for lo, hi in zip(lower, higher, strict=True))


# ---------------------------------------------------------------------------
# Issue #7082: insulation type (basic / supplementary / reinforced)
#
# Expected values trace to the issue #7057 curator tables (F.1 / F.2) and the
# issue #7082 curator rule: reinforced steps U_imp up one entry of the F.1
# preferred series before F.2 (clause 5.2.5); reinforced creepage = 2 x basic.
# ---------------------------------------------------------------------------

# (rms, ovc) -> (basic_mm, basic U_imp, reinforced_mm, reinforced U_imp), PD2.
#   230 V -> F.1 300 V row; 400 V -> F.1 600 V row.
_INSULATION_CASES = {
    (230, "II"): (1.5, 2500.0, 3.0, 4000.0),
    (230, "III"): (3.0, 4000.0, 5.5, 6000.0),
    (400, "II"): (3.0, 4000.0, 5.5, 6000.0),
    (400, "III"): (5.5, 6000.0, 8.0, 8000.0),
}


@pytest.mark.parametrize("standard_id", ["iec60664", "iec62368"])
@pytest.mark.parametrize("key,expected", list(_INSULATION_CASES.items()))
def test_basic_vs_reinforced_clearance(standard_id, key, expected):
    voltage, ovc = key
    basic_mm, basic_uimp, reinf_mm, reinf_uimp = expected
    std = get_standard(standard_id)
    b, bprov = std.required_clearance(voltage, 2, ovc, insulation="basic")
    r, rprov = std.required_clearance(voltage, 2, ovc, insulation="reinforced")
    assert b == pytest.approx(basic_mm)
    assert r == pytest.approx(reinf_mm)
    assert bprov["insulation"] == "basic"
    assert bprov["required_impulse_voltage_v"] == basic_uimp
    assert rprov["insulation"] == "reinforced"
    # Provenance keeps the pre-step Table F.1 value and the stepped value.
    assert rprov["impulse_withstand_voltage_v"] == basic_uimp
    assert rprov["required_impulse_voltage_v"] == reinf_uimp
    assert rprov["governing_component"] == "impulse"
    assert "UNVERIFIED" in rprov["insulation_unverified"]


def test_default_insulation_is_basic_230v_ovc_ii_is_1p5mm():
    """Default output equals the pre-#7082 basic values (byte-identical to main)."""
    assert DEFAULT_INSULATION == "basic"
    std = get_standard("iec60664")
    value, prov = std.required_clearance(230, 2)
    assert value == pytest.approx(1.5)
    assert prov["insulation"] == "basic"
    assert (value, prov) == std.required_clearance(230, 2, insulation="basic")
    creep, cprov = std.required_creepage(230, 2, "I")
    assert creep == pytest.approx(1.25)  # F.5, 250 V row, group I
    assert cprov["insulation"] == "basic"


@pytest.mark.parametrize("voltage,group", [(230, "I"), (230, "IIIa"), (400, "II"), (400, "IIIa")])
def test_reinforced_creepage_is_twice_basic(voltage, group):
    std = get_standard("iec60664")
    basic, _ = std.required_creepage(voltage, 2, group, insulation="basic")
    reinf, prov = std.required_creepage(voltage, 2, group, insulation="reinforced")
    assert reinf == pytest.approx(2 * basic)
    assert prov["basic_value_mm"] == basic
    assert prov["insulation_factor"] == 2.0


def test_supplementary_equals_basic():
    std = get_standard("iec60664")
    for v in (48, 120, 230, 400, 1000):
        for ovc in ("I", "II", "III", "IV"):
            assert (
                std.required_clearance(v, 2, ovc, insulation="supplementary")[0]
                == std.required_clearance(v, 2, ovc, insulation="basic")[0]
            )
        assert (
            std.required_creepage(v, 2, "IIIa", insulation="supplementary")[0]
            == std.required_creepage(v, 2, "IIIa", insulation="basic")[0]
        )


@pytest.mark.parametrize("standard_id", ["iec60664", "iec62368"])
def test_never_less_than_basic(standard_id):
    """Property: no insulation type ever yields less than basic, at any row."""
    std = get_standard(standard_id)
    voltages = [*_CREEPAGE_VOLTAGE_ROWS, 37.0, 230.0, 277.0, 480.0, 999.0]
    for v in voltages:
        for pd in (1, 2, 3):
            for ovc in ("I", "II", "III", "IV"):
                basic, _ = std.required_clearance(v, pd, ovc, insulation="basic")
                for ins in INSULATION_TYPES:
                    try:
                        val, _ = std.required_clearance(v, pd, ovc, insulation=ins)
                    except StandardLookupError:
                        # Only the top-of-series reinforced step may refuse.
                        assert ins == "reinforced"
                        continue
                    assert val >= basic
            group = "IIIa"
            cbasic, _ = std.required_creepage(v, pd, group, insulation="basic")
            for ins in INSULATION_TYPES:
                assert std.required_creepage(v, pd, group, insulation=ins)[0] >= cbasic


def test_reinforced_pd_floor_still_applies():
    std = get_standard("iec60664")
    # 48 V OVC I: 330 -> 500 V -> 0.04 mm (F.2), below the PD floors.
    v2, p2 = std.required_clearance(48, 2, "I", insulation="reinforced")
    assert v2 == pytest.approx(0.2)
    assert p2["governing_component"] == "pollution-degree floor"
    assert p2["required_impulse_voltage_v"] == 500.0
    v3, _ = std.required_clearance(48, 3, "I", insulation="reinforced")
    assert v3 == pytest.approx(0.8)


def test_reinforced_top_of_series_fails_loud():
    std = get_standard("iec60664")
    # 1000 V OVC IV -> U_imp 12000 V, the top of the series: no next step.
    with pytest.raises(StandardLookupError, match="top of the Table F.1"):
        std.required_clearance(1000, 2, "IV", insulation="reinforced")
    # 600 V OVC IV -> 8000 -> 12000 V -> 14.0 mm is still tabulated.
    value, _ = std.required_clearance(600, 2, "IV", insulation="reinforced")
    assert value == pytest.approx(14.0)


def test_reinforced_steps_along_f1_series_not_f2_axis():
    # 2500 -> 4000 (F.1 series), NOT 2500 -> 3000 (the denser F.2 axis).
    assert reinforced_impulse_voltage(2500.0) == 4000.0
    assert reinforced_impulse_voltage(330.0) == 500.0
    with pytest.raises(StandardLookupError):
        reinforced_impulse_voltage(3000.0)  # not on the F.1 series


def test_preferred_series_is_the_f1_body():
    body = sorted({u for col in _IMPULSE_WITHSTAND_V.values() for u in col})
    assert tuple(body) == _IMPULSE_PREFERRED_SERIES


def test_unknown_insulation_fails_loud():
    std = get_standard("iec60664")
    with pytest.raises(StandardLookupError, match="insulation"):
        std.required_clearance(230, 2, "II", insulation="double")
    with pytest.raises(StandardLookupError, match="insulation"):
        std.required_creepage(230, 2, "I", insulation="")
