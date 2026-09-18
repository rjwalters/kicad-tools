"""Bound how many native KiCad subprocesses may run at once (Issue #5501).

Every ``kicad-cli`` invocation costs roughly 2.3-2.7 GiB of resident memory
(858 native launches measured across the CI bulk pool of run 35099894489:
median sampled peak 2.31 GiB, maximum 2.66 GiB), and the launches are spread
over more than fifty test modules rather than concentrated in a few. Four
pytest workers therefore reach a combined anonymous footprint above the Test
container's 12 GiB cgroup whenever three or four of them happen to overlap --
which is exactly the CONSTRAINT_MEMCG kill recorded for PR #5499 (three native
processes at 2.36/2.36/1.66 GiB) and the 12 GiB ``memory.peak`` with 209
``memory.events.max`` events recorded afterwards.

Isolating modules cannot fix a workload that is spread this thinly, so this
module bounds the *number of native processes that may exist at one time*
instead. It is completely inert unless ``KCT_NATIVE_MAX_CONCURRENCY`` names a
positive integer, so local runs, published releases and every other CI job
behave exactly as before.

Mechanism
---------
``install_from_environment()`` replaces :class:`subprocess.Popen` with a
subclass that, for native argv only, takes one permit from a cross-process
counting semaphore before the child is created and returns it once the child
has been reaped. The semaphore is a directory of ``flock``-ed slot files: the
kernel drops an ``flock`` when the holding file descriptor closes *or* when the
holding process dies, so a killed pytest worker can never strand a permit.

Permits are not re-entrant by design: only the innermost native launch takes
one. A permitted child is given ``KCT_NATIVE_SLOT_HELD=1`` so any native
descendant it spawns reuses the permit already charged for that subtree.

Waiting for a permit is ordinary wall-clock time inside the calling test, so
existing ``pytest-timeout`` values and ``subprocess`` timeouts keep their exact
meaning -- nothing here extends, suppresses or re-arms a deadline. To make a
programming error impossible to turn into a hang, a permit wait longer than
``KCT_NATIVE_SLOT_WAIT_SECONDS`` (default 120 s, versus a measured worst-case
native process lifetime of 2.7 s) gives up and launches unbounded rather than
blocking forever; the event is recorded and reported on stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by platform, not by branch
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "ENV_HELD",
    "ENV_LIMIT",
    "ENV_LOG",
    "ENV_OBSERVER_OUTPUT",
    "ENV_SLOT_DIR",
    "ENV_WAIT_SECONDS",
    "configured_limit",
    "install_from_environment",
    "is_native_launch",
    "native_slot",
]

#: Positive integer: the maximum number of simultaneous native processes.
ENV_LIMIT = "KCT_NATIVE_MAX_CONCURRENCY"
#: Directory holding the semaphore's slot files (shared by every participant).
ENV_SLOT_DIR = "KCT_NATIVE_SLOT_DIR"
#: Set in a permitted child's environment; suppresses nested re-acquisition.
ENV_HELD = "KCT_NATIVE_SLOT_HELD"
#: Fail-open ceiling on a single permit wait, in seconds.
ENV_WAIT_SECONDS = "KCT_NATIVE_SLOT_WAIT_SECONDS"
#: Optional directory for append-only JSONL permit records.
ENV_LOG = "KCT_NATIVE_SLOT_LOG"
#: ``scripts/ci/native_observer.py`` exports this to the workload it supervises.
#: Defaulting to it puts permit records beside that group's process samples, so
#: the gate is only ever logging under the same explicit diagnostics opt-in.
ENV_OBSERVER_OUTPUT = "KCT_NATIVE_OBSERVER_OUTPUT"

DEFAULT_WAIT_SECONDS = 120.0
_MIN_POLL_SECONDS = 0.005
_MAX_POLL_SECONDS = 0.05

# Kept in exact agreement with ``scripts/ci/native_observer.category`` by
# ``tests/test_native_concurrency.py`` so the gate and the independent /proc
# observer can never disagree about what counts as a native workload.
_NATIVE_EXECUTABLE = "kicad-cli"
_NATIVE_PYTHON_WORKER = "_fixed_fill_worker.py"

_installed = False


def _words(args: Any, executable: Any = None) -> list[str]:
    """Normalise Popen's many argv spellings; never retain the result."""
    if isinstance(args, (str, bytes, os.PathLike)):
        candidates: Sequence[Any] = [args]
    elif isinstance(args, Sequence):
        candidates = list(args)
    else:  # pragma: no cover - Popen rejects everything else anyway
        return []
    words = []
    for candidate in candidates:
        if isinstance(candidate, (str, bytes, os.PathLike)):
            words.append(os.fsdecode(candidate))
        else:  # pragma: no cover - Popen rejects everything else anyway
            return []
    if executable is not None:
        words = [os.fsdecode(executable), *words[1:]]
    return words


