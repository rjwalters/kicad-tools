"""Tests for the datasheet module."""

import importlib.util
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.datasheet.cache import DatasheetCache, get_default_cache_path
from kicad_tools.datasheet.exceptions import (
    DatasheetDownloadError,
    DatasheetSearchError,
)
from kicad_tools.datasheet.models import (
    Datasheet,
    DatasheetResult,
    DatasheetSearchResult,
)
from kicad_tools.datasheet.images import ExtractedImage, classify_image
from kicad_tools.datasheet.tables import ExtractedTable


class TestDatasheetResult:
    """Tests for DatasheetResult dataclass."""

    def test_creation(self):
        result = DatasheetResult(
            part_number="STM32F103C8T6",
            manufacturer="STMicroelectronics",
            description="32-bit ARM Cortex-M3 MCU",
            datasheet_url="https://example.com/datasheet.pdf",
            source="lcsc",
            confidence=0.95,
        )
        assert result.part_number == "STM32F103C8T6"
        assert result.manufacturer == "STMicroelectronics"
        assert result.source == "lcsc"
        assert result.confidence == 0.95

    def test_default_confidence(self):
        result = DatasheetResult(
            part_number="TEST",
            manufacturer="Test",
            description="Test",
            datasheet_url="https://example.com/test.pdf",
            source="test",
        )
        assert result.confidence == 1.0

    def test_str(self):
        result = DatasheetResult(
            part_number="STM32",
            manufacturer="ST",
            description="MCU",
            datasheet_url="https://example.com/ds.pdf",
            source="lcsc",
        )
        assert "STM32" in str(result)
        assert "lcsc" in str(result)


class TestDatasheet:
    """Tests for Datasheet dataclass."""

    @pytest.fixture
    def sample_datasheet(self, tmp_path):
        # Create a dummy PDF file
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test content")

        return Datasheet(
            part_number="STM32F103C8T6",
            manufacturer="STMicroelectronics",
            local_path=pdf_path,
            source_url="https://example.com/datasheet.pdf",
            downloaded_at=datetime.now(),
            file_size=1024 * 1024,  # 1 MB
            source="lcsc",
        )

    def test_exists(self, sample_datasheet):
        assert sample_datasheet.exists is True

    def test_exists_missing_file(self, tmp_path):
        ds = Datasheet(
            part_number="TEST",
            manufacturer="Test",
            local_path=tmp_path / "missing.pdf",
            source_url="https://example.com/test.pdf",
            downloaded_at=datetime.now(),
            file_size=1000,
        )
        assert ds.exists is False

    def test_file_size_mb(self, sample_datasheet):
        assert sample_datasheet.file_size_mb == 1.0

    def test_str(self, sample_datasheet):
        assert "STM32F103C8T6" in str(sample_datasheet)


class TestDatasheetSearchResult:
    """Tests for DatasheetSearchResult dataclass."""

    def test_has_results(self):
        result = DatasheetSearchResult(
            query="STM32",
            results=[
                DatasheetResult(
                    part_number="STM32F103",
                    manufacturer="ST",
                    description="MCU",
                    datasheet_url="https://example.com/ds.pdf",
                    source="lcsc",
                )
            ],
        )
        assert result.has_results is True

    def test_has_no_results(self):
        result = DatasheetSearchResult(query="NOTFOUND")
        assert result.has_results is False

    def test_sources_searched(self):
        result = DatasheetSearchResult(
            query="test",
            results=[
                DatasheetResult(
                    part_number="P1",
                    manufacturer="M1",
                    description="D1",
                    datasheet_url="url1",
                    source="lcsc",
                ),
                DatasheetResult(
                    part_number="P2",
                    manufacturer="M2",
                    description="D2",
                    datasheet_url="url2",
                    source="octopart",
                ),
                DatasheetResult(
                    part_number="P3",
                    manufacturer="M3",
                    description="D3",
                    datasheet_url="url3",
                    source="lcsc",
                ),
            ],
        )
        sources = result.sources_searched
        assert "lcsc" in sources
        assert "octopart" in sources
        assert len(sources) == 2

    def test_sources_failed(self):
        result = DatasheetSearchResult(
            query="test",
            errors={"digikey": "API key required", "mouser": "Rate limited"},
        )
        assert "digikey" in result.sources_failed
        assert "mouser" in result.sources_failed

    def test_len_and_iter(self):
        results = [
            DatasheetResult(
                part_number=f"P{i}",
                manufacturer="M",
                description="D",
                datasheet_url=f"url{i}",
                source="test",
            )
            for i in range(5)
        ]
        search_result = DatasheetSearchResult(query="test", results=results)

        assert len(search_result) == 5
        assert list(search_result) == results


