"""Lattice via-in-pad tier gate (issue #4284).

The P2.7 claim "via-in-pad is N/A-by-construction" did not hold: the static
pad masks exclude only OTHER-net pads, so a lattice node under a same-net
SMD pad was a legal via site and the cheapest layer change on a net
departing a pad was often AT the pad itself -- 6 measured ``via_in_pad``
manufacturability errors on board 02 at the default jlcpcb tier (3 drilled
dead-center, 3 at 0.283 mm offset, all inside 1.0 x 1.3 mm SMD pads).

The fix mirrors the mesh engine's ``_via_allowed_at`` gate
(``mesh/pathfinder.py``): a via whose barrel would intersect a same-net SMD
pad rect (window: pad half-extent + via radius) is rejected unless
``MfrLimits.via_in_pad_supported`` holds for ``DesignRules.manufacturer``
(base ``jlcpcb`` = False, ``jlcpcb-tier1`` = True; no manufacturer =
conservative False).  Other-net pad sites stay unconditionally rejected.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

_REPO = Path(__file__).resolve().parents[3]
_CHARLIEPLEX = _REPO / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"

# The 6 measured offenders from issue #4284 (ref, pin): three vias were
# drilled dead-center in the pad, three at (+/-0.2, +/-0.2) = 0.283 mm from
# center -- all entirely within the 0.5 x 0.65 mm pad half-extent.
_AFFECTED_PADS: list[tuple[str, str]] = [
    ("D1", "2"),  # NODE_A, via at (-0.200, +0.200)
    ("R1", "2"),  # NODE_A, via at (0.000, -0.200)
    ("D2", "1"),  # NODE_A, via at (-0.200, +0.200)
    ("D4", "2"),  # NODE_C, dead center
    ("D3", "1"),  # NODE_C, dead center
    ("D7", "1"),  # NODE_C, dead center
]


def _board_text() -> str:
    return _CHARLIEPLEX.read_text()


def _find_pad(pads: list[Pad], ref: str, pin: str) -> Pad:
    for p in pads:
        if p.ref == ref and p.pin == pin:
            return p
    raise AssertionError(f"pad {ref}-{pin} not found on board 02")


def _barrel_nodes(pf: LatticePathfinder, pad: Pad) -> list[tuple]:
    """Lattice node keys whose via barrel would intersect ``pad``'s rect."""
    lattice = pf.build()
    via_r = pf.rules.via_diameter / 2.0
    hx = pad.width / 2.0 + via_r
    hy = pad.height / 2.0 + via_r
    return [
        key
        for key, pt in lattice.nodes.items()
        if abs(pt[0] - pad.x) <= hx and abs(pt[1] - pad.y) <= hy
    ]


# ---------------------------------------------------------------------------
# Unit gate: _via_ok on the 6 measured board-02 geometries.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manufacturer", [None, "jlcpcb"])
def test_via_ok_rejects_same_net_smd_pad_sites_at_default_tier(
    manufacturer: str | None,
) -> None:
    """Every lattice node whose barrel intersects one of the 6 measured
    same-net SMD pads is via-illegal at the default tier (and with no
    manufacturer configured -- the conservative default)."""
    from kicad_tools.router.io import load_pads_for_analysis

    text = _board_text()
    pads = load_pads_for_analysis(text)
    pf = LatticePathfinder.from_board(text, rules=DesignRules(manufacturer=manufacturer))
    committed = pf._fresh_committed()

    for ref, pin in _AFFECTED_PADS:
        pad = _find_pad(pads, ref, pin)
        keys = _barrel_nodes(pf, pad)
        # The refine regions put fine lattice nodes on every pad; the bug
        # report proves dead-center nodes exist (3 vias drilled at exact
        # pad centers), so an empty set would mean the fixture broke.
        assert keys, f"no lattice nodes under pad {ref}-{pin}; fixture broken"
        for key in keys:
            assert not pf._via_ok(key, pad.net, committed), (
                f"via allowed at node {key} inside same-net SMD pad {ref}-{pin} "
                f"at manufacturer={manufacturer!r}"
            )


