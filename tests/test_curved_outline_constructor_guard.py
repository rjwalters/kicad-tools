"""BBox-only engines must not silently admit newly supported curved edges."""

import pytest

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.mesh.pathfinder import MeshPathfinder


@pytest.mark.parametrize("engine", [MeshPathfinder, LatticePathfinder])
@pytest.mark.parametrize(
    "outline",
    [
        '(gr_circle (center 5 5) (end 10 5) (layer "Edge.Cuts"))',
        '(gr_rect (start 0 0) (end 10 10) (layer "Edge.Cuts")) '
        '(gr_circle (center 5 5) (end 6 5) (layer "Edge.Cuts"))',
    ],
)
def test_bbox_constructor_refuses_curved_boundary(engine, outline):
    with pytest.raises(ValueError, match="curved Edge.Cuts.*bounding box"):
        engine.from_board(f"(kicad_pcb {outline})")


@pytest.mark.parametrize("engine", [MeshPathfinder, LatticePathfinder])
def test_bbox_constructor_retains_shifted_rectangle(engine):
    router = engine.from_board(
        '(kicad_pcb (gr_rect (start 20 30) (end 30 40) (layer "Edge.Cuts")))'
    )
    assert router.outline == [(20, 30), (30, 30), (30, 40), (20, 40)]
