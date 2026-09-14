"""Revision B: real ATmega32U4-AU USB joystick hardware, shared by both generators.

Pin numbers follow Microchip 7766J, figure 1-1 and section 21.3.1.
No footprint pin may be reassigned to improve routing. See DESIGN_REVIEW.md.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    ref: str
    symbol: str
    value: str
    footprint: str
    pins: dict[str, str]
    position: tuple[float, float, float]
    mpn: str
    manufacturer: str


MCU_PINS = {
    "2": "VCC",
    "3": "USB_MCU_D-",
    "4": "USB_MCU_D+",
    "5": "GND",
    "6": "UCAP",
    "7": "VBUS",
    "9": "ISP_SCK",
    "10": "ISP_MOSI",
    "11": "ISP_MISO",
    "13": "RESET",
    "14": "VCC",
    "15": "GND",
    "16": "XTAL2",
    "17": "XTAL1",
    "18": "BTN1",
    "19": "BTN2",
    "20": "BTN3",
    "21": "BTN4",
    "23": "GND",
    "24": "VCC",
    "25": "JOY_BTN",
    "33": "HWB",
    "34": "VCC",
    "35": "GND",
    "40": "JOY_Y",
    "41": "JOY_X",
    "42": "AREF",
    "43": "GND",
    "44": "VCC",
}
USB_PINS = {
    **dict.fromkeys(["A1", "A12", "B1", "B12", "SH"], "GND"),
    **dict.fromkeys(["A4", "A9", "B4", "B9"], "VBUS"),
    "A5": "USB_CC1",
    "B5": "USB_CC2",
    "A6": "USB_D+",
    "B6": "USB_D+",
    "A7": "USB_D-",
    "B7": "USB_D-",
}
R_FP = "Resistor_SMD:R_0603_1608Metric"
C_FP = "Capacitor_SMD:C_0603_1608Metric"
SW_FP = "Button_Switch_SMD:SW_SPST_TL3342"


def resistor(ref, value, a, b, x, y, angle=0):
    code = {"22": "22R", "5.1k": "5K1", "10k": "10K", "1k": "1K"}[value]
    return Component(
        ref,
        "Device:R",
        value,
        "Resistor_SMD:R_0402_1005Metric" if value == "22" else R_FP,
        {"1": a, "2": b},
        (x, y, angle),
        f"RC{'0402' if value == '22' else '0603'}FR-07{code}L",
        "Yageo",
    )


def capacitor(ref, value, net, x, y, angle=0):
    part = {
        "100nF": "GRM188R71C104KA01D",
        "1uF": "GRM188R71A105KA61D",
        "4.7uF": "GRM188R61A475KE15D",
        "15pF": "GRM1885C1H150JA01D",
        "10nF": "GRM188R71H103KA01D",
    }[value]
    return Component(
        ref, "Device:C", value, C_FP, {"1": net, "2": "GND"}, (x, y, angle), part, "Murata"
    )


COMPONENTS = [
    Component(
        "U1",
        "MCU_Microchip_ATmega:ATmega32U4-A",
        "ATMEGA32U4-AU",
        "Package_QFP:TQFP-44_10x10mm_P0.8mm",
        MCU_PINS,
        (38, 28, 270),
        "ATMEGA32U4-AU",
        "Microchip",
    ),
    Component(
        "J1",
        "Connector:USB_C_Receptacle_USB2.0_16P",
        "USB4085-GF-A",
        "Connector_USB:USB_C_Receptacle_GCT_USB4085",
        USB_PINS,
        (40.975, 5.8, 180),
        "USB4085-GF-A",
        "GCT",
    ),
    Component(
        "U2",
        "Power_Protection:USBLC6-2SC6",
        "USBLC6-2SC6",
        "Package_TO_SOT_SMD:SOT-23-6",
        {"1": "USB_D-", "6": "USB_D-", "3": "USB_D+", "4": "USB_D+", "2": "GND", "5": "VBUS"},
        (38, 10.5, 270),
        "USBLC6-2SC6",
        "STMicroelectronics",
    ),
    Component(
        "F1",
        "Device:Polyfuse",
        "350mA",
        "Fuse:Fuse_1206_3216Metric",
        {"1": "VBUS", "2": "VCC"},
        (49, 10, 0),
        "1206L035/16YR",
        "Littelfuse",
    ),
    Component(
        "Y1",
        "Device:Crystal_GND24",
        "8MHz",
        "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
        {"1": "XTAL1", "3": "XTAL2", "2": "GND", "4": "GND"},
        (28, 28, 0),
        "ECS-80-12-33-JGN-TR",
        "ECS",
    ),
    Component(
        "J2",
        "Connector_Generic:Conn_01x05",
        "Joystick 5V X Y SW",
        "Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal",
        {"1": "VCC", "2": "GND", "3": "JOY_X_RAW", "4": "JOY_Y_RAW", "5": "JOY_BTN"},
        (15, 42, 180),
        "S5B-PH-SM4-TB(LF)(SN)",
        "JST",
    ),
    Component(
        "J3",
        "Connector_Generic:Conn_02x03_Odd_Even",
        "AVR ISP",
        "Joystick:Samtec_TSM-103-01-T-DV",
        {"1": "ISP_MISO", "2": "VCC", "3": "ISP_SCK", "4": "ISP_MOSI", "5": "RESET", "6": "GND"},
        (55, 18, 0),
        "TSM-103-01-T-DV-P-TR",
        "Samtec",
    ),
    resistor("R1", "5.1k", "USB_CC1", "GND", 30, 9, 90),
    resistor("R2", "5.1k", "USB_CC2", "GND", 45, 9, 90),
    resistor("R3", "22", "USB_D-", "USB_MCU_D-", 40.55, 18, 270),
    resistor("R4", "22", "USB_D+", "USB_MCU_D+", 39.45, 18, 270),
    resistor("R5", "10k", "VCC", "RESET", 50, 31),
    resistor("R6", "10k", "HWB", "GND", 38, 39),
    resistor("R10", "1k", "JOY_X_RAW", "JOY_X", 25, 32),
    resistor("R11", "1k", "JOY_Y_RAW", "JOY_Y", 25, 35),
    capacitor("C1", "100nF", "VCC", 43, 19),
    capacitor("C2", "100nF", "VCC", 29, 24),
    capacitor("C3", "100nF", "VCC", 47, 22),
    capacitor("C4", "100nF", "VCC", 47, 34),
    capacitor("C5", "100nF", "VCC", 34, 37),
    capacitor("C6", "1uF", "UCAP", 37, 18),
    capacitor("C7", "100nF", "AREF", 47, 31),
    capacitor("C8", "15pF", "XTAL1", 23, 30),
    capacitor("C9", "15pF", "XTAL2", 23, 26),
    capacitor("C10", "10nF", "JOY_X", 22, 32),
    capacitor("C11", "10nF", "JOY_Y", 22, 35),
    capacitor("C12", "4.7uF", "VBUS", 55, 10),
    capacitor("C13", "100nF", "VBUS", 42, 11),
]
for index, net in enumerate(["BTN1", "BTN2", "BTN3", "BTN4", "JOY_BTN"], 1):
    if index <= 4:
        COMPONENTS.append(
            Component(
                f"SW{index}",
                "Switch:SW_Push",
                "TL3342F160QG",
                SW_FP,
                {"1": net, "2": "GND"},
                (50 + ((index - 1) % 2) * 12, 42 + ((index - 1) // 2) * 10, 0),
                "TL3342F160QG",
                "E-Switch",
            )
        )
    COMPONENTS.append(
        resistor(f"R{11 + index}", "10k", "VCC", net, 40 + (index - 1) * 4, 46 if index < 5 else 37)
    )
COMPONENTS.append(
    Component(
        "SW5",
        "Switch:SW_Push",
        "TL3342F160QG",
        SW_FP,
        {"1": "RESET", "2": "GND"},
        (63, 29, 0),
        "TL3342F160QG",
        "E-Switch",
    )
)

NETS = {
    "": 0,
    **{
        name: i + 1
        for i, name in enumerate(sorted({net for comp in COMPONENTS for net in comp.pins.values()}))
    },
}
