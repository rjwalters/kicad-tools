"""Native departure API port; independent endpoint/crossover policy is out of scope."""
import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="matching native backend required")
_P = (10, 10)
_N = (22, 10)

def _finder():
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=4,
        height=3,
        rules=rules,
        resolution_override=0.1,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    # Represent actual own-net endpoint pad metal. Its drill-cell exception
    # must remain available even though pad_blocked is set.
    for (x, y), net in [(_P, 1), (_N, 2)]:
        grid._blocked[0, y, x] = True
        grid._pad_blocked[0, y, x] = True
        grid._net[0, y, x] = net
    return CoupledPathfinder(grid, rules, target_spacing_cells=12, min_spacing_cells=3)


def _prefix():
    return [(10, y, 0, 22, y, 0) for y in range(9, 5, -1)] + [(10, 6, 3, 22, 6, 3)]


def _route(finder, prefix=None, budget=1000, corridor=None):
    return finder._get_cpp_coupled_impl().route(
        p_start_xy=(10, 10),
        n_start_xy=(22, 10),
        start_layer=0,
        p_goal_xy=(10, 10),
        n_goal_xy=(22, 10),
        end_layer=3,
        p_net=1,
        n_net=2,
        effective_target_spacing=12,
        effective_approach_radius=6,
        effective_departure_radius=14,
        routable_layers=list(finder.grid.get_routable_indices()),
        corridor_bitset=corridor or [],
        max_iterations_budget=budget,
        timeout_seconds=1.0,
        departure_prefix=prefix,
    )


def test_valid_departure_keeps_original_start_and_via_history():
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    path, _ = _route(finder, prefix)
    assert path is not None
    assert path[0][:6] == (10, 10, 0, 22, 10, 0)
    assert [step[:6] for step in path[1 : 1 + len(prefix)]] == prefix
    assert path[len(prefix)][6] is True
    assert path[-1][:6] == (10, 10, 3, 22, 10, 3)


@pytest.mark.parametrize(
    ("obstruction", "progress"),
    [
        # Issue #5333: the step each obstruction actually blocks.  0 means the
        # very first required step was refused; a value one below the prefix
        # length means only the closing paired via was.
        ("jump", 0),
        ("self_trail", 2),
        ("foreign_barrel", 4),
        ("corridor", 0),
    ],
)
def test_a_refused_departure_reports_the_step_that_blocked_it(obstruction, progress):
    """Without this, every refusal looks identical: an empty validated path.

    A pair whose escape dies leaving the pad row and one whose escape dies on
    its layer transition need different fixes, and ``departures=0`` alone
    cannot tell them apart (#5333).
    """
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    corridor = None
    if obstruction == "jump":
        prefix = [prefix[2]]
    elif obstruction == "self_trail":
        prefix = [prefix[0], prefix[1], prefix[0]]
    elif obstruction == "partner_barrel":
        prefix += [(x, 6, 3, 22, 6, 3) for x in range(11, 19)]
    elif obstruction == "foreign_barrel":
        finder.grid._blocked[1, 6, 10] = True
        finder.grid._net[1, 6, 10] = 99
    elif obstruction == "unsupported_pad":
        prefix = [(10, 10, 3, 22, 10, 3)]
    else:
        corridor = [0] * (finder.grid.rows * finder.grid.cols)
    path, diagnostics = _route(finder, prefix, corridor=corridor)
    assert path is None
    assert diagnostics["departure_prefix_progress"] == progress < len(prefix)


def test_a_validated_departure_reports_its_full_prefix_progress():
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    _, diagnostics = _route(finder, prefix, budget=len(prefix) + 2)
    assert diagnostics["departure_prefix_progress"] == len(prefix)
    assert [step[:6] for step in diagnostics["validated_departure_path"][1:]] == prefix
    # No prefix requested means no prefix progress to report.
    assert _route(finder, [])[1]["departure_prefix_progress"] == 0


@pytest.mark.parametrize(
    "obstruction",
    ["jump", "self_trail", "foreign_barrel", "corridor"],
)
def test_departure_cannot_bypass_normal_constraints(obstruction):
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    corridor = None
    if obstruction == "jump":
        prefix = [prefix[2]]
    elif obstruction == "self_trail":
        prefix = [prefix[0], prefix[1], prefix[0]]
    elif obstruction == "partner_barrel":
        prefix += [(x, 6, 3, 22, 6, 3) for x in range(11, 19)]
    elif obstruction == "foreign_barrel":
        finder.grid._blocked[1, 6, 10] = True
        finder.grid._net[1, 6, 10] = 99
    elif obstruction == "unsupported_pad":
        prefix = [(10, 10, 3, 22, 10, 3)]
    else:
        corridor = [0] * (finder.grid.rows * finder.grid.cols)
    path, diagnostics = _route(finder, prefix, corridor=corridor)
    assert path is None
    assert diagnostics["validated_departure_path"] == []


def test_prefix_steps_consume_original_iteration_budget():
    path, diagnostics = _route(_finder(), _prefix(), budget=4)
    assert path is None
    assert diagnostics["iteration_limited"]
    assert diagnostics["iterations"] == 4
    assert diagnostics["validated_departure_path"] == []


