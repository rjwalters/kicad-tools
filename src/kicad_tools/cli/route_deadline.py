"""Process supervision for the route CLI's end-to-end wall-clock budget.

The parent can terminate native calls that cannot service Python signals. A
separate five-second grace permits a raw partial save; it never permits more
routing or validation work. No timed-out output is promoted as canonical.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from kicad_tools.core.route_run_lock import (
    LOCK_HELD_ENV,
    RouteLockError,
    acquire_route_lock,
    extend_lock_owner,
    held_env_value,
    release_route_lock,
)
from kicad_tools.router.access_witness import ACCESS_WITNESS_SIDECAR_SUFFIX
from kicad_tools.router.reporting import RouteAttemptResult

DEADLINE_ENV = "KCT_ROUTE_INVOCATION_DEADLINE"
CONTROL_ENV = "KCT_ROUTE_DEADLINE_CONTROL"
SAVE_SECONDS = 5.0
TIMEOUT_EXIT = 124


class RouteDeadlineExpired(BaseException):
    """Unwind routing even through fallback handlers catching Exception."""


def _read_control(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {"stage": "startup"}
    except (OSError, ValueError):
        return {"stage": "startup"}


def record_stage(stage: str, **fields) -> None:
    """Publish only small control metadata; no geometry or router serialization."""
    name = os.environ.get(CONTROL_ENV)
    if not name:
        return
    path = Path(name)
    state = _read_control(path)
    state.update(fields, stage=stage)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def current_stage() -> str | None:
    """Return the stage currently published to the control file.

    ``None`` when no supervisor is attached (``CONTROL_ENV`` unset), which is
    also the "nothing to restore" signal :func:`restore_stage` keys on.
    """
    name = os.environ.get(CONTROL_ENV)
    if not name:
        return None
    stage = _read_control(Path(name)).get("stage")
    return stage if isinstance(stage, str) else None


@contextlib.contextmanager
def restore_stage():
    """Snapshot the published stage and restore it when the block exits.

    Issue #5266.  Some work is a *transient excursion* out of the stage the
    invocation is really in -- most importantly a best-so-far checkpoint
    write, which routes through ``_write_routed_pcb`` and therefore stamps
    ``serialization`` even though the router is mid-search.  Without a
    restore, the very first checkpoint leaves ``serialization`` published for
    the rest of the run and a later timeout is misattributed to it (the stale
    label that made the #5164 Board 07 evidence unreadable).

    The restore is deliberately *not* a substitute for the interrupt record:
    :func:`_deadline_signal` stamps ``interrupted_stage`` at signal time and
    :func:`_supervise` prefers that field, so a timeout that genuinely lands
    *inside* the checkpoint write is still reported as ``serialization``.
    """
    previous = current_stage()
    try:
        yield previous
    finally:
        if previous is not None:
            record_stage(previous)


def _paths_alias(left: Path, right: Path) -> bool:
    """Compare names, symlink destinations and existing hardlink identities."""
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def _output_identity(args) -> dict:
    # Retain the requested stem: derived outputs use this name even if the
    # canonical PCB is a symlink. Resolve only when comparing identities.
    source = Path(args.pcb).absolute()
    # Issue #6090: under --lint-gate, args.output names a staging file; the
    # supervisor must still see (and quarantine) the requested output.
    requested = getattr(args, "_lint_gate_output", None) or args.output
    output = Path(requested).absolute() if requested else source.with_stem(source.stem + "_routed")
    protected = set()
    for board in (source, source.resolve()):
        protected.add(board)
        protected.update(
            board.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru", ".kicad_prl")
        )
    partial = output.with_stem(output.stem + "_partial")
    targets = {
        output,
        output.with_suffix(output.suffix + ".tmp"),
        partial,
        partial.with_suffix(partial.suffix + ".tmp"),
        output.with_suffix(".timeout.json"),
        *(output.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru", ".kicad_prl")),
        output.with_name(output.stem + "_placement_diff.json"),
        output.with_name(output.stem + "_placement_delta.json"),
        # The escalation pipeline removes these stale output siblings.
        *(
            output.with_name(f"{output.stem}_{layers}layer{suffix}")
            for layers in (4, 6)
            for suffix in (".kicad_pcb", ".kicad_prl")
        ),
    }
    for target in sorted(targets):
        for original in sorted(protected):
            if _paths_alias(target, original):
                raise ValueError(
                    "Timed routing requires a separate output path and derived artifacts: "
                    f"{target} aliases protected input {original}"
                )
    return {
        "input": str(source),
        "output": str(output),
        "snapshot_saved": False,
        # Issue #9628 review: a --dry-run writes nothing, so its timeout
        # handling must not quarantine a canonical board another run owns.
        "writes_output": not getattr(args, "dry_run", False),
    }


def configure_output(args) -> None:
    if CONTROL_ENV in os.environ:
        record_stage("setup", **_output_identity(args))


def remaining_deadline() -> float | None:
    value = os.environ.get(DEADLINE_ENV)
    return float(value) if value is not None else None


# Issue #6324: a ``RouteDeadlineExpired`` raised from a signal handler is
# discarded by the interpreter when the signal lands inside a ``__del__``
# ("Exception ignored in ...").  Recording that the deadline fired lets the
# route flows' stage-boundary polls re-raise it, so a swallowed deadline still
# ends in the partial-save handler instead of routing on until the SIGKILL.
_deadline_state: dict[str, bool] = {"fired": False}


def deadline_fired() -> bool:
    """Whether the supervisor's deadline signal has been delivered."""
    return _deadline_state["fired"]


def reset_deadline_state() -> None:
    """Forget a delivered deadline (end of an in-process invocation)."""
    _deadline_state["fired"] = False


def _deadline_signal(signum, frame) -> None:
    # Record first: everything after this line may itself be interrupted.
    _deadline_state["fired"] = True
    stage = _read_control(Path(os.environ[CONTROL_ENV])).get("stage", "routing")
    record_stage(
        stage, interrupted_stage=stage, interrupted_function=frame.f_code.co_name if frame else None
    )
    raise RouteDeadlineExpired()


def _signal_group(process: subprocess.Popen, *, kill: bool) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
    elif kill:  # pragma: no cover - Windows process-tree termination
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    else:  # pragma: no cover - Windows console process group
        process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]


