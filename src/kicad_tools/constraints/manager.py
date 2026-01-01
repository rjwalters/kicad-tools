"""
Constraint manager for multi-stage optimization.

The ConstraintManager is the primary interface for working with constraints.
It provides methods to:
- Lock components, nets, and regions
- Apply constraints to optimizers
- Validate that optimizations respect constraints
- Load/save constraint manifests
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .locks import ComponentLock, LockType, NetRouteLock, RegionConstraint, RelativeConstraint
from .manifest import YAML_MANIFEST_NAME, ConstraintManifest

if TYPE_CHECKING:
    from kicad_tools.optim import PlacementOptimizer
    from kicad_tools.reasoning.state import PCBState


class ConstraintViolation:
    """
    A violation of a constraint.

    Attributes:
        constraint_type: Type of constraint violated (component, net, region, relative)
        constraint_name: Name/identifier of the violated constraint
        message: Human-readable description of the violation
        location: Optional (x, y) location of the violation
        severity: "error" or "warning"
    """

    def __init__(
        self,
        constraint_type: str,
        constraint_name: str,
        message: str,
        location: tuple[float, float] | None = None,
        severity: str = "error",
    ):
        self.constraint_type = constraint_type
        self.constraint_name = constraint_name
        self.message = message
        self.location = location
        self.severity = severity

    def __repr__(self) -> str:
        loc = f" @({self.location[0]:.1f}, {self.location[1]:.1f})" if self.location else ""
        return f"ConstraintViolation({self.constraint_type}: {self.message}{loc})"


class ConstraintManager:
    """
    Manages constraints across optimization stages.

    The ConstraintManager is the primary interface for:
    - Creating and managing constraints
    - Applying constraints to optimizers
    - Validating that optimizations respect constraints
    - Persisting constraints to disk

    Example:
        >>> cm = ConstraintManager()
        >>> cm.lock_component("FB1", LockType.POSITION,
        ...                   "Domain boundary", "domain_analyzer")
        >>> cm.apply_to_placer(placer)
        >>> placer.run(iterations=1000)
        >>> violations = cm.validate_placement(placer)
    """

    def __init__(self, manifest: ConstraintManifest | None = None):
        """
        Initialize the constraint manager.

        Args:
            manifest: Optional pre-loaded manifest. If None, starts empty.
        """
        self._manifest = manifest or ConstraintManifest()

    # =========================================================================
    # Component Locks
    # =========================================================================

    def lock_component(
        self,
        ref: str,
        lock_type: LockType,
        reason: str,
        locked_by: str,
        position: tuple[float, float] | None = None,
        rotation: float | None = None,
    ) -> ComponentLock:
        """
        Lock a component with semantic annotation.

        Args:
            ref: Component reference designator (e.g., "FB1", "U3")
            lock_type: What to lock (POSITION, ROTATION, or FULL)
            reason: Human-readable explanation of why the lock exists
            locked_by: Identifier of the agent/optimizer creating the lock
            position: Optional locked position (x, y). If None, uses current position.
            rotation: Optional locked rotation. If None, uses current rotation.

        Returns:
            The created ComponentLock
        """
        lock = ComponentLock(
            ref=ref,
            lock_type=lock_type,
            reason=reason,
            locked_by=locked_by,
            timestamp=datetime.now(),
            position=position,
            rotation=rotation,
        )
        self._manifest.component_locks[ref] = lock
        return lock

    def unlock_component(self, ref: str) -> bool:
        """
        Remove a lock from a component.

        Args:
            ref: Component reference designator

        Returns:
            True if a lock was removed, False if no lock existed
        """
        if ref in self._manifest.component_locks:
            del self._manifest.component_locks[ref]
            return True
        return False

    def get_component_lock(self, ref: str) -> ComponentLock | None:
        """Get the lock for a component, if any."""
        return self._manifest.component_locks.get(ref)

    def is_component_locked(self, ref: str) -> bool:
        """Return True if the component has any lock."""
        return ref in self._manifest.component_locks

    @property
    def locked_components(self) -> list[str]:
        """Return list of locked component references."""
        return list(self._manifest.component_locks.keys())

    # =========================================================================
    # Net Route Locks
    # =========================================================================

    def lock_net_route(
        self,
        net_name: str,
        reason: str,
        locked_by: str,
        trace_geometry: list[tuple[float, float, float, float, str, float]] | None = None,
        via_positions: list[tuple[float, float, tuple[str, str]]] | None = None,
    ) -> NetRouteLock:
        """
        Lock a routed net path.

        Args:
            net_name: Net name (e.g., "MCLK_MCU")
            reason: Human-readable explanation
            locked_by: Identifier of the agent/optimizer creating the lock
            trace_geometry: Optional list of trace segments. If None, will be
                           extracted from PCB when applied.
            via_positions: Optional list of via positions.

        Returns:
            The created NetRouteLock
        """
        lock = NetRouteLock(
            net_name=net_name,
            reason=reason,
            locked_by=locked_by,
            timestamp=datetime.now(),
            trace_geometry=trace_geometry or [],
            via_positions=via_positions or [],
        )
        self._manifest.net_route_locks[net_name] = lock
        return lock

    def unlock_net_route(self, net_name: str) -> bool:
        """Remove a lock from a net route."""
        if net_name in self._manifest.net_route_locks:
            del self._manifest.net_route_locks[net_name]
            return True
        return False

    def get_net_route_lock(self, net_name: str) -> NetRouteLock | None:
        """Get the lock for a net route, if any."""
        return self._manifest.net_route_locks.get(net_name)

    def is_net_route_locked(self, net_name: str) -> bool:
        """Return True if the net route is locked."""
        return net_name in self._manifest.net_route_locks

    @property
    def locked_net_routes(self) -> list[str]:
        """Return list of locked net names."""
        return list(self._manifest.net_route_locks.keys())

    # =========================================================================
    # Region Constraints
    # =========================================================================

    def define_region(
        self,
        name: str,
        bounds: dict[str, float],
        reason: str,
        locked_by: str = "",
        allowed_nets: list[str] | None = None,
        disallowed_nets: list[str] | None = None,
        allowed_components: list[str] | None = None,
        disallowed_components: list[str] | None = None,
    ) -> RegionConstraint:
        """
        Define a region constraint.

        Args:
            name: Human-readable region name (e.g., "analog_domain")
            bounds: Dict with x_min, x_max, y_min, y_max
            reason: Human-readable explanation
            locked_by: Identifier of the agent/optimizer creating the constraint
            allowed_nets: List of net names allowed in this region
            disallowed_nets: List of net names not allowed in this region
            allowed_components: List of component refs allowed in this region
            disallowed_components: List of component refs not allowed in this region

        Returns:
            The created RegionConstraint
        """
        region = RegionConstraint(
            name=name,
            bounds=bounds,
            reason=reason,
            locked_by=locked_by,
            allowed_nets=allowed_nets or [],
            disallowed_nets=disallowed_nets or [],
            allowed_components=allowed_components or [],
            disallowed_components=disallowed_components or [],
        )
        self._manifest.region_constraints[name] = region
        return region

    def remove_region(self, name: str) -> bool:
        """Remove a region constraint."""
        if name in self._manifest.region_constraints:
            del self._manifest.region_constraints[name]
            return True
        return False

    def get_region(self, name: str) -> RegionConstraint | None:
        """Get a region constraint by name."""
        return self._manifest.region_constraints.get(name)

    @property
    def regions(self) -> list[str]:
        """Return list of region names."""
        return list(self._manifest.region_constraints.keys())

    # =========================================================================
    # Relative Constraints
    # =========================================================================

    def add_relative_constraint(
        self,
        ref1: str,
        relation: str,
        ref2: str,
        max_distance: float | None = None,
        reason: str = "",
        locked_by: str = "",
    ) -> RelativeConstraint:
        """
        Add a relative constraint between two components.

        Args:
            ref1: First component reference
            relation: Type of relationship ("near", "aligned", "symmetric")
            ref2: Second component reference
            max_distance: Maximum distance in mm (for "near" relation)
            reason: Human-readable explanation
            locked_by: Identifier of the agent/optimizer creating the constraint

        Returns:
            The created RelativeConstraint
        """
        constraint = RelativeConstraint(
            ref1=ref1,
            relation=relation,
            ref2=ref2,
            max_distance=max_distance,
            reason=reason,
            locked_by=locked_by,
        )
        self._manifest.relative_constraints.append(constraint)
        return constraint

    def remove_relative_constraints(self, ref: str) -> int:
        """
        Remove all relative constraints involving a component.

        Args:
            ref: Component reference

        Returns:
            Number of constraints removed
        """
        before = len(self._manifest.relative_constraints)
        self._manifest.relative_constraints = [
            c for c in self._manifest.relative_constraints if c.ref1 != ref and c.ref2 != ref
        ]
        return before - len(self._manifest.relative_constraints)

    @property
    def relative_constraints(self) -> list[RelativeConstraint]:
        """Return list of relative constraints."""
        return self._manifest.relative_constraints.copy()

    # =========================================================================
    # Integration with Optimizers
    # =========================================================================

    def apply_to_placer(self, placer: PlacementOptimizer) -> int:
        """
        Apply constraints to a placement optimizer.

        Sets the `fixed` flag on components that have position locks.

        Args:
            placer: PlacementOptimizer instance

        Returns:
            Number of components marked as fixed
        """
        fixed_count = 0
        for ref, lock in self._manifest.component_locks.items():
            if lock.locks_position():
                comp = placer.get_component(ref)
                if comp:
                    comp.fixed = True
                    fixed_count += 1
        return fixed_count

    def capture_positions_from_placer(self, placer: PlacementOptimizer) -> int:
        """
        Capture current positions/rotations from placer for locked components.

        For components that have locks but no stored position/rotation,
        captures the current values from the placer.

        Args:
            placer: PlacementOptimizer instance

        Returns:
            Number of positions captured
        """
        captured = 0
        for ref, lock in self._manifest.component_locks.items():
            comp = placer.get_component(ref)
            if comp:
                if lock.locks_position() and lock.position is None:
                    lock.position = (comp.x, comp.y)
                    captured += 1
                if lock.locks_rotation() and lock.rotation is None:
                    lock.rotation = comp.rotation
                    captured += 1
        return captured

    # =========================================================================
    # Validation
    # =========================================================================

    def validate_placement(self, placer: PlacementOptimizer) -> list[ConstraintViolation]:
        """
        Validate that a placement respects all constraints.

        Args:
            placer: PlacementOptimizer with current component positions

        Returns:
            List of constraint violations
        """
        violations: list[ConstraintViolation] = []

        # Check component locks
        for ref, lock in self._manifest.component_locks.items():
            comp = placer.get_component(ref)
            if not comp:
                continue

            if lock.locks_position() and lock.position is not None:
                dx = abs(comp.x - lock.position[0])
                dy = abs(comp.y - lock.position[1])
                if dx > 0.01 or dy > 0.01:
                    violations.append(
                        ConstraintViolation(
                            constraint_type="component",
                            constraint_name=ref,
                            message=(
                                f"{ref} position changed from "
                                f"({lock.position[0]:.2f}, {lock.position[1]:.2f}) to "
                                f"({comp.x:.2f}, {comp.y:.2f}). "
                                f"Reason for lock: {lock.reason}"
                            ),
                            location=(comp.x, comp.y),
                        )
                    )

            if lock.locks_rotation() and lock.rotation is not None:
                # Normalize rotation difference to [-180, 180]
                diff = (comp.rotation - lock.rotation + 180) % 360 - 180
                if abs(diff) > 0.1:
                    violations.append(
                        ConstraintViolation(
                            constraint_type="component",
                            constraint_name=ref,
                            message=(
                                f"{ref} rotation changed from "
                                f"{lock.rotation:.1f}° to {comp.rotation:.1f}°. "
                                f"Reason for lock: {lock.reason}"
                            ),
                            location=(comp.x, comp.y),
                        )
                    )

        # Check region constraints
        for region_name, region in self._manifest.region_constraints.items():
            for comp in placer.components:
                in_region = region.contains_point(comp.x, comp.y)

                if in_region and not region.is_component_allowed(comp.ref):
                    violations.append(
                        ConstraintViolation(
                            constraint_type="region",
                            constraint_name=region_name,
                            message=(
                                f"{comp.ref} is in region '{region_name}' but is not allowed. "
                                f"Reason: {region.reason}"
                            ),
                            location=(comp.x, comp.y),
                        )
                    )

        # Check relative constraints
        for rel in self._manifest.relative_constraints:
            comp1 = placer.get_component(rel.ref1)
            comp2 = placer.get_component(rel.ref2)

            if comp1 and comp2:
                satisfied, message = rel.check_satisfied((comp1.x, comp1.y), (comp2.x, comp2.y))
                if not satisfied:
                    violations.append(
                        ConstraintViolation(
                            constraint_type="relative",
                            constraint_name=f"{rel.ref1}-{rel.ref2}",
                            message=message,
                            location=((comp1.x + comp2.x) / 2, (comp1.y + comp2.y) / 2),
                        )
                    )

        return violations

    def validate_pcb_state(
        self,
        before: PCBState,
        after: PCBState,
    ) -> list[ConstraintViolation]:
        """
        Validate that a PCB state change respects all constraints.

        Args:
            before: PCB state before optimization
            after: PCB state after optimization

        Returns:
            List of constraint violations
        """
        violations: list[ConstraintViolation] = []

        # Check component position locks
        for ref, lock in self._manifest.component_locks.items():
            before_comp = before.get_component(ref)
            after_comp = after.get_component(ref)

            if not before_comp or not after_comp:
                continue

            if lock.locks_position():
                dx = abs(after_comp.x - before_comp.x)
                dy = abs(after_comp.y - before_comp.y)
                if dx > 0.01 or dy > 0.01:
                    violations.append(
                        ConstraintViolation(
                            constraint_type="component",
                            constraint_name=ref,
                            message=(
                                f"{ref} position changed from "
                                f"({before_comp.x:.2f}, {before_comp.y:.2f}) to "
                                f"({after_comp.x:.2f}, {after_comp.y:.2f}). "
                                f"Lock reason: {lock.reason}"
                            ),
                            location=(after_comp.x, after_comp.y),
                        )
                    )

            if lock.locks_rotation():
                diff = (after_comp.rotation - before_comp.rotation + 180) % 360 - 180
                if abs(diff) > 0.1:
                    violations.append(
                        ConstraintViolation(
                            constraint_type="component",
                            constraint_name=ref,
                            message=(
                                f"{ref} rotation changed from "
                                f"{before_comp.rotation:.1f}° to {after_comp.rotation:.1f}°. "
                                f"Lock reason: {lock.reason}"
                            ),
                            location=(after_comp.x, after_comp.y),
                        )
                    )

        return violations

    # =========================================================================
    # Persistence
    # =========================================================================

    def save(self, path: Path | str, format: str = "auto") -> None:
        """
        Save constraints to file.

        Args:
            path: File path to save to
            format: "yaml", "json", or "auto" (detect from extension)
        """
        self._manifest.save(path, format)

    @classmethod
    def load(cls, path: Path | str) -> ConstraintManager:
        """
        Load constraints from file.

        Args:
            path: File path to load from

        Returns:
            ConstraintManager with loaded constraints
        """
        manifest = ConstraintManifest.load(path)
        return cls(manifest)

    @classmethod
    def from_pcb_directory(cls, pcb_dir: Path | str) -> ConstraintManager:
        """
        Load constraints from a PCB directory.

        Searches for .constraints.yaml or .constraints.json.

        Args:
            pcb_dir: Directory containing the PCB file

        Returns:
            ConstraintManager (empty if no manifest found)
        """
        manifest = ConstraintManifest.find_and_load(pcb_dir)
        return cls(manifest)

    def save_to_pcb_directory(self, pcb_dir: Path | str, format: str = "yaml") -> Path:
        """
        Save constraints to a PCB directory.

        Args:
            pcb_dir: Directory containing the PCB file
            format: "yaml" or "json"

        Returns:
            Path to the saved file
        """
        pcb_dir = Path(pcb_dir)
        if format == "yaml":
            path = pcb_dir / YAML_MANIFEST_NAME
        else:
            path = pcb_dir / ".constraints.json"

        self._manifest.save(path, format)
        return path

    # =========================================================================
    # Utility
    # =========================================================================

    def clear(self) -> None:
        """Remove all constraints."""
        self._manifest = ConstraintManifest()

    def is_empty(self) -> bool:
        """Return True if there are no constraints."""
        return self._manifest.is_empty()

    @property
    def manifest(self) -> ConstraintManifest:
        """Return the underlying manifest (for serialization)."""
        return self._manifest

    def summary(self) -> dict[str, int]:
        """Return a summary of constraint counts."""
        return {
            "component_locks": len(self._manifest.component_locks),
            "net_route_locks": len(self._manifest.net_route_locks),
            "region_constraints": len(self._manifest.region_constraints),
            "relative_constraints": len(self._manifest.relative_constraints),
        }

    def __repr__(self) -> str:
        s = self.summary()
        return (
            f"ConstraintManager("
            f"components={s['component_locks']}, "
            f"nets={s['net_route_locks']}, "
            f"regions={s['region_constraints']}, "
            f"relative={s['relative_constraints']})"
        )
