"""Mesh-router navigation substrate (issue #4268, epic #4267).

The mesh strategy is an alternative to the uniform-grid router that trades the
grid's ``area / resolution^2`` memory wall for a triangle-dual navmesh whose
node count scales with feature complexity (~3,500x less memory on softstart
rev-C in the P0 spike).  P1 is the single-net vertical slice, default OFF
behind ``--strategy mesh``:

    poly2tri constrained-Delaunay (with pad keep-out holes)
      -> triangle-dual portal-midpoint A*        (navmesh.py)
      -> Simple Stupid Funnel string-pull        (funnel.py)
      -> clearance-aware 45-degree best-fit       (octilinear.py)
      -> ordinary Route -> existing emission + DRC tail

Multi-net negotiation, capacity, and matched routing are explicitly out of
scope for P1 (epic children P2/P3).
"""

from __future__ import annotations

from .funnel import string_pull
from .navmesh import NavMesh
from .obstacles import ObstacleModel
from .octilinear import octilinear_fit
from .pathfinder import MeshPathfinder

__all__ = [
    "MeshPathfinder",
    "NavMesh",
    "ObstacleModel",
    "octilinear_fit",
    "string_pull",
]
