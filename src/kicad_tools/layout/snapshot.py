"""
Layout snapshot capture for PCB files.

Captures the complete layout state from a PCB file including:
- Component positions indexed by hierarchical address
- Trace routing indexed by net
- Via placements
- Zone definitions
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from kicad_tools.schema.pcb import PCB

from .addressing import AddressRegistry
from .types import (
    ComponentLayout,
    LayoutSnapshot,
    TraceSegment,
    ViaLayout,
    ZoneLayout,
)


class SnapshotCapture:
    """
    Captures layout state from a PCB file.

    Creates a LayoutSnapshot that can be used to restore placement
    and routing after PCB regeneration.

    Example:
        >>> capture = SnapshotCapture("board.kicad_pcb", "board.kicad_sch")
        >>> snapshot = capture.capture()
        >>> print(f"Captured {snapshot.component_count} components")
    """

    def __init__(self, pcb_path: str | Path, schematic_path: str | Path):
        """
        Initialize snapshot capture.

        Args:
            pcb_path: Path to the .kicad_pcb file
            schematic_path: Path to the root .kicad_sch file for hierarchy
        """
        self._pcb_path = Path(pcb_path)
        self._schematic_path = Path(schematic_path)
        self._pcb: PCB | None = None
        self._registry: AddressRegistry | None = None

    def capture(self) -> LayoutSnapshot:
        """
        Capture the complete layout state.

        Returns:
            LayoutSnapshot containing all placement and routing data
        """
        self._load_files()

        snapshot = LayoutSnapshot(
            timestamp=datetime.now(),
            pcb_path=str(self._pcb_path),
            schematic_hash=self._compute_schematic_hash(),
        )

        # Capture components
        self._capture_components(snapshot)

        # Capture traces
        self._capture_traces(snapshot)

        # Capture vias
        self._capture_vias(snapshot)

        # Capture zones
        self._capture_zones(snapshot)

        return snapshot

    def _load_files(self) -> None:
        """Load PCB and build address registry."""
        if not self._pcb_path.exists():
            raise FileNotFoundError(f"PCB file not found: {self._pcb_path}")

        self._pcb = PCB.load(str(self._pcb_path))
        self._registry = AddressRegistry(self._schematic_path)

    def _compute_schematic_hash(self) -> str:
        """Compute hash of schematic for version tracking."""
        if not self._schematic_path.exists():
            return ""

        content = self._schematic_path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]

    def _capture_components(self, snapshot: LayoutSnapshot) -> None:
        """Capture all component positions."""
        if not self._pcb or not self._registry:
            return

        for footprint in self._pcb.footprints:
            # Try to find hierarchical address by reference
            # First, check if there's a direct address match
            address = self._find_address_for_reference(footprint.reference)

            if address:
                layout = ComponentLayout(
                    address=address,
                    x=footprint.position[0],
                    y=footprint.position[1],
                    rotation=footprint.rotation,
                    layer=footprint.layer,
                    locked=False,  # Would need to parse locked attr from PCB
                    reference=footprint.reference,
                    uuid=footprint.uuid,
                )
                snapshot.component_positions[address] = layout

    def _find_address_for_reference(self, reference: str) -> str | None:
        """
        Find hierarchical address for a reference designator.

        Uses the address registry to match reference to hierarchical path.
        Falls back to reference itself if not in registry.

        Args:
            reference: Reference designator (e.g., "C1", "U2")

        Returns:
            Hierarchical address or None if not found
        """
        if not self._registry:
            return reference

        # Try exact match first
        if reference in self._registry:
            return reference

        # Try pattern match for the reference
        matches = self._registry.match_by_pattern(f"**{reference}")
        if len(matches) == 1:
            return matches[0].full_path

        # Multiple matches or no matches - use reference as fallback
        return reference

    def _capture_traces(self, snapshot: LayoutSnapshot) -> None:
        """Capture all trace segments indexed by net."""
        if not self._pcb:
            return

        for segment in self._pcb.segments:
            net = self._pcb.get_net(segment.net_number)
            net_name = net.name if net else f"net_{segment.net_number}"

            trace = TraceSegment(
                net_name=net_name,
                start=segment.start,
                end=segment.end,
                width=segment.width,
                layer=segment.layer,
                uuid=segment.uuid,
            )

            if net_name not in snapshot.traces:
                snapshot.traces[net_name] = []
            snapshot.traces[net_name].append(trace)

    def _capture_vias(self, snapshot: LayoutSnapshot) -> None:
        """Capture all via placements."""
        if not self._pcb:
            return

        for via in self._pcb.vias:
            net = self._pcb.get_net(via.net_number)
            net_name = net.name if net else f"net_{via.net_number}"

            via_layout = ViaLayout(
                net_name=net_name,
                position=via.position,
                size=via.size,
                drill=via.drill,
                layers=via.layers,
                uuid=via.uuid,
            )
            snapshot.vias.append(via_layout)

    def _capture_zones(self, snapshot: LayoutSnapshot) -> None:
        """Capture all zone definitions."""
        if not self._pcb:
            return

        for zone in self._pcb.zones:
            # Use zone name if available, otherwise use net_name+layer as key
            zone_key = zone.name if zone.name else f"{zone.net_name}_{zone.layer}"

            zone_layout = ZoneLayout(
                net_name=zone.net_name,
                layer=zone.layer,
                name=zone.name,
                polygon=zone.polygon,
                priority=zone.priority,
                uuid=zone.uuid,
            )
            snapshot.zones[zone_key] = zone_layout


def capture_layout(pcb_path: str | Path, schematic_path: str | Path) -> LayoutSnapshot:
    """
    Convenience function to capture layout snapshot.

    Args:
        pcb_path: Path to the .kicad_pcb file
        schematic_path: Path to the root .kicad_sch file

    Returns:
        LayoutSnapshot containing captured layout state
    """
    capture = SnapshotCapture(pcb_path, schematic_path)
    return capture.capture()
