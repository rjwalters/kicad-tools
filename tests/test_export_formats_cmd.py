"""Tests for ``kct export-formats`` (Issue #8017)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import export_formats_cmd
from kicad_tools.export import kicad_cli_exports as mod

FAKE_CLI = Path("/usr/bin/kicad-cli")


@pytest.fixture
def pcb(tmp_path):
    p = tmp_path / "my board" / "b.kicad_pcb"
    p.parent.mkdir()
    p.write_text("(kicad_pcb)")
    return p


def _runner(behavior):
    """subprocess.run fake; behavior(fmt, out) -> (rc, stderr), may write the file."""

    def fake(cmd, **kw):
        if cmd[-1] == "version" or "version" in cmd[1:2]:
            return subprocess.CompletedProcess(cmd, 0, "10.0.6\n", "")
        fmt = cmd[cmd.index("export") + 1]
        out = Path(cmd[cmd.index("--output") + 1])
        rc, err = behavior(fmt, out)
        return subprocess.CompletedProcess(cmd, rc, "", err)

    return fake


def _write(fmt, out):
    out.write_bytes(b"data")
    return 0, ""


def test_parse_formats():
    assert mod.parse_formats("pdf, STEP,pdf") == ["pdf", "step"]
    with pytest.raises(ValueError):
        mod.parse_formats("dxf")


def test_all_ok(pcb, tmp_path):
    with patch.object(mod.subprocess, "run", _runner(_write)):
        res = mod.export_formats(
            pcb, tmp_path / "o" / "new", list(mod.FORMAT_SUFFIX), kicad_cli=FAKE_CLI
        )
    assert [r.status for r in res] == ["ok"] * 5
    assert all(r.size_bytes == 4 for r in res)


def test_missing_cli(pcb, tmp_path):
    with (
        patch.object(mod, "find_kicad_cli", return_value=None),
        patch.object(mod, "docker_fallback_enabled", return_value=False),
    ):
        res = mod.export_formats(pcb, tmp_path, ["pdf", "step"])
    assert [r.status for r in res] == ["failed", "failed"]
    assert "kicad-cli" in res[0].error


def test_nonzero_exit_exit0_empty_and_unsupported(pcb, tmp_path):
    def behavior(fmt, out):
        if fmt == "pdf":
            return 1, "boom"
        if fmt == "svg":
            return 0, ""  # exit 0, no file
        if fmt == "step":
            out.write_bytes(b"")  # exit 0, empty file
            return 0, ""
        if fmt == "odb":
            return 1, "Failed to parse 'odb', did you mean 'pos'"
        return _write(fmt, out)

    with patch.object(mod.subprocess, "run", _runner(behavior)):
        res = {
            r.format: r
            for r in mod.export_formats(pcb, tmp_path, list(mod.FORMAT_SUFFIX), kicad_cli=FAKE_CLI)
        }
    assert "boom" in res["pdf"].error
    assert "no/empty" in res["svg"].error
    assert "no/empty" in res["step"].error
    assert res["odb"].error.startswith("unsupported by kicad-cli 10.0.6")
    assert res["ipc2581"].status == "ok"


def test_stale_output_not_reported_ok(pcb, tmp_path):
    (tmp_path / "b.pdf").write_bytes(b"stale")
    with patch.object(mod.subprocess, "run", _runner(lambda f, o: (0, ""))):
        res = mod.export_formats(pcb, tmp_path, ["pdf"], kicad_cli=FAKE_CLI)
    assert res[0].status == "failed"


def test_cli_exit_codes_and_json(pcb, tmp_path, capsys):
    out = tmp_path / "out"

    def partial(fmt, o):
        return _write(fmt, o) if fmt == "pdf" else (1, "nope")

    with (
        patch.object(mod, "find_kicad_cli", return_value=FAKE_CLI),
        patch.object(mod.subprocess, "run", _runner(partial)),
    ):
        rc = export_formats_cmd.main([str(pcb), "--format", "pdf,step", "-o", str(out), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and data["success"] is False
    assert [f["status"] for f in data["formats"]] == ["ok", "failed"]
    assert set(data["formats"][0]) == {"format", "status", "path", "size_bytes", "error"}

    with (
        patch.object(mod, "find_kicad_cli", return_value=FAKE_CLI),
        patch.object(mod.subprocess, "run", _runner(_write)),
    ):
        assert export_formats_cmd.main([str(pcb), "--format", "pdf", "-o", str(out)]) == 0


def test_bad_inputs(pcb, tmp_path):
    assert export_formats_cmd.main([str(tmp_path / "nope.kicad_pcb")]) == 2
    assert export_formats_cmd.main([str(pcb), "--format", "dxf"]) == 2
