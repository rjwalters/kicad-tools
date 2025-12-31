"""
Differential pair routing support for the autorouter.

This module provides:
- DifferentialSignal: Represents one signal (P or N) of a differential pair
- DifferentialPair: A pair of P/N signals with routing constraints
- detect_differential_pairs: Parse net names to identify differential pairs
- DifferentialPairConfig: Configuration for differential pair routing

Differential pairs are detected from common naming conventions:
- Plus/minus notation: USB_D+/USB_D-, ETH_TX+/ETH_TX-
- P/N suffix: HDMI_D0_P/HDMI_D0_N, USB3_TX_P/USB3_TX_N
- Positive/negative suffix: CLK_POS/CLK_NEG
"""

import re
from dataclasses import dataclass
from enum import Enum


class DifferentialPairType(Enum):
    """Known differential pair signal types with predefined rules."""

    USB2 = "usb2"
    USB3 = "usb3"
    ETHERNET = "ethernet"
    HDMI = "hdmi"
    LVDS = "lvds"
    CUSTOM = "custom"


@dataclass
class DifferentialPairRules:
    """Design rules for a specific differential pair type.

    Attributes:
        spacing: Target spacing between P and N traces in mm
        max_length_delta: Maximum allowed length difference in mm
        trace_width: Recommended trace width in mm
        impedance: Target differential impedance in ohms (for reference)
    """

    spacing: float
    max_length_delta: float
    trace_width: float = 0.2
    impedance: float = 90.0

    @classmethod
    def for_type(cls, pair_type: DifferentialPairType) -> "DifferentialPairRules":
        """Get predefined rules for a differential pair type."""
        rules_map = {
            DifferentialPairType.USB2: cls(
                spacing=0.2, max_length_delta=2.5, trace_width=0.2, impedance=90.0
            ),
            DifferentialPairType.USB3: cls(
                spacing=0.15, max_length_delta=0.5, trace_width=0.2, impedance=90.0
            ),
            DifferentialPairType.ETHERNET: cls(
                spacing=0.2, max_length_delta=2.0, trace_width=0.2, impedance=100.0
            ),
            DifferentialPairType.HDMI: cls(
                spacing=0.15, max_length_delta=0.5, trace_width=0.2, impedance=100.0
            ),
            DifferentialPairType.LVDS: cls(
                spacing=0.15, max_length_delta=0.5, trace_width=0.15, impedance=100.0
            ),
            DifferentialPairType.CUSTOM: cls(
                spacing=0.2, max_length_delta=1.0, trace_width=0.2, impedance=90.0
            ),
        }
        return rules_map.get(pair_type, rules_map[DifferentialPairType.CUSTOM])


@dataclass
class DifferentialSignal:
    """A signal that is part of a differential pair.

    Attributes:
        net_name: Original net name (e.g., "USB_D+")
        net_id: Net ID in the router
        base_name: Base name without polarity suffix (e.g., "USB_D")
        polarity: "P" for positive, "N" for negative
        notation: How the pair was named ("plus_minus", "pn_suffix", "pos_neg")
    """

    net_name: str
    net_id: int
    base_name: str
    polarity: str  # "P" or "N"
    notation: str  # "plus_minus", "pn_suffix", "pos_neg"

    def __hash__(self) -> int:
        return hash((self.net_id, self.base_name, self.polarity))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DifferentialSignal):
            return NotImplemented
        return (
            self.net_id == other.net_id
            and self.base_name == other.base_name
            and self.polarity == other.polarity
        )


@dataclass
class DifferentialPair:
    """A differential pair consisting of P and N signals.

    Attributes:
        name: Base name of the pair (e.g., "USB_D")
        positive: The positive signal (D+, _P, etc.)
        negative: The negative signal (D-, _N, etc.)
        pair_type: Detected or assigned pair type
        rules: Design rules for this pair
        routed_length_p: Length of routed positive trace (populated after routing)
        routed_length_n: Length of routed negative trace (populated after routing)
    """

    name: str
    positive: DifferentialSignal
    negative: DifferentialSignal
    pair_type: DifferentialPairType = DifferentialPairType.CUSTOM
    rules: DifferentialPairRules | None = None
    routed_length_p: float = 0.0
    routed_length_n: float = 0.0

    def __post_init__(self):
        if self.rules is None:
            self.rules = DifferentialPairRules.for_type(self.pair_type)

    @property
    def length_delta(self) -> float:
        """Get the length difference between P and N traces."""
        return abs(self.routed_length_p - self.routed_length_n)

    @property
    def is_length_matched(self) -> bool:
        """Check if the pair meets length matching requirements."""
        if self.rules is None:
            return True
        return self.length_delta <= self.rules.max_length_delta

    def get_net_ids(self) -> tuple[int, int]:
        """Get net IDs as (positive, negative) tuple."""
        return (self.positive.net_id, self.negative.net_id)

    def __str__(self) -> str:
        notation_map = {
            "plus_minus": f"{self.name}+/-",
            "pn_suffix": f"{self.name}_P/N",
            "pos_neg": f"{self.name}_POS/NEG",
        }
        return notation_map.get(self.positive.notation, self.name)


