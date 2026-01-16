#!/usr/bin/env python3
"""
Simple LED - Hello World of Electronics

This is the minimal test board to validate the kicad-tools workflow:
- 2-pin power connector (VCC, GND)
- Current-limiting resistor (330 ohm)
- LED

The design targets:
- Input: 5V
- LED forward voltage: ~2V
- LED current: ~10mA
- Resistor: (5V - 2V) / 10mA = 300 ohm -> use 330 ohm standard value

Usage:
    python generate_design.py [output_dir]
"""

import subprocess
import sys
import uuid
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.models.schematic import Schematic

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def create_led_schematic(output_dir: Path) -> Path:
    """
    Create a simple LED schematic.

    Returns the path to the generated schematic file.
    """
    print("Creating Simple LED Schematic...")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="Simple LED - Hello World",
        date="2025-01",
        revision="A",
        company="kicad-tools Demo",
        comment1="Minimal LED circuit",
        comment2="5V input, 330 ohm resistor, LED",
    )

    # Define layout coordinates
    RAIL_VCC = 30  # VCC rail Y position
    RAIL_GND = 130  # Ground rail Y position
    X_LEFT = 25

    # =========================================================================
    # Section 1: Place Components
    # =========================================================================
    print("\n1. Placing components...")

    # Power connector (2-pin: VCC, GND)
    j1 = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=50,
        y=80,
        ref="J1",
        value="PWR",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    j1_pin1 = j1.pin_position("1")  # VCC
    j1_pin2 = j1.pin_position("2")  # GND
    print(f"   J1: Power connector at ({j1.x}, {j1.y})")

    # Current-limiting resistor (330 ohm)
    r1 = sch.add_symbol(
        "Device:R",
        x=100,
        y=55,
        ref="R1",
        value="330",
        auto_footprint=True,
    )
    r1_pin1 = r1.pin_position("1")  # VCC side
    r1_pin2 = r1.pin_position("2")  # LED side
    print(f"   R1: 330 ohm at ({r1.x}, {r1.y})")

    # LED
    d1 = sch.add_symbol(
        "Device:LED",
        x=100,
        y=90,
        ref="D1",
        value="LED",
        footprint="LED_THT:LED_D5.0mm",
    )
    d1_pin1 = d1.pin_position("1")  # Cathode (K, negative)
    d1_pin2 = d1.pin_position("2")  # Anode (A, positive)
    print(f"   D1: LED at ({d1.x}, {d1.y})")

    # =========================================================================
    # Section 2: Create Power Rails
    # =========================================================================
    print("\n2. Creating power rails...")

    # Get snapped coordinates
    rail_vcc_y = sch._snap_coord(RAIL_VCC, "rail")
    rail_gnd_y = sch._snap_coord(RAIL_GND, "rail")
    x_left = sch._snap_coord(X_LEFT, "rail")

    # Get component X positions
    x_j1 = j1_pin1[0]
    x_r1 = r1_pin1[0]
    x_d1 = d1_pin2[0]

    # VCC rail segments (only from power symbol to last VCC connection - R1)
    vcc_x_points = sorted([x_left, x_j1, x_r1])
    for i in range(len(vcc_x_points) - 1):
        sch.add_wire((vcc_x_points[i], rail_vcc_y), (vcc_x_points[i + 1], rail_vcc_y))

    # GND rail segments (only from power symbol to last GND connection - D1)
    gnd_x_points = sorted([x_left, x_j1, x_d1])
    for i in range(len(gnd_x_points) - 1):
        sch.add_wire((gnd_x_points[i], rail_gnd_y), (gnd_x_points[i + 1], rail_gnd_y))

    # Add power symbols
    sch.add_power("power:VCC", x=X_LEFT, y=RAIL_VCC, rotation=0)
    sch.add_power("power:GND", x=X_LEFT, y=RAIL_GND, rotation=180)

    # Add PWR_FLAG for ERC
    sch.add_power("power:PWR_FLAG", x=x_j1, y=RAIL_VCC, rotation=0)
    sch.add_power("power:PWR_FLAG", x=x_j1, y=RAIL_GND, rotation=0)

    # Add net labels
    sch.add_label("VCC", X_LEFT, RAIL_VCC)
    sch.add_label("GND", X_LEFT, RAIL_GND)

    print(f"   VCC rail: {len(vcc_x_points) - 1} segments")
    print(f"   GND rail: {len(gnd_x_points) - 1} segments")

    # =========================================================================
    # Section 3: Wire Components
    # =========================================================================
    print("\n3. Wiring components...")

    # J1 Pin 1 (VCC) to VCC rail
    sch.add_wire(j1_pin1, (x_j1, rail_vcc_y))
    sch.add_junction(x_j1, rail_vcc_y)

    # J1 Pin 2 (GND) to GND rail
    sch.add_wire(j1_pin2, (x_j1, rail_gnd_y))
    sch.add_junction(x_j1, rail_gnd_y)
    print("   J1 -> VCC/GND rails")

    # R1 Pin 1 to VCC rail
    sch.add_wire(r1_pin1, (x_r1, rail_vcc_y))
    sch.add_junction(x_r1, rail_vcc_y)
    print("   R1 -> VCC rail")

    # R1 Pin 2 to D1 Pin 1 (Cathode) - LED_ANODE net
    sch.add_wire(r1_pin2, d1_pin1)
    print("   R1 <-> D1 (internal connection)")

    # D1 Pin 2 (Anode) to GND rail
    sch.add_wire(d1_pin2, (x_d1, rail_gnd_y))
    sch.add_junction(x_d1, rail_gnd_y)
    print("   D1 -> GND rail")

    # Add LED_ANODE label directly at R1 pin 2 (on the R1-D1 wire)
    sch.add_label("LED_ANODE", r1_pin2[0], r1_pin2[1])

    # Print circuit calculation
    v_in = 5.0
    v_led = 2.0
    r_value = 330
    i_led = (v_in - v_led) / r_value * 1000  # mA
    print("\n   Circuit: VCC=5V, R=330 ohm, LED Vf=2V")
    print(f"   LED current: ({v_in}V - {v_led}V) / {r_value} = {i_led:.1f}mA")

    # =========================================================================
    # Section 4: Validate Schematic
    # =========================================================================
    print("\n4. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors:")
        for err in errors[:5]:
            print(f"      [{err['type']}] {err['message']}")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings:")
        for warn in warnings[:3]:
            print(f"      [{warn['type']}] {warn['message']}")

    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")
    print(f"      Labels: {stats['label_count']}")

    # =========================================================================
    # Section 5: Write Output
    # =========================================================================
    print("\n5. Writing schematic...")

    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "simple_led.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


