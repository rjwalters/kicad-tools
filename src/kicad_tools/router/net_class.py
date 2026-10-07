"""
Net class auto-detection from schematic symbols.

This module provides automatic net classification based on:
1. Symbol library properties (lib_id patterns)
2. Pin function analysis (electrical types)
3. Enhanced net name pattern matching
4. Signal path analysis (component connectivity)

The auto-detection reduces manual configuration by intelligently
classifying nets as power, clock, high-speed, analog, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schematic.models import Pin, Schematic, SymbolInstance

    from .rules import NetClassRouting


class NetClass(Enum):
    """Net classification types for automatic routing configuration.

    Each net class has different routing requirements:
    - POWER: Wide traces, solid zone connections, prefer inner layers
    - GROUND: Similar to power, highest zone priority
    - HIGH_CURRENT_SIGNAL: High-current outputs (motor phases, coil drives)
      that need POWER-tier routing priority but are NOT poured (point-to-
      point traces preserve switching-edge integrity).
    - CLOCK: Length-critical, controlled impedance, avoid noise coupling
    - HIGH_SPEED: Impedance-controlled, length matching, inner layers preferred
    - DIFFERENTIAL: Must be routed as pairs, tight length matching
    - ANALOG: Noise-sensitive, avoid crossing digital signals
    - RF: Impedance-controlled, short traces, proper termination
    - DEBUG: Low priority, can route last
    - SIGNAL: Default digital signals
    """

    POWER = "power"
    GROUND = "ground"
    HIGH_CURRENT_SIGNAL = "high_current_signal"
    CLOCK = "clock"
    HIGH_SPEED = "high_speed"
    DIFFERENTIAL = "differential"
    ANALOG = "analog"
    RF = "rf"
    DEBUG = "debug"
    SIGNAL = "signal"  # Default


# =============================================================================
# SYMBOL LIBRARY INDICATORS
# =============================================================================

# Maps lib_id patterns to net classes
# Pattern can use * for wildcards
SYMBOL_INDICATORS: dict[str, NetClass] = {
    # Power symbols and regulators
    "power:*": NetClass.POWER,
    "Device:Ferrite*": NetClass.POWER,
    "Regulator_Linear:*": NetClass.POWER,
    "Regulator_Switching:*": NetClass.POWER,
    # Clock and oscillator components
    "Device:Crystal*": NetClass.CLOCK,
    "Device:Resonator*": NetClass.CLOCK,
    "Oscillator:*": NetClass.CLOCK,
    "Timer:*": NetClass.CLOCK,
    # High-speed interfaces
    "Connector:USB*": NetClass.HIGH_SPEED,
    "Connector_USB:*": NetClass.HIGH_SPEED,
    "Interface:FT232*": NetClass.HIGH_SPEED,
    "Interface:FT2232*": NetClass.HIGH_SPEED,
    "Interface:CH340*": NetClass.HIGH_SPEED,
    "Interface:CP210*": NetClass.HIGH_SPEED,
    "Interface_USB:*": NetClass.HIGH_SPEED,
    "Interface_Ethernet:*": NetClass.HIGH_SPEED,
    "Interface_HDMI:*": NetClass.HIGH_SPEED,
    "Memory_Flash:*": NetClass.HIGH_SPEED,
    "Memory_RAM:*": NetClass.HIGH_SPEED,
    # RF components
    "RF_Module:*": NetClass.RF,
    "RF_Amplifier:*": NetClass.RF,
    "RF_Filter:*": NetClass.RF,
    "RF_Mixer:*": NetClass.RF,
    "RF_Switch:*": NetClass.RF,
    # Analog components
    "Amplifier_Operational:*": NetClass.ANALOG,
    "Amplifier_Audio:*": NetClass.ANALOG,
    "Amplifier_Instrumentation:*": NetClass.ANALOG,
    "Reference_Voltage:*": NetClass.ANALOG,
    "Sensor:*": NetClass.ANALOG,
    "Sensor_Temperature:*": NetClass.ANALOG,
    "Sensor_Pressure:*": NetClass.ANALOG,
    "Sensor_Humidity:*": NetClass.ANALOG,
    "Analog_ADC:*": NetClass.ANALOG,
    "Analog_DAC:*": NetClass.ANALOG,
    "Analog:*": NetClass.ANALOG,
    "Audio:*": NetClass.ANALOG,
    # Debug interfaces
    "Connector:Conn_ARM_JTAG*": NetClass.DEBUG,
    "Connector:Conn_ARM_SWD*": NetClass.DEBUG,
    "Connector_Debug:*": NetClass.DEBUG,
}


# =============================================================================
# NET NAME PATTERNS
# =============================================================================

# Comprehensive patterns for net name classification
NET_CLASS_PATTERNS: dict[NetClass, list[str]] = {
    NetClass.POWER: [
        # Issue #3513: PWR(?!_?LED) / POWER(?!_?LED) -- indicator nets
        # like PWR_LED are 2-pad signals (LED cathode -> resistor), not
        # rails.  Classifying them POWER made auto-pour emit a thin,
        # geometrically-unfillable zone and auto-skip routing the net,
        # shipping an open circuit on board 05.
        r"^(VCC|VDD|VBUS|VIN|VOUT|PWR(?!_?LED)|POWER(?!_?LED)|AVDD|DVDD)",
        r"^[+-]?\d+\.?\d*V[ADPS]?$",  # +3.3V, -5V, 1.8VA, 3.3VD
        r"^[+-]?\d+V\d+[ADPS]?$",  # +3V3, +5V0, +1V8, +12V0, -5V0 (no-decimal)
        r"^(PVDD|PVCC|VBAT|VCORE|VCAP|VIO)$",
        r"_VCC$|_VDD$|_PWR$",
        r"^V\d+",  # V5, V3.3, etc.
        r"^(VMOTOR|VMOT|VMAIN|VPWR|VDRIVE|VACT|VSRV)$",  # Motor/actuator power
    ],
    NetClass.GROUND: [
        r"^(GND|VSS|GNDA|GNDD|AGND|DGND|PGND|SGND|GROUND|CGND)$",
        r"^(CHASSIS|EARTH|SHIELD)$",
        r"_GND$|_VSS$|_AGND$|_DGND$",
    ],
    # High-current outputs that should route at POWER priority but NOT
    # be poured.  Examples: BLDC motor phases (PHASE_A/B/C), stepper coil
    # drives (COIL_A/B), solenoid/relay returns, generic MOTOR_* nets.
    # These nets carry switching currents and benefit from early routing
    # access to wide corridors, but pouring them as a copper plane would
    # couple noise into adjacent traces and destroy the point-to-point
    # nature of the FET-output -> motor-pin path.
    NetClass.HIGH_CURRENT_SIGNAL: [
        r"^PHASE_?[A-Z0-9]+$",  # PHASE_A, PHASE_B, PHASE1
        r"^MOTOR_?[A-Z0-9]+$",  # MOTOR_A, MOTOR1, MOTOR_PWM
        r"^COIL_?[A-Z0-9]+$",  # COIL_A, COIL1
        r"^STATOR_?[A-Z0-9]+$",
        r"^ROTOR_?[A-Z0-9]+$",
        r"^SOLENOID_?[A-Z0-9]*$",
        r"^RELAY_?[A-Z0-9]*$",
    ],
    NetClass.CLOCK: [
        r"(CLK|CLOCK|MCLK|SCLK|PCLK|BCLK|LRCLK|FCLK|SYSCLK)",
        r"(OSC|XTAL|CRYSTAL|XIN|XOUT)",
        r"_CLK$|_SCK$",
        r"^(TCK|TCLK|JTCK)$",  # JTAG clock
    ],
    NetClass.HIGH_SPEED: [
        r"(USB|ETH|HDMI|LVDS|PCIE|SDIO|QSPI|OSPI)",
        r"(MIPI|DSI|CSI|RGMII|RMII|MII)",
        r"(SATA|SAS|DP|DISPLAYPORT)",
        r"[_-](DP|DM|D[+-])$",  # USB D+/D-
        r"(SD_D|SDIO_D|MMC_D)\d",  # SD/MMC data lines
        r"(QSPI_D|OSPI_D)\d",  # Quad SPI data
    ],
    NetClass.DIFFERENTIAL: [
        r"[_-]?[PN]$",  # _P, _N suffixes
        r"[+-]$",  # +/- suffixes
        r"_DIFF[PN]?$",
        r"(TX|RX)[PN]$",  # Differential TX/RX pairs
        r"(CLK|DATA)[PN]$",  # Differential clock/data
    ],
    NetClass.ANALOG: [
        r"(AIN|AOUT|SENSE|FB|COMP|ISET)",
        r"^VREF",  # Reference voltage
        r"^AN\d+",  # AN0, AN1, etc.
        r"(ADC|DAC)_?(CH)?\d*$",
        r"(AUDIO|MIC|SPK|LINE)(_[LRIO])?",
        r"(I2S|TDM|PDM)_(DIN|DOUT|SD|WS)",
    ],
    NetClass.RF: [
        r"(RF_|ANT_|ANTENNA)",
        r"(LNA|PA)_",  # Low noise amp, power amp
        r"^(RF|ANT|ANTENNA)\d*$",
        r"(TX_RF|RX_RF)",
    ],
    NetClass.DEBUG: [
        r"(SWDIO|SWCLK|SWDCLK|SWO)",
        r"(NRST|RESET|RST)",
        r"(TDI|TDO|TMS|TCK|TRST)",  # JTAG
        r"(DEBUG|DBG|TRACE)",
        r"(BOOT|PROG)",
    ],
}


# =============================================================================
# SUPPLY-RAIL NAME HEURISTICS (issue #5939)
# =============================================================================
#
# ``is_power_rail_name`` / ``is_ground_rail_name`` are the shared *name-only*
# fallback for "is this net a supply rail?".  They exist because several
# callers (``explain/mistakes.py`` most visibly) used substring matching --
# ``"+" in name`` or ``"5V" in name`` -- which classified ``USB_D+``,
# ``ISENSE_A+``, ``/PG_3V3``, ``TRIGGER_5V`` and ``unconnected-(U11-D+-Pad2)``
# as power rails and produced false decoupling / trace-width findings.
#
# Rules, all anchored to whole tokens of the sheet-local name:
#
# * the hierarchical sheet path is stripped first (``/+5V`` -> ``+5V``);
# * KiCad's auto-generated ``unconnected-(...)`` and ``Net-(...)`` names are
#   never rails, whatever pin names they embed;
# * a voltage token must *lead* the name (``+3V3``, ``3.3V``, ``3V3_MCU``,
#   ``12V_IN``), optionally behind a ``P``/``V`` prefix (``P3V3``, ``V5V0``,
#   ``V3P3``), or the name must lead with ``+`` (``+BATT``, ``+5V_USB``); a
#   voltage fragment inside a signal name (``PG_3V3``, ``TRIGGER_5V``) is
#   not a rail;
# * rail keywords match as a leading word -- ``VDD_CORE``, ``VCCIO``,
#   ``VBUS_FUSED``, ``VOUT_PRE`` -- or as a trailing word (``SENSOR_VDD``,
#   ``USB_VBUS``);  ``PWR``/``POWER`` only as the whole name, because
#   ``PWR_LED``/``POWER_GOOD`` are signals;
# * a qualifier word that names a *signal derived from* a rail -- ``_SENSE``,
#   ``_DET``, ``_EN``, ``_FB``, ``_PG`` ... (:data:`_SIGNAL_QUALIFIERS`) --
#   makes the name a signal: ``VBUS_DET``, ``VIN_SENSE``, ``3V3_EN``.
#   ``+``-led names are exempt (``+`` is KiCad's power-symbol convention).
#
# Regulator compensation pins (``VCAP1``, ``VREG_1V2``, ``UCAP``) are
# deliberately *not* rails: they carry no load current, and counting them
# made the decoupling/trace-width checks fire on STM32 ``VCAP`` nets.
#
# Callers that have real evidence -- a pad/pin of electrical type
# ``power_in``/``power_out`` (see :data:`POWER_PIN_TYPES`) or a power symbol
# on the net -- should prefer it and use these only as the fallback.

#: Pin electrical types that mark a net as a supply rail.
POWER_PIN_TYPES: frozenset[str] = frozenset({"power_in", "power_out"})

# --- Switching-regulator pins (issue #5998) ---------------------------------
#
# KiCad's libraries type a buck/boost converter's switch-node pin as
# ``power_out`` (TPS54302 ``SW``, many ``LX`` pins), so "touches a
# ``power_in``/``power_out`` pin" alone classifies the switch node as a supply
# rail -- and ``kct detect-mistakes`` then asks for a decoupling capacitor
# from the switch node to ground, which would short the converter's output
# stage through the cap every cycle.  The bootstrap pin (``BOOT``/``BST``)
# rides on top of the switch node and is not a rail either.
#
# Names are matched as whole pin/net names (after the sheet path), so the
# STM32's ``PH0``/``PH1`` port pins and ``BOOT0`` are *not* switch-node pins.

#: Switch-node pin/net names: SW, SW1, SW_2, LX, LX1, PH, PHASE, SWN, SW_NODE.
_SWITCH_NODE_NAME_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(?:SW|LX|PHASE|SWN)(?:_?\d+|[A-D])?$",
        # TI's "PH" (phase) pin, bare only: PH0..PH15 are STM32 port-H pins.
        r"^PH$",
        r"^SW_?NODE\d*$",
        # Trailing word on a net name: BUCK_SW, VREG_LX, U2_SW1
        r"_(?:SW|LX|PHASE|SW_?NODE)\d*$",
    )
)

#: Bootstrap pin names: BOOT, BST, BS, CB, CBOOT.  Numbered forms are not
#: matched -- ``BOOT0``/``BOOT1`` are MCU strap pins, which often sit on a
#: rail.
_BOOTSTRAP_PIN_NAME_RE = re.compile(r"^(?:BOOT|BST|BS|CB|CBOOT|BOOTSTRAP)$", re.IGNORECASE)


def is_switch_node_name(name: str) -> bool:
    """Whole-name match for a switching regulator's switch node.

    Applies to both pin names (``pinfunction``: ``SW``, ``LX``, ``PH``,
    ``PHASE``, ``SW1``) and net names (``SW``, ``/SW``, ``BUCK_SW``,
    ``SW_NODE``).  ``PH0`` (an MCU port pin), ``SWDIO``/``SWCLK`` and
    ``SW_3V3`` (a load-switched rail) do not match.
    """
    base = _rail_base_name(name)
    if base is None:
        return False
    return any(rx.search(base) for rx in _SWITCH_NODE_NAME_RES)


def is_switching_regulator_pin(pin_name: str) -> bool:
    """True for a switch-node or bootstrap pin name (``SW``, ``LX``, ``BOOT``).

    Such a pin is never evidence that its net is a supply rail, whatever its
    electrical type: KiCad types switch-node pins ``power_out``.
    """
    if not pin_name:
        return False
    if is_switch_node_name(pin_name):
        return True
    return bool(_BOOTSTRAP_PIN_NAME_RE.match(pin_name.strip()))


def is_power_rail_pin(pintype: str, pin_name: str = "") -> bool:
    """Is this pin *evidence* that its net is a supply rail?

    Combines the electrical type (``power_in``/``power_out``, see
    :data:`POWER_PIN_TYPES`) with the pin name: a switch-node or bootstrap
    pin (:func:`is_switching_regulator_pin`) is not evidence even when typed
    ``power_out``.
    """
    return pintype in POWER_PIN_TYPES and not is_switching_regulator_pin(pin_name)


_AUTO_NET_NAME_RE = re.compile(r"^(unconnected|net)-\(", re.IGNORECASE)

_VOLTAGE = r"[+-]?\d+(?:\.\d+)?V\d*"
# Unsigned voltage token for the P-/V-prefixed forms: 3V3, 5V0, 12V, 3P3.
_PREFIXED_VOLTAGE = r"\d+(?:(?:\.\d+)?V\d*|P\d+)"
# Zero or more ``_WORD`` qualifiers: 3V3_MCU, VDD_CORE, VOUT_PRE.
_QUALIFIERS = r"(?:_[A-Z0-9.]+)*"

#: Qualifier words that turn a rail-ish name into a *signal* derived from
#: the rail: ``VBUS_DET``, ``VIN_SENSE``, ``3V3_EN``, ``VOUT_FB``.
_SIGNAL_QUALIFIERS: frozenset[str] = frozenset(
    {
        "SENSE", "SNS", "SEN", "DET", "DETECT", "EN", "ENA", "ENABLE",
        "FB", "PG", "PGOOD", "GOOD", "POK", "OK", "FAULT", "FLT", "ALERT",
        "MON", "ADC", "DIV", "CTRL", "CTL", "SET", "ADJ", "ON", "OFF",
        "REF", "IRQ", "INT", "SEL", "KELVIN", "OVP", "UVP", "OCP",
        "STATUS", "STAT",
    }
)  # fmt: skip

#: For the VCC/VDD/VEE supply families a trailing word usually names what the
#: supply *feeds* (``VDD_ADC``, ``AVDD_ADC``, ``VCC_REF``, ``VDD_MON``), not a
#: signal derived from it, so only these control/telemetry words make such a
#: name a signal (``VDD_EN``, ``VCC_PG``, ``VDD_SENSE``).
_SUPPLY_FAMILY_SIGNAL_QUALIFIERS: frozenset[str] = _SIGNAL_QUALIFIERS - {
    "ADC", "REF", "MON", "DIV", "SET", "ADJ", "SEL", "INT",
}  # fmt: skip

_SUPPLY_FAMILY_RE = re.compile(r"^(?:A|D|P|IO)?V(?:CC|DD|EE)", re.IGNORECASE)

_POWER_RAIL_NAME_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Leading voltage token, optionally qualified: +3V3, 3.3V, -12V,
        # +1V8, 3V3A, +5VD, 3V3_MCU, 5V_USB, 12V_IN
        rf"^{_VOLTAGE}[ADPS]?{_QUALIFIERS}$",
        # P-/V-prefixed voltage: P3V3, P5V, P12V, V3V3, V5V0, V3P3, V1P8
        rf"^[PV]{_PREFIXED_VOLTAGE}[ADPS]?{_QUALIFIERS}$",
        # Leading '+' is KiCad's power-symbol convention: +BATT, +5V_USB
        r"^\+[A-Z0-9][A-Z0-9_.]*$",
        # VCC / VDD families as a leading word: VCC, VCCIO, VDDA, VDD_CORE,
        # AVDD, DVDD, PVDD, IOVDD, AVCC, VEE
        rf"^(?:A|D|P|IO)?V(?:CC|DD|EE)[A-Z0-9]*{_QUALIFIERS}$",
        # Named rails as the leading word (optionally numbered/qualified):
        # VBUS, VBUS_FUSED, VBAT, VSYS, VIN, PVIN, VIN_12V, VOUT, VOUT_PRE, VAA, VUSB
        r"^(?:VBUS|VBAT|VBATT|VSYS|VIN|PVIN|VOUT|VMAIN|VCORE|VIO|VSUPPLY|VMOT|"
        rf"VMOTOR|VPWR|VDRIVE|VM|VAA|VUSB)\d*{_QUALIFIERS}$",
        # PWR / POWER only as the whole name: PWR_LED / POWER_GOOD are signals
        rf"^(?:PWR|POWER)\d*(?:_{_VOLTAGE})?$",
        # Rail keyword as the trailing word: SENSOR_VDD, USB_VBUS, MCU_VCC
        r"_(?:VCC|VDD|VBUS|VBAT|PWR)$",
    )
)


def _has_signal_qualifier(base: str) -> bool:
    """True when a qualifier word after the first marks a derived signal."""
    qualifiers = (
        _SUPPLY_FAMILY_SIGNAL_QUALIFIERS if _SUPPLY_FAMILY_RE.match(base) else _SIGNAL_QUALIFIERS
    )
    # Trailing digits are an index, not part of the word: VIN_EN2, 3V3_PG1.
    return any(tok.rstrip("0123456789") in qualifiers for tok in base.upper().split("_")[1:])


_GROUND_RAIL_NAME_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # GND family as a leading word: GND, GNDA, GNDD, GNDPWR, AGND, PGND,
        # GND_ISO
        r"^(?:A|D|P|S|C)?GND[A-Z0-9]*(?:_[A-Z0-9]+)*$",
        r"^(?:VSS[A-Z0-9]*|GROUND|EARTH|CHASSIS)$",
        # Trailing word: SHIELD_GND, MCU_VSS
        r"_(?:A|D|P|S|C)?(?:GND|VSS)$",
    )
)


def _rail_base_name(net_name: str) -> str | None:
    """Return the sheet-local, trimmed name, or None for auto-generated nets."""
    if not net_name:
        return None
    name = net_name.strip()
    if _AUTO_NET_NAME_RE.match(name):
        return None
    # Strip the hierarchical sheet path: "/sheet/+5V" -> "+5V".
    base = name.rsplit("/", 1)[-1].strip()
    if not base or _AUTO_NET_NAME_RE.match(base):
        return None
    return base


def is_unconnected_net_name(net_name: str) -> bool:
    """Return True for KiCad's auto-named no-connect nets (``unconnected-(...)``)."""
    return bool(net_name) and net_name.strip().lower().startswith("unconnected-")


