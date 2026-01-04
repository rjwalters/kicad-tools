"""BOM-to-PCB placement verification.

Verifies that all BOM components are placed on the PCB and identifies
unplaced or missing parts.

Example:
    >>> from kicad_tools.schema.bom import extract_bom
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.validate.placement import BOMPlacementVerifier
    >>>
    >>> bom = extract_bom("project.kicad_sch")
    >>> pcb = PCB.load("project.kicad_pcb")
    >>> verifier = BOMPlacementVerifier(bom, pcb)
    >>> result = verifier.verify()
    >>>
    >>> for status in result.unplaced:
    ...     print(f"{status.reference}: {status.issues}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.bom import BOM
    from kicad_tools.schema.pcb import PCB


# Default threshold for considering a component at origin as "unplaced"
# Components within this distance from (0,0) are flagged as potentially unplaced
ORIGIN_THRESHOLD = 0.1  # mm


@dataclass(frozen=True)
class PlacementStatus:
    """Placement status for a BOM item.

    Attributes:
        reference: Component reference designator (e.g., "R1", "U3")
        value: Component value (e.g., "10k", "STM32F103")
        footprint: Footprint name (e.g., "0402", "TSSOP-20")
        in_bom: Whether component is in the BOM
        in_pcb: Whether component exists in the PCB file
        is_placed: Whether component has a valid position on the board
        position: Component position as (x, y) tuple, or None if not in PCB
        layer: Layer the component is on, or None if not in PCB
        issues: List of issues found with this component
    """

    reference: str
    value: str
    footprint: str
    in_bom: bool
    in_pcb: bool
    is_placed: bool
    position: tuple[float, float] | None
    layer: str | None
    issues: tuple[str, ...]  # Use tuple for hashability (frozen dataclass)

    @property
    def has_issues(self) -> bool:
        """Check if this component has any issues."""
        return len(self.issues) > 0

    @property
    def is_error(self) -> bool:
        """Check if this is an error (missing or not placed)."""
        return not self.in_pcb or not self.is_placed

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (has issues but is placed)."""
        return self.has_issues and self.is_placed

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "reference": self.reference,
            "value": self.value,
            "footprint": self.footprint,
            "in_bom": self.in_bom,
            "in_pcb": self.in_pcb,
            "is_placed": self.is_placed,
            "position": list(self.position) if self.position else None,
            "layer": self.layer,
            "issues": list(self.issues),
        }


@dataclass
class PlacementResult:
    """Aggregates all placement verification results.

    Provides convenient filtering and counting methods.
    """

    statuses: list[PlacementStatus] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        """Total number of components checked."""
        return len(self.statuses)

    @property
    def placed_count(self) -> int:
        """Number of components that are placed."""
        return sum(1 for s in self.statuses if s.is_placed)

    @property
    def unplaced_count(self) -> int:
        """Number of components that are not placed or missing."""
        return sum(1 for s in self.statuses if not s.is_placed)

    @property
    def missing_count(self) -> int:
        """Number of components in BOM but not in PCB."""
        return sum(1 for s in self.statuses if s.in_bom and not s.in_pcb)

    @property
    def all_placed(self) -> bool:
        """True if all components are placed."""
        return self.unplaced_count == 0

    @property
    def placed(self) -> list[PlacementStatus]:
        """List of placed components."""
        return [s for s in self.statuses if s.is_placed]

    @property
    def unplaced(self) -> list[PlacementStatus]:
        """List of unplaced or missing components."""
        return [s for s in self.statuses if not s.is_placed]

    @property
    def missing(self) -> list[PlacementStatus]:
        """List of components in BOM but not in PCB."""
        return [s for s in self.statuses if s.in_bom and not s.in_pcb]

    @property
    def at_origin(self) -> list[PlacementStatus]:
        """List of components at or near origin (possibly unplaced)."""
        return [s for s in self.statuses if not s.is_placed and s.in_pcb]

    def __iter__(self):
        """Iterate over all statuses."""
        return iter(self.statuses)

    def __len__(self) -> int:
        """Total number of statuses."""
        return len(self.statuses)

    def __bool__(self) -> bool:
        """True if there are any statuses."""
        return len(self.statuses) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "all_placed": self.all_placed,
            "total": self.total_count,
            "placed": self.placed_count,
            "unplaced": self.unplaced_count,
            "missing": self.missing_count,
            "statuses": [s.to_dict() for s in self.statuses],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "ALL PLACED" if self.all_placed else "INCOMPLETE"
        parts = [f"BOM ↔ Placement {status}: {self.placed_count}/{self.total_count} placed"]

        if self.missing_count > 0:
            parts.append(f"  Missing from PCB: {self.missing_count}")
        if at_origin := len(self.at_origin):
            parts.append(f"  At origin (unplaced): {at_origin}")

        return "\n".join(parts)


