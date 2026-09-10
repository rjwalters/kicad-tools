#!/usr/bin/env python3
"""Check the selected interface against installed KiCad symbols and packages.

This is a pin/package and transfer-budget check, not ERC or a circuit release.
"""

import json
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.schematic.models.schematic import Schematic

ROOT = Path(__file__).resolve().parent


def check():
    design = json.loads((ROOT / "engineering/interface.json").read_text())
    schematic = Schematic(title="Board08 interface audit")
    board = PCB.create(width=100, height=70, layers=4)
    for ref, key in [("U1", "adc"), ("U2", "host")]:
        part = design[key]
        symbol = schematic.add_symbol(part["symbol"], x=50, y=50, ref=ref)
        pins = {p.number: p.name for p in symbol.symbol_def.pins}
        expected = part.get("pins", part.get("used_pins"))
        for number, name in expected.items():
            if pins.get(number) != name:
                raise ValueError(f"{ref} pin {number}: expected {name}, got {pins.get(number)}")
        for number in part.get("ground_pins", []):
            if pins.get(number) not in {"GND", "AGND"}:
                raise ValueError(f"{ref} pin {number} is not ground")
        footprint = board.add_footprint(part["footprint"], ref, 50, 30)
        lands = {p.number for p in footprint.pads if p.number}
        if lands != pins.keys():
            raise ValueError(f"{ref} symbol/package pins differ: {lands ^ pins.keys()}")
    acquisition = design["acquisition"]
    sample_rate = (
        acquisition["external_clock_hz"] / acquisition["modulator_divider"] / acquisition["osr"]
    )
    if sample_rate != acquisition["sample_rate_hz"]:
        raise ValueError("Clock/OSR does not produce the requested sample rate")
    spi = design["spi"]
    for link in spi["links"]:
        adc_name = design["adc"]["pins"][link["adc_pin"]]
        adc_name = adc_name.replace("~{", "").replace("}", "").replace("/", "_")
        host_name = design["host"]["used_pins"][link["pico_pin"]]
        if adc_name != link["signal"] or host_name != f"GPIO{link['gpio']}":
            raise ValueError(f"Interface pin mapping mismatch: {link}")
    frame_seconds = spi["word_bits"] * spi["frame_words"] / spi["sclk_hz"]
    if frame_seconds >= 1 / sample_rate:
        raise ValueError("SPI transfer cannot fit between conversion results")
    print(
        json.dumps(
            {
                "scope": "pin/package and nominal timing only; no circuit or firmware validation",
                "adc_pins": 20,
                "host_pins": 40,
                "sample_rate_hz": sample_rate,
                "spi_frame_us": frame_seconds * 1e6,
                "sample_period_us": 1e6 / sample_rate,
                "raw_channel_bytes_per_second": sample_rate * acquisition["channels"] * 3,
                "manufacturing_ready": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    check()