class TestDatasheetCache:
    """Tests for DatasheetCache."""

    @pytest.fixture
    def temp_cache(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        return DatasheetCache(cache_dir=cache_dir, ttl_days=90)

    @pytest.fixture
    def sample_datasheet(self, tmp_path):
        # Create a dummy PDF file
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(exist_ok=True)
        part_dir = cache_dir / "STM32F103C8T6"
        part_dir.mkdir(exist_ok=True)
        pdf_path = part_dir / "datasheet.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test content" * 100)

        return Datasheet(
            part_number="STM32F103C8T6",
            manufacturer="STMicroelectronics",
            local_path=pdf_path,
            source_url="https://example.com/datasheet.pdf",
            source="lcsc",
            downloaded_at=datetime.now(),
            file_size=pdf_path.stat().st_size,
        )

    def test_put_and_get(self, temp_cache, sample_datasheet):
        temp_cache.put(sample_datasheet)
        retrieved = temp_cache.get("STM32F103C8T6")

        assert retrieved is not None
        assert retrieved.part_number == "STM32F103C8T6"
        assert retrieved.manufacturer == "STMicroelectronics"
        assert retrieved.source == "lcsc"

    def test_get_not_found(self, temp_cache):
        result = temp_cache.get("NOTFOUND")
        assert result is None

    def test_is_cached(self, temp_cache, sample_datasheet):
        assert temp_cache.is_cached("STM32F103C8T6") is False
        temp_cache.put(sample_datasheet)
        assert temp_cache.is_cached("STM32F103C8T6") is True

    def test_is_cached_file_missing(self, temp_cache, sample_datasheet):
        temp_cache.put(sample_datasheet)
        # Delete the file
        sample_datasheet.local_path.unlink()
        assert temp_cache.is_cached("STM32F103C8T6") is False

    def test_delete(self, temp_cache, sample_datasheet):
        temp_cache.put(sample_datasheet)
        assert temp_cache.is_cached("STM32F103C8T6") is True

        deleted = temp_cache.delete("STM32F103C8T6")
        assert deleted is True
        assert temp_cache.is_cached("STM32F103C8T6") is False
        # File should be deleted too
        assert not sample_datasheet.local_path.exists()

    def test_delete_not_found(self, temp_cache):
        deleted = temp_cache.delete("NOTFOUND")
        assert deleted is False

    def test_list(self, temp_cache, tmp_path):
        # Create multiple datasheets
        for i in range(3):
            part_dir = temp_cache.cache_dir / f"PART{i}"
            part_dir.mkdir()
            pdf_path = part_dir / "datasheet.pdf"
            pdf_path.write_bytes(b"%PDF content")

            ds = Datasheet(
                part_number=f"PART{i}",
                manufacturer="Mfr",
                local_path=pdf_path,
                source_url=f"url{i}",
                source="test",
                downloaded_at=datetime.now(),
                file_size=pdf_path.stat().st_size,
            )
            temp_cache.put(ds)

        datasheets = temp_cache.list()
        assert len(datasheets) == 3
        part_numbers = [ds.part_number for ds in datasheets]
        assert "PART0" in part_numbers
        assert "PART1" in part_numbers
        assert "PART2" in part_numbers

    def test_clear(self, temp_cache, sample_datasheet):
        temp_cache.put(sample_datasheet)
        assert len(temp_cache.list()) == 1

        count = temp_cache.clear()
        assert count == 1
        assert len(temp_cache.list()) == 0

    def test_stats(self, temp_cache, sample_datasheet):
        temp_cache.put(sample_datasheet)
        stats = temp_cache.stats()

        assert stats["total_count"] == 1
        assert stats["valid_count"] == 1
        assert stats["expired_count"] == 0
        assert stats["total_size_bytes"] > 0
        assert "lcsc" in stats["sources"]

    def test_expired_entries(self, tmp_path):
        # Create cache with very short TTL
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache = DatasheetCache(cache_dir=cache_dir, ttl_days=0)

        # Create a datasheet
        part_dir = cache_dir / "EXPIRED"
        part_dir.mkdir()
        pdf_path = part_dir / "datasheet.pdf"
        pdf_path.write_bytes(b"%PDF content")

        ds = Datasheet(
            part_number="EXPIRED",
            manufacturer="Mfr",
            local_path=pdf_path,
            source_url="url",
            source="test",
            downloaded_at=datetime.now() - timedelta(days=1),
            file_size=pdf_path.stat().st_size,
        )
        cache.put(ds)

        # Should be expired
        assert cache.get("EXPIRED") is None
        # But available with ignore_expiry
        assert cache.get("EXPIRED", ignore_expiry=True) is not None

    def test_clear_older_than(self, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cache = DatasheetCache(cache_dir=cache_dir, ttl_days=90)

        # Create a datasheet
        part_dir = cache_dir / "OLD"
        part_dir.mkdir()
        pdf_path = part_dir / "datasheet.pdf"
        pdf_path.write_bytes(b"%PDF content")

        ds = Datasheet(
            part_number="OLD",
            manufacturer="Mfr",
            local_path=pdf_path,
            source_url="url",
            source="test",
            downloaded_at=datetime.now(),
            file_size=pdf_path.stat().st_size,
        )
        cache.put(ds)

        # Entry was just cached (now), so should not be cleared
        count = cache.clear_older_than(30)
        assert count == 0

        # But clearing entries older than 0 days should clear everything
        count = cache.clear_older_than(0)
        assert count == 1

    def test_get_datasheet_path(self, temp_cache):
        path = temp_cache.get_datasheet_path("STM32F103C8T6")
        assert "STM32F103C8T6" in str(path)
        assert path.name == "datasheet.pdf"


class TestGetDefaultCachePath:
    """Tests for get_default_cache_path function."""

    def test_default_path(self):
        path = get_default_cache_path()
        assert "kicad-tools" in str(path)
        assert "datasheets" in str(path)

    def test_xdg_cache_home(self, monkeypatch, tmp_path):
        xdg_cache = tmp_path / "xdg_cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))

        path = get_default_cache_path()
        assert str(xdg_cache) in str(path)


HAS_REQUESTS = importlib.util.find_spec("requests") is not None


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestLCSCDatasheetSource:
    """Tests for LCSCDatasheetSource."""

    def test_name(self):
        from kicad_tools.datasheet.sources import LCSCDatasheetSource

        source = LCSCDatasheetSource()
        assert source.name == "lcsc"

    def test_search_with_mock(self, tmp_path):
        """Test search with mocked LCSC client."""
        from kicad_tools.datasheet.sources import LCSCDatasheetSource
        from kicad_tools.parts.models import Part, SearchResult

        source = LCSCDatasheetSource()

        # Mock the LCSCClient
        with patch("kicad_tools.datasheet.sources.lcsc.LCSCClient") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client

            mock_search_result = SearchResult(
                query="STM32",
                parts=[
                    Part(
                        lcsc_part="C123456",
                        mfr_part="STM32F103C8T6",
                        manufacturer="STMicroelectronics",
                        description="32-bit MCU",
                        datasheet_url="https://example.com/stm32.pdf",
                    ),
                    Part(
                        lcsc_part="C789012",
                        mfr_part="STM32F401",
                        manufacturer="STMicroelectronics",
                        description="32-bit MCU",
                        datasheet_url="https://example.com/stm32f4.pdf",
                    ),
                ],
                total_count=2,
            )
            mock_client.search.return_value = mock_search_result

            results = source.search("STM32")

            assert len(results) == 2
            assert results[0].part_number == "STM32F103C8T6"
            assert results[0].source == "lcsc"
            assert "stm32.pdf" in results[0].datasheet_url

    def test_search_lcsc_part_lookup(self, tmp_path):
        """Test search with LCSC part number triggers lookup."""
        from kicad_tools.datasheet.sources import LCSCDatasheetSource
        from kicad_tools.parts.models import Part

        source = LCSCDatasheetSource()

        with patch("kicad_tools.datasheet.sources.lcsc.LCSCClient") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value = mock_client

            mock_part = Part(
                lcsc_part="C123456",
                mfr_part="RC0402FR-0710KL",
                manufacturer="Yageo",
                description="10K Resistor",
                datasheet_url="https://example.com/resistor.pdf",
            )
            mock_client.lookup.return_value = mock_part

            results = source.search("C123456")

            mock_client.lookup.assert_called_once_with("C123456")
            assert len(results) == 1
            assert results[0].part_number == "RC0402FR-0710KL"

    def test_download_success(self, tmp_path):
        """Test successful datasheet download."""
        from kicad_tools.datasheet.models import DatasheetResult
        from kicad_tools.datasheet.sources import LCSCDatasheetSource

        source = LCSCDatasheetSource()

        with patch.object(source, "_get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.iter_content.return_value = [b"%PDF-1.4 test content"]
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.get.return_value = mock_resp

            result = DatasheetResult(
                part_number="TEST",
                manufacturer="Test",
                description="Test",
                datasheet_url="https://example.com/test.pdf",
                source="lcsc",
            )

            output_path = tmp_path / "test.pdf"
            downloaded = source.download(result, output_path)

            assert downloaded == output_path
            assert output_path.exists()

    def test_download_failure(self, tmp_path):
        """Test download failure handling."""
        import requests

        from kicad_tools.datasheet.models import DatasheetResult
        from kicad_tools.datasheet.sources import LCSCDatasheetSource

        source = LCSCDatasheetSource()

        with patch.object(source, "_get_session") as mock_session:
            mock_session.return_value.get.side_effect = requests.RequestException("Error")

            result = DatasheetResult(
                part_number="TEST",
                manufacturer="Test",
                description="Test",
                datasheet_url="https://example.com/test.pdf",
                source="lcsc",
            )

            with pytest.raises(DatasheetDownloadError):
                source.download(result, tmp_path / "test.pdf")


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestOctopartDatasheetSource:
    """Tests for OctopartDatasheetSource."""

    def test_name(self):
        from kicad_tools.datasheet.sources import OctopartDatasheetSource

        source = OctopartDatasheetSource()
        assert source.name == "octopart"

    def test_search_without_api_key(self):
        """Test search returns empty when no API key."""
        from kicad_tools.datasheet.sources import OctopartDatasheetSource

        source = OctopartDatasheetSource(api_key=None)
        results = source.search("STM32")
        assert len(results) == 0

    def test_search_with_api_key(self):
        """Test search with API key."""
        from kicad_tools.datasheet.sources import OctopartDatasheetSource

        source = OctopartDatasheetSource(api_key="test-key")

        with patch.object(source, "_get_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "results": [
                    {
                        "part": {
                            "mpn": "STM32F103C8T6",
                            "manufacturer": {"name": "STMicroelectronics"},
                            "descriptions": [{"text": "32-bit MCU"}],
                            "datasheets": [
                                {"url": "https://example.com/stm32.pdf"},
                            ],
                        },
                    },
                ],
            }
            mock_resp.raise_for_status = MagicMock()
            mock_session.return_value.get.return_value = mock_resp

            results = source.search("STM32")

            assert len(results) == 1
            assert results[0].part_number == "STM32F103C8T6"
            assert results[0].source == "octopart"

    def test_rate_limiting(self):
        """Test that rate limiting enforces delay."""
        import time

        from kicad_tools.datasheet.sources import OctopartDatasheetSource

        source = OctopartDatasheetSource(api_key="test-key")
        source._last_request_time = time.time()

        start = time.time()
        source._rate_limit()
        elapsed = time.time() - start

        # Should have waited at least part of MIN_REQUEST_INTERVAL
        assert elapsed >= 0.1  # Allow for some tolerance


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestDatasheetManager:
    """Tests for DatasheetManager."""

    @pytest.fixture
    def manager(self, tmp_path):
        from kicad_tools.datasheet import DatasheetManager

        cache_dir = tmp_path / "cache"
        return DatasheetManager(cache_dir=cache_dir)

    def test_search_aggregates_sources(self, manager):
        """Test that search aggregates results from all sources."""
        from kicad_tools.datasheet.models import DatasheetResult

        # Mock both sources
        with patch.object(manager.sources[0], "search") as mock_lcsc:
            with patch.object(manager.sources[1], "search") as mock_octopart:
                mock_lcsc.return_value = [
                    DatasheetResult(
                        part_number="STM32",
                        manufacturer="ST",
                        description="MCU",
                        datasheet_url="url1",
                        source="lcsc",
                        confidence=0.9,
                    ),
                ]
                mock_octopart.return_value = [
                    DatasheetResult(
                        part_number="STM32",
                        manufacturer="ST",
                        description="MCU",
                        datasheet_url="url2",
                        source="octopart",
                        confidence=0.8,
                    ),
                ]

                results = manager.search("STM32")

                assert len(results) == 2
                # Should be sorted by confidence
                assert results.results[0].confidence >= results.results[1].confidence

    def test_search_deduplicates_urls(self, manager):
        """Test that search deduplicates by URL."""
        from kicad_tools.datasheet.models import DatasheetResult

        with patch.object(manager.sources[0], "search") as mock_lcsc:
            with patch.object(manager.sources[1], "search") as mock_octopart:
                # Same URL from both sources
                mock_lcsc.return_value = [
                    DatasheetResult(
                        part_number="STM32",
                        manufacturer="ST",
                        description="MCU",
                        datasheet_url="https://same-url.pdf",
                        source="lcsc",
                        confidence=0.9,
                    ),
                ]
                mock_octopart.return_value = [
                    DatasheetResult(
                        part_number="STM32",
                        manufacturer="ST",
                        description="MCU",
                        datasheet_url="https://same-url.pdf",
                        source="octopart",
                        confidence=0.8,
                    ),
                ]

                results = manager.search("STM32")

                # Should only have one result (highest confidence)
                assert len(results) == 1
                assert results.results[0].source == "lcsc"

    def test_download_caches_result(self, manager, tmp_path):
        """Test that download caches the result."""
        from kicad_tools.datasheet.models import DatasheetResult

        result = DatasheetResult(
            part_number="TEST",
            manufacturer="Test",
            description="Test",
            datasheet_url="https://example.com/test.pdf",
            source="lcsc",
        )

        with patch.object(manager.sources[0], "download") as mock_download:
            output_path = manager.cache.get_datasheet_path("TEST")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"%PDF content")
            mock_download.return_value = output_path

            datasheet = manager.download(result)

            assert datasheet.part_number == "TEST"
            assert manager.is_cached("TEST")

    def test_download_uses_cache(self, manager, tmp_path):
        """Test that download uses cached result."""
        from kicad_tools.datasheet.models import Datasheet, DatasheetResult

        # Pre-cache a datasheet
        cache_path = manager.cache.get_datasheet_path("CACHED")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"%PDF content")

        ds = Datasheet(
            part_number="CACHED",
            manufacturer="Test",
            local_path=cache_path,
            source_url="url",
            source="lcsc",
            downloaded_at=datetime.now(),
            file_size=cache_path.stat().st_size,
        )
        manager.cache.put(ds)

        result = DatasheetResult(
            part_number="CACHED",
            manufacturer="Test",
            description="Test",
            datasheet_url="https://example.com/cached.pdf",
            source="lcsc",
        )

        with patch.object(manager.sources[0], "download") as mock_download:
            datasheet = manager.download(result)

            # Should not have called download
            mock_download.assert_not_called()
            assert datasheet.part_number == "CACHED"

    def test_download_by_part(self, manager):
        """Test download_by_part convenience method."""
        from kicad_tools.datasheet.models import DatasheetResult

        with patch.object(manager, "search") as mock_search:
            with patch.object(manager, "download") as mock_download:
                mock_search.return_value = MagicMock(
                    has_results=True,
                    results=[
                        DatasheetResult(
                            part_number="STM32",
                            manufacturer="ST",
                            description="MCU",
                            datasheet_url="url",
                            source="lcsc",
                        ),
                    ],
                )
                mock_download.return_value = MagicMock(part_number="STM32")

                datasheet = manager.download_by_part("STM32")

                mock_search.assert_called_once_with("STM32")
                assert datasheet.part_number == "STM32"

    def test_download_by_part_not_found(self, manager):
        """Test download_by_part raises error when not found."""
        with patch.object(manager, "search") as mock_search:
            mock_search.return_value = MagicMock(has_results=False, results=[])

            with pytest.raises(DatasheetSearchError):
                manager.download_by_part("NOTFOUND")

    def test_list_cached(self, manager, tmp_path):
        """Test list_cached method."""
        from kicad_tools.datasheet.models import Datasheet

        # Create some cached datasheets
        for i in range(3):
            cache_path = manager.cache.get_datasheet_path(f"PART{i}")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"%PDF content")

            ds = Datasheet(
                part_number=f"PART{i}",
                manufacturer="Test",
                local_path=cache_path,
                source_url=f"url{i}",
                source="test",
                downloaded_at=datetime.now(),
                file_size=cache_path.stat().st_size,
            )
            manager.cache.put(ds)

        cached = manager.list_cached()
        assert len(cached) == 3

    def test_clear_cache(self, manager, tmp_path):
        """Test clear_cache method."""
        from kicad_tools.datasheet.models import Datasheet

        cache_path = manager.cache.get_datasheet_path("TEST")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"%PDF content")

        ds = Datasheet(
            part_number="TEST",
            manufacturer="Test",
            local_path=cache_path,
            source_url="url",
            source="test",
            downloaded_at=datetime.now(),
            file_size=cache_path.stat().st_size,
        )
        manager.cache.put(ds)

        count = manager.clear_cache()
        assert count == 1
        assert len(manager.list_cached()) == 0

    def test_cache_stats(self, manager):
        """Test cache_stats method."""
        stats = manager.cache_stats()
        assert "total_count" in stats
        assert "cache_dir" in stats
        assert "ttl_days" in stats


