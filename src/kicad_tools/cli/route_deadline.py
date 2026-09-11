"""Process supervision for the route CLI's end-to-end wall-clock budget.

The parent can terminate native calls that cannot service Python signals. A
separate five-second grace permits a raw partial save; it never permits more
routing or validation work. No timed-out output is promoted as canonical.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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


def _paths_alias(left: Path, right: Path) -> bool:
    """Compare names, symlink destinations and existing hardlink identities."""
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


def _output_identity(args) -> dict:
    # Retain the requested stem: derived outputs use this name even if the
    # canonical PCB is a symlink. Resolve only when comparing identities.
    source = Path(args.pcb).absolute()
    output = (
        Path(args.output).absolute() if args.output else source.with_stem(source.stem + "_routed")
    )
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
    return {"input": str(source), "output": str(output), "snapshot_saved": False}


def configure_output(args) -> None:
    if CONTROL_ENV in os.environ:
        record_stage("setup", **_output_identity(args))


def remaining_deadline() -> float | None:
    value = os.environ.get(DEADLINE_ENV)
    return float(value) if value is not None else None


def _deadline_signal(signum, frame) -> None:
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


def _supervise(
    command: list[str], budget: float, control: Path, *, save_seconds=SAVE_SECONDS
) -> int:
    """Run and reap one process group against a single monotonic deadline."""
    deadline = time.monotonic() + budget
    env = dict(os.environ, **{DEADLINE_ENV: str(deadline), CONTROL_ENV: str(control)})
    process = subprocess.Popen(
        command,
        env=env,
        start_new_session=os.name == "posix",
        creationflags=0 if os.name == "posix" else subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
    )
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
    """Keep unbounded in-process behavior; supervise every finite CLI invocation."""
    from .route_cmd import _in_process_main, _route_parser

    argv = list(sys.argv[1:] if argv is None else argv)
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--timeout", type=float)
    timeout_args, _ = probe.parse_known_args(argv)
    budget = timeout_args.timeout
    if budget is None or budget <= 0 or "--help" in argv or "-h" in argv:
        return _in_process_main(argv)
    if not math.isfinite(budget):
        print("Error: --timeout must be finite", file=sys.stderr)
        return 1
    parsed = _route_parser().parse_args(argv)
    try:
        state = _output_identity(parsed)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="kct-route-deadline-") as directory:
        control = Path(directory) / "control.json"
        # Bind output before launching: even startup timeout cannot leave a
        # previous canonical result masquerading as this invocation's output.
        control.write_text(json.dumps(dict(state, stage="startup")))
        return _supervise([sys.executable, "-m", __name__, *argv], budget, control)


def _worker() -> int:
    from .route_cmd import _in_process_main

    signal.signal(signal.SIGTERM, _deadline_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _deadline_signal)
    try:
        return _in_process_main(sys.argv[1:])
    except RouteDeadlineExpired:
        return TIMEOUT_EXIT


if __name__ == "__main__":
    # Use the canonical module identity so the exception type is shared.
    from kicad_tools.cli.route_deadline import _worker as worker

    sys.exit(worker())
