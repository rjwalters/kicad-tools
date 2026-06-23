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
    "RescueConfig",
    "all_net_names",
    "build_rescue_command",
    "complete_unfinished_nets",
    "partially_connected_signal_nets",
    "strip_net_copper",
    "rescue_partial_nets",
]


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
) -> list[str]:
    """Build the ``kct route`` argv for one single-net rescue stage."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(routed_path),
        "--output",
        str(output_path),
        "--preserve-existing",
        "--auto-layers",
        "--starting-layers",
        str(config.starting_layers),
        "--max-layers",
        str(config.max_layers),
        "--manufacturer",
        config.manufacturer,
    ]
    if config.micro_via_in_pad_fallback:
        cmd.append("--micro-via-in-pad-fallback")
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
    cmd.extend(
        [
            "--skip-nets",
            ",".join(skip_nets),
        ]
    )
    cmd.extend(config.extra_args)
    return cmd


def complete_unfinished_nets(
    routed_path: Path,
    config: RescueConfig,
    *,
    max_passes: int = 3,
    pass_timeout_s: int = 600,
    quiet: bool = False,
) -> list[tuple[int, int]]:
    """Batch completion passes: route ALL unfinished nets together.

    The single-net rescue loop (:func:`rescue_partial_nets`) fails on
    dense boards (measured on chorus-test-revA, issue #3474 R2: 0/6
    rescues) because a net routed alone cannot negotiate with the
    PRESERVED copper of the strictly-routed nets -- the relief machinery
    correctly reports "blocked only by non-rippable copper" and rolls
    back.  Routing every unfinished net *together* in one
    ``--preserve-existing`` pass keeps negotiation alive among the
    unfinished cohort while still protecting the finished nets' copper.

    This also fixes the budget shape of the escalation ladder: each
    ladder attempt re-routes the whole board from scratch and times out
    mid-queue, re-spending its stage budget on the same head-of-queue
    nets five times over.  A completion pass starts from the committed
    copper, so its entire budget goes to nets that still need work.

    Each pass:

    1. Detects unfinished signal nets (checker-based, partial AND
       unrouted, pour nets excluded).
    2. Strips their stranded copper (the #3470 overlap-stub lesson).
    3. Routes them together against the preserved copper of everything
       else (fresh ``kct route`` subprocess, same recipe knobs).
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
        List of ``(unfinished_before, unfinished_after)`` tuples, one
        per executed pass.
    """
    import shutil

    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    _log("\n" + "=" * 60)
    _log("Completion passes for unfinished nets (Issue #3474 R2)...")
    _log("=" * 60)

    history: list[tuple[int, int]] = []
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

        skip = [n for n in all_net_names(routed_path) if n not in set(targets)]
        tmp_out = routed_path.with_name(routed_path.stem + "_completion.kicad_pcb")
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
        cmd = build_rescue_command(routed_path, tmp_out, skip, pass_config)
        subprocess.run(cmd, capture_output=True, text=True)

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
        history.append((len(targets), len(remaining)))
        _log(
            f"   Pass {pass_index + 1} result: {len(targets)} -> {len(remaining)} unfinished net(s)"
        )

        if len(remaining) >= len(targets):
            _log("   No progress; restoring pre-pass board and stopping.")
            shutil.copyfile(backup, routed_path)
            backup.unlink(missing_ok=True)
            break

        backup.unlink(missing_ok=True)
        if not remaining:
            break

    return history


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

    for net in partial:
        skip = [n for n in all_nets if n != net]
        tmp_out = routed_path.with_name(routed_path.stem + "_rescue.kicad_pcb")
        cmd = build_rescue_command(routed_path, tmp_out, skip, config)
        result = subprocess.run(cmd, capture_output=True, text=True)

        if not tmp_out.exists():
            _log(f"   Rescue {net}: FAILED (no output produced)")
            results[net] = False
            continue

        # Promote the rescue output; on failure strip the net's copper
        # so no stranded stubs remain.
        tmp_out.replace(routed_path)
        if result.returncode == 0:
            _log(f"   Rescue {net}: SUCCESS (fully connected)")
            results[net] = True
        else:
            removed = strip_net_copper(routed_path, [net])
            _log(
                f"   Rescue {net}: failed (exit {result.returncode}); "
                f"stripped {removed} stub block(s)"
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
    return results
