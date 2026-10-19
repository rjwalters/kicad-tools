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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..cost.suggest import (
    ComponentType,
    extract_package_from_footprint,
    parse_component_value,
)

if TYPE_CHECKING:
    from ..parts.cache import PartsCache
    from ..parts.jlcparts_catalog import JlcpartsCatalog
    from ..parts.models import Part

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
        base = (
            f"part value {self.candidate_value!r} does not match requested {self.requested_value!r}"
        )
        # A NaN candidate SI marks a cross-type disagreement (e.g. a
        # resistor request against a capacitor description, issue #4128):
        # the two values are not numerically comparable, so flag the
        # wrong component type explicitly.
        if math.isnan(self.candidate_si):
            return f"{base} (different component type)"
        return base


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


def _description_dominant_type(
    description: str, request_type: ComponentType
) -> ComponentType | None:
    """Detect a *foreign* component type dominating a free-text description.

    Returns a :class:`ComponentType` other than ``request_type`` when the
    description clearly parses as that type's primary value AND carries no
    value token of ``request_type``'s own unit; otherwise ``None``.

    This backstops the cross-type gap behind #3590/#3597 (issue #4128):
    when a resistor-typed request (``330R``) is checked against a
    capacitor description (``"100nF ... Capacitor"``), the ohm extractor
    finds nothing -- previously read as "cannot validate, accept".  A
    description that unambiguously states *another* component's value is
    a different part, not an unvalidatable one, so it should reject.

    The rule is deliberately conservative to avoid false rejects on
    genuinely mixed-unit descriptions (RC networks, ferrite beads spec'd
    in Ω-at-MHz, etc.):

    - If the description matches ``request_type``'s own unit pattern, we
      do NOT report a foreign type -- the same-type extractor already
      handles it and any incidental foreign unit is not "dominant".
    - Only when exactly one foreign numeric type's unit pattern matches
      (and the request's own does not) is that type treated as dominant.
      An ambiguous description matching two foreign types resolves to
      ``None`` (cannot validate -- stay permissive).
    """
    if not description:
        return None

    request_pattern = _DESC_PATTERNS.get(request_type)
    if request_pattern is not None and request_pattern.search(description):
        # The description carries the request's own unit -- same-type
        # comparison applies; do not treat any co-occurring unit as a
        # dominating foreign type.
        return None

    foreign_types = [
        ctype
        for ctype, pattern in _DESC_PATTERNS.items()
        if ctype is not request_type and pattern.search(description)
    ]
    if len(foreign_types) != 1:
        # Zero foreign units -> genuinely cannot validate.
        # Multiple foreign units -> ambiguous; stay permissive.
        return None
    return foreign_types[0]


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
    # Resolve the requested component type up front.  The type can be
    # known even when the numeric value is not (e.g. "330R" parses as a
    # RESISTOR with no SI value under the canonical parser) -- and the
    # cross-type reject in issue #4128 only needs the type, since values
    # of different types are not numerically comparable anyway.
    parsed_request = parse_component_value(requested_value, reference)
    if parsed_request.component_type not in _NUMERIC_TYPES:
        return None
    component_type = parsed_request.component_type
    requested_si: float | None = parsed_request.numeric_value

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
        # No same-type value found.  Before accepting as "cannot
        # validate", check whether the description clearly states a
        # *different* component type's value (issue #4128: a 330R
        # resistor request validated against a "100nF ... Capacitor"
        # description).  A dominating foreign unit means the candidate is
        # a different part, so reject rather than accept.
        foreign_type = _description_dominant_type(part_description, component_type)
        if foreign_type is not None:
            foreign = _extract_from_description(part_description, foreign_type)
            candidate_str = foreign[1] if foreign is not None else foreign_type.value
            return ValueMismatch(
                requested_value=requested_value,
                candidate_value=candidate_str,
                requested_si=math.nan if requested_si is None else requested_si,
                candidate_si=math.nan,
            )
        return None  # cannot validate -- accept

    # We have a same-type candidate value.  A numeric comparison needs a
    # numeric request; if the request value did not parse numerically we
    # cannot compare magnitudes -- accept (same type, unvalidatable).
    if requested_si is None:
        return None

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
    # No IC check here: enrichment (bom_enrich/bom_formats) relies on this
    # function's chip-only package policy (issue #7166).
    return _compare_part(part, requested_value, reference, footprint, None, check_ic=False)