# ============================================================================
# PDF Parsing Tests
# ============================================================================


class TestExtractedImage:
    """Tests for ExtractedImage dataclass."""

    @pytest.fixture
    def sample_image(self):
        return ExtractedImage(
            page=1,
            index=0,
            width=800,
            height=600,
            format="png",
            data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
            caption="Figure 1: Block diagram",
            classification="block_diagram",
        )

    def test_suggested_filename(self, sample_image):
        assert sample_image.suggested_filename == "page_1_img_0_block_diagram.png"

    def test_suggested_filename_no_classification(self):
        img = ExtractedImage(
            page=2,
            index=3,
            width=100,
            height=100,
            format="jpg",
            data=b"test",
        )
        assert img.suggested_filename == "page_2_img_3.jpg"

    def test_size_kb(self, sample_image):
        # 8 bytes header + 100 bytes padding = 108 bytes
        assert sample_image.size_kb == pytest.approx(108 / 1024, rel=1e-2)

    def test_save(self, sample_image, tmp_path):
        output_path = tmp_path / "images" / "test.png"
        sample_image.save(output_path)

        assert output_path.exists()
        assert output_path.read_bytes() == sample_image.data

    def test_repr(self, sample_image):
        repr_str = repr(sample_image)
        assert "page=1" in repr_str
        assert "800x600" in repr_str
        assert "png" in repr_str


