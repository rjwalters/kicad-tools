"""Committed board evidence regenerates byte-for-byte (Issue #7384).

``scripts/boards/refresh_readiness.py <board> --dry-run`` with the board's
committed ``checked_at`` must list **no** file to publish: re-running the
producer on the pinned toolchain is a no-op on evidence nobody changed.
``test_board_readiness_committed_hashes`` only checks that the recorded input
hashes match, so without this guard a producer that rewrites its own output on
every run (a KiCad run ``date``, re-encoded zip metadata) went unnoticed on six
of seven boards.

* The unit tests need no KiCad and always run.
* The native test needs the pinned evidence toolchain (``KCT_KICAD_DOCKER=1``
  or a matching kicad-cli) and skips with the refusal's own reason otherwise.
  It runs in the nightly ``slow-tests.yml`` job inside CI's pinned image.

A board whose drift has a tracked cause may be excepted in
``EXPECTED_DRIFT`` by name and issue only.  The exception is a strict xfail (a
board that becomes clean fails until its entry is removed) and it expires: the
expiry test fails once the named issue is closed.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "boards" / "refresh_readiness.py"
BOARDS_DIR = REPO_ROOT / "boards"
REPO = "rjwalters/kicad-tools"

#: Boards whose dry-run is allowed to list files, keyed by board directory and
#: mapped to the open issue that owns the drift.  Empty: every board reproduces.
EXPECTED_DRIFT: dict[str, int] = {}


@functools.cache
def _module():
    spec = importlib.util.spec_from_file_location("refresh_readiness_7384", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _module()


def gh_issue_state(issue: int) -> str | None:
    """``OPEN`` / ``CLOSED`` for *issue*, or ``None`` when GitHub cannot be asked."""
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", str(issue), "--repo", REPO, "--json", "state", "-q", ".state"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = proc.stdout.strip()
    return state if proc.returncode == 0 and state else None


def expired_exceptions(
    exceptions: dict[str, int], state_of: Callable[[int], str | None]
) -> dict[str, int]:
    """Exceptions whose issue is no longer open.

    Raises ``LookupError`` when an issue's state cannot be read, so the caller
    can skip visibly instead of treating "unknown" as "still open".
    """
    expired: dict[str, int] = {}
    for board, issue in sorted(exceptions.items()):
        state = state_of(issue)
        if state is None:
            raise LookupError(f"cannot read the state of #{issue} (exception for {board})")
        if state != "OPEN":
            expired[board] = issue
    return expired


@functools.cache
def _toolchain_refusal() -> str | None:
    """Why the pinned evidence toolchain is unavailable, or ``None`` if it is."""
    try:
        with evidence_toolchain():
            return None
    except KiCadEnvironmentError as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Unit: always runs
# ---------------------------------------------------------------------------


def test_exceptions_name_configured_boards_and_issues():
    configured = set(_module().BOARDS)
    for board, issue in EXPECTED_DRIFT.items():
        assert board in configured, f"{board}: not a refresh_readiness board"
        assert isinstance(issue, int) and issue > 0


def test_an_exception_naming_a_closed_issue_expires():
    states = {7241: "CLOSED", 9999: "OPEN"}
    exceptions = {"05-bldc-motor-controller": 7241, "07-matchgroup-test": 9999}
    assert expired_exceptions(exceptions, states.get) == {"05-bldc-motor-controller": 7241}


def test_an_unreadable_issue_state_never_counts_as_open():
    with pytest.raises(LookupError, match="#7241"):
        expired_exceptions({"05-bldc-motor-controller": 7241}, lambda _issue: None)


def test_exceptions_have_not_outlived_their_issue():
    if not EXPECTED_DRIFT:
        return
    try:
        expired = expired_exceptions(EXPECTED_DRIFT, gh_issue_state)
    except LookupError as exc:
        pytest.skip(f"cannot check exception expiry: {exc}")
    assert not expired, f"remove EXPECTED_DRIFT entries whose issue closed: {expired}"


def test_settle_report_keeps_a_report_that_differs_only_by_run_date(mod, tmp_path):
    target = tmp_path / "native-erc.json"
    committed = b'{\n  "date": "2026-10-19T19:58:00",\n  "sheets": []\n}\n'
    target.write_bytes(committed)
    fresh = json.dumps({"date": "2026-10-19T23:27:41", "sheets": []}).encode()

    assert mod.settle_report(target, fresh) == committed
    assert target.read_bytes() == committed


@pytest.mark.parametrize(
    "fresh",
    [
        {"date": "2026-10-19T23:27:41", "sheets": [{"violations": [{"type": "x"}]}]},
        {"date": "2026-10-19T19:58:00", "sheets": [], "kicad_version": "10.0.7"},
    ],
    ids=["new-finding", "other-field"],
)
def test_settle_report_writes_a_report_whose_content_changed(mod, tmp_path, fresh):
    target = tmp_path / "native-erc.json"
    target.write_text(json.dumps({"date": "2026-10-19T19:58:00", "sheets": []}))
    raw = json.dumps(fresh).encode()

    assert mod.settle_report(target, raw) == raw
    assert target.read_bytes() == raw


def test_settle_report_creates_a_missing_report(mod, tmp_path):
    target = tmp_path / "drc_report.json"
    assert mod.settle_report(target, b'{"date": "now"}') == b'{"date": "now"}'
    assert target.read_bytes() == b'{"date": "now"}'


def _foreign_zip(path: Path, members: dict[str, bytes]) -> None:
    """A deterministic zip in another writer's encoding (``kct readiness``'s)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, members[name])


