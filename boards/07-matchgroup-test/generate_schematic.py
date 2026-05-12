#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the match-group test board (board 07).

This emits a flat schematic with global labels for each declared net.
The schematic is a minimal logical map of the PCB --- it's a routing
regression testbench, not a working device, so the schematic uses
placeholder symbols (Conn_01xN connectors) for source / sink endpoints.

Each global label declares one net.  Match-group members are drawn
in adjacent groups so the schematic visually communicates the
length-matching intent without a (more complex) bus annotation.

Usage:
    python generate_schematic.py [output_file]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# Warn if running source scripts with stale pipx install
warn_if_stale()

WIRE_STUB = 5.08  # 200 mils


def create_matchgroup_schematic(output_path: Path) -> bool:
    """Create the match-group testbench schematic."""
    print("Creating Match-Group Test Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="Match-Group Test Board",
        date="2026-05",
        revision="A",
        company="kicad-tools",
        comment1="N-trace + group-of-pairs match-group regression testbench",
        comment2="Epic #2661 Phase 3L (issue #2724)",
        snap_mode=SnapMode.AUTO,
        grid=2.54,
    )

    # =========================================================================
    # Source connectors (left column)
    # =========================================================================
    print("\n1. Placing source connectors / ICs...")

    # U1 -- DDR controller (source for DDR data byte)
    u1 = sch.add_symbol(
        "Connector_Generic:Conn_02x24_Counter_Clockwise",
        x=50.8,
        y=50.8,
        ref="U1",
        value="QFN48_DDR_CTRL",
        footprint="Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm",
    )
    print(f"   U1 (DDR controller source): ({u1.x}, {u1.y})")

    # J1 -- MIPI CSI source (FFC-6)
    j1 = sch.add_symbol(
        "Connector_Generic:Conn_01x06",
        x=50.8,
        y=125.0,
        ref="J1",
        value="FFC6_MIPI",
        footprint="Connector_FFC:FFC_6P_1.0mm",
    )
    print(f"   J1 (MIPI source): ({j1.x}, {j1.y})")

    # J2 -- HDMI source
    j2 = sch.add_symbol(
        "Connector_Generic:Conn_01x08",
        x=50.8,
        y=170.0,
        ref="J2",
        value="HDMI19",
        footprint="Connector_Video:HDMI_A_Receptacle",
    )
    print(f"   J2 (HDMI source): ({j2.x}, {j2.y})")

    # J3 -- ADDR header (source for address bus)
    j3 = sch.add_symbol(
        "Connector_Generic:Conn_01x09",
        x=50.8,
        y=215.0,
        ref="J3",
        value="ADDR_HDR",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x09_P2.54mm_Vertical",
    )
    print(f"   J3 (ADDR source): ({j3.x}, {j3.y})")

    # =========================================================================
    # Sink ICs (right column)
    # =========================================================================
    print("\n2. Placing sink ICs...")

    u2 = sch.add_symbol(
        "Connector_Generic:Conn_02x24_Counter_Clockwise",
        x=152.4,
        y=50.8,
        ref="U2",
        value="QFN48_DRAM",
        footprint="Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm",
    )
    print(f"   U2 (DDR DRAM sink): ({u2.x}, {u2.y})")

    u3 = sch.add_symbol(
        "Connector_Generic:Conn_02x12_Counter_Clockwise",
        x=152.4,
        y=125.0,
        ref="U3",
        value="QFN24_MIPI",
        footprint="Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm",
    )
    print(f"   U3 (MIPI sink): ({u3.x}, {u3.y})")

    u4 = sch.add_symbol(
        "Connector_Generic:Conn_02x16_Counter_Clockwise",
        x=152.4,
        y=170.0,
        ref="U4",
        value="BGA49_HDMI",
        footprint="Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
    )
    print(f"   U4 (HDMI sink): ({u4.x}, {u4.y})")

    u5 = sch.add_symbol(
        "Connector_Generic:Conn_02x24_Counter_Clockwise",
        x=152.4,
        y=215.0,
        ref="U5",
        value="QFP48_SRAM",
        footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
    )
    print(f"   U5 (ADDR sink): ({u5.x}, {u5.y})")

    # =========================================================================
    # Power flags
    # =========================================================================
    print("\n3. Power symbols + PWR_FLAG...")

    rails = [
        ("+1V2", 7.62),
        ("+1V8", 17.78),
        ("GND", 240.0),
    ]
    for name, y in rails:
        pwr_x = 25.4
        sch.add_global_label(name, pwr_x, y, shape="input", rotation=0, snap=False)
        sch.add_pwr_flag(pwr_x, y)

    # =========================================================================
    # Match-group label emission (the schematic-side "nets")
    # =========================================================================
    print("\n4. Match-group declarations (global labels)...")

    # Labels arranged in a column on the right (x=190.5) for readability.
    # Grouped by match-group so the schematic communicates intent.
    label_x = 190.5
    label_y = 30.0

    # DDR data byte: 9 singles + DQS pair = 10 group members
    print("   DDR_DATA_BYTE_0:")
    ddr_singles = ["DQ0", "DQ1", "DQ2", "DQ3", "DQ4", "DQ5", "DQ6", "DQ7", "DM0"]
    for name in ddr_singles:
        sch.add_global_label(name, label_x, label_y, shape="bidirectional", rotation=0, snap=False)
        label_y += 5.08
    # DQS pair
    sch.add_global_label("DQS_P", label_x, label_y, shape="bidirectional", rotation=0, snap=False)
    label_y += 5.08
    sch.add_global_label("DQS_N", label_x, label_y, shape="bidirectional", rotation=0, snap=False)
    label_y += 7.62  # extra spacing between groups

    # MIPI CSI: 3 pairs
    print("   MIPI_CSI_LANES:")
    mipi_pairs = [
        ("MIPI_CLK_P", "MIPI_CLK_N"),
        ("MIPI_DAT0_P", "MIPI_DAT0_N"),
        ("MIPI_DAT1_P", "MIPI_DAT1_N"),
    ]
    for p_name, n_name in mipi_pairs:
        sch.add_global_label(
            p_name, label_x, label_y, shape="bidirectional", rotation=0, snap=False
        )
        sch.add_global_label(
            n_name, label_x, label_y + 5.08, shape="bidirectional", rotation=0, snap=False
        )
        label_y += 12.7
    label_y += 5.08

    # HDMI TMDS: 3 pairs
    print("   HDMI_TMDS_LANES:")
    tmds_pairs = [
        ("TMDS_D0_P", "TMDS_D0_N"),
        ("TMDS_D1_P", "TMDS_D1_N"),
        ("TMDS_D2_P", "TMDS_D2_N"),
    ]
    for p_name, n_name in tmds_pairs:
        sch.add_global_label(
            p_name, label_x, label_y, shape="bidirectional", rotation=0, snap=False
        )
        sch.add_global_label(
            n_name, label_x, label_y + 5.08, shape="bidirectional", rotation=0, snap=False
        )
        label_y += 12.7
    label_y += 5.08

    # ADDR bus: A0-A7
    print("   ADDR_BUS:")
    addr = ["A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7"]
    for name in addr:
        sch.add_global_label(name, label_x, label_y, shape="bidirectional", rotation=0, snap=False)
        label_y += 5.08

    print(
        f"   Emitted {len(ddr_singles) + 2 + 6 + 6 + len(addr)} signal labels across 4 match groups"
    )

    # =========================================================================
    # Write
    # =========================================================================
    print("\n5. Writing schematic...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sch.write(output_path)
    print(f"   Schematic: {output_path}")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate match-group testbench schematic")
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output file path or directory (default: output/matchgroup_test.kicad_sch)",
    )
    args = parser.parse_args()

    default_filename = "matchgroup_test.kicad_sch"
    if args.output:
        output_path = Path(args.output)
        if output_path.is_dir():
            output_path = output_path / default_filename
    else:
        output_path = Path(__file__).parent / "output" / default_filename

    try:
        ok = create_matchgroup_schematic(output_path)
        return 0 if ok else 1
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
