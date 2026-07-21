"""Electrical-rating lint: LED overcurrent + capacitor voltage derating.

Advisory-only analyzer (issue #4381) that reads per-part electrical ratings
from **schematic symbol fields** and checks two deterministic, textbook
conditions:

* **LED overcurrent** -- for a single-LED / single-series-resistor branch the
  forward current is ``I = (V_rail - Vf) / R_series``. The branch ``FAIL``s
  when ``I > If_max``.
* **Capacitor voltage derating** -- a cap sitting across a power rail must be
  rated for the rail voltage plus a headroom margin. It ``FAIL``s when
  ``rated_V < V_rail * (1 + margin)``.

Spec source (v1, issue #4381): the ratings come from KiCad symbol fields read
via :meth:`SymbolInstance.get_property` -- ``Vf`` / ``If_max`` for LEDs and
``Voltage_Rating`` for capacitors (case-insensitive). A part that is missing
its rating field, or whose rail voltage cannot be inferred from the net name,
is **SKIPPED and census-counted -- never failed**. This keeps the analyzer at
zero false positives: silence on a part means "not enough data", surfaced in
the ``skipped`` count, not a clean bill of health.

Topology is extracted with
:func:`kicad_tools.operations.netlist.build_netlist_from_schematic`; field and
resistor values are parsed with
:func:`kicad_tools.spec.units.parse_unit_value`; the rail voltage is inferred
from the power-net *name* by :func:`infer_rail_voltage`, whose conventions are
informed by the rail-name regex prior art in
:mod:`kicad_tools.analysis.analog_detect`.

This module is advisory only: it **never raises** on malformed geometry, bad
field values, or un-inspectable data -- every failure path degrades to a SKIP
row with a human-readable reason (mirroring the advisory contract of
:mod:`kicad_tools.analysis.current_sense`).

Clean-room provenance (LICENSE-CRITICAL): the idea for these two checks was
surfaced by the open-sourced Pinscope project (github.com/Faradworks/Pinscope),
which is **AGPL-3.0**. kicad-tools is MIT. Only the *physics/algorithm*
(Ohm's-law LED current, a voltage-derating ratio -- both textbook and
unencumbered) is referenced here. **No Pinscope source, structure, identifiers,
or test data were read, ported, copied, or paraphrased.** This implementation
was written clean-room from the equations above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.operations.netlist import build_netlist_from_schematic
from kicad_tools.spec.units import parse_unit_value

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_tools.operations.netlist import Netlist
    from kicad_tools.schema.symbol import SymbolInstance

__all__ = [
    "DEFAULT_DERATE_MARGIN",
    "ElectricalRatingAnalyzer",
    "ElectricalRatingResult",
    "infer_rail_voltage",
]

# Default capacitor voltage-derating headroom. margin=0.2 means a cap on a 12V
# rail must be rated >= 12 * 1.2 = 14.4V. Documented, overridable parameter.
DEFAULT_DERATE_MARGIN = 0.2

# Status / check-kind string constants (kept as plain strings, mirroring the
# PASS/FAIL vocabulary of the sibling current-sense analyzer).
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

CHECK_LED = "led_overcurrent"
CHECK_CAP = "cap_derating"

# European rail notation (3V3 -> 3.3, 1V8 -> 1.8, 12V0 -> 12.0): digits, a 'V'
# separator, then fractional digits. Checked before the decimal form so the
# 'V' is read as the decimal point rather than a unit marker.
_RAIL_EURO_RE = re.compile(r"(\d+)V(\d+)", re.IGNORECASE)

# Decimal / integer volts with a trailing 'V' unit (+3.3V -> 3.3, +5V -> 5,
# +12V -> 12, +5VA analog -> 5). The optional analog/marker suffix after 'V'
# is ignored.
_RAIL_DECIMAL_RE = re.compile(r"(\d+(?:\.\d+)?)V", re.IGNORECASE)

# Named rails carrying an implied voltage by widely-used convention.
_NAMED_RAIL_VOLTAGES: dict[str, float] = {
    "VBUS": 5.0,  # USB bus voltage
}

# Resistor / LED / capacitor library-part recognition. We look at the part
# portion of the lib_id (after the last ':') plus the reference prefix so the
# checks fire on the dominant Device:* conventions without a hard dependency on
# a specific symbol library.
_LED_PART_HINTS = ("LED",)
_CAP_PARTS = frozenset({"C", "CP", "C_SMALL", "CP_SMALL", "C_POLARIZED", "C_POLARISED"})
_RES_PARTS = frozenset({"R", "R_SMALL", "R_US"})


def infer_rail_voltage(net_name: str | None) -> float | None:
    """Infer a rail's working voltage (V) from its net *name*.

    Deterministic name-based inference for conventional power-rail names:
    ``+3.3V`` / ``3V3`` -> 3.3, ``+5V`` -> 5.0, ``+12V`` -> 12.0,
    ``1V8`` -> 1.8, ``+5VA`` (analog) -> 5.0, ``VBUS`` -> 5.0. Names that
    carry no parseable voltage (``VCC``, ``VBAT``, ``+VIN``, ``GND``, and
    auto-generated ``Net-(...)`` names) return ``None`` -- the rail voltage is
    unknown, so the caller SKIPs rather than guesses.

    The magnitude is returned for negative rails (``-12V`` -> 12.0) since it is
    the stress the cap/LED sees. Never raises.
    """
    if not net_name:
        return None
    name = net_name.strip().lstrip("/")
    if not name:
        return None

    upper = name.upper()
    if upper in _NAMED_RAIL_VOLTAGES:
        return _NAMED_RAIL_VOLTAGES[upper]

    try:
        m = _RAIL_EURO_RE.search(name)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m = _RAIL_DECIMAL_RE.search(name)
        if m:
            return float(m.group(1))
    except (ValueError, TypeError):
        return None
    return None


@dataclass
class ElectricalRatingResult:
    """One census row for a checked (or skipped) electrical-rating part.

    Attributes:
        reference: Component reference designator (e.g. ``D1``, ``C3``).
        check: Which check produced this row -- ``"led_overcurrent"`` or
            ``"cap_derating"``.
        status: ``"PASS"``, ``"FAIL"``, or ``"SKIP"``. A SKIP means the part
            carried the intent (an LED / a capacitor) but lacked the data to
            judge it -- it is *never* a FAIL.
        rail_net: The power net whose voltage was used, or ``None``.
        rail_voltage_v: Inferred rail voltage (V), or ``None``.
        reason: Human-readable explanation for a SKIP (or an assumption note),
            or ``None`` for a plain PASS/FAIL.
        vf_v: LED forward voltage (V) used, or ``None``.
        if_max_a: LED rated forward current (A), or ``None``.
        r_series_ohms: Series resistance (ohms) used for the LED, or ``None``.
        series_ref: Reference of the series resistor, or ``None``.
        current_a: Computed LED forward current (A), or ``None``.
        rated_voltage_v: Capacitor rated working voltage (V), or ``None``.
        required_voltage_v: Minimum acceptable cap rating ``V_rail*(1+margin)``
            (V), or ``None``.
    """

    reference: str
    check: str
    status: str
    rail_net: str | None = None
    rail_voltage_v: float | None = None
    reason: str | None = None
    # LED-specific
    vf_v: float | None = None
    if_max_a: float | None = None
    r_series_ohms: float | None = None
    series_ref: str | None = None
    current_a: float | None = None
    # Cap-specific
    rated_voltage_v: float | None = None
    required_voltage_v: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict (mirrors sibling analyzers)."""

        def _r(v: float | None, ndigits: int = 4) -> float | None:
            return None if v is None else round(v, ndigits)

        out: dict[str, Any] = {
            "reference": self.reference,
            "check": self.check,
            "status": self.status,
            "rail_net": self.rail_net,
            "rail_voltage_v": _r(self.rail_voltage_v, 3),
        }
        if self.check == CHECK_LED:
            out["vf_v"] = _r(self.vf_v, 3)
            out["if_max_a"] = _r(self.if_max_a, 6)
            out["r_series_ohms"] = _r(self.r_series_ohms, 3)
            out["series_ref"] = self.series_ref
            out["current_a"] = _r(self.current_a, 6)
        else:
            out["rated_voltage_v"] = _r(self.rated_voltage_v, 3)
            out["required_voltage_v"] = _r(self.required_voltage_v, 3)
        if self.reason is not None:
            out["reason"] = self.reason
        return out


