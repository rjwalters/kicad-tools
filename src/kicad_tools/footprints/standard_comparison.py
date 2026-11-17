"""Standard footprint comparison module.

Compares footprints against KiCad's standard library to detect dimension mismatches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.sexp_file import load_footprint
from kicad_tools.sexp import SExp

from .library_path import (
    detect_kicad_library_path,
    guess_standard_library,
    parse_library_id,
)

if TYPE_CHECKING:
    from ..schema.pcb import Footprint as PCBFootprint
    from ..schema.pcb import Pad as PCBPad


class ComparisonSeverity(Enum):
    """Severity level for comparison issues."""

    ERROR = "error"  # Significant mismatch that likely causes problems
    WARNING = "warning"  # Notable difference worth investigating
    INFO = "info"  # Minor difference, likely acceptable


class ComparisonType(Enum):
    """Types of comparison issues."""

    PAD_POSITION_MISMATCH = "pad_position_mismatch"
    PAD_SIZE_MISMATCH = "pad_size_mismatch"
    PAD_SHAPE_MISMATCH = "pad_shape_mismatch"
    PAD_COUNT_MISMATCH = "pad_count_mismatch"
    PAD_NOT_FOUND = "pad_not_found"
    STANDARD_NOT_FOUND = "standard_not_found"


@dataclass
class PadComparison:
    """Result of comparing a single pad against the standard."""

    pad_number: str
    comparison_type: ComparisonType
    severity: ComparisonSeverity
    message: str
    our_value: str | tuple[float, float] | None = None
    standard_value: str | tuple[float, float] | None = None
    delta: float | tuple[float, float] | None = None
    delta_percent: float | None = None


@dataclass
class FootprintComparison:
    """Result of comparing a footprint against the standard library."""

    footprint_ref: str
    footprint_name: str
    standard_library: str | None
    standard_footprint: str | None
    found_standard: bool
    pad_comparisons: list[PadComparison] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        """Whether any comparison issues were found."""
        return len(self.pad_comparisons) > 0

    @property
    def error_count(self) -> int:
        """Number of error-level issues."""
        return sum(1 for p in self.pad_comparisons if p.severity == ComparisonSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        """Number of warning-level issues."""
        return sum(1 for p in self.pad_comparisons if p.severity == ComparisonSeverity.WARNING)

    @property
    def matches_standard(self) -> bool:
        """Whether the footprint matches the standard (no errors or warnings)."""
        return self.found_standard and self.error_count == 0 and self.warning_count == 0


@dataclass
class StandardPad:
    """Pad data extracted from a standard library footprint."""

    number: str
    type: str  # smd, thru_hole
    shape: str  # roundrect, rect, circle, oval
    position: tuple[float, float]  # (x, y) in mm
    size: tuple[float, float]  # (width, height) in mm
    rotation: float = 0.0


@dataclass
class StandardFootprint:
    """A footprint loaded from the standard library."""

    name: str
    library: str
    path: Path
    pads: list[StandardPad] = field(default_factory=list)

    def get_pad(self, number: str) -> StandardPad | None:
        """Get a pad by number."""
        for pad in self.pads:
            if pad.number == number:
                return pad
        return None


class StandardFootprintComparator:
    """Compares footprints against KiCad standard library.

    Detects dimension mismatches that could cause soldering issues
    or other manufacturing problems.

    Example::

        from kicad_tools.footprints.standard_comparison import StandardFootprintComparator
        from kicad_tools.schema import PCB

        pcb = PCB.load("board.kicad_pcb")
        comparator = StandardFootprintComparator()

        for footprint in pcb.footprints:
            result = comparator.compare_footprint(footprint)
            if result.has_issues:
                print(f"{footprint.reference}: {result.error_count} errors")
    """

    def __init__(
        self,
        tolerance_mm: float = 0.05,
        library_path: str | Path | None = None,
        library_mappings: dict[str, str] | None = None,
    ):
        """Initialize the comparator.

        Args:
            tolerance_mm: Maximum allowed deviation in mm (default: 0.05mm)
            library_path: Override path to KiCad footprints directory
            library_mappings: Custom mappings from footprint names to library paths
        """
        self.tolerance_mm = tolerance_mm
        self.library_mappings = library_mappings or {}
        self._library_paths = detect_kicad_library_path(library_path)
        self._cache: dict[str, StandardFootprint | None] = {}

    @property
    def library_found(self) -> bool:
        """Whether the KiCad standard library was found."""
        return self._library_paths.found

    @property
    def library_path(self) -> Path | None:
        """Path to the KiCad footprints directory."""
        return self._library_paths.footprints_path

    def load_standard_footprint(
        self, footprint_name: str, library_override: str | None = None
    ) -> StandardFootprint | None:
        """Load a footprint from the standard library.

        Args:
            footprint_name: The footprint name (e.g., "C_0402_1005Metric")
            library_override: Optional library name to use instead of guessing

        Returns:
            StandardFootprint if found and loaded, None otherwise.
        """
        cache_key = f"{library_override or ''}:{footprint_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if not self._library_paths.found:
            return None

        # Determine library name
        library_name = library_override

        # Check custom mappings first
        if not library_name and footprint_name in self.library_mappings:
            library_name = self.library_mappings[footprint_name]

        # Try to guess from naming conventions
        if not library_name:
            library_name = guess_standard_library(footprint_name)

        if not library_name:
            self._cache[cache_key] = None
            return None

        # Find the footprint file
        fp_path = self._library_paths.get_footprint_file(library_name, footprint_name)
        if not fp_path:
            self._cache[cache_key] = None
            return None

        # Load and parse the footprint
        try:
            standard = self._parse_footprint_file(fp_path, footprint_name, library_name)
            self._cache[cache_key] = standard
            return standard
        except Exception:
            self._cache[cache_key] = None
            return None

    def _parse_footprint_file(
        self, path: Path, footprint_name: str, library_name: str
    ) -> StandardFootprint:
        """Parse a .kicad_mod file into a StandardFootprint."""
        sexp = load_footprint(path)

        pads = []
        for pad_sexp in sexp.find_children("pad"):
            pad = self._parse_pad(pad_sexp)
            if pad:
                pads.append(pad)

        return StandardFootprint(
            name=footprint_name,
            library=library_name,
            path=path,
            pads=pads,
        )

    def _parse_pad(self, sexp: SExp) -> StandardPad | None:
        """Parse a pad from S-expression."""
        if not sexp.values:
            return None

        number = str(sexp.values[0]) if sexp.values else ""
        pad_type = str(sexp.values[1]) if len(sexp.values) > 1 else ""
        shape = str(sexp.values[2]) if len(sexp.values) > 2 else ""

        position = (0.0, 0.0)
        rotation = 0.0
        if at := sexp.find_child("at"):
            x = float(at.values[0]) if at.values else 0.0
            y = float(at.values[1]) if len(at.values) > 1 else 0.0
            rotation = float(at.values[2]) if len(at.values) > 2 else 0.0
            position = (x, y)

        size = (0.0, 0.0)
        if size_node := sexp.find_child("size"):
            w = float(size_node.values[0]) if size_node.values else 0.0
            h = float(size_node.values[1]) if len(size_node.values) > 1 else w
            size = (w, h)

        return StandardPad(
            number=number,
            type=pad_type,
            shape=shape,
            position=position,
            size=size,
            rotation=rotation,
        )

    def compare_footprint(
        self,
        pcb_footprint: PCBFootprint,
        library_override: str | None = None,
    ) -> FootprintComparison:
        """Compare a PCB footprint against the standard library.

        Args:
            pcb_footprint: The footprint from the PCB to compare
            library_override: Optional library name to use

        Returns:
            FootprintComparison with all comparison results.
        """
        # Parse the footprint name to extract library if present
        lib_name, fp_name = parse_library_id(pcb_footprint.name)
        library = library_override or lib_name

        # Try to load the standard footprint
        standard = self.load_standard_footprint(fp_name, library)

        if not standard:
            return FootprintComparison(
                footprint_ref=pcb_footprint.reference,
                footprint_name=pcb_footprint.name,
                standard_library=library,
                standard_footprint=fp_name,
                found_standard=False,
                pad_comparisons=[
                    PadComparison(
                        pad_number="*",
                        comparison_type=ComparisonType.STANDARD_NOT_FOUND,
                        severity=ComparisonSeverity.INFO,
                        message=f"Standard footprint not found for '{fp_name}'",
                    )
                ],
            )

        # Compare pads
        pad_comparisons = self._compare_pads(pcb_footprint.pads, standard)

        return FootprintComparison(
            footprint_ref=pcb_footprint.reference,
            footprint_name=pcb_footprint.name,
            standard_library=standard.library,
            standard_footprint=standard.name,
            found_standard=True,
            pad_comparisons=pad_comparisons,
        )

    def _compare_pads(
        self, pcb_pads: list[PCBPad], standard: StandardFootprint
    ) -> list[PadComparison]:
        """Compare PCB pads against standard footprint pads."""
        comparisons: list[PadComparison] = []

        # Check pad count
        if len(pcb_pads) != len(standard.pads):
            comparisons.append(
                PadComparison(
                    pad_number="*",
                    comparison_type=ComparisonType.PAD_COUNT_MISMATCH,
                    severity=ComparisonSeverity.WARNING,
                    message=f"Pad count mismatch: {len(pcb_pads)} vs {len(standard.pads)} standard",
                    our_value=str(len(pcb_pads)),
                    standard_value=str(len(standard.pads)),
                )
            )

        # Compare each pad
        for pcb_pad in pcb_pads:
            std_pad = standard.get_pad(pcb_pad.number)

            if not std_pad:
                # Pad exists in PCB but not in standard - skip, could be aux pad
                continue

            # Compare position
            pos_comparison = self._compare_position(pcb_pad, std_pad)
            if pos_comparison:
                comparisons.append(pos_comparison)

            # Compare size
            size_comparison = self._compare_size(pcb_pad, std_pad)
            if size_comparison:
                comparisons.append(size_comparison)

            # Compare shape
            shape_comparison = self._compare_shape(pcb_pad, std_pad)
            if shape_comparison:
                comparisons.append(shape_comparison)

        return comparisons

    def _compare_position(self, pcb_pad: PCBPad, std_pad: StandardPad) -> PadComparison | None:
        """Compare pad positions."""
        dx = abs(pcb_pad.position[0] - std_pad.position[0])
        dy = abs(pcb_pad.position[1] - std_pad.position[1])
        distance = math.sqrt(dx * dx + dy * dy)

        if distance <= self.tolerance_mm:
            return None

        # Calculate percentage difference (relative to standard position distance from origin)
        std_dist = math.sqrt(std_pad.position[0] ** 2 + std_pad.position[1] ** 2)
        delta_percent = (distance / std_dist * 100) if std_dist > 0 else 0

        severity = ComparisonSeverity.WARNING
        if delta_percent > 30 or distance > 0.3:
            severity = ComparisonSeverity.ERROR

        return PadComparison(
            pad_number=pcb_pad.number,
            comparison_type=ComparisonType.PAD_POSITION_MISMATCH,
            severity=severity,
            message=(
                f"Pad {pcb_pad.number} position mismatch: "
                f"({pcb_pad.position[0]:.3f}, {pcb_pad.position[1]:.3f}) vs "
                f"({std_pad.position[0]:.3f}, {std_pad.position[1]:.3f}) standard"
            ),
            our_value=pcb_pad.position,
            standard_value=std_pad.position,
            delta=(dx, dy),
            delta_percent=delta_percent,
        )

    def _compare_size(self, pcb_pad: PCBPad, std_pad: StandardPad) -> PadComparison | None:
        """Compare pad sizes."""
        dw = abs(pcb_pad.size[0] - std_pad.size[0])
        dh = abs(pcb_pad.size[1] - std_pad.size[1])

        if dw <= self.tolerance_mm and dh <= self.tolerance_mm:
            return None

        # Calculate percentage difference
        std_area = std_pad.size[0] * std_pad.size[1]
        our_area = pcb_pad.size[0] * pcb_pad.size[1]
        area_diff_percent = abs(our_area - std_area) / std_area * 100 if std_area > 0 else 0

        severity = ComparisonSeverity.WARNING
        if area_diff_percent > 25:
            severity = ComparisonSeverity.ERROR

        return PadComparison(
            pad_number=pcb_pad.number,
            comparison_type=ComparisonType.PAD_SIZE_MISMATCH,
            severity=severity,
            message=(
                f"Pad {pcb_pad.number} size mismatch: "
                f"({pcb_pad.size[0]:.3f}, {pcb_pad.size[1]:.3f}) vs "
                f"({std_pad.size[0]:.3f}, {std_pad.size[1]:.3f}) standard"
            ),
            our_value=pcb_pad.size,
            standard_value=std_pad.size,
            delta=(dw, dh),
            delta_percent=area_diff_percent,
        )

    def _compare_shape(self, pcb_pad: PCBPad, std_pad: StandardPad) -> PadComparison | None:
        """Compare pad shapes."""
        if pcb_pad.shape == std_pad.shape:
            return None

        return PadComparison(
            pad_number=pcb_pad.number,
            comparison_type=ComparisonType.PAD_SHAPE_MISMATCH,
            severity=ComparisonSeverity.INFO,
            message=(
                f"Pad {pcb_pad.number} shape mismatch: "
                f"'{pcb_pad.shape}' vs '{std_pad.shape}' standard"
            ),
            our_value=pcb_pad.shape,
            standard_value=std_pad.shape,
        )

    def compare_pcb(
        self,
        pcb,  # PCB type
        progress_callback=None,
    ) -> list[FootprintComparison]:
        """Compare all footprints in a PCB against standard library.

        Args:
            pcb: The PCB to compare
            progress_callback: Optional callback(current, total) for progress

        Returns:
            List of FootprintComparison for all footprints.
        """
        results = []
        footprints = list(pcb.footprints)
        total = len(footprints)

        for i, footprint in enumerate(footprints):
            if progress_callback:
                progress_callback(i + 1, total)

            result = self.compare_footprint(footprint)
            results.append(result)

        return results

    def summarize(self, comparisons: list[FootprintComparison]) -> dict:
        """Generate a summary of comparison results.

        Args:
            comparisons: List of comparison results

        Returns:
            Summary dict with counts and statistics.
        """
        total_checked = len(comparisons)
        found_standard = sum(1 for c in comparisons if c.found_standard)
        with_issues = sum(1 for c in comparisons if c.has_issues and c.found_standard)
        matching = sum(1 for c in comparisons if c.matches_standard)

        total_errors = sum(c.error_count for c in comparisons)
        total_warnings = sum(c.warning_count for c in comparisons)

        # Group by footprint name
        by_footprint_name: dict[str, int] = {}
        for c in comparisons:
            if c.has_issues:
                name = c.footprint_name
                by_footprint_name[name] = by_footprint_name.get(name, 0) + 1

        return {
            "total_checked": total_checked,
            "found_standard": found_standard,
            "not_found": total_checked - found_standard,
            "with_issues": with_issues,
            "matching_standard": matching,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "by_footprint_name": by_footprint_name,
        }
