"""``kct erc`` / ``kct sch validate`` keep every finding kicad-cli reports on
two-sheet designs (Issue #7161).

``kct erc`` used to drop ``isolated_pin_label`` / ``single_global_label``
findings whose label *name* appeared on two or more sheets, without checking
whether the net reached a second pin, so a design kicad-cli 10.0.6 FAILS with
3 errors printed ``ERC PASSED`` and exited 0.  A sibling filter keyed
``power_pin_not_driven`` on the pin name and never matched KiCad 10's item
format.  Both were removed; KiCad 10 already aggregates global labels and
power drivers across the hierarchy.

Fixtures (``tests/fixtures/erc_two_sheet/``) are real two-sheet designs, each
with ``native.json`` -- the report kicad-cli 10.0.6 wrote for it (pinned
config, ``--severity-all``):

* ``isolated_label`` -- root R1.1 -> global label ``SIG``; the sub sheet holds
  two ``SIG`` labels joined by a wire and no pin.  Net ``SIG`` has one pin.
  ``.kicad_pro`` raises ``isolated_pin_label`` to error: 3 errors.
* ``connected_label`` -- control: the sub-sheet ``SIG`` reaches R2.1.  Clean.
* ``power_undriven`` -- root ``power:VCC`` on U1's ``power_in`` pin, sub-sheet
  ``power:VCC`` on R2.1, no driver anywhere: 1 ``power_pin_not_driven``.
* ``power_driver_on_subsheet`` -- same, plus a ``PWR_FLAG`` on the sub sheet
  only.  kicad-cli reports nothing: it sees the driver across sheets.

The report-backed tests run everywhere; the ``live`` ones re-run kicad-cli
and are skipped unless a KiCad 10 kicad-cli is installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli import erc_cmd
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.erc.report import ERCReport

FIXTURES = Path(__file__).parent / "fixtures" / "erc_two_sheet"
LABEL_TYPES = {"isolated_pin_label", "single_global_label"}


def _sch(name: str) -> Path:
    return FIXTURES / name / "h.kicad_sch"


def _native(name: str) -> Path:
    return FIXTURES / name / "native.json"


def _run_kct_erc_with_native(name: str, extra: list[str], monkeypatch) -> int:
    """``kct erc <fixture>.kicad_sch`` with kicad-cli replaced by its recorded report."""
    monkeypatch.setattr(
        erc_cmd,
        "run_erc_on_schematic",
        lambda *_a, **_k: ERCReport.load(_native(name)),
    )
    return erc_cmd.main([str(_sch(name)), *extra])


# ---------------------------------------------------------------------------
# Report-backed: the recorded kicad-cli 10.0.6 output reaches the verdict intact
# ---------------------------------------------------------------------------


def test_native_report_has_three_isolated_label_errors():
    """Guard the fixture itself: kicad-cli reported exactly 3 errors."""
    report = ERCReport.load(_native("isolated_label"))
    assert [v.type_str for v in report.errors] == ["isolated_pin_label"] * 3
    assert all("Global Label 'SIG'" in " ".join(v.items) for v in report.errors)


def test_kct_erc_schematic_fails_on_single_pin_global_label(monkeypatch, capsys):
    rc = _run_kct_erc_with_native("isolated_label", [], monkeypatch)
    out = capsys.readouterr().out

    assert rc == 1
    assert "Errors:     3" in out
    assert "ERC PASSED" not in out
    assert "Filtered:" not in out


def test_kct_erc_schematic_json_lists_the_three_violations(monkeypatch, capsys):
    rc = _run_kct_erc_with_native("isolated_label", ["--format", "json"], monkeypatch)
    data = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert data["summary"]["errors"] == 3
    assert "filtered" not in data["summary"]
    assert [v["type"] for v in data["violations"]] == ["isolated_pin_label"] * 3


def test_kct_erc_schematic_and_report_agree(monkeypatch, capsys):
    """Running on the schematic gives the verdict of parsing kicad-cli's own report."""
    rc_report = erc_cmd.main([str(_native("isolated_label")), "--format", "json"])
    from_report = json.loads(capsys.readouterr().out)
    rc_sch = _run_kct_erc_with_native("isolated_label", ["--format", "json"], monkeypatch)
    from_sch = json.loads(capsys.readouterr().out)

    assert rc_report == rc_sch == 1
    assert from_report["summary"] == from_sch["summary"]


