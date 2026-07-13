"""Tests for LCSC value-mismatch guards (issue #3590).

Reproduces the manufacturing defect where the enrichment cache fallback
assigned C1525 (a 100nF 0402 capacitor) to a 16nF BOM row, and verifies
the three defense layers:

1. ``enrich_bom_lcsc`` cache fallback rejects + evicts poisoned entries.
2. ``apply_existing_lcsc_assignments`` (merge_lcsc read-back) drops
   committed-BOM assignments whose known value mismatches the row.
3. BOM preflight (``bom_lcsc_values``) FAILs on assigned parts whose
   known value mismatches the BOM value.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.export.bom_enrich import enrich_bom_lcsc
from kicad_tools.export.bom_formats import apply_existing_lcsc_assignments
from kicad_tools.export.lcsc_value_check import (
    check_lcsc_against_cache,
    find_value_mismatch,
)
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FP_0402_CAP = "Capacitor_SMD:C_0402_1005Metric"

C1525 = Part(
    lcsc_part="C1525",
    mfr_part="CL05B104KO5NNNC",
    manufacturer="Samsung Electro-Mechanics",
    description="16V 100nF X7R ±10% 0402 Multilayer Ceramic Capacitors MLCC",
    package="0402",
    value="100nF",
    is_basic=True,
)


def _make_item(ref: str, value: str, footprint: str, lcsc: str = "", dnp: bool = False) -> BOMItem:
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:C",
        lcsc=lcsc,
        dnp=dnp,
    )


def _make_cache(tmp_path: Path) -> PartsCache:
    return PartsCache(db_path=tmp_path / "parts.db")


def _poisoned_cache(tmp_path: Path) -> PartsCache:
    """Cache primed with the exact #3590 poison: 16nF -> C1525 (100nF)."""
    cache = _make_cache(tmp_path)
    cache.put(C1525)
    cache.put_enrichment_match("16nF", FP_0402_CAP, "C1525", confidence=0.9, part_type="Basic")
    return cache


def _make_suggester_mock(mock_instance: MagicMock, cache: PartsCache | None) -> None:
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    mock_client = MagicMock()
    mock_client.cache = cache
    mock_instance._get_client.return_value = mock_client


# ---------------------------------------------------------------------------
# find_value_mismatch unit tests
# ---------------------------------------------------------------------------


class TestFindValueMismatch:
    def test_capacitor_clear_mismatch(self):
        m = find_value_mismatch("16nF", "C10", part_value="100nF")
        assert m is not None
        assert m.requested_si == pytest.approx(16e-9)
        assert m.candidate_si == pytest.approx(100e-9)

    def test_capacitor_match(self):
        assert find_value_mismatch("100nF", "C1", part_value="100nF") is None

    def test_equivalent_notation_matches(self):
        # 0.1uF == 100nF
        assert find_value_mismatch("0.1uF", "C1", part_value="100nF") is None

    def test_description_fallback(self):
        m = find_value_mismatch("16nF", "C10", part_description=C1525.description)
        assert m is not None
        assert "100nF" in m.candidate_value

    def test_description_ignores_part_number_tokens(self):
        # "GRM155R71H104KE14" contains "1H104" / "14" -- must not parse
        # as a value token.
        assert (
            find_value_mismatch("100nF", "C1", part_description="GRM155R71H104KE14 Murata") is None
        )

    def test_voltage_token_not_mistaken_for_value(self):
        # "16V" in a description must not be read as 16 farads.
        assert find_value_mismatch("100nF", "C1", part_description="16V 100nF X7R") is None

    def test_resistor_mismatch(self):
        m = find_value_mismatch("10k", "R1", part_value="4.7k")
        assert m is not None

    def test_resistor_description_ohm(self):
        m = find_value_mismatch("10k", "R1", part_description="62kΩ ±1% 0402")
        assert m is not None
        assert m.candidate_si == 62000

    def test_resistor_match_with_units(self):
        assert find_value_mismatch("10k", "R1", part_value="10k") is None

    def test_unparseable_requested_value_accepts(self):
        # ICs etc. cannot be validated numerically.
        assert find_value_mismatch("STM32C011F4P6", "U1", part_value="100nF") is None

    def test_unknown_candidate_accepts(self):
        assert find_value_mismatch("16nF", "C1", part_value="", part_description="") is None


