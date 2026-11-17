"""``kct fleet status`` surfaces readiness ``known_issues`` (Issue #7261).

A known ``defect`` turns Ship? to ``NO (known defect #N)``; ``exception`` /
``advisory`` entries keep the board ship-ready but mark it ``YES*`` with a
``known: #N,#M`` cell.  A missing or malformed readiness report stays warn-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.fleet_cmd import main
from tests.test_fleet_status import make_fake_board


def _entry(issue: int, kind: str) -> dict:
    return {"issue": issue, "kind": kind, "summary": f"issue {issue}", "recorded": "2026-10-18"}


def _board(boards: Path, name: str, known: list[dict] | None, *, raw: str | None = None) -> None:
    make_fake_board(boards, name, has_gerbers=True, has_bom=True, has_cpl=True, has_manifest=True)
    report = boards / name / "output" / "readiness.json"
    if raw is not None:
        report.write_text(raw)
    elif known is not None:
        report.write_text(
            json.dumps({"schema_version": 1, "status": "ready", "known_issues": known})
        )


def _fleet(boards: Path) -> Path:
    _board(boards, "a-clean", None)
    _board(boards, "b-advisory", [_entry(6269, "exception"), _entry(7220, "advisory")])
    _board(boards, "c-defect", [_entry(7220, "advisory"), _entry(7250, "defect")])
    _board(boards, "d-garbled", None, raw="{not json")
    _board(boards, "e-malformed", [{"issue": "x"}])
    return boards


def test_json_reports_known_issues_and_defects_block(tmp_path, capsys):
    boards = _fleet(tmp_path / "boards")
    code = main(["status", "--boards-dir", str(boards), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    by_name = {b["name"]: b for b in data["boards"]}

    assert code == 2  # c-defect is not ship-ready
    assert by_name["a-clean"]["ship_ready"] is True
    assert by_name["a-clean"]["known_issues"] == []
    assert by_name["b-advisory"]["ship_ready"] is True
    assert [e["issue"] for e in by_name["b-advisory"]["known_issues"]] == [6269, 7220]
    assert by_name["c-defect"]["ship_ready"] is False
    assert by_name["c-defect"]["blockers"] == ["known defect #7250"]
    # Warn-only: unreadable/malformed reports add nothing.
    assert by_name["d-garbled"]["ship_ready"] is True
    assert by_name["e-malformed"]["known_issues"] == []
    assert data["summary"]["ship_ready"] == 4
    assert data["summary"]["with_known_issues"] == 2


def test_table_marks_yes_star_and_known_cell(tmp_path, capsys):
    boards = _fleet(tmp_path / "boards")
    main(["status", "--boards-dir", str(boards)])
    out = capsys.readouterr().out
    rows = {line.split()[0]: line for line in out.splitlines() if line and line[0] in "abcde"}

    assert rows["a-clean"].rstrip().endswith("YES")
    assert "YES*  known: #6269,#7220" in rows["b-advisory"]
    assert "NO  (known defect #7250)  known: #7220,#7250" in rows["c-defect"]
    assert "5 boards surveyed, 4 ship-ready" in out
    assert "2 with known issues" in out
