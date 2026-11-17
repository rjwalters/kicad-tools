#!/usr/bin/env python3
"""Capture Board07's TMDS_D1 coupled diff-pair search as a replayable fixture.

Issue #6816 (blocks #5333).  With ``--differential-pairs`` on, every Board07
pair's coupled search fails, and the only way to see it used to be a full
~20-minute recipe run.  This script records the native inputs of ONE pair's
search (both the corridor phase and the open phase) and writes them to
``tests/fixtures/board07_coupled_tmds_d1/``, where
``tests/test_coupled_search_replay_board07.py`` replays them in seconds.

Usage (from the repository root, C++ backend built -- ``kct build-native``):

    uv run python boards/07-matchgroup-test/capture_coupled_search.py

Options: ``--pair`` (default ``TMDS_D1``), ``--fixture-dir``, ``--work-dir``
(default: a temporary directory, removed afterwards).

What it does:

1. Generates the unrouted board exactly as the recipe does
   (``generate_design.py <dir> --step pcb --seed 42`` with
   ``PYTHONHASHSEED=42``).
2. Calls the recipe's own ``route_pcb()`` with its ``subprocess`` module
   shimmed, so the ``kct route`` argv (routing-frame translation and net-class
   sidecar included) is taken from the recipe rather than hand-copied.
3. Runs that argv plus ``--differential-pairs`` through the ``kct route``
   entry point **in this process** (the ``--timeout`` supervisor would put the
   router in a child process, so ``route_cmd.main`` is pointed at the worker's
   ``_in_process_main``; only the deadline supervision is skipped).
4. While the target pair is being routed, swaps ``router_cpp.Grid3D`` /
   ``router_cpp.CoupledPathfinder`` for recording subclasses
   (:mod:`kicad_tools.router.coupled_capture`) that log every native call.
   As soon as the pair returns, a ``CaptureStop`` sentinel ends the run, so no
   board route happens.

Production code is untouched: no hook, env var or flag.  The recorder exists
only for the duration of the target pair.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_FIXTURE = REPO / "tests" / "fixtures" / "board07_coupled_tmds_d1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


class _RouteArgv(BaseException):  # noqa: N818 - control flow
    def __init__(self, cmd: list[str], env: dict[str, str]):
        super().__init__("route argv captured")
        self.cmd = cmd
        self.env = env


def _recipe_route_argv(work: Path) -> tuple[list[str], dict[str, str], Path, Path]:
    """Generate the unrouted board and return the recipe's ``kct route`` argv."""
    subprocess.run(
        [
            sys.executable,
            str(HERE / "generate_design.py"),
            str(work),
            "--step",
            "pcb",
            "--seed",
            "42",
        ],
        check=True,
        env={**os.environ, "PYTHONHASHSEED": "42"},
    )
    spec = importlib.util.spec_from_file_location(
        "board07_generate_design", HERE / "generate_design.py"
    )
    assert spec is not None and spec.loader is not None
    gd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gd)

    def fake_run(cmd, **kwargs):
        raise _RouteArgv(list(cmd), dict(kwargs.get("env") or {}))

    gd.subprocess = types.SimpleNamespace(run=fake_run)
    pcb = work / "matchgroup_test.kicad_pcb"
    try:
        gd.route_pcb(pcb, work / "matchgroup_test_routed.kicad_pcb")
    except _RouteArgv as captured:
        cmd, env = captured.cmd, captured.env
    else:
        raise RuntimeError("route_pcb() did not invoke kct route")
    if cmd[1:4] != ["-m", "kicad_tools.cli", "route"]:
        raise RuntimeError(f"unexpected recipe command: {cmd[:4]}")
    if env.get("PYTHONHASHSEED") != "42":
        raise RuntimeError("recipe no longer pins PYTHONHASHSEED=42")
    sidecar = Path(cmd[cmd.index("--net-class-map") + 1])
    route_input = Path(cmd[4])
    return cmd[4:], env, route_input, sidecar


def _plain(value):
    """JSON-friendly form of a ``_try_cpp_route_coupled`` keyword value."""
    if all(hasattr(value, a) for a in ("x", "y", "layer")):
        return [int(value.x), int(value.y), int(value.layer)]
    return value


def _normalise(argv: list[str], work: Path) -> list[str]:
    return [a.replace(str(work), "<WORK>") for a in argv]