class TestCrossTypeMismatch:
    """Cross-type rejection (issue #4128): a resistor request validated
    against a capacitor description must reject, not accept as
    "cannot validate"."""

    def test_resistor_request_capacitor_description_rejected(self):
        # The literal chorus v23 repro: 330R -> C1525 (a 100nF cap).
        m = find_value_mismatch("330R", "R1", part_description=C1525.description)
        assert m is not None
        assert "100nF" in m.candidate_value
        assert "different component type" in m.describe()

    def test_resistor_request_synthetic_capacitor_rejected(self):
        m = find_value_mismatch("330R", "R1", part_description="100nF X7R ±10% 0402 Capacitor")
        assert m is not None
        assert math.isnan(m.candidate_si)

    def test_capacitor_request_resistor_description_rejected(self):
        m = find_value_mismatch("16nF", "C10", part_description="62kΩ ±1% 0402 Resistor")
        assert m is not None
        assert "62k" in m.candidate_value or "62" in m.candidate_value

    def test_capacitor_request_inductor_description_rejected(self):
        m = find_value_mismatch("16nF", "C10", part_description="10uH ±20% 0805 Inductor")
        assert m is not None
        assert math.isnan(m.candidate_si)

    def test_no_units_anywhere_stays_permissive(self):
        # Genuine cannot-validate: description carries no unit tokens at
        # all -> accept (unchanged behavior).
        assert (
            find_value_mismatch("330R", "R1", part_description="SOT-23 general purpose part")
            is None
        )

    def test_same_type_description_not_treated_as_foreign(self):
        # A resistor description validated against a resistor request:
        # the ohm extractor handles it directly; not a cross-type reject.
        assert find_value_mismatch("10k", "R1", part_description="10kΩ ±1% 0402") is None

    def test_mixed_unit_description_does_not_false_reject(self):
        # A plausible mixed-unit (RC-network-style) description that
        # carries BOTH the request's own unit (Ω) and a foreign unit (F):
        # the request's own unit is present, so this is a same-type
        # comparison, NOT a cross-type reject.  330R matches "330Ω".
        assert (
            find_value_mismatch(
                "330R",
                "R1",
                part_description="RC filter network 330Ω 100nF integrated 0402",
            )
            is None
        )

    def test_ambiguous_multi_foreign_description_stays_permissive(self):
        # A resistor request whose description contains TWO foreign types
        # (F and H) but not the request's own (Ω) is ambiguous -> stay
        # permissive rather than guess which one dominates.
        assert (
            find_value_mismatch("330R", "R1", part_description="LC module 100nF 10uH combined 0805")
            is None
        )


class TestMpnCodeFallback:
    """EIA-code decoding from MLCC part numbers (last-resort fallback)."""

    def test_real_machine_poison_record(self):
        # The actual C1525 record on the machine that shipped the
        # defect: no parametric value, description is just the MPN.
        m = find_value_mismatch(
            "16nF",
            "C10",
            part_value="",
            part_description="CL05B104KO5NNNC",
            part_mfr="CL05B104KO5NNNC",
        )
        assert m is not None
        assert m.candidate_si == pytest.approx(100e-9)
        assert "100nF" in m.candidate_value

    def test_samsung_mpn_matching_value_accepts(self):
        assert find_value_mismatch("100nF", "C1", part_mfr="CL05B104KO5NNNC") is None

    def test_murata_mpn_size_code_not_misread(self):
        # "155" (size) and "1H" (voltage) must not be decoded; "104K" must.
        m = find_value_mismatch("16nF", "C10", part_mfr="GRM155R71H104KE14")
        assert m is not None
        assert m.candidate_si == pytest.approx(100e-9)
        assert find_value_mismatch("100nF", "C1", part_mfr="GRM155R71H104KE14") is None

    def test_tdk_mpn(self):
        # C1005X7R1H104K050BB: "1005" is the metric size, "104K" the code.
        m = find_value_mismatch("16nF", "C10", part_mfr="C1005X7R1H104K050BB")
        assert m is not None
        assert m.candidate_si == pytest.approx(100e-9)

    def test_mpn_fallback_only_for_capacitors(self):
        # A resistor row must not be validated via the MLCC decoder.
        assert find_value_mismatch("10k", "R1", part_mfr="CL05B104KO5NNNC") is None

    def test_structured_value_takes_precedence_over_mpn(self):
        # value field says 16nF -> match, even though MPN says 100nF.
        assert (
            find_value_mismatch("16nF", "C10", part_value="16nF", part_mfr="CL05B104KO5NNNC")
            is None
        )

    def test_no_code_in_mpn_accepts(self):
        assert find_value_mismatch("16nF", "C10", part_mfr="GCM21BR71H?") is None

    def test_sparse_cache_record_caught_via_cache_check(self, tmp_path):
        """End-to-end: a cache record carrying ONLY the MPN (the real
        #3590 record shape) is still rejected."""
        cache = _make_cache(tmp_path)
        cache.put(
            Part(
                lcsc_part="C1525",
                mfr_part="CL05B104KO5NNNC",
                description="CL05B104KO5NNNC",
                value="",
            )
        )
        m = check_lcsc_against_cache(cache, "C1525", "16nF", "C10")
        assert m is not None
        assert m.candidate_si == pytest.approx(100e-9)


