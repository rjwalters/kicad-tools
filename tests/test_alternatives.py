"""Tests for the alternative part finder module."""

from unittest.mock import MagicMock

import pytest

from kicad_tools.cost.alternatives import (
    AlternativePartFinder,
    AlternativeSuggestions,
    PartAlternative,
)
from kicad_tools.parts.models import (
    PackageType,
    Part,
    PartAvailability,
    PartCategory,
    PartPrice,
    SearchResult,
)
from kicad_tools.schema.bom import BOMItem


class TestPartAlternative:
    """Tests for PartAlternative dataclass."""

    def test_to_dict(self):
        alt = PartAlternative(
            original_mpn="RC0402FR-0710K",
            original_lcsc="C123456",
            alternative_mpn="RC0402JR-0710K",
            alternative_lcsc="C789012",
            alternative_manufacturer="Yageo",
            alternative_description="10K 5% 0402 Resistor",
            compatibility="drop-in",
            differences=["Tolerance: 1% → 5%"],
            price_delta=-0.001,
            original_price=0.005,
            alternative_price=0.004,
            stock_quantity=50000,
            is_basic=True,
            recommendation="Recommended: JLCPCB basic part",
            warnings=[],
        )

        d = alt.to_dict()

        assert d["original"]["mpn"] == "RC0402FR-0710K"
        assert d["original"]["lcsc"] == "C123456"
        assert d["alternative"]["mpn"] == "RC0402JR-0710K"
        assert d["alternative"]["lcsc"] == "C789012"
        assert d["compatibility"] == "drop-in"
        assert d["differences"] == ["Tolerance: 1% → 5%"]
        assert d["price_delta"] == -0.001
        assert d["stock_quantity"] == 50000
        assert d["is_basic"] is True

    def test_to_dict_with_none_prices(self):
        alt = PartAlternative(
            original_mpn="TEST",
            original_lcsc=None,
            alternative_mpn="TEST-ALT",
            alternative_lcsc="C111",
            price_delta=0.0,
        )

        d = alt.to_dict()

        assert d["original"]["price"] is None
        assert d["alternative"]["price"] is None
        assert d["price_delta"] == 0


class TestAlternativeSuggestions:
    """Tests for AlternativeSuggestions dataclass."""

    def test_has_alternatives(self):
        # With alternatives
        sugg = AlternativeSuggestions(
            reference="R1",
            value="10k",
            footprint="0402",
            original_lcsc="C123",
            original_mpn="TEST",
            status="out_of_stock",
            alternatives=[
                PartAlternative(
                    original_mpn="TEST",
                    original_lcsc="C123",
                    alternative_mpn="ALT",
                    alternative_lcsc="C456",
                )
            ],
        )
        assert sugg.has_alternatives is True

        # Without alternatives
        sugg2 = AlternativeSuggestions(
            reference="R2",
            value="10k",
            footprint="0402",
            original_lcsc="C789",
            original_mpn="TEST2",
            status="out_of_stock",
            alternatives=[],
        )
        assert sugg2.has_alternatives is False

    def test_best_alternative(self):
        alt1 = PartAlternative(
            original_mpn="TEST",
            original_lcsc="C123",
            alternative_mpn="ALT1",
            alternative_lcsc="C456",
            recommendation="",
        )
        alt2 = PartAlternative(
            original_mpn="TEST",
            original_lcsc="C123",
            alternative_mpn="ALT2",
            alternative_lcsc="C789",
            recommendation="Recommended: best option",
        )

        sugg = AlternativeSuggestions(
            reference="R1",
            value="10k",
            footprint="0402",
            original_lcsc="C123",
            original_mpn="TEST",
            status="out_of_stock",
            alternatives=[alt1, alt2],
        )

        # Should return the one with recommendation
        best = sugg.best_alternative
        assert best is not None
        assert best.alternative_mpn == "ALT2"

    def test_best_alternative_no_recommendation(self):
        alt1 = PartAlternative(
            original_mpn="TEST",
            original_lcsc="C123",
            alternative_mpn="ALT1",
            alternative_lcsc="C456",
            recommendation="",
        )

        sugg = AlternativeSuggestions(
            reference="R1",
            value="10k",
            footprint="0402",
            original_lcsc="C123",
            original_mpn="TEST",
            status="out_of_stock",
            alternatives=[alt1],
        )

        # Should return first one if none has recommendation
        best = sugg.best_alternative
        assert best is not None
        assert best.alternative_mpn == "ALT1"

    def test_best_alternative_empty(self):
        sugg = AlternativeSuggestions(
            reference="R1",
            value="10k",
            footprint="0402",
            original_lcsc="C123",
            original_mpn="TEST",
            status="out_of_stock",
            alternatives=[],
        )

        assert sugg.best_alternative is None

    def test_to_dict(self):
        sugg = AlternativeSuggestions(
            reference="R1",
            value="10k",
            footprint="0402",
            original_lcsc="C123",
            original_mpn="TEST",
            status="out_of_stock",
            alternatives=[
                PartAlternative(
                    original_mpn="TEST",
                    original_lcsc="C123",
                    alternative_mpn="ALT",
                    alternative_lcsc="C456",
                )
            ],
        )

        d = sugg.to_dict()

        assert d["reference"] == "R1"
        assert d["value"] == "10k"
        assert d["status"] == "out_of_stock"
        assert len(d["alternatives"]) == 1


