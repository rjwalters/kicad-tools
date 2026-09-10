#!/usr/bin/env python3
"""Generate a staged real STM32F429/SDRAM circuit, never the gallery output.

uv run python boards/07-matchgroup-test/real_design/generate.py /tmp/board07-sdram
Placement is preliminary. This generator makes no routing/sign-off claim.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.physics import Stackup, TransmissionLine
from kicad_tools.router.rules import NetClassRouting, net_class_map_to_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.sexp import SExp


@dataclass
class Part:
    ref: str
    symbol: str
    footprint: str
    value: str
    pins: dict[str, str]
    xy: tuple[float, float]
    schematic_xy: tuple[float, float]
    mpn: str = ""
    lcsc: str = ""
    decouples: str = ""


# GPIO names are independently traceable to MB1075 and the STM32 datasheet.
FMC_GPIO = {
    **dict(
        zip(
            (f"DQ{i}" for i in range(16)),
            (
                "PD14",
                "PD15",
                "PD0",
                "PD1",
                "PE7",
                "PE8",
                "PE9",
                "PE10",
                "PE11",
                "PE12",
                "PE13",
                "PE14",
                "PE15",
                "PD8",
                "PD9",
                "PD10",
            ),
            strict=True,
        )
    ),
    **dict(
        zip(
            (f"A{i}" for i in range(12)),
            (
                "PF0",
                "PF1",
                "PF2",
                "PF3",
                "PF4",
                "PF5",
                "PF12",
                "PF13",
                "PF14",
                "PF15",
                "PG0",
                "PG1",
            ),
            strict=True,
        )
    ),
    "BA0": "PG4",
    "BA1": "PG5",
    "SDCLK": "PG8",
    "SDCKE1": "PB5",
    "SDNE1": "PB6",
    "SDNWE": "PC0",
    "SDNRAS": "PF11",
    "SDNCAS": "PG15",
    "LDQM": "PE0",
    "UDQM": "PE1",
}

SDRAM_PINS = {
    **dict(
        zip(
            (
                "2",
                "4",
                "5",
                "7",
                "8",
                "10",
                "11",
                "13",
                "42",
                "44",
                "45",
                "47",
                "48",
                "50",
                "51",
                "53",
            ),
            (f"DQ{i}" for i in range(16)),
            strict=True,
        )
    ),
    **dict(
        zip(
            ("23", "24", "25", "26", "29", "30", "31", "32", "33", "34", "22", "35"),
            (f"A{i}" for i in range(12)),
            strict=True,
        )
    ),
    "20": "BA0",
    "21": "BA1",
    "15": "LDQM",
    "39": "UDQM",
    "16": "SDNWE",
    "17": "SDNCAS",
    "18": "SDNRAS",
    "19": "SDNE1",
    "37": "SDCKE1",
    "38": "SDCLK",
    **dict.fromkeys(("1", "3", "9", "14", "27", "43", "49"), "+3V3"),
    **dict.fromkeys(("6", "12", "28", "41", "46", "52", "54"), "GND"),
}


def build_parts() -> list[Part]:
    """Build the electrical circuit using actual library pin names/numbers."""
    probe = Schematic(title="Pin map probe")
    mcu = probe.add_symbol("MCU_ST_STM32F4:STM32F429ZITx", x=100, y=100, ref="U1")
    names = {p.name: p.number for p in mcu.symbol_def.pins}
    mcu_pins = {names[gpio]: net for net, gpio in FMC_GPIO.items()}
    for pin in mcu.symbol_def.pins:
        if pin.name in {"VDD", "VBAT", "PDR_ON"}:
            mcu_pins[pin.number] = "+3V3"
        elif pin.name in {"VSS", "VSSA"}:
            mcu_pins[pin.number] = "GND"
        elif pin.name in {"VDDA", "VREF+"}:
            mcu_pins[pin.number] = "+3V3_A"
    for gpio, net in {
        "VCAP_1": "VCAP1",
        "VCAP_2": "VCAP2",
        "BOOT0": "BOOT0",
        "NRST": "NRST",
        "PA13": "SWDIO",
        "PA14": "SWCLK",
        "PB3": "SWO",
        "PA2": "UART_TX",
        "PA3": "UART_RX",
        "PG13": "LED_PASS",
        "PG14": "LED_FAIL",
    }.items():
        mcu_pins[names[gpio]] = net
    parts = [
        Part(
            "U1",
            "MCU_ST_STM32F4:STM32F429ZITx",
            "Package_QFP:LQFP-144_20x20mm_P0.5mm",
            "STM32F429ZIT6",
            mcu_pins,
            (35, 40),
            (106.68, 127),
            "STM32F429ZIT6",
            "C84808",
        ),
        Part(
            "U2",
            "Memory_RAM:IS42S16400J-xT",
            "Package_SO:TSOP-II-54_22.2x10.16mm_P0.8mm",
            "IS42S16400J-6TLI-TR",
            SDRAM_PINS,
            (72, 40),
            (238.76, 81.28),
            "IS42S16400J-6TLI-TR",
            "C17216754",
        ),
        Part(
            "U3",
            "Regulator_Linear:AP2112K-3.3",
            "Package_TO_SOT_SMD:SOT-23-5",
            "AP2112K-3.3TRG1",
            {"1": "+5V", "2": "GND", "3": "+5V", "5": "+3V3"},
            (12, 15),
            (335.28, 45.72),
            "AP2112K-3.3TRG1",
        ),
        Part(
            "J1",
            "Connector_Generic:Conn_01x02",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            "5V_INPUT",
            {"1": "+5V", "2": "GND"},
            (5, 10),
            (304.8, 45.72),
        ),
        Part(
            "J2",
            "Connector_Generic:Conn_01x06",
            "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
            "SWD_3V3",
            {"1": "+3V3", "2": "SWDIO", "3": "GND", "4": "SWCLK", "5": "NRST", "6": "SWO"},
            (5, 45),
            (335.28, 86.36),
        ),
        Part(
            "J3",
            "Connector_Generic:Conn_01x03",
            "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            "UART_3V3",
            {"1": "GND", "2": "UART_TX", "3": "UART_RX"},
            (5, 68),
            (378.46, 86.36),
        ),
    ]
    cap_index = 0

    def capacitor(net, value, xy, decouples=""):
        nonlocal cap_index
        cap_index += 1
        parts.append(
            Part(
                f"C{cap_index}",
                "Device:C",
                "Capacitor_SMD:C_0603_1608Metric",
                value,
                {"1": net, "2": "GND"},
                xy,
                (210.82 + ((cap_index - 1) % 8) * 22.86, 160.02 + ((cap_index - 1) // 8) * 27.94),
                decouples=decouples,
            )
        )

    # Initial placements are deliberately separate from electrical topology.
    # Decouplers must move to their associated package power pads before routing.
    for index, pin in enumerate(p for p in mcu.symbol_def.pins if p.name == "VDD"):
        angle = 2 * math.pi * index / 12
        capacitor(
            "+3V3",
            "100nF",
            (35 + 16 * math.cos(angle), 40 + 16 * math.sin(angle)),
            decouples=f"U1.{pin.number}",
        )
    for index, pin_number in enumerate(("1", "3", "9", "14", "27", "43", "49")):
        capacitor("+3V3", "100nF", (64 + index * 3, 24), decouples=f"U2.{pin_number}")
    capacitor("+3V3", "4.7uF", (52, 24))
    capacitor("+3V3", "4.7uF", (85, 27))
    capacitor("+3V3_A", "100nF", (22, 24))
    capacitor("+3V3_A", "1uF", (19, 24))
    capacitor("VCAP1", "2.2uF", (35, 25), decouples="U1.71")
    capacitor("VCAP2", "2.2uF", (50, 40), decouples="U1.106")
    capacitor("+5V", "10uF", (8, 17))
    capacitor("+3V3", "10uF", (16, 17))
    capacitor("NRST", "100nF", (17, 33))
    # AN4488 section 8.4.2 calls for 10 nF ground-to-power stitching when
    # SDRAM traces use a power reference plane. Place beside local via pairs
    # at each IC in the final six-layer placement recipe.
    capacitor("+3V3", "10nF", (35, 20))
    capacitor("+3V3", "10nF", (72, 20))
    for index, (value, nets, xy) in enumerate(
        [
            ("0R", ("+3V3", "+3V3_A"), (19, 20)),
            ("10k", ("BOOT0", "GND"), (30, 58)),
            ("10k", ("NRST", "+3V3"), (17, 30)),
            ("1k", ("LED_PASS", "LED_PASS_A"), (40, 66)),
            ("1k", ("LED_FAIL", "LED_FAIL_A"), (46, 66)),
        ],
        1,
    ):
        parts.append(
            Part(
                f"R{index}",
                "Device:R",
                "Resistor_SMD:R_0603_1608Metric",
                value,
                dict(zip(("1", "2"), nets, strict=True)),
                xy,
                (304.8 + (index - 1) * 22.86, 279.4),
            )
        )
    for index, name in enumerate(("PASS", "FAIL"), 1):
        parts.append(
            Part(
                f"D{index}",
                "Device:LED",
                "LED_SMD:LED_0603_1608Metric",
                "GREEN" if index == 1 else "RED",
                {"1": "GND", "2": f"LED_{name}_A"},
                (34 + index * 6, 70),
                (330.2 + index * 27.94, 307.34),
            )
        )
    catalog = json.loads((Path(__file__).parent / "procurement.json").read_text())["parts"]
    for part in parts:
        key = part.ref if part.ref[0] in "UJD" else f"{part.ref[0]}:{part.value}"
        selected = catalog[key]
        part.mpn, part.lcsc = selected["mpn"], selected["lcsc"]
    return parts


def generate(output: Path, sdram_rotation: int = 0, routing_clearance: float = 0.15) -> None:
    if routing_clearance < 0.15:
        raise ValueError("Routing clearance cannot undercut the authored0.15mm floor")
    output.mkdir(parents=True, exist_ok=True)
    parts = build_parts()
    sch = Schematic(
        title="Board 07 real SDRAM memory test — DRAFT",
        paper="A2",
        date="2026-09-09",
        project_name="sdram_demo",
        revision="A-draft",
    )
    pcb = PCB.create(width=100, height=80, layers=4, title="SDRAM demo DRAFT")
    # Router loader #4978 only recognizes gr_rect. Preserve exactly the same
    # physical rectangle instead of allowing its silent65x56mm HAT fallback.
    edges = [
        node
        for node in pcb._sexp.find_children("gr_line")
        if node.get("layer").get_string(0) == "Edge.Cuts"
    ]
    endpoints = [
        (node.get(which).get_float(0), node.get(which).get_float(1))
        for node in edges
        for which in ("start", "end")
    ]
    rect = SExp("gr_rect")
    rect.add(SExp("start").add(min(p[0] for p in endpoints)).add(min(p[1] for p in endpoints)))
    rect.add(SExp("end").add(max(p[0] for p in endpoints)).add(max(p[1] for p in endpoints)))
    rect.add(SExp("stroke").add(SExp("width").add(0.1)).add(SExp("type").add("default")))
    rect.add(SExp("fill").add("none"))
    rect.add(SExp("layer").add("Edge.Cuts"))
    pcb._sexp.children = [node for node in pcb._sexp.children if node not in edges]
    pcb._sexp.add(rect)
    # Explicit JLC04161H-3313 stack, verified against jlcpcb.com/impedance.
    # Thin outer dielectrics allow 50-ohm escape traces at the MCU's 0.5 mm pitch.
    stack = pcb._sexp.get("setup").get("stackup")
    for layer in stack.find_children("layer"):
        name = layer.get_atoms()[0]
        if name in {"dielectric 1", "dielectric 3"}:
            layer.get("thickness").set_value(0, 0.0994)
            layer.get("material").set_value(0, "FR4 3313")
            layer.get("epsilon_r").set_value(0, 4.1)
        elif name == "dielectric 2":
            layer.get("thickness").set_value(0, 1.265)
            layer.get("epsilon_r").set_value(0, 4.6)
        elif name in {"In1.Cu", "In2.Cu"}:
            layer.get("thickness").set_value(0, 0.0152)
    nets = {name: pcb.add_net(name) for name in sorted({n for p in parts for n in p.pins.values()})}
    assignments = {}
    for part in parts:
        symbol = sch.add_symbol(
            part.symbol,
            x=part.schematic_xy[0],
            y=part.schematic_xy[1],
            ref=part.ref,
            value=part.value,
            footprint=part.footprint,
        )
        symbol.properties["Manufacturer_Part_Number"] = part.mpn
        symbol.properties["LCSC"] = part.lcsc
        known_pins = {p.number for p in symbol.symbol_def.pins}
        if not set(part.pins) <= known_pins:
            raise ValueError(f"{part.ref}: nonexistent symbol pins {set(part.pins) - known_pins}")
        for pin in symbol.symbol_def.pins:
            x, y = symbol.pin_position(pin.number)
            if pin.number not in part.pins:
                sch.add_no_connect(x, y)
                continue
            radians = math.radians(pin.angle)
            end = (round(x - 5.08 * math.cos(radians), 3), round(y + 5.08 * math.sin(radians), 3))
            sch.add_wire((x, y), end)
            sch.add_label(part.pins[pin.number], *end)
        fp = pcb.add_footprint(part.footprint, part.ref, *part.xy, value=part.value)
        footprint_pins = {pad.number for pad in fp.pads}
        if known_pins != footprint_pins:
            raise ValueError(
                f"{part.ref}: symbol/footprint pin mismatch {known_pins ^ footprint_pins}"
            )
        for pad in fp.pads:
            name = part.pins.get(pad.number)
            if name:
                assert pcb.assign_net_to_footprint_pad(part.ref, pad.number, name)
        assignments[part.ref] = part.pins
    # KiCad stores pad angles in absolute board coordinates while pad centers
    # remain footprint-local. Rotate both angles before deriving decoupling.
    memory = pcb.get_footprint("U2")
    for pad in memory.pads:
        pad.rotation += sdram_rotation - memory.rotation
    memory.rotation = sdram_rotation
    # Place supply decouplers by actual package pads, not symbol ordering.
    for part in parts:
        if not part.decouples:
            continue
        target_ref, target_pad = part.decouples.split(".")
        target = pcb.get_footprint(target_ref)
        px, py = pcb.get_pad_position(target_ref, target_pad)
        dx, dy = px - target.position[0], py - target.position[1]
        distance = 6.5 if part.pins["1"].startswith("VCAP") else 3.0
        if target_ref == "U2" or abs(dx) > abs(dy):
            x, y = px + math.copysign(distance, dx), py
        else:
            x, y = px, py + math.copysign(distance, dy)
        part.xy = (x, y)
        pcb.update_footprint_position(part.ref, x, y)
    # TSOP supply capacitors can be just1.6mm apart. Put their references
    # outward beside the parts, respecting either memory orientation.
    for part in parts:
        if not part.decouples or not part.decouples.startswith("U2."):
            continue
        cap = pcb.get_footprint(part.ref)
        offset = math.copysign(2.8, cap.position[0] - memory.position[0])
        for prop in cap._sexp_node.find_children("property"):
            if prop.get_string(0) == "Reference":
                prop.get("at").set_value(0, offset)
                prop.get("at").set_value(1, 0)
    # External power and its return are driven by the attached regulated supply.
    for index, name in enumerate(("+5V", "GND", "+3V3_A")):
        flag = sch.add_symbol(
            "power:PWR_FLAG", x=304.8 + index * 20.32, y=25.4, ref=f"#FLG0{index + 1:02d}"
        )
        x, y = flag.pin_position("1")
        sch.add_wire((x, y), (x, y + 5.08))
        sch.add_label(name, x, y + 5.08)
    sch.write(output / "sdram_demo.kicad_sch")
    pcb.save(output / "sdram_demo.kicad_pcb")
    actual_stack = Stackup.from_pcb(PCB.load(output / "sdram_demo.kicad_pcb"))
    width = round(TransmissionLine(actual_stack).width_for_impedance(50, "F.Cu"), 3)
    groups = {
        "SDRAM_BYTE0": [*(f"DQ{i}" for i in range(8)), "LDQM"],
        "SDRAM_BYTE1": [*(f"DQ{i}" for i in range(8, 16)), "UDQM"],
        "SDRAM_ADDRESS": [*(f"A{i}" for i in range(12)), "BA0", "BA1"],
        "SDRAM_COMMAND": ["SDNWE", "SDNRAS", "SDNCAS", "SDNE1", "SDCKE1", "SDCLK"],
    }
    classes = {}
    for group, members in groups.items():
        for name in members:
            classes[name] = NetClassRouting(
                name=group,
                trace_width=width,
                clearance=routing_clearance,
                target_single_impedance=50,
                impedance_tolerance_percent=10,
                length_match_group=group,
                length_match_tolerance_mm=5.0,
            )
    (output / "net_class_map.json").write_text(json.dumps(net_class_map_to_dict(classes), indent=2))
    (output / "sdram_constraints.json").write_text(
        json.dumps(
            {
                "groups": groups,
                "group_skew_mm": 5.0,
                "clock_reference": "SDCLK",
                "clock_relative_tolerance_mm": 10.0,
                "max_trace_length_mm": 120.0,
                "target_impedance_ohm": 50.0,
                "impedance_tolerance_percent": 10.0,
                "stackup": "JLC04161H-3313",
                "trace_width_mm": width,
            },
            indent=2,
        )
    )
    project = create_minimal_project("sdram_demo")
    # Native KiCad and the router must enforce the same new-circuit geometry.
    project["net_settings"]["classes"][0].update(clearance=0.15, track_width=width)
    project["board"]["design_settings"]["rules"].update(
        min_clearance=0.15,
        min_track_width=0.15,
        min_via_diameter=0.6,
        min_through_hole_diameter=0.3,
        min_copper_edge_clearance=0.3,
        solder_mask_min_width=0.1,
    )
    save_project(project, output / "sdram_demo.kicad_pro")
    (output / "circuit.json").write_text(
        json.dumps(
            {
                "parts": [asdict(p) for p in parts],
                "pin_nets": assignments,
                "placement": {"U2_rotation_degrees": sdram_rotation},
                "routing_clearance_mm": routing_clearance,
                "strict_pad_clearance_required": True,
            },
            indent=2,
        )
    )
    (output / "status.json").write_text(
        json.dumps(
            {
                "status": "draft",
                "routed": False,
                "procurement_complete": False,
                "firmware_verified": False,
            },
            indent=2,
        )
    )
    print(f"Generated {len(parts)} real-package parts, {len(nets)} nets in {output}; NOT routed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sdram-rotation", type=int, choices=(0, 180), default=0)
    parser.add_argument("--routing-clearance", type=float, default=0.15)
    args = parser.parse_args()
    generate(args.output, args.sdram_rotation, args.routing_clearance)
