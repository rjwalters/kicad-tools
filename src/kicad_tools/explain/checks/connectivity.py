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

from ..mistakes import Mistake, MistakeCategory, is_ground_net, is_power_net

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
    shortens its life or destroys it outright.

    Path rule (issue #5940, replacing the #4899/#5316 two-terminal-junction
    test): a two-net LED is current-limited when, on its anode side or its
    cathode side, every DC path from the LED to a rail or a driver pin goes
    through a resistor. Concretely, starting from one LED terminal net we
    walk the net graph, crossing other two-net diodes/LEDs and inductors /
    ferrite beads (DC-conductive series parts) and ignoring capacitors and
    test points. The side is protected when that walk reaches no rail net
    and no other component pad (IC, connector, transistor, ...), and stops
    at at least one resistor whose far side leaves the LED's own
    neighbourhood (so a resistor in parallel with the LED, or shorted onto
    one net, does not count).

    Shared resistors -- one line resistor per charlieplex line, or one
    resistor feeding several LEDs -- therefore pass, while an LED whose net
    also touches a GPIO/connector pin directly (a branch that bypasses the
    resistor) is still flagged. Constant-current drivers wired straight to
    the LED remain warnings because this PCB-only check cannot establish
    their current-limiting behaviour.
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
                        f"{fp.reference} has no confirmed series resistor in its net topology. "
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
        if len(led_nets) != 2:
            return False
        net_index = _pads_by_net(pcb)
        first, second = sorted(led_nets)
        side_a = _explore_side(net_index, led, first, stop=second)
        side_b = _explore_side(net_index, led, second, stop=first)
        neighbourhood = side_a.nets | side_b.nets | led_nets
        return side_a.is_protected(neighbourhood) or side_b.is_protected(neighbourhood)


# Two-net parts that conduct DC and so extend a path rather than end it.
_SERIES_PREFIXES = ("LED", "D", "DS", "L", "FB", "F")
# Parts that never sit in a DC path to a source (decoupling, test access).
_IGNORED_PREFIXES = ("C", "TP", "MH", "H")


def _ref_prefix(ref: str) -> str:
    match = re.match(r"[A-Za-z_]+", ref)
    return match.group(0).upper() if match else ""


def _pads_by_net(pcb: PCB) -> dict[str, list[Footprint]]:
    """Map net name -> footprints with a pad on it (one entry per pad)."""
    index: dict[str, list[Footprint]] = {}
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name:
                index.setdefault(pad.net_name, []).append(fp)
    return index


class _SideWalk:
    """Result of walking the net graph out from one LED terminal."""

    def __init__(self) -> None:
        self.nets: set[str] = set()
        self.unprotected = False
        # Far-side nets of each resistor the walk stopped at.
        self.resistor_far_nets: list[set[str]] = []

    def is_protected(self, neighbourhood: set[str]) -> bool:
        if self.unprotected:
            return False
        return any(far - neighbourhood for far in self.resistor_far_nets)


def _explore_side(
    index: dict[str, list[Footprint]], led: Footprint, start: str, stop: str
) -> _SideWalk:
    """Walk out from *start*, never entering the LED's other terminal *stop*.

    A path that loops back to the LED's own other terminal (a second LED in
    parallel, an anti-parallel charlieplex partner) is not a path to a
    source; whatever lies beyond *stop* belongs to the other side's walk.
    """
    walk = _SideWalk()
    queue = [start]
    seen_parts: set[int] = {id(led)}
    while queue:
        net = queue.pop()
        if net in walk.nets or net == stop:
            continue
        walk.nets.add(net)
        if is_power_net(net) or is_ground_net(net):
            walk.unprotected = True
            continue
        for fp in index.get(net, []):
            if fp is led:
                continue
            prefix = _ref_prefix(fp.reference)
            fp_nets = {pad.net_name for pad in fp.pads if pad.net_name}
            if prefix.startswith("R"):
                if id(fp) not in seen_parts:
                    seen_parts.add(id(fp))
                    walk.resistor_far_nets.append(fp_nets - {net})
                continue
            if prefix in _IGNORED_PREFIXES:
                continue
            if prefix in _SERIES_PREFIXES and len(fp_nets) == 2:
                queue.extend(fp_nets - walk.nets)
                continue
            # IC, connector, transistor, switch, ...: a driver pin reached
            # without passing through a resistor.
            walk.unprotected = True
    return walk
