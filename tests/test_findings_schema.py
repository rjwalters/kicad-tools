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
