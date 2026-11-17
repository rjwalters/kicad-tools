#!/usr/bin/env python3
"""Per-phase wall-clock profile of ``kct route`` (Issue #5787, Epic #5784 Phase 3).

``docs/research/kicad-routing-tools-comparison.md`` measured ``kct route``
taking ~50 s on board 02 and ~105 s on board 03 against KRT's 1.2 s / 7.8 s,
but could not say *where* the time goes. This script answers that question
for one board at a time, before anyone changes an algorithm.

It runs the documented default recipe (``kct route IN -o OUT`` plus any
``--extra`` arguments) **in-process**, through the same ``kicad_tools.cli.main``
entry point the ``kct`` console script uses, and attributes wall-clock time
three independent ways:

1. **Function phases.** A curated list of phase entry points (``PHASES``
   below) is wrapped with a low-overhead timer. Each phase reports inclusive
   and *exclusive* time (inclusive minus time spent in any nested wrapped
   phase), so the exclusive column sums to the total wall time exactly,
   with the remainder reported as ``(unattributed)``. Unlike ``cProfile`` the
   wrappers fire only on these few dozen entry points, so they do not inflate
   Python-heavy phases relative to the C++ A* loop.
2. **Stage timeline.** ``kicad_tools.cli.route_deadline.record_stage`` is the
   route CLI's own published stage marker (``routing``, ``optimization``,
   ``drc-nudge``, ``serialization``, ``post-route-drc`` ...). It is hooked to
   record transition times; the durations between transitions are reported.
3. **Negotiated iterations.** Every stdout line is timestamped; the
   ``--- Iteration N`` / ``=== Negotiated Routing Complete ===`` markers give a
   per-iteration wall-clock split of the negotiated loop.

Optionally (``--cprofile FILE``) the whole run is also recorded with
``cProfile`` for drill-down. That inflates Python-heavy phases, so the
function-phase table should be read from a run *without* it.

``--repeat N`` runs the whole profile ``N`` times and reports the **median**
of each phase together with its min/max across runs, plus the wall-time
spread factor (max/min) and the load average observed during each run. A
single run carries no variance estimate at all, which is exactly what made
the first published profile unusable for absolute seconds on a loaded host;
every repeat is a **fresh subprocess**, so no module-level cache, grid, or
instrumentation state from one run can leak into the next.

Research-only: not wired into CI. It routes a *copy* of the input (the input is
never modified); the output lands wherever ``-o`` points -- keep it outside
``boards/``.

Usage (from a worktree with the C++ backend built)::

    uv run kct build-native --check
    uv run python scripts/research/route_phase_profile.py \\
        boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb \\
        --work-dir /tmp/route-profile/02 --json /tmp/route-profile/02.json \\
        --repeat 3
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib
import io
import json
import os
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# (label, module, qualname). Order is the report order. ``qualname`` may be
# ``Class.method`` or a module-level function name; module-level functions are
# also replaced in every loaded ``kicad_tools`` module that imported them by
# name, so ``from .x import f`` call sites are covered too.
PHASES: tuple[tuple[str, str, str], ...] = (
    ("cli: argument handling, setup, reporting", "kicad_tools.cli.route_cmd", "_run_main_impl"),
    ("load PCB for routing", "kicad_tools.router.io", "load_pcb_for_routing"),
    (
        "layer-escalation driver (grid build, per-attempt setup)",
        "kicad_tools.cli.route_cmd",
        "route_with_layer_escalation",
    ),
    (
        "pre-route auto-pour (kicad-cli zone fill of missing pours)",
        "kicad_tools.router.auto_pour",
        "auto_pour_if_missing",
    ),
    ("routing grid construction", "kicad_tools.router.grid", "RoutingGrid.__init__"),
    ("plan-gate preflight", "kicad_tools.cli.route_cmd", "_plan_gate_preflight"),
    (
        "attempt preserved-copper capture",
        "kicad_tools.cli.route_cmd",
        "_capture_attempt_preserved_copper",
    ),
    (
        "connectivity-invariant enforcement",
        "kicad_tools.cli.route_cmd",
        "_enforce_connectivity_invariant_or_exit",
    ),
    (
        "negotiated loop: bookkeeping between searches",
        "kicad_tools.router.core",
        "Autorouter.route_all_negotiated",
    ),
    (
        "escape-routing driver",
        "kicad_tools.router.core",
        "Autorouter.route_with_escape",
    ),
    (
        "two-phase router: bookkeeping",
        "kicad_tools.router.algorithms.two_phase",
        "TwoPhaseRouter.route_all",
    ),
    (
        "two-phase detailed negotiated loop: bookkeeping",
        "kicad_tools.router.algorithms.two_phase",
        "TwoPhaseRouter._detailed_negotiated",
    ),
    (
        "global routing plan (tile graph + global negotiation)",
        "kicad_tools.router.routing_plan",
        "build_plan",
    ),
    ("A* search (CppPathfinder.route)", "kicad_tools.router.cpp_backend", "CppPathfinder.route"),
    ("pure-Python A* fallback (Router.route)", "kicad_tools.router.pathfinder", "Router.route"),
    (
        "A* result clearance re-validation",
        "kicad_tools.router.cpp_backend",
        "CppPathfinder._validate_route_clearance",
    ),
    ("grid mark_route", "kicad_tools.router.grid", "RoutingGrid.mark_route"),
    ("grid unmark_route", "kicad_tools.router.grid", "RoutingGrid.unmark_route"),
    (
        "grid resync_route_occupancy",
        "kicad_tools.router.grid",
        "RoutingGrid.resync_route_occupancy",
    ),
    (
        "pad-access invariant: arm (Epic #5508 Phase 2)",
        "kicad_tools.router.pad_access_invariant",
        "PadAccessInvariant.arm",
    ),
    (
        "pad-access invariant: per-commit veto check",
        "kicad_tools.router.pad_access_invariant",
        "PadAccessInvariant.veto_for",
    ),
    (
        "negotiated: find nets through overused cells",
        "kicad_tools.router.algorithms.negotiated",
        "NegotiatedRouter.find_nets_through_overused_cells",
    ),
    (
        "negotiated: via-segment violation scan",
        "kicad_tools.router.algorithms.negotiated",
        "NegotiatedRouter.find_nets_with_via_segment_violations",
    ),
    (
        "negotiated: segment-segment violation scan",
        "kicad_tools.router.algorithms.negotiated",
        "NegotiatedRouter.find_segment_segment_violation_pairs",
    ),
    (
        "negotiated: rip_up_nets",
        "kicad_tools.router.algorithms.negotiated",
        "NegotiatedRouter.rip_up_nets",
    ),
    (
        "net connectivity validation",
        "kicad_tools.router.observability",
        "validate_net_connectivity",
    ),
    ("io.validate_routes", "kicad_tools.router.io", "validate_routes"),
    (
        "best-so-far snapshot restore",
        "kicad_tools.router.core",
        "Autorouter._restore_negotiated_route_snapshot",
    ),
    (
        "post-route clearance correction",
        "kicad_tools.router.core",
        "Autorouter._post_route_clearance_correction",
    ),
    (
        "pad-clearance demotion",
        "kicad_tools.router.core",
        "Autorouter._demote_pad_clearance_violation_nets",
    ),
    (
        "trace optimizer (grid-synced)",
        "kicad_tools.router.optimizer.trace",
        "optimize_routes_grid_synced",
    ),
    (
        "unrouted congested/blocked diagnosis (solo A* per connection)",
        "kicad_tools.router.unrouted_cause",
        "diagnose_unrouted",
    ),
    ("write routed PCB (checkpoints + final)", "kicad_tools.cli.route_cmd", "_write_routed_pcb"),
    (
        "routing-plan sidecar",
        "kicad_tools.cli.route_cmd",
        "_write_routing_plan_sidecar",
    ),
    (
        "access-witness sidecar (journal replay)",
        "kicad_tools.cli.route_cmd",
        "_write_access_witness_sidecar",
    ),
    ("zone fill after route (kicad-cli)", "kicad_tools.cli.route_cmd", "_fill_zones_after_route"),
    (
        "pour-net oracle completion loop (kicad-cli DRC rounds)",
        "kicad_tools.cli.route_cmd",
        "_complete_pour_nets_with_oracle",
    ),
    (
        "DRC-constraint sidecars (.kicad_pro/.kicad_dru)",
        "kicad_tools.cli.route_cmd",
        "_write_drc_constraint_sidecars",
    ),
    ("post-route DRC validation", "kicad_tools.cli.route_cmd", "run_post_route_drc"),
    ("geometric DRC (kicad-cli)", "kicad_tools.drc.geometric", "run_geometric_drc"),
    ("kicad-cli --version probe", "kicad_tools.export.gerber", "get_kicad_cli_version"),
)

ASTAR_PHASE = "A* search (CppPathfinder.route)"
UNATTRIBUTED = "(unattributed: imports, glue, untimed passes)"


@dataclass
class SubprocessTally:
    """Every ``kicad-cli`` child process the run launched, with its wall time.

    Each invocation pays a fixed process-startup tax before doing any work
    (~6 s on the macOS host this was first measured on, where even
    ``kicad-cli --help`` takes that long), so the *count* of invocations is a
    first-class cost driver and not a detail. The phase table cannot show it:
    a phase that shells out twice looks the same as one that shells out once.
    """

    calls: list[dict] = field(default_factory=list)

    @property
    def total_s(self) -> float:
        return sum(c["seconds"] for c in self.calls)

    @staticmethod
    def _subcommand(argv: list[str]) -> str:
        """``kicad-cli pcb drc --refill-zones board.kicad_pcb`` -> ``pcb drc``.

        Flags and file arguments are dropped so that two invocations of the
        same subcommand on different boards group together -- otherwise every
        call looks unique and the count-per-subcommand is useless.
        """
        words = [
            a
            for a in argv[1:]
            if not a.startswith("-") and "/" not in a and not Path(a).suffix.startswith(".kicad")
        ]
        return " ".join(words[:2])

    def record(self, argv: list[str], seconds: float) -> None:
        exe = Path(argv[0]).name if argv else "?"
        self.calls.append(
            {
                "exe": exe,
                "subcommand": self._subcommand(argv),
                "seconds": round(seconds, 3),
            }
        )

    def summary(self) -> dict:
        by_sub: dict[str, dict] = {}
        for c in self.calls:
            row = by_sub.setdefault(
                c["subcommand"], {"subcommand": c["subcommand"], "calls": 0, "seconds": 0.0}
            )
            row["calls"] += 1
            row["seconds"] = round(row["seconds"] + c["seconds"], 3)
        return {
            "invocations": len(self.calls),
            "total_s": round(self.total_s, 3),
            "by_subcommand": sorted(by_sub.values(), key=lambda r: -r["seconds"]),
        }


def _count_kicad_cli_subprocesses(tally: SubprocessTally):
    """Patch ``subprocess.run`` to tally ``kicad-cli`` children; returns an undo.

    Every ``kicad_tools`` caller does ``import subprocess`` and looks ``run``
    up on the module at call time, so one patch covers them all.
    """
    original = subprocess.run

    @functools.wraps(original)
    def counting_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        is_kicad = False
        with contextlib.suppress(Exception):
            if isinstance(argv, (list, tuple)) and argv:
                is_kicad = "kicad-cli" in Path(str(argv[0])).name
        if not is_kicad:
            return original(*args, **kwargs)
        t0 = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            tally.record([str(a) for a in argv], time.perf_counter() - t0)

    subprocess.run = counting_run  # type: ignore[assignment]
    return lambda: setattr(subprocess, "run", original)


@dataclass
class PhaseStat:
    label: str
    calls: int = 0
    inclusive: float = 0.0
    exclusive: float = 0.0


@dataclass
class PhaseTimer:
    """Stack-based inclusive/exclusive wall-clock accounting.

    Only the main thread is accounted; a wrapped function entered from a worker
    thread is counted in ``offthread_calls`` and otherwise ignored, so worker
    time is never double-counted against a main-thread frame.
    """

    stats: dict[str, PhaseStat] = field(default_factory=dict)
    offthread_calls: int = 0
    _stack: list[list] = field(default_factory=list)  # [label, start, child_time]
    _main: int = field(default_factory=threading.get_ident)

    def stat(self, label: str) -> PhaseStat:
        if label not in self.stats:
            self.stats[label] = PhaseStat(label)
        return self.stats[label]

    def enter(self, label: str) -> bool:
        if threading.get_ident() != self._main:
            self.offthread_calls += 1
            return False
        self._stack.append([label, time.perf_counter(), 0.0])
        return True

    def exit(self) -> None:
        label, start, child = self._stack.pop()
        elapsed = time.perf_counter() - start
        st = self.stat(label)
        st.calls += 1
        st.exclusive += elapsed - child
        # Recursion: only the outermost frame of a label adds inclusive time.
        if all(frame[0] != label for frame in self._stack):
            st.inclusive += elapsed
        if self._stack:
            self._stack[-1][2] += elapsed

    def wrap(self, label: str, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            accounted = self.enter(label)
            try:
                return fn(*args, **kwargs)
            finally:
                if accounted:
                    self.exit()

        wrapper.__route_phase_profile__ = True  # type: ignore[attr-defined]
        return wrapper


def instrument(timer: PhaseTimer, phases=PHASES) -> list[tuple[object, str, object]]:
    """Wrap every phase entry point; return an undo list of (owner, attr, original)."""
    undo: list[tuple[object, str, object]] = []
    for label, module_name, qualname in phases:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            print(f"route_phase_profile: skip {label}: {exc}", file=sys.stderr)
            continue
        owner: object = module
        *path, attr = qualname.split(".")
        for part in path:
            owner = getattr(owner, part)
        original = getattr(owner, attr, None)
        if original is None:
            print(f"route_phase_profile: skip {label}: {qualname} not found", file=sys.stderr)
            continue
        wrapped = timer.wrap(label, original)
        setattr(owner, attr, wrapped)
        undo.append((owner, attr, original))
        if not path:  # module-level function: patch by-name imports too
            for name, other in list(sys.modules.items()):
                if other is module or not name.startswith("kicad_tools"):
                    continue
                for other_attr, value in list(vars(other).items()):
                    if value is original:
                        setattr(other, other_attr, wrapped)
                        undo.append((other, other_attr, original))
    return undo


def uninstrument(undo: list[tuple[object, str, object]]) -> None:
    for owner, attr, original in reversed(undo):
        setattr(owner, attr, original)


class TimestampTee(io.TextIOBase):
    """Write-through stream that keeps ``(offset_s, line, probe)`` records.

    ``probe`` (optional) is called once per completed line; the iteration
    split uses it to read the A* phase's running total at each marker.
    """

    def __init__(self, inner, t0: float, echo: bool, probe=None):
        self.inner = inner
        self.t0 = t0
        self.echo = echo
        self.probe = probe
        self.lines: list[tuple[float, str, float | None]] = []
        self._buf = ""

    def write(self, s: str) -> int:
        if self.echo:
            self.inner.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            probe = self.probe() if self.probe is not None else None
            self.lines.append((time.perf_counter() - self.t0, line, probe))
        return len(s)

    def flush(self) -> None:
        if self.echo:
            self.inner.flush()

    def isatty(self) -> bool:
        return False


# Two loop shapes print their iterations differently:
# * ``Autorouter.route_all_negotiated`` (board 02): ``--- Iteration N: ...``;
# * ``TwoPhaseRouter._detailed_negotiated`` (board 03, escape + two-phase):
#   ``--- Phase 2: Detailed Routing ---`` opens the initial pass (iteration 0)
#   and ``Iteration N: ripping up K nets`` opens each rip-up round.
ITER_RE = re.compile(
    r"--- Iteration (?P<a>\d+)|^\s*Iteration (?P<b>\d+): ripping up|(?P<p2>--- Phase 2: Detailed Routing ---)"
)
ITER_END_RE = re.compile(
    r"(=== Negotiated Routing Complete ===|=== Two-Phase Routing Complete ===|Best-metric early-stop)"
)


def iteration_split(lines) -> list[dict]:
    """Per-iteration wall time of every negotiated loop seen in the log.

    ``lines`` holds ``(offset_s, text)`` or ``(offset_s, text, astar_s)``
    records. An iteration runs from its ``--- Iteration N`` marker to the next
    marker, or to the early-stop / completion line that ends the loop. A board
    that escalates layers runs several loops; each restart at iteration 0 is
    reported as a new ``attempt``. When the records carry the A* phase's
    running total, each iteration also reports its ``astar_s`` share.
    """
    out: list[dict] = []
    attempt = 0
    open_iter: dict | None = None

    def close(rec: tuple) -> None:
        assert open_iter is not None
        open_iter["seconds"] = round(rec[0] - open_iter.pop("_t"), 3)
        a0 = open_iter.pop("_a")
        if a0 is not None and len(rec) > 2 and rec[2] is not None:
            open_iter["astar_s"] = round(rec[2] - a0, 3)
        out.append(open_iter)

    for rec in lines:
        line = rec[1]
        m = ITER_RE.search(line)
        if m:
            n = 0 if m.group("p2") else int(m.group("a") or m.group("b"))
            if open_iter is not None:
                close(rec)
            if n == 0 and out:
                attempt += 1
            open_iter = {
                "attempt": attempt,
                "iteration": n,
                "_t": rec[0],
                "_a": rec[2] if len(rec) > 2 else None,
                "line": line.strip(),
            }
            continue
        if open_iter is not None and ITER_END_RE.search(line):
            close(rec)
            open_iter = None
    if open_iter is not None:  # only reachable with a non-empty ``lines``
        close(lines[-1])
        out[-1]["unterminated"] = True
    return out


def stage_split(transitions: list[tuple[float, str]], total: float) -> list[dict]:
    """Collapse ``record_stage`` transitions into per-stage summed durations."""
    durations: dict[str, float] = {}
    counts: dict[str, int] = {}
    for i, (t, stage) in enumerate(transitions):
        end = transitions[i + 1][0] if i + 1 < len(transitions) else total
        durations[stage] = durations.get(stage, 0.0) + max(0.0, end - t)
        counts[stage] = counts.get(stage, 0) + 1
    first = transitions[0][0] if transitions else total
    rows = [{"stage": "(before first stage marker)", "seconds": round(first, 3), "entries": 1}]
    rows += [
        {"stage": s, "seconds": round(d, 3), "entries": counts[s]} for s, d in durations.items()
    ]
    return rows


def _host_info() -> dict:
    info = {"cpu_count": os.cpu_count(), "platform": sys.platform}
    with contextlib.suppress(OSError):
        info["loadavg_start"] = [round(x, 2) for x in os.getloadavg()]
    with contextlib.suppress(OSError, subprocess.CalledProcessError):
        info["git_sha"] = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    try:
        from kicad_tools.router import cpp_backend

        info["cpp_backend"] = bool(cpp_backend.is_cpp_available())
    except Exception as exc:  # pragma: no cover - diagnostic only
        info["cpp_backend"] = f"unknown ({exc})"
    return info


def profile_route(
    pcb: Path, work_dir: Path, extra: list[str], echo: bool, cprofile: Path | None
) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    src = work_dir / pcb.name
    if pcb.resolve() != src.resolve():
        shutil.copyfile(pcb, src)
        for suffix in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
            sib = pcb.with_suffix(suffix)
            if sib.exists():
                shutil.copyfile(sib, src.with_suffix(suffix))
    out = work_dir / f"{pcb.stem}_routed.kicad_pcb"
    argv = ["route", str(src), "-o", str(out), *extra]

    import kicad_tools.cli as cli
    from kicad_tools.cli import route_cmd, route_deadline

    host = _host_info()
    timer = PhaseTimer()
    undo = instrument(timer)
    tally = SubprocessTally()
    undo_tally = _count_kicad_cli_subprocesses(tally)

    transitions: list[tuple[float, str]] = []
    original_record_stage = route_deadline.record_stage
    t0 = time.perf_counter()

    def record_stage_hook(stage: str, **fields):
        if not transitions or transitions[-1][1] != stage:
            transitions.append((time.perf_counter() - t0, stage))
        return original_record_stage(stage, **fields)

    route_deadline.record_stage = record_stage_hook  # type: ignore[assignment]
    route_cmd.record_stage = record_stage_hook  # type: ignore[attr-defined]

    # Attach a control file, as the ``--timeout`` supervisor does. Without one,
    # ``record_stage`` is a no-op and ``restore_stage`` cannot restore the
    # stage a best-so-far checkpoint write interrupted (#5266), so every
    # negotiated iteration after the first checkpoint would be booked as
    # ``serialization``. The control file is only read by the deadline signal
    # path, which this harness never arms, so routing behaviour is unchanged.
    control_dir = tempfile.TemporaryDirectory(prefix="route-phase-profile-")
    previous_control = os.environ.get(route_deadline.CONTROL_ENV)
    os.environ[route_deadline.CONTROL_ENV] = str(Path(control_dir.name) / "control.json")

    astar = timer.stat(ASTAR_PHASE)
    tee = TimestampTee(sys.stdout, t0, echo, probe=lambda: astar.inclusive)
    real_stdout = sys.stdout
    sys.stdout = tee
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    rc0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    prof = None
    timer.enter(UNATTRIBUTED)
    try:
        if cprofile is not None:
            import cProfile

            prof = cProfile.Profile()
            rc = prof.runcall(cli.main, argv)
        else:
            rc = cli.main(argv)
    except SystemExit as exc:  # argparse / sys.exit inside the CLI
        rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        timer.exit()
        total = time.perf_counter() - t0
        sys.stdout = real_stdout
        route_deadline.record_stage = original_record_stage  # type: ignore[assignment]
        route_cmd.record_stage = original_record_stage  # type: ignore[attr-defined]
        if previous_control is None:
            os.environ.pop(route_deadline.CONTROL_ENV, None)
        else:
            os.environ[route_deadline.CONTROL_ENV] = previous_control
        control_dir.cleanup()
        undo_tally()
        uninstrument(undo)
        if prof is not None and cprofile is not None:
            prof.dump_stats(str(cprofile))
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    rc1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    with contextlib.suppress(OSError):
        host["loadavg_end"] = [round(x, 2) for x in os.getloadavg()]

    phases = [
        {
            "phase": s.label,
            "calls": s.calls,
            "inclusive_s": round(s.inclusive, 3),
            "exclusive_s": round(s.exclusive, 3),
            "exclusive_pct": round(100.0 * s.exclusive / total, 1) if total else 0.0,
        }
        for s in timer.stats.values()
        if s.calls
    ]
    phases.sort(key=lambda r: -r["exclusive_s"])
    return {
        "board": str(pcb),
        "argv": ["kct", *argv],
        "exit_code": rc,
        "wall_s": round(total, 3),
        "cpu_self_s": round((ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime), 3),
        "cpu_children_s": round((rc1.ru_utime - rc0.ru_utime) + (rc1.ru_stime - rc0.ru_stime), 3),
        "host": host,
        "phases": phases,
        "kicad_cli": tally.summary(),
        "offthread_calls": timer.offthread_calls,
        "stages": stage_split(transitions, total),
        "iterations": iteration_split(tee.lines),
        "log": [[round(rec[0], 3), rec[1]] for rec in tee.lines],
    }


def render_markdown(result: dict) -> str:
    lines = [
        f"**{result['board']}** -- wall {result['wall_s']:.1f} s, "
        f"CPU self {result['cpu_self_s']:.1f} s, children {result['cpu_children_s']:.1f} s, "
        f"exit {result['exit_code']}",
        "",
        "| Phase (exclusive) | Calls | Exclusive s | % | Inclusive s |",
        "|---|---|---|---|---|",
    ]
    for r in result["phases"]:
        lines.append(
            f"| {r['phase']} | {r['calls']} | {r['exclusive_s']:.2f} | "
            f"{r['exclusive_pct']:.1f} | {r['inclusive_s']:.2f} |"
        )
    cli = result.get("kicad_cli")
    if cli:
        lines += [
            "",
            f"kicad-cli: {cli['invocations']} invocations, {cli['total_s']:.1f} s total",
            "",
            "| kicad-cli subcommand | Calls | Seconds |",
            "|---|---|---|",
        ]
        for r in cli["by_subcommand"]:
            lines.append(
                f"| {r['subcommand'] or '(no subcommand)'} | {r['calls']} | {r['seconds']:.2f} |"
            )
    lines += ["", "| Stage marker | Entries | Seconds |", "|---|---|---|"]
    for r in result["stages"]:
        lines.append(f"| {r['stage']} | {r['entries']} | {r['seconds']:.2f} |")
    if result["iterations"]:
        lines += ["", "| Attempt | Iteration | Seconds | of which A* s |", "|---|---|---|---|"]
        for r in result["iterations"]:
            astar_s = f"{r['astar_s']:.2f}" if "astar_s" in r else "--"
            lines.append(f"| {r['attempt']} | {r['iteration']} | {r['seconds']:.2f} | {astar_s} |")
    return "\n".join(lines)


def aggregate_runs(results: list[dict]) -> dict:
    """Median / min / max across repeated profile runs of the same board.

    ``results`` is a list of ``profile_route`` payloads for the *same* board
    and argv. Per phase the median exclusive time is the headline number and
    min/max bracket it; a phase that did not appear in every run reports the
    ``runs`` it did appear in, so a sometimes-skipped phase cannot masquerade
    as a cheap one. ``wall_spread`` (max/min wall) is the honesty check on
    absolute seconds: a spread far above 1.0 means the host was not quiet and
    the seconds should not be quoted.
    """
    if not results:
        raise ValueError("aggregate_runs() needs at least one run")
    walls = [r["wall_s"] for r in results]
    n = len(results)

    labels: list[str] = []
    for r in results:
        for row in r["phases"]:
            if row["phase"] not in labels:
                labels.append(row["phase"])

    phases = []
    for label in labels:
        rows = [row for r in results for row in r["phases"] if row["phase"] == label]
        excl = [row["exclusive_s"] for row in rows]
        phases.append(
            {
                "phase": label,
                "runs": len(rows),
                "calls_median": round(statistics.median([row["calls"] for row in rows]), 1),
                "exclusive_median_s": round(statistics.median(excl), 3),
                "exclusive_min_s": round(min(excl), 3),
                "exclusive_max_s": round(max(excl), 3),
                "exclusive_median_pct": round(
                    100.0 * statistics.median(excl) / statistics.median(walls), 1
                ),
            }
        )
    phases.sort(key=lambda r: -r["exclusive_median_s"])

    stage_labels: list[str] = []
    for r in results:
        for row in r["stages"]:
            if row["stage"] not in stage_labels:
                stage_labels.append(row["stage"])
    stages = []
    for label in stage_labels:
        secs = [row["seconds"] for r in results for row in r["stages"] if row["stage"] == label]
        stages.append(
            {
                "stage": label,
                "runs": len(secs),
                "median_s": round(statistics.median(secs), 3),
                "min_s": round(min(secs), 3),
                "max_s": round(max(secs), 3),
            }
        )

    cli_runs = [r["kicad_cli"] for r in results if r.get("kicad_cli")]
    kicad_cli = None
    if cli_runs:
        counts = [c["invocations"] for c in cli_runs]
        secs = [c["total_s"] for c in cli_runs]
        kicad_cli = {
            "invocations_median": statistics.median(counts),
            "invocations_all": counts,
            "total_median_s": round(statistics.median(secs), 3),
            "total_min_s": round(min(secs), 3),
            "total_max_s": round(max(secs), 3),
            "median_pct_of_wall": round(
                100.0 * statistics.median(secs) / statistics.median(walls), 1
            ),
        }

    return {
        "board": results[0]["board"],
        "argv": results[0]["argv"],
        "runs": n,
        "kicad_cli": kicad_cli,
        "exit_codes": [r["exit_code"] for r in results],
        "wall_median_s": round(statistics.median(walls), 3),
        "wall_min_s": round(min(walls), 3),
        "wall_max_s": round(max(walls), 3),
        "wall_spread": round(max(walls) / min(walls), 2) if min(walls) else None,
        "wall_all_s": [round(w, 3) for w in walls],
        "loadavg_per_run": [
            {
                "start": r.get("host", {}).get("loadavg_start"),
                "end": r.get("host", {}).get("loadavg_end"),
            }
            for r in results
        ],
        "host": results[0].get("host", {}),
        "phases": phases,
        "stages": stages,
    }


def render_aggregate_markdown(agg: dict) -> str:
    lines = [
        f"**{agg['board']}** -- {agg['runs']} runs, wall median {agg['wall_median_s']:.1f} s "
        f"(min {agg['wall_min_s']:.1f}, max {agg['wall_max_s']:.1f}, "
        f"spread {agg['wall_spread']}x), exit codes {agg['exit_codes']}",
        "",
        "| Phase (exclusive) | Runs | Calls | Median s | Min s | Max s | % of median wall |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in agg["phases"]:
        lines.append(
            f"| {r['phase']} | {r['runs']} | {r['calls_median']:g} | "
            f"{r['exclusive_median_s']:.2f} | {r['exclusive_min_s']:.2f} | "
            f"{r['exclusive_max_s']:.2f} | {r['exclusive_median_pct']:.1f} |"
        )
    cli = agg.get("kicad_cli")
    if cli:
        lines += [
            "",
            f"kicad-cli: {cli['invocations_median']:g} invocations (median), "
            f"{cli['total_median_s']:.1f} s total "
            f"(min {cli['total_min_s']:.1f}, max {cli['total_max_s']:.1f}) "
            f"= {cli['median_pct_of_wall']:.1f}% of median wall",
        ]
    lines += ["", "| Stage marker | Runs | Median s | Min s | Max s |", "|---|---|---|---|---|"]
    for r in agg["stages"]:
        lines.append(
            f"| {r['stage']} | {r['runs']} | {r['median_s']:.2f} | "
            f"{r['min_s']:.2f} | {r['max_s']:.2f} |"
        )
    lines += ["", "| Run | wall s | loadavg start | loadavg end |", "|---|---|---|---|"]
    for i, (wall, load) in enumerate(zip(agg["wall_all_s"], agg["loadavg_per_run"], strict=True)):
        lines.append(f"| {i} | {wall:.1f} | {load['start']} | {load['end']} |")
    return "\n".join(lines)


def run_repeats(args, repeat: int) -> list[dict]:
    """Run the profile ``repeat`` times, each in a fresh subprocess.

    A cold process per run is the point: the in-process harness patches
    ``kicad_tools`` modules and the router keeps module-level caches, so
    back-to-back in-process runs would not be independent samples.
    """
    results: list[dict] = []
    for i in range(repeat):
        run_json = args.work_dir / f"run-{i}.json"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            str(args.pcb),
            "--work-dir",
            str(args.work_dir / f"run-{i}"),
            "--json",
            str(run_json),
        ]
        if args.extra:
            cmd += ["--extra", args.extra]
        print(f"route_phase_profile: run {i + 1}/{repeat} ...", file=sys.stderr, flush=True)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not run_json.exists():
            sys.stderr.write(proc.stdout[-4000:])
            sys.stderr.write(proc.stderr[-4000:])
            raise SystemExit(f"route_phase_profile: run {i} failed (rc={proc.returncode})")
        results.append(json.loads(run_json.read_text()))
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcb", type=Path, help="unrouted .kicad_pcb (copied, never modified)")
    ap.add_argument("--work-dir", type=Path, required=True)
    ap.add_argument("--json", type=Path, help="write the full result (incl. log) here")
    ap.add_argument("--cprofile", type=Path, help="also dump a cProfile .prof (inflates Python)")
    ap.add_argument("--echo", action="store_true", help="echo kct output while running")
    ap.add_argument(
        "--extra",
        default="",
        help="extra kct route arguments, one string (e.g. '--monotone-certificate-order')",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="run N times in fresh subprocesses and report median/min/max per phase "
        "plus the wall-time spread (default 1: single run, no variance estimate)",
    )
    args = ap.parse_args()
    if not args.pcb.exists():
        ap.error(f"{args.pcb} does not exist")
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")
    if args.repeat > 1:
        if args.cprofile:
            ap.error("--cprofile profiles one run only; use --repeat 1")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        agg = aggregate_runs(run_repeats(args, args.repeat))
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(agg, indent=2) + "\n")
        print(render_aggregate_markdown(agg))
        return 0
    result = profile_route(
        args.pcb, args.work_dir, args.extra.split(), echo=args.echo, cprofile=args.cprofile
    )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
