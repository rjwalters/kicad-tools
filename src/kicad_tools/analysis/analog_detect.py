"""Analog detection for PCB designs.

Two complementary, advisory-only detectors live here:

* :func:`detect_analog_components` -- *component-level* detection (Phase 1).
  Identifies components that are sensitive to layout quality (analog
  DACs/ADCs, op-amps, precision voltage references, audio-frequency
  crystals, analog switches) by matching footprint library IDs and
  component values against known patterns.

* :func:`detect_analog_nets` and :func:`check_analog_ground_bridge` --
  *net-level* detection (Phase 2).  Names analog NETS (AUDIO_L/R, GNDA,
  +3.3VA, VREF, ...) by reusing the router's
  :func:`kicad_tools.router.net_class.classify_from_name` plus a thin
  analog-specific naming layer, and flags an analog ground (GNDA/AGND)
  that has no discrete bridge component (ferrite / net-tie) to digital
  ground.

Both are advisory only -- they never block export and never raise.

Example::

    >>> from kicad_tools.analysis.analog_detect import detect_analog_components
    >>> components = detect_analog_components(pcb)
    >>> for c in components:
    ...     print(f"{c.reference}: {c.reason}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "AnalogComponent",
    "AnalogNet",
    "check_analog_ground_bridge",
    "detect_analog_components",
    "detect_analog_nets",
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


@dataclass
class AnalogNet:
    """A detected analog net.

    ``kind`` is one of ``"audio"``, ``"analog_supply"``, ``"analog_ground"``
    or ``"analog_signal"``.  ``reason`` is a human-readable hint that already
    bakes in the layout advice for that ``kind`` (e.g. "isolated analog
    ground; keep separate from digital return, bridge to GND at a single
    point").
    """

    name: str
    kind: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
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


# ===========================================================================
# Net-level analog detection (Phase 2, issue #3170)
# ===========================================================================
#
# The component detector above matches footprint name/value and never
# inspects nets.  These functions are its *net-level* sibling: they classify
# net NAMES and run a local 2-pad bridge scan for an isolated analog ground.
#
# Classification reuses the router's ``classify_from_name`` (issue #3151) so
# analog-net detection stays consistent with the router's view, then layers a
# thin analog-specific naming pass on top -- the generic NetClass.GROUND /
# .POWER do not distinguish "analog ground" / "analog supply" specifically.
# ---------------------------------------------------------------------------

# Analog-supply suffix: a power-rail name ending in an "A" (analog) marker,
# e.g. +3.3VA, 1.8VA, AVDD, +5VA.  We deliberately keep this conservative so
# that plain digital rails (+3.3V, +5V, DVDD) are NOT flagged.
_ANALOG_SUPPLY_RE = re.compile(
    r"^(?:[+-]?\d+\.?\d*VA|[+-]?\d+V\d+A|AVDD|AVCC|VDDA|VCCA|VANA)$",
    re.IGNORECASE,
)

# Analog-ground names: a ground net dedicated to the analog return path.
_ANALOG_GROUND_RE = re.compile(r"^(?:GNDA|AGND)$|_AGND$", re.IGNORECASE)

# Digital-ground names: the return we expect an analog ground to bridge TO.
_DIGITAL_GROUND_RE = re.compile(r"^(?:GND|GNDD|DGND|VSS|GROUND)$|_DGND$", re.IGNORECASE)

# Audio net names (subset of NetClass.ANALOG that is specifically audio).
_AUDIO_RE = re.compile(
    r"(?:AUDIO|MIC|SPK|LINE)(?:_[LRIO])?|(?:I2S|TDM|PDM)_(?:DIN|DOUT|SD|WS)",
    re.IGNORECASE,
)

# Per-kind layout hint appended to the net name in the audit advisory.
_KIND_REASONS: dict[str, str] = {
    "audio": "audio signal; keep short, away from digital/switching nets",
    "analog_supply": (
        "analog supply rail; route separately from digital power, decouple close to the analog IC"
    ),
    "analog_ground": (
        "isolated analog ground; keep separate from digital return, bridge to GND at a single point"
    ),
    "analog_signal": "analog signal; noise-sensitive, avoid crossing digital signals",
}

# Two-pad bridge component recognition.  A bridge ties the analog ground to
# the digital ground at a single point; it is either a KiCad net-tie or a
# ferrite bead.
_NETTIE_NAME_RE = re.compile(r"NetTie", re.IGNORECASE)

# Ferrite-bead value patterns.  Kept intentionally small and documented:
#   FB / FB1                 -- ferrite-bead reference convention used as value
#   BLM... / MPZ... / HZ...  -- common ferrite part-number prefixes
#   600R@100MHz / 120@100MHz -- impedance-at-frequency value strings
_FERRITE_VALUE_RE = re.compile(
    r"^FB\d*$|^(?:BLM|MPZ|HZ|MMZ|BK)\w*|\d+\s*R?\s*@\s*\d+\s*MHZ",
    re.IGNORECASE,
)


def detect_analog_nets(pcb: PCB) -> list[AnalogNet]:
    """Scan a PCB's nets and return analog nets classified by name.

    Detection is purely name-based.  Each net's name is run through the
    router's :func:`classify_from_name` and a thin analog-specific naming
    pass to assign a ``kind`` (``audio`` / ``analog_supply`` /
    ``analog_ground`` / ``analog_signal``) and a human-readable layout hint.

    Net 0 (the KiCad unconnected net) and nets with empty names are skipped.
    Digital nets (``GND``, ``+3.3V``, ``D0``, ``SPI_MOSI``, ``USB_DP`` ...)
    are NOT returned.

    The function never raises; nets that cannot be inspected are skipped.

    Parameters
    ----------
    pcb:
        A loaded PCB object exposing ``nets`` (a mapping of net number to a
        net with a ``.name`` attribute).

    Returns
    -------
    list[AnalogNet]
        Detected analog nets, sorted by net name.
    """
    results: list[AnalogNet] = []
    seen: set[str] = set()

    nets = getattr(pcb, "nets", None) or {}
    for number, net in nets.items():
        # Skip the unconnected net (KiCad reserves number 0).
        if number == 0:
            continue
        name = getattr(net, "name", "") or ""
        if not name or name in seen:
            continue

        kind = _classify_analog_net(name)
        if kind is None:
            continue

        seen.add(name)
        results.append(AnalogNet(name=name, kind=kind, reason=_KIND_REASONS[kind]))

    results.sort(key=lambda n: n.name)
    return results


def check_analog_ground_bridge(pcb: PCB) -> list[str]:
    """Flag an analog ground that has no discrete bridge to digital ground.

    Local 2-pad heuristic (no connectivity graph required): when a board has
    BOTH an analog-ground net (``GNDA`` / ``AGND``) AND a digital-ground net
    (``GND`` / ``DGND`` / ``VSS``), this looks for a single 2-pad bridge
    component -- a net-tie (footprint name contains ``NetTie``) or a ferrite
    bead (value matches a ferrite pattern) -- with one pad on the analog
    ground and one pad on the digital ground.  If no such component exists,
    a missing-bridge message is returned for that analog ground.

    Limitations (documented intentionally)
    --------------------------------------
    This is a *pad-membership scan only*.  It does NOT consult routed copper
    or zone fills.  If a board ties the grounds with a 0R resistor that is
    not recognised as a ferrite, or via a zone stitch / single bridge via
    rather than a discrete net-tie or ferrite component, this check may emit
    a false "no bridge" advisory.  Full connectivity-topology verification --
    confirming GNDA and GND join through *exactly one* electrical path via
    the copper/zone graph -- is deferred to Phase 2b (tracked separately) and
    requires cross-net single-point-bridge modelling in
    ``validate/connectivity.py``.

    The function never raises; on any failure it returns an empty list.

    Parameters
    ----------
    pcb:
        A loaded PCB object exposing ``nets`` and ``footprints``.

    Returns
    -------
    list[str]
        One advisory string per analog ground lacking a recognised bridge.
        Empty when every analog ground is bridged, when no analog ground is
        present, or when no digital ground exists to bridge to.
    """
    findings: list[str] = []

    nets = getattr(pcb, "nets", None) or {}
    analog_grounds = {
        net.name
        for number, net in nets.items()
        if number != 0 and getattr(net, "name", "") and _ANALOG_GROUND_RE.search(net.name)
    }
    digital_grounds = {
        net.name
        for number, net in nets.items()
        if number != 0 and getattr(net, "name", "") and _DIGITAL_GROUND_RE.match(net.name)
    }

    # Nothing to bridge, or nothing to bridge to.
    if not analog_grounds or not digital_grounds:
        return findings

    # Collect, per bridge component, the set of net names its pads land on.
    bridged_pairs: list[tuple[set[str], str]] = []
    for fp in getattr(pcb, "footprints", []) or []:
        if not _is_bridge_component(fp):
            continue
        pads = getattr(fp, "pads", []) or []
        if len(pads) != 2:
            continue
        pad_nets = {getattr(p, "net_name", "") or "" for p in pads}
        ref = getattr(fp, "reference", "") or "?"
        bridged_pairs.append((pad_nets, ref))

    for agnd in sorted(analog_grounds):
        # The analog ground is "bridged" if some 2-pad bridge component has
        # one pad on this analog ground and one on a digital ground.
        bridged = any(
            agnd in pad_nets and (pad_nets & digital_grounds) for pad_nets, _ref in bridged_pairs
        )
        if not bridged:
            dg = sorted(digital_grounds)[0]
            findings.append(
                f"analog ground {agnd} has no bridge to {dg} -- "
                "add a ferrite/net-tie single-point bridge"
            )

    return findings


# ---------------------------------------------------------------------------
# Internal helpers (net-level)
# ---------------------------------------------------------------------------


def _classify_analog_net(name: str) -> str | None:
    """Return the analog ``kind`` for a net name, or ``None`` if not analog.

    Reuses the router's ``classify_from_name`` and layers the analog-specific
    naming pass on top.  Order matters: the most specific analog kinds
    (audio, analog supply, analog ground) are checked before the generic
    analog-signal class.
    """
    # Local import to avoid a heavy/circular import at module load.
    from kicad_tools.router.net_class import NetClass, classify_from_name

    net_class = classify_from_name(name)

    # Audio is the most specific analog signal kind.
    if _AUDIO_RE.search(name):
        return "audio"

    # Analog supply: a power rail with an explicit analog marker.
    if net_class == NetClass.POWER and _ANALOG_SUPPLY_RE.match(name):
        return "analog_supply"

    # Analog ground: a ground net dedicated to the analog return.
    if net_class == NetClass.GROUND and _ANALOG_GROUND_RE.search(name):
        return "analog_ground"

    # Generic analog signal (VREF, AIN*, ADC_*/DAC_*, I2S_*, SENSE ...).
    if net_class == NetClass.ANALOG:
        return "analog_signal"

    return None


def _is_bridge_component(fp: object) -> bool:
    """Return True if a footprint looks like a ground-bridge (net-tie/ferrite)."""
    name = getattr(fp, "name", "") or ""
    value = getattr(fp, "value", "") or ""
    if _NETTIE_NAME_RE.search(name) or _NETTIE_NAME_RE.search(value):
        return True
    if _FERRITE_VALUE_RE.search(value):
        return True
    return False
