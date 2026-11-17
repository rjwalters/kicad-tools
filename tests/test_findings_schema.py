"""Findings model and its emitters validate against the committed schema (#8015)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import jsonschema
import pytest

from kicad_tools.findings import SCHEMA_VERSION, from_drc_dict, from_violation_dict, split_items

SCHEMA = json.loads(
    (Path(__file__).parent.parent / "src/kicad_tools/data/schemas/finding.schema.json").read_text()
)


def _validate(findings: list[dict]) -> None:
    assert findings
    for f in findings:
        jsonschema.validate(f, SCHEMA)


def test_split_items_forms():
    refs, pads = split_items(
        [
            "C5",
            "U1-3",
            "R2.1 hole",
            "Pad 6 [<no net>] of U3 on F.Cu",
            "Footprint C52",
            "net_settings",
        ]
    )
    assert refs == ["C5", "U1", "R2", "U3", "C52"]
    assert pads == ["U1.3", "R2.1", "U3.6"]


def test_violation_without_location_has_null_point():
    f = from_violation_dict({"rule_id": "x", "severity": "error", "message": "m", "items": []})
    assert f.point is None and f.bbox is None
    jsonschema.validate(f.to_dict(), SCHEMA)


def test_drc_dict_bbox_from_two_locations():
    f = from_drc_dict(
        {
            "type": "clearance",
            "type_str": "clearance",
            "severity": "error",
            "message": "m",
            "items": ["Pad 1 of U1 on F.Cu"],
            "nets": ["GND"],
            "locations": [
                {"x_mm": 1.0, "y_mm": 5.0, "layer": "F.Cu"},
                {"x_mm": 3.0, "y_mm": 2.0, "layer": "F.Cu"},
            ],
        }
    )
    assert f.point == [1.0, 5.0]
    assert f.bbox == [1.0, 2.0, 3.0, 5.0]
    assert f.layer == "F.Cu" and f.pads == ["U1.1"]


def test_check_output_json_validates():
    from kicad_tools.cli.check_cmd import output_json
    from kicad_tools.validate import DRCResults, DRCViolation

    v = DRCViolation(
        rule_id="clearance_trace_trace",
        severity="error",
        message="too close",
        location=(10.0, 20.0),
        layer="F.Cu",
        items=("U1-2", "C5"),
        nets=("GND",),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        output_json([v], DRCResults(violations=[v]), Path("b.kicad_pcb"), "jlcpcb", 2)
    data = json.loads(buf.getvalue())
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["violations"][0]["rule_id"] == "clearance_trace_trace"  # legacy key intact
    _validate(data["findings"])
    assert data["findings"][0]["pads"] == ["U1.2"]
    assert data["findings"][0]["point"] == [10.0, 20.0]


def test_mistakes_output_json_validates_and_uses_file_frame():
    from kicad_tools.cli.mistakes_cmd import _output_json
    from kicad_tools.explain.mistakes import Mistake, MistakeCategory

    m = Mistake(
        category=MistakeCategory.BYPASS_CAP,
        severity="warning",
        title="Far cap",
        components=["C5", "U1"],
        explanation="e",
        fix_suggestion="f",
        location=(1.0, 2.0),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        _output_json([m], origin=(100.0, 50.0))
    data = json.loads(buf.getvalue())
    assert data["schema_version"] == SCHEMA_VERSION
    _validate(data["findings"])
    assert data["findings"][0]["point"] == [101.0, 52.0]
    assert data["mistakes"][0]["location"] == [1.0, 2.0]  # legacy key unchanged


def test_drc_output_json_validates():
    from kicad_tools.cli.drc_cmd import output_json
    from kicad_tools.drc import DRCReport, DRCViolation, ViolationType
    from kicad_tools.drc.violation import Location, Severity

    v = DRCViolation(
        type=ViolationType.CLEARANCE_PAD_PAD,
        type_str="clearance",
        severity=Severity.ERROR,
        message="m",
        locations=[Location(x_mm=1.0, y_mm=2.0, layer="F.Cu")],
        items=["Pad 1 of U1 on F.Cu"],
        nets=["GND"],
    )
    report = DRCReport(source_file="b.json", created_at=None, pcb_name="b", violations=[v])
    buf = io.StringIO()
    with redirect_stdout(buf):
        output_json([v], report)
    data = json.loads(buf.getvalue())
    assert data["schema_version"] == SCHEMA_VERSION
    _validate(data["findings"])
    assert data["findings"][0]["pads"] == ["U1.1"]


@pytest.mark.parametrize("bad", [{"schema_version": 2}, {}])
def test_schema_rejects_bad(bad):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, SCHEMA)


def _net_status_json(pcb: Path, capsys) -> dict:
    from kicad_tools.cli.net_status_cmd import main

    main([str(pcb), "--format", "json"])
    return json.loads(capsys.readouterr().out)


def test_net_status_output_json_validates(tmp_path, capsys):
    from tests.test_net_status_cmd import UNROUTED_PCB

    pcb = tmp_path / "unrouted.kicad_pcb"
    pcb.write_text(UNROUTED_PCB)
    data = _net_status_json(pcb, capsys)
    assert data["schema_version"] == SCHEMA_VERSION
    assert "nets" in data and "summary" in data  # legacy keys intact
    _validate(data["findings"])
    sig1 = next(f for f in data["findings"] if f["nets"] == ["SIG1"])
    assert sig1["rule_id"].startswith("net_status.")
    assert sig1["pads"]  # REF.PAD of the unconnected pads
    assert sig1["point"] is not None


def test_net_status_findings_use_file_frame_on_offset_board(tmp_path, capsys):
    """Edge.Cuts origin is added back: PadInfo is board-relative (#8015)."""
    from tests.test_net_status_cmd import UNROUTED_PCB

    base = tmp_path / "base.kicad_pcb"
    base.write_text(UNROUTED_PCB)
    shifted = tmp_path / "shifted.kicad_pcb"
    shifted.write_text(
        UNROUTED_PCB.replace("(start 0 0)", "(start 100 50)")
        .replace("(end 40 40)", "(end 140 90)")
        .replace("(at 10 20)", "(at 110 70)")
        .replace("(at 30 20)", "(at 130 70)")
    )
    a = {f["nets"][0]: f for f in _net_status_json(base, capsys)["findings"]}
    b = {f["nets"][0]: f for f in _net_status_json(shifted, capsys)["findings"]}
    assert a.keys() == b.keys()
    for net, fa in a.items():
        assert b[net]["pads"] == fa["pads"]
        assert b[net]["point"] == pytest.approx([fa["point"][0] + 100, fa["point"][1] + 50])


def test_net_status_complete_board_has_no_findings(tmp_path, capsys):
    from tests.test_net_status_cmd import CONNECTED_PCB

    pcb = tmp_path / "ok.kicad_pcb"
    pcb.write_text(CONNECTED_PCB)
    assert _net_status_json(pcb, capsys)["findings"] == []


def test_net_status_stranded_only_has_null_point():
    from types import SimpleNamespace

    from kicad_tools.findings import from_net_status

    net = SimpleNamespace(
        status="complete",
        net_name="GND",
        unconnected_pads=[],
        is_stranded=True,
        padless_components=1,
        stranded_items=["Via 3"],
        is_advisory_incomplete=False,
    )
    (f,) = from_net_status(net)
    assert f.rule_id == "net_status.stranded" and f.point is None
    jsonschema.validate(f.to_dict(), SCHEMA)


def _clearance_result(tmp_path):
    from kicad_tools.mcp.tools.analysis import measure_clearance
    from tests.test_mcp_clearance import CLEARANCE_TEST_PCB

    pcb = tmp_path / "c.kicad_pcb"
    pcb.write_text(CLEARANCE_TEST_PCB)
    return measure_clearance(str(pcb), "U1", "R1")


def test_measure_clearance_passing_has_empty_findings(tmp_path):
    d = _clearance_result(tmp_path).to_dict()
    assert d["schema_version"] == SCHEMA_VERSION
    assert d["findings"] == []
    assert "min_clearance_mm" in d  # legacy keys intact


def test_measure_clearance_failing_finding_validates_and_uses_file_frame(tmp_path):
    from dataclasses import replace

    result = _clearance_result(tmp_path)
    assert result.board_origin == (100.0, 100.0)  # Edge.Cuts start of the fixture
    failing = replace(result, passes_rules=False, required_clearance_mm=99.0)
    d = failing.to_dict()
    _validate(d["findings"])
    (f,) = d["findings"]
    assert f["point"] == pytest.approx([result.location[0] + 100.0, result.location[1] + 100.0])
    assert f["layer"] == result.layer
    assert set(f["refs"]) <= {"U1", "R1"} and f["refs"]
    assert d["location"] == {"x": result.location[0], "y": result.location[1]}  # unchanged


def test_detect_mistakes_real_run_on_nonzero_origin_board(tmp_path, capsys):
    """End-to-end carry-over from #8019: Edge.Cuts starts at (100, 100), U1 is
    placed at (115, 115) in the file, and the finding must say so."""
    from kicad_tools.cli.mistakes_cmd import main
    from tests.test_mcp_clearance import CLEARANCE_TEST_PCB

    pcb = tmp_path / "offset.kicad_pcb"
    pcb.write_text(CLEARANCE_TEST_PCB)
    main([str(pcb), "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    f = next(f for f in data["findings"] if f["refs"] == ["U1"])
    assert f["point"] == [115.0, 115.0]
    assert next(m for m in data["mistakes"] if m["components"] == ["U1"])["location"] == [
        15.0,
        15.0,
    ]  # legacy key stays board-relative
