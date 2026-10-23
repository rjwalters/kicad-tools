"""Derived intra-pair skew budgets, in picoseconds (Issue #7892).

Step 1 of the operator ruling on #7883 (issuecomment-6370363844).  The
derivation and every source key cited below are in
``docs/research/diffpair-phase-skew-tolerance.md`` (the "note").

The rule
========

A skew ``dt`` between the two legs of a pair is a phase error
``dphi = 2 pi f dt`` at frequency ``f``; it converts ``|Scd21| = sin(dphi/2)``
of the differential signal to common mode (note §1).  The ruling fixes the
tolerance at **-30 dB of mode conversion** (Q-a), so::

    dphi_max = 2 asin(10^(-30/20)) = 3.62 deg
    dt_max   = dphi_max / (2 pi f_eval)

``f_eval`` (Q-b) is the edge knee ``0.35 / t_r,min`` where the protocol
publishes an edge floor (USB 2.0 FS/HS, LVDS), otherwise Nyquist (bit rate
/ 2); with both available the higher one is used, as in the note's script
(the knee is above Nyquist for every row that has one).  At Nyquist the
budget is exactly UI/50.

DDR strobes and clocks are a timing quantity, not an S/N one (Q-f): they keep
the sourced UG583 limit of 2 ps instead of the phase rule.

Severity (Q-c)
==============

A budget derived from the protocol's **maximum** rate is a ``warning``: it is
what the protocol allows, not what this board runs.  A class that declares
its rate (``NetClassRouting.data_rate_gbps``) turns the budget into an
``error``.  The DDR timing limit does not depend on the rate; it is an
``error`` when the protocol is declared explicitly (``pair_protocol``) or the
class declares a rate, and a ``warning`` when the protocol was only inferred
from a name.

Protocol identification (deterministic)
=======================================

:func:`identify_protocol` tries, in order, and stops at the first hit:

1. the class's explicit ``pair_protocol`` field;
2. the net-class **name**;
3. the **positive net's name**, then the negative net's.

A name is split into upper-case tokens on every non-alphanumeric character
(``MIPI_CSI_LANES`` -> ``MIPI``, ``CSI``, ``LANES``).  The rules of
:data:`NAME_RULES` are tried in their fixed order; a rule matches when a token
equals one of its words, or equals one once trailing digits are stripped
(``LVDS1`` -> ``LVDS``, ``DQS0`` -> ``DQS``).  The first matching rule wins,
so ``USB3_TX1`` is USB 3 (its rule precedes the USB 2 rule) and a name
matching no rule identifies nothing.  A pair with no identified protocol and
no declared rate gets no time budget; ``kct check`` then keeps its legacy
millimetre limit (protocol table or the generic 0.5 mm).

Per-layer delay and the via model
=================================

:func:`layer_delays` turns a stackup into a propagation delay per copper
layer, ``sqrt(eps_eff) / c``:

* outer layers: the Hammerstad-Jensen microstrip ``eps_eff`` at the class
  trace width (:class:`~kicad_tools.physics.transmission_line.TransmissionLine`);
* inner layers: a stripline is a TEM line, so ``eps_eff`` is the surrounding
  dielectric's ``eps_r`` (:meth:`Stackup.get_dielectric_constant`, the mean
  of the two sides);
* vias: the barrel runs through the board's dielectric, so its height (the
  same stackup span kicad-cli's length counts) is charged at the slowest
  inner-layer delay, or, on a board without inner layers, at
  ``sqrt(eps_r)`` of its densest dielectric.

A leg's delay is ``sum(length on layer x delay of layer) + via height x via
delay``; a pair's skew is the difference of its two legs' delays.

kicad-cli parity
================

kicad-cli 10.0.6 parses a ``skew (max 4ps)`` constraint but, without a
project tuning profile, enforces nothing (black-box probe, Issue #7892).  The
``.kicad_dru`` therefore keeps a millimetre rule: the budget divided by the
slowest layer delay (:attr:`LayerDelays.slowest`), which kicad-cli fails
whenever a pair's legs share a layer mix and ``kct check`` fails it.  A pair
whose legs run on layers of different speed can be length-matched and still
over budget; ``kct check`` reports that finding as kct-only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .constants import SPEED_OF_LIGHT

if TYPE_CHECKING:
    from .stackup import Stackup

#: The research note this module implements; every source key cites it.
NOTE = "docs/research/diffpair-phase-skew-tolerance.md"

#: Mode-conversion tolerance, |Scd21| at ``f_eval`` (ruling Q-a).
CONVERSION_DB = -30.0

#: Bandwidth of a single-pole response with 10-90 % rise time t_r: ln(9)/(2 pi) = 0.3497.
KNEE_COEFF = math.log(9.0) / (2.0 * math.pi)

#: Speed of light in mm/ps (0.2998).
C_MM_PER_PS = SPEED_OF_LIGHT * 1e3 / 1e12

#: Slack on the ps comparison (one femtosecond): summing many segment delays in
#: floating point must not fail a pair routed exactly to its budget.
SKEW_BUDGET_EPSILON_PS = 1e-3

#: FR-4 relative permittivity used when a stackup gives no dielectric at all.
_FALLBACK_EPS_R = 4.5


def dphi_max_rad(conversion_db: float = CONVERSION_DB) -> float:
    """Phase error (rad) that converts ``conversion_db`` dB to common mode."""
    return 2.0 * math.asin(10.0 ** (conversion_db / 20.0))


def scd21_db(dphi_rad: float) -> float:
    """Mode conversion ``20 log10 |sin(dphi/2)|`` of a phase error, in dB."""
    s = abs(math.sin(dphi_rad / 2.0))
    return 20.0 * math.log10(s) if s > 0 else float("-inf")


#: Delta-phi of the ruling, 3.62 deg.
DPHI_MAX_RAD = dphi_max_rad()


@dataclass(frozen=True)
class OperatingPoint:
    """A protocol's rate and fastest permitted edge, each with its source.

    ``timing_limit_ps`` marks a strobe/clock whose crossing point is a timing
    reference: the S/N framing does not set its limit (note §5.1).
    """

    key: str
    rate_gbps: float
    rate_source: str
    edge_floor_ps: float | None = None
    edge_source: str = ""
    timing_limit_ps: float | None = None
    timing_source: str = ""

    @property
    def sources(self) -> str:
        parts = [self.rate_source, self.edge_source, self.timing_source]
        return "; ".join(p for p in parts if p)


# Source keys ([USB2], [TI1], ...) are the note's "Sources" list.
USB2_FS = OperatingPoint(
    "USB2_FS",
    0.012,
    "[USB2] USB 2.0 full speed, 12 Mb/s",
    4000.0,
    "[USB2] §7.1.2.1: FS rise/fall 4-20 ns",
)
USB2_HS = OperatingPoint(
    "USB2_HS",
    0.48,
    "[USB2] USB 2.0 high speed, 480 Mb/s",
    500.0,
    "[USB2] §7.1.2.2: T_HSR/T_HSF >= 500 ps",
)
USB3_GEN1 = OperatingPoint("USB3_GEN1", 5.0, "[TI2] §2.1-2.4: USB 3.x Gen1 (SuperSpeed), 5 Gb/s")
USB3_GEN2 = OperatingPoint("USB3_GEN2", 10.0, "[TI2] §2.1-2.4: USB 3.2 Gen2, 10 Gb/s")
PCIE_GEN1 = OperatingPoint("PCIE_GEN1", 2.5, "[T] §7.1 / [TI2]: PCIe Gen1, 2.5 GT/s (1.25 GHz)")
PCIE_GEN2 = OperatingPoint("PCIE_GEN2", 5.0, "[T] §7.1: PCIe Gen2, 5 GT/s")
PCIE_GEN3 = OperatingPoint("PCIE_GEN3", 8.0, "[T] §7.1: PCIe Gen3, 8 GT/s")
MIPI_DPHY = OperatingPoint(
    "MIPI_DPHY", 2.5, "[TI1] App. A: D-PHY UI = 400 ps (2.5 Gb/s); faster lanes set data_rate_gbps"
)
HDMI_14 = OperatingPoint("HDMI_14", 3.4, "[TI2] §2.3: HDMI 1.4b TMDS, data up to 1.7 GHz")
HDMI_20 = OperatingPoint("HDMI_20", 6.0, "[TI2] §2.3: HDMI 2.0b TMDS, data up to 3 GHz")
LVDS = OperatingPoint(
    "LVDS",
    0.4,
    "[LV1] SN65LVDS1/2 link: 400 Mb/s",
    600.0,
    "[LV1] driver differential t_r typ 0.6 ns (no minimum published; typical used)",
)
DDR_DQS_CK = OperatingPoint(
    "DDR_DQS_CK",
    3.2,
    "DDR4-3200 (rate does not set this limit)",
    timing_limit_ps=2.0,
    timing_source="[X1] UG583: DQS_T/DQS_C and CK_T/CK_C 2 ps",
)

#: The protocol table: every operating point the note sources.
OPERATING_POINTS: dict[str, OperatingPoint] = {
    p.key: p
    for p in (
        USB2_FS,
        USB2_HS,
        USB3_GEN1,
        USB3_GEN2,
        PCIE_GEN1,
        PCIE_GEN2,
        PCIE_GEN3,
        MIPI_DPHY,
        HDMI_14,
        HDMI_20,
        LVDS,
        DDR_DQS_CK,
    )
}

#: ``PairProtocol`` name -> the operating point of its **maximum** rate (Q-c).
#: Protocols missing here (SATA, ETHERNET, CUSTOM) have no derived budget.
PROTOCOL_MAX_POINT: dict[str, OperatingPoint] = {
    "USB2_FS": USB2_FS,
    "USB2_HS": USB2_HS,
    "USB3_SS": USB3_GEN1,  # SuperSpeed; SuperSpeed+ (Gen2) sets data_rate_gbps=10
    "PCIE_G1": PCIE_GEN1,
    "PCIE_G2_PLUS": PCIE_GEN3,
    "MIPI_DPHY": MIPI_DPHY,
    "HDMI_TMDS": HDMI_20,
    "LVDS": LVDS,
    "DDR_DQS_CK": DDR_DQS_CK,
}

#: Name rules of :func:`identify_protocol`, tried in this order.
NAME_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("DDR_DQS_CK", frozenset({"DQS", "DDR"})),
    ("MIPI_DPHY", frozenset({"MIPI", "CSI", "DSI", "DPHY"})),
    ("HDMI_TMDS", frozenset({"HDMI", "TMDS"})),
    ("USB3_SS", frozenset({"USB3", "SSTX", "SSRX"})),
    ("PCIE_G2_PLUS", frozenset({"PCIE"})),
    ("USB2_HS", frozenset({"USB2", "USB"})),
    ("LVDS", frozenset({"LVDS"})),
)

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _normalise_protocol(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", value)
    return str(name).strip().upper().replace("-", "_") or None


def protocol_from_name(name: str | None) -> str | None:
    """The protocol a class or net name implies under :data:`NAME_RULES`, or ``None``."""
    if not name:
        return None
    tokens = [t.upper() for t in _TOKEN_SPLIT.split(name) if t]
    stripped = [t.rstrip("0123456789") for t in tokens]
    for protocol, words in NAME_RULES:
        if any(t in words for t in tokens) or any(t in words for t in stripped):
            return protocol
    return None


def identify_protocol(
    net_class: Any | None, p_name: str = "", n_name: str = ""
) -> tuple[str | None, str]:
    """``(protocol name, how)`` of a pair; ``how`` names the rule that matched.

    ``how`` is one of ``"class field"``, ``"class name"``, ``"pair name"`` or
    ``"none"``.  See the module docstring for the order.
    """
    explicit = _normalise_protocol(getattr(net_class, "pair_protocol", None))
    if explicit is not None:
        return explicit, "class field"
    by_class = protocol_from_name(str(getattr(net_class, "name", "") or ""))
    if by_class is not None:
        return by_class, "class name"
    for name in (p_name, n_name):
        by_net = protocol_from_name(name)
        if by_net is not None:
            return by_net, "pair name"
    return None, "none"


def evaluation_frequency_ghz(
    rate_gbps: float, edge_floor_ps: float | None = None
) -> tuple[float, str]:
    """``(f_eval in GHz, "knee" | "Nyquist")``: ``max(rate / 2, 0.35 / t_r,min)``."""
    if rate_gbps <= 0:
        raise ValueError(f"data rate must be positive, got {rate_gbps!r} Gb/s")
    nyquist = rate_gbps / 2.0
    knee = KNEE_COEFF / (edge_floor_ps * 1e-3) if edge_floor_ps else 0.0
    return (knee, "knee") if knee > nyquist else (nyquist, "Nyquist")


def phase_skew_budget_ps(
    rate_gbps: float,
    edge_floor_ps: float | None = None,
    conversion_db: float = CONVERSION_DB,
) -> float:
    """``dt_max = 2 asin(10^(T/20)) / (2 pi f_eval)`` in ps (``f_eval`` in GHz)."""
    f_eval, _basis = evaluation_frequency_ghz(rate_gbps, edge_floor_ps)
    return dphi_max_rad(conversion_db) / (2.0 * math.pi * f_eval) * 1e3


@dataclass(frozen=True)
class SkewBudget:
    """A pair's intra-pair skew budget in ps, with where it came from."""

    budget_ps: float
    severity: str
    protocol: str | None
    identified_by: str
    rate_gbps: float | None
    rate_declared: bool
    f_eval_ghz: float | None
    basis: str
    sources: str

    @property
    def provenance(self) -> str:
        """One line naming the rule, the operating point and the source."""
        if self.basis == "timing":
            head = f"timing:{self.protocol}"
        else:
            head = (
                f"derived:{CONVERSION_DB:g} dB at {self.basis} {self.f_eval_ghz:.3g} GHz "
                f"({self.protocol or 'declared rate'} {self.rate_gbps:g} Gb/s, "
                f"{'declared rate' if self.rate_declared else 'protocol max rate'})"
            )
        how = "" if self.identified_by in ("none", "class field") else f"; by {self.identified_by}"
        return f"{head}{how}; {self.sources}"


