"""Tests for the cost estimation module."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cost import (
    AlternativePart,
    AssemblyCost,
    AvailabilityStatus,
    BOMAvailabilityResult,
    ComponentCost,
    CostEstimate,
    LCSCAvailabilityChecker,
    ManufacturingCostEstimator,
    PartAvailabilityResult,
    PCBCost,
)


class TestComponentCost:
    """Tests for ComponentCost dataclass."""

    def test_component_cost_basic(self):
        cost = ComponentCost(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402FR-0710KL",
            lcsc="C123456",
            quantity_per_board=5,
            unit_cost=0.003,
            extended_cost=0.015,
            in_stock=True,
            lead_time_days=3,
            is_basic=True,
        )
        assert cost.reference == "R1"
        assert cost.quantity_per_board == 5
        assert cost.extended_cost == 0.015
        assert cost.in_stock is True
        assert cost.is_basic is True

    def test_total_for_quantity(self):
        cost = ComponentCost(
            reference="C1",
            value="100nF",
            footprint="0402",
            mpn=None,
            lcsc="C654321",
            quantity_per_board=10,
            unit_cost=0.004,
            extended_cost=0.04,
            in_stock=True,
            lead_time_days=None,
            is_basic=True,
        )
        assert cost.total_for_quantity == 0.04


class TestPCBCost:
    """Tests for PCBCost dataclass."""

    def test_pcb_cost_basic(self):
        cost = PCBCost(
            cost_per_unit=2.50,
            total_cost=25.0,
            quantity=10,
            base_cost=2.0,
            area_cost=1.0,
            layer_cost=1.6,
            finish_cost=1.0,
            color_cost=0.0,
            via_cost=0.0,
            thickness_cost=0.0,
            width_mm=50.0,
            height_mm=40.0,
            area_cm2=20.0,
            layer_count=4,
            surface_finish="enig",
            solder_mask_color="green",
            board_thickness_mm=1.6,
        )
        assert cost.cost_per_unit == 2.50
        assert cost.layer_count == 4
        assert cost.area_cm2 == 20.0


class TestAssemblyCost:
    """Tests for AssemblyCost dataclass."""

    def test_assembly_cost_basic(self):
        cost = AssemblyCost(
            cost_per_unit=1.50,
            total_cost=15.0,
            quantity=10,
            smt_cost=12.0,
            through_hole_cost=0.5,
            setup_cost=9.5,
            bga_cost=0.0,
            fine_pitch_cost=0.0,
            smt_parts=50,
            through_hole_parts=2,
            unique_parts=25,
            bga_parts=0,
            double_sided=False,
        )
        assert cost.cost_per_unit == 1.50
        assert cost.smt_parts == 50
        assert cost.through_hole_parts == 2
        assert cost.double_sided is False


class TestCostEstimate:
    """Tests for CostEstimate dataclass."""

    @pytest.fixture
    def sample_estimate(self):
        """Create a sample cost estimate for testing."""
        pcb = PCBCost(
            cost_per_unit=2.00,
            total_cost=20.0,
            quantity=10,
            base_cost=2.0,
            area_cost=0.5,
            layer_cost=0.8,
            finish_cost=0.0,
            color_cost=0.0,
            via_cost=0.0,
            thickness_cost=0.0,
            width_mm=50.0,
            height_mm=40.0,
            area_cm2=20.0,
            layer_count=4,
            surface_finish="hasl",
            solder_mask_color="green",
            board_thickness_mm=1.6,
        )
        components = [
            ComponentCost(
                reference="R1",
                value="10k",
                footprint="0402",
                mpn="RC0402FR",
                lcsc="C123456",
                quantity_per_board=10,
                unit_cost=0.003,
                extended_cost=0.03,
                in_stock=True,
                lead_time_days=3,
                is_basic=True,
            ),
            ComponentCost(
                reference="U1",
                value="STM32F103",
                footprint="LQFP-48",
                mpn="STM32F103C8T6",
                lcsc="C8734",
                quantity_per_board=1,
                unit_cost=2.50,
                extended_cost=2.50,
                in_stock=True,
                lead_time_days=5,
                is_basic=False,
            ),
        ]
        assembly = AssemblyCost(
            cost_per_unit=1.00,
            total_cost=10.0,
            quantity=10,
            smt_cost=8.0,
            through_hole_cost=0.0,
            setup_cost=9.5,
            bga_cost=0.0,
            fine_pitch_cost=0.0,
            smt_parts=40,
            through_hole_parts=0,
            unique_parts=20,
            bga_parts=0,
            double_sided=False,
        )
        return CostEstimate(
            pcb_cost_per_unit=2.00,
            component_cost_per_unit=2.53,
            assembly_cost_per_unit=1.00,
            total_per_unit=5.53,
            total_for_quantity=55.30,
            quantity=10,
            pcb=pcb,
            components=components,
            assembly=assembly,
            cost_drivers=["4-layer board adds $0.80"],
            optimization_suggestions=["Consider 2-layer design"],
            manufacturer="jlcpcb",
        )

    def test_estimate_totals(self, sample_estimate):
        assert sample_estimate.total_per_unit == 5.53
        assert sample_estimate.total_for_quantity == 55.30
        assert sample_estimate.quantity == 10

    def test_component_breakdown(self, sample_estimate):
        breakdown = sample_estimate.component_breakdown
        assert "Resistors" in breakdown
        assert "ICs" in breakdown
        assert breakdown["Resistors"] == 0.03
        assert breakdown["ICs"] == 2.50

    def test_to_dict(self, sample_estimate):
        data = sample_estimate.to_dict()
        assert data["manufacturer"] == "jlcpcb"
        assert data["quantity"] == 10
        assert "summary" in data
        assert data["summary"]["total_per_unit"] == 5.53
        assert "pcb" in data
        assert "components" in data
        assert "assembly" in data
        assert "cost_drivers" in data
        assert "optimization_suggestions" in data


class TestManufacturingCostEstimator:
    """Tests for ManufacturingCostEstimator class."""

    def test_init_default_manufacturer(self):
        estimator = ManufacturingCostEstimator()
        assert estimator.manufacturer == "jlcpcb"

    def test_init_custom_manufacturer(self):
        estimator = ManufacturingCostEstimator(manufacturer="pcbway")
        assert estimator.manufacturer == "pcbway"

    def test_get_default_pricing(self):
        estimator = ManufacturingCostEstimator()
        pricing = estimator._get_default_pricing()
        assert "pcb" in pricing
        assert "assembly" in pricing
        assert "components" in pricing
        assert pricing["pcb"]["base_cost"] == 2.0

    def test_estimate_pcb_cost(self):
        estimator = ManufacturingCostEstimator()
        pcb_cost = estimator._estimate_pcb_cost(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            surface_finish="hasl",
            solder_mask_color="green",
            board_thickness_mm=1.6,
            quantity=10,
        )
        assert pcb_cost.width_mm == 50.0
        assert pcb_cost.height_mm == 40.0
        assert pcb_cost.layer_count == 2
        assert pcb_cost.quantity == 10
        assert pcb_cost.cost_per_unit > 0

    def test_estimate_pcb_cost_4_layer(self):
        estimator = ManufacturingCostEstimator()
        cost_2layer = estimator._estimate_pcb_cost(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            surface_finish="hasl",
            solder_mask_color="green",
            board_thickness_mm=1.6,
            quantity=10,
        )
        cost_4layer = estimator._estimate_pcb_cost(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=4,
            surface_finish="hasl",
            solder_mask_color="green",
            board_thickness_mm=1.6,
            quantity=10,
        )
        # 4-layer should cost more than 2-layer
        assert cost_4layer.cost_per_unit > cost_2layer.cost_per_unit

    def test_estimate_pcb_cost_enig_finish(self):
        estimator = ManufacturingCostEstimator()
        cost_hasl = estimator._estimate_pcb_cost(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            surface_finish="hasl",
            solder_mask_color="green",
            board_thickness_mm=1.6,
            quantity=10,
        )
        cost_enig = estimator._estimate_pcb_cost(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            surface_finish="enig",
            solder_mask_color="green",
            board_thickness_mm=1.6,
            quantity=10,
        )
        # ENIG should cost more than HASL
        assert cost_enig.total_cost > cost_hasl.total_cost

    def test_estimate_without_pcb_or_dimensions_raises(self):
        estimator = ManufacturingCostEstimator()
        with pytest.raises(ValueError, match="Must provide PCB or dimensions"):
            estimator.estimate()

    def test_estimate_with_dimensions_only(self):
        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            quantity=10,
        )
        assert estimate.quantity == 10
        assert estimate.pcb.width_mm == 50.0
        assert estimate.pcb.height_mm == 40.0
        assert estimate.total_per_unit > 0

    def test_estimate_quantity_discount(self):
        estimator = ManufacturingCostEstimator()
        estimate_10 = estimator.estimate(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            quantity=10,
        )
        estimate_100 = estimator.estimate(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=2,
            quantity=100,
        )
        # Per-unit cost should be lower at higher quantity
        assert estimate_100.pcb_cost_per_unit < estimate_10.pcb_cost_per_unit

    def test_identify_cost_drivers(self):
        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=4,
            surface_finish="enig",
            quantity=10,
        )
        # Should identify layer count and finish as cost drivers
        drivers_text = " ".join(estimate.cost_drivers)
        # At least one cost driver should be identified
        assert len(estimate.cost_drivers) > 0

    def test_suggest_optimizations(self):
        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(
            width_mm=50.0,
            height_mm=40.0,
            layer_count=4,
            surface_finish="enig",
            solder_mask_color="white",
            quantity=10,
        )
        # Should suggest optimizations
        assert len(estimate.optimization_suggestions) > 0

    def test_estimate_pcb_method(self):
        """Test the estimate_pcb method for audit integration."""
        estimator = ManufacturingCostEstimator()
        result = estimator.estimate_pcb(
            width_mm=50.0,
            height_mm=40.0,
            layers=2,
            quantity=10,
        )
        # Should return SimplePCBCostResult with expected attributes
        assert result.quantity == 10
        assert result.total > 0
        assert result.cost_per_unit > 0
        assert result.total == result.cost_per_unit * 10

    def test_estimate_pcb_with_all_options(self):
        """Test estimate_pcb with all optional parameters."""
        estimator = ManufacturingCostEstimator()
        result = estimator.estimate_pcb(
            width_mm=100.0,
            height_mm=80.0,
            layers=4,
            quantity=50,
            surface_finish="enig",
            solder_mask_color="black",
            board_thickness_mm=1.6,
        )
        # Should return valid cost estimate
        assert result.quantity == 50
        assert result.total > 0
        assert result.cost_per_unit > 0


class TestManufacturingCostEstimatorWithMockPCB:
    """Tests for ManufacturingCostEstimator with mocked PCB."""

    @pytest.fixture
    def mock_pcb(self):
        """Create a mock PCB object."""
        pcb = MagicMock()
        pcb.get_board_outline.return_value = [
            (0.0, 0.0),
            (50.0, 0.0),
            (50.0, 40.0),
            (0.0, 40.0),
        ]
        pcb.copper_layers = [MagicMock(), MagicMock()]  # 2-layer
        pcb.setup = None
        pcb.footprints = []
        return pcb

    def test_estimate_with_pcb(self, mock_pcb):
        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(pcb=mock_pcb, quantity=10)

        assert estimate.quantity == 10
        assert estimate.pcb.width_mm == 50.0
        assert estimate.pcb.height_mm == 40.0
        assert estimate.pcb.layer_count == 2

    def test_estimate_with_pcb_4_layer(self, mock_pcb):
        mock_pcb.copper_layers = [MagicMock() for _ in range(4)]  # 4-layer
        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(pcb=mock_pcb, quantity=10)

        assert estimate.pcb.layer_count == 4

    def test_get_pcb_dimensions_from_outline(self, mock_pcb):
        estimator = ManufacturingCostEstimator()
        dims = estimator._get_pcb_dimensions(mock_pcb)
        assert dims["width"] == 50.0
        assert dims["height"] == 40.0

    def test_get_pcb_dimensions_fallback(self, mock_pcb):
        mock_pcb.get_board_outline.return_value = []
        # Add footprints for fallback
        fp1 = MagicMock()
        fp1.position = (10.0, 10.0)
        fp2 = MagicMock()
        fp2.position = (40.0, 30.0)
        mock_pcb.footprints = [fp1, fp2]

        estimator = ManufacturingCostEstimator()
        dims = estimator._get_pcb_dimensions(mock_pcb)

        # Should calculate from footprint positions + margin
        assert dims["width"] > 30.0  # 40 - 10 = 30 + 2*margin
        assert dims["height"] > 20.0  # 30 - 10 = 20 + 2*margin

    def test_bom_from_pcb_footprints(self, mock_pcb):
        """Test that components are extracted from PCB footprints."""
        # Create mock footprints
        fp1 = MagicMock()
        fp1.reference = "R1"
        fp1.value = "10k"
        fp1.name = "Resistor_SMD:R_0402"
        fp1.texts = []

        fp2 = MagicMock()
        fp2.reference = "C1"
        fp2.value = "100nF"
        fp2.name = "Capacitor_SMD:C_0402"
        fp2.texts = []

        fp3 = MagicMock()
        fp3.reference = "U1"
        fp3.value = "STM32F103"
        fp3.name = "Package_QFP:LQFP-48"
        fp3.texts = []

        mock_pcb.footprints = [fp1, fp2, fp3]

        estimator = ManufacturingCostEstimator()
        bom = estimator._bom_from_pcb(mock_pcb)

        assert bom is not None
        assert len(bom.items) == 3
        assert bom.items[0].reference == "R1"
        assert bom.items[0].value == "10k"
        assert bom.items[1].reference == "C1"
        assert bom.items[2].reference == "U1"

    def test_bom_from_pcb_skips_invalid(self, mock_pcb):
        """Test that footprints without references are skipped."""
        fp1 = MagicMock()
        fp1.reference = "R1"
        fp1.value = "10k"
        fp1.name = "R_0402"
        fp1.texts = []

        fp2 = MagicMock()
        fp2.reference = ""  # No reference
        fp2.value = "Logo"
        fp2.name = "Logo"
        fp2.texts = []

        fp3 = MagicMock()
        fp3.reference = "#PWR01"  # Power symbol placeholder
        fp3.value = "GND"
        fp3.name = "Power"
        fp3.texts = []

        mock_pcb.footprints = [fp1, fp2, fp3]

        estimator = ManufacturingCostEstimator()
        bom = estimator._bom_from_pcb(mock_pcb)

        assert bom is not None
        assert len(bom.items) == 1
        assert bom.items[0].reference == "R1"

    def test_estimate_with_pcb_footprints_no_bom(self, mock_pcb):
        """Test that estimate uses PCB footprints when no BOM provided."""
        # Create mock footprints with proper structure
        fp1 = MagicMock()
        fp1.reference = "R1"
        fp1.value = "10k"
        fp1.name = "Resistor_SMD:R_0402"
        fp1.texts = []
        fp1.position = (10.0, 10.0)
        fp1.layer = "F.Cu"

        fp2 = MagicMock()
        fp2.reference = "R2"
        fp2.value = "10k"
        fp2.name = "Resistor_SMD:R_0402"
        fp2.texts = []
        fp2.position = (20.0, 10.0)
        fp2.layer = "F.Cu"

        fp3 = MagicMock()
        fp3.reference = "C1"
        fp3.value = "100nF"
        fp3.name = "Capacitor_SMD:C_0402"
        fp3.texts = []
        fp3.position = (30.0, 10.0)
        fp3.layer = "F.Cu"

        mock_pcb.footprints = [fp1, fp2, fp3]
        mock_pcb.footprints_on_layer.return_value = []

        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(pcb=mock_pcb, quantity=10)

        # Should have detected 3 components
        assert len(estimate.components) == 2  # Grouped by value+footprint
        assert estimate.assembly.smt_parts == 3
        assert estimate.assembly.unique_parts == 2

    def test_estimate_with_empty_pcb_no_bom(self, mock_pcb):
        """Test that estimate handles empty PCB gracefully."""
        mock_pcb.footprints = []
        mock_pcb.footprints_on_layer.return_value = []

        estimator = ManufacturingCostEstimator()
        estimate = estimator.estimate(pcb=mock_pcb, quantity=10)

        # Should work but have no components
        assert len(estimate.components) == 0
        assert estimate.component_cost_per_unit == 0


class TestCostEstimateJSON:
    """Tests for JSON output of cost estimates."""

    def test_to_dict_structure(self):
        pcb = PCBCost(
            cost_per_unit=2.00,
            total_cost=20.0,
            quantity=10,
            base_cost=2.0,
            area_cost=0.5,
            layer_cost=0.0,
            finish_cost=0.0,
            color_cost=0.0,
            via_cost=0.0,
            thickness_cost=0.0,
            width_mm=50.0,
            height_mm=40.0,
            area_cm2=20.0,
            layer_count=2,
            surface_finish="hasl",
            solder_mask_color="green",
            board_thickness_mm=1.6,
        )
        assembly = AssemblyCost(
            cost_per_unit=1.00,
            total_cost=10.0,
            quantity=10,
            smt_cost=8.0,
            through_hole_cost=0.0,
            setup_cost=9.5,
            bga_cost=0.0,
            fine_pitch_cost=0.0,
            smt_parts=40,
            through_hole_parts=0,
            unique_parts=20,
            bga_parts=0,
            double_sided=False,
        )
        estimate = CostEstimate(
            pcb_cost_per_unit=2.00,
            component_cost_per_unit=0.0,
            assembly_cost_per_unit=1.00,
            total_per_unit=3.00,
            total_for_quantity=30.0,
            quantity=10,
            pcb=pcb,
            components=[],
            assembly=assembly,
            cost_drivers=[],
            optimization_suggestions=[],
            manufacturer="jlcpcb",
        )

        data = estimate.to_dict()

        # Check top-level keys
        assert set(data.keys()) == {
            "manufacturer",
            "quantity",
            "currency",
            "summary",
            "pcb",
            "components",
            "assembly",
            "cost_drivers",
            "optimization_suggestions",
        }

        # Check summary
        assert data["summary"]["total_per_unit"] == 3.00
        assert data["summary"]["total_for_quantity"] == 30.0

        # Check PCB breakdown
        assert "breakdown" in data["pcb"]
        assert "specs" in data["pcb"]
        assert data["pcb"]["specs"]["layer_count"] == 2

        # Check assembly breakdown
        assert "breakdown" in data["assembly"]
        assert "specs" in data["assembly"]
        assert data["assembly"]["specs"]["smt_parts"] == 40


# ============================================================================
# Availability Checker Tests
# ============================================================================


class TestPartAvailabilityResult:
    """Tests for PartAvailabilityResult dataclass."""

    def test_basic_creation(self):
        result = PartAvailabilityResult(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402FR-0710KL",
            lcsc_part="C123456",
            quantity_needed=100,
            quantity_available=5000,
            status=AvailabilityStatus.AVAILABLE,
            in_stock=True,
            min_order_qty=10,
            price_breaks=[(1, 0.01), (100, 0.008), (1000, 0.005)],
        )
        assert result.reference == "R1"
        assert result.sufficient_stock is True
        assert result.in_stock is True

    def test_insufficient_stock(self):
        result = PartAvailabilityResult(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402FR-0710KL",
            lcsc_part="C123456",
            quantity_needed=1000,
            quantity_available=500,
            status=AvailabilityStatus.LOW_STOCK,
            in_stock=True,
        )
        assert result.sufficient_stock is False
        assert result.in_stock is True
        assert result.status == AvailabilityStatus.LOW_STOCK

    def test_out_of_stock(self):
        result = PartAvailabilityResult(
            reference="U1",
            value="STM32F103",
            footprint="LQFP-48",
            mpn="STM32F103C8T6",
            lcsc_part="C8734",
            quantity_needed=10,
            quantity_available=0,
            status=AvailabilityStatus.OUT_OF_STOCK,
            in_stock=False,
        )
        assert result.sufficient_stock is False
        assert result.in_stock is False
        assert result.status == AvailabilityStatus.OUT_OF_STOCK

    def test_unit_price_calculation(self):
        result = PartAvailabilityResult(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn=None,
            lcsc_part="C123456",
            quantity_needed=150,
            quantity_available=5000,
            status=AvailabilityStatus.AVAILABLE,
            in_stock=True,
            price_breaks=[(1, 0.01), (100, 0.008), (1000, 0.005)],
        )
        # Should use 100-qty price break for 150 units
        assert result.unit_price == 0.008
        assert result.extended_price == 0.008 * 150

    def test_unit_price_no_breaks(self):
        result = PartAvailabilityResult(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn=None,
            lcsc_part="C123456",
            quantity_needed=100,
            quantity_available=5000,
            status=AvailabilityStatus.AVAILABLE,
            in_stock=True,
            price_breaks=[],
        )
        assert result.unit_price is None
        assert result.extended_price is None

    def test_to_dict(self):
        result = PartAvailabilityResult(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402FR-0710KL",
            lcsc_part="C123456",
            quantity_needed=100,
            quantity_available=5000,
            status=AvailabilityStatus.AVAILABLE,
            in_stock=True,
            min_order_qty=10,
            price_breaks=[(1, 0.01), (100, 0.008)],
            alternatives=[
                AlternativePart(
                    lcsc_part="C654321",
                    mfr_part="RC0402FR-0710KL-ALT",
                    description="Alt part",
                    stock=10000,
                    price_diff=0.001,
                    is_basic=True,
                )
            ],
        )
        data = result.to_dict()
        assert data["reference"] == "R1"
        assert data["status"] == "available"
        assert data["sufficient_stock"] is True
        assert len(data["alternatives"]) == 1
        assert data["alternatives"][0]["lcsc_part"] == "C654321"


class TestBOMAvailabilityResult:
    """Tests for BOMAvailabilityResult dataclass."""

    @pytest.fixture
    def sample_results(self):
        """Create sample availability results."""
        return [
            PartAvailabilityResult(
                reference="R1",
                value="10k",
                footprint="0402",
                mpn=None,
                lcsc_part="C123456",
                quantity_needed=100,
                quantity_available=5000,
                status=AvailabilityStatus.AVAILABLE,
                in_stock=True,
                price_breaks=[(1, 0.01)],
            ),
            PartAvailabilityResult(
                reference="R2",
                value="10k",
                footprint="0402",
                mpn=None,
                lcsc_part="C123457",
                quantity_needed=200,
                quantity_available=100,
                status=AvailabilityStatus.LOW_STOCK,
                in_stock=True,
                price_breaks=[(1, 0.01)],
            ),
            PartAvailabilityResult(
                reference="U1",
                value="STM32",
                footprint="LQFP-48",
                mpn=None,
                lcsc_part="C8734",
                quantity_needed=10,
                quantity_available=0,
                status=AvailabilityStatus.OUT_OF_STOCK,
                in_stock=False,
                price_breaks=[(1, 2.50)],
            ),
            PartAvailabilityResult(
                reference="J1",
                value="USB-C",
                footprint="USB-C",
                mpn=None,
                lcsc_part=None,
                quantity_needed=1,
                quantity_available=0,
                status=AvailabilityStatus.NO_LCSC,
                in_stock=False,
            ),
        ]

    def test_available_property(self, sample_results):
        result = BOMAvailabilityResult(items=sample_results)
        available = result.available
        assert len(available) == 1
        assert available[0].reference == "R1"

    def test_low_stock_property(self, sample_results):
        result = BOMAvailabilityResult(items=sample_results)
        low_stock = result.low_stock
        assert len(low_stock) == 1
        assert low_stock[0].reference == "R2"

    def test_out_of_stock_property(self, sample_results):
        result = BOMAvailabilityResult(items=sample_results)
        out_of_stock = result.out_of_stock
        assert len(out_of_stock) == 1
        assert out_of_stock[0].reference == "U1"

    def test_missing_property(self, sample_results):
        result = BOMAvailabilityResult(items=sample_results)
        missing = result.missing
        assert len(missing) == 1
        assert missing[0].reference == "J1"

    def test_all_available_false(self, sample_results):
        result = BOMAvailabilityResult(items=sample_results)
        assert result.all_available is False

    def test_all_available_true(self):
        items = [
            PartAvailabilityResult(
                reference="R1",
                value="10k",
                footprint="0402",
                mpn=None,
                lcsc_part="C123456",
                quantity_needed=100,
                quantity_available=5000,
                status=AvailabilityStatus.AVAILABLE,
                in_stock=True,
            ),
            PartAvailabilityResult(
                reference="C1",
                value="100nF",
                footprint="0402",
                mpn=None,
                lcsc_part="C654321",
                quantity_needed=50,
                quantity_available=10000,
                status=AvailabilityStatus.AVAILABLE,
                in_stock=True,
            ),
        ]
        result = BOMAvailabilityResult(items=items)
        assert result.all_available is True

    def test_total_cost(self, sample_results):
        result = BOMAvailabilityResult(items=sample_results)
        # R1: 100 * 0.01 = 1.00
        # R2: 200 * 0.01 = 2.00
        # U1: 10 * 2.50 = 25.00
        # J1: no price
        assert result.total_cost is None  # Missing price for J1

    def test_total_cost_all_priced(self):
        items = [
            PartAvailabilityResult(
                reference="R1",
                value="10k",
                footprint="0402",
                mpn=None,
                lcsc_part="C123456",
                quantity_needed=100,
                quantity_available=5000,
                status=AvailabilityStatus.AVAILABLE,
                in_stock=True,
                price_breaks=[(1, 0.01)],
            ),
            PartAvailabilityResult(
                reference="C1",
                value="100nF",
                footprint="0402",
                mpn=None,
                lcsc_part="C654321",
                quantity_needed=50,
                quantity_available=10000,
                status=AvailabilityStatus.AVAILABLE,
                in_stock=True,
                price_breaks=[(1, 0.02)],
            ),
        ]
        result = BOMAvailabilityResult(items=items)
        # R1: 100 * 0.01 = 1.00
        # C1: 50 * 0.02 = 1.00
        assert result.total_cost == 2.00

    def test_summary(self, sample_results):
        result = BOMAvailabilityResult(
            items=sample_results,
            quantity_multiplier=10,
        )
        summary = result.summary()
        assert summary["total_items"] == 4
        assert summary["available"] == 1
        assert summary["low_stock"] == 1
        assert summary["out_of_stock"] == 1
        assert summary["missing"] == 1
        assert summary["all_available"] is False
        assert summary["quantity_multiplier"] == 10

    def test_to_dict(self, sample_results):
        result = BOMAvailabilityResult(
            items=sample_results,
            checked_at=datetime(2024, 1, 15, 10, 30, 0),
        )
        data = result.to_dict()
        assert "summary" in data
        assert "items" in data
        assert data["checked_at"] == "2024-01-15T10:30:00"
        assert len(data["items"]) == 4


class TestLCSCAvailabilityChecker:
    """Tests for LCSCAvailabilityChecker class."""

    def test_init_default(self):
        checker = LCSCAvailabilityChecker()
        assert checker.low_stock_threshold == 100
        assert checker.find_alternatives is True
        assert checker.max_alternatives == 3

    def test_init_custom(self):
        checker = LCSCAvailabilityChecker(
            low_stock_threshold=500,
            find_alternatives=False,
            max_alternatives=5,
        )
        assert checker.low_stock_threshold == 500
        assert checker.find_alternatives is False
        assert checker.max_alternatives == 5

    def test_determine_status_available(self):
        checker = LCSCAvailabilityChecker()
        status = checker._determine_status(stock=5000, needed=100)
        assert status == AvailabilityStatus.AVAILABLE

    def test_determine_status_out_of_stock(self):
        checker = LCSCAvailabilityChecker()
        status = checker._determine_status(stock=0, needed=100)
        assert status == AvailabilityStatus.OUT_OF_STOCK

    def test_determine_status_low_stock_insufficient(self):
        checker = LCSCAvailabilityChecker()
        status = checker._determine_status(stock=50, needed=100)
        assert status == AvailabilityStatus.LOW_STOCK

    def test_determine_status_low_stock_threshold(self):
        checker = LCSCAvailabilityChecker(low_stock_threshold=100)
        # Stock is enough for order but below threshold
        status = checker._determine_status(stock=80, needed=10)
        assert status == AvailabilityStatus.LOW_STOCK

    @patch("kicad_tools.parts.LCSCClient")
    def test_check_item_no_lcsc(self, mock_client_class):
        checker = LCSCAvailabilityChecker()
        result = checker._check_item(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402",
            lcsc=None,
            quantity_needed=100,
            parts_map={},
        )
        assert result.status == AvailabilityStatus.NO_LCSC
        assert result.error == "No LCSC part number"

    @patch("kicad_tools.parts.LCSCClient")
    def test_check_item_not_found(self, mock_client_class):
        checker = LCSCAvailabilityChecker()
        result = checker._check_item(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402",
            lcsc="C999999",
            quantity_needed=100,
            parts_map={},  # Empty - part not found
        )
        assert result.status == AvailabilityStatus.NOT_FOUND
        assert "not found" in result.error.lower()

    @patch("kicad_tools.parts.LCSCClient")
    def test_check_item_available(self, mock_client_class):
        # Create mock part
        mock_part = MagicMock()
        mock_part.stock = 5000
        mock_part.min_order = 10
        mock_part.prices = [MagicMock(quantity=1, unit_price=0.01)]
        mock_part.mfr_part = "RC0402FR-0710KL"

        checker = LCSCAvailabilityChecker()
        result = checker._check_item(
            reference="R1",
            value="10k",
            footprint="0402",
            mpn="RC0402",
            lcsc="C123456",
            quantity_needed=100,
            parts_map={"C123456": mock_part},
        )
        assert result.status == AvailabilityStatus.AVAILABLE
        assert result.in_stock is True
        assert result.quantity_available == 5000

    @patch("kicad_tools.parts.LCSCClient")
    def test_check_bom_integration(self, mock_client_class):
        """Test check_bom with mocked LCSC client."""
        # Create mock client
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Create mock part
        mock_part = MagicMock()
        mock_part.stock = 5000
        mock_part.min_order = 10
        mock_part.prices = [MagicMock(quantity=1, unit_price=0.01)]
        mock_part.mfr_part = "RC0402FR-0710KL"

        mock_client.lookup_many.return_value = {"C123456": mock_part}

        # Create mock BOM
        mock_bom = MagicMock()
        mock_group = MagicMock()
        mock_group.references = "R1, R2, R3"
        mock_group.value = "10k"
        mock_group.footprint = "0402"
        mock_group.mpn = "RC0402"
        mock_group.lcsc = "C123456"
        mock_group.quantity = 3
        mock_group.items = [MagicMock(dnp=False)]

        mock_bom.grouped.return_value = [mock_group]

        checker = LCSCAvailabilityChecker()
        result = checker.check_bom(mock_bom, quantity=10)

        assert len(result.items) == 1
        assert result.items[0].reference == "R1"
        assert result.items[0].quantity_needed == 30  # 3 parts * 10 boards

    def test_context_manager(self):
        with LCSCAvailabilityChecker() as checker:
            assert checker is not None
        # After exit, client should be cleaned up


class TestAlternativePart:
    """Tests for AlternativePart dataclass."""

    def test_basic_creation(self):
        alt = AlternativePart(
            lcsc_part="C654321",
            mfr_part="RC0402FR-0710KL-ALT",
            description="Alternative 10k resistor",
            stock=10000,
            price_diff=0.001,
            is_basic=True,
        )
        assert alt.lcsc_part == "C654321"
        assert alt.stock == 10000
        assert alt.price_diff == 0.001
        assert alt.is_basic is True

    def test_price_diff_none(self):
        alt = AlternativePart(
            lcsc_part="C654321",
            mfr_part="RC0402FR",
            description="Alt part",
            stock=5000,
            price_diff=None,
            is_basic=False,
        )
        assert alt.price_diff is None
