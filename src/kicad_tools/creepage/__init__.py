"""HV creepage/clearance audit engine (Issue #4327, phase 1).

This package computes **surface-path (creepage)** distances between a
high-voltage (HV) net group and every other conductor / board edge, honoring
milled Edge.Cuts slots that lengthen the surface path.  It complements the
straight-line copper *clearance* that ``kct check`` already measures.

Phase 1 (this package) takes the required minimum from the operator via
``--min``.  IEC 60664-1 / 62368-1 standard-table lookup (#4332) and
``kct audit`` integration (#4333) are tracked as follow-ups.

See :mod:`kicad_tools.creepage.engine` for the public API.
"""

from __future__ import annotations

from .engine import (
    CreepagePair,
    CreepageReport,
    compute_creepage_census,
    resolve_hv_nets,
    surface_path_length,
)

__all__ = [
    "CreepagePair",
    "CreepageReport",
    "compute_creepage_census",
    "resolve_hv_nets",
    "surface_path_length",
]