class TestClassifyImage:
    """Tests for image classification function."""

    def test_pinout_by_caption(self):
        assert classify_image(800, 600, "Figure 5: Pin Configuration") == "pinout"
        assert classify_image(800, 600, "pinout diagram") == "pinout"
        assert classify_image(800, 600, "Pin Assignment Table") == "pinout"

    def test_package_by_caption(self):
        assert classify_image(400, 400, "Package Dimensions") == "package"
        assert classify_image(400, 400, "Mechanical Drawing") == "package"

    def test_block_diagram_by_caption(self):
        assert classify_image(1000, 500, "Block Diagram Overview") == "block_diagram"
        assert classify_image(1000, 500, "Functional Diagram") == "block_diagram"

    def test_schematic_by_caption(self):
        assert classify_image(800, 600, "Application Circuit") == "schematic"
        assert classify_image(800, 600, "Typical Application") == "schematic"

    def test_graph_by_caption(self):
        assert classify_image(600, 400, "Characteristic Curve") == "graph"
        assert classify_image(600, 400, "Plot of voltage vs current") == "graph"

    def test_timing_by_caption(self):
        assert classify_image(1200, 300, "Timing Diagram") == "timing"
        assert classify_image(1200, 300, "Waveform example") == "timing"

    def test_timing_by_aspect_ratio(self):
        # Wide images without caption classified as timing
        assert classify_image(1500, 400, None) == "timing"

    def test_unknown_no_caption(self):
        # Square image without caption - can't classify
        assert classify_image(500, 500, None) is None


