#!/usr/bin/env python3
"""Generate board09's real circuit and placed four-layer PCB; no release claim.

Run with uv run python boards/09-usbc-pd-power/generate_design.py [OUTPUT].
Critical power routing and manufacturing release are separate checked stages.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.router.rules import NetClassRouting, net_class_map_to_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.sexp import SExp

ROOT = Path(__file__).resolve().parent
NAME = "usbc_pd_power"
R = "Resistor_SMD:R_0603_1608Metric"
C = "Capacitor_SMD:C_0603_1608Metric"


@dataclass
class Part:
    ref: str
    symbol: str
    footprint: str
    value: str
    pins: dict[str, str]
    xy: tuple[float, float]
    mpn: str = ""
    lcsc: str = ""
    rotation: int = 0
    assembly: str = "smt"


def parts() -> list[Part]:
    result = [
        Part(
            "J1",
            "Connector:USB_C_Receptacle_USB2.0_16P",
            "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            "USB-C PD INPUT",
            {
                **dict.fromkeys(["A1", "A12", "B1", "B12", "SH"], "GND"),
                **dict.fromkeys(["A4", "A9", "B4", "B9"], "VBUS_RAW"),
                "A5": "CC1",
                "B5": "CC2",
            },
            (10, 7),
            "TYPE-C-31-M-12",
            "C165948",
            180,
            "manual",
        ),
        Part(
            "U1",
            "Interface_USB:STUSB4500QTR",
            "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm",
            "STUSB4500QTR",
            {
                "1": "CC1",
                "2": "CC1",
                "4": "CC2",
                "5": "CC2",
                "6": "GND",
                "7": "SCL",
                "8": "SDA",
                "9": "DISCHARGE",
                "10": "GND",
                "12": "GND",
                "13": "GND",
                "14": "PD_OK3",
                "16": "SINK_GATE",
                "18": "VBUS_SENSE",
                "19": "PD_ALERT",
                "20": "PD_OK2",
                "21": "VREG_1V2",
                "22": "GND",
                "23": "VREG_2V7",
                "24": "VBUS_RAW",
                "25": "GND",
            },
            (18, 25),
            "STUSB4500QTR",
            "C2678061",
        ),
        Part(
            "Q1",
            "board09:AO4407A",
            "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "AO4407A",
            {
                **dict.fromkeys(["1", "2", "3"], "PMOS_SOURCE"),
                "4": "PMOS_GATE",
                **dict.fromkeys(["5", "6", "7", "8"], "VBUS_FUSED"),
            },
            (34, 18),
            "AO4407A",
            "C16072",
        ),
        Part(
            "Q2",
            "board09:AO4407A",
            "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "AO4407A",
            {
                **dict.fromkeys(["1", "2", "3"], "PMOS_SOURCE"),
                "4": "PMOS_GATE",
                **dict.fromkeys(["5", "6", "7", "8"], "VIN"),
            },
            (44, 18),
            "AO4407A",
            "C16072",
            180,
        ),
        Part(
            "U2",
            "Regulator_Switching:TPS54302",
            "Package_TO_SOT_SMD:TSOT-23-6",
            "TPS54302DDCR",
            {"1": "GND", "2": "SW", "3": "VIN", "4": "FB", "5": "BUCK_EN", "6": "BOOT"},
            (58, 29),
            "TPS54302DDCR",
            "C311983",
        ),
        Part(
            "L1",
            "Device:L",
            "board09:SRP7050TA",
            "10uH",
            {"1": "SW", "2": "VOUT_PRE"},
            (48, 27),
            "SRP7050TA-100M",
            "C2041441",
            90,
        ),
        Part(
            "RSH1",
            "Device:R_Shunt",
            "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm",
            "10m 1% 1W",
            {"1": "VOUT_PRE", "2": "KELVIN_P", "3": "KELVIN_N", "4": "+5V_OUT"},
            (70, 18),
            "WSK2512R0100FEA",
            "C3985410",
        ),
        Part(
            "U3",
            "Sensor_Energy:INA226",
            "Package_SO:MSOP-10_3x3mm_P0.5mm",
            "INA226AIDGSR",
            {
                "1": "GND",
                "2": "GND",
                "3": "MON_ALERT",
                "4": "SDA",
                "5": "SCL",
                "6": "+3V3",
                "7": "GND",
                "8": "+5V_OUT",
                "9": "SENSE_N",
                "10": "SENSE_P",
            },
            (74, 32),
            "INA226AIDGSR",
            "C49851",
        ),
        Part(
            "U4",
            "board09:TLV76033",
            "Package_TO_SOT_SMD:SOT-23",
            "TLV76033DBZR",
            {"1": "+3V3", "2": "VBUS_RAW", "3": "GND"},
            (16, 45),
            "TLV76033DBZR",
            "C2683368",
        ),
        Part(
            "J2",
            "Connector_Generic:Conn_01x02",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
            "5V OUTPUT",
            {"1": "+5V_OUT", "2": "GND"},
            (86, 16),
            "MKDS 1,5/2-5,08",
            "",
            0,
            "manual",
        ),
        Part(
            "J3",
            "Connector_Generic:Conn_01x06",
            "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
            "I2C HOST 3V3",
            {"1": "GND", "2": "SCL", "3": "SDA", "4": "+3V3", "5": "PD_ALERT", "6": "MON_ALERT"},
            (86, 42),
            "TSW-106-07-G-S",
            "",
            0,
            "manual",
        ),
        Part(
            "F1",
            "Device:Fuse",
            "Fuse:Fuse_1206_3216Metric",
            "2A 32V",
            {"1": "VBUS_RAW", "2": "VBUS_FUSED"},
            (24, 10),
            "0467002.NR",
            "",
        ),
        Part(
            "D1",
            "Device:D_Zener",
            "Diode_SMD:D_SOD-123",
            "12V",
            {"1": "PMOS_SOURCE", "2": "PMOS_GATE"},
            (39, 27),
            "BZT52C12",
            "",
        ),
        Part(
            "D2",
            "Device:LED",
            "LED_SMD:LED_0603_1608Metric",
            "OUTPUT",
            {"1": "GND", "2": "LED_A"},
            (86, 30),
            "LTST-C190KGKT",
            "C125094",
        ),
    ]
    resistors = [
        ("R1", "100k", "PMOS_SOURCE", "PMOS_GATE", (33, 27)),
        ("R2", "10k", "PMOS_GATE", "SINK_GATE", (29, 30)),
        ("R3", "1k", "VBUS_RAW", "VBUS_SENSE", (25, 22)),
        ("R4", "1k", "VIN", "DISCHARGE", (26, 26)),
        ("R5", "680k", "VIN", "BUCK_EN", (64, 35)),
        ("R6", "100k", "BUCK_EN", "GND", (64, 39)),
        ("R7", "100k 0.1%", "VOUT_PRE", "FB", (66, 25)),
        ("R8", "13.3k 0.1%", "FB", "GND", (66, 29)),
        ("R9", "49.9", "BOOT", "BOOT_CAP", (62, 24)),
        ("R10", "10", "KELVIN_P", "SENSE_P", (70, 26)),
        ("R11", "10", "KELVIN_N", "SENSE_N", (76, 26)),
        ("R12", "4.7k", "+3V3", "SCL", (30, 44)),
        ("R13", "4.7k", "+3V3", "SDA", (30, 48)),
        ("R14", "10k", "+3V3", "PD_ALERT", (36, 44)),
        ("R15", "10k", "+3V3", "MON_ALERT", (36, 48)),
        ("R16", "2.2k", "+5V_OUT", "LED_A", (86, 26)),
        ("R17", "10k", "+3V3", "PD_OK2", (24, 35)),
        ("R18", "10k", "+3V3", "PD_OK3", (30, 35)),
    ]
    for ref, value, a, b, xy in resistors:
        fp = "Resistor_SMD:R_1206_3216Metric" if ref in {"R3", "R4"} else R
        result.append(Part(ref, "Device:R", fp, value, {"1": a, "2": b}, xy))
    capacitors = [
        ("C1", "1u 50V", "VBUS_RAW", "GND", (15, 18), C),
        ("C2", "1u 16V", "VREG_1V2", "GND", (20, 19), C),
        ("C3", "1u 16V", "VREG_2V7", "GND", (10, 23), C),
        ("C4", "4.7n 50V", "PMOS_SOURCE", "PMOS_GATE", (39, 31), C),
        ("C5", "10u 50V", "VIN", "GND", (53, 29), "Capacitor_SMD:C_1206_3216Metric"),
        ("C6", "100n 50V", "VIN", "GND", (55.15, 29), C),
        ("C7", "100n 50V", "BOOT_CAP", "SW", (62, 20), C),
        ("C8", "22u 25V", "VOUT_PRE", "GND", (50, 16), "Capacitor_SMD:C_1206_3216Metric"),
        ("C9", "22u 25V", "VOUT_PRE", "GND", (56, 16), "Capacitor_SMD:C_1206_3216Metric"),
        ("C10", "75p C0G", "VOUT_PRE", "FB", (66, 22), C),
        ("C11", "100n 50V", "SENSE_P", "SENSE_N", (79, 29), C),
        ("C12", "100n 50V", "+3V3", "GND", (79, 34), C),
        ("C13", "1u 50V", "VBUS_RAW", "GND", (11, 43), C),
        ("C14", "1u 16V", "+3V3", "GND", (20, 43), C),
        ("C15", "1u 16V", "+5V_OUT", "GND", (81, 20), C),
    ]
    for ref, value, a, b, xy, fp in capacitors:
        result.append(
            Part(
                ref,
                "Device:C",
                fp,
                value,
                {"1": a, "2": b},
                xy,
                rotation=90 if ref in {"C5", "C6"} else 0,
            )
        )
    # The catalog binds exact MPNs later; blank entries stay explicit blockers.
    catalog_path = ROOT / "procurement.json"
    catalog = json.loads(catalog_path.read_text()).get("parts", {}) if catalog_path.exists() else {}
    for part in result:
        selected = catalog.get(part.ref, catalog.get(part.value, {}))
        part.mpn = selected.get("mpn", part.mpn)
        part.lcsc = selected.get("lcsc", part.lcsc)
    return result


def write_library(output: Path) -> Path:
    """Two manufacturer-pinout symbols absent from the installed stock library."""
    specs = [
        ("TLV76033", ["OUT", "IN", "GND"], {1: "power_out", 2: "power_in", 3: "power_in"}, "U"),
        ("AO4407A", ["S", "S", "S", "G", "D", "D", "D", "D"], {4: "input"}, "Q"),
    ]
    symbols = []
    for name, names, types, ref in specs:
        rows = (len(names) + 1) // 2
        half = (rows + 1) * 1.27
        body = f'(symbol "{name}" (pin_names (offset 1.016)) (in_bom yes) (on_board yes) (property "Reference" "{ref}" (at 0 {half + 3.81} 0) (effects (font (size 1.27 1.27)))) (property "Value" "{name}" (at 0 {half + 1.27} 0) (effects (font (size 1.27 1.27)))) (symbol "{name}_0_1" (rectangle (start -7.62 {half}) (end 7.62 {-half}) (stroke (width 0.254) (type default)) (fill (type background)))) (symbol "{name}_1_1" '
        for i, label in enumerate(names):
            left = i < rows
            x = -10.16 if left else 10.16
            y = half - 2.54 - (i if left else i - rows) * 2.54
            body += f'(pin {types.get(i + 1, "passive")} line (at {x} {y} {0 if left else 180}) (length 2.54) (name "{label}" (effects (font (size 1.27 1.27)))) (number "{i + 1}" (effects (font (size 1.27 1.27)))))'
        symbols.append(body + "))")
    path = output / "board09.kicad_sym"
    path.write_text(
        '(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor") '
        + "".join(symbols)
        + ")"
    )
    return path


def write_inductor(output: Path) -> Path:
    library = output / "board09.pretty"
    library.mkdir(exist_ok=True)
    # Bourns SRP7050TA manufacturer land pattern:8.4 overall,2.5x3.5 pads.
    data = """(footprint "SRP7050TA" (version 20240108) (generator pcbnew) (layer "F.Cu")
      (descr "Bourns SRP7050TA, manufacturer recommended 8.4mm overall pad span") (attr smd)
      (fp_text reference "REF**" (at 0 -4.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
      (fp_text value "SRP7050TA" (at 0 4.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
      (fp_rect (start -3.65 -3.3) (end 3.65 3.3) (stroke (width 0.1) (type default)) (fill none) (layer "F.Fab"))
      (fp_rect (start -4.45 -3.6) (end 4.45 3.6) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
      (fp_line (start -2 -3.5) (end 2 -3.5) (stroke (width 0.15) (type default)) (layer "F.SilkS"))
      (fp_line (start -2 3.5) (end 2 3.5) (stroke (width 0.15) (type default)) (layer "F.SilkS"))
      (pad "1" smd roundrect (at -2.95 0) (size 2.5 3.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.1))
      (pad "2" smd roundrect (at 2.95 0) (size 2.5 3.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.1)))"""
    path = library / "SRP7050TA.kicad_mod"
    path.write_text(data)
    return path


def add_critical_copper(pcb: PCB) -> None:
    """Reviewed geometry for the local bypass loop and switch-node escape.

    The short TSOT escape is narrower than the power trunk. Its current and
    temperature limits still require review; these connections are not a 3 A
    qualification or a complete power-stage route.
    """
    pcb.add_trace(("U2", "3"), ("C6", "1"), width=0.45, net="VIN")
    pcb.add_trace(("C6", "1"), ("C5", "1"), width=0.8, net="VIN")
    pcb.add_trace(("U2", "1"), ("C6", "2"), width=0.45, net="GND")
    pcb.add_trace(("C6", "2"), ("C5", "2"), width=0.8, net="GND")
    # Escape between C6's lands; keep vias outside the solderable pads.
    pcb.add_trace(("U2", "2"), (54.15, 29), width=0.35, net="SW")
    for x in (54.15, 53.35, 52.55):
        pcb.add_via(x, 29, net="SW")
    pcb.add_trace((54.15, 29), (52.55, 29), width=0.6, net="SW")
    for x in (46.8, 47.6, 48.4):
        pcb.add_via(x, 31.6, net="SW")
        pcb.add_trace((x, 31.6), (x, 29.95), width=0.6, net="SW")
    pcb.add_trace((46.8, 31.6), (48.4, 31.6), width=0.6, net="SW")
    pcb.add_trace(
        (54.15, 29), (48.4, 31.6), width=2.0, layer="B.Cu", waypoints=[(51, 29)], net="SW"
    )
    pcb.add_trace((48.4, 31.6), (46.8, 31.6), width=2.0, layer="B.Cu", net="SW")
    # Dedicated shunt sense terminals: never tap the 3 A force pads.
    pcb.add_trace(
        ("RSH1", "2"),
        ("R10", "1"),
        width=0.25,
        net="KELVIN_P",
        waypoints=[(66.57, 20), (69.225, 22.655)],
    )
    pcb.add_trace(
        ("RSH1", "3"),
        ("R11", "1"),
        width=0.25,
        net="KELVIN_N",
        waypoints=[(73.43, 15.8), (75.5, 15.8), (75.5, 21), (75.225, 21.275)],
    )
    pcb.add_trace(
        ("R10", "2"),
        ("C11", "1"),
        width=0.25,
        net="SENSE_P",
        waypoints=[(72.775, 28), (77.225, 28)],
    )
    pcb.add_trace(("C11", "1"), ("U3", "10"), width=0.25, net="SENSE_P", waypoints=[(78.225, 31)])
    pcb.add_trace(
        ("R11", "2"),
        ("C11", "2"),
        width=0.25,
        net="SENSE_N",
        waypoints=[(78.5, 26), (80.5, 28), (80.5, 29)],
    )
    pcb.add_trace(
        ("C11", "2"), ("U3", "9"), width=0.25, net="SENSE_N", waypoints=[(80.5, 29), (80.5, 31.5)]
    )


def generate(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    library = write_library(output)
    inductor = write_inductor(output)
    sch = Schematic(
        title="USB-C PD 5V power supply — development",
        paper="A1",
        revision="A",
        company="kicad-tools",
        project_name=NAME,
        local_symbol_libs=[library],
    )
    pcb = PCB.create(width=100, height=65, layers=4, title="USB-C PD 5V supply — development")
    # Explicit rectangle avoids inferred-outline fallback in the router loader.
    edges = pcb._sexp.find_children("gr_line")
    coords = [
        (n.get(k).get_float(0), n.get(k).get_float(1)) for n in edges for k in ["start", "end"]
    ]
    rect = SExp("gr_rect").add(
        SExp("start").add(min(x for x, y in coords)).add(min(y for x, y in coords))
    )
    rect.add(SExp("end").add(max(x for x, y in coords)).add(max(y for x, y in coords)))
    rect.add(SExp("stroke").add(SExp("width").add(0.1)).add(SExp("type").add("default")))
    rect.add(SExp("fill").add("none")).add(SExp("layer").add("Edge.Cuts"))
    pcb._sexp.children = [n for n in pcb._sexp.children if n not in edges]
    pcb._sexp.add(rect)
    # Explicit JLC7628 stack; both inner layers are reserved continuous ground.
    for node in pcb._sexp.get("setup").get("stackup").find_children("layer"):
        name = node.get_atoms()[0]
        if name in ["dielectric 1", "dielectric 3"]:
            node.get("thickness").set_value(0, 0.2104)
            node.get("epsilon_r").set_value(0, 4.6)
        elif name == "dielectric 2":
            node.get("thickness").set_value(0, 1.065)
        elif name in ["In1.Cu", "In2.Cu"]:
            node.get("thickness").set_value(0, 0.0152)
    components = parts()
    net_names = sorted({n for p in components for n in p.pins.values()})
    for n in net_names:
        pcb.add_net(n)
    assignments = {}
    for i, part in enumerate(components):
        # A widely spaced pin-labelled sheet is electrically auditable while
        # a later grouped schematic presentation can be generated separately.
        x = 50.8 + (i % 8) * 96.52
        y = 55.88 + (i // 8) * 73.66
        symbol = sch.add_symbol(
            part.symbol, x=x, y=y, ref=part.ref, value=part.value, footprint=part.footprint
        )
        symbol.properties["Manufacturer_Part_Number"] = part.mpn
        symbol.properties["LCSC"] = part.lcsc
        known = {p.number for p in symbol.symbol_def.pins}
        assert set(part.pins) <= known, (part.ref, set(part.pins) - known)
        for pin in symbol.symbol_def.pins:
            px, py = symbol.pin_position(pin.number)
            if pin.number not in part.pins:
                sch.add_no_connect(px, py)
                continue
            angle = math.radians(pin.angle)
            end = (round(px - 5.08 * math.cos(angle), 3), round(py + 5.08 * math.sin(angle), 3))
            sch.add_wire((px, py), end)
            sch.add_label(part.pins[pin.number], *end)
        if part.ref == "L1":
            fp = pcb.add_footprint_from_file(inductor, part.ref, *part.xy, value=part.value)
        else:
            fp = pcb.add_footprint(part.footprint, part.ref, *part.xy, value=part.value)
        fp.name = part.footprint
        fp._sexp_node.set_value(0, part.footprint)
        actual = {p.number for p in fp.pads if p.number}
        assert actual == known, (part.ref, "symbol/land-pattern mismatch", actual ^ known)
        for pad in fp.pads:
            if pad.number in part.pins:
                pcb.assign_net_to_footprint_pad(part.ref, pad.number, part.pins[pad.number])
            pad.rotation += part.rotation
        fp.rotation = part.rotation
        if part.ref in {"C5", "C6"}:
            # Move the rotated reference text out of the compact bypass loop.
            for node in fp._sexp_node.children:
                if node.name in {"property", "fp_text"} and node.get_atoms()[0] in {
                    "Reference",
                    "reference",
                }:
                    at = node.get("at")
                    at.set_value(0, 4 if part.ref == "C5" else -4)
                    at.set_value(1, 0)
                    at.set_value(2, 0)
        assignments[part.ref] = part.pins
    for i, name in enumerate(["VBUS_RAW", "GND", "VIN", "VOUT_PRE"]):
        flag = sch.add_symbol("power:PWR_FLAG", x=50.8 + i * 50.8, y=566.42, ref=f"#FLG0{i + 1}")
        pos = flag.pin_position("1")
        end = (pos[0], pos[1] + 5.08)
        sch.add_wire(pos, end)
        sch.add_label(name, *end)
    add_critical_copper(pcb)
    sch.write(output / f"{NAME}.kicad_sch")
    pcb.save(output / f"{NAME}.kicad_pcb")
    pro = create_minimal_project(NAME)
    pro.setdefault("text_variables", {})["KCT_PRESERVE_BOARD_RULES"] = "1"
    pro["board"]["design_settings"]["rules"].update(
        min_clearance=0.15,
        min_track_width=0.15,
        min_via_diameter=0.6,
        min_via_hole=0.3,
        min_copper_edge_clearance=0.3,
    )
    pro["net_settings"]["classes"][0].update(
        clearance=0.15, track_width=0.25, via_diameter=0.6, via_drill=0.3
    )
    save_project(pro, output / f"{NAME}.kicad_pro")
    (output / "sym-lib-table").write_text(
        '(sym_lib_table (lib (name "board09") (type "KiCad") (uri "${KIPRJMOD}/board09.kicad_sym") (options "") (descr "Board09 reviewed pinouts")))\n'
    )
    (output / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "board09") (type "KiCad") (uri "${KIPRJMOD}/board09.pretty") (options "") (descr "Bourns land pattern")))\n'
    )
    classes = {
        n: NetClassRouting(name="SIGNAL", trace_width=0.25, clearance=0.15, avoid_layers=[1, 2])
        for n in net_names
    }
    for name in ["VBUS_RAW", "VBUS_FUSED", "PMOS_SOURCE", "VIN"]:
        classes[name] = NetClassRouting(
            name="INPUT_POWER",
            trace_width=1.2,
            clearance=0.2,
            target_ampacity=1.5,
            avoid_layers=[1, 2],
        )
    for name in ["SW", "VOUT_PRE", "+5V_OUT"]:
        classes[name] = NetClassRouting(
            name="OUTPUT_POWER",
            trace_width=2.0,
            clearance=0.2,
            target_ampacity=3.0,
            avoid_layers=[1, 2],
        )
    classes["GND"] = NetClassRouting(name="GROUND", trace_width=0.5, clearance=0.15)
    (output / "net_class_map.json").write_text(
        json.dumps(net_class_map_to_dict(classes), indent=2) + "\n"
    )
    result = {
        "parts": [asdict(p) for p in components],
        "pin_nets": assignments,
        "nets": net_names,
        "status": "placed with partial critical copper; routing incomplete",
        "hardware_tested": False,
        "manufacturing_ready": False,
    }
    (output / "circuit.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "readiness.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "assembly",
                "status": "blocked",
                "blockers": [
                    "Development circuit: routing, electrical/procurement review and manufacturing export are incomplete."
                ],
                "checks": [],
                "inputs": {},
                "evidence": {},
            },
            indent=2,
        )
        + "\n"
    )
    print(
        f"Generated {len(components)} real-package components / {len(net_names)} nets; not a manufacturing release."
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "output")
    generate(parser.parse_args().output)
