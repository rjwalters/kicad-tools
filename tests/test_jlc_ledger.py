"""Order ledger for the attended checkout leg (issue #8935): idempotence and fail-closed reads."""

from __future__ import annotations

import json
import stat

import pytest

from kicad_tools.manufacturers.jlc_ledger import (
    LedgerError,
    OrderLedger,
    clear_unconfirmed,
)

A, B = "a" * 64, "b" * 64


def test_empty_ledger_is_not_ordered(tmp_path):
    assert OrderLedger(tmp_path).plan_status(A).state == "not-ordered"


def test_ordered_beats_everything_and_is_per_plan(tmp_path):
    ledger = OrderLedger(tmp_path)
    ledger.append("payment-attempted", A, approved_total="USD 1.00")
    ledger.append("ordered", A, order_id="Y1-000001", charged_total="USD 1.00")
    ledger.append("cleared-by-operator", A)
    status = ledger.plan_status(A)
    assert status.state == "ordered" and status.order_id == "Y1-000001"
    assert ledger.plan_status(B).state == "not-ordered"


def test_attempt_without_confirmation_is_unresolved_until_cleared(tmp_path):
    ledger = OrderLedger(tmp_path)
    ledger.append("payment-attempted", A)
    assert ledger.plan_status(A).state == "unresolved"
    assert not clear_unconfirmed(ledger, A, lambda p: "yes")
    assert ledger.plan_status(A).state == "unresolved"
    assert clear_unconfirmed(ledger, A, lambda p: "not charged")
    assert ledger.plan_status(A).state == "not-ordered"


def test_clear_never_touches_an_ordered_plan(tmp_path):
    ledger = OrderLedger(tmp_path)
    ledger.append("ordered", A, order_id="Y1-000001")
    assert not clear_unconfirmed(ledger, A, lambda p: "not charged")
    assert ledger.plan_status(A).state == "ordered"


def test_unreadable_line_fails_closed(tmp_path):
    ledger = OrderLedger(tmp_path)
    ledger.append("ordered", A, order_id="Y1-000001")
    with ledger.path.open("a") as handle:
        handle.write("{truncated\n")
    with pytest.raises(LedgerError, match="not JSON"):
        ledger.plan_status(B)


def test_ledger_refuses_unknown_keys_and_events(tmp_path):
    ledger = OrderLedger(tmp_path)
    with pytest.raises(LedgerError, match="unknown ledger keys"):
        ledger.append("ordered", A, card_number="4111111111111111")
    with pytest.raises(LedgerError, match="unknown ledger event"):
        ledger.append("paid-maybe", A)
    assert not ledger.path.exists()


def test_ledger_file_is_private_and_append_only(tmp_path):
    ledger = OrderLedger(tmp_path / "orders")
    ledger.append("payment-attempted", A)
    ledger.append("payment-unconfirmed", A)
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    lines = ledger.path.read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == [
        "payment-attempted",
        "payment-unconfirmed",
    ]


def test_valid_json_that_is_not_an_object_fails_closed(tmp_path):
    ledger = OrderLedger(tmp_path)
    ledger.append("payment-attempted", A)
    with ledger.path.open("a") as handle:
        handle.write('["ordered"]\n')
    with pytest.raises(LedgerError, match="not a JSON object"):
        ledger.plan_status(A)


def test_ledger_mode_is_verified_on_every_open(tmp_path):
    import os

    ledger = OrderLedger(tmp_path)
    ledger.append("payment-attempted", A)
    os.chmod(ledger.path, 0o644)
    with pytest.raises(LedgerError, match="mode 644"):
        ledger.plan_status(A)
    with pytest.raises(LedgerError, match="mode 644"):
        ledger.append("payment-unconfirmed", A)


def test_symlinked_ledger_is_refused(tmp_path):
    real = tmp_path / "elsewhere.jsonl"
    real.write_text("")
    real.chmod(0o600)
    (tmp_path / "orders").mkdir()
    ledger = OrderLedger(tmp_path / "orders")
    ledger.path.symlink_to(real)
    with pytest.raises(LedgerError, match="not a regular file"):
        ledger.plan_status(A)