def _fresh_partial(partial: Path, launched_at: float) -> bool:
    """Whether ``partial`` is a complete board written after ``launched_at``."""
    try:
        if partial.with_suffix(partial.suffix + ".tmp").exists():
            return False
        return partial.stat().st_mtime >= launched_at
    except OSError:
        return False


def _supervise(
    command: list[str], budget: float, control: Path, *, save_seconds=SAVE_SECONDS
) -> int:
    """Run and reap one process group against a single monotonic deadline."""
    deadline = time.monotonic() + budget
    launched_at = time.time()
    env = dict(os.environ, **{DEADLINE_ENV: str(deadline), CONTROL_ENV: str(control)})
    # Issue #9628: tell the worker (and anything it spawns) this run already
    # holds the output's lock, so they never refuse against their own parent.
    held = held_env_value()
    if held is not None:
        env[LOCK_HELD_ENV] = held
    process = subprocess.Popen(
        command,
        env=env,
        start_new_session=os.name == "posix",
        creationflags=0 if os.name == "posix" else subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
    )
    # Issue #9628 review: the worker's process group writes the board and can
    # outlive this supervisor; naming it as a co-owner keeps the lock live
    # until the last process able to write the output has exited.
    held_path, _, held_pid = (held or "").rpartition(":")
    if held_path and held_pid.isdigit() and os.name == "posix":
        extend_lock_owner(Path(held_path), pgid=process.pid)
    timed_out = False
    group_killed = False
    try:
        try:
            result = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            timed_out = result == TIMEOUT_EXIT
            if not timed_out:
                return result
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_group(process, kill=False)
            try:
                process.wait(timeout=save_seconds)
            except subprocess.TimeoutExpired:
                _signal_group(process, kill=True)
                group_killed = True
                process.wait()
    except KeyboardInterrupt:
        _signal_group(process, kill=False)
        try:
            process.wait(timeout=save_seconds)
        except subprocess.TimeoutExpired:
            _signal_group(process, kill=True)
            group_killed = True
            process.wait()
        return 130
    finally:
        # Also terminate descendants left behind by a worker that exited first.
        # Do not signal the same numeric group again after hard-kill + reap:
        # it no longer identifies a live worker group and may be recycled.
        if not group_killed:
            _signal_group(process, kill=True)
        process.wait()
    if timed_out:
        state = _read_control(control)
        state.setdefault("snapshot_saved", False)
        state["stage"] = state.get("interrupted_stage", state.get("stage", "startup"))
        state.update(
            status="partial",
            reason="timeout",
            timeout_seconds=budget,
            serialization_budget_seconds=save_seconds,
            manufacturing_ready=False,
        )
        output_name = state.get("output")
        if output_name:
            output = Path(output_name)
            if state.get("writes_output", True):
                # Issue #6308: the copper-bearing board is ``snapshot`` (the raw
                # ``<stem>_partial``); ``unverified_output`` is only the quarantined
                # canonical output and may hold no copper.  A worker hard-killed
                # after writing the partial but before recording it must still be
                # named, so recover it here -- only if written by THIS invocation
                # and not a torn ``.tmp`` leftover.
                partial = output.with_stem(output.stem + "_partial")
                if not state.get("snapshot_saved") and _fresh_partial(partial, launched_at):
                    state["snapshot"] = str(partial)
                    state["snapshot_saved"] = True
                # Preserve even a pre-existing successful result, but remove its
                # canonical name so it cannot masquerade as this invocation's result.
                if output.exists():
                    fd, name = tempfile.mkstemp(
                        prefix=output.stem + "_timeout_unverified_",
                        suffix=output.suffix,
                        dir=output.parent,
                    )
                    os.close(fd)
                    output.replace(name)
                    state["unverified_output"] = name
                    # Issue #8258: the worker prints the checkpoint as the user
                    # typed it (possibly relative); ``output`` is absolute.
                    checkpoint = state.get("checkpoint")
                    if isinstance(checkpoint, str) and checkpoint:
                        if Path(checkpoint).resolve() == output.resolve():
                            state["checkpoint"] = name
                    # Keep authored context beside the quarantined checkpoint. It
                    # remains unverified; these copies do not authorize promotion.
                    sidecars = []
                    for suffix in (".kicad_pro", ".kicad_dru"):
                        source = output.with_suffix(suffix)
                        if source.exists():
                            destination = Path(name).with_suffix(suffix)
                            try:
                                shutil.copyfile(source, destination)
                                sidecars.append(str(destination))
                            except OSError as exc:
                                state.setdefault("quarantine_context_errors", []).append(str(exc))
                    # Issue #5639: the access-witness sidecar is stem-keyed, so it
                    # names the board it describes.  MOVE it with the quarantined
                    # checkpoint -- left behind under the canonical stem it would
                    # describe an output that no longer exists, and a reader
                    # discovering it from the quarantined board would find nothing.
                    witness_source = output.parent / f"{output.stem}{ACCESS_WITNESS_SIDECAR_SUFFIX}"
                    if witness_source.exists():
                        quarantined = Path(name)
                        witness_target = (
                            quarantined.parent
                            / f"{quarantined.stem}{ACCESS_WITNESS_SIDECAR_SUFFIX}"
                        )
                        try:
                            os.replace(witness_source, witness_target)
                            sidecars.append(str(witness_target))
                            if state.get("access_witness") == str(witness_source):
                                state["access_witness"] = str(witness_target)
                        except OSError as exc:
                            state.setdefault("quarantine_context_errors", []).append(str(exc))
                    state["unverified_sidecars"] = sidecars
            # else (issue #9628 review): a --dry-run writes nothing; the
            # canonical board and its sidecars belong to whichever run holds
            # the output and are left exactly as found.
            report = output.with_suffix(".timeout.json")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(state, indent=2) + "\n")
            print(
                f"PARTIAL: routing deadline exceeded during {state.get('stage')}; report: {report}",
                file=sys.stderr,
            )
        else:
            print(
                "PARTIAL: routing deadline exceeded during startup; no snapshot available",
                file=sys.stderr,
            )
        return TIMEOUT_EXIT
    raise AssertionError("unreachable")


