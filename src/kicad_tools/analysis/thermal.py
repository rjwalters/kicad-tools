"""Thermal analysis for PCB designs.

Analyzes thermal characteristics to identify hotspots, estimate power
dissipation, and suggest improvements for heat management.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis import ThermalAnalyzer
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = ThermalAnalyzer()
    >>> hotspots = analyzer.analyze(pcb)
    >>> for hotspot in hotspots:
    ...     print(f"{hotspot.severity}: {hotspot.total_power_w:.2f}W at {hotspot.position}")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB, Footprint, Zone


class ThermalSeverity(Enum):
    """Thermal severity level."""

    OK = "ok"
    WARM = "warm"
    HOT = "hot"
    CRITICAL = "critical"


# Patterns to identify heat-generating components
HEAT_SOURCE_PATTERNS = {
    # Voltage regulators (LDOs, switching regulators)
    "regulator": [
        r"(?i)^U\d+$",  # Generic IC - check value for regulator keywords
        r"(?i)LDO",
        r"(?i)REG",
        r"(?i)78\d{2}",  # 7805, 7812, etc.
        r"(?i)LM\d{4}",  # LM7805, LM1117, etc.
        r"(?i)AMS1117",
        r"(?i)AP\d{4}",  # AP2112, etc.
        r"(?i)MIC\d{4}",  # MIC5219, etc.
        r"(?i)TPS\d{4,5}",  # TPS62200, etc.
        r"(?i)LT\d{4}",  # LT1117, etc.
    ],
    # MOSFETs and transistors
    "mosfet": [
        r"(?i)^Q\d+$",  # Transistor designator
        r"(?i)MOSFET",
        r"(?i)FET",
        r"(?i)IRF\d+",
        r"(?i)IRLZ\d+",
        r"(?i)SI\d{4}",
        r"(?i)AO\d{4}",
        r"(?i)BSS\d+",
        r"(?i)2N\d{4}",
    ],
    # Power resistors
    "resistor": [
        r"(?i)^R\d+$",  # Resistor designator - check footprint for power
    ],
    # LEDs (high-power)
    "led": [
        r"(?i)^D\d+$",  # Diode designator - check if LED
        r"(?i)^LED\d*$",
    ],
    # Motor drivers, H-bridges
    "driver": [
        r"(?i)DRV\d+",
        r"(?i)L298",
        r"(?i)L293",
        r"(?i)TB\d{4}",
        r"(?i)A4988",
        r"(?i)TMC\d{4}",
    ],
    # Power ICs
    "power_ic": [
        r"(?i)^U\d+$",  # Check value for power-related keywords
    ],
}

# Typical power dissipation estimates by component type (in Watts)
TYPICAL_POWER = {
    "regulator_ldo": 0.5,  # LDO with 1V dropout at 500mA
    "regulator_switching": 0.2,  # Switching regulator losses
    "mosfet_low_side": 0.1,  # Low-side switch at 1A
    "mosfet_high_side": 0.15,  # High-side switch at 1A
    "resistor_0402": 0.0625,  # 1/16W
    "resistor_0603": 0.1,  # 1/10W
    "resistor_0805": 0.125,  # 1/8W
    "resistor_1206": 0.25,  # 1/4W
    "resistor_2512": 1.0,  # 1W
    "led_indicator": 0.02,  # 20mA indicator LED
    "led_power": 0.5,  # Power LED
    "driver_motor": 1.0,  # Motor driver
    "unknown": 0.1,  # Default estimate
}

# Thermal resistance estimates (°C/W) for common packages
THERMAL_RESISTANCE = {
    "SOT-23": 250.0,
    "SOT-223": 50.0,
    "TO-220": 5.0,
    "TO-252": 15.0,  # DPAK
    "TO-263": 10.0,  # D2PAK
    "QFN": 30.0,
    "SOIC-8": 100.0,
    "TSSOP": 120.0,
    "0402": 300.0,
    "0603": 250.0,
    "0805": 200.0,
    "1206": 150.0,
}


@dataclass
class ThermalSource:
    """Component that generates heat.

    Attributes:
        reference: Component reference designator.
        power_w: Estimated power dissipation in Watts.
        package: Package type (e.g., "SOT-223", "TO-220").
        thermal_resistance: Junction-to-ambient thermal resistance in °C/W.
        position: Component position (x, y) in mm.
        component_type: Type of component (regulator, mosfet, etc.).
        value: Component value string.
    """

    reference: str
    power_w: float
    package: str
    thermal_resistance: float | None
    position: tuple[float, float]
    component_type: str
    value: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "reference": self.reference,
            "power_w": round(self.power_w, 3),
            "package": self.package,
            "position": {"x": round(self.position[0], 2), "y": round(self.position[1], 2)},
            "component_type": self.component_type,
        }
        if self.thermal_resistance is not None:
            result["thermal_resistance_c_per_w"] = round(self.thermal_resistance, 1)
        if self.value:
            result["value"] = self.value
        return result


@dataclass
class ThermalHotspot:
    """Identified thermal concern on the board.

    Attributes:
        position: Center position (x, y) in mm.
        radius_mm: Radius of the thermal zone in mm.
        sources: Heat source components in this area.
        total_power_w: Total power dissipation in Watts.
        copper_area_mm2: Estimated copper area for heat spreading.
        via_count: Total vias in the area.
        thermal_vias: Number of vias suitable for thermal relief.
        severity: Thermal severity level.
        suggestions: Improvement suggestions.
        max_temp_rise_c: Estimated temperature rise in °C.
    """

    position: tuple[float, float]
    radius_mm: float

    # Heat sources
    sources: list[ThermalSource] = field(default_factory=list)
    total_power_w: float = 0.0

    # Thermal relief
    copper_area_mm2: float = 0.0
    via_count: int = 0
    thermal_vias: int = 0

    # Assessment
    severity: ThermalSeverity = ThermalSeverity.OK
    max_temp_rise_c: float = 0.0

    # Suggestions
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "position": {"x": round(self.position[0], 2), "y": round(self.position[1], 2)},
            "radius_mm": round(self.radius_mm, 2),
            "sources": [s.to_dict() for s in self.sources],
            "total_power_w": round(self.total_power_w, 3),
            "copper_area_mm2": round(self.copper_area_mm2, 1),
            "via_count": self.via_count,
            "thermal_vias": self.thermal_vias,
            "severity": self.severity.value,
            "max_temp_rise_c": round(self.max_temp_rise_c, 1),
            "suggestions": self.suggestions,
        }


class PowerEstimator:
    """Estimate power dissipation from component information.

    Uses component type, package, and value to estimate typical
    power dissipation for thermal analysis.
    """

    def estimate(self, footprint: Footprint, component_type: str) -> float:
        """Estimate power dissipation for a component.

        Args:
            footprint: Component footprint.
            component_type: Type classification (regulator, mosfet, etc.).

        Returns:
            Estimated power dissipation in Watts.
        """
        package = self._detect_package(footprint.name)
        value = footprint.value.upper()

        # Resistors: estimate from package power rating
        if component_type == "resistor":
            return self._estimate_resistor_power(package)

        # LEDs: check if high-power
        if component_type == "led":
            if self._is_power_led(footprint):
                return TYPICAL_POWER["led_power"]
            return TYPICAL_POWER["led_indicator"]

        # Regulators: check if LDO or switching
        if component_type == "regulator":
            if self._is_switching_regulator(value):
                return TYPICAL_POWER["regulator_switching"]
            return TYPICAL_POWER["regulator_ldo"]

        # MOSFETs
        if component_type == "mosfet":
            return TYPICAL_POWER["mosfet_low_side"]

        # Motor drivers
        if component_type == "driver":
            return TYPICAL_POWER["driver_motor"]

        return TYPICAL_POWER["unknown"]

    def _detect_package(self, library: str) -> str:
        """Detect package type from footprint library name."""
        lib_upper = library.upper()

        for package in THERMAL_RESISTANCE:
            if package.upper() in lib_upper:
                return package

        # Check for common patterns
        if "SOT-223" in lib_upper or "SOT223" in lib_upper:
            return "SOT-223"
        if "SOT-23" in lib_upper or "SOT23" in lib_upper:
            return "SOT-23"
        if "TO-220" in lib_upper or "TO220" in lib_upper:
            return "TO-220"
        if "QFN" in lib_upper:
            return "QFN"
        if "SOIC" in lib_upper:
            return "SOIC-8"

        # Resistor packages
        if "0402" in lib_upper:
            return "0402"
        if "0603" in lib_upper:
            return "0603"
        if "0805" in lib_upper:
            return "0805"
        if "1206" in lib_upper:
            return "1206"
        if "2512" in lib_upper:
            return "2512"

        return "unknown"

    def _estimate_resistor_power(self, package: str) -> float:
        """Estimate resistor power rating from package."""
        power_ratings = {
            "0402": 0.0625,
            "0603": 0.1,
            "0805": 0.125,
            "1206": 0.25,
            "2512": 1.0,
        }
        # Assume 50% of max rating as typical dissipation
        max_power = power_ratings.get(package, 0.1)
        return max_power * 0.5

    def _is_power_led(self, footprint: Footprint) -> bool:
        """Check if LED is a power LED based on footprint."""
        lib_upper = footprint.name.upper()
        # Power LEDs typically have larger packages
        return any(
            pattern in lib_upper for pattern in ["5050", "3535", "3030", "CREE", "OSRAM", "LUXEON"]
        )

    def _is_switching_regulator(self, value: str) -> bool:
        """Check if regulator is a switching type."""
        switching_patterns = [
            r"TPS6",
            r"LM26",
            r"LM34",
            r"MP\d{4}",
            r"RT\d{4}",
            r"SY\d{4}",
            r"AOZ\d{4}",
        ]
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in switching_patterns)

    def get_thermal_resistance(self, footprint: Footprint) -> float | None:
        """Get thermal resistance estimate for a package.

        Args:
            footprint: Component footprint.

        Returns:
            Thermal resistance in °C/W, or None if unknown.
        """
        package = self._detect_package(footprint.name)
        return THERMAL_RESISTANCE.get(package)


class ThermalAnalyzer:
    """Analyze thermal characteristics of PCB design.

    Identifies heat-generating components, clusters them into hotspots,
    analyzes thermal relief (copper pours, thermal vias), and provides
    improvement suggestions.

    Args:
        cluster_radius: Radius in mm for clustering heat sources.
        min_power_w: Minimum power to consider a source relevant.
    """

    def __init__(
        self,
        cluster_radius: float = 10.0,
        min_power_w: float = 0.05,
    ):
        """Initialize the thermal analyzer.

        Args:
            cluster_radius: Radius in mm for clustering heat sources.
            min_power_w: Minimum power threshold to include a source.
        """
        self.cluster_radius = cluster_radius
        self.min_power_w = min_power_w
        self._power_estimator = PowerEstimator()

    def analyze(self, board: PCB) -> list[ThermalHotspot]:
        """Find thermal hotspots and issues.

        Args:
            board: PCB object to analyze.

        Returns:
            List of thermal hotspots sorted by severity.
        """
        # Identify heat sources
        sources = self._identify_heat_sources(board)

        if not sources:
            return []

        # Cluster nearby sources
        clusters = self._cluster_sources(sources)

        # Analyze each cluster
        hotspots = []
        for cluster in clusters:
            hotspot = self._analyze_cluster(cluster, board)
            hotspot.suggestions = self._suggest_improvements(hotspot, board)
            hotspots.append(hotspot)

        # Sort by severity (critical first)
        severity_order = {
            ThermalSeverity.CRITICAL: 0,
            ThermalSeverity.HOT: 1,
            ThermalSeverity.WARM: 2,
            ThermalSeverity.OK: 3,
        }
        hotspots.sort(key=lambda h: severity_order[h.severity])

        return hotspots

    def _identify_heat_sources(self, board: PCB) -> list[ThermalSource]:
        """Identify components that generate heat.

        Args:
            board: PCB object to analyze.

        Returns:
            List of identified heat sources.
        """
        sources = []

        for fp in board.footprints:
            component_type = self._classify_component(fp)
            if component_type is None:
                continue

            power = self._power_estimator.estimate(fp, component_type)
            if power < self.min_power_w:
                continue

            package = self._power_estimator._detect_package(fp.name)
            thermal_r = self._power_estimator.get_thermal_resistance(fp)

            source = ThermalSource(
                reference=fp.reference,
                power_w=power,
                package=package,
                thermal_resistance=thermal_r,
                position=fp.position,
                component_type=component_type,
                value=fp.value,
            )
            sources.append(source)

        return sources

    def _classify_component(self, footprint: Footprint) -> str | None:
        """Classify component as heat source type.

        Args:
            footprint: Component footprint.

        Returns:
            Component type string or None if not a heat source.
        """
        ref = footprint.reference
        value = footprint.value

        # Check each category
        for comp_type, patterns in HEAT_SOURCE_PATTERNS.items():
            for pattern in patterns:
                # Check reference designator
                if re.match(pattern, ref):
                    # For generic designators (U, R, Q, D), verify by value
                    if comp_type == "regulator" and pattern == r"(?i)^U\d+$":
                        if self._is_regulator_by_value(value):
                            return "regulator"
                    elif comp_type == "power_ic" and pattern == r"(?i)^U\d+$":
                        continue  # Skip generic - handled by regulator check
                    elif comp_type == "resistor" and pattern == r"(?i)^R\d+$":
                        # All resistors are potential heat sources
                        return "resistor"
                    elif comp_type == "led" and pattern == r"(?i)^D\d+$":
                        if self._is_led_by_value(value, footprint):
                            return "led"
                    elif comp_type == "mosfet" and pattern == r"(?i)^Q\d+$":
                        if self._is_mosfet_by_value(value):
                            return "mosfet"
                    else:
                        return comp_type

                # Check value
                if re.search(pattern, value, re.IGNORECASE):
                    return comp_type

        return None

    def _is_regulator_by_value(self, value: str) -> bool:
        """Check if component value indicates a voltage regulator."""
        regulator_keywords = [
            r"78\d{2}",
            r"79\d{2}",
            r"LM\d{4}",
            r"AMS1117",
            r"LDO",
            r"REG",
            r"TPS",
            r"LT\d{4}",
            r"AP\d{4}",
            r"MIC\d{4}",
            r"XC\d{4}",
        ]
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in regulator_keywords)

    def _is_led_by_value(self, value: str, footprint: Footprint) -> bool:
        """Check if diode is an LED."""
        # Check value for LED indication
        if re.search(r"(?i)LED", value):
            return True
        # Check footprint name for LED
        if re.search(r"(?i)LED", footprint.name):
            return True
        return False

    def _is_mosfet_by_value(self, value: str) -> bool:
        """Check if transistor is a MOSFET."""
        mosfet_patterns = [
            r"(?i)IRF",
            r"(?i)IRLZ",
            r"(?i)SI\d{4}",
            r"(?i)AO\d{4}",
            r"(?i)FET",
            r"(?i)MOS",
        ]
        return any(re.search(pattern, value) for pattern in mosfet_patterns)

    def _cluster_sources(self, sources: list[ThermalSource]) -> list[list[ThermalSource]]:
        """Cluster nearby heat sources.

        Uses simple distance-based clustering to group heat sources
        that are close enough to interact thermally.

        Args:
            sources: List of heat sources to cluster.

        Returns:
            List of clusters (each cluster is a list of sources).
        """
        if not sources:
            return []

        # Simple greedy clustering
        clusters: list[list[ThermalSource]] = []
        assigned = set()

        for i, source in enumerate(sources):
            if i in assigned:
                continue

            # Start new cluster
            cluster = [source]
            assigned.add(i)

            # Find nearby sources
            for j, other in enumerate(sources):
                if j in assigned:
                    continue

                dist = math.sqrt(
                    (source.position[0] - other.position[0]) ** 2
                    + (source.position[1] - other.position[1]) ** 2
                )

                if dist <= self.cluster_radius:
                    cluster.append(other)
                    assigned.add(j)

            clusters.append(cluster)

        return clusters

    def _analyze_cluster(self, sources: list[ThermalSource], board: PCB) -> ThermalHotspot:
        """Analyze a cluster of heat sources.

        Args:
            sources: Heat sources in the cluster.
            board: PCB for context (zones, vias).

        Returns:
            ThermalHotspot analysis result.
        """
        # Calculate cluster center and bounds
        if len(sources) == 1:
            center = sources[0].position
            radius = 5.0  # Minimum radius around single component
        else:
            xs = [s.position[0] for s in sources]
            ys = [s.position[1] for s in sources]
            center = (sum(xs) / len(xs), sum(ys) / len(ys))

            # Radius to encompass all sources
            max_dist = max(
                math.sqrt((s.position[0] - center[0]) ** 2 + (s.position[1] - center[1]) ** 2)
                for s in sources
            )
            radius = max(max_dist + 2.0, 5.0)  # Add margin

        total_power = sum(s.power_w for s in sources)

        # Count vias in area
        via_count = 0
        thermal_vias = 0
        for via in board.vias:
            dist = math.sqrt(
                (via.position[0] - center[0]) ** 2 + (via.position[1] - center[1]) ** 2
            )
            if dist <= radius:
                via_count += 1
                # Thermal vias are typically smaller drill, many layers
                if via.drill <= 0.4 and len(via.layers) >= 2:
                    thermal_vias += 1

        # Estimate copper area from zones
        copper_area = self._estimate_copper_area(center, radius, board)

        # Calculate severity based on power density and thermal relief
        max_temp_rise = self._estimate_temp_rise(total_power, copper_area, thermal_vias)
        severity = self._classify_severity(max_temp_rise, total_power)

        return ThermalHotspot(
            position=center,
            radius_mm=radius,
            sources=sources,
            total_power_w=total_power,
            copper_area_mm2=copper_area,
            via_count=via_count,
            thermal_vias=thermal_vias,
            severity=severity,
            max_temp_rise_c=max_temp_rise,
        )

    def _estimate_copper_area(
        self, center: tuple[float, float], radius: float, board: PCB
    ) -> float:
        """Estimate copper pour area for heat spreading.

        Args:
            center: Center position of the thermal zone.
            radius: Radius of the thermal zone.
            board: PCB object.

        Returns:
            Estimated copper area in mm².
        """
        # Check if any zones (copper pours) cover this area
        copper_area = 0.0

        for zone in board.zones:
            # Simple check: if zone polygon overlaps with our area
            if self._zone_overlaps(zone, center, radius):
                # Estimate overlap area (simplified)
                zone_area = self._estimate_zone_area(zone)
                copper_area += min(zone_area, math.pi * radius * radius)

        # If no zones, estimate based on trace density (minimal copper)
        if copper_area == 0:
            # Count traces in area and estimate copper
            trace_length = 0.0
            for seg in board.segments:
                seg_center = (
                    (seg.start[0] + seg.end[0]) / 2,
                    (seg.start[1] + seg.end[1]) / 2,
                )
                dist = math.sqrt(
                    (seg_center[0] - center[0]) ** 2 + (seg_center[1] - center[1]) ** 2
                )
                if dist <= radius:
                    dx = seg.end[0] - seg.start[0]
                    dy = seg.end[1] - seg.start[1]
                    trace_length += math.sqrt(dx * dx + dy * dy)

            # Assume average trace width of 0.25mm
            copper_area = trace_length * 0.25

        return copper_area

    def _zone_overlaps(self, zone: Zone, center: tuple[float, float], radius: float) -> bool:
        """Check if a zone overlaps with a circular area.

        Args:
            zone: Zone to check.
            center: Center of the circular area.
            radius: Radius of the circular area.

        Returns:
            True if zone overlaps with the area.
        """
        if not zone.polygon:
            return False

        # Simple bounding box check
        xs = [p[0] for p in zone.polygon]
        ys = [p[1] for p in zone.polygon]

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        # Check if circle overlaps with bounding box
        closest_x = max(min_x, min(center[0], max_x))
        closest_y = max(min_y, min(center[1], max_y))

        dist = math.sqrt((closest_x - center[0]) ** 2 + (closest_y - center[1]) ** 2)
        return dist <= radius

    def _estimate_zone_area(self, zone: Zone) -> float:
        """Estimate zone area using shoelace formula.

        Args:
            zone: Zone to calculate area for.

        Returns:
            Area in mm².
        """
        if not zone.polygon or len(zone.polygon) < 3:
            return 0.0

        # Shoelace formula
        n = len(zone.polygon)
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += zone.polygon[i][0] * zone.polygon[j][1]
            area -= zone.polygon[j][0] * zone.polygon[i][1]

        return abs(area) / 2.0

    def _estimate_temp_rise(
        self, power_w: float, copper_area_mm2: float, thermal_vias: int
    ) -> float:
        """Estimate temperature rise above ambient.

        Uses simplified thermal model based on copper area and vias.

        Args:
            power_w: Total power dissipation.
            copper_area_mm2: Available copper area.
            thermal_vias: Number of thermal vias.

        Returns:
            Estimated temperature rise in °C.
        """
        if power_w <= 0:
            return 0.0

        # Base thermal resistance from copper area
        # Approximate: 50°C/W per cm² at low airflow
        if copper_area_mm2 > 0:
            thermal_r = 5000.0 / copper_area_mm2  # °C/W
        else:
            thermal_r = 200.0  # High resistance if no copper

        # Thermal vias reduce resistance
        # Each via reduces effective resistance by ~10%
        via_factor = 1.0 / (1.0 + 0.1 * thermal_vias)
        thermal_r *= via_factor

        # Minimum thermal resistance (can't be lower than package)
        thermal_r = max(thermal_r, 10.0)

        return power_w * thermal_r

    def _classify_severity(self, temp_rise: float, power_w: float) -> ThermalSeverity:
        """Classify thermal severity based on temperature rise.

        Args:
            temp_rise: Estimated temperature rise in °C.
            power_w: Total power dissipation.

        Returns:
            Severity classification.
        """
        # Also consider absolute power (even with good cooling, high power is concerning)
        if temp_rise > 60 or power_w > 2.0:
            return ThermalSeverity.CRITICAL
        if temp_rise > 40 or power_w > 1.0:
            return ThermalSeverity.HOT
        if temp_rise > 20 or power_w > 0.5:
            return ThermalSeverity.WARM
        return ThermalSeverity.OK

    def _suggest_improvements(self, hotspot: ThermalHotspot, board: PCB) -> list[str]:
        """Generate thermal improvement suggestions.

        Args:
            hotspot: Thermal hotspot to improve.
            board: PCB for context.

        Returns:
            List of actionable suggestions.
        """
        suggestions = []

        # Check thermal vias
        if hotspot.thermal_vias < 4 and hotspot.total_power_w > 0.2:
            main_source = max(hotspot.sources, key=lambda s: s.power_w)
            suggestions.append(
                f"Add thermal vias under {main_source.reference} "
                f"(currently {hotspot.thermal_vias}, recommend 4+ for {hotspot.total_power_w:.2f}W)"
            )

        # Check copper area
        min_copper = hotspot.total_power_w * 100  # ~100mm² per watt minimum
        if hotspot.copper_area_mm2 < min_copper:
            suggestions.append(
                f"Increase copper pour area for heat spreading "
                f"(current: {hotspot.copper_area_mm2:.0f}mm², recommend: {min_copper:.0f}mm²+)"
            )

        # Multiple heat sources clustered
        if len(hotspot.sources) > 1 and hotspot.total_power_w > 0.5:
            refs = ", ".join(s.reference for s in hotspot.sources[:3])
            if len(hotspot.sources) > 3:
                refs += f" (+{len(hotspot.sources) - 3} more)"
            suggestions.append(
                f"Consider separating heat sources ({refs}) to distribute thermal load"
            )

        # High power component without adequate cooling
        for source in hotspot.sources:
            if source.power_w > 0.5 and source.thermal_resistance:
                temp_rise = source.power_w * source.thermal_resistance
                if temp_rise > 50:
                    suggestions.append(
                        f"{source.reference} may exceed safe temperature "
                        f"(estimated +{temp_rise:.0f}°C rise) - consider heatsink or larger pad"
                    )

        # Package-specific suggestions
        for source in hotspot.sources:
            if source.package == "SOT-23" and source.power_w > 0.2:
                suggestions.append(
                    f"{source.reference} ({source.package}) has limited thermal capability - "
                    f"consider SOT-223 or larger package for {source.power_w:.2f}W"
                )

        return suggestions
