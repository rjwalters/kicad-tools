"""Real-library smoke test for every exported schematic block (issue #7399).

The block unit tests in ``tests/test_schematic_blocks.py`` and friends run
against ``Mock()`` schematics that invent their own pin names, so a block whose
default symbol ID or pin names went stale in the KiCad libraries still passes
them.  This module builds each exported factory/class in
:mod:`kicad_tools.schematic.blocks` -- with default arguments plus each
documented variant -- on a real :class:`Schematic` resolving symbols from the
installed KiCad 10 libraries, and asserts that:

* the block builds (no ``SymbolNotFoundError`` / ``PinNotFoundError``);
* no pin lookup was silently swallowed -- every pin name the block asked for
  exists on its symbol, apart from the documented optional/alias probes in
  :data:`OPTIONAL_PIN_PROBES`;
* every ``block.ports`` value is anchored: it coincides with a placed pin's
  connection point or a wire endpoint, or lies on a wire (KiCad joins a wire
  end that lands anywhere on another wire, so a mid-wire tap is a real
  connection point).

Cases that still fail are pinned with ``xfail(strict=True)`` citing the
defect, so fixing a block makes its case XPASS -- which fails the run until
the marker is removed.

Every case uses an on-grid origin.  Blocks compute port coordinates from the
raw ``(x, y)`` while symbols and wires snap to the 1.27 mm grid, so an
off-grid origin misplaces ports in many blocks by the snap distance (tracked
in #7402); that is a separate defect from the per-block ones checked here.

The module skips when no KiCad 10 symbol library is installed.  CI's ``Test``
job runs inside the KiCad 10 container (#3436), which ships them; locally set
``KICAD_SYMBOL_DIR`` or install KiCad 10.
"""

from __future__ import annotations

import inspect
import re
import warnings
from collections.abc import Callable
from typing import Any

import pytest

import kicad_tools.schematic.blocks as B
from kicad_tools.schematic.grid import get_symbol_search_paths
from kicad_tools.schematic.models import symbol as symbol_module
from kicad_tools.schematic.models.schematic import Schematic


def _kicad10_symbols_skip_reason() -> str | None:
    """Return a skip reason unless a KiCad 10 ``Device.kicad_sym`` is installed."""
    for path in get_symbol_search_paths():
        device = path / "Device.kicad_sym"
        if not device.is_file():
            continue
        with device.open(encoding="utf-8") as fh:
            head = fh.read(512)
        match = re.search(r'\(generator_version\s+"(\d+)', head)
        if match and int(match.group(1)) >= 10:
            return None
        return (
            f"KiCad symbol library at {path} is not KiCad 10 "
            f"(generator_version {match.group(1) if match else 'unknown'}); "
            "the block defaults target the KiCad 10 libraries"
        )
    return (
        "No KiCad symbol libraries found (see "
        "kicad_tools.schematic.grid.get_symbol_search_paths). Install KiCad 10 "
        "or set KICAD_SYMBOL_DIR to its symbols directory."
    )


_SKIP_REASON = _kicad10_symbols_skip_reason()
pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")

# On the 1.27 mm schematic grid (80 * 1.27).
X = Y = 101.6

# Pin names some blocks *probe* for and handle the absence of -- the
# regulator blocks' alias chains (VIN/VI/IN, VOUT/VO/OUT, OUT/SW/VOUT,
# GND/VSS) and the optional EN/FB pins in ``power/regulators.py``.  A miss on
# any other name means a block asked for a pin its symbol does not have.
OPTIONAL_PIN_PROBES = frozenset({"VIN", "VI", "IN", "VOUT", "VO", "OUT", "SW", "EN", "FB", "VSS"})

# Exports that are not circuit blocks (base classes, typed ports, validators,
# pure helpers) and so have no case below.
NON_BLOCK_EXPORTS = frozenset(
    {
        "Port",
        "CircuitBlock",
        "ComposedCircuitBlock",
        "PowerPort",
        "DataPort",
        "ConnectionValidator",
        "ConnectionWarning",
        "WarningSeverity",
        "match_ports",
        "charlieplex_pairs_for_grid",
    }
)


def _xfail(reason: str, raises: type[BaseException] | None = None) -> pytest.MarkDecorator:
    if raises is None:
        return pytest.mark.xfail(strict=True, reason=reason)
    return pytest.mark.xfail(strict=True, reason=reason, raises=raises)


# -- Known defects -----------------------------------------------------------

JTAG_DEFAULT = _xfail(
    "#7402: DebugHeader(interface='jtag') rejects its own default pins=10",
    ValueError,
)
UCC27211_LIB = _xfail(
    "#7402: UCC27211GateDriver's default symbol softstart_custom:UCC27211 is "
    "in a board-local library not shipped in this repo",
)


# -- Cases -------------------------------------------------------------------
#
# Each case is (id, export_name, factory).  ``export_name`` is the
# ``blocks.__all__`` entry the case exercises; ``test_every_export_has_a_case``
# keeps the table in step with the package's exports.

