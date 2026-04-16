"""Tests for BOM LCSC auto-enrichment (export.bom_enrich)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.export.bom_enrich import EnrichmentReport, enrich_bom_lcsc
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_skips_dnp_items(self, MockSuggester):
        """DNP items are not searched."""
        mock_instance = MagicMock()
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)

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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)
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
        """After a 403, remaining groups are marked unmatched without API calls."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        mock_instance = MagicMock()
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)

        # First call raises LCSCForbiddenError
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError(
            "403 Forbidden"
        )
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)

        # The first group that needs a lookup will get 403
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError(
            "403 Forbidden"
        )
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
        mock_instance.__enter__ = MagicMock(return_value=mock_instance)
        mock_instance.__exit__ = MagicMock(return_value=False)

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
                    value="IC1",
                    footprint="QFN-32",
                    references=["U1"],
                    lcsc_part="",
                    source="unmatched",
                ),
            ]
        )
        assert report.total_groups == 3
        assert report.auto_matched == 1
        assert report.already_populated == 1
        assert report.unmatched == 1
