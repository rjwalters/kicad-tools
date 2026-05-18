"""
PCB autorouting CLI command.

Provides command-line access to the autorouter:

    kicad-tools route board.kicad_pcb
    kicad-tools route board.kicad_pcb -o board_routed.kicad_pcb
    kicad-tools route board.kicad_pcb --skip-nets GND,VCC --strategy negotiated

Performance Profiling:

    Use --profile to measure routing performance and identify bottlenecks:

    # Profile routing and save results
    kicad-tools route board.kicad_pcb --profile

    # Specify custom output file
    kicad-tools route board.kicad_pcb --profile --profile-output my_profile.prof

    # Analyze results with pstats
    python -m pstats route_profile.prof

    # Visualize with snakeviz (pip install snakeviz)
    snakeviz route_profile.prof

Layer Stack Configuration:

    By default, the autorouter uses a 2-layer configuration (F.Cu, B.Cu).
    For multi-layer boards, use the --layers option:

    # 4-layer board with GND/PWR planes (typical for Pi HAT, Arduino shields)
    kicad-tools route board.kicad_pcb --layers 4

    # 4-layer with 2 signal layers (for high-density routing)
    kicad-tools route board.kicad_pcb --layers 4-sig

    # 4-layer with all 4 signal layers (no planes, maximum routing resources)
    kicad-tools route board.kicad_pcb --layers 4-all

    # 6-layer with 4 signal layers
    kicad-tools route board.kicad_pcb --layers 6

    Layer stack configurations:
    - '2': F.Cu (signal), B.Cu (signal)
    - '4': F.Cu (signal), In1.Cu (GND plane), In2.Cu (PWR plane), B.Cu (signal)
    - '4-sig': F.Cu (signal), In1.Cu (signal), In2.Cu (GND plane), B.Cu (mixed)
    - '4-all': F.Cu (signal), In1.Cu (signal), In2.Cu (signal), B.Cu (signal)
    - '6': F.Cu, In1.Cu (GND), In2.Cu (signal), In3.Cu (signal), In4.Cu (PWR), B.Cu

    For 4-layer boards with inner planes (--layers 4), signals are routed on
    the outer layers (F.Cu and B.Cu) with vias providing layer transitions
    through the planes. This is the most common configuration for hobby/small
    production boards.
"""

import argparse
import contextlib
import logging
import math
import os
import random
import shutil
import signal
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router import Autorouter, LayerStack

# Issue #3035: ``_auto_skip_pour_nets`` was promoted to a public helper at
# ``kicad_tools.router.auto_pour.auto_skip_pour_nets`` so in-process router
# callers (board generate_design.py scripts using
# ``router.route_all_negotiated()`` instead of subprocessing ``kct route``)
# can reach it without importing a CLI private.  The alias here is
# load-bearing: the 4 call sites in this module (and the
# ``@patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", ...)`` decorators
# in ``tests/test_layer_escalation.py`` and ``tests/test_route_auto_fix.py``)
# all reference the underscore name; this re-import preserves the symbol so
# they continue to resolve to the public implementation transparently.  Do
# not remove this alias without also updating those patch targets.
from kicad_tools.router.auto_pour import auto_skip_pour_nets as _auto_skip_pour_nets  # noqa: E402

logger = logging.getLogger(__name__)


# =============================================================================
# Issue #2802: Total wall-clock deadline helpers
# =============================================================================
#
# ``--timeout`` is intended to be a *total* wall-clock budget for the whole
# routing invocation, but historically the orchestration layer re-used it as a
# per-stage budget: every layer-escalation attempt, every placement-feedback
# iteration, and every auto-fix pass received its own fresh copy of the same
# ``args.timeout`` value, so worst-case wall-clock for a single ``kct route``
# invocation could exceed 10x the configured budget.
#
# The fix is a single monotonic deadline computed once in ``main()`` and
# threaded through every outer-loop site via these helpers.  The deadline is
# stored on the parsed ``args`` namespace as ``_wall_clock_deadline`` so it
# travels naturally with the other CLI parameters.  When ``--timeout`` is not
# set the deadline is ``None`` and these helpers preserve legacy unbounded
# behaviour.


def _set_wall_clock_deadline(args) -> None:
    """Stamp a monotonic deadline on ``args`` from ``args.timeout``.

    Called once near the start of ``main()`` (after argparse).  If
    ``args.timeout`` is falsy (None, 0, or negative) the deadline is set
    to ``None`` so the rest of the orchestration treats the run as
    unbounded.
    """
    timeout = getattr(args, "timeout", None)
    if timeout and timeout > 0:
        args._wall_clock_deadline = time.monotonic() + float(timeout)
    else:
        args._wall_clock_deadline = None


def _remaining_budget(args) -> float | None:
    """Return seconds remaining vs the total wall-clock deadline.

    Returns ``None`` when no deadline is configured (legacy unbounded
    behaviour).  Returns a non-negative float otherwise; callers should
    treat zero as "deadline expired."
    """
    deadline = getattr(args, "_wall_clock_deadline", None)
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _deadline_expired(args) -> bool:
    """True iff a deadline is configured and has been reached or passed."""
    rem = _remaining_budget(args)
    return rem is not None and rem <= 0.0


def _budgeted_timeout(args) -> float | None:
    """Return the per-call timeout to pass into inner router routines.

    When a deadline is configured this is the smaller of the original
    ``--timeout`` (preserving per-stage semantics for the *first* stage)
    and the remaining wall-clock budget (so the *final* stage shortens
    naturally as time runs out).  When no deadline is configured this
    returns ``args.timeout`` unchanged so existing behaviour is preserved
    for users who never passed ``--timeout``.
    """
    timeout = getattr(args, "timeout", None)
    remaining = _remaining_budget(args)
    if remaining is None:
        return timeout
    if timeout is None:
        # ``_wall_clock_deadline`` is derived from ``args.timeout`` so this
        # branch is unreachable in practice; guard against future refactors
        # that decouple the two.
        return remaining
    return min(float(timeout), remaining)


# =============================================================================
# Issue #2823: Per-attempt budget allocation for escalation loops
# =============================================================================
#
# ``_budgeted_timeout`` (above) intentionally preserves "first-stage semantics":
# the *first* escalation attempt is allowed to consume the full ``--timeout``
# value as its per-call budget, and only later attempts get a smaller slice as
# the wall-clock deadline approaches.  This is correct for *single-stage* call
# sites (placement-feedback iterations, the inner route in
# ``route_with_strategy``, etc.), but it is *wrong* for the multi-attempt
# layer-escalation and combined-escalation loops.
#
# In ``--auto-layers`` mode the orchestration tries 2L, 4L, 4L-all-sig, then 6L
# in sequence.  When ``--timeout`` is set tight relative to the natural runtime
# of the first attempt, the greedy allocation gives the entire budget to the 2L
# attempt, leaves nothing for 4L, and never starts 6L.  The escalation strategy
# degenerates to "spend everything on the lowest layer count, give up."
#
# The fix is a *per-attempt* helper that divides the remaining wall-clock
# budget evenly across the remaining attempts, returning the minimum of:
#   1. ``args.timeout``          - never exceed user's original cap
#   2. ``remaining_budget``      - never overrun the total deadline
#   3. ``per_attempt_budget``    - fair slice across remaining attempts
#
# When ``--timeout`` is unset, the helper falls through to ``None`` (legacy
# unbounded behaviour, identical to ``_budgeted_timeout``).  When ``--timeout``
# is set generously (i.e. larger than the natural per-attempt runtime), the
# per-attempt cap is an *upper bound* not a target runtime, so the inner
# router can still finish early on its own.


def _per_attempt_budgeted_timeout(args, attempt_index: int, max_attempts: int) -> float | None:
    """Return the per-call timeout for one attempt of an escalation loop.

    Unlike :func:`_budgeted_timeout` (which lets the first stage consume the
    full ``--timeout`` value), this helper divides the *remaining* wall-clock
    budget evenly across the *remaining* attempts and returns the minimum of
    that fair slice, the original ``--timeout``, and the remaining budget.

    Args:
        args: Parsed CLI namespace; must carry ``timeout`` and (after
            ``_set_wall_clock_deadline``) ``_wall_clock_deadline``.
        attempt_index: 0-based index of the current attempt within the
            escalation loop.  Determines how many attempts are still
            outstanding (``max_attempts - attempt_index``).
        max_attempts: Total number of attempts the escalation loop intends
            to run.  For 1D layer escalation this is ``len(layer_configs)``;
            for the 2D combined escalation this is
            ``len(layer_configs) * len(tiers)``.

    Returns:
        ``None`` when no wall-clock deadline is configured (legacy unbounded
        behaviour, matches :func:`_budgeted_timeout`).  Otherwise a positive
        float bounded by ``args.timeout`` and the remaining wall-clock
        budget, fairly sliced across remaining attempts so later attempts
        also get a real chance to run.

    Notes:
        - When ``max_attempts <= 1`` this collapses to :func:`_budgeted_timeout`
          (no fair-slicing needed for a single attempt).
        - Unused budget from a fast-finishing attempt rolls forward
          automatically: the next call uses the *current* ``remaining_budget``
          divided by the *new* ``remaining_attempts`` count, so an early
          completion on attempt N enlarges the slice available to N+1.
    """
    timeout = getattr(args, "timeout", None)
    remaining = _remaining_budget(args)
    if remaining is None:
        # No deadline configured -> legacy unbounded behaviour.
        return timeout

    # Attempts still outstanding *including* the current one.
    remaining_attempts = max(1, max_attempts - attempt_index)
    per_attempt_slice = remaining / remaining_attempts

    if timeout is None:
        # Defensive: ``_wall_clock_deadline`` is derived from ``args.timeout``,
        # so this branch is unreachable in practice.
        return per_attempt_slice

    return min(float(timeout), remaining, per_attempt_slice)


def _emit_single_pad_net_warning(
    router: "Autorouter",
    single_pad_nets: list[int],
) -> None:
    """Print a top-of-output warning when single-pad signal nets exist.

    These nets are structurally unroutable -- the router silently skips
    them, which makes "13/13 routed, DRC clean" look like a successful
    build even when 4 SWD signals are floating.  Pour nets (POWER /
    GROUND) are silently allowed because a single test point or
    pour-only net is a legitimate design pattern.

    The remaining nets are categorized using the same regex rules used
    by :class:`kicad_tools.validate.rules.SinglePadNetRule` (see issue
    #2613).  Categorized output ensures the warning is not a firehose:

    - **Genuine NCs** (KiCad-emitted ``unconnected-(REF-PIN-PadN)``):
      reported at INFO level, no action required.
    - **Connector NCs** (``Net-(REF-PadN)`` on a J/P prefix, typically
      intentional GPIO no-connects): reported at INFO level.
    - **Real defects** (everything else): reported at WARNING level
      with a pointer to ``kct check --only single_pad_net``.

    See ``kct check --only single_pad_net`` for the full DRC-style
    report (this banner exists to surface the defect early in the
    pipeline; the actionable error lives in the check command).

    Args:
        router: The Autorouter (used for ``router.net_names`` lookup).
        single_pad_nets: Net IDs that have exactly one pad assigned.
    """
    if not single_pad_nets:
        return

    from kicad_tools.cli.progress import flush_print
    from kicad_tools.validate.rules.single_pad_net import (
        _CONNECTOR_NET_PATTERN,
        _KICAD_NC_PATTERN,
    )

    try:
        from kicad_tools.router.net_class import classify_and_apply_rules

        single_pad_name_map = {
            net_num: router.net_names.get(net_num, "") for net_num in single_pad_nets
        }
        # Drop unnamed nets so we don't classify the empty string.
        single_pad_name_map = {num: name for num, name in single_pad_name_map.items() if name}
        pour_rules = classify_and_apply_rules(single_pad_name_map) if single_pad_name_map else {}
        signal_single_pad_names = sorted(
            name
            for num, name in single_pad_name_map.items()
            if not (pour_rules.get(name) and pour_rules[name].is_pour_net)
        )
    except Exception:
        # Conservative: if classification blows up, surface every named
        # single-pad net so we don't hide real defects.
        signal_single_pad_names = sorted(
            router.net_names.get(num, "")
            for num in single_pad_nets
            if router.net_names.get(num, "")
        )

    if not signal_single_pad_names:
        return

    # Categorize each surviving single-pad net using the same regex
    # rules as the validate rule.  Note that this banner cannot validate
    # the footprint-ref-prefix match (it has no Footprint handle), so
    # connector_nc here is detected purely from the net name pattern.
    # A net like ``Net-(J5-1)`` whose lone pad is actually on a
    # non-connector footprint is rare in practice; the DRC-side
    # validate rule does the stricter cross-check.
    genuine_nc: list[str] = []
    connector_nc: list[str] = []
    defects: list[str] = []
    for name in signal_single_pad_names:
        if _KICAD_NC_PATTERN.match(name):
            genuine_nc.append(name)
        elif _CONNECTOR_NET_PATTERN.match(name):
            connector_nc.append(name)
        else:
            defects.append(name)

    flush_print("")
    # Banner header reflects categorized counts so agents see signal
    # vs. noise at a glance.
    parts: list[str] = []
    if defects:
        parts.append(f"{len(defects)} defect(s)")
    if connector_nc:
        parts.append(f"{len(connector_nc)} connector NC")
    if genuine_nc:
        parts.append(f"{len(genuine_nc)} explicit NC")
    summary = ", ".join(parts)
    flush_print(
        f"  WARNING: {len(signal_single_pad_names)} single-pad signal net(s) detected ({summary}):"
    )
    if defects:
        flush_print("    DEFECTS (likely missing footprint or schematic/PCB drift):")
        for name in defects:
            flush_print(f"      - {name}")
    if connector_nc:
        flush_print("    INFO -- connector-pin NCs (typically intentional GPIO no-connects):")
        for name in connector_nc:
            flush_print(f"      - {name}")
    if genuine_nc:
        flush_print("    INFO -- explicit NCs from symbol pin attributes:")
        for name in genuine_nc:
            flush_print(f"      - {name}")
    flush_print(
        "  Run 'kct check --only single_pad_net <pcb>' for details. "
        "Routing will proceed but these nets cannot be connected."
    )


def _strip_route_blocks(pcb_content: str) -> tuple[str, int, int]:
    """Strip top-level ``(segment ...)`` and ``(via ...)`` blocks from PCB content.

    Issue #2976: When ``_stage_input_for_auto_pour`` aliases ``pcb_path`` to
    ``output_path`` (so the user's input file isn't mutated by ``auto_pour``),
    subsequent ``_write_routed_pcb`` calls re-read the **previous write's**
    output rather than the original input.  Each write then *appends* the
    current route s-expression on top of stale segments/vias from the prior
    write, doubling (or worse) the via population in the output file.

    The duplicated vias trigger ``dimension_drill_clearance`` errors --
    -0.300mm clearance == two coincident drills on the same net -- because
    they are literally the same physical via emitted twice with different
    UUIDs.

    The fix is to remove any top-level routed-element blocks before
    inserting fresh ones: the in-memory router state is the source of
    truth, and the new ``route_sexp`` already contains every segment and
    via that should appear in the output.  Footprints, pads, zones, and
    other PCB structure are preserved because we only strip blocks whose
    first token is ``segment`` or ``via``.

    Args:
        pcb_content: Original PCB file content.

    Returns:
        Tuple of ``(stripped_content, segments_removed, vias_removed)``.
        Counts are returned so callers can log a diagnostic when a stale
        write is observed.
    """
    # Walk the content counting depth, identifying top-level forms whose
    # first token is "segment" or "via" and excising them.  Footprints
    # contain their own (pad ...) blocks, never (segment ...) or top-level
    # (via ...), so stripping at depth=1 is safe.
    out: list[str] = []
    i = 0
    n = len(pcb_content)
    depth = 0
    in_string = False
    prev_char = ""
    segments_removed = 0
    vias_removed = 0
    while i < n:
        ch = pcb_content[i]
        if ch == '"' and prev_char != "\\":
            in_string = not in_string
            out.append(ch)
            prev_char = ch
            i += 1
            continue
        if not in_string and ch == "(":
            # Peek at the token following the paren.
            j = i + 1
            while j < n and pcb_content[j].isspace():
                j += 1
            token_start = j
            while (
                j < n
                and not pcb_content[j].isspace()
                and pcb_content[j] != "("
                and pcb_content[j] != ")"
            ):
                j += 1
            token = pcb_content[token_start:j]
            if depth == 1 and token in ("segment", "via"):
                # Skip the entire form: find matching ")".
                form_depth = 1
                k = j
                form_in_string = False
                form_prev = ""
                while k < n and form_depth > 0:
                    c = pcb_content[k]
                    if c == '"' and form_prev != "\\":
                        form_in_string = not form_in_string
                    elif not form_in_string:
                        if c == "(":
                            form_depth += 1
                        elif c == ")":
                            form_depth -= 1
                    form_prev = c
                    k += 1
                if token == "segment":
                    segments_removed += 1
                else:
                    vias_removed += 1
                # Skip whitespace that was preceding this form (trailing
                # newline/tab from the previous emission) to keep the
                # resulting file tidy.
                while out and out[-1] in (" ", "\t"):
                    out.pop()
                # Also drop a single trailing newline so we collapse the
                # blank line the form previously occupied.
                if out and out[-1] == "\n":
                    out.pop()
                i = k
                prev_char = ")"
                continue
            depth += 1
        elif not in_string and ch == ")":
            depth -= 1
        out.append(ch)
        prev_char = ch
        i += 1
    return "".join(out), segments_removed, vias_removed


def _insert_sexp_before_closing(pcb_content: str, sexp_fragments: str) -> str:
    """Insert S-expression fragments before the final closing parenthesis of a PCB file.

    This correctly removes only the last closing parenthesis from the PCB content
    and re-adds it after the inserted fragments. Unlike ``rstrip(")")``, which
    strips ALL trailing ``)``, this function preserves the S-expression structure.

    Issue #2976: Before inserting fresh route s-expressions, any pre-existing
    ``(segment ...)`` and ``(via ...)`` blocks at the top level are stripped.
    This prevents accumulation when ``pcb_path == output_path`` (which happens
    after ``_stage_input_for_auto_pour``): each ``_write_routed_pcb`` call
    used to *append* the current route state on top of the previous write,
    producing duplicate same-net vias that the DRC flagged with negative
    drill-edge clearance.

    Args:
        pcb_content: Original PCB file content.
        sexp_fragments: S-expression string(s) to insert (segments, vias, zones).

    Returns:
        Modified PCB content with fragments inserted before the final ``)``.
    """
    pcb_content, _stripped_segs, _stripped_vias = _strip_route_blocks(pcb_content)
    content = pcb_content.rstrip()
    if content.endswith(")"):
        content = content[:-1].rstrip()

    result = content + "\n\n"
    result += f"  {sexp_fragments}\n"
    result += ")\n"
    return result


def _validate_sexp_parentheses(content: str) -> bool:
    """Validate that S-expression parentheses are balanced.

    Scans the content respecting quoted strings (parentheses inside quotes
    are not counted). Returns True if parentheses are balanced.

    Args:
        content: S-expression content to validate.

    Returns:
        True if parentheses are balanced, False otherwise.
    """
    depth = 0
    in_string = False
    prev_char = ""
    for char in content:
        if char == '"' and prev_char != "\\":
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return False
        prev_char = char
    return depth == 0


def _write_routed_pcb(
    pcb_path: Path,
    output_path: Path,
    route_sexp: str,
    *,
    layer_count: int = 2,
    is_checkpoint: bool = False,
) -> Path:
    """Atomically write a routed PCB file.

    Consolidates the read_text -> update_pcb_layer_stackup ->
    _insert_sexp_before_closing -> _validate_sexp_parentheses -> write
    sequence that was previously duplicated at four save sites in this
    module.

    Uses an atomic write via ``<output>.tmp`` + ``os.fsync`` +
    ``os.replace(tmp, output)`` so a SIGKILL/OOM between bytes-on-the-wire
    and final flush cannot leave a torn (truncated) PCB file at the user's
    output path.  Closes Issue #2808's torn-file hazard.

    Honors ``output_path`` exactly -- callers must NOT pre-suffix the path
    with ``_4layer`` or similar; the layer count is recorded inside the
    PCB content via :func:`update_pcb_layer_stackup`, not in the filename
    (closes Issue #2809).

    Args:
        pcb_path: Path to the source (unrouted) PCB file.  Read fresh on
            every call so checkpoint writes pick up upstream edits to the
            input file (rare, but supported).
        output_path: Final destination path.  Written atomically via a
            sibling ``.tmp`` file.
        route_sexp: S-expression fragment(s) to insert before the closing
            ``)``.  Can be empty -- in that case the original PCB is
            written back unchanged (useful for checkpoints that fire
            before any net has routed).
        layer_count: Target copper layer count for the layer stackup
            update.  ``2`` is a no-op.  Defaults to ``2``.
        is_checkpoint: When True, skip the layer-stackup mutation.
            Checkpoints serialize the in-progress best snapshot; they
            should match whatever stackup is currently in use and not
            attempt to escalate.  Defaults to False.

    Returns:
        The ``output_path`` that was written to (returned unchanged so
        callers can use the helper inline, e.g.
        ``written = _write_routed_pcb(...)``).

    Raises:
        ValueError: If the generated PCB has unbalanced S-expression
            parentheses (indicates a bug in route generation -- caller
            is responsible for surfacing this).
    """
    original_content = pcb_path.read_text()

    # Update layer stackup for terminal writes when we escalated above 2L.
    # Checkpoints skip this -- they reflect mid-route state and should not
    # rewrite the stackup at every flush.
    if not is_checkpoint and layer_count > 2:
        original_content = update_pcb_layer_stackup(original_content, layer_count)

    if route_sexp:
        output_content = _insert_sexp_before_closing(original_content, route_sexp)
    else:
        output_content = original_content

    if not _validate_sexp_parentheses(output_content):
        logger.error("Generated PCB file has unbalanced parentheses")
        raise ValueError(
            "Generated PCB file has invalid S-expression syntax "
            "(unbalanced parentheses). This is a bug in kicad-tools. "
            "Please report it."
        )

    # Atomic write: tmp file -> fsync -> rename.  Sibling-in-same-dir
    # ensures os.replace is a same-filesystem rename (atomic on POSIX).
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(output_content)
    # fsync the file so a crash between write and rename does not leave
    # a partial-content tmp file masquerading as the routed PCB.
    with open(tmp_path, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp_path, output_path)

    return output_path


def _connectivity_snapshot(router: "Autorouter"):
    """Capture per-net connectivity + deep-copied routes (issue #2596).

    Thin wrapper around
    :func:`kicad_tools.router.connectivity_invariant.snapshot_connectivity`
    so each pipeline call site has a single-line invocation.

    Returns:
        :class:`ConnectivitySnapshot` to pass to
        :func:`_enforce_connectivity_invariant_or_exit` after the phase
        runs.
    """
    from kicad_tools.router.connectivity_invariant import snapshot_connectivity

    return snapshot_connectivity(router)


def _enforce_connectivity_invariant_or_exit(
    router: "Autorouter",
    snapshot,
    *,
    phase: str,
    args: argparse.Namespace,
    quiet: bool = False,
) -> None:
    """Enforce the post-phase connectivity invariant (issue #2596).

    Reverts regressed nets in default mode; in ``--strict`` mode this
    function calls :func:`sys.exit` with code 6 (the same code used by
    the post-save ``verify_output_connectivity`` block) so the
    behaviour is identical regardless of which guard catches the
    regression first.

    Args:
        router: The :class:`Autorouter` whose routes were just mutated.
        snapshot: Snapshot returned by :func:`_connectivity_snapshot`.
        phase: Short label identifying the phase (``"optimize"`` or
            ``"nudge"``).
        args: Parsed CLI arguments (used for ``--strict`` and
            ``--verbose``).
        quiet: Suppress all stdout output.
    """
    from kicad_tools.router.connectivity_invariant import (
        ConnectivityRegressionError,
        enforce_connectivity_invariant,
    )

    strict = bool(getattr(args, "strict", False))
    verbose = bool(getattr(args, "verbose", False))
    try:
        enforce_connectivity_invariant(
            router,
            snapshot,
            phase=phase,
            strict=strict,
            verbose=verbose,
            quiet=quiet,
        )
    except ConnectivityRegressionError:
        # Strict mode: regression detected.  Mirror the exit-code-6
        # contract documented near the bottom of main() for the post-save
        # output connectivity verification.
        sys.exit(6)


def _make_checkpoint_callback(
    pcb_path: Path,
    output_path: Path,
    interval: float,
    quiet: bool = False,
):
    """Build a checkpoint callback for ``route_all_negotiated`` (Issue #2808).

    Returns a callable matching ``Callable[[list[Route], IterationMetrics], None]``
    that atomically flushes the best-so-far snapshot to ``output_path`` no
    more often than every ``interval`` seconds.

    Returns ``None`` when ``interval <= 0`` so the caller can pass the
    return value directly to ``route_all_negotiated(checkpoint_callback=...)``
    without conditional plumbing -- the router already treats ``None`` as
    "no checkpointing".

    The callback is gated by ``time.monotonic()`` (immune to wall-clock
    skew during long routing runs).  The first improvement event always
    fires immediately (no warm-up delay) so the first checkpoint lands
    at iteration 0/1 rather than waiting one full ``interval``.

    Args:
        pcb_path: Source PCB path (passed through to ``_write_routed_pcb``).
        output_path: User's ``--output`` destination.  Honored exactly --
            no ``_4layer`` suffix appending (closes #2809).
        interval: Minimum seconds between checkpoint writes.  ``0`` (or
            negative) disables checkpointing entirely.
        quiet: Suppress the "checkpoint: wrote ..." log line.

    Returns:
        Callback or ``None``.
    """
    if interval <= 0:
        return None

    # Mutable container so the closure can write back the timestamp.
    # ``[None]`` sentinel means "no checkpoint written yet"; the first
    # improvement event triggers an immediate write.
    last_time: list[float | None] = [None]

    def _checkpoint(best_routes, best_metrics) -> None:
        from kicad_tools.cli.progress import flush_print

        now = time.monotonic()
        if last_time[0] is not None and (now - last_time[0]) < interval:
            return

        # Materialize sexp from the snapshot (NOT self.routes).  Each
        # Route has its own to_sexp() so we can serialize directly without
        # touching the router's live state.
        route_sexp = "\n\t".join(r.to_sexp() for r in best_routes)
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            is_checkpoint=True,
        )

        last_time[0] = now
        if not quiet:
            flush_print(
                f"  checkpoint: wrote best-so-far "
                f"(iter={best_metrics.iteration}, "
                f"routed={best_metrics.routed_count}, "
                f"overflow={best_metrics.overflow}) to {output_path}"
            )

    return _checkpoint