Factory = Callable[[Schematic], Any]
_CASES: list[Any] = []


def case(case_id: str, export: str, factory: Factory, *marks: pytest.MarkDecorator) -> None:
    _CASES.append(pytest.param(export, factory, id=case_id, marks=list(marks)))


_LED_PAIRS = [(0, 1), (1, 0), (1, 2), (2, 1), (0, 2), (2, 0)]

# Indicators
case("LEDIndicator", "LEDIndicator", lambda s: B.LEDIndicator(s, X, Y))
case("create_power_led", "create_power_led", lambda s: B.create_power_led(s, X, Y))
case("create_status_led", "create_status_led", lambda s: B.create_status_led(s, X, Y))
case(
    "CharlieplexMatrix",
    "CharlieplexMatrix",
    lambda s: B.CharlieplexMatrix(s, X, Y, pin_count=3, led_pairs=_LED_PAIRS),
)
case(
    "create_charlieplex_matrix",
    "create_charlieplex_matrix",
    lambda s: B.create_charlieplex_matrix(s, X, Y, pin_count=3, led_pairs=_LED_PAIRS[:2]),
)

# Power
case(
    "DecouplingCaps",
    "DecouplingCaps",
    lambda s: B.DecouplingCaps(s, X, Y, values=["100nF", "10uF"]),
)
case("LDOBlock", "LDOBlock", lambda s: B.LDOBlock(s, X, Y))
for _topology in ("async", "sync"):
    case(
        f"BuckConverter[{_topology}]",
        "BuckConverter",
        lambda s, t=_topology: B.BuckConverter(s, X, Y, topology=t),
    )
case(
    "BuckConverter[feedback_divider]",
    "BuckConverter",
    lambda s: B.BuckConverter(s, X, Y, feedback_divider=True),
)
case(
    "DualSupplyCascade",
    "DualSupplyCascade",
    lambda s: B.DualSupplyCascade(s, X, X + 81.28, Y, vin=24.0, v_mid=5.0, vout=3.3),
)
for _protection in ("pfet", "diode", "none"):
    case(
        f"BarrelJackInput[{_protection}]",
        "BarrelJackInput",
        lambda s, p=_protection: B.BarrelJackInput(s, X, Y, protection=p),
    )
    case(
        f"create_12v_barrel_jack[{_protection}]",
        "create_12v_barrel_jack",
        lambda s, p=_protection: B.create_12v_barrel_jack(s, X, Y, protection=p),
    )
    case(
        f"BatteryInput[{_protection}]",
        "BatteryInput",
        lambda s, p=_protection: B.BatteryInput(s, X, Y, protection=p),
    )
for _protection in ("fuse", "polyfuse", "none"):
    case(
        f"USBPowerInput[{_protection}]",
        "USBPowerInput",
        lambda s, p=_protection: B.USBPowerInput(s, X, Y, protection=p),
    )
case("VoltageDivider", "VoltageDivider", lambda s: B.VoltageDivider(s, X, Y))
case("PrechargeSubsystem", "PrechargeSubsystem", lambda s: B.PrechargeSubsystem(s, X, Y))
case("create_3v3_ldo", "create_3v3_ldo", lambda s: B.create_3v3_ldo(s, X, Y))
case("create_3v3_buck", "create_3v3_buck", lambda s: B.create_3v3_buck(s, X, Y))
case("create_5v_buck", "create_5v_buck", lambda s: B.create_5v_buck(s, X, Y))
case("create_12v_buck", "create_12v_buck", lambda s: B.create_12v_buck(s, X, Y))
case(
    "create_dual_supply_cascade",
    "create_dual_supply_cascade",
    lambda s: B.create_dual_supply_cascade(s, X, X + 81.28, Y),
)
case("create_usb_power", "create_usb_power", lambda s: B.create_usb_power(s, X, Y))
case("create_lipo_battery", "create_lipo_battery", lambda s: B.create_lipo_battery(s, X, Y))
case(
    "create_mcu_decoupling_array",
    "create_mcu_decoupling_array",
    lambda s: B.create_mcu_decoupling_array(s, X, Y, supply_pins=3),
)
case(
    "create_voltage_divider",
    "create_voltage_divider",
    lambda s: B.create_voltage_divider(s, X, Y, 5.0, 3.3),
)
case(
    "create_voltage_divider[filter]",
    "create_voltage_divider",
    lambda s: B.create_voltage_divider(s, X, Y, 5.0, 3.3, with_filter=True),
)

# Timing
case("OscillatorBlock", "OscillatorBlock", lambda s: B.OscillatorBlock(s, X, Y))
case("CrystalOscillator", "CrystalOscillator", lambda s: B.CrystalOscillator(s, X, Y))
case(
    "create_crystal_with_loads",
    "create_crystal_with_loads",
    lambda s: B.create_crystal_with_loads(s, X, Y),
)
case(
    "create_mclk_oscillator", "create_mclk_oscillator", lambda s: B.create_mclk_oscillator(s, X, Y)
)

