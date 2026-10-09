"""Route-check acceptance for ``kct optimize-placement`` (issue #6234).

Neither calibration study (#5948, PR #6232 and its fresh routed corpus) found
a pre-route score that reliably predicts which of two placements routes
better. HPWL was the best single signal at 0.749 pairwise. RUDY sat at
chance, and the best fitted gate rejected 57% of moves that really were
better. So ``--route-check`` stops predicting and measures: a candidate
placement is accepted only if a short, *bounded* real ``kct route`` reaches
at least the incumbent's completion with no more DRC errors.

The idea comes from TraceMaker. TraceMaker is GPL-3.0, so only the idea is
used and none of its code (``kct ecosystem show tracemaker``).

Where the checks run
--------------------
They run once, at the end of the run, as a staged acceptance chain. They
never run inside the optimizer loop, so the total cost is bounded:

* **incumbent**: the placement as read from the board, with no moves.
* **stage A** (``optimize``): the optimizer result after the warm-start
  keep-seed rule and the post-pass slide-off. It becomes the incumbent only
  if :func:`route_check_accepts` says so.
* **stage B** (``decoupling_snap``): the decoupling snap applied to whatever
  stage A left as the incumbent. If stage A was rejected, that is a snap-only
  candidate on the board as read. It is gated the same way, so the snap is
  route-checked too.

A stage whose vector equals the current incumbent is skipped without routing.
Route results are cached by placement vector, and the incumbent is routed
only when a stage actually needs to be compared against it. The cost is
therefore **at most three bounded routes per run** (incumbent, A, B), and
each route is bounded by an iteration budget.

Determinism
-----------
Every check route runs with ``--deterministic-budget --per-net-iterations
<budget> --deterministic-rescue --seed <seed>``. The budget caps the A* node
expansions per net, which is load-independent, so the same board gives the
same verdict on any machine and on both router backends (#6217). The outer
``--timeout`` is only a safety backstop. If it binds anyway, the router logs
``[deterministic-budget] stage deadline fired``, and that outcome is flagged
as not reproducible.

What is measured
----------------
* **completion** = ``nets_routed / nets_requested`` from the router's own
  ``--format json`` summary. A net that a placement makes unroutable counts
  against the candidate, because the requested denominator is the same for
  every placement of the board.
* **DRC errors** = the error count of the router's own post-route DRC
  (:func:`kicad_tools.cli.route_cmd.run_post_route_drc`: the internal engine
  plus kicad-cli geometric DRC when it is installed). The check route is
  launched with ``--skip-drc`` and that function is called once on the routed
  board with ``quiet=True``, so the count is read as an integer instead of
  being scraped from prose.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from kicad_tools.placement.vector import PlacementVector

# Per-net A* node-expansion cap for a check route (``--route-check-budget``).
#
# The production cap ``DETERMINISTIC_BUDGET_PER_NET_ITERATIONS`` (1,000,000)
# was tuned to let a chorus-class board route as far as it ever will. A check
# route has a different job: it only has to *rank* two placements of the same
# board, so it can give up on grinder nets sooner. 100,000 is one tenth of the
# production cap. Measured with the check-route switches below on fleet
# boards: board 01 routes 3/3 nets in about 3 s unloaded (at 5,000 it already
# drops a net, so much lower caps stop measuring the placement); board 04
# routes every signal net, and board 02 routes 9/10. Board 02 takes minutes
# whatever the cap, because negotiated rip-up rounds dominate its time, not
# A* expansions. So this keeps a small board's check to seconds or tens of
# seconds. Raise it on boards whose nets need long detours.
DEFAULT_ROUTE_CHECK_BUDGET = 100_000

# Outer wall-clock backstop per check route, in seconds. It is deliberately
# generous: the iteration budget, not this timeout, is meant to bound the
# work. If it does bind, the outcome is flagged ``reproducible=False``.
DEFAULT_ROUTE_CHECK_TIMEOUT_S = 900.0

# The router's own log line when a wall-clock stage deadline cut an
# iteration-budgeted run (issue #5765). It is matched at the *start* of a
# line: the router's ``--timeout`` advisory quotes the same words mid-line.
DEADLINE_FIRED_MARKER = "[deterministic-budget] stage deadline fired"


def deadline_fired(log: str) -> bool:
    """Whether *log* carries the router's stage-deadline-fired line."""
    return any(line.lstrip().startswith(DEADLINE_FIRED_MARKER) for line in log.splitlines())


