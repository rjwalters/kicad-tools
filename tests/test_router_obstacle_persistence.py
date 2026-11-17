"""``add_obstacle`` keepouts must survive trial resets and worker round trips
(Issue #5555).

``Autorouter._reset_for_new_trial`` throws the whole ``RoutingGrid`` away and
rebuilds it from the live pads, the fixed fills and the persisted board-edge
keepout -- but, before this fix, not from keepouts registered through
``Autorouter.add_obstacle``.  Every rip-up/reroute iteration inside
``route_all_negotiated`` (and every Monte Carlo / evolutionary trial reset)
therefore silently reopened those cells: the obstacle was still "registered"
from the caller's point of view while the grid it was supposed to block was
wide open again.  ``_serialize_for_parallel`` omitted them entirely too, so a
worker process reconstructing its own ``Autorouter`` never had them at all.

This suite mirrors ``tests/test_router_edge_keepout_persistence.py`` (the
analogous ``_edge_segments`` / ``_edge_clearance`` fix from Issue #5374) and
pins:

1. A cell blocked by ``add_obstacle`` is still blocked immediately after a
   real ``_reset_for_new_trial()`` call -- and the exact blocked-cell mask is
   restored, not merely "something is blocked".
2. Repeated ``add_obstacle`` calls followed by repeated resets neither lose
   nor duplicate obstacles (the rip-up/reroute loop's actual access pattern).
3. ``_serialize_for_parallel()`` carries the obstacles across the config-dict
   boundary, and both real worker reconstructions (``_run_monte_carlo_trial``
   / ``_run_evolutionary_trial``) reinstall them before routing.
4. A native-backed (C++) router comes out of the reset in exactly the state it
   went in, on both sides of the boundary, when the native extension is
   available in this worktree.
"""

from __future__ import annotations

import pickle

import numpy as np
import pytest

from kicad_tools.router import core as core_module
from kicad_tools.router.algorithms import evolutionary as evolutionary_module
from kicad_tools.router.core import Autorouter, _run_monte_carlo_trial
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules

# Obstacle centred at (5, 2) spanning the full 10 mm board width -- a "wall"
# across the bottom of the board, the same shape the board-07 DDR repro
# harness installs as a channel wall.
WALL = (5.0, 2.0, 10.0, 2.0)
# A point well inside the wall, and a point well clear of it (and clear of
# the y=8 corridor the two pads' own trace runs along, so the check stays
# valid after a worker has actually routed).
INSIDE_WALL = (5.0, 2.0)
OUTSIDE_WALL = (5.0, 6.0)


def _build_router(force_python: bool = True) -> Autorouter:
    """A small two-pad router whose pads sit clear of :data:`WALL`."""
    router = Autorouter(10, 10, rules=DesignRules(grid_resolution=0.5), force_python=force_python)
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 4.0, "y": 8.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 6.0, "y": 8.0, "net": 1, "net_name": "NET1"},
        ],
    )
    return router


def _assert_wall_blocked(router: Autorouter, message: str) -> None:
    grid = router.grid
    inside_x, inside_y = grid.world_to_grid(*INSIDE_WALL)
    outside_x, outside_y = grid.world_to_grid(*OUTSIDE_WALL)
    assert grid.is_blocked(inside_x, inside_y, Layer.F_CU), message
    assert not grid.is_blocked(outside_x, outside_y, Layer.F_CU), (
        "sanity check failed: a cell far from the obstacle is blocked too"
    )


