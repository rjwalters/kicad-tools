"""Edge parsing must never borrow coordinates or layers from adjacent objects."""

import pytest

from kicad_tools.router.io import _extract_edge_segments


@pytest.mark.parametrize("layer_first", [False, True])
def test_rectangle_does_not_consume_following_copper(layer_first):
    coords = "(start -10 -20) (end 30 40)"
    layer = '(layer "Edge.Cuts")'
    rect = f"(gr_rect {layer} {coords})" if layer_first else f"(gr_rect {coords} {layer})"
    pcb = f'(kicad_pcb {rect} (segment (start 5 6) (end 15 6) (layer "F.Cu")))'
    assert _extract_edge_segments(pcb) == [
        ((-10, -20), (30, -20)),
        ((30, -20), (30, 40)),
        ((30, 40), (-10, 40)),
        ((-10, 40), (-10, -20)),
    ]


def test_non_edge_graphics_cannot_borrow_later_edge_layer():
    pcb = '(kicad_pcb (gr_line (start 5 5) (end 9 9) (layer "F.SilkS")) (gr_line (layer "Edge.Cuts") (start -1 -2) (end 3 4)))'
    assert _extract_edge_segments(pcb) == [((-1, -2), (3, 4))]


def test_missing_coordinates_cannot_borrow_next_object():
    with pytest.raises(ValueError, match="Malformed Edge.Cuts"):
        _extract_edge_segments(
            '(kicad_pcb (gr_rect (layer "Edge.Cuts")) (segment (start 1 2) (end 3 4)))'
        )