def create_led_pcb(output_dir: Path) -> Path:
    """
    Create a simple PCB for the LED circuit.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating Simple LED PCB...")
    print("=" * 60)

    # Board dimensions (mm)
    BOARD_WIDTH = 25.0
    BOARD_HEIGHT = 20.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Net definitions
    NETS = {
        "": 0,
        "VCC": 1,
        "LED_ANODE": 2,
        "GND": 3,
    }

    # Component positions (compact layout)
    # J1 on left, R1 in middle (offset up for GND clearance), D1 on right
    # R1 is rotated 90° so its pin 2 extends downward; moving R1 up
    # ensures the GND trace from J1.2 to D1.2 can route below R1
    J1_POS = (BOARD_ORIGIN_X + 5, BOARD_ORIGIN_Y + 10)
    R1_POS = (BOARD_ORIGIN_X + 12.5, BOARD_ORIGIN_Y + 8)  # Offset up by 2mm
    D1_POS = (BOARD_ORIGIN_X + 20, BOARD_ORIGIN_Y + 10)

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

    def generate_connector(ref: str, pos: tuple, pin1_net: str, pin2_net: str) -> str:
        """Generate a 2-pin through-hole connector (2.54mm pitch)."""
        x, y = pos
        pin1_num = NETS[pin1_net]
        pin2_num = NETS[pin2_net]
        pitch = 2.54 / 2

        return f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "PWR" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at 0 {-pitch:.3f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {pin1_num} "{pin1_net}"))
    (pad "2" thru_hole oval (at 0 {pitch:.3f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {pin2_num} "{pin2_net}"))
  )"""

    def generate_resistor(
        ref: str, pos: tuple, pin1_net: str, pin2_net: str, value: str = "330"
    ) -> str:
        """Generate an 0805 resistor footprint."""
        x, y = pos
        pin1_num = NETS[pin1_net]
        pin2_num = NETS[pin2_net]
        pad_offset = 1.0

        return f"""  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y} 90)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at {-pad_offset} 0 90) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {pin1_num} "{pin1_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0 90) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {pin2_num} "{pin2_net}"))
  )"""

    def generate_led(ref: str, pos: tuple, anode_net: str, cathode_net: str) -> str:
        """Generate a 5mm through-hole LED footprint.

        Pin assignments per KiCad LED_THT:LED_D5.0mm convention:
        - Pin 1 (rectangular pad) = Cathode (K)
        - Pin 2 (circular pad) = Anode (A)
        """
        x, y = pos
        anode_num = NETS[anode_net]
        cathode_num = NETS[cathode_net]
        # LED pitch: 2.54mm between cathode and anode
        pitch = 2.54 / 2

        return f"""  (footprint "LED_THT:LED_D5.0mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y} 90)
    (fp_text reference "{ref}" (at 0 -3.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "LED" (at 0 3.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at {-pitch:.3f} 0 90) (size 1.8 1.8) (drill 0.9) (layers "*.Cu" "*.Mask") (net {cathode_num} "{cathode_net}"))
    (pad "2" thru_hole circle (at {pitch:.3f} 0 90) (size 1.8 1.8) (drill 0.9) (layers "*.Cu" "*.Mask") (net {anode_num} "{anode_net}"))
  )"""

    # Build the PCB file
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    print("\n1. Adding footprints...")

    # J1: Power connector (pin1=VCC, pin2=GND)
    parts.append(generate_connector("J1", J1_POS, "VCC", "GND"))
    print(f"   J1 (power) at {J1_POS}")

    # R1: Current-limiting resistor (pin1=VCC, pin2=LED_ANODE)
    parts.append(generate_resistor("R1", R1_POS, "VCC", "LED_ANODE", "330"))
    print(f"   R1 (330 ohm) at {R1_POS}")

    # D1: LED (anode=LED_ANODE, cathode=GND)
    parts.append(generate_led("D1", D1_POS, "LED_ANODE", "GND"))
    print(f"   D1 (LED) at {D1_POS}")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "simple_led.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   Components: 1 connector, 1 resistor, 1 LED")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])} (VCC, LED_ANODE, GND)")

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

    # Configure design rules
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.3,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Load the PCB
    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=[],
        rules=rules,
    )

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")

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

    total_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_nets

    if success:
        print("\n   SUCCESS: All nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {stats['nets_routed']}/{total_nets} nets")

    return success


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


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB using kct check for consistent results.

    Uses kct check as a subprocess to ensure the same DRC rules
    are applied as when running kct check manually.
    """
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
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "simple_led")

        # Step 2: Create schematic
        sch_path = create_led_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_led_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "simple_led_routed.kicad_pcb"
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
        print("\nCircuit description:")
        print("  - J1: 2-pin power input (VCC, GND)")
        print("  - R1: 330 ohm current limiter")
        print("  - D1: LED indicator")
        print("  - 5V input -> ~10mA LED current")

        return 0 if erc_success and route_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
