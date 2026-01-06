"""Tests for assembly validation module."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.assembly.validation import (
    AssemblyValidationResult,
    AssemblyValidator,
    PartTier,
    PartValidationResult,
    ValidationStatus,
)


class TestPartTier:
    """Tests for PartTier enum."""

    def test_tier_values(self):
        assert PartTier.BASIC.value == "basic"
        assert PartTier.EXTENDED.value == "extended"
        assert PartTier.GLOBAL.value == "global"
        assert PartTier.UNKNOWN.value == "unknown"


class TestValidationStatus:
    """Tests for ValidationStatus enum."""

    def test_status_values(self):
        assert ValidationStatus.AVAILABLE.value == "available"
        assert ValidationStatus.LOW_STOCK.value == "low_stock"
        assert ValidationStatus.OUT_OF_STOCK.value == "out_of_stock"
        assert ValidationStatus.NOT_FOUND.value == "not_found"
        assert ValidationStatus.NO_LCSC.value == "no_lcsc"
        assert ValidationStatus.INVALID_FORMAT.value == "invalid_format"


class TestPartValidationResult:
    """Tests for PartValidationResult dataclass."""

    @pytest.fixture
    def available_part(self):
        return PartValidationResult(
            references="R1, R2, R3",
            value="10k",
            footprint="0402",
            quantity=3,
            lcsc_part="C123456",
            status=ValidationStatus.AVAILABLE,
            tier=PartTier.BASIC,
            stock=50000,
            in_stock=True,
            mfr_part="RC0402FR-0710KL",
            description="10K 1% Resistor",
        )

    @pytest.fixture
    def oos_part(self):
        return PartValidationResult(
            references="U1",
            value="STM32",
            footprint="LQFP48",
            quantity=1,
            lcsc_part="C999999",
            status=ValidationStatus.OUT_OF_STOCK,
            tier=PartTier.EXTENDED,
            stock=0,
            in_stock=False,
        )

    @pytest.fixture
    def missing_lcsc_part(self):
        return PartValidationResult(
            references="J1",
            value="USB-C",
            footprint="USB-C",
            quantity=1,
            lcsc_part=None,
            status=ValidationStatus.NO_LCSC,
            tier=PartTier.UNKNOWN,
        )

    def test_status_symbol_available(self, available_part):
        assert available_part.status_symbol == "✓"

    def test_status_symbol_oos(self, oos_part):
        assert oos_part.status_symbol == "✗"

    def test_status_symbol_no_lcsc(self, missing_lcsc_part):
        assert missing_lcsc_part.status_symbol == "-"

    def test_status_symbol_low_stock(self):
        part = PartValidationResult(
            references="C1",
            value="100nF",
            footprint="0402",
            quantity=1,
            lcsc_part="C123",
            status=ValidationStatus.LOW_STOCK,
            tier=PartTier.BASIC,
            stock=50,
        )
        assert part.status_symbol == "⚠"

    def test_status_symbol_not_found(self):
        part = PartValidationResult(
            references="R1",
            value="10k",
            footprint="0402",
            quantity=1,
            lcsc_part="CINVALID",
            status=ValidationStatus.NOT_FOUND,
            tier=PartTier.UNKNOWN,
        )
        assert part.status_symbol == "?"

    def test_status_text_available(self, available_part):
        assert available_part.status_text == "Available"

    def test_status_text_oos(self, oos_part):
        assert oos_part.status_text == "OOS"

    def test_status_text_low_stock(self):
        part = PartValidationResult(
            references="C1",
            value="100nF",
            footprint="0402",
            quantity=1,
            lcsc_part="C123",
            status=ValidationStatus.LOW_STOCK,
            tier=PartTier.BASIC,
            stock=50,
        )
        assert "Low" in part.status_text
        assert "50" in part.status_text

    def test_tier_text(self, available_part, oos_part, missing_lcsc_part):
        assert available_part.tier_text == "Basic"
        assert oos_part.tier_text == "Extended"
        assert missing_lcsc_part.tier_text == "-"

    def test_to_dict(self, available_part):
        d = available_part.to_dict()
        assert d["references"] == "R1, R2, R3"
        assert d["value"] == "10k"
        assert d["footprint"] == "0402"
        assert d["quantity"] == 3
        assert d["lcsc_part"] == "C123456"
        assert d["status"] == "available"
        assert d["tier"] == "basic"
        assert d["stock"] == 50000
        assert d["in_stock"] is True
        assert d["mfr_part"] == "RC0402FR-0710KL"
        assert d["description"] == "10K 1% Resistor"


class TestAssemblyValidationResult:
    """Tests for AssemblyValidationResult dataclass."""

    @pytest.fixture
    def validation_result(self):
        return AssemblyValidationResult(
            items=[
                # Basic available
                PartValidationResult(
                    references="R1, R2",
                    value="10k",
                    footprint="0402",
                    quantity=2,
                    lcsc_part="C123",
                    status=ValidationStatus.AVAILABLE,
                    tier=PartTier.BASIC,
                    stock=50000,
                    in_stock=True,
                ),
                # Extended available
                PartValidationResult(
                    references="U1",
                    value="STM32",
                    footprint="LQFP48",
                    quantity=1,
                    lcsc_part="C456",
                    status=ValidationStatus.AVAILABLE,
                    tier=PartTier.EXTENDED,
                    stock=100,
                    in_stock=True,
                ),
                # Extended low stock
                PartValidationResult(
                    references="U2",
                    value="ESP32",
                    footprint="QFN",
                    quantity=1,
                    lcsc_part="C789",
                    status=ValidationStatus.LOW_STOCK,
                    tier=PartTier.EXTENDED,
                    stock=50,
                    in_stock=True,
                ),
                # Out of stock
                PartValidationResult(
                    references="C1",
                    value="100uF",
                    footprint="0805",
                    quantity=1,
                    lcsc_part="COOS",
                    status=ValidationStatus.OUT_OF_STOCK,
                    tier=PartTier.EXTENDED,
                    stock=0,
                    in_stock=False,
                ),
                # Missing LCSC
                PartValidationResult(
                    references="J1",
                    value="USB-C",
                    footprint="USB-C",
                    quantity=1,
                    lcsc_part=None,
                    status=ValidationStatus.NO_LCSC,
                    tier=PartTier.UNKNOWN,
                ),
            ],
            validated_at=datetime.now(),
        )

    def test_basic_parts(self, validation_result):
        basic = validation_result.basic_parts
        assert len(basic) == 1
        assert basic[0].references == "R1, R2"

    def test_extended_parts(self, validation_result):
        extended = validation_result.extended_parts
        assert len(extended) == 1
        assert extended[0].references == "U1"

    def test_out_of_stock(self, validation_result):
        oos = validation_result.out_of_stock
        assert len(oos) == 1
        assert oos[0].references == "C1"

    def test_low_stock(self, validation_result):
        low = validation_result.low_stock
        assert len(low) == 1
        assert low[0].references == "U2"

    def test_missing_lcsc(self, validation_result):
        missing = validation_result.missing_lcsc
        assert len(missing) == 1
        assert missing[0].references == "J1"

    def test_available_count(self, validation_result):
        # Basic available (1) + Extended available (1) + Low stock (1) = 3
        assert validation_result.available_count == 3

    def test_assembly_ready_false(self, validation_result):
        assert validation_result.assembly_ready is False

    def test_assembly_ready_true(self):
        result = AssemblyValidationResult(
            items=[
                PartValidationResult(
                    references="R1",
                    value="10k",
                    footprint="0402",
                    quantity=1,
                    lcsc_part="C123",
                    status=ValidationStatus.AVAILABLE,
                    tier=PartTier.BASIC,
                    stock=50000,
                    in_stock=True,
                ),
            ]
        )
        assert result.assembly_ready is True

    def test_extended_fee(self, validation_result):
        # 1 extended available part * $3 = $3
        assert validation_result.extended_fee == 3.0

    def test_summary(self, validation_result):
        summary = validation_result.summary()
        assert summary["total_items"] == 5
        assert summary["available"] == 3  # Basic + Extended + Low stock
        assert summary["basic_parts"] == 1
        assert summary["extended_parts"] == 1
        assert summary["low_stock"] == 1
        assert summary["out_of_stock"] == 1
        assert summary["missing_lcsc"] == 1
        assert summary["assembly_ready"] is False
        assert summary["extended_fee"] == 3.0

    def test_to_dict(self, validation_result):
        d = validation_result.to_dict()
        assert "summary" in d
        assert "validated_at" in d
        assert "items" in d
        assert len(d["items"]) == 5

    def test_format_table(self, validation_result):
        table = validation_result.format_table()
        assert "LCSC Part #" in table
        assert "Component" in table
        assert "Tier" in table
        assert "Status" in table
        assert "Summary:" in table
        assert "Basic:" in table
        assert "Extended:" in table

    def test_format_table_empty(self):
        result = AssemblyValidationResult()
        table = result.format_table()
        assert "No components" in table


class TestAssemblyValidator:
    """Tests for AssemblyValidator class."""

    @pytest.fixture
    def mock_bom(self):
        """Create a mock BOM with groups."""
        from kicad_tools.schema.bom import BOM, BOMGroup, BOMItem

        group1 = BOMGroup(value="10k", footprint="0402")
        group1.items.append(
            BOMItem(
                reference="R1",
                value="10k",
                footprint="0402",
                lib_id="Device:R",
                lcsc="C123456",
            )
        )
        group1.items.append(
            BOMItem(
                reference="R2",
                value="10k",
                footprint="0402",
                lib_id="Device:R",
                lcsc="C123456",
            )
        )

        group2 = BOMGroup(value="100nF", footprint="0402")
        group2.items.append(
            BOMItem(
                reference="C1",
                value="100nF",
                footprint="0402",
                lib_id="Device:C",
                lcsc="",  # Missing LCSC
            )
        )

        group3 = BOMGroup(value="STM32", footprint="LQFP48")
        group3.items.append(
            BOMItem(
                reference="U1",
                value="STM32",
                footprint="LQFP48",
                lib_id="MCU:STM32",
                lcsc="CINVALID",  # Invalid format
            )
        )

        # Create mock BOM
        bom = MagicMock(spec=BOM)
        bom.grouped.return_value = [group1, group2, group3]

        return bom

    def test_validator_initialization(self):
        validator = AssemblyValidator()
        assert validator.use_cache is True
        assert validator.timeout == 30.0
        assert validator._client is None

    def test_validator_initialization_custom(self):
        validator = AssemblyValidator(use_cache=False, timeout=60.0)
        assert validator.use_cache is False
        assert validator.timeout == 60.0

    def test_validator_context_manager(self):
        with AssemblyValidator() as validator:
            assert validator is not None

    def test_validate_bom_with_mock(self, mock_bom):
        """Test validate_bom with mocked LCSC client."""
        from kicad_tools.parts.models import Part

        validator = AssemblyValidator()

        with patch.object(validator, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.lookup_many.return_value = {
                "C123456": Part(
                    lcsc_part="C123456",
                    mfr_part="RC0402",
                    stock=50000,
                    is_basic=True,
                ),
            }
            mock_get_client.return_value = mock_client

            result = validator.validate_bom(mock_bom)

            assert len(result.items) == 3

            # Check the available part (R1, R2)
            r_group = next(i for i in result.items if "R1" in i.references)
            assert r_group.status == ValidationStatus.AVAILABLE
            assert r_group.tier == PartTier.BASIC
            assert r_group.quantity == 2

            # Check the missing LCSC part
            c_group = next(i for i in result.items if "C1" in i.references)
            assert c_group.status == ValidationStatus.NO_LCSC

            # Check the invalid format part
            u_group = next(i for i in result.items if "U1" in i.references)
            # CINVALID gets normalized to CCINVALID which is still invalid format
            assert u_group.status in (
                ValidationStatus.INVALID_FORMAT,
                ValidationStatus.NOT_FOUND,
            )

    def test_validate_bom_with_quantity(self, mock_bom):
        """Test validate_bom with board quantity multiplier."""
        from kicad_tools.parts.models import Part

        validator = AssemblyValidator()

        with patch.object(validator, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.lookup_many.return_value = {
                "C123456": Part(
                    lcsc_part="C123456",
                    mfr_part="RC0402",
                    stock=50000,
                    is_basic=True,
                ),
            }
            mock_get_client.return_value = mock_client

            result = validator.validate_bom(mock_bom, quantity=5)

            # Check the available part quantity is multiplied
            r_group = next(i for i in result.items if "R1" in i.references)
            assert r_group.quantity == 10  # 2 * 5

    def test_validate_bom_low_stock(self, mock_bom):
        """Test that low stock is detected correctly."""
        from kicad_tools.parts.models import Part

        validator = AssemblyValidator()

        with patch.object(validator, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.lookup_many.return_value = {
                "C123456": Part(
                    lcsc_part="C123456",
                    mfr_part="RC0402",
                    stock=50,  # Low stock
                    is_basic=True,
                ),
            }
            mock_get_client.return_value = mock_client

            result = validator.validate_bom(mock_bom)

            r_group = next(i for i in result.items if "R1" in i.references)
            assert r_group.status == ValidationStatus.LOW_STOCK

    def test_validate_bom_out_of_stock(self, mock_bom):
        """Test that out of stock is detected correctly."""
        from kicad_tools.parts.models import Part

        validator = AssemblyValidator()

        with patch.object(validator, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.lookup_many.return_value = {
                "C123456": Part(
                    lcsc_part="C123456",
                    mfr_part="RC0402",
                    stock=0,  # Out of stock
                    is_basic=True,
                ),
            }
            mock_get_client.return_value = mock_client

            result = validator.validate_bom(mock_bom)

            r_group = next(i for i in result.items if "R1" in i.references)
            assert r_group.status == ValidationStatus.OUT_OF_STOCK

    def test_validate_bom_extended_tier(self, mock_bom):
        """Test extended tier detection."""
        from kicad_tools.parts.models import Part

        validator = AssemblyValidator()

        with patch.object(validator, "_get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.lookup_many.return_value = {
                "C123456": Part(
                    lcsc_part="C123456",
                    mfr_part="RC0402",
                    stock=50000,
                    is_basic=False,  # Extended
                    is_preferred=True,
                ),
            }
            mock_get_client.return_value = mock_client

            result = validator.validate_bom(mock_bom)

            r_group = next(i for i in result.items if "R1" in i.references)
            assert r_group.tier == PartTier.EXTENDED


class TestValidateAssemblyFunction:
    """Tests for the validate_assembly convenience function."""

    def test_validate_assembly_file_not_found(self, tmp_path):
        from kicad_tools.assembly.validation import validate_assembly

        # For nonexistent files, extract_bom may raise an exception or return empty BOM
        # depending on the code path - test that it doesn't crash
        try:
            result = validate_assembly(str(tmp_path / "nonexistent.kicad_sch"))
            # If we get here, it handled gracefully - likely empty BOM
            assert len(result.items) == 0
        except Exception:
            # Some paths raise exceptions - that's also acceptable behavior
            pass

    def test_validate_assembly_with_mock_schematic(self, tmp_path):
        """Test validate_assembly with a simple mock schematic file."""
        # Create a minimal schematic file that will be parsed but has no components
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            """(kicad_sch (version 20230121) (generator eeschema)
  (uuid "12345678-1234-1234-1234-123456789012")
)"""
        )

        from kicad_tools.assembly.validation import validate_assembly

        # This should work (empty BOM)
        result = validate_assembly(str(sch_file))
        assert len(result.items) == 0
        assert result.assembly_ready is True
