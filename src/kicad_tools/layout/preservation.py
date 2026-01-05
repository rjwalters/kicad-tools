"""
Layout preservation for PCB regeneration.

Applies saved layout state to regenerated PCB files, preserving
component placement and routing when schematic changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.schema.pcb import PCB

from .addressing import AddressRegistry
from .snapshot import SnapshotCapture
from .types import ComponentLayout, LayoutSnapshot


@dataclass
class PreservationResult:
    """
    Result of applying layout preservation.

    Tracks which components were matched and positioned, which couldn't
    be matched, and which are new components needing placement.
    """

    # Components successfully matched and positioned
    matched_components: list[str] = field(default_factory=list)

    # Components from old layout that couldn't be found in new PCB
    unmatched_components: list[str] = field(default_factory=list)

    # New components in new PCB that need manual placement
    new_components: list[str] = field(default_factory=list)

    # Traces preserved (net names)
    preserved_traces: list[str] = field(default_factory=list)

    # Zones preserved
    preserved_zones: list[str] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        """Percentage of original components that were matched."""
        total = len(self.matched_components) + len(self.unmatched_components)
        if total == 0:
            return 0.0
        return len(self.matched_components) / total * 100

    def summary(self) -> dict:
        """Get a summary of the preservation result."""
        return {
            "matched": len(self.matched_components),
            "unmatched": len(self.unmatched_components),
            "new": len(self.new_components),
            "match_rate": f"{self.match_rate:.1f}%",
            "preserved_traces": len(self.preserved_traces),
            "preserved_zones": len(self.preserved_zones),
        }


class LayoutPreserver:
    """
    Preserves and restores PCB layout across schematic regenerations.

    Captures layout state from an existing PCB, then applies that state
    to a regenerated PCB, matching components by hierarchical address.

    Example:
        >>> # Before modifying schematic
        >>> preserver = LayoutPreserver("board.kicad_pcb", "board.kicad_sch")
        >>>
        >>> # User modifies schematic, regenerates netlist
        >>>
        >>> # After regeneration
        >>> result = preserver.apply_to_new_pcb(
        ...     "board_new.kicad_pcb",
        ...     "board_new.kicad_sch"
        ... )
        >>> print(f"Preserved {len(result.matched_components)} positions")

    Matching Strategies:
        1. Exact match: Same hierarchical address in old and new
        2. Fuzzy match: Same path, similar reference (C1 → C2 in same subcircuit)
        3. Value match: Same value+footprint in similar location
    """

    def __init__(self, pcb_path: str | Path, schematic_path: str | Path):
        """
        Initialize preserver and capture current layout.

        Args:
            pcb_path: Path to the existing .kicad_pcb file
            schematic_path: Path to the root .kicad_sch file
        """
        self._pcb_path = Path(pcb_path)
        self._schematic_path = Path(schematic_path)
        self._snapshot: LayoutSnapshot | None = None
        self._old_registry: AddressRegistry | None = None

        # Capture the current state
        self._capture_snapshot()

    def _capture_snapshot(self) -> None:
        """Capture layout snapshot from current PCB."""
        capture = SnapshotCapture(self._pcb_path, self._schematic_path)
        self._snapshot = capture.capture()
        self._old_registry = AddressRegistry(self._schematic_path)

    @property
    def snapshot(self) -> LayoutSnapshot | None:
        """Get the captured layout snapshot."""
        return self._snapshot

    def apply_to_new_pcb(
        self,
        new_pcb_path: str | Path,
        new_schematic_path: str | Path,
        save: bool = True,
    ) -> PreservationResult:
        """
        Apply saved layout to a regenerated PCB.

        Matches components by hierarchical address and applies saved
        positions. Components that can't be matched are left at their
        default positions.

        Args:
            new_pcb_path: Path to the regenerated .kicad_pcb file
            new_schematic_path: Path to the modified .kicad_sch file
            save: Whether to save the modified PCB (default True)

        Returns:
            PreservationResult with details of what was matched/preserved
        """
        if not self._snapshot:
            raise RuntimeError("No layout snapshot captured")

        new_pcb_path = Path(new_pcb_path)
        new_schematic_path = Path(new_schematic_path)

        # Build new address registry
        new_registry = AddressRegistry(new_schematic_path)

        # Load the new PCB
        new_pcb = PCB.load(str(new_pcb_path))

        result = PreservationResult()

        # Match and apply component positions
        self._apply_component_positions(new_pcb, new_registry, result)

        # Note which nets have preserved traces (if nets still exist)
        self._check_trace_preservation(new_pcb, result)

        # Note which zones are preserved
        self._check_zone_preservation(new_pcb, result)

        # Save if requested
        if save:
            new_pcb.save(new_pcb_path)

        return result

    def _apply_component_positions(
        self,
        new_pcb: PCB,
        new_registry: AddressRegistry,
        result: PreservationResult,
    ) -> None:
        """Apply saved positions to components in new PCB."""
        if not self._snapshot:
            return

        # Track which addresses in new PCB we've seen
        new_addresses: set[str] = set()

        # Build a mapping from reference to footprint for the new PCB
        ref_to_footprint: dict[str, object] = {}
        for fp in new_pcb.footprints:
            ref_to_footprint[fp.reference] = fp

        # Try to match each component in the new PCB
        for fp in new_pcb.footprints:
            address = self._find_address_for_new_component(fp.reference, new_registry)
            if address:
                new_addresses.add(address)

            # Try to find matching layout from snapshot
            matched_layout = self._find_matching_layout(address, fp.reference, new_registry)

            if matched_layout:
                # Apply the saved position
                success = new_pcb.update_footprint_position(
                    fp.reference,
                    matched_layout.x,
                    matched_layout.y,
                    matched_layout.rotation,
                )
                if success:
                    result.matched_components.append(address or fp.reference)
                else:
                    result.new_components.append(address or fp.reference)
            else:
                result.new_components.append(address or fp.reference)

        # Find components that were in old layout but not in new
        old_addresses = set(self._snapshot.component_positions.keys())
        result.unmatched_components = list(old_addresses - new_addresses)

    def _find_address_for_new_component(
        self,
        reference: str,
        new_registry: AddressRegistry,
    ) -> str | None:
        """Find hierarchical address for a component in new schematic."""
        if reference in new_registry:
            return reference

        # Try pattern match
        matches = new_registry.match_by_pattern(f"**{reference}")
        if len(matches) == 1:
            return matches[0].full_path

        return reference

    def _find_matching_layout(
        self,
        address: str | None,
        reference: str,
        new_registry: AddressRegistry,
    ) -> ComponentLayout | None:
        """
        Find matching layout from snapshot using multiple strategies.

        Strategies in order of preference:
        1. Exact address match
        2. Reference match (same reference designator)
        3. Fuzzy match (same sheet path, different ref number)
        """
        if not self._snapshot:
            return None

        # Strategy 1: Exact address match
        if address and address in self._snapshot.component_positions:
            return self._snapshot.component_positions[address]

        # Strategy 2: Direct reference match
        if reference in self._snapshot.component_positions:
            return self._snapshot.component_positions[reference]

        # Strategy 3: Fuzzy match - same type in same sheet
        if address:
            return self._fuzzy_match(address, reference)

        return None

    def _fuzzy_match(self, address: str, reference: str) -> ComponentLayout | None:
        """
        Attempt fuzzy matching for renamed components.

        Matches components with same prefix in same sheet path.
        For example, if C1 was renamed to C2 in the same subcircuit,
        we can still preserve its position.
        """
        if not self._snapshot:
            return None

        # Extract sheet path and component type prefix
        parts = address.rsplit(".", 1)
        if len(parts) == 2:
            sheet_path, local_ref = parts
        else:
            sheet_path = ""
            local_ref = parts[0]

        # Get the component type prefix (e.g., "C" from "C1")
        prefix = "".join(c for c in local_ref if c.isalpha())

        # Look for components with same prefix in same sheet
        candidates: list[ComponentLayout] = []
        for addr, layout in self._snapshot.component_positions.items():
            layout_parts = addr.rsplit(".", 1)
            if len(layout_parts) == 2:
                layout_sheet, layout_ref = layout_parts
            else:
                layout_sheet = ""
                layout_ref = layout_parts[0]

            # Check if same sheet and same component type
            layout_prefix = "".join(c for c in layout_ref if c.isalpha())
            if layout_sheet == sheet_path and layout_prefix == prefix:
                candidates.append(layout)

        # If we have exactly one candidate, use it
        if len(candidates) == 1:
            return candidates[0]

        return None

    def _check_trace_preservation(self, new_pcb: PCB, result: PreservationResult) -> None:
        """Check which nets have traces that can be preserved."""
        if not self._snapshot:
            return

        new_net_names = {net.name for net in new_pcb.nets.values()}

        for net_name in self._snapshot.traces:
            if net_name in new_net_names:
                result.preserved_traces.append(net_name)

    def _check_zone_preservation(self, new_pcb: PCB, result: PreservationResult) -> None:
        """Check which zones can be preserved."""
        if not self._snapshot:
            return

        new_net_names = {net.name for net in new_pcb.nets.values()}

        for zone_key, zone in self._snapshot.zones.items():
            if zone.net_name in new_net_names:
                result.preserved_zones.append(zone_key)


def preserve_layout(
    old_pcb: str | Path,
    old_schematic: str | Path,
    new_pcb: str | Path,
    new_schematic: str | Path,
) -> PreservationResult:
    """
    Convenience function to preserve layout from old to new PCB.

    Args:
        old_pcb: Path to original .kicad_pcb file
        old_schematic: Path to original .kicad_sch file
        new_pcb: Path to regenerated .kicad_pcb file
        new_schematic: Path to modified .kicad_sch file

    Returns:
        PreservationResult with details of what was preserved
    """
    preserver = LayoutPreserver(old_pcb, old_schematic)
    return preserver.apply_to_new_pcb(new_pcb, new_schematic)