STAGE_OPTIMIZE = "optimize"
STAGE_DECOUPLING_SNAP = "decoupling_snap"


@dataclass(frozen=True)
class RouteCheckOutcome:
    """What one bounded check route measured for one placement."""

    nets_routed: int
    nets_requested: int
    drc_errors: int | None
    exit_code: int | None
    wall_time_s: float
    reproducible: bool = True
    error: str | None = None

    @property
    def measured(self) -> bool:
        """True when the route produced a summary (a completion was measured)."""
        return self.error is None

    @property
    def completion(self) -> float:
        """Routed nets over requested nets (1.0 for a board with nothing to route)."""
        if self.nets_requested <= 0:
            return 1.0
        return self.nets_routed / self.nets_requested

    def as_dict(self) -> dict[str, Any]:
        return {
            "nets_routed": self.nets_routed,
            "nets_requested": self.nets_requested,
            "completion": round(self.completion, 6) if self.measured else None,
            "drc_errors": self.drc_errors,
            "exit_code": self.exit_code,
            "wall_time_s": round(self.wall_time_s, 3),
            "reproducible": self.reproducible,
            "error": self.error,
        }

    def summary(self) -> str:
        if not self.measured:
            return f"not measured ({self.error})"
        drc = "n/a" if self.drc_errors is None else str(self.drc_errors)
        return (
            f"{self.nets_routed}/{self.nets_requested} nets "
            f"({self.completion * 100:.1f}%), DRC errors {drc}"
        )


def route_check_accepts(incumbent: RouteCheckOutcome, candidate: RouteCheckOutcome) -> bool:
    """Whether *candidate* may replace *incumbent* (issue #6234).

    Accept only when the candidate's completion is at least the incumbent's
    **and** its DRC error count is no higher. Unknown measurements are
    resolved toward keeping the incumbent:

    * A candidate that could not be measured (route crashed or produced no
      summary) is rejected. So is every candidate when the incumbent itself
      could not be measured, because then there is nothing to compare with.
    * When only one side has a DRC count (DRC failed to run on the other),
      the candidate is rejected. When neither has one, completion alone
      decides.
    """
    if not incumbent.measured or not candidate.measured:
        return False
    if candidate.completion < incumbent.completion:
        return False
    if incumbent.drc_errors is None and candidate.drc_errors is None:
        return True
    if incumbent.drc_errors is None or candidate.drc_errors is None:
        return False
    return candidate.drc_errors <= incumbent.drc_errors


@dataclass
class RouteCheckStage:
    """One gated stage of the acceptance chain, for the report."""

    name: str
    skipped: bool
    accepted: bool
    incumbent: RouteCheckOutcome | None = None
    candidate: RouteCheckOutcome | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "skipped": self.skipped,
            "accepted": self.accepted,
            "incumbent": self.incumbent.as_dict() if self.incumbent else None,
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "note": self.note,
        }


@dataclass
class RouteCheckReport:
    """The whole chain's verdicts, as surfaced in text and ``--format json``."""

    budget: int
    stages: list[RouteCheckStage] = field(default_factory=list)
    routes_run: int = 0
    route_wall_time_s: float = 0.0
    accepted_stage: str = "incumbent"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "budget": self.budget,
            "stages": [s.as_dict() for s in self.stages],
            "routes_run": self.routes_run,
            "route_wall_time_s": round(self.route_wall_time_s, 3),
            "accepted_stage": self.accepted_stage,
            "warnings": list(self.warnings),
        }

    def lines(self) -> list[str]:
        out = [f"Route check (per-net budget {self.budget:,} node expansions):"]
        for stage in self.stages:
            if stage.skipped:
                out.append(f"  {stage.name}: skipped ({stage.note})")
                continue
            verdict = "ACCEPTED" if stage.accepted else "REJECTED -- keeping incumbent"
            assert stage.incumbent is not None and stage.candidate is not None
            out.append(f"  {stage.name}: {verdict}")
            out.append(f"    incumbent: {stage.incumbent.summary()}")
            out.append(f"    candidate: {stage.candidate.summary()}")
        out.append(
            f"  {self.routes_run} check route(s), {self.route_wall_time_s:.1f}s; "
            f"accepted placement: {self.accepted_stage}"
        )
        out.extend(f"  WARNING: {w}" for w in self.warnings)
        return out