def is_ground_rail_name(net_name: str) -> bool:
    """Name-only heuristic: does *net_name* look like a ground rail?

    See the module comment above :data:`POWER_PIN_TYPES` for the rules.
    """
    base = _rail_base_name(net_name)
    if base is None:
        return False
    return any(rx.search(base) for rx in _GROUND_RAIL_NAME_RES)


def is_power_rail_name(net_name: str) -> bool:
    """Name-only heuristic: does *net_name* look like a (non-ground) power rail?

    ``+3V3``, ``/+5V``, ``VBUS``, ``VDD_CORE``, ``3V3_MCU``, ``P3V3``,
    ``VOUT_PRE`` -> True;
    ``USB_D+``, ``ISENSE_A+``, ``/PG_3V3``, ``TRIGGER_5V``, ``VBUS_DET``,
    ``unconnected-(U11-D+-Pad2)``, ``GND`` -> False.
    """
    base = _rail_base_name(net_name)
    if base is None:
        return False
    if any(rx.search(base) for rx in _GROUND_RAIL_NAME_RES):
        return False
    if not base.startswith("+") and _has_signal_qualifier(base):
        return False
    return any(rx.search(base) for rx in _POWER_RAIL_NAME_RES)


# =============================================================================
# CLASSIFICATION FUNCTIONS
# =============================================================================


