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
A pid too large for ``os.kill`` to represent is malformed, not dead, and so
is an out-of-range ``owner_pgid``. Liveness is zombie-aware on Linux: a pid
or process group whose remaining members are unreaped zombies counts as dead
(a zombie can never write the output again, and a container init that never
reaps adopted orphans must not pin the lock forever).

The record is staged in a temp file and published with ``os.link``, so the
lock name never exists half-written: a run killed anywhere between the
exclusive claim and the payload write leaves no zero-byte lock that later
runs would refuse as malformed.

Stale recovery is serialized per output through a companion ``<lock>.guard``
file held with ``flock``, so two contenders cannot both steal one stale lock;
the guard file is never removed (an unlinked flock guard would let a newcomer
flock a fresh inode beside a waiter on the old one). A supervised run also
records its worker's process group as a co-owner: the lock stays live until
the last process able to write the output has exited, even when the recording
supervisor dies first, and a worker only extends the lock while it still
names its own supervisor as the holder. Lock identity is the output's
resolved parent directory plus its name, so ``out``, ``sub/../out`` and a
path through a symlinked directory all share one lock.

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

# ``os.kill`` raises OverflowError beyond the C ``pid_t`` range; a record
# carrying such a pid is malformed rather than dead (issue #9628 review).
_PID_MAX = 2**31 - 1

try:  # pragma: no cover - exercised by platform, not by branch
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

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
    """Lock file for ``output``; relative, absolute, dot-dot and symlink spellings agree.

    Identity is the parent directory after ``Path.resolve`` -- which collapses
    ``..`` by the *resolved* route rather than lexically -- plus the output's
    name, so ``out`` and ``sub/../out`` share one lock (a nested alias is a
    no-op reentrant acquire instead of an accidental steal-and-release) and a
    path through a symlinked directory cannot fork a second lock beside the
    canonical one (issue #9628 review).
    """
    destination = Path(output).absolute()
    return destination.parent.resolve() / f".{destination.name}.kct-route.lock"


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
    if not 0 < pid <= _PID_MAX:
        # Unrepresentable in os.kill (OverflowError otherwise); treat as
        # malformed so refusal and warn still apply (issue #9628 review).
        return None
    group = data.get("owner_pgid")
    if group is not None and (
        isinstance(group, bool) or not isinstance(group, int) or not 0 < group <= _PID_MAX
    ):
        # Same reasoning as the pid range: os.killpg would OverflowError and
        # the error would escape the selected policy (issue #9628 review).
        return None
    return data


