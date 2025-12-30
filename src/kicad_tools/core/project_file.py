"""
File I/O utilities for KiCad project files (.kicad_pro).

KiCad 6+ project files are JSON format containing project metadata,
design settings, and library references.
"""

import json
from pathlib import Path
from typing import Any, Dict, Union


def load_project(path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load a KiCad project file.

    Args:
        path: Path to .kicad_pro file

    Returns:
        Parsed JSON as dictionary

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not valid JSON
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Project file not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in project file: {e}")

    return data


def save_project(data: Dict[str, Any], path: Union[str, Path]) -> None:
    """
    Save a KiCad project file.

    Args:
        data: Project data dictionary
        path: Path to save to
    """
    path = Path(path)
    text = json.dumps(data, indent=2)
    path.write_text(text, encoding="utf-8")


def get_design_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get design settings from project data.

    Args:
        data: Project data dictionary

    Returns:
        Design settings dictionary (creates if missing)
    """
    if "board" not in data:
        data["board"] = {}
    if "design_settings" not in data["board"]:
        data["board"]["design_settings"] = {}
    return data["board"]["design_settings"]


def get_rule_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get rule defaults from project data.

    Args:
        data: Project data dictionary

    Returns:
        Rule defaults dictionary (creates if missing)
    """
    settings = get_design_settings(data)
    if "rules" not in settings:
        settings["rules"] = {}
    if "defaults" not in settings["rules"]:
        settings["rules"]["defaults"] = {}
    return settings["rules"]["defaults"]


def apply_manufacturer_rules(
    data: Dict[str, Any],
    min_clearance_mm: float,
    min_track_width_mm: float,
    min_via_diameter_mm: float,
    min_via_drill_mm: float,
    min_annular_ring_mm: float,
    min_hole_diameter_mm: float = 0.3,
    min_copper_to_edge_mm: float = 0.3,
) -> Dict[str, Any]:
    """
    Apply manufacturer design rules to project data.

    Updates the design_settings section with manufacturer-specific minimums.

    Args:
        data: Project data dictionary
        min_clearance_mm: Minimum clearance in mm
        min_track_width_mm: Minimum track width in mm
        min_via_diameter_mm: Minimum via diameter in mm
        min_via_drill_mm: Minimum via drill in mm
        min_annular_ring_mm: Minimum annular ring in mm
        min_hole_diameter_mm: Minimum hole diameter in mm
        min_copper_to_edge_mm: Minimum copper to edge in mm

    Returns:
        Modified project data
    """
    settings = get_design_settings(data)

    # Ensure rules section exists
    if "rules" not in settings:
        settings["rules"] = {}

    rules = settings["rules"]

    # Apply minimum constraints
    rules["min_clearance"] = min_clearance_mm
    rules["min_track_width"] = min_track_width_mm
    rules["min_via_diameter"] = min_via_diameter_mm
    rules["min_via_annular_width"] = min_annular_ring_mm
    rules["min_through_hole_diameter"] = min_hole_diameter_mm
    rules["min_via_hole"] = min_via_drill_mm
    rules["min_copper_edge_clearance"] = min_copper_to_edge_mm

    # Also update defaults if they exist
    if "defaults" not in settings:
        settings["defaults"] = {}

    defaults = settings["defaults"]
    defaults["track_min_width"] = min_track_width_mm
    defaults["clearance_min"] = min_clearance_mm
    defaults["via_min_diameter"] = min_via_diameter_mm
    defaults["via_min_drill"] = min_via_drill_mm

    # Store manufacturer metadata
    if "meta" not in data:
        data["meta"] = {}

    return data


def set_manufacturer_metadata(
    data: Dict[str, Any],
    manufacturer_id: str,
    layers: int = 2,
    copper_oz: float = 1.0,
) -> Dict[str, Any]:
    """
    Set manufacturer metadata in project.

    Args:
        data: Project data dictionary
        manufacturer_id: Manufacturer identifier (e.g., "jlcpcb")
        layers: Number of copper layers
        copper_oz: Copper weight in oz

    Returns:
        Modified project data
    """
    if "meta" not in data:
        data["meta"] = {}

    data["meta"]["manufacturer"] = manufacturer_id
    data["meta"]["layers"] = layers
    data["meta"]["copper_oz"] = copper_oz

    return data


def get_manufacturer_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get manufacturer metadata from project.

    Args:
        data: Project data dictionary

    Returns:
        Manufacturer metadata (empty dict if not set)
    """
    return data.get("meta", {})