class TestExtractedTable:
    """Tests for ExtractedTable dataclass."""

    @pytest.fixture
    def sample_table(self):
        return ExtractedTable(
            page=5,
            headers=["Pin", "Name", "Function"],
            rows=[
                ["1", "VCC", "Power supply"],
                ["2", "GND", "Ground"],
                ["3", "IN", "Input signal"],
            ],
        )

    def test_cols(self, sample_table):
        assert sample_table.cols == 3

    def test_cols_no_headers(self):
        table = ExtractedTable(
            page=1,
            headers=[],
            rows=[["a", "b", "c"], ["d", "e", "f"]],
        )
        assert table.cols == 3

    def test_cols_empty(self):
        table = ExtractedTable(page=1, headers=[], rows=[])
        assert table.cols == 0

    def test_row_count(self, sample_table):
        assert sample_table.row_count == 3

    def test_to_markdown(self, sample_table):
        md = sample_table.to_markdown()
        assert "| Pin | Name | Function |" in md
        assert "| --- | --- | --- |" in md
        assert "| 1 | VCC | Power supply |" in md

    def test_to_markdown_no_headers(self):
        table = ExtractedTable(
            page=1,
            headers=[],
            rows=[["a", "b"], ["c", "d"]],
        )
        md = table.to_markdown()
        assert "| a | b |" in md
        assert "| c | d |" in md

    def test_to_csv(self, sample_table):
        csv_content = sample_table.to_csv()
        assert "Pin,Name,Function" in csv_content
        assert "1,VCC,Power supply" in csv_content

    def test_to_dict(self, sample_table):
        d = sample_table.to_dict()
        assert d["page"] == 5
        assert d["headers"] == ["Pin", "Name", "Function"]
        assert len(d["rows"]) == 3

    def test_to_json(self, sample_table):
        import json

        j = sample_table.to_json()
        data = json.loads(j)
        assert data["page"] == 5
        assert len(data["rows"]) == 3

    def test_to_dataframe_import_error(self, sample_table):
        with patch.dict("sys.modules", {"pandas": None}):
            # Simulate pandas not being installed
            with pytest.raises(ImportError, match="pandas is required"):
                sample_table.to_dataframe()

    def test_repr(self, sample_table):
        repr_str = repr(sample_table)
        assert "page=5" in repr_str
        assert "3 rows" in repr_str
        assert "3 cols" in repr_str