def run(argv: list[str] | None = None) -> int:
    """Preserve the public CLI integer exit contract."""
    return run_attempt(argv).exit_code


def _lock_target(parsed) -> Path | None:
    """Output a run would write, or None when the run writes nothing (``--dry-run``)."""
    if getattr(parsed, "dry_run", False):
        return None
    source = Path(parsed.pcb).absolute()
    requested = getattr(parsed, "_lint_gate_output", None) or parsed.output
    return Path(requested).absolute() if requested else source.with_stem(source.stem + "_routed")


def _probe_lock_target(argv: list[str]) -> Path | None:
    """Lock target for an in-process run; None for help, bad args or dry runs.

    Argument errors are left for the real parse to report once, so the probe's
    own usage message is swallowed.
    """
    from .route_cmd import _route_parser

    if "--help" in argv or "-h" in argv:
        return None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parsed, _ = _route_parser().parse_known_args(argv)
    except SystemExit:
        return None
    return _lock_target(parsed)


def _early_sigterm_net(signum, frame) -> None:
    raise KeyboardInterrupt


def is_lock_sigterm_net(handler: object) -> bool:
    """Whether ``handler`` is the lock-safety net another flow may replace.

    Flows that install their own deliberate SIGTERM handler (the lint gate)
    defer to whatever is installed; this net only covers the window between
    lock acquisition and the first deliberate handler, so it must not win
    that deference (issue #9628 review).
    """
    return handler is _early_sigterm_net