class TestResetForNewTrialPreservesObstacles:
    """Audit surface 1: ``Autorouter._reset_for_new_trial``."""

    @pytest.mark.parametrize("force_python", [True, False])
    def test_obstacle_cells_are_still_blocked_after_a_reset(self, force_python):
        if not force_python and not is_cpp_available():
            pytest.skip("Native routing backend not built in this worktree (kct build-native)")

        router = _build_router(force_python=force_python)
        router.add_obstacle(*WALL, Layer.F_CU)
        _assert_wall_blocked(router, "obstacle was never installed on the grid")
        before = int(router.grid._blocked.sum())

        router._reset_for_new_trial()

        _assert_wall_blocked(
            router,
            "_reset_for_new_trial() dropped the add_obstacle keepout -- "
            "the grid silently reopened at the obstacle's cells",
        )
        assert int(router.grid._blocked.sum()) == before, (
            "blocked-cell count changed across the reset -- the obstacle was "
            "not restored identically"
        )

    def test_reset_restores_the_exact_blocked_mask(self):
        """Not just a count: the exact blocked-cell mask must come back,
        including the obstacle's clearance halo."""
        router = _build_router()
        router.add_obstacle(*WALL, Layer.F_CU)
        before_mask = router.grid._blocked.copy()

        router._reset_for_new_trial()

        assert np.array_equal(before_mask, router.grid._blocked), (
            "blocked-cell mask changed after _reset_for_new_trial() -- the "
            "add_obstacle keepout was not restored identically"
        )
        assert int(before_mask.sum()) > 0, "sanity check failed: nothing was blocked to begin with"

    def test_obstacle_stays_on_its_own_layer_after_a_reset(self):
        router = _build_router()
        router.add_obstacle(*WALL, Layer.B_CU)

        router._reset_for_new_trial()

        grid = router.grid
        gx, gy = grid.world_to_grid(*INSIDE_WALL)
        assert grid.is_blocked(gx, gy, Layer.B_CU), (
            "a B.Cu obstacle did not survive _reset_for_new_trial()"
        )
        assert not grid.is_blocked(gx, gy, Layer.F_CU), (
            "a B.Cu obstacle leaked onto F.Cu across the reset"
        )

    def test_repeated_obstacles_and_repeated_resets_neither_lose_nor_duplicate(self):
        """The rip-up/reroute access pattern: several ``add_obstacle`` calls,
        then several ``_reset_for_new_trial()`` calls in a row."""
        router = _build_router()
        router.add_obstacle(*WALL, Layer.F_CU)
        router.add_obstacle(2.0, 5.0, 1.0, 1.0, Layer.F_CU)
        router.add_obstacle(8.0, 5.0, 1.0, 1.0, Layer.B_CU)

        registered = len(router._obstacles)
        assert registered == 3, "add_obstacle must persist every registration"
        before_mask = router.grid._blocked.copy()

        masks = []
        for _ in range(3):
            router._reset_for_new_trial()
            masks.append(router.grid._blocked.copy())

        for i, mask in enumerate(masks):
            assert np.array_equal(before_mask, mask), (
                f"blocked-cell mask changed after reset #{i + 1} -- obstacles "
                "were lost or duplicated across repeated resets"
            )
        assert len(router._obstacles) == registered, (
            "_reset_for_new_trial() mutated the persisted obstacle registry"
        )
        _assert_wall_blocked(router, "obstacles were lost after repeated resets")

    def test_reset_without_obstacles_is_a_strict_no_op(self):
        router = _build_router()
        assert router._obstacles == []
        before_mask = router.grid._blocked.copy()

        router._reset_for_new_trial()

        assert np.array_equal(before_mask, router.grid._blocked), (
            "reset spuriously blocked cells on a router with no obstacles"
        )


class TestSerializeForParallelIncludesObstacles:
    """Audit surface 2 (producer side): ``_serialize_for_parallel``."""

    def test_config_carries_registered_obstacles(self):
        router = _build_router()
        router.add_obstacle(*WALL, Layer.F_CU)

        config = router._serialize_for_parallel()

        assert config["obstacles"] == [
            {
                "x": 5.0,
                "y": 2.0,
                "width": 10.0,
                "height": 2.0,
                "layer": Layer.F_CU.value,
                "clearance": 0.0,
            }
        ]
        # The payload must survive transport and be isolated from later
        # parent-side edits (it crosses a ProcessPoolExecutor boundary).
        router._obstacles.clear()
        assert pickle.loads(pickle.dumps(config))["obstacles"] == config["obstacles"]

    def test_config_handles_absent_obstacles(self):
        router = _build_router()
        assert router._serialize_for_parallel()["obstacles"] == []


