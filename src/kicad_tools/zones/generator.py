"""
Zone generator for creating copper pour zones on PCBs.

This module provides a high-level API for generating copper pour zones,
with automatic board outline detection and sensible defaults for power nets.
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import SExp, parse_file
from kicad_tools.sexp.builders import zone_node


@dataclass
class ZoneConfig:
    """Configuration for a copper pour zone.

    Attributes:
        net: Net name (e.g., "GND", "+3.3V")
        layer: Copper layer (e.g., "B.Cu", "F.Cu")
        priority: Zone fill priority (higher = fills later, on top)
        clearance: Clearance to other nets in mm
        min_thickness: Minimum copper thickness in mm
        thermal_gap: Thermal relief gap in mm
        thermal_bridge_width: Thermal relief spoke width in mm
        boundary: Custom boundary polygon, or None for board outline
    """

    net: str
    layer: str
    priority: int = 0
    clearance: float = 0.3
    min_thickness: float = 0.25
    thermal_gap: float = 0.3
    thermal_bridge_width: float = 0.4
    boundary: list[tuple[float, float]] | None = None


@dataclass
class GeneratedZone:
    """A generated zone ready for insertion.

    Attributes:
        config: The zone configuration used
        net_number: Resolved net number
        boundary: The actual boundary polygon used
        uuid: Generated UUID for the zone
    """

    config: ZoneConfig
    net_number: int
    boundary: list[tuple[float, float]]
    uuid: str = field(default_factory=lambda: str(uuid_module.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression node for this zone."""
        return zone_node(
            self.net_number,
            self.config.net,
            self.config.layer,
            self.boundary,
            self.uuid,
            self.config.priority,
            self.config.min_thickness,
            self.config.clearance,
            self.config.thermal_gap,
            self.config.thermal_bridge_width,
        )


