"""Incremental layout updates for KiCad PCBs.

Provides change detection and incremental update capabilities
to preserve layout when updating PCB from modified schematic.

Key classes:
- ChangeDetector: Detects what changed between old and new design
- IncrementalUpdater: Applies minimal updates preserving unchanged layout
- SnapshotBuilder: Creates layout snapshots from PCB state
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import (
    ChangeType,
    ComponentState,
    IncrementalSnapshot,
    LayoutChange,
    UpdateResult,
)

if TYPE_CHECKING:
    from kicad_tools.layout.addressing import AddressRegistry
    from kicad_tools.schema.pcb import PCB


class SnapshotBuilder:
    """Creates layout snapshots from PCB state.

    Captures current component positions and net connections
    for later comparison during incremental updates.

    Example:
        >>> builder = SnapshotBuilder()
        >>> snapshot = builder.build(pcb, registry)
        >>> # Save snapshot for later comparison
        >>> with open("layout.json", "w") as f:
        ...     json.dump(snapshot.to_dict(), f)
    """

    def build(
        self,
        pcb: PCB,
        registry: AddressRegistry | None = None,
    ) -> IncrementalSnapshot:
        """Build a snapshot from current PCB state.

        Args:
            pcb: The PCB to snapshot
            registry: Optional AddressRegistry for hierarchical addresses.
                     If not provided, uses reference designators as addresses.

        Returns:
            IncrementalSnapshot capturing current state
        """
        component_states: dict[str, ComponentState] = {}
        net_connections: dict[str, list[str]] = {}

        for fp in pcb.footprints:
            # Determine address: use registry if available, otherwise use reference
            if registry:
                addr = registry.get_address(fp.uuid)
                if addr is None:
                    # Fall back to reference if UUID not found
                    addr = fp.reference
            else:
                addr = fp.reference

            # Create component state
            state = ComponentState(
                reference=fp.reference,
                address=addr,
                position=fp.position,
                rotation=fp.rotation,
                layer=fp.layer,
                footprint=fp.name,
                uuid=fp.uuid,
            )
            component_states[addr] = state

            # Collect net connections from pads
            nets: list[str] = []
            for pad in fp.pads:
                if pad.net_name and pad.net_name not in nets:
                    nets.append(pad.net_name)
            if nets:
                net_connections[addr] = nets

        return IncrementalSnapshot(
            component_states=component_states,
            net_connections=net_connections,
        )


class ChangeDetector:
    """Detects changes between old layout snapshot and new design.

    Compares component addresses to identify:
    - Added components (in new, not in old)
    - Removed components (in old, not in new)
    - Modified components (in both, but schematic changes detected)
    - Unchanged components (in both, no changes)

    Example:
        >>> detector = ChangeDetector(old_snapshot, new_registry)
        >>> changes = detector.detect_changes()
        >>> for change in changes:
        ...     print(f"{change.component_address}: {change.change_type}")
    """

    def __init__(
        self,
        old_snapshot: IncrementalSnapshot,
        new_registry: AddressRegistry,
    ):
        """Initialize change detector.

        Args:
            old_snapshot: Snapshot of the old layout state
            new_registry: Registry of components in the new schematic
        """
        self.old_snapshot = old_snapshot
        self.new_registry = new_registry

    def detect_changes(self) -> list[LayoutChange]:
        """Detect all changes between old and new design.

        Returns:
            List of LayoutChange objects describing each change
        """
        changes: list[LayoutChange] = []

        old_addresses = self.old_snapshot.addresses()
        new_addresses = {addr.full_path for addr in self.new_registry.all_addresses()}

        # Find removed components (in old, not in new)
        for addr in old_addresses:
            if addr not in new_addresses:
                old_state = self.old_snapshot.get_state(addr)
                affected_nets = self.old_snapshot.get_nets(addr)
                changes.append(
                    LayoutChange(
                        change_type=ChangeType.REMOVED,
                        component_address=addr,
                        old_state=old_state,
                        new_state=None,
                        affected_nets=affected_nets,
                    )
                )

        # Find added components (in new, not in old)
        for addr in new_addresses:
            if addr not in old_addresses:
                # New component - no position yet
                new_component = self.new_registry.resolve(addr)
                if new_component:
                    changes.append(
                        LayoutChange(
                            change_type=ChangeType.ADDED,
                            component_address=addr,
                            old_state=None,
                            new_state=None,  # No PCB state yet
                            affected_nets=[],  # Will be determined when placed
                        )
                    )

        # Find modified or unchanged components (in both)
        for addr in old_addresses & new_addresses:
            old_state = self.old_snapshot.get_state(addr)
            affected_nets = self.old_snapshot.get_nets(addr)

            # Check if component has been modified in schematic
            # For now, consider all common components as potentially modifiable
            # but mark them unchanged since we can't detect schematic changes yet
            is_modified = self._is_component_modified(addr)

            if is_modified:
                changes.append(
                    LayoutChange(
                        change_type=ChangeType.MODIFIED,
                        component_address=addr,
                        old_state=old_state,
                        new_state=None,  # Will be updated during apply
                        affected_nets=affected_nets,
                    )
                )
            else:
                changes.append(
                    LayoutChange(
                        change_type=ChangeType.UNCHANGED,
                        component_address=addr,
                        old_state=old_state,
                        new_state=old_state,  # Same as old
                        affected_nets=[],  # No nets affected
                    )
                )

        return changes

    def _is_component_modified(self, addr: str) -> bool:
        """Check if a component has been modified.

        Currently returns False as we can't detect schematic-level
        changes without comparing netlists. Future enhancement could
        compare pin counts, footprint assignments, etc.

        Args:
            addr: Component address to check

        Returns:
            True if component was modified, False otherwise
        """
        # TODO: Implement actual modification detection by comparing:
        # - Footprint assignment
        # - Pin count changes
        # - Value changes
        # For now, assume common components are unchanged
        return False

    def get_summary(self) -> dict[str, int]:
        """Get a summary count of changes by type.

        Returns:
            Dictionary with counts for each change type
        """
        changes = self.detect_changes()
        return {
            "added": sum(1 for c in changes if c.is_added),
            "removed": sum(1 for c in changes if c.is_removed),
            "modified": sum(1 for c in changes if c.is_modified),
            "unchanged": sum(1 for c in changes if c.is_unchanged),
            "total": len(changes),
        }


class IncrementalUpdater:
    """Applies incremental updates to PCB preserving unchanged layout.

    Takes detected changes and applies them minimally:
    - Removed components: Mark as orphans (don't delete routing yet)
    - Added components: Flag for placement
    - Modified components: Update in place if possible
    - Unchanged components: Preserve position exactly

    Example:
        >>> updater = IncrementalUpdater()
        >>> result = updater.apply(pcb, changes)
        >>> print(f"Preserved {result.preserved_components} positions")
        >>> print(f"Need placement: {result.added_components}")
    """

    def apply(
        self,
        pcb: PCB,
        changes: list[LayoutChange],
    ) -> UpdateResult:
        """Apply incremental updates to PCB.

        Args:
            pcb: The PCB to update
            changes: List of changes from ChangeDetector

        Returns:
            UpdateResult with summary of applied changes
        """
        result = UpdateResult()
        all_affected_nets: set[str] = set()

        for change in changes:
            if change.change_type == ChangeType.REMOVED:
                # Mark component for removal
                result.removed_components.append(change.component_address)
                all_affected_nets.update(change.affected_nets)

            elif change.change_type == ChangeType.ADDED:
                # Flag for placement
                result.added_components.append(change.component_address)

            elif change.change_type == ChangeType.MODIFIED:
                # Try to preserve position
                if change.old_state and self._can_preserve(change):
                    # Keep old position
                    result.preserved_components += 1
                else:
                    # Needs re-placement
                    result.updated_components.append(change.component_address)
                    all_affected_nets.update(change.affected_nets)

            else:  # UNCHANGED
                result.preserved_components += 1

        result.affected_nets = sorted(all_affected_nets)
        return result

    def _can_preserve(self, change: LayoutChange) -> bool:
        """Check if a modified component's position can be preserved.

        Position can be preserved if:
        - Footprint hasn't changed
        - Pin count hasn't changed
        - Component is still on same layer

        Args:
            change: The change to evaluate

        Returns:
            True if position can be preserved
        """
        if not change.old_state:
            return False

        # TODO: Check if new component has same footprint
        # For now, preserve all modified components
        return True

    def apply_position_updates(
        self,
        pcb: PCB,
        changes: list[LayoutChange],
        new_positions: dict[str, tuple[float, float, float]] | None = None,
    ) -> UpdateResult:
        """Apply position updates for modified/unchanged components.

        This method actually updates the PCB positions, preserving
        unchanged components and optionally setting new positions
        for added/modified components.

        Args:
            pcb: The PCB to update
            changes: List of changes from ChangeDetector
            new_positions: Optional dict mapping addresses to (x, y, rotation)
                          for added/modified components

        Returns:
            UpdateResult with summary of applied changes
        """
        result = UpdateResult()
        all_affected_nets: set[str] = set()
        new_positions = new_positions or {}

        for change in changes:
            if change.change_type == ChangeType.REMOVED:
                result.removed_components.append(change.component_address)
                all_affected_nets.update(change.affected_nets)

            elif change.change_type == ChangeType.ADDED:
                # Apply new position if provided
                if change.component_address in new_positions:
                    x, y, rot = new_positions[change.component_address]
                    # Find the reference from the address
                    ref = change.component_address.split(".")[-1]
                    success = pcb.update_footprint_position(ref, x, y, rot)
                    if success:
                        result.preserved_components += 1
                    else:
                        result.added_components.append(change.component_address)
                        result.errors.append(f"Failed to place {change.component_address}")
                else:
                    result.added_components.append(change.component_address)

            elif change.change_type == ChangeType.MODIFIED:
                if change.old_state:
                    # Preserve old position
                    ref = change.component_address.split(".")[-1]
                    x, y = change.old_state.position
                    rot = change.old_state.rotation
                    pcb.update_footprint_position(ref, x, y, rot)
                    result.preserved_components += 1
                else:
                    result.updated_components.append(change.component_address)
                    all_affected_nets.update(change.affected_nets)

            else:  # UNCHANGED
                # Position already correct, no update needed
                result.preserved_components += 1

        result.affected_nets = sorted(all_affected_nets)
        return result


def detect_layout_changes(
    old_snapshot: IncrementalSnapshot,
    new_registry: AddressRegistry,
) -> list[LayoutChange]:
    """Convenience function to detect layout changes.

    Args:
        old_snapshot: Snapshot of the old layout state
        new_registry: Registry of components in the new schematic

    Returns:
        List of LayoutChange objects
    """
    detector = ChangeDetector(old_snapshot, new_registry)
    return detector.detect_changes()


def apply_incremental_update(
    pcb: PCB,
    changes: list[LayoutChange],
) -> UpdateResult:
    """Convenience function to apply incremental updates.

    Args:
        pcb: The PCB to update
        changes: List of changes from detect_layout_changes

    Returns:
        UpdateResult with summary
    """
    updater = IncrementalUpdater()
    return updater.apply(pcb, changes)
