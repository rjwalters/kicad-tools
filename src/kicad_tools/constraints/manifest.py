"""
Constraint manifest serialization and deserialization.

Supports both YAML and JSON formats for constraint manifests.
YAML is preferred for human editing, JSON is always available.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .locks import ComponentLock, LockType, NetRouteLock, RegionConstraint, RelativeConstraint

if TYPE_CHECKING:
    pass

# Try to import YAML, fall back to JSON-only mode if not available
try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# Standard manifest filenames
YAML_MANIFEST_NAME = ".constraints.yaml"
JSON_MANIFEST_NAME = ".constraints.json"


class ManifestEncoder(json.JSONEncoder):
    """Custom JSON encoder for constraint data types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, LockType):
            return obj.value
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        return super().default(obj)


def _serialize_component_lock(lock: ComponentLock) -> dict[str, Any]:
    """Serialize a ComponentLock to a dictionary."""
    result: dict[str, Any] = {
        "lock": lock.lock_type.value,
        "reason": lock.reason,
        "locked_by": lock.locked_by,
        "timestamp": lock.timestamp.isoformat(),
    }
    if lock.position is not None:
        result["position"] = list(lock.position)
    if lock.rotation is not None:
        result["rotation"] = lock.rotation
    return result


def _deserialize_component_lock(ref: str, data: dict[str, Any]) -> ComponentLock:
    """Deserialize a ComponentLock from a dictionary."""
    lock_value = data.get("lock", "full")
    if isinstance(lock_value, list):
        # Handle [position, rotation] format from YAML
        if "position" in lock_value and "rotation" in lock_value:
            lock_type = LockType.FULL
        elif "position" in lock_value:
            lock_type = LockType.POSITION
        elif "rotation" in lock_value:
            lock_type = LockType.ROTATION
        else:
            lock_type = LockType.FULL
    else:
        lock_type = LockType(lock_value)

    timestamp_str = data.get("timestamp")
    if timestamp_str:
        timestamp = datetime.fromisoformat(timestamp_str)
    else:
        timestamp = datetime.now()

    position = data.get("position")
    if position is not None:
        position = tuple(position)

    return ComponentLock(
        ref=ref,
        lock_type=lock_type,
        reason=data.get("reason", ""),
        locked_by=data.get("locked_by", ""),
        timestamp=timestamp,
        position=position,
        rotation=data.get("rotation"),
    )


def _serialize_net_route_lock(lock: NetRouteLock) -> dict[str, Any]:
    """Serialize a NetRouteLock to a dictionary."""
    return {
        "lock": "route",
        "reason": lock.reason,
        "locked_by": lock.locked_by,
        "timestamp": lock.timestamp.isoformat(),
        "trace_geometry": [list(seg) for seg in lock.trace_geometry],
        "via_positions": [[v[0], v[1], list(v[2])] for v in lock.via_positions],
    }


def _deserialize_net_route_lock(net_name: str, data: dict[str, Any]) -> NetRouteLock:
    """Deserialize a NetRouteLock from a dictionary."""
    timestamp_str = data.get("timestamp")
    if timestamp_str:
        timestamp = datetime.fromisoformat(timestamp_str)
    else:
        timestamp = datetime.now()

    trace_geometry = [tuple(seg) for seg in data.get("trace_geometry", [])]
    via_positions = [(v[0], v[1], tuple(v[2])) for v in data.get("via_positions", [])]

    return NetRouteLock(
        net_name=net_name,
        reason=data.get("reason", ""),
        locked_by=data.get("locked_by", ""),
        timestamp=timestamp,
        trace_geometry=trace_geometry,
        via_positions=via_positions,
    )


def _serialize_region_constraint(region: RegionConstraint) -> dict[str, Any]:
    """Serialize a RegionConstraint to a dictionary."""
    result: dict[str, Any] = {
        "bounds": region.bounds,
        "reason": region.reason,
    }
    if region.locked_by:
        result["locked_by"] = region.locked_by
    if region.allowed_nets:
        result["allowed_nets"] = region.allowed_nets
    if region.disallowed_nets:
        result["disallowed_nets"] = region.disallowed_nets
    if region.allowed_components:
        result["allowed_components"] = region.allowed_components
    if region.disallowed_components:
        result["disallowed_components"] = region.disallowed_components
    return result


def _deserialize_region_constraint(name: str, data: dict[str, Any]) -> RegionConstraint:
    """Deserialize a RegionConstraint from a dictionary."""
    return RegionConstraint(
        name=name,
        bounds=data.get("bounds", {}),
        reason=data.get("reason", ""),
        locked_by=data.get("locked_by", ""),
        allowed_nets=data.get("allowed_nets", []),
        disallowed_nets=data.get("disallowed_nets", []),
        allowed_components=data.get("allowed_components", []),
        disallowed_components=data.get("disallowed_components", []),
    )


def _serialize_relative_constraint(constraint: RelativeConstraint) -> list[Any]:
    """Serialize a RelativeConstraint to a list (YAML-friendly format)."""
    result: list[Any] = [
        constraint.ref1,
        constraint.relation,
        constraint.ref2,
    ]
    if constraint.max_distance is not None:
        result.append(f"{constraint.max_distance}mm")
    if constraint.reason:
        result.append(constraint.reason)
    return result


