"""
Placement and routing optimization module (EXPERIMENTAL).

.. warning::
    This module is experimental and not yet fully implemented.
    Classes will raise NotImplementedError when instantiated.

Provides metaheuristic optimization algorithms for:
- Component placement (force-directed, simulated annealing)
- Routing parameter optimization
- Figure of merit computation

Requires numpy and scipy. Install with::

    pip install kicad_tools[optim]
"""

import warnings

__all__ = [
    "PlacementOptimizer",
    "RoutingOptimizer",
    "FigureOfMerit",
]

# Emit warning on import
warnings.warn(
    "kicad_tools.optim is experimental and not yet fully implemented. "
    "Classes will raise NotImplementedError.",
    category=FutureWarning,
    stacklevel=2
)


class PlacementOptimizer:
    """
    Component placement optimizer using force-directed and SA algorithms.

    .. warning::
        Not yet implemented. Instantiation will raise NotImplementedError.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "PlacementOptimizer is not yet implemented in kicad_tools. "
            "This is an experimental module placeholder."
        )


class RoutingOptimizer:
    """
    Routing parameter optimizer using metaheuristics.

    .. warning::
        Not yet implemented. Instantiation will raise NotImplementedError.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "RoutingOptimizer is not yet implemented in kicad_tools. "
            "This is an experimental module placeholder."
        )


class FigureOfMerit:
    """Figure of merit computation for routing/placement quality."""

    pass
