"""UploadLedger must work when POSIX-only os flags are absent (Windows)."""

from __future__ import annotations

import os
import stat

import pytest

from kicad_tools.manufacturers import jlc_upload as ju


@pytest.fixture
def no_posix_flags(monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)


def test_open_and_append_without_posix_flags(tmp_path, no_posix_flags):
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    assert (tmp_path / "uploads.jsonl").exists()
    fd = ju._open_nofollow(ledger.path, os.O_WRONLY | os.O_APPEND)
    os.close(fd)
    with ju._upload_lock(ledger):
        assert (tmp_path / ".uploads.jsonl.upload-lock").exists()


def test_fsync_dir_is_noop_without_o_directory(tmp_path, no_posix_flags):
    ju._fsync_dir(tmp_path)


def test_symlink_refused_without_o_nofollow(tmp_path, no_posix_flags):
    target = tmp_path / "real.jsonl"
    target.write_bytes(b"")
    link = tmp_path / "uploads.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ju.LedgerError):
        ju.UploadLedger.open(link)
    with pytest.raises(OSError):
        ju._open_nofollow(link, os.O_WRONLY | os.O_APPEND)


def test_ledger_round_trip_without_posix_flags(tmp_path, no_posix_flags):
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    assert ledger.attempts() == {}
    assert ledger.events() == []
    record = {
        "schema_version": ju.SCHEMA_VERSION,
        "kind": ju._PREVIEW_KIND,
        "attempt_id": "0" * 32,
    }
    ledger._append(record)
    assert ledger.events() == [record]


def test_submission_read_and_inventory_without_posix_flags(tmp_path, no_posix_flags):
    from kicad_tools.export import submission_plan as sp

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "sub" / "b.txt").write_bytes(b"y")
    assert sp._read(tmp_path, "sub/b.txt").data == b"y"
    sp._inventory(tmp_path, {"a.txt", "sub/b.txt"})
    with pytest.raises(sp.SubmissionError):
        sp._inventory(tmp_path, {"a.txt"})
    try:
        (tmp_path / "ln.txt").symlink_to(tmp_path / "a.txt")
    except OSError:
        return
    with pytest.raises(sp.SubmissionError):
        sp._read(tmp_path, "ln.txt")


# --------------------------------------------------------------------------
# Publication locks taken before the ledger (Judge round 3 on PR #7530):
# prepare_submission / refresh_inventory / submit_review / render_review_page.
# --------------------------------------------------------------------------

from tests.test_submission_plan import FakeSource, _demand_codes  # noqa: E402
from tests.test_submission_review import REQUIRED, plan_inputs  # noqa: E402,F401


def test_submission_and_review_locks_without_posix_flags(plan_inputs, tmp_path, no_posix_flags):  # noqa: F811
    from kicad_tools.export import submission_plan as sp
    from kicad_tools.export import submission_review as sr

    assert not hasattr(os, "O_NOFOLLOW") and not hasattr(os, "O_DIRECTORY")
    plan = sp.prepare_submission(**plan_inputs)
    assert sp.verify_submission(plan.directory, plan.sha256) == plan

    codes = _demand_codes(plan)
    snapshot = sp.refresh_inventory(
        plan,
        source=FakeSource([{"componentCode": codes[0], "stockCount": 7}]),
        destination=tmp_path / "inventory.json",
        observed_at="2026-01-01T00:00:00Z",
    )
    assert (tmp_path / "inventory.json").read_bytes() == snapshot.snapshot_bytes

    record = sr.submit_review(
        plan=plan,
        reviewer="alice@example.com",
        reviewed_at="2026-01-01T00:00:00Z",
        policy_version="policy-v1",
        checklist=dict.fromkeys(REQUIRED, True),
        required_items=REQUIRED,
        destination=tmp_path / "review.json",
    )
    page = sr.render_review_page(plan, destination=tmp_path / "page", record=record)
    assert "human_approval: reviewed" in (page.directory / "index.md").read_text()
    # Every lock is released (removed) afterwards, as on POSIX.
    assert not list(tmp_path.glob(".*-lock"))


