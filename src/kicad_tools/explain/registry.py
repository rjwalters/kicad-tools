"""Explanation registry for design rules and interface specs.

This module provides a central registry for rule explanations and interface
specifications. It supports loading explanations from YAML files and provides
lookup by rule ID.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import InterfaceSpec, RuleExplanation, SpecReference

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Default specs directory
SPECS_DIR = Path(__file__).parent / "specs"


class ExplanationRegistry:
    """Registry of rule explanations.

    This class maintains a mapping from rule IDs to their explanations.
    Explanations can be registered programmatically or loaded from YAML files.
    """

    _explanations: dict[str, RuleExplanation] = {}
    _interfaces: dict[str, InterfaceSpec] = {}
    _loaded: bool = False

    @classmethod
    def register(cls, rule_id: str, explanation: RuleExplanation) -> None:
        """Register an explanation for a rule.

        Args:
            rule_id: Unique identifier for the rule
            explanation: The explanation to register
        """
        cls._explanations[rule_id] = explanation

    @classmethod
    def register_interface(cls, interface_name: str, spec: InterfaceSpec) -> None:
        """Register an interface specification.

        Args:
            interface_name: Name of the interface (e.g., "usb2_hs")
            spec: The interface specification
        """
        cls._interfaces[interface_name] = spec

    @classmethod
    def get(cls, rule_id: str) -> RuleExplanation | None:
        """Get an explanation by rule ID.

        Args:
            rule_id: The rule identifier to look up

        Returns:
            The RuleExplanation if found, None otherwise
        """
        cls._ensure_loaded()
        return cls._explanations.get(rule_id)

    @classmethod
    def get_interface(cls, interface_name: str) -> InterfaceSpec | None:
        """Get an interface specification by name.

        Args:
            interface_name: The interface name to look up

        Returns:
            The InterfaceSpec if found, None otherwise
        """
        cls._ensure_loaded()
        return cls._interfaces.get(interface_name)

    @classmethod
    def list_rules(cls) -> list[str]:
        """List all registered rule IDs.

        Returns:
            List of rule ID strings
        """
        cls._ensure_loaded()
        return sorted(cls._explanations.keys())

    @classmethod
    def list_interfaces(cls) -> list[str]:
        """List all registered interface names.

        Returns:
            List of interface name strings
        """
        cls._ensure_loaded()
        return sorted(cls._interfaces.keys())

    @classmethod
    def search(cls, query: str) -> list[RuleExplanation]:
        """Search for rules matching a query.

        Args:
            query: Search term to match against rule IDs and titles

        Returns:
            List of matching RuleExplanations
        """
        cls._ensure_loaded()
        query_lower = query.lower()
        results = []

        for rule_id, explanation in cls._explanations.items():
            if query_lower in rule_id.lower() or query_lower in explanation.title.lower():
                results.append(explanation)

        return results

    @classmethod
    def _ensure_loaded(cls) -> None:
        """Ensure explanations are loaded from YAML files."""
        if cls._loaded:
            return

        cls._load_builtin_explanations()
        cls._load_yaml_specs(SPECS_DIR)
        cls._loaded = True

    @classmethod
    def _load_builtin_explanations(cls) -> None:
        """Load built-in explanations that don't require YAML."""
        # These are registered as fallbacks for common rules
        pass

    @classmethod
    def _load_yaml_specs(cls, specs_dir: Path) -> None:
        """Load explanations from YAML files in the specs directory.

        Args:
            specs_dir: Path to the directory containing YAML spec files
        """
        if not specs_dir.exists():
            logger.debug(f"Specs directory not found: {specs_dir}")
            return

        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed, skipping YAML spec loading")
            return

        for yaml_file in specs_dir.glob("*.yaml"):
            try:
                cls._load_yaml_file(yaml_file)
            except Exception as e:
                logger.warning(f"Failed to load spec file {yaml_file}: {e}")

    @classmethod
    def _load_yaml_file(cls, path: Path) -> None:
        """Load a single YAML spec file.

        Args:
            path: Path to the YAML file
        """
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)

        if not data:
            return

        # Check if this is a manufacturer spec or interface spec
        if "manufacturer" in data:
            cls._load_manufacturer_spec(data)
        elif "interface" in data:
            cls._load_interface_spec(data)

    @classmethod
    def _load_manufacturer_spec(cls, data: dict[str, Any]) -> None:
        """Load a manufacturer specification from parsed YAML.

        Args:
            data: Parsed YAML data
        """
        manufacturer = data.get("manufacturer", "")
        base_url = data.get("url", "")
        version = data.get("last_updated", "")

        rules = data.get("rules", {})
        for rule_id, rule_data in rules.items():
            spec_ref = SpecReference(
                name=f"{manufacturer} Manufacturing Capabilities",
                section=rule_data.get("spec_section", ""),
                url=base_url,
                version=version,
            )

            # Build fix templates from values if available
            fix_templates = rule_data.get("fix_templates", [])
            if not fix_templates and "values" in rule_data:
                values = rule_data["values"]
                if isinstance(values, dict):
                    for layer_type, value in values.items():
                        fix_templates.append(
                            f"For {layer_type.replace('_', ' ')} boards: adjust to {value}"
                        )

            explanation = RuleExplanation(
                rule_id=rule_id,
                title=rule_data.get("title", rule_id),
                explanation=rule_data.get("explanation", "").strip(),
                spec_references=[spec_ref],
                fix_templates=fix_templates,
                related_rules=rule_data.get("related_rules", []),
                severity=rule_data.get("severity", "error"),
            )

            cls.register(rule_id, explanation)

    @classmethod
    def _load_interface_spec(cls, data: dict[str, Any]) -> None:
        """Load an interface specification from parsed YAML.

        Args:
            data: Parsed YAML data
        """
        interface_name = data.get("interface", "")
        spec_doc = data.get("spec_document", "")
        spec_url = data.get("spec_url", "")

        constraints = data.get("constraints", {})

        # Create InterfaceSpec
        spec = InterfaceSpec(
            interface=interface_name,
            spec_document=spec_doc,
            spec_url=spec_url,
            constraints=constraints,
        )

        # Register with a normalized name
        normalized_name = interface_name.lower().replace(" ", "_").replace(".", "")
        cls.register_interface(normalized_name, spec)

        # Also register individual constraint rules
        for constraint_id, constraint_data in constraints.items():
            spec_ref = SpecReference(
                name=spec_doc,
                section=constraint_data.get("section", ""),
                url=spec_url,
            )

            rule_id = f"{normalized_name}_{constraint_id}"
            explanation = RuleExplanation(
                rule_id=rule_id,
                title=f"{interface_name} - {constraint_id.replace('_', ' ').title()}",
                explanation=constraint_data.get("explanation", "").strip(),
                spec_references=[spec_ref],
                fix_templates=constraint_data.get("fix_templates", []),
                related_rules=[],
                severity=constraint_data.get("severity", "error"),
            )

            cls.register(rule_id, explanation)

    @classmethod
    def clear(cls) -> None:
        """Clear all registered explanations. Mainly for testing."""
        cls._explanations.clear()
        cls._interfaces.clear()
        cls._loaded = False

    @classmethod
    def reload(cls) -> None:
        """Force reload of all explanations."""
        cls.clear()
        cls._ensure_loaded()
