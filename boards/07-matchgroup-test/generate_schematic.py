#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the match-group test board (board 07).

This emits a flat schematic that is a **fully wired** logical map of the
PCB: every component pin carries a short wire stub terminated by a global
label naming the pin's net, so the schematic netlist is pad-for-pad
identical to the PCB's (issue #4012; the pre-#4012 revision emitted only
floating labels, which bound ZERO pins and made LVS vacuous, #4005/#4006).

Because the PCB's pad numbering is package-native (HDMI ``S1/S2`` shield
pads, FFC ``M1/M2`` mounting pads, BGA ``A1..G7``), the stock
``Connector_Generic`` symbols (numeric pins ``1..N``) can never bind those
pads.  Instead this script synthesizes one testbench symbol per component
via :mod:`kicad_tools.schematic.symbol_generator`, with pin numbers
matching the PCB pads exactly, writes them to a project-local
``matchgroup_lvs.kicad_sym`` library next to the schematic, and resolves
them through ``Schematic(local_symbol_libs=[...])`` (the board-05
``board05_custom`` pattern).  The emitted ``.kicad_sch`` embeds the
symbol definitions in ``lib_symbols``, so downstream consumers (LVS,
KiCad itself) need no library setup.

``PIN_NETS`` below is the schematic-side source of truth and mirrors the
per-pad net assignment in ``generate_pcb.py`` — the copper-LVS step in
``generate_design.py`` is what keeps the two in lock-step.  Note board 07
routes PARTIAL by design (5 seed-invariant unroutable nets, #3438:
DQ3 / DQ4 / MIPI_DAT0_N / TMDS_D0_N / TMDS_D1_N), so with a wired
schematic copper-LVS HONESTLY reports those 5 as opens.

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
# schematic).  lib_ids take the form ``matchgroup_lvs:<SYMBOL>``.
LIB_NAME = "matchgroup_lvs"

