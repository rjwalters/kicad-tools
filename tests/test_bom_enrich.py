"""Tests for BOM LCSC auto-enrichment (export.bom_enrich)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from kicad_tools.export.bom_enrich import EnrichmentReport, enrich_bom_lcsc
from kicad_tools.parts.cache import PartsCache
from kicad_tools.schema.bom import BOMItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    ref: str,
    value: str,
    footprint: str,
    lcsc: str = "",
    dnp: bool = False,
) -> BOMItem:
    """Build a BOMItem with minimal required fields."""
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:R",
        lcsc=lcsc,
        dnp=dnp,
    )


def _make_suggester_mock(mock_instance: MagicMock) -> None:
    """Configure a mock PartSuggester with no cache (default for tests)."""
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    # Ensure the cache fallback path finds no cache
    mock_client = MagicMock()
    mock_client.cache = None
    mock_instance._get_client.return_value = mock_client


def _mock_suggestion(lcsc_part: str, is_basic: bool = True, confidence: float = 0.8):
    """Return a mock PartSuggestion whose best_suggestion has the given LCSC part."""
    from kicad_tools.cost.suggest import PartSuggestion, SuggestedPart

    best = SuggestedPart(
        lcsc_part=lcsc_part,
        mfr_part="MFR-XXXX",
        description="Test part",
        package="0402",
        stock=5000,
        is_basic=is_basic,
        is_preferred=False,
        unit_price=0.001,
        confidence=confidence,
    )
    return PartSuggestion(
        reference="R1",
        value="10k",
        footprint="Resistor_SMD:R_0402_1005Metric",
        package="0402",
        existing_lcsc=None,
        suggestions=[best],
        best_suggestion=best,
    )


def _mock_no_match():
    """Return a mock PartSuggestion with no match."""
    from kicad_tools.cost.suggest import PartSuggestion

    return PartSuggestion(
        reference="U1",
        value="STM32C011F4P6",
        footprint="Package_SO:TSSOP-20",
        package="TSSOP-20",
        existing_lcsc=None,
        suggestions=[],
        best_suggestion=None,
        error="no matching parts found",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnrichBomLcsc:
    """Tests for the enrich_bom_lcsc function."""

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_auto_matches_missing_lcsc(self, MockSuggester):
        """Items without LCSC get populated from PartSuggester."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        mock_instance.suggest_for_component.return_value = _mock_suggestion("C25744")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("R2", "10k", "Resistor_SMD:R_0402_1005Metric"),
        ]

        report = enrich_bom_lcsc(items)

        # Both items should now have the LCSC number
        assert items[0].lcsc == "C25744"
        assert items[1].lcsc == "C25744"

        # Report should show 1 auto-matched group
        assert report.auto_matched == 1
        assert report.unmatched == 0
        assert report.already_populated == 0

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_preserves_existing_lcsc(self, MockSuggester):
        """Items with existing LCSC are not searched and are left as-is."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric", lcsc="C1525"),
            _make_item("C2", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
        ]

        report = enrich_bom_lcsc(items)

        # Existing LCSC preserved and propagated to group mate
        assert items[0].lcsc == "C1525"
        assert items[1].lcsc == "C1525"

        # suggest_for_component should NOT have been called
        mock_instance.suggest_for_component.assert_not_called()

        assert report.already_populated == 1
        assert report.auto_matched == 0

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_reports_unmatched(self, MockSuggester):
        """Unmatched parts appear in the report."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        mock_instance.suggest_for_component.return_value = _mock_no_match()
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("U1", "STM32C011F4P6", "Package_SO:TSSOP-20"),
        ]

        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == ""
        assert report.unmatched == 1
        assert len(report.unmatched_entries) == 1
        assert report.unmatched_entries[0].value == "STM32C011F4P6"

    @patch("kicad_tools.cost.suggest.PartSuggester._get_client")
    @patch("kicad_tools.parts.lcsc.LCSCClient._get_session")
    def test_null_list_yields_clean_unmatched_reason(self, mock_session, mock_get_client):
        """A no-match query (API ``"list": null``) reports a clean reason.

        End-to-end regression for #4407: previously the null candidate list
        raised ``TypeError: 'NoneType' object is not iterable`` inside
        ``LCSCClient.search()``; that TypeError was swallowed by
        ``suggest_for_component``'s broad ``except`` and stored verbatim as the
        per-part reason, so ``summary_lines()`` printed
        ``... (J1) ('NoneType' object is not iterable)``. With the parse-site
        guard the query flows through the normal unmatched path and reports the
        user-facing ``"no matching parts found"`` reason instead.
        """
        from kicad_tools.parts import LCSCClient

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 200,
            "data": {"componentPageInfo": {"list": None, "total": 0}},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.return_value.post.return_value = mock_resp

        # Real client (no cache / no offline catalog / no rate limit) so the
        # real search() + parse path runs against the mocked null response.
        client = LCSCClient(use_cache=False, use_local_catalog=False, rate_limit=0)
        mock_get_client.return_value = client

        items = [
            _make_item(
                "J1",
                "PinHeader_1x02",
                "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            ),
        ]

        report = enrich_bom_lcsc(items)

        # The live search was actually exercised (not short-circuited).
        assert mock_session.return_value.post.called

        # Clean unmatched outcome, no leaked exception.
        assert items[0].lcsc == ""
        assert report.unmatched == 1
        entry = report.unmatched_entries[0]
        assert entry.source == "unmatched"
        assert entry.error == "no matching parts found"

        # The raw exception string must never reach user-facing output.
        summary = "\n".join(report.summary_lines())
        assert "not iterable" not in summary
        assert "no matching parts found" in summary

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_skips_dnp_items(self, MockSuggester):
        """DNP items are not searched."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric", dnp=True),
        ]

        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == ""
        assert report.total_groups == 0
        mock_instance.suggest_for_component.assert_not_called()

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_groups_by_value_footprint(self, MockSuggester):
        """Same value+footprint searched only once, result applied to all."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        mock_instance.suggest_for_component.return_value = _mock_suggestion("C25744")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("R2", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("R3", "10k", "Resistor_SMD:R_0402_1005Metric"),
        ]

        report = enrich_bom_lcsc(items)

        # All items get the same LCSC
        for item in items:
            assert item.lcsc == "C25744"

        # Only one search call for the group
        mock_instance.suggest_for_component.assert_called_once()
        assert report.auto_matched == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_different_values_searched_separately(self, MockSuggester):
        """Different value+footprint combos are searched independently."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)

        # Return different parts for different searches
        def side_effect(*, reference, value, footprint, existing_lcsc):
            if value == "10k":
                return _mock_suggestion("C25744")
            else:
                return _mock_suggestion("C1525")

        mock_instance.suggest_for_component.side_effect = side_effect
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
        ]

        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == "C25744"
        assert items[1].lcsc == "C1525"
        assert report.auto_matched == 2

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_mixed_existing_and_missing(self, MockSuggester):
        """Mix of items with and without LCSC numbers."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        mock_instance.suggest_for_component.return_value = _mock_suggestion("C25744")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric", lcsc="C1525"),
        ]

        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == "C25744"  # auto-matched
        assert items[1].lcsc == "C1525"  # preserved
        assert report.auto_matched == 1
        assert report.already_populated == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_suggester_exception_handled(self, MockSuggester):
        """If PartSuggester.suggest_for_component raises, item is unmatched."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        # The suggest_for_component itself doesn't raise; errors are reported
        # via suggestion.error. But the PartSuggester catches exceptions internally.
        # Let's test the case where it returns an error suggestion.
        from kicad_tools.cost.suggest import PartSuggestion

        error_suggestion = PartSuggestion(
            reference="R1",
            value="10k",
            footprint="Resistor_SMD:R_0402_1005Metric",
            package="0402",
            existing_lcsc=None,
            suggestions=[],
            best_suggestion=None,
            error="API timeout",
        )
        mock_instance.suggest_for_component.return_value = error_suggestion
        MockSuggester.return_value = mock_instance

        items = [_make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric")]
        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == ""
        assert report.unmatched == 1
        assert report.unmatched_entries[0].error == "API timeout"


class TestEnrichBomLcscCircuitBreaker:
    """Tests for enrichment loop short-circuit on 403 Forbidden."""

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_403_stops_remaining_lookups(self, MockSuggester):
        """After a 403 with empty cache, remaining groups are unmatched."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)

        # First call raises LCSCForbiddenError
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            _make_item("L1", "4.7uH", "Inductor_SMD:L_0603_1608Metric"),
        ]

        report = enrich_bom_lcsc(items)

        # suggest_for_component should only be called once (for the first group)
        assert mock_instance.suggest_for_component.call_count == 1

        # All three groups should be unmatched with the 403 error
        assert report.unmatched == 3
        for entry in report.entries:
            assert entry.source == "unmatched"
            assert "403" in entry.error

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_403_preserves_existing_lcsc_in_remaining_groups(self, MockSuggester):
        """Groups with existing LCSC are still handled after 403."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)

        # The first group that needs a lookup will get 403
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
        MockSuggester.return_value = mock_instance

        items = [
            # Group 1: has existing LCSC -- processed before any API call
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric", lcsc="C1525"),
            # Group 2: needs lookup -- will trigger 403
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            # Group 3: needs lookup -- should be skipped
            _make_item("L1", "4.7uH", "Inductor_SMD:L_0603_1608Metric"),
        ]

        report = enrich_bom_lcsc(items)

        # Group 1 should still be from schematic
        assert report.already_populated == 1
        assert items[0].lcsc == "C1525"

        # Groups 2 and 3 should be unmatched with 403
        assert report.unmatched == 2
        assert mock_instance.suggest_for_component.call_count == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_successful_before_403(self, MockSuggester):
        """Groups matched before the 403 keep their results."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)

        call_count = [0]

        def side_effect(*, reference, value, footprint, existing_lcsc):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_suggestion("C25744")
            raise LCSCForbiddenError("403 Forbidden")

        mock_instance.suggest_for_component.side_effect = side_effect
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            _make_item("L1", "4.7uH", "Inductor_SMD:L_0603_1608Metric"),
        ]

        report = enrich_bom_lcsc(items)

        # First group was successfully matched
        assert items[0].lcsc == "C25744"
        assert report.auto_matched == 1

        # Remaining groups unmatched with 403
        assert report.unmatched == 2

        # Only 2 API calls made (first success, second 403, third skipped)
        assert mock_instance.suggest_for_component.call_count == 2


