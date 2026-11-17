"""``RoutingGrid.occupancy_generation`` and its completeness (Issue #4794).

The pure-Python pairwise (HV-isolation) widening bitmap
(``Router._pairwise_expanded_blocked``) caches full-grid dilated masks across
``route()`` calls.  That cache is only safe if EVERY mutation of
``grid._blocked`` / ``grid._net`` advances the grid's occupancy generation --
a missed bump means a search consults a bitmap in which foreign copper
committed since the last call is simply absent, which is strictly worse than
the recomputation the cache removes.

Two layers of guard live here:

* **Mechanical completeness** -- an AST scan of ``src/kicad_tools`` that finds
  every assignment into a ``_blocked``/``_net`` array (and every
  ``np.copyto`` into one) and asserts the enclosing function also bumps the
  counter.  This is the check that fails when a future change adds a new
  write site and forgets the bump.
* **Behavioural** -- the production commit/rip-up/pad paths actually move the
  counter.
"""

from __future__ import annotations

import ast
from pathlib import Path

from kicad_tools.router.grid import RoutedNetsUnblocker, RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kicad_tools"

# The two occupancy planes the pairwise cache is derived from.  Sibling planes
# (``_pad_blocked``, ``_original_net``, ``_is_obstacle``, ...) are deliberately
# NOT tracked: no occupancy-derived cache reads them.
OCCUPANCY_ARRAYS = frozenset({"_blocked", "_net"})
BUMP_METHOD = "bump_occupancy_generation"
COUNTER_ATTR = "_occupancy_generation"


def _attr_base(node: ast.expr) -> ast.Attribute | None:
    """Peel subscripts off an assignment target, returning the attribute."""
    while isinstance(node, ast.Subscript):
        node = node.value
    return node if isinstance(node, ast.Attribute) else None


def _bumps(func: ast.AST) -> bool:
    """True when ``func``'s body advances the occupancy generation."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr == BUMP_METHOD:
                return True
        if isinstance(node, ast.AugAssign):
            base = _attr_base(node.target)
            if base is not None and base.attr == COUNTER_ATTR:
                return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                base = _attr_base(target)
                if base is not None and base.attr == COUNTER_ATTR:
                    return True
        if isinstance(node, ast.AnnAssign):
            base = _attr_base(node.target)
            if base is not None and base.attr == COUNTER_ATTR:
                return True
    return False


def _write_sites(path: Path) -> list[tuple[int, str, bool]]:
    """Return ``(lineno, function, function_bumps)`` for occupancy writes."""
    tree = ast.parse(path.read_text())
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def enclosing(lineno: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        best = None
        for func in functions:
            end = func.end_lineno or func.lineno
            if func.lineno <= lineno <= end and (best is None or func.lineno > best.lineno):
                best = func
        return best

    writes: list[int] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            base = _attr_base(target)
            if base is not None and base.attr in OCCUPANCY_ARRAYS:
                writes.append(node.lineno)
        # ``np.copyto(grid._blocked, saved)`` mutates without an assignment.
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr == "copyto" and node.args:
                base = _attr_base(node.args[0])
                if base is not None and base.attr in OCCUPANCY_ARRAYS:
                    writes.append(node.lineno)

    sites: list[tuple[int, str, bool]] = []
    for lineno in sorted(set(writes)):
        func = enclosing(lineno)
        if func is None:  # module level -- no such site exists today
            sites.append((lineno, "<module>", False))
        else:
            sites.append((lineno, func.name, _bumps(func)))
    return sites


def test_every_occupancy_write_site_bumps_the_generation() -> None:
    """Invalidation-completeness check for the Option-A global counter.

    Mechanical, and deliberately over-broad: it flags any function anywhere in
    ``src/kicad_tools`` that writes ``_blocked``/``_net`` without bumping,
    including C++-mirrored commit paths (``Autorouter._mark_route`` and
    friends) that reach the Python planes.
    """
    offenders: list[str] = []
    seen = 0
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for lineno, func, bumps in _write_sites(path):
            seen += 1
            if not bumps:
                offenders.append(f"{path.relative_to(SRC_ROOT)}:{lineno} in {func}()")

    assert seen > 0, "AST scan found no occupancy write sites -- scanner is broken"
    assert not offenders, (
        "occupancy write site(s) that do not bump RoutingGrid.occupancy_generation "
        "(a cached pairwise widening bitmap would go stale here -- see Issue #4794):\n  "
        + "\n  ".join(offenders)
    )


def test_scanner_would_catch_a_missing_bump() -> None:
    """Negative control: the scan is not vacuously passing."""
    source = (
        "class G:\n"
        "    def sneaky(self):\n"
        "        self._blocked[0, 0, 0] = True\n"
        "    def honest(self):\n"
        "        self._net[0, 0, 0] = 1\n"
        "        self.bump_occupancy_generation()\n"
    )
    tmp = Path(__file__).parent / "_occupancy_scan_probe.py.txt"
    tmp.write_text(source)
    try:
        sites = _write_sites(tmp)
    finally:
        tmp.unlink()
    assert [(func, bumps) for _line, func, bumps in sites] == [
        ("sneaky", False),
        ("honest", True),
    ]


# ---------------------------------------------------------------------------
# Behavioural: the production paths move the counter
# ---------------------------------------------------------------------------


def _grid() -> RoutingGrid:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    return RoutingGrid(width=10.0, height=10.0, rules=rules, layer_stack=LayerStack.two_layer())


def _route(net: int = 1) -> Route:
    return Route(
        net=net,
        net_name="N1",
        segments=[
            Segment(
                x1=2.0,
                y1=5.0,
                x2=8.0,
                y2=5.0,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
                net_name="N1",
            )
        ],
    )


def test_fresh_grid_reports_a_generation() -> None:
    grid = _grid()
    assert isinstance(grid.occupancy_generation, int)
    before = grid.occupancy_generation
    grid.bump_occupancy_generation()
    assert grid.occupancy_generation == before + 1


def test_mark_and_unmark_route_bump_the_generation() -> None:
    grid = _grid()
    route = _route()
    before = grid.occupancy_generation
    grid.mark_route(route)
    after_mark = grid.occupancy_generation
    assert after_mark > before
    grid.unmark_route(route)
    assert grid.occupancy_generation > after_mark


def test_add_pad_paths_bump_the_generation() -> None:
    grid = _grid()
    before = grid.occupancy_generation
    grid.add_pad(Pad(x=3.0, y=3.0, width=1.0, height=1.0, net=1, net_name="N1", layer=Layer.F_CU))
    after_pad = grid.occupancy_generation
    assert after_pad > before

    grid.add_pad_vectorized(
        Pad(x=7.0, y=7.0, width=1.0, height=1.0, net=2, net_name="N2", layer=Layer.F_CU)
    )
    assert grid.occupancy_generation > after_pad


def test_routed_nets_unblocker_bumps_on_entry_and_exit() -> None:
    grid = _grid()
    grid.mark_route(_route())
    before = grid.occupancy_generation
    with RoutedNetsUnblocker(grid):
        inside = grid.occupancy_generation
        assert inside > before
    assert grid.occupancy_generation > inside
