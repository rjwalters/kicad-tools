"""IEC 60664-1 / 62368-1 creepage + clearance standard-table lookup (Issue #4332).

Phase 2 of ``kct creepage`` (phase 1: #4327).  Phase 1 required the operator
to supply the required creepage via ``--min``; this module lets that required
value be *derived* from the governing standard for a
``(working voltage, pollution degree, material group)`` triple, plus a
peak-voltage/pollution-degree *clearance* requirement.

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

* **Creepage** -- IEC 60664-1:2020 Table F.4 (the "Table 4" of older
  editions; renumbered Table F.5 in the 2020 Ed. 3.1) and the harmonised
  IEC 62368-1:2018 (3rd ed.) Table 17.  The full 10 V-1000 V voltage axis
  transcribed here (including the sub-50 V head) is cross-checked against the
  controlled copy of **EN 60664-1:2007 Table F.4 (p. 67)**, "Creepage
  distances to avoid failure due to tracking".  Keyed on
  **RMS working voltage**, pollution degree, and material group
  (I: CTI >= 600, II: 400 <= CTI < 600, IIIa: 175 <= CTI < 400,
  IIIb: 100 <= CTI < 175).  For pollution degree 1 the standard does not
  subdivide by material group (no conductive pollution -> tracking does not
  occur), so a single PD1 column is tabulated.  The two standards are
  harmonised on the tracking physics, so their creepage values are identical
  over the range encoded here.
* **Clearance** -- IEC 60664-1:2020 Table F.2 (inhomogeneous-field / "case A",
  basic insulation) with the pollution-degree minimum floors of clause 6.1,
  at altitude **<= 2000 m**; IEC 62368-1:2018 Table 14 is the harmonised
  counterpart.  Keyed on the **peak** value of the working voltage plus the
  pollution degree.  The required clearance is ``max(table value, PD floor)``.
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


# ---------------------------------------------------------------------------
# Creepage tables (keyed on RMS working voltage)
# ---------------------------------------------------------------------------

# Ascending RMS working-voltage rows shared by every creepage column below.
# IEC 60664-1:2020 Table F.4 / IEC 62368-1:2018 Table 17 (voltage axis).
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
# Source: IEC 60664-1:2020 Table F.4 (== IEC 62368-1:2018 Table 17, harmonised).
# The sub-50 V rows (10-40 V) are the general-material columns of
# EN 60664-1:2007 Table F.4 (p. 67); material groups I/II/III are identical
# through 32 V and first diverge at 40 V (PD2: 0.56/0.80/1.10) per the standard.
# PD1 is material-group independent (single column); PD2/PD3 subdivide by group.
# Rows read left-to-right at: 10, 12.5, 16, 20, 25, 32, 40, 50, 63, 80, 100,
# 125, 160, 200, 250, 320, 400, 500, 630, 800, 1000 V.
_CREEPAGE_MM: dict[int, dict[str, tuple[float, ...]]] = {
    # Pollution degree 1 -- Table F.4, PD1 column (no material-group split).
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
    # Pollution degree 2 -- Table F.4, PD2 columns.
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
            0.9,  # 80 V
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
    # Pollution degree 3 -- Table F.4, PD3 columns.  Material group IIIb is not
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
# encoded range (IEC 60664-1 Table F.4 tabulates them identically there).
_CREEPAGE_MM[2]["IIIb"] = _CREEPAGE_MM[2]["IIIa"]


# ---------------------------------------------------------------------------
# Clearance table (keyed on peak working voltage + pollution degree)
# ---------------------------------------------------------------------------

# Ascending peak-voltage rows for the clearance table.
# IEC 60664-1:2020 Table F.2 (inhomogeneous field / "case A", <= 2000 m).
_CLEARANCE_PEAK_ROWS: tuple[float, ...] = (
    330.0,
    500.0,
    800.0,
    1000.0,
    1500.0,
    2000.0,
    2500.0,
    3200.0,
    4000.0,
    5000.0,
    6300.0,
    8000.0,
    10000.0,
)

# Base clearance in mm (inhomogeneous field, basic insulation, altitude
# <= 2000 m), aligned with _CLEARANCE_PEAK_ROWS.  IEC 60664-1:2020 Table F.2.
# Rows: 330, 500, 800, 1000, 1500, 2000, 2500, 3200, 4000, 5000, 6300, 8000,
# 10000 V peak.  Below ~1.5 kV the pollution-degree floor below dominates.
_CLEARANCE_BASE_MM: tuple[float, ...] = (
    0.01,  # 330 V
    0.04,  # 500 V
    0.10,  # 800 V
    0.15,  # 1000 V
    0.5,  # 1500 V
    1.0,  # 2000 V
    1.5,  # 2500 V
    2.0,  # 3200 V
    3.0,  # 4000 V
    4.0,  # 5000 V
    5.5,  # 6300 V
    8.0,  # 8000 V
    11.0,  # 10000 V
)

# Pollution-degree minimum clearance floors (mm), IEC 60664-1 clause 6.1.
# The required clearance is max(base-table value, this floor).
_CLEARANCE_PD_FLOOR_MM: dict[int, float] = {
    1: 0.2,  # PD1
    2: 0.2,  # PD2
    3: 0.8,  # PD3
}

# Sinusoidal RMS -> peak conversion for deriving the clearance axis from an
# RMS working voltage when no explicit peak is supplied.
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
    clearance_peak_rows: tuple[float, ...] = field(default=_CLEARANCE_PEAK_ROWS)
    clearance_base_values: tuple[float, ...] = field(default=_CLEARANCE_BASE_MM)
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
        self, working_voltage_rms: float, pollution_degree: int, material_group: str
    ) -> tuple[float, dict[str, Any]]:
        """Derive the required creepage (mm) plus structured provenance.

        ``working_voltage_rms`` keys the table directly (step-up to the next
        higher row; never interpolated).  Raises :class:`StandardLookupError`
        for an out-of-range voltage or an undocumented ``(PD, group)`` combo.
        """
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
        value = column[idx]
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
            "value_mm": value,
            "disclaimer": DISCLAIMER,
        }
        return value, provenance

    def required_clearance(
        self, peak_voltage: float, pollution_degree: int
    ) -> tuple[float, dict[str, Any]]:
        """Derive the required clearance (mm) plus structured provenance.

        Keyed on the **peak** working voltage and pollution degree at altitude
        ``<= 2000 m``.  The result is ``max(base-table value, PD floor)``.
        """
        floor = self.clearance_pd_floor.get(pollution_degree)
        if floor is None:
            raise StandardLookupError(
                f"pollution degree {pollution_degree!r} has no clearance floor for "
                f"{self.standard_id} {self.edition} (expected 1, 2 or 3)"
            )
        idx = self._step_up_index(self.clearance_peak_rows, peak_voltage, "peak working voltage")
        base = self.clearance_base_values[idx]
        row_used = self.clearance_peak_rows[idx]
        value = max(base, floor)
        governing = "pollution-degree floor" if floor >= base else "peak-voltage table row"
        provenance = {
            "standard": self.standard_id,
            "edition": self.edition,
            "table_id": self.clearance_table_id,
            "clause": self.clearance_clause,
            "quantity": "clearance",
            "voltage_axis": "peak_working_voltage",
            "peak_voltage_v": peak_voltage,
            "voltage_row_used_v": row_used,
            "pollution_degree": pollution_degree,
            "base_table_mm": base,
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
    creepage_table_id="Table F.4",
    creepage_clause="clause 6.2 (creepage, keyed on RMS working voltage)",
    clearance_table_id="Table F.2",
    clearance_clause="clause 6.1 (clearance, inhomogeneous field / case A)",
)

_IEC_62368_1 = CreepageStandard(
    standard_id="IEC 62368-1",
    edition="2018 (Ed. 3.0)",
    creepage_table_id="Table 17",
    creepage_clause="5.4.3 (creepage; harmonised with IEC 60664-1 Table F.4)",
    clearance_table_id="Table 14",
    clearance_clause="5.4.2 (clearance; harmonised with IEC 60664-1 Table F.2)",
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
