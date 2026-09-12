"""Signal-connectivity checks: pull-up resistors and LED series resistors.

Covers the ``audit_connections`` gap called out by issue #4899 (item 4 of
#4880's Konnect ideas audit): I2C/reset pull-ups and LED series resistors.
Both checks are net-topology heuristics evaluated purely against the PCB
netlist (shared net names between resistors, buses, and LEDs) -- no
schematic pin-direction metadata is required.

"Floating inputs" and "shorted outputs" (the other two ``audit_connections``
sub-checks) are already covered by this repo's existing ``kicad-cli`` ERC
integration in ``kct check`` (unconnected-pin and conflicting-driver
violations are standard ERC error classes), so they are intentionally *not*
reimplemented here -- see the issue #4899 design note in the PR description.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory, is_power_net

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint

_I2C_NET_PATTERNS = ("SCL", "SDA")

# Matches RESET / RST / NRST as a whole "word" (delimited by start/end of
# string or `_`/`-`) so we don't false-positive on substrings like "BURST"
# or "FIRST".
_RESET_NET_RE = re.compile(r"(^|[_-])(N?RST|RESET)($|[_-])", re.IGNORECASE)


def _needs_pullup(net_name: str) -> bool:
    upper = net_name.upper()
    if any(pattern in upper for pattern in _I2C_NET_PATTERNS):
        return True
    return bool(_RESET_NET_RE.search(net_name))


class PullUpResistorCheck:
    """Check that I2C bus lines and reset lines have a pull-up resistor.

    I2C (SDA/SCL) is an open-drain bus and *requires* an external pull-up
    to a supply rail to function at all; active-low reset lines
    conventionally do too, to guarantee a defined idle state. This is a
    best-effort net-name heuristic (it does not know the true electrical
    pin type), so it only fires on multi-component nets -- a lone pad on a
    matching net name is far more likely to be a test point or unused
    label than a real bus.
    """

    category = MistakeCategory.CONNECTIVITY

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check I2C/reset nets for a pull-up resistor.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for buses/reset lines missing a pull-up
        """
        mistakes: list[Mistake] = []

        candidate_nets = self._candidate_nets(pcb)
        for net_name, refs in sorted(candidate_nets.items()):
            if len(refs) < 2:
                continue
            if self._has_pullup(pcb, net_name):
                continue
            mistakes.append(
                Mistake(
                    category=MistakeCategory.CONNECTIVITY,
                    severity="warning",
                    title="Missing pull-up resistor",
                    components=sorted(refs),
                    explanation=(
                        f"Net {net_name!r} looks like an I2C bus line or reset "
                        "line, but no resistor connects it to a supply rail. "
                        "I2C is open-drain and needs an external pull-up to "
                        "function; an unpulled reset line can float into an "
                        "indeterminate state."
                    ),
                    fix_suggestion=(
                        f"Add a pull-up resistor (typically 2.2k-10k for I2C) "
                        f"from {net_name} to the bus/logic supply rail."
                    ),
                    learn_more_url="docs/mistakes/pull-up-resistors.md",
                )
            )

        return mistakes

    def _candidate_nets(self, pcb: PCB) -> dict[str, set[str]]:
        """Map I2C/reset-looking net names to the set of component refs on them."""
        nets: dict[str, set[str]] = {}
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_name and _needs_pullup(pad.net_name):
                    nets.setdefault(pad.net_name, set()).add(fp.reference)
        return nets

    def _has_pullup(self, pcb: PCB, net_name: str) -> bool:
        """True if some resistor bridges *net_name* to a power net."""
        for fp in pcb.footprints:
            if not fp.reference.upper().startswith("R"):
                continue
            pad_nets = {pad.net_name for pad in fp.pads if pad.net_name}
            if net_name not in pad_nets:
                continue
            if any(n != net_name and is_power_net(n) for n in pad_nets):
                return True
        return False


def _is_led(fp: Footprint) -> bool:
    ref_upper = fp.reference.upper()
    if ref_upper.startswith("LED"):
        return True
    if not ref_upper.startswith("D"):
        return False
    haystack = f"{fp.value} {fp.name}".upper()
    return "LED" in haystack


class LedSeriesResistorCheck:
    """Check that LEDs have a series current-limiting resistor.

    Driving an LED directly from a rail (or a GPIO) without a series
    resistor relies on the LED's own (typically very low) forward
    resistance to limit current -- this usually over-drives the LED and
    shortens its life or destroys it outright. This heuristic only checks
    for *any* resistor sharing a net with the LED; it cannot distinguish a
    resistor from a constant-current driver IC, so a false positive is
    possible when current limiting is done in silicon rather than with a
    discrete resistor.
    """

    category = MistakeCategory.CONNECTIVITY

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check LEDs for a series resistor on one of their nets.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for LEDs with no series resistor
        """
        mistakes: list[Mistake] = []

        for fp in pcb.footprints:
            if not _is_led(fp):
                continue
            led_nets = {pad.net_name for pad in fp.pads if pad.net_name}
            if not led_nets:
                continue
            if self._has_series_resistor(pcb, fp, led_nets):
                continue
            mistakes.append(
                Mistake(
                    category=MistakeCategory.CONNECTIVITY,
                    severity="warning",
                    title="LED missing series resistor",
                    components=[fp.reference],
                    location=fp.position,
                    explanation=(
                        f"{fp.reference} has no resistor sharing a net with it. "
                        "Driving an LED without a series current-limiting "
                        "resistor risks over-current damage or a shortened "
                        "lifespan, since the LED's own forward resistance is "
                        "not a reliable current limit."
                    ),
                    fix_suggestion=(
                        f"Add a series resistor in-line with {fp.reference}, "
                        "sized for the LED's rated forward current at the "
                        "supply voltage in use (unless current limiting is "
                        "already provided by a dedicated driver IC)."
                    ),
                    learn_more_url="docs/mistakes/led-series-resistor.md",
                )
            )

        return mistakes

    def _has_series_resistor(self, pcb: PCB, led: Footprint, led_nets: set[str]) -> bool:
        for fp in pcb.footprints:
            if fp is led or not fp.reference.upper().startswith("R"):
                continue
            pad_nets = {pad.net_name for pad in fp.pads if pad.net_name}
            if pad_nets & led_nets:
                return True
        return False
