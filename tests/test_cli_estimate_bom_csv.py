"""``kct estimate cost --bom x.csv`` loads the CSV (Issue #7175)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from kicad_tools.cli import main


def _run(argv, capsys):
    rc = main(argv)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_csv_bom_json_is_one_document(minimal_pcb, tmp_path, capsys):
    bom = tmp_path / "bom.csv"
    bom.write_text(
        "Reference,Value,Footprint,LCSC\n"
        "R1,10k,R_0603_1608Metric,C25804\n"
        "C1,100nF,C_0603_1608Metric,C14663\n"
    )
    rc, out, _ = _run(
        ["estimate", "cost", str(minimal_pcb), "--bom", str(bom), "--format", "json", "--no-lcsc"],
        capsys,
    )
    assert rc == 0
    doc = json.loads(out)
    assert len(doc["components"]) > 0


def test_csv_bom_bad_inputs_exit_1_stdout_empty(minimal_pcb, tmp_path, capsys):
    empty = tmp_path / "empty.csv"
    empty.write_text("Reference,Value\n")
    nocols = tmp_path / "nocols.csv"
    nocols.write_text("foo,bar\n1,2\n")
    binary = tmp_path / "bin.csv"
    binary.write_bytes(b"\xff\xfe\x00\x80\x81\x82")
    for bom in (empty, nocols, binary, tmp_path / "missing.csv"):
        rc, out, err = _run(
            ["estimate", "cost", str(minimal_pcb), "--bom", str(bom), "--format", "json"], capsys
        )
        assert rc == 1, bom
        assert out == "", bom
        assert err, bom


def test_load_bom_csv_columns(tmp_path):
    from kicad_tools.schema.bom import load_bom_csv

    f = tmp_path / "b.csv"
    f.write_text("Designator,Comment,Package,JLC Part\nR1,1k,0402,C1\n,skipped,,\n")
    bom = load_bom_csv(f)
    assert [(i.reference, i.value, i.footprint, i.lcsc) for i in bom.items] == [
        ("R1", "1k", "0402", "C1")
    ]


def test_load_bom_csv_lcsc_header_variants(tmp_path):
    """Every spelling the loader ever accepted, plus the repo formatter's own
    ``LCSC Part #`` (Issue #9368), must populate ``BOMItem.lcsc``."""
    from kicad_tools.schema.bom import load_bom_csv

    for header in ("LCSC", "lcsc", "LCSC Part", "JLC Part", "LCSC Part #", "lcsc part#"):
        f = tmp_path / "b.csv"
        f.write_text(f"Designator,Comment,Footprint,{header}\nR1,1k,R_0402,C25804\n")
        bom = load_bom_csv(f)
        assert len(bom.items) == 1, header
        assert bom.items[0].lcsc == "C25804", header


def test_load_bom_csv_jlcpcb_formatter_round_trip(tmp_path):
    """Rows written by ``JLCPCBBOMFormatter`` must read back with designators
    and LCSC numbers intact (Issue #9368)."""
    from kicad_tools.export.bom_formats import JLCPCBBOMFormatter
    from kicad_tools.schema.bom import BOMItem, load_bom_csv

    items = [
        BOMItem(
            reference="R1",
            value="10k",
            footprint="Resistor_SMD:R_0603_1608Metric",
            lib_id="Device:R",
            lcsc="C25804",
        ),
        BOMItem(
            reference="C1",
            value="100nF",
            footprint="Capacitor_SMD:C_0603_1608Metric",
            lib_id="Device:C",
            lcsc="C14663",
        ),
    ]
    f = tmp_path / "bom_jlcpcb.csv"
    f.write_text(JLCPCBBOMFormatter().format(items))

    bom = load_bom_csv(f)
    loaded = {i.reference: i.lcsc for i in bom.items}
    assert loaded == {"C1": "C14663", "R1": "C25804"}


def test_load_bom_csv_lcsc_edge_cases(tmp_path):
    """Blank LCSC cell stays empty; a file with no LCSC column still loads."""
    from kicad_tools.schema.bom import load_bom_csv

    blank = tmp_path / "blank.csv"
    blank.write_text("Designator,Comment,Footprint,LCSC Part #\nR1,1k,R_0402,\n")
    bom = load_bom_csv(blank)
    assert [(i.reference, i.lcsc) for i in bom.items] == [("R1", "")]

    nolcsc = tmp_path / "nolcsc.csv"
    nolcsc.write_text("Designator,Comment,Footprint\nR1,1k,R_0402\n")
    bom = load_bom_csv(nolcsc)
    assert [(i.reference, i.lcsc) for i in bom.items] == [("R1", "")]


