"""Revision-B sensored BLDC circuit; all pins and land patterns are real parts."""

import json
import shutil
from dataclasses import dataclass
from math import cos, radians, sin
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp

ROOT = Path(__file__).resolve().parent
R = "Resistor_SMD:R_0805_2012Metric"
C = "Capacitor_SMD:C_0805_2012Metric"
DRV_FP = "board05_revB:TI_PWP0028C_EP3.4x9.7_Mask3.1x5.18_ThermalVias"
LDO_FP = "Package_SO:HVSSOP-8-1EP_3x3mm_P0.65mm_EP1.57x1.89mm_ThermalVias"
# Installed KiCad models with the correct visible body and lead geometry.
# Their hidden exposed-pad dimensions are approximate; fabrication uses pads.
MODEL_ALIASES = {
    "TSSOP-28-1EP_4.4x9.7mm_P0.65mm_EP3.4x9.7mm_Mask3.1x4.05mm.step": "HTSSOP-28-1EP_4.4x9.7mm_P0.65mm_EP3.4x9.5mm.step",
    "HVSSOP-8-1EP_3x3mm_P0.65mm_EP1.57x1.89mm.step": "MSOP-8-1EP_3x3mm_P0.65mm_EP1.5x1.8mm.step",
}
MCU_PINS = {
    "1": "PWM_A",
    "2": "EN_A",
    "3": "GND",
    "4": "+5V",
    "5": "GND",
    "6": "+5V",
    "9": "PWM_B",
    "10": "PWM_C",
    "11": "EN_B",
    "12": "EN_C",
    "13": "DRIVER_SLEEP",
    "14": "nFAULT",
    "15": "MOSI",
    "16": "MISO",
    "17": "SCK",
    "18": "+5V",
    "20": "AREF",
    "21": "GND",
    "23": "HALL_A",
    "24": "HALL_B",
    "25": "HALL_C",
    "26": "SPEED",
    "28": "RUN",
    "29": "RESET",
    "30": "STATUS_LED",
    "32": "nTRIP",
}
DRV_PINS = {
    "1": "CPL",
    "2": "CPH",
    "3": "VCP",
    "4": "VM",
    "5": "PHASE_A",
    "6": "GND_SENSE",
    "7": "GND_SENSE",
    "8": "PHASE_B",
    "9": "PHASE_C",
    "10": "GND_SENSE",
    "11": "VM",
    "12": "IREF",
    "13": "SENSE_IN",
    "14": "GND",
    "15": "V3P3",
    "16": "+5V",
    "17": "DRIVER_SLEEP",
    "18": "nFAULT",
    "19": "nTRIP",
    "20": "GND",
    "22": "EN_C",
    "23": "PWM_C",
    "24": "EN_B",
    "25": "PWM_B",
    "26": "EN_A",
    "27": "PWM_A",
    "28": "GND",
    "29": "GND",
}
LDO_PINS = {"1": "+5V", "3": "RESET", "4": "GND", "5": "VM", "7": "PG_DELAY", "8": "VM", "9": "GND"}


@dataclass(frozen=True)
class Part:
    ref: str
    symbol: str
    value: str
    footprint: str
    nets: dict[str, str]
    xy: tuple[float, float]
    sch: tuple[float, float]
    mpn: str
    lcsc: str


