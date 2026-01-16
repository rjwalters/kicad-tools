"""Differential pair routing checks.

Differential pairs (USB, Ethernet, LVDS, etc.) require:
- Matched trace lengths to maintain signal timing
- Consistent spacing for impedance control
- Proper termination
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import (
    Mistake,
    MistakeCategory,
    is_differential_pair_net,
    trace_length,
)

if TYPE_CHECKING:
    from ...schema.pcb import PCB


# Maximum length mismatch for USB 2.0 high-speed (mm)
MAX_USB_SKEW_MM = 2.0

# Maximum length mismatch for general differential pairs (mm)
MAX_GENERAL_SKEW_MM = 5.0


class DifferentialPairSkewCheck:
    """Check differential pair length matching.

    Differential pairs require matched trace lengths to maintain proper
    signal timing. USB 2.0 high-speed, for example, requires < 2mm
    length difference to maintain signal integrity.
    """

    category = MistakeCategory.DIFFERENTIAL_PAIR

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check differential pair length matching.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for mismatched differential pairs
        """
        mistakes: list[Mistake] = []

        # Find all differential pair nets
        diff_pairs = self._find_differential_pairs(pcb)

        for pair_base, (pos_net, neg_net) in diff_pairs.items():
            pos_segments = self._get_net_segments(pcb, pos_net)
            neg_segments = self._get_net_segments(pcb, neg_net)

            if not pos_segments or not neg_segments:
                continue

            pos_length = trace_length(pos_segments)
            neg_length = trace_length(neg_segments)
            skew = abs(pos_length - neg_length)

            # Determine max allowed skew based on interface type
            is_usb = "USB" in pair_base.upper()
            max_skew = MAX_USB_SKEW_MM if is_usb else MAX_GENERAL_SKEW_MM
            interface = "USB 2.0 high-speed" if is_usb else "differential"

            if skew > max_skew:
                longer = pos_net if pos_length > neg_length else neg_net
                shorter = neg_net if pos_length > neg_length else pos_net

                mistakes.append(
                    Mistake(
                        category=MistakeCategory.DIFFERENTIAL_PAIR,
                        severity="error" if is_usb else "warning",
                        title="Differential pair length mismatch",
                        components=[pos_net, neg_net],
                        location=None,
                        explanation=(
                            f"{pos_net} is {pos_length:.1f}mm, {neg_net} is {neg_length:.1f}mm "
                            f"({skew:.1f}mm difference). {interface.title()} requires "
                            f"< {max_skew}mm length difference to maintain signal timing. "
                            f"Mismatched lengths cause timing skew that degrades signal quality."
                        ),
                        fix_suggestion=(
                            f"Add serpentine to {shorter} to match {longer} length, "
                            f"or shorten the {longer} routing path. Target: {max(pos_length, neg_length):.1f}mm "
                            f"for both traces."
                        ),
                        learn_more_url="docs/mistakes/differential-pair-matching.md",
                    )
                )
            elif skew > max_skew * 0.5:
                # Warning for marginal cases
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.DIFFERENTIAL_PAIR,
                        severity="info",
                        title="Differential pair length matching could be improved",
                        components=[pos_net, neg_net],
                        location=None,
                        explanation=(
                            f"{pos_net}/{neg_net} have {skew:.1f}mm length difference. "
                            f"While within spec ({max_skew}mm max), better matching "
                            f"improves signal integrity margin."
                        ),
                        fix_suggestion=(
                            f"Consider adding serpentine to improve length matching. "
                            f"Current: {pos_length:.1f}mm / {neg_length:.1f}mm."
                        ),
                        learn_more_url="docs/mistakes/differential-pair-matching.md",
                    )
                )

        return mistakes

    def _find_differential_pairs(self, pcb: PCB) -> dict[str, tuple[str, str]]:
        """Find differential pair nets in the PCB.

        Returns:
            Dictionary mapping pair base name to (positive_net, negative_net)
        """
        pairs: dict[str, list[str]] = {}

        for net in pcb.nets.values():
            is_diff, base = is_differential_pair_net(net.name)
            if is_diff and base:
                if base not in pairs:
                    pairs[base] = []
                pairs[base].append(net.name)

        # Filter to only complete pairs
        complete_pairs = {}
        for base, nets in pairs.items():
            if len(nets) == 2:
                # Sort so positive is first
                nets_sorted = sorted(
                    nets,
                    key=lambda n: (
                        "_P" in n.upper()
                        or "+" in n
                        or n.upper().endswith("P")
                        or "DP" in n.upper()
                    ),
                    reverse=True,
                )
                complete_pairs[base] = (nets_sorted[0], nets_sorted[1])

        return complete_pairs

    def _get_net_segments(self, pcb: PCB, net_name: str) -> list:
        """Get all trace segments for a net."""
        net = pcb.get_net_by_name(net_name)
        if not net:
            return []
        return [seg for seg in pcb.segments if seg.net_number == net.number]
