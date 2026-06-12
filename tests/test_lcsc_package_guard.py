"""Tests for LCSC package-mismatch guards (issue #3597).

Reproduces the manufacturing defect where C1525 -- a 100nF 0402
capacitor -- was assigned to 100nF BOM rows with 0805 footprints
(board-05).  The *value* matched, so the #3590 value guard could not
catch it; the part is half the size of the pads.

Verifies the same three defense layers as the value guard:

1. ``enrich_bom_lcsc`` cache fallback rejects + evicts wrong-package
   entries.
2. ``apply_existing_lcsc_assignments`` (merge_lcsc read-back) drops
   committed-BOM assignments whose known package mismatches the
   footprint.
3. BOM preflight (``bom_lcsc_values``) FAILs on assigned parts whose
   known package mismatches the footprint (combined value/package
   report).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from kicad_tools.export.bom_enrich import enrich_bom_lcsc
from kicad_tools.export.bom_formats import apply_existing_lcsc_assignments
from kicad_tools.export.lcsc_value_check import (
    check_lcsc_against_cache,
    find_package_mismatch,
)
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FP_0805_CAP = "Capacitor_SMD:C_0805_2012Metric"
FP_0402_CAP = "Capacitor_SMD:C_0402_1005Metric"

# The actual on-machine C1525 record shape that shipped the defect:
# empty package, empty value, description is just the MPN.
C1525_SPARSE = Part(
    lcsc_part="C1525",
    mfr_part="CL05B104KO5NNNC",
    description="CL05B104KO5NNNC",
    package="",
    value="",
)

# The correct 100nF 0805 Basic part (Yageo CC0805KRX7R9BB104), as it
# appears in the local cache (sparse: MPN-only).
C49678_SPARSE = Part(
    lcsc_part="C49678",
    mfr_part="CC0805KRX7R9BB104",
    description="CC0805KRX7R9BB104",
    package="",
    value="",
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
    """Cache primed with the #3597 poison: 100nF/0805 -> C1525 (0402)."""
    cache = _make_cache(tmp_path)
    cache.put(C1525_SPARSE)
    cache.put_enrichment_match("100nF", FP_0805_CAP, "C1525", confidence=0.9, part_type="Basic")
    return cache


def _make_suggester_mock(mock_instance: MagicMock, cache: PartsCache | None) -> None:
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    mock_client = MagicMock()
    mock_client.cache = cache
    mock_instance._get_client.return_value = mock_client


# ---------------------------------------------------------------------------
# find_package_mismatch unit tests
# ---------------------------------------------------------------------------


class TestFindPackageMismatch:
    def test_package_field_mismatch(self):
        m = find_package_mismatch(FP_0805_CAP, "C12", part_package="0402")
        assert m is not None
        assert m.requested_package == "0805"
        assert m.candidate_package == "0402"
        assert "0402" in m.describe() and "0805" in m.describe()

    def test_package_field_match(self):
        assert find_package_mismatch(FP_0805_CAP, "C12", part_package="0805") is None

    def test_description_token_mismatch(self):
        m = find_package_mismatch(
            FP_0805_CAP, "C12", part_description="16V 100nF X7R ±10% 0402 MLCC"
        )
        assert m is not None
        assert m.candidate_package == "0402"

    def test_description_ignores_part_number_tokens(self):
        # "RC0805FR-0710KL" embedded in free text must not be read as a
        # description token (it IS decodable as an MPN, but only via the
        # dedicated decoder with its family rules).
        m = find_package_mismatch(
            FP_0402_CAP, "R1", part_description="some text RC0805FR-0710KL more text"
        )
        assert m is None  # not at start -> MPN decoder also passes

    def test_unknown_package_accepts(self):
        assert find_package_mismatch(FP_0805_CAP, "C12") is None

    def test_non_chip_footprint_accepts(self):
        # IC/connector packages are out of scope.
        m = find_package_mismatch("Package_TO_SOT_SMD:SOT-23", "U1", part_package="0402")
        assert m is None

    def test_package_field_takes_precedence_over_mpn(self):
        # Structured field says 0805 -> match, even though MPN says 0402.
        assert (
            find_package_mismatch(
                FP_0805_CAP, "C12", part_package="0805", part_mfr="CL05B104KO5NNNC"
            )
            is None
        )