class TestEnrichBomLcscCacheFallback:
    """Tests for cache fallback when API returns 403."""

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_403_falls_back_to_enrichment_cache(self, MockSuggester):
        """When API is forbidden, cached enrichment matches are used."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        # Create a real PartsCache with pre-populated enrichment matches
        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")
            cache.put_enrichment_match(
                "10k",
                "Resistor_SMD:R_0402_1005Metric",
                "C25744",
                confidence=0.85,
                part_type="Basic",
            )
            cache.put_enrichment_match(
                "100nF",
                "Capacitor_SMD:C_0402_1005Metric",
                "C1525",
                confidence=0.9,
                part_type="Basic",
            )

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            # Wire up the real cache
            mock_instance._get_client.return_value.cache = cache

            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
                _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ]

            report = enrich_bom_lcsc(items)

            # Both should be resolved from cache
            assert items[0].lcsc == "C25744"
            assert items[1].lcsc == "C1525"
            assert report.cache_matched == 2
            assert report.unmatched == 0

            # Verify source is "cache" for both entries
            for entry in report.entries:
                assert entry.source == "cache"

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_403_mixed_cache_hit_and_miss(self, MockSuggester):
        """When API is forbidden, parts not in cache remain unmatched."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")
            # Only cache the resistor, not the capacitor
            cache.put_enrichment_match(
                "10k",
                "Resistor_SMD:R_0402_1005Metric",
                "C25744",
                confidence=0.85,
                part_type="Basic",
            )

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache

            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
                _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ]

            report = enrich_bom_lcsc(items)

            assert items[0].lcsc == "C25744"
            assert items[1].lcsc == ""
            assert report.cache_matched == 1
            assert report.unmatched == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_auto_match_populates_enrichment_cache(self, MockSuggester):
        """Successful auto-matches are stored in the enrichment cache."""
        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache
            mock_instance.suggest_for_component.return_value = _mock_suggestion(
                "C25744", confidence=0.8
            )
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            ]

            enrich_bom_lcsc(items)

            # Verify the match was stored in the enrichment cache
            match = cache.get_enrichment_match("10k", "Resistor_SMD:R_0402_1005Metric")
            assert match is not None
            assert match["lcsc_part"] == "C25744"
            assert match["confidence"] == 0.8
            assert match["part_type"] == "Basic"

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_403_cache_fallback_uses_expired_entries(self, MockSuggester):
        """Expired enrichment cache entries are still used when API is forbidden."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            # Create cache with very short TTL so entries expire immediately
            cache = PartsCache(db_path=Path(tmp) / "test.db", ttl_days=0)
            cache.put_enrichment_match(
                "10k",
                "Resistor_SMD:R_0402_1005Metric",
                "C25744",
                confidence=0.85,
                part_type="Basic",
            )

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache

            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            ]

            report = enrich_bom_lcsc(items)

            # Should still resolve from expired cache
            assert items[0].lcsc == "C25744"
            assert report.cache_matched == 1
            assert report.unmatched == 0

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_403_empty_cache_reports_unmatched(self, MockSuggester):
        """With empty cache and API forbidden, parts are unmatched (no crash)."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache

            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            ]

            report = enrich_bom_lcsc(items)

            assert items[0].lcsc == ""
            assert report.unmatched == 1
            assert report.cache_matched == 0