def test_load_bom_csv_board07_jlcpcb_bom_keeps_lcsc():
    """The committed board-07 ``bom_jlcpcb.csv`` (the repo's own formatter
    output) must load with LCSC numbers matching its rows (Issue #9368)."""
    from kicad_tools.schema.bom import load_bom_csv

    bom_csv = (
        Path(__file__).resolve().parents[1]
        / "boards"
        / "07-matchgroup-test"
        / "output"
        / "manufacturing"
        / "bom_jlcpcb.csv"
    )
    if not bom_csv.exists():
        pytest.skip("Board 07 manufacturing BOM not committed")

    with open(bom_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    populated = [r for r in rows if (r.get("LCSC Part #") or "").strip()]
    assert populated, "fixture has no populated LCSC rows; this guard is vacuous"

    bom = load_bom_csv(bom_csv)
    expected_lcsc: dict[str, str] = {}
    for r in rows:
        lcsc_val = (r.get("LCSC Part #") or "").strip()
        for ref in (part.strip() for part in (r.get("Designator") or "").split(",")):
            if ref:
                expected_lcsc[ref] = lcsc_val
    for item in bom.items:
        assert item.lcsc == expected_lcsc[item.reference], item.reference


def test_load_bom_csv_expands_grouped_designators(tmp_path):
    from kicad_tools.schema.bom import load_bom_csv

    f = tmp_path / "grouped.csv"
    f.write_text('Designator,Comment,Package,JLC Part\n"R1,R2,R3",10k,R_0603,C25804\n')
    bom = load_bom_csv(f)
    assert [(i.reference, i.value, i.footprint, i.lcsc) for i in bom.items] == [
        ("R1", "10k", "R_0603", "C25804"),
        ("R2", "10k", "R_0603", "C25804"),
        ("R3", "10k", "R_0603", "C25804"),
    ]
    assert bom.total_components == 3
    group = bom.grouped()[0]
    assert group.quantity == 3


def test_load_bom_csv_grouped_edge_cases(tmp_path):
    from kicad_tools.schema.bom import load_bom_csv

    f = tmp_path / "edges.csv"
    f.write_text(
        "Reference,Value,Footprint,LCSC\n"
        '"C1, C2 ,C3",100nF,C_0603,C14663\n'
        '"R4,R5,",10k,R_0603,C25804\n'
        ",skipped,,\n"
    )
    bom = load_bom_csv(f)
    assert [i.reference for i in bom.items] == ["C1", "C2", "C3", "R4", "R5"]
    assert bom.total_components == 5


def test_csv_bom_grouped_jlcpcb_rows_count_all_designators(minimal_pcb, tmp_path, capsys):
    bom = tmp_path / "bom_jlcpcb.csv"
    bom.write_text(
        "Designator,Comment,Package,JLC Part\n"
        '"D1,D2,D3","Green; LED green",LED_0603_1608Metric,C2286\n'
        '"R1,R2",10k,R_0603_1608Metric,C25804\n'
    )
    rc, out, _ = _run(
        ["estimate", "cost", str(minimal_pcb), "--bom", str(bom), "--format", "json", "--no-lcsc"],
        capsys,
    )
    assert rc == 0
    doc = json.loads(out)
    specs = doc["assembly"]["specs"]
    assert specs["smt_parts"] == 5
    assert specs["unique_parts"] == 2


def test_lcsc_header_normaliser_matches_sidecar_reader():
    """``_norm_csv_header`` mirrors ``pcb.lcsc_bom._norm`` exactly, but the
    two readers keep separate accept-lists (Issue #9402): the CSV loader
    matches only its own spellings while the KiCad sidecar reader matches
    the wider property-name set."""
    from kicad_tools.pcb.lcsc_bom import _LCSC_PROPERTY_NAMES, _norm
    from kicad_tools.schema.bom import _LCSC_CSV_HEADERS, _norm_csv_header

    for header in (
        "",
        "LCSC",
        "lcsc",
        "LCSC Part",
        "LCSC Part #",
        "lcsc_part_#",
        "LCSC Part Number",
        "JLC Part",
        "JLCPCB",
        "JLCPCB Part #",
        "née colonne",
    ):
        assert _norm_csv_header(header) == _norm(header), repr(header)

    assert _LCSC_CSV_HEADERS == ("lcsc", "lcscpart", "jlcpart")
    assert sorted(_LCSC_PROPERTY_NAMES) == [
        "jlcpcb",
        "jlcpcbpart",
        "lcsc",
        "lcscpart",
        "lcscpartnumber",
    ]


def test_load_bom_csv_rejects_sidecar_only_lcsc_spellings(tmp_path):
    """Spellings only ``pcb.lcsc_bom`` accepts (``LCSC Part Number``,
    ``JLCPCB Part #``, ``JLCPCB``) intentionally do NOT populate
    ``BOMItem.lcsc`` in the CSV loader (Issue #9402)."""
    from kicad_tools.schema.bom import load_bom_csv

    for header in ("LCSC Part Number", "JLCPCB Part #", "JLCPCB"):
        f = tmp_path / "b.csv"
        f.write_text(f"Designator,Comment,Footprint,{header}\nR1,1k,R_0402,C25804\n")
        bom = load_bom_csv(f)
        assert len(bom.items) == 1, header
        assert bom.items[0].lcsc == "", header


def test_load_bom_csv_colliding_lcsc_columns_prefer_first_non_empty(tmp_path):
    """Two headers that normalise to the same key: a blank later column must
    not discard a populated earlier one (Issue #9402; pre-#9379 behaviour)."""
    from kicad_tools.schema.bom import load_bom_csv

    blank_last = tmp_path / "blank_last.csv"
    blank_last.write_text(
        "Designator,Comment,Footprint,LCSC Part,LCSC Part #\nR1,1k,R_0402,CAAA,\n"
    )
    bom = load_bom_csv(blank_last)
    assert [i.lcsc for i in bom.items] == ["CAAA"]

    blank_first = tmp_path / "blank_first.csv"
    blank_first.write_text(
        "Designator,Comment,Footprint,LCSC Part,LCSC Part #\nR1,1k,R_0402,,CAAA\n"
    )
    bom = load_bom_csv(blank_first)
    assert [i.lcsc for i in bom.items] == ["CAAA"]

    both = tmp_path / "both.csv"
    both.write_text("Designator,Comment,Footprint,LCSC Part,LCSC Part #\nR1,1k,R_0402,C1,C2\n")
    bom = load_bom_csv(both)
    assert [i.lcsc for i in bom.items] == ["C1"]

    distinct = tmp_path / "distinct.csv"
    distinct.write_text("Designator,Comment,Footprint,LCSC,JLC Part\nR1,1k,R_0402,,CBBB\n")
    bom = load_bom_csv(distinct)
    assert [i.lcsc for i in bom.items] == ["CBBB"]