def _match_pattern(lib_id: str, pattern: str) -> bool:
    """Check if lib_id matches a pattern with wildcards."""
    # Convert glob-style pattern to regex
    regex = pattern.replace("*", ".*").replace("?", ".")
    return bool(re.match(regex, lib_id, re.IGNORECASE))


def classify_from_symbol(lib_id: str) -> NetClass | None:
    """Classify net based on connected symbol's library ID.

    Args:
        lib_id: Library ID of the symbol (e.g., "Audio:PCM5122PW")

    Returns:
        NetClass if symbol indicates a specific type, None otherwise
    """
    for pattern, net_class in SYMBOL_INDICATORS.items():
        if _match_pattern(lib_id, pattern):
            return net_class
    return None


def classify_from_pin_type(pin_types: set[str]) -> NetClass | None:
    """Classify net based on connected pin electrical types.

    Args:
        pin_types: Set of pin types connected to this net
                   (e.g., {"power_in", "passive"})

    Returns:
        NetClass based on pin type analysis
    """
    # Power pins indicate power net
    if "power_in" in pin_types or "power_out" in pin_types:
        return NetClass.POWER

    # If all pins are passive (resistors, capacitors), don't classify
    # as this could be any signal type
    if pin_types == {"passive"}:
        return None

    return None


