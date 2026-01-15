#!/usr/bin/env python3
"""
Simple Voltage Divider - Validation Test Project

This is a minimal test to validate the kicad-tools workflow:
- 2-pin input connector (VIN, GND)
- 2-resistor voltage divider
- 2-pin output connector (VOUT, GND)

The design targets:
- Input: 5V
- Output: 2.5V (50% division with equal resistors)
- Resistors: 10k / 10k

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


def create_voltage_divider_schematic(output_dir: Path) -> Path:
    """
    Create a simple voltage divider schematic.

    Returns the path to the generated schematic file.
    """
    print("Creating Voltage Divider Schematic...")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="Voltage Divider Test",
        date="2025-01",
        revision="A",
        company="kicad-tools Validation",
        comment1="Simple 2-resistor voltage divider",
        comment2="5V -> 2.5V (10k/10k)",
    )

    # Define layout coordinates
    RAIL_VIN = 30  # Input voltage rail
    RAIL_GND = 150  # Ground rail
    X_LEFT = 25  # Rail starting point (power symbols)

    # =========================================================================
    # Section 1: Place Components First (to get pin positions)
    # =========================================================================
    print("\n1. Placing components...")

    # Input connector
    j_in = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=50,
        y=90,
        ref="J1",
        value="IN",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    j1_pin1 = j_in.pin_position("1")
    j1_pin2 = j_in.pin_position("2")
    print(f"   J1: Input connector at ({j_in.x}, {j_in.y})")

    # Voltage divider resistors (not using VoltageDivider block to control wiring)
    r1 = sch.add_symbol("Device:R", x=110, y=60, ref="R1", value="10k", auto_footprint=True)
    r1_pin1 = r1.pin_position("1")  # Top (VIN side)
    r1_pin2 = r1.pin_position("2")  # Bottom (VOUT side)
    print(f"   R1: 10k at ({r1.x}, {r1.y})")

    r2 = sch.add_symbol("Device:R", x=110, y=75, ref="R2", value="10k", auto_footprint=True)
    r2_pin1 = r2.pin_position("1")  # Top (VOUT side)
    r2_pin2 = r2.pin_position("2")  # Bottom (GND side)
    print(f"   R2: 10k at ({r2.x}, {r2.y})")

    # Output connector
    j_out = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=165,
        y=90,
        ref="J2",
        value="OUT",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    j2_pin1 = j_out.pin_position("1")
    j2_pin2 = j_out.pin_position("2")
    print(f"   J2: Output connector at ({j_out.x}, {j_out.y})")

    # =========================================================================
    # Section 2: Create Segmented Power Rails
    # =========================================================================
    print("\n2. Creating segmented power rails...")

    # Get the snapped rail Y positions
    rail_vin_y = sch._snap_coord(RAIL_VIN, "rail")
    rail_gnd_y = sch._snap_coord(RAIL_GND, "rail")
    x_left = sch._snap_coord(X_LEFT, "rail")

    # Get X positions of all components that connect to rails
    x_j1 = j1_pin1[0]  # J1 connection point
    x_r1 = r1_pin1[0]  # R1 VIN connection point
    x_r2 = r2_pin2[0]  # R2 GND connection point
    x_j2 = j2_pin2[0]  # J2 GND connection point

    # Sort X positions for VIN rail: left edge, J1, R1
    # Note: Don't extend past rightmost connection (R1) to avoid floating wire endpoints
    vin_x_points = sorted([x_left, x_j1, x_r1])

    # Sort X positions for GND rail: left edge, J1, R2, J2
    # Note: Don't extend past rightmost connection (J2) to avoid floating wire endpoints
    gnd_x_points = sorted([x_left, x_j1, x_r2, x_j2])

    # Create VIN rail as segments
    for i in range(len(vin_x_points) - 1):
        sch.add_wire((vin_x_points[i], rail_vin_y), (vin_x_points[i + 1], rail_vin_y))

    # Create GND rail as segments
    for i in range(len(gnd_x_points) - 1):
        sch.add_wire((gnd_x_points[i], rail_gnd_y), (gnd_x_points[i + 1], rail_gnd_y))

    # Add power symbols at rail start
    sch.add_power("power:+5V", x=X_LEFT, y=RAIL_VIN, rotation=0)
    sch.add_power("power:GND", x=X_LEFT, y=RAIL_GND, rotation=180)

    # Add PWR_FLAG symbols to indicate power entry points
    # This tells ERC that these nets are intentionally driven externally
    sch.add_power("power:PWR_FLAG", x=x_j1, y=RAIL_VIN, rotation=0)
    sch.add_power("power:PWR_FLAG", x=x_j1, y=RAIL_GND, rotation=0)

    # Add net labels
    sch.add_label("+5V", X_LEFT, RAIL_VIN)
    sch.add_label("GND", X_LEFT, RAIL_GND)

    print(f"   VIN rail with {len(vin_x_points) - 1} segments")
    print(f"   GND rail with {len(gnd_x_points) - 1} segments")

    # =========================================================================
    # Section 3: Wire Components to Rails
    # =========================================================================
    print("\n3. Wiring components...")

    # J1 Pin 1 to VIN rail (vertical wire)
    sch.add_wire(j1_pin1, (x_j1, rail_vin_y))
    sch.add_junction(x_j1, rail_vin_y)

    # J1 Pin 2 to GND rail (vertical wire)
    sch.add_wire(j1_pin2, (x_j1, rail_gnd_y))
    sch.add_junction(x_j1, rail_gnd_y)
    print("   J1 -> VIN/GND rails")

    # R1 Pin 1 to VIN rail
    sch.add_wire(r1_pin1, (x_r1, rail_vin_y))
    sch.add_junction(x_r1, rail_vin_y)
    print("   R1 -> VIN rail")

    # R1-R2 connection (VOUT junction)
    sch.add_wire(r1_pin2, r2_pin1)
    sch.add_junction(r1_pin2[0], r1_pin2[1])
    print("   R1 <-> R2 (VOUT)")

    # R2 Pin 2 to GND rail
    sch.add_wire(r2_pin2, (x_r2, rail_gnd_y))
    sch.add_junction(x_r2, rail_gnd_y)
    print("   R2 -> GND rail")

    # J2 Pin 1 to VOUT (horizontal then vertical)
    vout_y = r1_pin2[1]  # VOUT is at R1 pin 2 Y position
    sch.add_wire(r1_pin2, (x_j2, vout_y))  # Horizontal from VOUT to J2's X
    sch.add_wire((x_j2, vout_y), j2_pin1)  # Vertical down to J2 Pin 1
    print("   VOUT -> J2 Pin 1")

    # J2 Pin 2 to GND rail
    sch.add_wire(j2_pin2, (x_j2, rail_gnd_y))
    sch.add_junction(x_j2, rail_gnd_y)
    print("   J2 -> GND rail")

    # Add VOUT label
    sch.add_label("VOUT", x_j2 - 10, vout_y, rotation=0)

    # Print voltage divider info
    r_top = 10000  # 10k
    r_bottom = 10000  # 10k
    ratio = r_bottom / (r_top + r_bottom)
    print("\n   Voltage divider: R1=10k, R2=10k")
    print(f"   Division ratio: {ratio:.2f}")
    print(f"   Output voltage: {5.0 * ratio:.2f}V")

    # =========================================================================
    # Section 4: Validate Schematic
    # =========================================================================
    print("\n4. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors:")
        for err in errors:
            print(f"      [{err['type']}] {err['message']}")
            if err.get("location"):
                print(f"                  at {err['location']}")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings:")
        for warn in warnings[:5]:  # Limit to first 5
            print(f"      [{warn['type']}] {warn['message']}")
        if len(warnings) > 5:
            print(f"      ... and {len(warnings) - 5} more")

    # Get statistics
    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")
    print(f"      Labels: {stats['label_count']}")

    # =========================================================================
    # Section 5: Write Output Files
    # =========================================================================
    print("\n5. Writing schematic...")

    output_dir.mkdir(parents=True, exist_ok=True)

    sch_path = output_dir / "voltage_divider.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


def create_voltage_divider_pcb(output_dir: Path) -> Path:
    """
    Create a simple PCB for the voltage divider.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating Voltage Divider PCB...")
    print("=" * 60)

    # Board dimensions (mm)
    BOARD_WIDTH = 30.0
    BOARD_HEIGHT = 25.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Net definitions
    NETS = {
        "": 0,
        "VIN": 1,
        "VOUT": 2,
        "GND": 3,
    }

    # Component positions (relative to board origin)
    # Layout: J1 on left, R1-R2 in middle, J2 on right
    J1_POS = (BOARD_ORIGIN_X + 5, BOARD_ORIGIN_Y + 12.5)  # Input connector
    R1_POS = (BOARD_ORIGIN_X + 15, BOARD_ORIGIN_Y + 8)  # Top resistor
    R2_POS = (BOARD_ORIGIN_X + 15, BOARD_ORIGIN_Y + 17)  # Bottom resistor
    J2_POS = (BOARD_ORIGIN_X + 25, BOARD_ORIGIN_Y + 12.5)  # Output connector

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

        # Pin pitch 2.54mm (100mil)
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

    def generate_resistor(
        ref: str, pos: tuple, pin1_net: str, pin2_net: str, value: str = "10k"
    ) -> str:
        """Generate an 0805 resistor footprint."""
        x, y = pos
        pin1_num = NETS[pin1_net]
        pin2_num = NETS[pin2_net]

        # 0805 pad positions: ~1mm from center
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

    # Build the PCB file
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    # Add components
    print("\n1. Adding footprints...")

    # J1: Input connector (pin1=VIN, pin2=GND)
    parts.append(generate_connector("J1", J1_POS, "VIN", "GND"))
    print(f"   J1 (input) at {J1_POS}")

    # R1: Top resistor (pin1=VIN, pin2=VOUT)
    parts.append(generate_resistor("R1", R1_POS, "VIN", "VOUT", "10k"))
    print(f"   R1 (10k) at {R1_POS}")

    # R2: Bottom resistor (pin1=VOUT, pin2=GND)
    parts.append(generate_resistor("R2", R2_POS, "VOUT", "GND", "10k"))
    print(f"   R2 (10k) at {R2_POS}")

    # J2: Output connector (pin1=VOUT, pin2=GND)
    parts.append(generate_connector("J2", J2_POS, "VOUT", "GND"))
    print(f"   J2 (output) at {J2_POS}")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "voltage_divider.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   Components: 2 connectors, 2 resistors")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])} (VIN, VOUT, GND)")

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
    # Note: grid_resolution should be < trace_clearance for reliable DRC
    rules = DesignRules(
        grid_resolution=0.1,  # 0.1mm grid (fine for DRC compliance)
        trace_width=0.3,  # 0.3mm traces
        trace_clearance=0.2,  # 0.2mm clearance
        via_drill=0.3,  # 0.3mm via drill
        via_diameter=0.6,  # 0.6mm via pad
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Load the PCB
    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=[],  # Route all nets
        rules=rules,
    )

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")

    # Route all nets
    print("\n2. Routing nets...")
    router.route_all()

    # Get statistics before optimization
    stats_before = router.get_statistics()

    print("\n3. Raw routing results (before optimization):")
    print(f"   Routes created: {stats_before['routes']}")
    print(f"   Segments: {stats_before['segments']}")
    print(f"   Vias: {stats_before['vias']}")
    print(f"   Total length: {stats_before['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats_before['nets_routed']}")

    # Optimize traces - merge collinear segments, eliminate zigzags, etc.
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

    # Get statistics after optimization
    stats = router.get_statistics()

    segments_before = stats_before["segments"]
    segments_after = stats["segments"]
    reduction = (1 - segments_after / segments_before) * 100 if segments_before > 0 else 0

    print(f"   Segments: {segments_before} -> {segments_after} ({reduction:.1f}% reduction)")
    print(f"   Vias: {stats_before['vias']} -> {stats['vias']}")

    print("\n5. Final routing results:")
    print(f"   Routes created: {stats['routes']}")
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
    """
    Run ERC on the schematic.

    Returns True if no errors found.
    """
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.cli.runner import run_erc as kicad_run_erc
    from kicad_tools.erc import ERCReport

    print("\n" + "=" * 60)
    print("Running ERC...")
    print("=" * 60)

    # Check for kicad-cli
    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("\n   WARNING: kicad-cli not found - skipping ERC")
        print("   Install KiCad 8 from: https://www.kicad.org/download/")
        return True  # Skip ERC if kicad-cli not available

    result = kicad_run_erc(sch_path)

    if not result.success:
        print(f"\n   Error running ERC: {result.stderr}")
        return False

    # Parse the report
    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"\n   Error parsing ERC report: {e}")
        return False
    finally:
        # Clean up temp file
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    # Filter out excluded violations
    violations = [v for v in report.violations if not v.excluded]
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = len(violations) - error_count

    if error_count > 0:
        print(f"\n   Found {error_count} ERC errors:")
        for v in [v for v in violations if v.is_error][:5]:
            print(f"      - [{v.type_str}] {v.description}")
        if error_count > 5:
            print(f"      ... and {error_count - 5} more")
        return False
    elif warning_count > 0:
        print(f"\n   Found {warning_count} ERC warnings (no errors)")
        for v in violations[:3]:
            print(f"      - [{v.type_str}] {v.description}")
        if warning_count > 3:
            print(f"      ... and {warning_count - 3} more")
        return True
    else:
        print("\n   No ERC violations found!")
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
        project_path = create_project(output_dir, "voltage_divider")

        # Step 2: Create schematic
        sch_path = create_voltage_divider_schematic(output_dir)

        # Step 3: Run ERC on schematic
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_voltage_divider_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "voltage_divider_routed.kicad_pcb"
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
        print("\nDesign summary:")
        print("  - J1: 2-pin input connector (VIN, GND)")
        print("  - R1, R2: 10k voltage divider")
        print("  - J2: 2-pin output connector (VOUT, GND)")
        print("  - 5V input -> 2.5V output")

        return 0 if erc_success and route_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
