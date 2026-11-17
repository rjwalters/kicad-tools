"""Lattice keep-out around an NPTH honours hole_clearance (Issue #8684)."""

import pytest

from kicad_tools.router.grid import _KICAD_HOLE_CLEARANCE_MM
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.rules import DesignRules
from tests.test_router_npth_obstacle_8675 import PCB


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


def _stub(pads, rules):
    return type("S", (), {"pads": pads, "rules": rules})()


def test_npth_pad_extra_reaches_hole_clearance(router):
    pads = list(router.grid._pads)
    extra = LatticePathfinder._npth_pad_extra(_stub(pads, router.rules))
    assert extra is not None
    for pad, e in zip(pads, extra, strict=True):
        if pad.drill > 0 and not pad.through_hole:
            assert router.rules.trace_clearance + e >= _KICAD_HOLE_CLEARANCE_MM
        else:
            assert e == 0.0


def test_no_npth_is_unchanged(router):
    pads = [p for p in router.grid._pads if p.through_hole]
    assert LatticePathfinder._npth_pad_extra(_stub(pads, router.rules)) is None
