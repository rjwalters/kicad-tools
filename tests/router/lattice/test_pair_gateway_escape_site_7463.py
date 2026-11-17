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


def _disc(
    cx: float, cy: float, r: float, name: str, exempt: frozenset[int] = frozenset({VBUS})
) -> KeepoutArea:
    ring = tuple(
        (cx + r * dx, cy + r * dy)
        for dx, dy in ((1, 0), (0.7, 0.7), (0, 1), (-0.7, 0.7), (-1, 0), (-0.7, -0.7), (0, -1))
    )
    return KeepoutArea(
        polygon=ring,
        layers=frozenset(range(4)),
        blocks_tracks=True,
        blocks_vias=True,
        exempt=exempt,
        name=name,
    )


def _pf(*areas: KeepoutArea, added: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _keepouts=LatticeKeepoutMask(areas) if areas else None,
        rules=SimpleNamespace(via_diameter=0.6),
        pair_exempt_added=added or {},
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


def test_only_pad_layer_tracks_seal_the_pad() -> None:
    # Board 03 arm C: USB_D's In2.Cu body crosses VBUS U2.5's site, but VBUS
    # leaves on F.Cu between the parallel gateway vias.  The body does not
    # seal it; an F.Cu fan through the site does.
    pf = _pf(_disc(*SITE, 0.5, "pair-escape-via:VBUS:U2.5"))
    body = _result([], [(USB_P, 2, [(37.0, 12.8), (39.0, 12.8)])])
    fan = _result([], [(USB_P, 0, [(37.0, 12.8), (39.0, 12.8)])])
    pad_layers = frozenset({0})
    assert LatticePathfinder._escape_sites_sealed(pf, body, 0.1, pad_layers) == 0
    assert LatticePathfinder._escape_sites_sealed(pf, fan, 0.1, pad_layers) == 1
    # A via's barrel is on every layer: it always counts.
    via = _result([(USB_P, (37.6, 12.84))])
    assert LatticePathfinder._escape_sites_sealed(pf, via, 0.1, pad_layers) == 1


def test_the_pair_exemption_does_not_hide_a_sealing_via() -> None:
    # #6956 adds the pair's nets to the site's exempt set; the count still
    # reads the site as reserved (only VBUS may use it).
    pair = frozenset({USB_P, USB_N})
    name = "pair-escape-via:VBUS:U2.5"
    pf = _pf(_disc(*SITE, 0.5, name, frozenset({VBUS}) | pair), added={name: pair})
    on_site = _result([(USB_P, (37.6, 12.84)), (USB_N, (38.4, 12.84))])
    assert LatticePathfinder._escape_sites_sealed(pf, on_site, 0.1) == 2


def test_a_pair_net_in_its_own_escape_site_is_not_sealing() -> None:
    # A site reserved for USB_D+ itself (another pair sealed its pad), also
    # #6956-exempted for USB_D: USB_D+ copper there uses its own exit; only
    # USB_D- copper there seals it.
    pair = frozenset({USB_P, USB_N})
    name = "pair-escape-via:USB_D+:U9.2"
    # The router records only the net the exemption added (USB_D-).
    pf = _pf(_disc(*SITE, 0.5, name, frozenset({USB_P}) | pair), added={name: frozenset({USB_N})})
    assert LatticePathfinder._escape_sites_sealed(pf, _result([(USB_P, SITE)]), 0.1) == 0
    assert LatticePathfinder._escape_sites_sealed(pf, _result([(USB_N, SITE)]), 0.1) == 1
    own_track = _result([], [(USB_P, 0, [(37.0, 12.8), (39.0, 12.8)])])
    assert LatticePathfinder._escape_sites_sealed(pf, own_track, 0.1) == 0


def test_only_escape_sites_count_not_other_reservations() -> None:
    pf = _pf(_disc(*SITE, 0.5, "plane-via-site:GND:U2.2"))
    assert LatticePathfinder._escape_sites_sealed(pf, _result([(USB_P, SITE)]), 0.1) == 0
    assert LatticePathfinder._escape_sites_sealed(_pf(), _result([(USB_P, SITE)]), 0.1) == 0


def test_a_combo_that_seals_loses_to_a_longer_one_that_does_not() -> None:
    short_sealing = WorkableCombo(0, 7.16, 7.16, sealed=2)
    longer_clear = WorkableCombo(3, 7.21, 7.80)
    assert best_workable([short_sealing, longer_clear]) == longer_clear
    assert best_workable([longer_clear, short_sealing]) == longer_clear
    # Equal sealing: the shorter matched length (P+N plus skew) wins.  Board 03
    # arm C: 7.205/7.800 mm (D- over 16 mm on the whole net) lost nothing to
    # 7.516/7.516 mm on P+N alone.
    skewed = WorkableCombo(3, 7.205, 7.800)
    matched = WorkableCombo(0, 7.516, 7.516)
    assert skewed.score < matched.score
    assert best_workable([skewed, matched]) == matched
    # Then the earlier try.
    assert best_workable([WorkableCombo(1, 9.0, 9.0, 1), WorkableCombo(2, 8.0, 8.0, 1)]).order == 2


def test_core_records_only_the_nets_the_exemption_adds(capsys) -> None:
    from dataclasses import replace

    from tests.router.lattice.test_coupled_pairs import _chain_autorouter, _sealed_vbus_pads

    router = _chain_autorouter(_sealed_vbus_pads())
    # A reservation in the pair's envelope that already exempts USB_D+ (net 1).
    inner = replace(_disc(15.0, 5.0, 0.4, "plane-via-site:GND:U2.2"), exempt=frozenset({1}))
    router._plane_via_keepout_areas = (inner,)
    router.route_net(1)
    capsys.readouterr()
    added = router._lattice_pair_exempt_added
    assert added["plane-via-site:GND:U2.2"] == frozenset({2})
    assert added["pair-escape-via:VBUS:U2.5"] == frozenset({1, 2})
    # The pathfinder sees the same record, so its gateway ranking reads the
    # sites as reserved.
    assert router._lattice_pathfinder.pair_exempt_added == added
    plain = router._lattice_keepouts_without_pair_exemptions(router._lattice_pathfinder._keepouts)
    by_plain = {a.name: a for a in plain.areas}
    assert by_plain["plane-via-site:GND:U2.2"].exempt == frozenset({1})