def capture(pair_name: str, fixture_dir: Path, work: Path) -> dict:
    import numpy as np

    from kicad_tools.cli import main as kct_main
    from kicad_tools.cli import route_cmd
    from kicad_tools.router import cpp_backend
    from kicad_tools.router import diffpair_routing as dr
    from kicad_tools.router.coupled_capture import (
        CaptureStop,
        NativeCallLog,
        diagnostics_of,
        encode_log,
        recording_native_types,
        save_fixture,
    )

    if not cpp_backend.is_cpp_available():
        raise SystemExit("C++ router backend not available -- run `uv run kct build-native`")
    rc = cpp_backend.router_cpp

    argv, _env, route_input, sidecar = _recipe_route_argv(work)
    argv = [*argv, "--differential-pairs"]
    print(f"[capture] kct route {' '.join(_normalise(argv, work))}")

    log = NativeCallLog()
    state: dict = {"reached": False, "phases": [], "classes": [], "error": None}
    originals = {
        "pair": dr.DiffPairRouter.route_differential_pair_coupled,
        "prepass": dr.DiffPairRouter.route_all_with_diffpairs,
        "try_cpp": dr.CoupledPathfinder._try_cpp_route_coupled,
        "classify": dr.classify_coupled_pair_outcome,
        "route_main": route_cmd.main,
    }

    def try_cpp(self, **kwargs):
        corridor = kwargs.get("corridor")
        phase = {
            "phase": "corridor" if corridor is not None else "open",
            "python_kwargs": {k: _plain(v) for k, v in kwargs.items() if k != "corridor"},
            "corridor_cells": None if corridor is None else len(corridor),
            "corridor": None if corridor is None else sorted(corridor),
            "pathfinder": {
                "target_spacing_cells": self.target_spacing_cells,
                "min_spacing_cells": self.min_spacing_cells,
                "trace_half_width_cells": self._trace_half_width_cells,
                "via_extra_cells": self._via_extra_cells,
                "spacing_penalty_factor": self.spacing_penalty_factor,
                "heuristic_weight": self.heuristic_weight,
                "grid": {
                    "cols": self.grid.cols,
                    "rows": self.grid.rows,
                    "layers": self.grid.num_layers,
                    "resolution": self.grid.resolution,
                },
                "rules": {
                    k: v
                    for k, v in sorted(getattr(self.rules, "__dict__", {}).items())
                    if isinstance(v, (int, float, bool, str)) or v is None
                },
            },
        }
        handled = originals["try_cpp"](self, **kwargs)
        phase["python_diagnostics"] = {
            "iterations": self.last_iterations,
            "best_progress": self.last_best_progress,
            "iteration_limited": self.last_iteration_limited,
            "timeout_exceeded": self.last_timeout_exceeded,
            "rejections": dict(sorted(self.last_rejections.items())),
            "success": bool(handled and handled[1] is not None),
        }
        state["phases"].append(phase)
        return handled

    def classify(**kwargs):
        result = originals["classify"](**kwargs)
        state["classes"].append({"class": result, **kwargs})
        return result

    def pair_wrapper(self, pair, *args, **kwargs):
        if pair.name != pair_name:
            return originals["pair"](self, pair, *args, **kwargs)
        state["reached"] = True
        recording = recording_native_types(rc, log)
        saved = {name: getattr(rc, name) for name in recording}
        for name, cls in recording.items():
            setattr(rc, name, cls)
        dr.CoupledPathfinder._try_cpp_route_coupled = try_cpp
        dr.classify_coupled_pair_outcome = classify
        t0 = time.monotonic()
        try:
            originals["pair"](self, pair, *args, **kwargs)
        finally:
            for name, cls in saved.items():
                setattr(rc, name, cls)
            dr.CoupledPathfinder._try_cpp_route_coupled = originals["try_cpp"]
            dr.classify_coupled_pair_outcome = originals["classify"]
            state["pair_seconds"] = time.monotonic() - t0
        raise CaptureStop(f"captured {pair_name}")

    def prepass_wrapper(self, *args, **kwargs):
        result = originals["prepass"](self, *args, **kwargs)
        if not state["reached"]:
            raise CaptureStop(f"diff-pair pre-pass finished without reaching {pair_name}")
        return result

    dr.DiffPairRouter.route_differential_pair_coupled = pair_wrapper
    dr.DiffPairRouter.route_all_with_diffpairs = prepass_wrapper
    route_cmd.main = lambda a=None: route_cmd._in_process_main(a)
    t_run = time.monotonic()
    stop: CaptureStop | None = None
    try:
        kct_main(["route", *argv])
    except CaptureStop as exc:
        stop = exc
    finally:
        dr.DiffPairRouter.route_differential_pair_coupled = originals["pair"]
        dr.DiffPairRouter.route_all_with_diffpairs = originals["prepass"]
        route_cmd.main = originals["route_main"]
    run_seconds = time.monotonic() - t_run
    if stop is None or not state["reached"]:
        raise SystemExit(f"capture failed: {stop or 'route finished without the sentinel'}")

    calls, arrays = encode_log(log, rc.__name__)
    native_routes = [
        diagnostics_of(e["result"])
        for e in log.entries
        if isinstance(e, dict) and e.get("method") == "route" and "result" in e
    ]
    if len(native_routes) != len(state["phases"]):
        raise SystemExit(
            f"recorded {len(native_routes)} native searches but {len(state['phases'])} phases"
        )
    phases = []
    for index, (phase, native) in enumerate(zip(state["phases"], native_routes, strict=True)):
        corridor = phase.pop("corridor")
        if corridor is not None:
            arrays[f"corridor_{index}"] = np.asarray(corridor, dtype=np.int32).reshape(-1, 2)
        phase["corridor_key"] = None if corridor is None else f"corridor_{index}"
        phase["native_diagnostics"] = native
        phases.append(phase)

    native_new = [e for e in log.entries if isinstance(e, dict) and e["op"] == "new"]
    manifest = {
        "issue": 6816,
        "pair": pair_name,
        "provenance": {
            "source_commit": _git("rev-parse", "HEAD"),
            "source_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
            "input_pcb": route_input.name,
            "input_pcb_sha256": _sha256(route_input),
            "net_class_sidecar_sha256": _sha256(sidecar),
            "argv": _normalise(argv, work),
            "argv_sha256": hashlib.sha256(json.dumps(_normalise(argv, work)).encode()).hexdigest(),
            "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
            "router_cpp_version": rc.version(),
            "router_cpp_build_version": int(rc.BUILD_VERSION),
            "command": "uv run python boards/07-matchgroup-test/capture_coupled_search.py",
        },
        "native_objects": [
            {"obj": e["obj"], "type": e["type"], "args": c["args"]}
            for e, c in zip(native_new, [c for c in calls if c["op"] == "new"], strict=True)
            if e["type"] != "Grid3D"
        ],
        "native_call_counts": _call_counts(calls),
        "phases": phases,
        "classification": [
            {k: v for k, v in c.items() if isinstance(v, (int, float, bool, str)) or v is None}
            for c in state["classes"]
        ],
        "timing_s": {"pair": round(state["pair_seconds"], 2), "run": round(run_seconds, 2)},
    }
    save_fixture(fixture_dir, manifest, calls, arrays)
    return manifest


