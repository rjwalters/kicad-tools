"""Advisory run lock that stops two ``kct route`` runs sharing one output.

Every terminal write of a route run strips the top-level segment/via blocks
and re-inserts its own, so when two runs (or one run plus an external editor)
share an output the later saver silently deletes the other's copper.
:mod:`kicad_tools.core.kicad_lock` only reports KiCad's ``~<name>.lck`` marker
and never sees another kct run; this module covers that gap (issue #9628).

The mechanism is a sibling lock file ``<output dir>/.<output name>.kct-route.lock``
holding ``{"pid", "hostname", "started_at"}``, created with ``O_CREAT|O_EXCL``.
Its name differs from KiCad's marker, which KiCad owns and would mistake for a
foreign session. The lock never compares file stat, so a run's own rewrites
(checkpoints, zone fill, DRC re-saves) cannot trigger a refusal: it is checked
once, at acquire.

Stale rule: same host and the recorded pid no longer exists -> steal. A lock
from another host, or one that cannot be parsed, cannot be proven dead and is
refused; the message names the file so the user can delete it if sure. On
non-POSIX platforms ``os.kill(pid, 0)`` is not a liveness probe (it would
terminate the process), so a foreign-pid lock there is also treated as live.

This is advisory: it neither prevents a non-kct tool from writing the output
nor offers cross-host (NFS) guarantees.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

LOCK_POLICY_ENV = "KCT_ROUTE_LOCK_POLICY"
LOCK_HELD_ENV = "KCT_ROUTE_LOCK_HELD"
POLICIES = ("error", "warn", "ignore")

# Locks this process currently holds, keyed by lock path -> its own record.
# Lets a nested in-process acquire for the same output be a no-op.
_held: dict[Path, dict] = {}


class RouteLockError(RuntimeError):
    """The output is in use by another run, or the lock cannot be created."""


@dataclass(frozen=True)
class RouteLockHandle:
    """What :func:`acquire_route_lock` did; ``path`` is None when no lock is held."""

    path: Path | None
    acquired: bool


def lock_path_for(output: str | Path) -> Path:
    """Lock file for ``output``; relative and absolute spellings agree."""
    destination = Path(output).absolute()
    return destination.with_name(f".{destination.name}.kct-route.lock")


def held_env_value(lock_path: Path | None = None, pid: int | None = None) -> str | None:
    """``KCT_ROUTE_LOCK_HELD`` value naming a lock this process holds.

    Defaults to the single lock this process holds (None when it holds none).
    """
    if lock_path is None:
        if len(_held) != 1:
            return None
        lock_path = next(iter(_held))
    return f"{lock_path}:{os.getpid() if pid is None else pid}"


def _selected_policy(policy: str | None) -> str:
    selected = policy if policy is not None else os.environ.get(LOCK_POLICY_ENV, "error")
    if selected not in POLICIES:
        raise ValueError(
            f"Invalid {LOCK_POLICY_ENV} value {selected!r}; expected error, warn or ignore"
        )
    return selected


def _read_holder(path: Path) -> dict | None:
    """Parse the lock file; None for unreadable or malformed content."""
    try:
        with open(path, encoding="utf-8") as stream:
            text = stream.read(8193)
        if len(text) > 8192:
            return None
        data = json.loads(text)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    host = data.get("hostname")
    if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(host, str):
        return None
    return data


def _holder_is_dead(holder: dict | None) -> bool:
    """True only when the holder provably no longer exists."""
    if holder is None or holder["hostname"] != socket.gethostname():
        return False
    pid = holder["pid"]
    if pid == os.getpid():
        # We hold nothing at this path (checked by the caller), so a record
        # carrying our own pid is a leftover from a recycled pid.
        return True
    if os.name != "posix" or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # PermissionError: exists, owned by someone else
        return False
    return False


def _describe(holder: dict | None) -> str:
    if holder is None:
        return "unknown holder (lock file unreadable or malformed)"
    return f"pid {holder['pid']} on host {holder['hostname']!r}" + (
        f" since {holder['started_at']}" if holder.get("started_at") else ""
    )


def _refusal(output: Path, lock: Path, holder: dict | None) -> str:
    return (
        f"another kct route run appears to be writing {str(output)!r}: lock {str(lock)!r} "
        f"is held by {_describe(holder)}. Wait for it to finish, or delete the lock file "
        f"if you are sure no run is using this output. Set {LOCK_POLICY_ENV}=warn or "
        f"{LOCK_POLICY_ENV}=ignore to bypass this check."
    )


def _inherits_parent_lock(lock: Path) -> bool:
    """True when ``KCT_ROUTE_LOCK_HELD`` names ``lock`` and its live owner is the parent."""
    value = os.environ.get(LOCK_HELD_ENV)
    if not value:
        return False
    name, _, pid_text = value.rpartition(":")
    if not name or not pid_text.isdigit() or Path(name) != lock:
        return False
    holder = _read_holder(lock)
    return holder is not None and holder["pid"] == int(pid_text) == os.getppid()


def acquire_route_lock(output: str | Path, *, policy: str | None = None) -> RouteLockHandle:
    """Take the run lock for ``output`` or raise :class:`RouteLockError`.

    ``policy`` overrides ``KCT_ROUTE_LOCK_POLICY`` (``error`` default). ``ignore``
    touches nothing; ``warn`` reports a live holder or an uncreatable lock on
    stderr and proceeds without the lock. An invalid value raises ValueError.
    """
    selected = _selected_policy(policy)
    if selected == "ignore":
        return RouteLockHandle(None, False)
    destination = Path(output).absolute()
    lock = lock_path_for(destination)
    if lock in _held or _inherits_parent_lock(lock):
        return RouteLockHandle(lock, False)

    record = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    payload = json.dumps(record).encode("utf-8")

    def problem(message: str) -> RouteLockHandle:
        if selected == "warn":
            print(f"Warning: {message}", file=sys.stderr)
            return RouteLockHandle(None, False)
        raise RouteLockError(message)

    for _attempt in range(3):
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_holder(lock)
            if not _holder_is_dead(holder):
                return problem(_refusal(destination, lock, holder))
            # Re-read just before removing so a run that stole it first is kept.
            if _read_holder(lock) != holder:
                continue
            with contextlib.suppress(FileNotFoundError):
                lock.unlink()
            continue
        except OSError as exc:
            return problem(
                f"cannot create route lock {str(lock)!r} ({exc.strerror or exc}); the output "
                f"directory must be writable. Set {LOCK_POLICY_ENV}=ignore to skip locking."
            )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
        except OSError as exc:
            with contextlib.suppress(OSError):
                lock.unlink()
            return problem(f"cannot write route lock {str(lock)!r} ({exc.strerror or exc})")
        _held[lock] = record
        return RouteLockHandle(lock, True)
    return problem(
        f"could not take route lock {str(lock)!r}: it kept changing hands; another run is "
        "starting on the same output."
    )


def release_route_lock(handle: RouteLockHandle) -> None:
    """Remove the lock iff ``handle`` acquired it and the file is still ours."""
    if not handle.acquired or handle.path is None:
        return
    record = _held.pop(handle.path, None)
    if record is None:
        return
    if _read_holder(handle.path) == record:
        with contextlib.suppress(OSError):
            handle.path.unlink()


@contextlib.contextmanager
def route_run_lock(output: str | Path, *, policy: str | None = None) -> Iterator[RouteLockHandle]:
    """Hold the run lock for the block; released on any exit."""
    handle = acquire_route_lock(output, policy=policy)
    try:
        yield handle
    finally:
        release_route_lock(handle)
