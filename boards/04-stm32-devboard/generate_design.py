#!/usr/bin/env python3
"""
STM32 Development Board - End-to-End Example

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with power rails and components
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design includes:
- LDO voltage regulator (5V to 3.3V)
- 8MHz crystal oscillator
- SWD debug header
- User LED indicator

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import subprocess
import sys
import uuid
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.blocks import (
    CrystalOscillator,
    DebugHeader,
    LEDIndicator,
)
from kicad_tools.schematic.models.schematic import Schematic

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def create_stm32_schematic(output_dir: Path) -> Path:
    """
    Create an STM32 development board schematic.

    This demonstrates the workflow for creating a simple MCU board with:
    - Power rails (5V, 3.3V, GND)
    - LDO voltage regulator (manually added)
    - 8MHz crystal oscillator (using CrystalOscillator block)
    - SWD debug header (using DebugHeader block)
    - User LED (using LEDIndicator block)

    The schematic is organized with power on the left, peripherals in the center,
    and debug interface on the right.
    """
    print("Creating STM32 Development Board schematic...")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="STM32F103C8 Development Board",
        date="2025-01",
        revision="A",
        company="kicad-tools Example",
        comment1="End-to-end design example",
        comment2="Demonstrates circuit blocks API",
    )

    # Define power rail Y coordinates for organized layout
    RAIL_5V = 30  # 5V input power
    RAIL_3V3 = 50  # 3.3V regulated
    RAIL_GND = 200  # Ground

    # Schematic boundaries
    X_LEFT = 25
    X_RIGHT = 280

    # =========================================================================
    # Section 1: Power Rails
    # =========================================================================
    print("\n1. Creating power rails...")

    # Add power rails - endpoints should match actual component connection points
    # to avoid floating wire endpoints. For T-connections, use add_segmented_rail()
    # or ensure rail endpoints align with component tap points.
    #
    # Rail endpoints based on component positions:
    # - 5V: Power symbol (25) to LDO VIN (~93)
    # - 3.3V: LDO VOUT (~108) to debug header (~245)
    # - GND: Power symbol (25) to debug header (~245)
    sch.add_rail(RAIL_5V, x_start=X_LEFT, x_end=93, net_label="+5V")
    sch.add_rail(RAIL_3V3, x_start=80, x_end=245, net_label="+3.3V")
    sch.add_rail(RAIL_GND, x_start=X_LEFT, x_end=245, net_label="GND")
    print("   Added +5V, +3.3V, and GND rails")

    # Add power symbols
    sch.add_power("power:+5V", x=X_LEFT, y=RAIL_5V - 10, rotation=0)
    sch.add_power("power:+3V3", x=80, y=RAIL_3V3 - 10, rotation=0)
    sch.add_power("power:GND", x=X_LEFT, y=RAIL_GND + 10, rotation=0)
    print("   Added power symbols")

    # =========================================================================
    # Section 2: LDO Voltage Regulator (Manual Component Placement)
    # =========================================================================
    print("\n2. Adding LDO voltage regulator...")

    # Note: The LDOBlock requires specific symbol libraries. Here we
    # demonstrate manual component placement as an alternative.

    # Add LDO symbol (using a generic 3-terminal regulator)
    ldo = sch.add_symbol(
        "Regulator_Linear:AMS1117-3.3",
        x=100,
        y=100,
        ref="U1",
        value="AMS1117-3.3",
    )
    print(f"   LDO: {ldo.reference}")

    # Add input capacitor
    c_in = sch.add_symbol(
        "Device:C_Small",
        x=65,
        y=100,
        ref="C1",
        value="10uF",
    )
    print(f"   Input cap: {c_in.reference} = 10uF")

    # Add output capacitors
    c_out1 = sch.add_symbol(
        "Device:C_Small",
        x=135,
        y=100,
        ref="C2",
        value="10uF",
    )
    c_out2 = sch.add_symbol(
        "Device:C_Small",
        x=150,
        y=100,
        ref="C3",
        value="100nF",
    )
    print(f"   Output caps: {c_out1.reference} = 10uF, {c_out2.reference} = 100nF")

    # Wire LDO to power rails
    # VIN to 5V rail
    vin_pos = ldo.pin_position("VI")
    sch.add_wire(vin_pos, (vin_pos[0], RAIL_5V))
    sch.add_junction(vin_pos[0], RAIL_5V)

    # VOUT to 3.3V rail
    vout_pos = ldo.pin_position("VO")
    sch.add_wire(vout_pos, (vout_pos[0], RAIL_3V3))
    sch.add_junction(vout_pos[0], RAIL_3V3)

    # GND to ground rail
    gnd_pos = ldo.pin_position("GND")
    sch.add_wire(gnd_pos, (gnd_pos[0], RAIL_GND))
    sch.add_junction(gnd_pos[0], RAIL_GND)

    # Wire decoupling capacitors
    sch.wire_decoupling_cap(c_in, RAIL_5V, RAIL_GND)
    sch.wire_decoupling_cap(c_out1, RAIL_3V3, RAIL_GND)
    sch.wire_decoupling_cap(c_out2, RAIL_3V3, RAIL_GND)
    print("   Wired LDO and decoupling caps to power rails")

    # =========================================================================
    # Section 3: Crystal Oscillator (8MHz)
    # =========================================================================
    print("\n3. Adding 8MHz crystal oscillator...")

    # Crystal with load capacitors (using the CrystalOscillator block)
    xtal = CrystalOscillator(
        sch,
        x=200,
        y=100,
        frequency="8MHz",
        load_caps="20pF",
        ref_prefix="Y",
        cap_ref_start=10,
    )
    print(f"   Crystal: {xtal.crystal.reference} with C10, C11")

    # Connect crystal ground to GND rail
    xtal.connect_to_rails(gnd_rail_y=RAIL_GND)

    # Add labels for oscillator connections
    # Wire stubs must be added BEFORE labels to avoid "label not on wire" warnings
    in_pos = xtal.port("IN")
    out_pos = xtal.port("OUT")
    sch.add_wire(in_pos, (in_pos[0] - 10, in_pos[1]))
    sch.add_label("OSC_IN", in_pos[0] - 10, in_pos[1], rotation=0)
    sch.add_wire(out_pos, (out_pos[0] + 10, out_pos[1]))
    sch.add_label("OSC_OUT", out_pos[0] + 10, out_pos[1], rotation=0)
    print("   Added OSC_IN and OSC_OUT labels")

    # =========================================================================
    # Section 4: Debug Header (SWD)
    # =========================================================================
    print("\n4. Adding SWD debug header...")

    # 6-pin SWD header
    debug = DebugHeader(
        sch,
        x=250,
        y=100,
        interface="swd",
        pins=6,
        series_resistors=False,
        ref="J1",
    )
    print(f"   Debug header: {debug.header.reference} (SWD-6)")

    # Connect debug header power to rails
    debug.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)

    # =========================================================================
    # Section 5: User LED
    # =========================================================================
    print("\n5. Adding user LED...")

    # LED with current-limiting resistor
    led = LEDIndicator(
        sch,
        x=175,
        y=140,
        ref_prefix="D1",
        label="USER",
        resistor_value="330R",
    )
    print(f"   LED: {led.led.reference} with current-limiting resistor")

    # Connect LED to power rails
    led.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)

    # =========================================================================
    # Section 6: Design Notes
    # =========================================================================
    print("\n6. Adding design notes...")

    sch.add_text(
        "Design Notes:\n"
        "1. Add STM32F103C8T6 MCU from KiCad library\n"
        "2. Connect OSC_IN/OSC_OUT to PA0/PA1\n"
        "3. Connect SWDIO/SWCLK to PA13/PA14\n"
        "4. Connect USER LED to PA5\n"
        "5. Add reset button between NRST and GND",
        x=X_LEFT,
        y=230,
    )

    # =========================================================================
    # Validate Schematic
    # =========================================================================
    print("\n7. Validating schematic...")

    # Run validation
    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors:")
        for err in errors[:5]:
            print(f"      - {err['message']}")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings (floating wires expected)")

    # Get statistics
    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")
    print(f"      Labels: {stats['label_count']}")

    # =========================================================================
    # Write Output Files
    # =========================================================================
    print("\n8. Writing output files...")

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write schematic
    sch_path = output_dir / "stm32_devboard.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


def create_project(output_dir: Path, project_name: str) -> Path:
    """
    Create a KiCad project file.

    Returns the path to the generated project file.
    """
    print("\n" + "=" * 60)
    print("Creating Project File...")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{project_name}.kicad_pro"
    project_data = create_minimal_project(filename)

    project_path = output_dir / filename
    save_project(project_data, project_path)
    print(f"\n   Project: {project_path}")

    return project_path


def run_erc(sch_path: Path) -> bool:
    """Run ERC on the schematic."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.cli.runner import run_erc as kicad_run_erc
    from kicad_tools.erc import ERCReport

    print("\n" + "=" * 60)
    print("Running ERC...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("\n   WARNING: kicad-cli not found - skipping ERC")
        return True

    result = kicad_run_erc(sch_path)

    if not result.success:
        print(f"\n   Error running ERC: {result.stderr}")
        return False

    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"\n   Error parsing ERC report: {e}")
        return False
    finally:
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    violations = [v for v in report.violations if not v.excluded]
    error_count = sum(1 for v in violations if v.is_error)

    if error_count > 0:
        print(f"\n   Found {error_count} ERC errors:")
        for v in [v for v in violations if v.is_error][:5]:
            print(f"      - [{v.type_str}] {v.description}")
        return False
    else:
        print("\n   No ERC errors found!")
        return True


