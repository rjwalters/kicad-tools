"""Exempt refs must not pick up an LCSC from the CSV merge source (issue #8282)."""

from __future__ import annotations

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