def _lib_part(lib_id: str) -> str:
    """Return the upper-cased part portion of a lib_id (after the last ':')."""
    return lib_id.split(":")[-1].upper() if lib_id else ""


def _is_led(sym: SymbolInstance) -> bool:
    part = _lib_part(sym.lib_id)
    if any(h in part for h in _LED_PART_HINTS):
        return True
    return sym.reference.upper().startswith("LED")


def _is_cap(sym: SymbolInstance) -> bool:
    part = _lib_part(sym.lib_id)
    return part in _CAP_PARTS or part.startswith("C_")


def _is_resistor(sym: SymbolInstance) -> bool:
    part = _lib_part(sym.lib_id)
    if part in _RES_PARTS or part.startswith("R_"):
        return True
    ref = sym.reference.upper()
    return bool(re.match(r"^R\d", ref))


def _get_field(sym: SymbolInstance, *names: str) -> str | None:
    """Case-insensitively fetch the first present symbol field value.

    ``SymbolInstance.get_property`` is exact-match; designers may write
    ``If_max`` / ``IF_MAX`` / ``if_max``, so we fall back to a case-insensitive
    scan of the symbol's properties.
    """
    for name in names:
        val = sym.get_property(name)
        if val is not None:
            return val
    wanted = {n.lower() for n in names}
    for prop_name, prop in sym.properties.items():
        if prop_name.lower() in wanted:
            return prop.value
    return None