def _finalize_routes(
    router: "Autorouter",
    multi_pad_net_ids: set[int],
    nets_to_route: int,
    quiet: bool = False,
) -> tuple[str, dict, dict]:
    """Run cleanup, compute statistics, and generate S-expressions.

    This is the single canonical sequence that must be followed whenever
    route output is produced.  The ordering is:

    1. ``cleanup_artifacts()`` -- mutates ``router.routes`` in place,
       removing net-0 orphans and out-of-bounds segments while preserving
       connectivity.
    2. ``to_sexp(skip_cleanup=True)`` -- serialize the (now clean) routes.
    3. ``get_statistics()`` -- compute metrics from the cleaned routes so
       they match what was written to disk.

    All four output paths in route_cmd.py (main CLI, layer escalation,
    rule relaxation, multi-strategy matrix) must use this helper to
    prevent the stats-before-cleanup bug from recurring.

    Args:
        router: The Autorouter instance with completed routes.
        multi_pad_net_ids: Set of net IDs with >= 2 pads (for accurate
            nets_routed counting per Issue #1643).
        nets_to_route: Total number of nets targeted for routing.
        quiet: Suppress console output.

    Returns:
        Tuple of (route_sexp, stats, cleanup_stats) where:
        - route_sexp: S-expression string for the cleaned routes.
        - stats: Post-cleanup statistics dict from ``get_statistics()``.
        - cleanup_stats: Dict returned by ``cleanup_artifacts()`` with
          keys like ``net0_routes_removed``, ``oob_segments_removed``,
          ``segments_restored``, etc.
    """
    from kicad_tools.cli.progress import flush_print

    # Step 1: Run connectivity-aware cleanup before computing statistics
    # so that metrics reflect the segments actually written to the output
    # file.  The cleanup is safe (it restores segments whose removal would
    # fragment a net).  See io.py for the canonical ordering.
    pre_cleanup_segments = sum(len(r.segments) for r in router.routes)
    pre_cleanup_vias = sum(len(r.vias) for r in router.routes)
    cleanup_stats = router.cleanup_artifacts()
    post_cleanup_segments = sum(len(r.segments) for r in router.routes)
    post_cleanup_vias = sum(len(r.vias) for r in router.routes)

    segments_removed = pre_cleanup_segments - post_cleanup_segments
    vias_removed = pre_cleanup_vias - post_cleanup_vias

    if not quiet and (segments_removed > 0 or vias_removed > 0):
        flush_print("\n--- Cleanup ---")
        flush_print(
            f"  Segments: {pre_cleanup_segments} -> {post_cleanup_segments} "
            f"({segments_removed} removed)"
        )
        if vias_removed > 0:
            flush_print(
                f"  Vias:     {pre_cleanup_vias} -> {post_cleanup_vias} ({vias_removed} removed)"
            )
        if cleanup_stats.get("segments_restored", 0) > 0:
            flush_print(
                f"  Restored: {cleanup_stats['segments_restored']} segments, "
                f"{cleanup_stats.get('vias_restored', 0)} vias (connectivity preservation)"
            )

    # Step 2: Generate S-expressions from the cleaned routes
    route_sexp = router.to_sexp(skip_cleanup=True)

    # Step 3: Compute statistics from the cleaned routes
    stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)

    if not quiet:
        flush_print("\n--- Results ---")
        flush_print(f"  Routes created:  {stats['routes']}")
        flush_print(f"  Segments:        {stats['segments']}")
        flush_print(f"  Vias:            {stats['vias']}")
        flush_print(f"  Total length:    {stats['total_length_mm']:.2f}mm")
        flush_print(f"  Nets routed:     {stats['nets_routed']}/{nets_to_route}")

    return route_sexp, stats, cleanup_stats


# Global state for Ctrl+C handling
_interrupt_state = {
    "interrupted": False,
    "router": None,
    "output_path": None,
    "pcb_path": None,
    "quiet": False,
    "best_completed_attempt": False,
}


def _handle_interrupt(signum, frame):
    """Handle Ctrl+C by setting the interrupted flag and saving partial results."""
    _interrupt_state["interrupted"] = True
    if not _interrupt_state["quiet"]:
        print("\n\n⚠ Interrupt received! Saving partial results...")
    # Save partial results immediately
    saved = _save_partial_results()
    # Exit with code 5 to indicate SIGINT interruption with saved partial results.
    # This is distinct from code 2 (partial routing below threshold) so scripts can
    # distinguish user-interrupted from router-decided-partial.
    sys.exit(5 if saved else 130)  # 130 = 128 + SIGINT (2)


def _save_partial_results() -> bool:
    """Save partial routing results if interrupted.

    Returns:
        True if partial results were saved, False otherwise.
    """
    router = _interrupt_state["router"]
    output_path = _interrupt_state["output_path"]
    pcb_path = _interrupt_state["pcb_path"]
    quiet = _interrupt_state["quiet"]

    if router is None or output_path is None or pcb_path is None:
        return False

    if not router.routes:
        if not quiet:
            print("  No routes to save.")
        return False

    try:
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Get partial route S-expressions
        route_sexp = router.to_sexp()

        if route_sexp:
            # When the interrupt state holds a best *completed* attempt from
            # adaptive-rules routing, write to the main output path (not
            # _partial) because the result is a full routing pass.
            if _interrupt_state.get("best_completed_attempt"):
                save_path = output_path
            else:
                save_path = output_path.with_stem(output_path.stem + "_partial")

            # Insert routes before final closing parenthesis
            output_content = _insert_sexp_before_closing(original_content, route_sexp)

            save_path.write_text(output_content)

            if not quiet:
                stats = router.get_statistics()
                print(f"\n  Partial results saved to: {save_path}")
                print(f"    Nets routed: {stats['nets_routed']}")
                print(f"    Segments: {stats['segments']}")
                print(f"    Vias: {stats['vias']}")
            return True
    except Exception as e:
        if not quiet:
            print(f"  Error saving partial results: {e}")

    return False


def _export_failed_nets(
    router: "Autorouter",
    net_map: dict[str, int],
    export_path: str,
    quiet: bool = False,
    nets_to_route_ids: set[int] | None = None,
) -> bool:
    """Export the list of failed (unrouted) net names to a file.

    Writes one net name per line to the specified path.

    Args:
        router: The Autorouter instance with completed routing.
        net_map: Mapping of net names to net IDs.
        export_path: File path to write the failed net names.
        quiet: If True, suppress output messages.
        nets_to_route_ids: Optional set of net IDs targeted for routing
            (multi-pad signal nets).  When provided, only nets in this set
            are considered candidates so single-pad and power nets are
            excluded from the export.

    Returns:
        True if the file was written successfully, False otherwise.
    """
    reverse_net = {v: k for k, v in net_map.items() if v > 0}
    routed_net_ids = {route.net for route in router.routes}
    if nets_to_route_ids is not None:
        unrouted_ids = nets_to_route_ids - routed_net_ids
    else:
        all_net_ids = {v for k, v in net_map.items() if v > 0}
        unrouted_ids = all_net_ids - routed_net_ids

    if not unrouted_ids:
        if not quiet:
            print("  No failed nets to export.")
        return False

    try:
        failed_names = sorted(reverse_net.get(nid, f"Net_{nid}") for nid in unrouted_ids)
        export_file = Path(export_path)
        export_file.write_text("\n".join(failed_names) + "\n")
        if not quiet:
            print(f"  Failed nets exported to: {export_file} ({len(failed_names)} nets)")
        return True
    except Exception as e:
        if not quiet:
            print(f"  Error exporting failed nets: {e}")
        return False


def show_preview(
    router,
    net_map: dict[str, int],
    nets_to_route: int,
    quiet: bool = False,
    nets_to_route_ids: set[int] | None = None,
) -> str:
    """Display routing preview with per-net breakdown.

    Args:
        router: The Autorouter instance with completed routes
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets expected to be routed
        quiet: If True, skip interactive prompt and return 'n'
        nets_to_route_ids: Optional set of net IDs targeted for routing.
            When provided, ``nets_routed`` only counts nets in this set.

    Returns:
        User response: 'y' (apply), 'n' (reject), or 'e' (edit - future)
    """
    # Build reverse mapping: net_id -> net_name
    reverse_net = {v: k for k, v in net_map.items()}

    # Collect per-net statistics
    net_stats: dict[int, dict] = {}
    for route in router.routes:
        net_id = route.net
        if net_id not in net_stats:
            net_stats[net_id] = {
                "net_name": route.net_name or reverse_net.get(net_id, f"Net {net_id}"),
                "segments": 0,
                "vias": 0,
                "length": 0.0,
                "layers": set(),
            }
        stats = net_stats[net_id]
        stats["segments"] += len(route.segments)
        stats["vias"] += len(route.vias)
        for seg in route.segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            stats["length"] += math.sqrt(dx * dx + dy * dy)
            stats["layers"].add(seg.layer.kicad_name)

    # Identify unrouted nets — filter to target population so the
    # "No path found" list only shows actual routing candidates,
    # not skipped power nets or single-pad nets (Issue #1833).
    routed_net_ids = set(net_stats.keys())
    if nets_to_route_ids is not None:
        unrouted_ids = nets_to_route_ids - routed_net_ids
    else:
        all_net_ids = {v for k, v in net_map.items() if v > 0}
        unrouted_ids = all_net_ids - routed_net_ids

    # Print header
    print("\n" + "=" * 60)
    print("ROUTING PREVIEW")
    print("=" * 60)

    # Print per-net breakdown
    for net_id in sorted(net_stats.keys()):
        stats = net_stats[net_id]
        net_name = stats["net_name"]
        layers = " -> ".join(sorted(stats["layers"]))
        via_info = f", {stats['vias']} via(s)" if stats["vias"] > 0 else ""

        print(f"\nNet: {net_name}")
        print(f"  Layers:   {layers}")
        print(f"  Length:   {stats['length']:.2f}mm")
        print(f"  Segments: {stats['segments']}{via_info}")
        print("  Status:   \u2713 Routed")

    # Show unrouted nets
    if unrouted_ids:
        print("\n" + "-" * 40)
        for net_id in sorted(unrouted_ids):
            net_name = reverse_net.get(net_id, f"Net {net_id}")
            if net_name:  # Skip empty net names
                print(f"\nNet: {net_name}")
                print("  Status:   \u2717 No path found")

    # Summary statistics — filter to target population (Issue #1643)
    overall_stats = router.get_statistics(nets_to_route_ids=nets_to_route_ids)
    nets_routed = overall_stats["nets_routed"]
    success_rate = (nets_routed / nets_to_route * 100) if nets_to_route > 0 else 0

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Nets routed:  {nets_routed}/{nets_to_route} ({success_rate:.0f}%)")
    print(f"  Total length: {overall_stats['total_length_mm']:.2f}mm")
    print(f"  Total vias:   {overall_stats['vias']}")
    print(f"  Segments:     {overall_stats['segments']}")

    # Layer usage summary
    all_layers: dict[str, int] = {}
    for route in router.routes:
        for seg in route.segments:
            layer_name = seg.layer.kicad_name
            all_layers[layer_name] = all_layers.get(layer_name, 0) + 1

    if all_layers:
        print("\n  Layer usage:")
        for layer_name, count in sorted(all_layers.items()):
            print(f"    {layer_name}: {count} segments")

    print("=" * 60)

    # Interactive prompt (unless quiet mode)
    if quiet:
        return "n"

    print("\nApply routes? [y/N/e(dit)]:", end=" ")
    try:
        response = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return "n"

    if response in ("y", "yes"):
        return "y"
    elif response in ("e", "edit"):
        print("  (Edit mode not yet implemented - treating as reject)")
        return "n"
    else:
        return "n"


