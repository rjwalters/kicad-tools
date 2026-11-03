"""Exempt refs must not pick up an LCSC from the CSV merge source (issue #8282)."""

from __future__ import annotations

import pytest

from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.schema.bom import BOM, BOMItem

_KCT = """kct_version: '1.0'
project:
  name: exempt merge demo
  revision: A
requirements:
  manufacturing:
    target_fab: jlcpcb-tier1
    layers:
      preferred: 2
    assembly: both
bom_entries:
- ref: R1
  part: ''
  exempt: consigned
  exempt_reason: owner supplies
"""


def _items_after_merge(tmp_path):
    (tmp_path / "project.kct").write_text(_KCT)
    (tmp_path / "bom_jlcpcb.csv").write_text(
        "Comment,Designator,Footprint,LCSC Part #\n10k,R1,R_0402,C1234\n"
    )
    checker = PreflightChecker(
        pcb_path=tmp_path / "b.kicad_pcb",
        schematic_path=None,
        config=PreflightConfig(),
        parts_cache=PartsCache(db_path=tmp_path / "c.db"),
        output_dir=tmp_path,
    )
    items = [
        BOMItem(reference=r, value="10k", footprint="R_0402", lib_id="Device:R", lcsc="")
        for r in ("R1", "R2")
    ]
    return {i.reference: i for i in checker._items_with_spec_overlay(BOM(items=items))}


def test_exempt_ref_skips_csv_merge_but_non_exempt_merges(tmp_path):
    by_ref = _items_after_merge(tmp_path)
    assert by_ref["R1"].lcsc == ""
    assert by_ref["R2"].lcsc == "C1234"


# ---------------------------------------------------------------------------
# Export-level pins (the written bom_jlcpcb.csv), both auto-lcsc settings.
# ---------------------------------------------------------------------------


def _export_board06_with_exempt_r1(tmp_path, auto_lcsc, *, filter_export=True):
    import csv
    import shutil
    from unittest.mock import MagicMock, patch

    from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage
    from tests._board06_sourcing import BOARD06, BOARD06_PCB

    spec = tmp_path / "project.kct"
    spec.write_text(_KCT)
    source = tmp_path / "merge_source.csv"
    shutil.copy(BOARD06 / "output" / "manufacturing" / "bom_jlcpcb.csv", source)

    mock = MagicMock()
    mock.__enter__.return_value = mock
    mock._get_client.return_value = None
    mock.suggest_for_component.side_effect = AssertionError("No catalog calls expected")
    with (
        patch("kicad_tools.export.bom_enrich.PartSuggester", return_value=mock),
        patch("kicad_tools.parts.cache.PartsCache", return_value=None),
    ):
        config = AssemblyConfig(
            include_bom=True,
            include_pnp=False,
            include_gerbers=False,
            auto_lcsc=auto_lcsc,
            spec_path=spec,
            merge_lcsc_from=source,
        )
        package = AssemblyPackage(pcb_path=BOARD06_PCB, manufacturer="jlcpcb-tier1", config=config)
        result = package.export(output_dir=tmp_path / "out")
    with result.bom_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    return rows, result


@pytest.mark.parametrize("auto_lcsc", [True, False])
def test_export_written_bom_keeps_exempt_ref_blank(tmp_path, auto_lcsc):
    """Exempt R1 stays blank in bom_jlcpcb.csv; same value/footprint R2-R4 merge."""
    rows, result = _export_board06_with_exempt_r1(tmp_path, auto_lcsc)
    r1 = next(r for r in rows if "R1" in r["Designator"].split(","))
    r2 = next(r for r in rows if "R2" in r["Designator"].split(","))
    assert r1["LCSC Part #"] == ""
    assert "R2" in r2["Designator"] and r2["LCSC Part #"] == "C22775"
    assert result.lcsc_merge_exempt_skipped == ["R1 (source C22775)"]
    if auto_lcsc:
        assert any(
            e.source == "spec_exempt" and e.references == ["R1"]
            for e in result.lcsc_enrichment.entries
        )
