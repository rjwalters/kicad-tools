"""
Manufacturing cost estimation for PCB designs.

Estimates costs for PCB fabrication, component sourcing, and assembly
based on manufacturer pricing models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.bom import BOM, BOMItem
    from kicad_tools.schema.pcb import PCB


@dataclass
class ComponentCost:
    """Cost for a single component type."""

    reference: str  # First reference designator (e.g., "R1")
    value: str  # Component value (e.g., "10k")
    footprint: str  # Footprint name
    mpn: str | None  # Manufacturer part number
    lcsc: str | None  # LCSC part number
    quantity_per_board: int  # Number of this component per board
    unit_cost: float  # Cost per component in USD
    extended_cost: float  # unit_cost * quantity_per_board
    in_stock: bool  # Whether part is in stock
    lead_time_days: int | None  # Lead time if known
    is_basic: bool  # JLCPCB basic part (no setup fee)

    @property
    def total_for_quantity(self) -> float:
        """Total cost for given quantity of boards."""
        return self.extended_cost


@dataclass
class PCBCost:
    """PCB fabrication cost breakdown."""

    cost_per_unit: float  # Per-board cost in USD
    total_cost: float  # Total for quantity
    quantity: int

    # Cost breakdown
    base_cost: float  # Base board cost
    area_cost: float  # Cost from board area
    layer_cost: float  # Additional cost for layers > 2
    finish_cost: float  # Surface finish cost (HASL, ENIG, etc.)
    color_cost: float  # Non-green solder mask cost
    via_cost: float  # Via-in-pad or other special via costs
    thickness_cost: float  # Non-standard thickness cost

    # Board specs used for calculation
    width_mm: float
    height_mm: float
    area_cm2: float
    layer_count: int
    surface_finish: str
    solder_mask_color: str
    board_thickness_mm: float


@dataclass
class AssemblyCost:
    """Assembly cost breakdown."""

    cost_per_unit: float  # Per-board cost in USD
    total_cost: float  # Total for quantity
    quantity: int

    # Cost breakdown
    smt_cost: float  # SMT assembly cost
    through_hole_cost: float  # Through-hole assembly cost
    setup_cost: float  # Setup/stencil cost
    bga_cost: float  # BGA placement cost
    fine_pitch_cost: float  # Fine pitch component cost

    # Assembly specs
    smt_parts: int  # Number of SMT placements
    through_hole_parts: int  # Number of through-hole parts
    unique_parts: int  # Number of unique part types
    bga_parts: int  # Number of BGA components
    double_sided: bool  # Double-sided assembly


@dataclass
class CostEstimate:
    """Complete manufacturing cost estimate."""

    # Cost summaries
    pcb_cost_per_unit: float
    component_cost_per_unit: float
    assembly_cost_per_unit: float
    total_per_unit: float
    total_for_quantity: float
    quantity: int

    # Detailed breakdowns
    pcb: PCBCost
    components: list[ComponentCost]
    assembly: AssemblyCost

    # Analysis
    cost_drivers: list[str]  # Major factors affecting cost
    optimization_suggestions: list[str]  # Ways to reduce cost

    # Metadata
    manufacturer: str
    currency: str = "USD"

    @property
    def component_breakdown(self) -> dict[str, float]:
        """Group component costs by category."""
        categories: dict[str, float] = {}
        for comp in self.components:
            # Categorize by reference prefix
            prefix = "".join(c for c in comp.reference if c.isalpha())
            category = _get_category_name(prefix)
            categories[category] = categories.get(category, 0) + comp.extended_cost
        return categories

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "manufacturer": self.manufacturer,
            "quantity": self.quantity,
            "currency": self.currency,
            "summary": {
                "pcb_cost_per_unit": round(self.pcb_cost_per_unit, 2),
                "component_cost_per_unit": round(self.component_cost_per_unit, 2),
                "assembly_cost_per_unit": round(self.assembly_cost_per_unit, 2),
                "total_per_unit": round(self.total_per_unit, 2),
                "total_for_quantity": round(self.total_for_quantity, 2),
            },
            "pcb": {
                "cost_per_unit": round(self.pcb.cost_per_unit, 2),
                "total_cost": round(self.pcb.total_cost, 2),
                "breakdown": {
                    "base": round(self.pcb.base_cost, 2),
                    "area": round(self.pcb.area_cost, 2),
                    "layers": round(self.pcb.layer_cost, 2),
                    "finish": round(self.pcb.finish_cost, 2),
                    "color": round(self.pcb.color_cost, 2),
                    "vias": round(self.pcb.via_cost, 2),
                    "thickness": round(self.pcb.thickness_cost, 2),
                },
                "specs": {
                    "width_mm": self.pcb.width_mm,
                    "height_mm": self.pcb.height_mm,
                    "area_cm2": round(self.pcb.area_cm2, 2),
                    "layer_count": self.pcb.layer_count,
                    "surface_finish": self.pcb.surface_finish,
                    "solder_mask_color": self.pcb.solder_mask_color,
                    "board_thickness_mm": self.pcb.board_thickness_mm,
                },
            },
            "components": {
                "cost_per_unit": round(self.component_cost_per_unit, 2),
                "total_parts": sum(c.quantity_per_board for c in self.components),
                "unique_parts": len(self.components),
                "breakdown": self.component_breakdown,
                "items": [
                    {
                        "reference": c.reference,
                        "value": c.value,
                        "mpn": c.mpn,
                        "lcsc": c.lcsc,
                        "quantity": c.quantity_per_board,
                        "unit_cost": round(c.unit_cost, 4),
                        "extended_cost": round(c.extended_cost, 2),
                        "in_stock": c.in_stock,
                        "is_basic": c.is_basic,
                    }
                    for c in self.components
                ],
            },
            "assembly": {
                "cost_per_unit": round(self.assembly.cost_per_unit, 2),
                "total_cost": round(self.assembly.total_cost, 2),
                "breakdown": {
                    "smt": round(self.assembly.smt_cost, 2),
                    "through_hole": round(self.assembly.through_hole_cost, 2),
                    "setup": round(self.assembly.setup_cost, 2),
                    "bga": round(self.assembly.bga_cost, 2),
                    "fine_pitch": round(self.assembly.fine_pitch_cost, 2),
                },
                "specs": {
                    "smt_parts": self.assembly.smt_parts,
                    "through_hole_parts": self.assembly.through_hole_parts,
                    "unique_parts": self.assembly.unique_parts,
                    "bga_parts": self.assembly.bga_parts,
                    "double_sided": self.assembly.double_sided,
                },
            },
            "cost_drivers": self.cost_drivers,
            "optimization_suggestions": self.optimization_suggestions,
        }


def _get_category_name(prefix: str) -> str:
    """Get human-readable category name from reference prefix."""
    categories = {
        "R": "Resistors",
        "C": "Capacitors",
        "L": "Inductors",
        "D": "Diodes",
        "Q": "Transistors",
        "U": "ICs",
        "J": "Connectors",
        "P": "Connectors",
        "SW": "Switches",
        "Y": "Crystals",
        "F": "Fuses",
        "LED": "LEDs",
        "FB": "Ferrite Beads",
    }
    return categories.get(prefix, "Other")


# Default pricing data directory
_PRICING_DIR = Path(__file__).parent / "pricing"


class ManufacturingCostEstimator:
    """Estimate PCB manufacturing costs for a given manufacturer."""

    def __init__(self, manufacturer: str = "jlcpcb"):
        """
        Initialize cost estimator.

        Args:
            manufacturer: Manufacturer ID (jlcpcb, pcbway, etc.)
        """
        self.manufacturer = manufacturer.lower()
        self.pricing = self._load_pricing()

    def _load_pricing(self) -> dict:
        """Load pricing data for the manufacturer."""
        pricing_file = _PRICING_DIR / f"{self.manufacturer}.yaml"

        if pricing_file.exists():
            try:
                import yaml

                with open(pricing_file, encoding="utf-8") as f:
                    return yaml.safe_load(f)
            except ImportError:
                pass

        # Return default pricing if no file found
        return self._get_default_pricing()

    def _get_default_pricing(self) -> dict:
        """Get default JLCPCB-like pricing."""
        return {
            "pcb": {
                "base_cost": 2.0,  # Base cost for 5 boards
                "per_cm2": 0.02,  # Per square centimeter
                "layer_multiplier": {
                    1: 1.0,
                    2: 1.0,
                    4: 1.8,
                    6: 2.5,
                    8: 3.5,
                },
                "finish": {
                    "hasl": 0.0,
                    "hasl_lead_free": 0.0,
                    "enig": 1.0,
                    "osp": 0.0,
                },
                "color": {
                    "green": 0.0,
                    "red": 0.0,
                    "blue": 0.0,
                    "black": 0.0,
                    "white": 2.0,
                    "yellow": 2.0,
                    "matte_black": 5.0,
                    "matte_green": 5.0,
                },
                "thickness": {
                    0.8: 0.0,
                    1.0: 0.0,
                    1.2: 0.0,
                    1.6: 0.0,
                    2.0: 2.0,
                },
                "quantity_discount": {
                    5: 1.0,
                    10: 0.9,
                    20: 0.8,
                    50: 0.7,
                    100: 0.6,
                },
            },
            "assembly": {
                "setup_fee": 8.0,  # Per order
                "stencil_fee": 1.5,  # Per order
                "smt_per_joint": 0.0017,  # Per solder joint
                "through_hole_per_part": 0.02,  # Per TH part
                "extended_part_fee": 3.0,  # Per unique extended part
                "bga_per_part": 0.10,  # Per BGA component
                "fine_pitch_multiplier": 1.5,  # < 0.5mm pitch
                "double_sided_multiplier": 1.8,  # Double-sided assembly
                "minimum_fee": 1.0,  # Minimum assembly cost per board
            },
            "components": {
                "default_price": 0.01,  # Default for unknown parts
                "passives": {
                    "0201": 0.002,
                    "0402": 0.003,
                    "0603": 0.004,
                    "0805": 0.005,
                    "1206": 0.008,
                },
            },
        }

    def estimate(
        self,
        pcb: PCB | None = None,
        bom: BOM | None = None,
        quantity: int = 10,
        *,
        # Optional overrides when PCB not provided
        width_mm: float | None = None,
        height_mm: float | None = None,
        layer_count: int = 2,
        surface_finish: str = "hasl",
        solder_mask_color: str = "green",
        board_thickness_mm: float = 1.6,
    ) -> CostEstimate:
        """
        Estimate manufacturing costs.

        Args:
            pcb: PCB object (optional, can provide dimensions separately)
            bom: Bill of Materials (optional for component costs)
            quantity: Number of boards to manufacture
            width_mm: Board width if PCB not provided
            height_mm: Board height if PCB not provided
            layer_count: Number of layers if PCB not provided
            surface_finish: Surface finish type
            solder_mask_color: Solder mask color
            board_thickness_mm: Board thickness

        Returns:
            CostEstimate with detailed breakdown
        """
        # Extract PCB dimensions
        if pcb is not None:
            dims = self._get_pcb_dimensions(pcb)
            width_mm = dims["width"]
            height_mm = dims["height"]
            layer_count = len(pcb.copper_layers)

            # Try to get finish from stackup
            if pcb.setup and pcb.setup.stackup:
                for layer in pcb.setup.stackup:
                    if "finish" in layer.type.lower():
                        surface_finish = layer.material.lower() if layer.material else "hasl"
                        break
        elif width_mm is None or height_mm is None:
            raise ValueError("Must provide PCB or dimensions (width_mm, height_mm)")

        # Estimate PCB cost
        pcb_cost = self._estimate_pcb_cost(
            width_mm=width_mm,
            height_mm=height_mm,
            layer_count=layer_count,
            surface_finish=surface_finish,
            solder_mask_color=solder_mask_color,
            board_thickness_mm=board_thickness_mm,
            quantity=quantity,
        )

        # Estimate component costs
        component_costs: list[ComponentCost] = []
        if bom is not None:
            component_costs = self._estimate_component_costs(bom, quantity)

        # Estimate assembly costs
        assembly_cost = self._estimate_assembly_cost(
            bom=bom,
            pcb=pcb,
            quantity=quantity,
        )

        # Calculate totals
        component_total = sum(c.extended_cost for c in component_costs)
        total_per_unit = pcb_cost.cost_per_unit + component_total + assembly_cost.cost_per_unit

        # Identify cost drivers
        cost_drivers = self._identify_cost_drivers(pcb_cost, component_costs, assembly_cost)

        # Generate optimization suggestions
        suggestions = self._suggest_optimizations(
            pcb_cost, component_costs, assembly_cost, layer_count
        )

        return CostEstimate(
            pcb_cost_per_unit=pcb_cost.cost_per_unit,
            component_cost_per_unit=component_total,
            assembly_cost_per_unit=assembly_cost.cost_per_unit,
            total_per_unit=total_per_unit,
            total_for_quantity=total_per_unit * quantity,
            quantity=quantity,
            pcb=pcb_cost,
            components=component_costs,
            assembly=assembly_cost,
            cost_drivers=cost_drivers,
            optimization_suggestions=suggestions,
            manufacturer=self.manufacturer,
        )

    def _get_pcb_dimensions(self, pcb: PCB) -> dict[str, float]:
        """Extract board dimensions from PCB outline."""
        outline = pcb.get_board_outline()

        if not outline:
            # Fallback: calculate from footprint positions
            if pcb.footprints:
                xs = [fp.position[0] for fp in pcb.footprints]
                ys = [fp.position[1] for fp in pcb.footprints]
                # Add margin
                margin = 5.0
                return {
                    "width": max(xs) - min(xs) + 2 * margin,
                    "height": max(ys) - min(ys) + 2 * margin,
                }
            # Default size
            return {"width": 50.0, "height": 50.0}

        # Calculate bounding box
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        return {
            "width": max(xs) - min(xs),
            "height": max(ys) - min(ys),
        }

    def _estimate_pcb_cost(
        self,
        width_mm: float,
        height_mm: float,
        layer_count: int,
        surface_finish: str,
        solder_mask_color: str,
        board_thickness_mm: float,
        quantity: int,
    ) -> PCBCost:
        """Estimate PCB fabrication cost."""
        pricing = self.pricing["pcb"]

        # Calculate area
        area_cm2 = (width_mm * height_mm) / 100

        # Base cost
        base_cost = pricing["base_cost"]

        # Area cost
        area_cost = area_cm2 * pricing["per_cm2"] * quantity

        # Layer multiplier
        layer_mult = pricing["layer_multiplier"].get(layer_count, 1.0)
        layer_cost = base_cost * (layer_mult - 1.0) * quantity

        # Surface finish
        finish_key = surface_finish.lower().replace("-", "_").replace(" ", "_")
        finish_cost = pricing["finish"].get(finish_key, 0.0) * quantity

        # Solder mask color
        color_key = solder_mask_color.lower().replace("-", "_").replace(" ", "_")
        color_cost = pricing["color"].get(color_key, 0.0)

        # Board thickness
        thickness_cost = pricing["thickness"].get(board_thickness_mm, 0.0)

        # Quantity discount
        qty_mult = 1.0
        for qty_threshold, mult in sorted(pricing["quantity_discount"].items()):
            if quantity >= int(qty_threshold):
                qty_mult = mult

        # Calculate totals
        subtotal = base_cost + area_cost + layer_cost + finish_cost + color_cost + thickness_cost
        total_cost = subtotal * qty_mult
        cost_per_unit = total_cost / quantity

        return PCBCost(
            cost_per_unit=cost_per_unit,
            total_cost=total_cost,
            quantity=quantity,
            base_cost=base_cost,
            area_cost=area_cost,
            layer_cost=layer_cost,
            finish_cost=finish_cost,
            color_cost=color_cost,
            via_cost=0.0,  # Could add via-in-pad detection
            thickness_cost=thickness_cost,
            width_mm=width_mm,
            height_mm=height_mm,
            area_cm2=area_cm2,
            layer_count=layer_count,
            surface_finish=surface_finish,
            solder_mask_color=solder_mask_color,
            board_thickness_mm=board_thickness_mm,
        )

    def _estimate_component_costs(self, bom: BOM, quantity: int) -> list[ComponentCost]:
        """Estimate component costs from BOM."""
        costs: list[ComponentCost] = []
        pricing = self.pricing.get("components", {})

        for group in bom.grouped():
            # Skip DNP components
            if group.items and group.items[0].dnp:
                continue

            # Determine unit cost
            unit_cost = self._get_component_price(group, pricing)

            costs.append(
                ComponentCost(
                    reference=group.references.split(",")[0].strip(),
                    value=group.value,
                    footprint=group.footprint,
                    mpn=group.mpn or None,
                    lcsc=group.lcsc or None,
                    quantity_per_board=group.quantity,
                    unit_cost=unit_cost,
                    extended_cost=unit_cost * group.quantity,
                    in_stock=True,  # Would need API lookup for real data
                    lead_time_days=None,
                    is_basic=self._is_basic_part(group.lcsc) if group.lcsc else False,
                )
            )

        return costs

    def _get_component_price(self, group, pricing: dict) -> float:
        """Get estimated price for a component."""
        # Try to match footprint for passive pricing
        footprint = group.footprint.lower()

        passive_pricing = pricing.get("passives", {})
        for size, price in passive_pricing.items():
            if size in footprint:
                return price

        # Default price based on reference prefix
        ref = group.items[0].reference if group.items else ""
        prefix = "".join(c for c in ref if c.isalpha())

        default_prices = {
            "R": 0.005,
            "C": 0.008,
            "L": 0.02,
            "D": 0.02,
            "LED": 0.02,
            "Q": 0.05,
            "U": 0.50,  # ICs vary widely
            "J": 0.10,
            "P": 0.10,
            "Y": 0.20,  # Crystals
            "SW": 0.10,
            "F": 0.05,
            "FB": 0.01,
        }

        return default_prices.get(prefix, pricing.get("default_price", 0.01))

    def _is_basic_part(self, lcsc: str) -> bool:
        """Check if LCSC part is a basic part (no setup fee)."""
        # In reality, this would need an API lookup
        # Basic parts are typically common passives
        return True  # Assume basic for estimation

    def _estimate_assembly_cost(
        self,
        bom: BOM | None,
        pcb: PCB | None,
        quantity: int,
    ) -> AssemblyCost:
        """Estimate assembly costs."""
        pricing = self.pricing.get("assembly", {})

        # Count parts by type
        smt_parts = 0
        through_hole_parts = 0
        bga_parts = 0
        unique_parts = 0
        extended_parts = 0

        if bom is not None:
            groups = bom.grouped()
            unique_parts = len(groups)

            for group in groups:
                qty = group.quantity

                # Determine if SMT or through-hole based on footprint
                footprint = group.footprint.lower()
                is_through_hole = any(
                    x in footprint for x in ["dip", "to-220", "to-92", "sip", "through"]
                )
                is_bga = "bga" in footprint

                if is_bga:
                    bga_parts += qty
                    smt_parts += qty  # BGAs are also counted as SMT
                elif is_through_hole:
                    through_hole_parts += qty
                else:
                    smt_parts += qty

                # Count extended parts (non-basic)
                if group.lcsc and not self._is_basic_part(group.lcsc):
                    extended_parts += 1

        # Detect double-sided assembly
        double_sided = False
        if pcb is not None:
            bottom_fps = list(pcb.footprints_on_layer("B.Cu"))
            double_sided = len(bottom_fps) > 0

        # Calculate costs
        setup_cost = pricing.get("setup_fee", 8.0) + pricing.get("stencil_fee", 1.5)

        # SMT cost (per solder joint, estimate 4 joints per SMT part)
        smt_cost = smt_parts * 4 * pricing.get("smt_per_joint", 0.0017) * quantity

        # Through-hole cost
        through_hole_cost = (
            through_hole_parts * pricing.get("through_hole_per_part", 0.02) * quantity
        )

        # Extended part fees
        extended_fee = extended_parts * pricing.get("extended_part_fee", 3.0)

        # BGA cost
        bga_cost = bga_parts * pricing.get("bga_per_part", 0.10) * quantity

        # Double-sided multiplier
        multiplier = pricing.get("double_sided_multiplier", 1.8) if double_sided else 1.0

        # Total assembly cost
        assembly_subtotal = (smt_cost + through_hole_cost + bga_cost) * multiplier
        total_cost = setup_cost + extended_fee + assembly_subtotal

        # Apply minimum
        min_per_board = pricing.get("minimum_fee", 1.0) * quantity
        if total_cost < min_per_board:
            total_cost = min_per_board

        return AssemblyCost(
            cost_per_unit=total_cost / quantity,
            total_cost=total_cost,
            quantity=quantity,
            smt_cost=smt_cost,
            through_hole_cost=through_hole_cost,
            setup_cost=setup_cost + extended_fee,
            bga_cost=bga_cost,
            fine_pitch_cost=0.0,  # Would need footprint analysis
            smt_parts=smt_parts,
            through_hole_parts=through_hole_parts,
            unique_parts=unique_parts,
            bga_parts=bga_parts,
            double_sided=double_sided,
        )

    def _identify_cost_drivers(
        self,
        pcb: PCBCost,
        components: list[ComponentCost],
        assembly: AssemblyCost,
    ) -> list[str]:
        """Identify major cost drivers."""
        drivers: list[str] = []

        # PCB drivers
        if pcb.layer_cost > 0.5:
            drivers.append(f"{pcb.layer_count}-layer board adds ${pcb.layer_cost:.2f}")
        if pcb.finish_cost > 0:
            drivers.append(f"{pcb.surface_finish.upper()} finish adds ${pcb.finish_cost:.2f}")
        if pcb.area_cm2 > 50:
            drivers.append(f"Large board area ({pcb.area_cm2:.0f} cm2)")

        # Component drivers
        expensive = sorted(components, key=lambda c: c.extended_cost, reverse=True)[:3]
        for comp in expensive:
            if comp.extended_cost > 0.50:
                drivers.append(f"{comp.reference} ({comp.value}) is ${comp.extended_cost:.2f}")

        # Assembly drivers
        if assembly.through_hole_parts > 0:
            drivers.append(
                f"{assembly.through_hole_parts} through-hole parts add ${assembly.through_hole_cost:.2f}"
            )
        if assembly.double_sided:
            drivers.append("Double-sided assembly increases cost")
        if assembly.bga_parts > 0:
            drivers.append(f"{assembly.bga_parts} BGA parts add complexity")

        return drivers[:5]  # Limit to top 5

    def _suggest_optimizations(
        self,
        pcb: PCBCost,
        components: list[ComponentCost],
        assembly: AssemblyCost,
        layer_count: int,
    ) -> list[str]:
        """Suggest cost optimizations."""
        suggestions: list[str] = []

        # PCB suggestions
        if layer_count > 2:
            savings = pcb.layer_cost / pcb.quantity
            suggestions.append(f"Consider 2-layer design to save ${savings:.2f}/unit")

        if pcb.surface_finish not in ("hasl", "hasl_lead_free"):
            suggestions.append("HASL finish is lowest cost option")

        if pcb.color_cost > 0:
            suggestions.append("Green solder mask has no additional cost")

        # Component suggestions
        non_basic = [c for c in components if not c.is_basic]
        if non_basic:
            suggestions.append(
                f"Use basic parts to avoid extended part fees ({len(non_basic)} extended parts)"
            )

        # Assembly suggestions
        if assembly.through_hole_parts > 5:
            suggestions.append("Reduce through-hole parts for lower assembly cost")

        if assembly.double_sided:
            suggestions.append("Single-sided assembly is significantly cheaper")

        return suggestions[:5]  # Limit to top 5
