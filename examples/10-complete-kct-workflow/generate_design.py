#!/usr/bin/env python3
"""
Complete KCT Workflow Example: LED Indicator

This script demonstrates the complete workflow from a .kct project specification
to a routed PCB ready for manufacturing. It shows how to:

1. Load a project specification (.kct file)
2. Generate a schematic programmatically
3. Create a PCB with component footprints
4. Route the PCB using the autorouter
5. Verify the design with DRC

The LED indicator circuit is intentionally simple:
- Input connector (5V, GND)
- Current limiting resistor (330 ohm)
- LED indicator

Usage:
    python generate_design.py              # Generate in output/ directory
    python generate_design.py /path/to/dir # Generate in specified directory

This example pairs with export_manufacturing.py to show the complete
workflow from specification to manufacturing files.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Import kicad-tools components
from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.spec import load_spec


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def load_project_spec(spec_path: Path) -> dict:
    """
    Load and display project specification.

    This demonstrates reading the .kct file to get design parameters.
    """
    print("=" * 60)
    print("Loading Project Specification")
    print("=" * 60)

    spec = load_spec(spec_path)

    print(f"\n   Project: {spec.project.name}")
    print(f"   Revision: {spec.project.revision}")
    print(f"   Author: {spec.project.author}")

    if spec.intent:
        print(f"\n   Summary: {spec.intent.summary.strip()[:60]}...")

    if spec.requirements and spec.requirements.manufacturing:
        mfr = spec.requirements.manufacturing
        print(f"\n   Target fab: {mfr.target_fab}")
        print(f"   Layers: {mfr.layers}")
        print(f"   Min trace: {mfr.min_trace}")
        print(f"   Min space: {mfr.min_space}")

    return spec


def create_led_schematic(output_dir: Path) -> Path:
    """
    Create a simple LED indicator schematic.

    Components:
    - J1: 2-pin input connector (VIN, GND)
    - R1: 330 ohm current limiting resistor
    - D1: LED indicator

    Returns the path to the generated schematic file.
    """
    print("\n" + "=" * 60)
    print("Creating LED Indicator Schematic")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="LED Indicator",
        date="2025-01",
        revision="A",
        company="kicad-tools Example",
        comment1="Simple LED indicator circuit",
        comment2="5V input, 330R resistor, LED",
    )

    # Layout coordinates
    RAIL_VIN = 30  # Input voltage rail Y position
    RAIL_GND = 100  # Ground rail Y position
    X_LEFT = 25  # Rail starting point

    # =========================================================================
    # Section 1: Place Components
    # =========================================================================
    print("\n1. Placing components...")

    # Input connector
    j_in = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=50,
        y=65,
        ref="J1",
        value="IN",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    j1_pin1 = j_in.pin_position("1")  # VIN
    j1_pin2 = j_in.pin_position("2")  # GND
    print(f"   J1: Input connector at ({j_in.x}, {j_in.y})")

    # Current limiting resistor
    r1 = sch.add_symbol(
        "Device:R",
        x=100,
        y=50,
        ref="R1",
        value="330",
        auto_footprint=True,
    )
    r1_pin1 = r1.pin_position("1")  # VIN side
    r1_pin2 = r1.pin_position("2")  # LED side
    print(f"   R1: 330 ohm at ({r1.x}, {r1.y})")

    # LED
    d1 = sch.add_symbol(
        "Device:LED",
        x=140,
        y=65,
        ref="D1",
        value="LED",
        footprint="LED_SMD:LED_0805_2012Metric",
    )
    d1_pin1 = d1.pin_position("1")  # Anode (positive)
    d1_pin2 = d1.pin_position("2")  # Cathode (negative, to GND)
    print(f"   D1: LED at ({d1.x}, {d1.y})")

    # =========================================================================
    # Section 2: Create Power Rails
    # =========================================================================
    print("\n2. Creating power rails...")

    # Get snapped rail positions
    rail_vin_y = sch._snap_coord(RAIL_VIN, "rail")
    rail_gnd_y = sch._snap_coord(RAIL_GND, "rail")
    x_left = sch._snap_coord(X_LEFT, "rail")

    # Get component X positions
    x_j1 = j1_pin1[0]
    x_r1 = r1_pin1[0]
    x_d1 = d1_pin2[0]

    # VIN rail segments
    vin_x_points = sorted([x_left, x_j1, x_r1])
    for i in range(len(vin_x_points) - 1):
        sch.add_wire((vin_x_points[i], rail_vin_y), (vin_x_points[i + 1], rail_vin_y))

    # GND rail segments
    gnd_x_points = sorted([x_left, x_j1, x_d1])
    for i in range(len(gnd_x_points) - 1):
        sch.add_wire((gnd_x_points[i], rail_gnd_y), (gnd_x_points[i + 1], rail_gnd_y))

    # Add power symbols
    sch.add_power("power:+5V", x=X_LEFT, y=RAIL_VIN, rotation=0)
    sch.add_power("power:GND", x=X_LEFT, y=RAIL_GND, rotation=180)

    # Add PWR_FLAG symbols for ERC
    sch.add_power("power:PWR_FLAG", x=x_j1, y=RAIL_VIN, rotation=0)
    sch.add_power("power:PWR_FLAG", x=x_j1, y=RAIL_GND, rotation=0)

    # Add net labels
    sch.add_label("+5V", X_LEFT, RAIL_VIN)
    sch.add_label("GND", X_LEFT, RAIL_GND)

    print(f"   VIN rail: {len(vin_x_points) - 1} segments")
    print(f"   GND rail: {len(gnd_x_points) - 1} segments")

    # =========================================================================
    # Section 3: Wire Components
    # =========================================================================
    print("\n3. Wiring components...")

    # J1 Pin 1 to VIN rail
    sch.add_wire(j1_pin1, (x_j1, rail_vin_y))
    sch.add_junction(x_j1, rail_vin_y)

    # J1 Pin 2 to GND rail
    sch.add_wire(j1_pin2, (x_j1, rail_gnd_y))
    sch.add_junction(x_j1, rail_gnd_y)
    print("   J1 -> VIN/GND rails")

    # R1 Pin 1 to VIN rail
    sch.add_wire(r1_pin1, (x_r1, rail_vin_y))
    sch.add_junction(x_r1, rail_vin_y)
    print("   R1 -> VIN rail")

    # R1 Pin 2 to LED anode (D1 Pin 1)
    # Need to route via an intermediate point
    led_y = d1_pin1[1]
    sch.add_wire(r1_pin2, (r1_pin2[0], led_y))  # Down from R1
    sch.add_wire((r1_pin2[0], led_y), d1_pin1)  # To LED anode
    print("   R1 -> D1 anode")

    # LED cathode (D1 Pin 2) to GND rail
    sch.add_wire(d1_pin2, (x_d1, rail_gnd_y))
    sch.add_junction(x_d1, rail_gnd_y)
    print("   D1 cathode -> GND rail")

    # =========================================================================
    # Section 4: Validate and Save
    # =========================================================================
    print("\n4. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors")
        for err in errors[:3]:
            print(f"      [{err['type']}] {err['message']}")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings")

    # Statistics
    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")

    # Save
    print("\n5. Writing schematic...")
    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "led_indicator.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


def create_led_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the LED indicator.

    Board size: 25mm x 20mm
    Components placed for easy routing.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating LED Indicator PCB")
    print("=" * 60)

    # Board dimensions
    BOARD_WIDTH = 25.0
    BOARD_HEIGHT = 20.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Net definitions
    NETS = {
        "": 0,
        "VIN": 1,
        "LED_ANODE": 2,
        "GND": 3,
    }

    # Component positions
    J1_POS = (BOARD_ORIGIN_X + 5, BOARD_ORIGIN_Y + 10)
    R1_POS = (BOARD_ORIGIN_X + 12.5, BOARD_ORIGIN_Y + 7)
    D1_POS = (BOARD_ORIGIN_X + 20, BOARD_ORIGIN_Y + 10)

    def generate_header() -> str:
        """Generate PCB file header."""
        return """(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-example")
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
        """Generate board outline."""
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
        """Generate a 2-pin connector footprint."""
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
    (fp_text value "Conn_01x02" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at 0 {-pitch:.3f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {pin1_num} "{pin1_net}"))
    (pad "2" thru_hole oval (at 0 {pitch:.3f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {pin2_num} "{pin2_net}"))
  )"""

    def generate_resistor(ref: str, pos: tuple, pin1_net: str, pin2_net: str, value: str) -> str:
        """Generate an 0805 resistor footprint."""
        x, y = pos
        pin1_num = NETS[pin1_net]
        pin2_num = NETS[pin2_net]
        pad_offset = 1.0

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
    (pad "1" smd roundrect (at {-pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {pin1_num} "{pin1_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {pin2_num} "{pin2_net}"))
  )"""

    def generate_led(ref: str, pos: tuple, pin1_net: str, pin2_net: str) -> str:
        """Generate an 0805 LED footprint."""
        x, y = pos
        pin1_num = NETS[pin1_net]
        pin2_num = NETS[pin2_net]
        pad_offset = 1.0

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
    (pad "1" smd roundrect (at {-pad_offset} 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {pin1_num} "{pin1_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {pin2_num} "{pin2_net}"))
  )"""

    # Build PCB file
    print("\n1. Adding footprints...")
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    # J1: Input connector (pin1=VIN, pin2=GND)
    parts.append(generate_connector("J1", J1_POS, "VIN", "GND"))
    print(f"   J1 (input) at {J1_POS}")

    # R1: Current limiting resistor (pin1=VIN, pin2=LED_ANODE)
    parts.append(generate_resistor("R1", R1_POS, "VIN", "LED_ANODE", "330"))
    print(f"   R1 (330R) at {R1_POS}")

    # D1: LED (pin1=LED_ANODE, pin2=GND)
    parts.append(generate_led("D1", D1_POS, "LED_ANODE", "GND"))
    print(f"   D1 (LED) at {D1_POS}")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "led_indicator.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   Components: 1 connector, 1 resistor, 1 LED")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])} (VIN, LED_ANODE, GND)")

    return pcb_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """
    Route the PCB using the autorouter.

    Returns True if all nets were routed successfully.
    """
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

    print("\n" + "=" * 60)
    print("Routing PCB")
    print("=" * 60)

    # Design rules matching project.kct requirements
    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.3,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Load PCB
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

    # Optimize traces
    print("\n3. Optimizing traces...")
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

    segments_before = stats_before["segments"]
    segments_after = stats["segments"]
    reduction = (1 - segments_after / segments_before) * 100 if segments_before > 0 else 0

    print(f"   Segments: {segments_before} -> {segments_after} ({reduction:.1f}% reduction)")

    print("\n4. Final routing results:")
    print(f"   Routes: {stats['routes']}")
    print(f"   Segments: {stats['segments']}")
    print(f"   Vias: {stats['vias']}")
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

    # Save routed PCB
    print(f"\n5. Saving routed PCB: {output_path}")

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
    import subprocess

    print("\n" + "=" * 60)
    print("Running DRC")
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

        return result.returncode == 0

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

    project_dir = Path(__file__).parent
    spec_path = project_dir / "project.kct"

    try:
        # Step 1: Load project specification
        if spec_path.exists():
            load_project_spec(spec_path)

        # Step 2: Create project file
        print("\n" + "=" * 60)
        print("Creating Project File")
        print("=" * 60)
        output_dir.mkdir(parents=True, exist_ok=True)
        project_data = create_minimal_project("led_indicator.kicad_pro")
        project_path = output_dir / "led_indicator.kicad_pro"
        save_project(project_data, project_path)
        print(f"\n   Project: {project_path}")

        # Step 3: Create schematic
        sch_path = create_led_schematic(output_dir)

        # Step 4: Create PCB
        pcb_path = create_led_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "led_indicator_routed.kicad_pcb"
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
        print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
        print(f"  DRC: {'PASS' if drc_success else 'FAIL'}")
        print("\nCircuit:")
        print("  - J1: 2-pin input connector (5V, GND)")
        print("  - R1: 330 ohm current limiting resistor")
        print("  - D1: LED indicator")
        print("\nNext step: Run export_manufacturing.py to generate Gerbers and BOM")

        return 0 if route_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