class TestDatasheetParser:
    """Tests for DatasheetParser class."""

    def test_file_not_found(self):
        from kicad_tools.datasheet import DatasheetParser

        with pytest.raises(FileNotFoundError, match="PDF file not found"):
            DatasheetParser("/nonexistent/file.pdf")

    def test_not_pdf_file(self, tmp_path):
        from kicad_tools.datasheet import DatasheetParser

        # Create a non-PDF file
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("not a pdf")

        with pytest.raises(ValueError, match="Expected a PDF file"):
            DatasheetParser(txt_file)

    def test_init_with_valid_pdf_path(self, tmp_path):
        from kicad_tools.datasheet import DatasheetParser

        # Create a fake PDF file (won't pass validation but tests init)
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4")

        parser = DatasheetParser(pdf_file)
        assert parser.path == pdf_file

    def test_repr(self, tmp_path):
        from kicad_tools.datasheet import DatasheetParser

        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4")

        # Mock page_count since we don't have a real PDF
        with patch.object(DatasheetParser, "page_count", 10):
            parser = DatasheetParser(pdf_file)
            repr_str = repr(parser)
            assert "test.pdf" in repr_str


class TestDatasheetParserDependencies:
    """Tests for dependency checking."""

    def test_markitdown_import_error(self, tmp_path):
        from kicad_tools.datasheet.parser import _check_markitdown

        with patch.dict("sys.modules", {"markitdown": None}):
            with pytest.raises(ImportError, match="markitdown is required"):
                _check_markitdown()

    def test_pymupdf_import_error(self, tmp_path):
        from kicad_tools.datasheet.parser import _check_pymupdf

        with patch.dict("sys.modules", {"fitz": None}):
            with pytest.raises(ImportError, match="PyMuPDF is required"):
                _check_pymupdf()

    def test_pdfplumber_import_error(self, tmp_path):
        from kicad_tools.datasheet.parser import _check_pdfplumber

        with patch.dict("sys.modules", {"pdfplumber": None}):
            with pytest.raises(ImportError, match="pdfplumber is required"):
                _check_pdfplumber()