class TestMpnPackageDecoding:
    """Family-specific size-code decoding from chip-passive MPNs."""

    def test_real_machine_poison_record(self):
        # The actual C1525 record: package='', description=MPN.
        m = find_package_mismatch(
            FP_0805_CAP,
            "C12",
            part_package="",
            part_description="CL05B104KO5NNNC",
            part_mfr="CL05B104KO5NNNC",
        )
        assert m is not None
        assert m.candidate_package == "0402"
        assert "CL05" in m.candidate_source

    def test_samsung_cl_match_accepts(self):
        assert find_package_mismatch(FP_0402_CAP, "C1", part_mfr="CL05B104KO5NNNC") is None

    def test_yageo_literal_size(self):
        # CC0805KRX7R9BB104 (the C49678 fix) decodes to 0805.
        assert find_package_mismatch(FP_0805_CAP, "C12", part_mfr="CC0805KRX7R9BB104") is None
        m = find_package_mismatch(FP_0402_CAP, "C12", part_mfr="CC0805KRX7R9BB104")
        assert m is not None
        assert m.candidate_package == "0805"

    def test_murata_grm(self):
        m = find_package_mismatch(FP_0402_CAP, "C1", part_mfr="GRM21BR61E475KA12L")
        assert m is not None
        assert m.candidate_package == "0805"
        assert find_package_mismatch(FP_0805_CAP, "C1", part_mfr="GRM21BR61E475KA12L") is None

    def test_tdk_metric_size(self):
        # TDK encodes the METRIC size: C2012 = 0805 imperial.
        assert find_package_mismatch(FP_0805_CAP, "C1", part_mfr="C2012X7R1H104KT0J0N") is None
        m = find_package_mismatch(FP_0402_CAP, "C1", part_mfr="C2012X7R1H104KT0J0N")
        assert m is not None
        assert m.candidate_package == "0805"

    def test_taiyo_yuden(self):
        assert find_package_mismatch(FP_0805_CAP, "C1", part_mfr="EMK212B7475KG-T") is None

    def test_uniroyal_leading_size(self):
        m = find_package_mismatch("Resistor_SMD:R_0402_1005Metric", "R1", part_mfr="0805W8F1002T5E")
        assert m is not None
        assert m.candidate_package == "0805"

    def test_unknown_families_accept(self):
        # Families without a known size convention must not be guessed:
        # electrolytics, tantalums, KOA, Panasonic, Walsin, CGA...
        for mpn in (
            "KM107M050F12RR0VH2FP0",
            "TAJB107M006RNJ",
            "RK73B2ATTD103J",
            "ERJPB6B1002V",
            "WR08X1002FTL",
            "CGA4J1X7R1E475KT0Y0E",
            "RTT051002FTP",
            "RS-05K103JT",
        ):
            assert find_package_mismatch(FP_0805_CAP, "C1", part_mfr=mpn) is None, mpn

    def test_known_family_unknown_size_code_accepts(self):
        # CL99 is not a known Samsung size code -- do not guess.
        assert find_package_mismatch(FP_0805_CAP, "C1", part_mfr="CL99B104KO5NNNC") is None

    def test_mpn_decoding_gated_to_chip_passives(self):
        # An LED (D ref) on an 0805 footprint must not be validated via
        # chip-passive MPN conventions.
        assert (
            find_package_mismatch("LED_SMD:LED_0805_2012Metric", "D3", part_mfr="CL05B104KO5NNNC")
            is None
        )

    def test_non_passive_still_validated_via_description(self):
        # ... but a free-text description token still applies.
        m = find_package_mismatch(
            "LED_SMD:LED_0805_2012Metric", "D3", part_description="Red 0402 LED"
        )
        assert m is not None
        assert m.candidate_package == "0402"


