"""Softstart rev-C lattice sizing proof (issue #4278 acceptance 5).

The whole point of the epic (#4267) is breaking the uniform-grid memory
wall: the grid needs 1.14 GB for softstart rev-C (160x100 mm, 4 layers);
the lattice must stay at or under ~5% of that at default density.
Routing softstart is NOT in scope for this phase (that is P4 #4271) --
this is the substrate-size proof only.

The board is a local-only external fixture (``boards/external/softstart``
is a symlink that dangles in CI and fresh worktrees), so the test skips
cleanly when it is absent -- exactly like the chorus fixtures.  The
artifact is additionally pinned by content hash (``softstart_fixture``,
issue #4670): a drifted fixture skips with an explicit message.  Verified
against softstart commit 7800b04 (2026-08-07): the outline is still
160x100 mm after the PR-#26 NRST star-break rework, so the #4267 P0 grid
baseline below remains the right denominator.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import LayerStack

from .softstart_fixture import SOFTSTART_BOARD as _SOFTSTART
from .softstart_fixture import fixture_skip_reason

_GRID_BYTES = 1.14e9  # measured grid footprint for softstart rev-C (#4267 P0)
_BUDGET = 0.05  # "<= ~5 % of the grid" (issue #4278 acceptance 5)

# Skip when the local-only fixture is absent (CI, fresh worktrees) OR when it
# has drifted from the pinned content hash -- see softstart_fixture (#4670).
_SKIP_REASON = fixture_skip_reason()


@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
def test_softstart_revc_lattice_memory_under_five_percent_of_grid() -> None:
    text = _SOFTSTART.read_text()
    pf = LatticePathfinder.from_board(text, layer_stack=LayerStack.four_layer_all_signal())
    lattice = pf.build()
    assert pf.lattice_builds == 1
    assert pf.num_layers == 4

    n_nodes, n_edges, n_bytes = lattice.memory_estimate(pf.num_layers)
    assert n_nodes > 10_000, "suspiciously small lattice -- did the board load?"
    ratio = n_bytes / _GRID_BYTES
    assert ratio <= _BUDGET, (
        f"softstart lattice {n_bytes / 1e6:.1f} MB = {100 * ratio:.2f}% of the "
        f"1.14 GB grid, above the {100 * _BUDGET:.0f}% budget "
        f"({n_nodes} nodes, {n_edges} edges/layer)"
    )
