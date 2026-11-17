"""--drc-aware must not claim "no regressions" from the in-process referee alone (#8322)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.optimize_cmd import main
from kicad_tools.cli.runner import KiCadCLIResult
from tests.test_optimize_cmd import SIMPLE_PCB

TARGET = "kicad_tools.cli.optimize_cmd._kicad_cli_clearance_errors"
REFILL = "kicad_tools.cli.runner.run_refill_zones"


def _ok(n):
    return {"available": True, "clearance_errors": n, "reason": None}


_NA = {"available": False, "clearance_errors": None, "reason": "kicad-cli not found"}


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    p = tmp_path / "in.kicad_pcb"
    p.write_text(SIMPLE_PCB)
    return p


def _run(pcb, tmp_path, results, capsys, fmt="text"):
    args = [
        str(pcb),
        "-o",
        str(tmp_path / "o.kicad_pcb"),
        "--drc-aware",
        "--mfr",
        "jlcpcb",
        "--allow-default-rules",
    ]
    if fmt == "json":
        args += ["--format", "json"]
    with patch(TARGET, side_effect=results), patch(REFILL, return_value=KiCadCLIResult(True)):
        code = main(args)
    return code, capsys.readouterr().out


def test_kicad_cli_regression_fails_and_is_reported(pcb, tmp_path, capsys):
    code, out = _run(pcb, tmp_path, [_ok(1), _ok(17)], capsys)
    assert code == 2
    assert "1 ->     17" in out
    assert "REGRESSION" in out
    assert "(no regressions" not in out


def test_kicad_cli_regression_in_json(pcb, tmp_path, capsys):
    code, out = _run(pcb, tmp_path, [_ok(1), _ok(17)], capsys, fmt="json")
    assert code == 2
    d = json.loads(out)["kicad_cli_drc"]
    assert (d["clearance_errors_before"], d["clearance_errors_after"]) == (1, 17)
    assert d["regression"] is True


def test_no_kicad_cli_regression_keeps_exit_zero(pcb, tmp_path, capsys):
    code, out = _run(pcb, tmp_path, [_ok(3), _ok(3)], capsys)
    assert code == 0
    assert "3 ->      3" in out


def test_unavailable_with_dru_does_not_claim_no_regressions(pcb, tmp_path, capsys):
    pcb.with_suffix(".kicad_dru").write_text("(version 1)\n")
    code, out = _run(pcb, tmp_path, [_NA, _NA], capsys)
    assert code == 0
    assert "does not evaluate project .kicad_dru rules" in out
    assert "(no regressions" not in out
    assert "cross-check unavailable" in out


def test_dry_run_skips_kicad_cli(pcb, capsys):
    with patch(TARGET) as m:
        assert main([str(pcb), "--dry-run", "--drc-aware", "--mfr", "jlcpcb"]) == 0
    m.assert_not_called()


def test_output_elsewhere_stages_input_rules_for_after_run(pcb, tmp_path, capsys):
    out_dir = tmp_path / "elsewhere"
    out_dir.mkdir()
    args = [str(pcb), "-o", str(out_dir / "o.kicad_pcb"), "--drc-aware", "--mfr", "jlcpcb"]
    args.append("--allow-default-rules")
    with (
        patch(TARGET, side_effect=[_ok(1), _ok(1)]) as m,
        patch(REFILL, return_value=KiCadCLIResult(True)),
    ):
        assert main(args) == 0
    capsys.readouterr()
    assert m.call_args_list[0].kwargs.get("rules_from") is None
    assert m.call_args_list[1].kwargs["rules_from"] == pcb


def test_clearance_helper_stages_rules_beside_copy(tmp_path):
    from kicad_tools.cli.optimize_cmd import _kicad_cli_clearance_errors

    src = tmp_path / "in.kicad_pcb"
    src.write_text("x")
    src.with_suffix(".kicad_dru").write_text("(version 1)\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "o.kicad_pcb"
    out.write_text("y")
    seen = {}

    def fake_run_drc(target, output_path, **kw):
        seen["target"] = Path(target)
        seen["dru"] = Path(target).with_suffix(".kicad_dru").exists()
        output_path.write_text(
            json.dumps(
                {
                    "violations": [
                        {"type": "clearance", "severity": "error"},
                        {"type": "copper_edge_clearance", "severity": "error"},
                        {"type": "clearance", "severity": "warning"},
                    ]
                }
            )
        )
        return KiCadCLIResult(True)

    with patch("kicad_tools.cli.runner.run_drc", side_effect=fake_run_drc):
        res = _kicad_cli_clearance_errors(out, rules_from=src)
    assert res["clearance_errors"] == 2
    assert seen["dru"] is True
    assert seen["target"].parent != out_dir