class TestCheckLcscAgainstCache:
    def test_known_part_mismatch(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        m = check_lcsc_against_cache(cache, "C1525", "16nF", "C10")
        assert m is not None
        assert m.candidate_value == "100nF"

    def test_known_part_match(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        assert check_lcsc_against_cache(cache, "C1525", "100nF", "C1") is None

    def test_unknown_part_accepts(self, tmp_path):
        cache = _make_cache(tmp_path)
        assert check_lcsc_against_cache(cache, "C9999999", "16nF", "C10") is None

    def test_none_cache_accepts(self):
        assert check_lcsc_against_cache(None, "C1525", "16nF", "C10") is None


class TestDeleteEnrichmentMatch:
    def test_delete_existing(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        assert cache.delete_enrichment_match("16nF", FP_0402_CAP) is True
        assert cache.get_enrichment_match("16nF", FP_0402_CAP, ignore_expiry=True) is None

    def test_delete_missing(self, tmp_path):
        cache = _make_cache(tmp_path)
        assert cache.delete_enrichment_match("16nF", FP_0402_CAP) is False


# ---------------------------------------------------------------------------
# Layer 1: enrichment cache fallback (the original #3590 scenario)
# ---------------------------------------------------------------------------


class TestCacheFallbackValueGuard:
    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_poisoned_cache_entry_rejected_and_evicted(self, MockSuggester, tmp_path, caplog):
        """16nF row + poisoned cache (C1525/100nF) -> no assignment,
        WARNING logged, cache entry evicted."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        cache = _poisoned_cache(tmp_path)

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("C10", "16nF", FP_0402_CAP),
            _make_item("C11", "16nF", FP_0402_CAP),
        ]

        with caplog.at_level(logging.WARNING):
            report = enrich_bom_lcsc(items)

        # The wrong part must NOT be assigned
        assert items[0].lcsc == ""
        assert items[1].lcsc == ""
        assert report.cache_matched == 0
        assert report.unmatched == 1
        entry = report.unmatched_entries[0]
        assert "C1525" in entry.error
        assert "100nF" in entry.error

        # WARNING mentions both values
        warning_text = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
        assert "C1525" in warning_text
        assert "100nF" in warning_text
        assert "16nF" in warning_text

        # Poisoned entry evicted so it cannot strike again
        assert cache.get_enrichment_match("16nF", FP_0402_CAP, ignore_expiry=True) is None

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_valid_cache_entry_still_applied_with_warning(self, MockSuggester, tmp_path, caplog):
        """A consistent cache entry is applied, but at WARNING level
        (ignore_expiry fallback is a degraded mode)."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        cache = _make_cache(tmp_path)
        cache.put(C1525)
        cache.put_enrichment_match("100nF", FP_0402_CAP, "C1525", confidence=0.9, part_type="Basic")

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403")
        MockSuggester.return_value = mock_instance

        items = [_make_item("C1", "100nF", FP_0402_CAP)]

        with caplog.at_level(logging.WARNING, logger="kicad_tools.export.bom_enrich"):
            report = enrich_bom_lcsc(items)

        assert items[0].lcsc == "C1525"
        assert report.cache_matched == 1
        fallback_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Cache fallback" in r.message
        ]
        assert fallback_warnings, "cache fallback should log at WARNING"

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_cache_entry_with_unknown_part_still_applied(self, MockSuggester, tmp_path):
        """If the parts DB does not know the LCSC part, we cannot
        validate -- the fallback still applies (with WARNING)."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        cache = _make_cache(tmp_path)
        # Enrichment match present but no parts-table record
        cache.put_enrichment_match("16nF", FP_0402_CAP, "C123456", confidence=0.8, part_type="Ext")

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403")
        MockSuggester.return_value = mock_instance

        items = [_make_item("C10", "16nF", FP_0402_CAP)]
        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == "C123456"
        assert report.cache_matched == 1


class TestAutoMatchValueGuard:
    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_wrong_value_api_match_rejected_and_not_cached(self, MockSuggester, tmp_path, caplog):
        """An API suggestion whose description disagrees with the BOM
        value is rejected and never written to the enrichment cache."""
        from kicad_tools.cost.suggest import PartSuggestion, SuggestedPart

        cache = _make_cache(tmp_path)

        best = SuggestedPart(
            lcsc_part="C1525",
            mfr_part="CL05B104KO5NNNC",
            description=C1525.description,
            package="0402",
            stock=50000,
            is_basic=True,
            is_preferred=False,
            unit_price=0.001,
            confidence=0.9,
        )
        suggestion = PartSuggestion(
            reference="C10",
            value="16nF",
            footprint=FP_0402_CAP,
            package="0402",
            existing_lcsc=None,
            suggestions=[best],
            best_suggestion=best,
        )

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.return_value = suggestion
        MockSuggester.return_value = mock_instance

        items = [_make_item("C10", "16nF", FP_0402_CAP)]

        with caplog.at_level(logging.WARNING):
            report = enrich_bom_lcsc(items)

        assert items[0].lcsc == ""
        assert report.auto_matched == 0
        assert report.unmatched == 1
        # The wrong match must not poison the cache
        assert cache.get_enrichment_match("16nF", FP_0402_CAP, ignore_expiry=True) is None


# ---------------------------------------------------------------------------
# Layer 2: merge_lcsc read-back from the committed BOM CSV
# ---------------------------------------------------------------------------


class TestMergeReadBackValueGuard:
    def test_wrong_value_assignment_dropped(self, tmp_path, caplog):
        """A committed-BOM row carrying C1525 on a 16nF line is dropped
        instead of propagated."""
        cache = _poisoned_cache(tmp_path)
        existing = {("16nF", FP_0402_CAP): "C1525"}
        items = [
            _make_item("C10", "16nF", FP_0402_CAP),
            _make_item("C11", "16nF", FP_0402_CAP),
        ]

        with caplog.at_level(logging.WARNING):
            merged_count, merged_refs = apply_existing_lcsc_assignments(
                items, existing, parts_cache=cache
            )

        assert merged_count == 0
        assert merged_refs == set()
        assert items[0].lcsc == ""
        assert items[1].lcsc == ""

        warning_text = "\n".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "C1525" in warning_text
        assert "100nF" in warning_text

    def test_valid_assignment_preserved(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        existing = {("100nF", FP_0402_CAP): "C1525"}
        items = [_make_item("C1", "100nF", FP_0402_CAP)]

        merged_count, merged_refs = apply_existing_lcsc_assignments(
            items, existing, parts_cache=cache
        )

        assert merged_count == 1
        assert merged_refs == {"C1"}
        assert items[0].lcsc == "C1525"

    def test_unknown_part_preserved_without_cache_knowledge(self, tmp_path):
        cache = _make_cache(tmp_path)
        existing = {("16nF", FP_0402_CAP): "C987654"}
        items = [_make_item("C10", "16nF", FP_0402_CAP)]

        merged_count, _ = apply_existing_lcsc_assignments(items, existing, parts_cache=cache)

        assert merged_count == 1
        assert items[0].lcsc == "C987654"

    def test_no_cache_behaves_like_before(self):
        existing = {("10k", "0402"): "C123456"}
        items = [_make_item("R1", "10k", "0402")]

        merged_count, merged_refs = apply_existing_lcsc_assignments(
            items, existing, parts_cache=None
        )

        assert merged_count == 1
        assert merged_refs == {"R1"}
        assert items[0].lcsc == "C123456"

    def test_existing_lcsc_and_dnp_skipped(self, tmp_path):
        cache = _make_cache(tmp_path)
        existing = {("10k", "0402"): "C123456"}
        items = [
            _make_item("R1", "10k", "0402", lcsc="C999999"),
            _make_item("R2", "10k", "0402", dnp=True),
        ]

        merged_count, _ = apply_existing_lcsc_assignments(items, existing, parts_cache=cache)

        assert merged_count == 0
        assert items[0].lcsc == "C999999"
        assert items[1].lcsc == ""


# ---------------------------------------------------------------------------
# Layer 3: BOM preflight value-mismatch gate
# ---------------------------------------------------------------------------


class TestPreflightLcscValueCheck:
    def _checker(self, tmp_path: Path, items: list[BOMItem], cache: PartsCache) -> PreflightChecker:
        checker = PreflightChecker(
            pcb_path=tmp_path / "board.kicad_pcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
            parts_cache=cache,
        )
        checker._bom = BOM(items=items)
        return checker

    def test_mismatch_fails(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        items = [_make_item("C10", "16nF", FP_0402_CAP, lcsc="C1525")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.name == "bom_lcsc_values"
        assert result.status == "FAIL"
        assert "C1525" in result.details
        assert "100nF" in result.details

    def test_matching_assignment_ok(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        items = [_make_item("C1", "100nF", FP_0402_CAP, lcsc="C1525")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "OK"

    def test_unknown_parts_ok(self, tmp_path):
        cache = _make_cache(tmp_path)
        items = [_make_item("C10", "16nF", FP_0402_CAP, lcsc="C424242")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "OK"

    def test_missing_lcsc_and_dnp_ignored(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        items = [
            _make_item("C10", "16nF", FP_0402_CAP),  # no LCSC -> other check
            _make_item("C11", "16nF", FP_0402_CAP, lcsc="C1525", dnp=True),
        ]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "OK"
