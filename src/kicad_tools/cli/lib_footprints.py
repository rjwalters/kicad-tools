"""
CLI commands for footprint library operations.

Provides commands to list footprints in a .pretty directory
and show details of individual .kicad_mod files.
"""

import json
from pathlib import Path
from typing import Any

from kicad_tools.core.sexp_file import load_footprint
from kicad_tools.sexp import SExp


def list_footprints(directory: Path, output_format: str = "table") -> int:
    """
    List all footprints in a .pretty directory.

    Args:
        directory: Path to .pretty directory
        output_format: "table" or "json"

    Returns:
        Exit code (0 for success)
    """
    if not directory.is_dir():
        print(f"Error: Not a directory: {directory}")
        return 1

    # Find all .kicad_mod files
    mod_files = sorted(directory.glob("*.kicad_mod"))

    if not mod_files:
        print(f"No footprint files found in {directory}")
        return 0

    footprints = []
    for mod_file in mod_files:
        try:
            sexp = load_footprint(mod_file)
            name = _get_footprint_name(sexp)
            pad_count = _count_pads(sexp)
            layer = _get_layer(sexp)
            footprints.append(
                {
                    "file": mod_file.name,
                    "name": name,
                    "pads": pad_count,
                    "layer": layer,
                }
            )
        except Exception as e:
            footprints.append(
                {
                    "file": mod_file.name,
                    "name": "(error)",
                    "pads": 0,
                    "layer": "",
                    "error": str(e),
                }
            )

    if output_format == "json":
        print(json.dumps(footprints, indent=2))
    else:
        _print_footprint_table(directory, footprints)

    return 0


def show_footprint(file_path: Path, output_format: str = "text", show_pads: bool = False) -> int:
    """
    Show details of a single footprint file.

    Args:
        file_path: Path to .kicad_mod file
        output_format: "text" or "json"
        show_pads: Whether to show pad details

    Returns:
        Exit code (0 for success)
    """
    try:
        sexp = load_footprint(file_path)
    except Exception as e:
        print(f"Error loading footprint: {e}")
        return 1

    info = _extract_footprint_info(sexp, show_pads)

    if output_format == "json":
        print(json.dumps(info, indent=2))
    else:
        _print_footprint_info(file_path, info)

    return 0


def _get_footprint_name(sexp: SExp) -> str:
    """Extract footprint name from S-expression."""
    # The first value after the tag is the footprint name
    if sexp.values and isinstance(sexp.values[0], str):
        return sexp.values[0]
    return "(unknown)"


def _count_pads(sexp: SExp) -> int:
    """Count pads in a footprint."""
    return len(sexp.find_children("pad"))


def _get_layer(sexp: SExp) -> str:
    """Get the primary layer of the footprint."""
    layer = sexp.find_child("layer")
    if layer and layer.values:
        return str(layer.values[0])
    return "F.Cu"


def _extract_footprint_info(sexp: SExp, include_pads: bool = False) -> dict[str, Any]:
    """Extract detailed footprint information."""
    info: dict[str, Any] = {
        "name": _get_footprint_name(sexp),
        "format": "KiCad 6+" if sexp.tag == "footprint" else "KiCad 5",
        "layer": _get_layer(sexp),
    }

    # Get version if present
    version = sexp.find_child("version")
    if version and version.values:
        info["version"] = (
            int(version.values[0])
            if isinstance(version.values[0], (int, float))
            else str(version.values[0])
        )

    # Get description
    descr = sexp.find_child("descr")
    if descr and descr.values:
        info["description"] = str(descr.values[0])

    # Get tags
    tags = sexp.find_child("tags")
    if tags and tags.values:
        info["tags"] = str(tags.values[0])

    # Count elements
    pads = sexp.find_children("pad")
    info["pad_count"] = len(pads)

    fp_lines = sexp.find_children("fp_line")
    info["line_count"] = len(fp_lines)

    fp_arcs = sexp.find_children("fp_arc")
    info["arc_count"] = len(fp_arcs)

    fp_circles = sexp.find_children("fp_circle")
    info["circle_count"] = len(fp_circles)

    fp_text_elements = sexp.find_children("fp_text")
    info["text_count"] = len(fp_text_elements)

    # 3D model info
    models = sexp.find_children("model")
    if models:
        info["3d_models"] = [_extract_model_info(m) for m in models]

    # Extract pad details if requested
    if include_pads:
        info["pads"] = [_extract_pad_info(pad) for pad in pads]

    return info