def _parse_value(raw: str | None) -> float | None:
    """Parse a unit string to its base-unit float, or ``None`` on failure."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return float(parse_unit_value(text).value)
    except (ValueError, TypeError):
        return None


class ElectricalRatingAnalyzer:
    """LED-overcurrent + capacitor-derating lint over a schematic.

    Advisory only; never raises. Construct with an optional derating margin and
    a documented ``led_default_vf`` fallback (``None`` -> skip LEDs missing a
    ``Vf`` field, preserving zero false positives), then call :meth:`analyze`
    with a ``.kicad_sch`` path.
    """

    def __init__(
        self,
        derate_margin: float = DEFAULT_DERATE_MARGIN,
        led_default_vf: float | None = None,
    ) -> None:
        """Initialize the analyzer.

        Args:
            derate_margin: Capacitor headroom fraction. ``0.2`` requires a cap
                on a ``V`` rail to be rated ``>= V * 1.2``.
            led_default_vf: Fallback LED forward voltage (V) used only when a
                part has ``If_max`` but no ``Vf`` field. ``None`` (default)
                skips such parts instead, keeping zero false positives. When a
                default is supplied it is surfaced as an assumption in the row's
                ``reason``.
        """
        self.derate_margin = derate_margin
        self.led_default_vf = led_default_vf

    def analyze(self, sch_path: str | Path) -> list[ElectricalRatingResult]:
        """Return one census row per LED / capacitor candidate. Never raises."""
        try:
            netlist = build_netlist_from_schematic(sch_path)
        except Exception:
            return []

        try:
            from kicad_tools.schema.schematic import Schematic

            sch = Schematic.load(sch_path)
            symbols = [s for s in sch.symbols if s.reference and not s.reference.startswith("#")]
        except Exception:
            return []

        # Pre-index resistors for series-resistor lookup.
        resistors = {s.reference: s for s in symbols if _is_resistor(s)}

        results: list[ElectricalRatingResult] = []
        for sym in symbols:
            try:
                if _is_led(sym):
                    results.append(self._check_led(sym, netlist, resistors))
                elif _is_cap(sym):
                    results.append(self._check_cap(sym, netlist))
            except Exception:
                # Advisory contract: never let one part abort the census.
                results.append(
                    ElectricalRatingResult(
                        reference=sym.reference,
                        check=CHECK_LED if _is_led(sym) else CHECK_CAP,
                        status=STATUS_SKIP,
                        reason="analysis error",
                    )
                )
        results.sort(key=lambda r: (r.check, r.reference))
        return results

    # ------------------------------------------------------------------
    # Topology helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _pin_nets(netlist: Netlist, ref: str) -> list[str]:
        """Return the distinct net names touching a component, in pin order."""
        names: list[str] = []
        seen: set[str] = set()
        for net in netlist.nets:
            for node in net.nodes:
                if node.reference == ref and net.name not in seen:
                    seen.add(net.name)
                    names.append(net.name)
        return names

    # ------------------------------------------------------------------
    # Check 1: LED overcurrent
    # ------------------------------------------------------------------
    def _check_led(
        self,
        sym: SymbolInstance,
        netlist: Netlist,
        resistors: dict[str, SymbolInstance],
    ) -> ElectricalRatingResult:
        ref = sym.reference

        def skip(reason: str) -> ElectricalRatingResult:
            return ElectricalRatingResult(
                reference=ref, check=CHECK_LED, status=STATUS_SKIP, reason=reason
            )

        if_max = _parse_value(_get_field(sym, "If_max"))
        if _get_field(sym, "If_max") is None:
            return skip("no If_max field")
        if if_max is None or if_max <= 0:
            return skip("unparseable If_max field")

        # Resolve Vf: field value, else documented default, else skip.
        vf_raw = _get_field(sym, "Vf")
        assumption: str | None = None
        vf: float
        if vf_raw is None:
            if self.led_default_vf is None:
                return skip("no Vf field")
            vf = self.led_default_vf
            assumption = f"assumed Vf={vf:g}V (no Vf field)"
        else:
            parsed_vf = _parse_value(vf_raw)
            if parsed_vf is None:
                return skip("unparseable Vf field")
            vf = parsed_vf

        led_nets = self._pin_nets(netlist, ref)
        if len(led_nets) < 2:
            return skip("LED not connected on two nets")

        # Find the unique series resistor sharing a net with the LED.
        led_net_set = set(led_nets)
        candidates: list[tuple[str, SymbolInstance, list[str], str]] = []
        for r_ref, r_sym in resistors.items():
            r_nets = self._pin_nets(netlist, r_ref)
            shared = [n for n in r_nets if n in led_net_set]
            if shared:
                candidates.append((r_ref, r_sym, r_nets, shared[0]))
        if not candidates:
            return skip("no series resistor")
        if len(candidates) > 1:
            return skip("ambiguous topology (multiple series resistors)")

        r_ref, r_sym, r_nets, mid_net = candidates[0]
        r_series = _parse_value(r_sym.value)
        if r_series is None or r_series <= 0:
            return skip(f"unparseable series-resistor value ({r_ref}={r_sym.value!r})")

        # Rail is on the resistor's far pin (high-side R) or the LED's other
        # pin (low-side R). Prefer the resistor's far net.
        rail_net: str | None = None
        rail_v: float | None = None
        for name in [n for n in r_nets if n != mid_net]:
            v = infer_rail_voltage(name)
            if v is not None:
                rail_net, rail_v = name, v
                break
        if rail_v is None:
            for name in [n for n in led_nets if n != mid_net]:
                v = infer_rail_voltage(name)
                if v is not None:
                    rail_net, rail_v = name, v
                    break
        if rail_v is None:
            return skip("unknown rail voltage")

        current = (rail_v - vf) / r_series
        if current <= 0:
            # LED cannot conduct (Vf >= V_rail): not an overcurrent condition.
            note = "Vf >= V_rail (no forward conduction)"
            reason = f"{assumption}; {note}" if assumption else note
            return ElectricalRatingResult(
                reference=ref,
                check=CHECK_LED,
                status=STATUS_PASS,
                rail_net=rail_net,
                rail_voltage_v=rail_v,
                reason=reason,
                vf_v=vf,
                if_max_a=if_max,
                r_series_ohms=r_series,
                series_ref=r_ref,
                current_a=current,
            )

        status = STATUS_FAIL if current > if_max else STATUS_PASS
        return ElectricalRatingResult(
            reference=ref,
            check=CHECK_LED,
            status=status,
            rail_net=rail_net,
            rail_voltage_v=rail_v,
            reason=assumption,
            vf_v=vf,
            if_max_a=if_max,
            r_series_ohms=r_series,
            series_ref=r_ref,
            current_a=current,
        )

    # ------------------------------------------------------------------
    # Check 2: capacitor voltage derating
    # ------------------------------------------------------------------
    def _check_cap(self, sym: SymbolInstance, netlist: Netlist) -> ElectricalRatingResult:
        ref = sym.reference

        def skip(reason: str) -> ElectricalRatingResult:
            return ElectricalRatingResult(
                reference=ref, check=CHECK_CAP, status=STATUS_SKIP, reason=reason
            )

        rating_raw = _get_field(sym, "Voltage_Rating")
        if rating_raw is None:
            return skip("no Voltage_Rating field")
        rated_v = _parse_value(rating_raw)
        if rated_v is None or rated_v <= 0:
            return skip("unparseable Voltage_Rating field")

        # Infer the rail voltage from the cap's nets; pick the highest-stress
        # (max) parseable rail. Ground / unknown nets infer to None.
        rail_net: str | None = None
        rail_v: float | None = None
        for name in self._pin_nets(netlist, ref):
            v = infer_rail_voltage(name)
            if v is not None and (rail_v is None or v > rail_v):
                rail_net, rail_v = name, v
        if rail_v is None:
            return skip("unknown rail voltage")

        required = rail_v * (1.0 + self.derate_margin)
        status = STATUS_FAIL if rated_v < required else STATUS_PASS
        return ElectricalRatingResult(
            reference=ref,
            check=CHECK_CAP,
            status=status,
            rail_net=rail_net,
            rail_voltage_v=rail_v,
            rated_voltage_v=rated_v,
            required_voltage_v=required,
        )