def _same(a: PlacementVector, b: PlacementVector) -> bool:
    return a.data.shape == b.data.shape and bool((a.data == b.data).all())


def run_route_check_chain(
    incumbent: PlacementVector,
    stage_a: PlacementVector,
    snap: Callable[[PlacementVector], PlacementVector],
    route: Callable[[PlacementVector], RouteCheckOutcome],
    *,
    budget: int,
    locked_indices: Sequence[int] = (),
) -> tuple[PlacementVector, RouteCheckReport]:
    """Gate the optimizer's stage-A result and the decoupling snap with real routes.

    *route* measures one placement vector, and *snap* applies the decoupling
    snap to one. Both are injected so the chain is unit-testable without a
    router. Returns the accepted vector (one of *incumbent*, *stage_a*, or a
    snap of either) and the report. See the module docstring for the cost
    model: at most three calls to *route*.

    Locked footprints (#6262) never move in any candidate. Each candidate is
    asserted to match *incumbent* at every index in *locked_indices*, so an
    accept or a reject can never move a locked part.
    """
    report = RouteCheckReport(budget=budget)
    cache: dict[bytes, RouteCheckOutcome] = {}

    def measure(vec: PlacementVector) -> RouteCheckOutcome:
        key = vec.data.tobytes()
        if key not in cache:
            outcome = route(vec)
            cache[key] = outcome
            report.routes_run += 1
            report.route_wall_time_s += outcome.wall_time_s
            if not outcome.reproducible:
                report.warnings.append(
                    "a check route hit its wall-clock backstop before its iteration "
                    "budget ('stage deadline fired'), so this verdict is not "
                    "reproducible; raise the backstop or lower --route-check-budget"
                )
        return cache[key]

    def assert_locked(vec: PlacementVector) -> None:
        for i in locked_indices:
            assert (vec.data[4 * i : 4 * i + 3] == incumbent.data[4 * i : 4 * i + 3]).all(), (
                f"route-check candidate moved locked component index {i} (issue #6262)"
            )

    current = incumbent
    current_name = "incumbent"
    for name, build in (
        (STAGE_OPTIMIZE, lambda inc: stage_a),
        (STAGE_DECOUPLING_SNAP, snap),
    ):
        candidate = build(current)
        assert_locked(candidate)
        if _same(candidate, current):
            report.stages.append(
                RouteCheckStage(
                    name, skipped=True, accepted=False, note="no change from the incumbent"
                )
            )
            continue
        inc_outcome = measure(current)
        cand_outcome = measure(candidate)
        accepted = route_check_accepts(inc_outcome, cand_outcome)
        report.stages.append(
            RouteCheckStage(
                name,
                skipped=False,
                accepted=accepted,
                incumbent=inc_outcome,
                candidate=cand_outcome,
            )
        )
        if accepted:
            current, current_name = candidate, name
    report.accepted_stage = current_name
    return current, report


# ---------------------------------------------------------------------------
# The real bounded route
# ---------------------------------------------------------------------------


def route_check_argv(
    pcb: Path,
    output: Path,
    *,
    budget: int,
    seed: int,
    timeout_s: float,
    manufacturer: str | None,
    layers: int | None = None,
) -> list[str]:
    """``kct route`` arguments for one bounded, deterministic check route.

    ``--starting-layers L --max-layers L`` (L = the board's own copper layer
    count) makes the layer ladder a single attempt on the board's own stack,
    so no placement is judged on a stack it was not given.
    ``--no-auto-layers`` would do the same, but on that path ``--format json``
    prints no routing summary, only ``exit_code``/``verdict``.
    ``--no-placement-feedback`` stops the router from moving parts, since it
    is the placement that is being judged. The other switches drop
    report-only or polish stages that cannot change which nets route: the
    routing plan, the sync banner, trace optimisation and the pour oracle
    rounds. They match the switches the #5948 calibration corpus used.
    """
    argv = [
        str(pcb),
        "-o",
        str(output),
        "--format",
        "json",
        "--deterministic-budget",
        "--per-net-iterations",
        str(int(budget)),
        "--deterministic-rescue",
        "--seed",
        str(int(seed)),
        "--timeout",
        f"{float(timeout_s):g}",
        "--skip-drc",
        "--no-optimize",
        "--no-sync-check",
        "--no-placement-feedback",
        "--no-routing-plan",
        "--oracle-rounds",
        "0",
    ]
    if layers:
        if layers in (2, 4, 6):
            argv += ["--starting-layers", str(layers)]
        argv += ["--max-layers", str(layers)]
    if manufacturer:
        argv += ["--mfr", manufacturer]
    return argv


