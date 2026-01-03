"""
Functional clustering detection for component placement.

Analyzes netlist to identify functionally-related component groups
that should be placed near each other during optimization.
"""

from __future__ import annotations

import re

from kicad_tools.optim.components import ClusterType, Component, FunctionalCluster

__all__ = ["detect_functional_clusters", "ClusterDetector"]


# Common bypass/decoupling capacitor values (in Farads, scientific notation)
BYPASS_CAP_VALUES = {
    "100nF",
    "100n",
    "0.1uF",
    "0.1u",
    "10nF",
    "10n",
    "1uF",
    "1u",
    "4.7uF",
    "4.7u",
    "10uF",
    "10u",
    "22uF",
    "22u",
    "100pF",
    "100p",
}

# Power pin name patterns (case-insensitive)
POWER_PIN_PATTERNS = [
    r"^V(CC|DD|SS|EE)",  # VCC, VDD, VSS, VEE
    r"^V(BAT|IN|OUT)",  # VBAT, VIN, VOUT
    r"^(AV|DV)(CC|DD)",  # AVCC, AVDD, DVCC, DVDD
    r"^V\d+V\d*",  # V3V3, V5, etc.
    r"^(\+|\-)?\d+V",  # +3V3, +5V, -12V
    r"^PWR",  # PWR, PWR_IN
    r"^VDDIO",  # VDDIO
]

# Ground net name patterns
GROUND_NET_PATTERNS = [
    r"^GND",
    r"^AGND",
    r"^DGND",
    r"^VSS",
    r"^VEE",
    r"^GROUND",
    r"^0V",
]

# Oscillator/crystal pin patterns
CRYSTAL_PIN_PATTERNS = [
    r"^X(TAL)?(IN|OUT|I|O)?[12]?$",
    r"^OSC(IN|OUT)?[12]?$",
    r"^CLK(IN|OUT)?$",
    r"^Y[12]$",
]

# ESD protection device patterns (reference prefix)
ESD_DEVICE_PREFIXES = {"D", "TVS", "ESD"}

# Connector reference prefixes
CONNECTOR_PREFIXES = {"J", "CN", "P", "USB", "CONN"}

# IC reference prefixes
IC_PREFIXES = {"U", "IC"}


def detect_functional_clusters(
    components: list[Component],
    include_power: bool = True,
    include_timing: bool = True,
    include_interface: bool = True,
    include_driver: bool = True,
) -> list[FunctionalCluster]:
    """
    Analyze components and detect functional clusters.

    Args:
        components: List of Component objects with pin/net information
        include_power: Detect power/bypass clusters
        include_timing: Detect crystal/oscillator clusters
        include_interface: Detect connector/ESD clusters
        include_driver: Detect driver clusters

    Returns:
        List of detected FunctionalCluster objects
    """
    detector = ClusterDetector(components)
    clusters = []

    if include_power:
        clusters.extend(detector.detect_power_clusters())

    if include_timing:
        clusters.extend(detector.detect_timing_clusters())

    if include_interface:
        clusters.extend(detector.detect_interface_clusters())

    if include_driver:
        clusters.extend(detector.detect_driver_clusters())

    return clusters