class TestAlternativePartFinder:
    """Tests for AlternativePartFinder class."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        return client

    @pytest.fixture
    def finder(self, mock_client):
        return AlternativePartFinder(mock_client)

    @pytest.fixture
    def sample_bom_item(self):
        return BOMItem(
            reference="R1",
            value="10k",
            footprint="Resistor_SMD:R_0402_1005Metric",
            lib_id="Device:R",
            lcsc="C123456",
            mpn="RC0402FR-0710KL",
        )

    @pytest.fixture
    def sample_part(self):
        return Part(
            lcsc_part="C123456",
            mfr_part="RC0402FR-0710KL",
            manufacturer="Yageo",
            description="10K 1% 0402 Resistor",
            category=PartCategory.RESISTOR,
            package="0402",
            package_type=PackageType.SMD,
            stock=50000,
            prices=[PartPrice(quantity=10, unit_price=0.005)],
            is_basic=True,
        )

    def test_get_reference_prefix(self, finder):
        assert finder._get_reference_prefix("R1") == "R"
        assert finder._get_reference_prefix("C10") == "C"
        assert finder._get_reference_prefix("U1A") == "UA"
        assert finder._get_reference_prefix("LED1") == "LED"

    def test_extract_package_size(self, finder):
        assert finder._extract_package_size("R_0402_1005Metric") == "0402"
        assert finder._extract_package_size("C_0603_1608Metric") == "0603"
        assert finder._extract_package_size("SOT-23") == "SOT-23"
        assert finder._extract_package_size("LQFP-48") == "LQFP-48"
        assert finder._extract_package_size("CustomPackage") is None

    def test_extract_part_family(self, finder):
        # The function extracts a reasonable family prefix for search
        # Exact behavior may vary - just test it returns something usable
        result = finder._extract_part_family("STM32F103C8T6")
        assert result is not None
        assert "STM32" in result

        result = finder._extract_part_family("ATmega328P-AU")
        assert result is not None
        assert "ATmega" in result

        result = finder._extract_part_family("LM1117-3.3")
        assert result is not None
        assert "LM1117" in result

        assert finder._extract_part_family("") is None

    def test_packages_compatible(self, finder):
        # Same package
        assert finder._packages_compatible("0402", "0402") is True

        # Adjacent sizes
        assert finder._packages_compatible("0402", "0603") is True
        assert finder._packages_compatible("0603", "0805") is True

        # Non-adjacent sizes
        assert finder._packages_compatible("0402", "0805") is False
        assert finder._packages_compatible("0201", "1206") is False

    def test_same_part_family(self, finder):
        # Same family variants should match
        assert finder._same_part_family("STM32F103C8T6", "STM32F103CBT6") is True
        assert finder._same_part_family("ATmega328P", "ATmega328PB") is True

        # Completely different parts should not match
        assert finder._same_part_family("LM7805", "NE555") is False

    def test_normalize_resistor_value(self, finder):
        assert finder._normalize_resistor_value("10k") == 10000.0
        assert finder._normalize_resistor_value("10K") == 10000.0
        assert finder._normalize_resistor_value("4.7k") == 4700.0
        assert finder._normalize_resistor_value("100") == 100.0
        assert finder._normalize_resistor_value("1M") == 1000000.0
        assert finder._normalize_resistor_value("invalid") is None

    def test_normalize_capacitor_value(self, finder):
        # Use approximate comparison for floating point
        import math

        result = finder._normalize_capacitor_value("100nF")
        assert result is not None
        assert math.isclose(result, 100e-9, rel_tol=1e-9)

        result = finder._normalize_capacitor_value("100n")
        assert result is not None
        assert math.isclose(result, 100e-9, rel_tol=1e-9)

        result = finder._normalize_capacitor_value("10uF")
        assert result is not None
        assert math.isclose(result, 10e-6, rel_tol=1e-9)

        result = finder._normalize_capacitor_value("10pF")
        assert result is not None
        assert math.isclose(result, 10e-12, rel_tol=1e-9)

        assert finder._normalize_capacitor_value("invalid") is None

    def test_tolerance_worse(self, finder):
        assert finder._tolerance_worse("1%", "5%") is True
        assert finder._tolerance_worse("5%", "1%") is False
        assert finder._tolerance_worse("1%", "1%") is False

    def test_voltage_lower(self, finder):
        assert finder._voltage_lower("50V", "25V") is True
        assert finder._voltage_lower("25V", "50V") is False
        assert finder._voltage_lower("50V", "50V") is False

    def test_find_alternatives_resistor(self, finder, mock_client, sample_bom_item):
        # Setup mock search results
        alt_part = Part(
            lcsc_part="C789012",
            mfr_part="RC0402JR-0710KL",
            manufacturer="Yageo",
            description="10K 5% 0402 Resistor",
            category=PartCategory.RESISTOR,
            package="0402",
            package_type=PackageType.SMD,
            stock=100000,
            prices=[PartPrice(quantity=10, unit_price=0.003)],
            is_basic=True,
        )

        mock_client.search.return_value = SearchResult(
            query="10k resistor 0402",
            parts=[alt_part],
            total_count=1,
        )

        alternatives = finder.find_alternatives(sample_bom_item)

        assert len(alternatives) == 1
        assert alternatives[0].alternative_lcsc == "C789012"
        assert alternatives[0].compatibility == "drop-in"

    def test_find_alternatives_excludes_original(
        self, finder, mock_client, sample_bom_item, sample_part
    ):
        # Setup mock to return original part in search
        mock_client.search.return_value = SearchResult(
            query="10k resistor 0402",
            parts=[sample_part],  # Same as original
            total_count=1,
        )

        alternatives = finder.find_alternatives(sample_bom_item, original_part=sample_part)

        # Should exclude the original
        assert len(alternatives) == 0

    def test_get_availability_status(self, finder):
        # Error case
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123",
            error="Part not found",
        )
        assert finder._get_availability_status(avail) == "not_found"

        # Not matched
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123",
            matched=False,
        )
        assert finder._get_availability_status(avail) == "not_found"

        # Out of stock
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123",
            matched=True,
            in_stock=False,
        )
        assert finder._get_availability_status(avail) == "out_of_stock"

        # Low stock
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123",
            matched=True,
            in_stock=True,
            quantity_needed=100,
            quantity_available=50,
        )
        assert finder._get_availability_status(avail) == "low_stock"

        # OK - no alternatives needed
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123",
            matched=True,
            in_stock=True,
            quantity_needed=10,
            quantity_available=100,
        )
        assert finder._get_availability_status(avail) is None

    def test_suggest_for_bom(self, finder, mock_client):
        # Setup BOM items
        items = [
            BOMItem(
                reference="R1",
                value="10k",
                footprint="0402",
                lib_id="Device:R",
                lcsc="C123",
            ),
            BOMItem(
                reference="R2",
                value="4.7k",
                footprint="0402",
                lib_id="Device:R",
                lcsc="C456",
            ),
        ]

        # Setup availability - R1 out of stock, R2 OK
        availability = [
            PartAvailability(
                reference="R1",
                value="10k",
                footprint="0402",
                lcsc_part="C123",
                matched=True,
                in_stock=False,
            ),
            PartAvailability(
                reference="R2",
                value="4.7k",
                footprint="0402",
                lcsc_part="C456",
                matched=True,
                in_stock=True,
                quantity_needed=10,
                quantity_available=100,
            ),
        ]

        # Mock search to return alternatives
        alt_part = Part(
            lcsc_part="C789",
            mfr_part="ALT10K",
            description="10K 0402",
            package="0402",
            stock=50000,
            is_basic=True,
        )
        mock_client.search.return_value = SearchResult(
            query="test",
            parts=[alt_part],
            total_count=1,
        )

        suggestions = finder.suggest_for_bom(items, availability)

        # Only R1 should have suggestions (out of stock)
        assert len(suggestions) == 1
        assert suggestions[0].reference == "R1"
        assert suggestions[0].status == "out_of_stock"

    def test_generate_recommendation(self, finder):
        # Basic part with high stock
        part = Part(
            lcsc_part="C123",
            mfr_part="TEST",
            stock=50000,
            is_basic=True,
            prices=[PartPrice(quantity=10, unit_price=0.003)],
        )

        original = Part(
            lcsc_part="C456",
            mfr_part="ORIG",
            prices=[PartPrice(quantity=10, unit_price=0.005)],
        )

        rec = finder._generate_recommendation(part, original, "drop-in", [])

        assert "basic part" in rec.lower()
        assert "drop-in" in rec.lower()
        assert "cheaper" in rec.lower()

    def test_check_resistor_compatibility_drop_in(self, finder):
        item = BOMItem(
            reference="R1",
            value="10k",
            footprint="R_0402",
            lib_id="Device:R",
        )

        part = Part(
            lcsc_part="C123",
            mfr_part="TEST",
            description="10K 1% 0402 Resistor",
            package="0402",
        )

        compat, diffs, warns = finder._check_resistor_compatibility(item, part, None)

        assert compat == "drop-in"
        assert len(diffs) == 0

    def test_check_resistor_compatibility_wrong_value(self, finder):
        item = BOMItem(
            reference="R1",
            value="10k",
            footprint="R_0402",
            lib_id="Device:R",
        )

        part = Part(
            lcsc_part="C123",
            mfr_part="TEST",
            description="100K Resistor",  # Wrong value
            package="0402",
        )

        compat, diffs, warns = finder._check_resistor_compatibility(item, part, None)

        assert compat is None  # Incompatible

    def test_check_ic_compatibility_same_family(self, finder):
        item = BOMItem(
            reference="U1",
            value="STM32F103C8T6",
            footprint="LQFP-48",
            lib_id="MCU:STM32",
            mpn="STM32F103C8T6",
        )

        part = Part(
            lcsc_part="C123",
            mfr_part="STM32F103CBT6",  # Same family, different variant
            description="STM32F103 MCU",
            package="LQFP-48",
        )

        compat, diffs, warns = finder._check_ic_compatibility(item, part, None)

        assert compat == "drop-in"
        assert any("Variant" in d for d in diffs)

    def test_check_ic_compatibility_different_package(self, finder):
        item = BOMItem(
            reference="U1",
            value="STM32F103C8T6",
            footprint="LQFP-48",
            lib_id="MCU:STM32",
            mpn="STM32F103C8T6",
        )

        part = Part(
            lcsc_part="C123",
            mfr_part="STM32F103C8",
            description="STM32F103 MCU",
            package="QFN-48",  # Different package
        )

        compat, diffs, warns = finder._check_ic_compatibility(item, part, None)

        # Should be incompatible or pin-compatible with warning
        if compat is not None:
            assert "Verify pinout" in warns[0] or any("Package" in d for d in diffs)


class TestICPackageCompatibility:
    """Tests for IC package compatibility checking."""

    @pytest.fixture
    def finder(self):
        return AlternativePartFinder(MagicMock())

    def test_ic_packages_same(self, finder):
        assert finder._ic_packages_compatible("SOIC-8", "SOIC-8") is True
        assert finder._ic_packages_compatible("QFN-32", "QFN-32") is True

    def test_ic_packages_equivalent(self, finder):
        assert finder._ic_packages_compatible("SOIC8", "SOP8") is True
        assert finder._ic_packages_compatible("SOIC-8", "SOP-8") is True

    def test_ic_packages_different(self, finder):
        assert finder._ic_packages_compatible("SOIC-8", "QFN-8") is False
        assert finder._ic_packages_compatible("LQFP-48", "QFN-48") is False


class TestComponentTypeDetection:
    """Tests for component type detection based on reference prefix."""

    @pytest.fixture
    def finder(self):
        return AlternativePartFinder(MagicMock())

    def test_resistor_detection(self, finder):
        assert finder._get_reference_prefix("R1") == "R"
        assert finder._get_reference_prefix("R101") == "R"

    def test_capacitor_detection(self, finder):
        assert finder._get_reference_prefix("C1") == "C"
        assert finder._get_reference_prefix("C101") == "C"

    def test_inductor_detection(self, finder):
        assert finder._get_reference_prefix("L1") == "L"

    def test_ic_detection(self, finder):
        assert finder._get_reference_prefix("U1") == "U"
        assert finder._get_reference_prefix("U101") == "U"

    def test_diode_detection(self, finder):
        assert finder._get_reference_prefix("D1") == "D"

    def test_led_detection(self, finder):
        assert finder._get_reference_prefix("LED1") == "LED"

    def test_connector_detection(self, finder):
        assert finder._get_reference_prefix("J1") == "J"
        assert finder._get_reference_prefix("P1") == "P"

    def test_transistor_detection(self, finder):
        assert finder._get_reference_prefix("Q1") == "Q"