class BOMPlacementVerifier:
    """Verify BOM items are placed on PCB.

    Checks that:
    - All BOM components exist in the PCB file
    - Components have valid positions (not at origin)
    - Reports placement status for each component

    Example:
        >>> bom = extract_bom("project.kicad_sch")
        >>> pcb = PCB.load("project.kicad_pcb")
        >>> verifier = BOMPlacementVerifier(bom, pcb)
        >>> result = verifier.verify()
        >>>
        >>> if not result.all_placed:
        ...     for status in result.unplaced:
        ...         print(f"{status.reference}: {status.issues}")

    Attributes:
        bom: The BOM to verify
        pcb: The PCB to check against
        origin_threshold: Distance from origin to consider as "unplaced"
    """

    def __init__(
        self,
        bom: str | Path | BOM,
        pcb: str | Path | PCB,
        origin_threshold: float = ORIGIN_THRESHOLD,
    ) -> None:
        """Initialize the verifier.

        Args:
            bom: Path to schematic file or BOM object
            pcb: Path to PCB file or PCB object
            origin_threshold: Distance from origin to consider as "unplaced"
        """
        from kicad_tools.schema.bom import extract_bom
        from kicad_tools.schema.pcb import PCB as PCBClass

        # Load BOM if path provided
        if isinstance(bom, (str, Path)):
            self.bom = extract_bom(str(bom))
        else:
            self.bom = bom

        # Load PCB if path provided
        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb

        self.origin_threshold = origin_threshold

    def verify(self) -> PlacementResult:
        """Check placement status of all BOM items.

        Returns:
            PlacementResult containing status for each component
        """
        statuses: list[PlacementStatus] = []

        # Build lookup of PCB footprints by reference
        pcb_footprints = {fp.reference: fp for fp in self.pcb.footprints}

        # Check each BOM item
        for item in self.bom.items:
            # Skip virtual/power components
            if item.is_virtual:
                continue

            # Skip DNP components
            if item.dnp:
                continue

            footprint = pcb_footprints.get(item.reference)

            issues: list[str] = []
            position: tuple[float, float] | None = None
            layer: str | None = None
            is_placed = False

            if footprint:
                position = footprint.position
                layer = footprint.layer

                if self._is_at_origin(footprint.position):
                    issues.append("Component at origin (not placed on board)")
                    is_placed = False
                else:
                    is_placed = True
            else:
                issues.append("Component missing from PCB")

            status = PlacementStatus(
                reference=item.reference,
                value=item.value,
                footprint=item.footprint,
                in_bom=True,
                in_pcb=footprint is not None,
                is_placed=is_placed,
                position=position,
                layer=layer,
                issues=tuple(issues),
            )
            statuses.append(status)

        # Sort by reference for consistent output
        statuses.sort(key=lambda s: self._sort_key(s.reference))

        return PlacementResult(statuses=statuses)

    def get_unplaced(self) -> list[PlacementStatus]:
        """Get only unplaced components.

        Convenience method that returns just the unplaced components.

        Returns:
            List of PlacementStatus for unplaced components
        """
        return self.verify().unplaced

    def _is_at_origin(self, position: tuple[float, float]) -> bool:
        """Check if position is at or near origin (unplaced).

        Components at (0, 0) or very close to it are typically unplaced.

        Args:
            position: (x, y) position in mm

        Returns:
            True if position is at or near origin
        """
        if position is None:
            return True

        x, y = position
        return abs(x) < self.origin_threshold and abs(y) < self.origin_threshold

    @staticmethod
    def _sort_key(reference: str) -> tuple[str, int]:
        """Generate sort key for reference designators.

        Sorts by prefix (letter) first, then by number.
        E.g., C1, C2, C10, R1, R2, U1

        Args:
            reference: Reference designator (e.g., "R1", "U23")

        Returns:
            Tuple of (prefix, number) for sorting
        """
        import re

        match = re.match(r"([A-Za-z_#]+)(\d*)", reference)
        if match:
            prefix = match.group(1)
            number = int(match.group(2)) if match.group(2) else 0
            return (prefix, number)
        return (reference, 0)

    def __repr__(self) -> str:
        """Return string representation."""
        bom_count = len(self.bom.items) if self.bom else 0
        pcb_count = self.pcb.footprint_count if self.pcb else 0
        return f"BOMPlacementVerifier(bom_items={bom_count}, pcb_footprints={pcb_count})"