@contextlib.contextmanager
def _sigterm_as_interrupt():
    """Let a SIGTERM to this process unwind like Ctrl-C.

    The default action would kill the process outright, skipping the lock
    release — and, for a supervisor, orphaning the worker, which would keep
    writing the output with no lock held. ``_supervise`` already turns
    KeyboardInterrupt into "terminate the worker group"; an unsupervised run
    instead exits 130 from ``_with_run_lock``, either way after the lock is
    released (issue #9628 review).
    """
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    ):
        yield
        return

    signal.signal(signal.SIGTERM, _early_sigterm_net)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _with_run_lock(
    output: Path | None, body: Callable[[], RouteAttemptResult]
) -> RouteAttemptResult:
    """Run ``body()`` holding the output's run lock (issue #9628).

    A refused lock exits 1 before anything touches the output. SIGTERM is
    unwound like Ctrl-C on every path (supervised or not) for as long as the
    lock is held, so a supervisor-less run cannot die with the lock in place
    during the window before the routing signal handler takes over (issue
    #9628 review).
    """
    if output is None:
        return body()
    try:
        handle = acquire_route_lock(output)
    except (RouteLockError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return RouteAttemptResult(1)
    try:
        with _sigterm_as_interrupt():
            return body()
    except KeyboardInterrupt:
        return RouteAttemptResult(130)
    finally:
        release_route_lock(handle)


def run_attempt(argv: list[str] | None = None) -> RouteAttemptResult:
    """Return metadata from this invocation, including a supervised worker."""
    from .route_cmd import _in_process_main, _route_parser
    from .route_placement import capture_disposition, disposition_from_control, finish

    argv = list(sys.argv[1:] if argv is None else argv)
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--timeout", type=float)
    timeout_args, _ = probe.parse_known_args(argv)
    budget = timeout_args.timeout
    if budget is None or budget <= 0 or "--help" in argv or "-h" in argv:

        def in_process() -> RouteAttemptResult:
            with capture_disposition() as values:
                code = _in_process_main(argv)
                return RouteAttemptResult(code, values[-1] if values else None)

        return _with_run_lock(_probe_lock_target(argv), in_process)
    if not math.isfinite(budget):
        print("Error: --timeout must be finite", file=sys.stderr)
        return RouteAttemptResult(1)
    parsed = _route_parser().parse_args(argv)
    try:
        state = _output_identity(parsed)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return RouteAttemptResult(1)

    def supervised() -> RouteAttemptResult:
        with tempfile.TemporaryDirectory(prefix="kct-route-deadline-") as directory:
            control = Path(directory) / "control.json"
            # Bind output before launching: even startup timeout cannot leave a
            # previous canonical result masquerading as this invocation's output.
            control.write_text(json.dumps(dict(state, stage="startup")))
            # Capture report identities before the worker can replace stale files.
            for option, attribute in (
                ("complete_report", "_placement_report_before"),
                ("export_failed_nets", "_placement_failed_before"),
            ):
                value = getattr(parsed, option, None)
                path = Path(value) if value else None
                setattr(parsed, attribute, path.stat() if path and path.exists() else None)
            code = _supervise([sys.executable, "-m", __name__, *argv], budget, control)
            disposition = disposition_from_control(_read_control(control))
            if code == TIMEOUT_EXIT and disposition is not None:
                parsed._placement_disposition = disposition
                parsed._placement_output = Path(state["output"])
                output = parsed._placement_output
                # Even a checkpoint written before unwinding is unverified.
                parsed._placement_output_before = output.stat() if output.exists() else None
                finish(parsed, code)
            if code == TIMEOUT_EXIT:
                # After ``finish()``: it may write the ``--export-failed-nets``
                # target itself, and the note must describe the final state.
                _note_skipped_failed_export(parsed, state)
            return RouteAttemptResult(code, disposition)

    return _with_run_lock(_lock_target(parsed), supervised)


def _note_skipped_failed_export(parsed, state: dict) -> None:
    """Issue #8258: say so when ``--export-failed-nets`` wrote nothing on timeout."""
    target = getattr(parsed, "export_failed_nets", None)
    if not target:
        return
    path = Path(target)
    if path.exists() and path.stat() != getattr(parsed, "_placement_failed_before", None):
        return
    report = Path(state["output"]).with_suffix(".timeout.json")
    if not report.exists():
        return
    try:
        data = json.loads(report.read_text())
        output = Path(state["output"])
        sidecar = output.with_stem(output.stem + "_partial").with_suffix(".search_failures.json")
        note = {
            "requested": str(path.absolute()),
            "written": False,
            "reason": "routing deadline exceeded; the failed-net set of an "
            "unverified partial result is not exported",
        }
        # timeout.json never names a path that does not exist: the deadline can
        # land before the search-failures sidecar is written.
        if sidecar.exists():
            note["search_failures"] = str(sidecar)
        else:
            note["search_failures"] = None
            note["search_failures_reason"] = (
                "no search-failures sidecar was written before the deadline"
            )
        data["export_failed_nets"] = note
        report.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError):
        pass


def _extend_inherited_lock_owner() -> None:
    """Name this worker's group as co-owner of the inherited lock (#9628 review).

    The supervisor names the group itself right after launch; this covers a
    supervisor that died inside that window, so the lock stays live until the
    last process able to write the output has exited.
    """
    if os.name != "posix":
        return
    held = os.environ.get(LOCK_HELD_ENV)
    if not held:
        return
    name, _, pid_text = held.rpartition(":")
    if pid_text.isdigit():
        extend_lock_owner(Path(name), pgid=os.getpgrp())


def _worker() -> int:
    from .route_cmd import _in_process_main

    signal.signal(signal.SIGTERM, _deadline_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _deadline_signal)
    _extend_inherited_lock_owner()
    try:
        return _in_process_main(sys.argv[1:])
    except RouteDeadlineExpired:
        return TIMEOUT_EXIT


if __name__ == "__main__":
    # Use the canonical module identity so the exception type is shared.
    from kicad_tools.cli.route_deadline import _worker as worker

    sys.exit(worker())