# =============================================================================
# REGEX PATTERNS FOR DIFFERENTIAL PAIR DETECTION
# =============================================================================

# Pattern 1: Plus/minus notation - USB_D+, ETH_TX-, etc.
# Matches the base name and the +/- suffix
_PLUS_MINUS_PATTERN = re.compile(r"^(.+)([+-])$")

# Pattern 2: P/N suffix notation - HDMI_D0_P, USB3_TX_N, etc.
# Also handles _DP/_DN (differential positive/negative)
_PN_SUFFIX_PATTERN = re.compile(r"^(.+)_([PN]|D[PN])$", re.IGNORECASE)

# Pattern 3: POS/NEG suffix notation - CLK_POS, CLK_NEG
_POS_NEG_PATTERN = re.compile(r"^(.+)_(POS|NEG)$", re.IGNORECASE)


def parse_differential_signal(net_name: str) -> tuple[str, str, str] | None:
    """Parse a net name to extract differential pair information.

    Args:
        net_name: The net name to parse

    Returns:
        Tuple of (base_name, polarity, notation) if this is a differential signal,
        None otherwise. polarity is "P" for positive, "N" for negative.
        notation is one of: "plus_minus", "pn_suffix", "pos_neg"
    """
    # Try plus/minus notation first (most common for USB, etc.)
    match = _PLUS_MINUS_PATTERN.match(net_name)
    if match:
        base_name = match.group(1)
        polarity = "P" if match.group(2) == "+" else "N"
        return (base_name, polarity, "plus_minus")

    # Try P/N suffix notation (HDMI, USB3, etc.)
    match = _PN_SUFFIX_PATTERN.match(net_name)
    if match:
        base_name = match.group(1)
        suffix = match.group(2).upper()
        # Handle _DP/_DN as well as _P/_N
        polarity = "P" if suffix in ("P", "DP") else "N"
        return (base_name, polarity, "pn_suffix")

    # Try POS/NEG suffix notation
    match = _POS_NEG_PATTERN.match(net_name)
    if match:
        base_name = match.group(1)
        polarity = "P" if match.group(2).upper() == "POS" else "N"
        return (base_name, polarity, "pos_neg")

    return None


def detect_differential_signals(
    net_names: dict[int, str],
) -> list[DifferentialSignal]:
    """Detect differential pair signals from net names.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        List of detected DifferentialSignal objects
    """
    signals: list[DifferentialSignal] = []

    for net_id, net_name in net_names.items():
        parsed = parse_differential_signal(net_name)
        if parsed:
            base_name, polarity, notation = parsed
            signals.append(
                DifferentialSignal(
                    net_name=net_name,
                    net_id=net_id,
                    base_name=base_name,
                    polarity=polarity,
                    notation=notation,
                )
            )

    return signals


def _detect_pair_type(base_name: str) -> DifferentialPairType:
    """Detect the differential pair type from the base name.

    Args:
        base_name: Base name of the differential pair

    Returns:
        Detected DifferentialPairType
    """
    name_upper = base_name.upper()

    # USB detection
    if "USB" in name_upper:
        if "USB3" in name_upper or "SS" in name_upper:  # SuperSpeed
            return DifferentialPairType.USB3
        return DifferentialPairType.USB2

    # Ethernet detection
    if any(eth in name_upper for eth in ["ETH", "ETHERNET", "RGMII", "SGMII", "MDI"]):
        return DifferentialPairType.ETHERNET

    # HDMI detection
    if "HDMI" in name_upper or "TMDS" in name_upper:
        return DifferentialPairType.HDMI

    # LVDS detection
    if "LVDS" in name_upper:
        return DifferentialPairType.LVDS

    return DifferentialPairType.CUSTOM