# ---------------------------------------------------------------------------
# IC pin-count / package-family validation (issue #7166).
#
# Preflight-only.  ``find_package_mismatch`` above stays chip-only because
# BOM enrichment (``bom_enrich.py``) uses it to reject candidates; widening
# it would change which parts enrichment picks for fleet boards.
# ---------------------------------------------------------------------------

# Each regex below maps a whole family *group* to one key: naming differs
# between KiCad footprints and LCSC ``encapStandard`` strings (``TQFP-44``
# vs ``QFP-44(10x10)``, ``SOT-23-5`` vs ``SOT-25-5``, ``DIP-8`` vs
# ``PDIP-8``, ``HVSSOP-8`` vs ``MSOP-8-EP``), and members of a group
# compare as equivalent.  Group keys: SOT23, SOT223, SO, QFP, QFN, DIP.
_SOT223_RE = re.compile(r"^SOT-?223(?:-(\d+))?(?![0-9])")
# SOT-23 / SOT-25 / SOT-26 with optional -N and a trailing L (``SOT-23-6L``).
_SOT23_RE = re.compile(r"^SOT-?(23|25|26)(?![0-9])(?:-(\d+)L?)?(?![0-9])")
_SOT23_DEFAULT_PINS = {"23": 3, "25": 5, "26": 6}
_SO_RE = re.compile(
    r"^(HTSSOP|HVSSOP|VSSOP|TSSOP|MSOP|SSOP|SOIC|SOP|SO|TSOP(?:-?II)?)-?(\d+)(?![0-9])"
)
_QFP_RE = re.compile(r"^(HTQFP|LQFP|TQFP|QFP)-?(\d+)(?![0-9])")
_QFN_RE = re.compile(r"^([A-Z]{0,2}QFN|[A-Z]{0,2}DFN)-?(\d+)(?![0-9])")
_DIP_RE = re.compile(r"^(PDIP|SPDIP|DIP)-?(\d+)(?![0-9])")
# Body size: ``(10x10)`` on LCSC strings, ``_10x10mm`` on KiCad footprints.
_BODY_RE = re.compile(r"[(_-](\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)(?:MM)?(?:\)|_|$|-)")
# Exposed pad / tab markers in a footprint name or part package string.
_EP_MARKER_RE = re.compile(r"(?<![A-Z])EP|POWERPAD|THERMAL|TAB")

BODY_TOLERANCE_MM = 0.25
PITCH_TOLERANCE_MM = 0.05

# Explicit pitch token in a KiCad footprint name (``..._P1.27mm``).
_PITCH_RE = re.compile(r"_P(\d+(?:\.\d+)?)MM")
# Lead pitch implied by the SO family name (issue #7180).  SSOP / TSOP are
# deliberately absent: their pitch varies (0.65/0.635/0.5 mm), so without an
# explicit ``_P`` token the pitch is unknown and never compared.
_SO_FAMILY_PITCH_MM = {
    "SOIC": 1.27,
    "SOP": 1.27,
    "SO": 1.27,
    "TSSOP": 0.65,
    "HTSSOP": 0.65,
    "MSOP": 0.65,
    "VSSOP": 0.65,
    "HVSSOP": 0.65,
}


# Pitches at or below this are the "fine" class (TSSOP/MSOP/VSSOP: 0.65 / 0.5 mm).
_FINE_PITCH_MAX_MM = 0.65 + PITCH_TOLERANCE_MM


def _pitches_conflict(a: IcPackage, b: IcPackage) -> bool:
    """True when two SO packages' lead pitches provably disagree.

    Two explicit ``_P`` tokens are compared exactly.  A family-name-derived
    pitch is only a class hint (LCSC strings such as ``MSOP-10`` never state
    pitch and real parts come in 0.5 mm as well as 0.65 mm), so then only the
    coarse (1.27 mm) vs fine (<= 0.65 mm) class is compared.
    """
    assert a.pitch is not None and b.pitch is not None
    if a.pitch_explicit and b.pitch_explicit:
        return abs(a.pitch - b.pitch) > PITCH_TOLERANCE_MM
    return (a.pitch <= _FINE_PITCH_MAX_MM) != (b.pitch <= _FINE_PITCH_MAX_MM)


