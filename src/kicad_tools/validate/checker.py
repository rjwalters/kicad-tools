"""Pure Python DRC checker.

This module provides the main DRCChecker class that performs Design Rule
Checks on PCB designs without requiring kicad-cli.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.manufacturers import DesignRules, get_profile

from .rules.clearance import ClearanceRule
from .rules.edge import EdgeClearanceRule
from .rules.silkscreen import check_all_silkscreen
from .violations import DRCResults

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class DRCChecker:
    """Pure Python DRC checker for PCB validation.

    Validates PCB designs against manufacturer design rules without
    requiring kicad-cli to be installed.

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.validate import DRCChecker
        >>>
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
        >>> results = checker.check_all()
        >>>
        >>> if results.passed:
        ...     print("DRC passed!")
        >>> else:
        ...     for violation in results.errors:
        ...         print(f"{violation.severity}: {violation.message}")

    Attributes:
        pcb: The PCB being checked
        design_rules: Design rules from the manufacturer profile
        manufacturer: Manufacturer ID string
        layers: Number of PCB layers
    """

    def __init__(
        self,
        pcb: PCB,
        manufacturer: str = "jlcpcb",
        layers: int = 4,
        copper_oz: float = 1.0,
    ) -> None:
        """Initialize the DRC checker.

        Args:
            pcb: The PCB to check
            manufacturer: Manufacturer ID (e.g., "jlcpcb", "oshpark")
            layers: Number of PCB layers (2, 4, 6, etc.)
            copper_oz: Copper weight in oz

        Raises:
            ValueError: If manufacturer ID is not recognized
        """
        self.pcb = pcb
        self.manufacturer = manufacturer
        self.layers = layers
        self.copper_oz = copper_oz

        # Load manufacturer profile and design rules
        profile = get_profile(manufacturer)
        self.design_rules: DesignRules = profile.get_design_rules(layers, copper_oz)

    def check_all(self) -> DRCResults:
        """Run all DRC checks.

        Returns:
            DRCResults containing all violations found
        """
        results = DRCResults()

        # Run each category of checks
        results.merge(self.check_clearances())
        results.merge(self.check_dimensions())
        results.merge(self.check_edge_clearances())
        results.merge(self.check_silkscreen())

        return results

    def check_clearances(self) -> DRCResults:
        """Check clearance rules (trace-to-trace, trace-to-pad, etc.).

        Validates spacing between copper elements on the same layer
        but different nets against the manufacturer's minimum clearance.

        Returns:
            DRCResults containing clearance violations
        """
        rule = ClearanceRule()
        return rule.check(self.pcb, self.design_rules)

    def check_dimensions(self) -> DRCResults:
        """Check dimension rules (trace width, via drill, annular ring).

        Validates:
        - Minimum trace width
        - Minimum via drill diameter
        - Minimum via outer diameter
        - Minimum annular ring
        - Drill-to-drill clearance

        Returns:
            DRCResults containing dimension violations
        """
        from .rules.dimensions import DimensionRules

        rule = DimensionRules()
        return rule.check(self.pcb, self.design_rules)

    def check_edge_clearances(self) -> DRCResults:
        """Check edge clearance rules (copper-to-board-edge).

        Validates that all copper elements (traces, pads, zones) and holes
        (vias, through-hole pads) maintain minimum clearance from the board
        edge as specified by manufacturer design rules.

        Returns:
            DRCResults containing edge clearance violations
        """
        rule = EdgeClearanceRule()
        return rule.check(self.pcb, self.design_rules)

    def check_silkscreen(self) -> DRCResults:
        """Check silkscreen rules (line width, text height, over-pad).

        Validates:
        - Minimum silkscreen line width
        - Minimum silkscreen text height
        - Silkscreen elements overlapping exposed pads

        Returns:
            DRCResults containing silkscreen violations
        """
        return check_all_silkscreen(self.pcb, self.design_rules)

    def __repr__(self) -> str:
        return (
            f"DRCChecker(manufacturer={self.manufacturer!r}, "
            f"layers={self.layers}, copper_oz={self.copper_oz})"
        )
