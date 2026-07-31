#!/usr/bin/env python3
"""Plane-net / floating-pad completion gate for CI (issue #4531).

A floating power pad -- e.g. board-03's ``J2.1`` (VCC) with no track and no
reachable pour -- silently passed ALL 16 CI checks (PR #4525): the router's
"13/13 signal nets" excludes plane nets, geometric DRC excludes connectivity
by design, copper LVS's ``bound_pad_count`` is unmoved by a zone-bonded pad
going floating, and ``test_connectivity.py``'s single scalar re-baselined the
break as three more fill islands.  The information was there --
``kct net-status`` names the pad exactly -- but nothing asserted on it.  This
gate wires that assertion into every board's end-to-end job.

CRITICAL: this gate uses the RAW, UNFILTERED connectivity counts
-------------------------------------------------------------------
``NetStatusResult.blocking_incomplete_count`` (and ``blocking_incomplete``)
deliberately drop any net whose ``is_advisory_incomplete`` is ``True``
(:mod:`kicad_tools.analysis.net_status`, ~lines 136-163).  That property
returns ``True`` when ``net_type`` is ``"plane"`` or ``"power"`` -- and
``net_type`` is derived PURELY FROM THE NET NAME (any name starting with
``+``/``-``/``V`` or exactly ``GND``/``AGND``/``DGND``/``VCC``/``VDD``/``VSS``),
NOT from whether the net actually has a zone or filled copper.  So the exact
bug #4531 reports -- a net literally named ``VCC`` with ZERO connected pads
and no reachable pour -- is classified advisory and would be SILENTLY EXCLUDED
from ``blocking_incomplete_count``.  A gate built on that metric (the way
``scripts/ci/check_board_05_blocking.py`` is) would NOT catch this regression.

This gate therefore asserts on ``NetStatusResult.total_unconnected_pads`` --
the raw pad-centric count, exactly the signal ``kct net-status``'s own CLI
exit code already uses (``src/kicad_tools/cli/net_status_cmd.py``: nonzero exit
whenever ``incomplete_count`` or ``unrouted_count`` is nonzero).  That exit
code was simply never wired into CI, which is why the reporter's ``kct
net-status`` repro surfaced the break immediately while every other signal
missed it.

Connectivity model: STRICT (real copper geometry, matches KiCad)
----------------------------------------------------------------
This gate runs ``NetStatusAnalyzer(..., strict=True)`` so connectivity is
decided by real geometric copper contact (shapely polygon intersection),
matching KiCad's connectivity engine / ``kicad-cli pcb drc`` (Issue #4176).
The DEFAULT 0.01mm endpoint-proximity model UNDER-detects pad<->pour bonds and
would report FALSE opens on genuinely poured-connected pads (board 06's
GND/+1V2/VBUS_USB: 16 false opens under the default model, 0 under strict /
kicad-cli).  strict mode is both sound (no false positives on pour-connected
boards) and complete (a genuine float -- a pad with no copper reaching it --
is unconnected under either model, so the #4531 J2.1-VCC break still fails).
shapely is a CORE dependency (Issue #3824), so strict mode is always available.

Note on board 05: the ``check_board_05_blocking.py`` gate has the SAME
``blocking_incomplete_count`` blind spot described above (a floating pad on a
net named e.g. ``PHASE_A``/``ISENSE_A+`` would not trip the "power" name
pattern, so the gap is narrower there -- but a net renamed to start with
``V``/``+``/``-`` or matching the power-name list would reproduce it).  Fixing
that gate is OUT OF SCOPE for #4531 (its ``<= 11`` threshold is load-bearing
for #3775/#3766/#3829); board 05 keeps its own dedicated gate and is NOT gated
by this script.

``--known-open-nets NET[,NET...]``
----------------------------------
Board 07 ("matchgroup-test") routes PARTIAL by design: 5 seed-invariant
unroutable nets (#3438/#4012: ``DQ3,DQ4,MIPI_DAT0_N,TMDS_D0_N,TMDS_D1_N``).
Passing those via ``--known-open-nets`` excludes THEIR unconnected pads from
the gated count while still failing on an unconnected pad appearing on ANY
OTHER net -- so a *different* net silently going open is still caught (tighter
than merely raising ``--max-unconnected`` to their pad count, which would let
a substituted open slip through).  Mirrors
``check_board_00_e2e.py``'s ``assert_lvs_known_opens`` intent.

Exit codes (mirrors ``scripts/ci/check_board_05_blocking.py`` /
``check_routed_drc.py``):
    0 -- Unconnected-pad count within threshold (job passes).
    1 -- Tool failure (file missing, PCB parse error, etc.).
    2 -- Unconnected pads exceed threshold (regression -- job fails).

GitHub-Actions annotations (``::error file=...::``) are emitted to stdout so
the PR Files-changed view surfaces a regression inline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MAX_UNCONNECTED = 0


def annotate_error(file: str, message: str) -> None:
    """Emit a GitHub-Actions ``::error file=...::`` annotation."""
    print(f"::error file={file}::{message}", flush=True)


def annotate_warning(file: str, message: str) -> None:
    """Emit a GitHub-Actions ``::warning file=...::`` annotation."""
    print(f"::warning file={file}::{message}", flush=True)


def analyze_unconnected(pcb_path: Path) -> tuple[int, list[tuple[str, list[str]]]]:
    """Load the routed PCB and return its raw unconnected-pad status.

    Uses the RAW, unfiltered :class:`NetStatusResult` view -- explicitly NOT
    ``blocking_incomplete_count`` (see the module docstring's pitfall note).

    Args:
        pcb_path: Path to a routed ``.kicad_pcb`` file.

    Returns:
        Tuple ``(total_unconnected_pads, per_net)`` where
        ``total_unconnected_pads`` is
        ``NetStatusResult.total_unconnected_pads`` (the raw pad-centric count)
        and ``per_net`` is a sorted list of ``(net_name, [REF.PAD @ (x, y),
        ...])`` for every net carrying at least one unconnected pad, for
        diagnostic output.

    Raises:
        RuntimeError: If the PCB cannot be loaded or analyzed.
    """
    # Imported lazily so ``--help`` works even outside the ``uv run``
    # environment where ``kicad_tools`` is importable.
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    # ``strict=True`` decides connectivity by REAL geometric copper contact
    # (shapely polygon intersection), which matches KiCad's connectivity engine
    # / ``kicad-cli pcb drc`` (Issue #4176).  This is deliberate and load-
    # bearing for THIS gate: the DEFAULT 0.01mm endpoint-proximity model
    # UNDER-detects pad<->pour bonds -- a pad whose real copper overlaps a pour
    # but whose reference point is not within 0.01mm of a trace endpoint is
    # reported as a FALSE open.  Board 06 ("diffpair-test") is the concrete
    # case: its GND/+1V2/VBUS_USB pads are genuinely poured-connected
    # (kicad-cli DRC: 0 unconnected), yet the default model reports 16 false
    # opens.  strict mode reports 0 there while STILL catching a genuine float
    # (a pad with no copper reaching it is unconnected under either model), so
    # it is both sound (no false positives on pour-connected boards) and
    # complete (the #4531 J2.1-VCC break still fails).  shapely is a CORE
    # dependency (Issue #3824), so strict mode is always available in CI.
    try:
        result = NetStatusAnalyzer(str(pcb_path), strict=True).analyze()
    except Exception as e:  # noqa: BLE001 -- surface any load/parse failure as a tool error
        raise RuntimeError(f"failed to analyze {pcb_path}: {e}") from e

    per_net: list[tuple[str, list[str]]] = []
    for net in result.nets:
        if net.unconnected_count <= 0:
            continue
        pads = [
            f"{p.full_name} @ ({p.position[0]:.2f}, {p.position[1]:.2f})"
            for p in net.unconnected_pads
        ]
        per_net.append((net.net_name, pads))
    per_net.sort(key=lambda item: item[0])

    return result.total_unconnected_pads, per_net


def check_pcb(
    pcb_path: Path,
    max_unconnected: int,
    known_open_nets: set[str] | None = None,
) -> tuple[int, str]:
    """Compare a routed PCB's unconnected-pad count to the threshold.

    Args:
        pcb_path: Path to the routed ``.kicad_pcb`` file.
        max_unconnected: Maximum allowed unconnected pads on nets NOT in
            ``known_open_nets``.
        known_open_nets: Nets permitted to carry unconnected pads (board 07's
            documented #3438 opens).  Their pads are excluded from the gated
            count, but an unconnected pad on any OTHER net still fails.

    Returns:
        ``(exit_code, message)``.  ``exit_code`` is 0 (pass), 1 (tool
        failure), or 2 (regression).  ``message`` is a human-readable summary
        suitable for stdout and GitHub annotations.
    """
    known_open_nets = known_open_nets or set()

    if not pcb_path.is_file():
        return 1, f"routed PCB not found: {pcb_path}"

    try:
        total_unconnected, per_net = analyze_unconnected(pcb_path)
    except RuntimeError as e:
        return 1, str(e)

    # Split offending nets into gated (must be zero) vs tolerated (known open).
    gated: list[tuple[str, list[str]]] = [
        (name, pads) for name, pads in per_net if name not in known_open_nets
    ]
    tolerated: list[tuple[str, list[str]]] = [
        (name, pads) for name, pads in per_net if name in known_open_nets
    ]
    gated_pad_count = sum(len(pads) for _name, pads in gated)

    # Always surface the measured numbers on plain stdout, BEFORE the verdict,
    # so CI logs record the real figures on every path (pass, or regression
    # with exit 2) -- matching check_board_05_blocking.py's convention.
    known_suffix = (
        f" (known-open nets excluded: {', '.join(sorted(known_open_nets))})"
        if known_open_nets
        else ""
    )
    lines = [
        f"MEASURED total_unconnected_pads = {total_unconnected} "
        f"(threshold <= {max_unconnected} on non-known-open nets){known_suffix}"
    ]
    if per_net:
        lines.append("  unconnected pads by net:")
        for name, pads in per_net:
            tag = " [known-open, tolerated]" if name in known_open_nets else ""
            lines.append(f"    {name}{tag}: {len(pads)} unconnected")
            for pad in pads:
                lines.append(f"      - {pad}")
    else:
        lines.append("  (no unconnected pads)")
    print("\n".join(lines), flush=True)

    # A known-open net that is no longer open is an IMPROVEMENT, not a failure
    # -- surface it as a warning so reviewers can tighten --known-open-nets
    # (mirrors check_board_00_e2e's "graduate this job" note).
    open_net_names = {name for name, _pads in per_net}
    no_longer_open = sorted(known_open_nets - open_net_names)

    if gated_pad_count <= max_unconnected:
        offenders = (
            f" [tolerated known-open nets: {', '.join(name for name, _ in tolerated)}]"
            if tolerated
            else ""
        )
        graduate = (
            f" NOTE: known-open net(s) {', '.join(no_longer_open)} are no longer "
            f"open -- consider tightening --known-open-nets."
            if no_longer_open
            else ""
        )
        return (
            0,
            f"OK: {pcb_path} -- {gated_pad_count} unconnected pad(s) on gated nets "
            f"(threshold <= {max_unconnected}).{offenders}{graduate}",
        )

    offending_names = ", ".join(name for name, _ in gated)
    return (
        2,
        f"Floating-pad regression: {gated_pad_count} unconnected pad(s) on "
        f"{len(gated)} net(s) ({offending_names}) exceeds --max-unconnected="
        f"{max_unconnected}. This is the exact class of break #4531 gates: a pad "
        f"with no track and no reachable pour that passes DRC/LVS/router "
        f"'N/N nets' but is electrically floating. NOTE: this gate uses the RAW "
        f"total_unconnected_pads (NOT blocking_incomplete_count, which "
        f"name-excludes power/plane nets -- the very bug class here). Either fix "
        f"the routing (kct net-status names the pad) or, if a NEW net is a "
        f"legitimate designed open, add it to --known-open-nets with reviewer "
        f"sign-off.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_net_status",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "pcb",
        help="Path to the routed .kicad_pcb file to check.",
    )
    parser.add_argument(
        "--max-unconnected",
        type=int,
        default=DEFAULT_MAX_UNCONNECTED,
        help=(
            "Maximum allowed unconnected pads on nets NOT in --known-open-nets "
            f"before the gate fails (default: {DEFAULT_MAX_UNCONNECTED}). Uses "
            "the RAW NetStatusResult.total_unconnected_pads, NOT "
            "blocking_incomplete_count (which name-excludes power/plane nets -- "
            "the #4531 bug class)."
        ),
    )
    parser.add_argument(
        "--known-open-nets",
        default=None,
        metavar="NET[,NET...]",
        help=(
            "Comma-separated net names permitted to carry unconnected pads "
            "(e.g. board 07's 5 seed-invariant #3438 opens). Their pads are "
            "excluded from the gated count, but an unconnected pad on any OTHER "
            "net still fails -- so a different net silently going open is still "
            "caught."
        ),
    )
    args = parser.parse_args(argv)

    if args.max_unconnected < 0:
        print("::error::--max-unconnected must be a non-negative integer", flush=True)
        return 1

    known_open_nets: set[str] = set()
    if args.known_open_nets:
        known_open_nets = {n.strip() for n in args.known_open_nets.split(",") if n.strip()}
        if not known_open_nets:
            print("::error::--known-open-nets was given an empty net list", flush=True)
            return 1

    pcb_path = Path(args.pcb)
    exit_code, message = check_pcb(pcb_path, args.max_unconnected, known_open_nets)

    if exit_code == 0:
        print(message, flush=True)
    else:
        annotate_error(str(pcb_path), message)
        print(
            f"\nGate failed (exit {exit_code}). See ::error:: annotation above.",
            flush=True,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
