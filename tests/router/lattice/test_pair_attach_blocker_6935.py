"""A ``pair-pad-attach`` decline names the check that rejected the dogleg (#6935).

Board 03's USB_D[1] pass-under attach on In2.Cu was rejected only by reserved
via sites (``pair-escape-via`` / ``plane-via-site`` rule areas): a through-via
occupies every layer, so the decline was correct, but the log said nothing
about why.  The gateway decline line now carries the blocking area's name.
"""

from __future__ import annotations

from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
_OUTLINE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def _board() -> tuple[LatticePathfinder, Pad]:
    pad = Pad(
        x=2.0,
        y=5.0,
        width=0.5,
        height=0.5,
        net=1,
        net_name="D+",
        layer=Layer.F_CU,
        ref="U1",
        pin="3",
    )
    return LatticePathfinder(_OUTLINE, [pad], _RULES, LayerStack.two_layer()), pad


def _via_site_area(name: str) -> KeepoutArea:
    poly = ((2.6, 4.4), (3.4, 4.4), (3.4, 5.6), (2.6, 5.6))
    return KeepoutArea(
        polygon=poly, layers=frozenset({0, 1}), blocks_tracks=True, blocks_vias=True, name=name
    )


def test_blocking_names_lists_the_tripped_areas() -> None:
    mask = LatticeKeepoutMask([_via_site_area("pair-escape-via:VBUS:U2.5")])
    assert mask.blocking_names((2.0, 5.0), (4.0, 5.0), 0, 1, 0.1) == ["pair-escape-via:VBUS:U2.5"]
    assert mask.blocking_names((2.0, 8.0), (4.0, 8.0), 0, 1, 0.1) == []


def test_pair_attach_records_the_keepout_that_rejected_it() -> None:
    pf, pad = _board()
    pf.set_keepouts(LatticeKeepoutMask([_via_site_area("plane-via-site:GND:U2.2")]))
    committed = pf._fresh_committed()
    out = pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1}, committed, 0.1)
    assert out is None
    assert "U1.3" in pf._attach_blocker
    assert "keepout plane-via-site:GND:U2.2" in pf._attach_blocker


def test_pair_attach_clear_path_still_attaches() -> None:
    pf, pad = _board()
    committed = pf._fresh_committed()
    assert pf._pair_attach(pad, (4.0, 5.0), 0, 1, {1}, committed, 0.1) is not None
