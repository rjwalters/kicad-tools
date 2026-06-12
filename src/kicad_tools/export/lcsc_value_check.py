"""
Parametric value validation for LCSC part assignments.

Guards against wrong-part LCSC assignments by comparing the BOM row's
requested value against what is known about the candidate LCSC part
(its parametric ``value`` field and/or catalog description).

Motivating defect (issue #3590): the enrichment cache fallback assigned
C1525 -- a 100nF 0402 capacitor -- to a 16nF BOM row, and the bad
assignment then self-perpetuated through the ``merge_lcsc`` CSV
read-back on every subsequent export.

Design notes:

- Value parsing is delegated to :func:`kicad_tools.cost.suggest.parse_component_value`
  (the canonical parser per the #3593 survey) so requested values and
  candidate part values are interpreted with identical semantics.
- Validation is intentionally conservative: a mismatch is only reported
  when BOTH sides parse to a numeric value of the same kind and they
  clearly disagree.  Unparseable values (ICs, connectors, exotic value
  strings) are treated as "cannot validate" and accepted, so this guard
  never blocks enrichment of parts it does not understand.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..cost.suggest import ComponentType, parse_component_value

if TYPE_CHECKING:
    from ..parts.cache import PartsCache

logger = logging.getLogger(__name__)

# Relative tolerance for "same nominal value".  Adjacent E12/E24 series
# values differ by >= ~8%, so 5% cleanly separates rounding/formatting
# noise (0.1uF vs 100nF, 4.7k vs 4700) from genuinely different parts
# (16nF vs 100nF).
VALUE_REL_TOLERANCE = 0.05

# Component types whose values we know how to compare numerically.
_NUMERIC_TYPES = frozenset(
    {ComponentType.RESISTOR, ComponentType.CAPACITOR, ComponentType.INDUCTOR}
)

# SI multipliers for capacitor/inductor prefixes (case-insensitive use).
_CL_MULTIPLIERS = {
    "": 1.0,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
}

# Resistor multipliers are case-SENSITIVE in free text ("m" = milli,
# "M" = mega); "k"/"K" are both kilo.
_R_MULTIPLIERS = {
    "": 1.0,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}

# Patterns for finding a value token inside a free-text part description
# (e.g. "16V 100nF X7R ±10% 0402 MLCC").  The lookarounds reject tokens
# embedded in part numbers like "GRM155R71H104KE14".
_DESC_PATTERNS: dict[ComponentType, re.Pattern[str]] = {
    ComponentType.CAPACITOR: re.compile(
        r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*([pnuµμm]?)F(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
    ComponentType.INDUCTOR: re.compile(
        r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*([pnuµμm]?)H(?![A-Za-z0-9])",
        re.IGNORECASE,
    ),
    # No IGNORECASE: m/M distinction matters for resistors.
    ComponentType.RESISTOR: re.compile(
        r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*([mkKMG]?)\s*(?:Ω|[Oo]hms?)(?![A-Za-z0-9])"
    ),
}


@dataclass(frozen=True)
class ValueMismatch:
    """A detected disagreement between requested and candidate values."""

    requested_value: str  # BOM row value as written (e.g. "16nF")
    candidate_value: str  # candidate part's value as known (e.g. "100nF")
    requested_si: float  # requested value in SI units (F / H / ohm)
    candidate_si: float  # candidate value in SI units

    def describe(self) -> str:
        """Human-readable one-line description."""
        return (
            f"part value {self.candidate_value!r} does not match requested {self.requested_value!r}"
        )


def _parse_requested(value: str, reference: str) -> tuple[float, ComponentType] | None:
    """Parse the BOM-side requested value to (SI numeric, component type)."""
    parsed = parse_component_value(value, reference)
    if parsed.component_type not in _NUMERIC_TYPES:
        return None
    if parsed.numeric_value is None:
        return None
    return parsed.numeric_value, parsed.component_type


def _extract_from_description(
    description: str, component_type: ComponentType
) -> tuple[float, str] | None:
    """Find a value token of the given type inside a free-text description.

    Returns (SI numeric value, matched text) or None.
    """
    pattern = _DESC_PATTERNS.get(component_type)
    if pattern is None or not description:
        return None
    m = pattern.search(description)
    if m is None:
        return None
    num = float(m.group(1))
    prefix = m.group(2) or ""
    if component_type is ComponentType.RESISTOR:
        num *= _R_MULTIPLIERS.get(prefix, 1.0)
    else:
        num *= _CL_MULTIPLIERS.get(prefix.lower(), 1.0)
    return num, m.group(0).strip()


# EIA 3-digit capacitance code embedded in MLCC manufacturer part
# numbers, e.g. Samsung CL05B"104"K..., Murata GRM155R71H"104"KE14,
# TDK C1005X7R1H"104"K050BB: two significant digits + power-of-ten
# multiplier (in pF), immediately followed by an uppercase tolerance
# letter (J/K/M).  The tolerance-letter anchor and the no-leading-digit
# guard keep size codes like "0402"/"155"/"1005" from being misread.
_MLCC_MPN_CODE = re.compile(r"(?<![0-9])([1-9]\d)([0-6])(?=[JKM](?:[^a-z]|$))")


def _extract_from_capacitor_mpn(text: str) -> tuple[float, str] | None:
    """Decode an EIA capacitance code from an MLCC part number.

    Returns (SI farads, human-readable value string) or None.  This is
    the last-resort fallback for cache records that carry only the MPN
    (the actual #3590 poison record: C1525 cached with value='' and
    description='CL05B104KO5NNNC').
    """
    if not text:
        return None
    m = _MLCC_MPN_CODE.search(text)
    if m is None:
        return None
    picofarads = int(m.group(1)) * 10 ** int(m.group(2))
    farads = picofarads * 1e-12
    if farads >= 1e-6:
        human = f"{farads * 1e6:.3g}uF"
    elif farads >= 1e-9:
        human = f"{farads * 1e9:.3g}nF"
    else:
        human = f"{picofarads:g}pF"
    return farads, f"{human} (MPN code {m.group(1)}{m.group(2)})"


def find_value_mismatch(
    requested_value: str,
    reference: str,
    *,
    part_value: str = "",
    part_description: str = "",
    part_mfr: str = "",
) -> ValueMismatch | None:
    """Compare a BOM row's value against a candidate part's known value.

    Args:
        requested_value: The BOM row value (e.g. ``"16nF"``).
        reference: A reference designator from the row (e.g. ``"C10"``)
            used to determine the component type.
        part_value: The candidate part's parametric value field, if known.
        part_description: The candidate part's catalog description, used
            as a fallback when ``part_value`` is absent/unparseable.
        part_mfr: The candidate part's manufacturer part number, used as
            a last-resort fallback for capacitors (EIA code decoding).

    Returns:
        A :class:`ValueMismatch` when both sides parse numerically and
        clearly disagree, otherwise ``None`` (match OR cannot validate).
    """
    requested = _parse_requested(requested_value, reference)
    if requested is None:
        return None
    requested_si, component_type = requested

    candidate_si: float | None = None
    candidate_str = ""

    # Prefer the structured value field, parsed with the same parser
    # (and same reference hint) as the requested value.
    if part_value:
        parsed = parse_component_value(part_value, reference)
        if parsed.numeric_value is not None and parsed.component_type is component_type:
            candidate_si = parsed.numeric_value
            candidate_str = part_value

    # Fall back to scanning the catalog description.
    if candidate_si is None:
        extracted = _extract_from_description(part_description, component_type)
        if extracted is not None:
            candidate_si, candidate_str = extracted

    # Last resort for capacitors: decode the EIA code from the MPN
    # (covers sparse cache records that only carry the part number).
    if candidate_si is None and component_type is ComponentType.CAPACITOR:
        for text in (part_mfr, part_description):
            extracted = _extract_from_capacitor_mpn(text)
            if extracted is not None:
                candidate_si, candidate_str = extracted
                break

    if candidate_si is None:
        return None  # cannot validate -- accept

    if math.isclose(requested_si, candidate_si, rel_tol=VALUE_REL_TOLERANCE):
        return None

    return ValueMismatch(
        requested_value=requested_value,
        candidate_value=candidate_str,
        requested_si=requested_si,
        candidate_si=candidate_si,
    )


def check_lcsc_against_cache(
    cache: PartsCache | None,
    lcsc_part: str,
    requested_value: str,
    reference: str,
) -> ValueMismatch | None:
    """Validate an LCSC assignment against the local parts cache/DB.

    Looks the part up in the cache (ignoring expiry -- stale parametric
    data is still useful for detecting a 6x value disagreement) and
    compares its known value/description against the requested value.

    Returns:
        A :class:`ValueMismatch` when the cache knows the part and its
        value clearly disagrees; ``None`` when the part is unknown, the
        values agree, or validation is not possible.
    """
    if cache is None or not lcsc_part:
        return None
    try:
        part = cache.get(lcsc_part, ignore_expiry=True)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Parts cache lookup failed for %s: %s", lcsc_part, e)
        return None
    if part is None:
        return None
    return find_value_mismatch(
        requested_value,
        reference,
        part_value=part.value,
        part_description=part.description,
        part_mfr=part.mfr_part,
    )