def classify_from_name(net_name: str) -> NetClass | None:
    """Classify net based on name pattern matching.

    Uses comprehensive regex patterns to identify net class from common
    naming conventions. Patterns are checked in priority order to handle
    ambiguous cases (e.g., HDMI_CLK is HIGH_SPEED, not just CLOCK).

    Priority order (most specific first):
    1. GROUND - Very specific patterns
    2. HIGH_CURRENT_SIGNAL - PHASE_*/MOTOR_*/COIL_* high-current outputs
    3. HIGH_SPEED - Interface-specific signals
    4. RF - RF-specific signals
    5. DEBUG - Debug interface signals
    6. CLOCK - Clock signals
    7. POWER - Power supply signals
    8. ANALOG - Analog signals
    9. DIFFERENTIAL - Differential pair indicators

    Args:
        net_name: Name of the net (e.g., "+3.3V", "USB_DP", "GND")

    Returns:
        NetClass if name matches known patterns, None otherwise
    """
    name_upper = net_name.upper()

    # Check patterns in priority order (most specific first)
    check_order = [
        NetClass.GROUND,  # Very specific, check first
        # High-current motor/coil outputs must be checked before
        # DIFFERENTIAL (so MOTOR_N is not flagged as a diff-pair partner)
        # and before POWER (so PHASE_A doesn't match generic name patterns).
        NetClass.HIGH_CURRENT_SIGNAL,
        NetClass.HIGH_SPEED,  # Interface names often contain CLK
        NetClass.RF,  # RF-specific
        NetClass.DEBUG,  # Debug interfaces
        NetClass.CLOCK,  # Clock signals
        NetClass.POWER,  # Power supplies
        NetClass.ANALOG,  # Analog signals
        NetClass.DIFFERENTIAL,  # Differential indicators (checked last)
    ]

    for net_class in check_order:
        patterns = NET_CLASS_PATTERNS.get(net_class, [])
        for pattern in patterns:
            if re.search(pattern, name_upper, re.IGNORECASE):
                # Issue #2558 / Epic #2556 Phase 1B: when DIFFERENTIAL
                # patterns match, also confirm the name is not on the
                # single-ended refusal list (CC1/CC2, SBU1/SBU2, prefix
                # variants).  We deliberately keep the broader
                # NET_CLASS_PATTERNS[DIFFERENTIAL] entries (e.g.
                # ``(TX|RX)[PN]$``) accepted here since classify_from_name
                # is a *classification* hint -- it just biases routing
                # parameters and doesn't pair up nets.  The pair-formation
                # path (parse_differential_signal in diffpair.py) is
                # stricter.
                if net_class == NetClass.DIFFERENTIAL:
                    # Local import to avoid a forward ref cycle.
                    from .diffpair import is_single_ended_refused

                    if is_single_ended_refused(net_name):
                        continue
                return net_class

    return None


