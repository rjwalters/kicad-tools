#!/usr/bin/env python3
"""
BLDC Motor Controller - Full Design Pipeline

This script demonstrates the complete PCB design workflow for a power
electronics board:
1. Create project file
2. Create schematic with power rails and components
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design includes:
- Multi-voltage power supply (12-24V -> 5V -> 3.3V)
- Gate driver for 3-phase motor control
- Power MOSFET half-bridges
- Current sensing with shunt resistors
- MCU for motor control

Usage:
    python design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import subprocess
import sys
import uuid
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.blocks import (
    CurrentSenseShunt,
    DebugHeader,
    LEDIndicator,
    ThreePhaseInverter,
    create_bootstrap_capacitor_array,
    create_crystal_with_loads,
    create_dual_supply_cascade,
    create_gate_drive_resistor_array,
    create_hall_sensor_input,
    create_mcu_decoupling_array,
)
from kicad_tools.schematic.blocks._stub_helpers import (
    _emit_pin_net_stub,
    _stub_endpoint_would_collide,
)
from kicad_tools.schematic.models.schematic import Schematic

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def create_bldc_controller(output_dir: Path) -> Path:
    """
    Create a BLDC motor controller schematic.

    This demonstrates power electronics design patterns:
    - High-current power input with protection
    - Multi-stage power supply (buck + LDO)
    - 3-phase gate driver
    - Power MOSFET half-bridges
    - Current sensing for closed-loop control

    Returns the path to the generated schematic file.
    """
    print("Creating BLDC Motor Controller schematic...")
    print("=" * 60)

    # Path to the project-local symbol library shipped by PR #3388.  The
    # stock KiCad library only ships ``Driver_Motor:DRV8308`` (a 39-pin
    # sensorless-BLDC controller), but the BOM ships ``DRV8301`` (HTSSOP-56,
    # 57-pin pre-driver, LCSC C129292) and the PCB carries the matching
    # HTSSOP-56 footprint.  Registering the local lib lets ``add_symbol``
    # resolve ``board05_custom:DRV8301`` against this file.  See issues
    # #3384 / #3387.
    _BOARD05_SYMBOL_LIB = (
        Path(__file__).resolve().parent / "symbols" / "board05_custom.kicad_sym"
    )

    # Create schematic with title block
    sch = Schematic(
        title="BLDC Motor Controller",
        date="2025-01",
        revision="A",
        company="kicad-tools Example",
        comment1="3-Phase Brushless DC Motor Driver",
        comment2="Thermal analysis and high-current routing demo",
        local_symbol_libs=[_BOARD05_SYMBOL_LIB],
    )

    # Define power rail Y coordinates
    RAIL_VMOTOR = 25  # 12-24V motor power
    RAIL_5V = 45  # 5V gate driver supply
    RAIL_3V3 = 65  # 3.3V logic supply
    RAIL_GND = 280  # Ground

    # Schematic section X positions
    X_POWER_IN = 25  # Power input section
    X_BUCK = 80  # Buck converter
    X_LDO = 140  # LDO regulator
    X_MCU = 200  # MCU section
    X_GATE_DRV = 280  # Gate driver
    X_PHASE_A = 25  # Phase A half-bridge
    X_PHASE_C = 175  # Phase C half-bridge
    X_CONNECTORS = 260  # Connectors section

    # Y position for power stage (lower section)
    Y_POWER_STAGE = 160

    # =========================================================================
    # Section 1: Power Rails
    # =========================================================================
    print("\n1. Creating power rails...")

    # Power rails.  Each #PWR power-input symbol is wired down (or up
    # for GND) to its rail's left endpoint so the symbol pin meets a
    # real wire endpoint (silences ``pin_not_connected``) AND so the
    # symbol's global-net publication unifies with the rail's labelled
    # net.  The rail net_labels use the stock-KiCad power-symbol global
    # names ("+24V", "+3V3") so the schematic-side net name and the
    # PCB-side NETS dict agree -- previously the rail used VMOTOR /
    # +3.3V and the PCB used +24V / +3V3 (or vice versa),
    # producing 19+ residual rail-rename mismatches in
    # ``kct pcb sync-netlist``.  See issues #3393 and #3384.
    #
    # For rails that lack any Output-Power driver, a PWR_FLAG is added
    # on the same column to mark the net as externally driven (silences
    # ``power_pin_not_driven``).  Note that the LM2596 OUT pin is type
    # ``output`` (not ``power_output``) and AMS1117 VO is type
    # ``power_output`` only on some symbol variants; treat regulator
    # outputs conservatively and add PWR_FLAG on +5V too.
    #
    # For rails already driven by a genuine ``power_output`` source
    # (e.g. AMS1117.VO on this symbol variant for +3V3, AMS1117.GND for
    # GND), an additional PWR_FLAG would trigger a ``pin_to_pin``
    # Output<->Power-output conflict — skip the flag on those rails.
    # See issue #3096.
    sch.add_rail(RAIL_VMOTOR, x_start=X_POWER_IN, x_end=X_PHASE_C + 60, net_label="+24V")
    sch.add_power("power:+24V", x=X_POWER_IN, y=RAIL_VMOTOR - 10, rotation=0)
    # Wire +24V symbol pin down to the rail's left endpoint.  The +24V
    # global net then unifies with the rail's VMOTOR labelled net.
    sch.add_wire(
        (X_POWER_IN, RAIL_VMOTOR - 10),
        (X_POWER_IN, RAIL_VMOTOR),
        warn_on_collision=False,
    )
    # PWR_FLAG marks VMOTOR as externally driven (J1 passive pin → fuse
    # → rail).  Without this, U1.VIN (power_input) fires
    # ``power_pin_not_driven`` because the +24V symbol is also a
    # power_input pin.  Placed 7mm east of the +24V symbol to clear it.
    sch.add_pwr_flag(X_POWER_IN + 7, RAIL_VMOTOR - 10)
    sch.add_wire(
        (X_POWER_IN + 7, RAIL_VMOTOR - 10),
        (X_POWER_IN + 7, RAIL_VMOTOR),
        warn_on_collision=False,
    )
    sch.add_junction(X_POWER_IN + 7, RAIL_VMOTOR)

    # 5V rail.
    sch.add_rail(RAIL_5V, x_start=X_BUCK + 25, x_end=X_GATE_DRV + 30, net_label="+5V")
    sch.add_power("power:+5V", x=X_BUCK + 25, y=RAIL_5V - 10, rotation=0)
    sch.add_wire(
        (X_BUCK + 25, RAIL_5V - 10),
        (X_BUCK + 25, RAIL_5V),
        warn_on_collision=False,
    )
    # PWR_FLAG marks +5V as a power source.  The LM2596 OUT pin is type
    # ``output`` (regular, not ``power_output``); without PWR_FLAG, the
    # U1.FB pin (Input type, tied to +5V via ``buck_pin_nets``) fires
    # ``power_pin_not_driven`` because no Output-Power pin drives +5V.
    # Note: the BuckBlock SW-node wire was patched in #3096 to route
    # laterally around the inductor body (without the patch a vertical
    # wire crosses inductor pin 2 as an interior T-junction, shorting
    # SW to +5V and creating a fake Output<->Power_output conflict).
    sch.add_pwr_flag(X_BUCK + 32, RAIL_5V - 10)
    sch.add_wire(
        (X_BUCK + 32, RAIL_5V - 10),
        (X_BUCK + 32, RAIL_5V),
        warn_on_collision=False,
    )
    sch.add_junction(X_BUCK + 32, RAIL_5V)

    # 3.3V rail.  Start the rail west of the LDO so it covers both
    # U2.VO (x=147.32) AND the LDO output cap C6 (x=160.02), whose pin-1
    # vertical wire endpoint was previously floating past the rail's
    # left edge (rail used to start at X_LDO+25=165 — east of both).
    sch.add_rail(RAIL_3V3, x_start=X_LDO + 7, x_end=X_MCU + 80, net_label="+3V3")
    sch.add_power("power:+3V3", x=X_LDO + 25, y=RAIL_3V3 - 10, rotation=0)
    sch.add_wire(
        (X_LDO + 25, RAIL_3V3 - 10),
        (X_LDO + 25, RAIL_3V3),
        warn_on_collision=False,
    )
    # The +3V3 symbol-to-rail vertical wire now lands on the rail's
    # interior (the rail was extended westward above to cover C6); add a
    # junction so the T-connection is electrically valid.
    sch.add_junction(X_LDO + 25, RAIL_3V3)

    # Ground rail (spans full width).  Built as a single ``add_rail`` wire
    # ending at x=X_CONNECTORS+40 (=300).  The gate-driver bypass-cap
    # column at x=309.88 (C19 today, created by GateDriverBlock; see the
    # ``cap_ref_start=15`` note below — that block adds num_phases to the
    # start, so refs come out as C18/C19) lies past the rail's right
    # endpoint, so C19's pin-2 vertical wire endpoint at (309.88, 279.4)
    # has no rail endpoint to meet.  A short extension segment is added
    # after the rail to bridge from (299.72, 279.4) -> (309.88, 279.4) so
    # the C19 GND wire meets a real wire endpoint (closes the
    # ``pin_not_connected`` ERC error on C19's pin 2; see issue #3004).
    # GND rail.  GND is already driven by AMS1117.GND (power_in pin
    # type; the LDO symbol does NOT actually have a power_out GND, but
    # the demo-board MOSFET source pins and the connector pins drive
    # GND collectively).  Wire the GND symbol up to the rail's left
    # endpoint so its pin meets a real wire endpoint.
    sch.add_rail(RAIL_GND, x_start=X_POWER_IN, x_end=X_CONNECTORS + 40, net_label="GND")
    sch.add_power("power:GND", x=X_POWER_IN, y=RAIL_GND + 10, rotation=0)
    sch.add_wire(
        (X_POWER_IN, RAIL_GND + 10),
        (X_POWER_IN, RAIL_GND),
        warn_on_collision=False,
    )
    # GND-rail right-edge extension covering the gate-driver bypass-cap
    # column.  Snapped x's are 299.72 (rail end) and 309.88 (C19 pin 2 x).
    sch.add_wire((299.72, RAIL_GND), (309.88, RAIL_GND), warn_on_collision=False)
    # Junction marks the 3-way convergence at the existing rail end (rail
    # wire, C18 pin-2 vertical wire, and the new extension).
    sch.add_junction(299.72, RAIL_GND)

    print("   Added +24V (VMOTOR), +5V, +3V3, and GND rails")

    # =========================================================================
    # Section 2: Power Input (12-24V DC)
    # =========================================================================
    print("\n2. Adding power input section...")

    # Power input connector (2-pin)
    j_power = sch.add_symbol(
        "Connector:Conn_01x02_Pin",
        x=X_POWER_IN,
        y=100,
        ref="J1",
        value="Power Input",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    print(f"   Power connector: {j_power.reference}")

    # Input fuse
    fuse = sch.add_symbol(
        "Device:Fuse",
        x=X_POWER_IN + 20,
        y=100,
        ref="F1",
        value="15A",
        footprint="Fuse:Fuse_1206_3216Metric",
    )
    print(f"   Fuse: {fuse.reference} = 15A")

    # TVS diode for transient protection
    tvs = sch.add_symbol(
        "Device:D_TVS",
        x=X_POWER_IN + 40,
        y=120,
        ref="D1",
        value="SMBJ24A",
        rotation=90,
        footprint="Diode_SMD:D_SMA",
    )
    print(f"   TVS diode: {tvs.reference}")

    # Bulk input capacitors
    c_bulk1 = sch.add_symbol(
        "Device:C",
        x=X_POWER_IN + 55,
        y=120,
        ref="C1",
        value="470uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    c_bulk2 = sch.add_symbol(
        "Device:C",
        x=X_POWER_IN + 70,
        y=120,
        ref="C2",
        value="100nF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    print(f"   Bulk caps: {c_bulk1.reference} = 470uF, {c_bulk2.reference} = 100nF")

    # Wire power input to VMOTOR rail
    # Connector pin 1 → Fuse → TVS/Caps → VMOTOR rail
    pin1_pos = j_power.pin_position("1")
    fuse_in = fuse.pin_position("1")
    fuse_out = fuse.pin_position("2")

    sch.add_wire(pin1_pos, fuse_in)
    sch.add_wire(fuse_out, (fuse_out[0], RAIL_VMOTOR), warn_on_collision=False)
    sch.add_junction(fuse_out[0], RAIL_VMOTOR)

    # Wire TVS and caps to rails (TVS has pins A1, A2 for bidirectional)
    tvs_a1 = tvs.pin_position("A1")
    tvs_a2 = tvs.pin_position("A2")
    sch.add_wire(tvs_a1, (tvs_a1[0], RAIL_VMOTOR), warn_on_collision=False)
    sch.add_junction(tvs_a1[0], RAIL_VMOTOR)
    sch.add_wire(tvs_a2, (tvs_a2[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(tvs_a2[0], RAIL_GND)

    sch.wire_decoupling_cap(c_bulk1, RAIL_VMOTOR, RAIL_GND)
    sch.wire_decoupling_cap(c_bulk2, RAIL_VMOTOR, RAIL_GND)

    # Connector pin 2 → GND
    pin2_pos = j_power.pin_position("2")
    sch.add_wire(pin2_pos, (pin2_pos[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(pin2_pos[0], RAIL_GND)

    # =========================================================================
    # Sections 3 + 4: Cascaded Power Tree (24V → 5V buck → 3.3V LDO)
    # =========================================================================
    # The DualSupplyCascade block encapsulates the buck + LDO topology
    # decision (high-V in for efficiency, then linear regulator for clean
    # low-noise final rail). It owns:
    #   - U1 (LM2596-5.0 buck regulator) + C3 (input) + C4 (output)
    #     + L1 (33uH) + D2 (SS34 Schottky)
    #   - U2 (AMS1117-3.3 LDO) + C5 (input) + C6 (output)
    print("\n3. Adding cascaded power tree (24V → 5V → 3.3V)...")

    # The buck (U1) and LDO (U2) symbols carry Power-input pins on VIN /
    # GND and an Input-type FB pin (fixed-output LM2596 variants).  Without
    # an explicit label-on-wire stub at each pin, KiCad ERC reports
    # ``power_pin_not_driven`` (VIN/GND/VI) and ``pin_not_connected`` (FB,
    # VO).  The ``pin_nets`` kwargs below emit short stub-wires + labels
    # at each named pin so ERC sees the pin as driven via net-name
    # continuity (VMOTOR, +5V, +3.3V, GND).  See issue #2994.
    cascade = create_dual_supply_cascade(
        sch,
        x_buck=X_BUCK,
        x_ldo=X_LDO,
        y=100,
        vin=24.0,
        v_mid=5.0,
        vout=3.3,
        cap_ref_start=3,  # C3, C4 (buck), C5, C6 (LDO)
        buck_ref="U1",
        ldo_ref="U2",
        buck_diode_ref="D2",  # D1 is used for TVS diode
        buck_inductor_ref="L1",
        # U1 (LM2596-5.0): VIN <- +24V rail (VMOTOR), GND <- GND rail,
        # FB tied to +5V (fixed-output variant senses output directly).
        buck_pin_nets={
            "VIN": "+24V",
            "GND": "GND",
            "FB": "+5V",
        },
        # U2 (AMS1117-3.3): VI <- +5V rail, VO -> +3V3 rail (drives the
        # downstream MCU).  GND already wired by ``connect_to_rails`` but
        # we add a label here for symmetry / clarity.
        ldo_pin_nets={
            "VI": "+5V",
            "VO": "+3V3",
            "GND": "GND",
        },
    )

    # Patch footprints on the LDO stage so the BOM matches the
    # pre-refactor design exactly (the cascade leaves footprints unset by
    # default).
    cascade.ldo.ldo.footprint = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    cascade.ldo.input_cap.footprint = "Capacitor_SMD:C_0805_2012Metric"
    for cap in cascade.ldo.output_caps:
        cap.footprint = "Capacitor_SMD:C_0805_2012Metric"

    # Patch footprints on the buck stage so the BOM (extracted from the
    # schematic) reports the parts the PCB actually places. Strings must
    # mirror the PCB-side hard-codes in ``generate_d2pak`` /
    # ``generate_cap_0805`` / ``generate_inductor_smd`` /
    # ``generate_diode_sma`` (see design.py:1433, 1758, 1851, 1870) so the
    # schematic↔PCB drift checker stays clean. Without these, the
    # manufacturing preflight ``bom_fields`` check fails with "missing
    # footprint" warnings on U1/C3/C4/L1/D2 (issue #3211).
    cascade.buck.regulator.footprint = "Package_TO_SOT_SMD:TO-263-5_TabPin3"
    cascade.buck.input_cap.footprint = "Capacitor_SMD:C_0805_2012Metric"
    cascade.buck.output_cap.footprint = "Capacitor_SMD:C_0805_2012Metric"
    cascade.buck.inductor.footprint = "Inductor_SMD:L_1210_3225Metric"
    cascade.buck.diode.footprint = "Diode_SMD:D_SMA"

    # Patch the buck input cap value to match the PCB silkscreen.  The
    # ``create_dual_supply_cascade`` factory pulls ``buck_input_cap`` from
    # ``_BUCK_COMPONENTS_TABLE`` (100uF for the 24V->5V row), but the
    # board-05 PCB generator hardcodes a 220uF input cap (a reasonable
    # ripple choice for a 24V LM2596).  Without this post-construction
    # patch the schematic emits 100uF for C3 while the PCB emits 220uF,
    # producing a value-drift mismatch on every fresh build.  Patching
    # here rather than touching the SDK default keeps other boards
    # unaffected.  See issue #3210.
    cascade.buck.input_cap.value = "220uF"

    # Wire each stage to its rails in one call (buck VIN -> VMOTOR,
    # buck VOUT == LDO VIN -> 5V rail, LDO VOUT -> 3V3 rail).
    cascade.connect_to_rails(
        vin_rail_y=RAIL_VMOTOR,
        v_mid_rail_y=RAIL_5V,
        vout_rail_y=RAIL_3V3,
        gnd_rail_y=RAIL_GND,
    )

    # Tie the LM2596 ~ON/OFF pin to GND for always-on operation
    # (active-low: GND = ON, >1.3V = OFF). Drilling into ``cascade.buck``
    # keeps this board-specific tweak possible after the refactor.
    buck = cascade.buck
    try:
        on_off_pos = buck.regulator.pin_position("~{ON}/OFF")
        sch.add_wire(on_off_pos, (on_off_pos[0], RAIL_GND), warn_on_collision=False)
        sch.add_junction(on_off_pos[0], RAIL_GND)
    except KeyError:
        # Some LM2596 symbols may have different pin names
        try:
            on_off_pos = buck.regulator.pin_position("ON/OFF")
            sch.add_wire(on_off_pos, (on_off_pos[0], RAIL_GND), warn_on_collision=False)
            sch.add_junction(on_off_pos[0], RAIL_GND)
        except KeyError:
            pass  # Pin not found, may not be present on this symbol variant

    print(f"   Buck regulator: {buck.regulator.reference} (LM2596-5.0)")
    print(f"   Inductor: {buck.inductor.reference} = 33uH")
    print(f"   Diode: {buck.diode.reference} = SS34 (Schottky)")
    print(f"   Buck stage efficiency: {buck.get_efficiency_estimate() * 100:.0f}%")

    print(f"\n4. LDO stage (5V → 3.3V): {cascade.ldo.ldo.reference} (AMS1117-3.3)")
    print(
        f"   Cascade total efficiency: "
        f"{cascade.get_efficiency_estimate() * 100:.0f}% "
        f"(buck × LDO; multiplicative)"
    )

    # =========================================================================
    # Section 5: MCU (STM32G431K8Tx in LQFP-32)
    # =========================================================================
    print("\n5. Adding MCU section...")

    # Bypass capacitors for MCU (placed first so we can wire later).
    # STM32G431K8Tx has 2 VDD + 1 VDDA = 3 supply pins, but historically this
    # board uses only 2 bypass caps + 1 bulk cap (C7/C8/C9); preserve that.
    mcu_decoupling = create_mcu_decoupling_array(
        sch,
        x=X_MCU,
        y=100,
        supply_pins=2,
        ref_start=7,
        spacing=10,
        cap_symbol="Device:C",
        cap_footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    c_mcu1, c_mcu2, c_mcu3 = mcu_decoupling.caps
    print(f"   Bypass caps: {c_mcu1.reference}, {c_mcu2.reference}, {c_mcu3.reference}")

    mcu_decoupling.connect_to_rails(RAIL_3V3, RAIL_GND)

    # Place STM32G431K8Tx MCU (LQFP-32) below the bypass caps
    # The STM32G431K_6-8-B_Tx symbol body spans ~25mm wide x ~55mm tall.
    # Place it well below the bypass caps to leave room for pin wires.
    MCU_X = X_MCU + 30  # Approximate horizontal centre under MCU section
    MCU_Y = 165  # Below the rails (y=160 power stage row)
    mcu = sch.add_symbol(
        "MCU_ST_STM32G4:STM32G431K8Tx",
        x=MCU_X,
        y=MCU_Y,
        ref="U10",
        value="STM32G431K8Tx",
        footprint="Package_QFP:LQFP-32_7x7mm_P0.8mm",
    )
    print(f"   MCU: {mcu.reference} (STM32G431K8Tx, LQFP-32)")

    # Wire MCU power pins to rails.  The STM32G431K_6-8-B_Tx symbol exposes
    # VDD/VDDA/VSS/VSSA pins; route each to the appropriate rail with a small
    # local label so the netlist matches the PCB nets.
    def _connect_mcu_pin_to_label(pin_id: str, label_text: str, dx: int = 0, dy: int = 0):
        """Drop a wire from a MCU pin to a local label; the label provides the
        net connection by name (e.g. ``GATE_AH``).

        Collision-aware (issue #3379): the naive single-stub form silently
        bridges nets when the label coordinate lands on the interior of an
        unrelated foreign wire. Board 05's MCU SWDIO / SWCLK / ISENSE_A-/B-/C-
        / GND pins all suffered this: PHASE_A and PHASE_B wires from the
        motor connector run east-to-west across U10's right-side label
        row, and a vertical HallSensorInput rail at x=227.33 crosses
        through both the +3.3V and GND label rows. kicad-cli reported
        U10.23->PHASE_A, U10.24->PHASE_B, U10.14/16/32->+3.3V (instead
        of the intended SWDIO/SWCLK/GND), regressing DRC from 6 to 73
        violations after a fresh ``kct pcb sync-netlist --apply``.

        This implementation tries the requested geometry first, then
        escalates through (a) longer horizontal stub, (b) primary-side
        with a small vertical offset, (c) opposite-side stub, and (d)
        L-shaped stub (horizontal then vertical to a free row).  Raises
        ``ValueError`` if no candidate is collision-free -- silent
        net-bridging is unrecoverable at netlist time, so the failure
        must surface loudly.
        """
        pin_pos = mcu.pin_position(pin_id)

        # Build a list of candidate label endpoints in priority order.
        # Each candidate is either a straight stub (one wire) or an
        # L-shaped stub (two wires) terminating at ``(lx, ly)``.
        primary_dx = dx if dx != 0 else 5  # default outward direction
        opposite_dx = -primary_dx
        grid = 2.54  # KiCad default grid

        # Straight-stub candidates: try the requested geometry, then
        # progressively longer stubs in the same direction, then a
        # slight vertical nudge, then the opposite direction.
        straight: list[tuple[float, float]] = []
        for k in (1, 2, 3, 4, 5, 6):
            straight.append((pin_pos[0] + primary_dx * k, pin_pos[1] + dy))
        for k in (1, -1, 2, -2):
            straight.append((pin_pos[0] + primary_dx, pin_pos[1] + dy + grid * k))
        if opposite_dx != primary_dx:
            for k in (1, 2, 3):
                straight.append((pin_pos[0] + opposite_dx * k, pin_pos[1] + dy))

        # L-shaped candidates: horizontal stub to a midpoint, then
        # vertical to a row above/below.  Used when no straight stub
        # clears (SWDIO/SWCLK rely on this when the row is dense).
        l_shaped: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for dy_off in (-grid, grid, -2 * grid, 2 * grid, -3 * grid, 3 * grid):
            mid = (pin_pos[0] + primary_dx, pin_pos[1])
            end = (mid[0], pin_pos[1] + dy_off)
            l_shaped.append((mid, end))

        def _endpoint_safe(point: tuple[float, float]) -> bool:
            # Reject if the point lies on the interior of any existing
            # wire (silent net-bridging risk).  ``_stub_endpoint_would_collide``
            # also catches degenerate landing-on-existing-endpoint cases,
            # but those usually mean the caller is wiring to an existing
            # symbol pin -- conservative-reject is safer for MCU stubs.
            # Check the SNAPPED endpoint coordinates so the check matches
            # where the label actually lands after grid snapping.
            sx = sch._snap_coord(point[0], "_connect_mcu_pin_to_label probe")
            sy = sch._snap_coord(point[1], "_connect_mcu_pin_to_label probe")
            return not _stub_endpoint_would_collide(sch, sx, sy)

        # Try straight stubs first.
        for end_pos in straight:
            if _endpoint_safe(end_pos):
                sch.add_wire(pin_pos, end_pos, warn_on_collision=False)
                sch.add_label(
                    label_text,
                    end_pos[0],
                    end_pos[1],
                    rotation=0,
                    validate_connection=False,
                )
                return

        # Fall back to L-shaped routing.  The label sits on the second
        # wire's endpoint; check both the midpoint and the final endpoint
        # for collisions.
        for mid, end in l_shaped:
            if _endpoint_safe(mid) and _endpoint_safe(end):
                sch.add_wire(pin_pos, mid, warn_on_collision=False)
                sch.add_wire(mid, end, warn_on_collision=False)
                sch.add_label(
                    label_text,
                    end[0],
                    end[1],
                    rotation=0,
                    validate_connection=False,
                )
                return

        # No candidate worked.  Surface the failure loudly so the
        # caller can move the MCU or split the colliding rail.
        raise ValueError(
            f"_connect_mcu_pin_to_label: cannot place label {label_text!r} for "
            f"pin {pin_id} at {pin_pos} without colliding with a foreign wire. "
            f"All straight and L-shaped stub candidates landed on existing "
            f"wires (silent net-bridging risk). Move U10 or split the "
            f"colliding rails."
        )

    # U10 pin-label emission is DEFERRED until after Sections 6-10 have
    # added their wires (PHASE_A/B/C from J2, vertical HallSensorInput
    # rails near x=227, +3.3V/GND symbol stubs near the bypass caps).
    # Without deferral the collision check in ``_connect_mcu_pin_to_label``
    # can't see those future wires, and labels end up silently bridged
    # into PHASE_A / +3.3V / GND -- the original board 05 bug
    # (issue #3379) that regressed DRC from 6 to 73 violations after a
    # fresh sync-netlist. We record the desired (pin, label, dx, dy)
    # tuples here and emit them at the end of Section 11, after every
    # other section has finished drawing wires.
    deferred_mcu_labels: list[tuple[str, str, int, int]] = [
        # Power pins (VDD/VDDA = +3V3, VSS/VSSA = GND) get wired
        # straight to rails. Pin 1 (VDD), 17 (VDD), 15 (VDDA) -> +3V3.
        # Pin 14 (VSSA), 16 (VSS), 32 (VSS) -> GND.
        ("1", "+3V3", -5, 0),
        ("17", "+3V3", -5, 0),
        ("15", "+3V3", -5, 0),
        ("14", "GND", -5, 0),
        ("16", "GND", -5, 0),
        ("32", "GND", -5, 0),
        # Pin 4 = PG10 (configured as NRST)
        ("4", "NRST", -5, 0),
        # ADC current-sense returns: PA0/PA1/PA2 -> ISENSE_A-/B-/C-
        ("5", "ISENSE_A-", 5, 0),
        ("6", "ISENSE_B-", 5, 0),
        ("7", "ISENSE_C-", 5, 0),
        # Hall sensor inputs: PA6/PA7/PB0 (TIM3 CH1/CH2/CH3 capable)
        ("11", "HALL_A", 5, 0),
        ("12", "HALL_B", 5, 0),
        ("13", "HALL_C", 5, 0),
        # High-side gate PWM: PA8/PA9/PA10 (TIM1_CH1/CH2/CH3). These drive
        # the DRV8301 INH_A/B/C logic inputs (pins 17/19/21), not the MOSFET
        # gates directly -- gate output of the driver is GATE_DRV_*H, then
        # through the R20/R21/R22 slew-rate resistors to GATE_*H on the
        # MOSFET gates.
        ("18", "PWM_AH", 5, 0),
        ("19", "PWM_BH", 5, 0),
        ("20", "PWM_CH", 5, 0),
        # SWD debug pins
        ("23", "SWDIO", 5, 0),
        ("24", "SWCLK", 5, 0),
        ("26", "SWO", 5, 0),
        # Low-side gate PWM: PB6/PB7/PB8 (TIM4_CH1/CH2/CH3, sync'd with
        # TIM1). Drives DRV8301 INL_A/B/C logic inputs (pins 18/20/22).
        ("29", "PWM_AL", 5, 0),
        ("30", "PWM_BL", 5, 0),
        ("31", "PWM_CL", 5, 0),
        # Crystal pins: PF0/PF1 -> OSC_IN/OSC_OUT
        ("2", "OSC_IN", -5, 0),
        ("3", "OSC_OUT", -5, 0),
    ]

    # Unused STM32G431K8 GPIO pins (PA3-PA5, PA11-PA15, PB3-PB5 mapped to
    # LQFP-32 pins 8, 9, 10, 21, 22, 25, 27, 28).  This demo design does
    # not consume those signals, so mark each as intentionally unconnected
    # to silence ``pin_not_connected`` ERC errors.  See issue #3004.
    for nc_pin in ["8", "9", "10", "21", "22", "25", "27", "28"]:
        nc_pos = mcu.pin_position(nc_pin)
        sch.add_no_connect(nc_pos[0], nc_pos[1])

    print(
        f"   Deferred {len(deferred_mcu_labels)} U10 pin labels "
        f"(6 PWM, 3 HALL, 3 ISENSE-, 4 SWD, 3 power, 1 NRST, 2 OSC); "
        f"emitted after Section 11 (issue #3379)"
    )
    print("   Marked 8 unused GPIO pins as no-connect (PA3-PA5, PA11-PA15, PB3-PB5)")

    # Crystal oscillator (8MHz)
    xtal = create_crystal_with_loads(
        sch,
        x=X_MCU + 70,
        y=100,
        frequency="8MHz",
        load_pF=20,
        cap_ref_start=10,
    )
    xtal.connect_to_rails(gnd_rail_y=RAIL_GND)
    print(f"   Crystal: {xtal.crystal.reference} 8MHz")

    # Add OSC_IN/OSC_OUT labels on the crystal pins so they connect to the MCU
    # (the MCU side has the same labels above).
    xtal_in_pos = xtal.crystal.pin_position("1")
    xtal_out_pos = xtal.crystal.pin_position("2")
    # Add small wire stubs and labels (validate=False because the wires alone
    # may not have caught the label position before snapping)
    sch.add_wire(xtal_in_pos, (xtal_in_pos[0] - 5, xtal_in_pos[1]), warn_on_collision=False)
    sch.add_label(
        "OSC_IN", xtal_in_pos[0] - 5, xtal_in_pos[1], rotation=0, validate_connection=False
    )
    sch.add_wire(xtal_out_pos, (xtal_out_pos[0] + 5, xtal_out_pos[1]), warn_on_collision=False)
    sch.add_label(
        "OSC_OUT", xtal_out_pos[0] + 5, xtal_out_pos[1], rotation=0, validate_connection=False
    )

    # Debug header (SWD).  SWD-6 pinout: 1=VCC, 2=SWDIO, 3=GND, 4=SWCLK,
    # 5=GND, 6=NRST.  ``connect_to_rails`` already wires pin 1 (VCC) and
    # pin 3 (first GND) via topology, but ERC requires a label-on-wire
    # stub at each pin to see it as driven.  Pin 5 (second GND) is not
    # covered by the block's ``_build_ports`` dedup logic, so it falls
    # through to ``pin_not_connected``.  Use ``pin_nets`` to declare the
    # rail nets for pins 1 and 5 explicitly; signal pins 2/4/6 follow
    # below with the same kwarg (replaces the prior inline stub loop).
    debug = DebugHeader(
        sch,
        x=X_MCU + 100,
        y=100,
        interface="swd",
        pins=6,
        ref="J4",
        pin_nets={
            "1": "+3V3",
            "2": "SWDIO",
            "3": "GND",
            "4": "SWCLK",
            "5": "GND",
            "6": "NRST",
        },
    )
    debug.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   Debug header: {debug.header.reference}")

    # =========================================================================
    # Section 6: Gate Driver (DRV8301 manual placement + BootstrapCapacitorArray)
    # =========================================================================
    print("\n6. Adding gate driver...")

    # 3-phase gate driver IC (DRV8301).  This board ships the real TI DRV8301
    # (HTSSOP-56, LCSC C129292), NOT the stock-KiCad ``Driver_Motor:DRV8308``
    # that GateDriverBlock auto-selects (39-pin sensorless-BLDC controller
    # with a completely different pinout).  The project-local symbol
    # library at ``symbols/board05_custom.kicad_sym`` -- shipped by PR
    # #3388 -- provides a 57-pin DRV8301 symbol matching the HTSSOP-56
    # pinout in the PCB footprint.  Registering that lib via
    # ``Schematic(local_symbol_libs=[...])`` above lets ``add_symbol`` resolve
    # ``board05_custom:DRV8301`` against this file.
    #
    # We bypass ``GateDriverBlock`` here because the block hard-codes
    # ``Driver_Motor:DRV8308`` as its 3-phase symbol AND its pin_nets
    # heuristic assumes that symbol's pin names (UHSG/ULSG/...) -- neither
    # of which apply to the DRV8301 (which uses GH_A/GL_A/INH_A/INL_A
    # naming per TI's datasheet SLOS719F).  The bootstrap caps and bypass
    # caps below are still created via the existing factory helpers so the
    # net-name continuity to the rest of the board is preserved.  See
    # issues #3384 and #3387.
    #
    # Layout note: U3 is placed at (X_U3, Y_U3) = (355, 145), well east
    # of the J3 Hall connector (whose VCC/GND column wires at x=265.43
    # would otherwise collide with the DRV8301's left-edge pin lines
    # spanning x=264.16..267.3 if U3 sat at the historical X_GATE_DRV=280
    # position; that collision silently bridged AGND to +3.3V, see #3387
    # for the full investigation).  At (355, 145) the left-edge stubs land
    # at x=337.82 (clear of R22's output-stub label at x=332.74, which
    # at x=350 stacked GATE_CH on top of U3.12's GND label) and the
    # right-edge stubs land at x=373.38 (well east of all other components).
    X_U3 = 355
    Y_U3 = 145
    u3 = sch.add_symbol(
        "board05_custom:DRV8301",
        X_U3,
        Y_U3,
        "U3",
        "DRV8301",
        footprint="Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm",
    )
    print("   Gate driver: U3 DRV8301 (board05_custom:DRV8301)")

    # Per-pin net mapping for the DRV8301 schematic symbol.  Mirrors
    # ``DRV8301_PINS`` in the PCB-side block below (kept in lock-step --
    # ``test_board_05_u3_drv8301_pin_nets.py`` asserts the two agree, so a
    # divergence surfaces loudly).  See the PCB-side block for the full
    # per-pin rationale (datasheet SLOS719F pin-function table).
    #
    # Special handling:
    #   * Pin 28 (AGND) ties to the global GND net via a label, NOT a wire
    #     to the rail -- the AGND-vs-PGND distinction is made on the PCB
    #     copper via the analog-ground bridge (board-05 analyses #3178).
    #   * Pin 57 (PowerPAD) is the exposed thermal pad on the bottom of
    #     the package.  The symbol places this pin at the bottom-center,
    #     orientation 90 (pin line points upward), so the standard
    #     ``_emit_pin_net_stub`` horizontal heuristic does not apply --
    #     we emit a vertical stub + GND label manually.
    DRV8301_SCHEMATIC_PIN_NETS: dict[str, str] = {
        # Pins 1-7: Buck regulator support.
        "1": "GND",          # RT_CLK   (buck timing R)
        "2": "GND",          # COMP     (buck error amp output)
        "3": "+5V",          # VSENSE   (buck output FB = +5V rail)
        "4": "+3V3",        # PWRGD    (open-drain, pull-up)
        "5": "+3V3",        # nOCTW    (open-drain, pull-up)
        "6": "+3V3",        # nFAULT   (open-drain, pull-up)
        "7": "GND",          # DTC      (dead-time, R to GND)
        # Pins 8-16: SPI / control / charge pump / GVDD.
        "8": "+3V3",        # nSCS     (idle high)
        "9": "+3V3",        # SDI
        "10": "+3V3",       # SDO      (open-drain, pull-up)
        "11": "+3V3",       # SCLK
        "12": "GND",         # DC_CAL   (normal operation)
        "13": "+5V",         # GVDD
        "14": "+5V",         # CP1      (charge pump cap 1)
        "15": "+5V",         # CP2      (charge pump cap 2)
        "16": "+3V3",       # EN_GATE  (always-on)
        # Pins 17-22: PWM logic inputs (driven by the MCU).
        "17": "PWM_AH",      # INH_A
        "18": "PWM_AL",      # INL_A
        "19": "PWM_BH",      # INH_B
        "20": "PWM_BL",      # INL_B
        "21": "PWM_CH",      # INH_C
        "22": "PWM_CL",      # INL_C
        # Pins 23-28: Analog supplies / current-sense amps (left-edge).
        "23": "+3V3",       # DVDD
        "24": "+3V3",       # REF
        "25": "ISENSE_A+",   # SO1      (op-amp 1 output)
        "26": "ISENSE_B+",   # SO2      (op-amp 2 output)
        "27": "+5V",         # AVDD
        "28": "GND",         # AGND
        # Pins 29-33: PVDD / current-sense diff inputs (right edge top).
        "29": "+24V",      # PVDD1
        "30": "ISENSE_B+",   # SP2
        "31": "ISENSE_B-",   # SN2
        "32": "ISENSE_A+",   # SP1
        "33": "ISENSE_A-",   # SN1
        # Pins 34-38: Half-bridge C (low-side/gate/high-side/gate/bootstrap).
        "34": "ISENSE_C-",   # SL_C
        "35": "GATE_CL",     # GL_C
        "36": "PHASE_C",     # SH_C
        "37": "GATE_DRV_CH", # GH_C
        "38": "+24V",      # BST_C (DC tie via cap to VMOTOR rail)
        # Pins 39-43: Half-bridge B.
        "39": "ISENSE_B-",   # SL_B
        "40": "GATE_BL",     # GL_B
        "41": "PHASE_B",     # SH_B
        "42": "GATE_DRV_BH", # GH_B
        "43": "+24V",      # BST_B
        # Pins 44-48: Half-bridge A.
        "44": "ISENSE_A-",   # SL_A
        "45": "GATE_AL",     # GL_A
        "46": "PHASE_A",     # SH_A
        "47": "GATE_DRV_AH", # GH_A
        "48": "+24V",      # BST_A
        # Pins 49-56: SPI / buck pins (right edge bottom).
        "49": "+3V3",       # VDD_SPI
        "50": "SW_OUT",      # PH (buck switch node)
        "51": "SW_OUT",      # PH (buck switch node, 2nd pin)
        "52": "+24V",      # BST_BK (buck bootstrap)
        "53": "+24V",      # PVDD2_1
        "54": "+24V",      # PVDD2_2
        "55": "+3V3",       # EN_BUCK
        "56": "GND",         # SS_TR (cap to GND)
        # Pin 57 is handled separately below (vertical pin orientation).
    }

    # Emit per-pin stubs + labels for pins 1-56 using the shared
    # collision-aware helper.  Pin 57 (PowerPAD) needs vertical stubbing
    # and is handled afterwards.
    for _pin_num, _net_name in DRV8301_SCHEMATIC_PIN_NETS.items():
        _pin_pos = u3.pin_position(_pin_num)
        _emit_pin_net_stub(
            sch, _pin_pos, X_U3, _net_name, None,
            block_label="U3 DRV8301 ",
        )

    # Pin 57 (PowerPAD, exposed thermal pad) -- the symbol places this pin
    # at the bottom-center with orientation 90 (pin line projects upward
    # into the body).  The horizontal stub heuristic in
    # ``_emit_pin_net_stub`` is invalid for vertical pins, so emit a short
    # downward stub + GND label manually.
    _pp_pos = u3.pin_position("57")
    _pp_stub = (_pp_pos[0], _pp_pos[1] + 2.54)
    sch.add_wire(_pp_pos, _pp_stub, warn_on_collision=False)
    sch.add_label("GND", _pp_stub[0], _pp_stub[1], rotation=90, validate_connection=False)

    # External 3-phase bootstrap cap network (BST_x to PHASE_x).
    # Uses C12-C14 to preserve existing PCB-side ref numbering and layout.
    # Match the rest of the board's 0805 passives so C12/C13/C14 land in the
    # netlist with a non-empty footprint field (see issue #3017).
    create_bootstrap_capacitor_array(
        sch,
        x=X_GATE_DRV - 20,
        y=80,
        phases=3,
        value="100nF",
        cap_ref_start=12,
        high_nets=["BST_A", "BST_B", "BST_C"],
        phase_nets=["PHASE_A", "PHASE_B", "PHASE_C"],
        cap_footprint="Capacitor_SMD:C_0805_2012Metric",
    )

    print("   Bootstrap caps: C12, C13, C14 (BootstrapCapacitorArray)")

    # Gate-driver bypass caps (C15=100nF, C16=10uF) between +5V and GND.
    # These were previously instantiated by ``GateDriverBlock`` above the
    # IC, but with U3 now placed standalone (see #3387 layout-fix above)
    # we create them directly.  Keep them at the historical x positions
    # (299.72 and 309.88) so the PCB-side cap layout does not need to
    # move; the schematic-side x is purely cosmetic.
    c15 = sch.add_symbol(
        "Device:C", 300, 81, "C15", "100nF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    c16 = sch.add_symbol(
        "Device:C", 310, 81, "C16", "10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    sch.wire_decoupling_cap(c15, RAIL_5V, RAIL_GND)
    sch.wire_decoupling_cap(c16, RAIL_5V, RAIL_GND)
    print("   Bypass caps: C15 (100nF), C16 (10uF) on +5V")

    # Series gate-drive (slew-rate) resistors between DRV8301 outputs and the
    # high-side MOSFET gates.  ``GATE_DRV_AH/BH/CH`` are the driver-IC outputs;
    # ``GATE_AH/BH/CH`` are the MOSFET-gate-side nets.  The array sits in the
    # path between them.  Low-side gates remain direct-driven for now.
    create_gate_drive_resistor_array(
        sch,
        x=X_GATE_DRV + 30,
        y=120,
        channels=3,
        value="22",
        ref_start=20,  # R20-R22 (R10-R12 are the current-sense shunts)
        input_nets=["GATE_DRV_AH", "GATE_DRV_BH", "GATE_DRV_CH"],
        output_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
    )
    print("   Gate-drive resistors: R20, R21, R22 (22 ohms, HS only)")

    # =========================================================================
    # Section 7: Power Stage (using ThreePhaseInverter and CurrentSenseShunt)
    # =========================================================================
    print("\n7. Adding power stage (6 MOSFETs)...")

    # Create 3-phase inverter using ThreePhaseInverter block
    # This creates 6 MOSFETs (Q1-Q6) in three half-bridge configuration.
    # ``gate_*_nets`` make each HalfBridge emit a stub wire + label at
    # its Q.G pin so KiCad's label-on-wire ERC check sees a real
    # connection -- this closes the second half of issue #2980 (without
    # the kwargs, Q1-Q6 gate pins floated and ERC reported six
    # ``pin_not_connected`` errors).
    inverter = ThreePhaseInverter(
        sch,
        x=X_PHASE_A,
        y=Y_POWER_STAGE,
        ref_start=1,
        ref_prefix="Q",
        mosfet_value="IRLZ44N",
        phase_labels=["A", "B", "C"],
        phase_spacing=75,
        hs_ls_spacing=40,
        gate_hs_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
        gate_ls_nets=["GATE_AL", "GATE_BL", "GATE_CL"],
    )
    print("   Three-phase inverter: Q1-Q6 (ThreePhaseInverter block)")

    # Add current sense shunts for each phase (R10-R12)
    # Using CurrentSenseShunt blocks for proper current sensing.
    # NOTE: Build the shunts BEFORE wiring the inverter to the rails so
    # that ``inverter.connect_to_rails`` can be told (via ``inline_shunts``)
    # to skip its default LS-source-to-GND wire on every phase.  Without
    # this ordering, the half-bridge helper would emit a direct LS-source
    # to GND wire that shorts the shunt (the LS source belongs on the
    # shunt's IN+ side, not on GND).  See issue #3383.
    phases = ["A", "B", "C"]
    current_sensors = []
    for i, phase in enumerate(phases):
        x_phase = X_PHASE_A + (i * 75)

        # Create current sense shunt for this phase
        sense = CurrentSenseShunt(
            sch,
            x=x_phase,
            y=Y_POWER_STAGE + 80,
            shunt_value="5mR",
            shunt_package="2512",
            ref_start=10 + i,  # R10, R11, R12
            amplifier=False,  # No amplifier for basic sensing
        )
        # NOTE (issue #3379): we intentionally do NOT call
        # ``sense.connect_to_rails(gnd_rail_y=RAIL_GND)`` here. That
        # helper would wire the shunt's IN- pin straight down to the
        # global GND rail, which short-circuits the ``ISENSE_X-`` label
        # (placed on the same pin below) into the ``GND`` net.  The
        # committed PCB and the U10 ADC topology treat ``ISENSE_X-``
        # as a *separate* Kelvin-sense net (PCB nets 15/17/19),
        # distinct from the bulk GND that carries phase current.  With
        # the ``connect_to_rails`` call, kicad-cli collapses ISENSE_X-
        # onto GND, then ``sync-netlist`` rewrites U10.5/6/7 ADC pins
        # to ``GND`` instead of the intended ``ISENSE_X-`` nets --
        # regressing the U10 round-trip that is the locus of #3379.
        # The shunt's IN- pin is connected to GND *physically* via the
        # LS MOSFET source / GND copper zone on the PCB; only the
        # schematic label is needed for the netlister because
        # ``ISENSE_X-`` is consumed only by the U10 ADC pins.
        current_sensors.append(sense)

        # Wire the inverter LS output to the current sense input.
        # Get the phase LS-source position and wire to shunt IN+.  This
        # replaces the suppressed LS-source-to-GND wire from
        # ``ThreePhaseInverter.connect_to_rails(..., inline_shunts=...)``
        # so the LS source sits on ISENSE_X+ instead of GND.
        phase_gnd = inverter.half_bridges[i].port("GND")
        sense_in = sense.port("IN_POS")
        sense_gnd = sense.port("GND")
        sch.add_wire(phase_gnd, sense_in, warn_on_collision=False)

        # Add current sense labels with connecting wires
        label_x = x_phase - 10
        # Wire from sense_in to label position for ISENSE+
        sch.add_wire(sense_in, (label_x, sense_in[1]), warn_on_collision=False)
        sch.add_label(f"ISENSE_{phase}+", label_x, sense_in[1], rotation=0)
        # Wire from sense GND to label position for ISENSE-
        sch.add_wire(sense_gnd, (label_x, sense_gnd[1]), warn_on_collision=False)
        sch.add_label(f"ISENSE_{phase}-", label_x, sense_gnd[1], rotation=0)

        print(f"   Phase {phase}: Current sense R{10 + i} (CurrentSenseShunt block)")

    # Now wire the inverter to its power rails -- but pass the per-phase
    # in-line shunts so each half-bridge skips its default LS-source-to-
    # GND wire (which would short the shunt; issue #3383).  VIN/VMOTOR
    # wires + junctions are still emitted as usual.
    inverter.connect_to_rails(
        vin_rail_y=RAIL_VMOTOR,
        gnd_rail_y=RAIL_GND,
        inline_shunts=current_sensors,
    )

    # =========================================================================
    # Section 8: Motor Output Connector
    # =========================================================================
    print("\n8. Adding motor output connector...")

    j_motor = sch.add_symbol(
        "Connector:Conn_01x03_Pin",
        x=X_CONNECTORS,
        y=Y_POWER_STAGE + 20,
        ref="J2",
        value="Motor Output",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    )
    print(f"   Motor connector: {j_motor.reference} (U/V/W)")

    # Wire motor phases from inverter block outputs to connector
    for i, phase in enumerate(phases):
        pin_pos = j_motor.pin_position(str(i + 1))
        # Get phase output from inverter block
        phase_out = inverter.port(f"PHASE_{phase}")
        # Wire from connector pin horizontally, then down to phase output level,
        # then connect to the phase output
        mid_x = phase_out[0] + 15
        # Horizontal wire from connector pin
        sch.add_wire(pin_pos, (mid_x, pin_pos[1]))
        # Vertical wire down to phase output Y level
        sch.add_wire((mid_x, pin_pos[1]), (mid_x, phase_out[1]))
        # Horizontal wire to connect to phase output
        sch.add_wire((mid_x, phase_out[1]), phase_out, warn_on_collision=False)
        sch.add_junction(phase_out[0], phase_out[1])

    # =========================================================================
    # Section 9: Hall Sensor Connector
    # =========================================================================
    print("\n9. Adding hall sensor connector...")

    j_hall = sch.add_symbol(
        "Connector:Conn_01x05_Pin",
        x=X_CONNECTORS,
        y=100,
        ref="J3",
        value="Hall Sensors",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
    )
    print(f"   Hall connector: {j_hall.reference}")

    # Wire hall signal pins through filtered input blocks and add labels.
    # Pins 1-3 are hall signals, pin 4 is VCC, pin 5 is GND.
    #
    # Each Hall channel is fed through a HallSensorInput block providing
    # a 10kOhm pull-up to +3.3V and a 10nF cap to GND for noise immunity.
    # Existing R/C designators on this board peak at R12/C16, so the new
    # parts are assigned starting at R30/C30 with one ref consumed per
    # channel (R30/C30, R31/C31, R32/C32).
    hall_labels = ["HALL_A", "HALL_B", "HALL_C"]
    label_x = X_CONNECTORS - 20
    HALL_REF_BASE = 30
    # Stagger filter blocks horizontally so adjacent R/C symbols do not
    # overlap (each block spans ~30mm vertically; pin pitch is only
    # 2.54mm, so vertical stacking at the pin Y is not viable).
    hall_block_x = [label_x - 8, label_x - 16, label_x - 24]

    for i, label in enumerate(hall_labels):
        pin_pos = j_hall.pin_position(str(i + 1))
        block_x = hall_block_x[i]
        # Place the filter block with its junction at the pin's Y so
        # SIGNAL_IN is collinear with the connector pin and SIGNAL_OUT
        # can wire straight back to the label without a jog.
        hall_block = create_hall_sensor_input(
            sch,
            x=block_x,
            y=pin_pos[1],
            ref_start=HALL_REF_BASE + i,
        )
        # Connector pin -> block SIGNAL_IN (same Y, horizontal wire)
        sch.add_wire(pin_pos, hall_block.ports["SIGNAL_IN"], warn_on_collision=False)
        # Block SIGNAL_OUT -> label position (same Y, horizontal wire)
        sch.add_wire(hall_block.ports["SIGNAL_OUT"], (label_x, pin_pos[1]), warn_on_collision=False)
        sch.add_label(label, label_x, pin_pos[1], rotation=0)
        # Block VCC -> +3.3V rail (vertical wire upward)
        vcc_port = hall_block.ports["VCC"]
        sch.add_wire(vcc_port, (vcc_port[0], RAIL_3V3), warn_on_collision=False)
        sch.add_junction(vcc_port[0], RAIL_3V3)
        # Block GND -> GND rail (vertical wire downward)
        gnd_port = hall_block.ports["GND"]
        sch.add_wire(gnd_port, (gnd_port[0], RAIL_GND), warn_on_collision=False)
        sch.add_junction(gnd_port[0], RAIL_GND)

    # Wire hall connector VCC (pin 4) to 3.3V rail
    hall_vcc_pos = j_hall.pin_position("4")
    sch.add_wire(hall_vcc_pos, (hall_vcc_pos[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(hall_vcc_pos[0], RAIL_3V3)

    # Wire hall connector GND (pin 5) to GND rail
    hall_gnd_pos = j_hall.pin_position("5")
    sch.add_wire(hall_gnd_pos, (hall_gnd_pos[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(hall_gnd_pos[0], RAIL_GND)

    # =========================================================================
    # Section 10: Status LEDs
    # =========================================================================
    print("\n10. Adding status LEDs...")

    # Power LED
    led_pwr = LEDIndicator(
        sch,
        x=X_LDO + 50,
        y=120,
        ref_prefix="D3",
        label="PWR",
        resistor_value="1k",
    )
    led_pwr.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   Power LED: {led_pwr.led.reference}")

    # Status LED
    led_status = LEDIndicator(
        sch,
        x=X_LDO + 70,
        y=120,
        ref_prefix="D4",
        label="STATUS",
        resistor_value="1k",
    )
    led_status.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   Status LED: {led_status.led.reference}")

    # =========================================================================
    # Section 11: Design Notes
    # =========================================================================
    print("\n11. Adding design notes...")

    sch.add_text(
        "BLDC Motor Controller Design Notes:\n"
        "=====================================\n"
        "1. MOSFETs Q1-Q6 require thermal vias (min 6 per device)\n"
        "2. Use 2mm+ trace width for motor phase and power traces\n"
        "3. Ground plane on bottom layer for heat spreading\n"
        "4. Keep gate drive traces short (<10mm)\n"
        "5. Kelvin connection for current sense resistors\n"
        "6. Separate analog ground near current sense\n"
        "7. Bulk capacitors near MOSFETs for motor current\n",
        x=X_POWER_IN,
        y=RAIL_GND + 20,
    )

    # =========================================================================
    # Emit deferred U10 pin labels (issue #3379)
    # =========================================================================
    # Sections 5-10 added all the wires that could collide with U10's
    # right-side label row (PHASE_A/B/C from J2, vertical
    # HallSensorInput rails near x=227.33, bypass-cap rail stubs near
    # the +3.3V/GND rows). Now that those wires exist, the collision
    # check inside ``_connect_mcu_pin_to_label`` can see them and pick
    # a non-bridging endpoint for each MCU pin label.
    print("\n11b. Emitting deferred U10 pin labels (with collision avoidance)...")
    for pin_id, label_text, dx, dy in deferred_mcu_labels:
        _connect_mcu_pin_to_label(pin_id, label_text, dx=dx, dy=dy)
    print(f"   Emitted {len(deferred_mcu_labels)} U10 pin labels")

    # =========================================================================
    # Validate Schematic
    # =========================================================================
    print("\n12. Validating schematic...")

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
        print(f"   Found {len(warnings)} warnings")

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
    print("\n13. Writing output files...")

    output_dir.mkdir(parents=True, exist_ok=True)

    sch_path = output_dir / "bldc_controller.kicad_sch"
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


def create_bldc_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the BLDC motor controller.

    Generates a 60mm x 80mm board with all components from the schematic
    placed according to the project.kct layout suggestions:
    - MOSFETs in H-bridge near motor connector
    - Gate driver near MOSFETs
    - Current sense shunts between MOSFETs and ground
    - Power supply section separated from motor drive
    - Connectors at board edges

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating BLDC Motor Controller PCB...")
    print("=" * 60)

    # Board dimensions (mm) - from project.kct spec
    # Expanded from 60x80mm to 70x90mm to accommodate the STM32G431K8Tx MCU
    # (LQFP-32, 9x9mm body) which now lives between the bypass caps and
    # the gate driver.
    # Expanded again from 70x90mm to 80x100mm (Issue #3127 M-E Round 3) to
    # restore routing-channel headroom on both axes.  PR #3111 empirically
    # found that ``--placement-feedback`` rejected all 57 MOVE_COMPONENT
    # candidates as "unsafe (board bounds)" against the 70x90 envelope
    # (safety check at ``recovery/applicator.py:_position_within_bounds``);
    # the larger envelope opens those candidates and gives the router
    # additional channel width on the dense MOSFET / gate-driver columns.
    # 80x100mm keeps the long axis under the JLCPCB 100x100mm small-board
    # tier boundary so no manufacturing-tier change is incurred (+27% area).
    # Mounting holes / zones / component placement re-derive automatically
    # from the new corners via the BOARD_ORIGIN + offset arithmetic below.
    BOARD_WIDTH = 80.0
    BOARD_HEIGHT = 100.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Mounting hole positions (M3 at corners, 3mm inset)
    MH_INSET = 3.0
    MH_POSITIONS = [
        (BOARD_ORIGIN_X + MH_INSET, BOARD_ORIGIN_Y + MH_INSET),
        (BOARD_ORIGIN_X + BOARD_WIDTH - MH_INSET, BOARD_ORIGIN_Y + MH_INSET),
        (BOARD_ORIGIN_X + MH_INSET, BOARD_ORIGIN_Y + BOARD_HEIGHT - MH_INSET),
        (BOARD_ORIGIN_X + BOARD_WIDTH - MH_INSET, BOARD_ORIGIN_Y + BOARD_HEIGHT - MH_INSET),
    ]

    # Net definitions - must match schematic nets
    NETS = {
        "": 0,
        "+24V": 1,
        "+5V": 2,
        "+3V3": 3,
        "GND": 4,
        "PHASE_A": 5,
        "PHASE_B": 6,
        "PHASE_C": 7,
        "GATE_AH": 8,
        "GATE_AL": 9,
        "GATE_BH": 10,
        "GATE_BL": 11,
        "GATE_CH": 12,
        "GATE_CL": 13,
        "ISENSE_A+": 14,
        "ISENSE_A-": 15,
        "ISENSE_B+": 16,
        "ISENSE_B-": 17,
        "ISENSE_C+": 18,
        "ISENSE_C-": 19,
        "HALL_A": 20,
        "HALL_B": 21,
        "HALL_C": 22,
        "SWDIO": 23,
        "SWCLK": 24,
        "SWO": 25,
        "NRST": 26,
        "OSC_IN": 27,
        "OSC_OUT": 28,
        "PWR_LED": 29,
        "STATUS_LED": 30,
        "SW_OUT": 31,
        # Gate-driver-side (between DRV8301 outputs and the slew-rate resistors).
        # The MOSFET-gate-side nets remain GATE_AH/BH/CH.  Low-side gates stay
        # direct-driven so GATE_AL/BL/CL still tie the driver IC to the MOSFETs.
        "GATE_DRV_AH": 32,
        "GATE_DRV_BH": 33,
        "GATE_DRV_CH": 34,
        # MCU-side PWM logic inputs to the DRV8301 (pins 17-22).  These are
        # distinct from the GATE_* nets above, which are the MOSFET gates.
        "PWM_AH": 35,
        "PWM_AL": 36,
        "PWM_BH": 37,
        "PWM_BL": 38,
        "PWM_CH": 39,
        "PWM_CL": 40,
    }

    # =========================================================================
    # Component positions - organized by board section
    # Spacing increased to avoid pad overlap (min 4mm between components)
    # =========================================================================

    # Power input section (top-left, row 1)
    J1_POS = (BOARD_ORIGIN_X + 5, BOARD_ORIGIN_Y + 8)  # Power connector
    F1_POS = (BOARD_ORIGIN_X + 14, BOARD_ORIGIN_Y + 8)  # Fuse
    D1_POS = (BOARD_ORIGIN_X + 22, BOARD_ORIGIN_Y + 8)  # TVS diode
    C1_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 8)  # Bulk cap 470uF
    C2_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 14)  # Bulk cap 100nF

    # Buck converter (left, row 2)
    U1_POS = (BOARD_ORIGIN_X + 12, BOARD_ORIGIN_Y + 22)  # LM2596 (D2PAK)
    L1_POS = (BOARD_ORIGIN_X + 26, BOARD_ORIGIN_Y + 22)  # Inductor
    D2_POS = (BOARD_ORIGIN_X + 19, BOARD_ORIGIN_Y + 30)  # Schottky
    C3_POS = (BOARD_ORIGIN_X + 6, BOARD_ORIGIN_Y + 30)  # Buck input cap
    C4_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 30)  # Buck output cap

    # LDO (center-right, row 2) -- 10mm gap from buck section
    U2_POS = (BOARD_ORIGIN_X + 44, BOARD_ORIGIN_Y + 22)  # AMS1117
    C5_POS = (BOARD_ORIGIN_X + 38, BOARD_ORIGIN_Y + 30)  # LDO input cap
    C6_POS = (BOARD_ORIGIN_X + 50, BOARD_ORIGIN_Y + 30)  # LDO output cap

    # MCU bypass caps (center, row 3) -- 5mm spacing between caps
    C7_POS = (BOARD_ORIGIN_X + 34, BOARD_ORIGIN_Y + 37)
    C8_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 37)
    C9_POS = (BOARD_ORIGIN_X + 46, BOARD_ORIGIN_Y + 37)

    # Crystal (right side, row 3)
    Y1_POS = (BOARD_ORIGIN_X + 52, BOARD_ORIGIN_Y + 37)
    C10_POS = (BOARD_ORIGIN_X + 60, BOARD_ORIGIN_Y + 37)
    C11_POS = (BOARD_ORIGIN_X + 60, BOARD_ORIGIN_Y + 43)

    # Gate driver (left, row 4) -- DRV8301 HTSSOP-56 (DCA package), 14x8.1mm
    # body per TI SLOS719F.  Pin 1 is on the top-left of the long-axis-vertical
    # orientation, so the body extends ~7mm above and below U3_POS along Y and
    # ~4mm left/right along X (with leads).  Centred to clear the bypass caps
    # at x=4 and x=24 and the MCU at x=40.
    #
    # Note on routing density: the DCA package places half-bridge pins
    # (BST/GH/GL/SH/SL for A,B,C, pins 34-48) along the lower-right of the
    # device, while the H-bridge MOSFETs sit south at y=68/76.  Historically
    # (as of #2532 follow-up) the router achieved ~58-77% on this geometry
    # with the C++ negotiated backend at ``--timeout 240 --layers 2``; the
    # 2026-05-08 net-count growth (26 -> 35 after the block-refactor wave
    # added PWM_AH/AL/BH/BL/CH/CL + GATE_DRV_AH/BH/CH + R20-R22/R30-R32/
    # C30-C32) plus the per-net A* regression tracked in #2681 currently
    # reduce that completion to 6% under the same default flags.  Placement
    # has been re-tuned in issue #2682 (R20-R22 nudged north 2mm, Hall
    # filter R30-R32/C30-C32 shifted south 3mm) to clear known component-
    # courtyard overlaps and open fan-out corridors; the router-side fix
    # is tracked separately in #2681.
    U3_POS = (BOARD_ORIGIN_X + 14, BOARD_ORIGIN_Y + 50)  # DRV8301 HTSSOP-56
    C12_POS = (BOARD_ORIGIN_X + 4, BOARD_ORIGIN_Y + 47)  # Bootstrap A
    C13_POS = (BOARD_ORIGIN_X + 4, BOARD_ORIGIN_Y + 53)  # Bootstrap B
    C14_POS = (BOARD_ORIGIN_X + 4, BOARD_ORIGIN_Y + 59)  # Bootstrap C
    C15_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 47)  # Bypass 100nF
    C16_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 53)  # Bypass 10uF

    # MCU (right side, row 4) -- LQFP-32 7x7mm body, 9x9mm with leads.
    # Placed right of the gate driver, between the bypass caps row and the
    # MOSFET row.
    U10_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 50)

    # Power MOSFETs - H-bridge configuration (bottom section)
    # TO-220 pads are 2.54mm pitch, body ~5mm wide
    # Phase A (left)
    Q1_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 68)  # HS
    Q2_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 76)  # LS
    # Phase B (center)
    Q3_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 68)  # HS
    Q4_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 76)  # LS
    # Phase C (right)
    Q5_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 68)  # HS
    Q6_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 76)  # LS

    # Current sense shunts (below MOSFETs)
    R10_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 84)
    R11_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 84)
    R12_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 84)

    # Gate-drive (slew-rate) resistors -- between DRV8301 HS outputs and the
    # high-side MOSFET gates.  Sit just above each phase's HS MOSFET so the
    # GATE_DRV_*H -> GATE_*H net runs vertically.  R20-R22 use the same 0805
    # footprint generator as R3/R4.
    #
    # Iteration 3 (#2682): moved y=64 -> y=62 to clear the TO-220 Vertical
    # courtyard north edge.  Q1/Q3/Q5 use Package_TO_SOT_THT:TO-220-3_Vertical
    # whose F.CrtYd extends to y=-3.4 relative to the pin-2 origin, i.e., the
    # courtyard reaches up to (Q*_y - 3.4) = 164.6 absolute.  At the previous
    # y=64 (abs 164) the R20-R22 0805 body (1.3 mm tall) extended to y=164.65,
    # OVERLAPPING the TO-220 courtyard by ~0.05 mm.  Moving to y=62 leaves
    # the body north edge at y=162.65 with 1.95 mm clearance from the TO-220
    # courtyard, restoring routability for GATE_AH/BH/CH (previously
    # "No path found" because Q1/Q3/Q5 + R20-R22 footprints were touching).
    R20_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 62)  # Phase A HS
    R21_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 62)  # Phase B HS
    R22_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 62)  # Phase C HS

    # Motor connector (right edge, bottom -- near MOSFETs)
    J2_POS = (BOARD_ORIGIN_X + 65, BOARD_ORIGIN_Y + 76)

    # Hall sensor connector (right edge, middle)
    J3_POS = (BOARD_ORIGIN_X + 65, BOARD_ORIGIN_Y + 58)

    # Debug header (right edge, top)
    J4_POS = (BOARD_ORIGIN_X + 65, BOARD_ORIGIN_Y + 22)

    # LEDs (top-right corner)
    D3_POS = (BOARD_ORIGIN_X + 56, BOARD_ORIGIN_Y + 8)  # PWR LED
    R3_POS = (BOARD_ORIGIN_X + 56, BOARD_ORIGIN_Y + 13)  # PWR LED resistor
    D4_POS = (BOARD_ORIGIN_X + 62, BOARD_ORIGIN_Y + 8)  # STATUS LED
    R4_POS = (BOARD_ORIGIN_X + 62, BOARD_ORIGIN_Y + 13)  # STATUS LED resistor

    # Hall sensor filter network (next to J3, between MCU and connector).
    # Pull-ups (R30-R32) above filter caps (C30-C32).
    # Iteration 2: widen column pitch from 5mm to 6mm and shift the cluster
    # rightward by ~2mm so HALL_A/B/C traces from MCU bottom-edge pins
    # 11/12/13 (x=138.8/139.6/140.4, y=154.175) have room to fan out without
    # stacking three parallel traces against the MCU pad row.  J3 pads at
    # x=164.15..165.85 leave the rightmost cap (x=161) ~3mm clearance.
    #
    # Iteration 3 (#2682): shift the cluster SOUTH by 3mm (54/58 -> 57/61)
    # to clear the horizontal lane that HALL_A/B/C use to exit U10's bottom
    # edge.  At y=54 (abs 154) the R30-R32 0805 body (1.3mm tall, x=148-150)
    # sat in the same y=154 corridor that MCU bottom pins 11/12/13 launch
    # into (y=154.175).  The router had no clean east-west lane between
    # U10 and R30 -- traces had to thread between R30/C30 (4mm gap, but
    # blocked once R30 is placed).  Moving R30-R32 to y=57 (abs 157) leaves
    # body north edge at y=156.35 with 1.4mm clearance from U10's south
    # pad edge (y=154.925), opening a clean y=154-156 east-west corridor
    # for HALL_A/B/C and the SWDIO/SWCLK return traces.  C30-C32 follow at
    # y=61 (abs 161); body south edge at y=161.65 stays 1.4mm north of
    # J3's south pad (pin 5 GND at y=163.08).  The R30->C30 vertical
    # distance is preserved at 4mm so the existing HALL_A/B/C star-point
    # (pull-up + filter cap on the same column) keeps its compact layout.
    R30_POS = (BOARD_ORIGIN_X + 49, BOARD_ORIGIN_Y + 57)  # HALL_A pull-up
    C30_POS = (BOARD_ORIGIN_X + 49, BOARD_ORIGIN_Y + 61)  # HALL_A filter
    R31_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 57)  # HALL_B pull-up
    C31_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 61)  # HALL_B filter
    R32_POS = (BOARD_ORIGIN_X + 61, BOARD_ORIGIN_Y + 57)  # HALL_C pull-up
    C32_POS = (BOARD_ORIGIN_X + 61, BOARD_ORIGIN_Y + 61)  # HALL_C filter

    # =========================================================================
    # Footprint generators
    # =========================================================================

    def generate_header() -> str:
        """Generate the PCB file header."""
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
        """Generate net definitions."""
        lines = ['  (net 0 "")']
        for name, num in NETS.items():
            if num > 0:
                lines.append(f'  (net {num} "{name}")')
        return "\n".join(lines)

    def generate_board_outline() -> str:
        """Generate the board outline (Edge.Cuts) as a 60x80mm rectangle."""
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

    def generate_mounting_hole(pos: tuple, ref: str) -> str:
        """Generate M3 mounting hole footprint."""
        x, y = pos
        return f"""  (footprint "MountingHole:MountingHole_3.2mm_M3"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "MountingHole" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (attr exclude_from_pos_files exclude_from_bom)
    (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask"))
  )"""

    def generate_to220(
        ref: str, pos: tuple, value: str, gate_net: str, drain_net: str, source_net: str
    ) -> str:
        """Generate TO-220 footprint for power MOSFETs."""
        x, y = pos
        gate_num = NETS.get(gate_net, 0)
        drain_num = NETS.get(drain_net, 0)
        source_num = NETS.get(source_net, 0)
        return f"""  (footprint "Package_TO_SOT_THT:TO-220-3_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at -2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {gate_num} "{gate_net}"))
    (pad "2" thru_hole oval (at 0 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {drain_num} "{drain_net}"))
    (pad "3" thru_hole oval (at 2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {source_num} "{source_net}"))
  )"""

    def generate_sot223(
        ref: str, pos: tuple, value: str, pin1_net: str, pin2_net: str, pin3_net: str
    ) -> str:
        """Generate SOT-223 footprint for LDO."""
        x, y = pos
        net1 = NETS.get(pin1_net, 0)
        net2 = NETS.get(pin2_net, 0)
        net3 = NETS.get(pin3_net, 0)
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
    (pad "1" smd rect (at -3.15 2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {net1} "{pin1_net}"))
    (pad "2" smd rect (at -3.15 0) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {net2} "{pin2_net}"))
    (pad "3" smd rect (at -3.15 -2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {net3} "{pin3_net}"))
    (pad "2" smd rect (at 3.15 0) (size 2 3.8) (layers "F.Cu" "F.Paste" "F.Mask") (net {net2} "{pin2_net}"))
  )"""

    def generate_d2pak(ref: str, pos: tuple, value: str) -> str:
        """Generate D2PAK/TO-263 footprint for buck regulator."""
        x, y = pos
        return f"""  (footprint "Package_TO_SOT_SMD:TO-263-5_TabPin3"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -3.4 3.3) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+24V"]} "+24V"))
    (pad "2" smd rect (at -3.4 1.1) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+5V"]} "+5V"))
    (pad "3" smd rect (at -3.4 -1.1) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["SW_OUT"]} "SW_OUT"))
    (pad "4" smd rect (at -3.4 -3.3) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
    (pad "5" smd rect (at 3.4 0) (size 3 8) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
  )"""

    # DRV8301 HTSSOP-56 (DCA package) pinout from TI datasheet SLOS719F
    # (August 2011, revised January 2016).  The DRV8301 *only* ships in the
    # 56-pin HTSSOP package (14.00 mm x 8.10 mm body, 0.5 mm pitch).  The
    # KiCad library footprint used here is
    # ``Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm`` -- pin 1
    # is at the top-left, pins 1-28 run down the left edge, pins 29-56 run
    # up the right edge from the bottom, and pin 57 is the exposed PowerPAD.
    #
    # Pin -> net mapping (DRV8301 datasheet SLOS719F, page 3 "5 Pin
    # Configuration and Functions", and page 4-5 pin function table):
    #
    #   Buck regulator pins (1-7, 49-56):
    #     1  RT_CLK   (buck timing R/clock)        -> GND   (R to GND, DC tie)
    #     2  COMP     (buck error amp output)      -> GND   (cap to GND)
    #     3  VSENSE   (buck output FB)             -> +5V   (=buck output)
    #     4  PWRGD    (open-drain power-good)      -> +3.3V (pull-up)
    #     5  nOCTW    (open-drain over-current/T)  -> +3.3V (pull-up)
    #     6  nFAULT   (open-drain fault)           -> +3.3V (pull-up)
    #     7  DTC      (dead-time, R to GND)        -> GND
    #
    #   SPI / control / charge-pump / GVDD (8-16):
    #     8  nSCS     (SPI chip select)            -> +3.3V (idle high)
    #     9  SDI                                   -> +3.3V (idle high)
    #    10  SDO      (open-drain SPI output)      -> +3.3V (pull-up)
    #    11  SCLK                                  -> +3.3V
    #    12  DC_CAL                                -> GND   (normal operation)
    #    13  GVDD     (gate-driver internal LDO)   -> +5V   (cap to GND ext.)
    #    14  CP1      (charge pump cap 1)          -> +5V   (cap between CP1/CP2)
    #    15  CP2                                   -> +5V
    #    16  EN_GATE                               -> +3.3V (always-on)
    #
    #   PWM logic inputs (17-22):  driven by the MCU on PWM_* nets.
    #    17  INH_A                                 <- PWM_AH
    #    18  INL_A                                 <- PWM_AL
    #    19  INH_B                                 <- PWM_BH
    #    20  INL_B                                 <- PWM_BL
    #    21  INH_C                                 <- PWM_CH
    #    22  INL_C                                 <- PWM_CL
    #
    #   Analog supplies / current-sense amps (23-33):
    #    23  DVDD     (internal 3.3-V supply, cap) -> +3.3V
    #    24  REF      (current-sense reference)    -> +3.3V (=VDD/2 nominally)
    #    25  SO1      (current-sense amp 1 out)    -> ISENSE_A+ (tied to shunt+)
    #    26  SO2      (current-sense amp 2 out)    -> ISENSE_B+
    #    27  AVDD     (internal 6-V supply, cap)   -> +5V
    #    28  AGND                                  -> GND
    #    29  PVDD1    (gate-driver/SPI supply)     -> VMOTOR
    #    30  SP2      (amp 2 + input)              -> ISENSE_B+
    #    31  SN2      (amp 2 - input)              -> ISENSE_B-
    #    32  SP1      (amp 1 + input)              -> ISENSE_A+
    #    33  SN1      (amp 1 - input)              -> ISENSE_A-
    #
    #   Half-bridge C (34-38):
    #    34  SL_C     (low-side source / VDS-)     -> ISENSE_C-
    #    35  GL_C                                  -> GATE_CL
    #    36  SH_C                                  -> PHASE_C
    #    37  GH_C                                  -> GATE_DRV_CH (via R22 to GATE_CH)
    #    38  BST_C    (high-side bootstrap)        -> VMOTOR (via cap, DC tie)
    #
    #   Half-bridge B (39-43):
    #    39  SL_B                                  -> ISENSE_B-
    #    40  GL_B                                  -> GATE_BL
    #    41  SH_B                                  -> PHASE_B
    #    42  GH_B                                  -> GATE_DRV_BH (via R21 to GATE_BH)
    #    43  BST_B                                 -> VMOTOR
    #
    #   Half-bridge A (44-48):
    #    44  SL_A                                  -> ISENSE_A-
    #    45  GL_A                                  -> GATE_AL
    #    46  SH_A                                  -> PHASE_A
    #    47  GH_A                                  -> GATE_DRV_AH (via R20 to GATE_AH)
    #    48  BST_A                                 -> VMOTOR
    #
    #   SPI / buck pins (49-57):
    #    49  VDD_SPI  (SPI logic supply)           -> +3.3V
    #    50  PH       (buck high-side source)      -> SW_OUT
    #    51  PH                                    -> SW_OUT (same node)
    #    52  BST_BK   (buck bootstrap)             -> VMOTOR (via cap, DC tie)
    #    53  PVDD2    (buck supply)                -> VMOTOR
    #    54  PVDD2                                 -> VMOTOR
    #    55  EN_BUCK                               -> +3.3V (always-on)
    #    56  SS_TR    (buck soft-start)            -> GND   (cap to GND)
    #    57  PowerPAD (GND)                        -> GND
    #
    # The mapping above ties any pin that would otherwise float (mode/SPI/
    # open-drain reporting pins) to a power rail so the net has at least two
    # endpoints (single_pad_net is satisfied).  This also matches realistic
    # use of the part: open-drain outputs need a pull-up, mode pins are
    # strapped, and the buck regulator needs its enable / soft-start / sense
    # pins biased.  Fully-functional firmware would drive the SPI and PWM
    # inputs from the MCU at runtime; for a generated demo board the static
    # tie-up gives the autorouter sensible electrical endpoints.
    DRV8301_PINS: list[tuple[str, str]] = [
        # Pin, net               # Datasheet name (function)
        ("1", "GND"),  # RT_CLK   (buck timing R)
        ("2", "GND"),  # COMP     (buck error-amp output)
        ("3", "+5V"),  # VSENSE   (buck output FB = +5V rail)
        ("4", "+3V3"),  # PWRGD    (open-drain, pull-up)
        ("5", "+3V3"),  # nOCTW    (open-drain, pull-up)
        ("6", "+3V3"),  # nFAULT   (open-drain, pull-up)
        ("7", "GND"),  # DTC      (R to GND, programmable)
        ("8", "+3V3"),  # nSCS     (idle high)
        ("9", "+3V3"),  # SDI
        ("10", "+3V3"),  # SDO      (open-drain, pull-up)
        ("11", "+3V3"),  # SCLK
        ("12", "GND"),  # DC_CAL   (normal operation)
        ("13", "+5V"),  # GVDD     (gate-driver LDO, cap to GND)
        ("14", "+5V"),  # CP1      (charge pump cap 1)
        ("15", "+5V"),  # CP2      (charge pump cap 2)
        ("16", "+3V3"),  # EN_GATE  (always-on)
        ("17", "PWM_AH"),  # INH_A    (PWM input from MCU)
        ("18", "PWM_AL"),  # INL_A
        ("19", "PWM_BH"),  # INH_B
        ("20", "PWM_BL"),  # INL_B
        ("21", "PWM_CH"),  # INH_C
        ("22", "PWM_CL"),  # INL_C
        ("23", "+3V3"),  # DVDD     (internal 3.3-V LDO output)
        ("24", "+3V3"),  # REF      (current-sense reference)
        ("25", "ISENSE_A+"),  # SO1      (op-amp 1 output)
        ("26", "ISENSE_B+"),  # SO2      (op-amp 2 output)
        ("27", "+5V"),  # AVDD     (internal 6-V LDO output)
        ("28", "GND"),  # AGND
        ("29", "+24V"),  # PVDD1    (gate-driver supply)
        ("30", "ISENSE_B+"),  # SP2      (amp 2 + input)
        ("31", "ISENSE_B-"),  # SN2      (amp 2 - input)
        ("32", "ISENSE_A+"),  # SP1      (amp 1 + input)
        ("33", "ISENSE_A-"),  # SN1      (amp 1 - input)
        ("34", "ISENSE_C-"),  # SL_C     (low-side source, half-bridge C)
        ("35", "GATE_CL"),  # GL_C
        ("36", "PHASE_C"),  # SH_C
        ("37", "GATE_DRV_CH"),  # GH_C     (via R22 to GATE_CH)
        ("38", "+24V"),  # BST_C    (bootstrap, via cap)
        ("39", "ISENSE_B-"),  # SL_B
        ("40", "GATE_BL"),  # GL_B
        ("41", "PHASE_B"),  # SH_B
        ("42", "GATE_DRV_BH"),  # GH_B     (via R21 to GATE_BH)
        ("43", "+24V"),  # BST_B
        ("44", "ISENSE_A-"),  # SL_A
        ("45", "GATE_AL"),  # GL_A
        ("46", "PHASE_A"),  # SH_A
        ("47", "GATE_DRV_AH"),  # GH_A     (via R20 to GATE_AH)
        ("48", "+24V"),  # BST_A
        ("49", "+3V3"),  # VDD_SPI  (SPI logic supply)
        ("50", "SW_OUT"),  # PH       (buck switch node)
        ("51", "SW_OUT"),  # PH       (buck switch node, second pin)
        ("52", "+24V"),  # BST_BK   (buck bootstrap, via cap)
        ("53", "+24V"),  # PVDD2    (buck input supply)
        ("54", "+24V"),  # PVDD2    (buck input supply, 2nd pin)
        ("55", "+3V3"),  # EN_BUCK  (always-on)
        ("56", "GND"),  # SS_TR    (cap to GND)
    ]

    def _htssop56_pad_xy(pin_index: int) -> tuple[float, float, float, float]:
        """Return (x, y, size_x, size_y) for the given HTSSOP-56 pin (1-56).

        Layout matches the KiCad library footprint
        ``Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm``:
        long axis vertical, pin 1 at the top-left, pins 1-28 down the left
        edge (top to bottom), pins 29-56 up the right edge (bottom to top),
        0.5 mm pitch, pad geometry 1.55 mm wide (perpendicular to body) by
        0.30 mm tall (parallel to body).  Pad centre offsets are +/-3.75 mm
        in X with Y stepping by 0.5 mm from +/-6.75 mm.
        """
        if 1 <= pin_index <= 28:
            # Left edge, pin 1 at top (-6.75) -> pin 28 at bottom (+6.75)
            return (-3.75, -6.75 + (pin_index - 1) * 0.5, 1.55, 0.3)
        if 29 <= pin_index <= 56:
            # Right edge, pin 29 at bottom (+6.75) -> pin 56 at top (-6.75)
            return (3.75, 6.75 - (pin_index - 29) * 0.5, 1.55, 0.3)
        raise ValueError(f"HTSSOP-56 pin {pin_index} out of range")

    def generate_htssop56(ref: str, pos: tuple, value: str) -> str:
        """Generate the complete HTSSOP-56 footprint for the DRV8301 gate
        driver.

        Emits all 56 perimeter pads plus the exposed PowerPAD (pin 57).
        Net assignments come from ``DRV8301_PINS`` above; pin 57 is GND.
        Footprint matches TI's DCA package per SLOS719F.
        """
        x, y = pos
        pad_lines = []
        for pin_str, net_name in DRV8301_PINS:
            pin_idx = int(pin_str)
            px, py, sx, sy = _htssop56_pad_xy(pin_idx)
            net_num = NETS.get(net_name, 0)
            pad_lines.append(
                f'    (pad "{pin_str}" smd roundrect '
                f"(at {px:.4f} {py:.4f}) (size {sx} {sy}) "
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
                f'(net {net_num} "{net_name}"))'
            )
        # PowerPAD / exposed pad (pin 57 == GND).  EP geometry per KiCad
        # library footprint: 3.61 mm x 6.35 mm centred on the package.
        pad_lines.append(
            f'    (pad "57" smd rect (at 0 0) (size 3.61 6.35) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") '
            f'(net {NETS["GND"]} "GND"))'
        )
        pads = "\n".join(pad_lines)
        return f"""  (footprint "Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -8) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 8) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads}
  )"""

    # STM32G431K8Tx LQFP-32 pin -> net mapping.  Pin-to-port assignments come
    # from the steering decision in #2529 and the STM32G431 datasheet:
    #   * TIM1_CH1/2/3 (PA8/PA9/PA10)  -> PWM_AH/BH/CH (DRV8301 INH_A/B/C)
    #   * TIM4_CH1/2/3 (PB6/PB7/PB8)   -> PWM_AL/BL/CL (DRV8301 INL_A/B/C)
    #   * ADC1_IN1..IN3 (PA0/PA1/PA2)  -> ISENSE_A-/B-/C-
    #   * GPIO/TIM3 capture (PA6/PA7/PB0) -> HALL_A/B/C
    #   * SWD: PA13 SWDIO, PA14 SWCLK, PB3 SWO, PG10 NRST
    STM32G431K8_PINS: list[tuple[str, str]] = [
        ("1", "+3V3"),  # VDD
        ("2", "OSC_IN"),  # PF0 -> RCC_OSC_IN
        ("3", "OSC_OUT"),  # PF1 -> RCC_OSC_OUT
        ("4", "NRST"),  # PG10 (NRST)
        ("5", "ISENSE_A-"),  # PA0  ADC1_IN1
        ("6", "ISENSE_B-"),  # PA1  ADC1_IN2
        ("7", "ISENSE_C-"),  # PA2  ADC1_IN3
        ("8", "GND"),  # PA3 (unused -> GND for autorouter)
        ("9", "GND"),  # PA4 (unused)
        ("10", "GND"),  # PA5 (unused)
        ("11", "HALL_A"),  # PA6  TIM3_CH1
        ("12", "HALL_B"),  # PA7  TIM3_CH2
        ("13", "HALL_C"),  # PB0  TIM3_CH3
        ("14", "GND"),  # VSSA
        ("15", "+3V3"),  # VDDA
        ("16", "GND"),  # VSS
        ("17", "+3V3"),  # VDD
        ("18", "PWM_AH"),  # PA8  TIM1_CH1   (HS PWM -> DRV8301 INH_A)
        ("19", "PWM_BH"),  # PA9  TIM1_CH2   (HS PWM -> DRV8301 INH_B)
        ("20", "PWM_CH"),  # PA10 TIM1_CH3   (HS PWM -> DRV8301 INH_C)
        ("21", "GND"),  # PA11 (unused)
        ("22", "GND"),  # PA12 (unused)
        ("23", "SWDIO"),  # PA13
        ("24", "SWCLK"),  # PA14
        ("25", "GND"),  # PA15 (unused)
        ("26", "SWO"),  # PB3 (SWO/TIM2_CH2)
        ("27", "GND"),  # PB4 (unused)
        ("28", "GND"),  # PB5 (unused)
        ("29", "PWM_AL"),  # PB6  TIM4_CH1   (LS PWM -> DRV8301 INL_A)
        ("30", "PWM_BL"),  # PB7  TIM4_CH2   (LS PWM -> DRV8301 INL_B)
        ("31", "PWM_CL"),  # PB8  TIM4_CH3   (LS PWM -> DRV8301 INL_C)
        ("32", "GND"),  # VSS
    ]

    def _lqfp32_pad_xy(pin_index: int) -> tuple[float, float, float, float]:
        """Return (x, y, size_x, size_y) for the given LQFP-32 pin (1-32).

        Layout follows the KiCad ``LQFP-32_7x7mm_P0.8mm`` footprint: 8 pads
        per side, 0.8mm pitch, pin-1 at the top-left of the left edge.  Pads
        on the left/right edges are 1.5 wide x 0.5 tall; pads on the top/
        bottom edges are 0.5 wide x 1.5 tall.
        """
        if 1 <= pin_index <= 8:  # left edge, top->bottom
            return (-4.175, -2.8 + (pin_index - 1) * 0.8, 1.5, 0.5)
        if 9 <= pin_index <= 16:  # bottom edge, left->right
            return (-2.8 + (pin_index - 9) * 0.8, 4.175, 0.5, 1.5)
        if 17 <= pin_index <= 24:  # right edge, bottom->top
            return (4.175, 2.8 - (pin_index - 17) * 0.8, 1.5, 0.5)
        if 25 <= pin_index <= 32:  # top edge, right->left
            return (2.8 - (pin_index - 25) * 0.8, -4.175, 0.5, 1.5)
        raise ValueError(f"LQFP-32 pin {pin_index} out of range")

    def generate_lqfp32(ref: str, pos: tuple, value: str) -> str:
        """Generate the LQFP-32 footprint for the STM32G431K8Tx MCU.

        Emits all 32 perimeter pads with nets matching ``STM32G431K8_PINS``.
        """
        x, y = pos
        pad_lines = []
        for pin_str, net_name in STM32G431K8_PINS:
            pin_idx = int(pin_str)
            px, py, sx, sy = _lqfp32_pad_xy(pin_idx)
            net_num = NETS.get(net_name, 0)
            pad_lines.append(
                f'    (pad "{pin_str}" smd roundrect '
                f"(at {px:.4f} {py:.4f}) (size {sx} {sy}) "
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
                f'(net {net_num} "{net_name}"))'
            )
        pads = "\n".join(pad_lines)
        return f"""  (footprint "Package_QFP:LQFP-32_7x7mm_P0.8mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads}
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

    def generate_resistor_2512(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate 2512 resistor footprint for current sense shunts."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Resistor_SMD:R_2512_6332Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -3.1 0) (size 1.6 2.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 3.1 0) (size 1.6 2.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
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

    def generate_led_0805(ref: str, pos: tuple, value: str, net_a: str, net_k: str) -> str:
        """Generate 0805 LED footprint.

        The ``value`` argument is emitted as the ``fp_text value`` field
        so the PCB silkscreen matches the schematic-side semantic label
        (e.g. ``"PWR"`` / ``"STATUS"``).  Hardcoding ``"LED"`` here
        previously caused a schematic<->PCB value drift on every fresh
        build — see issue #3210.
        """
        x, y = pos
        net_a_num = NETS.get(net_a, 0)
        net_k_num = NETS.get(net_k, 0)
        return f"""  (footprint "LED_SMD:LED_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_a_num} "{net_a}"))
    (pad "2" smd roundrect (at 1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_k_num} "{net_k}"))
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

    def generate_inductor_smd(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate SMD inductor footprint."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Inductor_SMD:L_1210_3225Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.5 0) (size 1.2 2.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 1.5 0) (size 1.2 2.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_diode_sma(ref: str, pos: tuple, value: str, net_a: str, net_k: str) -> str:
        """Generate SMA diode footprint (for Schottky and TVS)."""
        x, y = pos
        net_a_num = NETS.get(net_a, 0)
        net_k_num = NETS.get(net_k, 0)
        return f"""  (footprint "Diode_SMD:D_SMA"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -2 0) (size 1.5 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_k_num} "{net_k}"))
    (pad "2" smd roundrect (at 2 0) (size 1.5 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_a_num} "{net_a}"))
  )"""

    def generate_fuse_holder(ref: str, pos: tuple, value: str) -> str:
        """Generate fuse holder footprint."""
        x, y = pos
        return f"""  (footprint "Fuse:Fuse_1206_3216Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.5 0) (size 1.2 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {NETS["+24V"]} "+24V"))
    (pad "2" smd roundrect (at 1.5 0) (size 1.2 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {NETS["+24V"]} "+24V"))
  )"""

    def generate_pin_header(ref: str, pos: tuple, pins: int, value: str, pin_nets: list) -> str:
        """Generate N-pin header footprint."""
        x, y = pos
        pitch = 2.54
        pad_lines = []
        for i in range(pins):
            pin_num = i + 1
            net_name = pin_nets[i] if i < len(pin_nets) else ""
            net_num = NETS.get(net_name, 0)
            y_offset = (i - (pins - 1) / 2.0) * pitch
            pad_type = "rect" if i == 0 else "oval"
            pad_lines.append(
                f'    (pad "{pin_num}" thru_hole {pad_type} (at 0 {y_offset:.2f}) '
                f'(size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") '
                f'(net {net_num} "{net_name}"))'
            )
        pads = "\n".join(pad_lines)
        return f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x{pins:02d}_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 {-(pins / 2.0) * pitch - 1:.1f}) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 {(pins / 2.0) * pitch + 1:.1f}) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads}
  )"""

    # =========================================================================
    # Build PCB
    # =========================================================================
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    print("\n1. Adding mounting holes...")
    for i, mh_pos in enumerate(MH_POSITIONS):
        parts.append(generate_mounting_hole(mh_pos, f"MH{i + 1}"))
    print(f"   4x M3 mounting holes at corners")

    print("\n2. Adding power input section...")
    # J1: Power input connector (2-pin)
    parts.append(generate_pin_header("J1", J1_POS, 2, "Power Input", ["+24V", "GND"]))
    print(f"   J1 (Power Input) at {J1_POS}")
    # F1: Fuse
    parts.append(generate_fuse_holder("F1", F1_POS, "15A"))
    print(f"   F1 (15A Fuse) at {F1_POS}")
    # D1: TVS diode
    parts.append(generate_diode_sma("D1", D1_POS, "SMBJ24A", "+24V", "GND"))
    print(f"   D1 (TVS) at {D1_POS}")
    # C1, C2: Bulk caps
    parts.append(generate_cap_0805("C1", C1_POS, "470uF", "+24V", "GND"))
    parts.append(generate_cap_0805("C2", C2_POS, "100nF", "+24V", "GND"))
    print(f"   C1, C2 (bulk caps) at {C1_POS}, {C2_POS}")

    print("\n3. Adding buck converter section...")
    # U1: LM2596 buck regulator
    parts.append(generate_d2pak("U1", U1_POS, "LM2596-5.0"))
    print(f"   U1 (LM2596) at {U1_POS}")
    # L1: Inductor
    parts.append(generate_inductor_smd("L1", L1_POS, "33uH", "SW_OUT", "+5V"))
    print(f"   L1 (33uH) at {L1_POS}")
    # D2: Schottky diode
    parts.append(generate_diode_sma("D2", D2_POS, "SS34", "GND", "SW_OUT"))
    print(f"   D2 (SS34) at {D2_POS}")
    # C3, C4: Buck caps
    parts.append(generate_cap_0805("C3", C3_POS, "220uF", "+24V", "GND"))
    parts.append(generate_cap_0805("C4", C4_POS, "220uF", "+5V", "GND"))
    print(f"   C3, C4 (buck caps) at {C3_POS}, {C4_POS}")

    print("\n4. Adding LDO section...")
    # U2: AMS1117-3.3
    parts.append(generate_sot223("U2", U2_POS, "AMS1117-3.3", "+5V", "GND", "+3V3"))
    print(f"   U2 (AMS1117) at {U2_POS}")
    # C5, C6: LDO caps
    parts.append(generate_cap_0805("C5", C5_POS, "10uF", "+5V", "GND"))
    parts.append(generate_cap_0805("C6", C6_POS, "10uF", "+3V3", "GND"))
    print(f"   C5, C6 (LDO caps) at {C5_POS}, {C6_POS}")

    print("\n5. Adding MCU bypass caps...")
    parts.append(generate_cap_0805("C7", C7_POS, "100nF", "+3V3", "GND"))
    parts.append(generate_cap_0805("C8", C8_POS, "100nF", "+3V3", "GND"))
    parts.append(generate_cap_0805("C9", C9_POS, "4.7uF", "+3V3", "GND"))
    print(f"   C7, C8, C9 at {C7_POS}, {C8_POS}, {C9_POS}")

    print("\n6. Adding crystal...")
    parts.append(generate_crystal_hc49("Y1", Y1_POS, "8MHz"))
    parts.append(generate_cap_0805("C10", C10_POS, "20pF", "OSC_IN", "GND"))
    parts.append(generate_cap_0805("C11", C11_POS, "20pF", "OSC_OUT", "GND"))
    print(f"   Y1 at {Y1_POS}, C10-C11 at {C10_POS}, {C11_POS}")

    print("\n7. Adding gate driver...")
    parts.append(generate_htssop56("U3", U3_POS, "DRV8301"))
    print(f"   U3 (DRV8301, HTSSOP-56) at {U3_POS}")
    # Bootstrap caps (VMOTOR to phase)
    parts.append(generate_cap_0805("C12", C12_POS, "100nF", "+24V", "PHASE_A"))
    parts.append(generate_cap_0805("C13", C13_POS, "100nF", "+24V", "PHASE_B"))
    parts.append(generate_cap_0805("C14", C14_POS, "100nF", "+24V", "PHASE_C"))
    # Bypass caps
    parts.append(generate_cap_0805("C15", C15_POS, "100nF", "+5V", "GND"))
    parts.append(generate_cap_0805("C16", C16_POS, "10uF", "+5V", "GND"))
    print(f"   C12-C14 (bootstrap), C15-C16 (bypass)")

    print("\n7b. Adding MCU (STM32G431K8Tx)...")
    parts.append(generate_lqfp32("U10", U10_POS, "STM32G431K8Tx"))
    print(f"   U10 (STM32G431K8Tx, LQFP-32) at {U10_POS}")

    print("\n8. Adding power MOSFETs (H-bridge)...")
    # Phase A: Q1 (high-side), Q2 (low-side)
    parts.append(generate_to220("Q1", Q1_POS, "IRLZ44N", "GATE_AH", "+24V", "PHASE_A"))
    parts.append(generate_to220("Q2", Q2_POS, "IRLZ44N", "GATE_AL", "PHASE_A", "ISENSE_A+"))
    # Phase B: Q3 (high-side), Q4 (low-side)
    parts.append(generate_to220("Q3", Q3_POS, "IRLZ44N", "GATE_BH", "+24V", "PHASE_B"))
    parts.append(generate_to220("Q4", Q4_POS, "IRLZ44N", "GATE_BL", "PHASE_B", "ISENSE_B+"))
    # Phase C: Q5 (high-side), Q6 (low-side)
    parts.append(generate_to220("Q5", Q5_POS, "IRLZ44N", "GATE_CH", "+24V", "PHASE_C"))
    parts.append(generate_to220("Q6", Q6_POS, "IRLZ44N", "GATE_CL", "PHASE_C", "ISENSE_C+"))
    print(f"   Q1-Q2 (Phase A), Q3-Q4 (Phase B), Q5-Q6 (Phase C)")

    print("\n9. Adding current sense shunts...")
    parts.append(generate_resistor_2512("R10", R10_POS, "5mR", "ISENSE_A+", "ISENSE_A-"))
    parts.append(generate_resistor_2512("R11", R11_POS, "5mR", "ISENSE_B+", "ISENSE_B-"))
    parts.append(generate_resistor_2512("R12", R12_POS, "5mR", "ISENSE_C+", "ISENSE_C-"))
    print(f"   R10, R11, R12 (5mOhm shunts)")

    print("\n9b. Adding gate-drive (slew-rate) resistors...")
    # Series 22-ohm resistors between DRV8301 HS outputs and the MOSFET gates.
    # Each connects GATE_DRV_*H (driver IC output) to GATE_*H (MOSFET gate).
    parts.append(generate_resistor_0805("R20", R20_POS, "22", "GATE_DRV_AH", "GATE_AH"))
    parts.append(generate_resistor_0805("R21", R21_POS, "22", "GATE_DRV_BH", "GATE_BH"))
    parts.append(generate_resistor_0805("R22", R22_POS, "22", "GATE_DRV_CH", "GATE_CH"))
    print("   R20, R21, R22 (22 ohm gate-drive, HS only)")

    print("\n10. Adding connectors...")
    # J2: Motor output (3-pin)
    parts.append(
        generate_pin_header("J2", J2_POS, 3, "Motor Output", ["PHASE_A", "PHASE_B", "PHASE_C"])
    )
    print(f"   J2 (Motor Output) at {J2_POS}")
    # J3: Hall sensors (5-pin)
    parts.append(
        generate_pin_header(
            "J3", J3_POS, 5, "Hall Sensors", ["HALL_A", "HALL_B", "HALL_C", "+3V3", "GND"]
        )
    )
    print(f"   J3 (Hall Sensors) at {J3_POS}")
    # J4: Debug header (6-pin SWD).  The schematic-side ``DebugHeader``
    # block emits its value as ``f"SWD-{self.pins}"`` (i.e. ``"SWD-6"``);
    # match that here so the schematic<->PCB value field stays in sync.
    # Issue #3210.
    parts.append(
        generate_pin_header(
            "J4", J4_POS, 6, "SWD-6", ["+3V3", "SWDIO", "SWCLK", "SWO", "NRST", "GND"]
        )
    )
    print(f"   J4 (SWD-6) at {J4_POS}")

    print("\n11. Adding LEDs...")
    parts.append(generate_resistor_0805("R3", R3_POS, "1k", "+3V3", "PWR_LED"))
    parts.append(generate_led_0805("D3", D3_POS, "PWR", "PWR_LED", "GND"))
    parts.append(generate_resistor_0805("R4", R4_POS, "1k", "+3V3", "STATUS_LED"))
    parts.append(generate_led_0805("D4", D4_POS, "STATUS", "STATUS_LED", "GND"))
    print(f"   D3 (PWR), D4 (STATUS) with resistors R3, R4")

    print("\n11b. Adding Hall sensor filter network...")
    # Pull-up resistors: +3.3V to each HALL_x signal
    parts.append(generate_resistor_0805("R30", R30_POS, "10k", "+3V3", "HALL_A"))
    parts.append(generate_resistor_0805("R31", R31_POS, "10k", "+3V3", "HALL_B"))
    parts.append(generate_resistor_0805("R32", R32_POS, "10k", "+3V3", "HALL_C"))
    # Filter caps: each HALL_x signal to GND
    parts.append(generate_cap_0805("C30", C30_POS, "10nF", "HALL_A", "GND"))
    parts.append(generate_cap_0805("C31", C31_POS, "10nF", "HALL_B", "GND"))
    parts.append(generate_cap_0805("C32", C32_POS, "10nF", "HALL_C", "GND"))
    print("   R30-R32 (10k pull-ups), C30-C32 (10nF filters)")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n12. Writing PCB file...")
    output_dir.mkdir(parents=True, exist_ok=True)
    pcb_path = output_dir / "bldc_controller.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print(f"   Mounting holes: 4x M3")
    component_count = (
        4  # mounting holes
        + 6  # MOSFETs
        + 4  # ICs (U1 buck, U2 LDO, U3 DRV8301, U10 STM32G431K8Tx)
        + 16  # capacitors (C1-C16)
        + 8  # resistors (R3, R4, R10-R12, R20-R22)
        + 4  # diodes (D1-D4)
        + 1  # inductor
        + 1  # fuse
        + 1  # crystal
        + 3  # connectors (J1-J3)
        + 1  # debug header (J4)
    )
    print(f"   Components: {component_count}")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])}")

    return pcb_path


def create_zones_for_pcb(pcb_path: Path) -> int:
    """Create copper-pour zones for power and ground nets on *pcb_path*.

    Issue #2899: board 05's committed routed PCB carried 0 zones because
    this script's pipeline jumps straight from PCB generation to routing
    without ever invoking the zone generator.  ``kct build``'s own
    ``_run_step_zones`` step never fires either, because ``design.py`` is
    detected as both the schematic and PCB generator -- it runs in the
    SCHEMATIC step and the build pipeline either short-circuits the
    later ZONES step (when this script exited 0) or aborts before
    reaching it (when this script exits 1 on ERC/DRC failures).
    Either way, zones never land in the on-disk PCB.

    Adding the zones here -- after the unrouted PCB is written and
    before the router consumes it -- makes the design self-contained:
    the router preserves zones via its raw-text concatenation (the
    write path fixed in #2770), so the committed routed PCB now ships
    with VMOTOR / +5V / +3.3V / GND / PWR_LED zones regardless of
    whether ``kct build`` runs the ZONES step or not.

    The implementation reuses ``auto_pour_if_missing`` (the same helper
    that ``kct route`` invokes) so layer assignment, priority handling,
    edge-clearance inset, and the all-power-board guard all match the
    rest of the toolchain.  The JLCPCB minimum-edge-clearance is
    sourced from ``mfr_limits`` so zone copper does not extend to the
    board edge and trigger ``edge_clearance_zone`` DRC violations.

    Args:
        pcb_path: Path to the unrouted .kicad_pcb file.  Modified in place.

    Returns:
        Number of zones created (0 if all power nets already have zones,
        or if the board classifies as all-power per issue #2740).
    """
    from kicad_tools.router.auto_pour import auto_pour_if_missing
    from kicad_tools.router.mfr_limits import get_mfr_limits

    print("\n" + "=" * 60)
    print("Creating copper-pour zones...")
    print("=" * 60)

    # Look up edge clearance from the JLCPCB profile so zone copper does
    # not bleed to the board edge.  Mirrors the lookup in
    # build_cmd._run_step_zones so both code paths use identical insets.
    edge_clearance: float | None = None
    try:
        _limits = get_mfr_limits("jlcpcb")
        if _limits.min_edge_clearance > 0:
            edge_clearance = _limits.min_edge_clearance
    except ValueError:
        pass  # Unknown manufacturer -- proceed without inset

    print(f"\n1. Loading PCB: {pcb_path}")
    if edge_clearance is not None:
        print(f"   Edge clearance inset: {edge_clearance}mm (from jlcpcb profile)")

    zones_created, pour_net_names = auto_pour_if_missing(
        pcb_path,
        quiet=False,
        edge_clearance=edge_clearance,
    )

    print("\n2. Result:")
    if zones_created > 0:
        print(f"   Created {zones_created} zone(s) for: {', '.join(pour_net_names)}")
    else:
        print("   No new zones created (zones already exist or board is all-power)")

    return zones_created


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """
    Route the PCB by invoking the ``kct route`` CLI with the proven flag recipe.

    Returns True if the routed file was produced (even partially).

    Issue #2975: Previously this function called
    ``router.route_all_negotiated()`` directly through the in-process API,
    which bypassed the CLI's flag stack and gave the default routing
    profile.

    Issue #3096 (M-E):  Extends the timeout from 240 s to 360 s so the
    negotiator's rip-up iteration has room to land additional nets on
    the larger signal-net pool (32 nets after the 2026-05-08 PWM/
    gate-drive additions).

    Issue #3111 (M-E follow-up): builder measurements after PR #3108 and
    PR #3110 (per-pair budget) merged showed the Python negotiator was
    consistently producing 21/32 fully-routed nets at iteration 2 (best
    state with overflow=28) but the 360s wall-clock budget was being
    consumed by the rip-up loop *and* the post-route DRC-fix pass, so
    the ``--auto-fix`` step was being skipped with "deadline reached"
    (issue #2802) and the run ended with stale 26+ connectivity errors.
    Three changes to recover the headroom:

    1. Bump ``--timeout`` 360 -> 900 s.  Empirically iter 0 reaches
       21/32 routed in ~120 s; iter 2 (best state) lands at ~250 s; the
       early-stop-patience=4 lets iter 3-6 explore further before the
       Restoring iteration-N best-state restore (Issue #3101) kicks
       in.  Leaving 600+ s for negotiator iterations + the
       Optimizing-traces nudge + the post-route fix-drc pass.
    2. Add ``--early-stop-patience 4`` (default 2) so the negotiator
       gets more rip-up attempts before bailing out.  Empirically the
       iter-1 metric usually regresses (overflow=42 vs iter-0 30) and
       iter-2 recovers to a new best (overflow=28); waiting for stall=4
       instead of stall=2 lets iter 5-6 attempt routing more
       difficult-to-route nets (SWCLK/SWDIO/NRST particularly hard).
    3. Add ``--auto-fix --auto-fix-passes 3`` so ``kct fix-drc`` runs
       *after* the negotiator finishes, with the remaining time budget
       on its own deadline.  The Optimizing-traces nudge alone resolves
       only 9/45 clearance violations; the standalone fix-drc pass on
       the post-route geometry was empirically destroying connectivity
       in PR #3108, so it stays scoped to clearance fixes only via
       ``--auto-fix-passes 3`` (the default).

    NOTE: ``--placement-feedback`` was evaluated and rejected.  On
    board-05's already-cramped 70x90 mm layout, the feedback loop
    rejected all 57 MOVE_COMPONENT candidates as "unsafe (board
    bounds)" and didn't move any components, but it DID rerun the
    full negotiator at iteration 0 which destroyed the iter-2 best
    state.  Net effect: 21/32 -> 12/32 (regression).  Skip it.

    What each flag does:

    - ``--no-auto-layers --layers 2``: pin a 2-layer stackup so the
      negotiator gets the full timeout budget on the right stack.  The
      default auto-layers loop escalates 2->4->6 and throws away the
      partial 2L result; pinning avoids that thrash.  Empirically a
      forced 4-layer route on this board UNDER-performed 2L (#3096
      builder measurement: 14/32 vs 18/32 in iteration 0) because the
      negotiator's layer-preference matrix saturates the wider stack
      with conflicting net routes that don't survive the recovery pass.
    - ``--manufacturer jlcpcb``: triggers the jlcpcb design-rule profile.
    - ``--differential-pairs``: enables the ISENSE_* matched-impedance
      pair handling.
    - **Backend selection (no explicit pin)**: the Issue #3096 ``--backend
      python`` pin was removed as of Issue #3130 -- the C++ pathfinder
      now accepts per-net ``emit_trace_width`` / ``emit_via_diameter`` /
      ``emit_via_drill`` parameters, so the original rationale
      ("C++ uses a single rules.trace_width") no longer applies.
      Empirically the C++ backend routes more signal nets than Python
      on this recipe (17/32 vs 15/32 at HEAD) and finishes in ~3 minutes
      vs ~6 minutes.  Letting ``--backend auto`` (the default) select
      cpp when the native extension is built preserves both the speed
      and the Python fallback for environments without it.
    - ``--seed 42``: deterministic output for byte-identical re-routes
      in CI.
    - ``--early-stop-patience 4``: per Issue #3101, the negotiator
      stops after N consecutive non-improving rip-up iterations.
      Default 2; bumped to 4 here to give the rip-up loop more shots
      at hard-to-route SWCLK/SWDIO/NRST nets.
    - ``--auto-fix --auto-fix-passes 3``: invoke fix-drc as a post-
      route step with its own time budget (instead of in-line --auto-
      fix which shares the routing deadline -- issue #2802).

    Skip nets remain the high-current power/phase nets that are carried
    by copper pours instead of routed traces.
    """
    print("\n" + "=" * 60)
    print("Routing PCB (via ``kct route`` flag recipe -- Issues #3096, #3111)...")
    print("=" * 60)

    # Skip power and high-current nets (route manually or use copper pour zones)
    # Phase nets carry motor current (10A+) and need wide traces (2mm+)
    skip_nets = ["+24V", "+5V", "+3V3", "GND", "PHASE_A", "PHASE_B", "PHASE_C"]

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--no-auto-layers",
        "--layers",
        "2",
        "--manufacturer",
        "jlcpcb",
        "--differential-pairs",
        # Issue #3221: re-pin ``--backend python``.  PR #3131 (Issue
        # #3130) removed this pin on the rationale that #3130's per-net
        # trace-width / via-size emit landed in the C++ pathfinder, but
        # the empirical board-05 measurement captured in that PR's body
        # (32 -> 55 DRC errors with cpp vs python) confirmed the C++
        # backend produces substantially more clearance violations on
        # this dense MOSFET-bridge layout in exchange for +2 routed
        # nets.  Bisect under #3221 traced board 05's 9 -> 37 blocking
        # regression directly to that pin removal (pre-#3131 c3cee787
        # produced 3 blocking; post-#3131 f653033f jumped to 35 blocking)
        # -- the per-pad channel budget work (#3198/#3201) and A* tie-
        # break changes (#3192/#3204) did not contribute, since A/B
        # measurements at HEAD with the budget short-circuited and the
        # comparator reverted to pre-#3192 both stayed at 36 blocking.
        # Re-pinning python recovers the original floor=9 baseline that
        # the routed-drc tolerance file documents.  Letting the backend
        # auto-resolve will be revisited under a follow-up issue once
        # the C++ pathfinder's per-net per-clearance cost surface
        # matches the Python backend's behaviour on this layout.
        #
        # Issue #3337 (2026-06-08) re-measurement on current main
        # (post Wave 9 fixes #3258/#3307): the gap remains substantial.
        # Side-by-side A/B at HEAD (committed pin vs fresh cpp re-route):
        #
        #   backend    | total | blocking | pad_seg | seg_seg | seg_via | reach
        #   committed  |   29  |     6    |    6    |    0    |    0    | 60%
        #   python new |   32  |    11    |    3    |    5    |    3    | 60%
        #   cpp new    |   68  |    54    |   11    |   31    |    9    | 67%
        #
        # The cpp backend buys +7% routing reach at the cost of a 9x
        # increase in clearance_segment_segment violations and a 5x
        # increase in total blocking errors -- the python pin remains
        # the correct choice for this layout.  The committed PCB is
        # also strictly better than a fresh python re-route under
        # current main (6 vs 11 blocking), per the
        # test_committed_pcb_has_no_segment_segment_or_segment_via
        # regression test from PR #3258.  DO NOT overwrite the
        # committed routed snapshot without manually verifying the
        # new fresh re-route is strictly better.
        "--backend",
        "python",
        "--seed",
        "42",
        "--timeout",
        "900",  # Issue #3111: was 360; bumped so auto-fix has budget after negotiator finishes
        "--per-net-timeout",
        "30",
        "--early-stop-patience",
        "4",  # Issue #3111: default 2, bumped to 4 so iter 5-6 try harder nets
        "--auto-fix",
        "--auto-fix-passes",
        "3",  # Issue #3111: post-route fix-drc with its own budget
        "--skip-nets",
        ",".join(skip_nets),
    ]

    print(f"\n1. Input: {input_path}")
    print(f"   Output: {output_path}")
    print(f"   Skipping power nets: {skip_nets}")
    print(f"   Command: {' '.join(cmd)}")
    print("\n2. Routing...")

    result = subprocess.run(cmd, capture_output=False, text=True)

    # ``kct route`` returns 0 on full success and a non-zero code on
    # partial / failed routing.  Either way it writes a routed PCB to
    # ``output_path`` (the partial-results file is at
    # ``<stem>_partial.kicad_pcb``).  As long as the output file exists,
    # downstream steps (zone fill + DRC) can run; report success/partial
    # purely informationally.
    success = result.returncode == 0

    if not output_path.exists():
        print(f"\n   ERROR: ``kct route`` did not produce {output_path}", file=sys.stderr)
        return False

    if success:
        print("\n   SUCCESS: ``kct route`` reports all signal nets routed!")
    else:
        print(
            f"\n   PARTIAL: ``kct route`` exited with code {result.returncode} "
            "(partial routing; downstream zone fill + DRC will continue)"
        )

    return success


def fill_zones_in_routed_pcb(routed_path: Path) -> int:
    """Fill copper zones in the routed PCB via ``kicad-cli``.

    Issue #2899 acceptance criterion 3 requires each zone's filled area
    to cover >=80% of its plane region.  ``add_zone`` only writes the
    zone *definition* (polygon outline + net + layer + priority); the
    actual copper polygon is computed by KiCad's fill engine when the
    zone is filled.

    ``kct route`` calls :func:`route_cmd._fill_zones_after_route` once
    routing completes, which invokes ``kicad-cli pcb fill-zones`` (or
    falls back to ``kicad-cli pcb drc`` on older KiCad versions which
    fills zones as a side-effect).  This script bypasses ``kct route``
    and uses :func:`kicad_tools.router.load_pcb_for_routing` directly,
    so we have to fill the zones ourselves for parity.

    Without this step the routed PCB carries ``(zone ...)`` blocks but
    no ``(filled_polygon ...)`` entries -- zone_unfilled DRC warnings
    surface in ``kct check`` and exported Gerbers ship without any
    plane copper.

    Returns the number of zones present in the routed PCB after fill
    (informational only; the actual fill polygons are validated by DRC).
    Returns 0 silently when ``kicad-cli`` is unavailable so the script
    still runs on developer machines without KiCad installed.
    """
    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones

    print("\n" + "=" * 60)
    print("Filling copper zones...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        print("\n   WARNING: kicad-cli not found - skipping zone fill")
        print("   Install KiCad 8 from: https://www.kicad.org/download/")
        return 0

    print(f"\n1. Filling zones in: {routed_path}")
    result = run_fill_zones(routed_path, kicad_cli=kicad_cli)

    if not result.success:
        print(f"\n   WARNING: Zone fill failed: {result.stderr or '(no stderr)'}")
        return 0

    # Re-read and report zone count for visibility.
    try:
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(routed_path))
        print(f"\n2. Result: {len(pcb.zones)} zone(s) filled")
        for z in pcb.zones:
            layers = z.layers if hasattr(z, "layers") and z.layers else ["?"]
            print(f"   - {z.net_name} on {layers[0]} (priority {getattr(z, 'priority', '?')})")
        return len(pcb.zones)
    except Exception as e:
        print(f"\n   WARNING: Could not re-read routed PCB to count zones: {e}")
        return 0


def run_fix_drc(pcb_path: Path) -> bool:
    """
    Run ``kct fix-drc`` on the routed PCB to repair clearance violations.

    Issue #3096 (M-E): the ``--auto-fix`` flag inside ``kct route`` shares
    the routing wall-clock budget, and on this board the negotiator
    consumes the entire timeout leaving auto-fix to skip with
    "deadline reached".  Calling fix-drc as a separate subprocess with
    its own budget lets the clearance repair actually run and brings
    the DRC error count down before the manufacturing-bundle gate.

    Returns True iff fix-drc reduced the violation count or had nothing
    to do.  Even on failure, the PCB is left in its post-route state
    (overwritten only when fix-drc successfully writes a new version).
    """
    print("\n" + "=" * 60)
    print("Repairing DRC violations (via ``kct fix-drc``)...")
    print("=" * 60)

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-drc",
        str(pcb_path),
        "--max-passes",
        "3",
        "--margin",
        "0.05",
        "--format",
        "summary",
    ]
    print(f"\n   Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            print(f"   {line}")
    if result.returncode != 0 and result.stderr:
        print(f"\n   stderr: {result.stderr}")
    return result.returncode == 0


def generate_manufacturing(routed_path: Path, output_dir: Path) -> bool:
    """
    Generate manufacturing artifacts (Gerbers, drill, BOM, CPL, project
    zip, DRC/ERC reports) into ``<output_dir>/manufacturing/`` using
    ``kct export``.

    Issue #3096 AC: "first mfg bundle produced".  Targets JLCPCB.
    Preflight DRC violations are reported but do not block export (the
    routed PCB ships with seg-seg/pad-seg clearance issues that are
    tracked as part of the same milestone -- see ``run_fix_drc()``).

    Returns True iff ``kct export`` succeeded.
    """
    print("\n" + "=" * 60)
    print("Generating manufacturing artifacts (kct export)...")
    print("=" * 60)

    mfr_dir = output_dir / "manufacturing"
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "export",
        str(routed_path),
        "--mfr",
        "jlcpcb",
        "--output",
        str(mfr_dir),
        # The post-route PCB has known clearance violations on this
        # board (see M-E follow-up); allow the bundle to be produced
        # so downstream tooling/UI sees the artifacts.
        "--skip-preflight",
    ]
    print(f"\n   Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            print(f"   {line}")
    if result.returncode != 0:
        if result.stderr:
            print(f"\n   Export stderr:\n{result.stderr}")
        print(f"\n   FAILED: kct export exited {result.returncode}")
        return False

    print(f"\n   SUCCESS: manufacturing artifacts written to {mfr_dir}")
    return True


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
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "bldc_controller")

        # Step 2: Create schematic
        sch_path = create_bldc_controller(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_bldc_pcb(output_dir)

        # Step 5: Create copper-pour zones for power/ground nets.
        # Issue #2899: must happen *before* routing so the router's raw-text
        # concatenation in route_pcb() preserves the zones (see #2770).
        zones_created = create_zones_for_pcb(pcb_path)

        # Step 6: Route PCB
        routed_path = output_dir / "bldc_controller_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 7: Repair DRC clearance violations introduced by routing.
        # Issue #3096: tried calling fix-drc as a separate subprocess
        # but on this board it repairs 5/12 clearance violations at the
        # cost of 2 connectivity regressions (nudging stranded existing
        # routed pads), and the net DRC count stays roughly flat.  The
        # route's built-in "Optimizing traces" DRC-nudge pass (which
        # ALWAYS runs regardless of --auto-fix) already does the
        # repairs that are safe; an additional fix-drc subprocess just
        # trades clearance errors for connectivity errors.  Skip it.
        # See builder notes on PR for measurements.
        # run_fix_drc(routed_path)  # disabled per builder measurement

        # Step 8: Fill copper zones in the routed PCB.
        # Issue #2899 AC: filled zones must cover >=80% of the plane region.
        # ``kct route`` performs this automatically after routing (see
        # route_cmd._fill_zones_after_route); this script bypasses the CLI
        # and uses the router API directly, so we fill the zones here for
        # parity.  Without this step the routed PCB carries (zone ...) blocks
        # but no (filled_polygon ...) entries -- zone_unfilled DRC warnings
        # surface and Gerbers ship without plane copper.
        zones_filled = fill_zones_in_routed_pcb(routed_path)

        # Step 9: Run DRC
        drc_success = run_drc(routed_path)

        # Step 10: Generate manufacturing bundle (Issue #3096 AC).
        # The export step is informational here -- failure does not
        # change the script's exit code, but the artifacts under
        # output/manufacturing/ satisfy the M-E milestone's "first mfg
        # bundle produced" criterion.
        mfg_success = generate_manufacturing(routed_path, output_dir)

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
        print(f"  Zones: {zones_created} zone(s) created, {zones_filled} filled")
        print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
        print(f"  DRC: {'PASS' if drc_success else 'FAIL'}")
        print(f"  Manufacturing bundle: {'PASS' if mfg_success else 'FAIL'}")
        print("\nComponent summary:")
        print("  Power input: J1, F1, D1, C1-C2")
        print("  Buck (24V->5V): U1, L1, D2, C3-C4")
        print("  LDO (5V->3.3V): U2, C5-C6")
        print("  MCU: C7-C9, Y1 (C10-C11)")
        print("  Gate driver: U3, C12-C16")
        print("  Power stage: Q1-Q6, R10-R12 (current sense), R20-R22 (gate-drive)")
        print("  Connectors: J1-J4")
        print("  LEDs: D3-D4, R3-R4")
        print("  Hall filter: R30-R32 (pull-ups), C30-C32 (filters)")

        # For this board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
