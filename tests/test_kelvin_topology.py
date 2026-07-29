"""Tests for the Kelvin / current-sense routing topology model (Issue #4473).

A Kelvin sense net must route as a *star rooted at the shunt pad* (the
Kelvin point): every sense terminal connects directly to the shunt pad so
the sense trace taps AT the Kelvin point and never shares the
high-current segment.  This module covers:

- detection (net-name + shunt-pad gates, conservative fall-through),
- root selection (single / multiple shunt pads, deterministic tie-break),
- star topology shape (no Steiner points, all edges rooted, ordering),
- ``build_rsmt`` integration (Kelvin -> star, ordinary net -> RSMT),
- a synthetic 4-pad Kelvin fixture proving the sense taps land at the
  Kelvin point and no edge joins two non-root (high-current) terminals.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from kicad_tools.router.algorithms.steiner import build_rsmt
from kicad_tools.router.kelvin import (
    KelvinTopology,
    build_kelvin_star_edges,
    build_kelvin_topology,
    detect_kelvin_topology,
    find_kelvin_root,
    is_kelvin_net,
    is_sense_net_name,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad


def _pad(
    x: float,
    y: float,
    ref: str = "",
    net_name: str = "ISENSE_A+",
    footprint_name: str = "",
    net: int = 1,
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        footprint_name=footprint_name,
    )


class TestSenseNameDetection:
    """``is_sense_net_name`` -- current-sense name recognition."""

    def test_isense_matches(self):
        assert is_sense_net_name("ISENSE_A+")
        assert is_sense_net_name("ISENSE_B-")

    def test_underscore_and_abbrev_variants(self):
        assert is_sense_net_name("I_SENSE_P")
        assert is_sense_net_name("VSNS")
        assert is_sense_net_name("CSNS_1")
        assert is_sense_net_name("SHUNT_A")
        assert is_sense_net_name("IMON")
        assert is_sense_net_name("CURRENT_SENSE")

    def test_hierarchical_suffix_matches(self):
        assert is_sense_net_name("/PWR/ISENSE_A")
        assert is_sense_net_name("/A/B/VSNS")

    def test_ordinary_analog_not_matched(self):
        # ADC0 classifies ANALOG but is not a sense tap.
        assert not is_sense_net_name("ADC0")
        assert not is_sense_net_name("VREF")
        assert not is_sense_net_name("GND")
        assert not is_sense_net_name("+3.3V")

    def test_empty_name(self):
        assert not is_sense_net_name("")


class TestFindKelvinRoot:
    """``find_kelvin_root`` -- shunt-pad identification."""

    def test_single_resistor_pad(self):
        pads = [_pad(0, 0, "U3"), _pad(10, 10, "R10"), _pad(20, 0, "Q1")]
        assert find_kelvin_root(pads) == 1

    def test_footprint_fallback_when_ref_missing(self):
        pads = [
            _pad(0, 0, "", footprint_name="Package_SO:SOIC-8"),
            _pad(10, 10, "", footprint_name="Resistor_SMD:R_2512_6332Metric"),
        ]
        assert find_kelvin_root(pads) == 1

    def test_no_resistor_pad_returns_none(self):
        pads = [_pad(0, 0, "U3"), _pad(10, 10, "Q1"), _pad(20, 0, "U2")]
        assert find_kelvin_root(pads) is None

    def test_multiple_resistor_pads_pick_most_central(self):
        # Two shunt-side pads on the net; the central one minimises total
        # Manhattan distance and is chosen deterministically.
        pads = [
            _pad(0, 0, "U3"),  # sense sink
            _pad(0, 100, "R10"),  # far shunt pad
            _pad(5, 5, "R10"),  # central shunt pad
            _pad(10, 0, "Q1"),  # high-current
        ]
        assert find_kelvin_root(pads) == 2

    def test_steiner_pad_never_root(self):
        p = _pad(5, 5, "R10")
        p.steiner_point = True
        pads = [_pad(0, 0, "R11"), p, _pad(20, 0, "U3")]
        # Only the non-Steiner resistor pad qualifies.
        assert find_kelvin_root(pads) == 0


class TestDetectKelvinTopology:
    """``detect_kelvin_topology`` -- full gating."""

    def test_detects_four_pad_kelvin(self):
        pads = [
            _pad(10, 10, "R10"),  # shunt / Kelvin point
            _pad(0, 0, "U3"),  # op-amp sense input
            _pad(20, 0, "Q1"),  # low-side FET source tap (high current)
            _pad(0, 20, "U2"),  # ADC
        ]
        topo = detect_kelvin_topology(pads)
        assert isinstance(topo, KelvinTopology)
        assert topo.root_index == 0

    def test_two_pin_net_not_kelvin(self):
        pads = [_pad(0, 0, "R10"), _pad(10, 0, "U3")]
        assert detect_kelvin_topology(pads) is None

    def test_sense_name_without_shunt_falls_through(self):
        pads = [_pad(0, 0, "U3"), _pad(10, 0, "U2"), _pad(20, 0, "Q1")]
        assert detect_kelvin_topology(pads) is None

    def test_shunt_without_sense_name_falls_through(self):
        pads = [
            _pad(10, 10, "R10", net_name="NET_FOO"),
            _pad(0, 0, "U3", net_name="NET_FOO"),
            _pad(20, 0, "Q1", net_name="NET_FOO"),
        ]
        assert detect_kelvin_topology(pads) is None

    def test_net_name_read_from_first_real_pad(self):
        steiner = _pad(5, 5, "", net_name="")
        steiner.steiner_point = True
        pads = [steiner, _pad(10, 10, "R10"), _pad(0, 0, "U3"), _pad(20, 0, "Q1")]
        topo = detect_kelvin_topology(pads)
        assert topo is not None


class TestStarEdges:
    """``build_kelvin_star_edges`` -- star shape + ordering."""

    def test_all_edges_rooted(self):
        pads = [_pad(10, 10, "R10"), _pad(0, 0, "U3"), _pad(20, 0, "Q1"), _pad(0, 20, "U2")]
        edges = build_kelvin_star_edges(pads, 0)
        assert len(edges) == 3
        assert all(e[0] == 0 for e in edges)
        # Every non-root terminal appears exactly once.
        assert sorted(e[1] for e in edges) == [1, 2, 3]

    def test_shortest_first_ordering(self):
        # Root at origin; terminals at distances 5, 10, 20.
        pads = [_pad(0, 0, "R10"), _pad(20, 0, "U3"), _pad(5, 0, "U2"), _pad(10, 0, "Q1")]
        edges = build_kelvin_star_edges(pads, 0)
        dists = [abs(pads[e[1]].x) for e in edges]
        assert dists == sorted(dists)
        assert dists == [5, 10, 20]

    def test_no_edge_between_two_non_root_terminals(self):
        # The Kelvin guarantee: no edge joins two non-root (high-current /
        # sense) terminals, so no sense tap shares the high-current segment.
        pads = [_pad(10, 10, "R10"), _pad(0, 0, "U3"), _pad(20, 0, "Q1"), _pad(0, 20, "U2")]
        edges = build_kelvin_star_edges(pads, 0)
        for a, b in edges:
            assert 0 in (a, b)


class TestBuildRsmtIntegration:
    """``build_rsmt`` routes Kelvin nets as stars, others as RSMT."""

    def test_kelvin_net_routes_as_star_no_steiner_points(self):
        pads = [_pad(10, 10, "R10"), _pad(0, 0, "U3"), _pad(20, 0, "Q1"), _pad(0, 20, "U2")]
        ext, edges = build_rsmt(pads)
        # A star adds no virtual Steiner pads.
        assert len(ext) == len(pads)
        assert not any(p.steiner_point for p in ext)
        # All edges emanate from the shunt pad (index 0).
        assert all(0 in e for e in edges)

    def test_allow_kelvin_false_forces_rsmt(self):
        pads = [_pad(0, 0, "R10"), _pad(10, 0, "U3"), _pad(0, 10, "Q1"), _pad(10, 10, "U2")]
        ext_star, edges_star = build_rsmt(pads, allow_kelvin=True)
        ext_rsmt, edges_rsmt = build_rsmt(pads, allow_kelvin=False)
        # The RSMT path may introduce Steiner points; the star never does.
        assert not any(p.steiner_point for p in ext_star)
        # The two paths need not agree on edge count, but the star is a
        # pure 3-edge star for 4 terminals.
        assert len(edges_star) == 3
        assert all(0 in e for e in edges_star)

    def test_ordinary_net_unchanged_by_kelvin_gate(self):
        # A non-sense multi-terminal net still gets the generic RSMT and
        # may synthesise Steiner points -- the Kelvin gate is a no-op.
        pads = [
            _pad(0, 0, "U1", net_name="DATA0"),
            _pad(10, 0, "U2", net_name="DATA0"),
            _pad(0, 10, "U3", net_name="DATA0"),
            _pad(10, 10, "U4", net_name="DATA0"),
        ]
        ext_default, edges_default = build_rsmt(pads)
        ext_forced, edges_forced = build_rsmt(pads, allow_kelvin=False)
        assert len(ext_default) == len(ext_forced)
        assert edges_default == edges_forced

    def test_is_kelvin_net_predicate(self):
        kelvin = [_pad(10, 10, "R10"), _pad(0, 0, "U3"), _pad(20, 0, "Q1")]
        ordinary = [
            _pad(0, 0, "U1", net_name="DATA0"),
            _pad(10, 0, "U2", net_name="DATA0"),
            _pad(0, 10, "U3", net_name="DATA0"),
        ]
        assert is_kelvin_net(kelvin)
        assert not is_kelvin_net(ordinary)


class TestSyntheticKelvinFixture:
    """End-to-end synthetic Kelvin fixture (acceptance criterion #3).

    Models a real 4-wire current-sense structure: a shunt resistor whose
    two pads carry the high current (shunt+ / shunt-), an op-amp sense
    input, and an ADC.  The sense net (shunt+ side) must connect the sense
    terminals AT the shunt+ pad, never routing them through the FET-source
    high-current terminal.
    """

    def _fixture_pads(self) -> list[Pad]:
        # ISENSE_A+ : shunt R10 pin-1 (Kelvin point), op-amp U3 SP input,
        # low-side FET Q1 source tap (carries load current), ADC U2 PA0.
        return [
            _pad(50.0, 20.0, "U3"),  # op-amp sense input (hi-Z)
            _pad(30.0, 10.0, "R10"),  # shunt pad == Kelvin point
            _pad(30.0, 40.0, "Q1"),  # FET source tap (high current)
            _pad(60.0, 45.0, "U2"),  # ADC input (hi-Z)
        ]

    def test_fixture_routes_with_kelvin_topology(self):
        pads = self._fixture_pads()
        ext, edges = build_rsmt(pads)

        # No synthetic Steiner branch points -- a branch point could merge
        # the sense tap into the high-current segment.
        assert not any(p.steiner_point for p in ext)

        # Every edge is incident to the shunt pad (the Kelvin point).
        root = next(i for i, p in enumerate(ext) if p.ref == "R10")
        assert all(root in e for e in edges)

        # The op-amp and ADC (sense sinks) connect DIRECTLY to the shunt,
        # not via the FET-source high-current terminal.
        fet = next(i for i, p in enumerate(ext) if p.ref == "Q1")
        opamp = next(i for i, p in enumerate(ext) if p.ref == "U3")
        adc = next(i for i, p in enumerate(ext) if p.ref == "U2")
        neighbours = {a if b == opamp else b for a, b in edges if opamp in (a, b)}
        assert neighbours == {root}
        neighbours_adc = {a if b == adc else b for a, b in edges if adc in (a, b)}
        assert neighbours_adc == {root}
        # No direct sense-to-FET edge exists.
        assert (opamp, fet) not in edges and (fet, opamp) not in edges
        assert (adc, fet) not in edges and (fet, adc) not in edges

    def test_topology_via_build_kelvin_topology_matches(self):
        pads = self._fixture_pads()
        via_helper = build_kelvin_topology(pads)
        assert via_helper is not None
        ext, edges = via_helper
        assert len(ext) == len(pads)
        root = next(i for i, p in enumerate(ext) if p.ref == "R10")
        assert all(root in e for e in edges)


# ---------------------------------------------------------------------------
# Board-05 acceptance (criterion #2): the real ISENSE nets are recognised
# and carry the Kelvin topology rooted at their shunt resistor.
# ---------------------------------------------------------------------------

_BOARD05_PCB = (
    Path(__file__).resolve().parents[1]
    / "boards"
    / "05-bldc-motor-controller"
    / "output"
    / "bldc_controller.kicad_pcb"
)


@pytest.mark.skipif(not _BOARD05_PCB.exists(), reason="board-05 PCB not generated")
class TestBoard05ISenseNets:
    """board-05's ISENSE nets carry the Kelvin topology (shunt-rooted)."""

    def _sense_pads_by_net(self):
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(_BOARD05_PCB))
        num_to_name = {num: n.name for num, n in pcb.nets.items()}
        pads_by_net: dict[str, list[Pad]] = defaultdict(list)
        for fp in pcb.footprints:
            for pad in fp.pads:
                name = pad.net_name or num_to_name.get(pad.net_number, "")
                if not name or not is_sense_net_name(name):
                    continue
                try:
                    pos = pcb.get_pad_position(fp.reference, pad.number)
                except Exception:
                    pos = (0.0, 0.0)
                if not pos or len(pos) < 2:
                    pos = (0.0, 0.0)
                pads_by_net[name].append(
                    _pad(float(pos[0]), float(pos[1]), fp.reference, net_name=name)
                )
        return pads_by_net

    def test_isense_nets_present(self):
        pads_by_net = self._sense_pads_by_net()
        # The board exposes the ISENSE_{A,B,C}{+,-} sense-net family.
        isense = {n for n in pads_by_net if n.startswith("ISENSE_")}
        assert len(isense) >= 5, f"expected >=5 ISENSE nets, got {sorted(isense)}"

    def test_multi_pad_isense_nets_route_kelvin(self):
        pads_by_net = self._sense_pads_by_net()
        multi = {
            n: pads for n, pads in pads_by_net.items() if n.startswith("ISENSE_") and len(pads) >= 3
        }
        assert multi, "expected at least one multi-pad ISENSE net"
        for name, pads in multi.items():
            topo = detect_kelvin_topology(pads)
            assert topo is not None, f"{name} not recognised as Kelvin"
            # The Kelvin point is a shunt resistor pad.
            root_ref = pads[topo.root_index].ref
            assert root_ref.upper().startswith("R"), (
                f"{name} Kelvin root {root_ref} is not a shunt resistor"
            )
            # Star topology: no synthesised Steiner points, every edge
            # rooted at the shunt pad (sense taps land at the Kelvin point).
            ext, edges = build_rsmt(pads)
            assert not any(p.steiner_point for p in ext)
            assert all(topo.root_index in e for e in edges)
