"""UploadLedger must work when POSIX-only os flags are absent (Windows)."""

from __future__ import annotations

import os

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
