"""
YAML pattern loader for user-defined PCB patterns.

This module provides functionality to load PCB patterns from YAML files,
allowing users to define custom patterns without writing Python code.

Example YAML pattern::

    name: temperature_sensor
    description: NTC thermistor with filtering and protection

    components:
      - role: thermistor
        reference_prefix: RT
        value_range: [10k, 100k]
      - role: bias_resistor
        reference_prefix: R
        value: match_thermistor
      - role: filter_cap
        reference_prefix: C
        value_range: [100n, 1u]
      - role: protection_diode
        reference_prefix: D
        optional: true

    placement_rules:
      - component: filter_cap
        relative_to: thermistor
        max_distance_mm: 5
        rationale: "Filter cap close to sensor for noise rejection"

    validation:
      - check: component_distance
        params:
          from_component: thermistor
          to_component: filter_cap
          max_mm: 5

Usage::

    from kicad_tools.patterns.loader import PatternLoader

    loader = PatternLoader()
    pattern, metadata = loader.load("patterns/my_sensor.yaml")

    # Use the pattern
    placements = pattern.get_placements(anchor_at=(50, 50))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .base import PCBPattern
from .checks import CheckContext, ValidationCheck, create_check
from .schema import (
    PatternSpec,
    PatternViolation,
    Placement,
    PlacementPriority,
    PlacementRule,
    RoutingConstraint,
)


@dataclass
class ComponentDefinition:
    """Definition of a component in a YAML pattern.

    Attributes:
        role: Role name for this component (e.g., "thermistor", "filter_cap")
        reference_prefix: Expected reference prefix (e.g., "R", "C", "U")
        value: Expected value or "match_<other_role>" to match another component
        value_range: Allowed value range as [min, max] strings
        optional: Whether this component is optional
    """

    role: str
    reference_prefix: str = ""
    value: str | None = None
    value_range: tuple[str, str] | None = None
    optional: bool = False


@dataclass
class YAMLPatternConfig:
    """Configuration parsed from a YAML pattern file.

    Attributes:
        name: Pattern name
        description: Human-readable description
        category: Category for organization
        components: List of component definitions
        placement_rules: Placement rules from YAML
        routing_constraints: Routing constraints from YAML
        validation_checks: Validation check specifications
    """

    name: str
    description: str = ""
    category: str = "custom"
    components: list[ComponentDefinition] = field(default_factory=list)
    placement_rules: list[dict[str, Any]] = field(default_factory=list)
    routing_constraints: list[dict[str, Any]] = field(default_factory=list)
    validation_checks: list[dict[str, Any]] = field(default_factory=list)


class YAMLPattern(PCBPattern):
    """A PCB pattern loaded from a YAML definition.

    This class wraps a YAML pattern configuration and provides the
    standard PCBPattern interface for placements and validation.
    """

    def __init__(self, config: YAMLPatternConfig, **kwargs: Any) -> None:
        """Initialize from YAML configuration.

        Args:
            config: Parsed YAML configuration
            **kwargs: Additional configuration parameters
        """
        super().__init__(**kwargs)
        self._yaml_config = config
        self._checks: list[ValidationCheck] = []
        self._build_checks()

    def _build_checks(self) -> None:
        """Build validation check instances from YAML spec."""
        for check_spec in self._yaml_config.validation_checks:
            check_name = check_spec.get("check", "")
            params = check_spec.get("params", {})
            try:
                check = create_check(check_name, params)
                self._checks.append(check)
            except (KeyError, TypeError) as e:
                import warnings

                warnings.warn(f"Failed to create check '{check_name}': {e}", stacklevel=2)

    def _build_spec(self) -> PatternSpec:
        """Build the pattern specification from YAML config."""
        placement_rules = []
        for rule_data in self._yaml_config.placement_rules:
            rule = PlacementRule(
                component=rule_data.get("component", ""),
                relative_to=rule_data.get("relative_to", ""),
                max_distance_mm=float(rule_data.get("max_distance_mm", 10.0)),
                min_distance_mm=float(rule_data.get("min_distance_mm", 0.0)),
                preferred_angle=rule_data.get("preferred_angle"),
                rationale=rule_data.get("rationale", ""),
                priority=PlacementPriority(rule_data.get("priority", "high").lower()),
            )
            placement_rules.append(rule)

        routing_constraints = []
        for constraint_data in self._yaml_config.routing_constraints:
            constraint = RoutingConstraint(
                net_role=constraint_data.get("net_role", constraint_data.get("nets", [""])[0]),
                min_width_mm=float(constraint_data.get("min_width_mm", 0.2)),
                max_length_mm=constraint_data.get("max_length_mm"),
                via_allowed=constraint_data.get("via_allowed", True),
                plane_connection=constraint_data.get("plane_connection", False),
                rationale=constraint_data.get("rationale", ""),
            )
            routing_constraints.append(constraint)

        return PatternSpec(
            name=self._yaml_config.name,
            description=self._yaml_config.description,
            components=[c.role for c in self._yaml_config.components],
            placement_rules=placement_rules,
            routing_constraints=routing_constraints,
        )

    def get_placements(self, anchor_at: tuple[float, float]) -> dict[str, Placement]:
        """Calculate placements based on YAML rules.

        For YAML patterns, placements are computed from the placement rules
        using preferred angles and distances.

        Args:
            anchor_at: Position of the anchor component

        Returns:
            Dictionary mapping component roles to Placement objects
        """
        placements: dict[str, Placement] = {}

        # Find the anchor component (first component or one specified)
        anchor_role = self._yaml_config.components[0].role if self._yaml_config.components else ""

        for rule in self.spec.placement_rules:
            if rule.relative_to == anchor_role:
                # Calculate position based on preferred angle and distance
                angle = rule.preferred_angle if rule.preferred_angle is not None else 0.0
                # Use midpoint of min/max distance
                distance = (rule.min_distance_mm + rule.max_distance_mm) / 2

                position = self._calculate_position(anchor_at, distance, angle)

                placements[rule.component] = Placement(
                    position=position,
                    rotation=0.0,
                    rationale=rule.rationale,
                )

        return placements

    def validate(self, pcb_path: Path | str) -> list[PatternViolation]:
        """Validate a PCB against this pattern.

        Runs all validation checks defined in the YAML and the
        standard placement rule checks.

        Args:
            pcb_path: Path to the KiCad PCB file

        Returns:
            List of violations found
        """
        violations: list[PatternViolation] = []

        # Build context for validation
        # In a real implementation, this would parse the PCB file
        # For now, we return a placeholder that can be expanded
        context = self._build_context(pcb_path)

        # Run YAML-defined validation checks
        for check in self._checks:
            violation = check.validate(context)
            if violation is not None:
                violations.append(violation)

        # Run standard placement rule validation
        for rule in self.spec.placement_rules:
            component_ref = self.component_map.get(rule.component, rule.component)
            anchor_ref = self.component_map.get(rule.relative_to, rule.relative_to)

            component_pos = context.component_positions.get(component_ref)
            anchor_pos = context.component_positions.get(anchor_ref)

            if component_pos is not None and anchor_pos is not None:
                violation = self._validate_placement_rule(rule, component_pos, anchor_pos)
                if violation is not None:
                    violations.append(violation)

        return violations

    def _build_context(self, pcb_path: Path | str) -> CheckContext:
        """Build a CheckContext from a PCB file.

        This is a placeholder that would be expanded to actually
        parse the KiCad PCB file.

        Args:
            pcb_path: Path to the PCB file

        Returns:
            CheckContext with PCB data
        """
        # TODO: Implement actual PCB parsing
        # For now, return empty context
        return CheckContext(
            component_positions={},
            component_values={},
            component_footprints={},
            net_lengths={},
            pcb_path=Path(pcb_path) if isinstance(pcb_path, str) else pcb_path,
        )

    @property
    def components(self) -> list[ComponentDefinition]:
        """Get the component definitions from the YAML config."""
        return self._yaml_config.components


class PatternLoader:
    """Loader for YAML pattern definitions.

    Parses YAML files and creates YAMLPattern instances that can be
    used like any other PCBPattern.

    Example::

        loader = PatternLoader()
        pattern, metadata = loader.load("patterns/sensor.yaml")

        # Use the pattern
        placements = pattern.get_placements((50, 50))
    """

    def load(self, path: str | Path) -> tuple[YAMLPattern, dict[str, Any]]:
        """Load a pattern from a YAML file.

        Args:
            path: Path to the YAML file

        Returns:
            Tuple of (YAMLPattern instance, metadata dict)

        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the YAML is invalid
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Pattern file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Invalid pattern file: expected dict, got {type(data).__name__}")

        config = self._parse_config(data)
        pattern = YAMLPattern(config)

        metadata = {
            "name": config.name,
            "description": config.description,
            "category": config.category,
            "source_file": str(path),
        }

        return pattern, metadata

    def _parse_config(self, data: dict[str, Any]) -> YAMLPatternConfig:
        """Parse YAML data into a YAMLPatternConfig.

        Args:
            data: Parsed YAML dictionary

        Returns:
            YAMLPatternConfig instance
        """
        # Parse components
        components = []
        for comp_data in data.get("components", []):
            value_range = None
            if "value_range" in comp_data:
                vr = comp_data["value_range"]
                if isinstance(vr, list) and len(vr) == 2:
                    value_range = (str(vr[0]), str(vr[1]))

            comp = ComponentDefinition(
                role=comp_data.get("role", ""),
                reference_prefix=comp_data.get("reference_prefix", ""),
                value=comp_data.get("value"),
                value_range=value_range,
                optional=comp_data.get("optional", False),
            )
            components.append(comp)

        return YAMLPatternConfig(
            name=data.get("name", "unnamed"),
            description=data.get("description", ""),
            category=data.get("category", "custom"),
            components=components,
            placement_rules=data.get("placement_rules", []),
            routing_constraints=data.get("routing_constraints", []),
            validation_checks=data.get("validation", []),
        )

    def load_string(
        self, yaml_string: str, name: str = "unnamed"
    ) -> tuple[YAMLPattern, dict[str, Any]]:
        """Load a pattern from a YAML string.

        Useful for testing or dynamically generated patterns.

        Args:
            yaml_string: YAML content as a string
            name: Default name if not specified in YAML

        Returns:
            Tuple of (YAMLPattern instance, metadata dict)
        """
        data = yaml.safe_load(yaml_string)
        if not isinstance(data, dict):
            raise ValueError(f"Invalid pattern: expected dict, got {type(data).__name__}")

        if "name" not in data:
            data["name"] = name

        config = self._parse_config(data)
        pattern = YAMLPattern(config)

        metadata = {
            "name": config.name,
            "description": config.description,
            "category": config.category,
            "source_file": None,
        }

        return pattern, metadata