def test_kct_erc_connected_two_sheet_label_is_clean(monkeypatch, capsys):
    rc = _run_kct_erc_with_native("connected_label", ["--format", "json"], monkeypatch)
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert not {v["type"] for v in data["violations"]} & LABEL_TYPES


def test_kct_erc_keeps_undriven_power_pin(monkeypatch, capsys):
    rc = _run_kct_erc_with_native("power_undriven", ["--format", "json"], monkeypatch)
    data = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert [v["type"] for v in data["violations"]] == ["power_pin_not_driven"]
    # KiCad 10's item format -- not the ``Pin VCC (power_in) of U1`` the
    # removed filter parsed.
    assert data["violations"][0]["items"] == ["Symbol #PWR01 Pin 1 [Power input, Line]"]


def test_kct_erc_power_driver_on_other_sheet_is_clean(monkeypatch, capsys):
    rc = _run_kct_erc_with_native("power_driver_on_subsheet", ["--format", "json"], monkeypatch)
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert data["violations"] == []


def test_sch_validate_erc_leg_keeps_isolated_label_errors(tmp_path):
    """``kct sch validate``'s ERC leg no longer drops them either."""
    from kicad_tools.cli.sch_validate import run_erc

    report = tmp_path / "erc.json"
    shutil.copy(_native("isolated_label"), report)

    class _FakeTmp:
        name = str(report)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    with (
        patch("kicad_tools.cli.sch_validate.find_kicad_cli", return_value="/usr/bin/kicad-cli"),
        patch("kicad_tools.cli.sch_validate.subprocess.run") as mock_run,
        patch("tempfile.NamedTemporaryFile", return_value=_FakeTmp()),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        issues = run_erc(str(_sch("isolated_label")))

    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 3
    assert all("SIG" in i.message for i in errors)


# ---------------------------------------------------------------------------
# Live: re-run kicad-cli so a KiCad behaviour change trips these
# ---------------------------------------------------------------------------


def _kicad10_cli() -> str | None:
    cli = find_kicad_cli()
    if cli is None:
        return None
    try:
        version = subprocess.run(
            [str(cli), "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return str(cli) if version.startswith("10.") else None


live = pytest.mark.skipif(_kicad10_cli() is None, reason="KiCad 10 kicad-cli not installed")


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest / "h.kicad_sch"


@live
def test_live_kct_erc_matches_kicad_cli_on_isolated_label(tmp_path, capsys):
    rc = erc_cmd.main([str(_copy_fixture("isolated_label", tmp_path)), "--format", "json"])
    data = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert data["summary"]["errors"] == 3
    assert [v["type"] for v in data["violations"] if v["severity"] == "error"] == [
        "isolated_pin_label"
    ] * 3


@live
def test_live_connected_label_has_no_label_findings(tmp_path, capsys):
    rc = erc_cmd.main([str(_copy_fixture("connected_label", tmp_path)), "--format", "json"])
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert not {v["type"] for v in data["violations"]} & LABEL_TYPES


@live
@pytest.mark.parametrize(
    ("name", "expected"),
    [("power_undriven", ["power_pin_not_driven"]), ("power_driver_on_subsheet", [])],
)
def test_live_kicad_aggregates_power_drivers_across_sheets(tmp_path, capsys, name, expected):
    """The premise of the removed power-pin filter does not hold on KiCad 10."""
    erc_cmd.main([str(_copy_fixture(name, tmp_path)), "--format", "json"])
    data = json.loads(capsys.readouterr().out)

    assert [v["type"] for v in data["violations"]] == expected
