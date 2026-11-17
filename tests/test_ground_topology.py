"""Tests for analog-ground bridge topology analysis (Phase 2b, issue #3178).

These tests exercise the topology-aware path of
:func:`kicad_tools.analysis.analog_detect.check_analog_ground_bridge` and
the new :mod:`kicad_tools.analysis.ground_topology` module.

The committed boards have no analog grounds, so all fixtures are built
from a small ``MockPCB`` that mirrors the surface
:class:`kicad_tools.validate.connectivity.ConnectivityValidator` consults:

* ``pcb.nets`` — ``{net_number: MockNet(number, name)}``
* ``pcb.footprints`` — list of ``MockFootprint`` with ``pads``,
  ``position``, ``rotation``, ``reference``, ``value``, ``name``
* ``pcb.segments`` / ``pcb.segments_in_net`` — list of ``MockSegment``
* ``pcb.vias`` / ``pcb.vias_in_net`` — list of ``MockVia``
* ``pcb.zones`` — list of ``MockZone``

The connectivity validator instantiates from a PCB object directly, so
duck typing is enough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kicad_tools.analysis.analog_detect import check_analog_ground_bridge
from kicad_tools.analysis.ground_topology import (
    BridgeInfo,
    GroundTopologyResult,
    analyze_ground_topology,
    pcb_has_copper_topology,
)

# ---------------------------------------------------------------------------
# Mock surface
# ---------------------------------------------------------------------------


@dataclass
class MockNet:
    number: int
    name: str


@dataclass
class MockPad:
    number: str = "1"
    type: str = "smd"
    shape: str = "rect"
    position: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] = (1.0, 1.0)
    layers: list[str] = field(default_factory=lambda: ["F.Cu"])
    net_number: int = 0
    net_name: str = ""
    drill: float = 0.0


@dataclass
class MockFootprint:
    name: str = ""
    layer: str = "F.Cu"
    position: tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    reference: str = ""
    value: str = ""
    pads: list[MockPad] = field(default_factory=list)
    texts: list[Any] = field(default_factory=list)
    graphics: list[Any] = field(default_factory=list)
    uuid: str = ""
    description: str = ""
    tags: str = ""
    attr: str = "smd"


@dataclass
class MockSegment:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.15
    layer: str = "F.Cu"
    net_number: int = 0
    net_name: str = ""


@dataclass
class MockVia:
    position: tuple[float, float]
    size: float = 0.6
    drill: float = 0.3
    layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])
    net_number: int = 0
    net_name: str = ""


@dataclass
class MockZone:
    net_number: int = 0
    net_name: str = ""
    layer: str = "F.Cu"
    polygon: list[tuple[float, float]] = field(default_factory=list)
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)


@dataclass
class MockPCB:
    """Duck-typed PCB mock.

    Designed so :class:`ConnectivityValidator` (called by the topology
    analyzer) accepts this object instead of a real ``schema.pcb.PCB``.
    """

    footprints: list[MockFootprint] = field(default_factory=list)
    nets: dict[int, MockNet] = field(default_factory=dict)
    _segments: list[MockSegment] = field(default_factory=list)
    _vias: list[MockVia] = field(default_factory=list)
    _zones: list[MockZone] = field(default_factory=list)

    @property
    def segments(self) -> list[MockSegment]:
        return self._segments

    @property
    def vias(self) -> list[MockVia]:
        return self._vias

    @property
    def zones(self) -> list[MockZone]:
        return self._zones

    def segments_in_net(self, net_number: int):
        for s in self._segments:
            if s.net_number == net_number:
                yield s

    def vias_in_net(self, net_number: int):
        for v in self._vias:
            if v.net_number == net_number:
                yield v


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _nets(*pairs: tuple[int, str]) -> dict[int, MockNet]:
    """Build a net map from (number, name) pairs.  Net 0 added automatically."""
    nets: dict[int, MockNet] = {0: MockNet(0, "")}
    for number, name in pairs:
        nets[number] = MockNet(number, name)
    return nets


def _make_pad(
    number: str,
    net_number: int,
    net_name: str,
    *,
    position: tuple[float, float] = (0.0, 0.0),
) -> MockPad:
    return MockPad(
        number=number,
        net_number=net_number,
        net_name=net_name,
        position=position,
        layers=["F.Cu"],
    )


def _make_bridge_fp(
    *,
    ref: str,
    name: str,
    value: str,
    pads_spec: list[tuple[str, int, str, tuple[float, float]]],
    position: tuple[float, float] = (0.0, 0.0),
) -> MockFootprint:
    """Build a footprint whose pads are (number, net_number, net_name, pos)."""
    return MockFootprint(
        name=name,
        value=value,
        reference=ref,
        position=position,
        pads=[
            _make_pad(num, net_no, net_nm, position=pos) for num, net_no, net_nm, pos in pads_spec
        ],
    )


def _make_load_fp(
    *,
    ref: str,
    net_number: int,
    net_name: str,
    position: tuple[float, float],
) -> MockFootprint:
    """Build a single-pad consumer footprint sitting on a ground net.

    Used to give a ground net more than one pad so a bridge actually has
    something to be "wired to" in the connectivity graph.
    """
    return MockFootprint(
        name="R_0402",
        value="LOAD",
        reference=ref,
        position=position,
        pads=[_make_pad("1", net_number, net_name, position=(0.0, 0.0))],
    )


def _segment(
    start: tuple[float, float],
    end: tuple[float, float],
    net_number: int,
    net_name: str,
) -> MockSegment:
    return MockSegment(
        start=start,
        end=end,
        width=0.15,
        layer="F.Cu",
        net_number=net_number,
        net_name=net_name,
    )


# ---------------------------------------------------------------------------
# Tests: pcb_has_copper_topology
# ---------------------------------------------------------------------------


class TestPCBHasCopperTopology:
    def test_empty_pcb_returns_false(self) -> None:
        assert pcb_has_copper_topology(MockPCB()) is False

    def test_segments_only(self) -> None:
        pcb = MockPCB(_segments=[_segment((0, 0), (1, 1), 1, "GND")])
        assert pcb_has_copper_topology(pcb) is True

    def test_vias_only(self) -> None:
        pcb = MockPCB(_vias=[MockVia(position=(0, 0), net_number=1, net_name="GND")])
        assert pcb_has_copper_topology(pcb) is True

    def test_filled_zone(self) -> None:
        pcb = MockPCB(
            _zones=[
                MockZone(
                    net_number=1,
                    net_name="GND",
                    filled_polygons=[[(0, 0), (1, 0), (1, 1), (0, 1)]],
                )
            ]
        )
        assert pcb_has_copper_topology(pcb) is True

    def test_boundary_only_zone(self) -> None:
        # A zone with just a boundary polygon (no fill computed yet) still
        # supplies geometry the topology analyzer can use.
        pcb = MockPCB(
            _zones=[MockZone(net_number=1, net_name="GND", polygon=[(0, 0), (1, 0), (1, 1)])]
        )
        assert pcb_has_copper_topology(pcb) is True

    def test_zones_with_no_geometry_skip(self) -> None:
        """An empty zone list is treated as "no copper".

        This guards the branch where a zone has neither ``filled_polygons``
        nor ``polygon`` set.  The function should continue past such a
        zone without claiming topology exists.
        """
        pcb = MockPCB(
            _zones=[MockZone(net_number=1, net_name="GND")]  # both polygon lists empty
        )
        assert pcb_has_copper_topology(pcb) is False

    def test_handles_raising_pcb_gracefully(self) -> None:
        class BadPCB:
            @property
            def segments(self) -> Any:
                raise RuntimeError("boom")

        assert pcb_has_copper_topology(BadPCB()) is False


# ---------------------------------------------------------------------------
# Tests: analyze_ground_topology — basic guard cases
# ---------------------------------------------------------------------------


class TestAnalyzeGroundTopologyGuards:
    def test_no_analog_ground_returns_empty(self) -> None:
        pcb = MockPCB(nets=_nets((1, "GND")))
        assert analyze_ground_topology(pcb) == []

    def test_no_digital_ground_returns_empty(self) -> None:
        pcb = MockPCB(nets=_nets((1, "GNDA")))
        assert analyze_ground_topology(pcb) == []

    def test_no_copper_topology_marks_fallback(self) -> None:
        pcb = MockPCB(nets=_nets((1, "GNDA"), (2, "GND")))
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        assert results[0].used_fallback is True
        assert results[0].analog_ground_name == "GNDA"
        assert results[0].digital_ground_name == "GND"

    def test_never_raises_on_garbage_pcb(self) -> None:
        class BadPCB:
            nets = None
            footprints = None

        # Should not raise; returns empty (no analog ground found).
        assert analyze_ground_topology(BadPCB()) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: single bridge — ferrite / 0Ω / NetTie
# ---------------------------------------------------------------------------


def _single_bridge_pcb(
    *,
    bridge_ref: str,
    bridge_name: str,
    bridge_value: str,
    bridge_pos: tuple[float, float] = (10.0, 0.0),
) -> MockPCB:
    """A board with GNDA, GND, two anchor pads on each, and one bridge.

    Layout (X coordinates only matter; segments run horizontally):

        GNDA loads  : R10 @ (0,0)  R11 @ (5,0)
        bridge      : <bridge_ref> @ (10,0) pad1→GNDA, pad2→GND
        GND  loads  : R20 @ (15,0)  R21 @ (20,0)
        copper      : segment (0,0)-(10,0) on GNDA; segment (10,0)-(20,0) on GND
    """
    nets = _nets((1, "GNDA"), (2, "GND"))
    bx, by = bridge_pos
    fps = [
        _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
        _make_load_fp(ref="R11", net_number=1, net_name="GNDA", position=(5.0, 0.0)),
        _make_bridge_fp(
            ref=bridge_ref,
            name=bridge_name,
            value=bridge_value,
            pads_spec=[
                ("1", 1, "GNDA", (-0.5, 0.0)),
                ("2", 2, "GND", (0.5, 0.0)),
            ],
            position=(bx, by),
        ),
        _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
        _make_load_fp(ref="R21", net_number=2, net_name="GND", position=(20.0, 0.0)),
    ]
    # GNDA copper: links R10 (0,0), R11 (5,0), bridge pad1 (bx-0.5, by).
    # GND  copper: links bridge pad2 (bx+0.5, by), R20 (15,0), R21 (20,0).
    segments = [
        _segment((0.0, 0.0), (5.0, 0.0), 1, "GNDA"),
        _segment((5.0, 0.0), (bx - 0.5, by), 1, "GNDA"),
        _segment((bx + 0.5, by), (15.0, 0.0), 2, "GND"),
        _segment((15.0, 0.0), (20.0, 0.0), 2, "GND"),
    ]
    return MockPCB(nets=nets, footprints=fps, _segments=segments)


class TestSingleBridge:
    def test_ferrite_bridge_no_advisory(self) -> None:
        pcb = _single_bridge_pcb(
            bridge_ref="FB1",
            bridge_name="L_0805",
            bridge_value="600R@100MHz",
        )
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 1
        assert r.advisory is None
        assert len(r.bridges) == 1
        assert r.bridges[0].kind == "ferrite"
        assert r.bridges[0].is_wired is True

    def test_zero_ohm_bridge_no_advisory(self) -> None:
        pcb = _single_bridge_pcb(
            bridge_ref="R5",
            bridge_name="R_0402",
            bridge_value="0",
        )
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 1
        assert r.bridges[0].kind == "zero_ohm"
        assert r.advisory is None

    def test_zero_ohm_value_variants(self) -> None:
        for value in ("0", "0R", "0E", "0.0", "R0", "0 ohm", "0ohm"):
            pcb = _single_bridge_pcb(
                bridge_ref="R7",
                bridge_name="R_0402",
                bridge_value=value,
            )
            results = analyze_ground_topology(pcb)
            assert results[0].bridge_count == 1, f"value={value!r}"
            assert results[0].advisory is None, f"value={value!r}"

    def test_nettie_bridge_no_advisory(self) -> None:
        pcb = _single_bridge_pcb(
            bridge_ref="NT1",
            bridge_name="NetTie-2_SMD",
            bridge_value="NetTie",
        )
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        assert results[0].bridges[0].kind == "nettie"
        assert results[0].advisory is None

    def test_non_resistor_zero_value_is_not_a_bridge(self) -> None:
        # A capacitor whose value happens to be "0" must NOT be classified
        # as a 0Ω bridge — its reference designator is C* not R*.
        pcb = _single_bridge_pcb(
            bridge_ref="C99",
            bridge_name="C_0402",
            bridge_value="0",
        )
        results = analyze_ground_topology(pcb)
        # No recognised bridge → "no bridge" advisory.
        assert results[0].bridge_count == 0
        assert results[0].advisory is not None
        assert "no bridge" in results[0].advisory


# ---------------------------------------------------------------------------
# Tests: no bridge
# ---------------------------------------------------------------------------


class TestNoBridge:
    def test_no_bridge_component_emits_advisory(self) -> None:
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_load_fp(ref="R11", net_number=1, net_name="GNDA", position=(5.0, 0.0)),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
            _make_load_fp(ref="R21", net_number=2, net_name="GND", position=(20.0, 0.0)),
        ]
        segments = [
            _segment((0.0, 0.0), (5.0, 0.0), 1, "GNDA"),
            _segment((15.0, 0.0), (20.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 0
        assert r.advisory is not None
        assert "no bridge" in r.advisory
        assert "GNDA" in r.advisory
        assert "GND" in r.advisory


# ---------------------------------------------------------------------------
# Tests: floating bridge
# ---------------------------------------------------------------------------


class TestFloatingBridge:
    def test_unwired_bridge_pad_emits_floating_advisory(self) -> None:
        """Bridge pads both sit on the right nets but no copper attaches them.

        With segments routed only between R10 and R11 (and R20–R21), the
        ferrite at (10,0) has no electrical path to either ground.
        """
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_load_fp(ref="R11", net_number=1, net_name="GNDA", position=(5.0, 0.0)),
            _make_bridge_fp(
                ref="FB1",
                name="L_0805",
                value="600R@100MHz",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.5, 0.0)),
                ],
                position=(50.0, 50.0),  # nowhere near the routed copper
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
            _make_load_fp(ref="R21", net_number=2, net_name="GND", position=(20.0, 0.0)),
        ]
        # Copper exists but never reaches the ferrite at (50, 50).
        segments = [
            _segment((0.0, 0.0), (5.0, 0.0), 1, "GNDA"),
            _segment((15.0, 0.0), (20.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 0
        # Should report "bridge present but not wired", NOT "no bridge".
        assert r.advisory is not None
        assert "not wired" in r.advisory
        assert "FB1" in r.advisory
        assert len(r.floating_bridges) == 1


# ---------------------------------------------------------------------------
# Tests: ground loop (≥2 wired bridges)
# ---------------------------------------------------------------------------


def _two_bridge_pcb(*, both_wired: bool) -> MockPCB:
    """GNDA and GND each have anchor pads + two bridges.

    Layout:
        R10 (0,0)              -- GNDA load
        FB1 @ (10, 0)          -- ferrite bridge, pads at 9.5/10.5
        R12 (15, 0)            -- GNDA load
        NT1 @ (25, 0)          -- nettie bridge, pads at 24.5/25.5
        R20 (30, 0)            -- GND load

    Copper:
        GNDA: 0→9.5, 10.5? no wait — we want FB1's GNDA pad wired and
              NT1's GNDA pad also wired.
              So GNDA copper: (0,0)-(9.5,0)  and  (15,0)-(24.5,0)
              We also need (9.5,0) connected to (15,0) so both bridges
              reach the same GNDA mass.  Add (9.5,0)-(15,0).
        GND : (10.5,0)-(25.5,0)? No, FB1 pad2 (10.5) and NT1 pad2 (25.5)
              are on GND; R20 at (30,0) is GND.
              Segments: (10.5,0)-(25.5,0) and (25.5,0)-(30,0).
    """
    nets = _nets((1, "GNDA"), (2, "GND"))
    fps = [
        _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
        _make_bridge_fp(
            ref="FB1",
            name="L_0805",
            value="600R@100MHz",
            pads_spec=[
                ("1", 1, "GNDA", (-0.5, 0.0)),
                ("2", 2, "GND", (0.5, 0.0)),
            ],
            position=(10.0, 0.0),
        ),
        _make_load_fp(ref="R12", net_number=1, net_name="GNDA", position=(15.0, 0.0)),
        _make_bridge_fp(
            ref="NT1",
            name="NetTie-2_SMD",
            value="NetTie",
            pads_spec=[
                ("1", 1, "GNDA", (-0.5, 0.0)),
                ("2", 2, "GND", (0.5, 0.0)),
            ],
            position=(25.0, 0.0),
        ),
        _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(30.0, 0.0)),
    ]
    if both_wired:
        segments = [
            _segment((0.0, 0.0), (9.5, 0.0), 1, "GNDA"),
            _segment((9.5, 0.0), (15.0, 0.0), 1, "GNDA"),
            _segment((15.0, 0.0), (24.5, 0.0), 1, "GNDA"),
            _segment((10.5, 0.0), (25.5, 0.0), 2, "GND"),
            _segment((25.5, 0.0), (30.0, 0.0), 2, "GND"),
        ]
    else:
        # Only FB1 wired; NT1 floats (its pads have no copper touching).
        segments = [
            _segment((0.0, 0.0), (9.5, 0.0), 1, "GNDA"),
            _segment((9.5, 0.0), (15.0, 0.0), 1, "GNDA"),
            _segment((10.5, 0.0), (30.0, 0.0), 2, "GND"),
        ]
    return MockPCB(nets=nets, footprints=fps, _segments=segments)


class TestGroundLoop:
    def test_two_wired_bridges_emit_ground_loop(self) -> None:
        pcb = _two_bridge_pcb(both_wired=True)
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 2
        assert r.advisory is not None
        assert "ground loop" in r.advisory
        assert "FB1" in r.advisory
        assert "NT1" in r.advisory
        assert "single-point bond required" in r.advisory

    def test_one_wired_one_floating_does_not_trigger_loop(self) -> None:
        pcb = _two_bridge_pcb(both_wired=False)
        results = analyze_ground_topology(pcb)
        r = results[0]
        assert r.bridge_count == 1  # only FB1 is wired
        # Should still mention the floating NT1.
        assert r.advisory is not None
        assert "NT1" in r.advisory
        assert "not wired" in r.advisory


# ---------------------------------------------------------------------------
# Tests: N-pad NetTie support
# ---------------------------------------------------------------------------


class TestNPadNetTie:
    def test_three_pad_nettie_recognised(self) -> None:
        """A NetTie_3 with one GNDA pad and two GND pads is still a bridge.

        Phase 2 hard-rejected any component with ``len(pads) != 2``; Phase 2b
        relaxes the rule to "at least one pad on each ground, no third
        unrelated net".
        """
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_bridge_fp(
                ref="NT3",
                name="NetTie_3",
                value="NetTie",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.0, 0.0)),
                    ("3", 2, "GND", (0.5, 0.0)),
                ],
                position=(10.0, 0.0),
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
        ]
        segments = [
            _segment((0.0, 0.0), (9.5, 0.0), 1, "GNDA"),
            _segment((10.5, 0.0), (15.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 1
        assert r.bridges[0].kind == "nettie"
        assert r.advisory is None

    def test_three_pad_component_with_third_unrelated_net_rejected(self) -> None:
        """A 3-pad component touching VCC should NOT be classified as a bridge."""
        nets = _nets((1, "GNDA"), (2, "GND"), (3, "VCC"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_bridge_fp(
                ref="U1",
                name="SOT-23",
                value="NetTie",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.0, 0.0)),
                    ("3", 3, "VCC", (0.5, 0.0)),
                ],
                position=(10.0, 0.0),
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
        ]
        segments = [
            _segment((0.0, 0.0), (9.5, 0.0), 1, "GNDA"),
            _segment((10.0, 0.0), (15.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        # The bridge has an unrelated net → rejected → no bridge.
        assert results[0].bridge_count == 0
        assert results[0].advisory is not None
        assert "no bridge" in results[0].advisory


# ---------------------------------------------------------------------------
# Tests: top-level check_analog_ground_bridge — Phase 2b selection
# ---------------------------------------------------------------------------


class TestCheckAnalogGroundBridgeWiring:
    """Confirm ``check_analog_ground_bridge`` consults the topology engine."""

    def test_routes_through_topology_when_copper_present(self) -> None:
        pcb = _two_bridge_pcb(both_wired=True)
        warnings = check_analog_ground_bridge(pcb)
        assert len(warnings) == 1
        assert "ground loop" in warnings[0]

    def test_falls_back_to_phase2_when_no_copper(self) -> None:
        """A board with no routed copper / zones uses the local 2-pad scan.

        With a ferrite present, the local scan reports "bridged" (no
        advisory) because Phase 2 cannot verify wiring.
        """
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_bridge_fp(
                ref="FB1",
                name="L_0805",
                value="600R@100MHz",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.5, 0.0)),
                ],
                position=(10.0, 0.0),
            ),
        ]
        pcb = MockPCB(nets=nets, footprints=fps)  # no segments / zones
        warnings = check_analog_ground_bridge(pcb)
        # Local scan finds the ferrite → no advisory.
        assert warnings == []

    def test_no_bridge_fallback(self) -> None:
        nets = _nets((1, "GNDA"), (2, "GND"))
        pcb = MockPCB(nets=nets)
        warnings = check_analog_ground_bridge(pcb)
        # Local scan: no bridge component → advisory.
        assert len(warnings) == 1
        assert "no bridge" in warnings[0]

    def test_returns_empty_when_grounds_missing(self) -> None:
        # Only a digital ground, no analog ground.
        pcb = MockPCB(nets=_nets((1, "GND")))
        assert check_analog_ground_bridge(pcb) == []


# ---------------------------------------------------------------------------
# Tests: BridgeInfo / GroundTopologyResult plumbing
# ---------------------------------------------------------------------------


class TestResultTypes:
    def test_bridge_info_to_dict(self) -> None:
        info = BridgeInfo(
            reference="FB1",
            kind="ferrite",
            analog_pads=("FB1.1",),
            digital_pads=("FB1.2",),
            analog_pad_wired=True,
            digital_pad_wired=True,
        )
        d = info.to_dict()
        assert d["reference"] == "FB1"
        assert d["kind"] == "ferrite"
        assert d["analog_pads"] == ["FB1.1"]
        assert d["digital_pads"] == ["FB1.2"]
        assert d["analog_pad_wired"] is True
        assert d["digital_pad_wired"] is True
        assert d["is_wired"] is True
        assert info.is_wired is True

    def test_bridge_info_is_wired_requires_both_sides(self) -> None:
        only_analog = BridgeInfo(
            reference="FB1",
            kind="ferrite",
            analog_pads=("FB1.1",),
            digital_pads=("FB1.2",),
            analog_pad_wired=True,
            digital_pad_wired=False,
        )
        assert only_analog.is_wired is False

    def test_topology_result_partitions_floating_vs_wired(self) -> None:
        wired = BridgeInfo("A", "ferrite", ("A.1",), ("A.2",), True, True)
        floating = BridgeInfo("B", "ferrite", ("B.1",), ("B.2",), False, False)
        result = GroundTopologyResult(
            analog_ground_name="GNDA",
            digital_ground_name="GND",
            bridge_count=1,
            bridges=[wired, floating],
        )
        assert result.wired_bridges == [wired]
        assert result.floating_bridges == [floating]

    def test_topology_result_to_dict(self) -> None:
        result = GroundTopologyResult(
            analog_ground_name="GNDA",
            digital_ground_name="GND",
            bridge_count=2,
            bridges=[
                BridgeInfo("A", "ferrite", ("A.1",), ("A.2",), True, True),
                BridgeInfo("B", "nettie", ("B.1",), ("B.2",), True, True),
            ],
            advisory="ground loop ...",
        )
        d = result.to_dict()
        assert d["analog_ground_name"] == "GNDA"
        assert d["digital_ground_name"] == "GND"
        assert d["bridge_count"] == 2
        assert d["advisory"] == "ground loop ..."
        assert len(d["bridges"]) == 2
        assert d["used_fallback"] is False


# ---------------------------------------------------------------------------
# Tests: multiple analog grounds
# ---------------------------------------------------------------------------


class TestMultipleAnalogGrounds:
    def test_each_analog_ground_gets_its_own_result(self) -> None:
        """A board with both GNDA and AGND emits one result per analog ground."""
        nets = _nets((1, "GNDA"), (2, "AGND"), (3, "GND"))
        # GND has loads R30, R31 to give it a topology footprint.
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_load_fp(ref="R20", net_number=2, net_name="AGND", position=(0.0, 5.0)),
            _make_load_fp(ref="R30", net_number=3, net_name="GND", position=(0.0, 10.0)),
            _make_load_fp(ref="R31", net_number=3, net_name="GND", position=(5.0, 10.0)),
        ]
        segments = [
            _segment((0.0, 10.0), (5.0, 10.0), 3, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        names = sorted(r.analog_ground_name for r in results)
        assert names == ["AGND", "GNDA"]
        assert all(r.bridge_count == 0 for r in results)


# ---------------------------------------------------------------------------
# Tests: zone-based wiring
# ---------------------------------------------------------------------------


class TestZoneBasedWiring:
    def test_bridge_wired_through_zone_polygon(self) -> None:
        """Bridge pads sit inside ground-zone boundary polygons → wired."""
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(2.0, 2.0)),
            _make_bridge_fp(
                ref="FB1",
                name="L_0805",
                value="600R@100MHz",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.5, 0.0)),
                ],
                position=(5.0, 5.0),
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(8.0, 8.0)),
        ]
        # A GNDA zone covering (0..5, 0..10) and a GND zone covering
        # (5..10, 0..10), each with a filled polygon big enough for the
        # ConnectivityValidator's containment check.
        zones = [
            MockZone(
                net_number=1,
                net_name="GNDA",
                layer="F.Cu",
                polygon=[(0, 0), (5, 0), (5, 10), (0, 10)],
                filled_polygons=[[(0, 0), (5, 0), (5, 10), (0, 10)]],
            ),
            MockZone(
                net_number=2,
                net_name="GND",
                layer="F.Cu",
                polygon=[(5, 0), (10, 0), (10, 10), (5, 10)],
                filled_polygons=[[(5, 0), (10, 0), (10, 10), (5, 10)]],
            ),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _zones=zones)
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 1
        assert r.advisory is None


# ---------------------------------------------------------------------------
# Tests: integration with check_analog_ground_bridge wording
# ---------------------------------------------------------------------------


class TestAdvisoryWording:
    def test_no_bridge_wording_matches_phase2(self) -> None:
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_load_fp(ref="R11", net_number=1, net_name="GNDA", position=(5.0, 0.0)),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
            _make_load_fp(ref="R21", net_number=2, net_name="GND", position=(20.0, 0.0)),
        ]
        segments = [
            _segment((0.0, 0.0), (5.0, 0.0), 1, "GNDA"),
            _segment((15.0, 0.0), (20.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        warnings = check_analog_ground_bridge(pcb)
        assert any("no bridge to GND" in w for w in warnings)

    def test_ground_loop_wording_lists_all_bridges(self) -> None:
        pcb = _two_bridge_pcb(both_wired=True)
        warnings = check_analog_ground_bridge(pcb)
        assert len(warnings) == 1
        msg = warnings[0]
        assert "ground loop" in msg
        assert "GNDA" in msg
        assert "GND" in msg
        assert "FB1" in msg and "NT1" in msg
        assert "2 bridges" in msg


# ---------------------------------------------------------------------------
# Tests: KiCad-10 name-only nets
# ---------------------------------------------------------------------------


class TestNameOnlyNets:
    def test_name_only_pad_not_misclassified(self) -> None:
        """A pad with net_number=0 and net_name set is not assigned to any ground.

        This protects against false positives from KiCad 10 name-only nets
        where the validator cannot tell which numeric net the pad belongs
        to without higher-level resolution.
        """
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            # Bridge has its analog pad correctly numbered, but the digital
            # pad uses net_number=0 (the "name only" KiCad-10 case).
            _make_bridge_fp(
                ref="FB1",
                name="L_0805",
                value="600R@100MHz",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 0, "GND", (0.5, 0.0)),  # name says GND but number is 0
                ],
                position=(10.0, 0.0),
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
        ]
        segments = [
            _segment((0.0, 0.0), (9.5, 0.0), 1, "GNDA"),
            _segment((10.5, 0.0), (15.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        # Since the second pad's numeric net is unresolved, we cannot
        # confirm it touches the digital ground → no bridge recognised.
        assert results[0].bridge_count == 0


# ---------------------------------------------------------------------------
# Tests: degradation paths
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_check_never_raises_on_bad_input(self) -> None:
        class BadPCB:
            @property
            def nets(self) -> Any:
                raise RuntimeError("boom")

            footprints: list[Any] = []

        # Falls back to local scan, which itself handles None nets.
        assert check_analog_ground_bridge(BadPCB()) == []  # type: ignore[arg-type]

    def test_analyze_returns_empty_on_validator_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ConnectivityValidator import or construction blows up, return []."""
        pcb = _single_bridge_pcb(
            bridge_ref="FB1",
            bridge_name="L_0805",
            bridge_value="600R@100MHz",
        )

        from kicad_tools.validate import connectivity as conn

        class _Boom:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                raise RuntimeError("validator boom")

        monkeypatch.setattr(conn, "ConnectivityValidator", _Boom)
        assert analyze_ground_topology(pcb) == []

    def test_per_net_graph_build_failure_recovers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A per-net graph or get-pads call may itself raise; bridges still flow.

        The wrapper isolates per-net failures so one bad net doesn't take
        down the whole analysis.  The resulting topology will simply treat
        that net's graph as empty (no bridges wired into it).
        """
        pcb = _single_bridge_pcb(
            bridge_ref="FB1",
            bridge_name="L_0805",
            bridge_value="600R@100MHz",
        )

        from kicad_tools.validate import connectivity as conn

        def boom_build(self, net_number):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"net {net_number} graph boom")

        monkeypatch.setattr(
            conn.ConnectivityValidator,
            "_build_connectivity_graph",
            boom_build,
        )

        # Should still return a result — but no bridges will be wired,
        # so the advisory should say "bridge present but not wired".
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        assert results[0].advisory is not None
        assert "not wired" in results[0].advisory

    def test_empty_pads_bridge_rejected(self) -> None:
        """A footprint matching a bridge regex but with NO pads on either ground.

        The regex matches a ferrite, but its pads sit on unrelated nets.
        That should not produce a bridge.
        """
        nets = _nets((1, "GNDA"), (2, "GND"), (3, "VCC"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_load_fp(ref="R11", net_number=1, net_name="GNDA", position=(5.0, 0.0)),
            # Ferrite-pattern value, but both pads on VCC — must not be a bridge.
            _make_bridge_fp(
                ref="FB99",
                name="L_0805",
                value="600R@100MHz",
                pads_spec=[
                    ("1", 3, "VCC", (-0.5, 0.0)),
                    ("2", 3, "VCC", (0.5, 0.0)),
                ],
                position=(10.0, 0.0),
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(15.0, 0.0)),
            _make_load_fp(ref="R21", net_number=2, net_name="GND", position=(20.0, 0.0)),
        ]
        segments = [
            _segment((0.0, 0.0), (5.0, 0.0), 1, "GNDA"),
            _segment((15.0, 0.0), (20.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        # No bridge candidate touches BOTH grounds → "no bridge" advisory.
        assert results[0].bridge_count == 0
        assert results[0].advisory is not None
        assert "no bridge" in results[0].advisory


# ---------------------------------------------------------------------------
# Tests: combined ground loop + floating bridge advisory
# ---------------------------------------------------------------------------


class TestCombinedAdvisory:
    def test_ground_loop_plus_floating_bridge(self) -> None:
        """Three bridges: two wired, one floating — single combined advisory."""
        nets = _nets((1, "GNDA"), (2, "GND"))
        fps = [
            _make_load_fp(ref="R10", net_number=1, net_name="GNDA", position=(0.0, 0.0)),
            _make_bridge_fp(
                ref="FB1",
                name="L_0805",
                value="600R@100MHz",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.5, 0.0)),
                ],
                position=(10.0, 0.0),
            ),
            _make_load_fp(ref="R12", net_number=1, net_name="GNDA", position=(15.0, 0.0)),
            _make_bridge_fp(
                ref="NT1",
                name="NetTie-2_SMD",
                value="NetTie",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.5, 0.0)),
                ],
                position=(25.0, 0.0),
            ),
            _make_load_fp(ref="R20", net_number=2, net_name="GND", position=(30.0, 0.0)),
            # A third, floating bridge nowhere near any copper.
            _make_bridge_fp(
                ref="R99",
                name="R_0402",
                value="0R",
                pads_spec=[
                    ("1", 1, "GNDA", (-0.5, 0.0)),
                    ("2", 2, "GND", (0.5, 0.0)),
                ],
                position=(100.0, 100.0),
            ),
        ]
        segments = [
            _segment((0.0, 0.0), (9.5, 0.0), 1, "GNDA"),
            _segment((9.5, 0.0), (15.0, 0.0), 1, "GNDA"),
            _segment((15.0, 0.0), (24.5, 0.0), 1, "GNDA"),
            _segment((10.5, 0.0), (25.5, 0.0), 2, "GND"),
            _segment((25.5, 0.0), (30.0, 0.0), 2, "GND"),
        ]
        pcb = MockPCB(nets=nets, footprints=fps, _segments=segments)
        results = analyze_ground_topology(pcb)
        assert len(results) == 1
        r = results[0]
        assert r.bridge_count == 2
        assert r.advisory is not None
        # Loop + floating combined.
        assert "ground loop" in r.advisory
        assert "additionally" in r.advisory
        assert "R99" in r.advisory


# ---------------------------------------------------------------------------
# Tests: explicitly empty ground graph (forces wired=False without exception)
# ---------------------------------------------------------------------------


class TestEmptyGroundGraph:
    def test_pad_set_wired_on_empty_graph_is_false(self) -> None:
        """``_pad_set_wired`` short-circuits on an empty graph dict."""
        from kicad_tools.analysis.ground_topology import _pad_set_wired

        assert _pad_set_wired(["FB1.1"], {}) is False

    def test_pad_set_wired_on_pad_without_neighbors_is_false(self) -> None:
        """A graph dict with an unrelated entry but no neighbors for the pad."""
        from kicad_tools.analysis.ground_topology import _pad_set_wired

        # FB1.1 has no neighbors; R10.1 has neighbors but isn't a bridge pad.
        graph: dict[str, set[str]] = {"R10.1": {"R11.1"}, "R11.1": {"R10.1"}}
        assert _pad_set_wired(["FB1.1"], graph) is False
