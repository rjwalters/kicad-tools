#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the differential-pair test board (board 06).

This emits a flat schematic that is a **fully wired** logical map of the
PCB: every component pin carries a short wire stub terminated by a global
label naming the pin's net, so the schematic netlist is pad-for-pad
identical to the PCB's (issue #4012; the pre-#4012 revision emitted only
floating labels, which bound ZERO pins and made LVS vacuous, #4005/#4006).

Because the PCB's pad numbering is package-native (USB-C ``A1..B12`` +
``S1/S2`` shield pads, BGA ``A1..G7``, FFC ``M1/M2/RST``), the stock
``Connector_Generic`` symbols (numeric pins ``1..N``) can never bind those
pads.  Instead this script synthesizes one testbench symbol per component
via :mod:`kicad_tools.schematic.symbol_generator`, with pin numbers
matching the PCB pads exactly, writes them to a project-local
``diffpair_lvs.kicad_sym`` library next to the schematic, and resolves
them through ``Schematic(local_symbol_libs=[...])`` (the board-05
``board05_custom`` pattern).  The emitted ``.kicad_sch`` embeds the
symbol definitions in ``lib_symbols``, so downstream consumers (LVS,
KiCad itself) need no library setup.

``PIN_NETS`` below is the schematic-side source of truth and mirrors the
per-pad net assignment in ``generate_pcb.py`` — the LVS gate in
``generate_design.py`` (label + copper comparators) is what keeps the two
in lock-step: any divergence fails the recipe.

Usage:
    python generate_schematic.py [output_file]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic import (
    PinDef,
    PinSide,
    PinType,
    SymbolDef,
    generate_symbol_sexp,
)
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# Warn if running source scripts with stale pipx install
warn_if_stale()

WIRE_STUB = 5.08  # 200 mils

# Name of the generated project-local symbol library (written next to the
# schematic).  lib_ids take the form ``diffpair_lvs:<SYMBOL>``.
LIB_NAME = "diffpair_lvs"

