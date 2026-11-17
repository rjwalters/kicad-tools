"""New copper-contact escapes must respect full copper and actual cutouts."""

from types import SimpleNamespace

import pytest

from kicad_tools.core.board_outline import OutlineSegments
from kicad_tools.router.core import Autorouter
from kicad_tools.router.lattice.escape_boundary import EscapeBoundary
from kicad_tools.router.lattice.pathfinder import LatticePathfinder


def ring(points):
    return list(zip(points, points[1:] + points[:1], strict=True))


OUTER = [(0, 0), (10, 0), (10, 10), (0, 10)]


def test_caps_width_clearance_and_curve_error_are_charged():
    boundary = EscapeBoundary(OutlineSegments(ring(OUTER), max_error_mm=0.05), 0.3)
    assert boundary.segment_clear((1.35, 3), (1.35, 7), 1.0)
    assert not boundary.segment_clear((1.349, 3), (1.349, 7), 1.0)
    assert not boundary.segment_clear((1.35, 1.349), (1.35, 7), 1.0)
    assert not boundary.segment_clear((-2, 3), (-2, 7), 1.0)


def test_concave_outline_and_cutout_cannot_be_crossed():
    cutout = [(4, 4), (6, 4), (6, 6), (4, 6)]
    boundary = EscapeBoundary(ring(OUTER) + ring(cutout), 0.2)
    assert boundary.segment_clear((2, 2), (2, 8), 0.5)
    assert not boundary.segment_clear((3, 5), (7, 5), 0.1)
    assert not boundary.segment_clear((5, 5), (5, 5), 0.1)
    notch = [(0, 0), (10, 0), (10, 10), (6, 10), (6, 4), (4, 4), (4, 10), (0, 10)]
    assert not EscapeBoundary(ring(notch), 0).segment_clear((2, 7), (8, 7), 0.2)


@pytest.mark.parametrize(
    "edges", [[], ring(OUTER)[:-1], ring([(0, 0), (10, 10), (0, 10), (10, 0)])]
)
def test_unproved_outline_disables_new_fallback(edges):
    assert not EscapeBoundary(edges, 0).segment_clear((2, 2), (3, 2), 0.1)


def test_router_refreshes_actual_edges_floor_and_error_on_reused_pathfinder():
    pf = LatticePathfinder(OUTER, [])
    router = Autorouter.__new__(Autorouter)
    router._lattice_pathfinder = pf
    router.grid = SimpleNamespace(fixed_fills=pf.fixed_fills)
    router._edge_segments = OutlineSegments(ring(OUTER), max_error_mm=0.05)
    router._edge_clearance = 0.3
    assert router._ensure_lattice_pathfinder() is pf
    assert not pf._escape_boundary.segment_clear((1.3, 3), (1.3, 7), 1.0)
    router._edge_clearance = 0.0
    router._ensure_lattice_pathfinder()
    assert pf._escape_boundary.segment_clear((1.3, 3), (1.3, 7), 1.0)
    router._edge_segments = []
    router._ensure_lattice_pathfinder()
    assert not pf._escape_boundary.segment_clear((5, 3), (5, 7), 1.0)


@pytest.mark.parametrize("worker", ["monte_carlo", "evolutionary"])
@pytest.mark.parametrize("empty", [False, True])
def test_worker_preserves_zero_floor_cutout_or_invalid_outline(monkeypatch, worker, empty):
    import pickle

    from kicad_tools.router.algorithms.evolutionary import _run_evolutionary_trial
    from kicad_tools.router.core import _run_monte_carlo_trial
    from kicad_tools.router.rules import DesignRules

    router = Autorouter(10, 10, rules=DesignRules(grid_resolution=0.5), force_python=True)
    edges = [] if empty else ring(OUTER) + ring([(4, 4), (6, 4), (6, 6), (4, 6)])
    router._edge_segments = OutlineSegments(edges, max_error_mm=0.05)
    router._edge_clearance = 0.0
    config = pickle.loads(pickle.dumps(router._serialize_for_parallel()))
    config.update(
        trial_num=0, chrom_idx=0, seed=42, base_order=[], net_order=[], use_negotiated=False
    )
    seen = []

    def observe(trial, *args, **kwargs):
        boundary = trial._ensure_lattice_pathfinder()._escape_boundary
        assert boundary.valid == (not empty)
        assert not boundary.segment_clear((3, 5), (7, 5), 0.1)
        assert not boundary.segment_clear((1.04, 2), (1.04, 8), 1.0)
        seen.append(True)
        return []

    monkeypatch.setattr(Autorouter, "route_all", observe)
    monkeypatch.setattr(Autorouter, "_evaluate_solution", lambda self, routes: 0)
    if worker == "monte_carlo":
        _run_monte_carlo_trial(config)
    else:
        _run_evolutionary_trial(config)
    assert seen
