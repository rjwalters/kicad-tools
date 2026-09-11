#!/usr/bin/env python3
"""Read board09 INA226 telemetry using a 3.3V Linux I2C host.

Install smbus2 separately. Does not write STUSB4500 configuration or NVM.
"""

import argparse
import json
import time

ADDRESS = 0x40
SHUNT_OHM = 0.01
CURRENT_LSB = 0.0002
CALIBRATION = 2560


def signed16(value):
    return value - 65536 if value & 0x8000 else value


def decode(shunt, bus, current, power):
    return {
        "shunt_V": signed16(shunt) * 2.5e-6,
        "output_V": bus * 0.00125,
        "current_A": signed16(current) * CURRENT_LSB,
        "power_W": power * 0.005,
    }


class Monitor:
    def __init__(self, bus, address=ADDRESS):
        self.bus, self.address = bus, address

    def read(self, register):
        # SMBus word operations are little endian; INA226 registers are MSB first.
        v = self.bus.read_word_data(self.address, register)
        return ((v & 255) << 8) | (v >> 8)

    def write(self, register, value):
        self.bus.write_word_data(self.address, register, ((value & 255) << 8) | (value >> 8))

    def configure(self):
        if self.read(0xFE) != 0x5449 or self.read(0xFF) & 0xFFF0 != 0x2260:
            raise RuntimeError("INA226 manufacturer/die ID mismatch; no configuration written")
        self.write(5, CALIBRATION)
        # 64 sample averages,1.1ms bus and shunt conversions,continuous mode.
        self.write(0, 0x4527)
        if self.read(5) != CALIBRATION or self.read(0) != 0x4527:
            raise RuntimeError("INA226 configuration readback mismatch")

    def sample(self):
        if self.read(5) != CALIBRATION:
            raise RuntimeError("INA226 reset or calibration changed")
        flags = self.read(6)
        if flags & 4:
            raise RuntimeError("INA226 math overflow")
        if not flags & 8:
            return None
        result = decode(self.read(1), self.read(2), self.read(4), self.read(3))
        # Registers are sampled sequentially; these are slow telemetry, not a
        # synchronous waveform capture or a protective current-control loop.
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bus", type=int, default=1)
    p.add_argument("--samples", type=int, default=10)
    a = p.parse_args()
    if a.samples < 1:
        p.error("--samples must be positive")
    from smbus2 import SMBus

    with SMBus(a.bus) as bus:
        device = Monitor(bus)
        device.configure()
        time.sleep(0.2)
        for _ in range(a.samples):
            row = device.sample()
            print(json.dumps(row if row is not None else {"status": "conversion pending"}))
            time.sleep(0.2)


if __name__ == "__main__":
    main()