# Interface: CAN
for _xcvr in B.CANTransceiver.TRANSCEIVER_CONFIGS:
    case(
        f"CANTransceiver[{_xcvr}]",
        "CANTransceiver",
        lambda s, t=_xcvr: B.CANTransceiver(s, X, Y, transceiver=t),
    )
case(
    "CANTransceiver[esd]",
    "CANTransceiver",
    lambda s: B.CANTransceiver(s, X, Y, esd_protection=True),
)
case(
    "CANTransceiver[termination]",
    "CANTransceiver",
    lambda s: B.CANTransceiver(s, X, Y, termination=True),
)
case(
    "CANTransceiver[split]",
    "CANTransceiver",
    lambda s: B.CANTransceiver(s, X, Y, termination="split"),
)
for _factory in ("mcp2551", "sn65hvd230", "tja1050"):
    _name = f"create_can_transceiver_{_factory}"
    case(_name, _name, lambda s, n=_name: getattr(B, n)(s, X, Y))
    case(
        f"{_name}[no_esd]",
        _name,
        lambda s, n=_name: getattr(B, n)(s, X, Y, esd_protection=False),
    )

# Interface: debug
case("DebugHeader[swd]", "DebugHeader", lambda s: B.DebugHeader(s, X, Y))
case(
    "DebugHeader[jtag]",
    "DebugHeader",
    lambda s: B.DebugHeader(s, X, Y, interface="jtag"),
    JTAG_DEFAULT,
)
case(
    "DebugHeader[jtag,20]",
    "DebugHeader",
    lambda s: B.DebugHeader(s, X, Y, interface="jtag", pins=20),
)
case(
    "DebugHeader[tag-connect]",
    "DebugHeader",
    lambda s: B.DebugHeader(s, X, Y, interface="tag-connect"),
)
case(
    "DebugHeader[series_resistors]",
    "DebugHeader",
    lambda s: B.DebugHeader(s, X, Y, series_resistors=True),
)
case("create_swd_header", "create_swd_header", lambda s: B.create_swd_header(s, X, Y))
case("create_jtag_header", "create_jtag_header", lambda s: B.create_jtag_header(s, X, Y))
case(
    "create_tag_connect_header",
    "create_tag_connect_header",
    lambda s: B.create_tag_connect_header(s, X, Y),
)

# Interface: USB
for _conn in B.USBConnector.CONNECTOR_CONFIGS:
    case(
        f"USBConnector[{_conn}]",
        "USBConnector",
        lambda s, t=_conn: B.USBConnector(s, X, Y, connector_type=t),
    )
    case(
        f"USBConnector[{_conn},no_esd]",
        "USBConnector",
        lambda s, t=_conn: B.USBConnector(s, X, Y, connector_type=t, esd_protection=False),
    )
case(
    "USBConnector[vbus_protection]",
    "USBConnector",
    lambda s: B.USBConnector(s, X, Y, esd_protection=False, vbus_protection=True),
)
case("create_usb_type_c", "create_usb_type_c", lambda s: B.create_usb_type_c(s, X, Y))
case("create_usb_micro_b", "create_usb_micro_b", lambda s: B.create_usb_micro_b(s, X, Y))

# Interface: I2C, joystick
case("I2CPullups", "I2CPullups", lambda s: B.I2CPullups(s, X, Y))
for _speed in ("standard", "fast", "fast_plus"):
    case(
        f"create_i2c_pullups[{_speed}]",
        "create_i2c_pullups",
        lambda s, sp=_speed: B.create_i2c_pullups(s, X, Y, speed=sp),
    )
case(
    "create_i2c_pullups[filter]",
    "create_i2c_pullups",
    lambda s: B.create_i2c_pullups(s, X, Y, with_filter=True),
)
case("AnalogJoystickBlock", "AnalogJoystickBlock", lambda s: B.AnalogJoystickBlock(s, X, Y))
case(
    "create_analog_joystick", "create_analog_joystick", lambda s: B.create_analog_joystick(s, X, Y)
)

# MCU
for _mode in ("stm32", "esp32", "generic"):
    case(
        f"BootModeSelector[{_mode}]",
        "BootModeSelector",
        lambda s, m=_mode: B.BootModeSelector(s, X, Y, mode=m),
    )
case(
    "MCUBlock",
    "MCUBlock",
    lambda s: B.MCUBlock(s, X, Y, mcu_symbol="MCU_ST_STM32F1:STM32F103C8Tx"),
)
case("ResetButton", "ResetButton", lambda s: B.ResetButton(s, X, Y))
case("ResetButton[esd]", "ResetButton", lambda s: B.ResetButton(s, X, Y, esd_protection=True))
case("create_reset_button", "create_reset_button", lambda s: B.create_reset_button(s, X, Y))
case("create_stm32_boot", "create_stm32_boot", lambda s: B.create_stm32_boot(s, X, Y))
case("create_esp32_boot", "create_esp32_boot", lambda s: B.create_esp32_boot(s, X, Y))
case("create_generic_boot", "create_generic_boot", lambda s: B.create_generic_boot(s, X, Y))
case(
    "create_gpio_pull_resistor[down]",
    "create_gpio_pull_resistor",
    lambda s: B.create_gpio_pull_resistor(s, X, Y),
)
case(
    "create_gpio_pull_resistor[up]",
    "create_gpio_pull_resistor",
    lambda s: B.create_gpio_pull_resistor(s, X, Y, pull_type="up", rail="VCC"),
)