def _parse_summary(stdout: str) -> dict[str, Any] | None:
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        doc = json.loads(stdout[start:])
    except ValueError:
        return None
    summary = doc.get("summary") if isinstance(doc, dict) else None
    return summary if isinstance(summary, dict) else None


def _post_route_drc_errors(routed: Path, source: Path, manufacturer: str | None) -> int | None:
    """The router's post-route DRC error count for *routed*, or None if DRC did not run."""
    from kicad_tools.cli.route_cmd import run_post_route_drc
    from kicad_tools.schema.pcb import PCB

    try:
        layers = len(PCB.load(str(routed)).copper_layers) or 2
        # stdout belongs to the caller (a --format json document), and the
        # checker's advisories (e.g. "rule ... is INACTIVE without a
        # net-class-map") would repeat once per check route: keep both quiet.
        # The number that matters is the returned error count.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            errors, _warnings = run_post_route_drc(
                output_path=routed,
                manufacturer=manufacturer or "jlcpcb",
                layers=layers,
                quiet=True,
                source_pcb_path=source,
            )
    except Exception:  # noqa: BLE001 -- an unmeasured DRC is reported, not raised
        return None
    return None if errors < 0 else int(errors)


def bounded_route(
    pcb: Path,
    *,
    budget: int = DEFAULT_ROUTE_CHECK_BUDGET,
    seed: int = 0,
    timeout_s: float = DEFAULT_ROUTE_CHECK_TIMEOUT_S,
    manufacturer: str | None = None,
    workdir: Path | None = None,
) -> RouteCheckOutcome:
    """Route *pcb* once under an iteration budget and measure the result.

    The router runs in a child process (``python -m kicad_tools.cli route``)
    with its output captured, so its prose and its signal handling never
    reach the caller. The routed board and its sidecars are written into
    *workdir* (a fresh temporary directory when None), which the caller owns.
    """
    own_dir = workdir is None
    work = Path(tempfile.mkdtemp(prefix="kct-route-check-")) if workdir is None else workdir
    output = work / f"{pcb.stem}.routed.kicad_pcb"
    try:
        from kicad_tools.schema.pcb import PCB

        layers: int | None = len(PCB.load(str(pcb)).copper_layers) or None
    except Exception:  # noqa: BLE001 -- let the router auto-detect
        layers = None
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        *route_check_argv(
            pcb,
            output,
            budget=budget,
            seed=seed,
            timeout_s=timeout_s,
            manufacturer=manufacturer,
            layers=layers,
        ),
    ]
    t0 = time.perf_counter()
    try:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # The router enforces --timeout itself; this only guards a hang
                # in startup or teardown.
                timeout=timeout_s * 2 + 120,
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
            )
        except subprocess.TimeoutExpired:
            return RouteCheckOutcome(
                0, 0, None, None, time.perf_counter() - t0, False, "route hung past its backstop"
            )
        log = (res.stdout or "") + (res.stderr or "")
        reproducible = not deadline_fired(log)
        summary = _parse_summary(res.stdout or "")
        if summary is None or "nets_requested" not in summary:
            tail = (res.stderr or "").strip().splitlines()[-1:] or [""]
            return RouteCheckOutcome(
                0,
                0,
                None,
                res.returncode,
                time.perf_counter() - t0,
                reproducible,
                f"no route summary (exit {res.returncode}): {tail[0][:160]}",
            )
        requested = int(summary.get("nets_requested") or 0)
        routed = int(summary.get("nets_routed") or 0)
        drc: int | None = 0
        if output.exists():
            drc = _post_route_drc_errors(output, pcb, manufacturer)
        elif routed > 0:
            drc = None  # routed something but wrote no board: cannot judge DRC
        return RouteCheckOutcome(
            routed,
            requested,
            drc,
            res.returncode,
            time.perf_counter() - t0,
            reproducible,
        )
    finally:
        if own_dir:
            shutil.rmtree(work, ignore_errors=True)
