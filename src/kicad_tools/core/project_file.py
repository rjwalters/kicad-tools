"""
File I/O utilities for KiCad project files (.kicad_pro).

KiCad 6+ project files are JSON format containing project metadata,
design settings, and library references.
"""

import json
from pathlib import Path
from typing import Any


def load_project(path: str | Path) -> dict[str, Any]:
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


def save_project(data: dict[str, Any], path: str | Path) -> None:
    """
    Save a KiCad project file.

    Args:
        data: Project data dictionary
        path: Path to save to
    """
    path = Path(path)
    text = json.dumps(data, indent=2)
    path.write_text(text, encoding="utf-8")


def get_design_settings(data: dict[str, Any]) -> dict[str, Any]:
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


def get_rule_defaults(data: dict[str, Any]) -> dict[str, Any]:
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
    data: dict[str, Any],
    min_clearance_mm: float,
    min_track_width_mm: float,
    min_via_diameter_mm: float,
    min_via_drill_mm: float,
    min_annular_ring_mm: float,
    min_hole_diameter_mm: float = 0.3,
    min_copper_to_edge_mm: float = 0.3,
) -> dict[str, Any]:
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
    data: dict[str, Any],
    manufacturer_id: str,
    layers: int = 2,
    copper_oz: float = 1.0,
) -> dict[str, Any]:
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


def get_manufacturer_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """
    Get manufacturer metadata from project.

    Args:
        data: Project data dictionary

    Returns:
        Manufacturer metadata (empty dict if not set)
    """
    return data.get("meta", {})


# =============================================================================
# NETCLASS FUNCTIONS
# =============================================================================

# Default netclass definition structure (KiCad 7+ format)
DEFAULT_NETCLASS_DEFINITION: dict[str, Any] = {
    "bus_width": 12,
    "clearance": 0.2,
    "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25,
    "diff_pair_width": 0.2,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "name": "Default",
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "track_width": 0.25,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "wire_width": 6,
}


def get_net_settings(data: dict[str, Any]) -> dict[str, Any]:
    """
    Get net_settings from project data, creating if missing.

    Args:
        data: Project data dictionary

    Returns:
        Net settings dictionary
    """
    if "net_settings" not in data:
        data["net_settings"] = {
            "classes": [DEFAULT_NETCLASS_DEFINITION.copy()],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        }
    return data["net_settings"]


