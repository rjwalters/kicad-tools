"""Protocol-aware differential-pair defaults -- the one table (Issue #6443).

Phase 1(a) of epic #6441.  Before this module two tables disagreed about the
same pair: ``DifferentialPairRules.for_type`` (``router/diffpair.py``) said a
USB2 pair may be 2.5 mm out of match, while ``NetClassRouting`` and the
``diffpair_length_skew`` rule said 0.5 mm.  Both now read
:data:`PROTOCOL_DEFAULTS`.

Every value is a :class:`Sourced` number: it carries the document it came
from, or the literal tag :data:`UNCITED`.  The tag matters -- a limit taken
from an ``uncited`` table value is reported at ``warning`` severity; a cited
table value, or a limit set explicitly on the class, is an ``error``.  A field
that is ``None`` means the table has **no** value for that protocol; the
generic default applies (skew) or no limit is resolved (uncoupled length, via
count).

The generic default (:data:`GENERIC_SKEW_MM`, for a class that declares no
protocol) has no source either, but in this piece it **stays an error**, so no
existing untagged class changes severity.  Whether it should warn too is an
open maintainer decision on #6443; this module does not settle it.

Resolution order, implemented once in
:func:`kicad_tools.validate.diffpair_engagement.resolve_pair_thresholds`::

    class field  >  protocol default  >  generic default

This module imports nothing from the router so both ``router/rules.py`` and
the validate package can depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum

#: Tag for a value nobody has traced to a published source.
UNCITED = "uncited"

#: Skew limit (mm) for a pair whose class declares no protocol and no explicit
#: ``skew_tolerance_mm``.  Unchanged from before #6443, so no untagged board is
#: loosened.  MUST equal ``DEFAULT_SKEW_TOLERANCE_MM`` in
#: ``validate/rules/diffpair_length_skew.py`` (a test asserts it).
GENERIC_SKEW_MM = 0.5

#: The intra-pair skew the P5 tuner (#6447) aims for on every protocol.  It is
#: reported next to the pass/fail limit as a stretch figure; it gates nothing.
SKEW_STRETCH_TARGET_MM = 0.5

_RENESAS_FS = "Renesas, Guidelines for Full-Speed USB2.0 Board Design, Table 2"
_TI_APP_A = "TI SPRAAR7J App. A"
_TI_BREAKOUT = "TI SPRAAR7J §3.3 (breakout within 0.25 in of the package)"
#: TI SPRAAR7J is written for a high-speed-capable USB2 PHY.  The full-speed
#: row borrows its via count and breakout distance, and its source strings say
#: so: piece (b) will turn these into limits on full-speed pairs.
_HS_GUIDANCE_ON_FS = "; high-speed guidance, applied to full speed"
_TI_APP_A_ON_FS = _TI_APP_A + _HS_GUIDANCE_ON_FS
_TI_BREAKOUT_ON_FS = _TI_BREAKOUT + _HS_GUIDANCE_ON_FS


class PairProtocol(Enum):
    """Signalling standard a differential pair is declared to carry."""

    USB2_FS = "USB2_FS"  # 12 Mbit/s
    USB2_HS = "USB2_HS"  # 480 Mbit/s
    USB3_SS = "USB3_SS"
    LVDS = "LVDS"
    PCIE_G1 = "PCIE_G1"
    PCIE_G2_PLUS = "PCIE_G2_PLUS"
    MIPI_DPHY = "MIPI_DPHY"
    ETHERNET = "ETHERNET"
    HDMI_TMDS = "HDMI_TMDS"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True)
class Sourced:
    """A number and where it came from (a citation, or :data:`UNCITED`)."""

    value: float
    source: str

    @property
    def cited(self) -> bool:
        return self.source != UNCITED


def _uncited(value: float) -> Sourced:
    return Sourced(value, UNCITED)


@dataclass(frozen=True)
class ProtocolDefaults:
    """Default pair rules of one protocol.  ``None`` = the table has no value."""

    protocol: PairProtocol
    max_skew_mm: Sourced | None = None
    """Intra-pair length mismatch per net, in mm."""
    max_uncoupled_per_terminal_mm: Sourced | None = None
    """Uncoupled copper allowed at each terminal, in mm."""
    max_vias_per_net: Sourced | None = None
    gap_mm: Sourced | None = None
    """Edge-to-edge P/N gap, in mm."""
    trace_width_mm: Sourced | None = None
    diff_impedance_ohm: Sourced | None = None
    impedance_tolerance_pct: Sourced | None = None

    def sourced_values(self) -> dict[str, Sourced]:
        """Every value this row actually carries, by field name."""
        out: dict[str, Sourced] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Sourced):
                out[f.name] = v
        return out


_P = PairProtocol

#: The single source of protocol defaults.  Values and sources are the epic
#: #6441 table plus the 2026-10-13 maintainer decisions on #6443 (USB2_FS skew
#: 2.5 mm, USB2_HS skew 1.27 mm).  Gap / width values are the former
#: ``DifferentialPairRules.for_type`` numbers; none of them has a source.
PROTOCOL_DEFAULTS: dict[PairProtocol, ProtocolDefaults] = {
    _P.USB2_FS: ProtocolDefaults(
        protocol=_P.USB2_FS,
        max_skew_mm=Sourced(2.5, _RENESAS_FS),
        max_uncoupled_per_terminal_mm=Sourced(6.35, _TI_BREAKOUT_ON_FS),
        max_vias_per_net=Sourced(4, _TI_APP_A_ON_FS),
        gap_mm=_uncited(0.2),
        trace_width_mm=_uncited(0.2),
        diff_impedance_ohm=Sourced(90.0, _RENESAS_FS),
        impedance_tolerance_pct=Sourced(15.0, _RENESAS_FS),
    ),
    _P.USB2_HS: ProtocolDefaults(
        protocol=_P.USB2_HS,
        max_skew_mm=Sourced(1.27, _TI_APP_A + " (50 mil)"),
        max_uncoupled_per_terminal_mm=Sourced(6.35, _TI_BREAKOUT),
        max_vias_per_net=Sourced(4, _TI_APP_A),
        gap_mm=_uncited(0.2),
        trace_width_mm=_uncited(0.2),
        diff_impedance_ohm=Sourced(90.0, _TI_APP_A),
        impedance_tolerance_pct=Sourced(10.0, _TI_APP_A),
    ),
    _P.USB3_SS: ProtocolDefaults(
        protocol=_P.USB3_SS,
        max_skew_mm=_uncited(0.1),
        gap_mm=_uncited(0.15),
        trace_width_mm=_uncited(0.2),
        diff_impedance_ohm=_uncited(90.0),
    ),
    _P.LVDS: ProtocolDefaults(
        protocol=_P.LVDS,
        max_skew_mm=_uncited(0.5),
        gap_mm=_uncited(0.15),
        trace_width_mm=_uncited(0.15),
        diff_impedance_ohm=_uncited(100.0),
    ),
    _P.PCIE_G1: ProtocolDefaults(
        protocol=_P.PCIE_G1,
        max_skew_mm=_uncited(0.127),
        diff_impedance_ohm=_uncited(85.0),
    ),
    # No value has been proposed for Gen 2+; the generic skew default applies.
    _P.PCIE_G2_PLUS: ProtocolDefaults(protocol=_P.PCIE_G2_PLUS),
    _P.MIPI_DPHY: ProtocolDefaults(
        protocol=_P.MIPI_DPHY,
        max_skew_mm=_uncited(0.038),
        diff_impedance_ohm=_uncited(100.0),
    ),
    _P.ETHERNET: ProtocolDefaults(
        protocol=_P.ETHERNET,
        max_skew_mm=_uncited(2.0),
        gap_mm=_uncited(0.2),
        trace_width_mm=_uncited(0.2),
        diff_impedance_ohm=_uncited(100.0),
    ),
    _P.HDMI_TMDS: ProtocolDefaults(
        protocol=_P.HDMI_TMDS,
        max_skew_mm=_uncited(0.5),
        gap_mm=_uncited(0.15),
        trace_width_mm=_uncited(0.2),
        diff_impedance_ohm=_uncited(100.0),
    ),
    # CUSTOM carries no skew: a pair of unknown protocol gets the generic default.
    _P.CUSTOM: ProtocolDefaults(
        protocol=_P.CUSTOM,
        gap_mm=_uncited(0.2),
        trace_width_mm=_uncited(0.2),
        diff_impedance_ohm=_uncited(90.0),
    ),
}


def parse_protocol(value: PairProtocol | str | None) -> PairProtocol | None:
    """``"usb2_fs"`` / ``PairProtocol.USB2_FS`` / ``None`` -> enum or ``None``.

    Raises:
        ValueError: ``value`` names no known protocol.  A misspelt protocol
            must not quietly fall back to the generic default.
    """
    if value is None or isinstance(value, PairProtocol):
        return value
    key = str(value).strip().upper().replace("-", "_")
    try:
        return PairProtocol[key]
    except KeyError:
        known = ", ".join(p.name for p in PairProtocol)
        raise ValueError(f"unknown pair protocol {value!r}; known: {known}") from None


def protocol_defaults(protocol: PairProtocol | str | None) -> ProtocolDefaults | None:
    """The table row of ``protocol`` (``None`` when no protocol is declared)."""
    parsed = parse_protocol(protocol)
    return PROTOCOL_DEFAULTS[parsed] if parsed is not None else None


def protocol_provenance(protocol: PairProtocol, value: Sourced) -> str:
    """Provenance string of a protocol value, e.g. ``protocol:USB2_FS (Renesas ...)``."""
    return f"protocol:{protocol.name} ({value.source})"


__all__ = [
    "GENERIC_SKEW_MM",
    "PROTOCOL_DEFAULTS",
    "SKEW_STRETCH_TARGET_MM",
    "UNCITED",
    "PairProtocol",
    "ProtocolDefaults",
    "Sourced",
    "parse_protocol",
    "protocol_defaults",
    "protocol_provenance",
]