def is_differential_pair_name(net_name: str) -> bool:
    """Check if net name suggests it's part of a differential pair.

    Issue #2558, Epic #2556 Phase 1B: this function is the BROAD hint
    used by ``classify_from_name``; it accepts patterns like ``TXP``
    or ``CLKP`` that don't have an underscore separator.  The strict
    pair-formation path lives in ``diffpair.py::parse_differential_signal``
    and is more conservative.  However, BOTH paths now agree on the
    refusal list (CC1/CC2, SBU1/SBU2, prefix variants) and the
    power-rail filter -- the single source of truth lives in
    ``diffpair.py`` and is consulted here.

    Args:
        net_name: Name of the net

    Returns:
        True if name pattern suggests differential pair
    """
    # Local import to avoid a circular import (diffpair.py is in the
    # same package and is loaded after net_class.py at module init).
    from .diffpair import is_single_ended_refused

    # Strict refusal: never classify single-ended pin pairs (CC1/CC2,
    # SBU1/SBU2) as differential.
    if is_single_ended_refused(net_name):
        return False

    name_upper = net_name.upper()
    diff_patterns = [
        r"[_-]?P$",
        r"[_-]?N$",
        r"\+$",
        r"-$",
        r"_DIFF[PN]?$",
        r"(TX|RX)[PN]$",
    ]
    return any(re.search(p, name_upper) for p in diff_patterns)


