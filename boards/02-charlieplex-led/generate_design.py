#!/usr/bin/env python3
"""
Charlieplex LED Grid - Complete Design Generation

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with MCU, resistors, and LED matrix
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design is a 3x3 charlieplexed LED grid driven by 4 GPIO pins,
demonstrating how N pins can drive N*(N-1) LEDs.

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import subprocess
import sys
import uuid
from pathlib import Path

from design_spec import (
    LED_CONNECTIONS,
    MCU_PINS,
    NETS,
    RESISTOR_CONNECTIONS,
    RESISTOR_VALUE,
)

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.grid import GridSize
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# =============================================================================
# Schematic Generation
# =============================================================================

# Wire stub length for connecting pins to labels
WIRE_STUB = 5.08  # 200 mils


def add_pin_label(sch: Schematic, pin_pos: tuple, net_name: str, direction: str = "right"):
    """Add a wire stub from a pin position to a global label."""
    if not pin_pos:
        return

    x, y = pin_pos
    if direction == "right":
        end_x = x + WIRE_STUB
        rotation = 180
    else:
        end_x = x - WIRE_STUB
        rotation = 0

    sch.add_wire((x, y), (end_x, y))
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation)


def create_charlieplex_schematic(output_dir: Path) -> Path:
    """
    Create a 3x3 charlieplex LED grid schematic.

    Returns the path to the generated schematic file.
    """
    print("\n" + "=" * 60)
    print("Creating Charlieplex LED Grid Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="Charlieplex LED Grid",
        date="2025-01",
        revision="A",
        company="kicad-tools Demo",
        comment1="3x3 LED matrix using charlieplexing technique",
        comment2="9 LEDs driven by 4 GPIO pins",
        snap_mode=SnapMode.AUTO,
        grid=GridSize.SCH_STANDARD.value,
    )

    # =========================================================================
    # Section 1: Place MCU
    # =========================================================================
    print("\n1. Placing MCU...")

    mcu_x, mcu_y = 50.8, 88.9
    mcu = sch.add_symbol(
        "Connector_Generic:Conn_01x08",
        x=mcu_x,
        y=mcu_y,
        ref="U1",
        value="MCU",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
    )
    print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")

    for pin_num, net_name in MCU_PINS.items():
        pin_pos = mcu.pin_position(pin_num)
        if net_name:
            add_pin_label(sch, pin_pos, net_name, direction="right")
        else:
            if pin_pos:
                sch.add_no_connect(pin_pos[0], pin_pos[1])

    # =========================================================================
    # Section 2: Place Resistors
    # =========================================================================
    print("\n2. Placing resistors...")

    resistor_base_x = 101.6
    resistor_base_y = 63.5
    resistor_spacing = 12.7

    for i, resistor in enumerate(RESISTOR_CONNECTIONS):
        x = resistor_base_x
        y = resistor_base_y + i * resistor_spacing

        r = sch.add_symbol(
            "Device:R", x=x, y=y, ref=resistor.ref, value=RESISTOR_VALUE, auto_footprint=True
        )
        print(f"   {resistor.ref}: placed at ({r.x}, {r.y})")

        pin1_pos = r.pin_position("1")
        pin2_pos = r.pin_position("2")
        add_pin_label(sch, pin1_pos, resistor.input_net, direction="left")
        add_pin_label(sch, pin2_pos, resistor.output_net, direction="right")

    # =========================================================================
    # Section 3: Place LEDs
    # =========================================================================
    print("\n3. Placing LEDs in 3x3 grid...")

    led_start_x = 152.4
    led_start_y = 50.8
    led_spacing_x = 25.4
    led_spacing_y = 25.4

    for i, led_conn in enumerate(LED_CONNECTIONS):
        row = i // 3
        col = i % 3
        x = led_start_x + col * led_spacing_x
        y = led_start_y + row * led_spacing_y

        led = sch.add_symbol(
            "Device:LED",
            x=x,
            y=y,
            ref=led_conn.ref,
            value="LED",
            footprint="LED_SMD:LED_0805_2012Metric",
        )
        print(f"   {led_conn.ref}: placed at ({led.x}, {led.y})")

        pin1_pos = led.pin_position("1")
        pin2_pos = led.pin_position("2")
        add_pin_label(sch, pin1_pos, led_conn.cathode_node, direction="left")
        add_pin_label(sch, pin2_pos, led_conn.anode_node, direction="right")

    # =========================================================================
    # Section 4: Power Symbols
    # =========================================================================
    print("\n4. Adding power symbols...")

    vcc_pwr = sch.add_power("power:VCC", x=25.4, y=25.4, rotation=0)
    vcc_conn = (vcc_pwr.x, vcc_pwr.y)
    sch.add_wire(vcc_conn, (vcc_conn[0] + WIRE_STUB, vcc_conn[1]))
    sch.add_global_label("VCC", vcc_conn[0] + WIRE_STUB, vcc_conn[1], shape="input", rotation=180)

    gnd_pwr = sch.add_power("power:GND", x=25.4, y=50.8, rotation=180)
    gnd_conn = (gnd_pwr.x, gnd_pwr.y)
    sch.add_wire(gnd_conn, (gnd_conn[0] + WIRE_STUB, gnd_conn[1]))
    sch.add_global_label("GND", gnd_conn[0] + WIRE_STUB, gnd_conn[1], shape="input", rotation=180)

    sch.add_pwr_flag(vcc_pwr.x, vcc_pwr.y)
    sch.add_pwr_flag(gnd_pwr.x, gnd_pwr.y)
    print("   Added VCC and GND power symbols with PWR_FLAG")

    # =========================================================================
    # Section 5: Validate and Write
    # =========================================================================
    print("\n5. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings")

    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")

    print("\n6. Writing schematic...")
    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "charlieplex_3x3.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


# =============================================================================
# PCB Generation
# =============================================================================

# Board dimensions (mm)
BOARD_WIDTH = 50.0
BOARD_HEIGHT = 55.0
BOARD_ORIGIN_X = 100.0
BOARD_ORIGIN_Y = 100.0

# Component positions
MCU_POS = (BOARD_ORIGIN_X + 25, BOARD_ORIGIN_Y + 47)

RESISTOR_POSITIONS = [
    (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 38),
    (BOARD_ORIGIN_X + 18, BOARD_ORIGIN_Y + 38),
    (BOARD_ORIGIN_X + 32, BOARD_ORIGIN_Y + 38),
    (BOARD_ORIGIN_X + 42, BOARD_ORIGIN_Y + 38),
]

LED_SPACING = 8.0
LED_START_X = BOARD_ORIGIN_X + 17
LED_START_Y = BOARD_ORIGIN_Y + 10
LED_POSITIONS = [
    (LED_START_X + i * LED_SPACING, LED_START_Y + j * LED_SPACING)
    for j in range(3)
    for i in range(3)
]


def create_charlieplex_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the charlieplex LED grid.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating Charlieplex LED Grid PCB...")
    print("=" * 60)

    def generate_header() -> str:
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
        lines = ['  (net 0 "")']
        for name, num in NETS.items():
            if num > 0:
                lines.append(f'  (net {num} "{name}")')
        return "\n".join(lines)

    def generate_board_outline() -> str:
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

    def generate_mcu() -> str:
        x, y = MCU_POS
        row_spacing = 7.62 / 2
        pin_pitch = 2.54

        pin_nets = [
            (1, "LINE_A"), (2, "LINE_B"), (3, "LINE_C"), (4, "LINE_D"),
            (5, ""), (6, ""), (7, "VCC"), (8, "GND"),
        ]

        pads = []
        for i in range(4):
            pin_num, net_name = pin_nets[i]
            net_num = NETS.get(net_name, 0)
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = -1.5 * pin_pitch + i * pin_pitch
            pads.append(f'    (pad "{pin_num}" thru_hole rect (at {-row_spacing:.3f} {py:.3f}) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net_str})')

        for i in range(4):
            pin_num, net_name = pin_nets[4 + i]
            net_num = NETS.get(net_name, 0)
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = 1.5 * pin_pitch - i * pin_pitch
            pads.append(f'    (pad "{pin_num}" thru_hole oval (at {row_spacing:.3f} {py:.3f}) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net_str})')

        pads_str = "\n".join(pads)
        return f"""  (footprint "Package_DIP:DIP-8_W7.62mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U1" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "MCU" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""

    def generate_resistor(ref: str, pos: tuple, input_net: str, output_net: str) -> str:
        x, y = pos
        input_num = NETS[input_net]
        output_num = NETS[output_net]
        pad_offset = 1.0

        return f"""  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{RESISTOR_VALUE}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at {-pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {input_num} "{input_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {output_num} "{output_net}"))
  )"""

    def generate_led(ref: str, pos: tuple, anode_net: str, cathode_net: str) -> str:
        x, y = pos
        anode_num = NETS[anode_net]
        cathode_num = NETS[cathode_net]
        pad_offset = 1.0

        return f"""  (footprint "LED_SMD:LED_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "LED" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (pad "1" smd roundrect (at {-pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {cathode_num} "{cathode_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {anode_num} "{anode_net}"))
  )"""

    # Build the PCB file
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
        generate_mcu(),
    ]

    print("\n1. Adding footprints...")
    print(f"   U1 (MCU) at {MCU_POS}")

    for i, resistor in enumerate(RESISTOR_CONNECTIONS):
        pos = RESISTOR_POSITIONS[i]
        parts.append(generate_resistor(resistor.ref, pos, resistor.input_net, resistor.output_net))
        print(f"   {resistor.ref} at {pos}")

    for i, (pos, led_conn) in enumerate(zip(LED_POSITIONS, LED_CONNECTIONS, strict=False)):
        parts.append(generate_led(led_conn.ref, pos, led_conn.anode_node, led_conn.cathode_node))

    print(f"   D1-D9 (LEDs) in 3x3 grid")

    parts.append(")")

    pcb_content = "\n".join(parts)

    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "charlieplex_3x3.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   Components: 1 MCU, 4 resistors, 9 LEDs")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])}")

    return pcb_path


