"""One sign-off exit gate (Issue #7230, Part B).

Three layers:

1. **Snapshot**: :func:`gate_exit_code` reproduces, state by state, the
   hand-rolled ladders the commands used before Part B (copied verbatim below
   as ``_old_*``).  Part B is a pure refactor: no exit code changes.
2. **Per command**: ``--format json`` carries top-level ``verdict`` and
   ``exit_code`` equal to the process exit code, in every state.
3. **Docs**: the ``docs/reference/cli.md`` "Sign-off exit codes" table matches
   :data:`EXIT_LADDERS`.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli._gate import EXIT_LADDERS, gate_exit_code

REPO = Path(__file__).resolve().parent.parent
DRC_FIXTURE = REPO / "tests" / "fixtures" / "drc" / "view_filter_error_and_warning_7230.json"
ERC_FIXTURE = REPO / "tests" / "fixtures" / "erc" / "kicad10_floating_wire_7170.json"

# ---------------------------------------------------------------------------
# 1. Snapshot of the pre-Part-B ladders
# ---------------------------------------------------------------------------


def _old_drc(errors, warnings, incomplete, strict, allow):
    # drc_cmd._drc_exit_code before #7230 Part B (plain and --mfr).
    if errors > 0:
        return 1
    if warnings > 0 and strict:
        return 2
    if incomplete and not allow:
        return 2
    return 0


def _old_erc_or_sync(errors, warnings, strict):
    # erc_cmd.main / validate_sync_cmd.main before Part B.
    if errors > 0:
        return 1
    elif warnings > 0 and strict:
        return 2
    return 0


def _old_sch(errors, warnings, incomplete, strict):
    # sch_validate.main before Part B (sys.exit codes; None -> 0).
    if errors > 0:
        return 1
    if incomplete:
        return 1
    if strict and warnings > 0:
        return 1
    return 0


def _old_check_drc_only(errors, warnings, strict):
    if errors > 0 or (warnings > 0 and strict):
        return 2
    return 0


def _old_check_meta(overall, allow):
    if overall == "FAILED":
        return 2
    if overall == "INCOMPLETE":
        return 0 if allow else 2
    return 0


def _old_audit(verdict, strict):
    if verdict == "not_ready" or (verdict == "warning" and strict):
        return 2
    return 0


STATES = list(itertools.product([0, 1, 3], [0, 1, 2], [False, True], [False, True], [False, True]))


def _expected_verdict(errors, warnings, incomplete, strict):
    if errors or (warnings and strict):
        return "fail"
    return "incomplete" if incomplete else "pass"


@pytest.mark.parametrize(("errors", "warnings", "incomplete", "strict", "allow"), STATES)
def test_drc_ladder_unchanged(errors, warnings, incomplete, strict, allow):
    code, verdict = gate_exit_code(
        errors,
        warnings,
        incomplete=incomplete,
        strict=strict,
        allow_incomplete=allow,
        style="drc",
    )
    assert code == _old_drc(errors, warnings, incomplete, strict, allow)
    assert verdict == _expected_verdict(errors, warnings, incomplete, strict)


@pytest.mark.parametrize(
    ("errors", "warnings", "strict"), list(itertools.product([0, 2], [0, 2], [False, True]))
)
def test_erc_and_sync_ladder_unchanged(errors, warnings, strict):
    assert gate_exit_code(errors, warnings, strict=strict, style="drc")[0] == _old_erc_or_sync(
        errors, warnings, strict
    )


@pytest.mark.parametrize(("errors", "warnings", "incomplete", "strict", "_allow"), STATES)
def test_sch_ladder_unchanged(errors, warnings, incomplete, strict, _allow):
    code, verdict = gate_exit_code(
        errors, warnings, incomplete=incomplete, strict=strict, style="sch"
    )
    assert code == _old_sch(errors, warnings, incomplete, strict)
    assert verdict == _expected_verdict(errors, warnings, incomplete, strict)


@pytest.mark.parametrize(
    ("errors", "warnings", "strict"), list(itertools.product([0, 2], [0, 2], [False, True]))
)
def test_check_drc_only_ladder_unchanged(errors, warnings, strict):
    from kicad_tools.cli.check_cmd import _check_verdict

    verdict, code = _check_verdict(
        mask_copper_failed=False,
        strict_copper_disagreement=False,
        drc_only=True,
        error_count=errors,
        warning_count=warnings,
        strict=strict,
        allow_incomplete=False,
        meta=None,
    )
    assert code == _old_check_drc_only(errors, warnings, strict)
    assert verdict == ("fail" if code else "pass")


@pytest.mark.parametrize(
    ("overall", "allow", "strict"),
    list(itertools.product(["PASSED", "FAILED", "INCOMPLETE"], [False, True], [False, True])),
)
def test_check_meta_ladder_unchanged(overall, allow, strict):
    from kicad_tools.cli.check_cmd import _check_verdict

    verdict, code = _check_verdict(
        mask_copper_failed=False,
        strict_copper_disagreement=False,
        drc_only=False,
        error_count=0,
        warning_count=1,
        strict=strict,
        allow_incomplete=allow,
        meta=SimpleNamespace(overall=overall),
    )
    assert code == _old_check_meta(overall, allow)
    assert verdict == {"PASSED": "pass", "FAILED": "fail", "INCOMPLETE": "incomplete"}[overall]


@pytest.mark.parametrize("which", ["mask", "copper"])
def test_check_mask_and_copper_failures_still_exit_2(which):
    from kicad_tools.cli.check_cmd import _check_verdict

    assert _check_verdict(
        mask_copper_failed=which == "mask",
        strict_copper_disagreement=which == "copper",
        drc_only=False,
        error_count=0,
        warning_count=0,
        strict=False,
        allow_incomplete=True,
        meta=SimpleNamespace(overall="PASSED"),
    ) == ("fail", 2)


# ---------------------------------------------------------------------------
# 2. Per command: JSON verdict/exit_code == process exit code
# ---------------------------------------------------------------------------


def _drc_report(tmp_path, keep):
    data = json.loads(DRC_FIXTURE.read_text())
    data["violations"] = [v for v in data["violations"] if v["severity"] in keep]
    path = tmp_path / "drc.json"
    path.write_text(json.dumps(data))
    return path


# (severities kept, extra flags, expected (exit, verdict)).  A .json report
# never proves schematic parity ran, so without --allow-incomplete it is the
# incomplete state.
DRC_CASES = [
    # --allow-incomplete changes the exit code, never the honest verdict.
    ((), ["--allow-incomplete"], (0, "incomplete")),
    ((), [], (2, "incomplete")),
    (("warning",), ["--allow-incomplete"], (0, "incomplete")),
    (("warning",), ["--allow-incomplete", "--strict"], (2, "fail")),
    (("warning",), ["--strict"], (2, "fail")),
    (("error", "warning"), ["--allow-incomplete"], (1, "fail")),
    (("error", "warning"), ["--allow-incomplete", "--net", "VCC"], (1, "fail")),
    (("error", "warning"), ["--errors-only", "--type", "silk"], (1, "fail")),
]


@pytest.mark.parametrize(("keep", "extra", "expected"), DRC_CASES)
def test_drc_json_carries_exit_code(tmp_path, capsys, keep, extra, expected):
    from kicad_tools.cli import drc_cmd

    rc = drc_cmd.main([str(_drc_report(tmp_path, keep)), "--format", "json", *extra])
    data = json.loads(capsys.readouterr().out)
    assert (rc, data["verdict"]) == expected
    assert data["exit_code"] == rc


@pytest.mark.parametrize(("keep", "extra", "expected"), DRC_CASES[:6])
def test_drc_mfr_json_carries_exit_code(tmp_path, capsys, keep, extra, expected):
    from kicad_tools.cli import drc_cmd

    rc = drc_cmd.main(
        [str(_drc_report(tmp_path, keep)), "--mfr", "jlcpcb", "--format", "json", *extra]
    )
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == rc
    code, verdict = expected
    if "error" in keep:
        # The 0.05 mm clearance breaches every fab's limit.
        assert (rc, data["verdict"]) == (1, "fail")
    else:
        assert (rc, data["verdict"]) == (code, verdict)


def _erc_report(tmp_path, keep):
    data = json.loads(ERC_FIXTURE.read_text())
    for sheet in data["sheets"]:
        sheet["violations"] = [v for v in sheet["violations"] if v["severity"] in keep]
    path = tmp_path / "erc.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    ("keep", "extra", "expected"),
    [
        ((), [], (0, "pass")),
        (("warning",), [], (0, "pass")),
        (("warning",), ["--strict"], (2, "fail")),
        (("error", "warning"), [], (1, "fail")),
        (("error", "warning"), ["--errors-only", "--type", "nothing-matches"], (1, "fail")),
    ],
)
def test_erc_json_carries_exit_code(tmp_path, capsys, keep, extra, expected):
    from kicad_tools.cli.erc_cmd import main

    rc = main([str(_erc_report(tmp_path, keep)), "--format", "json", *extra])
    data = json.loads(capsys.readouterr().out)
    assert (rc, data["verdict"]) == expected
    assert data["exit_code"] == rc


@pytest.mark.parametrize(
    ("severities", "extra", "expected"),
    [
        ((), [], (0, "pass")),
        (("warning",), [], (0, "pass")),
        (("warning",), ["--strict"], (2, "fail")),
        (("warning",), ["--strict", "--errors-only"], (2, "fail")),
        (("error", "warning"), ["--errors-only"], (1, "fail")),
    ],
)
def test_validate_sync_json_carries_exit_code(
    monkeypatch, tmp_path, capsys, severities, extra, expected
):
    from kicad_tools.cli import validate_sync_cmd
    from kicad_tools.validate.netlist import SyncIssue, SyncResult

    issues = [
        SyncIssue(sev, "net_mismatch", "m", "s", reference=f"R{i}")
        for i, sev in enumerate(severities)
    ]

    class _Validator:
        def __init__(self, *a, **k):
            pass

        def validate(self):
            return SyncResult(issues=issues)

    monkeypatch.setattr(validate_sync_cmd, "NetlistValidator", _Validator)
    sch = tmp_path / "a.kicad_sch"
    pcb = tmp_path / "a.kicad_pcb"
    sch.write_text("")
    pcb.write_text("")
    rc = validate_sync_cmd.main(["-s", str(sch), "-p", str(pcb), "--format", "json", *extra])
    data = json.loads(capsys.readouterr().out)
    assert (rc, data["verdict"]) == expected
    assert data["exit_code"] == rc


@pytest.mark.parametrize(
    ("severities", "not_run", "extra", "expected"),
    [
        ((), False, [], (0, "pass")),
        (("warning",), False, [], (0, "pass")),
        (("warning",), False, ["--strict"], (1, "fail")),
        ((), True, [], (1, "incomplete")),
        (("error",), True, [], (1, "fail")),
    ],
)
def test_sch_validate_json_carries_exit_code(
    monkeypatch, tmp_path, capsys, severities, not_run, extra, expected
):
    from kicad_tools.cli import sch_validate

    sch = tmp_path / "a.kicad_sch"
    sch.write_text("")
    result = sch_validate.ValidationResult(
        schematic=str(sch),
        issues=[sch_validate.ValidationIssue(sev, "erc", "m") for sev in severities],
        checks_run=["erc"],
        checks_not_run=[{"check": "pins", "reason": "test"}] if not_run else [],
    )
    monkeypatch.setattr(sch_validate, "validate_schematic", lambda *a, **k: result)
    try:
        sch_validate.main([str(sch), "--format", "json", *extra])
        rc = 0
    except SystemExit as exc:
        rc = exc.code
    data = json.loads(capsys.readouterr().out)
    assert (rc, data["verdict"]) == expected
    assert data["exit_code"] == rc


def test_check_json_carries_exit_code(drc_clean_pcb, capsys):
    from kicad_tools.cli.check_cmd import main

    for extra, expected in [
        ([], (2, "incomplete")),
        (["--allow-incomplete"], (0, "incomplete")),
        (["--drc-only"], (0, "pass")),
    ]:
        rc = main([str(drc_clean_pcb), "--format", "json", *extra])
        data = json.loads(capsys.readouterr().out)
        assert (rc, data["verdict"]) == expected
        assert data["exit_code"] == rc


@pytest.mark.parametrize(
    ("audit_verdict", "extra", "expected"),
    [
        ("ready", [], (0, "pass")),
        ("warning", [], (0, "pass")),
        ("warning", ["--strict"], (2, "fail")),
        ("not_ready", [], (2, "fail")),
    ],
)
def test_audit_json_carries_exit_code(
    monkeypatch, tmp_path, capsys, audit_verdict, extra, expected
):
    from kicad_tools.audit import AuditVerdict
    from kicad_tools.cli import audit_cmd

    verdict = AuditVerdict(audit_verdict)
    stub = SimpleNamespace(verdict=verdict, to_dict=lambda: {"verdict": verdict.value})
    monkeypatch.setattr(
        audit_cmd, "ManufacturingAudit", lambda *a, **k: SimpleNamespace(run=lambda: stub)
    )
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("")
    rc = audit_cmd.main([str(pcb), "--skip-erc", "--format", "json", *extra])
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == audit_verdict  # the audit's own verdict, unchanged
    assert (rc, data["gate_verdict"]) == expected
    assert data["exit_code"] == rc


# ---------------------------------------------------------------------------
# 3. The cli.md table matches EXIT_LADDERS
# ---------------------------------------------------------------------------


def _signoff_table() -> dict[str, list[str]]:
    text = (REPO / "docs" / "reference" / "cli.md").read_text()
    section = text.split("### Sign-off exit codes", 1)[1].split("\n### ", 1)[0]
    rows: dict[str, list[str]] = {}
    for line in section.splitlines():
        if not line.startswith("| `kct "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return rows


def _lead_int(cell: str) -> int | None:
    m = re.match(r"(\d+)", cell)
    return int(m.group(1)) if m else None


# Commands whose gate has an incomplete state (the "Check not run" column).
_HAS_INCOMPLETE = {"`kct drc` (plain and `--mfr`)", "`kct sch validate`"}


def test_cli_md_signoff_table_matches_ladders():
    rows = _signoff_table()
    on_gate = {cmd: cells for cmd, cells in rows.items() if cells[0].strip("`") in EXIT_LADDERS}
    assert len(on_gate) >= 6, rows
    for cmd, (ladder_cell, errors, strict, not_run, _tool) in on_gate.items():
        ladder = EXIT_LADDERS[ladder_cell.strip("`")]
        assert _lead_int(errors) == ladder.errors, cmd
        assert _lead_int(strict) == ladder.strict_warnings, cmd
        if cmd in _HAS_INCOMPLETE or cmd.startswith("`kct check`"):
            assert _lead_int(not_run) == ladder.incomplete, cmd
        else:
            assert not_run.startswith("—"), cmd
