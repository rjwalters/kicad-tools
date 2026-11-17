"""Pad dogleg-stub property tests (issue #4278).

Stubs are the pad<->lattice bridge: 45-degree-legal two-segment doglegs
from the EXACT pad position to nearby unmasked lattice nodes, every leg
clearance-checked before acceptance (the subgrid reject-and-retry
discipline; standing #3906 lesson).  These properties are pinned before
the routing tests, per the issue's mandated order.
"""

from __future__ import annotations

from kicad_tools.router.lattice.geometry import dist
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.router.rules import DesignRules


def _pad(
    x: float,
    y: float,
    net: int,
    *,
    ref: str,
    layer: Layer = Layer.F_CU,
    width: float = 1.0,
    height: float = 1.0,
    through: bool = False,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=f"N{net}",
        layer=layer,
        ref=ref,
        pin="1",
        through_hole=through,
        drill=0.3 if through else 0.0,
    )


def _pathfinder(pads: list[Pad], size: float = 20.0) -> LatticePathfinder:
    outline = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)]
    return LatticePathfinder(outline, pads, DesignRules())


def test_stub_legs_are_45_legal_and_start_at_exact_pad_position() -> None:
    pad = _pad(7.3, 9.1, net=1, ref="U1")  # deliberately off-lattice
    pf = _pathfinder([pad])
    stubs = pf.pad_stubs(pad, net=1)
    assert stubs, "open board: pad must attach"
    for _key, _layer, poly, length in stubs:
        assert poly[0] == (pad.x, pad.y), "pad end must stay exact (no quantization)"
        assert len(poly) <= 3, "dogleg is at most two legs"
        for a, b in zip(poly, poly[1:], strict=False):
            assert is_45_aligned(b[0] - a[0], b[1] - a[1]), f"off-angle stub leg {a}->{b}"
        assert length > 0.0


def test_stub_lands_on_lattice_node_and_respects_pad_layer() -> None:
    pad = _pad(7.3, 9.1, net=1, ref="U1", layer=Layer.B_CU)
    pf = _pathfinder([pad])
    lattice = pf.build()
    b_idx = pf.layer_stack.layer_enum_to_index(Layer.B_CU)
    stubs = pf.pad_stubs(pad, net=1)
    assert stubs
    for key, layer, poly, _length in stubs:
        assert layer == b_idx, "SMD pad stubs must stay on the pad's own layer"
        assert dist(poly[-1], lattice.node_point(key)) < 1e-9


def test_through_hole_pad_attaches_on_every_layer() -> None:
    pad = _pad(10.0, 10.0, net=1, ref="J1", through=True)
    pf = _pathfinder([pad])
    stubs = pf.pad_stubs(pad, net=1)
    layers = {layer for _k, layer, _p, _l in stubs}
    assert layers == {0, 1}, f"PTH pad must offer stubs on all layers, got {layers}"


def test_stub_legs_are_clearance_checked_against_other_net_pads() -> None:
    # A wall of other-net pads to the east: accepted stubs must not cross it.
    pad = _pad(10.0, 10.0, net=1, ref="U1")
    wall = [
        _pad(11.2, 10.0 + dy, net=2, ref=f"W{i}", width=0.8, height=3.0)
        for i, dy in enumerate((-3.0, 0.0, 3.0))
    ]
    pf = _pathfinder([pad] + wall)
    obstacles = pf.obstacles
    stubs = pf.pad_stubs(pad, net=1)
    assert stubs, "west side is open: pad must still attach"
    for _key, layer, poly, _length in stubs:
        for a, b in zip(poly, poly[1:], strict=False):
            assert not obstacles.segment_blocked(a, b, layer, net=1), (
                f"accepted stub leg {a}->{b} crosses an other-net keep-out"
            )


def test_stub_blocked_by_committed_copper_declines_never_blind_fits() -> None:
    """#3906-modeled: committed OTHER-net copper running straight through the
    pad's escape field must yield NO stubs -- every candidate dogleg would
    violate the copper gap, so the engine declines instead of emitting a
    short.  (A blind fit that ignored the committed model would happily
    return the geometrically-shortest dogleg here.)"""
    pad = _pad(10.0, 10.0, net=1, ref="U1", width=0.6, height=0.6)
    pf = _pathfinder([pad])
    committed = pf._fresh_committed()
    # Other-net trace directly through the pad centre, spanning well past the
    # stub search radius on the pad's layer.
    committed.add_run(0, [(2.0, 10.0), (18.0, 10.0)], net=2, half_width=0.1)

    stubs = pf.pad_stubs(pad, net=1, committed=committed)
    assert stubs == [], "stub across committed other-net copper must decline"

    # Without the committed copper the same pad attaches fine (the decline
    # above is the obstacle consult, not a general inability).
    assert pf.pad_stubs(pad, net=1)


def test_npth_drilled_pad_blocks_every_layer() -> None:
    """NPTH mounting holes load as np_thru_hole pads with
    ``through_hole=False`` on one copper layer, but the drilled barrel
    exists on EVERY layer (issue #4271: softstart shipped an In1.Cu track
    through a 2.7mm fuse-holder hole).  ``drill > 0`` must through-block."""
    from kicad_tools.router.lattice.pathfinder import LatticePathfinder
    from kicad_tools.router.layers import Layer, LayerStack
    from kicad_tools.router.primitives import Pad

    npth = Pad(
        x=10.0,
        y=5.0,
        width=2.7,
        height=2.7,
        net=0,
        net_name="",
        layer=Layer.F_CU,
        ref="F1",
        pin="",
        through_hole=False,
        drill=2.7,
    )
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)]
    pf = LatticePathfinder(outline, [npth], layer_stack=LayerStack.four_layer_all_signal())
    assert pf._pad_layer_indices(npth) == (0, 1, 2, 3)
