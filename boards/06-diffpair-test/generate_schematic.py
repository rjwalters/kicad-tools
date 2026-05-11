#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the differential-pair test board (board 06).

This emits a flat schematic with global labels for each declared net.
The schematic is a minimal logical map of the PCB --- it's a routing
regression testbench, not a working device, so the schematic uses
placeholder symbols (Conn_01xN connectors) for source / sink endpoints.

Each global label declares one net.  Differential pairs are drawn with
adjacent labels so the schematic visually communicates the P/N pairing
without needing the (more complex) ``DiffPair`` annotation.

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


def add_pin_label(sch: Schematic, pin_pos: tuple, net_name: str, direction: str = "right") -> None:
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


def create_diffpair_schematic(output_path: Path) -> bool:
    """Create the diff-pair testbench schematic."""
    print("Creating Differential Pair Test Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="Differential Pair Test Board",
        date="2026-05",
        revision="A",
        company="kicad-tools",
        comment1="Multi-protocol HSDI regression testbench",
        comment2="Epic #2556 Phase 4L (issue #2658)",
        snap_mode=SnapMode.AUTO,
        grid=2.54,
    )

    # =========================================================================
    # Source connectors (left column)
    # =========================================================================
    print("\n1. Placing source connectors...")

    # USB-C source (J1) - large connector to source USB 2.0 and USB 3.0 pairs
    j1 = sch.add_symbol(
        "Connector_Generic:Conn_02x12_Counter_Clockwise",
        x=50.8,
        y=50.8,
        ref="J1",
        value="USB-C",
        footprint="Connector_USB:USB_C_Receptacle_USB2.0",
    )
    print(f"   J1 (USB-C source): ({j1.x}, {j1.y})")

    # Mini-PCIe edge connector (J3)
    j3 = sch.add_symbol(
        "Connector_Generic:Conn_01x12",
        x=50.8,
        y=139.7,
        ref="J3",
        value="MiniPCIe",
        footprint="Connector_PCIE:PCIE_Mini_Edge",
    )
    print(f"   J3 (Mini-PCIe source): ({j3.x}, {j3.y})")

    # FFC connector (J4)
    j4 = sch.add_symbol(
        "Connector_Generic:Conn_01x04",
        x=50.8,
        y=190.5,
        ref="J4",
        value="FFC4",
        footprint="Connector_FFC:FFC_4P_0.5mm",
    )
    print(f"   J4 (FFC source): ({j4.x}, {j4.y})")

    # =========================================================================
    # Sink ICs (right column)
    # =========================================================================
    print("\n2. Placing sink ICs...")

    u1 = sch.add_symbol(
        "Connector_Generic:Conn_02x16_Counter_Clockwise",
        x=152.4,
        y=50.8,
        ref="U1",
        value="QFN32_USB2",
        footprint="Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm",
    )
    print(f"   U1 (QFN-32 USB 2.0 sink): ({u1.x}, {u1.y})")

    u2 = sch.add_symbol(
        "Connector_Generic:Conn_02x16_Counter_Clockwise",
        x=152.4,
        y=101.6,
        ref="U2",
        value="BGA49_USB3",
        footprint="Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
    )
    print(f"   U2 (BGA-49 USB 3.0 sink): ({u2.x}, {u2.y})")

    u3 = sch.add_symbol(
        "Connector_Generic:Conn_02x16_Counter_Clockwise",
        x=152.4,
        y=152.4,
        ref="U3",
        value="QFP48_PCIe",
        footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
    )
    print(f"   U3 (QFP-48 PCIe sink): ({u3.x}, {u3.y})")

    u4 = sch.add_symbol(
        "Connector_Generic:Conn_02x12_Counter_Clockwise",
        x=152.4,
        y=203.2,
        ref="U4",
        value="QFN24_MIPI",
        footprint="Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm",
    )
    print(f"   U4 (QFN-24 MIPI sink): ({u4.x}, {u4.y})")

    # =========================================================================
    # Power flags
    # =========================================================================
    print("\n3. Power symbols + PWR_FLAG...")

    rails = [
        ("VBUS_USB", 7.62),
        ("+3V3", 17.78),
        ("+1V8", 27.94),
        ("+1V2", 38.1),
        ("GND", 226.06),
    ]
    for name, y in rails:
        pwr_x = 25.4
        sch.add_global_label(name, pwr_x, y, shape="input", rotation=0, snap=False)
        sch.add_pwr_flag(pwr_x, y)

    # =========================================================================
    # Pair-by-pair global label emission (the schematic-side "nets")
    # =========================================================================
    print("\n4. Differential pair declarations (global labels)...")

    # We don't try to wire each connector pin to its exact partner; the
    # PCB-side net assignment carries the connectivity.  Instead we emit
    # one labelled wire stub per net, grouped by protocol, so the
    # schematic file contains every diff-pair net by name.  This makes
    # the schematic queryable for "does this design exercise the MIPI
    # CLK pair?" and similar audits.
    #
    # Labels are arranged in a column on the right (x=190.5) for
    # readability.  ERC will report unconnected pins on U1-U4 / J1-J4
    # because we don't wire each pin --- that's accepted, since this is
    # a routing scaffold not a wiring-correct device schematic.
    label_x = 190.5
    pairs = [
        ("USB2_D+", "USB2_D-"),
        ("USB3_TX1+", "USB3_TX1-"),
        ("USB3_RX1+", "USB3_RX1-"),
        ("USB3_TX2+", "USB3_TX2-"),
        ("USB3_RX2+", "USB3_RX2-"),
        ("PCIE_TX+", "PCIE_TX-"),
        ("PCIE_RX+", "PCIE_RX-"),
        ("MIPI_CLK+", "MIPI_CLK-"),
        ("MIPI_D0+", "MIPI_D0-"),
    ]
    label_y = 50.8
    for p_name, n_name in pairs:
        sch.add_global_label(
            p_name, label_x, label_y, shape="bidirectional", rotation=0, snap=False
        )
        sch.add_global_label(
            n_name, label_x, label_y + 5.08, shape="bidirectional", rotation=0, snap=False
        )
        label_y += 12.7

    # Single-ended sideband
    sideband = ["USB_CC1", "USB_CC2", "MIPI_RST"]
    for name in sideband:
        sch.add_global_label(name, label_x, label_y, shape="bidirectional", rotation=0, snap=False)
        label_y += 5.08

    print(f"   Emitted {len(pairs) * 2 + len(sideband)} signal labels")

    # =========================================================================
    # Write
    # =========================================================================
    print("\n5. Writing schematic...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sch.write(output_path)
    print(f"   Schematic: {output_path}")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate diff-pair testbench schematic")
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output file path or directory (default: output/diffpair_test.kicad_sch)",
    )
    args = parser.parse_args()

    default_filename = "diffpair_test.kicad_sch"
    if args.output:
        output_path = Path(args.output)
        if output_path.is_dir():
            output_path = output_path / default_filename
    else:
        output_path = Path(__file__).parent / "output" / default_filename

    try:
        ok = create_diffpair_schematic(output_path)
        return 0 if ok else 1
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