def skew_budget(
    protocol: str | None,
    data_rate_gbps: float | None = None,
    *,
    explicit_protocol: bool = True,
    identified_by: str = "class field",
) -> SkewBudget | None:
    """The budget of a protocol, optionally at a declared rate.

    ``None`` when there is nothing to derive from: no protocol in
    :data:`PROTOCOL_MAX_POINT` and no declared rate.
    """
    point = PROTOCOL_MAX_POINT.get(protocol) if protocol else None
    declared = data_rate_gbps is not None
    if point is None and not declared:
        return None
    if point is not None and point.timing_limit_ps is not None:
        return SkewBudget(
            budget_ps=point.timing_limit_ps,
            severity="error" if (explicit_protocol or declared) else "warning",
            protocol=protocol,
            identified_by=identified_by,
            rate_gbps=data_rate_gbps if declared else point.rate_gbps,
            rate_declared=declared,
            f_eval_ghz=None,
            basis="timing",
            sources=point.timing_source,
        )
    if data_rate_gbps is not None:
        rate = float(data_rate_gbps)
    else:
        assert point is not None  # guarded above: no point and no rate returns None
        rate = point.rate_gbps
    edge = point.edge_floor_ps if point is not None else None
    f_eval, basis = evaluation_frequency_ghz(rate, edge)
    if point is None:
        sources = "class data_rate_gbps"
    elif declared:
        sources = "class data_rate_gbps" + (f"; {point.edge_source}" if point.edge_source else "")
    else:
        sources = point.sources
    return SkewBudget(
        budget_ps=DPHI_MAX_RAD / (2.0 * math.pi * f_eval) * 1e3,
        severity="error" if declared else "warning",
        protocol=protocol if point is not None else None,
        identified_by="class data_rate_gbps" if point is None else identified_by,
        rate_gbps=rate,
        rate_declared=declared,
        f_eval_ghz=f_eval,
        basis=basis,
        sources=sources,
    )


