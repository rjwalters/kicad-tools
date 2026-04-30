"""PCB geometry data extraction for interactive HTML reports.

Extracts board outline, footprints, pads, tracks, vias, and zone outlines
from a parsed PCB into a JSON-serializable dictionary suitable for rendering
in a Canvas 2D viewer.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _natsort_key(s: str) -> list[int | str]:
    """Natural sort key: splits text into (str, int, str, int, ...) chunks.

    Ensures R1, R2, R10 sort correctly instead of R1, R10, R2.
    """
    parts: list[int | str] = []
    for chunk in re.split(r"(\d+)", s):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            parts.append(chunk.lower())
    return parts


def natsort_refs(refs: list[str]) -> list[str]:
    """Sort reference designators using natural alphanumeric order.

    Args:
        refs: List of reference designator strings (e.g. ["R1", "R10", "R2"]).

    Returns:
        Naturally sorted list (e.g. ["R1", "R2", "R10"]).
    """
    return sorted(refs, key=_natsort_key)


def extract_pcb_data(pcb: Any) -> dict[str, Any]:
    """Extract PCB geometry data from a parsed PCB object.

    Returns a JSON-serializable dictionary with the following top-level keys:

    - ``board_outline``: list of (x, y) points forming the board edge
    - ``bounds``: ``{min_x, min_y, max_x, max_y}`` bounding box in mm
    - ``footprints``: list of footprint dicts with position, pads, reference
    - ``segments``: list of trace segment dicts with start, end, layer, width
    - ``vias``: list of via dicts with position, size, drill, layers
    - ``layers``: list of layer name strings (copper only)

    Args:
        pcb: Loaded PCB object (``kicad_tools.schema.pcb.PCB``).

    Returns:
        Dictionary of PCB geometry data.
    """
    result: dict[str, Any] = {}

    # Board outline
    outline = _extract_outline(pcb)
    result["board_outline"] = outline

    # Bounding box
    result["bounds"] = _compute_bounds(outline, pcb)

    # Copper layers
    copper_layers = pcb.copper_layers
    result["layers"] = [layer.name for layer in copper_layers]

    # Footprints
    result["footprints"] = _extract_footprints(pcb)

    # Segments (traces)
    result["segments"] = _extract_segments(pcb)

    # Vias
    result["vias"] = _extract_vias(pcb)

    return result


def _extract_outline(pcb: Any) -> list[list[float]]:
    """Extract board outline as a list of [x, y] coordinate pairs."""
    try:
        points = pcb.get_board_outline()
        return [[round(p[0], 3), round(p[1], 3)] for p in points]
    except Exception:
        logger.debug("Could not extract board outline", exc_info=True)
        return []


def _compute_bounds(
    outline: list[list[float]], pcb: Any
) -> dict[str, float]:
    """Compute bounding box from outline or fallback to all geometry."""
    if outline:
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        return {
            "min_x": min(xs),
            "min_y": min(ys),
            "max_x": max(xs),
            "max_y": max(ys),
        }

    # Fallback: compute from footprint positions
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for fp in pcb.footprints:
        x, y = fp.position
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)

    if min_x == float("inf"):
        return {"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 100}

    # Add margin
    margin = 5.0
    return {
        "min_x": min_x - margin,
        "min_y": min_y - margin,
        "max_x": max_x + margin,
        "max_y": max_y + margin,
    }


def _extract_footprints(pcb: Any) -> list[dict[str, Any]]:
    """Extract footprint data including pads."""
    footprints = []
    for fp in pcb.footprints:
        fp_data: dict[str, Any] = {
            "reference": fp.reference or "",
            "value": fp.value or "",
            "position": [round(fp.position[0], 3), round(fp.position[1], 3)],
            "rotation": fp.rotation,
            "layer": fp.layer,
            "attr": fp.attr or "",
            "pads": [],
        }
        for pad in fp.pads:
            pad_data = {
                "number": pad.number,
                "type": pad.type,
                "shape": pad.shape,
                "position": [
                    round(pad.position[0], 3),
                    round(pad.position[1], 3),
                ],
                "size": [round(pad.size[0], 3), round(pad.size[1], 3)],
                "layers": pad.layers,
                "net_name": pad.net_name,
            }
            fp_data["pads"].append(pad_data)
        footprints.append(fp_data)

    return footprints


def _extract_segments(pcb: Any) -> list[dict[str, Any]]:
    """Extract trace segments."""
    segments = []
    for seg in pcb.segments:
        segments.append({
            "start": [round(seg.start[0], 3), round(seg.start[1], 3)],
            "end": [round(seg.end[0], 3), round(seg.end[1], 3)],
            "width": round(seg.width, 3),
            "layer": seg.layer,
            "net": seg.net_name or str(seg.net_number),
        })
    return segments


def _extract_vias(pcb: Any) -> list[dict[str, Any]]:
    """Extract vias."""
    vias = []
    for via in pcb.vias:
        vias.append({
            "position": [
                round(via.position[0], 3),
                round(via.position[1], 3),
            ],
            "size": round(via.size, 3),
            "drill": round(via.drill, 3),
            "layers": via.layers,
            "net": via.net_name or str(via.net_number),
        })
    return vias


def extract_pcb_data_from_path(pcb_path: Path) -> dict[str, Any]:
    """Load a .kicad_pcb file and extract geometry data.

    Convenience wrapper around :func:`extract_pcb_data` that handles
    loading the PCB file.

    Args:
        pcb_path: Path to ``.kicad_pcb`` file.

    Returns:
        Dictionary of PCB geometry data.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(pcb_path)
    return extract_pcb_data(pcb)
