"""``kct estimate cost --bom x.csv`` loads the CSV (Issue #7175)."""

from __future__ import annotations

import json

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
