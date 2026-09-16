"""Pad-padding provenance can be checked without loading an unbuilt native ABI."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def grid_and_pad(name="GND", net=0):
    grid = RoutingGrid(10, 10, DesignRules(grid_resolution=0.1), layer_stack=LayerStack.two_layer())
    pad = Pad(x=5, y=5, width=0.45, height=0.45, net=net, net_name=name, layer=Layer.F_CU)
    return grid, pad


@pytest.mark.parametrize(
    "name,net,pitch,eligible",
    [
        ("GND", 0, 1.27, True),
        ("+3V3", 2, 1.27, True),
        ("", 0, 1.27, False),
        ("MIPI_RST", 0, 1.27, False),
        ("GND", 0, None, False),
        ("GND", 0, 2.54, False),
    ],
)
def test_only_fine_pitch_plane_pad_padding_is_labelled(name, net, pitch, eligible):
    grid, pad = grid_and_pad(name, net)
    grid.add_pad(pad, pin_pitch=pitch)
    assert bool(grid._pad_halo_cells) == eligible
    x, y = grid.world_to_grid(5, 5)
    assert grid._blocked[0, y, x]
    assert grid._pad_blocked[0, y, x]
    # Metal ownership remains explicit even where later pad escape setup
    # clears the separate obstacle bit; native relief checks pad_blocked too.
    if eligible:
        padding = [
            key
            for key in grid._pad_halo_cells
            if not grid._pad_blocked[key] and not grid._is_obstacle[key]
        ]
        # Skipped plane pads retain an isolated clearance halo; an active
        # plane net may already have that halo opened by own-net escape.
        if net == 0:
            assert padding
        assert all(grid._blocked[key] for key in padding)


@pytest.mark.parametrize("first", [False, True])
def test_unknown_static_block_revokes_provenance_in_either_order(first):
    grid, pad = grid_and_pad()
    x, y = grid.world_to_grid(5.4, 5)
    if first:
        grid.cell_at(0, y, x).blocked = True
    grid.add_pad(pad, pin_pitch=1.27)
    if not first:
        assert (0, y, x) in grid._pad_halo_cells
        grid.cell_at(0, y, x).blocked = True
    assert grid._blocked[0, y, x]
    assert (0, y, x) not in grid._pad_halo_cells


def test_nonplane_pad_overlap_revokes_relief_without_erasing_metal():
    grid, pad = grid_and_pad()
    grid.add_pad(pad, pin_pitch=1.27)
    before = grid._pad_blocked.copy()
    grid.add_pad(
        Pad(x=5, y=5, width=0.45, height=0.45, net=0, net_name="NC", layer=Layer.F_CU),
        pin_pitch=1.27,
    )
    assert not grid._pad_halo_cells
    assert (grid._pad_blocked == before).all()


def test_threshold_is_strict():
    grid, pad = grid_and_pad()
    grid.add_pad(pad, pin_pitch=grid.rules.fine_pitch_threshold)
    assert not grid._pad_halo_cells