@dataclass(frozen=True)
class IcPackage:
    """A recognized IC package: family, signal pin count, optional body."""

    family: str  # group key: SOT23 | SOT223 | SO | QFP | QFN | DIP
    pins: int
    body: tuple[float, float] | None = None
    exposed_pad: bool = False
    pitch: float | None = None  # lead pitch in mm; SO group only, None = unknown
    pitch_explicit: bool = False  # True when read from a ``_P`` token, not the family name


def parse_ic_package(text: str) -> IcPackage | None:
    """Parse an IC package family and pin count from a package/footprint name.

    Accepts LCSC ``encapStandard`` strings (``SOT-25-5``, ``QFP-44(10x10)``,
    ``HTSSOP-28``, ``MSOP-8-EP``, ``TSOP-54-10.2mm``, ``PDIP-8``) and KiCad
    footprint names with or without the library prefix
    (``Package_SO:SOIC-8_3.9x4.9mm_P1.27mm``, ``SOT-223-3_TabPin2``).

    Returns ``None`` for anything that is not a recognized IC family
    (connectors, switches, crystals, chip passives, ``SMD-4P``...), which
    callers treat as "cannot validate".
    """
    if not text:
        return None
    name = text.rsplit(":", 1)[-1].strip().upper().replace(" ", "")
    if not name:
        return None
    exposed = bool(_EP_MARKER_RE.search(name))

    m = _SOT223_RE.match(name)
    if m is not None:
        pins = int(m.group(1)) if m.group(1) else 3
        return IcPackage("SOT223", pins, None, True)  # the tab is an extra pad

    m = _SOT23_RE.match(name)
    if m is not None:
        pins = int(m.group(2)) if m.group(2) else _SOT23_DEFAULT_PINS[m.group(1)]
        return IcPackage("SOT23", pins, None, exposed)

    m = _SO_RE.match(name)
    if m is not None:
        family = m.group(1)
        pitch_m = _PITCH_RE.search(name, m.end())
        pitch = float(pitch_m.group(1)) if pitch_m else _SO_FAMILY_PITCH_MM.get(family)
        return IcPackage(
            "SO",
            int(m.group(2)),
            None,
            exposed or family in ("HTSSOP", "HVSSOP"),
            pitch,
            pitch_m is not None,
        )

    for regex, family in ((_QFP_RE, "QFP"), (_QFN_RE, "QFN")):
        m = regex.match(name)
        if m is not None:
            body_m = _BODY_RE.search(name, m.end())
            body = (float(body_m.group(1)), float(body_m.group(2))) if body_m else None
            return IcPackage(family, int(m.group(2)), body, exposed or family == "QFN")

    m = _DIP_RE.match(name)
    if m is not None:
        return IcPackage("DIP", int(m.group(2)), None, exposed)

    return None


def _numeric_pad_count(pad_numbers: Iterable[str]) -> int:
    """Count distinct numeric pad numbers.

    Drops ``MP`` mounting pads, unnumbered paste/thermal-via pads and
    duplicates (a SOT-223 tab numbered ``2`` like pin 2).
    """
    return len({str(n).strip() for n in pad_numbers if str(n).strip().isdigit()})


