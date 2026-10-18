"""IEC 60664-1 / 62368-1 creepage + clearance standard-table lookup (Issue #4332).

Phase 2 of ``kct creepage`` (phase 1: #4327).  Phase 1 required the operator
to supply the required creepage via ``--min``; this module lets that required
value be *derived* from the governing standard for a
``(working voltage, pollution degree, material group)`` triple, plus a
*clearance* requirement derived from the working voltage, overvoltage category
and pollution degree.

.. warning::

   **Engineering aid -- NOT a certification.**  The values encoded here are a
   careful transcription of published IEC creepage/clearance tables intended
   to catch gross layout mistakes early.  They are **not** a substitute for
   the controlled copy of the governing standard or the judgement of a
   qualified engineer, who remain authoritative.  Spot-check every derived
   number against the standard before relying on it for a certifiable design.

Safety-critical transcription rules honoured here
-------------------------------------------------

* Every tabulated value cites its standard, edition, table and axis inline
  (see the module-level table constants below).
* Lookups **step UP** to the next-higher tabulated voltage row -- creepage is
  never linearly interpolated (IEC 60664-1 :cite:`clause 6.2` / IEC 62368-1
  Table 17 are defined on discrete rows; interpolation is not permitted for
  creepage).  Rounding is therefore always toward the *more conservative*
  (larger) value.
* An out-of-range voltage (above the highest tabulated row) or an undocumented
  ``(pollution degree, material group)`` combination raises a loud, actionable
  :class:`StandardLookupError` -- the module never extrapolates or emits a
  guessed number.

Table provenance (transcribed values -- spot-check before certifying)
---------------------------------------------------------------------

* **Creepage** -- IEC 60664-1:2020 (Ed. 3.0) Table F.5, "Creepage distances to
  avoid failure due to tracking" (the same table was numbered Table F.4 in
  IEC 60664-1:2007 Ed. 2.0; the 2020 edition inserted a new Table F.4, "Three-
  phase four-wire or three-wire AC systems", ahead of it), and the harmonised
  IEC 62368-1:2018 (3rd ed.) Table 17.  The full 10 V-1000 V voltage axis
  transcribed here (including the sub-50 V head) is cross-checked against the
  controlled copy of **EN 60664-1:2007 Table F.4 (p. 67)**.  Keyed on
  **RMS working voltage**, pollution degree, and material group
  (I: CTI >= 600, II: 400 <= CTI < 600, IIIa: 175 <= CTI < 400,
  IIIb: 100 <= CTI < 175).  For pollution degree 1 the standard does not
  subdivide by material group (no conductive pollution -> tracking does not
  occur), so a single PD1 column is tabulated.  The two standards are
  harmonised on the tracking physics, so their creepage values are identical
  over the range encoded here.
* **Clearance** (issue #7057) -- basic insulation, inhomogeneous field
  ("case A"), altitude **<= 2000 m**.  Clause 5.2.2.1 requires the clearance
  to withstand the *larger* of two stresses, so two routes are evaluated and
  the larger governs:

  1. **Transient (impulse) route.**  The RMS working voltage is taken as the
     line-to-neutral rated voltage and stepped up to a Table F.1 row; the
     overvoltage category (I-IV) then selects the **rated impulse withstand
     voltage** ``U_imp`` (clause 5.2.2.2).  ``U_imp`` is looked up in
     **Table F.2** ("Clearances to withstand transient overvoltages", clause
     5.2.2.3), which is keyed on the impulse voltage -- *not* the steady-state
     peak working voltage.  Keying F.2 on the working-voltage peak (as this
     module did before #7057) under-specified 230 V mains clearance ~7x.
  2. **Steady-state route.**  The steady-state peak (``RMS * sqrt(2)``) is
     looked up in **Table F.8** case A (clause 5.2.2.4).

  The required clearance is ``max(F.2[U_imp], F.8[peak], PD floor)``, where the
  pollution-degree floor reproduces F.2's PD columns.  IEC 62368-1:2018
  Table 14 is *claimed* (unverified here) to be harmonised with this
  derivation, so ``--standard iec62368`` shares the same tables.

  Not modelled: altitude correction above 2000 m (Table A.2) and the
  homogeneous-field "case B" columns.
* **Insulation type** (issue #7082) -- ``basic``, ``supplementary`` or
  ``reinforced``; the default is **reinforced** (operator ruling on #7082:
  mains-to-SELV isolation is the main case this check exists for, and a basic
  default under-reports it ~2x).  Reinforced clearance steps the Table F.1
  ``U_imp`` up one row in the Table F.1 preferred impulse series before the
  Table F.2 lookup (clause 5.2.5); reinforced creepage is twice the basic
  Table F.5 value (clause 6.2).  Supplementary insulation is dimensioned as
  basic.  The rule applied is recorded in provenance; see
  :data:`_INSULATION_RULES` for which parts are UNVERIFIED.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public disclaimer (rendered by the CLI + carried in provenance)
# ---------------------------------------------------------------------------

DISCLAIMER = (
    "Engineering aid, NOT a certification: derived from a transcription of IEC "
    "creepage/clearance tables. The governing standard and a qualified engineer "
    "are authoritative -- spot-check every value before relying on it."
)

# Canonical material-group labels (ascending required creepage for a fixed
# voltage/PD): I < II < IIIa <= IIIb.
MATERIAL_GROUPS: tuple[str, ...] = ("I", "II", "IIIa", "IIIb")

# Sentinel key used for the pollution-degree-1 column, which the standard does
# not subdivide by material group.
_PD1_ANY = "*"


class StandardLookupError(ValueError):
    """Raised for an out-of-range or undocumented standard-table lookup.

    Subclasses :class:`ValueError` so callers that already catch ``ValueError``
    keep working, but is a distinct type so the CLI can render an actionable,
    safety-critical message rather than silently emitting a guessed number.
    """


def normalize_material_group(group: str) -> str:
    """Return the canonical material-group label, or raise loudly.

    Accepts case-insensitive ``I``/``II``/``IIIa``/``IIIb`` (and the common
    ``3a``/``3b`` shorthands).
    """
    raw = (group or "").strip()
    lowered = raw.lower()
    alias = {
        "i": "I",
        "ii": "II",
        "iii": "IIIa",  # bare "III" -> conservative IIIa
        "iiia": "IIIa",
        "iiib": "IIIb",
        "3a": "IIIa",
        "3b": "IIIb",
    }
    if lowered in alias:
        return alias[lowered]
    raise StandardLookupError(
        f"unknown material group {group!r}; expected one of {', '.join(MATERIAL_GROUPS)} "
        "(I: CTI>=600, II: 400<=CTI<600, IIIa: 175<=CTI<400, IIIb: 100<=CTI<175)"
    )


def normalize_insulation(insulation: str) -> str:
    """Return the canonical insulation type, or raise loudly.

    Accepts case-insensitive ``basic`` / ``supplementary`` / ``reinforced``.
    There is no alias for "double" insulation: issue #7082 leaves it out of
    scope, so it fails loud instead of silently reusing the reinforced rule.
    """
    canon = (str(insulation) if insulation is not None else "").strip().lower()
    if canon in INSULATION_TYPES:
        return canon
    raise StandardLookupError(
        f"unknown insulation type {insulation!r}; expected one of "
        f"{', '.join(INSULATION_TYPES)} (IEC 60664-1 clause 5.2.5 / 6.2)"
    )


def normalize_overvoltage_category(category: str) -> str:
    """Return the canonical overvoltage category (``I``-``IV``), or raise loudly.

    Accepts case-insensitive Roman numerals and the Arabic ``1``-``4``.
    """
    raw = (str(category) if category is not None else "").strip().upper()
    alias = {"1": "I", "2": "II", "3": "III", "4": "IV"}
    canon = alias.get(raw, raw)
    if canon in OVERVOLTAGE_CATEGORIES:
        return canon
    raise StandardLookupError(
        f"unknown overvoltage category {category!r}; expected one of "
        f"{', '.join(OVERVOLTAGE_CATEGORIES)} (IEC 60664-1 clause 4.3.3.2)"
    )


# ---------------------------------------------------------------------------
# Creepage tables (keyed on RMS working voltage)
# ---------------------------------------------------------------------------

# Ascending RMS working-voltage rows shared by every creepage column below.
# IEC 60664-1:2020 Table F.5 (2007 ed.: Table F.4) / IEC 62368-1:2018 Table 17 (voltage axis).
# The sub-50 V head (10-40 V) is the low end of EN 60664-1:2007 Table F.4
# (p. 67); without it every working voltage <= 50 V spuriously stepped up to
# the 50 V row (1.2 mm @ PD2/IIIa), which no dense-board pad gap can meet and
# which kept the creepage gate from ever passing (issue #4402).
_CREEPAGE_VOLTAGE_ROWS: tuple[float, ...] = (
    10.0,
    12.5,
    16.0,
    20.0,
    25.0,
    32.0,
    40.0,
    50.0,
    63.0,
    80.0,
    100.0,
    125.0,
    160.0,
    200.0,
    250.0,
    320.0,
    400.0,
    500.0,
    630.0,
    800.0,
    1000.0,
)

# Creepage distances in mm, aligned index-for-index with _CREEPAGE_VOLTAGE_ROWS.
# Source: IEC 60664-1:2020 Table F.5 (2007 ed.: Table F.4; == IEC 62368-1:2018
# Table 17, harmonised).
# The sub-50 V rows (10-40 V) are the general-material columns of
# EN 60664-1:2007 Table F.4 (p. 67); material groups I/II/III are identical
# through 32 V and first diverge at 40 V (PD2: 0.56/0.80/1.10) per the standard.
# PD1 is material-group independent (single column); PD2/PD3 subdivide by group.
# Rows read left-to-right at: 10, 12.5, 16, 20, 25, 32, 40, 50, 63, 80, 100,
# 125, 160, 200, 250, 320, 400, 500, 630, 800, 1000 V.
_CREEPAGE_MM: dict[int, dict[str, tuple[float, ...]]] = {
    # Pollution degree 1 -- Table F.5, PD1 column (no material-group split).
    1: {
        _PD1_ANY: (
            0.080,  # 10 V
            0.090,  # 12.5 V
            0.100,  # 16 V
            0.110,  # 20 V
            0.125,  # 25 V
            0.14,  # 32 V
            0.16,  # 40 V
            0.18,  # 50 V
            0.20,  # 63 V
            0.22,  # 80 V
            0.25,  # 100 V
            0.28,  # 125 V
            0.32,  # 160 V
            0.42,  # 200 V
            0.56,  # 250 V
            0.75,  # 320 V
            1.0,  # 400 V
            1.3,  # 500 V
            1.8,  # 630 V
            2.4,  # 800 V
            3.2,  # 1000 V
        ),
    },
    # Pollution degree 2 -- Table F.5, PD2 columns.
    2: {
        "I": (
            0.40,  # 10 V
            0.42,  # 12.5 V
            0.45,  # 16 V
            0.48,  # 20 V
            0.50,  # 25 V
            0.53,  # 32 V
            0.56,  # 40 V
            0.6,  # 50 V
            0.63,  # 63 V
            0.67,  # 80 V
            0.71,  # 100 V
            0.75,  # 125 V
            0.8,  # 160 V
            1.0,  # 200 V
            1.25,  # 250 V
            1.6,  # 320 V
            2.0,  # 400 V
            2.5,  # 500 V
            3.2,  # 630 V
            4.0,  # 800 V
            5.0,  # 1000 V
        ),
        "II": (
            0.40,  # 10 V
            0.42,  # 12.5 V
            0.45,  # 16 V
            0.48,  # 20 V
            0.50,  # 25 V
            0.53,  # 32 V
            0.80,  # 40 V
            0.85,  # 50 V
            0.9,  # 63 V
            0.95,  # 80 V -- was 0.9 (repeated the 63 V cell) before issue #7057
            1.0,  # 100 V
            1.05,  # 125 V
            1.1,  # 160 V
            1.4,  # 200 V
            1.8,  # 250 V
            2.2,  # 320 V
            2.8,  # 400 V
            3.6,  # 500 V
            4.5,  # 630 V
            5.6,  # 800 V
            7.1,  # 1000 V
        ),
        "IIIa": (
            0.40,  # 10 V
            0.42,  # 12.5 V
            0.45,  # 16 V
            0.48,  # 20 V
            0.50,  # 25 V
            0.53,  # 32 V
            1.10,  # 40 V
            1.2,  # 50 V
            1.25,  # 63 V
            1.3,  # 80 V
            1.4,  # 100 V
            1.5,  # 125 V
            1.6,  # 160 V
            2.0,  # 200 V
            2.5,  # 250 V
            3.2,  # 320 V
            4.0,  # 400 V
            5.0,  # 500 V
            6.3,  # 630 V
            8.0,  # 800 V
            10.0,  # 1000 V
        ),
    },
    # Pollution degree 3 -- Table F.5, PD3 columns.  Material group IIIb is not
    # tabulated for PD3 over this range (its use is restricted), so a IIIb/PD3
    # lookup fails loud rather than aliasing to IIIa.
    3: {
        "I": (
            1.00,  # 10 V
            1.05,  # 12.5 V
            1.10,  # 16 V
            1.20,  # 20 V
            1.25,  # 25 V
            1.30,  # 32 V
            1.40,  # 40 V
            1.5,  # 50 V
            1.6,  # 63 V
            1.7,  # 80 V
            1.8,  # 100 V
            1.9,  # 125 V
            2.0,  # 160 V
            2.5,  # 200 V
            3.2,  # 250 V
            4.0,  # 320 V
            5.0,  # 400 V
            6.3,  # 500 V
            8.0,  # 630 V
            10.0,  # 800 V
            12.5,  # 1000 V
        ),
        "II": (
            1.00,  # 10 V
            1.05,  # 12.5 V
            1.10,  # 16 V
            1.20,  # 20 V
            1.25,  # 25 V
            1.30,  # 32 V
            1.60,  # 40 V
            1.7,  # 50 V
            1.8,  # 63 V
            1.9,  # 80 V
            2.0,  # 100 V
            2.1,  # 125 V
            2.2,  # 160 V
            2.8,  # 200 V
            3.6,  # 250 V
            4.5,  # 320 V
            5.6,  # 400 V
            7.1,  # 500 V
            9.0,  # 630 V
            11.0,  # 800 V
            14.0,  # 1000 V
        ),
        "IIIa": (
            1.00,  # 10 V
            1.05,  # 12.5 V
            1.10,  # 16 V
            1.20,  # 20 V
            1.25,  # 25 V
            1.30,  # 32 V
            1.80,  # 40 V
            1.9,  # 50 V
            2.0,  # 63 V
            2.1,  # 80 V
            2.2,  # 100 V
            2.4,  # 125 V
            2.5,  # 160 V
            3.2,  # 200 V
            4.0,  # 250 V
            5.0,  # 320 V
            6.3,  # 400 V
            8.0,  # 500 V
            10.0,  # 630 V
            12.5,  # 800 V
            16.0,  # 1000 V
        ),
    },
}

# Material group IIIb shares the IIIa creepage column for PD1/PD2 over the
# encoded range (IEC 60664-1:2020 Table F.5 tabulates them identically there).
_CREEPAGE_MM[2]["IIIb"] = _CREEPAGE_MM[2]["IIIa"]


# ---------------------------------------------------------------------------
# Clearance tables (issue #7057)
#
# Basic insulation, inhomogeneous field ("case A"), altitude <= 2000 m.
# (Supplementary / reinforced are derived from these basic tables -- see the
# insulation-type block below, issue #7082.)
# Required clearance = max(Table F.2[U_imp], Table F.8[steady peak], PD floor)
# per IEC 60664-1:2020 clause 5.2.2.1 (the larger of the transient and the
# steady-state requirement governs).
#
# source: IEC 60664-1:2020 (Ed. 3.0) Tables F.1, F.2 and F.8 as cited and
# transcribed in the curator's "verified and scoped" comment on issue #7057.
# NOT re-verified against a controlled copy of the 2020 text in this repo --
# spot-check against the standard before relying on any value.
# ---------------------------------------------------------------------------

# Overvoltage categories (IEC 60664-1 clause 4.3.3.2), least to most severe.
OVERVOLTAGE_CATEGORIES: tuple[str, ...] = ("I", "II", "III", "IV")

# Default overvoltage category: equipment energised from the fixed
# installation (the common mains-appliance case).
DEFAULT_OVERVOLTAGE_CATEGORY = "II"

# Table F.1 voltage axis: rated voltage, line-to-neutral, V RMS, "up to and
# including".  The working voltage steps UP to the next row.  The axis stops at
# 1000 V, the top of the creepage table (Table F.5); above it the lookup fails
# loud.
# source: IEC 60664-1:2020 Table F.1 (rated impulse voltage for equipment
# energised directly from the low-voltage mains), clause 5.2.2.2.
_IMPULSE_RATED_VOLTAGE_ROWS: tuple[float, ...] = (50.0, 100.0, 150.0, 300.0, 600.0, 1000.0)

# Table F.1 body: rated impulse withstand voltage U_imp (V peak) per
# overvoltage category, aligned with _IMPULSE_RATED_VOLTAGE_ROWS.
# source: IEC 60664-1:2020 Table F.1, OVC I / II / III / IV columns.
_IMPULSE_WITHSTAND_V: dict[str, tuple[float, ...]] = {
    #      50 V    100 V   150 V   300 V   600 V   1000 V
    "I": (330.0, 500.0, 800.0, 1500.0, 2500.0, 4000.0),
    "II": (500.0, 800.0, 1500.0, 2500.0, 4000.0, 6000.0),
    "III": (800.0, 1500.0, 2500.0, 4000.0, 6000.0, 8000.0),
    "IV": (1500.0, 2500.0, 4000.0, 6000.0, 8000.0, 12000.0),
}

# Table F.2 voltage axis: required impulse withstand voltage, V peak (the
# preferred impulse series).  There is NO 3200 V or 6300 V row -- those were
# transcription errors removed by issue #7057 (now 3000 V / 6000 V).
# source: IEC 60664-1:2020 Table F.2 ("Clearances to withstand transient
# overvoltages"), clause 5.2.2.3.
_CLEARANCE_IMPULSE_ROWS: tuple[float, ...] = (
    330.0,
    400.0,
    500.0,
    600.0,
    800.0,
    1000.0,
    1200.0,
    1500.0,
    2000.0,
    2500.0,
    3000.0,
    4000.0,
    5000.0,
    6000.0,
    8000.0,
    10000.0,
    12000.0,
)

# Table F.2 case A (inhomogeneous field) clearance in mm, aligned with
# _CLEARANCE_IMPULSE_ROWS.  The PD floors below reproduce F.2's pollution-
# degree columns (``max(case A, floor)``).
# source: IEC 60664-1:2020 Table F.2, case A column.
_CLEARANCE_IMPULSE_MM: tuple[float, ...] = (
    0.01,  # 330 V
    0.02,  # 400 V
    0.04,  # 500 V
    0.06,  # 600 V
    0.10,  # 800 V
    0.15,  # 1000 V
    0.25,  # 1200 V
    0.5,  # 1500 V
    1.0,  # 2000 V
    1.5,  # 2500 V
    2.0,  # 3000 V
    3.0,  # 4000 V
    4.0,  # 5000 V
    5.5,  # 6000 V
    8.0,  # 8000 V
    11.0,  # 10000 V
    14.0,  # 12000 V
)

# Table F.8 voltage axis: steady-state peak voltage, V peak.  Stepped UP to
# the next row -- never interpolated (the module's single lookup rule; always
# the more conservative value).  Below 330 V peak the 330 V row is used, which
# is conservative (and always below the PD floor).
# source: IEC 60664-1:2020 Table F.8 ("Dimensioning of clearances to withstand
# steady-state voltages, temporary overvoltages or recurring peak voltages"),
# clause 5.2.2.4.  Curator transcription; the 1000 / 2000 / 5000 V anchors
# (0.26 / 1.27 / 5.7 mm) also match the issue #7057 report.
_CLEARANCE_STEADY_ROWS: tuple[float, ...] = (
    330.0,
    400.0,
    500.0,
    600.0,
    800.0,
    1000.0,
    1200.0,
    1500.0,
    2000.0,
    2500.0,
    3000.0,
    4000.0,
    5000.0,
    6000.0,
    8000.0,
)

# Table F.8 case A clearance in mm, aligned with _CLEARANCE_STEADY_ROWS.
# source: IEC 60664-1:2020 Table F.8, case A column.
_CLEARANCE_STEADY_MM: tuple[float, ...] = (
    0.01,  # 330 V
    0.02,  # 400 V
    0.04,  # 500 V
    0.06,  # 600 V
    0.13,  # 800 V
    0.26,  # 1000 V
    0.42,  # 1200 V
    0.76,  # 1500 V
    1.27,  # 2000 V
    1.8,  # 2500 V
    2.4,  # 3000 V
    3.8,  # 4000 V
    5.7,  # 5000 V
    7.9,  # 6000 V
    11.0,  # 8000 V
)

# Pollution-degree minimum clearance floors (mm), IEC 60664-1 clause 6.1.
# The required clearance is max(base-table value, this floor).
_CLEARANCE_PD_FLOOR_MM: dict[int, float] = {
    1: 0.2,  # PD1
    2: 0.2,  # PD2
    3: 0.8,  # PD3
}

# ---------------------------------------------------------------------------
# Insulation type (issue #7082)
# ---------------------------------------------------------------------------

# Insulation types accepted by the clearance / creepage derivations.
# "double" insulation (basic + supplementary) is out of scope for #7082.
INSULATION_TYPES: tuple[str, ...] = ("basic", "supplementary", "reinforced")

# Default insulation type: reinforced.  source: operator ruling on issue #7082
# (2026-10-18) -- mains-to-SELV isolation is the main case this check exists
# for; a basic default reports ~1.5 mm where ~3.0 mm is needed at 230 V OVC II.
DEFAULT_INSULATION = "reinforced"

# Table F.1 preferred impulse-voltage series (V peak): the union of every
# U_imp value in Table F.1's body.  Reinforced insulation steps U_imp up ONE
# entry of THIS series -- not one row of the denser Table F.2 axis.
# source: IEC 60664-1:2020 clause 5.2.5, series as listed in the curator
# comment on issue #7082 (330, 500, 800, 1500, 2500, 4000, 6000, 8000, 12000).
_IMPULSE_PREFERRED_SERIES: tuple[float, ...] = (
    330.0,
    500.0,
    800.0,
    1500.0,
    2500.0,
    4000.0,
    6000.0,
    8000.0,
    12000.0,
)

# Reinforced creepage = 2 x the basic Table F.5 value.
# source: IEC 60664-1:2020 clause 6.2 (Table F.5 note), as cited by the curator
# comment and the operator ruling on issue #7082.  UNVERIFIED against a
# controlled copy of the 2020 text.
_REINFORCED_CREEPAGE_FACTOR = 2.0

# Reinforced steady-state factor: the Table F.8 route is evaluated at 160 % of
# the steady-state peak.  source: IEC 60664-1:2020 clause 5.2.5 as RECALLED in
# the curator comment on issue #7082 -- explicitly not checked against the
# standard.  Applied because it can only raise the requirement (never less
# safe); within the encoded 1000 V RMS range the reinforced impulse route
# governs anyway, so it changes no reported value there.  UNVERIFIED.
_REINFORCED_STEADY_STATE_FACTOR = 1.6

# Human-readable rule recorded in provenance, plus what is UNVERIFIED.
_INSULATION_RULES: dict[str, dict[str, str]] = {
    "basic": {
        "clearance": "basic: Table F.2 at the Table F.1 rated impulse voltage",
        "creepage": "basic: Table F.5 value",
        "unverified": "",
    },
    "supplementary": {
        # source: IEC 60664-1 clause 5.2.5 / 6.2 (supplementary insulation is
        # dimensioned as basic) -- recalled, NOT checked against the standard.
        "clearance": "supplementary: dimensioned as basic (clause 5.2.5)",
        "creepage": "supplementary: dimensioned as basic (clause 6.2)",
        "unverified": (
            "UNVERIFIED: 'supplementary = basic' is recalled from IEC 60664-1 "
            "clauses 5.2.5 / 6.2, not checked against a controlled copy."
        ),
    },
    "reinforced": {
        "clearance": (
            "reinforced: Table F.1 U_imp stepped up one entry of the preferred "
            "impulse series before the Table F.2 lookup (clause 5.2.5); Table F.8 "
            "route at 1.6 x the steady-state peak"
        ),
        "creepage": "reinforced: 2 x the basic Table F.5 value (clause 6.2)",
        "unverified": (
            "UNVERIFIED: the clause 5.2.5 one-step-up rule and the clause 6.2 2x "
            "creepage factor are transcribed from the issue #7082 curator comment; "
            "the 1.6x steady-state factor is recalled, not checked against the "
            "standard."
        ),
    },
}


def reinforced_impulse_voltage(u_imp: float) -> float:
    """Return the next-higher Table F.1 preferred-series impulse voltage.

    IEC 60664-1 clause 5.2.5: reinforced insulation is dimensioned for the
    rated impulse voltage one step higher in the preferred series.  Raises
    :class:`StandardLookupError` when ``u_imp`` is not on the series or is
    already its top entry (12000 V) -- never extrapolates.
    """
    for i, row in enumerate(_IMPULSE_PREFERRED_SERIES):
        if abs(u_imp - row) <= 1e-9:
            if i + 1 >= len(_IMPULSE_PREFERRED_SERIES):
                raise StandardLookupError(
                    f"rated impulse voltage {u_imp:g} V is the top of the Table F.1 "
                    "preferred series; reinforced insulation needs the next-higher "
                    "step, which is not tabulated -- the module does not extrapolate. "
                    "Consult the controlled standard or supply an explicit --min."
                )
            return _IMPULSE_PREFERRED_SERIES[i + 1]
    raise StandardLookupError(
        f"rated impulse voltage {u_imp:g} V is not on the Table F.1 preferred series "
        f"({', '.join(f'{v:g}' for v in _IMPULSE_PREFERRED_SERIES)} V)"
    )


# Sinusoidal RMS -> peak conversion for deriving the Table F.8 steady-state
# axis from an RMS working voltage.
RMS_TO_PEAK = math.sqrt(2.0)


# ---------------------------------------------------------------------------
# Standard descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreepageStandard:
    """A structured, self-describing creepage/clearance standard table set.

    Carries the metadata (``standard_id``, ``edition``, table ids, clauses)
    alongside the numeric tables so every derived value can be traced back to
    its source row.
    """

    standard_id: str
    edition: str
    creepage_table_id: str
    creepage_clause: str
    clearance_table_id: str
    clearance_clause: str
    altitude_assumption: str = "<= 2000 m"
    # Ascending RMS working-voltage rows for creepage.
    creepage_voltage_rows: tuple[float, ...] = field(default=_CREEPAGE_VOLTAGE_ROWS)
    # creepage_values[pollution_degree][material_group] aligned to the rows.
    creepage_values: dict[int, dict[str, tuple[float, ...]]] = field(
        default_factory=lambda: _CREEPAGE_MM
    )
    # Table-id labels for the two auxiliary clearance tables (issue #7057):
    # rated voltage + OVC -> U_imp, and the steady-state clearance table.
    impulse_table_id: str = "Table F.1"
    steady_state_table_id: str = "Table F.8"
    # Free-text note on how the clearance tables relate to this standard
    # (e.g. an unverified harmonisation claim); empty when native.
    clearance_harmonisation_note: str = ""
    # Table F.1: rated-voltage rows (V RMS) and U_imp (V peak) per OVC.
    impulse_rated_voltage_rows: tuple[float, ...] = field(default=_IMPULSE_RATED_VOLTAGE_ROWS)
    impulse_withstand_values: dict[str, tuple[float, ...]] = field(
        default_factory=lambda: _IMPULSE_WITHSTAND_V
    )
    # Table F.2 case A: impulse rows (V peak) -> clearance (mm).
    clearance_impulse_rows: tuple[float, ...] = field(default=_CLEARANCE_IMPULSE_ROWS)
    clearance_impulse_values: tuple[float, ...] = field(default=_CLEARANCE_IMPULSE_MM)
    # Table F.8 case A: steady-state peak rows (V peak) -> clearance (mm).
    clearance_steady_rows: tuple[float, ...] = field(default=_CLEARANCE_STEADY_ROWS)
    clearance_steady_values: tuple[float, ...] = field(default=_CLEARANCE_STEADY_MM)
    clearance_pd_floor: dict[int, float] = field(default_factory=lambda: _CLEARANCE_PD_FLOOR_MM)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _step_up_index(self, rows: tuple[float, ...], voltage: float, axis_label: str) -> int:
        """Return the index of the smallest row ``>= voltage`` (step-up rule).

        Raises :class:`StandardLookupError` if ``voltage`` is non-positive or
        exceeds the highest tabulated row (no extrapolation).
        """
        if not math.isfinite(voltage) or voltage <= 0.0:
            raise StandardLookupError(
                f"{axis_label} must be a positive, finite voltage; got {voltage!r}"
            )
        for i, row in enumerate(rows):
            if voltage <= row + 1e-9:
                return i
        raise StandardLookupError(
            f"{axis_label} {voltage:g} V exceeds the highest tabulated row "
            f"({rows[-1]:g} V) for {self.standard_id} {self.edition}; the standard's "
            "clause does not permit extrapolation -- consult the controlled standard "
            "or supply an explicit --min."
        )

    def required_creepage(
        self,
        working_voltage_rms: float,
        pollution_degree: int,
        material_group: str,
        insulation: str = DEFAULT_INSULATION,
    ) -> tuple[float, dict[str, Any]]:
        """Derive the required creepage (mm) plus structured provenance.

        ``working_voltage_rms`` keys the table directly (step-up to the next
        higher row; never interpolated).  ``insulation`` (issue #7082) is
        ``basic`` / ``supplementary`` (the table value) or ``reinforced``
        (2 x the table value, clause 6.2); default ``reinforced``.  Raises
        :class:`StandardLookupError` for an out-of-range voltage, an
        undocumented ``(PD, group)`` combo or an unknown insulation type.
        """
        ins = normalize_insulation(insulation)
        group = normalize_material_group(material_group)
        pd_columns = self.creepage_values.get(pollution_degree)
        if pd_columns is None:
            raise StandardLookupError(
                f"pollution degree {pollution_degree!r} is not tabulated for "
                f"{self.standard_id} {self.edition} (expected 1, 2 or 3)"
            )
        # PD1 is material-group independent.
        column_key = _PD1_ANY if pollution_degree == 1 else group
        column = pd_columns.get(column_key)
        if column is None:
            raise StandardLookupError(
                f"material group {group!r} is not tabulated under pollution degree "
                f"{pollution_degree} for {self.standard_id} {self.edition} "
                f"({self.creepage_table_id}); its use is restricted there -- "
                "use a lower material group or supply an explicit --min."
            )
        idx = self._step_up_index(
            self.creepage_voltage_rows, working_voltage_rms, "working voltage (RMS)"
        )
        basic_value = column[idx]
        factor = _REINFORCED_CREEPAGE_FACTOR if ins == "reinforced" else 1.0
        value = basic_value * factor
        row_used = self.creepage_voltage_rows[idx]
        provenance = {
            "standard": self.standard_id,
            "edition": self.edition,
            "table_id": self.creepage_table_id,
            "clause": self.creepage_clause,
            "quantity": "creepage",
            "voltage_axis": "rms_working_voltage",
            "working_voltage_v": working_voltage_rms,
            "voltage_row_used_v": row_used,
            "pollution_degree": pollution_degree,
            "material_group": group if pollution_degree != 1 else "n/a (PD1)",
            "lookup_rule": "step-up to next-higher row (no interpolation)",
            "altitude_assumption": "n/a (creepage)",
            "insulation": ins,
            "insulation_rule": _INSULATION_RULES[ins]["creepage"],
            "insulation_factor": factor,
            "basic_value_mm": basic_value,
            "insulation_unverified": _INSULATION_RULES[ins]["unverified"],
            "value_mm": value,
            "disclaimer": DISCLAIMER,
        }
        return value, provenance

    def rated_impulse_voltage(
        self, rated_voltage_rms: float, overvoltage_category: str = DEFAULT_OVERVOLTAGE_CATEGORY
    ) -> tuple[float, float]:
        """Return ``(U_imp, rated_voltage_row_used)`` from Table F.1.

        ``rated_voltage_rms`` (line-to-neutral, V RMS) steps UP to the next
        Table F.1 row; ``overvoltage_category`` (``I``-``IV``) selects the
        column.  Raises :class:`StandardLookupError` above the top row
        (1000 V) or for an unknown category.
        """
        ovc = normalize_overvoltage_category(overvoltage_category)
        idx = self._step_up_index(
            self.impulse_rated_voltage_rows, rated_voltage_rms, "working voltage (RMS)"
        )
        return self.impulse_withstand_values[ovc][idx], self.impulse_rated_voltage_rows[idx]

    def impulse_clearance(self, impulse_voltage: float) -> tuple[float, float]:
        """Return ``(clearance_mm, row_used)`` from Table F.2 case A.

        Keyed on the required impulse withstand voltage (V peak), stepping UP
        to the next preferred-series row.  No pollution-degree floor applied.
        """
        idx = self._step_up_index(
            self.clearance_impulse_rows, impulse_voltage, "impulse withstand voltage"
        )
        return self.clearance_impulse_values[idx], self.clearance_impulse_rows[idx]

    def steady_state_clearance(self, peak_voltage: float) -> tuple[float, float]:
        """Return ``(clearance_mm, row_used)`` from Table F.8 case A.

        Keyed on the steady-state peak voltage (V peak), stepping UP to the
        next row (never interpolated).  No pollution-degree floor applied.
        """
        idx = self._step_up_index(
            self.clearance_steady_rows, peak_voltage, "steady-state peak voltage"
        )
        return self.clearance_steady_values[idx], self.clearance_steady_rows[idx]

    def required_clearance(
        self,
        working_voltage_rms: float,
        pollution_degree: int,
        overvoltage_category: str = DEFAULT_OVERVOLTAGE_CATEGORY,
        insulation: str = DEFAULT_INSULATION,
    ) -> tuple[float, dict[str, Any]]:
        """Derive the required clearance (mm) plus structured provenance.

        Case A, altitude ``<= 2000 m`` (issue #7057), for the given
        ``insulation`` type (issue #7082, default ``reinforced``):

        1. ``working_voltage_rms`` (taken as the line-to-neutral rated
           voltage) + ``overvoltage_category`` -> rated impulse withstand
           voltage ``U_imp`` (Table F.1).  For ``reinforced`` insulation
           ``U_imp`` steps up one entry of the Table F.1 preferred series
           (clause 5.2.5).  The result keys the transient clearance
           (Table F.2).
        2. ``working_voltage_rms * sqrt(2)`` (steady-state peak; x 1.6 for
           ``reinforced``, UNVERIFIED) -> steady-state clearance (Table F.8).
        3. Result = ``max(step 1, step 2, pollution-degree floor)``.

        ``basic`` and ``supplementary`` give identical values.  Raises
        :class:`StandardLookupError` for an out-of-range voltage (above
        1000 V RMS), an unknown pollution degree, overvoltage category or
        insulation type, or a reinforced step above the top of the series --
        never extrapolates.
        """
        ins = normalize_insulation(insulation)
        floor = self.clearance_pd_floor.get(pollution_degree)
        if floor is None:
            raise StandardLookupError(
                f"pollution degree {pollution_degree!r} has no clearance floor for "
                f"{self.standard_id} {self.edition} (expected 1, 2 or 3)"
            )
        ovc = normalize_overvoltage_category(overvoltage_category)
        u_imp, rated_row = self.rated_impulse_voltage(working_voltage_rms, ovc)
        reinforced = ins == "reinforced"
        required_u_imp = reinforced_impulse_voltage(u_imp) if reinforced else u_imp
        impulse_mm, impulse_row = self.impulse_clearance(required_u_imp)
        peak_voltage = working_voltage_rms * RMS_TO_PEAK
        steady_factor = _REINFORCED_STEADY_STATE_FACTOR if reinforced else 1.0
        steady_voltage = peak_voltage * steady_factor
        steady_mm, steady_row = self.steady_state_clearance(steady_voltage)
        value = max(impulse_mm, steady_mm, floor)
        # Ties resolve toward the floor, then the impulse route, so the
        # reported governing component is stable.
        if floor >= impulse_mm and floor >= steady_mm:
            governing = "pollution-degree floor"
        elif impulse_mm >= steady_mm:
            governing = "impulse"
        else:
            governing = "steady-state"
        provenance = {
            "standard": self.standard_id,
            "edition": self.edition,
            "table_id": self.clearance_table_id,
            "clause": self.clearance_clause,
            "impulse_table_id": self.impulse_table_id,
            "steady_state_table_id": self.steady_state_table_id,
            "harmonisation_note": self.clearance_harmonisation_note,
            "quantity": "clearance",
            "insulation": ins,
            "insulation_rule": _INSULATION_RULES[ins]["clearance"],
            "insulation_unverified": _INSULATION_RULES[ins]["unverified"],
            "field": "inhomogeneous (case A)",
            "voltage_axis": "rated_impulse_and_steady_state_peak",
            "working_voltage_v": working_voltage_rms,
            "rated_voltage_row_used_v": rated_row,
            "overvoltage_category": ovc,
            # Table F.1 value (pre-step).  ``required_impulse_voltage_v`` is
            # the value Table F.2 is keyed on (stepped up for reinforced).
            "impulse_withstand_voltage_v": u_imp,
            "required_impulse_voltage_v": required_u_imp,
            "impulse_row_used_v": impulse_row,
            "impulse_clearance_mm": impulse_mm,
            "peak_voltage_v": peak_voltage,
            "steady_state_factor": steady_factor,
            "steady_state_voltage_v": steady_voltage,
            "steady_state_row_used_v": steady_row,
            "steady_state_clearance_mm": steady_mm,
            "pollution_degree": pollution_degree,
            "pd_floor_mm": floor,
            "governing_component": governing,
            "lookup_rule": "step-up to next-higher row (no interpolation)",
            "altitude_assumption": self.altitude_assumption,
            "value_mm": value,
            "disclaimer": DISCLAIMER,
        }
        return value, provenance


# ---------------------------------------------------------------------------
# Registered standards
# ---------------------------------------------------------------------------

_IEC_60664_1 = CreepageStandard(
    standard_id="IEC 60664-1",
    edition="2020 (Ed. 3.0)",
    creepage_table_id="Table F.5",
    creepage_clause="clause 6.2 (creepage, keyed on RMS working voltage)",
    clearance_table_id="Table F.2",
    clearance_clause=(
        "clause 5.2.2 (clearance: larger of Table F.2 at the Table F.1 rated impulse "
        "voltage and Table F.8 at the steady-state peak; case A)"
    ),
)

_IEC_62368_1 = CreepageStandard(
    standard_id="IEC 62368-1",
    edition="2018 (Ed. 3.0)",
    creepage_table_id="Table 17",
    creepage_clause="5.4.3 (creepage; harmonised with IEC 60664-1 Table F.5)",
    clearance_table_id="Table 14",
    clearance_clause="5.4.2 (clearance; harmonised with IEC 60664-1 Table F.2)",
    impulse_table_id="IEC 60664-1 Table F.1",
    steady_state_table_id="IEC 60664-1 Table F.8",
    clearance_harmonisation_note=(
        "UNVERIFIED: derived with the IEC 60664-1 Tables F.1/F.2/F.8 procedure on "
        "the claim that IEC 62368-1 Table 14 is harmonised with it; IEC 62368-1's "
        "own Tables 10/11/12 procedure is not re-derived here."
    ),
)

STANDARDS: dict[str, CreepageStandard] = {
    "iec60664": _IEC_60664_1,
    "iec62368": _IEC_62368_1,
}


def get_standard(standard_id: str) -> CreepageStandard:
    """Return the :class:`CreepageStandard` for a CLI ``--standard`` value."""
    key = (standard_id or "").strip().lower()
    std = STANDARDS.get(key)
    if std is None:
        raise StandardLookupError(
            f"unknown standard {standard_id!r}; expected one of {', '.join(STANDARDS)}"
        )
    return std
