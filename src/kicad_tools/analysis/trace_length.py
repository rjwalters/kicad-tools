"""Trace length analysis for PCB designs.

Analyzes trace lengths for timing-critical nets, differential pairs,
and length matching. Supports automatic identification of critical nets
and skew calculation for differential pairs.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis import TraceLengthAnalyzer
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = TraceLengthAnalyzer()
    >>> reports = analyzer.analyze_all_critical(pcb)
    >>> for report in reports:
    ...     print(f"{report.net_name}: {report.total_length_mm:.2f}mm")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


# Pattern for identifying timing-critical nets
CRITICAL_NET_PATTERNS = [
    # Clock signals
    r"(?i)^CLK",
    r"(?i)CLK$",
    r"(?i)CLOCK",
    r"(?i)_CLK_",
    # USB differential pairs
    r"(?i)USB.*[DP][\+\-]?$",
    r"(?i)USB.*DATA",
    r"(?i)^D[\+\-]$",
    # High-speed serial
    r"(?i)LVDS",
    r"(?i)MIPI",
    r"(?i)HDMI",
    r"(?i)DP_",  # DisplayPort
    r"(?i)PCIE",
    r"(?i)SATA",
    # DDR memory
    r"(?i)DDR",
    r"(?i)^DQ\d",
    r"(?i)^DQS",
    r"(?i)^DM\d",
    r"(?i)^A\d+$",  # Address lines (context dependent)
    # Ethernet
    r"(?i)ETH.*[TP][\+\-]?",
    r"(?i)RGMII",
    r"(?i)RMII",
    # CAN bus
    r"(?i)CAN.*[HL]$",
]

# Patterns for identifying differential pair partners
DIFF_PAIR_PATTERNS = [
    # USB: D+/D-, USB_D+/USB_D-
    (r"(?i)^(.*)[\+P]$", r"\1-", r"\1N"),
    (r"(?i)^(.*)[_]?P$", r"\1_N", r"\1N"),
    # LVDS/MIPI: _P/_N suffixes
    (r"(?i)^(.*)_P$", r"\1_N"),
    (r"(?i)^(.*)_N$", r"\1_P"),
    # TX+/TX-, RX+/RX-
    (r"(?i)^(.*)\+$", r"\1-"),
    (r"(?i)^(.*)-$", r"\1+"),
]


@dataclass
class TraceLengthReport:
    """Report on trace length for a net.

    Attributes:
        net_name: Name of the net.
        net_class: Net class if defined (e.g., "USB", "Clock").
        total_length_mm: Total trace length in millimeters.
        segment_count: Number of trace segments.
        segment_lengths: Per-segment length breakdown.
        via_count: Number of vias in the net.
        layer_changes: Description of layer transitions (e.g., ["F.Cu → B.Cu"]).
        layers_used: Set of copper layers used by this net.
        target_length_mm: Target length if specified.
        tolerance_mm: Tolerance if specified.
        length_delta_mm: Difference from target (actual - target).
        within_tolerance: Whether length is within tolerance.
        pair_net: Name of differential pair partner if applicable.
        pair_length_mm: Length of differential pair partner.
        skew_mm: Length difference between pair members.
    """

    net_name: str
    net_class: str | None = None

    # Length measurements
    total_length_mm: float = 0.0
    segment_count: int = 0
    segment_lengths: list[float] = field(default_factory=list)
    via_count: int = 0
    layer_changes: list[str] = field(default_factory=list)
    layers_used: set[str] = field(default_factory=set)

    # Comparison to requirements
    target_length_mm: float | None = None
    tolerance_mm: float | None = None
    length_delta_mm: float | None = None
    within_tolerance: bool | None = None

    # For diff pairs
    pair_net: str | None = None
    pair_length_mm: float | None = None
    skew_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "net_name": self.net_name,
            "total_length_mm": round(self.total_length_mm, 3),
            "segment_count": self.segment_count,
            "via_count": self.via_count,
            "layers_used": sorted(self.layers_used),
        }

        if self.net_class:
            result["net_class"] = self.net_class

        if self.layer_changes:
            result["layer_changes"] = self.layer_changes

        if self.target_length_mm is not None:
            result["target_length_mm"] = self.target_length_mm
            result["length_delta_mm"] = round(self.length_delta_mm or 0, 3)
            result["within_tolerance"] = self.within_tolerance

        if self.tolerance_mm is not None:
            result["tolerance_mm"] = self.tolerance_mm

        if self.pair_net:
            result["differential_pair"] = {
                "pair_net": self.pair_net,
                "pair_length_mm": round(self.pair_length_mm or 0, 3),
                "skew_mm": round(self.skew_mm or 0, 3),
            }

        return result


@dataclass
class DifferentialPairReport:
    """Report on a differential pair.

    Attributes:
        net_p: Positive net name.
        net_n: Negative net name.
        report_p: TraceLengthReport for positive net.
        report_n: TraceLengthReport for negative net.
        skew_mm: Length difference (absolute value).
        target_skew_mm: Maximum allowed skew.
        skew_within_tolerance: Whether skew is acceptable.
    """

    net_p: str
    net_n: str
    report_p: TraceLengthReport
    report_n: TraceLengthReport
    skew_mm: float
    target_skew_mm: float | None = None
    skew_within_tolerance: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "net_positive": self.net_p,
            "net_negative": self.net_n,
            "length_positive_mm": round(self.report_p.total_length_mm, 3),
            "length_negative_mm": round(self.report_n.total_length_mm, 3),
            "skew_mm": round(self.skew_mm, 3),
        }

        if self.target_skew_mm is not None:
            result["target_skew_mm"] = self.target_skew_mm
            result["skew_within_tolerance"] = self.skew_within_tolerance

        return result


class TraceLengthAnalyzer:
    """Analyze trace lengths on a PCB.

    Calculates trace lengths for individual nets, identifies timing-critical
    nets automatically, and analyzes differential pairs for skew.

    Args:
        critical_patterns: Additional regex patterns for critical net detection.
    """

    def __init__(self, critical_patterns: list[str] | None = None):
        """Initialize the analyzer.

        Args:
            critical_patterns: Additional regex patterns for identifying
                timing-critical nets. Added to the default patterns.
        """
        self._patterns = list(CRITICAL_NET_PATTERNS)
        if critical_patterns:
            self._patterns.extend(critical_patterns)
        self._compiled_patterns = [re.compile(p) for p in self._patterns]

    def analyze_net(self, board: PCB, net_name: str) -> TraceLengthReport:
        """Calculate trace length for a specific net.

        Args:
            board: PCB object to analyze.
            net_name: Name of the net to analyze.

        Returns:
            TraceLengthReport with length measurements.
        """
        # Find net number
        net = board.get_net_by_name(net_name)
        if net is None:
            return TraceLengthReport(net_name=net_name)

        net_number = net.number

        # Calculate segment lengths
        segment_lengths: list[float] = []
        layers_used: set[str] = set()
        ordered_layers: list[str] = []

        for segment in board.segments_in_net(net_number):
            dx = segment.end[0] - segment.start[0]
            dy = segment.end[1] - segment.start[1]
            length = math.sqrt(dx * dx + dy * dy)
            segment_lengths.append(length)
            layers_used.add(segment.layer)

            # Track layer order for transitions
            if not ordered_layers or ordered_layers[-1] != segment.layer:
                ordered_layers.append(segment.layer)

        # Count vias and identify layer changes
        via_count = sum(1 for _ in board.vias_in_net(net_number))

        # Build layer change descriptions
        layer_changes: list[str] = []
        for i in range(len(ordered_layers) - 1):
            layer_changes.append(f"{ordered_layers[i]} → {ordered_layers[i + 1]}")

        total_length = sum(segment_lengths)

        return TraceLengthReport(
            net_name=net_name,
            total_length_mm=total_length,
            segment_count=len(segment_lengths),
            segment_lengths=segment_lengths,
            via_count=via_count,
            layer_changes=layer_changes,
            layers_used=layers_used,
        )

    def analyze_diff_pair(
        self,
        board: PCB,
        net_p: str,
        net_n: str,
        target_skew_mm: float | None = None,
    ) -> DifferentialPairReport:
        """Analyze a differential pair with skew calculation.

        Args:
            board: PCB object to analyze.
            net_p: Name of the positive (or first) net.
            net_n: Name of the negative (or second) net.
            target_skew_mm: Maximum allowed skew in mm.

        Returns:
            DifferentialPairReport with both net reports and skew.
        """
        report_p = self.analyze_net(board, net_p)
        report_n = self.analyze_net(board, net_n)

        skew = abs(report_p.total_length_mm - report_n.total_length_mm)

        # Update individual reports with pair info
        report_p.pair_net = net_n
        report_p.pair_length_mm = report_n.total_length_mm
        report_p.skew_mm = skew

        report_n.pair_net = net_p
        report_n.pair_length_mm = report_p.total_length_mm
        report_n.skew_mm = skew

        # Check skew tolerance
        skew_ok = None
        if target_skew_mm is not None:
            skew_ok = skew <= target_skew_mm

        return DifferentialPairReport(
            net_p=net_p,
            net_n=net_n,
            report_p=report_p,
            report_n=report_n,
            skew_mm=skew,
            target_skew_mm=target_skew_mm,
            skew_within_tolerance=skew_ok,
        )

    def analyze_all_critical(
        self,
        board: PCB,
        include_diff_pairs: bool = True,
    ) -> list[TraceLengthReport]:
        """Analyze all timing-critical nets.

        Automatically identifies critical nets by name pattern matching
        (CLK, USB, DDR, etc.) and calculates their trace lengths.

        Args:
            board: PCB object to analyze.
            include_diff_pairs: If True, include differential pair analysis.

        Returns:
            List of TraceLengthReport objects for critical nets.
        """
        critical_nets = self._identify_critical_nets(board)
        reports: list[TraceLengthReport] = []
        analyzed: set[str] = set()

        for net_name in critical_nets:
            if net_name in analyzed:
                continue

            report = self.analyze_net(board, net_name)
            analyzed.add(net_name)

            # Check for differential pair partner
            if include_diff_pairs:
                pair_name = self._find_diff_pair_partner(net_name, board)
                if pair_name and pair_name not in analyzed:
                    pair_report = self.analyze_net(board, pair_name)
                    analyzed.add(pair_name)

                    # Calculate skew
                    skew = abs(report.total_length_mm - pair_report.total_length_mm)

                    # Update both reports with pair info
                    report.pair_net = pair_name
                    report.pair_length_mm = pair_report.total_length_mm
                    report.skew_mm = skew

                    pair_report.pair_net = net_name
                    pair_report.pair_length_mm = report.total_length_mm
                    pair_report.skew_mm = skew

                    reports.append(pair_report)

            reports.append(report)

        # Sort by net name for consistent output
        reports.sort(key=lambda r: r.net_name)

        return reports

    def find_differential_pairs(self, board: PCB) -> list[tuple[str, str]]:
        """Find all differential pairs in the board.

        Identifies differential pairs by naming convention (e.g., _P/_N,
        +/-, D+/D-).

        Args:
            board: PCB object to analyze.

        Returns:
            List of (positive_net, negative_net) tuples.
        """
        net_names = {net.name for net in board.nets.values() if net.name}
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        for net_name in sorted(net_names):
            if net_name in seen:
                continue

            pair_name = self._find_diff_pair_partner(net_name, board)
            if pair_name and pair_name in net_names and pair_name not in seen:
                # Determine which is P and which is N
                if self._is_positive_net(net_name):
                    pairs.append((net_name, pair_name))
                else:
                    pairs.append((pair_name, net_name))
                seen.add(net_name)
                seen.add(pair_name)

        return pairs

    def _identify_critical_nets(self, board: PCB) -> list[str]:
        """Identify timing-critical nets by name pattern.

        Args:
            board: PCB object to analyze.

        Returns:
            List of net names matching critical patterns.
        """
        critical = []

        for net in board.nets.values():
            if not net.name:
                continue

            for pattern in self._compiled_patterns:
                if pattern.search(net.name):
                    critical.append(net.name)
                    break

        return sorted(critical)

    def _find_diff_pair_partner(self, net_name: str, board: PCB) -> str | None:
        """Find the differential pair partner for a net.

        Args:
            net_name: Name of the net to find partner for.
            board: PCB object (to verify partner exists).

        Returns:
            Partner net name if found, None otherwise.
        """
        net_names = {net.name for net in board.nets.values()}

        for pattern_tuple in DIFF_PAIR_PATTERNS:
            pattern = pattern_tuple[0]
            replacements = pattern_tuple[1:]

            match = re.match(pattern, net_name)
            if match:
                base = match.group(1)
                for replacement in replacements:
                    # Handle replacement patterns
                    if replacement.startswith(r"\1"):
                        partner = base + replacement[2:]
                    else:
                        partner = replacement.replace(r"\1", base)

                    if partner in net_names and partner != net_name:
                        return partner

        return None

    def _is_positive_net(self, net_name: str) -> bool:
        """Check if a net name indicates positive polarity.

        Args:
            net_name: Net name to check.

        Returns:
            True if net appears to be positive/P side.
        """
        return bool(re.search(r"(?i)[\+P]$", net_name) or re.search(r"(?i)_P$", net_name))
