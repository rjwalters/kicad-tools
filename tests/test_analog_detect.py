"""Tests for analog component detection.

Verifies that the detection logic correctly identifies analog-sensitive
components (audio DACs/ADCs, op-amps, precision references, audio crystals,
analog switches) and produces no false positives for standard digital parts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kicad_tools.analysis.analog_detect import (
    AnalogComponent,
    detect_analog_components,
)

# ---------------------------------------------------------------------------
# Minimal mock objects
# ---------------------------------------------------------------------------


@dataclass
class MockPad:
    net_number: int = 0
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
    """Minimal PCB mock supporting footprint iteration."""

    footprints: list[MockFootprint] = field(default_factory=list)


def _make_pcb(*footprints: MockFootprint) -> MockPCB:
    return MockPCB(footprints=list(footprints))


def _fp(ref: str, value: str, name: str = "") -> MockFootprint:
    """Shorthand to build a mock footprint."""
    return MockFootprint(reference=ref, value=value, name=name)


# ---------------------------------------------------------------------------
# Tests: Audio DAC detection
# ---------------------------------------------------------------------------


class TestAudioDACDetection:
    """Detection of audio DAC/ADC ICs."""

    def test_pcm5122_by_name(self) -> None:
        pcb = _make_pcb(_fp("U1", "PCM5122", name="Package_SO:TSSOP-20_PCM5122"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert result[0].reference == "U1"
        assert "audio DAC" in result[0].reason

    def test_pcm5102_by_value(self) -> None:
        pcb = _make_pcb(_fp("U2", "PCM5102A"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "PCM51xx" in result[0].reason

    def test_cs4344_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "CS4344"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "Cirrus Logic CS43xx" in result[0].reason

    def test_es9038_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "ES9038PRO"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "ESS Sabre" in result[0].reason

    def test_ak4490_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "AK4490"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "AKM" in result[0].reason

    def test_wm8960_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "WM8960"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "Wolfson" in result[0].reason

    def test_tlv320_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "TLV320AIC3104"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "TLV320" in result[0].reason

    def test_sgtl5000_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "SGTL5000"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "SGTL5000" in result[0].reason


# ---------------------------------------------------------------------------
# Tests: Op-amp detection
# ---------------------------------------------------------------------------


class TestOpAmpDetection:
    """Detection of operational amplifiers."""

    def test_opa2134_by_value(self) -> None:
        pcb = _make_pcb(_fp("U3", "OPA2134"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "op-amp" in result[0].reason

    def test_ad8421_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "AD8421"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "AD8xx" in result[0].reason

    def test_ne5532_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "NE5532"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "NE5532" in result[0].reason

    def test_tl072_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "TL072"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "TL07x" in result[0].reason

    def test_lm358_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "LM358"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "LM358" in result[0].reason

    def test_opa1612_by_name(self) -> None:
        pcb = _make_pcb(_fp("U1", "OPA1612", name="Amplifier:OPA1612"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "audio op-amp" in result[0].reason


# ---------------------------------------------------------------------------
# Tests: Precision reference detection
# ---------------------------------------------------------------------------


class TestPrecisionReferenceDetection:
    """Detection of precision voltage references."""

    def test_ref5050_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "REF5050"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "precision voltage reference" in result[0].reason

    def test_lm4040_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "LM4040"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "LM4040" in result[0].reason

    def test_adr4550_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "ADR4550"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "ADR" in result[0].reason


# ---------------------------------------------------------------------------
# Tests: Audio crystal detection
# ---------------------------------------------------------------------------


class TestAudioCrystalDetection:
    """Detection of audio-frequency crystal oscillators."""

    def test_11_2896mhz(self) -> None:
        pcb = _make_pcb(_fp("Y1", "11.2896MHz"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "audio-frequency crystal" in result[0].reason
        assert "11.2896" in result[0].reason

    def test_12_288mhz(self) -> None:
        pcb = _make_pcb(_fp("Y1", "12.288 MHz"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "12.288" in result[0].reason

    def test_24_576mhz(self) -> None:
        pcb = _make_pcb(_fp("Y1", "24.576MHz"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "24.576" in result[0].reason

    def test_non_audio_crystal_not_detected(self) -> None:
        """Standard digital crystals should not be flagged."""
        pcb = _make_pcb(_fp("Y1", "8MHz"), _fp("Y2", "25MHz"))
        result = detect_analog_components(pcb)
        assert len(result) == 0

    def test_16mhz_not_detected(self) -> None:
        """Common MCU crystal should not be flagged."""
        pcb = _make_pcb(_fp("Y1", "16.000MHz"))
        result = detect_analog_components(pcb)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Tests: Analog switch detection
# ---------------------------------------------------------------------------


class TestAnalogSwitchDetection:
    """Detection of analog switches and multiplexers."""

    def test_adg708_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "ADG708"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "analog switch" in result[0].reason

    def test_ts5a3159_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "TS5A3159"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "analog switch" in result[0].reason

    def test_cd4066_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "CD4066"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "CD4066" in result[0].reason


# ---------------------------------------------------------------------------
# Tests: Instrumentation amplifier detection
# ---------------------------------------------------------------------------


class TestInstrumentationAmpDetection:
    """Detection of instrumentation amplifiers."""

    def test_ina128_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "INA128"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "instrumentation amplifier" in result[0].reason

    def test_ad620_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "AD620"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "AD620" in result[0].reason


# ---------------------------------------------------------------------------
# Tests: Audio amplifier detection
# ---------------------------------------------------------------------------


class TestAudioAmplifierDetection:
    """Detection of audio power amplifiers."""

    def test_tpa3116_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "TPA3116"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "audio amplifier" in result[0].reason

    def test_max98357_by_value(self) -> None:
        pcb = _make_pcb(_fp("U1", "MAX98357"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "audio amplifier" in result[0].reason


# ---------------------------------------------------------------------------
# Tests: No false positives
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    """Standard digital components should not be detected."""

    def test_mcu_not_detected(self) -> None:
        pcb = _make_pcb(
            _fp("U1", "STM32F411CEU6"),
            _fp("U2", "ESP32-WROOM-32"),
        )
        assert detect_analog_components(pcb) == []

    def test_passives_not_detected(self) -> None:
        pcb = _make_pcb(
            _fp("R1", "10k"),
            _fp("C1", "100nF"),
            _fp("L1", "10uH"),
        )
        assert detect_analog_components(pcb) == []

    def test_digital_ic_not_detected(self) -> None:
        pcb = _make_pcb(
            _fp("U1", "74HC595"),
            _fp("U2", "SN74LVC1G04"),
        )
        assert detect_analog_components(pcb) == []

    def test_voltage_regulator_not_detected(self) -> None:
        pcb = _make_pcb(
            _fp("U1", "AMS1117-3.3"),
            _fp("U2", "LM7805"),
        )
        assert detect_analog_components(pcb) == []

    def test_empty_pcb(self) -> None:
        pcb = _make_pcb()
        assert detect_analog_components(pcb) == []


# ---------------------------------------------------------------------------
# Tests: Mixed board
# ---------------------------------------------------------------------------


class TestMixedBoard:
    """Boards with both analog and digital components."""

    def test_mixed_board_detects_only_analog(self) -> None:
        pcb = _make_pcb(
            _fp("U1", "STM32F411CEU6"),  # digital MCU
            _fp("U2", "PCM5122"),  # audio DAC
            _fp("R1", "10k"),  # passive
            _fp("Y1", "12.288MHz"),  # audio crystal
            _fp("U3", "OPA2134"),  # op-amp
            _fp("C1", "100nF"),  # passive
        )
        result = detect_analog_components(pcb)
        refs = {c.reference for c in result}
        assert refs == {"U2", "Y1", "U3"}

    def test_results_sorted_by_reference(self) -> None:
        pcb = _make_pcb(
            _fp("U5", "OPA2134"),
            _fp("U1", "PCM5122"),
            _fp("U3", "AD8421"),
        )
        result = detect_analog_components(pcb)
        refs = [c.reference for c in result]
        assert refs == ["U1", "U3", "U5"]


# ---------------------------------------------------------------------------
# Tests: AnalogComponent dataclass
# ---------------------------------------------------------------------------


class TestAnalogComponentDataclass:
    """Tests for the AnalogComponent dataclass and to_dict."""

    def test_to_dict(self) -> None:
        comp = AnalogComponent(
            reference="U1",
            value="PCM5122",
            footprint="Package_SO:TSSOP-20",
            reason="audio DAC (PCM51xx family)",
        )
        d = comp.to_dict()
        assert d == {
            "reference": "U1",
            "value": "PCM5122",
            "footprint": "Package_SO:TSSOP-20",
            "reason": "audio DAC (PCM51xx family)",
        }


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and robustness."""

    def test_empty_reference_skipped(self) -> None:
        """Footprints without a reference are skipped."""
        pcb = _make_pcb(_fp("", "PCM5122"))
        assert detect_analog_components(pcb) == []

    def test_duplicate_reference_only_counted_once(self) -> None:
        """If two footprints share a reference, only the first is counted."""
        pcb = _make_pcb(
            _fp("U1", "PCM5122"),
            _fp("U1", "PCM5122"),
        )
        result = detect_analog_components(pcb)
        assert len(result) == 1

    def test_case_insensitive_matching(self) -> None:
        """Pattern matching is case-insensitive."""
        pcb = _make_pcb(_fp("U1", "pcm5122"))
        result = detect_analog_components(pcb)
        assert len(result) == 1

    def test_name_matching_takes_precedence_over_crystal(self) -> None:
        """Library ID match is returned even if crystal match would also hit."""
        # A component whose name matches a library pattern and whose value
        # could be interpreted as a crystal frequency -- library match wins.
        pcb = _make_pcb(_fp("U1", "12.288MHz", name="AudioCodec:PCM5122"))
        result = detect_analog_components(pcb)
        assert len(result) == 1
        assert "audio DAC" in result[0].reason