def _call_counts(calls: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in calls:
        key = c.get("type") if c["op"] == "new" else c["method"]
        counts[key] = counts.get(key, 0) + (c["rows"] if c["op"] == "batch" else 1)
    return dict(sorted(counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pair", default="TMDS_D1")
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args()

    if os.environ.get("PYTHONHASHSEED") != "42":
        # The recipe pins it for the routing process; string-keyed iteration
        # order (and therefore the board state the pair sees) depends on it.
        os.execve(
            sys.executable, [sys.executable, *sys.argv], {**os.environ, "PYTHONHASHSEED": "42"}
        )

    t0 = time.monotonic()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="board07-capture-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        manifest = capture(args.pair, args.fixture_dir.resolve(), work.resolve())
    finally:
        if args.work_dir is None:
            shutil.rmtree(work, ignore_errors=True)
    total = time.monotonic() - t0
    size = sum(p.stat().st_size for p in args.fixture_dir.iterdir())
    print(f"\n[capture] wrote {args.fixture_dir} ({size / 1e6:.2f} MB) in {total:.1f}s")
    for phase in manifest["phases"]:
        d = phase["native_diagnostics"]
        top = max(d["rejections"].items(), key=lambda kv: kv[1], default=("-", 0))
        print(
            f"  {phase['phase']:8s} success={d['success']} iterations={d['iterations']} "
            f"best_progress={d['best_progress']} iteration_limited={d['iteration_limited']} "
            f"dominant={top[0]}={top[1]}"
        )
    for c in manifest["classification"]:
        print(f"  class={c['class']} best_progress={c.get('best_progress')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
