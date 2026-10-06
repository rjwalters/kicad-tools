"""Missing decoupling capacitor checks.

Complements :class:`~kicad_tools.explain.checks.bypass.BypassCapDistanceCheck`
(which only evaluates the placement of *existing* bypass capacitors) by
flagging IC power pins that have **no** decoupling capacitor anywhere on
their net at all -- the "power pins without nearby caps" gap called out by
Konnect's ``audit_decoupling`` (issue #4899, item 4 of #4880's ideas audit).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..mistakes import (
    Mistake,
    MistakeCategory,
    is_bypass_cap,
    is_ground_net,
    is_power_net,
    power_pin_nets,
)

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint

# Minimum pad count for a footprint to be considered an IC rather than a
# passive/connector. Mirrors the reference-prefix exclusions used elsewhere
# in this module (R/C/L/D/Y/X are passives; J/SW/TP/FB are connectors,
# switches, test points, and ferrite beads -- none of these need bypass
# capacitors decoupling their own supply pin). Q is a discrete transistor:
# an 8-pad SO-8 power MOSFET sitting on a rail is not an IC supply pin.
_MIN_IC_PADS = 4
_NON_IC_REFERENCE_PREFIXES = (
    "R",
    "C",
    "L",
    "D",
    "Y",
    "X",
    "J",
    "Q",
    "SW",
    "TP",
    "FB",
    "MH",
    "FID",
)

# "Is there *any* decoupling on this rail?" accepts any capacitor of at least
# this value, not just :func:`is_bypass_cap`'s fixed value list (which misses
# 470nF, 2.2uF, 22uF ...). 10nF keeps load/compensation caps (22pF, 75pF C0G)
# from counting.
_MIN_DECOUPLING_FARADS = 10e-9
_CAP_VALUE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([pnuµμm])?(\d*)", re.IGNORECASE)
_CAP_MULTIPLIERS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}


def _capacitance_farads(value: str) -> float | None:
    """Parse ``100nF``, ``2.2uF``, ``4u7``, ``22u 25V`` to farads (None if unparseable)."""
    match = _CAP_VALUE_RE.match(value or "")
    if not match or not match.group(2):
        return None
    number = match.group(1)
    if match.group(3):  # RKM notation: 4u7 -> 4.7u
        if "." in number:
            return None
        number = f"{number}.{match.group(3)}"
    return float(number) * _CAP_MULTIPLIERS[match.group(2).lower()]


def _is_decoupling_cap(reference: str, value: str) -> bool:
    """A capacitor big enough to count as *some* decoupling on a rail."""
    if is_bypass_cap(reference, value):
        return True
    if not reference.upper().startswith("C"):
        return False
    farads = _capacitance_farads(value)
    return farads is not None and farads >= _MIN_DECOUPLING_FARADS


class MissingDecouplingCapCheck:
    """Check that every IC power net has at least one decoupling capacitor.

    Unlike :class:`BypassCapDistanceCheck`, which only fires when a bypass
    cap exists but is placed too far from the pin it decouples, this check
    fires when a power net feeding an IC has **zero** bypass-style
    capacitors anywhere on the board -- a strictly complementary failure
    mode (totally missing decoupling is worse than "too far").
    """

    category = MistakeCategory.DECOUPLING

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check for IC power pins with no decoupling capacitor on their net.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for power nets missing a decoupling cap
        """
        mistakes: list[Mistake] = []

        evidence = power_pin_nets(pcb)
        ics = self._find_ics(pcb, evidence)
        cap_nets = self._bypass_cap_nets(pcb, evidence)

        # Report each (IC, power net) pair only once even if the IC has
        # multiple pads on the same net (common for high-current pins).
        seen: set[tuple[str, str]] = set()

        for ic in ics:
            power_nets = {
                pad.net_name
                for pad in ic.pads
                if pad.net_name and is_power_net(pad.net_name, evidence)
            }
            for net_name in sorted(power_nets):
                if net_name in cap_nets:
                    continue
                key = (ic.reference, net_name)
                if key in seen:
                    continue
                seen.add(key)
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.DECOUPLING,
                        severity="error",
                        title="Missing decoupling capacitor",
                        components=[ic.reference],
                        location=ic.position,
                        explanation=(
                            f"{ic.reference} has a power pin on net {net_name!r}, "
                            "but no bypass/decoupling capacitor connects that net "
                            "to a recognized ground return. Without local decoupling, "
                            "supply transients from switching current can couple "
                            "into the IC and cause glitches, resets, or radiated "
                            "noise."
                        ),
                        fix_suggestion=(
                            f"Add a decoupling capacitor (typically 100nF, or per "
                            f"the datasheet) from {net_name} to the nearest ground "
                            f"return, placed within a few mm of {ic.reference}'s "
                            "power pin."
                        ),
                        learn_more_url="docs/mistakes/bypass-cap-placement.md",
                    )
                )

        return mistakes

    def _find_ics(self, pcb: PCB, evidence: set[str] | None = None) -> list[Footprint]:
        """Find components that look like ICs with a power-net pad."""
        ics: list[Footprint] = []
        for fp in pcb.footprints:
            if len(fp.pads) < _MIN_IC_PADS:
                continue
            ref_upper = fp.reference.upper()
            if ref_upper.startswith(_NON_IC_REFERENCE_PREFIXES):
                continue
            if any(pad.net_name and is_power_net(pad.net_name, evidence) for pad in fp.pads):
                ics.append(fp)
        return ics

    def _bypass_cap_nets(self, pcb: PCB, evidence: set[str] | None = None) -> set[str]:
        """Return supply nets with a two-terminal bypass cap to ground."""
        nets: set[str] = set()
        for fp in pcb.footprints:
            if not _is_decoupling_cap(fp.reference, fp.value):
                continue
            if len(fp.pads) != 2:
                continue
            pad_nets = {pad.net_name for pad in fp.pads if pad.net_name}
            if len(pad_nets) != 2 or not any(is_ground_net(net) for net in pad_nets):
                continue
            nets.update(net for net in pad_nets if is_power_net(net, evidence))
        return nets
