"""Content-derived controls for independent saved-copper verification."""

import json
from dataclasses import replace

import pytest

from kicad_tools.cli import readiness_cmd as cmd


def options(tmp_path):
    source = tmp_path / "output"
    source.mkdir()
    pcb = source / "demo.kicad_pcb"
    pcb.write_text(json.dumps({"F.Cu": 1.0}))
    project = source / "demo.kicad_pro"
    project.write_text('{"rule": "must survive"}')
    (source / "demo.kicad_dru").write_text("(version 1)")
    return cmd.ReadinessOptions(
        tmp_path,
        pcb,
        None,
        project,
        "jlcpcb",
        "pcb_only",
        source / "manufacturing",
        source / "readiness",
    )


@pytest.mark.parametrize(
    "filled,expected",
    [
        ({"F.Cu": 2.0}, cmd.FAILED),
        ({"F.Cu": 1.0}, cmd.PASSED),
        ({"F.Cu": 1.005}, cmd.PASSED),
        ({"F.Cu": 1.01}, cmd.PASSED),
        ({"F.Cu": 1.0100001}, cmd.FAILED),
        ({"B.Cu": 1.0}, cmd.FAILED),
    ],
)
def test_independent_refill_preserves_saved_source_and_project_context(tmp_path, filled, expected):
    opts = options(tmp_path)
    before = opts.pcb.read_bytes()
    seen = []

    def refill(pcb):
        assert pcb != opts.pcb
        assert pcb.read_bytes() == before
        assert pcb.with_suffix(".kicad_pro").read_bytes() == opts.project.read_bytes()
        assert pcb.with_suffix(".kicad_dru").read_text() == "(version 1)"
        seen.append(pcb)
        pcb.write_text(json.dumps(filled))
        return cmd.EngineRun(True)

    engines = replace(
        cmd.Engines(),
        refill=refill,
        fill_areas=lambda pcb: json.loads(pcb.read_text()),
        kicad_cli_version=lambda: "test engine",
    )
    result = cmd._gate_refill(opts, engines)
    assert result.status == expected
    assert len(seen) == 1
    assert opts.pcb.read_bytes() == before
    evidence = json.loads((opts.evidence_dir / "fill-consistency.json").read_text())
    assert evidence["saved_areas_mm2"] == {"F.Cu": 1.0}
    assert evidence["refilled_areas_mm2"] == filled
    assert evidence["engine"] == "test engine"
    assert evidence["saved_sha256"] == cmd._sha256_file(opts.pcb)
    assert (opts.evidence_dir / "fill-saved" / opts.pcb.name).read_bytes() == before


@pytest.mark.parametrize("bad", [{"F.Cu": float("nan")}, {"F.Cu": -1}, [], {"F.Cu": "x"}])
def test_malformed_fill_measurements_cannot_pass(tmp_path, bad):
    opts = options(tmp_path)
    engines = replace(
        cmd.Engines(),
        refill=lambda pcb: cmd.EngineRun(True),
        fill_areas=lambda pcb: bad,
        kicad_cli_version=lambda: "test",
    )
    assert cmd._gate_refill(opts, engines).status == cmd.FAILED


def test_failed_refill_cannot_modify_source(tmp_path):
    opts = options(tmp_path)
    before = opts.pcb.read_bytes()

    def fail(pcb):
        pcb.write_text("corrupt")
        return cmd.EngineRun(False, "refill failed", 2)

    engines = replace(
        cmd.Engines(),
        refill=fail,
        fill_areas=lambda pcb: json.loads(pcb.read_text()),
        kicad_cli_version=lambda: "test",
    )
    assert cmd._gate_refill(opts, engines).status == cmd.NOT_RUN
    assert opts.pcb.read_bytes() == before