def resolve_skew_budget(net_class: Any | None, p_name: str, n_name: str) -> SkewBudget | None:
    """The one skew-budget helper: a pair's budget in ps, or ``None``.

    ``net_class`` is the pair's ``NetClassRouting`` (read by attribute, so
    this module stays free of router imports); ``None`` still allows a
    name-identified protocol.
    """
    protocol, how = identify_protocol(net_class, p_name, n_name)
    rate = getattr(net_class, "data_rate_gbps", None) if net_class is not None else None
    return skew_budget(
        protocol,
        float(rate) if rate is not None else None,
        explicit_protocol=how == "class field",
        identified_by=how,
    )


# ---------------------------------------------------------------------------
# Per-layer propagation delay
# ---------------------------------------------------------------------------


def delay_ps_per_mm(eps_eff: float) -> float:
    """``sqrt(eps_eff) / c`` in ps/mm."""
    return math.sqrt(eps_eff) / C_MM_PER_PS


@dataclass(frozen=True)
class LayerDelays:
    """Propagation delay (ps/mm) of each copper layer and of a via barrel."""

    ps_per_mm: Mapping[str, float]
    via_ps_per_mm: float
    eps_eff: Mapping[str, float] = field(default_factory=dict)

    @property
    def slowest(self) -> float:
        """The largest delay of any layer or the via barrel (ps/mm)."""
        return max([*self.ps_per_mm.values(), self.via_ps_per_mm])

    def of(self, layer: str) -> float:
        """Delay of ``layer``; an unknown layer is charged the slowest delay."""
        return self.ps_per_mm.get(layer, self.slowest)

    def delay_ps(self, layer_mm: Mapping[str, float], via_mm: float = 0.0) -> float:
        """Delay of a leg with ``layer_mm`` of track per layer and ``via_mm`` of barrel."""
        total = 0.0
        for layer in sorted(layer_mm):
            total += layer_mm[layer] * self.of(layer)
        return total + via_mm * self.via_ps_per_mm

    def mm_equivalent(self, budget_ps: float) -> float:
        """The conservative length form of a budget: at the slowest delay."""
        return budget_ps / self.slowest


