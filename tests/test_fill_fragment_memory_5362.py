"""Sparse fragmented fills must not allocate all possible fragment pairs."""

import tracemalloc

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.copper_contact import build_copper_contact_graph


def test_fragmented_solid_zone_uses_bounded_python_memory(tmp_path):
    # Disjoint copper gives the spatial index only linear candidate work.
    # At 2,000 fragments an eager Python pair set alone costs >170 MiB.
    fills = "".join(
        f'(filled_polygon (layer "F.Cu") (pts (xy {3 * i} 0) '
        f"(xy {3 * i + 1} 0) (xy {3 * i + 1} 1) (xy {3 * i} 1)))"
        for i in range(2000)
    )
    board = tmp_path / "fragmented.kicad_pcb"
    board.write_text(
        '(kicad_pcb (version 20260206) (generator "test") '
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal)) '
        '(net 0 "") (net 1 "GND") '
        '(zone (net 1) (net_name "GND") (layer "F.Cu") '
        "(min_thickness 0.2) (filled_areas_thickness no) "
        f"{fills}))"
    )
    validator = ConnectivityValidator(PCB.load(board))
    assert len(validator.pcb.zones[0].filled_polygons) == 2000
    tracemalloc.start()
    try:
        # The shared copper-contact graph (Issue #6452) replaced the label-free
        # pour bonder; it must keep the same bounded pair bookkeeping.
        graph = build_copper_contact_graph(validator, None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A solid fill never bonds its own disjoint fragments.
    assert len(graph.components()) == 2000
    # Deliberately generous relative to linear bookkeeping (<5 MiB here).
    # This measures Python allocation, not native GEOS buffers or wall time.
    assert peak < 64 * 1024 * 1024, f"fragment bookkeeping allocated {peak} bytes"
