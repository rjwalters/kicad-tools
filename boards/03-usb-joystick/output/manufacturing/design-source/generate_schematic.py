#!/usr/bin/env python3
"""Generate the real ATmega32U4-AU circuit from the shared revision-B netlist."""

import argparse
import json
import math
import runpy
from pathlib import Path

from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# Resolve the sibling by file path, including when imported through importlib.
COMPONENTS = runpy.run_path(str(Path(__file__).with_name("joystick_hardware.py")))["COMPONENTS"]


def create_usb_joystick_schematic(output_path: Path, verbose=False):
    procurement = json.loads(Path(__file__).with_name("procurement-review.json").read_text())[
        "parts"
    ]
    sch = Schematic(
        title="USB joystick controller — ATmega32U4",
        date="2026-09",
        revision="B",
        company="kicad-tools",
        paper="A3",
        project_name="usb_joystick",
        comment1="USB bus powered, 8MHz crystal, AVR ISP programming",
        comment2="GPIO and ADC map is fixed to Microchip TQFP-44 pinout",
        snap_mode=SnapMode.AUTO,
        grid=1.27,
    )
    fixed = {
        "U1": (101.6, 101.6),
        "J1": (30.48, 38.1),
        "U2": (96.52, 30.48),
        "J2": (35.56, 177.8),
        "J3": (162.56, 33.02),
        "Y1": (162.56, 86.36),
        "F1": (210.82, 33.02),
    }
    passive_index = 0
    for comp in COMPONENTS:
        if comp.ref in fixed:
            x, y = fixed[comp.ref]
        else:
            x = 210.82 + (passive_index % 4) * 45.72
            y = 66.04 + (passive_index // 4) * 25.4
            passive_index += 1
        inst = sch.add_symbol(
            comp.symbol,
            x=x,
            y=y,
            ref=comp.ref,
            value=comp.value,
            footprint=comp.footprint,
            properties={
                "Manufacturer": comp.manufacturer,
                "MPN": comp.mpn,
                "LCSC": procurement[comp.mpn]["lcsc"],
            },
        )
        wired = set()
        for pin in inst.symbol_def.pins:
            pos = inst.pin_position(pin.number)
            if not pos:
                raise RuntimeError(f"Missing symbol pin {comp.ref}.{pin.number}")
            if pos in wired:
                continue  # Stacked power pins share one physical wire.
            wired.add(pos)
            net = comp.pins.get(pin.number)
            if net is None:
                sch.add_no_connect(*pos, snap=False)
                continue
            angle = math.radians(pin.angle + 180)
            end = (
                round(pos[0] + 5.08 * math.cos(angle), 4),
                round(pos[1] - 5.08 * math.sin(angle), 4),
            )
            sch.add_wire(pos, end, snap=False)
            sch.add_global_label(
                net, *end, shape="bidirectional", rotation=(pin.angle + 180) % 360, snap=False
            )
    for i, net in enumerate(["VBUS", "VCC", "GND"]):
        x, y = 35.56 + i * 30.48, 231.14
        flag = sch.add_symbol("power:PWR_FLAG", x=x, y=y, ref=f"#FLG0{i + 1}", value="PWR_FLAG")
        pin = flag.pin_position("1")
        end = (pin[0], pin[1] + 5.08)
        sch.add_wire(pin, end, snap=False)
        sch.add_global_label(net, *end, shape="bidirectional", rotation=90, snap=False)
    sch.add_text(
        "J2: 1=5V, 2=GND, 3=X, 4=Y, 5=SW. Use 5V potentiometer joystick.\nISP: disconnect USB; power the board from the programmer.",
        25.4,
        205.74,
    )
    sch.add_text(
        "8MHz external crystal; 15pF load capacitors target CL=12pF with ~4.5pF stray.\nExternal 10k button pullups. Firmware must configure PF0/PF1 ADC and USB HID.\nUCAP is internal USB regulator output: capacitor only; never tie it to 5V.",
        152.4,
        254,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sch.write(output_path)
    print(output_path)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output", nargs="?", default=str(Path(__file__).parent / "output/usb_joystick.kicad_sch")
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    path = Path(args.output)
    if path.suffix != ".kicad_sch":
        path = path / "usb_joystick.kicad_sch"
    create_usb_joystick_schematic(path, args.verbose)
