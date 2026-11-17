"""Vector geometry primitives for the router.

This module re-exports the canonical implementations from
:mod:`kicad_tools.core.geometry`.  Router-internal code that previously
imported from this module continues to work without changes.

Higher-level wrappers that accept Segment objects live in
``optimizer/geometry.py``.
"""

from __future__ import annotations

from kicad_tools.core.geometry import (
    point_to_segment_distance,
    segment_clearance,
    segment_to_segment_distance,
    segments_intersect,
)

__all__ = [
    "point_to_segment_distance",
    "segment_clearance",
    "segment_to_segment_distance",
    "segments_intersect",
]
