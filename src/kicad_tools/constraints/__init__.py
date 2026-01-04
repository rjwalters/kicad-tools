"""
Constraint locking system for multi-stage PCB optimization.

This module provides a constraint management system that allows optimizers
to "lock" components, nets, and regions in place. This enables multi-stage
optimization pipelines where early stages make structural decisions that
later stages must preserve.

Example:
    >>> from kicad_tools.constraints import ConstraintManager, LockType
    >>>
    >>> # Create constraint manager
    >>> cm = ConstraintManager()
    >>>
    >>> # Lock a component at the domain boundary
    >>> cm.lock_component("FB1", LockType.POSITION,
    ...                   "Analog/digital domain boundary",
    ...                   "domain_analyzer")
    >>>
    >>> # Apply constraints to placement optimizer
    >>> cm.apply_to_placer(placer)
    >>> placer.run(iterations=1000)
    >>>
    >>> # Validate that placement respected constraints
    >>> violations = cm.validate_placement(placer)
    >>> if violations:
    ...     for v in violations:
    ...         print(f"Violation: {v.message}")
    >>>
    >>> # Save constraints to disk
    >>> cm.save(".constraints.yaml")

Classes:
    ConstraintManager: Main interface for managing constraints
    LockType: Enum for component lock types (POSITION, ROTATION, FULL)
    ComponentLock: Lock on a component's position/rotation
    NetRouteLock: Lock on a routed net's trace geometry
    RegionConstraint: Constraint on which components/nets can be in a region
    RelativeConstraint: Constraint on relative positions between components
    ConstraintViolation: A detected violation of a constraint
    ConstraintManifest: Container for all constraints with serialization
"""

from .conflict import (
    ConflictResolution,
    ConflictType,
    ConstraintConflict,
    ConstraintConflictDetector,
)
from .locks import (
    ComponentLock,
    LockType,
    NetRouteLock,
    RegionConstraint,
    RelativeConstraint,
)
from .manager import ConstraintManager, ConstraintViolation
from .manifest import ConstraintManifest

__all__ = [
    # Main interface
    "ConstraintManager",
    "ConstraintViolation",
    # Lock types
    "LockType",
    "ComponentLock",
    "NetRouteLock",
    "RegionConstraint",
    "RelativeConstraint",
    # Conflict detection
    "ConflictType",
    "ConflictResolution",
    "ConstraintConflict",
    "ConstraintConflictDetector",
    # Serialization
    "ConstraintManifest",
]