class ZoneGenerator:
    """High-level zone generator for PCB copper pours.

    Provides an easy-to-use API for adding zones to PCB files with:
    - Automatic board outline detection for zone boundaries
    - Net name to net number resolution
    - Sensible defaults for power net zones (thermal relief, etc.)

    Example::

        gen = ZoneGenerator.from_pcb("board.kicad_pcb")

        # Add ground plane using board outline as boundary
        gen.add_zone(net="GND", layer="B.Cu", priority=1)

        # Add power plane with lower priority
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)

        # Generate zones and save
        gen.save("board_with_zones.kicad_pcb")
    """

    def __init__(self, pcb: PCB, doc: SExp | None = None):
        """Initialize zone generator.

        Args:
            pcb: Parsed PCB object
            doc: Raw S-expression document (for modification)
        """
        self._pcb = pcb
        self._doc = doc
        self._zones: list[GeneratedZone] = []
        self._board_outline: list[tuple[float, float]] | None = None

    @classmethod
    def from_pcb(cls, path: str | Path) -> ZoneGenerator:
        """Load PCB and create zone generator.

        Args:
            path: Path to .kicad_pcb file

        Returns:
            ZoneGenerator instance
        """
        path = Path(path)
        pcb = PCB.load(str(path))
        doc = parse_file(path)
        return cls(pcb, doc)

    @property
    def pcb(self) -> PCB:
        """The loaded PCB object."""
        return self._pcb

    @property
    def board_outline(self) -> list[tuple[float, float]]:
        """Get board outline polygon.

        Uses cached outline if available, otherwise extracts from PCB.
        Falls back to a default rectangle if no Edge.Cuts layer found.
        """
        if self._board_outline is None:
            outline = self._pcb.get_board_outline()
            if outline:
                self._board_outline = outline
            else:
                # Fallback: create outline from board bounds
                self._board_outline = self._estimate_board_bounds()
        return self._board_outline

    def _estimate_board_bounds(self) -> list[tuple[float, float]]:
        """Estimate board bounds from component positions.

        Used as fallback when no Edge.Cuts outline is found.
        """
        min_x, min_y = float("inf"), float("inf")
        max_x, max_y = float("-inf"), float("-inf")

        for fp in self._pcb.footprints:
            x, y = fp.position
            # Add some padding around components
            min_x = min(min_x, x - 5)
            min_y = min(min_y, y - 5)
            max_x = max(max_x, x + 5)
            max_y = max(max_y, y + 5)

        # Ensure we have valid bounds
        if min_x == float("inf"):
            # No footprints, use a default size
            return [(0, 0), (100, 0), (100, 100), (0, 100)]

        return [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]

    def get_net_number(self, net_name: str) -> int:
        """Get net number by name.

        Args:
            net_name: Net name (e.g., "GND", "+3.3V")

        Returns:
            Net number, or 0 if not found

        Raises:
            ValueError: If net name not found
        """
        net = self._pcb.get_net_by_name(net_name)
        if net is None:
            raise ValueError(f"Net '{net_name}' not found in PCB")
        return net.number

    def add_zone(
        self,
        net: str,
        layer: str,
        priority: int = 0,
        clearance: float = 0.3,
        min_thickness: float = 0.25,
        thermal_gap: float = 0.3,
        thermal_bridge_width: float = 0.4,
        boundary: list[tuple[float, float]] | None = None,
    ) -> GeneratedZone:
        """Add a copper pour zone.

        Args:
            net: Net name (e.g., "GND", "+3.3V")
            layer: Copper layer (e.g., "B.Cu", "F.Cu", "In1.Cu")
            priority: Zone fill priority (higher = fills later)
            clearance: Clearance to other nets in mm
            min_thickness: Minimum copper thickness in mm
            thermal_gap: Thermal relief gap in mm
            thermal_bridge_width: Thermal relief spoke width in mm
            boundary: Custom boundary polygon, or None for board outline

        Returns:
            GeneratedZone object

        Raises:
            ValueError: If net not found in PCB
        """
        config = ZoneConfig(
            net=net,
            layer=layer,
            priority=priority,
            clearance=clearance,
            min_thickness=min_thickness,
            thermal_gap=thermal_gap,
            thermal_bridge_width=thermal_bridge_width,
            boundary=boundary,
        )

        # Resolve net number
        net_number = self.get_net_number(net)

        # Use board outline if no boundary specified
        actual_boundary = boundary if boundary is not None else self.board_outline

        zone = GeneratedZone(
            config=config,
            net_number=net_number,
            boundary=actual_boundary,
        )

        self._zones.append(zone)
        return zone

    def add_ground_plane(
        self,
        layer: str = "B.Cu",
        priority: int = 1,
        **kwargs,
    ) -> GeneratedZone:
        """Add a ground plane zone.

        Convenience method for adding GND zones with appropriate defaults.

        Args:
            layer: Copper layer (default: "B.Cu" for bottom layer ground)
            priority: Zone priority (default: 1, higher than power)
            **kwargs: Additional arguments passed to add_zone()

        Returns:
            GeneratedZone object
        """
        return self.add_zone(net="GND", layer=layer, priority=priority, **kwargs)

    def add_power_plane(
        self,
        net: str,
        layer: str = "F.Cu",
        priority: int = 0,
        **kwargs,
    ) -> GeneratedZone:
        """Add a power plane zone.

        Convenience method for adding power net zones.

        Args:
            net: Power net name (e.g., "+3.3V", "+5V", "VCC")
            layer: Copper layer (default: "F.Cu" for top layer)
            priority: Zone priority (default: 0, lower than ground)
            **kwargs: Additional arguments passed to add_zone()

        Returns:
            GeneratedZone object
        """
        return self.add_zone(net=net, layer=layer, priority=priority, **kwargs)

    @property
    def zones(self) -> list[GeneratedZone]:
        """List of zones to be generated."""
        return self._zones

    def generate_sexp(self) -> str:
        """Generate S-expression string for all zones.

        Returns:
            S-expression string for inserting into PCB file
        """
        if not self._zones:
            return ""

        parts = []
        for zone in self._zones:
            parts.append(zone.to_sexp_node().to_string(indent=1))

        return "\n".join(parts)

    def apply(self) -> None:
        """Apply zones to the loaded document.

        Modifies the internal document by appending zone definitions.
        Call save() after this to write changes to disk.
        """
        if not self._doc:
            raise ValueError("No document loaded - use from_pcb() to load a PCB")

        for zone in self._zones:
            self._doc.append(zone.to_sexp_node())

    def save(self, output_path: str | Path | None = None) -> Path:
        """Save PCB with generated zones.

        Args:
            output_path: Output file path, or None to return path only

        Returns:
            Path to the output file
        """
        if not self._doc:
            raise ValueError("No document loaded - use from_pcb() to load a PCB")

        # Apply zones if not already applied
        self.apply()

        if output_path is None:
            raise ValueError("Output path required")

        output_path = Path(output_path)
        output_path.write_text(self._doc.to_string())
        return output_path

    def get_statistics(self) -> dict:
        """Get statistics about generated zones.

        Returns:
            Dictionary with zone generation statistics
        """
        return {
            "zone_count": len(self._zones),
            "zones": [
                {
                    "net": z.config.net,
                    "layer": z.config.layer,
                    "priority": z.config.priority,
                    "boundary_points": len(z.boundary),
                }
                for z in self._zones
            ],
        }


def parse_power_nets(spec: str) -> list[tuple[str, str]]:
    """Parse power nets specification string.

    Parses format: "NET1:LAYER1,NET2:LAYER2,..."
    e.g., "GND:B.Cu,+3.3V:F.Cu"

    Args:
        spec: Power nets specification string

    Returns:
        List of (net_name, layer) tuples

    Raises:
        ValueError: If format is invalid
    """
    if not spec or not spec.strip():
        return []

    result = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue

        if ":" not in item:
            raise ValueError(
                f"Invalid power net format: '{item}'. Expected 'NET:LAYER' (e.g., 'GND:B.Cu')"
            )

        parts = item.split(":", 1)
        net_name = parts[0].strip()
        layer = parts[1].strip()

        if not net_name:
            raise ValueError(f"Empty net name in: '{item}'")
        if not layer:
            raise ValueError(f"Empty layer in: '{item}'")

        result.append((net_name, layer))

    return result