# ---------------------------------------------------------------------------
# Schematic-side netlist: {ref: {pad: net}} for every pad on the board.
# Mirrors generate_pcb.py's per-pad net assignment pad-for-pad (the
# copper-LVS step in generate_design.py flags any divergence).
# ---------------------------------------------------------------------------
PIN_NETS: dict[str, dict[str, str]] = {
    # J1 -- FFC-6 MIPI CSI source
    "J1": {
        "1": "MIPI_CLK_P",
        "2": "MIPI_CLK_N",
        "3": "MIPI_DAT0_P",
        "4": "MIPI_DAT0_N",
        "5": "MIPI_DAT1_P",
        "6": "MIPI_DAT1_N",
        "M1": "GND",
        "M2": "GND",
    },
    # J2 -- HDMI TMDS source
    "J2": {
        "1": "TMDS_D0_P",
        "2": "TMDS_D0_N",
        "3": "GND",
        "4": "TMDS_D1_P",
        "5": "TMDS_D1_N",
        "6": "GND",
        "7": "TMDS_D2_P",
        "8": "TMDS_D2_N",
        "S1": "GND",
        "S2": "GND",
    },
    # J3 -- ADDR bus header
    "J3": {
        "1": "GND",
        "2": "A0",
        "3": "A1",
        "4": "A2",
        "5": "A3",
        "6": "A4",
        "7": "A5",
        "8": "A6",
        "9": "A7",
    },
    # U1 -- QFN-48 DDR controller (source for DDR data byte)
    "U1": {
        "1": "+1V8",
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
        "12": "+1V2",
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
        "25": "DQ0",
        "26": "DQ1",
        "27": "DQ2",
        "28": "DQ3",
        "29": "DM0",
        "30": "DQS_P",
        "31": "DQS_N",
        "32": "DQ4",
        "33": "DQ5",
        "34": "DQ6",
        "35": "DQ7",
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
    # U2 -- QFN-48 DDR DRAM sink
    "U2": {
        "1": "DQ0",
        "2": "DQ1",
        "3": "DQ2",
        "4": "DQ3",
        "5": "DM0",
        "6": "DQS_P",
        "7": "DQS_N",
        "8": "DQ4",
        "9": "DQ5",
        "10": "DQ6",
        "11": "DQ7",
        "12": "+1V2",
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
        "24": "+1V2",
        "25": "GND",
        "26": "GND",
        "27": "GND",
        "28": "GND",
        "29": "GND",
        "30": "GND",
        "31": "GND",
        "32": "GND",
        "33": "GND",
        "34": "GND",
        "35": "GND",
        "36": "+1V8",
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
    # U3 -- QFN-24 MIPI sink
    "U3": {
        "1": "MIPI_CLK_P",
        "2": "MIPI_CLK_N",
        "3": "MIPI_DAT0_P",
        "4": "MIPI_DAT0_N",
        "5": "MIPI_DAT1_P",
        "6": "MIPI_DAT1_N",
        "7": "GND",
        "8": "GND",
        "9": "GND",
        "10": "GND",
        "11": "GND",
        "12": "+1V8",
        "13": "GND",
        "14": "GND",
        "15": "GND",
        "16": "GND",
        "17": "GND",
        "18": "+1V2",
        "19": "GND",
        "20": "GND",
        "21": "GND",
        "22": "GND",
        "23": "GND",
        "24": "GND",
    },
    # U4 -- BGA-49 HDMI sink
    "U4": {
        "A1": "GND",
        "A2": "GND",
        "A3": "GND",
        "A4": "GND",
        "A5": "GND",
        "A6": "GND",
        "A7": "GND",
        "B1": "TMDS_D0_P",
        "B2": "TMDS_D0_N",
        "B3": "TMDS_D1_P",
        "B4": "TMDS_D1_N",
        "B5": "TMDS_D2_P",
        "B6": "TMDS_D2_N",
        "B7": "GND",
        "C1": "GND",
        "C2": "+1V2",
        "C3": "+1V8",
        "C4": "+1V8",
        "C5": "+1V8",
        "C6": "+1V8",
        "C7": "GND",
        "D1": "GND",
        "D2": "+1V8",
        "D3": "+1V8",
        "D4": "+1V8",
        "D5": "+1V8",
        "D6": "+1V8",
        "D7": "GND",
        "E1": "GND",
        "E2": "+1V8",
        "E3": "+1V8",
        "E4": "+1V8",
        "E5": "+1V8",
        "E6": "+1V2",
        "E7": "GND",
        "F1": "GND",
        "F2": "GND",
        "F3": "GND",
        "F4": "GND",
        "F5": "GND",
        "F6": "GND",
        "F7": "GND",
        "G1": "GND",
        "G2": "GND",
        "G3": "GND",
        "G4": "GND",
        "G5": "GND",
        "G6": "GND",
        "G7": "GND",
    },
    # U5 -- QFP-48 SRAM (ADDR sink)
    "U5": {
        "1": "A0",
        "2": "A1",
        "3": "A2",
        "4": "A3",
        "5": "A4",
        "6": "A5",
        "7": "A6",
        "8": "A7",
        "9": "GND",
        "10": "GND",
        "11": "GND",
        "12": "+1V8",
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
        "24": "+1V2",
        "25": "GND",
        "26": "GND",
        "27": "GND",
        "28": "GND",
        "29": "GND",
        "30": "GND",
        "31": "GND",
        "32": "GND",
        "33": "GND",
        "34": "GND",
        "35": "GND",
        "36": "+1V8",
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
}

# Placement + identity per component: (ref, symbol_name, value, footprint,
# x, y).  Sources in the left column, sinks in the right column; y spacing
# leaves room for each symbol's body (pin count / 2 * 2.54mm) plus stubs.
COMPONENTS: list[tuple[str, str, str, str, float, float]] = [
    (
        "U1",
        "QFN48_DDR_CTRL",
        "QFN48_DDR_CTRL",
        "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm",
        50.8,
        50.8,
    ),
    ("J1", "FFC6_MIPI_SRC", "FFC6_MIPI", "Connector_FFC:FFC_6P_1.0mm", 50.8, 101.6),
    ("J2", "HDMI_SRC", "HDMI19", "Connector_Video:HDMI_A_Receptacle", 50.8, 127.0),
    (
        "J3",
        "ADDR_HDR_SRC",
        "ADDR_HDR",
        "Connector_PinHeader_2.54mm:PinHeader_1x09_P2.54mm_Vertical",
        50.8,
        152.4,
    ),
    ("U2", "QFN48_DRAM", "QFN48_DRAM", "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm", 177.8, 50.8),
    ("U3", "QFN24_MIPI", "QFN24_MIPI", "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm", 177.8, 127.0),
    (
        "U4",
        "BGA49_HDMI",
        "BGA49_HDMI",
        "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
        177.8,
        190.5,
    ),
    ("U5", "QFP48_SRAM", "QFP48_SRAM", "Package_QFP:LQFP-48_7x7mm_P0.5mm", 177.8, 266.7),
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


def create_matchgroup_schematic(output_path: Path) -> bool:
    """Create the match-group testbench schematic (fully wired, #4012)."""
    print("Creating Match-Group Test Schematic...")
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
        title="Match-Group Test Board",
        date="2026-05",
        revision="B",
        company="kicad-tools",
        comment1="N-trace + group-of-pairs match-group regression testbench",
        comment2="Epic #2661 Phase 3L (issue #2724); wired netlist #4012",
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
        ("+1V2", 7.62),
        ("+1V8", 17.78),
        ("GND", 309.88),
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