def _deserialize_relative_constraint(data: list[Any]) -> RelativeConstraint:
    """Deserialize a RelativeConstraint from a list."""
    ref1 = data[0]
    relation = data[1]
    ref2 = data[2]
    max_distance = None
    reason = ""

    for item in data[3:]:
        if isinstance(item, str):
            if item.endswith("mm"):
                try:
                    max_distance = float(item[:-2])
                except ValueError:
                    reason = item
            else:
                reason = item
        elif isinstance(item, (int, float)):
            max_distance = float(item)

    return RelativeConstraint(
        ref1=ref1,
        relation=relation,
        ref2=ref2,
        max_distance=max_distance,
        reason=reason,
    )


class ConstraintManifest:
    """
    Container for all constraints with serialization support.

    Attributes:
        component_locks: Dict mapping component ref to ComponentLock
        net_route_locks: Dict mapping net name to NetRouteLock
        region_constraints: Dict mapping region name to RegionConstraint
        relative_constraints: List of RelativeConstraint objects
    """

    def __init__(self):
        self.component_locks: dict[str, ComponentLock] = {}
        self.net_route_locks: dict[str, NetRouteLock] = {}
        self.region_constraints: dict[str, RegionConstraint] = {}
        self.relative_constraints: list[RelativeConstraint] = []

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to a dictionary for serialization."""
        result: dict[str, Any] = {}

        if self.component_locks:
            result["components"] = {
                ref: _serialize_component_lock(lock) for ref, lock in self.component_locks.items()
            }

        if self.net_route_locks:
            result["nets"] = {
                name: _serialize_net_route_lock(lock) for name, lock in self.net_route_locks.items()
            }

        if self.region_constraints:
            result["regions"] = {
                name: _serialize_region_constraint(region)
                for name, region in self.region_constraints.items()
            }

        if self.relative_constraints:
            result["relative"] = [
                _serialize_relative_constraint(c) for c in self.relative_constraints
            ]

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConstraintManifest:
        """Create manifest from a dictionary."""
        manifest = cls()

        for ref, lock_data in data.get("components", {}).items():
            manifest.component_locks[ref] = _deserialize_component_lock(ref, lock_data)

        for net_name, lock_data in data.get("nets", {}).items():
            manifest.net_route_locks[net_name] = _deserialize_net_route_lock(net_name, lock_data)

        for region_name, region_data in data.get("regions", {}).items():
            manifest.region_constraints[region_name] = _deserialize_region_constraint(
                region_name, region_data
            )

        for rel_data in data.get("relative", []):
            manifest.relative_constraints.append(_deserialize_relative_constraint(rel_data))

        return manifest

    def to_yaml(self) -> str:
        """Serialize manifest to YAML string."""
        if not HAS_YAML:
            raise ImportError(
                "PyYAML is required for YAML serialization. Install with: pip install pyyaml"
            )
        return yaml.dump(self.to_dict(), default_flow_style=False, sort_keys=False)

    def to_json(self, indent: int = 2) -> str:
        """Serialize manifest to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, cls=ManifestEncoder)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> ConstraintManifest:
        """Create manifest from YAML string."""
        if not HAS_YAML:
            raise ImportError(
                "PyYAML is required for YAML parsing. Install with: pip install pyyaml"
            )
        data = yaml.safe_load(yaml_str) or {}
        return cls.from_dict(data)

    @classmethod
    def from_json(cls, json_str: str) -> ConstraintManifest:
        """Create manifest from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def save(self, path: Path | str, format: str = "auto") -> None:
        """
        Save manifest to file.

        Args:
            path: File path to save to
            format: "yaml", "json", or "auto" (detect from extension)
        """
        path = Path(path)

        if format == "auto":
            if path.suffix in (".yaml", ".yml"):
                format = "yaml"
            else:
                format = "json"

        if format == "yaml":
            path.write_text(self.to_yaml())
        else:
            path.write_text(self.to_json())

    @classmethod
    def load(cls, path: Path | str) -> ConstraintManifest:
        """
        Load manifest from file.

        Args:
            path: File path to load from

        Returns:
            Loaded ConstraintManifest
        """
        path = Path(path)
        content = path.read_text()

        if path.suffix in (".yaml", ".yml"):
            return cls.from_yaml(content)
        else:
            return cls.from_json(content)

    @classmethod
    def find_and_load(cls, pcb_dir: Path | str) -> ConstraintManifest | None:
        """
        Find and load constraint manifest from a PCB directory.

        Searches for .constraints.yaml first, then .constraints.json.

        Args:
            pcb_dir: Directory containing the PCB file

        Returns:
            Loaded manifest or None if not found
        """
        pcb_dir = Path(pcb_dir)

        yaml_path = pcb_dir / YAML_MANIFEST_NAME
        if yaml_path.exists():
            return cls.load(yaml_path)

        json_path = pcb_dir / JSON_MANIFEST_NAME
        if json_path.exists():
            return cls.load(json_path)

        return None

    def is_empty(self) -> bool:
        """Return True if the manifest has no constraints."""
        return (
            not self.component_locks
            and not self.net_route_locks
            and not self.region_constraints
            and not self.relative_constraints
        )

    def __repr__(self) -> str:
        return (
            f"ConstraintManifest("
            f"components={len(self.component_locks)}, "
            f"nets={len(self.net_route_locks)}, "
            f"regions={len(self.region_constraints)}, "
            f"relative={len(self.relative_constraints)})"
        )
