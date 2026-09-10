"""Observed R5 escape cells must move their own copper on both KiCad schemas."""

import runpy
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_string

REPAIR = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "boards/02-charlieplex-led/finalize_routing.py")
)["_relocate_escapes"]


@pytest.mark.parametrize("named_nets", [False, True])
@pytest.mark.parametrize(
    "name,old,new",
    [
        ("VCC", (140.665, 117.3), (139.2, 115.5)),
        ("VCC", (140.665, 117.2), (139.2, 115.5)),
        ("RESET", (141.1, 118.1), (140.9, 118.5)),
        ("RESET", (141.4, 117.7), (140.9, 118.5)),
    ],
)
def test_escape_variant_moves_via_and_both_layers_only_on_its_net(named_nets, name, old, new):
    own = f'"{name}"' if named_nets else "8"
    other = '"OTHER"' if named_nets else "9"
    root_nets = "" if named_nets else f'(net 8 "{name}")(net 9 "OTHER")'
    x, y = old
    doc = parse_string(f"""(kicad_pcb {root_nets}
      (via (at {x} {y}) (net {own}))
      (segment (start {x} {y}) (end 139 115) (layer "F.Cu") (net {own}))
      (segment (start 139 115) (end {x} {y}) (layer "B.Cu") (net {own}))
      (via (at {x} {y}) (net {other}))
      (segment (start {x} {y}) (end 142 120) (net {other})))""")
    REPAIR(doc)

    def point(item, key):
        return tuple(item.find(key).get_float(i) for i in range(2))

    vias = doc.find_all("via")
    tracks = doc.find_all("segment")
    assert point(vias[0], "at") == new
    assert point(tracks[0], "start") == point(tracks[1], "end") == new
    assert point(vias[1], "at") == point(tracks[2], "start") == old
    assert point(tracks[0], "end") == point(tracks[1], "start") == (139, 115)
    REPAIR(doc)
    assert point(vias[0], "at") == new  # Stable on rerun.


def test_quantized_waypoint_without_escape_via_is_not_relocated():
    doc = parse_string("""(kicad_pcb
      (segment (start 163.9 112.7) (end 157.1 105.9) (net "NODE_D"))
      (segment (start 157.1 105.9) (end 156.7 105.9) (net "NODE_D"))
      (via (at 156.7 105.9) (net "NODE_D")))""")
    from kicad_tools.sexp import serialize_sexp

    before = serialize_sexp(doc)
    REPAIR(doc)
    assert serialize_sexp(doc) == before