def test_rebuild_archive_keeps_an_archive_whose_members_are_unchanged(mod, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.kicad_pcb").write_bytes(b"pcb" * 500)
    (src / "a.kicad_sch").write_bytes(b"sch" * 500)
    archive = tmp_path / "kicad_project.zip"
    _foreign_zip(archive, {"a.kicad_pcb": b"pcb" * 500, "a.kicad_sch": b"sch" * 500})
    committed = archive.read_bytes()

    assert mod.rebuild_archive(archive, [src]) == []
    assert archive.read_bytes() == committed


def test_rebuild_archive_rewrites_deterministically_when_a_member_changes(mod, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.kicad_pcb").write_bytes(b"new pcb")
    (src / "a.kicad_sch").write_bytes(b"sch")
    archive = tmp_path / "kicad_project.zip"
    _foreign_zip(archive, {"a.kicad_pcb": b"old pcb", "a.kicad_sch": b"sch"})

    assert mod.rebuild_archive(archive, [src]) == ["a.kicad_pcb"]
    expected = tmp_path / "expected.zip"
    mod.write_deterministic_zip(expected, {"a.kicad_pcb": b"new pcb", "a.kicad_sch": b"sch"})
    assert archive.read_bytes() == expected.read_bytes()
    # A second refresh over the rewritten archive is a fixed point.
    assert mod.rebuild_archive(archive, [src]) == []
    assert archive.read_bytes() == expected.read_bytes()


# ---------------------------------------------------------------------------
# Native: pinned toolchain only
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("name", sorted(_module().BOARDS))
def test_dry_run_with_committed_checked_at_publishes_nothing(name, request, tmp_path) -> None:
    refusal = _toolchain_refusal()
    if refusal is not None:
        pytest.skip(f"pinned evidence toolchain unavailable: {refusal}")
    if name in EXPECTED_DRIFT:
        request.applymarker(
            pytest.mark.xfail(reason=f"tracked drift: #{EXPECTED_DRIFT[name]}", strict=True)
        )
    rr = _module()
    # Work on a copy: a dry run publishes nothing, but the guard must not be
    # able to touch the committed tree even if that ever regresses.
    board = tmp_path / name
    shutil.copytree(BOARDS_DIR / name, board, ignore=shutil.ignore_patterns("__pycache__"))
    checked_at = json.loads((board / "output" / "readiness.json").read_text())["checked_at"]

    with evidence_toolchain(mounts=[REPO_ROOT, tmp_path]):
        published = rr.refresh(board, checked_at, dry_run=True)

    assert published == [], (
        f"{name}: re-running refresh_readiness.py with the committed checked_at "
        f"would rewrite {published}"
    )
