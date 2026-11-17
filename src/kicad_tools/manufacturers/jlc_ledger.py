"""Append-only order ledger for the attended JLC checkout leg (issue #8935).

One JSON line per event, keyed by the frozen plan's sha256, in a file outside
the run directory (default ``~/.kct/orders/jlc-orders.jsonl``) so a re-run into
a fresh ``-o`` directory still sees earlier payments.

Events, in the order a checkout writes them:

* ``payment-attempted`` -- written and fsynced *before* the pay button is
  clicked, so a crash, kill or power loss after the click still leaves a record.
* ``ordered`` -- written only from the fab's confirmation page of a completed
  payment, with the order identifier and the charged total.
* ``payment-unconfirmed`` -- the click happened (or may have) but no
  confirmation page was recognised. The plan is NOT ordered.
* ``cleared-by-operator`` -- the operator checked the account by hand, found no
  charge and said so on a TTY (:func:`clear_unconfirmed`).

:func:`plan_status` folds these: ``ordered`` beats everything, an attempt with
no later ``ordered`` / ``cleared-by-operator`` is ``unresolved``. Both block a
new checkout, so a second order for the same plan cannot be placed by this tool
(and an ambiguous payment is never retried).

The ledger holds amounts and order identifiers only: never card data, contact
details or page text beyond the order id. Personal data it does hold: the
plan sha256, event times, approved and charged totals, the JLC order number,
and the step/reason of a stopped or unconfirmed payment.

The ledger lives under ``$HOME`` (``~/.kct/orders``). Its protection against a
second order is exactly as strong as that file: deleting it, or running with a
different ``HOME``, removes it. The file must be owned by you and mode 600 on
every open (refused otherwise), and :meth:`OrderLedger.locked` serialises the
"check, then record the attempt" step between concurrent runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows: no flock, so no checkout
    fcntl = None  # type: ignore[assignment]

LEDGER_NAME = "jlc-orders.jsonl"
LOCK_NAME = "jlc-orders.lock"
EVENTS = ("payment-attempted", "ordered", "payment-unconfirmed", "cleared-by-operator")
#: Keys an event may carry. Anything else is refused, so a caller cannot smuggle
#: card data or page text into the ledger.
_ALLOWED_KEYS = frozenset(
    {
        "event",
        "time",
        "plan_sha256",
        "kind",
        "approved_total",
        "charged_total",
        "order_id",
        "driver_version",
        "step",
        "reason",
    }
)


class LedgerError(RuntimeError):
    """The ledger could not be read or written; the checkout must not proceed."""


def default_ledger_dir() -> Path:
    return Path.home() / ".kct" / "orders"


@dataclass(frozen=True)
class PlanStatus:
    state: str  # not-ordered | ordered | unresolved
    order_id: str = ""
    charged_total: str = ""
    time: str = ""


class OrderLedger:
    def __init__(self, directory: Path | None = None, *, now: Callable[[], float] = time.time):
        self.directory = Path(directory) if directory else default_ledger_dir()
        self.path = self.directory / LEDGER_NAME
        self._now = now

    def _check_file(self, path: Path) -> None:
        """Refuse a ledger file that is a symlink, not ours, or readable by others."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerError(f"cannot stat {path}: {exc.strerror}") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LedgerError(f"{path} is not a regular file; refusing it")
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise LedgerError("the order ledger needs a POSIX system (owner checks)")
        if info.st_uid != getuid():
            raise LedgerError(f"{path} is not owned by you; refusing it")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise LedgerError(
                f"{path} has mode {stat.S_IMODE(info.st_mode):03o}, not 600; "
                f"check it and run chmod 600 {path}"
            )

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive lock across "check status, then append". Never waits: a held lock fails."""
        if fcntl is None:
            raise LedgerError("no file locking on this platform; the checkout is refused")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.directory / LOCK_NAME
        self._check_file(lock_path)
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise LedgerError(f"cannot open ledger lock {lock_path}: {exc.strerror}") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LedgerError(
                    "another kct checkout is recording a payment right now; nothing was "
                    "paid by this run"
                ) from None
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        self._check_file(self.path)
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LedgerError(f"cannot read order ledger {self.path}: {exc.strerror}") from None
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # An unreadable line could be a lost "ordered": fail closed.
                raise LedgerError(
                    f"order ledger {self.path} line {number} is not JSON; fix it by hand "
                    "before any checkout (an unreadable line may be a recorded order)"
                ) from None
            if not isinstance(record, dict):
                raise LedgerError(
                    f"order ledger {self.path} line {number} is not a JSON object; fix it by "
                    "hand before any checkout"
                )
            yield record

    def append(self, event: str, plan_sha256: str, **fields: Any) -> dict[str, Any]:
        if event not in EVENTS:
            raise LedgerError(f"unknown ledger event {event!r}")
        record: dict[str, Any] = {
            "event": event,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now())),
            "plan_sha256": plan_sha256,
            **fields,
        }
        unknown = set(record) - _ALLOWED_KEYS
        if unknown:
            raise LedgerError(f"refusing unknown ledger keys: {sorted(unknown)}")
        line = json.dumps(record, sort_keys=True) + "\n"
        self._check_file(self.path)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise LedgerError(f"cannot write order ledger {self.path}: {exc.strerror}") from None
        return record

    def plan_status(self, plan_sha256: str) -> PlanStatus:
        status = PlanStatus("not-ordered")
        for record in self.events():
            if record.get("plan_sha256") != plan_sha256:
                continue
            event = record.get("event")
            if event == "ordered":
                return PlanStatus(
                    "ordered",
                    str(record.get("order_id", "")),
                    str(record.get("charged_total", "")),
                    str(record.get("time", "")),
                )
            if event in ("payment-attempted", "payment-unconfirmed"):
                status = PlanStatus("unresolved", time=str(record.get("time", "")))
            elif event == "cleared-by-operator":
                status = PlanStatus("not-ordered")
        return status


def describe_block(status: PlanStatus, ledger: OrderLedger) -> str:
    if status.state == "ordered":
        return (
            f"this plan is already ordered (order {status.order_id}, charged "
            f"{status.charged_total}, {status.time}); refusing to place a second order"
        )
    return (
        f"a payment for this plan was attempted at {status.time} and never confirmed. "
        "Check your JLCPCB account by hand (Order History and the payment record). "
        "If it was charged, do not re-run. If it was not, record that with "
        "`kct submit-jlcpcb ... --clear-unconfirmed-payment` on a terminal. "
        f"Ledger: {ledger.path}"
    )


def clear_unconfirmed(
    ledger: OrderLedger,
    plan_sha256: str,
    ask: Callable[[str], str],
) -> bool:
    """Record the operator's by-hand finding that an unconfirmed payment was not charged.

    Only an ``unresolved`` plan can be cleared, and only on the exact typed
    answer ``not charged``. Never clears an ``ordered`` plan.
    """
    status = ledger.plan_status(plan_sha256)
    if status.state != "unresolved":
        return False
    answer = ask(
        "Have you checked your JLCPCB account and confirmed this attempt was NOT charged?\n"
        "Type exactly 'not charged' to record that: "
    )
    if answer.strip() != "not charged":
        return False
    ledger.append("cleared-by-operator", plan_sha256, reason="operator checked account by hand")
    return True