# ---------------------------------------------------------------------------
# Schematic-side netlist: {ref: {pad: net}} for every pad on the board.
# Mirrors generate_pcb.py's per-pad net assignment pad-for-pad (the LVS gate
# in generate_design.py fails the recipe if the two ever diverge).
# ---------------------------------------------------------------------------
PIN_NETS: dict[str, dict[str, str]] = {
    # J1 -- USB-C receptacle (source for USB 2.0 + USB 3.0 pairs)
    "J1": {
        "A1": "GND",
        "A2": "USB3_TX1+",
        "A3": "USB3_TX1-",
        "A4": "VBUS_USB",
        "A5": "USB_CC1",
        "A6": "USB2_D+",
        "A7": "USB2_D-",
        "A8": "+3V3",
        "A9": "VBUS_USB",
        "A10": "USB3_RX2-",
        "A11": "USB3_RX2+",
        "A12": "GND",
        "B1": "GND",
        "B2": "USB3_TX2+",
        "B3": "USB3_TX2-",
        "B4": "VBUS_USB",
        "B5": "USB_CC2",
        "B6": "USB2_D+",
        "B7": "USB2_D-",
        "B8": "+3V3",
        "B9": "VBUS_USB",
        "B10": "USB3_RX1-",
        "B11": "USB3_RX1+",
        "B12": "GND",
        "S1": "GND",
        "S2": "GND",
    },
    # J3 -- Mini-PCIe edge connector (PCIe pair source)
    "J3": {
        "1": "GND",
        "2": "GND",
        "3": "+3V3",
        "4": "GND",
        "5": "PCIE_TX+",
        "6": "PCIE_TX-",
        "7": "GND",
        "8": "PCIE_RX+",
        "9": "PCIE_RX-",
        "10": "+1V2",
        "11": "GND",
        "12": "GND",
    },
    # J4 -- FFC connector (MIPI source)
    "J4": {
        "1": "MIPI_CLK+",
        "2": "MIPI_CLK-",
        "3": "MIPI_D0+",
        "4": "MIPI_D0-",
        "M1": "GND",
        "M2": "GND",
        "RST": "MIPI_RST",
    },
    # U1 -- QFN-32 USB 2.0 sink
    "U1": {
        "1": "+3V3",
        "2": "GND",
        "3": "GND",
        "4": "GND",
        "5": "GND",
        "6": "GND",
        "7": "GND",
        "8": "GND",
        "9": "VBUS_USB",
        "10": "USB2_D+",
        "11": "USB2_D-",
        "12": "USB_CC1",
        "13": "USB_CC2",
        "14": "GND",
        "15": "GND",
        "16": "+1V8",
        "17": "+3V3",
        "18": "GND",
        "19": "GND",
        "20": "GND",
        "21": "GND",
        "22": "GND",
        "23": "GND",
        "24": "GND",
        "25": "GND",
        "26": "GND",
        "27": "GND",
        "28": "GND",
        "29": "GND",
        "30": "GND",
        "31": "GND",
        "32": "GND",
    },
    # U2 -- BGA-49 USB 3.0 sink
    "U2": {
        "A1": "GND",
        "A2": "GND",
        "A3": "GND",
        "A4": "GND",
        "A5": "GND",
        "A6": "GND",
        "A7": "GND",
        "B1": "GND",
        "B2": "USB3_TX1+",
        "B3": "USB3_TX1-",
        "B4": "GND",
        "B5": "USB3_RX1+",
        "B6": "USB3_RX1-",
        "B7": "GND",
        "C1": "GND",
        "C2": "+3V3",
        "C3": "+1V2",
        "C4": "+1V2",
        "C5": "+1V2",
        "C6": "+3V3",
        "C7": "GND",
        "D1": "GND",
        "D2": "+1V2",
        "D3": "+1V2",
        "D4": "+1V2",
        "D5": "+1V2",
        "D6": "+1V2",
        "D7": "GND",
        "E1": "GND",
        "E2": "+3V3",
        "E3": "+1V2",
        "E4": "+1V2",
        "E5": "+1V2",
        "E6": "+3V3",
        "E7": "GND",
        "F1": "GND",
        "F2": "USB3_TX2+",
        "F3": "USB3_TX2-",
        "F4": "GND",
        "F5": "USB3_RX2+",
        "F6": "USB3_RX2-",
        "F7": "GND",
        "G1": "GND",
        "G2": "GND",
        "G3": "GND",
        "G4": "GND",
        "G5": "GND",
        "G6": "GND",
        "G7": "GND",
    },
    # U3 -- QFP-48 PCIe sink
    "U3": {
        "1": "GND",
        "2": "GND",
        "3": "GND",
        "4": "GND",
        "5": "GND",
        "6": "GND",
        "7": "GND",
        "8": "GND",
        "9": "GND",
        "10": "GND",
        "11": "GND",
        "12": "GND",
        "13": "GND",
        "14": "GND",
        "15": "GND",
        "16": "GND",
        "17": "GND",
        "18": "GND",
        "19": "GND",
        "20": "GND",
        "21": "GND",
        "22": "GND",
        "23": "GND",
        "24": "GND",
        "25": "+3V3",
        "26": "GND",
        "27": "GND",
        "28": "PCIE_TX+",
        "29": "PCIE_TX-",
        "30": "GND",
        "31": "PCIE_RX+",
        "32": "PCIE_RX-",
        "33": "GND",
        "34": "GND",
        "35": "GND",
        "36": "+1V2",
        "37": "GND",
        "38": "GND",
        "39": "GND",
        "40": "GND",
        "41": "GND",
        "42": "GND",
        "43": "GND",
        "44": "GND",
        "45": "GND",
        "46": "GND",
        "47": "GND",
        "48": "GND",
    },
    # U4 -- QFN-24 MIPI sink
    "U4": {
        "1": "MIPI_CLK+",
        "2": "MIPI_CLK-",
        "3": "MIPI_D0+",
        "4": "MIPI_D0-",
        "5": "MIPI_RST",
        "6": "+1V8",
        "7": "GND",
        "8": "GND",
        "9": "GND",
        "10": "GND",
        "11": "GND",
        "12": "+3V3",
        "13": "GND",
        "14": "GND",
        "15": "GND",
        "16": "GND",
        "17": "GND",
        "18": "+1V8",
        "19": "GND",
        "20": "GND",
        "21": "GND",
        "22": "GND",
        "23": "GND",
        "24": "GND",
    },
}

# Placement + identity per component: (ref, symbol_name, value, footprint,
# x, y).  Sources in the left column, sinks in the right column; y spacing
# leaves room for each symbol's body (pin count / 2 * 2.54mm) plus stubs.
COMPONENTS: list[tuple[str, str, str, str, float, float]] = [
    ("J1", "USBC_SRC", "USB-C", "Connector_USB:USB_C_Receptacle_USB2.0", 50.8, 50.8),
    ("J3", "MINIPCIE_SRC", "MiniPCIe", "Connector_PCIE:PCIE_Mini_Edge", 50.8, 106.68),
    ("J4", "FFC_SRC", "FFC4", "Connector_FFC:FFC_4P_0.5mm", 50.8, 137.16),
    ("U1", "QFN32_USB2", "QFN32_USB2", "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm", 177.8, 50.8),
    (
        "U2",
        "BGA49_USB3",
        "BGA49_USB3",
        "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
        177.8,
        127.0,
    ),
    ("U3", "QFP48_PCIE", "QFP48_PCIe", "Package_QFP:LQFP-48_7x7mm_P0.5mm", 177.8, 203.2),
    ("U4", "QFN24_MIPI", "QFN24_MIPI", "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm", 177.8, 266.7),
]


