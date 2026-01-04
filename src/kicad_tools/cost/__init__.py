"""
Manufacturing cost estimation for KiCad PCB designs.

Provides tools for estimating PCB fabrication, component, and assembly costs
based on manufacturer pricing models.

Usage:
    from kicad_tools.cost import ManufacturingCostEstimator, CostEstimate

    estimator = ManufacturingCostEstimator(manufacturer="jlcpcb")
    estimate = estimator.estimate(pcb, bom, quantity=100)
    print(f"Total: ${estimate.total_per_unit:.2f}/unit")

    # Check part availability
    from kicad_tools.cost import LCSCAvailabilityChecker

    checker = LCSCAvailabilityChecker()
    availability = checker.check_bom(bom, quantity=100)
    print(f"Available: {len(availability.available)}/{len(availability.items)}")

Alternative Part Finding:
    from kicad_tools.cost import AlternativePartFinder
    from kicad_tools.parts import LCSCClient

    client = LCSCClient()
    finder = AlternativePartFinder(client)

    # Find alternatives for problematic BOM items
    suggestions = finder.suggest_for_bom(bom_items, availability)
"""

from .alternatives import (
    AlternativePartFinder,
    AlternativeSuggestions,
    PartAlternative,
)
from .availability import (
    AlternativePart,
    AvailabilityStatus,
    BOMAvailabilityResult,
    LCSCAvailabilityChecker,
    PartAvailabilityResult,
)
from .estimator import (
    AssemblyCost,
    ComponentCost,
    CostEstimate,
    ManufacturingCostEstimator,
    PCBCost,
)

__all__ = [
    # Cost estimation
    "ManufacturingCostEstimator",
    "CostEstimate",
    "PCBCost",
    "ComponentCost",
    "AssemblyCost",
    # Availability checking
    "LCSCAvailabilityChecker",
    "PartAvailabilityResult",
    "BOMAvailabilityResult",
    "AvailabilityStatus",
    "AlternativePart",
    # Alternative part finding
    "AlternativePartFinder",
    "AlternativeSuggestions",
    "PartAlternative",
]
