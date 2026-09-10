"""Build the ATtiny85 charlieplex circuit from one electrical component table."""

from pathlib import Path

from design_spec import LED_CONNECTIONS, MCU_PINS, RESISTOR_CONNECTIONS, RESISTOR_VALUE

from kicad_tools.schema.pcb import PCB
from kicad_tools.schematic.grid import GridSize
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

BOARD_WIDTH = 50.0
BOARD_HEIGHT = 55.0
R_FP = "Resistor_SMD:R_0805_2012Metric"
C_FP = "Capacitor_SMD:C_0805_2012Metric"
ISP_PINS = {"1": "LINE_D", "2": "VCC", "3": "ISP_SCK", "4": "LINE_C", "5": "RESET", "6": "GND"}


def components():
    """Reference, symbol, value, footprint, pad nets, PCB position, schematic position."""
    parts = [
        (
            "U1",
            "MCU_Microchip_ATtiny:ATtiny85-20P",
            "ATtiny85-20PU",
            "Package_DIP:DIP-8_W7.62mm",
            MCU_PINS,
            (20, 43),
            (50.8, 76.2),
        ),
        (
            "J1",
            "Connector_Generic:Conn_01x02",
            "POWER",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            {"1": "VCC", "2": "GND"},
            (5, 46),
            (25.4, 127),
        ),
        (
            "J2",
            "Connector_Generic:Conn_02x03_Odd_Even",
            "AVR-ISP",
            "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical",
            ISP_PINS,
            (43, 45),
            (76.2, 127),
        ),
        ("C1", "Device:C", "100nF", C_FP, {"1": "VCC", "2": "GND"}, (31, 40), (25.4, 38.1)),
        ("C2", "Device:C", "4.7uF", C_FP, {"1": "VCC", "2": "GND"}, (7, 42), (50.8, 38.1)),
        ("R5", "Device:R", "10k", R_FP, {"1": "VCC", "2": "RESET"}, (17, 40), (76.2, 38.1)),
    ]
    for i, resistor in enumerate(RESISTOR_CONNECTIONS):
        parts.append(
            (
                resistor.ref,
                "Device:R",
                RESISTOR_VALUE,
                R_FP,
                {"1": resistor.input_net, "2": resistor.output_net},
                (10 + i * 10, 36),
                (114.3, 50.8 + i * 25.4),
            )
        )
    for i, led in enumerate(LED_CONNECTIONS):
        row, col = divmod(i, 3)
        parts.append(
            (
                led.ref,
                "Device:LED",
                "LED",
                "LED_SMD:LED_0805_2012Metric",
                {"1": led.cathode_node, "2": led.anode_node},
                (15 + col * 10, 8 + row * 10),
                (165.1 + col * 38.1, 50.8 + row * 38.1),
            )
        )
    return parts


def build_schematic() -> Schematic:
    sch = Schematic(
        title="ATtiny85 Charlieplex LED Grid",
        revision="B",
        company="kicad-tools",
        snap_mode=SnapMode.AUTO,
        grid=GridSize.SCH_STANDARD.value,
    )
    for ref, symbol, value, footprint, nets, _, pos in components():
        instance = sch.add_symbol(
            symbol, x=pos[0], y=pos[1], ref=ref, value=value, footprint=footprint
        )
        for number, net in nets.items():
            x, y = instance.pin_position(number)
            dx, dy = x - instance.x, y - instance.y
            if abs(dx) >= abs(dy):
                end = (x + (5.08 if dx >= 0 else -5.08), y)
                rotation = 180 if dx >= 0 else 0
            else:
                end = (x, y + (5.08 if dy >= 0 else -5.08))
                rotation = 90 if dy < 0 else 270
            sch.add_wire((x, y), end)
            sch.add_global_label(net, *end, shape="bidirectional", rotation=rotation)
    # External regulated supply or the ISP programmer drives these rails.
    for net, x in [("VCC", 25.4), ("GND", 50.8)]:
        flag = sch.add_pwr_flag(x, 157.48)
        sch.add_wire((flag.x, flag.y), (flag.x + 5.08, flag.y))
        sch.add_global_label(net, flag.x + 5.08, flag.y, shape="bidirectional", rotation=180)
    return sch


def build_pcb() -> PCB:
    pcb = PCB.create(
        width=BOARD_WIDTH,
        height=BOARD_HEIGHT,
        layers=2,
        title="ATtiny85 Charlieplex LED Grid",
        revision="B",
    )
    for ref, _, value, footprint, nets, pos, _ in components():
        fp = pcb.add_footprint(footprint, ref, *pos, value=value)
        # Retain the full library ID for BOM and schematic/PCB comparison.
        for node in pcb._sexp.find_all("footprint"):
            if node.find("uuid").get_string(0) == fp.uuid:
                node.set_value(0, footprint)
                fp.name = footprint
                break
        for number, net in nets.items():
            if not pcb.assign_net_to_footprint_pad(ref, number, net):
                raise ValueError(f"Missing physical pad {ref}.{number}")
    return pcb


def write_schematic(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build_schematic().write(path)
    return path


def write_pcb(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build_pcb().save(path)
    return path