# =============================================================================
# Project, ERC, Routing, DRC
# =============================================================================

def create_project(output_dir: Path, project_name: str) -> Path:
    """Create a KiCad project file."""
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


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB using the autorouter."""
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.optimizer import GridCollisionChecker, OptimizationConfig, TraceOptimizer

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # Design rules - grid must be <= clearance/2 for DRC compliance
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.3,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
    )

    # Skip power nets
    skip_nets = ["VCC", "GND"]

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")
    print(f"   Skipping nets: {skip_nets}")

    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")

    print("\n2. Routing nets...")
    router.route_all()

    stats_before = router.get_statistics()

    print("\n3. Raw routing results:")
    print(f"   Routes: {stats_before['routes']}")
    print(f"   Segments: {stats_before['segments']}")
    print(f"   Vias: {stats_before['vias']}")

    print("\n4. Optimizing traces...")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    collision_checker = GridCollisionChecker(router.grid)
    optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

    optimized_routes = []
    for route in router.routes:
        optimized_route = optimizer.optimize_route(route)
        optimized_routes.append(optimized_route)
    router.routes = optimized_routes

    stats = router.get_statistics()

    print("\n5. Final routing results:")
    print(f"   Routes: {stats['routes']}")
    print(f"   Segments: {stats['segments']}")
    print(f"   Vias: {stats['vias']}")
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

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

    total_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_nets

    if success:
        print("\n   SUCCESS: All nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {stats['nets_routed']}/{total_nets} nets")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB."""
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb_path)],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


# =============================================================================
# Main Entry Point
# =============================================================================

def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "charlieplex_3x3")

        # Step 2: Create schematic
        sch_path = create_charlieplex_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_charlieplex_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "charlieplex_3x3_routed.kicad_pcb"
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
        print("\nCharlieplex LED mapping:")
        print("  LED   Anode    Cathode")
        for led_conn in LED_CONNECTIONS:
            print(f"  {led_conn.ref}    {led_conn.anode_node}  {led_conn.cathode_node}")

        return 0 if erc_success and route_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
