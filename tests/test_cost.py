"""Tests for the cost estimation module."""

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cost import (
    AssemblyCost,
    ComponentCost,
    CostEstimate,
    ManufacturingCostEstimator,
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