# ============================================================================
# CLI Tests
# ============================================================================


class TestDatasheetCLI:
    """Tests for datasheet CLI commands."""

    # Search/download CLI tests
    def test_search_command_text_format(self, capsys):
        """Test search command with text output."""
        from kicad_tools.cli.datasheet_cmd import main
        from kicad_tools.datasheet.models import DatasheetResult, DatasheetSearchResult

        with patch("kicad_tools.datasheet.manager.DatasheetManager") as MockManager:
            mock_manager = MagicMock()
            MockManager.return_value = mock_manager
            mock_manager.search.return_value = DatasheetSearchResult(
                query="STM32",
                results=[
                    DatasheetResult(
                        part_number="STM32F103",
                        manufacturer="ST",
                        description="MCU",
                        datasheet_url="https://example.com/ds.pdf",
                        source="lcsc",
                    ),
                ],
            )

            result = main(["search", "STM32"])

            assert result == 0
            captured = capsys.readouterr()
            assert "STM32F103" in captured.out
            assert "lcsc" in captured.out

    def test_search_command_no_results(self, capsys):
        """Test search command with no results."""
        from kicad_tools.cli.datasheet_cmd import main
        from kicad_tools.datasheet.models import DatasheetSearchResult

        with patch("kicad_tools.datasheet.manager.DatasheetManager") as MockManager:
            mock_manager = MagicMock()
            MockManager.return_value = mock_manager
            mock_manager.search.return_value = DatasheetSearchResult(
                query="NOTFOUND",
                results=[],
            )

            result = main(["search", "NOTFOUND"])

            assert result == 1
            captured = capsys.readouterr()
            assert "No datasheets found" in captured.out

    def test_list_command_empty(self, capsys):
        """Test list command with no cached datasheets."""
        from kicad_tools.cli.datasheet_cmd import main

        with patch("kicad_tools.datasheet.manager.DatasheetManager") as MockManager:
            mock_manager = MagicMock()
            MockManager.return_value = mock_manager
            mock_manager.list_cached.return_value = []

            result = main(["list"])

            assert result == 0
            captured = capsys.readouterr()
            assert "No cached" in captured.out

    def test_cache_stats_command(self, capsys):
        """Test cache stats command."""
        from kicad_tools.cli.datasheet_cmd import main

        with patch("kicad_tools.datasheet.manager.DatasheetManager") as MockManager:
            mock_manager = MagicMock()
            MockManager.return_value = mock_manager
            mock_manager.cache_stats.return_value = {
                "total_count": 10,
                "valid_count": 8,
                "expired_count": 2,
                "total_size_mb": 50.5,
                "ttl_days": 90,
                "cache_dir": "/path/to/cache",
                "sources": {"lcsc": 7, "octopart": 3},
            }

            result = main(["cache", "stats"])

            assert result == 0
            captured = capsys.readouterr()
            assert "10" in captured.out
            assert "50.5" in captured.out or "50.50" in captured.out

    # PDF parsing CLI tests
    def test_parse_pages_single(self):
        from kicad_tools.cli.datasheet_cmd import _parse_pages

        assert _parse_pages("5") == [5]

    def test_parse_pages_range(self):
        from kicad_tools.cli.datasheet_cmd import _parse_pages

        assert _parse_pages("1-5") == [1, 2, 3, 4, 5]

    def test_parse_pages_mixed(self):
        from kicad_tools.cli.datasheet_cmd import _parse_pages

        assert _parse_pages("1,3,5-7,10") == [1, 3, 5, 6, 7, 10]

    def test_parse_pages_none(self):
        from kicad_tools.cli.datasheet_cmd import _parse_pages

        assert _parse_pages(None) is None

    def test_parse_pages_duplicates(self):
        from kicad_tools.cli.datasheet_cmd import _parse_pages

        # Duplicates should be removed
        assert _parse_pages("1,1,2,2,3") == [1, 2, 3]

    def test_main_no_action(self):
        from kicad_tools.cli.datasheet_cmd import main

        # No action should print help and return 0
        result = main([])
        assert result == 0

    def test_convert_file_not_found(self):
        from kicad_tools.cli.datasheet_cmd import main

        result = main(["convert", "/nonexistent/file.pdf"])
        assert result == 1


class TestModuleExports:
    """Tests for module exports."""

    def test_datasheet_module_exports(self):
        from kicad_tools import datasheet

        # Search/download exports
        assert hasattr(datasheet, "DatasheetManager")
        assert hasattr(datasheet, "Datasheet")
        assert hasattr(datasheet, "DatasheetResult")
        assert hasattr(datasheet, "DatasheetSearchResult")
        assert hasattr(datasheet, "DatasheetCache")

        # PDF parsing exports
        assert hasattr(datasheet, "DatasheetParser")
        assert hasattr(datasheet, "ParsedDatasheet")
        assert hasattr(datasheet, "ExtractedImage")
        assert hasattr(datasheet, "ExtractedTable")
        assert hasattr(datasheet, "classify_image")

    def test_datasheet_init_exports(self):
        from kicad_tools.datasheet import (
            DatasheetManager,
            DatasheetParser,
            ExtractedImage,
            ExtractedTable,
            ParsedDatasheet,
            classify_image,
        )

        # All exports should be importable
        assert DatasheetManager is not None
        assert DatasheetParser is not None
        assert ParsedDatasheet is not None
        assert ExtractedImage is not None
        assert ExtractedTable is not None
        assert classify_image is not None