def find_ic_package_mismatch(
    footprint_name: str,
    pad_numbers: Iterable[str] | None,
    part_package: str,
) -> PackageMismatch | None:
    """Compare an IC part's package family/pin count with its footprint.

    Preflight-only companion of :func:`find_package_mismatch` (issue #7166).

    - The part side must parse to a recognized IC family
      (:func:`parse_ic_package`); otherwise the check cannot validate and
      accepts.
    - When ``pad_numbers`` are known (the PCB footprint's pads), the part's
      pin count must equal the number of distinct numeric pads, or that
      count minus one when the extra pad is an exposed pad / tab (marked in
      the footprint name or implied by the part package).
    - When the footprint name also parses, the families must belong to the
      same group, the pin counts must agree (if no pads were supplied), SO
      lead pitches must not conflict when both are known (SOIC 1.27 mm vs
      TSSOP/MSOP -- issue #7180; a pitch implied only by the family name
      is compared as coarse vs fine class, never 0.65 vs 0.5 mm), and QFP/QFN bodies must agree within :data:`BODY_TOLERANCE_MM` when both
      sides state one.

    Returns:
        A :class:`PackageMismatch` describing the disagreement, or ``None``
        (match OR cannot validate).
    """
    part = parse_ic_package(part_package)
    if part is None:
        return None
    footprint = parse_ic_package(footprint_name)
    fp_label = footprint_name.rsplit(":", 1)[-1] if footprint_name else "(unnamed footprint)"

    def mismatch(source: str) -> PackageMismatch:
        return PackageMismatch(
            requested_package=fp_label,
            candidate_package=part_package,
            candidate_source=source,
        )

    if footprint is not None and footprint.family != part.family:
        return mismatch(f"package family {part.family} vs footprint family {footprint.family}")

    if pad_numbers is not None:
        count = _numeric_pad_count(pad_numbers)
        if count:
            # The footprint name may mark an EP even when its family is
            # unparseable (``TI_PWP0028C_EP3.4x9.7..._ThermalVias``).
            exposed = part.exposed_pad or bool(_EP_MARKER_RE.search(fp_label.upper()))
            if part.pins != count and not (exposed and part.pins == count - 1):
                return mismatch(f"pin count {part.pins} vs {count} footprint pads")
    elif footprint is not None and footprint.pins != part.pins:
        return mismatch(f"pin count {part.pins} vs footprint {footprint.pins}")

    if (
        footprint is not None
        and part.family == "SO"
        and part.pitch is not None
        and footprint.pitch is not None
        and _pitches_conflict(part, footprint)
    ):
        return mismatch(f"pitch {part.pitch:g}mm vs footprint {footprint.pitch:g}mm")

    if (
        footprint is not None
        and part.family in ("QFP", "QFN")
        and part.body is not None
        and footprint.body is not None
    ):
        if any(
            abs(a - b) > BODY_TOLERANCE_MM for a, b in zip(part.body, footprint.body, strict=True)
        ):
            return mismatch(
                f"body {part.body[0]:g}x{part.body[1]:g}mm vs footprint "
                f"{footprint.body[0]:g}x{footprint.body[1]:g}mm"
            )
    return None


# ---------------------------------------------------------------------------
# Pin-header row x pin count (issue #7446).
# ---------------------------------------------------------------------------

_HEADER_FP_RE = re.compile(r"PIN(?:HEADER|SOCKET)_(\d+)X(\d+)")
# ``NxM`` / ``N*M`` / ``NxMP`` -- never a pitch/dimension (``2.54mm``, ``5.84mm``).
_HEADER_SHAPE_RE = re.compile(r"(?<![\d.])(\d+)\s*[xX*]\s*(\d+)P?(?![\d.]|\s*mm)")
# Single-token total pin count in an MPN: ``PZ254V-11-08P`` (not ``-2PZZ``).
_HEADER_MPN_COUNT_RE = re.compile(r"-(\d{1,3})P(?![A-Za-z0-9])")


def _part_header_count(
    part_description: str, part_mfr: str
) -> tuple[int, tuple[int, int] | None] | None:
    """Return ``(total pins, (rows, cols) or None)`` stated by an LCSC record."""
    for text in (part_description, part_mfr):
        m = _HEADER_SHAPE_RE.search(text or "")
        if m is not None:
            rows, cols = int(m.group(1)), int(m.group(2))
            if rows and cols:
                return rows * cols, (rows, cols)
    m = _HEADER_MPN_COUNT_RE.search(part_mfr or "")
    if m is not None and int(m.group(1)):
        return int(m.group(1)), None
    return None