def group_differential_pairs(
    signals: list[DifferentialSignal],
) -> list[DifferentialPair]:
    """Group differential signals into pairs.

    Args:
        signals: List of DifferentialSignal objects

    Returns:
        List of DifferentialPair objects (only complete pairs)
    """
    # Group signals by base name
    by_base_name: dict[str, dict[str, DifferentialSignal]] = {}

    for signal in signals:
        if signal.base_name not in by_base_name:
            by_base_name[signal.base_name] = {}
        by_base_name[signal.base_name][signal.polarity] = signal

    # Create pairs where both P and N exist
    pairs: list[DifferentialPair] = []

    for base_name, polarity_map in sorted(by_base_name.items()):
        if "P" in polarity_map and "N" in polarity_map:
            pair_type = _detect_pair_type(base_name)
            pairs.append(
                DifferentialPair(
                    name=base_name,
                    positive=polarity_map["P"],
                    negative=polarity_map["N"],
                    pair_type=pair_type,
                    rules=DifferentialPairRules.for_type(pair_type),
                )
            )

    return pairs


def detect_differential_pairs(
    net_names: dict[int, str],
) -> list[DifferentialPair]:
    """Detect and group differential pairs from net names.

    This is a convenience function that combines detect_differential_signals
    and group_differential_pairs.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        List of complete DifferentialPair objects
    """
    signals = detect_differential_signals(net_names)
    return group_differential_pairs(signals)


@dataclass
class DifferentialPairConfig:
    """Configuration for differential pair routing.

    Attributes:
        enabled: Whether differential pair routing is enabled
        auto_detect: Automatically detect pairs from net names
        spacing: Override spacing for all pairs (None = use per-type defaults)
        max_length_delta: Override max length delta (None = use per-type defaults)
        add_serpentines: Add serpentine/meander for length matching
    """

    enabled: bool = False
    auto_detect: bool = True
    spacing: float | None = None
    max_length_delta: float | None = None
    add_serpentines: bool = True

    def get_rules(self, pair_type: DifferentialPairType) -> DifferentialPairRules:
        """Get rules with any config overrides applied."""
        base_rules = DifferentialPairRules.for_type(pair_type)
        return DifferentialPairRules(
            spacing=self.spacing if self.spacing is not None else base_rules.spacing,
            max_length_delta=(
                self.max_length_delta
                if self.max_length_delta is not None
                else base_rules.max_length_delta
            ),
            trace_width=base_rules.trace_width,
            impedance=base_rules.impedance,
        )


@dataclass
class LengthMismatchWarning:
    """Warning for length mismatch in a differential pair.

    Attributes:
        pair: The differential pair with length mismatch
        delta: Actual length difference in mm
        max_allowed: Maximum allowed difference in mm
    """

    pair: DifferentialPair
    delta: float
    max_allowed: float

    def __str__(self) -> str:
        return (
            f"Length mismatch in {self.pair.name}: "
            f"{self.delta:.3f}mm (max allowed: {self.max_allowed:.3f}mm)"
        )


def analyze_differential_pairs(net_names: dict[int, str]) -> dict[str, any]:
    """Analyze net names to provide a differential pair detection summary.

    Args:
        net_names: Mapping of net ID to net name

    Returns:
        Dictionary with analysis results
    """
    signals = detect_differential_signals(net_names)
    pairs = group_differential_pairs(signals)

    # Find unpaired signals
    paired_net_ids = set()
    for pair in pairs:
        paired_net_ids.add(pair.positive.net_id)
        paired_net_ids.add(pair.negative.net_id)

    unpaired_signals = [s for s in signals if s.net_id not in paired_net_ids]

    return {
        "total_pairs": len(pairs),
        "total_signals": len(signals),
        "unpaired_signals": len(unpaired_signals),
        "pairs": [
            {
                "name": str(pair),
                "base_name": pair.name,
                "type": pair.pair_type.value,
                "positive_net": pair.positive.net_name,
                "negative_net": pair.negative.net_name,
                "spacing": pair.rules.spacing if pair.rules else 0,
                "max_delta": pair.rules.max_length_delta if pair.rules else 0,
            }
            for pair in pairs
        ],
        "unpaired": [
            {
                "net_name": s.net_name,
                "polarity": s.polarity,
                "base_name": s.base_name,
            }
            for s in unpaired_signals
        ],
    }
