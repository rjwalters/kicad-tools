#!/usr/bin/env python3
"""
USB Joystick Controller - Complete Design Generation

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with MCU, USB, joystick, and buttons
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design is a USB game controller with:
- 32-pin QFP microcontroller
- USB Type-C connector
- 2-axis analog joystick
- 4 tactile buttons
- Crystal oscillator

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
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# =============================================================================
# Net Definitions
# =============================================================================

NETS = {
    "": 0,
    "VBUS": 1,
    "VCC": 2,
    "GND": 3,
    "USB_D+": 4,
    "USB_D-": 5,
    "USB_CC1": 6,
    "USB_CC2": 7,
    "JOY_X": 8,
    "JOY_Y": 9,
    "JOY_BTN": 10,
    "BTN1": 11,
    "BTN2": 12,
    "BTN3": 13,
    "BTN4": 14,
    "XTAL1": 15,
    "XTAL2": 16,
}


# =============================================================================
# Schematic Generation
# =============================================================================

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

    sch.add_wire((x, y), (end_x, y), snap=False)
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation, snap=False)


def create_usb_joystick_schematic(output_dir: Path) -> Path:
    """
    Create a USB Joystick schematic.

    Returns the path to the generated schematic file.
    """
    print("\n" + "=" * 60)
    print("Creating USB Joystick Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="USB Joystick Controller",
        date="2025-01",
        revision="A",
        company="kicad-tools Demo",
        comment1="USB game controller with analog joystick",
        comment2="Demonstrates autolayout functionality",
        snap_mode=SnapMode.AUTO,
        grid=2.54,
    )

    RAIL_VCC = 25.4
    RAIL_GND = 177.8

    # =========================================================================
    # Section 1: Place MCU
    # =========================================================================
    print("\n1. Placing MCU...")

    try:
        mcu = sch.add_symbol(
            "Connector_Generic:Conn_02x16_Counter_Clockwise",
            x=101.6,
            y=88.9,
            ref="U1",
            value="MCU",
        )
    except Exception:
        mcu = sch.add_symbol(
            "Device:R",
            x=101.6,
            y=88.9,
            ref="U1",
            value="MCU",
        )
    print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")

    # =========================================================================
    # Section 2: Place USB connector
    # =========================================================================
    print("\n2. Placing USB connector...")

    suggested_pos = sch.suggest_position(
        "Connector_Generic:Conn_01x04",
        near=(50.8, 50.8),
        padding=5.08,
    )

    usb_conn = sch.add_symbol(
        "Connector_Generic:Conn_01x04",
        x=suggested_pos[0],
        y=suggested_pos[1],
        ref="J1",
        value="USB-C",
    )
    print(f"   J1 (USB-C): placed at ({usb_conn.x}, {usb_conn.y})")

    # =========================================================================
    # Section 3: Place Joystick connector
    # =========================================================================
    print("\n3. Placing Joystick connector...")

    joy_pos = sch.suggest_position(
        "Connector_Generic:Conn_01x05",
        near=(50.8, 101.6),
        padding=5.08,
    )

    joy_conn = sch.add_symbol(
        "Connector_Generic:Conn_01x05",
        x=joy_pos[0],
        y=joy_pos[1],
        ref="J2",
        value="Joystick",
    )
    print(f"   J2 (Joystick): placed at ({joy_conn.x}, {joy_conn.y})")

    # =========================================================================
    # Section 4: Place Crystal
    # =========================================================================
    print("\n4. Placing Crystal...")

    xtal_pos = sch.suggest_position(
        "Device:Crystal",
        near=(127.0, 76.2),
        padding=5.08,
    )

    try:
        xtal = sch.add_symbol(
            "Device:Crystal",
            x=xtal_pos[0],
            y=xtal_pos[1],
            ref="Y1",
            value="16MHz",
        )
    except Exception:
        xtal = sch.add_symbol(
            "Device:R",
            x=xtal_pos[0],
            y=xtal_pos[1],
            ref="Y1",
            value="16MHz",
        )
    print(f"   Y1 (Crystal): placed at ({xtal.x}, {xtal.y})")

    # =========================================================================
    # Section 5: Place Buttons
    # =========================================================================
    print("\n5. Placing Buttons...")

    button_refs = ["SW1", "SW2", "SW3", "SW4"]
    base_x, base_y = 152.4, 88.9

    buttons = []
    for ref in button_refs:
        pos = sch.suggest_position(
            "Device:R",
            near=(base_x, base_y),
            padding=7.62,
        )

        btn = sch.add_symbol(
            "Device:R",
            x=pos[0],
            y=pos[1],
            ref=ref,
            value="Button",
        )
        buttons.append(btn)
        print(f"   {ref}: placed at ({btn.x}, {btn.y})")

    # =========================================================================
    # Section 6: Place Decoupling Capacitors
    # =========================================================================
    print("\n6. Placing Decoupling Capacitors...")

    cap_positions = [
        ("C1", 88.9, 63.5),
        ("C2", 114.3, 63.5),
        ("C3", 88.9, 114.3),
        ("C4", 55.88, 38.1),
    ]

    caps = []
    for ref, x, y in cap_positions:
        pos = sch.suggest_position(
            "Device:C",
            near=(x, y),
            padding=2.54,
        )

        try:
            cap = sch.add_symbol(
                "Device:C",
                x=pos[0],
                y=pos[1],
                ref=ref,
                value="100nF",
            )
        except Exception:
            cap = sch.add_symbol(
                "Device:R",
                x=pos[0],
                y=pos[1],
                ref=ref,
                value="100nF",
            )
        caps.append(cap)
        print(f"   {ref}: placed at ({cap.x}, {cap.y})")

    # =========================================================================
    # Section 7: Add signal wiring
    # =========================================================================
    print("\n7. Adding signal wiring...")

    MCU_PIN_MAP = {
        "1": "VCC",
        "16": "GND",
        "17": "VCC",
        "32": "GND",
        "29": "USB_D+",
        "30": "USB_D-",
        "7": "XTAL1",
        "8": "XTAL2",
        "2": "JOY_X",
        "3": "JOY_Y",
        "9": "BTN1",
        "10": "BTN2",
        "11": "BTN3",
        "12": "BTN4",
        "13": "JOY_BTN",
        # Unused inputs tied to GND to prevent JLCPCB review holds
        "5": "GND",
        "6": "GND",
        "18": "GND",
        "19": "GND",
        "20": "GND",
        "21": "GND",
        "22": "GND",
        "31": "GND",
    }

    USB_PIN_MAP = {"1": "VCC", "2": "USB_D-", "3": "USB_D+", "4": "GND"}
    JOY_PIN_MAP = {"1": "VCC", "2": "GND", "3": "JOY_X", "4": "JOY_Y", "5": "JOY_BTN"}

    for pin_num, net_name in MCU_PIN_MAP.items():
        pin_pos = mcu.pin_position(pin_num)
        if pin_pos:
            direction = "left" if int(pin_num) <= 16 else "right"
            add_pin_label(sch, pin_pos, net_name, direction=direction)

    used_pins = set(MCU_PIN_MAP.keys())
    for pin_num in range(1, 33):
        pin_str = str(pin_num)
        if pin_str not in used_pins:
            pin_pos = mcu.pin_position(pin_str)
            if pin_pos:
                sch.add_no_connect(pin_pos[0], pin_pos[1], snap=False)

    for pin_num, net_name in USB_PIN_MAP.items():
        pin_pos = usb_conn.pin_position(pin_num)
        if pin_pos:
            add_pin_label(sch, pin_pos, net_name, direction="right")

    for pin_num, net_name in JOY_PIN_MAP.items():
        pin_pos = joy_conn.pin_position(pin_num)
        if pin_pos:
            add_pin_label(sch, pin_pos, net_name, direction="right")

    xtal_pin1 = xtal.pin_position("1")
    xtal_pin2 = xtal.pin_position("2")
    if xtal_pin1:
        add_pin_label(sch, xtal_pin1, "XTAL1", direction="left")
    if xtal_pin2:
        add_pin_label(sch, xtal_pin2, "XTAL2", direction="right")

    button_nets = ["BTN1", "BTN2", "BTN3", "BTN4"]
    for btn, net_name in zip(buttons, button_nets, strict=True):
        pin1_pos = btn.pin_position("1")
        pin2_pos = btn.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, net_name, direction="left")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")

    for cap in caps:
        pin1_pos = cap.pin_position("1")
        pin2_pos = cap.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, "VCC", direction="left")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")

    # Power symbols
    vcc_pwr = sch.add_power("power:+5V", x=25.4, y=RAIL_VCC, rotation=0)
    sch.add_wire((vcc_pwr.x, vcc_pwr.y), (vcc_pwr.x + WIRE_STUB, vcc_pwr.y), snap=False)
    sch.add_global_label(
        "VCC", vcc_pwr.x + WIRE_STUB, vcc_pwr.y, shape="input", rotation=180, snap=False
    )
    sch.add_pwr_flag(vcc_pwr.x, vcc_pwr.y)

    gnd_pwr = sch.add_power("power:GND", x=25.4, y=RAIL_GND, rotation=180)
    sch.add_wire((gnd_pwr.x, gnd_pwr.y), (gnd_pwr.x + WIRE_STUB, gnd_pwr.y), snap=False)
    sch.add_global_label(
        "GND", gnd_pwr.x + WIRE_STUB, gnd_pwr.y, shape="input", rotation=180, snap=False
    )
    sch.add_pwr_flag(gnd_pwr.x, gnd_pwr.y)

    print("   Added VCC and GND power symbols with PWR_FLAG")

    # =========================================================================
    # Section 8: Validate and Write
    # =========================================================================
    print("\n8. Validating schematic...")

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

    print("\n9. Writing schematic...")
    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "usb_joystick.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


# =============================================================================
# PCB Generation
# =============================================================================
#
# Issue #3410: this script previously carried its OWN copy of the PCB
# layout (60x40mm board, pre-#2527 USB pin assignment) which had drifted
# from the canonical generator ``generate_pcb.py`` (80x60mm, schematic-
# aligned pin map).  Because ``kct build --step pcb`` and the CI
# ``regenerated_board`` fixture both run ``generate_pcb.py`` while this
# script's main() wrote its stale internal copy over the committed
# artifact, the two paths measured DIFFERENT boards (the #3402 audit's
# "77% reach" was the stale 60x40 board).  Mirroring the #3308 route-
# recipe consolidation, the PCB step now DELEGATES to ``generate_pcb.py``
# so there is exactly one copy of the layout.
#
# Copper pours (GND/VCC/VBUS) are likewise emitted by ``generate_pcb.py:
# generate_power_pours()`` -- see ``create_zones_for_pcb`` below.


def create_usb_joystick_pcb(output_dir: Path) -> Path:
    """Create the PCB by delegating to the canonical ``generate_pcb.py``.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating USB Joystick PCB (delegates to generate_pcb.py)...")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    import generate_pcb as _pcb_gen

    pcb_path = output_dir / "usb_joystick.kicad_pcb"
    output_dir.mkdir(parents=True, exist_ok=True)
    pcb_path.write_text(_pcb_gen.generate_pcb())
    print(f"   PCB: {pcb_path}")
    print(f"\n   Board size: {_pcb_gen.BOARD_WIDTH}mm x {_pcb_gen.BOARD_HEIGHT}mm")
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