# Motor control
case("HalfBridge", "HalfBridge", lambda s: B.HalfBridge(s, X, Y))
case("ThreePhaseInverter", "ThreePhaseInverter", lambda s: B.ThreePhaseInverter(s, X, Y))
case("CurrentSenseShunt", "CurrentSenseShunt", lambda s: B.CurrentSenseShunt(s, X, Y))
case(
    "CurrentSenseShunt[amplifier]",
    "CurrentSenseShunt",
    lambda s: B.CurrentSenseShunt(s, X, Y, amplifier=True),
)
for _driver in ("3-phase", "half-bridge"):
    case(
        f"GateDriverBlock[{_driver}]",
        "GateDriverBlock",
        lambda s, d=_driver: B.GateDriverBlock(s, X, Y, driver_type=d),
    )
case(
    "GateDriveResistorArray", "GateDriveResistorArray", lambda s: B.GateDriveResistorArray(s, X, Y)
)
case(
    "BootstrapCapacitorArray",
    "BootstrapCapacitorArray",
    lambda s: B.BootstrapCapacitorArray(s, X, Y),
)
case("BackToBackFETPair", "BackToBackFETPair", lambda s: B.BackToBackFETPair(s, X, Y))
case(
    "UCC27211GateDriver",
    "UCC27211GateDriver",
    lambda s: B.UCC27211GateDriver(s, X, Y),
    UCC27211_LIB,
)
case("create_half_bridge", "create_half_bridge", lambda s: B.create_half_bridge(s, X, Y))
case(
    "create_half_bridge[bootstrap]",
    "create_half_bridge",
    lambda s: B.create_half_bridge(s, X, Y, with_bootstrap=True),
)
case(
    "create_3phase_inverter", "create_3phase_inverter", lambda s: B.create_3phase_inverter(s, X, Y)
)
case("create_current_sense", "create_current_sense", lambda s: B.create_current_sense(s, X, Y))
case(
    "create_current_sense[amplifier]",
    "create_current_sense",
    lambda s: B.create_current_sense(s, X, Y, with_amplifier=True),
)
case(
    "create_gate_drive_resistor_array",
    "create_gate_drive_resistor_array",
    lambda s: B.create_gate_drive_resistor_array(s, X, Y),
)
case(
    "create_bootstrap_capacitor_array",
    "create_bootstrap_capacitor_array",
    lambda s: B.create_bootstrap_capacitor_array(s, X, Y),
)

# Analog
for _topology in ("rc", "rc_clamp", "none"):
    case(
        f"ADCInputFilterBlock[{_topology}]",
        "ADCInputFilterBlock",
        lambda s, t=_topology: B.ADCInputFilterBlock(s, X, Y, topology=t),
    )
case("HallSensorInput", "HallSensorInput", lambda s: B.HallSensorInput(s, X, Y))
for _topology in ("buffer", "non_inverting", "inverting"):
    case(
        f"OpAmpBlock[{_topology}]",
        "OpAmpBlock",
        lambda s, t=_topology: B.OpAmpBlock(s, X, Y, topology=t),
    )
case("OvercurrentComparator", "OvercurrentComparator", lambda s: B.OvercurrentComparator(s, X, Y))
case("ThermistorSense", "ThermistorSense", lambda s: B.ThermistorSense(s, X, Y))
case("VoltageDividerSense", "VoltageDividerSense", lambda s: B.VoltageDividerSense(s, X, Y))
case("create_adc_filter", "create_adc_filter", lambda s: B.create_adc_filter(s, X, Y))
case(
    "create_hall_sensor_input",
    "create_hall_sensor_input",
    lambda s: B.create_hall_sensor_input(s, X, Y),
)
case(
    "create_opamp_buffer",
    "create_opamp_buffer",
    lambda s: B.create_opamp_buffer(s, X, Y),
)
case("create_opamp_gain", "create_opamp_gain", lambda s: B.create_opamp_gain(s, X, Y))
case(
    "create_temperature_sense",
    "create_temperature_sense",
    lambda s: B.create_temperature_sense(s, X, Y),
)
case("create_voltage_sense", "create_voltage_sense", lambda s: B.create_voltage_sense(s, X, Y))

