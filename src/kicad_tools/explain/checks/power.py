"""Power trace width checks.

Power traces need to be wide enough to handle the current without
excessive voltage drop or overheating.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from ...router.net_class import is_switch_node_name
from ..mistakes import (
    Mistake,
    MistakeCategory,
    is_ground_net,
    is_supply_net,
    power_pin_nets,
    reference_prefix,
)

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Segment


# Minimum recommended power trace width (mm) for various currents
# Based on IPC-2221 for 1oz copper, 10°C rise
POWER_TRACE_WIDTHS = {
    0.5: 0.25,  # 0.5A -> 0.25mm
    1.0: 0.5,  # 1A -> 0.5mm
    2.0: 1.0,  # 2A -> 1.0mm
    3.0: 1.5,  # 3A -> 1.5mm
    5.0: 2.5,  # 5A -> 2.5mm
}

# Minimum power trace width to flag (mm)
MIN_POWER_TRACE_WIDTH_MM = 0.3


def decoupling_only_output_nets(pcb: PCB) -> set[str]:
    """Nets driven by a ``power_out`` pin that feed nothing but capacitors.

    An IC's internal-regulator output (the STUSB4500's ``VREG_1V2`` /
    ``VREG_2V7``, an MCU's ``VCAP``, a codec's ``LDOO``) is typed
    ``power_out`` in the schematic symbol, but on the board it only connects
    to a decoupling capacitor: nothing draws load current from it, so the
    trace-width rule for supply rails does not apply (issue #6028).

    A net qualifies when, using the symbol-derived pad ``pintype``:

    * at least one pad is a ``power_out`` pin that is not a switch node
      (a buck ``SW``/``LX`` pin drives an inductor, not a capacitor), and
    * every other pad on the net sits on a capacitor (reference prefix
      ``C``) -- so there is no ``power_in`` load, no resistor, connector,
      IC input or test point drawing current from the net.

    Ground nets never qualify.  Boards without pin-type data yield an empty
    set, so their behaviour is unchanged.
    """
    has_output: set[str] = set()
    has_load: set[str] = set()
    for fp in pcb.footprints:
        is_capacitor = reference_prefix(fp.reference) == "C"
        for pad in fp.pads:
            net = pad.net_name
            if not net or is_ground_net(net):
                continue
            pintype = getattr(pad, "pintype", "") or ""
            pinfunction = getattr(pad, "pinfunction", "") or ""
            if pintype == "power_out" and not is_switch_node_name(pinfunction or net):
                has_output.add(net)
            elif not is_capacitor:
                has_load.add(net)
    return has_output - has_load


class PowerTraceWidthCheck:
    """Check that power traces are wide enough.

    Power and ground traces need sufficient width to:
    - Handle current without excessive heating
    - Minimize voltage drop (IR drop)
    - Reduce inductance

    One finding is reported per net. It cites the *narrowest* segment below
    :data:`MIN_POWER_TRACE_WIDTH_MM` and how many segments fall below it, so
    the output does not depend on the order of segments in the file
    (issue #6028).  Nets whose only supply pin is a regulator output feeding
    nothing but capacitors (:func:`decoupling_only_output_nets`) carry no
    load current and are skipped.
    """

    category = MistakeCategory.POWER_TRACE

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check power trace widths.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for narrow power traces, one per net,
            sorted by net name
        """
        # Find power and ground nets
        evidence = power_pin_nets(pcb)
        no_load = decoupling_only_output_nets(pcb)
        power_nets = set()
        for net in pcb.nets.values():
            if net.name in no_load:
                continue
            if is_supply_net(net.name, evidence):
                power_nets.add(net.number)

        # Group the narrow segments per net
        narrow: dict[str, list[Segment]] = defaultdict(list)
        for segment in pcb.segments:
            if segment.net_number not in power_nets:
                continue
            if segment.width >= MIN_POWER_TRACE_WIDTH_MM:
                continue
            net = pcb.get_net(segment.net_number)
            net_name = net.name if net else f"Net {segment.net_number}"
            narrow[net_name].append(segment)

        mistakes: list[Mistake] = []
        for net_name in sorted(narrow):
            segments = narrow[net_name]
            # Narrowest first; ties broken by position so the reported
            # location is independent of file order too.
            worst = min(segments, key=lambda s: (s.width, s.start, s.end))
            count = len(segments)
            plural = "segment" if count == 1 else "segments"
            mistakes.append(
                Mistake(
                    category=MistakeCategory.POWER_TRACE,
                    severity="warning",
                    title="Power trace too narrow",
                    components=[net_name],
                    location=worst.start,
                    explanation=(
                        f"Power trace on {net_name} is only {worst.width:.2f}mm wide "
                        f"({count} {plural} below {MIN_POWER_TRACE_WIDTH_MM}mm). "
                        f"Narrow power traces cause voltage drop and can overheat "
                        f"under load. For 1A at 1oz copper, traces should be at least "
                        f"0.5mm wide."
                    ),
                    fix_suggestion=(
                        f"Increase trace width on {net_name} to at least "
                        f"{MIN_POWER_TRACE_WIDTH_MM}mm, or use a copper pour for "
                        f"power distribution. Consider the expected current draw."
                    ),
                    learn_more_url="docs/mistakes/power-trace-width.md",
                )
            )
        return mistakes
