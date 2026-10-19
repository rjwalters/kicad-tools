"""A gateway must not close in a neighbour's #6697 escape site (issue #7463).

#6956 exempts a declared pair from the escape sites reserved in its gateway
envelope.  Board 03 arm C: USB_D's In2.Cu gateway vias at U2 landed on the via
site reserved for VBUS U2.5 (between the pair's own pads), VBUS lost its only
exit and was left unconnected (``decline[no-path-keepout-constrained]``).
The workable-combination ranking now puts a combination that leaves every
site open ahead of one that does not, however short.
"""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.router.lattice.obstacles import KeepoutArea, LatticeKeepoutMask
from kicad_tools.router.lattice.pathfinder import LatticePathfinder, _PairResult
from kicad_tools.router.pairplan.gateway import WorkableCombo, best_workable

SITE = (38.0, 12.8)
VBUS, USB_P, USB_N = 24, 20, 21


def _disc(cx: float, cy: float, r: float, name: str) -> KeepoutArea:
    ring = tuple(
        (cx + r * dx, cy + r * dy)
        for dx, dy in ((1, 0), (0.7, 0.7), (0, 1), (-0.7, 0.7), (-1, 0), (-0.7, -0.7), (0, -1))
    )
    return KeepoutArea(
        polygon=ring,
        layers=frozenset(range(4)),
        blocks_tracks=True,
        blocks_vias=True,
        exempt=frozenset({VBUS}),
        name=name,
    )


def _pf(*areas: KeepoutArea) -> SimpleNamespace:
    return SimpleNamespace(
        _keepouts=LatticeKeepoutMask(areas) if areas else None,
        rules=SimpleNamespace(via_diameter=0.6),
    )


def _result(vias: list, runs: list | None = None) -> _PairResult:
    return _PairResult(routes=[], runs=runs or [], resources=set(), vias=vias)


def test_a_via_on_the_escape_site_counts_as_sealing() -> None:
    pf = _pf(_disc(*SITE, 0.5, "pair-escape-via:VBUS:U2.5"))
    on_site = _result([(USB_P, (37.6, 12.84)), (USB_N, (38.4, 12.84))])
    clear = _result([(USB_P, (37.05, 13.6)), (USB_N, (38.95, 13.6))])
    assert LatticePathfinder._escape_sites_sealed(pf, on_site, 0.1) == 2
    assert LatticePathfinder._escape_sites_sealed(pf, clear, 0.1) == 0


def test_a_track_through_the_escape_site_counts_as_sealing() -> None:
    pf = _pf(_disc(*SITE, 0.5, "pair-escape-via:VBUS:U2.5"))
    through = _result([], [(USB_P, 0, [(37.0, 12.8), (39.0, 12.8)])])
    beside = _result([], [(USB_P, 0, [(37.0, 14.0), (39.0, 14.0)])])
    assert LatticePathfinder._escape_sites_sealed(pf, through, 0.1) == 1
    assert LatticePathfinder._escape_sites_sealed(pf, beside, 0.1) == 0


def test_only_escape_sites_count_not_other_reservations() -> None:
    pf = _pf(_disc(*SITE, 0.5, "plane-via-site:GND:U2.2"))
    assert LatticePathfinder._escape_sites_sealed(pf, _result([(USB_P, SITE)]), 0.1) == 0
    assert LatticePathfinder._escape_sites_sealed(_pf(), _result([(USB_P, SITE)]), 0.1) == 0


def test_a_combo_that_seals_loses_to_a_longer_one_that_does_not() -> None:
    short_sealing = WorkableCombo(0, 7.16, 7.16, sealed=2)
    longer_clear = WorkableCombo(3, 7.21, 7.80)
    assert best_workable([short_sealing, longer_clear]) == longer_clear
    assert best_workable([longer_clear, short_sealing]) == longer_clear
    # Equal sealing: still the shorter, then the earlier try.
    assert best_workable([WorkableCombo(1, 9.0, 9.0, 1), WorkableCombo(2, 8.0, 8.0, 1)]).order == 2