def create_stm32_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the STM32 development board.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating STM32 Development Board PCB...")
    print("=" * 60)

    # Board dimensions (mm) - from project.kct spec
    BOARD_WIDTH = 50.0
    BOARD_HEIGHT = 25.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Net definitions - must match schematic nets
    NETS = {
        "": 0,
        "+5V": 1,
        "+3.3V": 2,
        "GND": 3,
        "OSC_IN": 4,
        "OSC_OUT": 5,
        "SWDIO": 6,
        "SWCLK": 7,
        "SWO": 8,
        "NRST": 9,
        "USER_LED": 10,
    }

    # Component positions for a sensible layout
    # Left side: Power input and regulation
    # Center: Crystal and LED
    # Right side: Debug header
    # Spacing increased to avoid pad overlap (min 3mm between components)
    U1_POS = (BOARD_ORIGIN_X + 10, BOARD_ORIGIN_Y + 10)  # LDO
    C1_POS = (BOARD_ORIGIN_X + 4, BOARD_ORIGIN_Y + 18)  # Input cap
    C2_POS = (BOARD_ORIGIN_X + 18, BOARD_ORIGIN_Y + 10)  # Output cap 1
    C3_POS = (BOARD_ORIGIN_X + 18, BOARD_ORIGIN_Y + 15)  # Output cap 2
    Y1_POS = (BOARD_ORIGIN_X + 28, BOARD_ORIGIN_Y + 10)  # Crystal
    C10_POS = (BOARD_ORIGIN_X + 28, BOARD_ORIGIN_Y + 16)  # Crystal cap 1
    C11_POS = (BOARD_ORIGIN_X + 28, BOARD_ORIGIN_Y + 21)  # Crystal cap 2
    D1_POS = (BOARD_ORIGIN_X + 38, BOARD_ORIGIN_Y + 12)  # LED
    R1_POS = (BOARD_ORIGIN_X + 38, BOARD_ORIGIN_Y + 6)  # LED resistor
    J1_POS = (BOARD_ORIGIN_X + 46, BOARD_ORIGIN_Y + 12)  # Debug header

    def generate_header() -> str:
        """Generate the PCB file header."""
        return """(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-demo")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )"""

    def generate_nets() -> str:
        """Generate net definitions."""
        lines = ['  (net 0 "")']
        for name, num in NETS.items():
            if num > 0:
                lines.append(f'  (net {num} "{name}")')
        return "\n".join(lines)

    def generate_board_outline() -> str:
        """Generate the board outline (Edge.Cuts)."""
        x1 = BOARD_ORIGIN_X
        y1 = BOARD_ORIGIN_Y
        x2 = BOARD_ORIGIN_X + BOARD_WIDTH
        y2 = BOARD_ORIGIN_Y + BOARD_HEIGHT
        return f"""  (gr_rect (start {x1} {y1}) (end {x2} {y2})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "{generate_uuid()}")
  )"""

    def generate_sot223(ref: str, pos: tuple, value: str) -> str:
        """Generate SOT-223 footprint for LDO."""
        x, y = pos
        return f"""  (footprint "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -3.15 2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+5V"]} "+5V"))
    (pad "2" smd rect (at -3.15 0) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
    (pad "3" smd rect (at -3.15 -2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+3.3V"]} "+3.3V"))
    (pad "2" smd rect (at 3.15 0) (size 2 3.8) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
  )"""

    def generate_cap_0805(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate 0805 capacitor footprint."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Capacitor_SMD:C_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_crystal_hc49(ref: str, pos: tuple, value: str) -> str:
        """Generate HC49 crystal footprint."""
        x, y = pos
        return f"""  (footprint "Crystal:Crystal_HC49-4H_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole circle (at -2.44 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["OSC_IN"]} "OSC_IN"))
    (pad "2" thru_hole circle (at 2.44 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["OSC_OUT"]} "OSC_OUT"))
  )"""

    def generate_led_0805(ref: str, pos: tuple) -> str:
        """Generate 0805 LED footprint."""
        x, y = pos
        return f"""  (footprint "LED_SMD:LED_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "LED" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {NETS["USER_LED"]} "USER_LED"))
    (pad "2" smd roundrect (at 1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {NETS["GND"]} "GND"))
  )"""

    def generate_resistor_0805(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate 0805 resistor footprint."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_pin_header_6(ref: str, pos: tuple) -> str:
        """Generate 6-pin header footprint for SWD debug."""
        x, y = pos
        pitch = 2.54
        return f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -8) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "SWD" (at 0 8) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at 0 {-2.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["+3.3V"]} "+3.3V"))
    (pad "2" thru_hole oval (at 0 {-1.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["SWDIO"]} "SWDIO"))
    (pad "3" thru_hole oval (at 0 {-0.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["SWCLK"]} "SWCLK"))
    (pad "4" thru_hole oval (at 0 {0.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["SWO"]} "SWO"))
    (pad "5" thru_hole oval (at 0 {1.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["NRST"]} "NRST"))
    (pad "6" thru_hole oval (at 0 {2.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["GND"]} "GND"))
  )"""

    # Build the PCB file
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    print("\n1. Adding footprints...")

    # U1: LDO voltage regulator
    parts.append(generate_sot223("U1", U1_POS, "AMS1117-3.3"))
    print(f"   U1 (LDO) at {U1_POS}")

    # C1: Input capacitor (5V to GND)
    parts.append(generate_cap_0805("C1", C1_POS, "10uF", "+5V", "GND"))
    print(f"   C1 (10uF) at {C1_POS}")

    # C2, C3: Output capacitors (3.3V to GND)
    parts.append(generate_cap_0805("C2", C2_POS, "10uF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C3", C3_POS, "100nF", "+3.3V", "GND"))
    print(f"   C2 (10uF) at {C2_POS}")
    print(f"   C3 (100nF) at {C3_POS}")

    # Y1: Crystal oscillator
    parts.append(generate_crystal_hc49("Y1", Y1_POS, "8MHz"))
    print(f"   Y1 (8MHz) at {Y1_POS}")

    # C10, C11: Crystal load capacitors
    parts.append(generate_cap_0805("C10", C10_POS, "20pF", "OSC_IN", "GND"))
    parts.append(generate_cap_0805("C11", C11_POS, "20pF", "OSC_OUT", "GND"))
    print(f"   C10, C11 (20pF) at {C10_POS}, {C11_POS}")

    # R1: LED current-limiting resistor
    parts.append(generate_resistor_0805("R1", R1_POS, "330R", "+3.3V", "USER_LED"))
    print(f"   R1 (330R) at {R1_POS}")

    # D1: User LED
    parts.append(generate_led_0805("D1", D1_POS))
    print(f"   D1 (LED) at {D1_POS}")

    # J1: SWD debug header
    parts.append(generate_pin_header_6("J1", J1_POS))
    print(f"   J1 (SWD header) at {J1_POS}")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "stm32_devboard.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   Components: 1 LDO, 5 caps, 1 crystal, 1 resistor, 1 LED, 1 header")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])}")

    return pcb_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """
    Route the PCB using the autorouter.

    Returns True if all nets were routed successfully.
    """
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # Configure design rules (from project.kct spec)
    # Grid resolution must be <= clearance/2 for reliable DRC compliance
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Skip power nets (route manually or use planes)
    skip_nets = ["+5V", "+3.3V", "GND"]

    # Load the PCB
    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")
    print(f"   Skipping power nets: {skip_nets}")

    # Route all nets
    print("\n2. Routing nets...")
    router.route_all()

    # Get statistics before optimization
    stats_before = router.get_statistics()

    print("\n3. Raw routing results:")
    print(f"   Routes: {stats_before['routes']}")
    print(f"   Segments: {stats_before['segments']}")
    print(f"   Vias: {stats_before['vias']}")

    # Optimize traces
    print("\n4. Optimizing traces...")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    optimizer = TraceOptimizer(config=opt_config)

    optimized_routes = []
    for route in router.routes:
        optimized_route = optimizer.optimize_route(route)
        optimized_routes.append(optimized_route)
    router.routes = optimized_routes

    # Get final statistics
    stats = router.get_statistics()

    print("\n5. Final routing results:")
    print(f"   Routes: {stats['routes']}")
    print(f"   Segments: {stats['segments']}")
    print(f"   Vias: {stats['vias']}")
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

    # Save routed PCB
    print(f"\n6. Saving routed PCB: {output_path}")

    original_content = input_path.read_text()
    route_sexp = router.to_sexp()

    if route_sexp:
        output_content = original_content.rstrip().rstrip(")")
        output_content += "\n"
        output_content += f"  {route_sexp}\n"
        output_content += ")\n"
    else:
        output_content = original_content
        print("   Warning: No routes generated!")

    output_path.write_text(output_content)

    # Calculate success - we skipped power nets, so only count signal nets
    total_signal_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_signal_nets

    if success:
        print("\n   SUCCESS: All signal nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {stats['nets_routed']}/{total_signal_nets} signal nets")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB using kct check for consistent results."""
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb_path)],
            capture_output=True,
            text=True,
        )

        # Print the output
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        # Check for success
        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


def main() -> int:
    """Main entry point."""
    # Determine output directory
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "stm32_devboard")

        # Step 2: Create schematic
        sch_path = create_stm32_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_stm32_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "stm32_devboard_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"\nOutput directory: {output_dir.absolute()}")
        print("\nGenerated files:")
        print(f"  1. Project: {project_path.name}")
        print(f"  2. Schematic: {sch_path.name}")
        print(f"  3. PCB (unrouted): {pcb_path.name}")
        print(f"  4. PCB (routed): {routed_path.name}")
        print("\nResults:")
        print(f"  ERC: {'PASS' if erc_success else 'FAIL'}")
        print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
        print(f"  DRC: {'PASS' if drc_success else 'FAIL'}")
        print("\nBoard description:")
        print("  - U1: AMS1117-3.3 LDO (5V to 3.3V)")
        print("  - C1-C3: Decoupling capacitors")
        print("  - Y1: 8MHz crystal oscillator")
        print("  - C10-C11: Crystal load capacitors")
        print("  - R1, D1: User LED with resistor")
        print("  - J1: 6-pin SWD debug header")

        # For this demo board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