def test_empty_prefix_preserves_default_search():
    finder = _finder()
    assert _route(finder) == _route(finder, [])


def test_completed_prefix_survives_failure_without_goal_progress():
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    prefix = _prefix()
    path, diagnostics = _route(finder, prefix, budget=len(prefix) + 2)
    assert path is None
    assert diagnostics["iteration_limited"]
    validated = diagnostics["validated_departure_path"]
    assert validated[0][:6] == (10, 10, 0, 22, 10, 0)
    assert [step[:6] for step in validated[1:]] == prefix
    assert validated[-1][6] is True
    # The prefix moves away from these goals; best progress is a separate view.
    assert diagnostics["best_path"] != validated
    _, fresh = _route(finder, [], budget=2)
    assert fresh["validated_departure_path"] == []



def test_departure_prefix_cannot_bypass_current_fixed_fill_guard():
    from shapely.geometry import box

    from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles

    finder = _finder()
    finder.grid.install_fixed_fills(
        FixedFillObstacles((FixedFill("FOREIGN", 99, 0, 0.2, box(0.95, 0.84, 1.05, 0.94)),))
    )
    path, diagnostics = _route(finder, _prefix())
    assert path is None
    assert diagnostics["departure_prefix_progress"] == 0
    assert diagnostics["validated_departure_path"] == []
    assert diagnostics["best_path"][0][:6] == (10, 10, 0, 22, 10, 0)


def test_budget_before_first_expansion_has_no_diagnostic_geometry():
    path, diagnostics = _route(_finder(), _prefix(), budget=1)
    assert path is None
    assert diagnostics["iterations"] == 1
    assert diagnostics["iteration_limited"]
    assert diagnostics["best_path"] == []
    assert diagnostics["validated_departure_path"] == []
    assert diagnostics["departure_prefix_progress"] == 0


def test_success_keeps_best_failure_path_empty():
    path, diagnostics = _route(_finder(), _prefix())
    assert path is not None
    assert diagnostics["best_path"] == []
    assert diagnostics["departure_prefix_progress"] == len(_prefix())


def _public_pads(finder):
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    return tuple(
        Pad(x=x, y=y, width=0.2, height=0.2, net=net, net_name=str(net), layer=layer)
        for net, gx in ((1, 10), (2, 22))
        for layer in (Layer.F_CU, Layer.B_CU)
        for x, y in [finder.grid.grid_to_world(gx, 10)]
    )


def test_public_pathfinder_exposes_only_fully_validated_departure():
    finder = _finder()
    finder.rules.manufacturer = "jlcpcb"
    pads = _public_pads(finder)
    prefix = _prefix()
    result = finder.route_coupled(
        *pads, departure_prefix=prefix, max_iterations_budget=len(prefix) + 2, timeout_seconds=1
    )
    assert result is None
    assert finder.last_iterations == len(prefix) + 2
    assert [step[:6] for step in finder.last_validated_departure_path[1:]] == prefix
    assert finder.last_validated_departure_path[0][:6] == (10, 10, 0, 22, 10, 0)
    finder.route_coupled(*pads, departure_prefix=prefix, max_iterations_budget=4, timeout_seconds=1)
    assert finder.last_validated_departure_path == []
    assert finder.last_iteration_limited and finder.last_iterations == 4


def test_public_departure_fails_closed_without_native_backend(monkeypatch):
    finder = _finder()
    finder.last_validated_departure_path = [(1, 2, 3, 4, 5, 6, False)]
    monkeypatch.setattr(finder, "_cpp_coupled_available", lambda: False)
    monkeypatch.setattr(
        finder,
        "_get_coupled_neighbors",
        lambda *a, **kw: pytest.fail("unconstrained Python fallback"),
    )
    assert (
        finder.route_coupled(
            *_public_pads(finder),
            departure_prefix=_prefix(),
            max_iterations_budget=10,
            timeout_seconds=1,
        )
        is None
    )
    assert finder.last_validated_departure_path == []
    assert finder.last_rejections["departure_backend_unavailable"] == 1


def test_partial_reconstruction_stops_at_heads_without_mutating_terminals():
    finder = _finder()
    pads = _public_pads(finder)
    prefix = _prefix()
    assert finder.route_coupled(
        *pads, departure_prefix=prefix, max_iterations_budget=len(prefix) + 2, timeout_seconds=1
    ) is None
    terminals = finder._cpp_reconstruct_pads
    partial = finder._reconstruct_coupled_routes_from_cpp_path(
        finder.last_validated_departure_path, partial=True
    )
    for route, gx in zip(partial, (10, 22), strict=True):
        end = finder.grid.grid_to_world(gx, 6)
        assert (route.segments[-1].x2, route.segments[-1].y2) == end
        assert (route.vias[-1].x, route.vias[-1].y) == end
    assert finder._cpp_reconstruct_pads is terminals
    assert finder._cpp_reconstruct_pads[1].y == 1.0
    assert finder._cpp_reconstruct_pads[3].y == 1.0