def get_netclass_definitions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Get list of netclass definitions from project data.

    Args:
        data: Project data dictionary

    Returns:
        List of netclass definition dictionaries
    """
    net_settings = get_net_settings(data)
    if "classes" not in net_settings:
        net_settings["classes"] = [DEFAULT_NETCLASS_DEFINITION.copy()]
    return net_settings["classes"]


def get_netclass_patterns(data: dict[str, Any]) -> list[dict[str, str]]:
    """
    Get list of netclass patterns from project data.

    Args:
        data: Project data dictionary

    Returns:
        List of pattern dictionaries with 'netclass' and 'pattern' keys
    """
    net_settings = get_net_settings(data)
    if "netclass_patterns" not in net_settings:
        net_settings["netclass_patterns"] = []
    return net_settings["netclass_patterns"]


def create_netclass_definition(
    name: str,
    track_width: float = 0.25,
    clearance: float = 0.2,
    via_diameter: float = 0.6,
    via_drill: float = 0.3,
    pcb_color: str | None = None,
    schematic_color: str | None = None,
    diff_pair_width: float = 0.2,
    diff_pair_gap: float = 0.25,
) -> dict[str, Any]:
    """
    Create a netclass definition dictionary.

    Args:
        name: Netclass name
        track_width: Trace width in mm
        clearance: Trace-to-trace clearance in mm
        via_diameter: Via outer diameter in mm
        via_drill: Via drill diameter in mm
        pcb_color: PCB editor color (RGBA string) or None for default
        schematic_color: Schematic editor color (RGBA string) or None for default
        diff_pair_width: Differential pair trace width in mm
        diff_pair_gap: Differential pair gap in mm

    Returns:
        Netclass definition dictionary
    """
    definition = DEFAULT_NETCLASS_DEFINITION.copy()
    definition["name"] = name
    definition["track_width"] = track_width
    definition["clearance"] = clearance
    definition["via_diameter"] = via_diameter
    definition["via_drill"] = via_drill
    definition["diff_pair_width"] = diff_pair_width
    definition["diff_pair_gap"] = diff_pair_gap

    if pcb_color is not None:
        definition["pcb_color"] = pcb_color
    if schematic_color is not None:
        definition["schematic_color"] = schematic_color

    return definition


def add_netclass_definition(
    data: dict[str, Any],
    name: str,
    track_width: float = 0.25,
    clearance: float = 0.2,
    via_diameter: float = 0.6,
    via_drill: float = 0.3,
    pcb_color: str | None = None,
    schematic_color: str | None = None,
    diff_pair_width: float = 0.2,
    diff_pair_gap: float = 0.25,
) -> dict[str, Any]:
    """
    Add a netclass definition to project data.

    If a netclass with the same name already exists, it will be updated.

    Args:
        data: Project data dictionary
        name: Netclass name
        track_width: Trace width in mm
        clearance: Trace-to-trace clearance in mm
        via_diameter: Via outer diameter in mm
        via_drill: Via drill diameter in mm
        pcb_color: PCB editor color (RGBA string) or None for default
        schematic_color: Schematic editor color (RGBA string) or None for default
        diff_pair_width: Differential pair trace width in mm
        diff_pair_gap: Differential pair gap in mm

    Returns:
        The created/updated netclass definition
    """
    classes = get_netclass_definitions(data)

    # Check if netclass already exists
    for i, cls in enumerate(classes):
        if cls.get("name") == name:
            # Update existing
            definition = create_netclass_definition(
                name=name,
                track_width=track_width,
                clearance=clearance,
                via_diameter=via_diameter,
                via_drill=via_drill,
                pcb_color=pcb_color,
                schematic_color=schematic_color,
                diff_pair_width=diff_pair_width,
                diff_pair_gap=diff_pair_gap,
            )
            classes[i] = definition
            return definition

    # Add new netclass
    definition = create_netclass_definition(
        name=name,
        track_width=track_width,
        clearance=clearance,
        via_diameter=via_diameter,
        via_drill=via_drill,
        pcb_color=pcb_color,
        schematic_color=schematic_color,
        diff_pair_width=diff_pair_width,
        diff_pair_gap=diff_pair_gap,
    )
    classes.append(definition)
    return definition


def add_netclass_pattern(
    data: dict[str, Any],
    netclass: str,
    pattern: str,
) -> dict[str, str]:
    """
    Add a netclass pattern assignment to project data.

    Patterns use wildcard matching where * matches any substring.

    Args:
        data: Project data dictionary
        netclass: Target netclass name
        pattern: Wildcard pattern for matching net names

    Returns:
        The created pattern dictionary
    """
    patterns = get_netclass_patterns(data)

    # Check if this exact pattern already exists
    for p in patterns:
        if p.get("netclass") == netclass and p.get("pattern") == pattern:
            return p

    # Add new pattern
    pattern_dict = {"netclass": netclass, "pattern": pattern}
    patterns.append(pattern_dict)
    return pattern_dict


def add_netclass_patterns(
    data: dict[str, Any],
    netclass: str,
    patterns: list[str],
) -> list[dict[str, str]]:
    """
    Add multiple netclass pattern assignments for a single netclass.

    Args:
        data: Project data dictionary
        netclass: Target netclass name
        patterns: List of wildcard patterns for matching net names

    Returns:
        List of created pattern dictionaries
    """
    return [add_netclass_pattern(data, netclass, p) for p in patterns]


def clear_netclass_definitions(data: dict[str, Any], keep_default: bool = True) -> None:
    """
    Clear all netclass definitions from project data.

    Args:
        data: Project data dictionary
        keep_default: If True, keep the Default netclass
    """
    net_settings = get_net_settings(data)
    if keep_default:
        # Keep only the Default class
        classes = net_settings.get("classes", [])
        default_class = None
        for cls in classes:
            if cls.get("name") == "Default":
                default_class = cls
                break
        if default_class:
            net_settings["classes"] = [default_class]
        else:
            net_settings["classes"] = [DEFAULT_NETCLASS_DEFINITION.copy()]
    else:
        net_settings["classes"] = []


def clear_netclass_patterns(data: dict[str, Any]) -> None:
    """
    Clear all netclass patterns from project data.

    Args:
        data: Project data dictionary
    """
    net_settings = get_net_settings(data)
    net_settings["netclass_patterns"] = []


def create_minimal_project(filename: str) -> dict[str, Any]:
    """
    Create a minimal but valid KiCad project structure.

    This creates a project file with all required sections that KiCad expects,
    suitable for use with kct commands and opening in KiCad GUI.

    Args:
        filename: The project filename (e.g., "my_project.kicad_pro")

    Returns:
        Project data dictionary ready to be saved with save_project()
    """
    return {
        "board": {
            "3dviewports": [],
            "design_settings": {
                "defaults": {
                    "board_outline_line_width": 0.1,
                    "copper_line_width": 0.2,
                    "copper_text_size_h": 1.5,
                    "copper_text_size_v": 1.5,
                    "copper_text_thickness": 0.3,
                    "other_line_width": 0.15,
                    "silk_line_width": 0.15,
                    "silk_text_size_h": 1.0,
                    "silk_text_size_v": 1.0,
                    "silk_text_thickness": 0.15,
                },
                "diff_pair_dimensions": [],
                "drc_exclusions": [],
                "rules": {
                    "min_copper_edge_clearance": 0.0,
                    "solder_mask_clearance": 0.0,
                    "solder_mask_min_width": 0.0,
                },
                "track_widths": [],
                "via_dimensions": [],
            },
            "layer_presets": [],
            "viewports": [],
        },
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "libraries": {
            "pinned_footprint_libs": [],
            "pinned_symbol_libs": [],
        },
        "meta": {
            "filename": filename,
            "version": 1,
        },
        "net_settings": {
            "classes": [DEFAULT_NETCLASS_DEFINITION.copy()],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {
            "last_paths": {
                "gencad": "",
                "idf": "",
                "netlist": "",
                "specctra_dsn": "",
                "step": "",
                "vrml": "",
            },
            "page_layout_descr_file": "",
        },
        "schematic": {
            "annotate_start_num": 0,
            "drawing": {
                "dashed_lines_dash_length_ratio": 12.0,
                "dashed_lines_gap_length_ratio": 3.0,
                "default_line_thickness": 6.0,
                "default_text_size": 50.0,
                "field_names": [],
                "intersheets_ref_own_page": False,
                "intersheets_ref_prefix": "",
                "intersheets_ref_short": False,
                "intersheets_ref_show": False,
                "intersheets_ref_suffix": "",
                "junction_size_choice": 3,
                "label_size_ratio": 0.375,
                "pin_symbol_size": 25.0,
                "text_offset_ratio": 0.15,
            },
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "meta": {"version": 1},
            "net_format_name": "",
            "page_layout_descr_file": "",
            "plot_directory": "",
            "spice_adjust_passive_values": False,
            "spice_external_command": 'spice "%I"',
            "subpart_first_id": 65,
            "subpart_id_separator": 0,
        },
        "sheets": [],
        "text_variables": {},
    }
