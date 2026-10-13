"""Router-class floors must not grow the lattice's static pad masks (Issue #6449).

#6427 merged the router's own net-class clearances into
``DesignRules.net_clearance_floors`` so every pair gate holds foreign copper to
``max(class(a), class(b))``.  The lattice search's static pad masks read the same
map (``LatticePathfinder._authored_pad_extra``, #6243), so every pad of a
``Power`` / ``HighCurrentSignal`` / ``Clock`` net grew by ``0.2 - base``.  That
is exactly the change #6395 measured and rejected: it moves tracks by whole
lattice rows in a fine-pitch pin field.  On board 05 it walled ``GND_SENSE``
out of ``U2.6`` between the ``PHASE_A`` / ``PHASE_B`` pins (33/33 -> 32/33,
``decline[no-path-keepout-constrained]``).  The pad side of a router-class pair
is held by ``lattice/class_pad_repair.py`` instead.

These tests pin the split: the loader records the designer-authored subset as
``rules.designer_clearance_floors`` and the pad masks grow by that subset only,
while ``net_clearance_floors`` (the #6427 pair gate) keeps the class floors.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _board_text() -> str:
    nets = {1: "GND", 2: "LINE_A"}
    parts = []
    for ref, x, y, net in [
        ("G1", 104, 104, 1),
        ("G2", 116, 104, 1),
        ("L1", 104, 109, 2),
        ("L2", 116, 109, 2),
    ]:
        parts.append(
            f'''(footprint "test" (layer "F.Cu") (at {x} {y})
          (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
          (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net {net} "{nets[net]}")))'''
        )
    table = "\n".join(f'(net {i} "{n}")' for i, n in nets.items())
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) {table}
      (gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
      {" ".join(parts)})"""


@pytest.fixture
def loaded(tmp_path):
    """No ``.kicad_pro``: ``GND``'s 0.2 mm floor comes from the router class alone."""
    board = tmp_path / "board.kicad_pcb"
    board.write_text(_board_text())
    rules = DesignRules(trace_clearance=0.15, via_clearance=0.15)
    router, nets = load_pcb_for_routing(
        str(board), rules=rules, force_python=True, validate_drc=False
    )
    return router, nets


def _pad(x: float, net: int, name: str, authored: float = 0.0) -> Pad:
    return Pad(
        x=x, y=10.0, width=0.6, height=0.6, net=net, net_name=name, authored_clearance=authored
    )


def test_loader_records_the_designer_subset_apart_from_class_floors(loaded):
    router, nets = loaded
    # #6427's pair gate keeps the router class ...
    assert router.rules.net_clearance_floors == {nets["GND"]: pytest.approx(0.2)}
    # ... but none of it is designer-authored.
    assert router.rules.designer_clearance_floors == {}
    assert router.rules.designer_clearance_floors_by_name == {}


def test_router_class_floor_does_not_grow_the_lattice_pad_masks(loaded):
    router, _nets = loaded
    pads = list(router.pads.values())
    pf = LatticePathfinder(OUTLINE, pads, router.rules)
    assert pf._authored_pad_extra() is None


def test_designer_floors_still_grow_the_pad_masks():
    """#6243 unchanged: a designer minimum grows its pad; a class floor does not."""
    rules = DesignRules(
        trace_clearance=0.15,
        via_clearance=0.15,
        net_clearance_floors={1: 0.3, 2: 0.2},
        designer_clearance_floors={1: 0.3},
        designer_clearance_floors_by_name={"AGND": 0.25},
    )
    pads = [
        _pad(5.0, 1, "HV"),  # designer floor 0.3
        _pad(8.0, 2, "PHASE_A"),  # router class floor 0.2 only
        _pad(11.0, 0, "AGND", authored=0.25),  # neutralised, designer by name
        _pad(14.0, 0, "GND", authored=0.2),  # neutralised, router class only
    ]
    extra = LatticePathfinder(OUTLINE, pads, rules)._authored_pad_extra()
    assert extra == pytest.approx([0.15, 0.0, 0.1, 0.0])


def test_rules_built_outside_the_loader_keep_the_pre_6449_growth():
    """``designer_clearance_floors=None``: every floor counts as designer-authored."""
    rules = DesignRules(trace_clearance=0.15, via_clearance=0.15, net_clearance_floors={2: 0.2})
    pads = [_pad(5.0, 1, "SIG"), _pad(8.0, 2, "PWR"), _pad(11.0, 0, "GND", authored=0.25)]
    extra = LatticePathfinder(OUTLINE, pads, rules)._authored_pad_extra()
    assert extra == pytest.approx([0.0, 0.05, 0.1])
