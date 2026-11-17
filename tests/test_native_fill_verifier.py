"""Exercise native measurement failures without requiring Docker."""

import contextlib
import hashlib
import io
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def measure(tmp_path, second_report, returncode=0, mutate=False):
    script = (
        Path(__file__).resolve().parents[1] / "scripts/check_fill_fragment_bonding_vs_native.py"
    )
    module = runpy.run_path(str(script))
    for name in ("a", "b"):
        (tmp_path / f"{name}.kicad_pcb").write_text(name)
    reports = []

    def native(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        reports.append(output)
        if len(reports) == 1:
            output.write_text('{"unconnected_items": []}')
            return SimpleNamespace(returncode=0, stderr=b"")
        if second_report is not None:
            output.write_text(second_report)
        if mutate:
            Path(command[-1]).write_text("changed input")
        return SimpleNamespace(returncode=returncode, stderr=b"native diagnostic")

    code = module["VERIFY_IN_CONTAINER"].replace('"/w"', repr(str(tmp_path)))
    stdout = io.StringIO()
    with patch("subprocess.run", side_effect=native), contextlib.redirect_stdout(stdout):
        exec(code, {})
    return json.loads(stdout.getvalue()), reports


@pytest.mark.parametrize(
    ("report", "returncode"),
    [
        (None, 1),
        (None, 0),
        ('{"unconnected_items": []}', 1),
        ("{}", 0),
        ('{"unconnected_items": null}', 0),
        ('{"unconnected_items": {}}', 0),
        ("[]", 0),
        ("invalid json", 0),
    ],
)
def test_failed_second_measurement_never_inherits_success(tmp_path, report, returncode):
    result, paths = measure(tmp_path, report, returncode)
    assert result["a"]["unconnected"] == 0
    assert result["a"]["hash_stable"] is True
    assert "error" in result["b"]
    assert "unconnected" not in result["b"]
    assert paths[0] != paths[1]


def test_successful_measurement_retains_count_and_input_identity(tmp_path):
    result, _ = measure(tmp_path, '{"unconnected_items": [{"description": "open"}]}')
    assert result["b"] == {
        "unconnected": 1,
        "sha256": hashlib.sha256(b"b").hexdigest(),
        "hash_stable": True,
    }


def test_input_mutation_is_not_reported_stable(tmp_path):
    result, _ = measure(tmp_path, '{"unconnected_items": []}', mutate=True)
    assert result["b"]["hash_stable"] is False