def find_header_pin_mismatch(
    footprint_name: str,
    pad_numbers: Iterable[str] | None,
    part_package: str,
    part_description: str = "",
    part_mfr: str = "",
) -> PackageMismatch | None:
    """Compare a pin header/socket's stated pin count with its footprint.

    Preflight-only companion of :func:`find_ic_package_mismatch` (issue
    #7446): the IC guard ignores connectors, so a 1x2 header on a
    ``PinHeader_1x08`` footprint passed on pitch alone.

    The footprint must be named ``PinHeader_RxC`` / ``PinSocket_RxC`` and the
    part must state a count (``NxM`` in the description/MPN, or a ``-NNP`` MPN
    suffix meaning total pins).  A mismatch is a different total, a different
    ``NxM`` shape, or a numeric pad count that differs from the part's total.
    Anything unparseable returns ``None`` (cannot validate).
    """
    fp_label = footprint_name.rsplit(":", 1)[-1] if footprint_name else ""
    fm = _HEADER_FP_RE.search(fp_label.upper())
    if fm is None:
        return None
    stated = _part_header_count(part_description, part_mfr)
    if stated is None:
        return None
    total, shape = stated
    fp_rows, fp_cols = int(fm.group(1)), int(fm.group(2))

    def mismatch(source: str) -> PackageMismatch:
        return PackageMismatch(
            requested_package=fp_label,
            candidate_package=part_package,
            candidate_source=source,
        )

    if total != fp_rows * fp_cols or (
        shape is not None and sorted(shape) != sorted((fp_rows, fp_cols))
    ):
        label = f"{shape[0]}x{shape[1]}" if shape else str(total)
        return mismatch(f"pin count {label} vs footprint {fp_rows}x{fp_cols:02d}")
    if pad_numbers is not None:
        count = _numeric_pad_count(pad_numbers)
        if count and count != total:
            return mismatch(f"pin count {total} vs {count} footprint pads")
    return None


# ---------------------------------------------------------------------------
# Tri-state assignment verdict (issue #7166).
# ---------------------------------------------------------------------------

VerdictStatus = Literal["match", "mismatch", "unverified"]


@dataclass(frozen=True)
class LcscVerdict:
    """Outcome of validating one LCSC assignment.

    ``match``: the part is known (parts cache or offline catalog) and nothing
    we can compare disagrees.  ``mismatch``: a known part clearly disagrees
    (``mismatch`` set).  ``unverified``: the part is in neither source, so
    nothing was checked -- distinct from ``match``.
    """

    status: VerdictStatus
    source: str | None = None  # "parts cache" | "offline catalog"
    mismatch: LcscMismatch | None = None


def _compare_part(
    part: Part,
    requested_value: str,
    reference: str,
    footprint: str,
    pad_numbers: Iterable[str] | None,
    *,
    check_ic: bool,
) -> LcscMismatch | None:
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
    if mismatch is None and check_ic and (footprint or pad_numbers is not None):
        mismatch = find_ic_package_mismatch(footprint, pad_numbers, part.package)
        if mismatch is None:
            mismatch = find_header_pin_mismatch(
                footprint, pad_numbers, part.package, part.description, part.mfr_part
            )
    return mismatch


def verify_lcsc_assignment(
    cache: PartsCache | None,
    catalog: JlcpartsCatalog | None,
    lcsc_part: str,
    requested_value: str,
    reference: str,
    *,
    footprint: str = "",
    pad_numbers: Iterable[str] | None = None,
) -> LcscVerdict:
    """Validate an LCSC assignment, distinguishing "unknown" from "agrees".

    Read-only: looks the part up in the parts cache (ignoring expiry), then
    in the offline jlcparts catalog when the cache misses or its record has
    no ``package``.  Never writes to the cache and never touches the
    network.  Besides the value and chip-package checks of
    :func:`check_lcsc_against_cache`, runs :func:`find_ic_package_mismatch`.
    """
    if not lcsc_part:
        return LcscVerdict("unverified")

    candidates: list[tuple[Part, str]] = []
    if cache is not None:
        try:
            cached = cache.get(lcsc_part, ignore_expiry=True)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("Parts cache lookup failed for %s: %s", lcsc_part, e)
            cached = None
        if cached is not None:
            candidates.append((cached, "parts cache"))
    if catalog is not None and (not candidates or not candidates[0][0].package):
        try:
            listed = catalog.lookup(lcsc_part) if catalog.available else None
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("Offline catalog lookup failed for %s: %s", lcsc_part, e)
            listed = None
        if listed is not None:
            candidates.append((listed, "offline catalog"))

    if not candidates:
        return LcscVerdict("unverified")

    for part, source in candidates:
        mismatch = _compare_part(
            part, requested_value, reference, footprint, pad_numbers, check_ic=True
        )
        if mismatch is not None:
            return LcscVerdict("mismatch", source, mismatch)
    return LcscVerdict("match", candidates[-1][1])
