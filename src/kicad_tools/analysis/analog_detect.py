"""Analog component detection for PCB designs.

Identifies components that are sensitive to layout quality (analog DACs/ADCs,
op-amps, precision voltage references, audio-frequency crystals, analog
switches) by matching footprint library IDs and component values against
known patterns.  The results are advisory only -- they do not block export.

Example::

    >>> from kicad_tools.analysis.analog_detect import detect_analog_components
    >>> components = detect_analog_components(pcb)
    >>> for c in components:
    ...     print(f"{c['reference']}: {c['reason']}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "AnalogComponent",
    "detect_analog_components",
]


@dataclass
class AnalogComponent:
    """A detected analog-sensitive component."""

    reference: str
    value: str
    footprint: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "value": self.value,
            "footprint": self.footprint,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Library ID / footprint name patterns (case-insensitive).
# Each tuple is (compiled regex, human-readable reason).
_LIBRARY_ID_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Audio DACs
    (re.compile(r"PCM5\d{3}", re.IGNORECASE), "audio DAC (PCM51xx family)"),
    (re.compile(r"CS43\d{2}", re.IGNORECASE), "audio DAC (Cirrus Logic CS43xx)"),
    (re.compile(r"ES90\d{2}", re.IGNORECASE), "audio DAC (ESS Sabre ES90xx)"),
    (re.compile(r"AK4\d{3}", re.IGNORECASE), "audio DAC/ADC (AKM AK4xxx)"),
    (re.compile(r"WM89\d{2}", re.IGNORECASE), "audio codec (Wolfson WM89xx)"),
    (re.compile(r"TLV320", re.IGNORECASE), "audio codec (TI TLV320)"),
    (re.compile(r"SGTL5000", re.IGNORECASE), "audio codec (NXP SGTL5000)"),
    # Audio ADCs
    (re.compile(r"PCM18\d{2}", re.IGNORECASE), "audio ADC (PCM18xx family)"),
    (re.compile(r"CS53\d{2}", re.IGNORECASE), "audio ADC (Cirrus Logic CS53xx)"),
    # Audio amplifiers
    (re.compile(r"TPA\d{4}", re.IGNORECASE), "audio amplifier (TI TPA)"),
    (re.compile(r"LM48\d{2}", re.IGNORECASE), "audio amplifier (LM48xx)"),
    (re.compile(r"MAX98\d{3}", re.IGNORECASE), "audio amplifier (MAX98xxx)"),
    (re.compile(r"SSM\d{4}", re.IGNORECASE), "audio amplifier (Analog Devices SSM)"),
    # Op-amps: specific families before the general OPA pattern
    (re.compile(r"OPA16\d{2}", re.IGNORECASE), "audio op-amp (OPA16xx)"),
    (re.compile(r"LME49\d{3}", re.IGNORECASE), "audio op-amp (LME49xxx)"),
    (re.compile(r"OPA\d{3,4}", re.IGNORECASE), "op-amp (TI OPA)"),
    (re.compile(r"AD8\d{2,3}", re.IGNORECASE), "op-amp (Analog Devices AD8xx)"),
    (re.compile(r"LM358", re.IGNORECASE), "op-amp (LM358)"),
    (re.compile(r"LM324", re.IGNORECASE), "op-amp (LM324)"),
    (re.compile(r"NE5532", re.IGNORECASE), "op-amp (NE5532)"),
    (re.compile(r"TL07\d", re.IGNORECASE), "op-amp (TL07x)"),
    (re.compile(r"TL08\d", re.IGNORECASE), "op-amp (TL08x)"),
    # Precision voltage references
    (re.compile(r"REF\d{2,4}", re.IGNORECASE), "precision voltage reference"),
    (re.compile(r"LM4040", re.IGNORECASE), "precision voltage reference (LM4040)"),
    (re.compile(r"ADR\d{3}", re.IGNORECASE), "precision voltage reference (ADR)"),
    (re.compile(r"LT1009", re.IGNORECASE), "precision voltage reference (LT1009)"),
    (re.compile(r"MCP1541", re.IGNORECASE), "precision voltage reference (MCP1541)"),
    # Analog switches / multiplexers
    (re.compile(r"ADG\d{3,4}", re.IGNORECASE), "analog switch (Analog Devices ADG)"),
    (re.compile(r"DG4\d{2}", re.IGNORECASE), "analog switch (DG4xx)"),
    (re.compile(r"TS5A\d{4}", re.IGNORECASE), "analog switch (TI TS5A)"),
    (re.compile(r"CD4066", re.IGNORECASE), "analog switch (CD4066)"),
    (re.compile(r"MAX4\d{3}", re.IGNORECASE), "analog switch/mux (MAX4xxx)"),
    # Instrumentation amplifiers
    (re.compile(r"INA\d{3}", re.IGNORECASE), "instrumentation amplifier (TI INA)"),
    (re.compile(r"AD620", re.IGNORECASE), "instrumentation amplifier (AD620)"),
    (re.compile(r"AD623", re.IGNORECASE), "instrumentation amplifier (AD623)"),
]

# Audio-frequency crystal oscillator values (Hz).
# These are standard audio master clock frequencies.
_AUDIO_CRYSTAL_FREQUENCIES_MHZ: set[str] = {
    "11.2896",
    "12.288",
    "24.576",
    "22.5792",
    "45.1584",
    "16.9344",
    "33.8688",
}

# Regex to extract a numeric MHz value from a component value string.
_MHZ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[Mm][Hh][Zz]")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_analog_components(pcb: PCB) -> list[AnalogComponent]:
    """Scan all footprints on a PCB and return analog-sensitive components.

    Detection is based on:
    1. Footprint library ID / name matching against known analog part families.
    2. Component value matching for audio-frequency crystal oscillators.

    The function never raises; components that cannot be inspected are skipped.

    Parameters
    ----------
    pcb:
        A loaded PCB object from :mod:`kicad_tools.schema.pcb`.

    Returns
    -------
    list[AnalogComponent]
        Detected analog-sensitive components, sorted by reference designator.
    """
    results: list[AnalogComponent] = []
    seen_refs: set[str] = set()

    for fp in pcb.footprints:
        ref = fp.reference or ""
        if not ref or ref in seen_refs:
            continue

        # Fields available for matching
        name = fp.name or ""
        value = fp.value or ""

        reason = _match_library_id(name, value)
        if reason is None:
            reason = _match_crystal_value(value)

        if reason is not None:
            seen_refs.add(ref)
            results.append(
                AnalogComponent(
                    reference=ref,
                    value=value,
                    footprint=name,
                    reason=reason,
                )
            )

    results.sort(key=lambda c: c.reference)
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _match_library_id(name: str, value: str) -> str | None:
    """Check footprint name and value against known analog part patterns.

    Returns a human-readable reason string on match, or ``None``.
    """
    for pattern, reason in _LIBRARY_ID_PATTERNS:
        if pattern.search(name) or pattern.search(value):
            return reason
    return None


def _match_crystal_value(value: str) -> str | None:
    """Check whether a component value indicates an audio-frequency crystal.

    Returns a reason string on match, or ``None``.
    """
    m = _MHZ_RE.search(value)
    if m is None:
        return None
    freq = m.group(1)
    if freq in _AUDIO_CRYSTAL_FREQUENCIES_MHZ:
        return f"audio-frequency crystal ({freq} MHz)"
    return None