class ClusterDetector:
    """
    Detects functional component clusters from netlist analysis.

    Uses heuristics based on component types, pin names, net connections,
    and component values to identify groups that should be placed together.
    """

    def __init__(self, components: list[Component]):
        """
        Initialize detector with component list.

        Args:
            components: List of Component objects with pin and net info
        """
        self.components = components
        self._component_map: dict[str, Component] = {c.ref: c for c in components}
        self._net_to_pins: dict[int, list[tuple[str, str]]] = {}
        self._build_net_index()

    def _build_net_index(self):
        """Build index of nets to (component_ref, pin_number) tuples."""
        for comp in self.components:
            for pin in comp.pins:
                if pin.net > 0:  # Skip unconnected
                    if pin.net not in self._net_to_pins:
                        self._net_to_pins[pin.net] = []
                    self._net_to_pins[pin.net].append((comp.ref, pin.number))

    def _get_ref_prefix(self, ref: str) -> str:
        """Extract alphabetic prefix from reference designator."""
        return "".join(c for c in ref if c.isalpha()).upper()

    def _is_capacitor(self, ref: str) -> bool:
        """Check if component is a capacitor."""
        return self._get_ref_prefix(ref) == "C"

    def _is_ic(self, ref: str) -> bool:
        """Check if component is an IC."""
        prefix = self._get_ref_prefix(ref)
        return prefix in IC_PREFIXES

    def _is_connector(self, ref: str) -> bool:
        """Check if component is a connector."""
        prefix = self._get_ref_prefix(ref)
        return prefix in CONNECTOR_PREFIXES

    def _is_diode(self, ref: str) -> bool:
        """Check if component is a diode (including ESD protection)."""
        prefix = self._get_ref_prefix(ref)
        return prefix in ESD_DEVICE_PREFIXES or prefix == "D"

    def _is_resistor(self, ref: str) -> bool:
        """Check if component is a resistor."""
        return self._get_ref_prefix(ref) == "R"

    def _is_crystal(self, ref: str) -> bool:
        """Check if component is a crystal/oscillator."""
        prefix = self._get_ref_prefix(ref)
        return prefix in {"Y", "X", "XTAL"}

    def _is_power_pin(self, pin_name: str) -> bool:
        """Check if pin name matches power pin patterns."""
        name_upper = pin_name.upper()
        return any(re.match(pattern, name_upper, re.IGNORECASE) for pattern in POWER_PIN_PATTERNS)

    def _is_ground_net(self, net_name: str) -> bool:
        """Check if net name matches ground patterns."""
        name_upper = net_name.upper()
        return any(re.match(pattern, name_upper, re.IGNORECASE) for pattern in GROUND_NET_PATTERNS)

    def _is_crystal_pin(self, pin_name: str) -> bool:
        """Check if pin name matches crystal/oscillator patterns."""
        name_upper = pin_name.upper()
        return any(re.match(pattern, name_upper, re.IGNORECASE) for pattern in CRYSTAL_PIN_PATTERNS)

    def _components_on_net(self, net: int) -> list[str]:
        """Get all component refs connected to a net."""
        pins = self._net_to_pins.get(net, [])
        return list({ref for ref, _ in pins})

    def detect_power_clusters(self) -> list[FunctionalCluster]:
        """
        Detect power clusters (IC + bypass capacitors).

        Heuristic: Find capacitors connected between IC power pins and ground.
        """
        clusters: list[FunctionalCluster] = []
        processed_caps: set[str] = set()

        # Find all ICs
        ics = [c for c in self.components if self._is_ic(c.ref)]

        for ic in ics:
            bypass_caps: list[str] = []

            # Find power pins on this IC
            for pin in ic.pins:
                if not self._is_power_pin(pin.number) and not self._is_power_pin(pin.net_name):
                    continue

                # Find capacitors connected to this power net
                connected_refs = self._components_on_net(pin.net)
                for ref in connected_refs:
                    if ref == ic.ref:
                        continue
                    if not self._is_capacitor(ref):
                        continue
                    if ref in processed_caps:
                        continue

                    # Check if other pin of capacitor goes to ground
                    cap_comp = self._component_map.get(ref)
                    if cap_comp:
                        for cap_pin in cap_comp.pins:
                            if cap_pin.net != pin.net and self._is_ground_net(cap_pin.net_name):
                                bypass_caps.append(ref)
                                processed_caps.add(ref)
                                break

            if bypass_caps:
                cluster = FunctionalCluster(
                    cluster_type=ClusterType.POWER,
                    anchor=ic.ref,
                    members=bypass_caps,
                    max_distance_mm=3.0,  # Bypass caps should be very close
                )
                clusters.append(cluster)

        return clusters

    def detect_timing_clusters(self) -> list[FunctionalCluster]:
        """
        Detect timing clusters (crystal + load capacitors).

        Heuristic: Find crystals connected to IC oscillator pins with load caps to ground.
        """
        clusters: list[FunctionalCluster] = []

        # Find all crystals
        crystals = [c for c in self.components if self._is_crystal(c.ref)]

        for crystal in crystals:
            load_caps: list[str] = []
            anchor_ic: str | None = None

            # Find what's connected to crystal pins
            for pin in crystal.pins:
                connected_refs = self._components_on_net(pin.net)

                for ref in connected_refs:
                    if ref == crystal.ref:
                        continue

                    # Look for load capacitors (one pin to crystal, other to ground)
                    if self._is_capacitor(ref):
                        cap_comp = self._component_map.get(ref)
                        if cap_comp:
                            for cap_pin in cap_comp.pins:
                                if cap_pin.net != pin.net and self._is_ground_net(cap_pin.net_name):
                                    if ref not in load_caps:
                                        load_caps.append(ref)
                                    break

                    # Look for connected IC (the anchor)
                    if self._is_ic(ref) and anchor_ic is None:
                        anchor_ic = ref

            if anchor_ic:
                # Cluster includes crystal and load caps, anchored on IC
                members = [crystal.ref] + load_caps
                cluster = FunctionalCluster(
                    cluster_type=ClusterType.TIMING,
                    anchor=anchor_ic,
                    members=members,
                    max_distance_mm=5.0,
                )
                clusters.append(cluster)

        return clusters

    def detect_interface_clusters(self) -> list[FunctionalCluster]:
        """
        Detect interface clusters (connector + ESD protection + series resistors).

        Heuristic: Find ESD diodes and series resistors connected between
        connector pins and an IC.
        """
        clusters: list[FunctionalCluster] = []

        # Find all connectors
        connectors = [c for c in self.components if self._is_connector(c.ref)]

        for conn in connectors:
            esd_devices: list[str] = []
            series_resistors: list[str] = []

            # Check each connector pin
            for pin in conn.pins:
                connected_refs = self._components_on_net(pin.net)

                for ref in connected_refs:
                    if ref == conn.ref:
                        continue

                    # ESD devices connected to connector pins
                    if self._is_diode(ref) and ref not in esd_devices:
                        esd_devices.append(ref)

                    # Series resistors on signal lines
                    if self._is_resistor(ref) and ref not in series_resistors:
                        # Check if resistor is in series (has exactly 2 pins with different nets)
                        res_comp = self._component_map.get(ref)
                        if res_comp and len(res_comp.pins) == 2:
                            nets = {p.net for p in res_comp.pins}
                            if len(nets) == 2:  # Different nets on each pin = series
                                series_resistors.append(ref)

            members = esd_devices + series_resistors
            if members:
                cluster = FunctionalCluster(
                    cluster_type=ClusterType.INTERFACE,
                    anchor=conn.ref,
                    members=members,
                    max_distance_mm=8.0,  # Interface components can be slightly spread
                )
                clusters.append(cluster)

        return clusters

    def detect_driver_clusters(self) -> list[FunctionalCluster]:
        """
        Detect driver clusters (driver IC + gate resistors + flyback diodes).

        Heuristic: Find resistors and diodes connected to driver IC outputs.
        This is a simplified detection that looks for drivers with external components.
        """
        clusters: list[FunctionalCluster] = []

        # Find ICs that might be drivers (have output pins connected to R or D)
        for ic in (c for c in self.components if self._is_ic(c.ref)):
            gate_resistors: list[str] = []
            flyback_diodes: list[str] = []

            for pin in ic.pins:
                connected_refs = self._components_on_net(pin.net)

                for ref in connected_refs:
                    if ref == ic.ref:
                        continue

                    # Gate resistors
                    if self._is_resistor(ref) and ref not in gate_resistors:
                        gate_resistors.append(ref)

                    # Flyback diodes
                    if self._is_diode(ref) and ref not in flyback_diodes:
                        flyback_diodes.append(ref)

            # Only create cluster if we have both resistors and diodes
            # This is a heuristic to identify driver configurations
            if gate_resistors and flyback_diodes:
                members = gate_resistors + flyback_diodes
                cluster = FunctionalCluster(
                    cluster_type=ClusterType.DRIVER,
                    anchor=ic.ref,
                    members=members,
                    max_distance_mm=6.0,
                )
                clusters.append(cluster)

        return clusters