class TestEnrichBomLcscDeterminism:
    """Regression tests for degraded-mode (API-forbidden) determinism.

    Issue #3935: BOM enrichment was observed to drift across "identical"
    runs (8 -> 9 -> 10 matches) when the JLCPCB API was intermittently
    available. The drift comes from the enrichment cache *accumulating*
    matches across runs -- not from any nondeterminism within a single
    call. These tests pin the contract: for a fixed cache state and fixed
    input, two API-forbidden runs produce byte-identical reports.
    """

    def _seed_cache(self, cache: PartsCache) -> None:
        """Populate the enrichment cache with a known set of matches."""
        cache.put_enrichment_match(
            "10k",
            "Resistor_SMD:R_0402_1005Metric",
            "C25744",
            confidence=0.85,
            part_type="Basic",
        )
        cache.put_enrichment_match(
            "100nF",
            "Capacitor_SMD:C_0402_1005Metric",
            "C1525",
            confidence=0.9,
            part_type="Basic",
        )

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_two_offline_runs_produce_identical_reports(self, MockSuggester):
        """AC5: two 403-forbidden runs with the same cache are byte-identical.

        This is the direct regression guard for the sweep-observed drift:
        with a frozen cache state and frozen inputs, the degraded-mode
        output must not change between runs.
        """
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")
            self._seed_cache(cache)

            def _run() -> EnrichmentReport:
                mock_instance = MagicMock()
                _make_suggester_mock(mock_instance)
                mock_instance._get_client.return_value.cache = cache
                mock_instance.suggest_for_component.side_effect = LCSCForbiddenError(
                    "403 Forbidden"
                )
                MockSuggester.return_value = mock_instance
                # One cache hit ("10k"), one cache miss ("47uF" -> unmatched),
                # exercising both the cache and unmatched degraded branches.
                items = [
                    _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
                    _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
                    _make_item("C2", "47uF", "Capacitor_SMD:C_0805_2012Metric"),
                ]
                return enrich_bom_lcsc(items)

            report1 = _run()
            report2 = _run()

            # @dataclass generates field-by-field __eq__, so this asserts
            # same entries, sources, lcsc_part values, confidences, order.
            assert report1.entries == report2.entries
            assert report1 == report2

            # Sanity: the cache state was not mutated between runs, so the
            # bucket counts are stable and reflect the seeded state.
            assert report1.cache_matched == 2
            assert report1.unmatched == 1
            assert report2.cache_matched == 2
            assert report2.unmatched == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_offline_run_does_not_mutate_cache(self, MockSuggester):
        """The API-forbidden path performs pure reads (no cache writes).

        If a degraded run wrote to the cache, the second run could observe
        a different state -- the exact mechanism behind the reported drift.
        """
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")
            self._seed_cache(cache)

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache
            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
                # A brand-new (value, footprint) that is NOT in the cache:
                # a degraded run must not write it back.
                _make_item("C2", "47uF", "Capacitor_SMD:C_0805_2012Metric"),
            ]

            enrich_bom_lcsc(items)

            # The uncached group must remain absent -- no write-back occurred.
            assert (
                cache.get_enrichment_match(
                    "47uF", "Capacitor_SMD:C_0805_2012Metric", ignore_expiry=True
                )
                is None
            )
            # The seeded entries are untouched.
            match = cache.get_enrichment_match(
                "10k", "Resistor_SMD:R_0402_1005Metric", ignore_expiry=True
            )
            assert match is not None
            assert match["lcsc_part"] == "C25744"

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_offline_run_emits_warning_not_debug(self, MockSuggester, caplog):
        """AC2: degraded mode surfaces a WARNING-level message to callers.

        The 403 circuit breaker and each stale cache fallback must log at
        WARNING (not be silenced to DEBUG), so an operator reviewing logs
        can tell a connected run from an offline one.
        """
        import logging

        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")
            self._seed_cache(cache)

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache
            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            ]

            with caplog.at_level(logging.WARNING, logger="kicad_tools.export.bom_enrich"):
                enrich_bom_lcsc(items)

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            # The 403 circuit-breaker warning must be present.
            assert any("403 Forbidden" in r.getMessage() for r in warnings)
            # The stale cache fallback must also warn (degraded, verify-before-fab).
            assert any("Cache fallback" in r.getMessage() for r in warnings)

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_cache_bucket_surfaced_distinct_from_auto(self, MockSuggester):
        """AC3: the summary distinguishes cache-sourced entries from auto.

        A degraded run's ``cache`` count must be a distinct bucket in the
        summary so two exports can be compared without reading raw logs.
        """
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        with tempfile.TemporaryDirectory() as tmp:
            cache = PartsCache(db_path=Path(tmp) / "test.db")
            self._seed_cache(cache)

            mock_instance = MagicMock()
            _make_suggester_mock(mock_instance)
            mock_instance._get_client.return_value.cache = cache
            mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
            MockSuggester.return_value = mock_instance

            items = [
                _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
                _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            ]

            report = enrich_bom_lcsc(items)
            summary = report.summary_lines()[0]

            # "cache" is a distinct bucket, and none were auto-matched.
            assert "2 from cache" in summary
            assert "0 auto-matched" in summary
            assert report.cache_matched == 2
            assert report.auto_matched == 0


