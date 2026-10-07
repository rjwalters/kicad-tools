"""``kct route --lint-gate`` / ``kct route-auto --lint-gate`` (Issue #6054).

The router itself is replaced by a stub that writes a chosen board to
``--output`` (a seeded clearance short, a warning-only dangling stub, or a
clean copy), so these tests exercise the real gate -- real ``kct check`` and
``kct detect-mistakes`` runs on a small fixture board, real waivers -- around
the single post-route hook every ``kct route`` path goes through.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli import route_cmd
from kicad_tools.cli.route_lint_gate import LINT_GATE_EXIT, merge_into_json_document

FIXTURE = Path(__file__).parent / "fixtures" / "projects"

# A GND trace crossing the NET1 trace: two clearance errors + a warning.
SHORT = (
    '\t(segment (start 110 48) (end 110 52) (width 0.25) (layer "F.Cu") (net 2) (uuid "seed-1"))\n'
)
# A GND stub off D1.2: warnings only (track_dangling, power trace width).
DANGLING = '\t(segment (start 140.8 50) (end 140.8 53) (width 0.25) (layer "F.Cu") (net 2) (uuid "seed-w"))\n'
# A NET1 trace on top of the existing one: no new error findings.
HARMLESS = "\t# routed\n"


def _seed(text: str, extra: str) -> str:
    if extra == HARMLESS:
        return text + "\n"
    i = text.rstrip().rfind(")")
    return text[:i] + extra + text[i:]


@pytest.fixture
def board(tmp_path: Path) -> Path:
    for name in ("test_project.kicad_pcb", "test_project.kicad_pro"):
        shutil.copy(FIXTURE / name, tmp_path / name)
    return tmp_path / "test_project.kicad_pcb"


def _stub_router(extra: str, calls: list | None = None, raise_after: bool = False):
    """A ``_run_main_impl`` that 'routes' by writing a seeded copy to --output."""

    def fake(args, parser, argv):
        if calls is not None:
            calls.append(args)
        src = Path(args.pcb)
        out = Path(args.output) if args.output else src.with_stem(src.stem + "_routed")
        out.write_text(_seed(src.read_text(), extra))
        if raise_after:
            raise RuntimeError("router crashed after writing")
        return 0

    return fake


def _route(board: Path, extra: str, *flags: str, calls=None, raise_after=False) -> int:
    with patch.object(route_cmd, "_run_main_impl", _stub_router(extra, calls, raise_after)):
        return kct_main(["route", str(board), "--no-current-paths", "--lint-gate", *flags])


def _routed(board: Path) -> Path:
    return board.with_stem(board.stem + "_routed")


def _rejected(board: Path) -> Path:
    return board.with_name(f"{board.stem}_routed.lint-rejected.kicad_pcb")


def test_seeded_short_is_rolled_back(board, capsys):
    rc = _route(board, SHORT)

    assert rc == LINT_GATE_EXIT == 3
    assert not _routed(board).exists(), "the rejected route must not ship at --output"
    assert _rejected(board).exists() and "seed-1" in _rejected(board).read_text()
    err = capsys.readouterr().err
    assert "ROLLED BACK" in err
    assert "clearance_segment_segment|Trace@F.Cu:w0.25:110/48~110/52" in err
    assert "--waive" in err


def test_clean_route_is_kept_and_exit_code_unchanged(board, capsys):
    rc = _route(board, HARMLESS)

    assert rc == 0
    assert _routed(board).exists()
    assert not _rejected(board).exists()
    assert "PASS" in capsys.readouterr().err


def test_without_flag_nothing_is_gated(board):
    with patch.object(route_cmd, "_run_main_impl", _stub_router(SHORT)):
        rc = kct_main(["route", str(board), "--no-current-paths"])
    assert rc == 0
    assert "seed-1" in _routed(board).read_text()


def test_waived_findings_pass_the_gate(board, capsys):
    """Fail on the seeded mistake, waive it on the rejected board, then pass."""
    assert _route(board, SHORT, "--format", "json") == 3
    doc = json.loads(capsys.readouterr().out)
    gate = doc["lint_gate"]
    assert gate["status"] == "rolled_back"
    assert doc["exit_code"] == 3
    keys = [r["key"] for r in gate["introduced"]]
    errors = [r["key"] for r in gate["introduced"] if r["severity"] == "error"]
    assert len(errors) == 2

    # Waive every introduced finding on the rejected board, into the INPUT
    # board's sidecar (it does not exist yet: --waive creates it).
    waivers = board.with_name("test_project.kct-waivers.json")
    rejected = gate["rejected_board"]
    check_keys = [k for k in keys if not k.startswith("mistake.")]
    argv = ["check", rejected, "--no-net-class-map", "--no-current-paths"]
    argv += ["--waivers", str(waivers)]
    for key in check_keys:
        argv += ["--waive", key]
    argv += ["--waive-reason", "reviewed", "--waive-reviewer", "pytest"]
    assert kct_main(argv) in (0, 2)
    assert waivers.exists()
    capsys.readouterr()

    rc = _route(board, SHORT)
    assert rc == 0, capsys.readouterr().err
    assert "seed-1" in _routed(board).read_text()
    assert not _rejected(board).exists(), "a passing run discards the stale rejected board"


def test_mistake_findings_are_gated_and_waivable(board, capsys):
    """``detect-mistakes`` findings ride the same gate (strict: warnings block)."""
    assert _route(board, DANGLING, "--lint-gate-strict") == 3
    err = capsys.readouterr().err
    assert "mistake.power_trace_width" in err
    # Without strict, warnings alone do not roll back.
    assert _route(board, DANGLING) == 0
    _routed(board).unlink()

    waivers = board.with_name("test_project.kct-waivers.json")
    rejected = str(_rejected(board))
    assert _route(board, DANGLING, "--lint-gate-strict") == 3
    assert (
        kct_main(
            [
                "detect-mistakes",
                rejected,
                "--waivers",
                str(waivers),
                "--waive",
                "mistake.power_trace_width||GND|",
                "--waive-reason",
                "reviewed",
                "--waive-reviewer",
                "pytest",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert kct_main(
        [
            "check",
            rejected,
            "--no-net-class-map",
            "--no-current-paths",
            "--waivers",
            str(waivers),
            "--waive",
            "track_dangling|net:GND,seed-w|GND|F.Cu",
            "--waive-reason",
            "reviewed",
            "--waive-reviewer",
            "pytest",
        ]
    ) in (0, 2)
    capsys.readouterr()
    assert _route(board, DANGLING, "--lint-gate-strict") == 0, capsys.readouterr().err


def test_previous_output_is_restored_byte_for_byte(board):
    out = board.with_name("existing_out.kicad_pcb")
    out.write_text("previous result\n")
    rc = _route(board, SHORT, "-o", str(out))
    assert rc == 3
    assert out.read_text() == "previous result\n"


def test_in_place_route_restores_the_input(board):
    before = board.read_bytes()
    rc = _route(board, SHORT, "-o", str(board))
    assert rc == 3
    assert board.read_bytes() == before


def test_unlintable_baseline_aborts_before_routing(board, capsys):
    calls: list = []
    rc = _route(
        board, SHORT, "--lint-gate-waivers", str(board.with_name("missing.json")), calls=calls
    )
    assert rc == 1
    assert calls == [], "routing must not start when the baseline cannot be linted"
    assert "nothing was routed" in capsys.readouterr().err


def test_router_crash_never_leaves_an_unjudged_board(board, capsys):
    # The CLI's top-level handler turns the crash into a non-zero exit.
    assert _route(board, SHORT, raise_after=True) != 0
    assert not _routed(board).exists()
    # The unjudged board (e.g. a Ctrl+C best-attempt save) is kept aside.
    assert _rejected(board).exists() and "seed-1" in _rejected(board).read_text()
    assert "routing aborted" in capsys.readouterr().err


# -- linter exit codes are not success signals (judge review of #6084) ------


def _mistakes_with(extra: list[dict], *, only_candidate: bool = False):
    """Wrap the real ``detect-mistakes`` main, adding error-level findings.

    Like the real command it exits **1** whenever an error-severity finding is
    reported -- an ordinary result the gate must not treat as a tool failure.
    """
    import contextlib
    import io

    from kicad_tools.cli import mistakes_cmd

    real = mistakes_cmd.main

    def fake(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = real(argv)
        doc = json.loads(buf.getvalue())
        if not only_candidate or "_routed" in argv[0]:
            doc["mistakes"] = doc["mistakes"] + extra
        print(json.dumps(doc))
        if any(m["severity"] == "error" for m in doc["mistakes"] if not m.get("waived")):
            return 1
        return code

    return patch.object(mistakes_cmd, "main", fake)


ERROR_MISTAKE = {
    "key": "mistake.thermal_pad_connection|U9||",
    "rule_id": "mistake.thermal_pad_connection",
    "category": "thermal_pad_connection",
    "severity": "error",
    "title": "Thermal pad not connected",
    "location": [110.0, 50.0],
    "evidence_hash": "deadbeef",
    "waived": False,
}


def test_input_board_with_error_mistake_routes_with_it_as_baseline(board, capsys):
    """An unwaived error-level mistake on the INPUT is baseline, not a blocker."""
    with _mistakes_with([ERROR_MISTAKE]):
        rc = _route(board, HARMLESS, "--format", "json")
    assert rc == 0, capsys.readouterr().err
    doc = json.loads(capsys.readouterr().out)
    assert doc["lint_gate"]["status"] == "pass"
    assert ERROR_MISTAKE["key"] not in [r["key"] for r in doc["lint_gate"]["introduced"]]
    assert _routed(board).exists()


def test_real_board_with_error_mistakes_is_gated_not_refused(tmp_path, capsys):
    """Repro from the review: a real board on which detect-mistakes exits 1."""
    src = Path(__file__).parent / "fixtures" / "pin1_marker.kicad_pcb"
    pcb = tmp_path / src.name
    shutil.copy(src, pcb)
    assert kct_main(["detect-mistakes", str(pcb), "--format", "json"]) == 1
    capsys.readouterr()
    rc = _route(pcb, HARMLESS)
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "nothing was routed" not in err
    assert "PASS" in err


def test_route_introducing_error_mistake_is_rolled_back_with_waive_hint(board, capsys):
    with _mistakes_with([ERROR_MISTAKE], only_candidate=True):
        rc = _route(board, HARMLESS, "--format", "json")
    assert rc == 3
    captured = capsys.readouterr()
    gate = json.loads(captured.out)["lint_gate"]
    assert gate["status"] == "rolled_back"
    assert ERROR_MISTAKE["key"] in [r["key"] for r in gate["introduced"]]
    assert ERROR_MISTAKE["key"] in captured.err
    assert "kct detect-mistakes" in captured.err
    assert f"--waive '{ERROR_MISTAKE['key']}'" in captured.err
    assert not _routed(board).exists()
    assert _rejected(board).exists()


def _broken_mistakes(*, only_candidate: bool = False):
    from kicad_tools.cli import mistakes_cmd

    real = mistakes_cmd.main

    def fake(argv):
        if only_candidate and "_routed" not in argv[0]:
            return real(argv)
        print("Traceback: something broke")
        return 1

    return patch.object(mistakes_cmd, "main", fake)


def test_unparseable_linter_output_on_input_is_a_tool_failure(board, capsys):
    calls: list = []
    with _broken_mistakes():
        rc = _route(board, SHORT, "--format", "json", calls=calls)
    assert rc == 1
    assert calls == []
    captured = capsys.readouterr()
    assert "nothing was routed" in captured.err
    # --format json still yields exactly one document.
    doc = json.loads(captured.out)
    assert doc["exit_code"] == 1
    assert doc["lint_gate"]["status"] == "error"
    assert "detect-mistakes" in doc["lint_gate"]["reason"]


def test_unparseable_linter_output_on_routed_board_fails_closed(board, capsys):
    with _broken_mistakes(only_candidate=True):
        rc = _route(board, HARMLESS, "--format", "json")
    assert rc == 3
    gate = json.loads(capsys.readouterr().out)["lint_gate"]
    assert gate["status"] == "error"
    assert not _routed(board).exists()
    assert _rejected(board).exists()


def test_check_without_report_is_a_tool_failure(board, capsys):
    """``kct check`` exiting 2 (its error-findings code) without a report still fails."""
    from kicad_tools.cli import check_cmd

    with patch.object(check_cmd, "main", lambda argv: 2):
        rc = _route(board, HARMLESS)
    assert rc == 1
    assert "wrote no report" in capsys.readouterr().err


def test_route_auto_baseline_failure_emits_json(board, capsys):
    out = board.with_name("auto_out.kicad_pcb")
    argv = ["route-auto", str(board), "--net", "GND", "-o", str(out), "--lint-gate"]
    with _broken_mistakes():
        rc = kct_main([*argv, "--format", "json"])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["success"] is False
    assert doc["lint_gate"]["status"] == "error"
    assert not out.exists()


def test_route_auto_lint_gate_rolls_back(board, capsys):
    out = board.with_name("auto_out.kicad_pcb")

    def fake_route_net_auto(*, pcb_path, net_name, output_path, **_kw):
        Path(output_path).write_text(_seed(Path(pcb_path).read_text(), SHORT))
        return {"success": True, "partial": False, "net_name": net_name, "warnings": []}

    argv = ["route-auto", str(board), "--net", "GND", "-o", str(out), "--lint-gate"]
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", fake_route_net_auto):
        rc = kct_main([*argv, "--format", "json"])
    assert rc == 3
    doc = json.loads(capsys.readouterr().out)
    assert doc["lint_gate"]["status"] == "rolled_back"
    assert doc["success"] is False and doc["nets_routed"] == 0
    assert not out.exists()


def test_route_auto_clean_run_is_kept(board, capsys):
    out = board.with_name("auto_out.kicad_pcb")

    def fake_route_net_auto(*, pcb_path, net_name, output_path, **_kw):
        Path(output_path).write_text(_seed(Path(pcb_path).read_text(), HARMLESS))
        return {"success": True, "partial": False, "net_name": net_name, "warnings": []}

    argv = ["route-auto", str(board), "--net", "GND", "-o", str(out), "--lint-gate"]
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", fake_route_net_auto):
        assert kct_main(argv) == 0
    assert out.exists()


def test_merge_into_json_document_keeps_a_single_document():
    from kicad_tools.cli.route_lint_gate import GateOutcome

    outcome = GateOutcome("rolled_back", "x")
    merged = json.loads(merge_into_json_document('{"exit_code": 0, "success": true}', outcome, 3))
    assert merged["exit_code"] == 3 and merged["success"] is False
    assert merged["lint_gate"]["status"] == "rolled_back"
    assert json.loads(merge_into_json_document("", outcome, 3))["exit_code"] == 3
    # Never append a second document to something that is not one object.
    assert merge_into_json_document("[1]\n[2]\n", outcome, 3) == "[1]\n[2]\n"