def _proc_fields(pid: int) -> list[bytes] | None:
    """Fields of ``/proc/<pid>/stat`` after the comm column, or None.

    ``[0]`` is the state, ``[2]`` the process group. None where /proc does not
    exist (non-Linux) or the pid raced out of existence.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            stat = stream.read()
    except OSError:
        return None
    # comm can contain spaces and parens; everything after the last ")" is
    # plain whitespace-separated fields.
    fields = stat.rsplit(b")", 1)[-1].split()
    return fields if len(fields) >= 3 else None


_DEAD_STATES = (b"Z", b"X", b"x")


def _linux_live_group_members(group: int) -> bool | None:
    """Whether /proc shows a live (non-zombie) member of ``group``.

    None where /proc is unreadable (non-Linux): the caller keeps the
    ``os.killpg`` answer alone there.
    """
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        fields = _proc_fields(int(name))
        if fields is None or fields[0] in _DEAD_STATES:
            continue  # a zombie can never write the output again
        try:
            if int(fields[2]) == group:
                return True
        except ValueError:
            continue
    return False


def _holder_is_dead(holder: dict | None) -> bool:
    """True only when the holder provably no longer exists."""
    if holder is None or holder["hostname"] != socket.gethostname():
        return False
    # A supervised run names its worker's process group as a co-owner: that
    # group can still be writing the output after the recording supervisor
    # died, so a live group keeps the lock held (issue #9628 review).
    group = holder.get("owner_pgid")
    if os.name == "posix" and isinstance(group, int) and not isinstance(group, bool) and group > 0:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            pass  # every writer in the group is gone; the pid check decides
        except OSError:
            return False  # exists but is not ours to signal
        else:
            # killpg(0) still answers while only unreaped zombies remain: a
            # container init that never reaps adopted orphans would pin the
            # lock forever, so require a live member where /proc allows the
            # check (issue #9628 review).
            live = _linux_live_group_members(group)
            if live is not False:
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
    fields = _proc_fields(pid)
    return fields is not None and fields[0] in _DEAD_STATES


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


@contextlib.contextmanager
def _steal_guard(lock: Path) -> Iterator[bool]:
    """Serialize stale-lock recovery per output (issue #9628 review).

    Recovery is a read-decide-unlink sequence; unserialized, two contenders
    can both decide "dead" and one unlinks a lock the other just re-acquired.
    The guard file is never removed: an unlinked flock guard would let a
    newcomer flock a fresh inode beside a waiter still holding the old one.
    Yields False where ``flock`` is unavailable (non-POSIX best effort).
    """
    if fcntl is None:
        yield False
        return
    path = lock.with_name(lock.name + ".guard")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def extend_lock_owner(lock_path: Path, *, pgid: int, expected_pid: int | None = None) -> bool:
    """Record ``pgid`` as a co-owner keeping the lock at ``lock_path`` live.

    A supervised run writes its board from a separate process group; when the
    supervisor is killed, the recorded pid alone looks dead while the worker
    can still save (issue #9628 review). ``expected_pid`` -- the supervisor's
    own pid, or the one a worker inherited via ``KCT_ROUTE_LOCK_HELD`` --
    refuses the rewrite when the lock at this path now belongs to a different
    run: an orphaned worker must never adopt a replacement lock. Returns
    whether the record is (still) this run's to extend. Best effort: a failed
    extension write leaves the record exactly as it was.
    """
    with contextlib.ExitStack() as stack:
        # The read-modify-write runs under the steal guard so it cannot
        # interleave with a contender stealing the very record being extended;
        # where the guard cannot be opened the rewrite stays best effort.
        with contextlib.suppress(OSError):
            stack.enter_context(_steal_guard(lock_path))
        holder = _read_holder(lock_path)
        if holder is None:
            return False  # gone or unreadable: no longer this run's lock
        if expected_pid is not None and holder.get("pid") != expected_pid:
            return False  # stolen and re-taken by another run
        if holder.get("owner_pgid") == pgid:
            return True
        holder["owner_pgid"] = pgid
        try:
            temporary = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.owner.tmp")
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(holder, stream)
            os.replace(temporary, lock_path)
        except OSError:
            pass
        return True


def acquire_route_lock(output: str | Path, *, policy: str | None = None) -> RouteLockHandle:
    """Take the run lock for ``output`` or raise :class:`RouteLockError`.

    ``policy`` overrides ``KCT_ROUTE_LOCK_POLICY`` (``error`` default). ``ignore``
    touches nothing; ``warn`` reports a live holder or an uncreatable lock on
    stderr and proceeds without the lock. An invalid value raises ValueError.
    The record is staged in a temp file and published with :func:`os.link`, so
    the lock name never exists half-written: a run killed anywhere inside
    acquisition leaves no zero-byte lock that later runs would refuse as
    malformed (issue #9628 review).
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
    temporary = lock.with_name(f"{lock.name}.{os.getpid()}.tmp")

    def problem(message: str) -> RouteLockHandle:
        if selected == "warn":
            print(f"Warning: {message}", file=sys.stderr)
            return RouteLockHandle(None, False)
        raise RouteLockError(message)

    for _attempt in range(3):
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "wb") as stream:
                stream.write(payload)
            # link(2) is the exclusive publish: it fails with FileExistsError
            # when the name is taken and never exposes a partial file.
            os.link(temporary, lock)
        except FileExistsError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            try:
                with _steal_guard(lock):
                    if not lock.exists():
                        # Recovered by a contender that held the guard first;
                        # retry the create so the new holder is checked normally.
                        continue
                    holder = _read_holder(lock)
                    if not _holder_is_dead(holder):
                        return problem(_refusal(destination, lock, holder))
                    # Only one contender at a time can be inside this window, so
                    # the unlink cannot race a rival's just-acquired lock away
                    # (issue #9628 review).
                    with contextlib.suppress(FileNotFoundError):
                        lock.unlink()
            except OSError as exc:
                # The guard or the recovery itself failed (e.g. a read-only
                # directory): route it through the selected policy instead of
                # escaping as a raw PermissionError (issue #9628 review).
                return problem(
                    f"cannot check route lock {str(lock)!r} ({exc.strerror or exc}); fix the "
                    "directory permissions, or delete the lock file if you are sure no "
                    "run is using this output."
                )
            continue
        except OSError as exc:
            with contextlib.suppress(OSError):
                temporary.unlink()
            return problem(
                f"cannot create route lock {str(lock)!r} ({exc.strerror or exc}); the output "
                f"directory must be writable. Set {LOCK_POLICY_ENV}=ignore to skip locking."
            )
        with contextlib.suppress(OSError):
            temporary.unlink()
        _held[lock] = record
        return RouteLockHandle(lock, True)
    return problem(
        f"could not take route lock {str(lock)!r}: it kept changing hands; another run is "
        "starting on the same output."
    )


def release_route_lock(handle: RouteLockHandle) -> None:
    """Remove the lock iff ``handle`` acquired it and the file is still ours.

    Ownership is pid + hostname, not full-record equality: a held lock may
    legitimately gain fields after acquisition (``extend_lock_owner`` records
    the supervised worker's group as a co-owner, issue #9628 review).
    """
    if not handle.acquired or handle.path is None:
        return
    record = _held.pop(handle.path, None)
    if record is None:
        return
    holder = _read_holder(handle.path)
    if (
        holder is not None
        and holder.get("pid") == record.get("pid")
        and holder.get("hostname") == record.get("hostname")
    ):
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
