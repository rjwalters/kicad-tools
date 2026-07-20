"""HV creepage/clearance audit engine (Issue #4327, phase 1).

This package computes **surface-path (creepage)** distances between a
high-voltage (HV) net group and every other conductor / board edge, honoring
milled Edge.Cuts slots that lengthen the surface path.  It complements the
straight-line copper *clearance* that ``kct check`` already measures.

Phase 1 took the required minimum from the operator via ``--min``.  Phase 2
(#4332) adds IEC 60664-1 / 62368-1 standard-table lookup so the required
creepage *and* clearance are derived from working voltage + pollution degree +
material group (see :mod:`kicad_tools.creepage.standards`).  ``kct audit``
integration (#4333) remains a follow-up.

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
from .standards import (
    DISCLAIMER,
    STANDARDS,
    CreepageStandard,
    StandardLookupError,
    get_standard,
)

__all__ = [
    "DISCLAIMER",
    "STANDARDS",
    "CreepagePair",
    "CreepageReport",
    "CreepageStandard",
    "StandardLookupError",
    "compute_creepage_census",
    "get_standard",
    "resolve_hv_nets",
    "surface_path_length",
]
