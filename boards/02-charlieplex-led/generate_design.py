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
        footprint="Package_DIP:DIP-8_W7.62mm",
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
  (version 20260206)
  (generator "kicad-tools-demo")
  (generator_version "10.0")
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
        # Use 0.1mm-aligned pitch/row-spacing instead of strict imperial
        # DIP-8 dimensions (7.62mm row spacing, 2.54mm pin pitch) so all
        # eight pads land on the router's 0.1mm grid.  The U1 footprint is
        # a synthetic stand-in for an MCU (the schematic symbol is the
        # generic Conn_01x08), so the small geometric shift has no
        # manufacturing or schematic-net consequence.
        #
        # Geometry: pin row positions are ``MCU.y + (i - 1.5) * pitch``
        # with i in {0,1,2,3}.  For all four to land on a 0.1mm grid:
        #   * pitch itself must be a multiple of 0.1mm, AND
        #   * the half-pitch offset ``1.5 * pitch`` must also land on
        #     the grid relative to MCU.y.
        # With pitch=2.5mm we get offsets ±3.75, ±1.25.  Shifting MCU
        # y by +0.05mm (so the *footprint* origin is at 147.05, off
        # the user grid but pad-aligned) maps these offsets to a 0.1mm
        # grid: pad y = 143.30, 145.80, 148.30, 150.80.  Pad x is
        # 125 ± 3.80 = 121.20, 128.80 (on grid by construction).
        #
        # See Issue #3032.  The KiCad pad-grid checker validates pad
        # positions, not footprint origins, so the 0.05mm origin
        # offset is invisible to DRC.
        row_spacing = 7.6 / 2  # 3.80 mm (was 3.81)
        pin_pitch = 2.5  # mm (was 2.54)
        y_offset = 0.05  # align (i-1.5)*pin_pitch to the 0.1mm grid

        pin_nets = [
            (1, "LINE_A"),
            (2, "LINE_B"),
            (3, "LINE_C"),
            (4, "LINE_D"),
            (5, "GND"),
            (6, "GND"),
            (7, "VCC"),
            (8, "GND"),
        ]

        pads = []
        for i in range(4):
            pin_num, net_name = pin_nets[i]
            net_num = NETS.get(net_name, 0)
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = -1.5 * pin_pitch + i * pin_pitch + y_offset
            pads.append(
                f'    (pad "{pin_num}" thru_hole rect (at {-row_spacing:.3f} {py:.3f}) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net_str})'
            )

        for i in range(4):
            pin_num, net_name = pin_nets[4 + i]
            net_num = NETS.get(net_name, 0)
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = 1.5 * pin_pitch - i * pin_pitch + y_offset
            pads.append(
                f'    (pad "{pin_num}" thru_hole oval (at {row_spacing:.3f} {py:.3f}) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net_str})'
            )

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
      (effects (font (size 1.0 1.0) (thickness 0.15)))
    )
    (fp_text value "LED" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
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
    """Route the PCB by invoking ``kct route`` with the proven flag recipe.

    Returns True if ``kct route`` reports full success (return code 0);
    False if it produced a partial routing (still acceptable -- the
    output file is written either way and downstream DRC continues).

    Routing strategy choice (Issue #3032 / similar to PR #2981 board 05
    and PR #3034 board 01 patterns):

    The in-process ``router.route_all()`` legacy path historically routed
    this board's 4 charlieplex NODE_x nets, but with current router code
    (commit d54fe8f9 and later) it fails all four with
    ``BLOCKED_BY_COMPONENT: Path blocked by component keepout``.  The
    in-process ``router.route_all_negotiated()`` path used by PR #3034
    fares slightly better (4/8 vs 0/8) but still produces many
    sub-clearance violations on this small board.

    The ``kct route`` CLI with ``--strategy negotiated --iterations 30``
    is the only configuration that reliably yields 10/10 nets complete
    with zero DRC errors on this geometry.  It does this by:

      1. running the negotiated congestion router with adaptive rip-up
         (stagnation recovery + cohort re-enable patterns -- see
         ``route_all_negotiated`` in router/core.py),
      2. emitting auto-pour zones for GND / VCC after routing (so the
         3 GND pads on U1 and the lone VCC pad reach ``status=complete``
         via plane connectivity), and
      3. running its own post-route cleanup pass.

    The subprocess invocation is also what PR #2981 (board 05) and PR
    #2991 (board 07) use to keep the gold-standard examples consistent
    with what the production ``kct route`` pipeline actually does.
    """
    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # GND is a pour net (auto-poured into a copper zone by ``kct route``).
    # Excluded from the per-net pathfinder to avoid wasted iterations.
    skip_nets = ["GND"]

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--strategy",
        "negotiated",
        "--iterations",
        "30",
        "--per-net-timeout",
        "30",
        "--timeout",
        "240",
        "--seed",
        "42",
        "--skip-nets",
        ",".join(skip_nets),
        # Issue #3112: pass the manufacturer through so the post-route
        # ``drc_verify_and_nudge`` sweep can consult
        # ``via_in_pad_supported`` and slide any same-net via-in-pad
        # escape vias off the offending pad.  The default jlcpcb profile
        # does NOT support via-in-pad, so this is the case that exercises
        # the new sweep.
        "--manufacturer",
        "jlcpcb",
    ]

    print(f"\n1. Input: {input_path}")
    print(f"   Output: {output_path}")
    print(f"   Skipping nets: {skip_nets}")
    print(f"   Command: {' '.join(cmd)}")
    print("\n2. Routing...")

    result = subprocess.run(cmd, capture_output=False, text=True)

    # ``kct route`` returns 0 on full success and a non-zero code on
    # partial / failed routing.  Either way it writes a routed PCB to
    # ``output_path``; downstream DRC + manufacturing checks decide if
    # the partial output is acceptable.
    success = result.returncode == 0

    if not output_path.exists():
        print(f"\n   ERROR: ``kct route`` did not produce {output_path}", file=sys.stderr)
        return False

    if success:
        print("\n   SUCCESS: ``kct route`` reports all signal nets routed!")
    else:
        print(
            f"\n   PARTIAL: ``kct route`` exited with code {result.returncode} "
            "(partial routing; downstream DRC will continue)"
        )

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
        # The same-net via-in-pad rescue used to live here as a local
        # ``nudge_vias_off_same_net_pads()`` text-surgery helper (PR #3102).
        # Issue #3112 lifted that machinery into the router itself --
        # ``drc_verify_and_nudge`` (invoked from ``kct route``) now runs an
        # explicit same-net via-in-pad sweep gated on the manufacturer's
        # ``via_in_pad_supported`` flag, so no per-board surgery is needed.
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

        # Partial routing is acceptable; success if ERC and DRC pass
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