def test_lock_is_exclusive_and_never_waits(tmp_path):
    ledger = OrderLedger(tmp_path)
    with ledger.locked():
        with pytest.raises(LedgerError, match="another kct checkout"):
            with OrderLedger(tmp_path).locked():
                pass
    with ledger.locked():  # released
        pass


def test_default_ledger_lives_under_home(monkeypatch, tmp_path):
    from kicad_tools.manufacturers import jlc_ledger

    monkeypatch.setenv("HOME", str(tmp_path))
    assert OrderLedger().directory == tmp_path / ".kct" / "orders"
    assert jlc_ledger.default_ledger_dir() == tmp_path / ".kct" / "orders"


def test_ledger_and_lock_owner_is_checked(tmp_path, monkeypatch):
    import os

    ledger = OrderLedger(tmp_path)
    ledger.append("payment-attempted", A)
    with ledger.locked():
        pass  # creates the lock file
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    with pytest.raises(LedgerError, match="not owned"):
        ledger.plan_status(A)
    with pytest.raises(LedgerError, match="not owned"):
        with ledger.locked():
            pass


def test_lock_file_must_be_a_private_regular_file(tmp_path):
    import os

    ledger = OrderLedger(tmp_path / "a")
    ledger.directory.mkdir()
    target = tmp_path / "target"
    target.write_text("")
    target.chmod(0o600)
    (ledger.directory / "jlc-orders.lock").symlink_to(target)
    with pytest.raises(LedgerError, match="not a regular file"):
        with ledger.locked():
            pass
    other = OrderLedger(tmp_path / "b")
    other.directory.mkdir()
    lock = other.directory / "jlc-orders.lock"
    lock.write_text("")
    os.chmod(lock, 0o644)
    with pytest.raises(LedgerError, match="mode 644"):
        with other.locked():
            pass


def test_open_refuses_a_symlink_swapped_in_after_the_check(tmp_path, monkeypatch):
    """O_NOFOLLOW (or open_nofollow's fallback) is the guard once the lstat check has passed."""
    target = tmp_path / "target.jsonl"
    target.write_text("")
    target.chmod(0o600)
    ledger = OrderLedger(tmp_path / "orders")
    ledger.directory.mkdir()
    ledger.path.symlink_to(target)
    monkeypatch.setattr(OrderLedger, "_check_file", lambda self, path: None)
    with pytest.raises(LedgerError, match="cannot open"):
        ledger.append("payment-attempted", A)
    assert target.read_text() == ""


def test_lock_failure_other_than_busy_is_a_clear_refusal(tmp_path, monkeypatch):
    import errno

    from kicad_tools.manufacturers import jlc_ledger

    def flock(fd, op):
        if op & jlc_ledger.fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(jlc_ledger.fcntl, "flock", flock)
    with pytest.raises(LedgerError, match="file locking is unavailable"):
        with OrderLedger(tmp_path).locked():
            pass


def test_no_file_locking_refuses(tmp_path, monkeypatch):
    from kicad_tools.manufacturers import jlc_ledger

    monkeypatch.setattr(jlc_ledger, "fcntl", None)
    with pytest.raises(LedgerError, match="no file locking"):
        with OrderLedger(tmp_path).locked():
            pass


def test_write_error_is_a_ledger_error(tmp_path, monkeypatch):
    import os

    def boom(fd, data):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "write", boom)
    with pytest.raises(LedgerError, match="cannot write order ledger"):
        OrderLedger(tmp_path).append("payment-attempted", A)


@pytest.mark.parametrize("answer", ["y", "yes", "Not charged", "not charged!", ""])
def test_clear_needs_the_exact_phrase(tmp_path, answer):
    ledger = OrderLedger(tmp_path)
    ledger.append("payment-attempted", A)
    assert not clear_unconfirmed(ledger, A, lambda p: answer)
    assert ledger.plan_status(A).state == "unresolved"


def test_open_rechecks_the_file_it_actually_opened(tmp_path, monkeypatch):
    """A regular file swapped in after the lstat check is caught by the fstat recheck."""
    import os

    ledger = OrderLedger(tmp_path)
    ledger.path.write_text("")
    os.chmod(ledger.path, 0o644)
    monkeypatch.setattr(OrderLedger, "_check_file", lambda self, path: None)
    with pytest.raises(LedgerError, match="mode 644"):
        ledger.append("payment-attempted", A)
    assert ledger.path.read_text() == ""