def run_post_route_drc(
    output_path: Path,
    manufacturer: str,
    layers: int,
    quiet: bool = False,
    net_class_map: dict | None = None,
) -> tuple[int, int]:
    """Run DRC validation on the routed PCB.

    Args:
        output_path: Path to the routed PCB file
        manufacturer: Manufacturer profile for DRC rules (e.g., "jlcpcb")
        layers: Number of PCB layers
        quiet: If True, suppress output
        net_class_map: Optional ``{net_name: NetClassRouting}`` map from
            the autorouter.  When provided, the differential-pair
            routing-continuity rule (Phase 2.5b / Issue #2652) re-derives
            its engagement state from this map + the routed PCB and
            fires per Epic #2556 Phase 2G.  Without it, that rule is a
            no-op (graceful degradation).

    Returns:
        Tuple of (error_count, warning_count)
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    try:
        # Load the routed PCB
        pcb = PCB.load(str(output_path))

        # Run DRC
        checker = DRCChecker(
            pcb,
            manufacturer=manufacturer,
            layers=layers,
            net_class_map=net_class_map,
        )
        results = checker.check_all()

        error_count = results.error_count
        warning_count = results.warning_count

        if not quiet:
            print("\n--- DRC Validation ---")
            if error_count == 0 and warning_count == 0:
                print(f"  DRC PASSED ({manufacturer} profile, {layers} layers)")
            else:
                if error_count > 0:
                    print(f"  Errors:   {error_count}")
                if warning_count > 0:
                    print(f"  Warnings: {warning_count}")

                # Show first few violations
                shown = 0
                for v in results.errors[:5]:
                    location = (
                        f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                    )
                    print(f"    - {v.rule_id}: {v.message}{location}")
                    shown += 1
                if error_count > 5:
                    print(f"    ... and {error_count - 5} more errors")

                if warning_count > 0 and shown < 5:
                    for v in results.warnings[: 5 - shown]:
                        location = (
                            f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                        )
                        print(f"    - {v.rule_id}: {v.message}{location}")
                    if warning_count > (5 - shown):
                        print(f"    ... and {warning_count - (5 - shown)} more warnings")

                print(f"\n  Run 'kct check {output_path} --mfr {manufacturer}' for full details")
                if error_count > 0:
                    print(f"  Run 'kct fix-drc {output_path}' to auto-repair clearance violations")

        return error_count, warning_count

    except Exception as e:
        if not quiet:
            print("\n--- DRC Validation ---")
            print(f"  Warning: DRC check failed: {e}")
        return -1, -1  # Indicate failure to run DRC


def _run_auto_fix(
    output_path: Path,
    max_passes: int = 1,
    quiet: bool = False,
    args=None,
) -> int:
    """Run fix-drc on the routed PCB to auto-repair DRC violations.

    Args:
        output_path: Path to the routed PCB file to repair.
        max_passes: Number of iterative repair passes.
        quiet: If True, suppress output.
        args: Parsed ``route`` CLI args.  When provided, the function
            honors ``args._wall_clock_deadline`` (issue #2802) and skips
            the auto-fix invocation entirely if the total budget has been
            consumed.  Optional for backward compatibility with callers
            that do not have the args namespace handy.

    Returns:
        Exit code from fix_drc_cmd.main() (0 = all violations fixed).
        Returns a non-zero "skipped" code (1) when the wall-clock
        deadline has already expired.
    """
    from kicad_tools.cli.fix_drc_cmd import main as fix_drc_main

    # Issue #2802: skip auto-fix when the total wall-clock budget has
    # already been exhausted by upstream routing stages.  ``fix-drc``
    # itself has no ``--timeout`` flag and runs unbounded per pass, so
    # without this guard a single auto-fix invocation can easily double
    # the user's configured ``--timeout``.
    if args is not None and _deadline_expired(args):
        if not quiet:
            print("\n--- Auto-Fix DRC Violations ---")
            print("  Skipping: total wall-clock deadline reached (--timeout, issue #2802)")
        return 1

    if not quiet:
        print("\n--- Auto-Fix DRC Violations ---")

    fix_argv = [
        str(output_path),
        "--max-passes",
        str(max_passes),
        "--max-displacement",
        "2.0",
        "--local-reroute",
    ]
    if quiet:
        fix_argv.append("--quiet")

    result = fix_drc_main(fix_argv)

    if not quiet:
        # Exit-code contract from fix_drc_cmd.main (see fix_drc_cmd.py:375-390):
        #   0 = all targeted violations repaired
        #   1 = no violations found or no progress made
        #   2 = partial repair (some repairable violations remain)
        #   3 = connectivity rollback (issue #2839)
        #
        # Each code gets a distinct message so the user can distinguish
        # "rollback fired and silently zeroed out the work" (3) from
        # "fix-drc tried and partially succeeded" (2) and from "nothing
        # could be done at all" (1).
        if result == 0:
            print("  Auto-fix: all targeted violations repaired!")
        elif result == 3:
            print(
                "  Auto-fix: rolled back due to connectivity regression "
                "(nudges would have broken at least one net)."
            )
            print("  Run 'kct fix-drc <pcb> --no-connectivity-check' to apply the nudges anyway")
            print("  (only safe when partial-completion regressions are acceptable).")
        elif result == 2:
            print("  Auto-fix: partial repair; some violations remain.")
        elif result == 1:
            print("  Auto-fix: no progress made (manual repair may be needed).")
        else:
            print("  Auto-fix: some violations remain (manual repair may be needed)")

    return result


def _should_auto_fix(args) -> bool:
    """Determine whether auto-fix should run based on CLI flags.

    Auto-fix runs when --auto-fix is set (which is also implied by
    --auto-fix-passes), but is suppressed by --dry-run and --skip-drc.
    """
    auto_fix = getattr(args, "auto_fix", False)
    dry_run = getattr(args, "dry_run", False)
    skip_drc = getattr(args, "skip_drc", False)

    if not auto_fix:
        return False
    if dry_run or skip_drc:
        return False
    return True


# =============================================================================
# Issue #2595: Placement-routing feedback helpers
# =============================================================================


def _parse_ref_list(value: str | None) -> set[str]:
    """Parse a comma-separated component-ref list into a set."""
    if not value:
        return set()
    return {ref.strip() for ref in value.split(",") if ref.strip()}


def _auto_detect_anchored_refs(pcb) -> set[str]:
    """Auto-detect components that should be anchored during placement feedback.

    A footprint is anchored when *any* of the following hold:

    1. Its reference starts with ``J`` or ``P`` (connectors and headers
       are mechanically constrained -- the human chose their position
       on the board edge for cabling and they cannot move freely).
    2. Its KiCad ``locked`` attribute is set to True (the human pinned
       it explicitly in the layout editor).

    Args:
        pcb: A loaded ``PCB`` object whose ``footprints`` are iterable.

    Returns:
        Set of component references to anchor.
    """
    anchored: set[str] = set()
    for fp in getattr(pcb, "footprints", []) or []:
        ref = getattr(fp, "reference", "") or ""
        if not ref:
            continue
        # Connectors (J*) and headers/test-points (P*) are mechanically
        # constrained.  We deliberately do NOT auto-anchor U* or any
        # other prefix -- ICs and passives are the things we WANT the
        # feedback loop to be able to nudge.
        if ref[0].upper() in ("J", "P"):
            anchored.add(ref)
            continue
        if getattr(fp, "locked", False):
            anchored.add(ref)
    return anchored


def _resolve_placement_feedback_anchors(pcb, args) -> set[str]:
    """Compute the final set of anchored refs for the feedback loop.

    Combines auto-detected anchors (connectors, locked footprints) with
    the user's ``--placement-feedback-anchor`` overrides, then removes
    any refs the user explicitly opted out via
    ``--placement-feedback-no-anchor``.

    Args:
        pcb: Loaded PCB object.
        args: Parsed CLI args.

    Returns:
        Set of refs to anchor.
    """
    anchors = _auto_detect_anchored_refs(pcb)
    anchors |= _parse_ref_list(getattr(args, "placement_feedback_anchor", None))
    anchors -= _parse_ref_list(getattr(args, "placement_feedback_no_anchor", None))
    return anchors


def _placement_diff_path(args, pcb_path: Path) -> Path:
    """Resolve the path of the ``<output>_placement_diff.json`` artifact."""
    if getattr(args, "output", None):
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")
    return output_path.with_suffix("").with_name(output_path.stem + "_placement_diff.json")


def _run_placement_feedback(
    router,
    pcb_path: Path,
    args,
    quiet: bool,
) -> list[dict] | None:
    """Drive the placement-routing feedback loop for ``kct route``.

    Loads the PCB so the loop can mutate footprint positions, computes
    the anchor set, invokes ``router.route_with_placement_feedback``,
    persists ``<output>_placement_diff.json`` beside the routed PCB,
    and prints a human-readable summary of what moved.

    Args:
        router: The ``Autorouter`` whose state will be reset by the loop.
        pcb_path: Path to the (already-loaded) input PCB so we can
            create an in-memory ``PCB`` for placement mutation.
        args: Parsed CLI args (must include ``placement_feedback_*``).
        quiet: When True, suppress all stdout output.

    Returns:
        The placement diff as a list of dicts (suitable for JSON
        serialization), or None when the loop did not run / produced
        no diff.
    """
    import json

    from kicad_tools.schema.pcb import PCB

    # Issue #2802: bail out early when the total wall-clock budget has
    # already been consumed by upstream stages.  Each PF iteration kicks
    # off a fresh negotiated re-route, so skipping the loop entirely
    # preserves the remaining time for downstream steps (optimize, DRC,
    # auto-fix) instead of burning it on routing we'll never get to
    # finish.
    if _deadline_expired(args):
        if not quiet:
            print("\n--- Placement-Routing Feedback ---")
            print("  Skipping: total wall-clock deadline reached (--timeout, issue #2802)")
        return None

    if not quiet:
        print("\n--- Placement-Routing Feedback ---")
        failed = router.get_failed_nets()
        print(f"  Initial pass left {len(failed)} unrouted net(s); attempting feedback")

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as exc:
        if not quiet:
            print(f"  Warning: could not load PCB for feedback ({exc}); skipping")
        return None

    anchored = _resolve_placement_feedback_anchors(pcb, args)
    if not quiet and anchored:
        print(f"  Anchored refs ({len(anchored)}): {', '.join(sorted(anchored))}")

    budget = int(getattr(args, "placement_feedback_budget", 3) or 3)
    max_movement = float(getattr(args, "placement_feedback_max_movement", 5.0) or 5.0)
    use_negotiated = getattr(args, "strategy", "negotiated") == "negotiated"

    # Issue #2802: forward the remaining wall-clock budget as the per-call
    # ``timeout`` so each PF iteration's inner re-route gets a smaller
    # slice of the deadline as time runs out, rather than always asking
    # for a fresh ``args.timeout`` slot.  Falls back to ``args.timeout``
    # when no deadline is configured.
    timeout = _budgeted_timeout(args)
    per_net_timeout = getattr(args, "per_net_timeout", None)

    # Issue #2606: stagnation + outer-timeout guards.  Defaults match
    # the parser: patience=3, outer_timeout=None.
    stagnation_patience = int(getattr(args, "placement_feedback_stagnation_patience", 3) or 0)
    outer_timeout_raw = getattr(args, "placement_feedback_outer_timeout", None)
    outer_timeout = float(outer_timeout_raw) if outer_timeout_raw is not None else None

    # Issue #2802: cap the PF outer-timeout at the remaining total budget
    # so the feedback loop terminates when the deadline fires even if the
    # user did not pass ``--placement-feedback-outer-timeout``.  When both
    # are set, we take the smaller (most restrictive) value.
    _remaining = _remaining_budget(args)
    if _remaining is not None:
        outer_timeout = _remaining if outer_timeout is None else min(outer_timeout, _remaining)

    try:
        result = router.route_with_placement_feedback(
            pcb=pcb,
            max_adjustments=budget,
            use_negotiated=use_negotiated,
            verbose=not quiet,
            fixed_refs=anchored,
            max_movement=max_movement,
            timeout=timeout,
            per_net_timeout=per_net_timeout,
            stagnation_patience=stagnation_patience,
            outer_timeout=outer_timeout,
        )
    except Exception as exc:
        if not quiet:
            print(f"  Warning: placement feedback failed ({exc}); keeping initial routes")
        return None

    if not quiet:
        print(f"  Feedback iterations: {result.iterations}")
        # Issue #2606: surface the exit_reason so CI / humans can
        # distinguish "we made it" (pf_converged) from "we plateaued"
        # (pf_stagnated) or "we hit the wall clock" (pf_timeout).
        print(f"  Exit reason:        {result.exit_reason}")
        print(f"  Components moved:   {result.total_components_moved}")
        print(f"  Final failed nets:  {len(result.failed_nets)}")

    diff_data = [entry.to_dict() for entry in result.placement_diff]

    # Persist the diff JSON beside the routed PCB.  We write it even
    # when the diff is empty so consumers (CI, humans) can rely on the
    # file's presence to detect that the feedback loop was invoked.
    diff_path = _placement_diff_path(args, pcb_path)
    try:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(json.dumps(diff_data, indent=2) + "\n")
        if not quiet:
            print(f"  Placement diff saved to: {diff_path}")
    except OSError as exc:
        if not quiet:
            print(f"  Warning: could not write placement diff ({exc})")

    return diff_data


def _fill_zones_after_route(output_path: Path, quiet: bool = False) -> None:
    """Fill copper-pour zones after routing completes.

    Routing produces traces; copper pour zones must be filled *after* the
    trace geometry exists so the fill polygons respect trace clearances.
    Without this step, exported Gerbers contain ``(zone ...)`` definitions
    but zero ``G36...G37`` polygon-fill regions, so manufactured boards have
    no plane copper -- the bug fixed by issue #2516.

    This function mirrors :func:`pipeline_cmd._run_step_zones`:

    - Skips silently when ``kicad-cli`` is not installed (no-op with warning).
    - Delegates to :func:`runner.run_fill_zones` so the existing
      net-corruption snapshot/restore guard (``_snapshot_net_declarations`` /
      ``_restore_net_declarations``) runs.
    - Validates net format afterwards via :func:`runner.validate_net_format`
      and logs a warning when corruption is detected.

    The function is idempotent: filling a PCB whose zones are already filled
    simply rewrites the same fill polygons.  When the input PCB has no
    ``(zone ...)`` blocks at all, kicad-cli has nothing to fill and this is
    effectively a no-op.

    Args:
        output_path: Path to the routed PCB file (modified in place).
        quiet: Suppress informational output.
    """
    from kicad_tools.cli.runner import (
        find_kicad_cli,
        run_fill_zones,
        validate_net_format,
    )

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        if not quiet:
            print("  Zone fill: skipped (kicad-cli not installed)")
        return

    # Quick check: does the PCB even have any zones to fill?
    # KiCad's serializer wraps zones either as "(zone (net ...)" on one
    # line OR as "(zone\n  (net ...)" with a newline after the opening
    # token, so we need to detect both forms.
    try:
        text = output_path.read_text()
    except OSError as exc:
        logger.warning("Zone fill: could not read %s: %s", output_path, exc)
        return
    if "(zone " not in text and "(zone\n" not in text and "(zone\t" not in text:
        # Nothing to fill -- skip silently to avoid log noise on the
        # majority of boards that have no copper pours.
        return

    if not quiet:
        print("\n--- Filling Copper Zones ---")
        print(f"  Filling zones in {output_path.name}...")

    result = run_fill_zones(output_path, kicad_cli=kicad_cli)

    if not result.success:
        # Non-fatal: log warning and continue.  The board may still be
        # usable for partial inspection but the manufacturing artifacts
        # will lack plane copper.
        logger.warning(
            "Zone fill failed for %s: %s",
            output_path,
            result.stderr or "(no stderr)",
        )
        if not quiet:
            print("  Warning: zone fill failed -- Gerbers will lack plane copper")
        return

    # Validate net format after zone fill -- kicad-cli may corrupt nets.
    report = validate_net_format(output_path)
    if not report.valid:
        logger.warning(
            "Net format corruption detected after zone fill: "
            "%d element(s) have non-canonical net format "
            "(name_only_segments=%d, name_only_vias=%d, name_only_pads=%d, "
            "empty_net_segments=%d, empty_net_vias=%d, empty_net_pads=%d)",
            report.total_corrupt,
            report.name_only_segments,
            report.name_only_vias,
            report.name_only_pads,
            report.empty_net_segments,
            report.empty_net_vias,
            report.empty_net_pads,
        )

    if not quiet:
        print("  Zone fill: complete")


@dataclass
class LayerEscalationResult:
    """Result of a layer escalation routing attempt."""

    layer_count: int
    layer_stack: "LayerStack"
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    stats: dict | None = None
    overflow: int = 0


@dataclass
class RuleRelaxationResult:
    """Result of a rule relaxation routing attempt."""

    tier: int
    trace_width: float
    clearance: float
    via_drill: float
    via_diameter: float
    tier_description: str
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    layer_count: int = 2  # May be set by layer escalation integration
    stats: dict | None = None


def _is_better_result(
    candidate: "LayerEscalationResult | RuleRelaxationResult",
    best: "LayerEscalationResult | RuleRelaxationResult",
) -> bool:
    """Compare routing results with tiebreaking on connectivity metrics.

    Issue #2396: The primary comparison uses **absolute nets_routed** rather
    than completion ratio.  When ``nets_to_route`` differs across escalation
    attempts (e.g. power nets auto-skipped on 4L but not 2L), comparing
    ratios produces misleading results: 6/10 (0.60) vs 3/8 (0.375) looks
    like a clear win for 2L, but the raw ratio comparison used to use
    ``completion`` which could disagree when denominators differ.  Using
    absolute counts ensures we always keep the attempt that routed the most
    nets, breaking ties by completion ratio, then segments, vias, and layer
    count.
    """
    # Primary: absolute nets routed (cross-denominator safe)
    if candidate.nets_routed != best.nets_routed:
        return candidate.nets_routed > best.nets_routed

    # Tied on absolute count: use completion ratio as tiebreaker
    if candidate.completion != best.completion:
        return candidate.completion > best.completion

    # Tie on completion -- use stats-based tiebreakers (Issue #2397)
    c_stats = candidate.stats or {}
    b_stats = best.stats or {}

    c_segments = c_stats.get("segments", 0)
    b_segments = b_stats.get("segments", 0)
    if c_segments != b_segments:
        return c_segments > b_segments

    c_vias = c_stats.get("vias", 0)
    b_vias = b_stats.get("vias", 0)
    if c_vias != b_vias:
        return c_vias > b_vias

    # Still tied: prefer fewer layers (simpler board)
    return candidate.layer_count < best.layer_count


def update_pcb_layer_stackup(pcb_content: str, target_layers: int) -> str:
    """Update PCB content to have the specified number of copper layers.

    Args:
        pcb_content: Original PCB file content
        target_layers: Target number of copper layers (2, 4, or 6)

    Returns:
        Updated PCB content with correct layer definitions
    """
    import re

    # Layer definitions for different stackups
    layer_defs = {
        2: [
            '(0 "F.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        4: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        6: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(3 "In3.Cu" signal)',
            '(4 "In4.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
    }

    if target_layers not in layer_defs:
        return pcb_content

    # Check if we need to update — count ALL copper layers regardless of type
    # (signal, power, mixed, etc.) not just those marked "signal"
    current_layers = len(re.findall(r'\(\d+\s+"[^"]*\.Cu"\s+\w+', pcb_content))
    if current_layers >= target_layers:
        return pcb_content

    # Match the entire (layers ...) block including all inner entries.
    # Each inner entry is e.g. (0 "F.Cu" signal) or (44 "Edge.Cuts" user "Edge.Cuts").
    # The pattern matches from "(layers" through each "(...)" entry to the
    # block-closing ")".
    layers_pattern = re.compile(
        r'\(layers\s*\n(\s+\(\d+\s+"[^"]+"\s+\w+[^)]*\)\s*\n)+\s*\)',
        re.MULTILINE,
    )

    # Non-copper layer entry pattern (e.g. B.SilkS, Edge.Cuts, F.Fab)
    non_copper_re = re.compile(
        r'(\s*\(\d+\s+"(?!.*\.Cu")[^"]+"\s+\w+[^)]*\))',
    )

    def replace_layers(match):
        matched_text = match.group(0)
        # Extract non-copper layer entries from the original block
        non_copper_entries = non_copper_re.findall(matched_text)
        # Build new layers content with copper layers
        new_layers = "\n    ".join(layer_defs[target_layers])
        # Append non-copper layers after copper layers
        if non_copper_entries:
            non_copper_lines = "\n".join(entry.strip() for entry in non_copper_entries)
            return f"(layers\n    {new_layers}\n    {non_copper_lines}\n  )"
        return f"(layers\n    {new_layers}\n  )"

    new_content = layers_pattern.sub(replace_layers, pcb_content)

    # Validate output has balanced parentheses to catch regressions early
    if not _validate_sexp_parentheses(new_content):
        import warnings

        warnings.warn(
            "update_pcb_layer_stackup produced unbalanced parentheses; "
            "returning original content unchanged",
            stacklevel=2,
        )
        return pcb_content

    return new_content


def _print_power_stall_suggestions(
    stalled_nets: list[str],
    layer_count: int,
    pcb_arg: str,
) -> None:
    """Print actionable suggestions for a power-net stall (Issue #2388).

    Surfaces concrete remediation flags naming the stalled nets so users
    can pick the appropriate workaround instead of being told the router
    timed out.

    Args:
        stalled_nets: Names of the power/pour nets that stalled.
        layer_count: Layer count used for the failing attempt.
        pcb_arg: The original PCB argument the user passed (for echoing
            in suggested commands).
    """
    if not stalled_nets:
        return
    nets_csv = ", ".join(stalled_nets)
    # Default zone-layer assignment: GND on B.Cu, others on F.Cu.
    pour_assignments = []
    for n in stalled_nets:
        if n.upper() in {"GND", "VSS", "AGND", "DGND", "PGND", "GROUND"}:
            pour_assignments.append(f"{n}:B.Cu")
        else:
            pour_assignments.append(f"{n}:F.Cu")
    pour_arg = ",".join(pour_assignments)

    print()
    print(f"Routing did not complete: {nets_csv} could not be routed on {layer_count} layer(s).")
    print("Suggestions:")
    print(f'  1. Add copper zones for power nets:  --power-nets "{pour_arg}"')
    print("  2. Increase layer count:              --layers 4 (or --max-layers 6)")
    print(f"  3. Manual routing in KiCad for the {len(stalled_nets)} remaining net(s)")
    print()


def _stage_input_for_auto_pour(pcb_path: Path, output_path: Path) -> Path:
    """Stage the input PCB so ``auto_pour_if_missing`` does not mutate the user's input.

    ``auto_pour_if_missing`` writes zone definitions in-place to its target
    file.  When ``kct route INPUT -o OUTPUT`` is invoked with distinct paths
    the user expects ``INPUT`` to be left untouched; this helper copies
    ``pcb_path`` to ``output_path`` and returns ``output_path`` so the rest
    of the route flow can operate on the copy.

    When ``pcb_path`` and ``output_path`` resolve to the same file (the
    pipeline ``kct build`` case where input == output), no copy is needed
    and ``pcb_path`` is returned unchanged — preserving the existing
    in-place behavior the pipeline depends on.

    Args:
        pcb_path: User-supplied input PCB path.
        output_path: User-supplied output PCB path.

    Returns:
        Path that subsequent route steps should use as their working PCB.
        Either ``pcb_path`` (input == output) or ``output_path`` (after copy).

    See: issue #2548 — ``kct route`` should not modify INPUT when output
    differs.  Mirrors the ``shutil.copy2`` pattern in ``runner.py``.
    """
    if pcb_path.resolve() == output_path.resolve():
        return pcb_path
    shutil.copy2(pcb_path, output_path)
    return output_path


def _cleanup_stale_layer_artifacts(output_path: Path, quiet: bool = False) -> list[Path]:
    """Remove any stale ``<stem>_<N>layer.kicad_pcb`` siblings of *output_path*.

    The auto-layers escalation paths rename the output to include the layer
    count when escalation actually happens (e.g. ``board_routed_4layer.kicad_pcb``).
    A previous failed-2L run leaves that ``_4layer`` file behind; the next
    run that succeeds on 2 layers does NOT touch it.  The result is a
    confusing pair of files where only the canonical one reflects the
    current run (issue #2674).

    This helper deletes the stale siblings before routing begins so a
    clean run yields a clean output directory, deterministically reflecting
    the current run's result regardless of any prior failed attempts.

    Args:
        output_path: The canonical (un-suffixed) output PCB path that
            the route command was asked to write.  Stale ``_4layer`` and
            ``_6layer`` siblings of this path's stem are removed.
        quiet: Suppress informational output.

    Returns:
        List of paths that were actually removed.  Useful for tests
        and verbose logging.
    """
    removed: list[Path] = []
    # Accept str or Path -- the escalation entry points are sometimes
    # passed string paths in tests.
    output_path = Path(output_path)
    parent = output_path.parent
    stem = output_path.stem
    # Layer counts that the escalation path can produce (matches the
    # ``layer_configs`` table in ``route_with_layer_escalation`` and
    # ``route_with_combined_escalation``).
    for n in (4, 6):
        for suffix in (".kicad_pcb", ".kicad_prl"):
            stale = parent / f"{stem}_{n}layer{suffix}"
            if stale.exists():
                try:
                    stale.unlink()
                    removed.append(stale)
                except OSError:
                    # Best-effort cleanup; a permission error on a stale
                    # artifact must not abort the route.
                    continue
    if removed and not quiet:
        from kicad_tools.cli.progress import flush_print

        for p in removed:
            flush_print(f"  Removed stale artifact: {p.name}")
    return removed


def _detect_pcb_layer_profile(pcb_path: Path) -> tuple[int, bool]:
    """Inspect the PCB's declared ``(layers ...)`` block and inner-layer zones.

    Returns the number of copper layers and whether any *inner* copper layer
    has a zone definition (the canonical signature of a board that was
    designed against power/ground planes on ``In1.Cu``/``In2.Cu``).  These two
    pieces of information are what the layer-escalation loops need to refuse
    structurally-invalid 2L probes on a 4L board (issue #2916) and to put the
    plane-aware 4L variant ahead of the all-signal variant when planes exist.

    The parsing matches :func:`kicad_tools.router.io.detect_layer_stack` -- we
    reuse the same regexes so the two paths agree on what counts as a copper
    layer and what counts as a plane zone.

    Args:
        pcb_path: Path to the input ``.kicad_pcb`` file.

    Returns:
        ``(num_copper_layers, has_inner_plane_zones)``.  Returns
        ``(2, False)`` on any parse failure so the escalation loops fall
        through to legacy behaviour (start at 2L) rather than crashing.
    """
    import re

    # Accept str or Path (a few callers pass string paths, mirroring
    # ``_cleanup_stale_layer_artifacts``).
    pcb_path = Path(pcb_path)
    try:
        pcb_text = pcb_path.read_text()
    except OSError as exc:
        logger.debug("Could not read %s for layer detection: %s", pcb_path, exc)
        return (2, False)

    # Parse the (layers ...) section to find copper layers -- mirrors the
    # regex used by ``detect_layer_stack`` so both paths agree.
    copper_names: list[str] = []
    layers_match = re.search(r"\(layers\s+(.*?)\n\s*\)", pcb_text, re.DOTALL)
    if layers_match:
        for layer_match in re.finditer(r'\((\d+)\s+"([^"]+\.Cu)"\s+(\w+)', layers_match.group(1)):
            copper_names.append(layer_match.group(2))

    num_copper = len(copper_names) if copper_names else 2

    # Detect zones on inner layers (anything that is not F.Cu or B.Cu).
    has_inner_planes = False
    for zone_match in re.finditer(
        r'\(zone\s+.*?\(layer\s+"([^"]+)"\)',
        pcb_text,
        re.DOTALL,
    ):
        layer_name = zone_match.group(1)
        if layer_name.endswith(".Cu") and layer_name not in ("F.Cu", "B.Cu"):
            has_inner_planes = True
            break

    return (num_copper, has_inner_planes)


def _filter_layer_configs_for_pcb(
    layer_configs: list[tuple[int, "LayerStack"]],
    pcb_path: Path,
    max_layers: int,
    quiet: bool = False,
) -> list[tuple[int, "LayerStack"]]:
    """Filter and reorder *layer_configs* to honour the PCB's declared stackup.

    Issue #2916: ``--auto-layers`` previously started at 2L for every board
    regardless of the PCB's declared copper count.  On a 4L board (e.g.
    chorus-test-revA with ``In1.Cu``/``In2.Cu`` plane zones already drawn) the
    2L probe burns a fair share of the wall-clock budget on a configuration
    that cannot succeed, leaving the real 4L attempt to start against an
    exhausted deadline (issue #2823 + #2802).

    This helper applies three transformations:

    1. **Floor by detected copper count.**  Entries whose ``layer_count`` is
       below the PCB's declared count are dropped.  A 4-copper-layer PCB has
       4L as its minimum sensible probe; 2L is never tried.
    2. **Promote plane-aware variants.**  When inner-layer zones are present
       in the PCB (the ``four_layer_sig_gnd_pwr_sig`` shape), reorder so the
       plane-aware 4L variant runs before ``four_layer_all_signal``.  This
       matches what the single-pass path already does via
       :func:`detect_layer_stack`.
    3. **Honour ``--max-layers``.**  If the user-requested cap is below the
       detected count, emit a warning and keep the cap (the user's explicit
       wish wins, but they are told that it is structurally below the
       declared stackup so a partial/failed result is expected).

    Args:
        layer_configs: The unfiltered escalation ladder
            ``[(n_layers, LayerStack), ...]``.
        pcb_path: Path to the input ``.kicad_pcb`` file (used to probe the
            declared copper count and inner-zone presence).
        max_layers: User-requested ``--max-layers`` cap.
        quiet: Suppress informational output.

    Returns:
        A new list with entries below ``detected_count`` removed, plane-aware
        variants promoted (when applicable), and capped at ``max_layers``.
        Falls through to the input list unchanged when detection fails or the
        detected count is <= 2 (the natural starting point for the ladder).
    """
    from kicad_tools.cli.progress import flush_print

    detected_count, has_inner_planes = _detect_pcb_layer_profile(pcb_path)

    # Honour ``--max-layers`` even when it falls below the declared count --
    # but warn loudly so the user knows their cap is structurally too low.
    effective_floor = detected_count
    if max_layers < detected_count:
        if not quiet:
            flush_print(
                f"  Warning: --max-layers={max_layers} is below the PCB's "
                f"declared copper count ({detected_count}).  Using the "
                f"requested cap, but the board was designed for "
                f"{detected_count} layers so routing will likely fail or "
                "produce a partial result (issue #2916)."
            )
        # User's explicit cap wins -- relax the floor so the ladder still
        # has at least one rung.
        effective_floor = min(detected_count, max_layers)

    # Apply max_layers cap as before.
    filtered = [(n, s) for n, s in layer_configs if n <= max_layers]

    # Drop entries below the detected floor (or the user's cap if lower).
    filtered = [(n, s) for n, s in filtered if n >= effective_floor]

    # When inner-layer plane zones exist, promote the plane-aware 4L variant
    # ahead of the all-signal variant.  This matches the single-pass path
    # which calls ``detect_layer_stack`` and returns the plane-aware shape.
    if has_inner_planes:
        plane_aware = []
        all_signal = []
        other = []
        for n, s in filtered:
            if n == 4 and "ALL-SIG" in s.name.upper():
                all_signal.append((n, s))
            elif n == 4:
                plane_aware.append((n, s))
            else:
                other.append((n, s))
        # Preserve overall layer-count ordering: 4L (plane-aware then
        # all-signal) sandwiched between any 2L (none after floor) and 6L.
        # ``other`` already preserves the relative ordering of the original
        # list, so we splice the 4L entries back at their natural position.
        if plane_aware or all_signal:
            result = []
            inserted_four = False
            for n, s in other:
                if n > 4 and not inserted_four:
                    result.extend(plane_aware)
                    result.extend(all_signal)
                    inserted_four = True
                result.append((n, s))
            if not inserted_four:
                result.extend(plane_aware)
                result.extend(all_signal)
            filtered = result

    # Defensive: if the filter wiped the ladder (e.g. detected count > 6 and
    # max_layers < detected), fall back to the original list capped at
    # max_layers so the loop still has something to try.  The warning above
    # already alerted the user.
    if not filtered:
        filtered = [(n, s) for n, s in layer_configs if n <= max_layers]

    if not quiet and detected_count > 2:
        dropped = len(layer_configs) - len(filtered)
        if dropped > 0 or has_inner_planes:
            ladder_str = ", ".join(f"{n}L" for n, _ in filtered)
            flush_print(
                f"  Detected {detected_count}-copper-layer PCB"
                f"{' with inner plane zones' if has_inner_planes else ''}; "
                f"escalation ladder: [{ladder_str}] (issue #2916)"
            )

    return filtered


# Issue #3035: ``_auto_skip_pour_nets`` was promoted to
# ``kicad_tools.router.auto_pour.auto_skip_pour_nets``; the alias is bound
# near the top of this module (search ``Issue #3035``).  The 4 call sites
# in this file keep using ``_auto_skip_pour_nets`` unchanged so the
# ``@patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", ...)``
# decorators in ``tests/test_layer_escalation.py`` and
# ``tests/test_route_auto_fix.py`` continue to resolve.


def _apply_net_class_map_sidecar(router: "Autorouter", args, quiet: bool = False) -> None:
    """Merge the pre-loaded --net-class-map sidecar into the router (Issue #2996).

    ``main()`` validates and deserializes the sidecar early (so error
    paths short-circuit before any routing work runs) and stashes the
    resolved ``{net_name: NetClassRouting}`` map on
    ``args._loaded_net_class_map``.  Each post-load callsite (the
    standalone path in ``main()`` plus the three ``route_with_*``
    wrappers) calls this helper to merge the rich per-pair / per-group
    fields onto the router's name-pattern-classified map.

    Idempotent and a no-op when the flag was not supplied.
    """
    loaded = getattr(args, "_loaded_net_class_map", None)
    if not loaded:
        return
    router.net_class_map.update(loaded)
    if not quiet:
        from kicad_tools.cli.progress import flush_print

        flush_print(f"  Net-class map: merged {len(loaded)} sidecar entries")


def route_with_layer_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic layer escalation.

    Tries routing at 2, 4, and 6 layers until success or max is reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        ensure_cpp_backend_available,
        load_pcb_for_routing,
        show_routing_summary,
    )

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=quiet,
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Configure design rules
    fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
    rules = DesignRules(
        grid_resolution=args.grid,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
        fine_pitch_clearance=fine_pitch_cl,
        # Issue #2695: forward manufacturer so the escape router can opt in
        # to in-pad escape for fine-pitch LQFP/QFP (and SSOP/TSSOP) when the
        # manufacturer supports via-in-pad processing.
        manufacturer=getattr(args, "manufacturer", None),
        # Issue #2891: forward escalation-in-progress flag so the escape
        # router demotes the #2880 ERROR log when an outer wrapper will
        # retry on a tier that supports via-in-pad.
        auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
    )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Issue #2674: remove stale ``<stem>_<N>layer.kicad_pcb`` siblings
    # from a previous failed-2L run before routing begins.  Without this,
    # a successful 2L run leaves the prior failed 4L/6L artifact behind
    # and the output directory shows a confusing pair of routed PCBs.
    _cleanup_stale_layer_artifacts(output_path, quiet=quiet)

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Issue #2916: filter and reorder by the PCB's declared stackup.
    # Drops entries below the detected copper count (so a 4L board never
    # wastes budget on a 2L probe) and promotes the plane-aware 4L variant
    # ahead of all-signal when inner plane zones exist.
    layer_configs = _filter_layer_configs_for_pcb(
        layer_configs, pcb_path, args.max_layers, quiet=quiet
    )

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Layer Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Max layers:     {args.max_layers}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()

    best_result: LayerEscalationResult | None = None
    successful_result: LayerEscalationResult | None = None

    # Issue #2412: Track previous attempt metrics for early termination
    prev_nets_routed: int | None = None
    prev_overflow: int | None = None

    # Issue #2388: Track power-net stall across escalation attempts.  When
    # a 2-layer attempt aborts due to power-net stall, the next attempt
    # is biased toward a stack with dedicated planes for those nets, and
    # we auto-extend skip_nets with the plane nets so the router relies
    # on the plane connections instead of routing power as signals.
    last_power_stall_nets: list[str] = []

    # Issue #3051: Build the checkpoint callback ONCE before the
    # escalation loop so every per-attempt ``route_all_negotiated`` call
    # gets best-so-far persistence (closes the iteration-0 kill-loses-
    # work hole observed in the curator audit).  The primary single-
    # attempt path at the bottom of ``main()`` already wires this
    # callback; without lifting it here, kills mid-loop on the
    # layer-escalation path produce an empty output PCB.
    _checkpoint_cb = _make_checkpoint_callback(
        pcb_path,
        output_path,
        float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
        quiet=quiet,
    )

    for attempt_num, (layer_count, layer_stack) in enumerate(layer_configs, 1):
        # Issue #2802: honor the total wall-clock deadline before starting
        # another layer-stack attempt.  Without this guard the loop would
        # blindly start a fresh ``route_all_negotiated`` call (each with
        # its own copy of ``args.timeout``) even after the user-configured
        # total budget has expired.
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before attempt {attempt_num}; "
                    "stopping layer escalation (issue #2802)"
                )
            break

        # Issue #2388: When the previous attempt stalled on power nets and
        # this stack provides dedicated planes for them, auto-skip those
        # plane nets so the router doesn't try to route them as signals.
        attempt_skip_nets = list(skip_nets)
        plane_nets_in_stack = {lyr.plane_net for lyr in layer_stack.plane_layers if lyr.plane_net}
        if last_power_stall_nets and plane_nets_in_stack:
            auto_plane_skip = [
                n
                for n in last_power_stall_nets
                if n in plane_nets_in_stack and n not in attempt_skip_nets
            ]
            if auto_plane_skip:
                attempt_skip_nets.extend(auto_plane_skip)
                if not quiet:
                    flush_print(
                        f"  Auto-skipping {', '.join(auto_plane_skip)} "
                        "(connected via dedicated plane(s) in this stack)"
                    )

        if not quiet:
            flush_print("=" * 60)
            flush_print(f"Attempt {attempt_num}: {layer_count} layers ({layer_stack.name})")
            flush_print("=" * 60)

        # Load PCB with this layer stack
        try:
            with spinner(f"Loading PCB ({layer_count} layers)...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=attempt_skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    validate_drc=not args.force,
                    strict_drc=False,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Issue #2996: merge --net-class-map sidecar onto router's map.
        _apply_net_class_map_sidecar(router, args, quiet=quiet)

        # Issue #2396: Ensure pristine per-attempt state.  Today this is a
        # no-op (load_pcb_for_routing creates a fresh Autorouter) but it
        # documents the contract and prevents silent regression if future
        # refactors reuse an Autorouter across attempts.
        router.reset_attempt_state()

        # Issue #1841: Tell the autorouter which pour nets lack zones
        router._pour_nets_without_zones = set(_no_zone)

        # Count nets to route
        multi_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
        ]
        single_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) == 1
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            flush_print(f"  Nets to route: {nets_to_route}")
            _emit_single_pad_net_warning(router, single_pad_nets)

        # Route
        if not quiet:
            flush_print(f"\n  Routing ({args.strategy})...")

        escape_flag = _resolve_escape_routing_flag(args)

        # Issue #2823: divide the remaining wall-clock budget fairly across
        # the remaining layer-escalation attempts.  Without this, the first
        # attempt (typically 2L) greedily consumes the entire ``--timeout``,
        # leaving the higher-layer attempts (4L, 6L) no time to run -- so
        # the escalation strategy degenerates to "spend everything on the
        # lowest layer count, give up."  ``attempt_num`` is 1-based; the
        # helper expects a 0-based index, hence ``attempt_num - 1``.
        # Falls back to ``args.timeout`` when no deadline is configured.
        _attempt_timeout = _per_attempt_budgeted_timeout(
            args,
            attempt_index=attempt_num - 1,
            max_attempts=len(layer_configs),
        )

        try:
            if _should_use_escape_routing(router, escape_flag, quiet):
                router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                )
            elif getattr(args, "multi_resolution", False):
                router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                    # Issue #3039: forward --seed for deterministic routing.
                    seed=getattr(args, "seed", None),
                    # Issue #3051: forward checkpoint callback so kills
                    # mid-loop persist the best-so-far snapshot.
                    checkpoint_callback=_checkpoint_cb,
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            elif args.strategy == "evolutionary":
                router.route_all_evolutionary(
                    pop_size=args.pop_size,
                    generations=args.generations,
                    verbose=args.verbose and not quiet,
                    timeout=_attempt_timeout,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Issue #2426: Run cleanup before computing statistics so that the
        # best-result selector compares post-cleanup connectivity counts —
        # the same metric shown in the final summary.  cleanup_artifacts()
        # is idempotent, so the subsequent call in _finalize_routes() is a
        # safe no-op.
        router.cleanup_artifacts()

        # Calculate completion — filter to multi-pad nets only (Issue #1643)
        multi_pad_net_ids = set(multi_pad_nets)
        stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Issue #2412: Capture overflow for early termination detection
        overflow = int(router.grid.get_total_overflow())

        # Create result
        result = LayerEscalationResult(
            layer_count=layer_count,
            layer_stack=layer_stack,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            stats=stats,
            overflow=overflow,
        )

        # Track best result (Issue #2396: absolute nets_routed comparison)
        if best_result is None or _is_better_result(result, best_result):
            best_result = result
            if not quiet:
                flush_print(
                    f"  Best result so far: {best_result.layer_count}L with "
                    f"{best_result.nets_routed}/{best_result.nets_to_route} "
                    f"({best_result.completion:.0%})"
                )

        # Issue #2673: Completion-floor guard.  Both early-termination heuristics
        # below (zero-overflow at #2412, stagnation at #2412) are calibrated for
        # the case where the router already routed a substantial fraction of the
        # board on the prior attempt.  When best-so-far completion is very low
        # (e.g., < 50%), the failure mode is more likely "router got stuck on a
        # handful of nets and timed out" than "the design is genuinely unrouteable
        # with more layers", so we should keep trying additional layer
        # configurations rather than short-circuit.  Board 05 on 2026-05-11
        # exhibits exactly this regression: 2L=0/35, 4L sig-gnd-pwr-sig=0/35,
        # stagnation check fires and 4L all-sig is never attempted.  The same
        # board at commit a9790ad0 produced 2L=9/35 (26%) and tried all three
        # 4L variants before stopping.  Floor of 50% is conservative — any
        # board with >=50% completion has enough signal for the heuristics
        # to be trustworthy; below that, escalation should run to completion.
        best_completion_so_far = best_result.completion if best_result is not None else 0.0
        completion_floor_for_early_stop = 0.5
        below_completion_floor = best_completion_so_far < completion_floor_for_early_stop

        # Issue #2412: Early termination — zero overflow means failures
        # are placement/topology issues, not congestion.  Adding layers
        # cannot help when there is no congestion to relieve.
        #
        # Issue #2634: This heuristic was calibrated for ``route_all_negotiated``,
        # which deliberately allows overlapping tracks and records overflow as
        # a first-class congestion signal.  The basic A*, monte-carlo, and
        # evolutionary strategies never plant overlaps, so ``grid.get_total_overflow()``
        # is 0 by construction regardless of true congestion.  Reading 0 from
        # those strategies as "no congestion" is wrong, and historically it
        # broke after attempt #1 even with ``--auto-layers`` on (see the
        # chorus-test-revA fixture, which negotiated escalates 2L -> 4L while
        # MC stopped at 2L=42%).  Skip the heuristic for those strategies.
        strategies_without_overflow_signal = {"monte-carlo", "basic", "evolutionary"}
        if (
            overflow == 0
            and nets_routed < nets_to_route
            and args.strategy not in strategies_without_overflow_signal
            and not below_completion_floor
        ):
            if not quiet:
                flush_print(
                    "  Escalation stopped: failures are not congestion-related (overflow=0)"
                )
            # Report attempt result before breaking
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (zero overflow)")
            break
        elif (
            overflow == 0
            and nets_routed < nets_to_route
            and args.strategy not in strategies_without_overflow_signal
            and below_completion_floor
            and not quiet
        ):
            flush_print(
                "  Note: overflow=0 but best completion "
                f"{best_completion_so_far * 100:.0f}% < "
                f"{completion_floor_for_early_stop * 100:.0f}% floor — "
                "continuing escalation (issue #2673)"
            )

        # Issue #2412: Early termination — stagnation detection.  If adding
        # layers did not improve nets_routed or reduce overflow, further
        # escalation is unlikely to help.
        if (
            prev_nets_routed is not None
            and nets_routed <= prev_nets_routed
            and overflow >= prev_overflow
            and not below_completion_floor
        ):
            if not quiet:
                flush_print("  Escalation stopped: no improvement after adding layers")
            # Report attempt result before breaking
            if not quiet:
                flush_print(
                    f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)"
                )
                flush_print("  Status: INSUFFICIENT - early stop (stagnation)")
            break
        elif (
            prev_nets_routed is not None
            and nets_routed <= prev_nets_routed
            and overflow >= prev_overflow
            and below_completion_floor
            and not quiet
        ):
            flush_print(
                "  Note: stagnation detected but best completion "
                f"{best_completion_so_far * 100:.0f}% < "
                f"{completion_floor_for_early_stop * 100:.0f}% floor — "
                "continuing escalation (issue #2673)"
            )

        prev_nets_routed = nets_routed
        prev_overflow = overflow

        # Issue #2388: Record any power-net stall for the next attempt's
        # bias logic.  ``power_stall_nets`` is populated by
        # ``route_all_negotiated`` when the early-abort heuristic fires.
        if getattr(router, "power_stall_abort", False):
            last_power_stall_nets = list(getattr(router, "power_stall_nets", []))
            if not quiet and last_power_stall_nets:
                flush_print(
                    f"  Power-net stall on this attempt: {', '.join(last_power_stall_nets)}"
                )
        else:
            last_power_stall_nets = []

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - escalating"
        if not quiet:
            flush_print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            flush_print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("LAYER ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"on any layer count (max: {args.max_layers})"
            )
            # Issue #2388: Surface actionable suggestions when escalation
            # exhausted because of a power-net stall.
            if last_power_stall_nets:
                _print_power_stall_suggestions(
                    last_power_stall_nets,
                    final_result.layer_count,
                    args.pcb,
                )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
            if last_power_stall_nets:
                _print_power_stall_suggestions(
                    last_power_stall_nets,
                    args.max_layers,
                    args.pcb,
                )
        return 1

    # Issue #2621: placement-routing feedback for partial layer-escalation
    # results.  The main single-pass entry point hooks ``_run_placement_feedback``
    # at the end of routing (route_cmd.py around line 4802), but the
    # auto-layers escalation path never reached that hook -- so
    # ``--placement-feedback`` on a board that exhausts layer escalation
    # with PARTIAL completion silently did nothing.  Engage the feedback
    # loop here, mirroring the main-path trigger:
    #   * only when escalation didn't already succeed,
    #   * only when --placement-feedback is set,
    #   * only when there are still failed nets to address.
    # The loop mutates ``final_result.router.routes`` in place, so we
    # refresh ``final_result``'s stats afterwards before optimize/save.
    if (
        successful_result is None
        and getattr(args, "placement_feedback", False)
        and final_result.router.routes is not None
        and final_result.router.get_failed_nets()
    ):
        if not quiet:
            print(
                f"\n--- Engaging placement-routing feedback "
                f"(escalation stalled at {final_result.completion * 100:.0f}%) ---"
            )
        _run_placement_feedback(
            router=final_result.router,
            pcb_path=pcb_path,
            args=args,
            quiet=quiet,
        )
        # Refresh completion stats from the post-feedback router state so
        # optimize/save/summary all see the correct numbers.
        _refreshed_multi_pad_ids = {
            n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
        }
        _refreshed = final_result.router.get_statistics(nets_to_route_ids=_refreshed_multi_pad_ids)
        final_result.nets_routed = _refreshed["nets_routed"]
        final_result.completion = (
            final_result.nets_routed / final_result.nets_to_route
            if final_result.nets_to_route > 0
            else 1.0
        )
        final_result.success = final_result.completion >= args.min_completion
        if not quiet:
            print(
                f"  Post-feedback: {final_result.nets_routed}/"
                f"{final_result.nets_to_route} "
                f"({final_result.completion * 100:.0f}%)"
            )

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.  This prevents the
        # optimizer from fragmenting routes through overused cells.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize so
        # we can revert any net whose pad-to-pad connectivity regresses.
        _ci_snapshot = _connectivity_snapshot(final_result.router)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity again before nudge.  The
        # post-optimize routes are the new baseline -- nudge must not
        # regress them further.
        _ci_snapshot_nudge = _connectivity_snapshot(final_result.router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2}
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        if not route_sexp and not quiet:
            print("  Warning: No routes generated!")
        # Issue #2808: atomic write via _write_routed_pcb (consolidates the
        # read -> stackup-update -> insert -> validate -> write sequence).
        # Issue #2809: honor --output exactly; layer count is recorded in
        # the PCB content via update_pcb_layer_stackup, NOT in the filename.
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            layer_count=final_result.layer_count,
        )

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")

    # Fill copper-pour zones now that traces exist (issue #2516).
    # Must run BEFORE DRC so the DRC sees filled zones rather than bare
    # zone outlines and does not flag zone-to-trace clearance against
    # unfilled polygons.
    if final_result.nets_routed > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped
    fix_result: int | None = None
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: thread the autorouter's
            # net_class_map into the post-route DRC so the diff-pair
            # routing-continuity rule can re-derive its engagement state.
            net_class_map=getattr(final_result.router, "net_class_map", None),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(f"SUCCESS: Design requires minimum {final_result.layer_count} layers")
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"on {final_result.layer_count} layers"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
                # Issue #2634: auto-layer escalation already ran in this code
                # path; suppress the redundant "Try --auto-layers" recommendation.
                auto_layers_attempted=True,
            )

    # Issue #2881: Stash the final router on args so an outer
    # ``route_with_mfr_tier_escalation`` wrapper can inspect
    # ``missed_via_in_pad_rescues`` to decide whether to escalate to the
    # next manufacturer tier.  Harmless when no outer wrapper exists.
    with contextlib.suppress(AttributeError, TypeError):
        args._last_router = final_result.router

    if final_result.success:
        # Issue #2852: propagate --auto-fix rollback (exit 3) so callers can
        # detect a silent rollback on an otherwise-clean routing run.  The
        # documented exit-3 contract ("meets threshold but DRC violations
        # detected") already covers this case semantically.
        if fix_result == 3:
            return 3
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def route_with_rule_relaxation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic design rule relaxation.

    Tries routing with progressively relaxed design rules (trace width,
    clearance) until success or manufacturer minimum limits are reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        ensure_cpp_backend_available,
        get_relaxation_tiers,
        load_pcb_for_routing,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=quiet,
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Determine layer stack
    if args.layers == "auto":
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "4-all": LayerStack.four_layer_all_signal(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Adaptive Rules Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Manufacturer:   {args.manufacturer}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        flush_print(f"Relaxation tiers: {len(tiers)}")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None

    # Register signal handlers so SIGTERM/SIGINT save the best attempt so far
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["router"] = None
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_completed_attempt"] = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)

    # Issue #3051: Build the checkpoint callback ONCE before the
    # relaxation loop so every per-tier ``route_all_negotiated`` call
    # gets best-so-far persistence (closes the iteration-0 kill-loses-
    # work hole observed in the curator audit).
    _checkpoint_cb = _make_checkpoint_callback(
        pcb_path,
        output_path,
        float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
        quiet=quiet,
    )

    # Issue #2823: precompute total tier count so per-attempt budget can
    # divide the remaining wall-clock budget fairly across all tiers
    # rather than letting tier 0 greedily consume the full ``--timeout``.
    _relaxation_max_attempts = max(1, len(tiers))
    for _tier_idx, tier in enumerate(tiers):
        # Issue #2802: honor the total wall-clock deadline before starting
        # another rule-relaxation tier.
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before tier {tier.tier + 1}; "
                    "stopping rule relaxation (issue #2802)"
                )
            break

        if not quiet:
            flush_print("=" * 60)
            flush_print(f"Attempt {tier.tier + 1}: {tier.description}")
            flush_print(f"  trace={tier.trace_width:.3f}mm, clearance={tier.clearance:.3f}mm")
            flush_print("=" * 60)

        # Configure design rules for this tier
        fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
        rules = DesignRules(
            grid_resolution=args.grid,
            trace_width=tier.trace_width,
            trace_clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            fine_pitch_clearance=fine_pitch_cl,
            # Issue #2695: forward manufacturer so the escape router can opt
            # in to in-pad escape for fine-pitch LQFP/QFP/SSOP/TSSOP when
            # the manufacturer supports via-in-pad processing.
            manufacturer=getattr(args, "manufacturer", None),
            # Issue #2891: forward escalation-in-progress flag.
            auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
        )

        # Load PCB
        try:
            with spinner(f"Loading PCB (tier {tier.tier})...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    validate_drc=not args.force,
                    strict_drc=False,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Issue #2996: merge --net-class-map sidecar onto router's map.
        _apply_net_class_map_sidecar(router, args, quiet=quiet)

        # Issue #1841: Tell the autorouter which pour nets lack zones
        router._pour_nets_without_zones = set(_no_zone)

        # Count nets to route
        multi_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
        ]
        single_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) == 1
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            flush_print(f"  Nets to route: {nets_to_route}")
            _emit_single_pad_net_warning(router, single_pad_nets)

        # Route
        if not quiet:
            flush_print(f"\n  Routing ({args.strategy})...")

        escape_flag = _resolve_escape_routing_flag(args)

        # Issue #2823: divide the remaining wall-clock budget fairly across
        # the remaining rule-relaxation tiers so the looser-rule attempts
        # also get a real chance to run.  Without this, tier 0 greedily
        # consumes the full ``--timeout`` and the relaxation strategy
        # degenerates to "spend everything on the strictest tier, give up."
        # Falls back to ``args.timeout`` when no deadline is configured.
        _attempt_timeout = _per_attempt_budgeted_timeout(
            args,
            attempt_index=_tier_idx,
            max_attempts=_relaxation_max_attempts,
        )

        try:
            if _should_use_escape_routing(router, escape_flag, quiet):
                router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                )
            elif getattr(args, "multi_resolution", False):
                router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=_attempt_timeout,
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                    # Issue #3039: forward --seed for deterministic routing.
                    seed=getattr(args, "seed", None),
                    # Issue #3051: forward checkpoint callback so kills
                    # mid-loop persist the best-so-far snapshot.
                    checkpoint_callback=_checkpoint_cb,
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            elif args.strategy == "evolutionary":
                router.route_all_evolutionary(
                    pop_size=args.pop_size,
                    generations=args.generations,
                    verbose=args.verbose and not quiet,
                    timeout=_attempt_timeout,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Issue #2426: Run cleanup before computing statistics so that the
        # best-result selector compares post-cleanup connectivity counts.
        router.cleanup_artifacts()

        # Calculate completion — filter to multi-pad nets only (Issue #1643)
        multi_pad_net_ids = set(multi_pad_nets)
        stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Create result
        result = RuleRelaxationResult(
            tier=tier.tier,
            trace_width=tier.trace_width,
            clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            tier_description=tier.description,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            layer_count=layer_stack.num_layers,
            stats=stats,
        )

        # Track best result (Issue #2396: absolute nets_routed comparison)
        if best_result is None or _is_better_result(result, best_result):
            best_result = result
            # Update interrupt state so signal handler saves the best attempt
            _interrupt_state["router"] = result.router
            _interrupt_state["best_completed_attempt"] = True

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - relaxing rules"
        if not quiet:
            flush_print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            flush_print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

        # Early termination: skip remaining tiers when completion regresses
        if not getattr(args, "no_early_stop", False) and best_result is not None:
            if completion < best_result.completion:
                if not quiet:
                    flush_print(
                        f"\n  Early stop: tier {tier.tier + 1} completion "
                        f"({completion * 100:.0f}%) is worse than best "
                        f"({best_result.completion * 100:.0f}%) — "
                        f"skipping remaining tiers"
                    )
                break

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)
    _interrupt_state["best_completed_attempt"] = False

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("ADAPTIVE RULES SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully with relaxed rules "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm (was {args.trace_width}mm)")
            print(f"  Clearance:   {final_result.clearance:.3f}mm (was {args.clearance}mm)")
            if final_result.tier > 0:
                print(f"\n  Note: Rules were relaxed ({final_result.tier_description})")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at tier {final_result.tier} "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"even at manufacturer minimum tolerances"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider adding layers for more manufacturing margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize.
        _ci_snapshot = _connectivity_snapshot(final_result.router)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity before nudge.
        _ci_snapshot_nudge = _connectivity_snapshot(final_result.router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2}
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        if not route_sexp and not quiet:
            print("  Warning: No routes generated!")
        # Issue #2808: atomic write via _write_routed_pcb.  Rule-relaxation
        # flow always runs at the same layer count it started with, so we
        # pass layer_count=2 (no stackup update needed for the typical 2L
        # rule-relaxation path; layer_count is a no-op when <= 2 anyway).
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            layer_count=final_result.layer_count,
        )

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Fill copper-pour zones now that traces exist (issue #2516).
    if final_result.nets_routed > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped
    fix_result: int | None = None
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: thread the autorouter's
            # net_class_map into the post-route DRC so the diff-pair
            # routing-continuity rule can re-derive its engagement state.
            net_class_map=getattr(final_result.router, "net_class_map", None),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print("SUCCESS: Routing complete with adaptive rules")
            if final_result.tier > 0:
                print(
                    f"  Note: Relaxed from tier 0 to tier {final_result.tier} "
                    f"({final_result.tier_description})"
                )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at tier {final_result.tier}"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
            )

    if final_result.success:
        # Issue #2852: propagate --auto-fix rollback (exit 3) so callers can
        # detect a silent rollback on an otherwise-clean routing run.
        if fix_result == 3:
            return 3
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def _classify_dominant_failure_cause(router):
    """Pick the most-common FailureCause across ``router.routing_failures``.

    Issue #2883: the outer ``--auto-mfr-tier`` loop needs to consult the
    :data:`router.failure_analysis.MFR_TIER_ESCALATION_TRIGGERS` registry
    before walking forward.  That requires picking a single dominant cause
    from the (possibly heterogeneous) set of per-net failures returned by
    the inner routing attempt.

    The rule used here:

    1. If the router has no ``routing_failures`` attribute (e.g. the inner
       call was stubbed in tests or returned before classification), return
       ``None`` so callers fall back to the legacy
       ``missed_via_in_pad_rescues`` signal.
    2. Otherwise, tally :class:`FailureCause` values across all failures
       and return the most common.  Ties are broken by the registry order
       in :data:`MFR_TIER_ESCALATION_TRIGGERS` (triggering causes win), so
       that an even split between e.g. PIN_ACCESS and BLOCKED_PATH still
       lets escalation engage.

    Args:
        router: Inner ``Autorouter`` instance (may be ``None`` or a mock
            that lacks ``routing_failures``).

    Returns:
        The dominant :class:`FailureCause`, or ``None`` if no failure
        records were available.
    """
    from collections import Counter

    if router is None:
        return None
    failures = getattr(router, "routing_failures", None)
    if not failures:
        return None

    # Tally causes; ignore entries without a recognized FailureCause.
    causes = []
    for f in failures:
        cause = getattr(f, "failure_cause", None)
        if cause is not None:
            causes.append(cause)
    if not causes:
        return None

    counter = Counter(causes)
    most_common_count = counter.most_common(1)[0][1]

    # Find all causes tied for most common.
    tied = [c for c, n in counter.items() if n == most_common_count]
    if len(tied) == 1:
        return tied[0]

    # Tie-break: prefer a triggering cause so escalation isn't suppressed
    # by an arbitrary tally tie.  Lazy import to avoid module cycles.
    from kicad_tools.router.failure_analysis import MFR_TIER_ESCALATION_TRIGGERS

    for cause in tied:
        if MFR_TIER_ESCALATION_TRIGGERS.get(cause, False):
            return cause
    # No triggering cause present in the tie; return any (the first).
    return tied[0]


def route_with_mfr_tier_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with manufacturer-tier escalation (Issue #2881).

    Walks the ladder of manufacturer tiers (e.g. ``jlcpcb`` ->
    ``jlcpcb-tier1``).  For each tier in the ladder, mutates
    ``args.manufacturer`` to point at that tier and re-enters the
    layer-escalation path (``--auto-layers`` is enabled implicitly because
    this entry point is only reached when the user passed
    ``--auto-mfr-tier``; the inner call respects whatever
    ``args.auto_layers`` is set to).

    Escalation triggers on the per-attempt ``EscapeRouter`` instrumentation
    counter (``missed_via_in_pad_rescues``).  When that counter is non-zero
    after a routing attempt completes, the next tier in the ladder is
    tried -- but only if that tier offers a real capability gain (via-in-pad
    OR scalar relaxation; see :func:`mfr_limits.can_escalate_via_in_pad`
    and :func:`can_escalate_scalar`).  Pure same-scalar/same-capability
    tier swaps are skipped to avoid pointless retry loops.

    Trigger table (from Issue #2881):

    +----------------------------+-------------+----------------------------+
    | FailureCause               | Escalate?   | Why / why not              |
    +----------------------------+-------------+----------------------------+
    | PIN_ACCESS + fine-pitch    | YES         | Exactly what tier1 fixes.  |
    | + via_in_pad missing       |             |                            |
    +----------------------------+-------------+----------------------------+
    | CLEARANCE at mfr minimum   | conditional | Only if next tier offers   |
    |                            |             | scalar relaxation.         |
    +----------------------------+-------------+----------------------------+
    | BLOCKED_PATH               | NO          | Placement issue, not       |
    |                            |             | manufacturer-fixable.      |
    +----------------------------+-------------+----------------------------+
    | CONGESTION                 | NO          | Layer issue;               |
    |                            |             | --auto-layers handles it.  |
    +----------------------------+-------------+----------------------------+
    | UNKNOWN / timeouts         | NO          | Algorithm issue, may mask  |
    |                            |             | bugs.                      |
    +----------------------------+-------------+----------------------------+

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments (must have ``manufacturer``,
            ``auto_mfr_tier``, optionally ``mfr_tier_ladder``)
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure, 2 = partial)
    """
    from kicad_tools.cli.progress import flush_print
    from kicad_tools.router.failure_analysis import (
        MFR_TIER_ESCALATION_TRIGGERS,
        should_escalate_mfr_tier,
    )
    from kicad_tools.router.mfr_limits import (
        can_escalate_scalar,
        can_escalate_via_in_pad,
        get_mfr_limits,
        get_mfr_tier_ladder,
    )

    # Resolve the ladder.  Explicit --mfr-tier-ladder wins; otherwise look
    # up the default ladder for args.manufacturer.
    explicit_ladder = getattr(args, "mfr_tier_ladder", None)
    if explicit_ladder:
        ladder = [t.strip() for t in explicit_ladder.split(",") if t.strip()]
        # Validate each ladder entry resolves to a real manufacturer.
        for tier_name in ladder:
            get_mfr_limits(tier_name)  # raises ValueError on unknown
    else:
        try:
            ladder = get_mfr_tier_ladder(args.manufacturer)
        except ValueError as e:
            flush_print(f"Error: {e}", file=sys.stderr)
            return 1

    # Find the user's starting tier in the ladder.  If args.manufacturer
    # is not in the ladder, start from the first ladder entry that
    # matches; otherwise prepend args.manufacturer at the head.
    start_idx = 0
    cur_canonical = args.manufacturer.lower()
    for i, t in enumerate(ladder):
        if t.lower() == cur_canonical:
            start_idx = i
            break

    tiers_to_try = ladder[start_idx:]

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Manufacturer-Tier Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:           {pcb_path}")
        flush_print(f"Output:          {output_path}")
        flush_print(f"Starting tier:   {args.manufacturer}")
        flush_print(f"Tier ladder:     {' -> '.join(tiers_to_try)}")
        flush_print()

    last_exit_code: int = 1
    last_router = None
    final_exit_code: int = 1
    saw_terminating_success: bool = False

    # Issue #2891: suppress the per-attempt "does not support via-in-pad"
    # ERROR log (#2880) while escalation is in flight.  The flag is read
    # by ``EscapeRouter`` via ``rules.auto_mfr_tier_in_progress`` (set by
    # the DesignRules construction sites elsewhere in this module).  We
    # explicitly clear it before the FINAL tier attempt so a fully-
    # exhausted ladder still surfaces the diagnostic on its last try.
    last_tier_idx = len(tiers_to_try) - 1

    # Issue #2881: budget-fair per-tier slicing.  Reuse the existing
    # ``_per_attempt_budgeted_timeout`` helper transparently by having the
    # inner ``route_with_layer_escalation`` call divide ``args.timeout``
    # across its own layer attempts.  Wall-clock deadline is enforced by
    # ``_deadline_expired`` checked at the top of each tier iteration.
    for tier_idx, tier_name in enumerate(tiers_to_try):
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before tier "
                    f"{tier_idx + 1} ({tier_name}); stopping tier escalation."
                )
            break

        # Convergence guard (Issue #2881): only attempt subsequent tiers
        # when the new tier offers a real capability gain over the current
        # one.  Pure same-scalar/same-capability swaps are no-ops and would
        # produce identical routing results (potentially looping forever
        # if some tier got registered twice).
        if tier_idx > 0:
            prev_tier = tiers_to_try[tier_idx - 1]
            gains_capability = can_escalate_via_in_pad(prev_tier, tier_name)
            gains_scalar = can_escalate_scalar(prev_tier, tier_name)
            # Issue #2881: trigger-aware escalation -- only walk forward
            # when the failure mode is one that escalation could fix.
            # ``missed_via_in_pad_rescues`` is the canonical signal for
            # "via-in-pad would have helped".  Without an instrumented
            # signal, we still escalate if either capability gain holds
            # (defensive: the user opted in to escalation, and a tighter
            # tier is registered in the ladder).
            triggered_by_missed_in_pad = False
            if last_router is not None:
                # Read the canonical private attribute set by
                # Autorouter._escape (see core.py:8856).
                escape_router = getattr(last_router, "_escape_router", None)
                if escape_router is not None:
                    missed = getattr(escape_router, "missed_via_in_pad_rescues", 0)
                    try:
                        triggered_by_missed_in_pad = bool(missed and int(missed) > 0)
                    except (TypeError, ValueError):
                        triggered_by_missed_in_pad = False

            # Issue #2883: consult MFR_TIER_ESCALATION_TRIGGERS on the
            # dominant failure cause from the previous tier.  When the
            # router reports a placement-class failure (BLOCKED_PATH,
            # CONGESTION, KEEPOUT, ROUTING_ORDER, UNKNOWN, ...) the
            # trigger table returns False, and we suppress escalation
            # regardless of whether the next tier offers a capability /
            # scalar gain -- escalation cannot fix a placement problem.
            #
            # When ``routing_failures`` is unavailable (e.g. in unit
            # tests that stub the inner call), ``dominant_cause`` is
            # None and we fall back to the legacy
            # ``missed_via_in_pad_rescues`` / capability-gain heuristic
            # exactly as before.
            dominant_cause = _classify_dominant_failure_cause(last_router)
            trigger_table_vetoes = False
            if dominant_cause is not None:
                # The cause is recognized AND the registry explicitly
                # excludes it from escalation -- veto.
                if dominant_cause in MFR_TIER_ESCALATION_TRIGGERS and not should_escalate_mfr_tier(
                    dominant_cause
                ):
                    trigger_table_vetoes = True

            should_escalate = False
            reason = ""
            if trigger_table_vetoes:
                # Trigger table says this failure category is not
                # manufacturer-fixable.  Suppress escalation even if
                # capability / scalar gain exists.
                reason = (
                    f"dominant failure cause ({dominant_cause.value}) is not "
                    "manufacturer-fixable (trigger table veto)"
                )
            elif gains_capability and triggered_by_missed_in_pad:
                should_escalate = True
                reason = "missed via-in-pad rescues detected on previous tier"
            elif gains_capability:
                # No instrumented missed-rescue signal, but capability gain
                # exists -- escalate defensively (the user asked for it).
                should_escalate = True
                reason = (
                    "next tier offers via-in-pad capability (no missed-rescue signal available)"
                )
            elif gains_scalar:
                should_escalate = True
                reason = "next tier offers scalar relaxation (clearance/trace/via)"

            if not should_escalate:
                if not quiet:
                    if trigger_table_vetoes:
                        flush_print(f"  Skipping tier {tier_name}: {reason}.")
                    else:
                        flush_print(
                            f"  Skipping tier {tier_name}: no capability or scalar "
                            f"gain over {prev_tier} (convergence guard)."
                        )
                break

            if not quiet:
                flush_print(f"  Escalating to {tier_name}: {reason}")

        # Mutate args to point at this tier.  Note: route_with_layer_escalation
        # re-reads args.manufacturer when constructing DesignRules, so the
        # mutation takes effect for the next inner call.
        original_mfr = args.manufacturer
        args.manufacturer = tier_name

        # Issue #2891: declare whether escalation is in flight for THIS
        # tier attempt.  All tiers except the final one are escalation-in-
        # progress (the inner ERROR is a false alarm because we will retry
        # on the next tier).  The FINAL tier is NOT escalation-in-progress:
        # if it fails, the user must see the ERROR.  The DesignRules
        # construction sites in this module read this attribute via
        # ``getattr(args, "_auto_mfr_tier_in_progress", False)`` and
        # forward it onto ``DesignRules.auto_mfr_tier_in_progress``,
        # which gates the demotion in
        # ``EscapeRouter._escape_qfp_alternating``.
        is_final_tier = tier_idx == last_tier_idx
        args._auto_mfr_tier_in_progress = not is_final_tier
        try:
            if not quiet:
                flush_print("=" * 60)
                flush_print(f"Tier {tier_idx + 1}/{len(tiers_to_try)}: {tier_name}")
                flush_print("=" * 60)

            # Dispatch to the layer-escalation path.  When args.auto_layers
            # is False we fall through to single-layer routing.  When True,
            # full 2D escalation (layers x mfr-tier) occurs.
            if getattr(args, "auto_layers", True):
                inner_rc = route_with_layer_escalation(
                    pcb_path=pcb_path,
                    output_path=output_path,
                    args=args,
                    quiet=quiet,
                )
            else:
                # Single-layer routing path -- recurse via main() with
                # auto_layers/auto_mfr_tier turned off would be cleaner, but
                # to keep this PR focused we just call the layer-escalation
                # path with --max-layers=args.layers honored via the inner
                # filter.  When the user explicitly disabled auto-layers,
                # they should explicitly invoke the appropriate path.
                inner_rc = route_with_layer_escalation(
                    pcb_path=pcb_path,
                    output_path=output_path,
                    args=args,
                    quiet=quiet,
                )

            last_exit_code = inner_rc
            final_exit_code = inner_rc

            # Read the stashed router from the inner call.  This is the
            # signal source for ``missed_via_in_pad_rescues`` -- when
            # non-zero on a failed tier, the next iteration knows to walk
            # forward (if the next tier offers via-in-pad capability).
            last_router = getattr(args, "_last_router", None)

            # Successful routing -- stop escalation.
            if inner_rc == 0:
                saw_terminating_success = True
                if not quiet:
                    flush_print(
                        f"\n  Tier {tier_name} achieved routing success; stopping tier escalation."
                    )
                # Print cost note if escalation actually moved off the
                # starting tier.
                if tier_idx > 0:
                    final_limits = get_mfr_limits(tier_name)
                    if final_limits.cost_note:
                        flush_print(
                            f"\nRecommendation: order from {tier_name}. {final_limits.cost_note}."
                        )
                break

        finally:
            # Restore original manufacturer only when we did NOT succeed --
            # on success the mutation is intentional (and surfaced via the
            # cost-note recommendation).  On failure, restore so subsequent
            # CLI calls aren't surprised.
            if last_exit_code != 0:
                args.manufacturer = original_mfr
            # Issue #2891: always clear the escalation-in-progress flag
            # on exit so callers that re-use ``args`` aren't surprised by
            # log demotion in unrelated code paths.
            args._auto_mfr_tier_in_progress = False

    # Diagnostic: name the constraint when escalation did not succeed.
    if not saw_terminating_success and not quiet:
        flush_print("\n" + "=" * 60)
        flush_print("MANUFACTURER-TIER ESCALATION SUMMARY")
        flush_print("=" * 60)
        flush_print(f"Result: No tier in {' -> '.join(tiers_to_try)} achieved routing success.")

        # Issue #2884: name the dominant unfixable constraint surfaced by
        # the last inner attempt before printing the generic remediation
        # list.  This points the user at the specific component / pin /
        # constraint that blocked progress rather than leaving them to
        # guess from a categorical 4-option menu.
        named_line = _name_dominant_unfixable_constraint(
            last_router=last_router,
            manufacturer=args.manufacturer,
        )
        if named_line:
            flush_print("\nDiagnosis:")
            flush_print(f"  {named_line}")

        # Concrete remediation options.  Always print at least three so
        # users have actionable alternatives:
        flush_print("\nOptions:")
        flush_print(
            "  1. Switch to a tighter manufacturer tier (try "
            "--mfr-tier-ladder with a custom ladder)."
        )
        flush_print(
            "  2. Change fine-pitch package(s) to a wider-pitch alternative "
            "(e.g. LQFP-48 0.5mm -> LQFP-32 0.8mm)."
        )
        flush_print(
            "  3. Move fine-pitch component(s) toward the board centre so "
            "escape traces have a wider channel (5+mm from edge)."
        )
        flush_print("  4. Add layers via --auto-layers --max-layers 6 (if not already on).")

    return final_exit_code


def _name_dominant_unfixable_constraint(
    last_router,
    manufacturer: str | None,
) -> str | None:
    """Compose the per-component diagnostic for tier-ladder exhaustion.

    Issue #2884: When ``--auto-mfr-tier`` walks the full ladder and still
    fails, surface a single human-readable line identifying *which*
    component (and, where available, which pin) carried the unfixable
    constraint -- using :func:`name_unfixable_constraint` from
    ``failure_analysis``.

    The dominant signal we have at this surface is the EscapeRouter's
    ``missed_via_in_pad_rescues`` counter and the companion
    ``missed_via_in_pad_components`` ref set.  A non-zero counter means
    one or more fine-pitch pins would have been rescued by an in-pad
    via -- the canonical PIN_ACCESS failure mode for the mfr-tier
    trigger table.

    Args:
        last_router: The Autorouter instance from the last inner attempt
            (may be ``None`` if no attempt completed cleanly).
        manufacturer: Manufacturer name at the time of the final failure.

    Returns:
        A one-line diagnostic string suitable for terminal output, or
        ``None`` when no signal is available (caller suppresses the
        Diagnosis section in that case).
    """
    from kicad_tools.router.failure_analysis import (
        FailureCause,
        name_unfixable_constraint,
    )

    if last_router is None:
        return None

    escape_router = getattr(last_router, "_escape_router", None)
    if escape_router is None:
        return None

    # Primary signal: missed via-in-pad rescues -> PIN_ACCESS failure mode.
    missed = getattr(escape_router, "missed_via_in_pad_rescues", 0) or 0
    try:
        missed_int = int(missed)
    except (TypeError, ValueError):
        missed_int = 0

    if missed_int <= 0:
        return None

    # Pick a representative component ref from the per-attempt set.
    # Sorted to keep the diagnostic deterministic across runs.
    refs = getattr(escape_router, "missed_via_in_pad_components", None) or set()
    try:
        ref_list = sorted(refs)
    except TypeError:
        ref_list = []
    component_ref = ref_list[0] if ref_list else None

    # Compose the canonical named-constraint line.  We do not yet have
    # per-pin attribution at this surface -- the EscapeRouter tracks
    # component refs only -- so we pass ``pin=None`` and let
    # name_unfixable_constraint fall back to component-level phrasing.
    base = name_unfixable_constraint(
        FailureCause.PIN_ACCESS,
        manufacturer=manufacturer,
        component_ref=component_ref,
        pin=None,
    )

    # When multiple components shared the same fault, mention the count
    # so users know whether they're chasing one outlier or a board-wide
    # problem.
    extras = len(ref_list) - 1
    if extras > 0:
        base = f"{base} ({extras} other component(s) affected by the same constraint.)"

    return base


def route_with_combined_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with combined layer and rule escalation (2D search).

    Implements a 2D search across both layer counts and design rule tiers
    to find the minimum viable configuration.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import flush_print, spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        ensure_cpp_backend_available,
        get_relaxation_tiers,
        load_pcb_for_routing,
        show_routing_summary,
    )

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=quiet,
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Issue #2674: remove stale ``<stem>_<N>layer.kicad_pcb`` siblings
    # from a previous failed-2L run before routing begins.  Without this,
    # a successful 2L run leaves the prior failed 4L/6L artifact behind
    # and the output directory shows a confusing pair of routed PCBs.
    _cleanup_stale_layer_artifacts(output_path, quiet=quiet)

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=quiet)

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (4, LayerStack.four_layer_all_signal()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Issue #2916: filter and reorder by the PCB's declared stackup.
    # Drops entries below the detected copper count (so a 4L board never
    # wastes budget on a 2L probe) and promotes the plane-aware 4L variant
    # ahead of all-signal when inner plane zones exist.
    layer_configs = _filter_layer_configs_for_pcb(
        layer_configs, pcb_path, args.max_layers, quiet=quiet
    )

    if not quiet:
        flush_print("=" * 60)
        flush_print("KiCad PCB Autorouter - Combined Escalation Mode")
        flush_print("=" * 60)
        flush_print(f"Input:          {pcb_path}")
        flush_print(f"Output:         {output_path}")
        flush_print(f"Strategy:       {args.strategy}")
        flush_print(f"Manufacturer:   {args.manufacturer}")
        flush_print(f"Max layers:     {args.max_layers}")
        flush_print(f"Min completion: {args.min_completion * 100:.0f}%")
        flush_print(f"Rule tiers:     {len(tiers)}")
        flush_print(f"Layer configs:  {[n for n, _ in layer_configs]}")
        if skip_nets:
            flush_print(f"Skip:           {', '.join(skip_nets)}")
        flush_print()
        flush_print("Search matrix:")
        flush_print("         ", end="")
        for n, _ in layer_configs:
            flush_print(f" {n}L    ", end="")
        flush_print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None
    results_matrix: dict[tuple[int, int], float] = {}  # (tier, layers) -> completion

    # Register signal handlers so SIGTERM/SIGINT save the best attempt so far
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["router"] = None
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_completed_attempt"] = False
    prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)

    # Issue #3051: Build the checkpoint callback ONCE before the 2D
    # combined-escalation matrix so every ``route_all_negotiated`` cell
    # gets best-so-far persistence (closes the iteration-0 kill-loses-
    # work hole observed in the curator audit).
    _checkpoint_cb = _make_checkpoint_callback(
        pcb_path,
        output_path,
        float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
        quiet=quiet,
    )

    # 2D search: prioritize fewer layers first, then stricter rules.
    # Issue #2823: precompute total cell count so per-attempt budget can
    # divide the remaining wall-clock budget fairly across the entire 2D
    # matrix (not just within one layer column).
    _combined_max_attempts = max(1, len(layer_configs) * len(tiers))
    for _layer_idx, (layer_count, layer_stack) in enumerate(layer_configs):
        # Issue #2802: honor the total wall-clock deadline before starting
        # another layer-stack column of the 2D search.
        if _deadline_expired(args):
            if not quiet:
                flush_print(
                    f"  Wall-clock deadline reached before {layer_count}L column; "
                    "stopping combined escalation (issue #2802)"
                )
            break

        best_completion_for_layer: float | None = None
        for _tier_idx, tier in enumerate(tiers):
            # Issue #2802: honor the deadline before each tier within the
            # current layer column.
            if _deadline_expired(args):
                if not quiet:
                    flush_print(
                        f"  Wall-clock deadline reached before {layer_count}L "
                        f"tier {tier.tier}; stopping combined escalation "
                        "(issue #2802)"
                    )
                break

            # Issue #2823: linear attempt index across the 2D matrix
            # (row-major: layers outer, tiers inner) so the per-attempt
            # budget divides the remaining wall-clock budget across all
            # remaining cells, not just the cells in this column.
            _combined_attempt_index = _layer_idx * len(tiers) + _tier_idx

            if not quiet:
                flush_print(
                    f"\nTrying: {layer_count} layers, tier {tier.tier} "
                    f"(trace={tier.trace_width:.2f}mm, clearance={tier.clearance:.2f}mm)"
                )

            # Configure design rules for this tier
            fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
            rules = DesignRules(
                grid_resolution=args.grid,
                trace_width=tier.trace_width,
                trace_clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                fine_pitch_clearance=fine_pitch_cl,
                # Issue #2695: forward manufacturer so the escape router
                # can opt in to in-pad escape for fine-pitch LQFP/QFP/SSOP/
                # TSSOP when the manufacturer supports via-in-pad processing.
                manufacturer=getattr(args, "manufacturer", None),
                # Issue #2891: forward escalation-in-progress flag.
                auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
            )

            # Load PCB
            try:
                with spinner(f"Loading PCB ({layer_count}L, tier {tier.tier})...", quiet=quiet):
                    router, net_map = load_pcb_for_routing(
                        str(pcb_path),
                        skip_nets=skip_nets,
                        rules=rules,
                        edge_clearance=args.edge_clearance,
                        layer_stack=layer_stack,
                        force_python=force_python,
                        validate_drc=not args.force,
                        strict_drc=False,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Error loading PCB: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Issue #2996: merge --net-class-map sidecar onto router's map.
            _apply_net_class_map_sidecar(router, args, quiet=quiet)

            # Issue #1841: Tell the autorouter which pour nets lack zones
            router._pour_nets_without_zones = set(_no_zone)

            # Count nets to route
            multi_pad_nets = [
                net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
            ]
            nets_to_route = len(multi_pad_nets)

            # Route
            escape_flag = _resolve_escape_routing_flag(args)

            # Issue #2823: divide the remaining wall-clock budget fairly
            # across all remaining cells of the 2D combined-escalation
            # matrix so later (higher-layer or stricter-rule) attempts
            # also get a real chance to run.  Without this, the first
            # cell (2L, tier 0) greedily consumes the entire ``--timeout``
            # and the rest of the matrix is starved.
            # Falls back to ``args.timeout`` when no deadline is configured.
            _attempt_timeout = _per_attempt_budgeted_timeout(
                args,
                attempt_index=_combined_attempt_index,
                max_attempts=_combined_max_attempts,
            )

            try:
                if _should_use_escape_routing(router, escape_flag, quiet):
                    router.route_with_escape(
                        use_negotiated=(args.strategy == "negotiated"),
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    )
                elif getattr(args, "multi_resolution", False):
                    router.route_all_multi_resolution(
                        use_negotiated=(args.strategy == "negotiated"),
                        max_iterations=args.iterations,
                        timeout=_attempt_timeout,
                    )
                elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                    router.route_all_two_phase(
                        use_negotiated=True,
                        corridor_width_factor=2.0,
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        max_iterations=getattr(args, "two_phase_iterations", None)
                        or args.iterations,
                    )
                elif args.strategy == "negotiated":
                    router.route_all_negotiated(
                        max_iterations=args.iterations,
                        timeout=_attempt_timeout,
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        batch_routing=getattr(args, "batch_routing", False)
                        or getattr(args, "high_performance", False),
                        hierarchical=getattr(args, "hierarchical", False),
                        perturbation=getattr(args, "perturbation", True),
                        # Issue #3039: forward --seed for deterministic routing.
                        seed=getattr(args, "seed", None),
                        # Issue #3051: forward checkpoint callback so kills
                        # mid-loop persist the best-so-far snapshot.
                        checkpoint_callback=_checkpoint_cb,
                    )
                elif args.strategy == "basic":
                    router.route_all()
                elif args.strategy == "monte-carlo":
                    router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                    )
                elif args.strategy == "evolutionary":
                    router.route_all_evolutionary(
                        pop_size=args.pop_size,
                        generations=args.generations,
                        verbose=args.verbose and not quiet,
                        timeout=_attempt_timeout,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Routing error: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Issue #2426: Run cleanup before computing statistics so that
            # the best-result selector compares post-cleanup connectivity.
            router.cleanup_artifacts()

            # Calculate completion — filter to multi-pad nets only (Issue #1643)
            multi_pad_net_ids = set(multi_pad_nets)
            stats = router.get_statistics(nets_to_route_ids=multi_pad_net_ids)
            nets_routed = stats["nets_routed"]
            completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0
            results_matrix[(tier.tier, layer_count)] = completion

            if not quiet:
                flush_print(f"  Routed: {nets_routed}/{nets_to_route} ({completion * 100:.0f}%)")

            # Create result
            result = RuleRelaxationResult(
                tier=tier.tier,
                trace_width=tier.trace_width,
                clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                tier_description=tier.description,
                router=router,
                net_map=net_map,
                nets_routed=nets_routed,
                nets_to_route=nets_to_route,
                completion=completion,
                success=completion >= args.min_completion,
                layer_count=layer_count,
                stats=stats,
            )

            # Track best result (Issue #2396: absolute nets_routed comparison)
            if best_result is None or _is_better_result(result, best_result):
                best_result = result
                # Update interrupt state so signal handler saves the best attempt
                _interrupt_state["router"] = result.router
                _interrupt_state["best_completed_attempt"] = True

            # Track best completion for this layer config
            if best_completion_for_layer is None or completion > best_completion_for_layer:
                best_completion_for_layer = completion

            # Check for success (first success wins - minimum config)
            if result.success:
                successful_result = result
                break

            # Early termination: skip remaining tiers when completion regresses
            # within this layer config
            if not getattr(args, "no_early_stop", False):
                if best_completion_for_layer is not None and completion < best_completion_for_layer:
                    if not quiet:
                        flush_print(
                            f"\n  Early stop: {layer_count}L tier {tier.tier} "
                            f"completion ({completion * 100:.0f}%) is worse than "
                            f"best for {layer_count}L "
                            f"({best_completion_for_layer * 100:.0f}%) — "
                            f"skipping remaining tiers for {layer_count}L"
                        )
                    break

        # If we found a successful config at this layer count, stop
        if successful_result:
            break

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)
    _interrupt_state["best_completed_attempt"] = False

    # Print results matrix
    if not quiet:
        print("\n" + "=" * 60)
        print("SEARCH MATRIX RESULTS")
        print("=" * 60)
        print("         ", end="")
        for n, _ in layer_configs:
            print(f" {n}L     ", end="")
        print()
        for tier in tiers:
            print(f"Tier {tier.tier}:  ", end="")
            for n, _ in layer_configs:
                comp = results_matrix.get((tier.tier, n), 0.0)
                if comp >= args.min_completion:
                    print(f" {comp * 100:3.0f}%✓  ", end="")
                else:
                    print(f" {comp * 100:3.0f}%   ", end="")
            print()

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("COMBINED ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Minimum viable configuration found\n"
                f"  Layers: {final_result.layer_count}\n"
                f"  Tier: {final_result.tier} ({final_result.tier_description})\n"
                f"  Completion: {final_result.completion * 100:.0f}%"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm")
            print(f"  Clearance:   {final_result.clearance:.3f}mm")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at {final_result.layer_count} layers, "
                f"tier {final_result.tier} ({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"in any configuration"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider redesigning placement for more margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = final_result.router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(
            final_result.router.grid, ignore_overflow=has_overflow
        )
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize.
        _ci_snapshot = _connectivity_snapshot(final_result.router)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if final_result.router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity before nudge.
        _ci_snapshot_nudge = _connectivity_snapshot(final_result.router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(final_result.router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            final_result.router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    _final_multi_pad_ids = {n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2}
    route_sexp, final_stats, _cleanup_stats = _finalize_routes(
        final_result.router,
        _final_multi_pad_ids,
        final_result.nets_to_route,
        quiet=quiet,
    )
    # Update result with post-cleanup stats
    final_result.nets_routed = final_stats["nets_routed"]
    final_result.completion = (
        final_result.nets_routed / final_result.nets_to_route
        if final_result.nets_to_route > 0
        else 1.0
    )
    final_result.success = final_result.completion >= args.min_completion

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        if not route_sexp and not quiet:
            print("  Warning: No routes generated!")
        # Issue #2808: atomic write via _write_routed_pcb.
        # Issue #2809: honor --output exactly; do NOT append _Nlayer suffix.
        _write_routed_pcb(
            pcb_path,
            output_path,
            route_sexp,
            layer_count=final_result.layer_count,
        )

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Fill copper-pour zones now that traces exist (issue #2516).
    if final_result.nets_routed > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped
    fix_result: int | None = None
    if not args.skip_drc and final_result.nets_routed > 0:
        drc_errors, _ = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: thread the autorouter's
            # net_class_map into the post-route DRC so the diff-pair
            # routing-continuity rule can re-derive its engagement state.
            net_class_map=getattr(final_result.router, "net_class_map", None),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(
                f"SUCCESS: Minimum viable config = {final_result.layer_count} layers + "
                f"tier {final_result.tier} rules"
            )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at {final_result.layer_count} layers, tier {final_result.tier}"
            )
            _multi_pad_ids = {
                n for n, p in final_result.router.nets.items() if n > 0 and len(p) >= 2
            }
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
                current_strategy=args.strategy,
                pcb_file=args.pcb,
                nets_to_route_ids=_multi_pad_ids,
                single_pad_count=getattr(final_result, "single_pad_count", 0),
                # Issue #2634: combined escalation includes auto-layer escalation
                # as one of its axes; suppress the redundant "Try --auto-layers"
                # recommendation.
                auto_layers_attempted=True,
            )

    if final_result.success:
        # Issue #2852: propagate --auto-fix rollback (exit 3) so callers can
        # detect a silent rollback on an otherwise-clean routing run.
        if fix_result == 3:
            return 3
        return 0
    # Partial routing: some nets were routed but not all — pipeline should continue
    if final_result.nets_routed > 0:
        return 2
    # Nothing was routed — treat as fatal failure
    return 1


def _resolve_escape_routing_flag(args) -> bool | None:
    """Resolve --escape-routing / --no-escape-routing into a tri-state value.

    Returns:
        True if escape routing is explicitly enabled,
        False if explicitly disabled,
        None for auto-detect (default).
    """
    no_escape = getattr(args, "no_escape_routing", False)
    escape = getattr(args, "escape_routing", None)

    if no_escape:
        return False
    if escape:
        return True
    return None


def _should_use_escape_routing(router, escape_flag: bool | None, quiet: bool) -> bool:
    """Determine whether to use escape routing for the current board.

    Args:
        router: The Autorouter instance.
        escape_flag: True=force on, False=force off, None=auto-detect.
        quiet: Suppress progress output.

    Returns:
        True if escape routing should be used.
    """
    if escape_flag is True:
        if not quiet:
            print("  Escape routing: enabled (--escape-routing)")
        return True
    if escape_flag is False:
        return False

    # Auto-detect dense packages
    dense_packages = router.detect_dense_packages()
    if dense_packages:
        if not quiet:
            refs = [p.ref for p in dense_packages]
            print(f"  Escape routing: auto-enabled (dense packages: {refs})")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Main entry point for route command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools route",
        description="Autoroute a KiCad PCB file",
        epilog=textwrap.dedent("""\
            exit codes:
              0  all nets routed (or meets --min-completion), DRC clean
              1  fatal failure -- no nets routed
              2  partial routing -- below --min-completion threshold
              3  routing meets threshold but DRC violations remain
              4  partial routing AND segment-segment clearance violations
              5  interrupted by SIGINT with partial results saved
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: <input>_routed.kicad_pcb)",
    )
    parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo", "evolutionary"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    parser.add_argument(
        "--skip-nets",
        help="Comma-separated nets to skip (e.g., GND,VCC,VBUS)",
    )
    parser.add_argument(
        "--grid",
        type=str,
        default="auto",
        help=(
            "Grid resolution in mm or 'auto' for automatic selection "
            "(default: auto, analyzes pad positions and clearance; "
            "use explicit value like 0.1 for dense QFP)"
        ),
    )
    parser.add_argument(
        "--grid-strategy",
        choices=["adaptive", "uniform"],
        default="adaptive",
        help=(
            "Grid strategy when --grid auto is used. "
            "'adaptive' (default) uses multi-resolution grids with fine zones "
            "around fine-pitch components. 'uniform' forces single-resolution grid."
        ),
    )
    parser.add_argument(
        "--trace-width",
        type=float,
        default=0.2,
        help="Trace width in mm (default: 0.2)",
    )
    parser.add_argument(
        "--clearance",
        type=float,
        default=0.15,
        help="Trace clearance in mm (default: 0.15)",
    )
    parser.add_argument(
        "--fine-pitch-clearance",
        type=float,
        default=None,
        help=(
            "Clearance for fine-pitch components (pitch < 0.8mm) in mm. "
            "When set, SSOP/QFP/QFN packages automatically use this reduced "
            "clearance to allow traces between pins. Example: --fine-pitch-clearance 0.08"
        ),
    )
    parser.add_argument(
        "--via-drill",
        type=float,
        default=0.3,
        help="Via drill size in mm (default: 0.3)",
    )
    parser.add_argument(
        "--via-diameter",
        type=float,
        default=0.6,
        help="Via pad diameter in mm (default: 0.6)",
    )
    parser.add_argument(
        "--mc-trials",
        type=int,
        default=10,
        help="Number of Monte Carlo trials (default: 10)",
    )
    parser.add_argument(
        "--pop-size",
        type=int,
        default=20,
        help="Evolutionary optimizer population size (default: 20)",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=10,
        help="Evolutionary optimizer generations (default: 10)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=15,
        help=(
            "Max iterations for negotiated routing (default: 15). "
            "Also applies to two-phase routing when --two-phase-iterations "
            "is not explicitly set."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Total wall-clock timeout in seconds for the whole routing "
            "invocation (default: no timeout).  This is a TOTAL budget: "
            "auto-layer escalation, placement-routing feedback, auto-fix "
            "passes, and inner negotiated/two-phase/escape calls all share "
            "the same deadline.  The command returns the best partial "
            "result available when the deadline fires (issue #2802)."
        ),
    )
    parser.add_argument(
        "--per-net-timeout",
        type=float,
        default=30.0,
        help="Wall-clock timeout in seconds for each per-net A* search (default: 30). "
        "Prevents individual nets from monopolizing the router. Use 0 to disable.",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=float,
        default=30.0,
        help=(
            "Interval in seconds between best-so-far checkpoint writes to "
            "--output during the negotiated routing loop (Issue #2808). "
            "Each checkpoint atomically replaces the file at --output so a "
            "crash/SIGTERM/--timeout leaves the user with the best partial "
            "result rather than the original unrouted input. Default: 30.0. "
            "Use 0 to disable checkpointing (only the terminal save fires)."
        ),
    )
    # Issue #2819: declare --max-search-iterations on the inner parser so the
    # forwarding shim in ``commands/routing.py`` can hand it through.  Default
    # 0 = use the historical ``cols * rows * 4`` heuristic (matches the outer
    # parser declaration at parser.py and the C++ A* backstop semantics from
    # Issue #2610).
    parser.add_argument(
        "--max-search-iterations",
        type=int,
        default=0,
        help=(
            "Override the C++ A* iteration backstop (default: 0 = use "
            "cols*rows*4, which is ~1M for a 500x500 grid). Positive values "
            "let dense boards trade memory for completeness. Iteration-cap "
            "aborts are logged distinctly from --per-net-timeout (wall-clock) "
            "aborts so you can tell which limit fired."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global ``random`` module with N before routing for "
            "reproducible results (Issue #2589). When set, escape strategies "
            "in the negotiated router (``_escape_shuffle_order``, "
            "``_escape_random_subset``, ``_escape_full_reorder``) and the "
            "MST fine-grid trial shuffle become deterministic across "
            "invocations, so two runs with identical inputs and the same "
            "--seed produce byte-identical routed output (modulo UUID lines, "
            "which are intentionally random per element). Without --seed the "
            "router uses Python's default os.urandom-derived entropy and "
            "results vary run-to-run. Note: --seed does NOT remove all "
            "sources of variance -- wall-clock escape budgets (e.g. "
            "--timeout) can still terminate early on a loaded machine; "
            "for fully reproducible CI runs combine --seed with a generous "
            "--timeout."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show routing preview with per-net details before saving (interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyze routability before routing and show diagnostic report",
    )
    parser.add_argument(
        "--bus-routing",
        action="store_true",
        help="Enable bus-aware routing (routes bus signals together)",
    )
    parser.add_argument(
        "--bus-mode",
        choices=["parallel", "stacked", "bundled"],
        default="parallel",
        help="Bus routing mode (default: parallel)",
    )
    parser.add_argument(
        "--bus-spacing",
        type=float,
        help="Spacing between bus signals in mm (default: trace_width + clearance)",
    )
    parser.add_argument(
        "--bus-min-width",
        type=int,
        default=2,
        help="Minimum signals to form a bus group (default: 2)",
    )
    parser.add_argument(
        "--differential-pairs",
        action="store_true",
        help="Enable differential pair routing (routes paired signals together)",
    )
    parser.add_argument(
        "--diffpair-spacing",
        type=float,
        help="Spacing between differential pair traces in mm (default: auto based on type)",
    )
    parser.add_argument(
        "--diffpair-max-delta",
        type=float,
        help="Maximum length mismatch for differential pairs in mm (default: auto based on type)",
    )
    parser.add_argument(
        "--length-match-diffpairs",
        action="store_true",
        help=(
            "Enable per-pair differential length-match tuning (Epic #2556 "
            "Phase 3I). Inserts serpentines on the shorter half of each "
            "length-critical diff pair until skew is within the per-class "
            "tolerance. Requires --differential-pairs to be set; emits a "
            "warning and short-circuits otherwise."
        ),
    )
    parser.add_argument(
        "--length-match-groups",
        action="store_true",
        help=(
            "Enable N-trace match-group length-match tuning (Epic #2661 "
            "Phase 3H). Detects parallel-bus groups (DDR, MIPI, HDMI TMDS) "
            "declared via NetClassRouting.length_match_group, then inserts "
            "serpentines on shorter group members until the per-group skew "
            "is within tolerance. Compatible with --length-match-diffpairs; "
            "groups whose members are diff pairs (MIPI/HDMI lane groups) "
            "engage the Phase 2F symmetric-serpentine path."
        ),
    )
    parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (Issue #2996).  Merged into the autorouter's "
            "name-pattern-classified net_class_map so per-pair / per-group "
            "fields (intra_pair_clearance, coupled_routing, "
            "coupled_continuity_threshold, target_diff_impedance, "
            "length_match_group) project through to the routing-time "
            "pathfinder.  Mirrors the kct check --net-class-map flag."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )
    parser.add_argument(
        "--perturbation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable stochastic cost perturbation to escape local minima "
            "(default: enabled). Use --no-perturbation to disable."
        ),
    )
    parser.add_argument(
        "--power-nets",
        help=(
            "Generate copper zones for power nets: 'NET1:LAYER1,NET2:LAYER2,...' "
            "(e.g., 'GND:B.Cu,+3.3V:F.Cu')"
        ),
    )
    parser.add_argument(
        "--edge-clearance",
        type=float,
        help=(
            "Copper-to-edge clearance in mm. Blocks routing within this distance "
            "of the board edge. Common values: 0.25-0.5mm (default: no clearance)"
        ),
    )
    parser.add_argument(
        "--layers",
        choices=["auto", "2", "4", "4-sig", "4-all", "6"],
        default="auto",
        help=(
            "Layer stack configuration for routing: "
            "'auto' = auto-detect from PCB file (default); "
            "'2' = 2-layer (F.Cu, B.Cu); "
            "'4' = 4-layer with GND/PWR planes (F.Cu, In1=GND, In2=PWR, B.Cu); "
            "'4-sig' = 4-layer with 2 signal layers (F.Cu, In1=signal, In2=GND, B.Cu); "
            "'4-all' = 4-layer with all 4 signal layers (no planes); "
            "'6' = 6-layer with 4 signal layers. "
            "Auto-detection parses the PCB's layer definitions and zones to "
            "determine the appropriate layer stack."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force routing even when grid resolution exceeds clearance. "
            "Without this flag, routing will fail if grid > clearance to "
            "prevent DRC violations. Use with caution."
        ),
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "Enable profiling to measure performance. Outputs a cProfile "
            "stats file that can be analyzed with pstats or visualization tools."
        ),
    )
    parser.add_argument(
        "--profile-output",
        metavar="FILE",
        help=(
            "Output file for profile data (default: route_profile.prof). "
            "Analyze with: python -m pstats route_profile.prof, or "
            "visualize with: snakeviz route_profile.prof"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "cpp", "python"],
        default="auto",
        help=(
            "Router backend to use: "
            "'auto' = use C++ if available, fall back to Python (default); "
            "'cpp' = require C++ backend (fails if not available); "
            "'python' = force Python backend (for testing/debugging). "
            "C++ backend provides 10-100x speedup for fine-grid routing."
        ),
    )
    parser.add_argument(
        "--no-auto-build-native",
        action="store_true",
        help=(
            "Disable silent auto-build of the C++ router extension on first use "
            "(Issue #2549). When --backend is 'auto' (the default) and the "
            "compiled router_cpp.*.so is missing, kct route normally invokes "
            "'kct build-native' once and uses C++ for the rest of the session. "
            "Pass this flag (or set KICAD_TOOLS_NO_AUTO_BUILD=1) to skip the "
            "build attempt and fall straight through to pure Python."
        ),
    )
    parser.add_argument(
        "--skip-drc",
        action="store_true",
        help=(
            "Skip post-routing DRC validation. By default, the router runs "
            "a DRC check after routing and warns about violations. Use this "
            "flag for performance-critical use or when running separate validation."
        ),
    )
    parser.add_argument(
        "--manufacturer",
        "--mfr",
        default="jlcpcb",
        help=(
            "Manufacturer profile for DRC validation (default: jlcpcb). "
            "Determines minimum clearances, trace widths, and other design rules."
        ),
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help=(
            "Automatically run 'kct fix-drc' after routing if DRC violations are "
            "detected. Suppressed by --dry-run and --skip-drc. Uses iterative "
            "repair to fix clearance and drill violations."
        ),
    )
    parser.add_argument(
        "--auto-fix-passes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of repair passes for --auto-fix (default: 3). "
            "Implies --auto-fix. Multiple passes can fix cascading violations "
            "where fixing one violation exposes or resolves others."
        ),
    )
    # Issue #2595: placement-feedback opt-in flags.  When the initial
    # routing pass leaves nets unrouted with BLOCKED_BY_COMPONENT root
    # cause, --placement-feedback invokes the existing closed-loop
    # placement adjuster (Autorouter.route_with_placement_feedback) to
    # nudge non-anchored components and re-route.  Connectors (J*) and
    # locked footprints are anchored automatically.
    parser.add_argument(
        "--placement-feedback",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "After the initial routing pass, if any nets failed with "
            "BLOCKED_BY_COMPONENT root cause, invoke the placement-routing "
            "feedback loop to nudge non-anchored components and re-route "
            "(default: disabled).  Use --no-placement-feedback to disable "
            "explicitly.  Connectors (refs starting with 'J' or 'P') and "
            "footprints with the KiCad 'locked' attribute are never moved "
            "(see --placement-feedback-anchor / --placement-feedback-no-anchor "
            "for overrides).  Issue #2595."
        ),
    )
    parser.add_argument(
        "--placement-feedback-budget",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Maximum number of placement adjustments to attempt when "
            "--placement-feedback is set (default: 3). Each adjustment "
            "moves one or more components and re-routes from scratch."
        ),
    )
    parser.add_argument(
        "--placement-feedback-max-movement",
        type=float,
        default=5.0,
        metavar="MM",
        help=(
            "Hard cap on per-component movement distance for the placement "
            "feedback loop, in mm (default: 5.0). Strategies that would "
            "move any component by more than this distance are filtered out."
        ),
    )
    parser.add_argument(
        "--placement-feedback-anchor",
        default=None,
        metavar="REFS",
        help=(
            "Additional component references to anchor (never move) during "
            "the placement feedback loop, comma-separated. Combined with the "
            "auto-detected anchors (connectors, locked footprints). "
            "Example: --placement-feedback-anchor U5,U7"
        ),
    )
    parser.add_argument(
        "--placement-feedback-no-anchor",
        default=None,
        metavar="REFS",
        help=(
            "Component references to remove from the anchor set, "
            "comma-separated. Use this to allow movement of components that "
            "would otherwise be auto-anchored (e.g. a non-mechanical "
            "connector). Example: --placement-feedback-no-anchor J3"
        ),
    )
    # Issue #2606: stagnation + outer-timeout guards on the
    # PlacementFeedbackLoop.  Mirror parser.py:2349-2376 so the
    # top-level forwarder in commands/routing.py can pass these
    # through without tripping argparse.  Defaults match parser.py.
    # Issue #2620: previously missing on the inner parser, causing
    # "unrecognized arguments" when the forwarder injected them.
    parser.add_argument(
        "--placement-feedback-stagnation-patience",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Number of consecutive outer placement-feedback iterations with "
            "no fully-routed-net-count improvement before the loop exits "
            "early with exit_reason=pf_stagnated. Default 3. Set to 0 to "
            "disable stagnation detection. Issue #2606."
        ),
    )
    parser.add_argument(
        "--placement-feedback-outer-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Hard wall-clock budget for the entire outer placement-feedback "
            "loop, in seconds. When exceeded between iterations the loop "
            "exits with exit_reason=pf_timeout. Default: no outer cap "
            "(only the per-iteration --timeout applies). Issue #2606."
        ),
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help=(
            "Skip trace optimization after routing. By default, traces are "
            "optimized to merge collinear segments, eliminate zigzags, and "
            "convert corners to 45 degrees. Use this flag to keep raw "
            "grid-step segments for debugging."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        dest="no_optimize",
        help="Alias for --no-optimize (keep raw grid-step segments for debugging)",
    )
    parser.add_argument(
        "--auto-pour",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically create copper pour zones for power-classified "
            "nets (GND, VCC, etc.) when the input PCB has none "
            "(default: enabled). Use --no-auto-pour to disable."
        ),
    )
    parser.add_argument(
        "--auto-layers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Automatically escalate layer count on routing failure "
            "(default: enabled). Tries 2 → 4 → 6 layers until routing "
            "succeeds or --max-layers is reached. Reports minimum viable "
            "layer count for the design. The first attempt is still "
            "2-layer, so 2-layer-solvable boards pay no extra cost. "
            "Use --no-auto-layers to disable and route at a fixed layer "
            "count (whatever --layers specifies, or auto-detected)."
        ),
    )
    parser.add_argument(
        "--max-layers",
        type=int,
        default=6,
        choices=[2, 4, 6],
        help=(
            "Maximum layer count for auto-escalation (default: 6). Only used with --auto-layers."
        ),
    )
    # Issue #2881: --auto-mfr-tier / --mfr-tier-ladder.  Opt-in escalation
    # along a registered ladder of manufacturer tiers (e.g.
    # ``jlcpcb`` -> ``jlcpcb-tier1``).  Default off because tighter tiers
    # have higher cost; users explicitly opt in to allow the surcharge.
    parser.add_argument(
        "--auto-mfr-tier",
        action="store_true",
        default=False,
        help=(
            "Automatically escalate to a tighter manufacturer tier when "
            "geometric infeasibility blocks routing on the current tier "
            "(default: disabled). Walks the registered ladder for the "
            "current --mfr (e.g. jlcpcb -> jlcpcb-tier1, which adds via-in-pad "
            "capability for fine-pitch QFP escape). Opt-in because tighter "
            "tiers can incur a manufacturing surcharge."
        ),
    )
    parser.add_argument(
        "--mfr-tier-ladder",
        type=str,
        default=None,
        help=(
            "Explicit comma-separated manufacturer tier ladder for "
            "--auto-mfr-tier (e.g. 'jlcpcb,jlcpcb-tier1'). Overrides the "
            "default ladder registered for the current --mfr. Each entry "
            "must be a recognized manufacturer name."
        ),
    )
    parser.add_argument(
        "--min-completion",
        type=float,
        default=0.95,
        help=(
            "Minimum routing completion rate for success (default: 0.95 = 95%%). "
            "Controls the exit code threshold: routing above this rate returns "
            "exit code 0 (success), below returns exit code 2 (partial). "
            "Also used with --auto-layers to control layer escalation. "
            "If no layer count achieves this, the best result is saved."
        ),
    )
    parser.add_argument(
        "--adaptive-rules",
        action="store_true",
        help=(
            "Automatically relax design rules on routing failure. "
            "Tries progressively relaxed trace widths and clearances "
            "until routing succeeds or manufacturer limits are reached. "
            "Reports which rules were relaxed and warns if minimum tolerances used."
        ),
    )
    parser.add_argument(
        "--no-early-stop",
        action="store_true",
        help=(
            "Disable early termination of adaptive-rules tier search. "
            "By default, if a tier routes fewer nets than the best prior "
            "attempt, remaining tiers are skipped. Use this flag to force "
            "all tiers to run (useful for debugging or benchmarking)."
        ),
    )
    parser.add_argument(
        "--min-trace",
        type=float,
        help=(
            "Minimum trace width floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--min-clearance-floor",
        type=float,
        help=(
            "Minimum clearance floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--progressive-clearance",
        action="store_true",
        help=(
            "Enable progressive clearance relaxation for failed nets. "
            "Routes all nets with standard clearance first, then retries "
            "failed nets with progressively relaxed clearance (up to --min-clearance). "
            "Unlike --adaptive-rules which globally relaxes all rules, this only "
            "relaxes clearance for specific failed nets. Reports which nets needed "
            "relaxation and the clearance used."
        ),
    )
    parser.add_argument(
        "--min-clearance",
        type=float,
        help=(
            "Minimum clearance for progressive relaxation (mm). "
            "Used with --progressive-clearance to set the floor for relaxation. "
            "Default: 50%% of --clearance value."
        ),
    )
    parser.add_argument(
        "--relaxation-levels",
        type=int,
        default=3,
        help=(
            "Number of progressive relaxation levels (default: 3). "
            "More levels = finer-grained relaxation steps."
        ),
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help=(
            "Show detailed routing failure diagnostics. For each failed net, "
            "reports the specific failure reason, blocking obstacles, coordinates, "
            "and actionable suggestions. Failures are grouped by cause for analysis."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format for routing diagnostics: "
            "'text' = human-readable output (default); "
            "'json' = JSON output for tooling and automation."
        ),
    )
    parser.add_argument(
        "--high-performance",
        action="store_true",
        help=(
            "Use high-performance mode with aggressive parallelization and more trials. "
            "Uses calibrated settings if available (run 'kicad-tools calibrate' first)."
        ),
    )
    parser.add_argument(
        "--hierarchical",
        action="store_true",
        help=(
            "Enable hierarchical coarse-to-fine routing mode. "
            "First performs global routing on a coarse grid (4x resolution) "
            "to establish corridors, then refines with the fine grid only "
            "near pads and congestion points. Can significantly speed up "
            "fine-grid routing (0.05mm-0.1mm) on large boards."
        ),
    )
    parser.add_argument(
        "--multi-resolution",
        action="store_true",
        help=(
            "Enable multi-resolution routing with fine-grid fallback. "
            "First routes all nets on the coarse grid, then retries failed "
            "nets on a finer grid (2x resolution) scoped to their bounding "
            "boxes. Useful for boards where some nets fail due to grid "
            "resolution limitations."
        ),
    )
    parser.add_argument(
        "--escape-routing",
        action="store_true",
        default=None,
        help=(
            "Enable escape routing phase before global routing. "
            "Generates escape routes for dense QFP/QFN/BGA packages "
            "where pin pitch is too small for traces to pass between "
            "adjacent pins. Without this flag, escape routing is "
            "auto-detected based on package density."
        ),
    )
    parser.add_argument(
        "--no-escape-routing",
        action="store_true",
        help=(
            "Disable automatic escape routing detection. By default, "
            "the router auto-detects dense packages and enables escape "
            "routing when needed. Use this flag to skip escape routing "
            "even when dense packages are present."
        ),
    )
    parser.add_argument(
        "--two-phase",
        action="store_true",
        help=(
            "Use two-phase global+detailed routing. Phase 1 allocates "
            "coarse corridors on a tile graph; Phase 2 routes within those "
            "corridors using negotiated congestion. Produces dramatically "
            "better results on complex multi-layer boards by preventing "
            "overflow divergence. When combined with escape routing, "
            "replaces the negotiated rip-up phase after escape generation."
        ),
    )
    parser.add_argument(
        "--two-phase-iterations",
        type=int,
        default=None,
        help=(
            "Max rip-up-and-reroute iterations for the Phase 2 detailed "
            "negotiated routing loop in two-phase mode. Overrides --iterations "
            "for the two-phase path when both are given. If omitted, falls back "
            "to --iterations (default: 20 when neither flag is set). "
            "Only effective with --two-phase."
        ),
    )
    parser.add_argument(
        "--batch-routing",
        action="store_true",
        help=(
            "Enable GPU-accelerated batch routing for parallel net processing. "
            "Routes multiple independent nets simultaneously using GPU compute. "
            "Best results with 4+ independent nets and Metal/CUDA GPU. "
            "Enabled automatically in high-performance mode."
        ),
    )

    # Power plane stitching
    parser.add_argument(
        "--stitch-power-planes",
        action="store_true",
        help=(
            "Automatically add stitching vias for power planes after routing. "
            "Connects surface-mount component pads to their power plane layers. "
            "Equivalent to running 'kicad-pcb-stitch' after routing."
        ),
    )

    # Export failed nets
    parser.add_argument(
        "--export-failed-nets",
        metavar="PATH",
        help=(
            "Export failed (unrouted) net names to a file, one per line. "
            "Useful for scripted workflows or manual completion in KiCad."
        ),
    )

    # Cache arguments
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable routing cache (force fresh routing)",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Only use cached results (fail if cache miss)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear routing cache before routing",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Show routing cache statistics and exit",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail with exit code 6 if connectivity is reduced by the optimize "
            "or DRC nudge pipeline phases (issue #2596), or if output "
            "connectivity verification detects any disconnected net. Without "
            "this flag, regressions are reverted (optimize/nudge) or reported "
            "as warnings (output verification) but do not affect the exit code."
        ),
    )
    parser.add_argument(
        "--strict-in-pad-clearance",
        action="store_true",
        dest="strict_in_pad_clearance",
        help=(
            "Issue #3033 / #3062: refuse to commit an in-pad rescue via that "
            "would clip a neighbouring foreign-net pad on a fine-pitch "
            "QFP/SSOP (the 'proceed anyway, defer DRC to the user' branch "
            "from PR #2945).  Sets KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE=1 so "
            "the lazily-constructed EscapeRouter inside the negotiated/two-"
            "phase pipelines picks up the flag without each call site needing "
            "an explicit pass-through.  Default off preserves the legacy "
            "bit-for-bit behaviour."
        ),
    )
    parser.add_argument(
        "--show-congestion",
        action="store_true",
        help=(
            "Show pre-route RUDY congestion estimation before routing begins. "
            "Displays an ASCII heatmap of predicted congestion per tile, useful "
            "for diagnosing routing failures caused by congestion hotspots."
        ),
    )

    args = parser.parse_args(argv)

    # Issue #3033 / #3062: When --strict-in-pad-clearance is set, stamp the
    # env var so EscapeRouter (lazily constructed several layers below the
    # CLI) reads the same opt-in state.  See escape.py's __init__ for the
    # env-var read site.  Defaults to "0" so absence preserves the legacy
    # "proceed anyway" behaviour bit-for-bit.
    import os as _os

    if getattr(args, "strict_in_pad_clearance", False):
        _os.environ["KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE"] = "1"

    # Issue #2802: Stamp a single monotonic wall-clock deadline derived from
    # ``--timeout`` onto ``args`` so every orchestration site (layer-escalation
    # loop, rule-relaxation tiers, combined-escalation 2D search, placement
    # feedback, auto-fix passes, inner negotiated/two-phase/escape calls)
    # shares the same budget rather than receiving a fresh per-stage copy of
    # ``args.timeout``.  See ``_set_wall_clock_deadline`` / ``_remaining_budget``
    # / ``_deadline_expired`` for the helpers that consume it.
    _set_wall_clock_deadline(args)

    # Issue #2589: Seed the global ``random`` module for reproducible runs.
    # When ``--seed`` is supplied, all unseeded ``random.shuffle`` /
    # ``random.sample`` callsites in the negotiated router
    # (``algorithms/negotiated.py`` escape strategies, ``core.py`` MST trial
    # shuffle) and any other unseeded global-random consumers downstream
    # become deterministic.  This is the primary fix for board-03 run-to-run
    # variance.  When ``--seed`` is omitted, the global RNG is left at its
    # default os.urandom-derived state -- existing behaviour is preserved.
    if args.seed is not None:
        random.seed(args.seed)
        if not getattr(args, "quiet", False):
            print(f"[seed] Seeded global random with --seed {args.seed}")

    # Resolve two-phase iteration count.
    # Priority: --two-phase-iterations (explicit) > --iterations (explicit) > 20 (default)
    _TWO_PHASE_DEFAULT = 20
    _two_phase_iters_explicit = getattr(args, "two_phase_iterations", None) is not None
    _iterations_explicitly_set = args.iterations != parser.get_default("iterations")
    if not _two_phase_iters_explicit:
        if _iterations_explicitly_set:
            args.two_phase_iterations = args.iterations
        else:
            args.two_phase_iterations = _TWO_PHASE_DEFAULT

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Warning: Expected .kicad_pcb file, got {pcb_path.suffix}")

    # Issue #2996: Validate and load the optional --net-class-map sidecar
    # early -- before dispatching to any of the route_with_* sub-flows --
    # so the error paths (missing file / malformed JSON / invalid structure)
    # short-circuit with exit 1 and a clear message regardless of which
    # routing path the args select.  The loaded map is stashed on
    # ``args._loaded_net_class_map`` for downstream consumers (each
    # ``load_pcb_for_routing`` site merges it into ``router.net_class_map``).
    args._loaded_net_class_map = None
    if getattr(args, "net_class_map", None) is not None:
        import json as _ncm_json

        from kicad_tools.router.rules import net_class_map_from_dict

        ncm_path = Path(args.net_class_map).resolve()
        if not ncm_path.exists():
            print(f"Error: net-class-map file not found: {ncm_path}", file=sys.stderr)
            return 1
        try:
            _ncm_data = _ncm_json.loads(ncm_path.read_text())
        except _ncm_json.JSONDecodeError as e:
            print(f"Error parsing net-class-map JSON: {e}", file=sys.stderr)
            return 1
        try:
            args._loaded_net_class_map = net_class_map_from_dict(_ncm_data)
        except (TypeError, ValueError) as e:
            print(f"Error: invalid net-class-map structure: {e}", file=sys.stderr)
            return 1

    # Normalize --auto-fix-passes: explicit value implies --auto-fix
    if args.auto_fix_passes is not None:
        if args.auto_fix_passes < 1:
            print("Error: --auto-fix-passes must be at least 1", file=sys.stderr)
            return 1
        args.auto_fix = True
    else:
        # Default to 3 passes when --auto-fix is used without explicit --auto-fix-passes
        args.auto_fix_passes = 3

    # Issue #2388: --auto-layers is now enabled by default.  When --layers
    # is explicitly set, silently disable auto-escalation so existing
    # users of --layers see no behavior change.  Only raise an error if
    # the user *explicitly* typed both --auto-layers and --layers
    # (a true conflict in intent).
    _argv_for_detect = argv if argv is not None else sys.argv
    explicit_auto_layers = "--auto-layers" in _argv_for_detect
    if args.auto_layers and args.layers != "auto":
        if explicit_auto_layers:
            print(
                f"Error: --auto-layers cannot be used with --layers {args.layers}.\n"
                "Use --auto-layers alone, or use --layers to specify a "
                "fixed layer count (and pass --no-auto-layers to silence "
                "this error).",
                file=sys.stderr,
            )
            return 1
        # --layers was explicit but --auto-layers was the default; honor --layers.
        args.auto_layers = False

    # Validate --adaptive-rules is not used with explicit --layers (unless also using --auto-layers)
    if args.adaptive_rules and args.layers != "auto" and not args.auto_layers:
        print(
            f"Error: --adaptive-rules cannot be used with --layers {args.layers}.\n"
            "Use --adaptive-rules alone, with --auto-layers, or use --layers for fixed config.",
            file=sys.stderr,
        )
        return 1

    # Validate min-completion is between 0 and 1
    if args.min_completion < 0 or args.min_completion > 1:
        print(
            f"Error: --min-completion must be between 0 and 1 (got {args.min_completion}).",
            file=sys.stderr,
        )
        return 1

    # Apply high-performance settings if requested
    if getattr(args, "high_performance", False):
        from kicad_tools.performance import get_performance_config

        perf_config = get_performance_config(high_performance=True)

        # Override defaults with high-performance settings
        if not args.quiet:
            print("\n--- High-Performance Mode ---")
            print(f"  CPU cores:         {perf_config.cpu_cores}")
            print(f"  Monte Carlo trials: {perf_config.monte_carlo_trials}")
            print(f"  Parallel workers:   {perf_config.parallel_workers}")
            print(f"  Max iterations:     {perf_config.negotiated_iterations}")
            if perf_config.calibrated:
                print(f"  (Using calibrated settings from {perf_config.calibration_date})")
            print()

        # Apply to routing parameters
        args.mc_trials = perf_config.monte_carlo_trials
        args.iterations = perf_config.negotiated_iterations
        # Also apply calibrated iterations to two-phase if not explicitly set
        if not _two_phase_iters_explicit:
            args.two_phase_iterations = perf_config.negotiated_iterations

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")

    # Resolve grid value: "auto" or numeric
    # We need to resolve this early, before sub-functions are called
    grid_auto_result = None
    multi_res_plan = None
    if args.grid.lower() == "auto":
        from kicad_tools.router.io import (
            auto_select_grid_resolution,
            compute_multi_resolution_plan,
            extract_board_dimensions,
            extract_pad_positions,
        )

        if not args.quiet:
            print("\n--- Auto-selecting grid resolution ---")
        pad_positions = extract_pad_positions(pcb_path)
        board_dims = extract_board_dimensions(pcb_path)
        board_width = board_dims[0] if board_dims else None
        board_height = board_dims[1] if board_dims else None
        grid_auto_result = auto_select_grid_resolution(
            pads=pad_positions,
            clearance=args.clearance,
            board_width=board_width,
            board_height=board_height,
        )

        # When grid_strategy is adaptive (default), attempt multi-resolution
        grid_strategy = getattr(args, "grid_strategy", "adaptive")
        if grid_strategy == "adaptive":
            # compute_multi_resolution_plan needs full Pad objects (with ref)
            # Try loading them; fall back to uniform if not available
            try:
                from kicad_tools.router.io import load_pads_for_analysis

                full_pads = load_pads_for_analysis(pcb_path)
                multi_res_plan = compute_multi_resolution_plan(
                    pads=full_pads,
                    clearance=args.clearance,
                    board_width=board_width,
                    board_height=board_height,
                )
            except Exception:
                # Fall back: try with pad positions (won't have ref info)
                multi_res_plan = compute_multi_resolution_plan(
                    pads=pad_positions,
                    clearance=args.clearance,
                    board_width=board_width,
                    board_height=board_height,
                )

        if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
            # Use coarse resolution for the global grid
            args.grid = multi_res_plan.coarse_resolution
            if not args.quiet:
                print(multi_res_plan.summary())
                print()
        else:
            # No fine-pitch components or uniform strategy requested
            multi_res_plan = None
            args.grid = grid_auto_result.resolution
            if not args.quiet:
                print(grid_auto_result.summary())
                print()

        # Store grid origin offset from auto-selection for DesignRules
        args._grid_origin_offset = grid_auto_result.origin_offset
    else:
        try:
            args.grid = float(args.grid)
        except ValueError:
            print(
                f"Error: Invalid grid value '{args.grid}'. Use a number (e.g., 0.25) or 'auto'.",
                file=sys.stderr,
            )
            return 1

    # Handle cache-related commands early
    if args.cache_stats:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        stats = cache.stats()
        print("\n--- Routing Cache Statistics ---")
        print(f"  Cache directory:     {stats['cache_dir']}")
        print(f"  Routing results:     {stats['routing_results_count']}")
        print(f"  Partial net routes:  {stats['partial_routes_count']}")
        print(f"  Total size:          {stats['total_size_mb']:.2f} MB")
        print(f"  Valid results:       {stats['valid_results']}")
        print(f"  Expired results:     {stats['expired_results']}")
        print(f"  TTL:                 {stats['ttl_days']} days")
        print(f"  Max size:            {stats['max_size_mb']:.0f} MB")
        if stats["oldest"]:
            print(f"  Oldest entry:        {stats['oldest']}")
        if stats["newest"]:
            print(f"  Newest entry:        {stats['newest']}")
        return 0

    if args.clear_cache:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        count = cache.clear()
        if not args.quiet:
            print(f"Cleared {count} entries from routing cache")

    # Auto-apply edge clearance from manufacturer when not explicitly set.
    # This ensures --manufacturer jlcpcb automatically enforces the 0.3mm
    # copper-to-edge clearance without requiring a separate --edge-clearance flag.
    if args.edge_clearance is None:
        from kicad_tools.router.mfr_limits import get_mfr_limits

        try:
            _mfr = get_mfr_limits(args.manufacturer)
            if _mfr.min_edge_clearance > 0:
                args.edge_clearance = _mfr.min_edge_clearance
                if not args.quiet:
                    print(
                        f"Edge clearance: {args.edge_clearance}mm "
                        f"(from {args.manufacturer} manufacturer limits)"
                    )
        except ValueError:
            pass  # Unknown manufacturer -- edge_clearance stays None

    # Issue #2881: --auto-mfr-tier wraps --auto-layers / --adaptive-rules,
    # iterating over registered manufacturer tiers from cheapest -> tightest.
    # Inside each tier the routing dispatches to the existing layer-escalation
    # path (or the single-layer path when --no-auto-layers is set).
    if getattr(args, "auto_mfr_tier", False):
        return route_with_mfr_tier_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Handle auto-layers mode (separate code path)
    if args.auto_layers and args.adaptive_rules:
        # Combined 2D search: layers + rules
        return route_with_combined_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.auto_layers:
        return route_with_layer_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.adaptive_rules:
        # Adaptive rules only (fixed layer count)
        return route_with_rule_relaxation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Auto-create copper pours for power nets (before skip detection).
    # auto_pour_if_missing writes in-place; stage a copy at output_path
    # first so the user's INPUT is left untouched (issue #2548).
    if getattr(args, "auto_pour", True):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _stage_input_for_auto_pour(pcb_path, output_path)
        auto_pour_if_missing(
            pcb_path,
            quiet=args.quiet,
            edge_clearance=getattr(args, "edge_clearance", None),
        )

    # Auto-classify pour nets and extend skip_nets
    _skipped, _no_zone = _auto_skip_pour_nets(pcb_path, skip_nets, quiet=args.quiet)

    # Import router modules
    from kicad_tools.analysis import ComplexityAnalyzer, ComplexityRating
    from kicad_tools.router import (
        BusRoutingConfig,
        BusRoutingMode,
        DesignRules,
        DifferentialPairConfig,
        LayerStack,
        RoutabilityAnalyzer,
        ensure_cpp_backend_available,
        load_pcb_for_routing,
        print_routing_diagnostics_json,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.schema.pcb import PCB

    # Handle backend selection (auto-build C++ extension on first use; #2549)
    ok, force_python, exit_code = ensure_cpp_backend_available(
        backend=args.backend,
        quiet=getattr(args, "quiet", False),
        allow_auto_build=not getattr(args, "no_auto_build_native", False),
    )
    if not ok:
        return exit_code if exit_code is not None else 1

    # Grid resolution already resolved early in main()
    # (args.grid is now a float, grid_auto_result set if "auto" was used)

    # Validate grid resolution vs clearance (prevents DRC violations)
    # Skip validation for auto mode since auto_select_grid_resolution ensures DRC compliance
    if grid_auto_result is None and args.grid > args.clearance:
        recommended_grid = args.clearance / 2
        if not args.force:
            print(
                f"Error: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"This WILL cause DRC violations.\n\n"
                f"Options:\n"
                f"  1. Use a finer grid: --grid {recommended_grid}\n"
                f"  2. Use --grid auto for automatic selection\n"
                f"  3. Use --force to override (not recommended)\n",
                file=sys.stderr,
            )
            return 1
        else:
            # User forced, continue with warning
            print(
                f"Warning: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"Proceeding anyway due to --force flag. Expect DRC violations.",
                file=sys.stderr,
            )

    # Create layer stack from --layers argument (or auto-detect)
    if args.layers == "auto":
        # Auto-detect layer stack from PCB file
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "4-all": LayerStack.four_layer_all_signal(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

    # Configure design rules
    grid_origin_offset = getattr(args, "_grid_origin_offset", (0.0, 0.0))
    fine_pitch_cl = getattr(args, "fine_pitch_clearance", None)
    rules = DesignRules(
        grid_resolution=args.grid,
        grid_origin_offset=grid_origin_offset,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
        fine_pitch_clearance=fine_pitch_cl,
        # Issue #2605: forward manufacturer so the escape router can opt in
        # to in-pad escape for fine-pitch SSOP/TSSOP when the manufacturer
        # supports via-in-pad processing.
        manufacturer=getattr(args, "manufacturer", None),
        # Issue #2891: forward escalation-in-progress flag so the escape
        # router demotes the #2880 ERROR log when an outer wrapper will
        # retry on a tier that supports via-in-pad.
        auto_mfr_tier_in_progress=getattr(args, "_auto_mfr_tier_in_progress", False),
    )

    # Import progress helpers
    from kicad_tools.cli.progress import flush_print, spinner

    quiet = args.quiet

    # Print header (unless quiet)
    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter")
        print("=" * 60)
        print(f"Input:    {pcb_path}")
        print(f"Output:   {output_path}")
        print(f"Strategy: {args.strategy}")
        print(f"Layers:   {layer_stack.name} ({layer_stack.num_layers} layers)")
        if skip_nets:
            print(f"Skip:     {', '.join(skip_nets)}")
        if args.bus_routing:
            print(f"Bus:      enabled ({args.bus_mode} mode)")
        if args.differential_pairs:
            print("DiffPair: enabled")

        if args.edge_clearance:
            print(f"Edge:     {args.edge_clearance}mm clearance")
        if args.verbose:
            print("\nDesign Rules:")
            grid_mode = " (auto)" if grid_auto_result else ""
            print(f"  Grid resolution: {rules.grid_resolution}mm{grid_mode}")
            print(f"  Trace width:     {rules.trace_width}mm")
            print(f"  Clearance:       {rules.trace_clearance}mm")
            print(f"  Via drill:       {rules.via_drill}mm")
            print(f"  Via diameter:    {rules.via_diameter}mm")
            if args.edge_clearance:
                print(f"  Edge clearance:  {args.edge_clearance}mm")

            print(f"\nLayer Stack ({layer_stack.name}):")
            signal_layers = [lyr.name for lyr in layer_stack.signal_layers]
            plane_layers = [f"{lyr.name} ({lyr.plane_net})" for lyr in layer_stack.plane_layers]
            print(f"  Signal layers:  {', '.join(signal_layers)}")
            if plane_layers:
                print(f"  Plane layers:   {', '.join(plane_layers)}")

    # Load PCB
    if not quiet:
        flush_print("\n--- Loading PCB ---")
    try:
        with spinner("Loading PCB...", quiet=quiet):
            router, net_map = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=skip_nets,
                rules=rules,
                edge_clearance=args.edge_clearance,
                layer_stack=layer_stack,
                force_python=force_python,
                validate_drc=not args.force,
                strict_drc=False,  # Only fail on hard constraint (grid > clearance)
                # Issue #2610: thread --max-search-iterations through.
                # The inner parser declares this flag with default=0 (Issue
                # #2819), so the attribute is guaranteed to exist; the
                # ``or 0`` guards against an explicit ``--max-search-iterations 0``
                # being treated as falsy (which is the intended behaviour:
                # 0 means "use the cols*rows*4 heuristic").
                max_search_iterations=args.max_search_iterations or 0,
            )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Issue #2996: merge --net-class-map sidecar onto router's map.
    _apply_net_class_map_sidecar(router, args, quiet=quiet)

    # Pass fine zones from multi-resolution plan to the router (Issue #1828).
    # This enables SubGridRouter to use fine-grid resolution for escape
    # routing of pads within dense IC packages (e.g. SSOP at 0.05mm)
    # instead of the coarse global grid (e.g. 0.17mm).
    if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
        router.fine_zones = list(multi_res_plan.fine_zones)
        if not quiet:
            flush_print(f"  Fine zones: {len(router.fine_zones)} (sub-grid escape routing enabled)")

    # Issue #1841: Tell the autorouter which pour nets lack zones
    router._pour_nets_without_zones = set(_no_zone)

    # Set up Ctrl+C handling to save partial results
    _interrupt_state["router"] = router
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["interrupted"] = False
    signal.signal(signal.SIGINT, _handle_interrupt)

    # Count nets by category for accurate status reporting (Issue #812)
    # - Multi-pad nets: 2+ pads, need actual routing
    # - Single-pad nets: 1 pad, trivially complete (no routing needed)
    # - Power nets: skipped via skip_nets, handled by copper pours
    multi_pad_nets = []
    single_pad_nets = []
    for net_num, pads in router.nets.items():
        if net_num > 0:  # Skip net 0 (unconnected)
            if len(pads) >= 2:
                multi_pad_nets.append(net_num)
            elif len(pads) == 1:
                single_pad_nets.append(net_num)
    nets_to_route = len(multi_pad_nets)  # Only multi-pad nets need routing
    power_nets_skipped = len(skip_nets)

    if not quiet:
        flush_print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
        backend_info = router.backend_info
        grid_cells = router.grid.cols * router.grid.rows * router.grid.num_layers
        from kicad_tools.router.cpp_backend import format_backend_status

        backend_status = format_backend_status(backend_info, grid_cells)
        flush_print(f"  Backend:    {backend_status}")
        flush_print(
            f"  Grid:       {router.grid.cols}x{router.grid.rows}x{router.grid.num_layers} = {grid_cells:,} cells"
        )
        flush_print(f"  Total nets: {len(net_map)}")
        flush_print(f"  Nets to route: {nets_to_route} (multi-pad signal nets)")

        if args.verbose:
            print("\n  Net breakdown:")
            for net_name, net_num in sorted(net_map.items(), key=lambda x: x[1]):
                if net_name and net_name not in skip_nets:
                    pad_count = len(router.nets.get(net_num, []))
                    print(f"    {net_name}: {pad_count} pads")

    # Surface single-pad signal nets as a top-of-output warning before
    # routing starts.  These nets are structurally unroutable -- the router
    # silently skips them, which makes "13/13 routed, DRC clean" look like
    # a successful build even when 4 SWD signals are floating.  See
    # `kct check --only single_pad_net` for the full DRC-style report.
    if not quiet:
        _emit_single_pad_net_warning(router, single_pad_nets)

    # Analyze fine-pitch components for grid compatibility warnings
    # This runs automatically to warn users about potential routing issues
    if not quiet:
        from kicad_tools.router.fine_pitch import analyze_fine_pitch_components
        from kicad_tools.router.output import show_fine_pitch_warnings

        fine_pitch_report = analyze_fine_pitch_components(
            pads=router.pads,
            grid_resolution=args.grid,
            trace_width=args.trace_width,
            clearance=args.clearance,
        )
        if fine_pitch_report.has_warnings:
            if router.use_waypoint_injection:
                # Waypoint injection handles off-grid pads by injecting their
                # exact positions into the A* search graph, so the grid-alignment
                # warnings are misleading.  Show a brief summary instead.
                if fine_pitch_report.total_off_grid > 0:
                    flush_print(
                        f"\n  {fine_pitch_report.total_off_grid} pads off-grid; "
                        "waypoint injection will handle pad connections"
                    )
                # Still show full per-component detail at verbose (-v)
                if args.verbose:
                    flush_print("\n--- Fine-Pitch Component Analysis (verbose) ---")
                    show_fine_pitch_warnings(fine_pitch_report, quiet=quiet, verbose=True)
            else:
                flush_print("\n--- Fine-Pitch Component Analysis ---")
                show_fine_pitch_warnings(fine_pitch_report, quiet=quiet, verbose=args.verbose)

    # Show pre-route RUDY congestion estimation (Issue #2278)
    if getattr(args, "show_congestion", False):
        try:
            estimator = router._ensure_congestion_estimator()
            if not quiet:
                flush_print("\n--- Pre-Route Congestion Estimation (RUDY) ---")
            if args.format == "json":
                import json as _json

                print(_json.dumps(estimator.format_json(), indent=2))
            else:
                print(estimator.format_ascii_heatmap())
                # Summary stats
                max_demand = max(
                    (
                        estimator.demand[r][c]
                        for r in range(estimator.grid.rows)
                        for c in range(estimator.grid.cols)
                    ),
                    default=0.0,
                )
                scored_nets = [(nid, s) for nid, s in estimator.net_scores.items() if s > 0]
                scored_nets.sort(key=lambda x: -x[1])
                print(f"\n  Peak tile demand: {max_demand:.2f}")
                print(f"  Nets with congestion score: {len(scored_nets)}")
                if scored_nets and not quiet:
                    print("  Top congested nets:")
                    for nid, score in scored_nets[:5]:
                        name = router.net_names.get(nid, f"Net {nid}")
                        print(f"    {name}: {score:.3f}")
        except Exception as e:
            if not quiet:
                print(f"  Warning: Congestion estimation failed: {e}", file=sys.stderr)

    # Analyze routability if requested
    if args.analyze:
        # Run pre-routing complexity analysis first
        if not quiet:
            print("\n--- Pre-Routing Complexity Analysis ---")
        try:
            pcb_for_analysis = PCB.load(str(pcb_path))
            complexity_analyzer = ComplexityAnalyzer()
            complexity = complexity_analyzer.analyze(pcb_for_analysis)

            # Show complexity summary
            print(f"\n{'=' * 60}")
            print("COMPLEXITY ANALYSIS")
            print(f"{'=' * 60}")
            print(f"Board: {complexity.board_width_mm:.1f}mm x {complexity.board_height_mm:.1f}mm")
            print(f"Pads: {complexity.total_pads}, Nets: {complexity.total_nets}")

            # Show complexity rating with color
            rating_symbols = {
                ComplexityRating.TRIVIAL: "[TRIVIAL]",
                ComplexityRating.SIMPLE: "[SIMPLE]",
                ComplexityRating.MODERATE: "[MODERATE]",
                ComplexityRating.COMPLEX: "[COMPLEX]",
                ComplexityRating.EXTREME: "[EXTREME]",
            }
            print(
                f"Complexity: {complexity.overall_score:.0f}/100 - "
                f"{rating_symbols[complexity.complexity_rating]}"
            )

            # Show layer predictions
            print("\nLayer Predictions:")
            for pred in complexity.layer_predictions:
                rec_str = " (recommended)" if pred.recommended else ""
                print(
                    f"  {pred.layer_count} layers: {pred.success_probability * 100:.0f}% success{rec_str}"
                )

            # Show bottlenecks
            if complexity.bottlenecks:
                print(f"\nBottlenecks ({len(complexity.bottlenecks)}):")
                for bottleneck in complexity.bottlenecks[:3]:
                    print(f"  - {bottleneck.component_ref}: {bottleneck.description}")

            print(f"{'=' * 60}")
        except Exception as e:
            print(f"Warning: Complexity analysis failed: {e}", file=sys.stderr)

        if not quiet:
            print("\n--- Routability Analysis ---")
        try:
            analyzer = RoutabilityAnalyzer(router)
            report = analyzer.analyze()

            # Print analysis report
            print(f"\n{'=' * 60}")
            print("ROUTABILITY ANALYSIS")
            print(f"{'=' * 60}")
            print(
                f"Estimated completion: {report.estimated_success_rate * 100:.0f}% "
                f"({report.expected_routable}/{report.total_nets} nets)"
            )

            # Show layer utilization
            if report.layer_utilization:
                print("\nLayer Utilization:")
                for layer_name, util in report.layer_utilization.items():
                    bar = "#" * int(util * 20)
                    print(f"  {layer_name:10s}: [{bar:20s}] {util * 100:.0f}%")

            # Show problem nets
            if report.problem_nets:
                print(f"\nProblem Nets ({len(report.problem_nets)}):")
                for net_report in report.problem_nets[:10]:  # Show first 10
                    print(f"\n  {net_report.net_name} ({net_report.pad_count} pads):")
                    print(f"    Severity: {net_report.severity.name}")
                    print(f"    Difficulty: {net_report.difficulty_score:.0f}/100")
                    if net_report.blocking_obstacles:
                        print("    Blocked by:")
                        for obs in net_report.blocking_obstacles[:5]:
                            print(f"      - {obs}")
                    if net_report.alternatives:
                        print("    Alternatives:")
                        for alt in net_report.alternatives[:3]:
                            print(f"      {alt}")
                    if net_report.suggestions:
                        print("    Suggestions:")
                        for sug in net_report.suggestions:
                            print(f"      - {sug}")

            # Show recommendations
            if report.recommendations:
                print("\nRecommendations:")
                for i, rec in enumerate(report.recommendations, 1):
                    print(f"  {i}. {rec}")

            print(f"{'=' * 60}")

            # If just analyzing, exit here
            if args.dry_run:
                return 0

        except Exception as e:
            print(f"Warning: Analysis failed: {e}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

    # Configure bus routing if enabled
    bus_config = None
    if args.bus_routing:
        bus_mode_map = {
            "parallel": BusRoutingMode.PARALLEL,
            "stacked": BusRoutingMode.STACKED,
            "bundled": BusRoutingMode.BUNDLED,
        }
        bus_config = BusRoutingConfig(
            enabled=True,
            mode=bus_mode_map[args.bus_mode],
            spacing=args.bus_spacing,
            min_bus_width=args.bus_min_width,
        )

        # Show detected buses
        if args.verbose and not quiet:
            analysis = router.get_bus_analysis()
            if analysis["total_groups"] > 0:
                print(f"\n  Detected {analysis['total_groups']} bus groups:")
                for group in analysis["groups"]:
                    status = "complete" if group["complete"] else "partial"
                    print(f"    - {group['name']}: {group['width']} bits ({status})")
            else:
                print("\n  No bus signals detected")

    # Configure differential pair routing if enabled
    diffpair_config = None
    diffpair_warnings = []
    if args.differential_pairs:
        diffpair_config = DifferentialPairConfig(
            enabled=True,
            spacing=args.diffpair_spacing,
            max_length_delta=args.diffpair_max_delta,
        )

        # Show detected differential pairs
        if args.verbose and not quiet:
            analysis = router.analyze_differential_pairs()
            if analysis["total_pairs"] > 0:
                print(f"\n  Detected {analysis['total_pairs']} differential pairs:")
                for pair in analysis["pairs"]:
                    print(
                        f"    - {pair['name']}: {pair['type']} "
                        f"(spacing={pair['spacing']}mm, max_delta={pair['max_delta']}mm)"
                    )
                if analysis["unpaired"]:
                    print(f"\n  Unpaired differential signals: {analysis['unpaired_signals']}")
                    for sig in analysis["unpaired"]:
                        print(f"    - {sig['net_name']} ({sig['polarity']})")
            else:
                print("\n  No differential pairs detected")

    # Check cache for existing routing result (unless --no-cache)
    cache_key = None
    cached_result = None
    use_cache = not args.no_cache

    if use_cache:
        from kicad_tools.router import CacheKey, RoutingCache

        try:
            # Compute cache key from PCB content and rules
            pcb_content = pcb_path.read_bytes()
            cache_key = CacheKey.compute(pcb_content, rules, args.grid)

            cache = RoutingCache()

            if not quiet:
                flush_print("\n--- Checking routing cache ---")

            cached_result = cache.get(cache_key)
            if cached_result is not None:
                if not quiet:
                    print(f"  Cache HIT: {cached_result.success_count} nets routed")
                    print(
                        f"  Segments: {cached_result.total_segments}, Vias: {cached_result.total_vias}"
                    )
                    print(f"  Original compute time: {cached_result.compute_time_ms}ms")

                # Deserialize and apply cached routes
                cached_routes = cache.deserialize_routes(cached_result.routes_data)

                # Apply cached routes to router
                router.routes = cached_routes

                if not quiet:
                    print("  Using cached routing result")
            else:
                if not quiet:
                    print(f"  Cache MISS (key: {cache_key.full_key[:32]}...)")
                if args.cache_only:
                    print(
                        "Error: --cache-only specified but no cached result found", file=sys.stderr
                    )
                    return 1
        except Exception as e:
            if not quiet:
                print(f"  Cache error: {e}")
            cached_result = None
            if args.cache_only:
                print("Error: --cache-only specified but cache lookup failed", file=sys.stderr)
                return 1

    # Track nets that needed clearance relaxation (for --progressive-clearance)
    relaxed_nets_report: dict[int, float] = {}
    routing_start_time = None

    # Route (skip if using cached result)
    if cached_result is not None:
        # Skip routing - using cached result
        if not quiet:
            flush_print("\n--- Using cached result (skipping routing) ---")
    else:
        # Route
        if not quiet:
            flush_print(f"\n--- Routing ({args.strategy}) ---")
            if args.timeout:
                flush_print(f"  Timeout: {args.timeout}s")
            per_net_timeout_val = getattr(args, "per_net_timeout", None)
            if per_net_timeout_val:
                flush_print(f"  Per-net timeout: {per_net_timeout_val}s")
            # Issue #2610: report the iteration backstop override if set.
            # Issue #2819: the inner parser now declares the flag (default=0),
            # so the attribute is guaranteed to exist; ``or 0`` preserves the
            # historic semantics (0 = use cols*rows*4 heuristic, suppress log).
            _max_iter_val = args.max_search_iterations or 0
            if _max_iter_val:
                flush_print(f"  Max search iterations: {_max_iter_val}")
            if args.profile:
                profile_output = args.profile_output or "route_profile.prof"
                flush_print(f"  Profiling enabled: {profile_output}")

        import time

        routing_start_time = time.time()

        # Resolve escape routing flag: True=force on, False=force off, None=auto-detect
        escape_routing_flag = _resolve_escape_routing_flag(args)

        # Build checkpoint callback once -- shared across every
        # route_all_negotiated call in this invocation (Issue #2808).
        # Returns None when --checkpoint-interval is 0 or unset, in which
        # case the router treats it as "no checkpointing".
        # The last_write_time is internal to the closure and persists
        # across all route_all_negotiated calls (placement-feedback iter
        # loops do NOT reset cadence, so back-to-back PF iterations can
        # share a single throttle window).
        _checkpoint_cb = _make_checkpoint_callback(
            pcb_path,
            output_path,
            float(getattr(args, "checkpoint_interval", 30.0) or 0.0),
            quiet=quiet,
        )

        # Define routing function for profiling
        def do_routing():
            nonlocal diffpair_warnings, relaxed_nets_report

            # Adaptive multi-resolution routing (when --grid auto selects it)
            if multi_res_plan is not None and multi_res_plan.is_multi_resolution:
                from kicad_tools.router.adaptive_grid import AdaptiveGridRouter

                if not quiet:
                    flush_print("  Using adaptive multi-resolution grid strategy")
                adaptive_router = AdaptiveGridRouter(
                    grid=router.grid,
                    rules=rules,
                    router=router,
                )

                # Build nets dict (filter to routable multi-pad nets)
                adaptive_nets = {
                    net_id: pad_keys
                    for net_id, pad_keys in router.nets.items()
                    if net_id > 0 and len(pad_keys) >= 2
                }
                adaptive_pads = router.pads

                # Define the Phase 2 routing function
                def phase2_route_fn():
                    # Issue #2464: When --differential-pairs is set, run a
                    # diff-pair pre-pass before the configured strategy.
                    # The strategy then routes the remaining nets; diff-pair
                    # nets are filtered from the negotiated loop via the
                    # prerouted-net skip in route_all_negotiated.
                    if args.differential_pairs and args.strategy in ("negotiated", "basic"):

                        def _phase2_strategy():
                            if args.strategy == "negotiated":
                                return router.route_all_negotiated(
                                    max_iterations=args.iterations,
                                    timeout=_budgeted_timeout(args),
                                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                                    batch_routing=getattr(args, "batch_routing", False)
                                    or getattr(args, "high_performance", False),
                                    hierarchical=getattr(args, "hierarchical", False),
                                    perturbation=getattr(args, "perturbation", True),
                                    # Issue #3039: forward --seed for deterministic routing.
                                    seed=getattr(args, "seed", None),
                                    checkpoint_callback=_checkpoint_cb,
                                )
                            return router.route_all()

                        # coupled_only=True so pairs that the
                        # CoupledPathfinder cannot handle (3-pad nets,
                        # etc.) fall through to the main strategy
                        # rather than being half-routed independently
                        # and then skipped.  Issue #2464.
                        result, dp_warnings = router.route_all_with_diffpairs(
                            diffpair_config,
                            non_diffpair_strategy=_phase2_strategy,
                            coupled_only=(args.strategy == "negotiated"),
                        )
                        diffpair_warnings.extend(dp_warnings)
                        return result
                    if args.strategy == "evolutionary":
                        return router.route_all_evolutionary(
                            pop_size=args.pop_size,
                            generations=args.generations,
                            verbose=args.verbose and not quiet,
                            timeout=_budgeted_timeout(args),
                        )
                    elif args.strategy == "monte-carlo":
                        return router.route_all_monte_carlo(
                            num_trials=args.mc_trials,
                            verbose=args.verbose and not quiet,
                        )
                    elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                        return router.route_all_two_phase(
                            use_negotiated=True,
                            corridor_width_factor=2.0,
                            timeout=_budgeted_timeout(args),
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                            max_iterations=getattr(args, "two_phase_iterations", None)
                            or args.iterations,
                        )
                    elif args.strategy == "negotiated":
                        return router.route_all_negotiated(
                            max_iterations=args.iterations,
                            timeout=_budgeted_timeout(args),
                            per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                            batch_routing=getattr(args, "batch_routing", False)
                            or getattr(args, "high_performance", False),
                            hierarchical=getattr(args, "hierarchical", False),
                            perturbation=getattr(args, "perturbation", True),
                            # Issue #3039: forward --seed for deterministic routing.
                            seed=getattr(args, "seed", None),
                            checkpoint_callback=_checkpoint_cb,
                        )
                    else:
                        return router.route_all()

                adaptive_result = adaptive_router.route_adaptive(
                    nets=adaptive_nets,
                    pads=adaptive_pads,
                    route_fn=phase2_route_fn,
                )

                if not quiet:
                    flush_print(f"\n{adaptive_result.format_summary()}")

                return adaptive_result.all_routes

            # Check if escape routing should run as a pre-phase
            if _should_use_escape_routing(router, escape_routing_flag, quiet):
                return router.route_with_escape(
                    use_negotiated=(args.strategy == "negotiated"),
                    timeout=_budgeted_timeout(args),
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                )

            # Progressive clearance relaxation mode
            if getattr(args, "progressive_clearance", False):
                routes, relaxed_nets_report = router.route_with_progressive_clearance(
                    min_clearance=getattr(args, "min_clearance", None),
                    num_relaxation_levels=getattr(args, "relaxation_levels", 3),
                    max_iterations=args.iterations,
                    timeout=_budgeted_timeout(args),
                )
                return routes
            elif getattr(args, "multi_resolution", False):
                return router.route_all_multi_resolution(
                    use_negotiated=(args.strategy == "negotiated"),
                    max_iterations=args.iterations,
                    timeout=_budgeted_timeout(args),
                )
            elif getattr(args, "two_phase", False) and args.strategy == "negotiated":
                return router.route_all_two_phase(
                    use_negotiated=True,
                    corridor_width_factor=2.0,
                    timeout=_budgeted_timeout(args),
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    max_iterations=getattr(args, "two_phase_iterations", None) or args.iterations,
                )
            elif args.differential_pairs and args.strategy == "negotiated":
                # Issue #2464: Diff-pair pre-pass + negotiated for the rest.
                # The negotiated loop honors prerouted nets via the new skip
                # logic in route_all_negotiated.  coupled_only=True so
                # unsupported pad configurations fall through cleanly.
                def _neg_strategy():
                    return router.route_all_negotiated(
                        max_iterations=args.iterations,
                        timeout=_budgeted_timeout(args),
                        per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                        batch_routing=getattr(args, "batch_routing", False)
                        or getattr(args, "high_performance", False),
                        hierarchical=getattr(args, "hierarchical", False),
                        perturbation=getattr(args, "perturbation", True),
                        # Issue #3039: forward --seed for deterministic routing.
                        seed=getattr(args, "seed", None),
                        checkpoint_callback=_checkpoint_cb,
                    )

                result, dp_warnings = router.route_all_with_diffpairs(
                    diffpair_config,
                    non_diffpair_strategy=_neg_strategy,
                    coupled_only=True,
                )
                diffpair_warnings.extend(dp_warnings)
                return result
            elif args.strategy == "negotiated":
                return router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=_budgeted_timeout(args),
                    per_net_timeout=getattr(args, "per_net_timeout", None) or None,
                    batch_routing=getattr(args, "batch_routing", False)
                    or getattr(args, "high_performance", False),
                    hierarchical=getattr(args, "hierarchical", False),
                    perturbation=getattr(args, "perturbation", True),
                    # Issue #3039: forward --seed for deterministic routing.
                    seed=getattr(args, "seed", None),
                    checkpoint_callback=_checkpoint_cb,
                )
            elif args.differential_pairs and args.strategy == "basic":
                result, dp_warnings = router.route_all_with_diffpairs(diffpair_config)
                diffpair_warnings.extend(dp_warnings)
                return result
            elif args.bus_routing and args.strategy == "basic":
                return router.route_all_with_buses(bus_config)
            elif args.strategy == "basic":
                return router.route_all()
            elif args.differential_pairs and args.strategy in ("monte-carlo", "evolutionary"):
                # Issue #2464: MC/GA reset the grid per trial (see
                # _reset_for_new_trial), which would wipe pre-routed
                # diff-pair traces.  For now, surface a warning and fall
                # through to the standard strategy.  Follow-up work needed
                # to integrate diff-pair pre-pass with these strategies.
                if not quiet:
                    flush_print(
                        "  Warning: --differential-pairs is not yet supported with "
                        f"strategy='{args.strategy}' (each trial resets the grid). "
                        "Falling through to standard strategy. See Issue #2464."
                    )
                if args.strategy == "monte-carlo":
                    return router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                    )
                else:
                    return router.route_all_evolutionary(
                        pop_size=args.pop_size,
                        generations=args.generations,
                        verbose=args.verbose and not quiet,
                    )
            elif args.strategy == "monte-carlo":
                return router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
            elif args.strategy == "evolutionary":
                return router.route_all_evolutionary(
                    pop_size=args.pop_size,
                    generations=args.generations,
                    verbose=args.verbose and not quiet,
                    timeout=_budgeted_timeout(args),
                )
            return None

        try:
            if args.profile:
                # Profile the routing operation
                import cProfile
                import pstats

                profile_output = args.profile_output or "route_profile.prof"
                profiler = cProfile.Profile()
                profiler.enable()
                try:
                    _ = do_routing()
                finally:
                    profiler.disable()
                    # Save profile data
                    profiler.dump_stats(profile_output)
                    if not quiet:
                        print(f"\n  Profile saved to: {profile_output}")
                        # Print top 20 functions by cumulative time
                        print("\n--- Profile Summary (top 20 by cumulative time) ---")
                        stats = pstats.Stats(profiler)
                        stats.strip_dirs().sort_stats("cumulative").print_stats(20)
            else:
                # Normal routing without profiling
                if args.strategy == "negotiated":
                    # Negotiated routing has its own progress output - don't use spinner
                    _ = do_routing()
                else:
                    with spinner(f"Routing {nets_to_route} nets...", quiet=quiet):
                        _ = do_routing()
        except KeyboardInterrupt:
            # Handle any KeyboardInterrupt that wasn't caught by signal handler
            _interrupt_state["interrupted"] = True
            if not quiet:
                print("\n\n⚠ Routing interrupted!")
        except Exception as e:
            # Provide actionable guidance when escape routing detected a
            # doomed coarse grid (issue #2387).
            try:
                from kicad_tools.router.adaptive_grid import (
                    FinePitchEscapeFailure,
                )
            except Exception:
                FinePitchEscapeFailure = None  # type: ignore[assignment]
            if FinePitchEscapeFailure is not None and isinstance(e, FinePitchEscapeFailure):
                print(
                    f"Error during routing: {e}",
                    file=sys.stderr,
                )
                print(
                    f"  Suggested fix: rerun with --grid {e.suggested_grid:.4f}",
                    file=sys.stderr,
                )
            else:
                print(f"Error during routing: {e}", file=sys.stderr)
            # Still try to save partial results on error
            if router.routes:
                _save_partial_results()
            return 1

        # Check if interrupted and save partial results
        if _interrupt_state["interrupted"]:
            _save_partial_results()
            return 5  # Exit code 5 indicates interruption with partial results saved

        # Issue #2595: placement-routing feedback loop.  When the user
        # opted in via --placement-feedback and the initial pass left
        # nets unrouted, invoke route_with_placement_feedback() to
        # nudge non-anchored components and re-route from scratch.
        # The helper writes <output>_placement_diff.json beside the
        # routed PCB; the return value is not consumed here because
        # downstream stages (cache, optimize, save) read directly from
        # router.routes (which the feedback loop mutates in place).
        if (
            getattr(args, "placement_feedback", False)
            and router.routes is not None
            and router.get_failed_nets()
        ):
            _run_placement_feedback(
                router=router,
                pcb_path=pcb_path,
                args=args,
                quiet=quiet,
            )

        # Cache the routing result (if caching enabled and routing succeeded)
        if use_cache and cache_key is not None and router.routes:
            import time

            try:
                routing_time_ms = (
                    int((time.time() - routing_start_time) * 1000) if routing_start_time else 0
                )
                stats = router.get_statistics()
                cache.put(cache_key, router.routes, stats, routing_time_ms)
                if not quiet:
                    print(f"  Cached routing result ({routing_time_ms}ms compute time)")
            except Exception as e:
                if not quiet:
                    print(f"  Warning: Failed to cache result: {e}")

    # Get pre-optimization statistics (also used in the no-optimize path
    # below so the segment/via summary print does not raise
    # UnboundLocalError when --no-optimize is set).
    pre_segments = sum(len(r.segments) for r in router.routes)
    pre_vias = sum(len(r.vias) for r in router.routes)

    # Optimize traces (unless --no-optimize/--raw flag is set)
    if not args.no_optimize and router.routes:
        from kicad_tools.router.optimizer import (
            OptimizationConfig,
            TraceOptimizer,
            make_collision_checker,
        )

        if not quiet:
            print("\n--- Optimizing traces ---")

        # Configure and run optimizer
        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        # Issue #2303: Use overflow-tolerant collision checking when
        # the router finished with residual overflow.
        has_overflow = router.grid.get_total_overflow() > 0
        collision_checker = make_collision_checker(router.grid, ignore_overflow=has_overflow)
        optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

        # Issue #2596: snapshot per-net connectivity before optimize so
        # any net whose pad-to-pad connectivity regresses can be reverted.
        _ci_snapshot = _connectivity_snapshot(router)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            router.routes = optimized_routes

        _enforce_connectivity_invariant_or_exit(
            router,
            _ci_snapshot,
            phase="optimize",
            args=args,
            quiet=quiet,
        )

    # Post-optimization DRC nudge pass
    if router.routes:
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        # Issue #2596: snapshot connectivity before nudge.
        _ci_snapshot_nudge = _connectivity_snapshot(router)

        with spinner("DRC nudge pass...", quiet=quiet):
            nudge_result = drc_verify_and_nudge(router)
        if not quiet and nudge_result.initial_violations > 0:
            print(f"  {nudge_result.summary()}")

        _enforce_connectivity_invariant_or_exit(
            router,
            _ci_snapshot_nudge,
            phase="nudge",
            args=args,
            quiet=quiet,
        )

        # Get post-optimization statistics
        post_segments = sum(len(r.segments) for r in router.routes)
        post_vias = sum(len(r.vias) for r in router.routes)

        if not quiet:
            segment_reduction = (
                ((pre_segments - post_segments) / pre_segments * 100) if pre_segments > 0 else 0
            )
            via_reduction = ((pre_vias - post_vias) / pre_vias * 100) if pre_vias > 0 else 0
            print(f"  Segments: {pre_segments} -> {post_segments} ({-segment_reduction:+.1f}%)")
            if pre_vias > 0:
                print(f"  Vias:     {pre_vias} -> {post_vias} ({-via_reduction:+.1f}%)")

    # Differential-pair length-match (skew) tuning -- Epic #2556 Phase 3I (Issue #2648).
    # Engaged via --length-match-diffpairs (opt-in).  Requires --differential-pairs;
    # otherwise emit a warning and short-circuit (no detection means no pairs).
    if getattr(args, "length_match_diffpairs", False) and router.routes:
        if not args.differential_pairs:
            if not quiet:
                print(
                    "\n--- Length-Match Diff-Pairs: skipped ---\n"
                    "  --length-match-diffpairs requires --differential-pairs "
                    "(no detection means no pairs to tune)."
                )
        else:
            from kicad_tools.router.diffpair_detection import detect_diff_pairs

            if not quiet:
                print("\n--- Length-Match Diff-Pairs (Epic #2556 Phase 3I) ---")
            detected_pairs = detect_diff_pairs(net_names=router.net_names)
            if not detected_pairs:
                if not quiet:
                    print("  No differential pairs detected; nothing to tune.")
            else:
                # Snapshot connectivity for the pad-to-pad invariant.
                _ci_snapshot_skew = _connectivity_snapshot(router)
                tune_results = router.apply_diffpair_length_tuning(
                    detected_pairs=detected_pairs,
                    verbose=not quiet,
                )
                _enforce_connectivity_invariant_or_exit(
                    router,
                    _ci_snapshot_skew,
                    phase="length_match_diffpairs",
                    args=args,
                    quiet=quiet,
                )
                if not quiet:
                    n_tuned = sum(1 for r in tune_results.values() if r.reason == "tuned")
                    n_clean = sum(
                        1 for r in tune_results.values() if r.reason == "already_within_tolerance"
                    )
                    n_rollback = sum(
                        1
                        for r in tune_results.values()
                        if r.reason == "post_insertion_drc_violation"
                    )
                    n_budget = sum(
                        1 for r in tune_results.values() if r.reason == "exceeded_max_inserts"
                    )
                    n_skipped = sum(
                        1 for r in tune_results.values() if r.reason == "not_length_critical"
                    )
                    print(
                        f"  Summary: {n_tuned} tuned, {n_clean} clean, "
                        f"{n_rollback} rolled back, {n_budget} budget-exhausted, "
                        f"{n_skipped} skipped (not length-critical)"
                    )

    # N-trace match-group length-match (skew) tuning -- Epic #2661 Phase 3H
    # (Issue #2723).  Engaged via --length-match-groups (opt-in).  Runs
    # AFTER --length-match-diffpairs so the within-pair skew invariant is
    # preserved before group tuning perturbs lane lengths.  Graceful
    # short-circuit when no groups are declared / detected.
    if getattr(args, "length_match_groups", False) and router.routes:
        from kicad_tools.router.match_group_detection import detect_match_groups

        if not quiet:
            print("\n--- Length-Match Groups (Epic #2661 Phase 3H) ---")
        # Build net_to_class so explicit declarations can be consulted
        # (mirrors the construction in core.py:_finalize_routing).
        net_to_class: dict[str, str] = {}
        for net_name, net_class in router.net_class_map.items():
            net_to_class[net_name] = net_class.name
        try:
            detected_groups = detect_match_groups(
                net_names=router.net_names,
                net_class_routing=router.net_class_map,
                net_to_class=net_to_class,
                length_tracker=router.length_tracker,
                enable_suffix_inference=False,
            )
        except Exception as exc:
            if not quiet:
                print(f"  Match-group detection failed: {exc}; skipping.")
            detected_groups = []

        if not detected_groups:
            if not quiet:
                print("  No match groups detected; nothing to tune.")
        else:
            # Snapshot connectivity for the pad-to-pad invariant.
            _ci_snapshot_groups = _connectivity_snapshot(router)
            tune_results_groups = router.apply_match_group_tuning(
                detected_groups=detected_groups,
                verbose=not quiet,
            )
            _enforce_connectivity_invariant_or_exit(
                router,
                _ci_snapshot_groups,
                phase="length_match_groups",
                args=args,
                quiet=quiet,
            )
            if not quiet:
                # Aggregate per-member counters across all groups for a
                # single end-of-phase summary line.
                all_results = [
                    res
                    for member_dict in tune_results_groups.values()
                    for (_route, res) in member_dict.values()
                ]
                n_tuned = sum(1 for r in all_results if r.reason == "tuned")
                n_clean = sum(1 for r in all_results if r.reason == "already_within_tolerance")
                n_rollback = sum(
                    1 for r in all_results if r.reason == "post_insertion_drc_violation"
                )
                n_budget = sum(
                    1
                    for r in all_results
                    if r.reason in ("exceeded_max_inserts", "cascade_budget_exhausted")
                )
                n_skipped = sum(1 for r in all_results if r.reason == "not_length_critical")
                print(
                    f"  Summary: {len(tune_results_groups)} groups, "
                    f"{n_tuned} tuned, {n_clean} clean, "
                    f"{n_rollback} rolled back, {n_budget} budget-exhausted, "
                    f"{n_skipped} skipped (not length-critical)"
                )

    # Finalize: cleanup -> sexp -> stats (canonical ordering)
    multi_pad_net_ids = set(multi_pad_nets)
    route_sexp, stats, cleanup_stats = _finalize_routes(
        router,
        multi_pad_net_ids,
        nets_to_route,
        quiet=quiet,
    )

    # Report differential pair length mismatch warnings
    if diffpair_warnings and not quiet:
        print(f"\n--- Differential Pair Warnings ({len(diffpair_warnings)}) ---")
        for warning in diffpair_warnings:
            print(f"  {warning}")

    # Report nets that needed clearance relaxation (--progressive-clearance mode)
    if relaxed_nets_report and not quiet:
        original_clearance = rules.trace_clearance
        print(f"\n--- Clearance Relaxation Report ({len(relaxed_nets_report)} nets) ---")
        print(f"  Original clearance: {original_clearance:.3f}mm")
        for net_id, clearance in sorted(relaxed_nets_report.items(), key=lambda x: x[1]):
            net_name = router.net_names.get(net_id, f"Net {net_id}")
            reduction = (1 - clearance / original_clearance) * 100
            print(f"  {net_name}: {clearance:.3f}mm ({reduction:.0f}% relaxation)")

    # Show preview if requested
    if args.preview:
        response = show_preview(
            router,
            net_map,
            nets_to_route,
            quiet=quiet,
            nets_to_route_ids=multi_pad_net_ids,
        )
        if response != "y":
            if not quiet:
                print("\nRouting cancelled. No changes saved.")
            return 0

    # Generate power zones if requested
    zone_sexp = ""
    if args.power_nets:
        from kicad_tools.zones import ZoneGenerator, parse_power_nets

        try:
            power_nets = parse_power_nets(args.power_nets)
        except ValueError as e:
            print(f"Error parsing power-nets: {e}", file=sys.stderr)
            return 1

        if power_nets and not quiet:
            print("\n--- Generating copper zones ---")
            print(f"  Power nets: {', '.join(f'{n}:{l}' for n, l in power_nets)}")

        if power_nets:
            try:
                gen = ZoneGenerator.from_pcb(
                    str(pcb_path),
                    edge_clearance=args.edge_clearance,
                )
                for net_name, layer in power_nets:
                    # GND gets higher priority (fills last, on top)
                    priority = 1 if net_name.upper() in ("GND", "GNDA", "GNDD") else 0
                    try:
                        gen.add_zone(
                            net=net_name,
                            layer=layer,
                            priority=priority,
                        )
                        if not quiet:
                            print(f"    Added zone: {net_name} on {layer} (priority {priority})")
                    except ValueError as e:
                        print(f"  Warning: Could not add zone for {net_name}: {e}")

                zone_sexp = gen.generate_sexp()
            except Exception as e:
                print(f"  Warning: Zone generation failed: {e}")

    # Pre-save clearance validation
    # Issue #1666: Segment-to-segment violations now cause a non-zero exit
    # code so that CI pipelines and DRC workflows can detect the failure.
    seg_seg_violation_count = 0
    if stats["nets_routed"] > 0 and not args.dry_run:
        from kicad_tools.router.io import format_clearance_violations, validate_routes

        clearance_violations = validate_routes(router)
        if clearance_violations:
            seg_seg_violation_count = sum(
                1
                for v in clearance_violations
                if v.obstacle_type == "segment" and not v.component_inherent
            )
            if not quiet:
                print("\n--- Pre-save Clearance Validation ---")
                if seg_seg_violation_count > 0:
                    print(
                        f"  ERROR: {seg_seg_violation_count} segment-to-segment "
                        f"clearance violation(s) remain after routing"
                    )
                print(f"  {format_clearance_violations(clearance_violations)}")

    # Save output
    output_content = ""  # Tracks written content for output connectivity verification
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
    else:
        if not quiet:
            print("\n--- Saving routed PCB ---")

        with spinner("Saving routed PCB...", quiet=quiet):
            # route_sexp was already generated by _finalize_routes() above
            # Combine zone + route fragments before insertion.
            # Note: KiCad's S-expression format doesn't support ; comments.
            combined_sexp = ""
            if route_sexp or zone_sexp:
                fragments = []
                if zone_sexp:
                    fragments.append(zone_sexp)
                if route_sexp:
                    fragments.append(route_sexp)
                combined_sexp = "\n  ".join(fragments)
            elif not quiet:
                print("  Warning: No routes generated!")

            # Issue #2808: atomic write via _write_routed_pcb.  No layer
            # escalation in this flow, so layer_count defaults to 2 (no-op).
            _write_routed_pcb(
                pcb_path,
                output_path,
                combined_sexp,
            )

            # We need output_content for the connectivity verification below;
            # re-read since the atomic write left output_path with the final
            # content we just wrote.
            output_content = output_path.read_text()

        if not quiet:
            print(f"  Saved to: {output_path}")

    # Output connectivity verification (Issue #2264)
    # Re-parse written S-expressions and verify pad-to-pad connectivity
    output_has_disconnected = False
    if not args.dry_run and router.pads and router.nets and output_content:
        from kicad_tools.router.io import verify_output_connectivity

        # Build net_pads mapping (same as get_statistics)
        verify_net_pads: dict[int, list] = {}
        for net_id, pad_keys in router.nets.items():
            if net_id not in multi_pad_net_ids:
                continue
            pad_list = [router.pads[k] for k in pad_keys if k in router.pads]
            if len(pad_list) >= 2:
                verify_net_pads[net_id] = pad_list

        # Build net name lookup
        reverse_net_map = {v: k for k, v in net_map.items()}

        output_connectivity = verify_output_connectivity(
            pcb_content=output_content,
            net_pads=verify_net_pads,
            net_names=reverse_net_map,
        )

        disconnected_nets = {
            nid: info
            for nid, info in output_connectivity.items()
            if not info["connected"] and info["total_pads"] >= 2
        }

        if disconnected_nets:
            output_has_disconnected = True
            if not quiet:
                print("\n--- Output Connectivity Verification ---")
                print(
                    f"  WARNING: {len(disconnected_nets)} net(s) have disconnected "
                    f"pads in written output"
                )
                for nid, info in sorted(disconnected_nets.items(), key=lambda x: x[1]["net_name"]):
                    disc_str = ", ".join(info["disconnected_pads"][:5])
                    if len(info["disconnected_pads"]) > 5:
                        disc_str += f" (+{len(info['disconnected_pads']) - 5} more)"
                    print(
                        f"  {info['net_name']}: "
                        f"{info['connected_pads']}/{info['total_pads']} pads connected"
                        f" -- disconnected: {disc_str}"
                    )
        else:
            if not quiet:
                print("\n--- Output Connectivity Verification ---")
                print("  All nets verified connected in written output")

    # Run power plane stitching if requested
    stitch_result = None
    if getattr(args, "stitch_power_planes", False) and not args.dry_run:
        from kicad_tools.cli.stitch_cmd import find_all_plane_nets, run_stitch

        if not quiet:
            print("\n--- Stitching Power Planes ---")

        # Load the saved PCB to find plane nets
        from kicad_tools.core.sexp_file import load_pcb as load_stitch_pcb

        stitch_sexp = load_stitch_pcb(output_path)
        plane_nets = find_all_plane_nets(stitch_sexp)

        if plane_nets:
            net_names = list(plane_nets.keys())
            if not quiet:
                print(f"  Found {len(net_names)} power plane nets: {', '.join(sorted(net_names))}")

            stitch_result = run_stitch(
                pcb_path=output_path,
                net_names=net_names,
                via_size=args.via_diameter,  # Use same via size as routing
                drill=args.via_drill,
                clearance=args.clearance,
                dry_run=False,
            )

            if not quiet:
                if stitch_result.vias_added:
                    print(f"  Added {len(stitch_result.vias_added)} stitching vias")
                else:
                    print("  No stitching vias needed (all pads already connected)")
        else:
            if not quiet:
                print("  No power plane nets found (no zones with assigned nets)")

    # Fill copper-pour zones now that traces (and any stitching vias)
    # are in place (issue #2516).  Must run BEFORE DRC so the DRC sees
    # filled zones rather than bare zone outlines.
    if not args.dry_run and stats["nets_routed"] > 0:
        _fill_zones_after_route(output_path, quiet=quiet)

    # Run DRC validation unless skipped or dry-run
    drc_errors = 0
    drc_warnings = 0
    drc_ran = False
    fix_result: int | None = None

    if not args.dry_run and not args.skip_drc and stats["nets_routed"] > 0:
        drc_ran = True
        drc_errors, drc_warnings = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=layer_stack.num_layers,
            quiet=quiet,
            # Issue #2652, Epic #2556 Phase 2.5b: see other call sites.
            net_class_map=getattr(router, "net_class_map", None),
        )

        # Auto-fix DRC violations if requested
        if drc_errors > 0 and _should_auto_fix(args):
            fix_result = _run_auto_fix(
                output_path=output_path,
                max_passes=getattr(args, "auto_fix_passes", 1),
                quiet=quiet,
                args=args,  # Issue #2802: honor total wall-clock deadline
            )
            if fix_result == 0:
                drc_errors = 0

    # Summary
    all_nets_routed = stats["nets_routed"] == nets_to_route
    drc_passed = drc_errors <= 0  # -1 means DRC failed to run, treat as passed
    completion_ratio = stats["nets_routed"] / nets_to_route if nets_to_route > 0 else 1.0
    meets_threshold = completion_ratio >= args.min_completion

    # Build summary suffix for net breakdown (Issue #812)
    summary_parts = []
    if len(single_pad_nets) > 0:
        summary_parts.append(f"{len(single_pad_nets)} single-pad")
    if power_nets_skipped > 0:
        summary_parts.append(f"{power_nets_skipped} power skipped")
    summary_suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""

    if not quiet:
        print("\n" + "=" * 60)
        if all_nets_routed and drc_passed:
            if drc_ran and drc_errors == 0:
                print(f"SUCCESS: All signal nets routed, DRC passed!{summary_suffix}")
            else:
                print(f"SUCCESS: All signal nets routed!{summary_suffix}")
                if not drc_ran and not args.skip_drc and not args.dry_run:
                    print("  Note: Run 'kct check' to validate before manufacturing")
        elif meets_threshold and not all_nets_routed and drc_passed:
            pct = completion_ratio * 100
            print(
                f"SUCCESS: Routed {stats['nets_routed']}/{nets_to_route} signal nets "
                f"({pct:.0f}%, meets {args.min_completion * 100:.0f}% threshold){summary_suffix}"
            )
            if not drc_ran and not args.skip_drc and not args.dry_run:
                print("  Note: Run 'kct check' to validate before manufacturing")
        elif all_nets_routed and not drc_passed:
            print("ROUTING FAILED: DRC violations detected")
            print("=" * 60)
            print()
            print("Net Statistics:")
            print(f"  Multi-pad nets:  {nets_to_route}")
            print(f"  Nets connected:  {stats['nets_routed']} (topologically complete)")
            print("  Nets DRC-clean:  0 (manufacturing blocked)")
            if len(single_pad_nets) > 0 or power_nets_skipped > 0:
                print(f"  Also:{summary_suffix}")
            print()
            print("DRC Summary:")
            print(f"  Violations: {drc_errors}")
            print()
            print("The autorouter connected all nets but violated design rules.")
            print("This board cannot be manufactured without fixing DRC errors.")
            print()
            print("Suggestions:")
            print(f"  - Auto-repair DRC violations: kct fix-drc {output_path} --max-passes 20")
            print(
                f"  - Try Monte Carlo routing: kct route {args.pcb} --strategy monte-carlo --mc-trials 10"
            )
            print("  - Increase board area")
            print("  - Reduce component density")
            print("  - Try 4-layer routing: kct route --layers 4")
            print(f"  - Or re-route with auto-fix: kct route {args.pcb} --auto-fix")
            print()
            print(f"  Run 'kct check {output_path} --mfr {args.manufacturer}' for full details")
        else:
            print(
                f"PARTIAL: Routed {stats['nets_routed']}/{nets_to_route} signal nets{summary_suffix}"
            )
            if drc_ran and drc_errors > 0:
                print(f"  Additionally, {drc_errors} DRC violation(s) detected.")

            # Issue #2388: When the negotiated loop bailed out due to a
            # power-net stall, surface actionable suggestions naming the
            # specific stalled nets and recommended remediation flags.
            if getattr(router, "power_stall_abort", False):
                _print_power_stall_suggestions(
                    list(getattr(router, "power_stall_nets", [])),
                    layer_stack.num_layers,
                    args.pcb,
                )

            # Show comprehensive routing summary with successes, failures, and suggestions
            # Use JSON format if requested
            if args.format == "json":
                print_routing_diagnostics_json(
                    router,
                    net_map,
                    nets_to_route,
                    current_strategy=args.strategy,
                    nets_to_route_ids=multi_pad_net_ids,
                    single_pad_count=len(single_pad_nets),
                )
            else:
                # Verbose mode shows detailed path analysis for each failure
                verbose = args.verbose or args.diagnostics
                show_routing_summary(
                    router,
                    net_map,
                    nets_to_route,
                    quiet=quiet,
                    verbose=verbose,
                    current_strategy=args.strategy,
                    pcb_file=args.pcb,
                    nets_to_route_ids=multi_pad_net_ids,
                    single_pad_count=len(single_pad_nets),
                )

    # Save partial results on clean partial exit (not just SIGINT)
    if not all_nets_routed and not args.dry_run and router.routes:
        partial_saved = _save_partial_results()
        if partial_saved and not quiet:
            print("  Open in KiCad to complete remaining nets manually")

    # Export failed nets to file if requested
    if getattr(args, "export_failed_nets", None) and not all_nets_routed:
        _export_failed_nets(
            router,
            net_map,
            args.export_failed_nets,
            quiet=quiet,
            nets_to_route_ids=multi_pad_net_ids,
        )

    # Exit codes:
    # 0 = Routing meets --min-completion threshold AND (DRC passed OR DRC not run)
    # 1 = Fatal failure — no nets routed, no useful output
    # 2 = Partial routing — some nets routed but below --min-completion threshold
    # 3 = Meets threshold but DRC violations detected (includes seg-seg violations).
    #     Issue #2852: also returned when --auto-fix rolled back due to a
    #     connectivity regression (fix-drc exit 3) — semantically the same
    #     contract ("routing succeeded but DRC is dirty"); callers cannot
    #     trust the post-route DRC state.
    # 4 = Seg-seg clearance violations remain AND routing is below threshold (Issue #1666)
    # 5 = Interrupted by SIGINT with partial results saved (handled in _handle_interrupt)
    # 6 = Connectivity regression detected (--strict mode only): either
    #     the optimize / DRC nudge phases reduced the number of fully-
    #     connected nets (issue #2596) or post-save output verification
    #     reported disconnected pads (issue #2264).
    #
    # The --min-completion flag (default 0.95) controls the success threshold.
    # With --min-completion 0.80, routing 85% of nets returns exit code 0.
    completion_ratio = stats["nets_routed"] / nets_to_route if nets_to_route > 0 else 1.0
    meets_threshold = completion_ratio >= args.min_completion

    # --strict: output connectivity verification failure is fatal
    if getattr(args, "strict", False) and output_has_disconnected:
        return 6

    # Issue #2852: make the --auto-fix rollback path explicit.  Today this
    # case already falls through to the ``return 3`` branch below because
    # ``drc_errors`` is unchanged when fix-drc rolls back -- but that is
    # accidentally correct, not by design.  Surface it explicitly so the
    # contract is documented in code and survives future refactors that
    # might zero out ``drc_errors`` on the fix-drc path.
    if fix_result == 3:
        return 3

    if stats["nets_routed"] == 0 and nets_to_route > 0:
        # Nothing was routed — treat as fatal failure
        return 1
    elif meets_threshold and drc_passed and seg_seg_violation_count == 0:
        return 0
    elif meets_threshold and (not drc_passed or seg_seg_violation_count > 0):
        # Meets completion threshold but has DRC or clearance violations
        return 3
    elif not meets_threshold and seg_seg_violation_count > 0:
        # Below threshold AND has seg-seg clearance violations
        return 4
    else:
        # Partial routing: some nets routed but below threshold
        return 2


if __name__ == "__main__":
    sys.exit(main())
