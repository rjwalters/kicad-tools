"""Host-side register protocol and calculation guards, without hardware claims."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "boards/09-usbc-pd-power"


def load(relative):
    spec = importlib.util.spec_from_file_location("board09_" + Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor = load("host/monitor.py")
calculations = load("engineering/calculate.py")


class Bus:
    def __init__(self):
        self.registers = {0xFE: 0x4954, 0xFF: 0x6122}
        self.writes = []

    def read_word_data(self, address, register):
        assert address == 0x40
        return self.registers.get(register, 0)

    def write_word_data(self, address, register, value):
        assert address == 0x40
        self.writes.append((register, value))
        self.registers[register] = value


def test_configuration_uses_ina_byte_order():
    bus = Bus()
    monitor.Monitor(bus).configure()
    assert bus.writes == [(5, 0x000A), (0, 0x2745)]


def test_wrong_device_does_not_receive_writes():
    bus = Bus()
    bus.registers[0xFE] = 0
    with pytest.raises(RuntimeError, match="ID mismatch"):
        monitor.Monitor(bus).configure()
    assert bus.writes == []


def test_signed_current_and_register_scales():
    row = monitor.decode(0xFFF6, 4000, 0xFFCE, 3)
    assert row == pytest.approx(
        {"shunt_V": -25e-6, "output_V": 5, "current_A": -0.01, "power_W": 0.015}
    )


def test_sample_rejects_reset_and_overflow_and_waits_for_conversion():
    device = monitor.Monitor(Bus())
    device.configure()
    assert device.sample() is None
    device.write(6, 12)
    with pytest.raises(RuntimeError, match="overflow"):
        device.sample()
    device.write(5, 0)
    with pytest.raises(RuntimeError, match="reset or calibration"):
        device.sample()


def test_failed_configuration_readback():
    class ReadOnlyBus(Bus):
        def write_word_data(self, address, register, value):
            pass

    with pytest.raises(RuntimeError, match="readback mismatch"):
        monitor.Monitor(ReadOnlyBus()).configure()


def test_calculations_bound_to_generated_values():
    result = calculations.calculate()
    assert result["screen_passed"]
    assert result["pd_budget"]["required_input_W"] > 18
    changed = dict(result["component_value_binding"], R8="10k")
    with pytest.raises(ValueError, match="R8"):
        calculations.calculate(changed)


def test_real_pinouts_and_kelvin_force_isolation():
    parts = {p.ref: p for p in load("generate_design.py").parts()}
    # These package-specific pins are easy to accidentally substitute with
    # superficially similar regulators or two-terminal shunt footprints.
    assert parts["U4"].pins == {"1": "+3V3", "2": "VBUS_RAW", "3": "GND"}
    assert parts["U2"].pins == {
        "1": "GND",
        "2": "SW",
        "3": "VIN",
        "4": "FB",
        "5": "BUCK_EN",
        "6": "BOOT",
    }
    assert parts["RSH1"].pins == {
        "1": "VOUT_PRE",
        "2": "KELVIN_P",
        "3": "KELVIN_N",
        "4": "+5V_OUT",
    }
    assert parts["RSH1"].footprint.endswith("T1.19mm")
    for ref in ["R3", "R4"]:
        assert parts[ref].footprint.endswith("R_1206_3216Metric")
        assert parts[ref].mpn == "CRCW12061K00FKEAHP"
    for ref in ["Q1", "Q2"]:
        assert all(parts[ref].pins[p] == "PMOS_SOURCE" for p in ["1", "2", "3"])
        assert parts[ref].pins["4"] == "PMOS_GATE"