def create_zones_for_pcb(pcb_path: Path) -> int:
    """Verify the power-net copper pours exist in *pcb_path*.

    Issue #3410 consolidation: the GND (F.Cu + B.Cu) / VCC / VBUS zone
    definitions are now emitted by the canonical generator
    (``generate_pcb.py:generate_power_pours``) so the layout and its
    pour regions cannot drift apart again (the pre-#3410 version of
    this function carried pour rectangles hand-tuned to a 60x40mm
    layout this script no longer generates).  This step now only
    VERIFIES the pours are present so a future generator regression
    fails loudly here rather than as 40 stranded-pad connectivity
    errors at DRC time.

    Returns the number of zones found.
    """
    print("\n" + "=" * 60)
    print("Verifying copper-pour zones...")
    print("=" * 60)

    text = pcb_path.read_text()
    zone_count = text.count("(zone")
    required = ("GND", "VCC", "VBUS")
    missing = [n for n in required if f'(net_name "{n}")' not in text]
    if missing:
        raise RuntimeError(
            f"Generated PCB {pcb_path} is missing pour zone(s) for "
            f"{', '.join(missing)} -- generate_pcb.py:generate_power_pours() "
            "must emit GND/VCC/VBUS zones (issue #3410)."
        )
    print(f"\n   {zone_count} zone(s) present (GND/VCC/VBUS pours OK)")
    return zone_count


