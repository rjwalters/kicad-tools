"""
Manufacturing cost estimation for KiCad PCB designs.

Provides tools for estimating PCB fabrication, component, and assembly costs
based on manufacturer pricing models.

Usage:
    from kicad_tools.cost import ManufacturingCostEstimator, CostEstimate

    estimator = ManufacturingCostEstimator(manufacturer="jlcpcb")
    estimate = estimator.estimate(pcb, bom, quantity=100)
    print(f"Total: ${estimate.total_per_unit:.2f}/unit")
"""

from .estimator import (
    AssemblyCost,
    ComponentCost,
    CostEstimate,
    ManufacturingCostEstimator,
    PCBCost,
)

__all__ = [
    "ManufacturingCostEstimator",
    "CostEstimate",
    "PCBCost",
    "ComponentCost",
    "AssemblyCost",
]