def _build_symbol_lib(lib_path: Path) -> None:
    """Write the project-local testbench symbol library.

    One symbol per component, pin numbers matching the PCB pads exactly
    (first half of the pad list on the left edge, second half on the
    right).  All pins are ``passive`` — these are testbench endpoints, not
    electrical models.
    """
    blocks: list[str] = []
    for _ref, symbol_name, _value, footprint, _x, _y in COMPONENTS:
        pads = list(PIN_NETS[_ref])
        half = (len(pads) + 1) // 2
        pins = [
            PinDef(
                number=pad,
                name=pad,
                pin_type=PinType.PASSIVE,
                side=PinSide.LEFT if i < half else PinSide.RIGHT,
            )
            for i, pad in enumerate(pads)
        ]
        sym = SymbolDef(name=symbol_name, pins=pins, reference="U", footprint=footprint)
        text = generate_symbol_sexp(sym)
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("\t(symbol "))
        if lines[-1] != ")":
            raise RuntimeError(f"unexpected symbol-lib tail for {symbol_name}")
        blocks.append("\n".join(lines[start:-1]))

    header = (
        "(kicad_symbol_lib\n"
        "\t(version 20231120)\n"
        '\t(generator "kicad_tools")\n'
        '\t(generator_version "1.0")\n'
    )
    lib_path.write_text(header + "\n".join(blocks) + "\n)\n")


def add_pin_label(sch: Schematic, pin_pos: tuple, net_name: str, direction: str = "right") -> None:
    """Add a wire stub from a pin position to a global label.

    Pin attach is ENDPOINT-only (KiCad semantics): the wire must start
    exactly at the pin position.  The label names the net from the stub's
    far end.
    """
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


def _wire_all_pins(sch: Schematic, sym, pin_nets: dict[str, str]) -> int:
    """Wire every pin of ``sym`` to a global label carrying its net name."""
    wired = 0
    for pin in sym.symbol_def.pins:
        pos = sym.pin_position(pin.number)
        if pos is None:
            raise RuntimeError(f"{sym.reference}.{pin.number}: no pin position")
        net = pin_nets[pin.number]
        direction = "left" if pos[0] < sym.x else "right"
        add_pin_label(sch, pos, net, direction)
        wired += 1
    return wired


def create_diffpair_schematic(output_path: Path) -> bool:
    """Create the diff-pair testbench schematic (fully wired, #4012)."""
    print("Creating Differential Pair Test Schematic...")
    print("=" * 60)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    # 1. Synthesize the project-local symbol library (PCB-native pads)
    # ---------------------------------------------------------------------
    print("\n1. Writing project-local symbol library...")
    lib_path = output_path.parent / f"{LIB_NAME}.kicad_sym"
    _build_symbol_lib(lib_path)
    print(f"   {lib_path} ({len(COMPONENTS)} symbols)")

    sch = Schematic(
        title="Differential Pair Test Board",
        date="2026-05",
        revision="B",
        company="kicad-tools",
        comment1="Multi-protocol HSDI regression testbench",
        comment2="Epic #2556 Phase 4L (issue #2658); wired netlist #4012",
        snap_mode=SnapMode.AUTO,
        grid=1.27,
        local_symbol_libs=[lib_path],
    )

    # ---------------------------------------------------------------------
    # 2. Place components and wire every pin to its net label
    # ---------------------------------------------------------------------
    print("\n2. Placing components + wiring all pins...")
    total_wired = 0
    for ref, symbol_name, value, footprint, x, y in COMPONENTS:
        sym = sch.add_symbol(
            f"{LIB_NAME}:{symbol_name}",
            x=x,
            y=y,
            ref=ref,
            value=value,
            footprint=footprint,
        )
        wired = _wire_all_pins(sch, sym, PIN_NETS[ref])
        total_wired += wired
        print(f"   {ref} ({symbol_name}): {wired} pins wired at ({sym.x}, {sym.y})")

    expected = sum(len(v) for v in PIN_NETS.values())
    print(f"   Total: {total_wired}/{expected} pins wired")
    if total_wired != expected:
        raise RuntimeError(f"expected {expected} wired pins, got {total_wired}")

    # ---------------------------------------------------------------------
    # 3. Power flags (ERC: mark rails as externally driven)
    # ---------------------------------------------------------------------
    print("\n3. Power symbols + PWR_FLAG...")
    rails = [
        ("VBUS_USB", 7.62),
        ("+3V3", 17.78),
        ("+1V8", 27.94),
        ("+1V2", 38.1),
        ("GND", 297.18),
    ]
    for name, y in rails:
        pwr_x = 12.7
        sch.add_global_label(name, pwr_x, y, shape="input", rotation=0, snap=False)
        sch.add_pwr_flag(pwr_x, y)

    # ---------------------------------------------------------------------
    # 4. Write
    # ---------------------------------------------------------------------
    print("\n4. Writing schematic...")
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