def parts():
    result = [
        Part(
            "U1",
            "MCU_Microchip_ATmega:ATmega328P-A",
            "ATmega328P-AU",
            "Package_QFP:TQFP-32_7x7mm_P0.8mm",
            MCU_PINS,
            (47, 40),
            (63.5, 83.82),
            "ATMEGA328P-AU",
            "C14877",
        ),
        Part(
            "U2",
            "board05_revB:DRV8313PWPR",
            "DRV8313PWPR",
            DRV_FP,
            DRV_PINS,
            (25, 40),
            (165.1, 83.82),
            "DRV8313PWPR",
            "C92482",
        ),
        Part(
            "U3",
            "board05_revB:TPS7A1650DGNR",
            "TPS7A1650DGNR",
            LDO_FP,
            LDO_PINS,
            (48, 15),
            (266.7, 53.34),
            "TPS7A1650DGNR",
            "C468238",
        ),
        Part(
            "J1",
            "Connector_Generic:Conn_01x02",
            "12-24V INPUT",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            {"1": "VIN", "2": "GND"},
            (8, 8),
            (25.4, 160.02),
            "ZX-PZ2.54-1-2PZZ",
            "C7501260",
        ),
        Part(
            "J2",
            "Connector_Generic:Conn_01x03",
            "MOTOR ABC",
            "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            {"1": "PHASE_A", "2": "PHASE_B", "3": "PHASE_C"},
            (6, 37.5),
            (63.5, 160.02),
            "2.54-1*3P",
            "C49257",
        ),
        Part(
            "J3",
            "Connector_Generic:Conn_01x06",
            "HALL",
            "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
            {"1": "+5V", "2": "GND", "3": "HALL_A", "4": "HALL_B", "5": "HALL_C", "6": "GND"},
            (65, 30),
            (101.6, 160.02),
            "2.54-1*6P",
            "C37208",
        ),
        Part(
            "J4",
            "Connector_Generic:Conn_02x03_Odd_Even",
            "AVR ISP",
            "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical",
            {"1": "MISO", "2": "+5V", "3": "SCK", "4": "MOSI", "5": "RESET", "6": "GND"},
            (57, 56),
            (144.78, 160.02),
            "2.54-2*3P",
            "C65114",
        ),
        Part(
            "J5",
            "Connector_Generic:Conn_01x02",
            "RUN SWITCH",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            {"1": "RUN", "2": "GND"},
            (44, 68),
            (190.5, 160.02),
            "ZX-PZ2.54-1-2PZZ",
            "C7501260",
        ),
        Part(
            "RV1",
            "Device:R_Potentiometer",
            "10k",
            "Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical",
            {"1": "+5V", "2": "SPEED", "3": "GND"},
            (59, 71),
            (228.6, 160.02),
            "3296W-1-103LF",
            "C34846",
        ),
        Part(
            "F1",
            "Device:Fuse",
            "1A 63V",
            "Fuse:Fuse_1206_3216Metric",
            {"1": "VIN", "2": "FUSED"},
            (15, 8),
            (25.4, 198.12),
            "SF-1206S100-2",
            "C3167176",
        ),
        Part(
            "D1",
            "Device:D_Schottky",
            "SS36",
            "Diode_SMD:D_SMA",
            {"1": "VM", "2": "FUSED"},
            (23, 8),
            (63.5, 198.12),
            "SS36",
            "C16015",
        ),
        Part(
            "D2",
            "Device:D_Zener",
            "SMBJ24A",
            "Diode_SMD:D_SMB",
            {"1": "VM", "2": "GND"},
            (32, 8),
            (101.6, 198.12),
            "SMBJ24A",
            "C224017",
        ),
        Part(
            "D3",
            "Device:LED",
            "RED",
            "LED_SMD:LED_0805_2012Metric",
            {"1": "GND", "2": "LED_A"},
            (34, 69),
            (144.78, 198.12),
            "17-21SURC/S530-A2/TR8",
            "C131244",
        ),
    ]
    caps = [
        (
            "C1",
            "220uF 50V",
            "board05_revB:CP_Elec_8x10.5_PolarityMark",
            "VM",
            "GND",
            (12, 21),
            "EEEFT1H221AP",
            "C178594",
        ),
        ("C2", "100nF", C, "VM", "GND", (18, 39), "CC0805KRX7R9BB104", "C49678"),
        ("C3", "100nF", C, "VM", "GND", (18, 44), "CC0805KRX7R9BB104", "C49678"),
        ("C4", "10nF", C, "CPL", "CPH", (17, 32), "CL21B103KBANNNC", "C1710"),
        ("C5", "100nF", C, "VCP", "VM", (17, 35.5), "CC0805KRX7R9BB104", "C49678"),
        ("C6", "470nF", C, "V3P3", "GND", (30, 48), "CL21B474KBFNNNE", "C13967"),
        ("C7", "1uF", C, "VM", "GND", (40, 15), "CL21B105KBFNNNE", "C28323"),
        ("C8", "10uF", C, "+5V", "GND", (54, 15), "CL21A106KAYNNNE", "C15850"),
        ("C9", "100nF", C, "+5V", "GND", (39, 36), "CC0805KRX7R9BB104", "C49678"),
        ("C10", "100nF", C, "+5V", "GND", (39, 43), "CC0805KRX7R9BB104", "C49678"),
        ("C11", "100nF", C, "+5V", "GND", (55, 43), "CC0805KRX7R9BB104", "C49678"),
        ("C12", "100nF", C, "AREF", "GND", (55, 38), "CC0805KRX7R9BB104", "C49678"),
        ("C13", "10nF", C, "PG_DELAY", "GND", (48, 21), "CL21B103KBANNNC", "C1710"),
        ("C14", "100nF", C, "SPEED", "GND", (58, 66), "CC0805KRX7R9BB104", "C49678"),
        ("C15", "100nF", C, "IREF", "GND", (26, 58), "CC0805KRX7R9BB104", "C49678"),
        ("C16", "100nF", C, "+5V", "GND", (61, 26), "CC0805KRX7R9BB104", "C49678"),
    ]
    for i, (ref, value, fp, a, b, xy, mpn, lcsc) in enumerate(caps):
        result.append(
            Part(
                ref,
                "Device:C_Polarized" if ref == "C1" else "Device:C",
                value,
                fp,
                {"1": a, "2": b},
                xy,
                (266.7 + (i % 4) * 38.1, 101.6 + (i // 4) * 33.02),
                mpn,
                lcsc,
            )
        )
    resistors = [
        (
            "R1",
            "0.5R",
            "Resistor_SMD:R_2512_6332Metric",
            "GND_SENSE",
            "GND",
            (14, 50),
            "WSL2512R5000FEA",
            "C511023",
        ),
        ("R2", "56k", R, "V3P3", "IREF", (23, 54), "0805W8F5602T5E", "C17756"),
        ("R3", "10k", R, "IREF", "GND", (30, 54), "0805W8F1002T5E", "C17414"),
        ("R4", "1k", R, "GND_SENSE", "SENSE_IN", (18, 54), "0805W8F1001T5E", "C17513"),
        ("R5", "10k", R, "+5V", "RESET", (47, 29), "0805W8F1002T5E", "C17414"),
        ("R6", "10k", R, "+5V", "nFAULT", (40, 51), "0805W8F1002T5E", "C17414"),
        ("R7", "10k", R, "+5V", "nTRIP", (40, 56), "0805W8F1002T5E", "C17414"),
        ("R8", "10k", R, "DRIVER_SLEEP", "GND", (34, 50), "0805W8F1002T5E", "C17414"),
        ("R9", "10k", R, "+5V", "RUN", (46, 62), "0805W8F1002T5E", "C17414"),
        ("R10", "10k", R, "+5V", "HALL_A", (59, 31), "0805W8F1002T5E", "C17414"),
        ("R11", "10k", R, "+5V", "HALL_B", (59, 34), "0805W8F1002T5E", "C17414"),
        ("R12", "10k", R, "+5V", "HALL_C", (59, 47), "0805W8F1002T5E", "C17414"),
        ("R13", "1k", R, "STATUS_LED", "LED_A", (34, 64), "0805W8F1001T5E", "C17513"),
    ]
    for i, (ref, value, fp, a, b, xy, mpn, lcsc) in enumerate(resistors):
        result.append(
            Part(
                ref,
                "Device:R",
                value,
                fp,
                {"1": a, "2": b},
                xy,
                (25.4 + (i % 6) * 38.1, 223.52 + (i // 6) * 25.4),
                mpn,
                lcsc,
            )
        )
    return result


def write_library():
    """Datasheet pin functions, not a generic MCU/driver stand-in."""
    driver_names = [
        "CPL",
        "CPH",
        "VCP",
        "VM",
        "OUT1",
        "PGND1",
        "PGND2",
        "OUT2",
        "OUT3",
        "PGND3",
        "VM",
        "COMPP",
        "COMPN",
        "GND",
        "V3P3",
        "nRESET",
        "nSLEEP",
        "nFAULT",
        "nCOMPO",
        "GND",
        "NC",
        "EN3",
        "IN3",
        "EN2",
        "IN2",
        "EN1",
        "IN1",
        "GND",
        "EP",
    ]
    driver_types = {
        5: "output",
        8: "output",
        9: "output",
        15: "power_out",
        18: "open_collector",
        19: "open_collector",
        21: "no_connect",
    }
    for n in [4, 6, 7, 10, 11, 14, 20, 28, 29]:
        driver_types[n] = "power_in"
    regulator_names = ["OUT", "DNC", "PG", "GND", "EN", "NC", "DELAY", "IN", "EP"]
    regulator_types = {
        1: "power_out",
        2: "no_connect",
        3: "open_collector",
        4: "power_in",
        5: "input",
        6: "no_connect",
        7: "passive",
        8: "power_in",
        9: "power_in",
    }
    symbols = []
    for name, names, types, fp in [
        ("DRV8313PWPR", driver_names, driver_types, DRV_FP),
        ("TPS7A1650DGNR", regulator_names, regulator_types, LDO_FP),
    ]:
        count = len(names)
        rows = (count + 1) // 2
        half = rows * 2.54 / 2
        body = f'(symbol "{name}" (pin_names (offset 1.016)) (in_bom yes) (on_board yes) (property "Reference" "U" (at 0 {half + 5.08} 0) (effects (font (size 1.27 1.27)))) (property "Value" "{name}" (at 0 {half + 2.54} 0) (effects (font (size 1.27 1.27)))) (property "Footprint" "{fp}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)) (symbol "{name}_0_1" (rectangle (start -12.7 {half}) (end 12.7 {-half}) (stroke (width 0.254) (type default)) (fill (type background)))) (symbol "{name}_1_1" '
        for i, label in enumerate(names):
            left = i < rows
            row = i if left else i - rows
            x = -15.24 if left else 15.24
            y = half - 1.27 - row * 2.54
            angle = 0 if left else 180
            body += f'(pin {types.get(i + 1, "passive" if i + 1 <= 3 else "input")} line (at {x} {y} {angle}) (length 2.54) (name "{label}" (effects (font (size 1.27 1.27)))) (number "{i + 1}" (effects (font (size 1.27 1.27)))))'
        symbols.append(body + "))")
    path = ROOT / "board05_revB.kicad_sym"
    path.write_text(
        '(kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor") '
        + "".join(symbols)
        + ")"
    )
    return path


def build_schematic():
    library = write_library()
    sch = Schematic(
        title="Sensored BLDC Controller",
        revision="B",
        company="kicad-tools",
        paper="A3",
        local_symbol_libs=[library],
    )
    for part in parts():
        inst = sch.add_symbol(
            part.symbol,
            x=part.sch[0],
            y=part.sch[1],
            ref=part.ref,
            value=part.value,
            footprint=part.footprint,
        )
        connected_positions = set()
        for pin in inst.symbol_def.pins:
            number = pin.number
            x, y = inst.pin_position(number)
            if number not in part.nets:
                sch.add_no_connect(x, y)
                continue
            # Stacked power pins share one connection; pin direction, not location
            # relative to the body center, determines which way a stub extends.
            if (x, y) in connected_positions:
                continue
            connected_positions.add((x, y))
            dx, dy = -round(cos(radians(pin.angle))), round(sin(radians(pin.angle)))
            end = (round(x + 5.08 * dx, 4), round(y + 5.08 * dy, 4))
            rot = {(-1, 0): 0, (1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
            sch.add_wire((x, y), end)
            sch.add_global_label(part.nets[number], *end, shape="bidirectional", rotation=rot)
    for name, x in [("VIN", 266.7), ("VM", 292.1), ("GND", 317.5), ("GND_SENSE", 350.52)]:
        f = sch.add_pwr_flag(x, 261.62)
        sch.add_wire((f.x, f.y), (f.x + 5.08, f.y))
        sch.add_global_label(name, f.x + 5.08, f.y, shape="bidirectional", rotation=180)
    return sch


def build_pcb():
    pcb = PCB.create(width=70, height=90, layers=4, title="Sensored BLDC Controller", revision="B")
    # Equivalent rectangular outline, for legacy routing loader #4978 which
    # ignores gr_line outlines and silently substitutes Raspberry Pi HAT bounds.
    for edge in list(pcb._sexp.find_all("gr_line")):
        if edge.find("layer").get_string(0) == "Edge.Cuts":
            pcb._sexp.remove(edge)
    ox, oy = pcb.board_origin
    pcb._sexp.append(
        parse_string(
            f"(gr_rect (start {ox} {oy}) (end {ox + 70} {oy + 90}) "
            '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
        )
    )
    for part in parts():
        fp = (
            pcb.add_footprint_from_file(
                ROOT / "board05_revB.pretty" / (part.footprint.split(":")[1] + ".kicad_mod"),
                part.ref,
                *part.xy,
                value=part.value,
            )
            if part.footprint.startswith("board05_revB:")
            else pcb.add_footprint(part.footprint, part.ref, *part.xy, value=part.value)
        )
        for n in pcb._sexp.find_all("footprint"):
            if n.find("uuid").get_string(0) == fp.uuid:
                n.set_value(0, part.footprint)
                fp.name = part.footprint
                break
        for number, net in part.nets.items():
            assert pcb.assign_net_to_footprint_pad(part.ref, number, net), (part.ref, number)
    for footprint in pcb._sexp.find_all("footprint"):
        for model in footprint.find_all("model"):
            path = model.get_string(0)
            for old, new in MODEL_ALIASES.items():
                path = path.replace(old, new)
            model.set_value(0, path)
    return pcb


def generate(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    build_schematic().write(output / "bldc_controller.kicad_sch")
    schematic = parse_file(output / "bldc_controller.kicad_sch")
    identities = {part.ref: part for part in parts()}
    for symbol in schematic.find_all("symbol"):
        properties = {node.get_string(0): node for node in symbol.find_all("property")}
        reference = properties.get("Reference")
        if reference and reference.get_string(1) in identities:
            part = identities[reference.get_string(1)]
            for key, value in [("LCSC", part.lcsc), ("MPN", part.mpn)]:
                symbol.append(
                    parse_string(
                        f'(property "{key}" {json.dumps(value)} (at 0 0 0) '
                        "(effects (font (size 1.27 1.27)) hide))"
                    )
                )
    (output / "bldc_controller.kicad_sch").write_text(serialize_sexp(schematic))
    build_pcb().save(output / "bldc_controller.kicad_pcb")
    shutil.copy2(ROOT / "board05_revB.kicad_sym", output / "board05_revB.kicad_sym")
    shutil.copytree(
        ROOT / "board05_revB.pretty", output / "board05_revB.pretty", dirs_exist_ok=True
    )
    (output / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "board05_revB") (type "KiCad") (uri "${KIPRJMOD}/board05_revB.kicad_sym") (options "") (descr "Datasheet symbols")))'
    )
    (output / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "board05_revB") (type "KiCad") (uri "${KIPRJMOD}/board05_revB.pretty") (options "") (descr "TI DRV8313 land pattern")))'
    )
    (output / "bldc_controller.kicad_pro").write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_clearance": 0.127,
                            "min_through_hole_diameter": 0.2,
                            "min_track_width": 0.15,
                            "min_via_diameter": 0.45,
                            "min_via_annular_width": 0.1,
                            "min_via_hole": 0.2,
                            "min_copper_edge_clearance": 0.3,
                            "min_hole_to_hole": 0.5,
                        }
                    }
                },
                "net_settings": {
                    "classes": [
                        {
                            "name": "Default",
                            "clearance": 0.127,
                            "track_width": 0.2,
                            "via_diameter": 0.6,
                            "via_drill": 0.3,
                        }
                    ]
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    import sys

    generate(Path(sys.argv[1]))
