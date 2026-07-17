"""Adaptive octilinear lattice route engine (issue #4278, epic #4267 P2.7).

Third routing substrate behind ``--route-engine {grid,mesh,lattice}``: a
balanced-quadtree octilinear lattice replicated per copper layer, where an
A* path IS 45-degree-legal copper by construction -- no funnel, no
octilinear post-fit.  Staged replacement candidate for the navmesh+funnel
line (``router/mesh/``), which remains untouched and fully working; any
supersession is a later owner decision gated on the P4 large-board proof.

Package layout:

* :mod:`.quadtree` -- balanced quadtree lattice generator (<=1-level jumps,
  octilinear edges by construction), built ONCE per board.
* :mod:`.obstacles` -- static per-layer pad masks + the geometric
  committed-copper model (the #3906 never-blind-fit discipline).
* :mod:`.pathfinder` -- pad dogleg stubs, negotiated (node, layer) A*,
  emission to ordinary :class:`~kicad_tools.router.primitives.Route`.

Via gating documentation (issue #4278 acceptance 7): vias land only on
free-space lattice nodes, so **via-in-pad is N/A-by-construction** (not a
disabled feature -- it cannot occur), and only **through-vias** are ever
generated (blind/buried are likewise N/A; a committed through-via masks its
node on ALL layers).
"""

from .obstacles import CommittedCopper, LatticeObstacleModel
from .pathfinder import LatticeNegotiationStats, LatticePathfinder
from .quadtree import OctilinearLattice, RefineRegion

__all__ = [
    "CommittedCopper",
    "LatticeNegotiationStats",
    "LatticeObstacleModel",
    "LatticePathfinder",
    "OctilinearLattice",
    "RefineRegion",
]
