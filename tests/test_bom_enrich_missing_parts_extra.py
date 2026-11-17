"""Tests for the hard, actionable failure when the ``parts`` extra is missing.

Issue #4104: ``kct export <board> --mfr jlcpcb --auto-lcsc`` on a venv without
the optional ``parts`` extra (``requests``) used to soft-fail -- every BOM
group was recorded as "unmatched" with the ImportError text, the BOM CSV was
written with an empty ``LCSC Part #`` column, the manifest/bundle was finalized,
and the command exited 0.  A missing capability is a precondition failure, not a
degraded result, so it must now:

- translate the swallowed ``ImportError`` into a distinct
  :class:`LCSCDependencyMissingError` at :meth:`PartSuggester.suggest_for_component`,
- short-circuit :func:`enrich_bom_lcsc` on the first occurrence (mirroring the
  existing ``LCSCForbiddenError`` circuit breaker) and re-raise,
- propagate out of ``AssemblyPackage.export`` / ``_generate_bom`` before the BOM
  CSV is written, and
- abort the manufacturing pipeline before the manifest is finalized, so
  ``kct export`` exits non-zero with the install hint.

These tests simulate the missing ``requests`` import by monkeypatching the
search/import path to raise ``ImportError`` -- they do NOT uninstall ``requests``
in CI (mirroring PR #4111's ``test_cmaes_missing_extra.py`` approach), so they
run deterministically regardless of the real environment's installed packages.

Genuine no-match results (dependency present, API reachable, zero candidates)
must continue to produce the existing warning-only degrade with exit 0 -- this
issue must not regress that legitimate use case.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cost.suggest import PartSuggester
from kicad_tools.export.bom_enrich import enrich_bom_lcsc
from kicad_tools.parts.lcsc import (
    PARTS_INSTALL_HINT,
    LCSCDependencyMissingError,
    LCSCForbiddenError,
)
from kicad_tools.schema.bom import BOMItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(ref: str, value: str, footprint: str, lcsc: str = "") -> BOMItem:
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:R",
        lcsc=lcsc,
    )


def _make_suggester_mock(mock_instance: MagicMock) -> None:
    """Configure a mock PartSuggester context manager with no cache."""
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    mock_client = MagicMock()
    mock_client.cache = None
    mock_instance._get_client.return_value = mock_client


# ---------------------------------------------------------------------------
# Install hint constant
# ---------------------------------------------------------------------------


class TestPartsInstallHint:
    """The canonical install hint must name the 'parts' extra (PR #4111 shape)."""

    def test_hint_names_parts_extra(self):
        assert "parts" in PARTS_INSTALL_HINT
        assert "kicad-tools[parts]" in PARTS_INSTALL_HINT

    def test_hint_gives_both_uv_and_pip(self):
        assert "uv sync --extra parts" in PARTS_INSTALL_HINT
        assert "pip install" in PARTS_INSTALL_HINT


# ---------------------------------------------------------------------------
# suggest.py: ImportError -> LCSCDependencyMissingError (not swallowed)
# ---------------------------------------------------------------------------


class TestSuggestForComponentDependencyMissing:
    """The dependency ImportError must surface distinctly, not as a no-match."""

    def test_import_error_raises_dependency_missing(self):
        """A search-time ImportError becomes LCSCDependencyMissingError."""
        suggester = PartSuggester()

        client = MagicMock()
        client.search.side_effect = ImportError(PARTS_INSTALL_HINT)
        with patch.object(suggester, "_get_client", return_value=client):
            with pytest.raises(LCSCDependencyMissingError) as excinfo:
                suggester.suggest_for_component(
                    reference="R1",
                    value="10k",
                    footprint="Resistor_SMD:R_0402_1005Metric",
                )

        # Actionable hint is preserved, and the original ImportError chains.
        assert "kicad-tools[parts]" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ImportError)

    def test_genuine_no_match_still_degrades(self):
        """A real 'no candidates' result must NOT raise -- warning-only degrade."""
        suggester = PartSuggester()

        results = MagicMock()
        results.parts = []  # API reachable, zero candidates
        client = MagicMock()
        client.search.return_value = results
        with patch.object(suggester, "_get_client", return_value=client):
            suggestion = suggester.suggest_for_component(
                reference="U1",
                value="STM32C011F4P6",
                footprint="Package_SO:TSSOP-20",
            )

        # No exception; a normal (empty) suggestion is returned.
        assert suggestion.has_suggestion is False


