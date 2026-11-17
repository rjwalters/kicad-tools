"""Tests for net-level analog detection (Phase 2, issue #3170).

Verifies that :func:`detect_analog_nets` names analog NETS by pattern
(audio / analog supply / analog ground / analog signal), does not flag
digital nets, and that :func:`check_analog_ground_bridge` fires only when an
analog ground lacks a discrete net-tie/ferrite bridge to a digital ground.

The committed boards have no analog nets, so these tests use an extended
MockPCB that adds a net-name surface (``MockNet`` / ``MockPCB.nets`` and
``net_name`` on ``MockPad``) on top of the footprint/pad mocks used by
``tests/test_analog_detect.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kicad_tools.analysis.analog_detect import (
    AnalogNet,
    check_analog_ground_bridge,
    detect_analog_nets,
)

# ---------------------------------------------------------------------------
# Extended mock objects (net-name surface)
# ---------------------------------------------------------------------------


@dataclass
class MockNet:
    """Mirror of the real ``schema.pcb.Net`` shape (number + name)."""

    number: int
    name: str


@dataclass
class MockPad:
    net_number: int = 0
    net_name: str = ""
    layers: list[str] = field(default_factory=lambda: ["F.Cu"])
    size: tuple[float, float] = (1.0, 1.0)


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
class MockPCB:
    """Minimal PCB mock with both a footprint and a net surface."""

    footprints: list[MockFootprint] = field(default_factory=list)
    nets: dict[int, MockNet] = field(default_factory=dict)


def _pcb_with_nets(*net_names: str) -> MockPCB:
    """Build a MockPCB whose nets are named as given (net 0 is unconnected)."""
    nets: dict[int, MockNet] = {0: MockNet(0, "")}
    for i, name in enumerate(net_names, start=1):
        nets[i] = MockNet(i, name)
    return MockPCB(nets=nets)


def _bridge_fp(ref: str, name: str, value: str, *pad_nets: str) -> MockFootprint:
    """Build a 2-(or N-)pad footprint with pads on the named nets."""
    return MockFootprint(
        reference=ref,
        name=name,
        value=value,
        pads=[MockPad(net_name=n) for n in pad_nets],
    )


# ---------------------------------------------------------------------------
# Tests: analog net naming
# ---------------------------------------------------------------------------


class TestAnalogNetNaming:
    """Analog nets are named with the correct kind."""

    def _by_name(self, pcb: MockPCB) -> dict[str, AnalogNet]:
        return {n.name: n for n in detect_analog_nets(pcb)}

    def test_audio_left_right(self) -> None:
        pcb = _pcb_with_nets("AUDIO_L", "AUDIO_R")
        found = self._by_name(pcb)
        assert set(found) == {"AUDIO_L", "AUDIO_R"}
        assert all(n.kind == "audio" for n in found.values())

    def test_audio_generic(self) -> None:
        pcb = _pcb_with_nets("AUDIO_OUT", "MIC_IN", "SPK_L", "LINE_R")
        found = self._by_name(pcb)
        assert all(n.kind == "audio" for n in found.values())
        assert set(found) == {"AUDIO_OUT", "MIC_IN", "SPK_L", "LINE_R"}

    def test_i2s_is_audio(self) -> None:
        pcb = _pcb_with_nets("I2S_SD", "I2S_WS")
        found = self._by_name(pcb)
        assert set(found) == {"I2S_SD", "I2S_WS"}
        assert all(n.kind == "audio" for n in found.values())

    def test_analog_supply(self) -> None:
        pcb = _pcb_with_nets("+3.3VA", "AVDD", "+5VA")
        found = self._by_name(pcb)
        assert set(found) == {"+3.3VA", "AVDD", "+5VA"}
        assert all(n.kind == "analog_supply" for n in found.values())

    def test_analog_ground(self) -> None:
        pcb = _pcb_with_nets("GNDA", "AGND")
        found = self._by_name(pcb)
        assert set(found) == {"GNDA", "AGND"}
        assert all(n.kind == "analog_ground" for n in found.values())

    def test_analog_signals(self) -> None:
        pcb = _pcb_with_nets("VREF", "AIN0", "ADC_CH0", "DAC_0", "SENSE")
        found = self._by_name(pcb)
        assert set(found) == {"VREF", "AIN0", "ADC_CH0", "DAC_0", "SENSE"}
        assert all(n.kind == "analog_signal" for n in found.values())

    def test_reason_carries_layout_hint(self) -> None:
        pcb = _pcb_with_nets("GNDA")
        net = self._by_name(pcb)["GNDA"]
        assert "bridge to GND at a single point" in net.reason

    def test_results_sorted_by_name(self) -> None:
        pcb = _pcb_with_nets("VREF", "AUDIO_L", "GNDA")
        names = [n.name for n in detect_analog_nets(pcb)]
        assert names == sorted(names)

    def test_net_zero_skipped(self) -> None:
        # Net 0 carrying an analog-looking name must never be reported.
        pcb = MockPCB(nets={0: MockNet(0, "AUDIO_L")})
        assert detect_analog_nets(pcb) == []

    def test_empty_pcb(self) -> None:
        assert detect_analog_nets(MockPCB()) == []


# ---------------------------------------------------------------------------
# Tests: no false positives on digital nets
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    """Standard digital nets must NOT be flagged as analog."""

    def test_digital_nets_not_flagged(self) -> None:
        pcb = _pcb_with_nets(
            "GND",
            "DGND",
            "+3.3V",
            "+5V",
            "D0",
            "D1",
            "SPI_MOSI",
            "USB_DP",
            "CLK",
        )
        assert detect_analog_nets(pcb) == []

    def test_digital_ground_not_analog_ground(self) -> None:
        pcb = _pcb_with_nets("GND", "DGND", "VSS")
        assert detect_analog_nets(pcb) == []

    def test_digital_supply_not_analog_supply(self) -> None:
        pcb = _pcb_with_nets("+3.3V", "+5V", "DVDD", "VCC")
        assert detect_analog_nets(pcb) == []


# ---------------------------------------------------------------------------
# Tests: isolated-analog-ground bridge flag (local 2-pad scan)
# ---------------------------------------------------------------------------


class TestGndaBridge:
    """The missing-bridge advisory fires only when no bridge component spans
    the analog and digital grounds."""

    def test_nettie_bridge_no_flag(self) -> None:
        pcb = _pcb_with_nets("GNDA", "GND")
        pcb.footprints = [_bridge_fp("NT1", "NetTie-2_SMD", "NetTie", "GNDA", "GND")]
        assert check_analog_ground_bridge(pcb) == []

    def test_ferrite_bridge_no_flag(self) -> None:
        pcb = _pcb_with_nets("GNDA", "GND")
        pcb.footprints = [_bridge_fp("FB1", "L_0805", "600R@100MHz", "GNDA", "GND")]
        assert check_analog_ground_bridge(pcb) == []

    def test_missing_bridge_flagged(self) -> None:
        pcb = _pcb_with_nets("GNDA", "GND")
        # No bridge component at all.
        warnings = check_analog_ground_bridge(pcb)
        assert len(warnings) == 1
        assert "GNDA" in warnings[0]
        assert "GND" in warnings[0]
        assert "no bridge" in warnings[0]

    def test_unrelated_bridge_does_not_satisfy(self) -> None:
        # A net-tie that does NOT touch GND (both pads on GNDA) is not a bridge.
        pcb = _pcb_with_nets("GNDA", "GND")
        pcb.footprints = [_bridge_fp("NT1", "NetTie-2_SMD", "NetTie", "GNDA", "GNDA")]
        warnings = check_analog_ground_bridge(pcb)
        assert len(warnings) == 1

    def test_no_digital_ground_no_flag(self) -> None:
        # Analog ground present but nothing digital to bridge to.
        pcb = _pcb_with_nets("GNDA")
        assert check_analog_ground_bridge(pcb) == []

    def test_no_analog_ground_no_flag(self) -> None:
        pcb = _pcb_with_nets("GND", "DGND")
        assert check_analog_ground_bridge(pcb) == []

    def test_agnd_dgnd_pair(self) -> None:
        pcb = _pcb_with_nets("AGND", "DGND")
        # No bridge -> flag.
        warnings = check_analog_ground_bridge(pcb)
        assert len(warnings) == 1
        assert "AGND" in warnings[0]

    def test_three_pad_component_not_a_bridge(self) -> None:
        # A 3-pad part is not the 2-pad single-point bridge we look for.
        pcb = _pcb_with_nets("GNDA", "GND")
        pcb.footprints = [_bridge_fp("U1", "SOT-23", "NetTie", "GNDA", "GND", "VCC")]
        warnings = check_analog_ground_bridge(pcb)
        assert len(warnings) == 1
