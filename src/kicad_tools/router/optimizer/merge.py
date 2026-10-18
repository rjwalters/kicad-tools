"""Conservative collinear-merge pass shared by the routing save paths."""

from __future__ import annotations

import copy
import logging

logger = logging.getLogger(__name__)


def merge_collinear_routes(router) -> bool:
    """Collapse the router's per-grid-cell segments into straight runs.

    Issues #7120 / #7138: the router emits one segment per grid step, so a
    straight 9 mm run is ~90 collinear segments.  ``kct route`` runs the
    trace optimizer before saving; this applies the conservative subset of
    it (``merge_collinear`` only) to every route on ``router``, guarded by
    the connectivity invariant.

    On any failure ``router.routes`` is restored to its pre-call geometry and
    ``False`` is returned, so callers can save the raw routes.
    """
    # Imported at call time so the package-level names stay patchable.
    from ..connectivity_invariant import (
        enforce_connectivity_invariant,
        snapshot_connectivity,
    )
    from . import (
        OptimizationConfig,
        TraceOptimizer,
        make_collision_checker,
        optimize_routes_grid_synced,
    )

    original_routes = copy.deepcopy(router.routes)
    try:
        snapshot = snapshot_connectivity(router)
        optimizer = TraceOptimizer(
            config=OptimizationConfig(
                merge_collinear=True,
                eliminate_zigzags=False,
                compress_staircase=False,
                convert_45_corners=False,
                minimize_vias=False,
            ),
            collision_checker=make_collision_checker(router.grid),
        )
        optimize_routes_grid_synced(router, optimizer)
        enforce_connectivity_invariant(router, snapshot, "optimize", quiet=True)
        return True
    except Exception:
        logger.warning("Trace optimization failed; saving raw routes")
        router.routes = original_routes
        return False
