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

The ledger never holds card data or contact details. What it does hold: the
plan sha256, event times, approved and charged totals, the JLC order number,
and, for an unconfirmed payment, a short reason that can quote page-derived
text (the post-click URL path, or a parse error), passed through
``jlc_site_driver.printable`` (control characters replaced, length capped).

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

from ..export._fsutil import open_nofollow
from .jlc_site_driver import printable

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
        self._check_info(path, info)

    def _check_directory(self) -> None:
        """Refuse a pre-existing ledger directory that is a symlink, not ours, or not private.

        ``mkdir(mode=0o700)`` sets the mode only when it creates the directory.
        Someone who can write to this directory can delete the ledger, and the
        ledger is what stops a second order.
        """
        try:
            info = self.directory.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return  # nothing there yet: the mkdir below creates it (failures wrapped there)
        except OSError as exc:
            raise LedgerError(f"cannot stat {self.directory}: {exc.strerror}") from None
        if not stat.S_ISDIR(info.st_mode):
            raise LedgerError(f"{self.directory} is not a real directory; refusing it")
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise LedgerError("the order ledger needs a POSIX system (owner checks)")
        if info.st_uid != getuid():
            raise LedgerError(f"{self.directory} is not owned by you; refusing it")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise LedgerError(
                f"{self.directory} is writable by group or others "
                f"({stat.S_IMODE(info.st_mode):03o}); refusing it"
            )

    def _open_checked(self, path: Path, flags: int) -> int:
        """Open the ledger file without following a symlink, then re-check the descriptor.

        ``os.O_NOFOLLOW`` is required: a platform without it is refused outright,
        because its check-then-open window could follow a symlink swapped in
        between. The ``fstat`` afterwards re-checks the descriptor that was
        opened, so a file swapped in after the pre-open ``lstat`` is still
        caught. ``O_NONBLOCK`` is added so a FIFO swapped in cannot hang the
        open; the ``fstat`` check refuses it.
        """
        if not getattr(os, "O_NOFOLLOW", 0):
            raise LedgerError(
                "the order ledger requires os.O_NOFOLLOW; refusing it on this platform"
            )
        self._check_file(path)
        try:
            fd = open_nofollow(path, flags | getattr(os, "O_NONBLOCK", 0), 0o600)
        except OSError as exc:
            raise LedgerError(f"cannot open {path}: {exc.strerror}") from None
        try:
            self._check_info(path, os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _check_info(self, path: Path, info: os.stat_result) -> None:
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
        self._check_directory()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise LedgerError(f"cannot create {self.directory}: {exc.strerror}") from None
        lock_path = self.directory / LOCK_NAME
        fd = self._open_checked(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LedgerError(
                    "another kct checkout is recording a payment right now; nothing was "
                    "paid by this run"
                ) from None
            except OSError as exc:  # ENOLCK / ENOTSUP, e.g. on NFS or SMB
                raise LedgerError(
                    f"file locking is unavailable on {self.directory} ({exc.strerror}); "
                    "the checkout is refused and nothing was paid"
                ) from None
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def events(self) -> Iterator[dict[str, Any]]:
        self._check_directory()
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
        for key in ("reason", "step"):
            if key in record:
                record[key] = printable(record[key])
        unknown = set(record) - _ALLOWED_KEYS
        if unknown:
            raise LedgerError(f"refusing unknown ledger keys: {sorted(unknown)}")
        line = json.dumps(record, sort_keys=True) + "\n"
        self._check_directory()
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise LedgerError(f"cannot create {self.directory}: {exc.strerror}") from None
        fd = self._open_checked(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:  # a write failure must reach callers as LedgerError
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
