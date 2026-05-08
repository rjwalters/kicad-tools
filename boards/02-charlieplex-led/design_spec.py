"""
Shared design specification for the 3x3 Charlieplex LED Grid.

This module defines the single source of truth for:
- Charlieplex topology (pin labels + ordered LED pairs)
- LED connections (charlieplex topology, derived)
- MCU pin assignments
- Resistor connections (derived)
- Net definitions

Both generate_schematic.py and generate_pcb.py import from this module,
ensuring the schematic and PCB stay synchronized.

Charlieplexing allows driving N*(N-1) LEDs with N pins.
With 4 pins (A, B, C, D), we can drive 12 LEDs:
  A->B, B->A, A->C, C->A, A->D, D->A (6 LEDs)
  B->C, C->B, B->D, D->B (4 LEDs)
  C->D, D->C (2 LEDs)

For a 3x3 grid, we use 9 of these 12 combinations.

Source of truth: CHARLIEPLEX_PIN_LABELS and CHARLIEPLEX_LED_PAIRS below
fully describe the topology. LED_CONNECTIONS and RESISTOR_CONNECTIONS are
derived from them so existing consumers (generate_pcb.py) stay unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class LedConnection(NamedTuple):
    """Defines a single LED's connection in the charlieplex matrix.

    Attributes:
        ref: Component reference (e.g., "D1")
        anode_node: Net name connected to LED anode
        cathode_node: Net name connected to LED cathode
    """

    ref: str
    anode_node: str
    cathode_node: str


class ResistorConnection(NamedTuple):
    """Defines a current-limiting resistor's connection.

    Attributes:
        ref: Component reference (e.g., "R1")
        input_net: Net connected to pin 1 (MCU side)
        output_net: Net connected to pin 2 (LED node side)
    """

    ref: str
    input_net: str
    output_net: str


# =============================================================================
# Charlieplex topology (source of truth)
# =============================================================================
# These two values fully describe the charlieplex matrix topology.  Everything
# else (LED_CONNECTIONS, RESISTOR_CONNECTIONS) is derived from them and the
# kicad_tools.schematic.blocks.create_charlieplex_matrix factory consumes the
# same pair of values directly.

# Per-pin label used in net names (LINE_<L>, NODE_<L>).
CHARLIEPLEX_PIN_LABELS: tuple[str, ...] = ("A", "B", "C", "D")

# Ordered (anode_pin_idx, cathode_pin_idx) pairs.  Index ranges 0..3.
# To light D{i+1}: drive pair[0] HIGH, pair[1] LOW, others HIGH-Z.
CHARLIEPLEX_LED_PAIRS: tuple[tuple[int, int], ...] = (
    (0, 1),  # D1: A->B
    (1, 0),  # D2: B->A
    (0, 2),  # D3: A->C
    (2, 0),  # D4: C->A
    (0, 3),  # D5: A->D
    (3, 0),  # D6: D->A
    (1, 2),  # D7: B->C
    (2, 1),  # D8: C->B
    (1, 3),  # D9: B->D
)


# =============================================================================
# LED Connections: derived from the charlieplex topology
# =============================================================================
# Each LED is connected between two nodes (A, B, C, D).
# The direction determines which node is anode vs cathode.
# To light an LED: set anode HIGH, cathode LOW, others HIGH-Z.

LED_CONNECTIONS: tuple[LedConnection, ...] = tuple(
    LedConnection(
        ref=f"D{i + 1}",
        anode_node=f"NODE_{CHARLIEPLEX_PIN_LABELS[anode_idx]}",
        cathode_node=f"NODE_{CHARLIEPLEX_PIN_LABELS[cathode_idx]}",
    )
    for i, (anode_idx, cathode_idx) in enumerate(CHARLIEPLEX_LED_PAIRS)
)


# =============================================================================
# Resistor Connections: derived from the charlieplex topology
# =============================================================================
# Each LINE_x connects to the MCU pin, NODE_x connects to the LED network.
# Resistor value should be calculated based on LED Vf and desired current.

RESISTOR_CONNECTIONS: tuple[ResistorConnection, ...] = tuple(
    ResistorConnection(
        ref=f"R{i + 1}",
        input_net=f"LINE_{label}",
        output_net=f"NODE_{label}",
    )
    for i, label in enumerate(CHARLIEPLEX_PIN_LABELS)
)

# Default resistor value (ohms) - calculated for ~10mA with typical LED
RESISTOR_VALUE = "330R"


# =============================================================================
# MCU Pin Assignments
# =============================================================================
# Maps MCU connector pin numbers to net names.
# None indicates no-connect (NC) pins.

MCU_PINS: dict[str, str | None] = {
    "1": "LINE_A",
    "2": "LINE_B",
    "3": "LINE_C",
    "4": "LINE_D",
    "5": "GND",  # Unused input tied to GND
    "6": "GND",  # Unused input tied to GND
    "7": "VCC",
    "8": "GND",
}


# =============================================================================
# Net Definitions with Numeric IDs (for PCB)
# =============================================================================
# Net number 0 is reserved for "no net" in KiCad PCB files.
# These IDs are used in PCB footprint pad assignments.


@dataclass(frozen=True)
class NetDefinition:
    """Defines a net with its name and numeric ID for PCB."""

    name: str
    number: int


# Build net definitions from the design
_NETS: list[NetDefinition] = [
    NetDefinition("LINE_A", 1),
    NetDefinition("LINE_B", 2),
    NetDefinition("LINE_C", 3),
    NetDefinition("LINE_D", 4),
    NetDefinition("NODE_A", 5),
    NetDefinition("NODE_B", 6),
    NetDefinition("NODE_C", 7),
    NetDefinition("NODE_D", 8),
    NetDefinition("VCC", 9),
    NetDefinition("GND", 10),
]

# Export as dict for easy lookup by name
NETS: dict[str, int] = {"": 0}  # Start with empty net = 0
NETS.update({net.name: net.number for net in _NETS})


# =============================================================================
# Helper Functions
# =============================================================================


def get_led_nodes() -> set[str]:
    """Return all unique node names used by LEDs."""
    nodes = set()
    for led in LED_CONNECTIONS:
        nodes.add(led.anode_node)
        nodes.add(led.cathode_node)
    return nodes


def get_line_nets() -> list[str]:
    """Return MCU line net names in order."""
    return [net for net in MCU_PINS.values() if net and net.startswith("LINE_")]


def get_led_by_ref(ref: str) -> LedConnection | None:
    """Look up an LED connection by reference designator."""
    for led in LED_CONNECTIONS:
        if led.ref == ref:
            return led
    return None


def get_resistor_by_ref(ref: str) -> ResistorConnection | None:
    """Look up a resistor connection by reference designator."""
    for res in RESISTOR_CONNECTIONS:
        if res.ref == ref:
            return res
    return None


# =============================================================================
# Design Validation
# =============================================================================


def validate_design() -> list[str]:
    """Validate design consistency, return list of issues (empty if valid)."""
    issues = []

    # Check all resistor output nets are valid LED nodes
    led_nodes = get_led_nodes()
    for res in RESISTOR_CONNECTIONS:
        if res.output_net not in led_nodes:
            issues.append(f"{res.ref}: output net {res.output_net} not in LED nodes")

    # Check all LED nodes have a corresponding resistor
    resistor_outputs = {r.output_net for r in RESISTOR_CONNECTIONS}
    for node in led_nodes:
        if node not in resistor_outputs:
            issues.append(f"LED node {node} has no corresponding resistor")

    # Check net IDs are unique and contiguous
    net_numbers = sorted(n.number for n in _NETS)
    expected = list(range(1, len(net_numbers) + 1))
    if net_numbers != expected:
        issues.append(f"Net numbers not contiguous: {net_numbers}")

    return issues


if __name__ == "__main__":
    # Print design summary when run directly
    print("Charlieplex 3x3 LED Grid Design Specification")
    print("=" * 50)
    print(f"\nLEDs: {len(LED_CONNECTIONS)}")
    for led in LED_CONNECTIONS:
        print(f"  {led.ref}: {led.anode_node} -> {led.cathode_node}")

    print(f"\nResistors: {len(RESISTOR_CONNECTIONS)}")
    for res in RESISTOR_CONNECTIONS:
        print(f"  {res.ref}: {res.input_net} -> {res.output_net}")

    print("\nMCU Pins:")
    for pin, net in MCU_PINS.items():
        print(f"  Pin {pin}: {net or 'NC'}")

    print(f"\nNets: {len(NETS) - 1}")  # Exclude empty net
    for name, num in sorted(NETS.items(), key=lambda x: x[1]):
        if name:
            print(f"  {num}: {name}")

    issues = validate_design()
    if issues:
        print("\nValidation FAILED:")
        for issue in issues:
            print(f"  ERROR: {issue}")
    else:
        print("\nValidation: PASSED")