class TestEnrichmentReport:
    """Tests for the EnrichmentReport dataclass."""

    def test_summary_lines_all_matched(self):
        """Summary with only auto-matched entries."""
        from kicad_tools.export.bom_enrich import EnrichmentEntry

        report = EnrichmentReport(
            entries=[
                EnrichmentEntry(
                    value="10k",
                    footprint="R_0402",
                    references=["R1", "R2"],
                    lcsc_part="C25744",
                    source="auto",
                    confidence=0.9,
                    part_type="Basic",
                ),
            ]
        )
        lines = report.summary_lines()
        assert "1 auto-matched" in lines[0]
        assert "0 unmatched" in lines[0]

    def test_summary_lines_with_cache(self):
        """Summary includes cache hit count when present."""
        from kicad_tools.export.bom_enrich import EnrichmentEntry

        report = EnrichmentReport(
            entries=[
                EnrichmentEntry(
                    value="10k",
                    footprint="R_0402",
                    references=["R1"],
                    lcsc_part="C25744",
                    source="cache",
                    confidence=0.85,
                    part_type="Basic",
                ),
            ]
        )
        lines = report.summary_lines()
        assert "1 from cache" in lines[0]
        assert "0 auto-matched" in lines[0]

    def test_summary_lines_with_unmatched(self):
        """Summary includes detail lines for unmatched parts."""
        from kicad_tools.export.bom_enrich import EnrichmentEntry

        report = EnrichmentReport(
            entries=[
                EnrichmentEntry(
                    value="STM32C011F4P6",
                    footprint="TSSOP-20",
                    references=["U1"],
                    lcsc_part="",
                    source="unmatched",
                    error="no matching parts found",
                ),
            ]
        )
        lines = report.summary_lines()
        assert "1 unmatched" in lines[0]
        assert len(lines) >= 2
        assert "STM32C011F4P6" in lines[2]  # after "Unmatched parts:" header

    def test_properties(self):
        """Test EnrichmentReport property accessors."""
        from kicad_tools.export.bom_enrich import EnrichmentEntry

        report = EnrichmentReport(
            entries=[
                EnrichmentEntry(
                    value="10k",
                    footprint="R_0402",
                    references=["R1"],
                    lcsc_part="C25744",
                    source="auto",
                ),
                EnrichmentEntry(
                    value="100nF",
                    footprint="C_0402",
                    references=["C1"],
                    lcsc_part="C1525",
                    source="schematic",
                ),
                EnrichmentEntry(
                    value="4.7uH",
                    footprint="L_0603",
                    references=["L1"],
                    lcsc_part="C99999",
                    source="cache",
                ),
                EnrichmentEntry(
                    value="IC1",
                    footprint="QFN-32",
                    references=["U1"],
                    lcsc_part="",
                    source="unmatched",
                ),
            ]
        )
        assert report.total_groups == 4
        assert report.auto_matched == 1
        assert report.already_populated == 1
        assert report.cache_matched == 1
        assert report.unmatched == 1
