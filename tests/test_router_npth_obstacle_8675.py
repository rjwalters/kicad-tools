"""An NPTH pad is a hole on every copper layer (Issue #8675)."""

from types import SimpleNamespace

import pytest

from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.rules import DesignRules

PCB = """(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "N")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default))
           (fill none) (layer "Edge.Cuts"))
  (footprint "Connector" (layer "F.Cu") (at 10 10)
    (property "Reference" "J1")
    (pad "1" thru_hole circle (at -5 0) (size 1.6 1.6) (drill 0.8)
         (layers "*.Cu" "*.Mask") (net 1 "N"))
    (pad "2" thru_hole circle (at 5 0) (size 1.6 1.6) (drill 0.8)
         (layers "*.Cu" "*.Mask") (net 1 "N"))
    (pad "" np_thru_hole circle (at 0 0) (size 2 2) (drill 2)
         (layers "*.Cu" "*.Mask"))))
"""


@pytest.fixture
def router(tmp_path):
    path = tmp_path / "npth.kicad_pcb"
    path.write_text(PCB)
    r, _ = load_pcb_for_routing(
        str(path),
        rules=DesignRules(grid_resolution=0.1),
        use_pcb_rules=False,
        validate_drc=False,
        force_python=True,
    )
    return r


def test_npth_blocks_every_layer_at_hole_clearance(router):
    g = router.grid
    # Hole radius 1.0 + hole clearance 0.25 + half trace width.
    reach = 1.0 + 0.25 + router.rules.trace_width / 2
    for layer in range(g.num_layers):
        for dx in (0.0, reach - 0.15):
            gx, gy = g.world_to_grid(10 + dx, 10)
            assert g.grid[layer][gy][gx].blocked, (layer, dx)
        gx, gy = g.world_to_grid(10 + reach + 0.3, 10)
        assert not g.grid[layer][gy][gx].blocked


def test_lattice_masks_npth_on_every_layer(router):
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder

    npth = next(p for p in router.grid._pads if p.drill > 0 and not p.through_hole)
    stub = SimpleNamespace(num_layers=router.grid.num_layers)
    got = LatticePathfinder._pad_layer_indices(stub, npth)
    assert got == tuple(range(router.grid.num_layers))
