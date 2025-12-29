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