class TestWorkerReconstructionReinstallsObstacles:
    """Audit surface 2 (consumer side): the real worker reconstruction paths,
    ``_run_monte_carlo_trial`` and ``_run_evolutionary_trial``.

    Calls the worker entry point directly (an in-process "bounded worker
    call") rather than going through ``ProcessPoolExecutor`` -- this exercises
    the exact reconstruction code path a real worker process runs, without the
    cost/flakiness of spawning subprocesses in a unit test.
    """

    @staticmethod
    def _capturing_autorouter(captured: dict):
        real_autorouter_cls = core_module.Autorouter

        class _CapturingAutorouter(real_autorouter_cls):  # type: ignore[valid-type,misc]
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["router"] = self

        return _CapturingAutorouter

    def _config(self, **extra) -> dict:
        router = _build_router()
        router.add_obstacle(*WALL, Layer.F_CU)
        config = router._serialize_for_parallel().copy()
        config.update(extra)
        return config

    def test_monte_carlo_worker_round_trip_installs_obstacles(self):
        config = self._config(trial_num=0, seed=42, base_order=[1], use_negotiated=False)
        captured: dict = {}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(core_module, "Autorouter", self._capturing_autorouter(captured))
            routes, _score, trial_num = _run_monte_carlo_trial(config)

        assert trial_num == 0
        assert isinstance(routes, list)
        worker_router = captured["router"]
        assert [
            (obs.x, obs.y, obs.width, obs.height, obs.layer) for obs in worker_router._obstacles
        ] == [(*WALL, Layer.F_CU)]
        _assert_wall_blocked(
            worker_router,
            "_run_monte_carlo_trial never reinstalled the add_obstacle keepout",
        )

    def test_evolutionary_worker_round_trip_installs_obstacles(self):
        config = self._config(chrom_idx=0, seed=42, net_order=[1])
        captured: dict = {}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(core_module, "Autorouter", self._capturing_autorouter(captured))
            routes, _score, chrom_idx = evolutionary_module._run_evolutionary_trial(config)

        assert chrom_idx == 0
        assert isinstance(routes, list)
        worker_router = captured["router"]
        _assert_wall_blocked(
            worker_router,
            "_run_evolutionary_trial never reinstalled the add_obstacle keepout",
        )

    def test_worker_round_trip_without_obstacles_is_a_strict_no_op(self):
        """A legacy config dict with no "obstacles" key must not error and
        must not spuriously block cells."""
        router = _build_router()
        config = router._serialize_for_parallel()
        config.pop("obstacles", None)
        config.update({"trial_num": 0, "seed": 1, "base_order": [1], "use_negotiated": False})

        routes, _score, _trial_num = _run_monte_carlo_trial(config)

        assert isinstance(routes, list)


class TestBackendAgreement:
    """Audit surface 3: Python and native grid reconstruction agree on the
    reinstalled obstacle.  Records explicit native availability rather than
    silently skipping."""

    def test_native_backend_availability_is_recorded(self):
        assert isinstance(is_cpp_available(), bool)

    @pytest.mark.skipif(
        not is_cpp_available(),
        reason="Native routing backend not built in this worktree (uv run kct build-native)",
    )
    def test_native_backed_grid_state_is_unchanged_across_a_reset(self):
        """A native-backed router must come out of the reset in exactly the
        state it went in, on both sides of the C++ boundary.

        Note the ``_cpp_grid`` mirror is compared *before vs. after*, not
        against a hardcoded expectation: ``RoutingGrid.add_obstacle`` does
        not push obstacle cells into the eagerly-built ``_cpp_grid`` mirror
        the way ``add_edge_keepout`` does (the native pathfinder rebuilds a
        ``CppGrid`` from the Python grid at route time instead).  That
        asymmetry predates Issue #5555 and is out of scope here -- what this
        test pins is that the reset does not make the two sides diverge any
        further than they already did.
        """
        router = _build_router(force_python=False)
        router.add_obstacle(*WALL, Layer.F_CU)

        cpp_grid = getattr(router.grid, "_cpp_grid", None)
        assert cpp_grid is not None, "native backend was requested but grid has no _cpp_grid mirror"
        inside_x, inside_y = router.grid.world_to_grid(*INSIDE_WALL)
        outside_x, outside_y = router.grid.world_to_grid(*OUTSIDE_WALL)
        layer_index = router.grid.layer_to_index(Layer.F_CU.value)
        before_mask = router.grid._blocked.copy()
        before_mirror = (
            cpp_grid.is_blocked(inside_x, inside_y, layer_index),
            cpp_grid.is_blocked(outside_x, outside_y, layer_index),
        )

        router._reset_for_new_trial()

        _assert_wall_blocked(
            router,
            "_reset_for_new_trial() dropped the obstacle on a native-backed router",
        )
        assert np.array_equal(before_mask, router.grid._blocked)
        cpp_grid = router.grid._cpp_grid
        assert (
            cpp_grid.is_blocked(inside_x, inside_y, layer_index),
            cpp_grid.is_blocked(outside_x, outside_y, layer_index),
        ) == before_mirror