# Protection
case("ESDProtectionBlock[1]", "ESDProtectionBlock", lambda s: B.ESDProtectionBlock(s, X, Y))
case(
    "ESDProtectionBlock[2]",
    "ESDProtectionBlock",
    lambda s: B.ESDProtectionBlock(s, X, Y, channels=2),
)
case("FuseBlock", "FuseBlock", lambda s: B.FuseBlock(s, X, Y))
case("OvervoltageClampBlock", "OvervoltageClampBlock", lambda s: B.OvervoltageClampBlock(s, X, Y))
case("PTCResetBlock", "PTCResetBlock", lambda s: B.PTCResetBlock(s, X, Y))
case(
    "ReversePolarityBlock[pfet]", "ReversePolarityBlock", lambda s: B.ReversePolarityBlock(s, X, Y)
)
case(
    "ReversePolarityBlock[diode]",
    "ReversePolarityBlock",
    lambda s: B.ReversePolarityBlock(s, X, Y, topology="diode"),
)
case("ThermalCutoffBlock", "ThermalCutoffBlock", lambda s: B.ThermalCutoffBlock(s, X, Y))
case("create_esd_protection", "create_esd_protection", lambda s: B.create_esd_protection(s, X, Y))
case("create_fuse", "create_fuse", lambda s: B.create_fuse(s, X, Y))
case(
    "create_overvoltage_clamp",
    "create_overvoltage_clamp",
    lambda s: B.create_overvoltage_clamp(s, X, Y),
)
case("create_ptc_fuse", "create_ptc_fuse", lambda s: B.create_ptc_fuse(s, X, Y))
case(
    "create_reverse_polarity_diode",
    "create_reverse_polarity_diode",
    lambda s: B.create_reverse_polarity_diode(s, X, Y),
)
case(
    "create_reverse_polarity_pfet",
    "create_reverse_polarity_pfet",
    lambda s: B.create_reverse_polarity_pfet(s, X, Y),
)
case("create_thermal_cutoff", "create_thermal_cutoff", lambda s: B.create_thermal_cutoff(s, X, Y))


# -- Helpers -----------------------------------------------------------------


def _pt(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 2), round(p[1], 2))


def _placed_pins(sym: Any) -> list[Any]:
    """Pins of the unit this instance actually places (plus unit-0 common pins).

    ``pin_position`` answers for any pin of the symbol def, including those of
    units that were never placed, so counting every pin would anchor a port on
    a point where no pin exists.
    """
    return [p for p in sym.symbol_def.pins if p.unit in (0, sym.unit)]


def _anchor_points(sch: Schematic) -> set[tuple[float, float]]:
    """Every placed pin's connection point plus every wire endpoint."""
    points: set[tuple[float, float]] = set()
    for sym in sch.symbols:
        for pin in _placed_pins(sym):
            points.add(_pt(sym.pin_position(pin.number)))
    for wire in sch.wires:
        points.add(_pt((wire.x1, wire.y1)))
        points.add(_pt((wire.x2, wire.y2)))
    return points


def _on_a_wire(sch: Schematic, p: tuple[float, float]) -> bool:
    """True if ``p`` lies on an orthogonal wire segment (a mid-wire tap)."""
    eps = 1e-6
    for wire in sch.wires:
        (x1, y1), (x2, y2) = _pt((wire.x1, wire.y1)), _pt((wire.x2, wire.y2))
        if x1 == x2 == p[0] and min(y1, y2) - eps <= p[1] <= max(y1, y2) + eps:
            return True
        if y1 == y2 == p[1] and min(x1, x2) - eps <= p[0] <= max(x1, x2) + eps:
            return True
    return False