def test_via_ok_allows_same_net_pad_site_when_tier_supports_via_in_pad() -> None:
    """At jlcpcb-tier1 (via_in_pad_supported=True) the gate flips: the
    dead-center node under a same-net SMD pad becomes via-legal again."""
    from kicad_tools.router.io import load_pads_for_analysis

    text = _board_text()
    pads = load_pads_for_analysis(text)
    pf = LatticePathfinder.from_board(text, rules=DesignRules(manufacturer="jlcpcb-tier1"))
    committed = pf._fresh_committed()
    lattice = pf.build()

    flipped = 0
    for ref, pin in _AFFECTED_PADS:
        pad = _find_pad(pads, ref, pin)
        keys = _barrel_nodes(pf, pad)
        assert keys
        # The node nearest the pad center (the dead-center attach node) must
        # pass: nothing but the same-net pad itself is anywhere near it.
        nearest = min(keys, key=lambda k: math.dist(lattice.node_point(k), (pad.x, pad.y)))
        if pf._via_ok(nearest, pad.net, committed):
            flipped += 1
    assert flipped == len(_AFFECTED_PADS), (
        f"tier1 gate only flipped {flipped}/{len(_AFFECTED_PADS)} pad-center sites"
    )


def test_via_ok_still_rejects_other_net_pad_sites_at_every_tier() -> None:
    """The other-net veto is tier-independent: a node under a foreign pad is
    never a legal via site, even on a via-in-pad-capable tier."""
    from kicad_tools.router.io import load_pads_for_analysis

    text = _board_text()
    pads = load_pads_for_analysis(text)
    for manufacturer in (None, "jlcpcb", "jlcpcb-tier1"):
        pf = LatticePathfinder.from_board(text, rules=DesignRules(manufacturer=manufacturer))
        committed = pf._fresh_committed()
        pad = _find_pad(pads, "D4", "2")
        foreign_net = pad.net + 1000  # any net that is not the pad's
        for key in _barrel_nodes(pf, pad):
            assert not pf._via_ok(key, foreign_net, committed), (
                f"via allowed under OTHER-net pad D4-2 at manufacturer={manufacturer!r}"
            )


def test_via_in_pad_allowed_property_reads_mfr_limits() -> None:
    """The gate reads the real fab model (rules.py -> mfr_limits.py)."""
    outline = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    for manufacturer, expected in [
        (None, False),
        ("jlcpcb", False),
        ("jlcpcb-tier1", True),
        ("pcbway", True),
        ("no-such-fab", False),  # unknown tier -> conservative False
    ]:
        pf = LatticePathfinder(outline, [], DesignRules(manufacturer=manufacturer))
        assert pf._via_in_pad_allowed is expected, f"manufacturer={manufacturer!r}"


# ---------------------------------------------------------------------------
# Integration: the negotiated board-02 net set ships zero via-in-pad copper
# at the default tier (issue acceptance 1) and still meets the completion
# floor with the static substrate built exactly once.
# ---------------------------------------------------------------------------


def _connections(pads: list[Pad]) -> list[tuple[object, Pad, Pad, object]]:
    bynet: dict[int, list[Pad]] = {}
    for p in pads:
        if p.net > 0:
            bynet.setdefault(p.net, []).append(p)
    conns: list[tuple[object, Pad, Pad, object]] = []
    for net, ps in bynet.items():
        anchor = ps[0]
        for seq, other in enumerate(ps[1:]):
            conns.append(((net, seq), anchor, other, None))
    return conns


def test_charlieplex_netset_ships_no_via_in_pad_at_default_tier() -> None:
    from kicad_tools.router.io import load_pads_for_analysis

    text = _board_text()
    pads = load_pads_for_analysis(text)
    conns = _connections(pads)
    assert len(conns) == 24

    pf = LatticePathfinder.from_board(text, rules=DesignRules(manufacturer="jlcpcb"))
    routes, stats = pf.route_netset(conns, max_iterations=8)
    # The fix must not strand any net (issue stranding check: the 1.2-2.0 mm
    # annulus around every affected pad keeps plenty of via-legal sites).
    assert stats.routed >= 17, (
        f"completion {stats.routed}/24 below the pre-fix floor; declines: {pf.failure_reasons}"
    )
    assert stats.lattice_builds == 1

    via_r = pf.rules.via_diameter / 2.0
    smd_pads = [p for p in pads if not p.through_hole]
    offenders: list[str] = []
    for route in routes.values():
        for via in route.vias:
            for pad in smd_pads:
                if (
                    abs(via.x - pad.x) <= pad.width / 2.0 + via_r
                    and abs(via.y - pad.y) <= pad.height / 2.0 + via_r
                ):
                    offenders.append(
                        f"via ({via.x:.3f}, {via.y:.3f}) net {route.net} intersects "
                        f"pad {pad.ref}-{pad.pin} (net {pad.net})"
                    )
    assert not offenders, "via-in-pad copper shipped at default tier:\n" + "\n".join(offenders)