def find_differential_partner(net_name: str) -> str | None:
    """Find the expected partner name for a differential pair.

    Args:
        net_name: Name of one net in a differential pair

    Returns:
        Expected name of the partner net, or None if not identifiable
    """
    # Common suffixes and their partners (order matters - check longer first)
    suffix_pairs = [
        ("_DP", "_DM"),
        ("_DM", "_DP"),
        ("_DIFFP", "_DIFFN"),
        ("_DIFFN", "_DIFFP"),
        ("_P", "_N"),
        ("_N", "_P"),
        ("+", "-"),
        ("-", "+"),
        ("P", "N"),
        ("N", "P"),
    ]

    name_upper = net_name.upper()
    for suffix, partner_suffix in suffix_pairs:
        if name_upper.endswith(suffix.upper()):
            # Preserve original case for prefix
            prefix_len = len(net_name) - len(suffix)
            prefix = net_name[:prefix_len]
            # Match case of suffix if possible
            if net_name.endswith(suffix):
                return prefix + partner_suffix
            elif net_name.endswith(suffix.lower()):
                return prefix + partner_suffix.lower()
            else:
                # Mixed case - use upper suffix convention
                return prefix + partner_suffix

    return None


@dataclass
class NetClassification:
    """Result of net classification with confidence and source info."""

    net_class: NetClass
    confidence: float  # 0.0 to 1.0
    source: str  # "symbol", "pin_type", "name_pattern", "signal_path"
    details: str = ""  # Additional info about classification

    def __repr__(self) -> str:
        return f"NetClassification({self.net_class.value}, {self.confidence:.0%}, {self.source})"


