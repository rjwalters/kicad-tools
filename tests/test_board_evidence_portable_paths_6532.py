"""Board evidence must not record absolute host paths (Issue #6532).

Two halves:

* The report writers (``kct check --format json`` / ``--output`` and
  ``kct net-status --format json``) record the checked PCB through
  :func:`kicad_tools.cli.check_cmd.portable_report_path`: cwd-relative when
  the PCB is under the cwd, otherwise the bare filename, never absolute.
* A scan of every committed ``boards/*/output/**/*.json`` file.  Evidence
  is hash-bound, so it is not rewritten here; instead the files that still
  leak are listed in ``KNOWN_OFFENDERS`` and each board's next gated re-pin
  drops its entries.  The list may only shrink: a new offender fails, and so
  does an allowlisted file that is now clean.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.check_cmd import (
    output_json,
    portable_report_path,
    write_json_report,
)
from kicad_tools.validate import DRCResults
from tests.test_kct_readiness_cmd import FakeEngines, make_board

REPO_ROOT = Path(__file__).resolve().parent.parent

# A JSON string value that starts with an absolute host path.
ABSOLUTE_HOST_PATH = re.compile(r'"(?:/home/|/Users/|/private/var/|/tmp/|[A-Z]:\\\\)')

# Files that still carry an absolute path, as of the #6532 writer fix.
# Shrink-only: remove an entry when its board's evidence is re-issued.
KNOWN_OFFENDERS = frozenset(
    {
        "boards/00-simple-led/output/drc_report.json",
        "boards/03-usb-joystick/output/drc_report.json",
        "boards/03-usb-joystick/output/manufacturing/check-report.json",
        "boards/03-usb-joystick/output/manufacturing/native-clearance-qualification/factory-check.json",
        "boards/03-usb-joystick/output/manufacturing/native-clearance-qualification/final-board-manufacturing-check.json",
        "boards/04-stm32-devboard/output/check-report.json",
        "boards/05-bldc-motor-controller/output/manufacturing/check-report.json",
        "boards/05-bldc-motor-controller/output/manufacturing/kct-check.json",
        "boards/05-bldc-motor-controller/output/readiness/kct-check.json",
        "boards/06-diffpair-test/output/manufacturing/check-report.json",
        "boards/06-diffpair-test/output/readiness/kct-check.json",
        "boards/09-usbc-pd-power/output/manufacturer-check.json",
        "boards/09-usbc-pd-power/output/net-status.json",
    }
)


def _committed_board_evidence() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", "boards/*/output/*.json", "boards/*/output/**/*.json"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return sorted({p for p in out.decode().split("\0") if p})


def _offenders() -> set[str]:
    found: set[str] = set()
    for rel in _committed_board_evidence():
        path = REPO_ROOT / rel
        if not path.is_file():  # deleted in the working tree
            continue
        if ABSOLUTE_HOST_PATH.search(path.read_text(errors="replace")):
            found.add(rel)
    return found


# ---------------------------------------------------------------------------
# Committed evidence scan (shrink-only allowlist)
# ---------------------------------------------------------------------------


def test_no_new_board_evidence_records_an_absolute_host_path():
    new = sorted(_offenders() - KNOWN_OFFENDERS)
    assert not new, (
        "Board evidence records an absolute host path (Issue #6532). Produce it "
        "with a cwd-relative or bare PCB path instead:\n  " + "\n  ".join(new)
    )


def test_known_offender_allowlist_only_shrinks():
    cleaned = sorted(KNOWN_OFFENDERS - _offenders())
    assert not cleaned, (
        "These files no longer contain an absolute host path; remove them from "
        "KNOWN_OFFENDERS so they cannot regress:\n  " + "\n  ".join(cleaned)
    )


@pytest.mark.parametrize(
    "text, leaks",
    [
        ('{"file": "/home/ubuntu/x.kicad_pcb"}', True),
        ('{"file": "/Users/me/x.kicad_pcb"}', True),
        ('{"file": "/private/var/folders/T/x.kicad_pcb"}', True),
        ('{"file": "/tmp/x.kicad_pcb"}', True),
        ('{"file": "C:\\\\work\\\\x.kicad_pcb"}', True),
        ('{"file": "output/x.kicad_pcb"}', False),
        ('{"file": "x.kicad_pcb"}', False),
        ('{"note": "see /home/ for details"}', False),
    ],
)
def test_scan_pattern(text, leaks):
    assert bool(ABSOLUTE_HOST_PATH.search(text)) is leaks


# ---------------------------------------------------------------------------
# portable_report_path
# ---------------------------------------------------------------------------


def test_helper_absolute_under_cwd_is_cwd_relative(tmp_path, monkeypatch):
    pcb = tmp_path / "board" / "output" / "x.kicad_pcb"
    monkeypatch.chdir(tmp_path)
    assert portable_report_path(pcb) == "board/output/x.kicad_pcb"


def test_helper_absolute_outside_cwd_is_bare_name(tmp_path, monkeypatch):
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    assert portable_report_path(tmp_path / "other" / "x.kicad_pcb") == "x.kicad_pcb"


def test_helper_relative_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert portable_report_path(Path("../somewhere/x.kicad_pcb")) == str(
        Path("../somewhere/x.kicad_pcb")
    )
    assert portable_report_path("output/x.kicad_pcb") == "output/x.kicad_pcb"


def test_helper_resolves_symlinked_cwd(tmp_path, monkeypatch):
    """macOS ``/var`` -> ``/private/var``: compare resolved paths on both sides."""
    real = tmp_path / "real"
    (real / "output").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(link)
    assert portable_report_path(real / "output" / "x.kicad_pcb") == "output/x.kicad_pcb"
    monkeypatch.chdir(real)
    assert portable_report_path(link / "output" / "x.kicad_pcb") == "output/x.kicad_pcb"


def test_helper_explicit_cwd(tmp_path):
    assert portable_report_path(tmp_path / "a" / "x.kicad_pcb", cwd=tmp_path) == "a/x.kicad_pcb"


# ---------------------------------------------------------------------------
# kct check JSON writers
# ---------------------------------------------------------------------------


def _stdout_report(pcb: Path, capsys) -> dict:
    output_json([], DRCResults(), pcb, "jlcpcb", 2)
    return json.loads(capsys.readouterr().out)


def _file_report(pcb: Path, out: Path) -> dict:
    write_json_report([], DRCResults(), pcb, "jlcpcb", 2, out)
    return json.loads(out.read_text())


@pytest.mark.parametrize("writer", ["stdout", "file"])
def test_check_writers_record_portable_path(
    writer, tmp_path, tmp_path_factory, monkeypatch, capsys
):
    def report(pcb: Path) -> dict:
        if writer == "stdout":
            return _stdout_report(pcb, capsys)
        return _file_report(pcb, tmp_path / "report.json")

    pcb = tmp_path / "board" / "x.kicad_pcb"

    monkeypatch.chdir(tmp_path)
    assert report(pcb.resolve())["file"] == "board/x.kicad_pcb"
    assert report(Path("board/x.kicad_pcb"))["file"] == "board/x.kicad_pcb"

    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    assert report(pcb.resolve())["file"] == "x.kicad_pcb"


# ---------------------------------------------------------------------------
# readiness keeps recording the board-relative path (#6076)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cwd_kind", ["board_parent", "elsewhere"])
def test_readiness_rewrites_portable_kct_check_path_board_relative(
    cwd_kind, tmp_path, tmp_path_factory, monkeypatch
):
    """``kct check`` now writes cwd-relative / bare names; readiness evidence
    must still record ``output/<name>`` relative to the board directory."""
    board = make_board(tmp_path)
    fake = FakeEngines(board)
    inner = fake.kct_check

    def kct_check(pcb, mfr, report_path, extra):
        run = inner(pcb, mfr, report_path, extra)
        data = json.loads(report_path.read_text())
        data["file"] = portable_report_path(Path(pcb).resolve())
        report_path.write_text(json.dumps(data, indent=2) + "\n")
        return run

    fake.kct_check = kct_check
    monkeypatch.chdir(board.parent if cwd_kind == "board_parent" else tmp_path_factory.mktemp("x"))
    rc = readiness_cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=fake.bundle())
    assert rc == 0
    evidence = json.loads((board / "output/readiness/kct-check.json").read_text())
    assert evidence["file"] == "output/demo_routed.kicad_pcb"