def layer_delays(stackup: Stackup, trace_width_mm: float = 0.2) -> LayerDelays:
    """Per-layer delays of ``stackup`` for a trace ``trace_width_mm`` wide."""
    from .transmission_line import TransmissionLine

    tl = TransmissionLine(stackup)
    eps: dict[str, float] = {}
    for layer in stackup.copper_layers:
        name = layer.name
        if stackup.is_outer_layer(name):
            try:
                eps[name] = tl.microstrip(trace_width_mm, name).epsilon_eff
            except (ValueError, ZeroDivisionError):
                eps[name] = stackup.get_dielectric_constant(name)
        else:
            eps[name] = stackup.get_dielectric_constant(name)
    inner = [eps[n.name] for n in stackup.copper_layers if not stackup.is_outer_layer(n.name)]
    if inner:
        via_eps = max(inner)
    else:
        dielectrics = [d.epsilon_r for d in stackup.dielectric_layers if d.epsilon_r > 0]
        via_eps = max(dielectrics) if dielectrics else _FALLBACK_EPS_R
    return LayerDelays(
        ps_per_mm={name: delay_ps_per_mm(e) for name, e in eps.items()},
        via_ps_per_mm=delay_ps_per_mm(via_eps),
        eps_eff=eps,
    )


__all__ = [
    "CONVERSION_DB",
    "C_MM_PER_PS",
    "DPHI_MAX_RAD",
    "KNEE_COEFF",
    "NAME_RULES",
    "OPERATING_POINTS",
    "PROTOCOL_MAX_POINT",
    "SKEW_BUDGET_EPSILON_PS",
    "LayerDelays",
    "OperatingPoint",
    "SkewBudget",
    "delay_ps_per_mm",
    "dphi_max_rad",
    "evaluation_frequency_ghz",
    "identify_protocol",
    "layer_delays",
    "phase_skew_budget_ps",
    "protocol_from_name",
    "resolve_skew_budget",
    "scd21_db",
    "skew_budget",
]
