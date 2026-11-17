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
