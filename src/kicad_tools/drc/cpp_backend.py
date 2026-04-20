"""
C++ DRC backend with Python fallback.

This module provides a unified interface for pad-to-pad clearance checking
that automatically uses the C++ implementation when available, falling back
to pure Python.

The C++ backend provides significant speedup for the O(P1 x P2) inner loop
in pad-to-pad clearance computation by using:
- Squared-distance optimization (avoids sqrt for non-minimum pairs)
- Struct-of-arrays memory layout for contiguous access
- Single trig computation per footprint
- No Python interpreter overhead per iteration
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import Footprint

# Try to import C++ module with detailed error tracking
_CPP_IMPORT_ERROR: str | None = None
try:
    from . import drc_cpp

    _CPP_AVAILABLE = True
except ImportError as e:
    _CPP_AVAILABLE = False
    _CPP_IMPORT_ERROR = str(e)
    drc_cpp = None  # type: ignore[assignment]


def is_cpp_available() -> bool:
    """Check if the C++ DRC backend is available."""
    return _CPP_AVAILABLE


def get_cpp_unavailable_reason() -> str | None:
    """Get the reason why C++ backend is unavailable.

    Returns:
        Error message if C++ backend failed to load, None if available.
    """
    if _CPP_AVAILABLE:
        return None
    return _CPP_IMPORT_ERROR


def get_backend_info() -> dict:
    """Get information about the active DRC backend.

    Returns a dictionary with:
        - backend: "cpp" or "python"
        - version: version string
        - available: True if C++ backend is available
        - unavailable_reason: Error message if C++ unavailable
    """
    import platform
    import sys

    platform_info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
    }

    if _CPP_AVAILABLE:
        return {
            "backend": "cpp",
            "version": drc_cpp.version(),
            "available": True,
            "platform": platform_info,
        }

    reason = _CPP_IMPORT_ERROR or "Unknown error"
    build_hint = (
        "Build the C++ extension for faster DRC clearance checking:\n"
        "  kct build-native\n"
        "Or install with native support:\n"
        "  pip install kicad-tools[native]"
    )

    return {
        "backend": "python",
        "version": "pure-python",
        "available": False,
        "unavailable_reason": reason,
        "build_hint": build_hint,
        "platform": platform_info,
    }


def _extract_pad_arrays(
    fp: Footprint,
) -> tuple[list[float], list[float], list[float], list[int], list[str]]:
    """Extract pad data into struct-of-arrays format.

    Returns:
        Tuple of (local_x, local_y, radius, net_numbers, pad_numbers)
        where pad_numbers are the pad identifier strings (e.g. "1", "2").
    """
    local_x: list[float] = []
    local_y: list[float] = []
    radius: list[float] = []
    net_nums: list[int] = []
    pad_ids: list[str] = []

    for pad in fp.pads:
        local_x.append(pad.position[0])
        local_y.append(pad.position[1])
        radius.append(max(pad.size[0], pad.size[1]) / 2.0)
        net_nums.append(pad.net_number)
        pad_ids.append(pad.number)

    return local_x, local_y, radius, net_nums, pad_ids


def check_pair_clearance_cpp(
    fp1: Footprint,
    fp2: Footprint,
    ref1: str,
    ref2: str,
    fp1_position: tuple[float, float] | None = None,
) -> tuple[float, tuple[float, float], tuple[str, ...], tuple[str, ...]] | None:
    """Check pad-to-pad clearance using C++ backend.

    Args:
        fp1: First footprint
        fp2: Second footprint
        ref1: Reference designator for fp1
        ref2: Reference designator for fp2
        fp1_position: Override position for fp1 (for moved component checks).
            If None, uses fp1.position.

    Returns:
        Tuple of (min_clearance, location, items, nets) or None if no pads.
        - min_clearance: Minimum edge-to-edge clearance in mm
        - location: (x, y) midpoint of closest pad pair
        - items: ("ref1-pad1", "ref2-pad2") identifiers
        - nets: (net_name1, net_name2) net names
    """
    if not _CPP_AVAILABLE:
        raise RuntimeError("C++ DRC backend not available")

    lx1, ly1, r1, nets1, pids1 = _extract_pad_arrays(fp1)
    lx2, ly2, r2, nets2, pids2 = _extract_pad_arrays(fp2)

    if not lx1 or not lx2:
        return None

    pos1 = fp1_position if fp1_position is not None else fp1.position
    rot1_rad = math.radians(fp1.rotation)
    rot2_rad = math.radians(fp2.rotation)

    result = drc_cpp.check_pair_clearance(
        lx1,
        ly1,
        r1,
        nets1,
        pos1[0],
        pos1[1],
        rot1_rad,
        lx2,
        ly2,
        r2,
        nets2,
        fp2.position[0],
        fp2.position[1],
        rot2_rad,
    )

    if not result.has_result:
        return None

    i = result.pad1_index
    j = result.pad2_index

    items = (f"{ref1}-{pids1[i]}", f"{ref2}-{pids2[j]}")
    net_names = (fp1.pads[i].net_name, fp2.pads[j].net_name)

    return (result.min_clearance, (result.location_x, result.location_y), items, net_names)