@pytest.fixture
def pin_misses(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Record every pin lookup that fails, even if the block swallows it."""
    misses: list[tuple[str, str, str]] = []
    original = symbol_module.SymbolInstance.find_pin

    def spy(self: Any, pin_name_or_number: str) -> Any:
        try:
            return original(self, pin_name_or_number)
        except Exception:
            misses.append((self.reference, self.symbol_def.lib_id, pin_name_or_number))
            raise

    monkeypatch.setattr(symbol_module.SymbolInstance, "find_pin", spy)
    return misses


# -- Tests -------------------------------------------------------------------


@pytest.mark.parametrize(("export", "factory"), _CASES)
def test_block_builds_on_real_symbols(
    export: str, factory: Factory, pin_misses: list[tuple[str, str, str]]
) -> None:
    sch = Schematic(title=f"real-symbol smoke: {export}")
    with warnings.catch_warnings():
        # Wire-collision UserWarnings are layout advice, not build failures.
        warnings.simplefilter("ignore", UserWarning)
        block = factory(sch)

    unexpected = [m for m in pin_misses if m[2] not in OPTIONAL_PIN_PROBES]
    assert not unexpected, f"{export}: pin lookups failed on real symbols: {unexpected}"

    anchors = _anchor_points(sch)
    floating = {
        name: pos
        for name, pos in block.ports.items()
        if pos is not None and _pt(pos) not in anchors and not _on_a_wire(sch, _pt(pos))
    }
    assert not floating, (
        f"{export}: ports not on any pin or wire: {floating} "
        f"(symbols: {[(s.reference, s.symbol_def.lib_id) for s in sch.symbols]}, "
        f"wires: {len(sch.wires)})"
    )


def test_every_export_has_a_case() -> None:
    """Each exported block factory/class is exercised by at least one case."""
    covered = {p.values[0] for p in _CASES}
    blocks = {
        name
        for name in B.__all__
        if name not in NON_BLOCK_EXPORTS
        and (inspect.isclass(getattr(B, name)) or inspect.isfunction(getattr(B, name)))
    }
    assert blocks - covered == set(), f"exports without a real-symbol case: {blocks - covered}"
    assert covered - blocks == set(), f"cases naming unknown exports: {covered - blocks}"


# Rail hookups re-resolve pins after construction (BarrelJackInput's
# ``connect_to_rails`` asked for the nonexistent "Sleeve" pin a second time),
# so exercise them on the blocks whose pin names #7399 corrected.
_VCC_RAIL = 50.8
_GND_RAIL = 152.4
_RAIL_CASES = [
    pytest.param(
        lambda s: B.BarrelJackInput(s, X, Y, protection=p),
        lambda b: b.connect_to_rails(gnd_rail_y=_GND_RAIL),
        id=f"BarrelJackInput[{p}]",
    )
    for p in ("pfet", "diode", "none")
] + [
    pytest.param(
        lambda s: B.BatteryInput(s, X, Y),
        lambda b: b.connect_to_rails(gnd_rail_y=_GND_RAIL),
        id="BatteryInput[pfet]",
    ),
    pytest.param(
        lambda s: B.CurrentSenseShunt(s, X, Y, amplifier=True),
        lambda b: b.connect_to_rails(gnd_rail_y=_GND_RAIL, vcc_rail_y=_VCC_RAIL),
        id="CurrentSenseShunt[amplifier]",
    ),
    pytest.param(
        lambda s: B.create_usb_type_c(s, X, Y, with_vbus_protection=True),
        lambda b: b.connect_to_rails(vbus_rail_y=_VCC_RAIL, gnd_rail_y=_GND_RAIL),
        id="create_usb_type_c[vbus_protection]",
    ),
    pytest.param(
        lambda s: B.create_can_transceiver_tja1050(s, X, Y),
        lambda b: b.connect_to_rails(vcc_rail_y=_VCC_RAIL, gnd_rail_y=_GND_RAIL),
        id="create_can_transceiver_tja1050",
    ),
    pytest.param(
        lambda s: B.ResetButton(s, X, Y, esd_protection=True),
        lambda b: b.connect_to_rails(vcc_rail_y=_VCC_RAIL, gnd_rail_y=_GND_RAIL),
        id="ResetButton[esd]",
    ),
]


@pytest.mark.parametrize(("factory", "hookup"), _RAIL_CASES)
def test_rail_hookup_resolves_real_pins(
    factory: Factory,
    hookup: Callable[[Any], None],
    pin_misses: list[tuple[str, str, str]],
) -> None:
    sch = Schematic(title="real-symbol rail hookup")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        block = factory(sch)
        wires_before = len(sch.wires)
        hookup(block)

    assert len(sch.wires) > wires_before
    unexpected = [m for m in pin_misses if m[2] not in OPTIONAL_PIN_PROBES]
    assert not unexpected, f"pin lookups failed on real symbols: {unexpected}"


def _nets(sch: Schematic) -> dict[tuple[float, float], int]:
    """Union-find connectivity: wires join at endpoints, or where an endpoint
    lands on another wire's span (KiCad joins those); pins join at their point.
    Returns a point -> net-id map over every wire endpoint and placed pin."""
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(p: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(p, p)
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    def union(a: tuple[float, float], b: tuple[float, float]) -> None:
        parent[find(a)] = find(b)

    segs = [(_pt((w.x1, w.y1)), _pt((w.x2, w.y2))) for w in sch.wires]
    for a, b in segs:
        union(a, b)
    for a, b in segs:
        for end in (a, b):
            for c, d in segs:
                if (c, d) != (a, b) and _on_segment(end, c, d):
                    union(end, c)
    for sym in sch.symbols:
        for pin in _placed_pins(sym):
            find(_pt(sym.pin_position(pin.number)))
    roots: dict[tuple[float, float], int] = {}
    return {p: roots.setdefault(find(p), len(roots)) for p in list(parent)}


def _on_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    eps = 1e-6
    if a[0] == b[0] == p[0]:
        return min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    if a[1] == b[1] == p[1]:
        return min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
    return False


@pytest.mark.parametrize("topology", ["buffer", "non_inverting", "inverting"])
def test_opamp_topologies_are_connected_as_drawn(topology: str) -> None:
    """IN and OUT sit on different nets; OUT feeds back; supplies reach placed pins."""
    sch = Schematic(title=f"opamp connectivity: {topology}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        gain = 1.0 if topology == "buffer" else 3.0
        block = B.OpAmpBlock(sch, X, Y, topology=topology, gain=gain)

    nets = _nets(sch)
    net = lambda p: nets[_pt(p)]  # noqa: E731
    pins = {
        (sym.reference, sym.unit, pin.number): _pt(sym.pin_position(pin.number))
        for sym in sch.symbols
        for pin in _placed_pins(sym)
    }
    out, inv, non_inv = (pins[("U1", 1, n)] for n in ("1", "2", "3"))

    assert net(block.ports["IN"]) != net(block.ports["OUT"])
    assert net(block.ports["OUT"]) == nets[out]
    if topology == "buffer":
        assert nets[out] == nets[inv], "follower: OUT must feed the inverting input"
        assert nets[non_inv] == net(block.ports["IN"])
        assert nets[inv] != nets[non_inv], "IN shorted to the feedback node"
    else:
        rf_nets = {nets[p] for k, p in pins.items() if k[0] == "R1"}
        assert nets[out] in rf_nets and nets[inv] in rf_nets
        assert nets[out] != nets[inv]
    for port in ("VCC", "GND"):
        assert net(block.ports[port]) in {nets[p] for k, p in pins.items() if k[0] == "U1"}
    assert net(block.ports["VCC"]) != net(block.ports["GND"])
    # The supply pins belong to the placed power unit, not a phantom one.
    assert any(k[0] == "U1" and k[2] in ("4", "8") for k in pins)


@pytest.mark.parametrize("channels", [3, 4, 8])
def test_esd_array_rejects_more_channels_than_the_part_has(channels: int) -> None:
    """USBLC6-2SC6 has two channels; asking for more must raise, not invent ports."""
    with pytest.raises(ValueError, match="2 ESD channel"):
        B.ESDProtectionBlock(Schematic(title="esd"), X, Y, channels=channels)


def _pin_nets(sch: Schematic) -> tuple[dict[tuple[float, float], int], dict[str, int]]:
    """``_nets`` plus a ``"REF.PIN" -> net`` map over every placed pin."""
    nets = _nets(sch)
    pins = {
        f"{sym.reference}.{pin.number}": nets[_pt(sym.pin_position(pin.number))]
        for sym in sch.symbols
        for pin in _placed_pins(sym)
    }
    return nets, pins


def _net_at(sch: Schematic, nets: dict[tuple[float, float], int], p: tuple[float, float]) -> int:
    """Net of point ``p``: a wire endpoint or pin, or a tap in a wire's span."""
    p = _pt(p)
    if p in nets:
        return nets[p]
    for w in sch.wires:
        a, b = _pt((w.x1, w.y1)), _pt((w.x2, w.y2))
        if _on_segment(p, a, b):
            return nets[a]
    raise AssertionError(f"{p} is on no pin or wire")


def _pin_number(sym: Any, name: str) -> str:
    return next(p.number for p in sym.symbol_def.pins if p.name == name)


# (factory, input port, output port).  Each places Q1 as a high-side P-FET.
_REVERSE_PFET_CASES = [
    pytest.param(
        lambda s: B.ReversePolarityBlock(s, X, Y), "VIN", "VOUT", id="ReversePolarityBlock"
    ),
    pytest.param(
        lambda s: B.create_reverse_polarity_pfet(s, X, Y),
        "VIN",
        "VOUT",
        id="create_reverse_polarity_pfet",
    ),
    pytest.param(lambda s: B.BarrelJackInput(s, X, Y), "VIN", "VOUT", id="BarrelJackInput"),
    pytest.param(
        lambda s: B.create_12v_barrel_jack(s, X, Y), "VIN", "VOUT", id="create_12v_barrel_jack"
    ),
    pytest.param(lambda s: B.BatteryInput(s, X, Y), "VBAT_IN", "VBAT", id="BatteryInput"),
    pytest.param(
        lambda s: B.create_lipo_battery(s, X, Y), "VBAT_IN", "VBAT", id="create_lipo_battery"
    ),
]


@pytest.mark.parametrize(("factory", "vin", "vout"), _REVERSE_PFET_CASES)
def test_reverse_polarity_pfet_takes_the_input_on_its_drain(
    factory: Factory, vin: str, vout: str
) -> None:
    """Drain on the input, source on the load, gate to ground (issue #7399).

    The body diode (anode = drain) must conduct from the input toward the
    load; with the source on the input it conducts from the load back into a
    reversed supply and the block protects nothing.
    """
    sch = Schematic(title="reverse-polarity P-FET orientation")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        block = factory(sch)
    nets, pins = _pin_nets(sch)
    port = lambda name: _net_at(sch, nets, block.ports[name])  # noqa: E731
    q = next(s for s in sch.symbols if s.reference == "Q1")
    d, s_, g = (f"Q1.{_pin_number(q, n)}" for n in ("D", "S", "G"))

    assert pins[d] == port(vin), "the input must land on the P-FET drain"
    assert pins[s_] == port(vout), "the protected output must come from the source"
    assert len({port(vin), port(vout), port("GND")}) == 3
    gate_net = pins[g]
    gate_r = next((k for k in ("R_gate", "R_GATE") if k in block.components), None)
    if gate_r:
        r_gate = block.components[gate_r].reference
        assert gate_net == pins[f"{r_gate}.1"]
        assert pins[f"{r_gate}.2"] == port("GND")
    else:
        assert gate_net == port("GND"), "the gate must be tied to ground"
    if "C_FILT" in block.components:
        assert pins[f"{block.components['C_FILT'].reference}.1"] == port(vout)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda s: B.BarrelJackInput(s, X, Y), id="BarrelJackInput"),
        pytest.param(lambda s: B.create_12v_barrel_jack(s, X, Y), id="create_12v_barrel_jack"),
    ],
)
def test_barrel_jack_pfet_clamps_vgs_with_a_zener(factory: Factory) -> None:
    """R_GATE returns the gate to the sleeve; D_GATE clamps G-S (issue #7489)."""
    sch = Schematic(title="barrel jack gate clamp")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        block = factory(sch)
    nets, pins = _pin_nets(sch)
    port = lambda name: _net_at(sch, nets, block.ports[name])  # noqa: E731
    q = block.components["Q"]
    gate, source = pins[f"Q1.{_pin_number(q, 'G')}"], pins[f"Q1.{_pin_number(q, 'S')}"]
    r, d = block.components["R_GATE"], block.components["D_GATE"]
    ref_r, ref_d = r.reference, d.reference

    assert gate != source, "the gate must not be shorted to the source"
    assert pins[f"{ref_d}.{_pin_number(d, 'K')}"] == source, "Zener cathode on the source"
    assert pins[f"{ref_d}.{_pin_number(d, 'A')}"] == gate, "Zener anode on the gate"
    assert pins[f"{ref_r}.1"] == gate
    assert pins[f"{ref_r}.2"] == port("GND"), "gate resistor returns to the sleeve"
    assert gate != port("GND"), "the gate may no longer be tied straight to the sleeve"


