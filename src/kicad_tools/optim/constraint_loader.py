"""
YAML loader for grouping constraints.

Provides functionality to load grouping constraints from YAML configuration files,
enabling users to define component grouping rules in a human-readable format.

Example YAML format:
    groups:
      - name: status_leds
        members: ["LED1", "LED2", "LED3", "LED4"]
        constraints:
          - type: alignment
            axis: horizontal
            tolerance_mm: 0.5
          - type: ordering
            axis: horizontal
            order: ["LED1", "LED2", "LED3", "LED4"]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from kicad_tools.optim.constraints import (
    ConstraintType,
    GroupingConstraint,
    SpatialConstraint,
)

__all__ = ["load_constraints_from_yaml", "save_constraints_to_yaml"]


def load_constraints_from_yaml(path: str | Path) -> list[GroupingConstraint]:
    """
    Load grouping constraints from a YAML file.

    Args:
        path: Path to YAML constraint file

    Returns:
        List of GroupingConstraint objects

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If YAML format is invalid
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Constraint file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if not data:
        return []

    return _parse_constraints(data)


def save_constraints_to_yaml(
    constraints: list[GroupingConstraint],
    path: str | Path,
) -> None:
    """
    Save grouping constraints to a YAML file.

    Args:
        constraints: List of GroupingConstraint objects to save
        path: Output file path
    """
    path = Path(path)
    data = _serialize_constraints(constraints)

    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _parse_constraints(data: dict[str, Any]) -> list[GroupingConstraint]:
    """Parse constraint data from YAML dict."""
    groups = data.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("'groups' must be a list")

    result = []
    for group_data in groups:
        result.append(_parse_group(group_data))

    return result


def _parse_group(data: dict[str, Any]) -> GroupingConstraint:
    """Parse a single grouping constraint from dict."""
    if not isinstance(data, dict):
        raise ValueError(f"Group must be a dict, got {type(data)}")

    name = data.get("name")
    if not name:
        raise ValueError("Group must have a 'name' field")

    members = data.get("members", [])
    if not isinstance(members, list):
        raise ValueError(f"'members' must be a list, got {type(members)}")

    constraints_data = data.get("constraints", [])
    constraints = [_parse_spatial_constraint(c) for c in constraints_data]

    return GroupingConstraint(
        name=name,
        members=members,
        constraints=constraints,
    )


def _parse_spatial_constraint(data: dict[str, Any]) -> SpatialConstraint:
    """Parse a single spatial constraint from dict."""
    if not isinstance(data, dict):
        raise ValueError(f"Constraint must be a dict, got {type(data)}")

    type_str = data.get("type")
    if not type_str:
        raise ValueError("Constraint must have a 'type' field")

    # Map type string to enum
    try:
        constraint_type = ConstraintType(type_str)
    except ValueError:
        valid_types = [t.value for t in ConstraintType]
        raise ValueError(f"Unknown constraint type '{type_str}', must be one of: {valid_types}")

    # Extract parameters (all fields except 'type')
    parameters = {k: v for k, v in data.items() if k != "type"}

    # Validate required parameters for each type
    _validate_constraint_params(constraint_type, parameters)

    return SpatialConstraint(
        constraint_type=constraint_type,
        parameters=parameters,
    )


def _validate_constraint_params(
    constraint_type: ConstraintType,
    params: dict[str, Any],
) -> None:
    """Validate constraint parameters based on type."""
    if constraint_type == ConstraintType.MAX_DISTANCE:
        if "anchor" not in params:
            raise ValueError("max_distance constraint requires 'anchor' parameter")
        if "radius_mm" not in params:
            raise ValueError("max_distance constraint requires 'radius_mm' parameter")
        if not isinstance(params["radius_mm"], (int, float)):
            raise ValueError("'radius_mm' must be a number")

    elif constraint_type == ConstraintType.ALIGNMENT:
        if "axis" not in params:
            raise ValueError("alignment constraint requires 'axis' parameter")
        if params["axis"] not in ("horizontal", "vertical"):
            raise ValueError("'axis' must be 'horizontal' or 'vertical'")

    elif constraint_type == ConstraintType.ORDERING:
        if "axis" not in params:
            raise ValueError("ordering constraint requires 'axis' parameter")
        if "order" not in params:
            raise ValueError("ordering constraint requires 'order' parameter")
        if params["axis"] not in ("horizontal", "vertical"):
            raise ValueError("'axis' must be 'horizontal' or 'vertical'")
        if not isinstance(params["order"], list):
            raise ValueError("'order' must be a list")

    elif constraint_type == ConstraintType.WITHIN_BOX:
        required = ["x", "y", "width", "height"]
        for field in required:
            if field not in params:
                raise ValueError(f"within_box constraint requires '{field}' parameter")
            if not isinstance(params[field], (int, float)):
                raise ValueError(f"'{field}' must be a number")

    elif constraint_type == ConstraintType.RELATIVE_POSITION:
        if "reference" not in params:
            raise ValueError("relative_position constraint requires 'reference' parameter")
        if "dx" not in params or "dy" not in params:
            raise ValueError("relative_position constraint requires 'dx' and 'dy' parameters")


def _serialize_constraints(constraints: list[GroupingConstraint]) -> dict[str, Any]:
    """Serialize constraints to YAML-compatible dict."""
    groups = []
    for constraint in constraints:
        group_data = {
            "name": constraint.name,
            "members": constraint.members,
            "constraints": [_serialize_spatial_constraint(c) for c in constraint.constraints],
        }
        groups.append(group_data)

    return {"groups": groups}


def _serialize_spatial_constraint(constraint: SpatialConstraint) -> dict[str, Any]:
    """Serialize a spatial constraint to dict."""
    data = {"type": constraint.constraint_type.value}
    data.update(constraint.parameters)
    return data