class TestCheckLcscAgainstCachePackage:
    def test_known_part_package_mismatch(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        m = check_lcsc_against_cache(cache, "C1525", "100nF", "C12", footprint=FP_0805_CAP)
        assert m is not None
        assert "0402" in m.describe()
        assert "0805" in m.describe()

    def test_value_mismatch_reported_before_package(self, tmp_path):
        # 16nF row + C1525: the value disagreement (16nF vs 100nF) is
        # the primary defect and reported first.
        cache = _poisoned_cache(tmp_path)
        m = check_lcsc_against_cache(cache, "C1525", "16nF", "C12", footprint=FP_0805_CAP)
        assert m is not None
        assert "100nF" in m.describe()

    def test_correct_part_accepted(self, tmp_path):
        cache = _make_cache(tmp_path)
        cache.put(C49678_SPARSE)
        assert (
            check_lcsc_against_cache(cache, "C49678", "100nF", "C12", footprint=FP_0805_CAP) is None
        )

    def test_no_footprint_skips_package_check(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        assert check_lcsc_against_cache(cache, "C1525", "100nF", "C12") is None

    def test_unknown_part_accepts(self, tmp_path):
        cache = _make_cache(tmp_path)
        assert (
            check_lcsc_against_cache(cache, "C9999999", "100nF", "C12", footprint=FP_0805_CAP)
            is None
        )


# ---------------------------------------------------------------------------
# Layer 1: enrichment cache fallback
# ---------------------------------------------------------------------------


class TestCacheFallbackPackageGuard:
    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_wrong_package_cache_entry_rejected_and_evicted(self, MockSuggester, tmp_path, caplog):
        """100nF/0805 row + poisoned cache (C1525/0402) -> no
        assignment, WARNING logged, cache entry evicted."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        cache = _poisoned_cache(tmp_path)

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("C12", "100nF", FP_0805_CAP),
            _make_item("C13", "100nF", FP_0805_CAP),
        ]

        with caplog.at_level(logging.WARNING):
            report = enrich_bom_lcsc(items)

        # The wrong-size part must NOT be assigned
        assert items[0].lcsc == ""
        assert items[1].lcsc == ""
        assert report.cache_matched == 0
        assert report.unmatched == 1
        entry = report.unmatched_entries[0]
        assert "C1525" in entry.error
        assert "0402" in entry.error

        # WARNING mentions both packages
        warning_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "C1525" in warning_text
        assert "0402" in warning_text
        assert "0805" in warning_text

        # Poisoned entry evicted so it cannot strike again
        assert cache.get_enrichment_match("100nF", FP_0805_CAP, ignore_expiry=True) is None

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_right_package_cache_entry_still_applied(self, MockSuggester, tmp_path):
        """C1525 on a 100nF 0402 row is value- AND package-correct."""
        from kicad_tools.parts.lcsc import LCSCForbiddenError

        cache = _make_cache(tmp_path)
        cache.put(C1525_SPARSE)
        cache.put_enrichment_match("100nF", FP_0402_CAP, "C1525", confidence=0.9, part_type="Basic")

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403")
        MockSuggester.return_value = mock_instance

        items = [_make_item("C1", "100nF", FP_0402_CAP)]
        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == "C1525"
        assert report.cache_matched == 1


class TestAutoMatchPackageGuard:
    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_wrong_package_api_match_rejected_and_not_cached(self, MockSuggester, tmp_path):
        """An API suggestion whose package disagrees with the footprint
        is rejected and never written to the enrichment cache."""
        from kicad_tools.cost.suggest import PartSuggestion, SuggestedPart

        cache = _make_cache(tmp_path)

        best = SuggestedPart(
            lcsc_part="C1525",
            mfr_part="CL05B104KO5NNNC",
            description="16V 100nF X7R ±10% 0402 Multilayer Ceramic Capacitors MLCC",
            package="0402",
            stock=50000,
            is_basic=True,
            is_preferred=False,
            unit_price=0.001,
            confidence=0.9,
        )
        suggestion = PartSuggestion(
            reference="C12",
            value="100nF",
            footprint=FP_0805_CAP,
            package="0805",
            existing_lcsc=None,
            suggestions=[best],
            best_suggestion=best,
        )

        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance, cache)
        mock_instance.suggest_for_component.return_value = suggestion
        MockSuggester.return_value = mock_instance

        items = [_make_item("C12", "100nF", FP_0805_CAP)]
        report = enrich_bom_lcsc(items)

        assert items[0].lcsc == ""
        assert report.auto_matched == 0
        assert report.unmatched == 1
        # The wrong match must not poison the cache
        assert cache.get_enrichment_match("100nF", FP_0805_CAP, ignore_expiry=True) is None


# ---------------------------------------------------------------------------
# Layer 2: merge_lcsc read-back from the committed BOM CSV
# ---------------------------------------------------------------------------


class TestMergeReadBackPackageGuard:
    def test_wrong_package_assignment_dropped(self, tmp_path, caplog):
        """The exact board-05 defect: a committed-BOM row carrying
        C1525 on a 100nF/0805 line is dropped instead of propagated."""
        cache = _poisoned_cache(tmp_path)
        existing = {("100nF", FP_0805_CAP): "C1525"}
        items = [
            _make_item("C12", "100nF", FP_0805_CAP),
            _make_item("C13", "100nF", FP_0805_CAP),
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
        assert "0402" in warning_text

    def test_correct_replacement_preserved(self, tmp_path):
        """The board-05 fix: C49678 on a 100nF/0805 line passes."""
        cache = _make_cache(tmp_path)
        cache.put(C49678_SPARSE)
        existing = {("100nF", FP_0805_CAP): "C49678"}
        items = [_make_item("C12", "100nF", FP_0805_CAP)]

        merged_count, merged_refs = apply_existing_lcsc_assignments(
            items, existing, parts_cache=cache
        )

        assert merged_count == 1
        assert merged_refs == {"C12"}
        assert items[0].lcsc == "C49678"

    def test_unknown_part_preserved_without_cache_knowledge(self, tmp_path):
        cache = _make_cache(tmp_path)
        existing = {("100nF", FP_0805_CAP): "C987654"}
        items = [_make_item("C12", "100nF", FP_0805_CAP)]

        merged_count, _ = apply_existing_lcsc_assignments(items, existing, parts_cache=cache)

        assert merged_count == 1
        assert items[0].lcsc == "C987654"


# ---------------------------------------------------------------------------
# Layer 3: BOM preflight combined value/package gate
# ---------------------------------------------------------------------------


class TestPreflightLcscPackageCheck:
    def _checker(self, tmp_path: Path, items: list[BOMItem], cache: PartsCache) -> PreflightChecker:
        checker = PreflightChecker(
            pcb_path=tmp_path / "board.kicad_pcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
            parts_cache=cache,
        )
        checker._bom = BOM(items=items)
        return checker

    def test_package_mismatch_fails(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        items = [_make_item("C12", "100nF", FP_0805_CAP, lcsc="C1525")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.name == "bom_lcsc_values"
        assert result.status == "FAIL"
        assert "C1525" in result.details
        assert "0402" in result.details
        assert "0805" in result.details

    def test_combined_value_and_package_report(self, tmp_path):
        """Value and package mismatches surface in one combined check."""
        cache = _poisoned_cache(tmp_path)
        items = [
            _make_item("C12", "100nF", FP_0805_CAP, lcsc="C1525"),  # package
            _make_item("C20", "16nF", FP_0402_CAP, lcsc="C1525"),  # value
        ]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "FAIL"
        assert "2 BOM item(s)" in result.message
        assert "package" in result.details  # package-mismatch line
        assert "value" in result.details  # value-mismatch line

    def test_correct_replacement_ok(self, tmp_path):
        cache = _make_cache(tmp_path)
        cache.put(C49678_SPARSE)
        items = [_make_item("C12", "100nF", FP_0805_CAP, lcsc="C49678")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "OK"

    def test_matching_package_ok(self, tmp_path):
        cache = _poisoned_cache(tmp_path)
        items = [_make_item("C1", "100nF", FP_0402_CAP, lcsc="C1525")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "OK"

    def test_unknown_parts_ok(self, tmp_path):
        cache = _make_cache(tmp_path)
        items = [_make_item("C12", "100nF", FP_0805_CAP, lcsc="C424242")]
        checker = self._checker(tmp_path, items, cache)

        result = checker._check_bom_lcsc_values()

        assert result.status == "OK"
