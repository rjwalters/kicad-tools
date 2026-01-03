"""
Signal integrity awareness for placement optimization.

Provides net classification, signal integrity analysis, and placement hints
to help minimize trace lengths for high-speed signals and reduce crosstalk
risk between sensitive nets.

Example::

    from kicad_tools.optim.signal_integrity import (
        classify_nets,
        analyze_placement_for_si,
        get_si_score,
    )
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")

    # Classify nets by signal type
    classifications = classify_nets(pcb)

    # Analyze placement and get hints
    hints = analyze_placement_for_si(pcb, classifications)
    for hint in hints:
        print(f"[{hint.severity}] {hint.description}")

    # Get overall SI score
    score = get_si_score(pcb, classifications)
    print(f"Signal integrity score: {score:.1f}/100")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.optim.placement import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "SignalClass",
    "NetClassification",
    "SignalIntegrityHint",
    "classify_nets",
    "analyze_placement_for_si",
    "get_si_score",
    "add_si_constraints",
]


class SignalClass(Enum):
    """Signal classification for net types."""

    CLOCK = "clock"  # Clock signals - minimize length
    HIGH_SPEED_DATA = "high_speed_data"  # USB, SPI, I2C, etc.
    DIFFERENTIAL = "differential"  # Differential pairs
    ANALOG_SENSITIVE = "analog_sensitive"  # ADC inputs, analog references
    POWER = "power"  # Power rails
    GENERAL = "general"  # Everything else


@dataclass
class NetClassification:
    """Classification of a net by signal type with constraints."""

    net_name: str
    signal_class: SignalClass
    max_length_mm: float | None = None  # Target max trace length
    matched_group: str | None = None  # Group name for length matching
    keep_away_from: list[str] = field(default_factory=list)  # Nets to avoid crossing
    priority: int = 0  # Higher = more important (for optimizer weighting)

    @property
    def is_critical(self) -> bool:
        """Check if this net is considered critical for SI."""
        return self.signal_class in (
            SignalClass.CLOCK,
            SignalClass.HIGH_SPEED_DATA,
            SignalClass.DIFFERENTIAL,
            SignalClass.ANALOG_SENSITIVE,
        )


@dataclass
class SignalIntegrityHint:
    """A placement hint for signal integrity improvement."""

    hint_type: str
    severity: str  # "critical", "warning", "info"
    description: str
    affected_components: list[str]
    suggestion: str
    estimated_improvement: float | None = None  # Estimated improvement in mm

    def __str__(self) -> str:
        """Human-readable hint representation."""
        severity_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(
            self.severity, "⚪"
        )
        return f"{severity_icon} [{self.hint_type}] {self.description}\n   → {self.suggestion}"


# Net name patterns for auto-detection
_CLOCK_PATTERNS = [
    r".*CLK.*",
    r".*CLOCK.*",
    r".*XTAL.*",
    r".*OSC.*",
    r".*MCLK.*",
    r".*SCLK.*",
    r".*BCLK.*",
    r".*LRCLK.*",
    r".*WCLK.*",
    r".*PCLK.*",
    r".*FCLK.*",
    r".*HCLK.*",
    r".*SYSCLK.*",
]

_HIGH_SPEED_PATTERNS = [
    r".*USB_D[PM\+\-]?.*",
    r".*SPI_.*",
    r".*MISO.*",
    r".*MOSI.*",
    r".*SCK.*",
    r".*I2C_.*",
    r".*SDA.*",
    r".*SCL.*",
    r".*UART_.*",
    r".*TX[D]?$",
    r".*RX[D]?$",
    r".*ETH_.*",
    r".*RMII.*",
    r".*MDIO.*",
    r".*MDC.*",
    r".*JTAG.*",
    r".*TDI.*",
    r".*TDO.*",
    r".*TMS.*",
    r".*TCK.*",
    r".*SWDIO.*",
    r".*SWCLK.*",
    r".*QSPI.*",
    r".*SDIO.*",
    r".*SD_.*",
    r".*HDMI.*",
    r".*LVDS.*",
]

_DIFFERENTIAL_PATTERNS = [
    (r"(.*)_P$", r"\1_N"),  # name_P / name_N pairs
    (r"(.*)_DP$", r"\1_DM"),  # USB DP/DM
    (r"(.*)\+$", r"\1-"),  # name+ / name- pairs
    (r"(.*)_POS$", r"\1_NEG"),  # name_POS / name_NEG
]

_ANALOG_PATTERNS = [
    r".*ADC.*",
    r".*AIN.*",
    r".*VREF.*",
    r".*AREF.*",
    r".*ANALOG.*",
    r".*SENSE.*",
    r".*FB$",  # Feedback
    r".*ISENSE.*",
    r".*VSENSE.*",
    r".*TEMP.*",
    r".*NTC.*",
    r".*THERMISTOR.*",
]

_POWER_PATTERNS = [
    r"^VCC.*",
    r"^VDD.*",
    r"^VSS.*",
    r"^GND.*",
    r"^V[0-9]+V?[0-9]*$",  # V3V3, V5, V12, etc.
    r"^\+[0-9]+V?.*",  # +3V3, +5V, +12V
    r"^\-[0-9]+V?.*",  # -5V, -12V
    r"^VBAT.*",
    r"^VIN.*",
    r"^VOUT.*",
    r"^AVCC.*",
    r"^AVDD.*",
    r"^DVCC.*",
    r"^DVDD.*",
    r"^PWR.*",
    r"^POWER.*",
]


def _match_patterns(name: str, patterns: list[str]) -> bool:
    """Check if name matches any pattern (case-insensitive)."""
    name_upper = name.upper()
    return any(re.match(pattern, name_upper, re.IGNORECASE) for pattern in patterns)


def _find_differential_pair(name: str, all_net_names: set[str]) -> str | None:
    """Find differential pair partner for a net name."""
    name_upper = name.upper()
    for pattern, complement in _DIFFERENTIAL_PATTERNS:
        match = re.match(pattern, name_upper, re.IGNORECASE)
        if match:
            # Construct the complement name
            complement_name = re.sub(pattern, complement, name_upper, flags=re.IGNORECASE)
            # Check if complement exists
            for net in all_net_names:
                if net.upper() == complement_name:
                    return net
    return None


def _get_max_length_for_class(signal_class: SignalClass) -> float | None:
    """Get recommended max trace length for signal class."""
    defaults = {
        SignalClass.CLOCK: 50.0,  # 50mm max for clocks
        SignalClass.HIGH_SPEED_DATA: 100.0,  # 100mm for high-speed data
        SignalClass.DIFFERENTIAL: 75.0,  # 75mm for diff pairs
        SignalClass.ANALOG_SENSITIVE: 25.0,  # 25mm for analog (minimize noise)
        SignalClass.POWER: None,  # Power routing is different
        SignalClass.GENERAL: None,  # No constraint
    }
    return defaults.get(signal_class)


def _get_priority_for_class(signal_class: SignalClass) -> int:
    """Get optimizer priority for signal class (higher = more important)."""
    priorities = {
        SignalClass.CLOCK: 100,
        SignalClass.DIFFERENTIAL: 90,
        SignalClass.HIGH_SPEED_DATA: 80,
        SignalClass.ANALOG_SENSITIVE: 70,
        SignalClass.POWER: 30,
        SignalClass.GENERAL: 10,
    }
    return priorities.get(signal_class, 10)


def classify_nets(pcb: PCB) -> dict[str, NetClassification]:
    """
    Classify all nets in a PCB by signal type.

    Uses heuristics based on net names to auto-detect signal types:
    - Clock: CLK, XTAL, OSC, etc.
    - High-speed: USB, SPI, UART, I2C, etc.
    - Differential: Pairs like USB_DP/USB_DM, _P/_N
    - Analog: ADC, AIN, VREF, etc.
    - Power: VCC, VDD, GND, etc.

    Args:
        pcb: Loaded PCB object

    Returns:
        Dictionary mapping net names to their classifications
    """
    classifications: dict[str, NetClassification] = {}

    # Get all unique net names from the PCB
    net_names: set[str] = set()
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name:
                net_names.add(pad.net_name)

    # First pass: identify all nets except differential (need to check pairs)
    for net_name in net_names:
        if not net_name:
            continue

        signal_class = SignalClass.GENERAL
        keep_away: list[str] = []

        # Check patterns in priority order
        if _match_patterns(net_name, _CLOCK_PATTERNS):
            signal_class = SignalClass.CLOCK
            # Clocks should stay away from analog
            keep_away = [n for n in net_names if _match_patterns(n, _ANALOG_PATTERNS)]
        elif _match_patterns(net_name, _ANALOG_PATTERNS):
            signal_class = SignalClass.ANALOG_SENSITIVE
            # Analog should stay away from clocks and high-speed
            keep_away = [
                n
                for n in net_names
                if _match_patterns(n, _CLOCK_PATTERNS)
                or _match_patterns(n, _HIGH_SPEED_PATTERNS)
            ]
        elif _match_patterns(net_name, _HIGH_SPEED_PATTERNS):
            signal_class = SignalClass.HIGH_SPEED_DATA
        elif _match_patterns(net_name, _POWER_PATTERNS):
            signal_class = SignalClass.POWER

        classifications[net_name] = NetClassification(
            net_name=net_name,
            signal_class=signal_class,
            max_length_mm=_get_max_length_for_class(signal_class),
            keep_away_from=keep_away,
            priority=_get_priority_for_class(signal_class),
        )

    # Second pass: identify differential pairs
    processed_pairs: set[str] = set()
    for net_name in net_names:
        if net_name in processed_pairs:
            continue

        pair_name = _find_differential_pair(net_name, net_names)
        if pair_name and pair_name not in processed_pairs:
            # Found a differential pair
            # Generate group name (remove the +/- or P/N suffix)
            group_name = re.sub(r"[_]?[PN\+\-]$", "", net_name, flags=re.IGNORECASE)
            group_name = re.sub(r"[_]?(DP|DM|POS|NEG)$", "", group_name, flags=re.IGNORECASE)

            for name in [net_name, pair_name]:
                classifications[name] = NetClassification(
                    net_name=name,
                    signal_class=SignalClass.DIFFERENTIAL,
                    max_length_mm=_get_max_length_for_class(SignalClass.DIFFERENTIAL),
                    matched_group=group_name,
                    priority=_get_priority_for_class(SignalClass.DIFFERENTIAL),
                )
                processed_pairs.add(name)

    return classifications


def _compute_net_length(
    pcb: PCB, net_name: str, optimizer: PlacementOptimizer | None = None
) -> float:
    """
    Compute estimated length for a net based on pin positions.

    Uses Manhattan distance between pins as an approximation.
    If an optimizer is provided, uses its component positions.
    """
    # Collect all pin positions for this net
    pins: list[tuple[float, float]] = []

    if optimizer:
        # Use optimizer's current positions
        for comp in optimizer.components:
            for pin in comp.pins:
                if pin.net_name == net_name:
                    pins.append((pin.x, pin.y))
    else:
        # Use PCB footprint positions
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_name == net_name:
                    # Compute absolute pad position
                    px = fp.position[0] + pad.position[0]
                    py = fp.position[1] + pad.position[1]
                    pins.append((px, py))

    if len(pins) < 2:
        return 0.0

    # Compute minimum spanning tree length (approximation)
    # Use simple star topology from centroid
    cx = sum(p[0] for p in pins) / len(pins)
    cy = sum(p[1] for p in pins) / len(pins)

    total_length = 0.0
    for px, py in pins:
        # Manhattan distance
        total_length += abs(px - cx) + abs(py - cy)

    return total_length


def _get_component_distance(
    pcb: PCB, ref1: str, ref2: str, optimizer: PlacementOptimizer | None = None
) -> float:
    """Get distance between two components."""
    if optimizer:
        comp1 = optimizer.get_component(ref1)
        comp2 = optimizer.get_component(ref2)
        if comp1 and comp2:
            dx = comp1.x - comp2.x
            dy = comp1.y - comp2.y
            return math.sqrt(dx * dx + dy * dy)
        return float("inf")

    # Use PCB footprint positions
    pos1 = pos2 = None
    for fp in pcb.footprints:
        if fp.reference == ref1:
            pos1 = fp.position
        elif fp.reference == ref2:
            pos2 = fp.position

    if pos1 and pos2:
        dx = pos1[0] - pos2[0]
        dy = pos1[1] - pos2[1]
        return math.sqrt(dx * dx + dy * dy)
    return float("inf")


def analyze_placement_for_si(
    pcb: PCB,
    classifications: dict[str, NetClassification] | None = None,
    optimizer: PlacementOptimizer | None = None,
) -> list[SignalIntegrityHint]:
    """
    Analyze current placement and generate signal integrity hints.

    Args:
        pcb: Loaded PCB object
        classifications: Net classifications (will be computed if not provided)
        optimizer: Optional optimizer with current positions

    Returns:
        List of SignalIntegrityHint objects with suggestions
    """
    if classifications is None:
        classifications = classify_nets(pcb)

    hints: list[SignalIntegrityHint] = []

    # Build mapping of components to their critical nets
    comp_critical_nets: dict[str, list[str]] = {}
    net_to_components: dict[str, list[str]] = {}

    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name:
                # Track which components have which nets
                if fp.reference not in comp_critical_nets:
                    comp_critical_nets[fp.reference] = []
                classification = classifications.get(pad.net_name)
                if classification and classification.is_critical:
                    if pad.net_name not in comp_critical_nets[fp.reference]:
                        comp_critical_nets[fp.reference].append(pad.net_name)

                # Track which nets connect which components
                if pad.net_name not in net_to_components:
                    net_to_components[pad.net_name] = []
                if fp.reference not in net_to_components[pad.net_name]:
                    net_to_components[pad.net_name].append(fp.reference)

    # Check 1: Net length violations
    for net_name, classification in classifications.items():
        if classification.max_length_mm is None:
            continue

        current_length = _compute_net_length(pcb, net_name, optimizer)
        if current_length > classification.max_length_mm:
            components = net_to_components.get(net_name, [])
            excess = current_length - classification.max_length_mm

            severity = "critical" if excess > classification.max_length_mm * 0.5 else "warning"

            hints.append(
                SignalIntegrityHint(
                    hint_type="net_length",
                    severity=severity,
                    description=f"Net '{net_name}' ({classification.signal_class.value}) "
                    f"is {current_length:.1f}mm, exceeds target {classification.max_length_mm:.1f}mm",
                    affected_components=components,
                    suggestion=f"Move components {', '.join(components[:3])} "
                    f"closer together to reduce trace length by {excess:.1f}mm",
                    estimated_improvement=excess,
                )
            )

    # Check 2: Differential pair length mismatch
    diff_pairs: dict[str, list[str]] = {}
    for net_name, classification in classifications.items():
        if classification.signal_class == SignalClass.DIFFERENTIAL and classification.matched_group:
            group = classification.matched_group
            if group not in diff_pairs:
                diff_pairs[group] = []
            diff_pairs[group].append(net_name)

    for group, nets in diff_pairs.items():
        if len(nets) != 2:
            continue

        len1 = _compute_net_length(pcb, nets[0], optimizer)
        len2 = _compute_net_length(pcb, nets[1], optimizer)
        mismatch = abs(len1 - len2)

        # Mismatch > 5mm is concerning for high-speed diff pairs
        if mismatch > 5.0:
            all_comps = []
            for net in nets:
                all_comps.extend(net_to_components.get(net, []))
            all_comps = list(set(all_comps))

            hints.append(
                SignalIntegrityHint(
                    hint_type="diff_pair_mismatch",
                    severity="warning" if mismatch < 10.0 else "critical",
                    description=f"Differential pair '{group}' has {mismatch:.1f}mm length mismatch "
                    f"({nets[0]}={len1:.1f}mm, {nets[1]}={len2:.1f}mm)",
                    affected_components=all_comps,
                    suggestion="Adjust placement to balance trace lengths within 2mm",
                    estimated_improvement=mismatch - 2.0,
                )
            )

    # Check 3: Clock near analog (crosstalk risk)
    clock_nets = [n for n, c in classifications.items() if c.signal_class == SignalClass.CLOCK]
    analog_nets = [
        n for n, c in classifications.items() if c.signal_class == SignalClass.ANALOG_SENSITIVE
    ]

    for clock_net in clock_nets:
        clock_comps = set(net_to_components.get(clock_net, []))
        for analog_net in analog_nets:
            analog_comps = set(net_to_components.get(analog_net, []))

            # Check if any clock component is close to analog component
            for c_comp in clock_comps:
                for a_comp in analog_comps:
                    if c_comp == a_comp:
                        continue  # Same component, skip

                    distance = _get_component_distance(pcb, c_comp, a_comp, optimizer)
                    if distance < 10.0:  # Less than 10mm is risky
                        hints.append(
                            SignalIntegrityHint(
                                hint_type="crosstalk_risk",
                                severity="warning",
                                description=f"Clock net '{clock_net}' component {c_comp} is only "
                                f"{distance:.1f}mm from analog net '{analog_net}' component {a_comp}",
                                affected_components=[c_comp, a_comp],
                                suggestion="Increase separation to at least 15mm to reduce crosstalk",
                                estimated_improvement=15.0 - distance,
                            )
                        )

    # Check 4: Crystal/oscillator placement
    # Crystals should be very close to their driving IC
    for fp in pcb.footprints:
        ref_prefix = "".join(c for c in fp.reference if c.isalpha())
        if ref_prefix in ("Y", "X"):  # Common crystal designators
            # Find what this crystal connects to
            for pad in fp.pads:
                if pad.net_name and _match_patterns(pad.net_name, _CLOCK_PATTERNS):
                    connected_comps = [
                        c for c in net_to_components.get(pad.net_name, []) if c != fp.reference
                    ]
                    for comp_ref in connected_comps:
                        distance = _get_component_distance(pcb, fp.reference, comp_ref, optimizer)
                        if distance > 15.0:  # Crystal should be within 15mm
                            hints.append(
                                SignalIntegrityHint(
                                    hint_type="crystal_placement",
                                    severity="critical",
                                    description=f"Crystal {fp.reference} is {distance:.1f}mm from "
                                    f"connected IC {comp_ref}",
                                    affected_components=[fp.reference, comp_ref],
                                    suggestion=f"Move {fp.reference} within 10mm of {comp_ref} "
                                    f"to minimize clock trace length and noise pickup",
                                    estimated_improvement=distance - 10.0,
                                )
                            )
                    break  # Only check first clock net

    # Sort hints by severity (critical first) then by improvement potential
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    hints.sort(
        key=lambda h: (
            severity_order.get(h.severity, 3),
            -(h.estimated_improvement or 0),
        )
    )

    return hints


def get_si_score(
    pcb: PCB,
    classifications: dict[str, NetClassification] | None = None,
    optimizer: PlacementOptimizer | None = None,
) -> float:
    """
    Score placement for signal integrity (0-100).

    Higher scores indicate better signal integrity potential.

    Scoring components:
    - Net length compliance (40 points)
    - Differential pair matching (20 points)
    - Crosstalk separation (20 points)
    - Crystal placement (20 points)

    Args:
        pcb: Loaded PCB object
        classifications: Net classifications (computed if not provided)
        optimizer: Optional optimizer with current positions

    Returns:
        SI score from 0 (poor) to 100 (excellent)
    """
    if classifications is None:
        classifications = classify_nets(pcb)

    score = 100.0

    # Get hints to assess issues
    hints = analyze_placement_for_si(pcb, classifications, optimizer)

    # Deduct points based on hint severity
    severity_penalty = {
        "critical": 15.0,
        "warning": 5.0,
        "info": 1.0,
    }

    for hint in hints:
        penalty = severity_penalty.get(hint.severity, 0)
        score -= penalty

    # Ensure score is within bounds
    return max(0.0, min(100.0, score))


def add_si_constraints(
    optimizer: PlacementOptimizer,
    classifications: dict[str, NetClassification],
) -> int:
    """
    Add signal integrity constraints to optimizer.

    Modifies spring stiffnesses based on signal classifications:
    - Clock nets get higher stiffness (shorter traces)
    - High-speed nets get higher stiffness
    - Differential pairs get matched stiffness

    Args:
        optimizer: PlacementOptimizer instance to modify
        classifications: Net classifications from classify_nets()

    Returns:
        Number of springs modified
    """
    modified = 0

    # Stiffness multipliers by signal class
    stiffness_multipliers = {
        SignalClass.CLOCK: 3.0,  # Strong pull for clock nets
        SignalClass.DIFFERENTIAL: 2.5,  # Strong for diff pairs
        SignalClass.HIGH_SPEED_DATA: 2.0,  # Moderate for high-speed
        SignalClass.ANALOG_SENSITIVE: 1.5,  # Some priority for analog
        SignalClass.POWER: 0.5,  # Lower for power (wider traces OK)
        SignalClass.GENERAL: 1.0,  # Default
    }

    for spring in optimizer.springs:
        if not spring.net_name:
            continue

        classification = classifications.get(spring.net_name)
        if classification:
            multiplier = stiffness_multipliers.get(classification.signal_class, 1.0)
            if multiplier != 1.0:
                spring.stiffness *= multiplier
                modified += 1

    return modified