def is_native_launch(args: Any, executable: Any = None) -> bool:
    """Report whether ``args`` starts a KiCad native workload.

    Recognises the ``kicad-cli`` binary and the KiCad Python zone-fill worker
    launched by :mod:`kicad_tools.zones.placement_fill`, which is a native
    ``pcbnew.ZONE_FILLER`` workload despite running under ``python``.
    """
    words = _words(args, executable)
    if not words:
        return False
    name = Path(words[0]).name
    if name == _NATIVE_EXECUTABLE:
        return True
    if name.startswith("python"):
        return any(Path(word).name == _NATIVE_PYTHON_WORKER for word in words[1:])
    return False


def configured_limit(environ: Any = None) -> int | None:
    """Return the configured positive bound, or ``None`` when inert."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_LIMIT, "")
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


def _wait_ceiling(environ: Any) -> float:
    try:
        value = float(environ.get(ENV_WAIT_SECONDS, ""))
    except (TypeError, ValueError):
        return DEFAULT_WAIT_SECONDS
    return value if value > 0 else DEFAULT_WAIT_SECONDS


def _slot_directory(environ: Any) -> Path:
    configured = environ.get(ENV_SLOT_DIR)
    if configured:
        return Path(configured)
    import tempfile

    return Path(tempfile.gettempdir()) / "kct-native-slots"


def _owner_id(value: str | None) -> str | None:
    """Parameter IDs may embed credentials; retain a stable digest only."""
    if not value:
        return None
    if "[" in value and "]" in value:
        start, end = value.index("["), value.rindex("]")
        digest = hashlib.sha256(value[start : end + 1].encode()).hexdigest()[:16]
        return value[:start] + "[redacted-" + digest + "]" + value[end + 1 :]
    return value


def _record(event: dict[str, Any]) -> None:
    """Append one permit record; diagnostics must never break a workload."""
    directory = os.environ.get(ENV_LOG) or os.environ.get(ENV_OBSERVER_OUTPUT)
    if not directory:
        return
    with suppress(OSError, ValueError):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        # One writer per file, flushed per record, so an abrupt worker death
        # still leaves every record written before it as durable history.
        with (path / f"slots-{os.getpid()}.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "time_ns": time.time_ns(),
                        "pid": os.getpid(),
                        "worker": os.environ.get("PYTEST_XDIST_WORKER"),
                        "owner": _owner_id(os.environ.get("PYTEST_CURRENT_TEST")),
                        **event,
                    }
                )
                + "\n"
            )


def _notice(message: str) -> None:
    # A closed or full stderr must not become a second failure.
    with suppress(Exception):
        print(f"native concurrency gate: {message}", file=sys.stderr, flush=True)


class _Permit:
    """One held slot; releasing closes the fd, which drops the ``flock``."""

    def __init__(self, fd: int | None, index: int | None, waited_s: float) -> None:
        self._fd = fd
        self.index = index
        self.waited_s = waited_s

    @property
    def bounded(self) -> bool:
        """False when the gate failed open instead of holding a slot."""
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with suppress(OSError):
            os.close(fd)
        _record({"event": "release", "slot": self.index})


def _try_slot(directory: Path, index: int) -> int | None:
    fd = os.open(directory / f"slot-{index}", os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _acquire(limit: int, environ: Any) -> _Permit:
    directory = _slot_directory(environ)
    ceiling = _wait_ceiling(environ)
    started = time.monotonic()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Without a shared directory there is no semaphore to take. Launching
        # unbounded is strictly better than failing a test over diagnostics.
        _notice(f"slot directory unavailable ({exc.__class__.__name__}); launching unbounded")
        _record({"event": "unbounded", "reason": "slot_directory_unavailable"})
        return _Permit(None, None, 0.0)
    poll = _MIN_POLL_SECONDS
    while True:
        for index in range(limit):
            try:
                fd = _try_slot(directory, index)
            except OSError as exc:
                _notice(f"slot file unavailable ({exc.__class__.__name__}); launching unbounded")
                _record({"event": "unbounded", "reason": "slot_file_unavailable"})
                return _Permit(None, None, time.monotonic() - started)
            if fd is not None:
                waited = time.monotonic() - started
                _record({"event": "acquire", "slot": index, "waited_s": round(waited, 6)})
                return _Permit(fd, index, waited)
        waited = time.monotonic() - started
        if waited >= ceiling:
            # Measured worst-case native lifetime in this pool is 2.7 s, so a
            # wait this long means a defect, not contention. Proceed unbounded
            # rather than converting it into an unbounded hang.
            _notice(
                f"no slot after {waited:.0f}s (limit {limit}); launching unbounded. "
                "Existing test and subprocess timeouts are unchanged."
            )
            _record({"event": "unbounded", "reason": "wait_ceiling", "waited_s": round(waited, 6)})
            return _Permit(None, None, waited)
        time.sleep(poll)
        poll = min(poll * 2, _MAX_POLL_SECONDS)


@contextmanager
def native_slot(environ: Any = None) -> Iterator[_Permit]:
    """Hold one native permit for the duration of the block."""
    environ = os.environ if environ is None else environ
    limit = configured_limit(environ)
    if limit is None or fcntl is None or environ.get(ENV_HELD):
        yield _Permit(None, None, 0.0)
        return
    permit = _acquire(limit, environ)
    try:
        yield permit
    finally:
        permit.release()


class _GatedPopen(subprocess.Popen):
    """``Popen`` that holds a native permit for the child's whole lifetime."""

    def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
        permit: _Permit | None = None
        limit = configured_limit()
        if (
            limit is not None
            and fcntl is not None
            and not os.environ.get(ENV_HELD)
            and is_native_launch(args, kwargs.get("executable"))
        ):
            permit = _acquire(limit, os.environ)
            if permit.bounded:
                # Charge the whole subtree once: a native descendant of a
                # permitted child reuses this permit instead of deadlocking
                # behind it.
                source = kwargs.get("env")
                kwargs["env"] = {
                    **(os.environ if source is None else source),
                    ENV_HELD: "1",
                }
        self._kct_permit = permit
        try:
            super().__init__(args, *rest, **kwargs)
        except BaseException:
            self._kct_release()
            raise

    def _kct_release(self) -> None:
        permit, self._kct_permit = getattr(self, "_kct_permit", None), None
        if permit is not None:
            permit.release()

    def poll(self) -> int | None:
        code = super().poll()
        if code is not None:
            self._kct_release()
        return code

    def wait(self, timeout: float | None = None) -> int:
        try:
            return super().wait(timeout)
        finally:
            if self.returncode is not None:
                self._kct_release()

    def communicate(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().communicate(*args, **kwargs)
        finally:
            if self.returncode is not None:
                self._kct_release()

    def __exit__(self, *exc: Any) -> Any:
        try:
            return super().__exit__(*exc)
        finally:
            # ``Popen.__exit__`` waits, so the child is gone by now even when
            # the body raised.
            self._kct_release()

    def __del__(self, *args: Any, **kwargs: Any) -> None:
        # Last-resort return for a child that is never waited on. The kernel
        # would release the flock at process exit regardless.
        with suppress(Exception):
            self._kct_release()
        with suppress(Exception):
            super().__del__(*args, **kwargs)


def install_from_environment() -> bool:
    """Install the gate when configured. Idempotent; returns whether active."""
    global _installed
    if _installed:
        return True
    if configured_limit() is None or fcntl is None:
        return False
    if subprocess.Popen is not _GatedPopen:
        subprocess.Popen = _GatedPopen  # type: ignore[misc]
    _installed = True
    return True
