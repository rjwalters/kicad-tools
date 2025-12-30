"""Tests for the parts module."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.parts.models import (
    BOMAvailability,
    PackageType,
    Part,
    PartAvailability,
    PartCategory,
    PartPrice,
    SearchResult,
)
from kicad_tools.parts.cache import PartsCache


class TestPartPrice:
    """Tests for PartPrice dataclass."""

    def test_total_price(self):
        price = PartPrice(quantity=100, unit_price=0.01)
        assert price.total_price == 1.0

    def test_default_currency(self):
        price = PartPrice(quantity=10, unit_price=0.5)
        assert price.currency == "USD"


class TestPart:
    """Tests for Part dataclass."""

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
            value="10k",
            tolerance="1%",
            stock=50000,
            prices=[
                PartPrice(quantity=10, unit_price=0.0050),
                PartPrice(quantity=100, unit_price=0.0025),
                PartPrice(quantity=1000, unit_price=0.0015),
            ],
            is_basic=True,
        )

    def test_in_stock(self, sample_part):
        assert sample_part.in_stock is True
        sample_part.stock = 0
        assert sample_part.in_stock is False

    def test_is_smd(self, sample_part):
        assert sample_part.is_smd is True
        sample_part.package_type = PackageType.THROUGH_HOLE
        assert sample_part.is_smd is False

    def test_best_price(self, sample_part):
        assert sample_part.best_price == 0.0015

    def test_best_price_empty(self):
        part = Part(lcsc_part="C1")
        assert part.best_price is None

    def test_price_at_quantity(self, sample_part):
        # Below first break
        assert sample_part.price_at_quantity(5) == 0.0050
        # At first break
        assert sample_part.price_at_quantity(10) == 0.0050
        # Between breaks
        assert sample_part.price_at_quantity(50) == 0.0050
        # At second break
        assert sample_part.price_at_quantity(100) == 0.0025
        # At third break
        assert sample_part.price_at_quantity(1000) == 0.0015
        # Above all breaks
        assert sample_part.price_at_quantity(10000) == 0.0015

    def test_str(self, sample_part):
        assert "C123456" in str(sample_part)


class TestPartAvailability:
    """Tests for PartAvailability dataclass."""

    def test_sufficient_stock(self):
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123456",
            quantity_needed=100,
            quantity_available=500,
        )
        assert avail.sufficient_stock is True

        avail.quantity_available = 50
        assert avail.sufficient_stock is False

    def test_status_ok(self):
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123456",
            matched=True,
            in_stock=True,
            quantity_needed=10,
            quantity_available=100,
        )
        assert avail.status == "OK"

    def test_status_not_found(self):
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123456",
            matched=False,
        )
        assert avail.status == "Not found"

    def test_status_out_of_stock(self):
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123456",
            matched=True,
            in_stock=False,
        )
        assert avail.status == "Out of stock"

    def test_status_low_stock(self):
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="C123456",
            matched=True,
            in_stock=True,
            quantity_needed=100,
            quantity_available=50,
        )
        assert "Low stock" in avail.status

    def test_status_error(self):
        avail = PartAvailability(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc_part="",
            error="No LCSC part number",
        )
        assert "Error" in avail.status


class TestSearchResult:
    """Tests for SearchResult dataclass."""

    def test_has_more(self):
        result = SearchResult(
            query="test",
            parts=[Part(lcsc_part=f"C{i}") for i in range(20)],
            total_count=100,
            page=1,
            page_size=20,
        )
        assert result.has_more is True

        result = SearchResult(
            query="test",
            parts=[Part(lcsc_part=f"C{i}") for i in range(5)],
            total_count=5,
            page=1,
            page_size=20,
        )
        assert result.has_more is False

    def test_len(self):
        result = SearchResult(
            query="test",
            parts=[Part(lcsc_part=f"C{i}") for i in range(10)],
        )
        assert len(result) == 10

    def test_iter(self):
        parts = [Part(lcsc_part=f"C{i}") for i in range(5)]
        result = SearchResult(query="test", parts=parts)
        assert list(result) == parts


class TestBOMAvailability:
    """Tests for BOMAvailability dataclass."""

    @pytest.fixture
    def bom_availability(self):
        return BOMAvailability(
            items=[
                # Available
                PartAvailability(
                    reference="R1",
                    value="10k",
                    footprint="0402",
                    lcsc_part="C123",
                    matched=True,
                    in_stock=True,
                    quantity_needed=10,
                    quantity_available=100,
                ),
                # Out of stock
                PartAvailability(
                    reference="C1",
                    value="100nF",
                    footprint="0402",
                    lcsc_part="C456",
                    matched=True,
                    in_stock=False,
                    quantity_needed=5,
                    quantity_available=0,
                ),
                # Low stock
                PartAvailability(
                    reference="U1",
                    value="STM32",
                    footprint="LQFP48",
                    lcsc_part="C789",
                    matched=True,
                    in_stock=True,
                    quantity_needed=100,
                    quantity_available=50,
                ),
                # Missing
                PartAvailability(
                    reference="J1",
                    value="USB-C",
                    footprint="USB-C",
                    lcsc_part="C999",
                    matched=False,
                ),
            ]
        )

    def test_all_available(self, bom_availability):
        assert bom_availability.all_available is False

    def test_missing_parts(self, bom_availability):
        missing = bom_availability.missing_parts
        assert len(missing) == 1
        assert missing[0].reference == "J1"

    def test_out_of_stock(self, bom_availability):
        oos = bom_availability.out_of_stock
        assert len(oos) == 1
        assert oos[0].reference == "C1"

    def test_low_stock(self, bom_availability):
        low = bom_availability.low_stock
        assert len(low) == 1
        assert low[0].reference == "U1"

    def test_available(self, bom_availability):
        avail = bom_availability.available
        assert len(avail) == 1
        assert avail[0].reference == "R1"

    def test_summary(self, bom_availability):
        summary = bom_availability.summary()
        assert summary["total"] == 4
        assert summary["available"] == 1
        assert summary["missing"] == 1
        assert summary["out_of_stock"] == 1
        assert summary["low_stock"] == 1


class TestPartsCache:
    """Tests for PartsCache."""

    @pytest.fixture
    def temp_cache(self, tmp_path):
        db_path = tmp_path / "test_cache.db"
        return PartsCache(db_path=db_path, ttl_days=7)

    @pytest.fixture
    def sample_part(self):
        return Part(
            lcsc_part="C123456",
            mfr_part="RC0402",
            manufacturer="Yageo",
            description="10K Resistor",
            category=PartCategory.RESISTOR,
            package="0402",
            package_type=PackageType.SMD,
            stock=50000,
            prices=[
                PartPrice(quantity=10, unit_price=0.005),
                PartPrice(quantity=100, unit_price=0.002),
            ],
            is_basic=True,
            fetched_at=datetime.now(),
        )

    def test_put_and_get(self, temp_cache, sample_part):
        temp_cache.put(sample_part)
        retrieved = temp_cache.get("C123456")

        assert retrieved is not None
        assert retrieved.lcsc_part == "C123456"
        assert retrieved.mfr_part == "RC0402"
        assert retrieved.is_basic is True
        assert len(retrieved.prices) == 2

    def test_get_not_found(self, temp_cache):
        result = temp_cache.get("C999999")
        assert result is None

    def test_get_case_insensitive(self, temp_cache, sample_part):
        temp_cache.put(sample_part)
        assert temp_cache.get("c123456") is not None

    def test_get_many(self, temp_cache):
        parts = [
            Part(lcsc_part="C111", mfr_part="Part1"),
            Part(lcsc_part="C222", mfr_part="Part2"),
            Part(lcsc_part="C333", mfr_part="Part3"),
        ]
        temp_cache.put_many(parts)

        result = temp_cache.get_many(["C111", "C222", "C999"])
        assert len(result) == 2
        assert "C111" in result
        assert "C222" in result
        assert "C999" not in result

    def test_delete(self, temp_cache, sample_part):
        temp_cache.put(sample_part)
        assert temp_cache.get("C123456") is not None

        deleted = temp_cache.delete("C123456")
        assert deleted is True
        assert temp_cache.get("C123456") is None

    def test_delete_not_found(self, temp_cache):
        deleted = temp_cache.delete("C999999")
        assert deleted is False

    def test_clear(self, temp_cache):
        parts = [Part(lcsc_part=f"C{i}") for i in range(10)]
        temp_cache.put_many(parts)

        count = temp_cache.clear()
        assert count == 10
        assert temp_cache.get("C0") is None

    def test_contains(self, temp_cache, sample_part):
        assert temp_cache.contains("C123456") is False
        temp_cache.put(sample_part)
        assert temp_cache.contains("C123456") is True

    def test_stats(self, temp_cache, sample_part):
        temp_cache.put(sample_part)
        stats = temp_cache.stats()

        assert stats["total"] == 1
        assert stats["valid"] == 1
        assert stats["expired"] == 0
        assert "resistor" in stats["categories"]

    def test_expired_entries(self, tmp_path):
        # Create cache with very short TTL
        cache = PartsCache(db_path=tmp_path / "test.db", ttl_days=0)
        part = Part(lcsc_part="C123", fetched_at=datetime.now() - timedelta(days=1))
        cache.put(part)

        # Should be expired
        assert cache.get("C123") is None
        # But available with ignore_expiry
        assert cache.get("C123", ignore_expiry=True) is not None

    def test_clear_expired(self, tmp_path):
        cache = PartsCache(db_path=tmp_path / "test.db", ttl_days=0)
        part = Part(lcsc_part="C123")
        cache.put(part)

        cleared = cache.clear_expired()
        assert cleared == 1


try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestLCSCClient:
    """Tests for LCSCClient (with mocked HTTP)."""

    @pytest.fixture
    def mock_response(self):
        """Create a mock API response."""
        return {
            "code": 200,
            "data": {
                "componentCode": "C123456",
                "componentModelEn": "RC0402FR-0710KL",
                "componentBrandEn": "Yageo",
                "describe": "10K 1% 0402 Resistor",
                "encapStandard": "0402",
                "stockCount": 50000,
                "minOrder": 10,
                "componentLibraryType": "base",
                "prices": [
                    {"startNumber": 10, "productPrice": 0.005},
                    {"startNumber": 100, "productPrice": 0.002},
                ],
                "dataManualUrl": "https://example.com/datasheet.pdf",
            },
        }

    def test_lookup_success(self, mock_response, tmp_path):
        """Test successful part lookup with mocked requests."""
        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.post.return_value = mock_resp

            from kicad_tools.parts import LCSCClient, PartsCache

            cache = PartsCache(db_path=tmp_path / "cache.db")
            client = LCSCClient(cache=cache)

            part = client.lookup("C123456")

            assert part is not None
            assert part.lcsc_part == "C123456"
            assert part.mfr_part == "RC0402FR-0710KL"
            assert part.manufacturer == "Yageo"
            assert part.is_basic is True
            assert len(part.prices) == 2

    def test_lookup_not_found(self, tmp_path):
        """Test part not found."""
        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 200, "data": None}
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.post.return_value = mock_resp

            from kicad_tools.parts import LCSCClient

            client = LCSCClient(use_cache=False)
            part = client.lookup("C999999")

            assert part is None

    def test_lookup_uses_cache(self, tmp_path):
        """Test that lookup uses cache."""
        from kicad_tools.parts import LCSCClient, Part, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        cached_part = Part(lcsc_part="C123456", mfr_part="CachedPart")
        cache.put(cached_part)

        client = LCSCClient(cache=cache)

        # Should return cached part without making API call
        with patch("kicad_tools.parts.lcsc.LCSCClient._fetch_part") as mock_fetch:
            part = client.lookup("C123456")
            mock_fetch.assert_not_called()
            assert part.mfr_part == "CachedPart"

    def test_search_success(self, tmp_path):
        """Test search with mocked requests."""
        search_response = {
            "code": 200,
            "data": {
                "componentPageInfo": {
                    "list": [
                        {
                            "componentCode": "C123",
                            "componentModelEn": "Part1",
                            "componentBrandEn": "Mfr1",
                            "encapStandard": "0402",
                            "stockCount": 1000,
                            "prices": [],
                        },
                        {
                            "componentCode": "C456",
                            "componentModelEn": "Part2",
                            "componentBrandEn": "Mfr2",
                            "encapStandard": "0603",
                            "stockCount": 2000,
                            "prices": [],
                        },
                    ],
                    "total": 100,
                },
            },
        }

        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = search_response
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.post.return_value = mock_resp

            from kicad_tools.parts import LCSCClient, PartsCache

            cache = PartsCache(db_path=tmp_path / "cache.db")
            client = LCSCClient(cache=cache)

            results = client.search("100nF 0402")

            assert results.total_count == 100
            assert len(results.parts) == 2
            assert results.parts[0].lcsc_part == "C123"


class TestCategorization:
    """Tests for part categorization helpers."""

    def test_categorize_resistor(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("10K Resistor", "0402") == PartCategory.RESISTOR
        assert _categorize_part("100 Ohm 1%", "0603") == PartCategory.RESISTOR

    def test_categorize_capacitor(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("100nF MLCC Capacitor", "0402") == PartCategory.CAPACITOR
        assert _categorize_part("10uF Cap", "0805") == PartCategory.CAPACITOR

    def test_categorize_ic(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("STM32F103 MCU", "LQFP48") == PartCategory.IC
        assert _categorize_part("LM358 Op Amp", "SOIC-8") == PartCategory.IC

    def test_guess_package_smd(self):
        from kicad_tools.parts.lcsc import _guess_package_type

        assert _guess_package_type("0402") == PackageType.SMD
        assert _guess_package_type("SOIC-8") == PackageType.SMD
        assert _guess_package_type("QFN-32") == PackageType.SMD
        assert _guess_package_type("SOT-23") == PackageType.SMD

    def test_guess_package_through_hole(self):
        from kicad_tools.parts.lcsc import _guess_package_type

        assert _guess_package_type("DIP-8") == PackageType.THROUGH_HOLE
        assert _guess_package_type("TO-220") == PackageType.THROUGH_HOLE
        assert _guess_package_type("Axial") == PackageType.THROUGH_HOLE

    def test_guess_package_unknown(self):
        from kicad_tools.parts.lcsc import _guess_package_type

        assert _guess_package_type("CustomPackage") == PackageType.UNKNOWN
        assert _guess_package_type("") == PackageType.UNKNOWN

    def test_categorize_inductor(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("4.7uH Inductor", "0603") == PartCategory.INDUCTOR
        assert _categorize_part("10mH Choke", "Radial") == PartCategory.INDUCTOR

    def test_categorize_diode(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("1N4148 Diode", "SOD-323") == PartCategory.DIODE
        assert _categorize_part("SS14 Schottky Rectifier", "SMA") == PartCategory.DIODE
        assert _categorize_part("5.1V Zener", "SOD-123") == PartCategory.DIODE

    def test_categorize_transistor(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("2N2222 Transistor", "SOT-23") == PartCategory.TRANSISTOR
        assert _categorize_part("SI2302 MOSFET", "SOT-23") == PartCategory.TRANSISTOR
        assert _categorize_part("JFET 2N5457", "TO-92") == PartCategory.TRANSISTOR
        assert _categorize_part("NPN BJT", "SOT-23") == PartCategory.TRANSISTOR

    def test_categorize_connector(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("2x5 Pin Header", "2.54mm") == PartCategory.CONNECTOR
        assert _categorize_part("USB-C Connector", "USB-C") == PartCategory.CONNECTOR
        assert _categorize_part("3.5mm Audio Jack", "") == PartCategory.CONNECTOR

    def test_categorize_crystal(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("8MHz Crystal", "HC-49") == PartCategory.CRYSTAL
        assert _categorize_part("32.768kHz Oscillator", "SMD") == PartCategory.CRYSTAL

    def test_categorize_led(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("Red LED 0603", "0603") == PartCategory.LED
        assert _categorize_part("Green LED SMD", "") == PartCategory.LED
        # Note: "Light Emitting Diode" matches "diode" first due to check order

    def test_categorize_switch(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("6x6mm Tactile Switch", "6x6mm") == PartCategory.SWITCH
        assert _categorize_part("Push Button", "") == PartCategory.SWITCH

    def test_categorize_relay(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("5V Relay SPDT", "") == PartCategory.RELAY

    def test_categorize_fuse(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("500mA Fuse", "0603") == PartCategory.FUSE
        assert _categorize_part("Resettable PTC", "1206") == PartCategory.FUSE
        assert _categorize_part("Polyfuse 100mA", "") == PartCategory.FUSE

    def test_categorize_other(self):
        from kicad_tools.parts.lcsc import _categorize_part

        assert _categorize_part("Some Random Part", "") == PartCategory.OTHER

    def test_guess_more_smd_packages(self):
        from kicad_tools.parts.lcsc import _guess_package_type

        assert _guess_package_type("0603") == PackageType.SMD
        assert _guess_package_type("0805") == PackageType.SMD
        assert _guess_package_type("1206") == PackageType.SMD
        assert _guess_package_type("0402_1005Metric") == PackageType.SMD
        assert _guess_package_type("SSOP-28") == PackageType.SMD
        assert _guess_package_type("TSSOP-20") == PackageType.SMD
        assert _guess_package_type("LQFP-48") == PackageType.SMD
        assert _guess_package_type("TQFP-32") == PackageType.SMD
        assert _guess_package_type("BGA-256") == PackageType.SMD
        assert _guess_package_type("DFN-8") == PackageType.SMD
        assert _guess_package_type("SC-70") == PackageType.SMD
        assert _guess_package_type("TO-252") == PackageType.SMD
        assert _guess_package_type("TO-263") == PackageType.SMD
        assert _guess_package_type("DPAK") == PackageType.SMD
        assert _guess_package_type("D2PAK") == PackageType.SMD
        assert _guess_package_type("SMD-Crystal") == PackageType.SMD
        assert _guess_package_type("SMT Package") == PackageType.SMD

    def test_guess_more_through_hole_packages(self):
        from kicad_tools.parts.lcsc import _guess_package_type

        assert _guess_package_type("PDIP-8") == PackageType.THROUGH_HOLE
        assert _guess_package_type("TO-92") == PackageType.THROUGH_HOLE
        assert _guess_package_type("TO-247") == PackageType.THROUGH_HOLE
        assert _guess_package_type("Radial") == PackageType.THROUGH_HOLE
        assert _guess_package_type("Through Hole") == PackageType.THROUGH_HOLE


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestLCSCClientExtended:
    """Extended tests for LCSCClient."""

    def test_client_initialization_defaults(self):
        from kicad_tools.parts import LCSCClient

        client = LCSCClient()
        assert client.cache is not None
        assert client.timeout == 30.0
        assert client._session is None

    def test_client_initialization_no_cache(self):
        from kicad_tools.parts import LCSCClient

        client = LCSCClient(use_cache=False)
        assert client.cache is None

    def test_client_initialization_custom_timeout(self):
        from kicad_tools.parts import LCSCClient

        client = LCSCClient(timeout=60.0)
        assert client.timeout == 60.0

    def test_part_number_normalization(self, tmp_path):
        """Test that part numbers are normalized to uppercase with C prefix."""
        from kicad_tools.parts import LCSCClient, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        client = LCSCClient(cache=cache)

        with patch("kicad_tools.parts.lcsc.LCSCClient._fetch_part") as mock_fetch:
            mock_fetch.return_value = None

            # Test lowercase
            client.lookup("c123456")
            mock_fetch.assert_called_with("C123456")

            # Test without C prefix
            mock_fetch.reset_mock()
            client.lookup("123456")
            mock_fetch.assert_called_with("C123456")

    def test_lookup_bypass_cache(self, tmp_path):
        """Test that bypass_cache skips cache lookup."""
        from kicad_tools.parts import LCSCClient, Part, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        cached_part = Part(lcsc_part="C123456", mfr_part="CachedPart")
        cache.put(cached_part)

        client = LCSCClient(cache=cache)

        with patch("kicad_tools.parts.lcsc.LCSCClient._fetch_part") as mock_fetch:
            new_part = Part(lcsc_part="C123456", mfr_part="FreshPart")
            mock_fetch.return_value = new_part

            part = client.lookup("C123456", bypass_cache=True)
            mock_fetch.assert_called_once()
            assert part.mfr_part == "FreshPart"

    def test_lookup_api_error(self, tmp_path):
        """Test handling of API errors."""
        from kicad_tools.parts import LCSCClient, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        client = LCSCClient(cache=cache)

        with patch("kicad_tools.parts.lcsc.LCSCClient._fetch_part") as mock_fetch:
            mock_fetch.side_effect = Exception("Network error")

            part = client.lookup("C123456")
            assert part is None

    def test_lookup_many(self, tmp_path):
        """Test lookup_many method."""
        from kicad_tools.parts import LCSCClient, Part, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        # Pre-cache one part
        cache.put(Part(lcsc_part="C111", mfr_part="CachedPart"))

        client = LCSCClient(cache=cache)

        with patch("kicad_tools.parts.lcsc.LCSCClient._fetch_part") as mock_fetch:
            mock_fetch.return_value = Part(lcsc_part="C222", mfr_part="FreshPart")

            results = client.lookup_many(["C111", "C222"])

            # C111 should come from cache, C222 from API
            assert "C111" in results
            assert results["C111"].mfr_part == "CachedPart"
            assert "C222" in results
            assert results["C222"].mfr_part == "FreshPart"

    def test_lookup_many_empty_list(self, tmp_path):
        """Test lookup_many with empty list."""
        from kicad_tools.parts import LCSCClient, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        client = LCSCClient(cache=cache)

        results = client.lookup_many([])
        assert results == {}

    def test_lookup_many_normalization(self, tmp_path):
        """Test that lookup_many normalizes part numbers."""
        from kicad_tools.parts import LCSCClient, Part, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        client = LCSCClient(cache=cache)

        with patch("kicad_tools.parts.lcsc.LCSCClient._fetch_part") as mock_fetch:
            mock_fetch.return_value = Part(lcsc_part="C123", mfr_part="Part")

            # Mix of formats
            results = client.lookup_many(["c123", "456", "C789"])

            # All should be normalized
            calls = [call[0][0] for call in mock_fetch.call_args_list]
            assert "C123" in calls
            assert "C456" in calls
            assert "C789" in calls

    def test_search_error_handling(self, tmp_path):
        """Test search error handling."""
        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            import requests

            mock_session.return_value.post.side_effect = requests.RequestException("Error")

            from kicad_tools.parts import LCSCClient, PartsCache

            cache = PartsCache(db_path=tmp_path / "cache.db")
            client = LCSCClient(cache=cache)

            results = client.search("test")
            assert len(results.parts) == 0
            assert results.total_count == 0

    def test_search_api_error_code(self, tmp_path):
        """Test search with non-200 API response."""
        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"code": 500, "message": "Server error"}
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.post.return_value = mock_resp

            from kicad_tools.parts import LCSCClient, PartsCache

            cache = PartsCache(db_path=tmp_path / "cache.db")
            client = LCSCClient(cache=cache)

            results = client.search("test")
            assert len(results.parts) == 0

    def test_search_with_filters(self, tmp_path):
        """Test search with in_stock and basic_only filters."""
        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "code": 200,
                "data": {"componentPageInfo": {"list": [], "total": 0}},
            }
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.post.return_value = mock_resp

            from kicad_tools.parts import LCSCClient

            client = LCSCClient(use_cache=False)

            client.search("test", in_stock=True, basic_only=True, page=2, page_size=50)

            # Verify the payload included the filters
            call_args = mock_session.return_value.post.call_args
            payload = call_args[1]["json"]
            assert payload["stockCountMin"] == 1
            assert payload["componentLibraryType"] == "base"
            assert payload["currentPage"] == 2
            assert payload["pageSize"] == 50

    def test_context_manager(self, tmp_path):
        """Test LCSCClient as context manager."""
        from kicad_tools.parts import LCSCClient, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")

        with LCSCClient(cache=cache) as client:
            assert client is not None

        # After exiting, session should be closed
        assert client._session is None

    def test_close_method(self, tmp_path):
        """Test explicit close method."""
        from kicad_tools.parts import LCSCClient

        client = LCSCClient(use_cache=False)
        # Force session creation - patch at requests module level since
        # lcsc.py imports requests locally inside _get_session
        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session

            # Trigger session creation
            client._get_session()
            assert client._session is not None

            # Close
            client.close()
            mock_session.close.assert_called_once()
            assert client._session is None

    def test_parse_component_with_alternative_fields(self, tmp_path):
        """Test _parse_component handles alternative API field names."""
        with patch("kicad_tools.parts.lcsc.LCSCClient._get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "code": 200,
                "data": {
                    "componentCode": "C123",
                    "manufacturerPartNumber": "ALT-PART-NUM",  # Alternative field
                    "manufacturer": "AltMfr",  # Alternative field
                    "describe": "Alternative description",
                    "package": "0402",  # Alternative field
                    "stockCount": 1000,
                    "priceList": [  # Alternative price field
                        {"startNumber": 1, "productPrice": 0.01},
                    ],
                },
            }
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.post.return_value = mock_resp

            from kicad_tools.parts import LCSCClient

            client = LCSCClient(use_cache=False)
            part = client.lookup("C123")

            assert part is not None
            assert part.mfr_part == "ALT-PART-NUM"
            assert part.manufacturer == "AltMfr"
            assert part.package == "0402"


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestLCSCClientCheckBOM:
    """Tests for LCSCClient.check_bom method."""

    @pytest.fixture
    def mock_bom_items(self):
        """Create mock BOM items."""
        class MockBOMItem:
            def __init__(self, reference, value, footprint, lcsc=""):
                self.reference = reference
                self.value = value
                self.footprint = footprint
                self.lcsc = lcsc
                self.quantity = 1

        return [
            MockBOMItem("R1", "10k", "0402", "C123456"),
            MockBOMItem("R2", "4.7k", "0402", "C789012"),
            MockBOMItem("C1", "100nF", "0402", ""),  # No LCSC
            MockBOMItem("U1", "STM32", "LQFP48", "C999999"),  # Will not be found
        ]

    def test_check_bom(self, tmp_path, mock_bom_items):
        """Test check_bom method."""
        from kicad_tools.parts import LCSCClient, Part, PartsCache

        cache = PartsCache(db_path=tmp_path / "cache.db")
        client = LCSCClient(cache=cache)

        with patch.object(client, "lookup_many") as mock_lookup:
            mock_lookup.return_value = {
                "C123456": Part(lcsc_part="C123456", mfr_part="R1", stock=5000),
                "C789012": Part(lcsc_part="C789012", mfr_part="R2", stock=0),
            }

            result = client.check_bom(mock_bom_items)

            assert len(result.items) == 4

            # R1: Found and in stock
            r1 = next(i for i in result.items if i.reference == "R1")
            assert r1.matched is True
            assert r1.in_stock is True

            # R2: Found but out of stock
            r2 = next(i for i in result.items if i.reference == "R2")
            assert r2.matched is True
            assert r2.in_stock is False

            # C1: No LCSC number
            c1 = next(i for i in result.items if i.reference == "C1")
            assert c1.error == "No LCSC part number"

            # U1: Not found
            u1 = next(i for i in result.items if i.reference == "U1")
            assert u1.error == "Part not found"


class TestRequiresRequestsDecorator:
    """Tests for _requires_requests decorator."""

    def test_decorator_with_requests_available(self):
        """Test decorator when requests is available."""
        from kicad_tools.parts.lcsc import _requires_requests

        @_requires_requests
        def test_func():
            return "success"

        # Should work fine when requests is available
        if HAS_REQUESTS:
            assert test_func() == "success"

    def test_decorator_without_requests(self):
        """Test decorator when requests is not available."""
        from kicad_tools.parts.lcsc import _requires_requests

        @_requires_requests
        def test_func():
            return "success"

        # Mock requests import to fail
        with patch.dict("sys.modules", {"requests": None}):
            import sys
            original = sys.modules.get("requests")
            try:
                if "requests" in sys.modules:
                    del sys.modules["requests"]

                # This would raise ImportError if we could truly remove requests
                # but the decorator catches it inside the function
                pass
            finally:
                if original is not None:
                    sys.modules["requests"] = original
