"""Native DRC checks saved copper and rejects unavailable/malformed reports."""

import json

import pytest

from kicad_tools.cli import readiness_cmd as cmd


@pytest.mark.parametrize(
    "report,code,ok,errors",
    [
        ({"violations": [], "unconnected_items": []}, 0, True, 0),
        (
            {
                "violations": [
                    {"type": "clearance", "severity": "error", "description": "gap", "items": []}
                ],
                "unconnected_items": [],
            },
            0,
            True,
            1,
        ),
        (
            {
                "violations": [],
                "unconnected_items": [
                    {
                        "type": "unconnected_items",
                        "severity": "error",
                        "description": "open",
                        "items": [],
                    }
                ],
            },
            0,
            True,
            1,
        ),
        ({"violations": []}, 0, False, None),
        ({"violations": [], "unconnected_items": []}, 1, False, None),
        ([], 0, False, None),
    ],
)
def test_saved_native_report_contract(tmp_path, monkeypatch, report, code, ok, errors):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    output = tmp_path / "native-drc.json"

    def native(args, raw):
        assert "--refill-zones" not in args
        assert str(pcb) == args[-1]
        raw.write_text(json.dumps(report))
        return cmd.EngineRun(True, returncode=code)

    monkeypatch.setattr(cmd, "_run_kicad_cli", native)
    run = cmd._run_native_drc(pcb, output)
    assert run.ok is ok
    data = json.loads(output.read_text())
    assert data["ran"] is ok
    assert data.get("error_count") == errors
    assert data["pcb_sha256"] == cmd._sha256_file(pcb)
