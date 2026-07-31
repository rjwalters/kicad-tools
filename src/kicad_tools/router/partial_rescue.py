"""Per-net rescue loop for partially-routed signal nets (Issues #3471/#3474).

The negotiated initial pass + rip-up settles at a valid state that can
leave a cluster of signal nets *partially* routed: copper exists (escape
stubs, some pad-pairs), but at least one pad is unreached because the
net lost the multi-net congestion negotiation.  On chorus-test-revA this
is the dominant failure mode (issue #3474 Phase R2: ~26 of 51 nets stuck
at 1/N pads); on board 05 it was the residual ISENSE/PWM cluster
(issue #3471).

Re-routing each residual net ALONE -- with every other net's copper
preserved as immutable obstacles -- sidesteps the negotiation entirely
and lands a subset of the cluster that the global pass cannot extract:

* ``--preserve-existing`` keeps all other nets' copper (#3155),
* ``--skip-nets`` everything except the rescue target,
* ALL partial nets' stranded copper is stripped upfront so the rescue
  A* starts from a clean slate (a stale partial stub of a later rescue
  target is an immutable obstacle for the current one, and stranded
  stubs are the #3470 defect-2 overlapping-copper DRC liability),
* a FAILED rescue strips the target's copper again -- a rescue never
  leaves stranded stubs and never makes the board worse (strict reach
  counts fully-connected nets only; partial nets count as unrouted
  either way).

This module is the reusable generalization of the recipe-side loop that
shipped in ``boards/05-bldc-motor-controller/design.py`` (step 6b,
``rescue_partial_nets``, PR #3491).  Board recipes and tests should call
:func:`rescue_partial_nets` with board-specific knobs (manufacturer,
excluded pour nets, seed, budgets) instead of re-implementing the loop.

Each rescue stage is a fresh ``kct route`` subprocess so the rescue A*
sees exactly the same loading path as the main recipe (zones, escape
stubs, manufacturer rules).  Stages are independently budgeted; the
total loop cost is bounded by ``stage_timeout * len(partial_nets)``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CompletionResult",
    "RescueConfig",
    "UnroutableLink",
    "all_net_names",
    "build_rescue_command",
    "complete_unfinished_nets",
    "partially_connected_signal_nets",
    "strip_net_copper",
    "rescue_partial_nets",
]


@dataclass
class UnroutableLink:
    """One still-unroutable link parsed from a ``--complete-report`` (issue #4478).

    Mirrors the per-link entries emitted by
    :func:`kicad_tools.cli.route_cmd._build_complete_link_report` so a caller of
    :func:`complete_unfinished_nets` can tell *why* a completion pass left a
    link open (closed vs. unroutable-with-blockers) without re-parsing the raw
    JSON.  ``blocking_copper`` non-empty is the "blocked by non-rippable copper"
    signal the old grid-engine rescue could only report opaquely.
    """

    net: str
    reason: str
    start: str | None = None
    end: str | None = None
    deadline_hit: bool = False
    blocking_copper: tuple[str, ...] = ()
    nearest_blocker_mm: float | None = None
    stuck_classification: str | None = None
    tier_limit_note: str | None = None

    @classmethod
    def from_report_entry(cls, entry: dict) -> UnroutableLink:
        link = entry.get("link") or {}
        return cls(
            net=entry.get("net", ""),
            reason=entry.get("reason", ""),
            start=link.get("start") if isinstance(link, dict) else None,
            end=link.get("end") if isinstance(link, dict) else None,
            deadline_hit=bool(entry.get("deadline_hit", False)),
            blocking_copper=tuple(entry.get("blocking_copper") or ()),
            nearest_blocker_mm=entry.get("nearest_blocker_mm"),
            stuck_classification=entry.get("stuck_classification"),
            tier_limit_note=entry.get("tier_limit_note"),
        )


@dataclass
class CompletionResult:
    """Structured outcome of a batch :func:`complete_unfinished_nets` run (#4478).

    Extends the legacy ``list[(before, after)]`` progress history with the
    Phase-4 per-link report so rescue outcomes (closed vs. unroutable-with-
    blockers) propagate to callers instead of being discarded with the
    subprocess output.  ``bool(result)`` and iteration/len forward to
    :attr:`history` so existing call sites that treated the return as the
    progress list keep working.
    """

    #: (unfinished_before, unfinished_after) per executed pass -- the legacy
    #: history shape.
    history: list[tuple[int, int]] = field(default_factory=list)
    #: Links still unroutable after the final executed pass, parsed from that
    #: pass's ``--complete-report``.  Empty when every link closed (or when no
    #: report was produced -- a pass that closed everything writes none).
    unroutable_links: list[UnroutableLink] = field(default_factory=list)

    def __iter__(self):
        return iter(self.history)

    def __len__(self) -> int:
        return len(self.history)

    def __getitem__(self, index):
        return self.history[index]

    def __bool__(self) -> bool:
        return bool(self.history)

    def __eq__(self, other: object) -> bool:
        # Backward-compat: allow ``result == [(2, 1), (1, 0)]`` comparisons.
        if isinstance(other, list):
            return self.history == other
        if isinstance(other, CompletionResult):
            return self.history == other.history and self.unroutable_links == other.unroutable_links
        return NotImplemented


@dataclass
class RescueConfig:
    """Knobs for one board's rescue loop.

    Defaults match the chorus-test-revA pinned recipe (issue #3474);
    board 05 uses ``manufacturer="jlcpcb-tier1", seed=7,
    micro_via_in_pad_fallback=True``.
    """

    manufacturer: str = "jlcpcb-tier1"
    backend: str = "cpp"
    seed: int = 42
    #: Wall budget per rescue stage (one net).  300 s bounds the #3485
    #: budget-leak overshoot inside escape/rip-up phases.
    stage_timeout_s: int = 300
    #: Per-net A* budget inside the stage (wall-clock).  Ignored when
    #: :attr:`deterministic_budget` is set (issue #3877): the wall-clock
    #: per-net cutoff is what makes a rescue/completion pass load-dependent
    #: (a slow/loaded machine cuts a per-net A* short and lands less
    #: copper), so for a reproducible-across-machines route the chorus
    #: recipe sets ``deterministic_budget=True`` and drops this cutoff in
    #: favour of the fixed C++ iteration backstop (#3538).
    per_net_timeout_s: int = 60
    #: Issue #3877: replace the wall-clock ``--per-net-timeout`` with
    #: ``--deterministic-budget`` on every rescue/completion ``kct route``
    #: subprocess.  The flag pins the C++ A* node-expansion backstop to a
    #: fixed count (12M) so each per-net search aborts after the SAME
    #: amount of work on every machine, making the rescued copper
    #: reproducible regardless of load.  The outer ``--timeout``
    #: (``stage_timeout_s`` / ``pass_timeout_s``) is retained only as a
    #: safety backstop.  Off by default to preserve legacy behaviour for
    #: callers that have not re-measured their floor.
    deterministic_budget: bool = False
    starting_layers: int = 4
    max_layers: int = 4
    #: Pour/skip nets carried by copper zones -- excluded from rescue
    #: and from partial-net detection (their connectivity is by zone
    #: fill, which the trace-connectivity checker does not credit).
    excluded_nets: frozenset[str] = field(default_factory=frozenset)
    micro_via_in_pad_fallback: bool = False
    #: Issue #4528: emit ``--allow-unsafe-grid`` on every rescue/completion
    #: ``kct route`` subprocess.  A board whose MAIN pass opts into the
    #: memory-forced coarse grid (grid > clearance/2, the #3911 auto-grid
    #: safety gate) MUST set this so the rescue subprocess inherits the same
    #: documented opt-in -- otherwise ``kct route`` refuses the grid and exits
    #: 1 before routing anything, and the rescue loop reports bogus
    #: ``no_output`` failures for every net.  Off by default: a board that
    #: never opts into the unsafe grid on its main pass emits byte-identical
    #: rescue argv (no flag).
    allow_unsafe_grid: bool = False
    #: Extra args appended verbatim to each ``kct route`` invocation.
    extra_args: tuple[str, ...] = ()


def all_net_names(pcb_path: Path) -> list[str]:
    """Parse all named nets from the PCB's ``(net N "NAME")`` declarations."""
    text = pcb_path.read_text()
    names = {m.group(2) for m in re.finditer(r'\(net (\d+) "([^"]+)"\)', text)}
    return sorted(n for n in names if n)


def partially_connected_signal_nets(
    pcb_path: Path,
    *,
    manufacturer: str = "jlcpcb-tier1",
    excluded_nets: frozenset[str] = frozenset(),
    include_unrouted: bool = False,
) -> list[str]:
    """Return signal nets whose pads are not all trace-connected.

    Runs ``kct check`` (connectivity is an advisory rule, so this never
    interferes with the blocking-DRC gate) and parses the
    "Net 'X' is partially routed" messages, excluding *excluded_nets*
    (pour-carried power nets).

    With ``include_unrouted=True`` the "is not routed" class is included
    too -- useful when the rescue loop should also attempt nets the main
    pass never reached (chorus's budget-starved tail).
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(pcb_path),
            "--mfr",
            manufacturer,
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    partial: list[str] = []
    for v in data.get("violations", data.get("errors", [])):
        rule = v.get("rule_id") or v.get("rule") or v.get("type")
        if rule != "connectivity":
            continue
        msg = v.get("message", "")
        if "'" not in msg:
            continue
        is_partial = "partially routed" in msg
        is_unrouted = "is not routed" in msg or "unrouted" in msg
        if not (is_partial or (include_unrouted and is_unrouted)):
            continue
        net = msg.split("'")[1]
        if net not in excluded_nets and not net.startswith("unconnected-"):
            partial.append(net)
    return sorted(set(partial))


def strip_net_copper(pcb_path: Path, net_names: list[str]) -> int:
    """Remove all top-level ``(segment ...)``/``(via ...)`` copper for *net_names*.

    Zones, pads, and footprints are untouched.  Returns the number of
    copper blocks removed.  Used by the rescue loop so a stranded
    partial route does not poison the rescue A* (and so a FAILED rescue
    leaves no stub copper behind -- the #3470 overlap-stub lesson).
    """
    text = pcb_path.read_text()
    net_ids = {
        m.group(1)
        for m in re.finditer(r'\(net (\d+) "([^"]+)"\)', text)
        if m.group(2) in set(net_names)
    }
    if not net_ids:
        return 0

    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"^\t\((?:segment|via)\b", text, re.MULTILINE):
        start = m.start()
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        end = i + 1
        if end < len(text) and text[end] == "\n":
            end += 1
        block = text[start:end]
        net_match = re.search(r"\(net (\d+)\)", block)
        if net_match and net_match.group(1) in net_ids:
            spans.append((start, end))

    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    pcb_path.write_text(text)
    return len(spans)


def build_rescue_command(
    routed_path: Path,
    output_path: Path,
    skip_nets: list[str],
    config: RescueConfig,
    *,
    complete: bool = False,
    complete_report: Path | None = None,
) -> list[str]:
    """Build the ``kct route`` argv for one rescue/completion stage.

    Two shapes, selected by *complete* (issue #4478, epic #4465 Phase 5):

    * ``complete=False`` (default, single-net rescue path unchanged): route
      one net against every OTHER net's preserved copper by naming the others
      in ``--skip-nets``.  This shells the coarse uniform-grid A* via
      ``--auto-layers`` -- the legacy rescue mechanism (#3471).

    * ``complete=True`` (batch completion, walled-pocket case): shell
      ``kct route --complete`` instead.  ``--complete`` auto-detects the
      currently-unconnected signal nets (the SAME
      :func:`partially_connected_signal_nets` detector the caller uses, so the
      target sets agree by construction) and routes ONLY those links on the
      lattice engine against all other copper held fixed -- the engine that can
      thread a walled pocket / place a via-in-pad, which the grid A* cannot.

    ``--complete`` is mutually exclusive with ``--skip-nets`` / ``--nets``
    (``route_cmd._resolve_complete_nets`` hard-errors exit 2), so the
    completion shape drops the skip-list entirely and relies on ``--complete``'s
    auto-detection.  ``--complete`` also already implies ``--preserve-existing``
    and disables ``--auto-layers`` unless explicitly passed, so the completion
    shape omits ``--auto-layers`` (completion routes within the committed layer
    stack -- issue #4477).  When *complete_report* is given, the structured
    per-link JSON report (issue #4477 Phase 4) is written there for the caller
    to consume.
    """
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(routed_path),
        "--output",
        str(output_path),
        "--preserve-existing",
    ]
    if not complete:
        # Grid-engine rescue shape: whole-board layer escalation is fine here
        # because a single net is being routed against everything else fixed.
        # --complete deliberately OMITS --auto-layers (it disables it anyway)
        # so completion stays within the committed layer stack (#4477).
        cmd.append("--auto-layers")
    cmd.extend(
        [
            "--starting-layers",
            str(config.starting_layers),
            "--max-layers",
            str(config.max_layers),
            "--manufacturer",
            config.manufacturer,
        ]
    )
    if config.micro_via_in_pad_fallback:
        cmd.append("--micro-via-in-pad-fallback")
    # Issue #4528: inherit the main pass's #3911 auto-grid opt-in.  The gate
    # itself is grid-engine-only, but ``kct route`` accepts the flag regardless
    # of engine, so it is safe to emit for both the rescue (grid A*) and the
    # completion (lattice) shape.  Without it, a board whose main pass forced a
    # coarse grid sees every rescue subprocess exit 1 at the CLI gate before
    # routing anything (bogus ``no_output`` failures).
    if config.allow_unsafe_grid:
        cmd.append("--allow-unsafe-grid")
    cmd.extend(
        [
            "--backend",
            config.backend,
            "--seed",
            str(config.seed),
            "--timeout",
            str(config.stage_timeout_s),
        ]
    )
    # Issue #3877: deterministic-budget mode replaces the wall-clock
    # per-net cutoff with the fixed C++ iteration backstop so the rescued
    # copper is reproducible regardless of machine load.  The outer
    # ``--timeout`` above is kept only as a safety backstop.
    if config.deterministic_budget:
        cmd.append("--deterministic-budget")
    else:
        cmd.extend(["--per-net-timeout", str(config.per_net_timeout_s)])
    if complete:
        # #4478: --complete self-selects the stranded nets; a skip/nets list
        # would trip the mutual-exclusivity guard.  Never emit one here.
        cmd.append("--complete")
        if complete_report is not None:
            cmd.extend(["--complete-report", str(complete_report)])
    else:
        cmd.extend(
            [
                "--skip-nets",
                ",".join(skip_nets),
            ]
        )
    cmd.extend(config.extra_args)
    return cmd


def _parse_complete_report(report_path: Path) -> list[UnroutableLink]:
    """Parse a ``--complete-report`` JSON into :class:`UnroutableLink` records.

    Returns an empty list when the file is absent (the CLI only writes it when
    a link stays unroutable) or malformed -- a diagnostic report must never
    break the completion loop it is reporting on.
    """
    if not report_path.exists():
        return []
    try:
        data = json.loads(report_path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return []
    return [
        UnroutableLink.from_report_entry(entry)
        for entry in data.get("unroutable_links", [])
        if isinstance(entry, dict)
    ]


def complete_unfinished_nets(
    routed_path: Path,
    config: RescueConfig,
    *,
    max_passes: int = 3,
    pass_timeout_s: int = 600,
    quiet: bool = False,
) -> CompletionResult:
    """Batch completion passes: route ALL unfinished nets together.

    The single-net rescue loop (:func:`rescue_partial_nets`) fails on
    dense boards (measured on chorus-test-revA, issue #3474 R2: 0/6
    rescues) because a net routed alone cannot negotiate with the
    PRESERVED copper of the strictly-routed nets -- the relief machinery
    correctly reports "blocked only by non-rippable copper" and rolls
    back.  Routing every unfinished net *together* in one
    ``--preserve-existing`` pass keeps negotiation alive among the
    unfinished cohort while still protecting the finished nets' copper.

    Issue #4478 (epic #4465 Phase 5): each pass now shells
    ``kct route --complete`` instead of the coarse grid-engine
    ``--skip-nets`` shape.  ``--complete`` routes the stranded links on the
    LATTICE engine -- the only engine that can thread a walled SMD pocket or
    place a last-resort via-in-pad (Phase 3, #4475) -- so a completion pass can
    finally close the walled pads the grid A* traded 1:1 forever (#4434).  The
    per-link ``--complete-report`` (Phase 4, #4477) is captured and surfaced on
    the returned :class:`CompletionResult` so callers see *why* a link stayed
    open (closed vs. unroutable-with-blockers).

    This also fixes the budget shape of the escalation ladder: each
    ladder attempt re-routes the whole board from scratch and times out
    mid-queue, re-spending its stage budget on the same head-of-queue
    nets five times over.  A completion pass starts from the committed
    copper, so its entire budget goes to nets that still need work.

    Each pass:

    1. Detects unfinished signal nets (checker-based, partial AND
       unrouted, pour nets excluded).
    2. Strips their stranded copper (the #3470 overlap-stub lesson) -- which
       also makes ``--complete``'s own auto-detection (the SAME detector) name
       exactly this cohort.
    3. Routes them together via ``kct route --complete`` against the preserved
       copper of everything else (fresh subprocess, same recipe knobs).
    4. Keeps the result only if the unfinished count went DOWN;
       otherwise restores the pre-pass board byte-for-byte and stops.

    Args:
        routed_path: Routed PCB, repaired in place.
        config: Board-specific knobs (the per-stage ``stage_timeout_s``
            is ignored here in favour of *pass_timeout_s*).
        max_passes: Upper bound on completion passes.  Loop exits early
            on convergence (no unfinished nets) or no progress.
        pass_timeout_s: Wall budget per completion pass.
        quiet: Suppress progress prints.

    Returns:
        A :class:`CompletionResult` carrying the ``(before, after)`` progress
        history (list-compatible for legacy callers) and the per-link
        unroutable-link report from the final executed pass.
    """
    import shutil

    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    _log("\n" + "=" * 60)
    _log("Completion passes for unfinished nets (Issue #3474 R2 / #4478)...")
    _log("=" * 60)

    result = CompletionResult()
    for pass_index in range(max_passes):
        targets = partially_connected_signal_nets(
            routed_path,
            manufacturer=config.manufacturer,
            excluded_nets=config.excluded_nets,
            include_unrouted=True,
        )
        if not targets:
            _log(f"\n   Pass {pass_index + 1}: all signal nets connected -- done.")
            break

        _log(f"\n   Pass {pass_index + 1}: {len(targets)} unfinished net(s): {', '.join(targets)}")

        # Byte-for-byte backup so a no-progress pass can be discarded
        # entirely (including its freshly-stranded stubs).
        backup = routed_path.with_name(routed_path.stem + "_prepass.kicad_pcb")
        shutil.copyfile(routed_path, backup)

        stripped = strip_net_copper(routed_path, targets)
        _log(f"   Stripped {stripped} stale copper block(s)")

        tmp_out = routed_path.with_name(routed_path.stem + "_completion.kicad_pcb")
        report_path = routed_path.with_name(routed_path.stem + "_complete_report.json")
        report_path.unlink(missing_ok=True)
        pass_config = RescueConfig(
            manufacturer=config.manufacturer,
            backend=config.backend,
            seed=config.seed,
            stage_timeout_s=pass_timeout_s,
            per_net_timeout_s=config.per_net_timeout_s,
            deterministic_budget=config.deterministic_budget,
            starting_layers=config.starting_layers,
            max_layers=config.max_layers,
            excluded_nets=config.excluded_nets,
            micro_via_in_pad_fallback=config.micro_via_in_pad_fallback,
            extra_args=config.extra_args,
        )
        # Issue #4478: shell ``kct route --complete`` (lattice engine) rather
        # than the grid-engine ``--skip-nets`` shape.  --complete self-selects
        # the stranded nets (same detector as ``targets`` above), so NO skip
        # list is passed (it would trip the mutual-exclusivity guard).
        cmd = build_rescue_command(
            routed_path,
            tmp_out,
            [],
            pass_config,
            complete=True,
            complete_report=report_path,
        )
        subprocess.run(cmd, capture_output=True, text=True)

        # Capture the per-link report BEFORE promoting/cleaning up.  A pass
        # that closed every link writes no report (empty list); a pass that
        # left a walled pad open writes one with blocking_copper populated.
        pass_links = _parse_complete_report(report_path)
        report_path.unlink(missing_ok=True)

        if not tmp_out.exists():
            _log("   Pass produced no output; restoring pre-pass board.")
            shutil.copyfile(backup, routed_path)
            backup.unlink(missing_ok=True)
            break

        tmp_out.replace(routed_path)
        for stray in (
            tmp_out.with_suffix(".kicad_prl"),
            tmp_out.with_name(tmp_out.stem + "_partial.kicad_pcb"),
        ):
            stray.unlink(missing_ok=True)

        remaining = partially_connected_signal_nets(
            routed_path,
            manufacturer=config.manufacturer,
            excluded_nets=config.excluded_nets,
            include_unrouted=True,
        )
        result.history.append((len(targets), len(remaining)))
        result.unroutable_links = pass_links
        _log(
            f"   Pass {pass_index + 1} result: {len(targets)} -> {len(remaining)} unfinished net(s)"
        )
        if pass_links:
            _log(
                f"   {len(pass_links)} link(s) still unroutable: "
                + ", ".join(
                    f"{lk.net}"
                    + (
                        f" (blocked by {', '.join(lk.blocking_copper)})"
                        if lk.blocking_copper
                        else ""
                    )
                    for lk in pass_links
                )
            )

        # Issue #4478 / AC item 4: a ``--complete`` pass that closed some but
        # not all links still makes progress (count went down) and is kept;
        # only a pass that did NOT reduce the unfinished count is discarded.
        if len(remaining) >= len(targets):
            _log("   No progress; restoring pre-pass board and stopping.")
            shutil.copyfile(backup, routed_path)
            backup.unlink(missing_ok=True)
            break

        backup.unlink(missing_ok=True)
        if not remaining:
            break

    return result


def rescue_partial_nets(
    routed_path: Path,
    config: RescueConfig,
    *,
    nets: list[str] | None = None,
    quiet: bool = False,
) -> dict[str, bool]:
    """Rescue partially-routed signal nets one at a time, in place.

    *routed_path* is mutated: successful rescues add the net's copper;
    failed rescues leave the net with NO copper (stripped stubs).

    Args:
        routed_path: Routed PCB to repair in place.
        config: Board-specific knobs.
        nets: Explicit rescue targets.  Default: auto-detect via
            :func:`partially_connected_signal_nets` (partial only --
            unrouted nets without stubs are better served by another
            main-pass attempt, but callers may pass them explicitly).
        quiet: Suppress progress prints.

    Returns:
        Mapping of rescue-target net name -> True (fully connected after
        rescue) / False (rescue failed; net left with no copper).
    """

    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    _log("\n" + "=" * 60)
    _log("Rescuing partially-routed nets (Issues #3471/#3474)...")
    _log("=" * 60)

    partial = (
        list(nets)
        if nets is not None
        else partially_connected_signal_nets(
            routed_path,
            manufacturer=config.manufacturer,
            excluded_nets=config.excluded_nets,
        )
    )
    if not partial:
        _log("\n   No partially-routed signal nets -- nothing to rescue.")
        return {}

    _log(f"\n   Rescue targets ({len(partial)}): {', '.join(partial)}")
    all_nets = all_net_names(routed_path)
    results: dict[str, bool] = {}

    # Strip ALL targets' stranded copper upfront: a stale partial stub
    # of net B is a preserved (immutable) obstacle during net A's rescue
    # and measurably blocks rescues that succeed on the stripped board.
    # Stripping is loss-free for the strict-reach metric and removes the
    # stranded-stub DRC liability (#3470 defect 2).
    stripped_total = strip_net_copper(routed_path, partial)
    _log(f"   Stripped {stripped_total} stale copper block(s) for {len(partial)} net(s)")

    # Issue #4469: accumulate a concrete failure reason per stranded net so the
    # loop can print a reason table instead of the opaque "FAILED (no output
    # produced)".  Reasons are parsed from the rescue subprocess's captured
    # output by :func:`classify_rescue_failure` (diagnose-only, never re-routes).
    from kicad_tools.router.rescue_diagnostics import (
        RescueFailureReason,
        classify_rescue_failure,
        format_rescue_reason_table,
    )

    failure_reasons: list[RescueFailureReason] = []

    for net in partial:
        skip = [n for n in all_nets if n != net]
        tmp_out = routed_path.with_name(routed_path.stem + "_rescue.kicad_pcb")
        cmd = build_rescue_command(routed_path, tmp_out, skip, config)
        result = subprocess.run(cmd, capture_output=True, text=True)

        if not tmp_out.exists():
            reason = classify_rescue_failure(
                net, result.stdout or "", result.stderr or "", output_produced=False
            )
            failure_reasons.append(reason)
            _log(f"   Rescue {net}: FAILED -- {reason.category.value}: {reason.detail}")
            results[net] = False
            continue

        # Promote the rescue output; on failure strip the net's copper
        # so no stranded stubs remain.
        tmp_out.replace(routed_path)
        if result.returncode == 0:
            _log(f"   Rescue {net}: SUCCESS (fully connected)")
            results[net] = True
        else:
            reason = classify_rescue_failure(
                net, result.stdout or "", result.stderr or "", output_produced=True
            )
            failure_reasons.append(reason)
            removed = strip_net_copper(routed_path, [net])
            _log(
                f"   Rescue {net}: FAILED -- {reason.category.value}: {reason.detail} "
                f"(exit {result.returncode}; stripped {removed} stub block(s))"
            )
            results[net] = False

        # Clean up per-stage side files (``kct route`` writes a
        # .kicad_prl next to its output, and a *_partial.kicad_pcb on
        # partial exits).
        for stray in (
            tmp_out.with_suffix(".kicad_prl"),
            tmp_out.with_name(tmp_out.stem + "_partial.kicad_pcb"),
        ):
            stray.unlink(missing_ok=True)

    rescued = sum(1 for ok in results.values() if ok)
    _log(f"\n   Rescue summary: {rescued}/{len(results)} net(s) rescued")
    # Issue #4469 AC1: emit the per-stranded-net reason table (replaces the old
    # opaque "FAILED (no output produced)" line).
    table = format_rescue_reason_table(failure_reasons)
    if table:
        _log(table)
    return results
