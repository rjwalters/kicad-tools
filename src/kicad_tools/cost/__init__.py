"""
Manufacturing cost estimation for KiCad PCB designs.

Provides tools for estimating PCB fabrication, component, and assembly costs
based on manufacturer pricing models.

Usage:
    from kicad_tools.cost import ManufacturingCostEstimator, CostEstimate

    estimator = ManufacturingCostEstimator(manufacturer="jlcpcb")
    estimate = estimator.estimate(pcb, bom, quantity=100)
    print(f"Total: ${estimate.total_per_unit:.2f}/unit")

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
    # Alternative part finding
    "AlternativePartFinder",
    "AlternativeSuggestions",
    "PartAlternative",
]