@pytest.mark.parametrize("protection", ["diode", "none"])
@pytest.mark.parametrize(
    ("make", "vin", "vout"),
    [
        pytest.param(B.BarrelJackInput, "VIN", "VOUT", id="BarrelJackInput"),
        pytest.param(B.BatteryInput, "VBAT_IN", "VBAT", id="BatteryInput"),
    ],
)
def test_power_input_feeds_its_filter_cap(make: Any, vin: str, vout: str, protection: str) -> None:
    """The input reaches the filter cap through the diode, or directly with no protection."""
    sch = Schematic(title=f"power input: {protection}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        block = make(sch, X, Y, protection=protection)
    nets, pins = _pin_nets(sch)
    port = lambda name: _net_at(sch, nets, block.ports[name])  # noqa: E731
    cap = block.components["C_FILT"].reference

    assert pins["J1.1"] == port(vin)
    assert pins[f"{cap}.1"] == port(vout)
    if protection == "none":
        assert port(vin) == port(vout), "unprotected input must feed the filter cap"
    else:
        diode = block.components["D"]
        anode = pins[f"D1.{_pin_number(diode, 'A')}"]
        cathode = pins[f"D1.{_pin_number(diode, 'K')}"]
        assert (anode, cathode) == (port(vin), port(vout))
        assert anode != cathode


@pytest.mark.parametrize("filter_cap", [None, "100nF"])
def test_voltage_divider_sense_is_a_voltage_divider(filter_cap: str | None) -> None:
    """``VoltageDividerSense`` is the ratio-sized ``VoltageDivider`` (issue #7399 step 4)."""
    sch = Schematic(title="voltage divider sense")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        block = B.VoltageDividerSense(sch, X, Y, ratio=11.0, filter_cap=filter_cap)
    assert isinstance(block, B.VoltageDivider)
    assert (block.r1.value, block.r2.value) == ("100.0k", "10k")
    assert block.get_ratio() == pytest.approx(1 / 11)

    nets, pins = _pin_nets(sch)
    port = lambda name: _net_at(sch, nets, block.ports[name])  # noqa: E731
    assert pins["R1.1"] == port("VIN")
    assert pins["R1.2"] == pins["R2.1"] == port("VOUT")
    assert pins["R2.2"] == port("GND")
    assert len({port("VIN"), port("VOUT"), port("GND")}) == 3
    if filter_cap is not None:
        assert (pins["C1.1"], pins["C1.2"]) == (port("VOUT"), port("GND"))


def test_create_voltage_sense_sizes_the_divider_from_the_adc_range() -> None:
    block = B.create_voltage_sense(Schematic(title="sense"), X, Y, max_voltage=33.0, adc_ref=3.3)
    assert isinstance(block, B.VoltageDivider)
    assert block.get_output_voltage(33.0) == pytest.approx(3.3)


@pytest.mark.parametrize("ratio", [1.0, 0.5])
def test_voltage_divider_sense_rejects_a_ratio_that_cannot_divide(ratio: float) -> None:
    with pytest.raises(ValueError, match="ratio must be > 1"):
        B.VoltageDividerSense(Schematic(title="sense"), X, Y, ratio=ratio)
