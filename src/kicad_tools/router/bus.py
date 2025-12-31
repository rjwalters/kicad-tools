"""
Bus routing support for the autorouter.

This module provides:
- BusSignal: Represents a signal that is part of a bus
- BusGroup: A group of related bus signals (e.g., DATA[7:0])
- detect_bus_signals: Parse net names to identify bus signals
- group_buses: Group related signals into bus groups
- BusRoutingMode: Routing modes for bus signals

Bus signals are detected from common naming conventions:
- Array notation: DATA[0], DATA[1], ADDR[15]
- Underscore suffix: DATA_0, DATA_1, ADDR_15
- Numeric suffix: DATA0, DATA1, ADDR15
"""

import re
from dataclasses import dataclass, field
from enum import Enum


class BusRoutingMode(Enum):
    """Routing modes for bus signals."""

    PARALLEL = "parallel"  # All traces run side-by-side
    STACKED = "stacked"  # Traces on alternating layers
    BUNDLED = "bundled"  # Closest packing for dense routing


@dataclass
class BusSignal:
    """A signal that is part of a bus.

    Attributes:
        net_name: Original net name (e.g., "DATA[7]")
        net_id: Net ID in the router
        bus_name: Base name of the bus (e.g., "DATA")
        index: Bit index in the bus (e.g., 7)
        notation: How the signal was named ("bracket", "underscore", "numeric")
    """

    net_name: str
    net_id: int
    bus_name: str
    index: int
    notation: str  # "bracket", "underscore", "numeric"

    def __hash__(self) -> int:
        return hash((self.net_id, self.bus_name, self.index))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BusSignal):
            return NotImplemented
        return (
            self.net_id == other.net_id
            and self.bus_name == other.bus_name
            and self.index == other.index
        )


@dataclass
class BusGroup:
    """A group of related bus signals.

    Attributes:
        name: Base name of the bus (e.g., "DATA")
        signals: List of signals in the bus, sorted by index
        width: Number of bits in the bus
        min_index: Lowest bit index
        max_index: Highest bit index
    """

    name: str
    signals: list[BusSignal] = field(default_factory=list)

    @property
    def width(self) -> int:
        """Number of signals in the bus."""
        return len(self.signals)

    @property
    def min_index(self) -> int:
        """Lowest bit index."""
        if not self.signals:
            return 0
        return min(s.index for s in self.signals)

    @property
    def max_index(self) -> int:
        """Highest bit index."""
        if not self.signals:
            return 0
        return max(s.index for s in self.signals)

    def is_complete(self) -> bool:
        """Check if all indices from min to max are present."""
        if not self.signals:
            return False
        indices = {s.index for s in self.signals}
        expected = set(range(self.min_index, self.max_index + 1))
        return indices == expected

    def get_net_ids(self) -> list[int]:
        """Get net IDs in bit order (LSB to MSB)."""
        return [s.net_id for s in sorted(self.signals, key=lambda s: s.index)]

    def __str__(self) -> str:
        return f"{self.name}[{self.max_index}:{self.min_index}]"


# Regex patterns for bus signal detection
# Pattern 1: Bracket notation - DATA[7], ADDR[15], etc.
_BRACKET_PATTERN = re.compile(r"^(.+)\[(\d+)\]$")

# Pattern 2: Underscore suffix - DATA_7, ADDR_15, etc.
_UNDERSCORE_PATTERN = re.compile(r"^(.+)_(\d+)$")

# Pattern 3: Numeric suffix - DATA7, ADDR15, etc.
# Must have at least one non-digit before the number
_NUMERIC_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_]*[A-Za-z_])(\d+)$")


def parse_bus_signal(net_name: str) -> tuple[str, int, str] | None:
    """Parse a net name to extract bus information.

    Args:
        net_name: The net name to parse

    Returns:
        Tuple of (bus_name, index, notation) if this is a bus signal, None otherwise.
        notation is one of: "bracket", "underscore", "numeric"
    """
    # Try bracket notation first (most explicit)
    match = _BRACKET_PATTERN.match(net_name)
    if match:
        return (match.group(1), int(match.group(2)), "bracket")

    # Try underscore notation
    match = _UNDERSCORE_PATTERN.match(net_name)
    if match:
        return (match.group(1), int(match.group(2)), "underscore")

    # Try numeric suffix (least specific, may have false positives)
    match = _NUMERIC_PATTERN.match(net_name)
    if match:
        return (match.group(1), int(match.group(2)), "numeric")

    return None