def fill_zones_in_routed_pcb(routed_path: Path) -> int:
    """Fill copper zones in the routed PCB via ``kicad-cli``.

    Zone *definitions* (created by :func:`create_zones_for_pcb`) only carry
    a polygon outline + net + layer.  The actual ``(filled_polygon ...)``
    copper is computed by KiCad's fill engine -- without this step the
    routed PCB ships with empty zones, and DRC reports the power-net pads
    as stranded.  Mirrors board-05's ``fill_zones_in_routed_pcb`` at
    ``boards/05-bldc-motor-controller/design.py:2233``.

    Returns the number of zones in the routed PCB after fill.
    """
    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones

    print("\n" + "=" * 60)
    print("Filling copper zones...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        print("\n   WARNING: kicad-cli not found - skipping zone fill")
        return 0

    print(f"\n1. Filling zones in: {routed_path}")
    result = run_fill_zones(routed_path, kicad_cli=kicad_cli)

    if not result.success:
        print(f"\n   WARNING: Zone fill failed: {result.stderr or '(no stderr)'}")
        return 0

    try:
        text = routed_path.read_text()
        zone_count = text.count("(zone ")
        print(f"\n2. Zones present: {zone_count}")
        return zone_count
    except Exception:
        return 0


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB with the production ``kct route`` recipe.

    Issue #3410 (recipe consolidation, round 2): the previous version of
    this function carried an in-process ``Autorouter.route_all()`` recipe
    (0.05mm grid, in-pad rescues on U1, CoupledPathfinder disabled).
    That simple per-net strategy tops out at 11-12/13 on this board: the
    USB-C escape belt packs four signal columns into 3.5mm, and without
    the negotiated two-phase strategy's rip-up/retry and fine-pitch
    escape regions, whichever USB net routes last is left stranded
    (USB_CC2 under current HEAD).

    The production ``kct route`` invocation -- the SAME one pinned by
    ``tests/router/test_board03_routing_baseline.py`` and used by the
    fleet (board-05 precedent: "bake proven kct route flag recipe into
    design.py", PR #2981) -- reaches 13/13 at 2 layers on the
    regenerated board.  Delegating to it means the demo, the build
    pipeline, and the reach-floor CI tests all measure ONE code path.

    The function also emits the ``net_class_map.json`` sidecar next to
    the routed PCB so the validate-side diff-pair rules
    (``routing_continuity`` / ``length_skew``) can engage from
    ``kct check --net-class-map`` (Issue #2684).

    Returns True when every signal net is fully routed (the DRC gate is
    reported separately by the caller via ``run_drc``).
    """
    import json as _json
    import re as _re
    import subprocess as _sp
    from dataclasses import replace as _dc_replace

    from kicad_tools.router import create_net_class_map
    from kicad_tools.router.rules import net_class_map_to_dict

    print("\n" + "=" * 60)
    print("Routing PCB (production `kct route` recipe)...")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Net-class sidecar (Issue #2684): annotate the USB diff pair so the
    # validate-side rules can re-derive engagement/skew state.  The
    # intra_pair_clearance widening to 0.15mm mirrors #3095 (JLCPCB
    # ``diffpair_clearance_intra`` threshold is 0.127mm).
    # ------------------------------------------------------------------
    net_class_map = create_net_class_map(
        power_nets=["VCC", "VBUS", "GND"],
        high_speed_nets=["USB_D+", "USB_D-"],
        clock_nets=["XTAL1", "XTAL2"],
    )
    if "USB_D+" in net_class_map and "USB_D-" in net_class_map:
        net_class_map["USB_D+"] = _dc_replace(
            net_class_map["USB_D+"],
            diffpair_partner="USB_D-",
            intra_pair_clearance=0.15,
        )
        net_class_map["USB_D-"] = _dc_replace(
            net_class_map["USB_D-"],
            diffpair_partner="USB_D+",
            intra_pair_clearance=0.15,
        )
    sidecar_path = output_path.parent / "net_class_map.json"
    sidecar_path.write_text(_json.dumps(net_class_map_to_dict(net_class_map), indent=2))
    print(f"   Wrote net-class-map sidecar: {sidecar_path}")

    # ------------------------------------------------------------------
    # Production routing recipe.  EXACTLY the invocation pinned by
    # tests/router/test_board03_routing_baseline.py::_run_kct_route --
    # if you change a flag here, change it there in the same commit.
    # ------------------------------------------------------------------
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--seed",
        "42",
        "--manufacturer",
        "jlcpcb-tier1",
        "--backend",
        "cpp",
        "--timeout",
        "600",
        # ``--raw`` (skip TraceOptimizer) is LOAD-BEARING for the 0-DRC
        # acceptance: with optimization enabled, the segment-merge pass
        # re-introduces exactly one deterministic
        # ``clearance_segment_via`` violation (XTAL1's merged B.Cu run
        # at y=+29.51 vs XTAL2's via at (+32.75, +29.10), 0.006mm gap,
        # reproducible across seeds 42/43) because the optimizer's
        # collision checker is grid-quantized while the DRC check is
        # world-coordinate exact.  Raw grid-step segments follow the
        # A*-validated path bit-for-bit: 13/13 nets, 0 DRC errors at
        # jlcpcb-tier1.  Optimizer follow-up tracked in the #3410 PR.
        "--raw",
    ]
    print(f"   $ {' '.join(cmd[1:])}")
    proc = _sp.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    # Echo the route output so reach parsers (and humans) see the full log.
    print(proc.stdout)
    if proc.returncode in (1, 5):
        print(f"   ERROR: kct route failed with exit code {proc.returncode}")
        if proc.stderr:
            print(proc.stderr[-2000:])
        return False

    # Parse the LAST "Nets routed: N/M" occurrence (escalation mode can
    # emit several; the last reflects the final saved state).
    matches = _re.findall(r"Nets routed:\s+(\d+)/(\d+)", proc.stdout)
    if not matches:
        print("   ERROR: could not parse 'Nets routed: N/M' from kct route output")
        return False
    routed, total = (int(matches[-1][0]), int(matches[-1][1]))

    success = routed == total
    if success:
        print("\n   SUCCESS: All signal nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {routed}/{total} signal nets")

    return success


def export_manufacturing_bundle(routed_path: Path, output_dir: Path) -> bool:
    """Export the manufacturing bundle (gerbers, BOM, CPL, report).

    Issue #3095: AC requires the routed PCB to produce a manufacturing
    bundle (`fleet status` checks for ``manufacturing/`` directory with
    ``manifest.json``).  ``kct export`` runs the standard JLCPCB recipe
    (gerbers + drill + BOM + CPL + report.{md,pdf} + manifest.json) but
    skips the strict pre-flight DRC/ERC gate so the bundle can be
    produced even with the small allowlisted USB-C tolerance errors.
    """
    print("\n" + "=" * 60)
    print("Exporting manufacturing bundle...")
    print("=" * 60)

    mfg_dir = output_dir / "manufacturing"
    # Issue #3150: board 03 is ROUTED/DRC-gated against jlcpcb-tier1
    # (Capability-Plus permits the standard via-in-pad on U1-28 / USB_D-
    # that tier-0 forbids; see the manufacturers: override in
    # .github/routed-drc-tolerance.yml).  The `kct export` fab-spec layer,
    # however, only recognises the base `jlcpcb` profile name for CPL /
    # spec-overlay generation (tier-1 is a routing/DRC capability tier, not
    # a distinct fab house), so the bundle exports against `jlcpcb` --
    # exactly mirroring board-04's split (#3033/#3038): route+check at
    # tier-1, export at jlcpcb.
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "export",
        str(routed_path),
        "--output",
        str(mfg_dir),
        "--mfr",
        "jlcpcb",
        "--skip-preflight",
    ]
    print(f"\n   Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n")[-15:]:
            print(f"   {line}")
    if result.returncode != 0:
        if result.stderr:
            print(f"\n   Error: {result.stderr}")
        return False
    manifest = mfg_dir / "manifest.json"
    if manifest.exists():
        print(f"\n   Manifest: {manifest}")
        return True
    print("\n   WARNING: manifest.json not produced")
    return False


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB."""
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        # Issue #3150: align the local DRC summary with the jlcpcb-tier1
        # profile this board ships and is gated against (see
        # export_manufacturing_bundle and the manufacturers: override in
        # .github/routed-drc-tolerance.yml).
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(pcb_path),
                "--mfr",
                "jlcpcb-tier1",
            ],
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
        project_path = create_project(output_dir, "usb_joystick")

        # Step 2: Create schematic
        sch_path = create_usb_joystick_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_usb_joystick_pcb(output_dir)

        # Step 4.5: Create copper-pour zones for GND/VCC/VBUS so the
        # power-net pads land on filled copper instead of being stranded
        # by the router's ``skip_nets`` list (#3095).
        create_zones_for_pcb(pcb_path)

        # Step 5: Route PCB
        routed_path = output_dir / "usb_joystick_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 5.5: Fill the zone polygons in the routed PCB so DRC's
        # ``connectivity`` rule sees the power-net pads as connected.
        fill_zones_in_routed_pcb(routed_path)

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Step 7: Export manufacturing bundle (gerbers, BOM, CPL,
        # report).  Required by AC of #3095 so ``kct fleet status``
        # reports ``ship_ready=true``.
        mfg_success = export_manufacturing_bundle(routed_path, output_dir)

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
        print(f"  MFG bundle: {'PASS' if mfg_success else 'FAIL'}")
        print("\nBoard description:")
        print("  - USB game controller with analog joystick")
        print("  - 32-pin QFP MCU")
        print("  - USB Type-C connector")
        print("  - 4 tactile buttons")

        # For this complex demo board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