def classify_net(
    net_name: str,
    connected_pins: list[tuple[str, Pin]] | None = None,
    connected_symbols: list[SymbolInstance] | None = None,
) -> NetClassification:
    """Classify a net using all available information sources.

    Classification priority (highest confidence first):
    1. Pin electrical type (power_in/power_out indicates power net)
    2. Symbol library ID (specific component types)
    3. Net name pattern matching
    4. Default to SIGNAL

    Args:
        net_name: Name of the net
        connected_pins: List of (symbol_ref, Pin) tuples for pins on this net
        connected_symbols: List of SymbolInstance objects connected to this net

    Returns:
        NetClassification with class, confidence, and source
    """
    # 1. Check pin electrical types (highest confidence for power)
    if connected_pins:
        # A switching regulator's switch node carries a ``power_out`` pin
        # (KiCad types SW/LX that way) but is a high-current switching
        # signal, never a rail to pour or decouple (issue #5998).
        if any(is_switch_node_name(pin.name or "") for _, pin in connected_pins):
            return NetClassification(
                net_class=NetClass.HIGH_CURRENT_SIGNAL,
                confidence=0.90,
                source="pin_type",
                details="Switching-regulator switch node",
            )

        # Power pins are definitive (switch-node / bootstrap pins excluded)
        if any(is_power_rail_pin(pin.pin_type, pin.name or "") for _, pin in connected_pins):
            pin_types = {pin.pin_type for _, pin in connected_pins}
            # Determine if it's power or ground
            name_lower = net_name.lower()
            if any(g in name_lower for g in ["gnd", "vss", "ground", "agnd", "dgnd"]):
                return NetClassification(
                    net_class=NetClass.GROUND,
                    confidence=0.95,
                    source="pin_type",
                    details="Power pin connected to ground-named net",
                )
            return NetClassification(
                net_class=NetClass.POWER,
                confidence=0.95,
                source="pin_type",
                details=f"Pin types: {pin_types}",
            )

    # 2. Check symbol library IDs
    if connected_symbols:
        for symbol in connected_symbols:
            lib_id = symbol.symbol_def.lib_id
            net_class = classify_from_symbol(lib_id)
            if net_class:
                return NetClassification(
                    net_class=net_class,
                    confidence=0.85,
                    source="symbol",
                    details=f"Symbol: {lib_id}",
                )

    # 3. Check net name patterns
    net_class = classify_from_name(net_name)
    if net_class:
        # Higher confidence for ground (very specific patterns)
        confidence = 0.80 if net_class == NetClass.GROUND else 0.70
        return NetClassification(
            net_class=net_class,
            confidence=confidence,
            source="name_pattern",
            details=f"Matched pattern for {net_class.value}",
        )

    # 4. Check for differential pair naming
    if is_differential_pair_name(net_name):
        return NetClassification(
            net_class=NetClass.DIFFERENTIAL,
            confidence=0.65,
            source="name_pattern",
            details="Differential pair naming convention",
        )

    # Default to SIGNAL
    return NetClassification(
        net_class=NetClass.SIGNAL,
        confidence=0.50,
        source="default",
        details="No specific classification matched",
    )


# =============================================================================
# ORCHESTRATION FUNCTIONS
# =============================================================================