def detect_bus_signals(
    net_names: dict[int, str],
    min_bus_width: int = 2,
) -> list[BusSignal]:
    """Detect bus signals from net names.

    Args:
        net_names: Mapping of net ID to net name
        min_bus_width: Minimum number of signals to consider a bus (default: 2)

    Returns:
        List of detected BusSignal objects
    """
    signals: list[BusSignal] = []
    bus_counts: dict[str, int] = {}  # Count signals per bus name

    # First pass: parse all potential bus signals and count
    potential_signals: list[BusSignal] = []
    for net_id, net_name in net_names.items():
        parsed = parse_bus_signal(net_name)
        if parsed:
            bus_name, index, notation = parsed
            potential_signals.append(
                BusSignal(
                    net_name=net_name,
                    net_id=net_id,
                    bus_name=bus_name,
                    index=index,
                    notation=notation,
                )
            )
            bus_counts[bus_name] = bus_counts.get(bus_name, 0) + 1

    # Second pass: only include signals from buses with enough members
    for signal in potential_signals:
        if bus_counts[signal.bus_name] >= min_bus_width:
            signals.append(signal)

    return signals


def group_buses(
    signals: list[BusSignal],
    min_bus_width: int = 2,
) -> list[BusGroup]:
    """Group bus signals into bus groups.

    Args:
        signals: List of BusSignal objects
        min_bus_width: Minimum width to form a bus group

    Returns:
        List of BusGroup objects, sorted by bus name
    """
    # Group signals by bus name
    groups_dict: dict[str, list[BusSignal]] = {}
    for signal in signals:
        if signal.bus_name not in groups_dict:
            groups_dict[signal.bus_name] = []
        groups_dict[signal.bus_name].append(signal)

    # Create BusGroup objects
    groups: list[BusGroup] = []
    for bus_name, bus_signals in sorted(groups_dict.items()):
        if len(bus_signals) >= min_bus_width:
            # Sort signals by index
            sorted_signals = sorted(bus_signals, key=lambda s: s.index)
            groups.append(BusGroup(name=bus_name, signals=sorted_signals))

    return groups


def get_bus_routing_order(
    groups: list[BusGroup],
    mode: BusRoutingMode = BusRoutingMode.PARALLEL,
) -> list[list[int]]:
    """Get the routing order for bus signals.

    Returns a list of routing batches. In PARALLEL mode, each batch contains
    one signal from each bus to route simultaneously. In other modes, signals
    are routed sequentially within each bus.

    Args:
        groups: List of BusGroup objects
        mode: Routing mode

    Returns:
        List of batches, where each batch is a list of net IDs to route together
    """
    if mode == BusRoutingMode.PARALLEL:
        # Route signals at the same bit position together across buses
        # This promotes parallel trace alignment
        max_width = max((g.width for g in groups), default=0)
        batches: list[list[int]] = []

        for i in range(max_width):
            batch: list[int] = []
            for group in groups:
                if i < len(group.signals):
                    batch.append(group.signals[i].net_id)
            if batch:
                batches.append(batch)

        return batches

    else:
        # STACKED or BUNDLED: route each bus as a unit
        batches = []
        for group in groups:
            batches.append(group.get_net_ids())
        return batches


@dataclass
class BusRoutingConfig:
    """Configuration for bus routing.

    Attributes:
        enabled: Whether bus routing is enabled
        mode: Routing mode (parallel, stacked, bundled)
        spacing: Spacing between bus signals in mm (default: trace_width + clearance)
        min_bus_width: Minimum signals to consider a bus
        maintain_order: Keep signals in bit order during routing
    """

    enabled: bool = False
    mode: BusRoutingMode = BusRoutingMode.PARALLEL
    spacing: float | None = None  # None = auto (trace_width + clearance)
    min_bus_width: int = 2
    maintain_order: bool = True

    def get_spacing(self, trace_width: float, clearance: float) -> float:
        """Get the actual spacing value."""
        if self.spacing is not None:
            return self.spacing
        return trace_width + clearance


def analyze_buses(net_names: dict[int, str]) -> dict[str, any]:
    """Analyze net names to provide a bus detection summary.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        Dictionary with analysis results
    """
    signals = detect_bus_signals(net_names)
    groups = group_buses(signals)

    return {
        "total_signals": len(signals),
        "total_groups": len(groups),
        "groups": [
            {
                "name": str(g),
                "width": g.width,
                "complete": g.is_complete(),
                "signals": [s.net_name for s in g.signals],
            }
            for g in groups
        ],
        "non_bus_nets": [
            name
            for net_id, name in net_names.items()
            if not any(s.net_id == net_id for s in signals)
        ],
    }