def test_open_nofollow_keeps_posix_flag(tmp_path, monkeypatch):
    """POSIX behaviour is unchanged: O_NOFOLLOW is OR-ed in, no islink probe."""
    from kicad_tools.export import _fsutil

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform has no O_NOFOLLOW")
    seen = []
    real_open = os.open
    monkeypatch.setattr(
        os,
        "open",
        lambda path, flags, mode=0o777: seen.append(flags) or real_open(path, flags, mode),
    )
    monkeypatch.setattr(_fsutil, "is_link", lambda path: pytest.fail("fallback used on POSIX"))
    os.close(_fsutil.open_nofollow(tmp_path / "f", os.O_CREAT | os.O_WRONLY, 0o600))
    assert seen == [os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW]


def test_fallback_refuses_directory_junction(tmp_path, monkeypatch, no_posix_flags):
    """``islink`` misses Windows junctions; the fallbacks must still refuse them."""
    from kicad_tools.export import _fsutil
    from kicad_tools.export import submission_plan as sp

    junction = tmp_path / "junction"
    junction.mkdir()
    (junction / "f.txt").write_bytes(b"x")
    monkeypatch.setattr(
        os.path, "isjunction", lambda path: os.fspath(path) == os.fspath(junction), raising=False
    )
    assert _fsutil.is_link(junction)
    assert not _fsutil.is_link(tmp_path)
    with pytest.raises(sp.SubmissionError):
        sp._root(junction)
    with pytest.raises(sp.SubmissionError):
        sp._read(tmp_path, "junction/f.txt")
    with pytest.raises(sp.SubmissionError):
        sp._inventory(tmp_path, {"junction/f.txt"})
    with pytest.raises(OSError):
        _fsutil.open_nofollow(junction, os.O_RDONLY)


def test_junction_detected_via_reparse_tag_without_isjunction(tmp_path, monkeypatch):
    """Python < 3.12 has no ``os.path.isjunction``: fall back to the reparse tag."""
    from kicad_tools.export import _fsutil

    monkeypatch.delattr(os.path, "isjunction", raising=False)
    real_lstat = os.lstat

    class _Info:
        def __init__(self, tag):
            self.st_reparse_tag = tag
            self.st_mode = stat.S_IFDIR | 0o755

    target = tmp_path / "j"
    target.mkdir()
    monkeypatch.setattr(
        os,
        "lstat",
        lambda path, *a, **k: (
            _Info(_fsutil._IO_REPARSE_TAG_MOUNT_POINT)
            if os.fspath(path) == os.fspath(target)
            else real_lstat(path, *a, **k)
        ),
    )
    assert _fsutil.is_link(target)
    assert not _fsutil.is_link(tmp_path)


# --------------------------------------------------------------------------
# Regression guard: no new bare POSIX-only flag access anywhere in src/.
# --------------------------------------------------------------------------

_POSIX_ONLY_FLAGS = frozenset({"O_NOFOLLOW", "O_DIRECTORY"})
# The one module allowed to name them, behind supports_dir_fd()/getattr.
_ALLOWED = {"kicad_tools/export/_fsutil.py"}


def test_no_bare_posix_only_os_flags_in_src():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel in _ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _POSIX_ONLY_FLAGS
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                offenders.append(f"{rel}:{node.lineno}: os.{node.attr}")
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                offenders.extend(
                    f"{rel}:{node.lineno}: from os import {alias.name}"
                    for alias in node.names
                    if alias.name in _POSIX_ONLY_FLAGS
                )
    assert not offenders, (
        "POSIX-only os flags crash on Windows (issue #7529); use "
        "kicad_tools.export._fsutil (open_nofollow / fsync_dir / posix_dir_flags) "
        "or getattr(os, ..., 0):\n" + "\n".join(offenders)
    )
