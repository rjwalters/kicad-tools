"""
Footprint Selector for Passive Components.

Provides intelligent footprint selection for passive components (capacitors, resistors,
inductors) based on their values. Different component values often require different
package sizes for optimal performance characteristics.

Supports configurable profiles:
- machine: Optimized for pick & place assembly (smaller packages)
- hand_solder: Larger packages easier to hand solder
- compact: Smallest packages that can handle the values
- default: Balanced profile for general use
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FootprintProfile(str, Enum):
    """Predefined footprint selection profiles."""

    DEFAULT = "default"
    MACHINE = "machine"
    HAND_SOLDER = "hand_solder"
    COMPACT = "compact"


@dataclass
class FootprintRule:
    """A rule mapping a value range to a footprint."""

    max_value: float  # Maximum value (in base units) for this footprint
    footprint: str  # KiCad footprint string


@dataclass
class ProfileRules:
    """Footprint selection rules for a specific profile."""

    capacitor_rules: list[FootprintRule] = field(default_factory=list)
    resistor_rules: list[FootprintRule] = field(default_factory=list)
    inductor_rules: list[FootprintRule] = field(default_factory=list)


# Default profiles with footprint mappings
# Values are in base units: Farads for capacitors, Ohms for resistors, Henries for inductors
# Note: Boundary values use 1.01x multiplier to handle floating-point precision
# (e.g., 100nF parsed as 100 * 1e-9 may be slightly > 1e-7 due to float representation)
DEFAULT_PROFILES: dict[str, ProfileRules] = {
    "default": ProfileRules(
        capacitor_rules=[
            FootprintRule(1.01e-7, "Capacitor_SMD:C_0402_1005Metric"),  # ≤100nF
            FootprintRule(1.01e-6, "Capacitor_SMD:C_0603_1608Metric"),  # ≤1µF
            FootprintRule(1.01e-5, "Capacitor_SMD:C_0805_2012Metric"),  # ≤10µF
            FootprintRule(float("inf"), "Capacitor_SMD:C_1206_3216Metric"),  # >10µF
        ],
        resistor_rules=[
            FootprintRule(1.01e4, "Resistor_SMD:R_0402_1005Metric"),  # ≤10k
            FootprintRule(float("inf"), "Resistor_SMD:R_0603_1608Metric"),  # >10k
        ],
        inductor_rules=[
            FootprintRule(1.01e-6, "Inductor_SMD:L_0603_1608Metric"),  # ≤1µH
            FootprintRule(1.01e-5, "Inductor_SMD:L_0805_2012Metric"),  # ≤10µH
            FootprintRule(float("inf"), "Inductor_SMD:L_1206_3216Metric"),  # >10µH
        ],
    ),
    "machine": ProfileRules(
        capacitor_rules=[
            FootprintRule(1.01e-6, "Capacitor_SMD:C_0402_1005Metric"),  # ≤1µF
            FootprintRule(1.01e-5, "Capacitor_SMD:C_0603_1608Metric"),  # ≤10µF
            FootprintRule(4.71e-5, "Capacitor_SMD:C_0805_2012Metric"),  # ≤47µF
            FootprintRule(float("inf"), "Capacitor_SMD:C_1206_3216Metric"),  # >47µF
        ],
        resistor_rules=[
            FootprintRule(float("inf"), "Resistor_SMD:R_0402_1005Metric"),  # All values
        ],
        inductor_rules=[
            FootprintRule(1.01e-6, "Inductor_SMD:L_0402_1005Metric"),  # ≤1µH
            FootprintRule(1.01e-5, "Inductor_SMD:L_0603_1608Metric"),  # ≤10µH
            FootprintRule(float("inf"), "Inductor_SMD:L_0805_2012Metric"),  # >10µH
        ],
    ),
    "hand_solder": ProfileRules(
        capacitor_rules=[
            FootprintRule(1.01e-7, "Capacitor_SMD:C_0603_1608Metric"),  # ≤100nF
            FootprintRule(1.01e-6, "Capacitor_SMD:C_0805_2012Metric"),  # ≤1µF
            FootprintRule(1.01e-5, "Capacitor_SMD:C_1206_3216Metric"),  # ≤10µF
            FootprintRule(float("inf"), "Capacitor_SMD:C_1210_3225Metric"),  # >10µF
        ],
        resistor_rules=[
            FootprintRule(1.01e4, "Resistor_SMD:R_0603_1608Metric"),  # ≤10k
            FootprintRule(float("inf"), "Resistor_SMD:R_0805_2012Metric"),  # >10k
        ],
        inductor_rules=[
            FootprintRule(1.01e-5, "Inductor_SMD:L_0805_2012Metric"),  # ≤10µH
            FootprintRule(float("inf"), "Inductor_SMD:L_1206_3216Metric"),  # >10µH
        ],
    ),
    "compact": ProfileRules(
        capacitor_rules=[
            FootprintRule(1.01e-5, "Capacitor_SMD:C_0402_1005Metric"),  # ≤10µF
            FootprintRule(4.71e-5, "Capacitor_SMD:C_0603_1608Metric"),  # ≤47µF
            FootprintRule(1.01e-4, "Capacitor_SMD:C_0805_2012Metric"),  # ≤100µF
            FootprintRule(float("inf"), "Capacitor_SMD:C_1206_3216Metric"),  # >100µF
        ],
        resistor_rules=[
            FootprintRule(float("inf"), "Resistor_SMD:R_0402_1005Metric"),  # All values
        ],
        inductor_rules=[
            FootprintRule(1.01e-5, "Inductor_SMD:L_0402_1005Metric"),  # ≤10µH
            FootprintRule(float("inf"), "Inductor_SMD:L_0603_1608Metric"),  # >10µH
        ],
    ),
}


class FootprintSelector:
    """
    Select appropriate footprints for passive components based on value.

    Supports capacitors, resistors, and inductors with configurable profiles
    for different assembly requirements.

    Example:
        selector = FootprintSelector(profile="hand_solder")

        # Get footprint for a capacitor
        fp = selector.select_capacitor_footprint("100nF")
        # Returns: "Capacitor_SMD:C_0603_1608Metric"

        # Get footprint for a resistor
        fp = selector.select_resistor_footprint("10k")
        # Returns: "Resistor_SMD:R_0603_1608Metric"
    """

    def __init__(
        self,
        profile: str = "default",
        custom_rules: dict[str, Any] | None = None,
    ):
        """
        Initialize the footprint selector.

        Args:
            profile: Profile name - one of "default", "machine", "hand_solder", "compact"
            custom_rules: Optional custom rules to override or extend default profiles.
                         Format: {"capacitor": {"0-100nF": "Footprint:Name", ...}, ...}
        """
        self.profile = profile
        self._rules = self._load_rules(profile, custom_rules)

    def _load_rules(self, profile: str, custom_rules: dict[str, Any] | None) -> ProfileRules:
        """Load rules for the specified profile, with optional custom overrides."""
        # Start with default profile if specified profile not found
        if profile in DEFAULT_PROFILES:
            rules = DEFAULT_PROFILES[profile]
        else:
            rules = DEFAULT_PROFILES["default"]

        # Apply custom rules if provided
        if custom_rules:
            rules = self._apply_custom_rules(rules, custom_rules)

        return rules

    def _apply_custom_rules(
        self, base_rules: ProfileRules, custom_rules: dict[str, Any]
    ) -> ProfileRules:
        """Apply custom rules on top of base rules."""
        capacitor_rules = list(base_rules.capacitor_rules)
        resistor_rules = list(base_rules.resistor_rules)
        inductor_rules = list(base_rules.inductor_rules)

        # Parse custom capacitor rules
        if "capacitor" in custom_rules:
            capacitor_rules = self._parse_custom_component_rules(
                custom_rules["capacitor"], "capacitor"
            )

        # Parse custom resistor rules
        if "resistor" in custom_rules:
            resistor_rules = self._parse_custom_component_rules(
                custom_rules["resistor"], "resistor"
            )

        # Parse custom inductor rules
        if "inductor" in custom_rules:
            inductor_rules = self._parse_custom_component_rules(
                custom_rules["inductor"], "inductor"
            )

        return ProfileRules(
            capacitor_rules=capacitor_rules,
            resistor_rules=resistor_rules,
            inductor_rules=inductor_rules,
        )

    def _parse_custom_component_rules(
        self, rules_dict: dict[str, str], component_type: str
    ) -> list[FootprintRule]:
        """
        Parse custom rules from config format to FootprintRule objects.

        Config format: {"0-100nF": "Footprint:Name", "100nF-1uF": "Other:Footprint", ...}
        """
        rules = []
        for range_str, footprint in rules_dict.items():
            # Parse range like "0-100nF" or "100nF-1uF" or "10uF+"
            if range_str.endswith("+"):
                # "10uF+" means > 10uF
                max_value = float("inf")
            elif "-" in range_str:
                # "100nF-1uF" - use the upper bound
                parts = range_str.split("-")
                if len(parts) == 2:
                    max_value = parse_component_value(parts[1], component_type)
                else:
                    continue
            else:
                # Single value - treat as max
                max_value = parse_component_value(range_str, component_type)

            rules.append(FootprintRule(max_value, footprint))

        # Sort by max_value for proper rule matching
        rules.sort(key=lambda r: r.max_value)
        return rules

    def select_capacitor_footprint(self, value: str, voltage: float | None = None) -> str:
        """
        Select footprint for a capacitor based on its value.

        Args:
            value: Capacitor value string (e.g., "100nF", "10uF", "4.7µF")
            voltage: Optional voltage rating (for future derating support)

        Returns:
            KiCad footprint string (e.g., "Capacitor_SMD:C_0603_1608Metric")
        """
        try:
            value_farads = parse_component_value(value, "capacitor")
            return self._select_from_rules(value_farads, self._rules.capacitor_rules)
        except ValueError:
            # Fallback to default footprint if value can't be parsed
            return "Capacitor_SMD:C_0603_1608Metric"

    def select_resistor_footprint(self, value: str, power: float | None = None) -> str:
        """
        Select footprint for a resistor based on its value.

        Args:
            value: Resistor value string (e.g., "10k", "4.7k", "100R")
            power: Optional power rating in watts (for future support)

        Returns:
            KiCad footprint string (e.g., "Resistor_SMD:R_0402_1005Metric")
        """
        try:
            value_ohms = parse_component_value(value, "resistor")
            return self._select_from_rules(value_ohms, self._rules.resistor_rules)
        except ValueError:
            # Fallback to default footprint if value can't be parsed
            return "Resistor_SMD:R_0603_1608Metric"

    def select_inductor_footprint(self, value: str, current: float | None = None) -> str:
        """
        Select footprint for an inductor based on its value.

        Args:
            value: Inductor value string (e.g., "10uH", "100nH", "4.7µH")
            current: Optional current rating in amps (for future support)

        Returns:
            KiCad footprint string (e.g., "Inductor_SMD:L_0603_1608Metric")
        """
        try:
            value_henries = parse_component_value(value, "inductor")
            return self._select_from_rules(value_henries, self._rules.inductor_rules)
        except ValueError:
            # Fallback to default footprint if value can't be parsed
            return "Inductor_SMD:L_0805_2012Metric"

    def select_footprint(
        self,
        lib_id: str,
        value: str,
    ) -> str | None:
        """
        Auto-select footprint based on component type and value.

        Determines component type from lib_id and selects appropriate footprint.

        Args:
            lib_id: KiCad library ID (e.g., "Device:C", "Device:R", "Device:L")
            value: Component value string

        Returns:
            Footprint string if component type is recognized, None otherwise
        """
        lib_id_lower = lib_id.lower()

        # Detect component type from lib_id
        if ":c" in lib_id_lower or lib_id_lower.endswith(":c"):
            return self.select_capacitor_footprint(value)
        elif ":r" in lib_id_lower or lib_id_lower.endswith(":r"):
            return self.select_resistor_footprint(value)
        elif ":l" in lib_id_lower or lib_id_lower.endswith(":l"):
            return self.select_inductor_footprint(value)

        # Check for common passive component symbols
        if "capacitor" in lib_id_lower or "cap" in lib_id_lower:
            return self.select_capacitor_footprint(value)
        elif "resistor" in lib_id_lower or "res" in lib_id_lower:
            return self.select_resistor_footprint(value)
        elif "inductor" in lib_id_lower or "ind" in lib_id_lower:
            return self.select_inductor_footprint(value)

        return None

    def _select_from_rules(self, value: float, rules: list[FootprintRule]) -> str:
        """Select footprint from rules based on value."""
        for rule in rules:
            if value <= rule.max_value:
                return rule.footprint

        # Fallback to last rule (should have inf max_value)
        if rules:
            return rules[-1].footprint

        raise ValueError("No rules defined")


def parse_component_value(value_str: str, component_type: str = "capacitor") -> float:
    """
    Parse a component value string to its base unit value.

    Supports various formats:
    - Capacitors: "100nF", "10uF", "4.7µF", "1pF", "0.1uF"
    - Resistors: "10k", "4.7k", "100R", "1M", "4R7" (inline decimal)
    - Inductors: "10uH", "100nH", "4.7µH", "1mH"

    Args:
        value_str: Value string to parse
        component_type: Type of component ("capacitor", "resistor", "inductor")

    Returns:
        Value in base units (Farads, Ohms, or Henries)

    Raises:
        ValueError: If the value cannot be parsed
    """
    value_str = value_str.strip()

    if component_type == "resistor":
        return _parse_resistance(value_str)
    elif component_type == "capacitor":
        return _parse_capacitance(value_str)
    elif component_type == "inductor":
        return _parse_inductance(value_str)
    else:
        raise ValueError(f"Unknown component type: {component_type}")


def _parse_capacitance(value_str: str) -> float:
    """Parse capacitance value to Farads."""
    value_str = value_str.strip().upper()

    # Replace common unicode characters
    value_str = value_str.replace("Μ", "U").replace("μ", "U")

    # Multiplier map for capacitors (to Farads)
    multipliers = {
        "F": 1,
        "MF": 1e-3,  # millifarads (rare)
        "UF": 1e-6,  # microfarads
        "NF": 1e-9,  # nanofarads
        "PF": 1e-12,  # picofarads
    }

    # Try to match value with unit
    match = re.match(r"^([\d.]+)\s*([A-Z]+)?$", value_str)
    if match:
        number = float(match.group(1))
        unit = match.group(2) or "F"

        if unit in multipliers:
            return number * multipliers[unit]

    raise ValueError(f"Cannot parse capacitance value: {value_str}")


def _parse_resistance(value_str: str) -> float:
    """Parse resistance value to Ohms."""
    value_str = value_str.strip().upper()

    # Handle inline decimal notation (e.g., "4R7" = 4.7 ohms, "4K7" = 4.7k)
    inline_match = re.match(r"^(\d+)([RKM])(\d+)$", value_str)
    if inline_match:
        whole = inline_match.group(1)
        unit = inline_match.group(2)
        decimal = inline_match.group(3)
        number = float(f"{whole}.{decimal}")

        if unit == "R":
            return number
        elif unit == "K":
            return number * 1000
        elif unit == "M":
            return number * 1_000_000

    # Standard notation (e.g., "10k", "4.7k", "100R")
    match = re.match(r"^([\d.]+)\s*([RKOM])?$", value_str)
    if match:
        number = float(match.group(1))
        unit = match.group(2) or "R"

        if unit == "R" or unit == "O":  # ohms
            return number
        elif unit == "K":
            return number * 1000
        elif unit == "M":
            return number * 1_000_000

    raise ValueError(f"Cannot parse resistance value: {value_str}")


def _parse_inductance(value_str: str) -> float:
    """Parse inductance value to Henries."""
    value_str = value_str.strip().upper()

    # Replace common unicode characters
    value_str = value_str.replace("Μ", "U").replace("μ", "U")

    # Multiplier map for inductors (to Henries)
    multipliers = {
        "H": 1,
        "MH": 1e-3,  # millihenries
        "UH": 1e-6,  # microhenries
        "NH": 1e-9,  # nanohenries
        "PH": 1e-12,  # picohenries (very rare)
    }

    # Try to match value with unit
    match = re.match(r"^([\d.]+)\s*([A-Z]+)?$", value_str)
    if match:
        number = float(match.group(1))
        unit = match.group(2) or "H"

        if unit in multipliers:
            return number * multipliers[unit]

    raise ValueError(f"Cannot parse inductance value: {value_str}")


# Module-level convenience function
_default_selector: FootprintSelector | None = None


def get_default_selector() -> FootprintSelector:
    """Get or create the default footprint selector."""
    global _default_selector
    if _default_selector is None:
        _default_selector = FootprintSelector()
    return _default_selector


def select_footprint_for_passive(
    lib_id: str,
    value: str,
    profile: str = "default",
) -> str | None:
    """
    Convenience function to select footprint for a passive component.

    Args:
        lib_id: KiCad library ID
        value: Component value string
        profile: Footprint profile to use

    Returns:
        Footprint string or None if not applicable
    """
    if profile == "default":
        selector = get_default_selector()
    else:
        selector = FootprintSelector(profile=profile)

    return selector.select_footprint(lib_id, value)
