"""Power trace width checks.

Power traces need to be wide enough to handle the current without
excessive voltage drop or overheating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory, is_ground_net, is_power_net

if TYPE_CHECKING:
    from ...schema.pcb import PCB


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


class PowerTraceWidthCheck:
    """Check that power traces are wide enough.

    Power and ground traces need sufficient width to:
    - Handle current without excessive heating
    - Minimize voltage drop (IR drop)
    - Reduce inductance
    """

    category = MistakeCategory.POWER_TRACE

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check power trace widths.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for narrow power traces
        """
        mistakes: list[Mistake] = []

        # Find power and ground nets
        power_nets = set()
        for net in pcb.nets.values():
            if is_power_net(net.name) or is_ground_net(net.name):
                power_nets.add(net.number)

        # Check trace widths on power nets
        for segment in pcb.segments:
            if segment.net_number not in power_nets:
                continue

            net = pcb.get_net(segment.net_number)
            net_name = net.name if net else f"Net {segment.net_number}"

            if segment.width < MIN_POWER_TRACE_WIDTH_MM:
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.POWER_TRACE,
                        severity="warning",
                        title="Power trace too narrow",
                        components=[net_name],
                        location=segment.start,
                        explanation=(
                            f"Power trace on {net_name} is only {segment.width:.2f}mm wide. "
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

        return self._deduplicate_mistakes(mistakes)

    def _deduplicate_mistakes(self, mistakes: list[Mistake]) -> list[Mistake]:
        """Reduce multiple narrow segments on same net to one mistake."""
        seen_nets: set[str] = set()
        unique_mistakes = []

        for mistake in mistakes:
            net_key = tuple(mistake.components)
            if net_key not in seen_nets:
                seen_nets.add(net_key)
                unique_mistakes.append(mistake)

        return unique_mistakes