# ---------------------------------------------------------------------------
# bom_enrich.py: short-circuit + re-raise on first dependency-missing signal
# ---------------------------------------------------------------------------


class TestEnrichBomLcscDependencyMissing:
    """enrich_bom_lcsc must raise (not accumulate unmatched) when the extra is gone."""

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_first_import_error_short_circuits_and_raises(self, MockSuggester):
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        mock_instance.suggest_for_component.side_effect = LCSCDependencyMissingError(
            PARTS_INSTALL_HINT
        )
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
            _make_item("L1", "4.7uH", "Inductor_SMD:L_0603_1608Metric"),
        ]

        with pytest.raises(LCSCDependencyMissingError):
            enrich_bom_lcsc(items)

        # Short-circuited after the FIRST group -- did not loop all three
        # accumulating misleading "no LCSC match" entries.
        assert mock_instance.suggest_for_component.call_count == 1

    @patch("kicad_tools.export.bom_enrich.PartSuggester")
    def test_forbidden_still_falls_back_to_cache(self, MockSuggester):
        """The LCSCForbiddenError circuit breaker is unaffected (issue #3935)."""
        mock_instance = MagicMock()
        _make_suggester_mock(mock_instance)
        mock_instance.suggest_for_component.side_effect = LCSCForbiddenError("403 Forbidden")
        MockSuggester.return_value = mock_instance

        items = [
            _make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric"),
            _make_item("C1", "100nF", "Capacitor_SMD:C_0402_1005Metric"),
        ]

        # Forbidden must NOT raise -- it degrades to cache/unmatched (exit 0).
        report = enrich_bom_lcsc(items)
        assert report.unmatched == 2
        assert mock_instance.suggest_for_component.call_count == 1


# ---------------------------------------------------------------------------
# assembly / manufacturing: hard-fail, no BOM CSV, no manifest
# ---------------------------------------------------------------------------


class TestAssemblyPropagatesDependencyMissing:
    """AssemblyPackage.export must let the dependency error escape, unwritten BOM."""

    def test_generate_bom_raises_before_writing_csv(self, tmp_path):
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage
        from kicad_tools.schema.bom import BOM

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        config = AssemblyConfig()
        config.auto_lcsc = True
        config.no_spec = True  # skip spec-overlay auto-detection
        config.bom_source = "pcb"  # avoid needing a schematic on disk

        pkg = AssemblyPackage(
            pcb_path=pcb,
            schematic_path=None,
            manufacturer="jlcpcb",
            config=config,
        )

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        bom = BOM(items=[_make_item("R1", "10k", "Resistor_SMD:R_0402_1005Metric")])

        with (
            patch("kicad_tools.schema.bom.extract_bom_from_pcb", return_value=bom),
            patch(
                "kicad_tools.export.assembly.enrich_bom_lcsc",
                side_effect=LCSCDependencyMissingError(PARTS_INSTALL_HINT),
            ),
            pytest.raises(LCSCDependencyMissingError),
        ):
            pkg._generate_bom(out_dir)

        # No BOM CSV was written -- the failure happens before export_bom().
        assert list(out_dir.glob("*.csv")) == []


class TestManufacturingHardFailsWithoutParts:
    """The full pipeline must abort before the manifest when the extra is gone."""

    def test_no_manifest_written_and_error_reported(self, tmp_path):
        from kicad_tools.export.manufacturing import ManufacturingPackage

        pkg = ManufacturingPackage.__new__(ManufacturingPackage)  # skip __init__
        pkg.pcb_path = tmp_path / "board.kicad_pcb"
        pkg.schematic_path = None
        pkg.manufacturer = "jlcpcb"
        pkg.config = MagicMock()

        result = MagicMock()
        result.errors = []
        result.assembly_result = None

        # Simulate AssemblyPackage.export() raising the dependency error.
        assembly = MagicMock()
        assembly.export.side_effect = LCSCDependencyMissingError(PARTS_INSTALL_HINT)

        out_dir = tmp_path / "out"
        with patch(
            "kicad_tools.export.manufacturing.AssemblyPackage",
            return_value=assembly,
        ):
            assembly_ok = pkg._generate_assembly(out_dir, result)

        # The stage aborts (returns False) so the manufacturing pipeline
        # skips Report/ProjectZip/Manifest -- no bundle is finalized.
        assert assembly_ok is False
        assert any("parts" in e for e in result.errors)