def auto_classify_nets(
    net_names: dict[int, str],
    schematic: Schematic | None = None,
    min_confidence: float = 0.5,
) -> dict[int, NetClassification]:
    """Automatically classify all nets in a design.

    Args:
        net_names: Mapping of net ID to net name
        schematic: Optional Schematic for symbol/pin analysis
        min_confidence: Minimum confidence threshold (default 0.5)

    Returns:
        Dict mapping net ID to NetClassification
    """
    classifications: dict[int, NetClassification] = {}

    for net_id, net_name in net_names.items():
        # Get connected pins and symbols if schematic available
        connected_pins = None
        connected_symbols = None

        if schematic:
            # Find symbols/pins connected to this net
            # Note: This requires schematic net extraction which may need
            # implementation depending on the schematic model capabilities
            pass

        classification = classify_net(
            net_name=net_name,
            connected_pins=connected_pins,
            connected_symbols=connected_symbols,
        )

        if classification.confidence >= min_confidence:
            classifications[net_id] = classification

    return classifications


def apply_net_class_rules(
    classifications: dict[int, NetClassification],
    net_names: dict[int, str],
) -> dict[str, NetClassRouting]:
    """Apply routing rules based on net classifications.

    Creates NetClassRouting objects with appropriate parameters for
    each net class type.

    Args:
        classifications: Net ID to NetClassification mapping
        net_names: Net ID to name mapping

    Returns:
        Dict mapping net name to NetClassRouting object
    """
    from .rules import (
        NET_CLASS_AUDIO,
        NET_CLASS_CLOCK,
        NET_CLASS_DEBUG,
        NET_CLASS_DIGITAL,
        NET_CLASS_HIGH_CURRENT_SIGNAL,
        NET_CLASS_HIGH_SPEED,
        NET_CLASS_POWER,
        NetClassRouting,
    )

    # Map NetClass enum to predefined routing configs
    class_to_routing: dict[NetClass, NetClassRouting] = {
        NetClass.POWER: NET_CLASS_POWER,
        NetClass.GROUND: NetClassRouting(
            name="Ground",
            priority=1,
            trace_width=0.5,
            clearance=0.2,
            via_size=0.8,
            cost_multiplier=0.7,  # Prefer ground routing
            zone_priority=20,  # Highest zone priority
            zone_connection="solid",
            is_pour_net=True,
            # Issue #2772: declarative routing-intent.  Ground nets are
            # always satisfied by a copper zone (or fall through to the
            # no-zone warning path) -- they should never enter the
            # pathfinder as ordinary traces.
            route_via="pour",
        ),
        NetClass.HIGH_CURRENT_SIGNAL: NET_CLASS_HIGH_CURRENT_SIGNAL,
        NetClass.CLOCK: NET_CLASS_CLOCK,
        NetClass.HIGH_SPEED: NET_CLASS_HIGH_SPEED,
        NetClass.DIFFERENTIAL: NetClassRouting(
            name="Differential",
            priority=2,
            trace_width=0.15,
            clearance=0.15,
            cost_multiplier=0.85,
            length_critical=True,
            # Issue #2651 / Epic #2556 Phase 2.5a: auto-classifier path for
            # nets recognized as DIFFERENTIAL must engage CoupledPathfinder
            # for semantic consistency with the explicit ``high_speed_nets=``
            # opt-in (which routes through ``NET_CLASS_HIGH_SPEED``).
            coupled_routing=True,
        ),
        NetClass.ANALOG: NET_CLASS_AUDIO,
        NetClass.RF: NetClassRouting(
            name="RF",
            priority=2,
            trace_width=0.2,
            clearance=0.2,
            cost_multiplier=0.9,
            length_critical=True,
            noise_sensitive=True,
        ),
        NetClass.DEBUG: NET_CLASS_DEBUG,
        NetClass.SIGNAL: NET_CLASS_DIGITAL,
    }

    net_rules: dict[str, NetClassRouting] = {}

    for net_id, classification in classifications.items():
        if net_id in net_names:
            net_name = net_names[net_id]
            routing = class_to_routing.get(classification.net_class, NET_CLASS_DIGITAL)
            net_rules[net_name] = routing

    return net_rules


def classify_and_apply_rules(
    net_names: dict[int, str],
    schematic: Schematic | None = None,
    min_confidence: float = 0.5,
) -> dict[str, NetClassRouting]:
    """Convenience function to classify nets and apply routing rules.

    Combines auto_classify_nets() and apply_net_class_rules() into a
    single call for simpler integration.

    Args:
        net_names: Mapping of net ID to net name
        schematic: Optional Schematic for enhanced classification
        min_confidence: Minimum confidence threshold

    Returns:
        Dict mapping net name to NetClassRouting object
    """
    classifications = auto_classify_nets(net_names, schematic, min_confidence)
    return apply_net_class_rules(classifications, net_names)
