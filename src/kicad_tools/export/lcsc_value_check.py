"""
Parametric value and package validation for LCSC part assignments.

Guards against wrong-part LCSC assignments by comparing the BOM row's
requested value (and chip package, derived from the footprint) against
what is known about the candidate LCSC part (its parametric ``value`` /
``package`` fields, catalog description, and manufacturer part number).

Motivating defects:

- Issue #3590: the enrichment cache fallback assigned C1525 -- a 100nF
  0402 capacitor -- to a 16nF BOM row, and the bad assignment then
  self-perpetuated through the ``merge_lcsc`` CSV read-back on every
  subsequent export.
- Issue #3597: the same C1525 (an 0402 part) was assigned to 100nF rows
  with 0805 footprints -- the *value* matched, but JLCPCB would place a
  part half the size of the pads.  Package validation catches this.

Design notes:

- Value parsing is delegated to :func:`kicad_tools.cost.suggest.parse_component_value`
  (the canonical parser per the #3593 survey) so requested values and
  candidate part values are interpreted with identical semantics.
- BOM-side package extraction is delegated to
  :func:`kicad_tools.cost.suggest.extract_package_from_footprint`.
- Validation is intentionally conservative: a mismatch is only reported
  when BOTH sides parse to a numeric value (or both yield a recognized
  chip package) and they clearly disagree.  Unparseable values and
  unknown packages (ICs, connectors, exotic value strings) are treated
  as "cannot validate" and accepted, so this guard never blocks
  enrichment of parts it does not understand.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..cost.suggest import (
    ComponentType,
    extract_package_from_footprint,
    parse_component_value,
)

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


@dataclass(frozen=True)
class PackageMismatch:
    """A detected disagreement between footprint and part chip packages."""

    requested_package: str  # from the BOM footprint, e.g. "0805"
    candidate_package: str  # from the part record, e.g. "0402"
    candidate_source: str  # where the part-side package came from

    def describe(self) -> str:
        """Human-readable one-line description."""
        return (
            f"part package {self.candidate_package!r} ({self.candidate_source}) "
            f"does not match footprint package {self.requested_package!r}"
        )


# Union of the mismatch kinds this module can report.  Both expose
# ``describe()`` for logging/reporting.
LcscMismatch = ValueMismatch | PackageMismatch

# Imperial chip-size codes we know how to compare.  Package validation
# is restricted to these: two-terminal chip packages where a size
# disagreement is unambiguous (an 0402 part on 0805 pads is always
# wrong).  IC/connector package comparisons have far more naming
# variation and are out of scope (issue #3597).
CHIP_PACKAGES = frozenset(
    {"01005", "0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2220", "2512"}
)

# A chip-size token in free text ("16V 100nF X7R ±10% 0402 MLCC").
# The lookarounds reject tokens embedded in part numbers like
# "RC0805FR-0710KL" or "0805W8F1002T5E" -- those are handled by the
# dedicated MPN decoders below, which understand family conventions.
_DESC_CHIP_RE = re.compile(
    r"(?<![A-Za-z0-9.])(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)(?![A-Za-z0-9])"
)

# A chip-size token anywhere in a structured ``package`` field
# (LCSC parametric packages are usually exactly "0402", "0805", ...).
_PKG_FIELD_CHIP_RE = re.compile(r"(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)")

# ---------------------------------------------------------------------------
# MPN size-code decoders.
#
# Local cache records are frequently sparse -- empty ``package``, and a
# description that is just the MPN (the actual #3590/#3597 poison
# record: C1525 cached with package='' and description='CL05B104KO5NNNC').
# Chip-passive part numbers encode the size, per family convention.
# Patterns below were validated against the on-machine parts cache
# (Samsung CL*, Murata GRM/GCM*, TDK C<metric>*, Yageo CC/RC/AC/AF*,
# Ever Ohms CR*, UNI-ROYAL 0805W8*, Taiyo Yuden EMK/LMK*).  Unknown
# families simply decode to nothing (-> cannot validate -> accept).
# ---------------------------------------------------------------------------

# Samsung MLCC: CL05 = 0402, CL10 = 0603, CL21 = 0805, ...
_SAMSUNG_CL_RE = re.compile(r"^CL(\d{2})")
_SAMSUNG_CL_SIZES = {
    "03": "0201",
    "05": "0402",
    "10": "0603",
    "21": "0805",
    "31": "1206",
    "32": "1210",
}

# Murata MLCC (GRM/GCM/GJM): GRM15 = 0402, GRM18 = 0603, GRM21 = 0805, ...
_MURATA_G_M_RE = re.compile(r"^G[A-Z]M(\d{2})")
_MURATA_G_M_SIZES = {
    "03": "0201",
    "15": "0402",
    "18": "0603",
    "21": "0805",
    "31": "1206",
    "32": "1210",
    "55": "2220",
}

# TDK C-series MLCC encodes the METRIC size: C1005 = 0402, C2012 = 0805.
_TDK_C_RE = re.compile(r"^C(1005|1608|2012|3216|3225|4532|5750)(?=[A-Z])")
_METRIC_TO_IMPERIAL = {
    "1005": "0402",
    "1608": "0603",
    "2012": "0805",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5750": "2220",
}

# Taiyo Yuden MLCC ([ELU]MK + 3-digit metric short code): LMK212 = 0805.
_TAIYO_YUDEN_RE = re.compile(r"^[A-Z]MK(\d{3})")
_TAIYO_YUDEN_SIZES = {
    "105": "0402",
    "107": "0603",
    "212": "0805",
    "316": "1206",
    "325": "1210",
    "432": "1812",
}

# Literal imperial size embedded right after a 2-letter family prefix:
# Yageo CC0805.../RC0805FR..., Ever Ohms CR0402FF..., Yageo AC/AF....
_PREFIXED_CHIP_RE = re.compile(
    r"^[A-Z]{2}(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)(?=[A-Z])"
)

# Literal imperial size at the very start: UNI-ROYAL "0805W8F1002T5E",
# FH "0805F104M500NT".
_LEADING_CHIP_RE = re.compile(
    r"^(01005|0201|0402|0603|0805|1206|1210|1812|2010|2220|2512)(?=[A-Z])"
)


def _package_from_mpn(mpn: str) -> tuple[str, str] | None:
    """Decode an imperial chip-size code from a chip-passive MPN.

    Returns (imperial size, human-readable source) or None when the MPN
    does not follow a known family convention.
    """
    if not mpn:
        return None
    mpn = mpn.strip().upper()

    m = _SAMSUNG_CL_RE.match(mpn)
    if m is not None:
        size = _SAMSUNG_CL_SIZES.get(m.group(1))
        if size is not None:
            return size, f"MPN prefix CL{m.group(1)}"
        return None  # known family, unknown size code -- do not guess

    m = _MURATA_G_M_RE.match(mpn)
    if m is not None:
        size = _MURATA_G_M_SIZES.get(m.group(1))
        if size is not None:
            return size, f"MPN prefix {mpn[:3]}{m.group(1)}"
        return None

    m = _TDK_C_RE.match(mpn)
    if m is not None:
        return _METRIC_TO_IMPERIAL[m.group(1)], f"MPN metric size C{m.group(1)}"

    m = _TAIYO_YUDEN_RE.match(mpn)
    if m is not None:
        size = _TAIYO_YUDEN_SIZES.get(m.group(1))
        if size is not None:
            return size, f"MPN prefix {mpn[:3]}{m.group(1)}"
        return None

    m = _PREFIXED_CHIP_RE.match(mpn)
    if m is not None:
        return m.group(1), f"MPN size code in {mpn[:2]}{m.group(1)}"

    m = _LEADING_CHIP_RE.match(mpn)
    if m is not None:
        return m.group(1), f"MPN leading size code {m.group(1)}"

    return None


def _extract_part_chip_package(
    part_package: str,
    part_description: str,
    part_mfr: str,
    *,
    allow_mpn: bool,
) -> tuple[str, str] | None:
    """Determine the candidate part's chip package, if recognizable.

    Returns (imperial size, human-readable source) or None.  Sources in
    priority order: structured ``package`` field, free-text description
    token, MPN family decode (only when ``allow_mpn``; chip-passive MPN
    conventions do not generalize to LEDs/fuses/etc.).
    """
    if part_package:
        m = _PKG_FIELD_CHIP_RE.search(part_package)
        if m is not None:
            return m.group(1), "package field"

    if part_description:
        m = _DESC_CHIP_RE.search(part_description)
        if m is not None:
            return m.group(1), "description"

    if allow_mpn:
        for text in (part_mfr, part_description):
            decoded = _package_from_mpn(text)
            if decoded is not None:
                return decoded

    return None


def find_package_mismatch(
    footprint: str,
    reference: str,
    *,
    part_package: str = "",
    part_description: str = "",
    part_mfr: str = "",
) -> PackageMismatch | None:
    """Compare a BOM row's footprint package against a candidate part's.

    Args:
        footprint: The BOM row footprint (e.g.
            ``"Capacitor_SMD:C_0805_2012Metric"``).
        reference: A reference designator from the row (e.g. ``"C12"``).
            MPN-based package decoding is only trusted for chip passives
            (R/C/L references); other parts rely on the structured
            package field or description.
        part_package: The candidate part's parametric package field.
        part_description: The candidate part's catalog description.
        part_mfr: The candidate part's manufacturer part number.

    Returns:
        A :class:`PackageMismatch` when both sides yield a recognized
        imperial chip size and they differ, otherwise ``None`` (match OR
        cannot validate).
    """
    requested = extract_package_from_footprint(footprint)
    if requested not in CHIP_PACKAGES:
        return None  # not a chip footprint -- out of scope, accept

    # MPN size-code conventions are only reliable for chip passives.
    parsed = parse_component_value("", reference)
    allow_mpn = parsed.component_type in _NUMERIC_TYPES

    candidate = _extract_part_chip_package(
        part_package, part_description, part_mfr, allow_mpn=allow_mpn
    )
    if candidate is None:
        return None  # cannot validate -- accept
    candidate_package, source = candidate

    if candidate_package == requested:
        return None

    return PackageMismatch(
        requested_package=requested,
        candidate_package=candidate_package,
        candidate_source=source,
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
    *,
    footprint: str = "",
) -> LcscMismatch | None:
    """Validate an LCSC assignment against the local parts cache/DB.

    Looks the part up in the cache (ignoring expiry -- stale parametric
    data is still useful for detecting a 6x value disagreement) and
    compares its known value/description against the requested value.
    When ``footprint`` is provided, also compares the part's known chip
    package against the package extracted from the footprint (issue
    #3597: right value, wrong package -- C1525 0402 on 0805 pads).

    Returns:
        A :class:`ValueMismatch` or :class:`PackageMismatch` when the
        cache knows the part and it clearly disagrees; ``None`` when the
        part is unknown, both sides agree, or validation is not
        possible.
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
    mismatch: LcscMismatch | None = find_value_mismatch(
        requested_value,
        reference,
        part_value=part.value,
        part_description=part.description,
        part_mfr=part.mfr_part,
    )
    if mismatch is None and footprint:
        mismatch = find_package_mismatch(
            footprint,
            reference,
            part_package=part.package,
            part_description=part.description,
            part_mfr=part.mfr_part,
        )
    return mismatch