def _extract_pad_info(pad: SExp) -> dict[str, Any]:
    """Extract information about a single pad."""
    pad_info: dict[str, Any] = {}

    # Pad number/name is first value
    if pad.values and isinstance(pad.values[0], str):
        pad_info["name"] = pad.values[0]

    # Pad type is second value (smd, thru_hole, np_thru_hole, connect)
    if len(pad.values) > 1 and isinstance(pad.values[1], str):
        pad_info["type"] = pad.values[1]

    # Pad shape is third value
    if len(pad.values) > 2 and isinstance(pad.values[2], str):
        pad_info["shape"] = pad.values[2]

    # Position
    at = pad.find_child("at")
    if at and at.values:
        pad_info["x"] = float(at.values[0]) if at.values else 0.0
        pad_info["y"] = float(at.values[1]) if len(at.values) > 1 else 0.0
        if len(at.values) > 2:
            pad_info["rotation"] = float(at.values[2])

    # Size
    size = pad.find_child("size")
    if size and size.values:
        pad_info["width"] = float(size.values[0]) if size.values else 0.0
        pad_info["height"] = (
            float(size.values[1]) if len(size.values) > 1 else pad_info.get("width", 0.0)
        )

    # Drill
    drill = pad.find_child("drill")
    if drill and drill.values:
        pad_info["drill"] = float(drill.values[0]) if drill.values else None

    # Layers
    layers = pad.find_child("layers")
    if layers and layers.values:
        pad_info["layers"] = [str(v) for v in layers.values]

    return pad_info


def _extract_model_info(model: SExp) -> dict[str, Any]:
    """Extract 3D model information."""
    model_info: dict[str, Any] = {}

    # Model path is first value
    if model.values and isinstance(model.values[0], str):
        model_info["path"] = model.values[0]

    return model_info


def _print_footprint_table(directory: Path, footprints: list[dict[str, Any]]) -> None:
    """Print footprints in table format."""
    print(f"\nFootprints in {directory.name}:")
    print("=" * 70)
    print(f"{'Name':<40} {'Pads':>6} {'Layer':<12}")
    print("-" * 70)

    for fp in footprints:
        if "error" in fp:
            print(f"{fp['file']:<40} {'ERROR':>6} {fp.get('error', ''):<12}")
        else:
            print(f"{fp['name']:<40} {fp['pads']:>6} {fp['layer']:<12}")

    print("-" * 70)
    print(f"Total: {len(footprints)} footprints")


def _print_footprint_info(file_path: Path, info: dict[str, Any]) -> None:
    """Print footprint information in text format."""
    print(f"\nFootprint: {info['name']}")
    print("=" * 60)

    print(f"\nFile: {file_path}")
    print(f"Format: {info['format']}")
    print(f"Layer: {info['layer']}")

    if "version" in info:
        print(f"Version: {info['version']}")

    if "description" in info:
        print(f"\nDescription: {info['description']}")

    if "tags" in info:
        print(f"Tags: {info['tags']}")

    print("\nElements:")
    print(f"  Pads: {info['pad_count']}")
    print(f"  Lines: {info['line_count']}")
    print(f"  Arcs: {info['arc_count']}")
    print(f"  Circles: {info['circle_count']}")
    print(f"  Text: {info['text_count']}")

    if "3d_models" in info:
        print("\n3D Models:")
        for model in info["3d_models"]:
            print(f"  - {model.get('path', '(unknown)')}")

    if "pads" in info:
        print("\nPad Details:")
        print("-" * 60)
        for pad in info["pads"]:
            name = pad.get("name", "?")
            pad_type = pad.get("type", "?")
            shape = pad.get("shape", "?")
            x = pad.get("x", 0)
            y = pad.get("y", 0)
            w = pad.get("width", 0)
            h = pad.get("height", 0)
            print(
                f"  {name:>4}: {pad_type:<10} {shape:<10} at ({x:>7.3f}, {y:>7.3f}) size {w:.3f}x{h:.3f}"
            )

    print("")
