"""Concrete ``RoutingEvaluator`` implementations.

The placement-side ``RoutingEvaluator`` Protocol is declared in
``kicad_tools.optim.evolutionary``.  Concrete implementations live here so
that the placement layer can stay free of router imports while consumers
who want a *real* (C++ A* backed) evaluator pull from this package.

See :class:`CppAstarRoutingEvaluator` for the production implementation
that backs the cascaded place-and-route flow (Issue #2719 / Epic
spheresemi/sphere#7199 KiCad-1).
"""

from __future__ import annotations

from .cpp_astar import (
    CppAstarRoutingEvaluator,
    RoutingEvaluatorConfig,
    compute_hybrid_completion_rate,
)

__all__ = [
    "CppAstarRoutingEvaluator",
    "RoutingEvaluatorConfig",
    "compute_hybrid_completion_rate",
]
