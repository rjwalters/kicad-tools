"""Shared native (kicad-cli) geometric DRC reconciliation.

kct has two internal DRC entry points -- ``kct audit``
(:class:`kicad_tools.audit.auditor.DesignAuditor`) and the ``kct route``
post-route gate (:func:`kicad_tools.cli.route_cmd.run_post_route_drc`).
Both must reconcile their internal verdict against KiCad's own
``kicad-cli pcb drc`` so that a clean *internal* verdict can never
overstate cleanliness when KiCad finds geometric defects the internal
engine is structurally blind to (shorts, ``copper_edge_clearance``,
``solder_mask_bridge``, ``silk_*`` overlaps, ...).

Previously only ``kct audit`` did this (issue #3721); the route gate did
not, so ``kct route`` could print ``DRC PASSED`` while ``kicad-cli pcb
drc`` reported 400+ violations including real shorts (issue #3803).

This module factors that reconciliation into a single shared helper,
:func:`run_geometric_drc`, so the two entry points cannot drift again.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.drc.violation import DRCViolation

logger = logging.getLogger(__name__)

__all__ = [
    "FAILED_REASONS",
    "KICAD_CLI_DRC_OK_EXIT_CODES",
    "KICAD_CLI_ERR_ARGS",
    "GeometricDRCResult",
    "SavedFillCheck",
    "check_saved_fill",
    "describe_returncode",
    "load_kicad_cli_json_report",
    "run_geometric_drc",
    "saved_fill_regressions",
]


# Machine-readable skip reasons (Issue #4178).  ``--strict-drc`` uses these
# to emit an actionable message distinguishing WHY the native DRC did not run.
REASON_OK = "ok"  # kicad-cli ran and produced a report
REASON_ABSENT = "kicad_cli_absent"  # kicad-cli not found on PATH
REASON_TIMEOUT = "kicad_cli_timeout"  # kicad-cli exceeded the timeout budget
REASON_NO_REPORT = "kicad_cli_no_report"  # kicad-cli ran but wrote no report
REASON_CRASH = "kicad_cli_crash"  # kicad-cli raised/crashed/exited non-zero
# kicad-cli rejected its command line (exit 1, ``EXIT_CODES::ERR_ARGS``): an
# option kct passed is not supported by this kicad-cli version (Issue #6724).
REASON_UNSUPPORTED = "kicad_cli_unsupported_option"

#: ``kicad-cli``'s argument/usage-error exit status (``EXIT_CODES::ERR_ARGS``).
#: KiCad 9's ``pcb drc`` returns it, prints ``Unknown argument: --refill-zones``
#: plus its usage text on stdout, and writes no report.
KICAD_CLI_ERR_ARGS = 1

#: ``kicad-cli pcb drc`` exit codes that can accompany a complete report:
#: 0, and 5 (``EXIT_CODES::ERR_RC_VIOLATIONS``, only with
#: ``--exit-code-violations``).  Anything else -- 3 for a board that failed to
#: load, a negative status for a signal kill -- means the board was not
#: checked (Issue #6724).
KICAD_CLI_DRC_OK_EXIT_CODES: frozenset[int] = frozenset({0, 5})

#: Reasons meaning kicad-cli was found and started but did not produce a
#: usable report (Issue #6724).  Unlike :data:`REASON_ABSENT` (a KiCad-less
#: environment, where callers may soft-degrade), these are failures of the
#: check itself: the board was not judged and must not read as clean.
FAILED_REASONS: frozenset[str] = frozenset({REASON_CRASH, REASON_NO_REPORT, REASON_UNSUPPORTED})


def describe_returncode(returncode: int) -> str:
    """Human-readable description of a non-zero kicad-cli exit status.

    A negative ``returncode`` means the process was killed by that signal
    (``-9`` is SIGKILL, which is what the Linux OOM killer sends).
    """
    if returncode < 0:
        import signal

        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        hint = " (out of memory?)" if returncode == -signal.SIGKILL else ""
        return f"kicad-cli was killed by {name}{hint}"
    if returncode == KICAD_CLI_ERR_ARGS:
        return (
            "kicad-cli rejected its arguments (exit 1, usage error): this kicad-cli "
            "does not support an option kct passed; a newer KiCad may be required"
        )
    return f"kicad-cli exited with status {returncode}"


def unknown_argument(*streams: object) -> str | None:
    """The option kicad-cli reported as ``Unknown argument: <opt>``, if any.

    kicad-cli prints that line (on stdout) when it rejects an option it does
    not support, e.g. KiCad 9 given ``--refill-zones`` (Issue #6724).
    """
    import re

    for stream in streams:
        if isinstance(stream, bytes):
            stream = stream.decode("utf-8", errors="replace")
        if not isinstance(stream, str):
            continue
        match = re.search(r"Unknown argument:\s*(\S+)", stream)
        if match:
            return match.group(1)
    return None


def load_kicad_cli_json_report(report_path: Path) -> str | None:
    """Return why ``report_path`` is not a complete kicad-cli JSON DRC report.

    ``None`` means the report looks complete: it exists, is non-empty, parses
    as a JSON object and has a ``violations`` list (kicad-cli always writes
    one).  Anything else -- missing, zero bytes, truncated mid-write, not an
    object -- returns a short reason string (Issue #6724).  A killed kicad-cli
    must never be read as an empty, clean report.
    """
    import json

    try:
        if not report_path.is_file():
            return "kicad-cli wrote no DRC report"
        content = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"kicad-cli DRC report unreadable: {exc}"
    if not content.strip():
        return "kicad-cli wrote an empty DRC report"
    try:
        data = json.loads(content)
    except ValueError:
        return "kicad-cli DRC report is not valid JSON (truncated?)"
    if not isinstance(data, dict) or not isinstance(data.get("violations"), list):
        return "kicad-cli DRC report has no violations list (not a DRC report)"
    return None


@dataclass
class GeometricDRCResult:
    """Outcome of a native ``kicad-cli pcb drc`` reconciliation run.

    Attributes:
        ran: ``True`` when kicad-cli actually executed and produced a
            report.  ``False`` for every skip path (kicad-cli absent,
            timeout, no report, crash) -- callers MUST treat a
            ``ran=False`` result as "geometric DRC not performed" and
            never as a clean PASS.
        error_count: Number of error-severity geometric violations found
            by kicad-cli.  ``0`` when ``ran`` is ``False``.
        by_type: ``{kicad-cli type_str: count}`` for the error-severity
            violations (unnamespaced; callers that need a namespace add
            their own prefix).
        all_by_type: ``{kicad-cli type_str: count}`` across all severities.
            This includes warning-only checks such as ``copper_sliver`` while
            preserving ``by_type``'s error-only contract for existing gates.
        note: A human-readable note describing the outcome -- always set
            for skip paths (e.g. the "kicad-cli not found" fallback) and
            ``None`` on a clean successful run.
        reason: A machine-readable outcome code (one of ``REASON_*``)
            distinguishing "kicad-cli not found" from "kicad-cli timed
            out" from "kicad-cli crashed" so ``kct route --strict-drc``
            can emit an actionable failure message (Issue #4178).
        unconnected_items: kicad-cli's ``unconnected_items`` array (the
            ratsnest links KiCad's own connectivity, after the zone refill,
            still reports missing), parsed into
            :class:`~kicad_tools.drc.violation.DRCViolation` records.  Kept
            out of ``error_count`` / ``by_type`` on purpose (issue #4498:
            those stay a geometric-only verdict).  Issue #5785 reads it as
            the completion oracle for pour nets.  Empty when ``ran`` is
            ``False``.
        error_violations: The error-severity violations behind
            ``error_count``, so a caller can locate a regression (Issue
            #5785 attributes one to the copper that caused it).
    """

    ran: bool = False
    error_count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    all_by_type: dict[str, int] = field(default_factory=dict)
    note: str | None = None
    reason: str = REASON_OK
    unconnected_items: list[DRCViolation] = field(default_factory=list)
    error_violations: list[DRCViolation] = field(default_factory=list)

    @property
    def unconnected_count(self) -> int:
        """Number of kicad-cli ``unconnected_items`` (0 when ``ran`` is False)."""
        return len(self.unconnected_items)

    @property
    def failed(self) -> bool:
        """``True`` when kicad-cli was found but crashed, was killed, exited
        non-zero or left no usable report (Issue #6724).

        Distinct from a KiCad-less skip (:data:`REASON_ABSENT`): the board
        was meant to be judged and was not, so callers must treat it as an
        error rather than degrading to an internal-only verdict.
        """
        return not self.ran and self.reason in FAILED_REASONS

    @property
    def has_errors(self) -> bool:
        """``True`` when kicad-cli ran and found >=1 error-severity violation."""
        return self.ran and self.error_count > 0

    def top_types(self, n: int = 3) -> list[tuple[str, int]]:
        """Return the ``n`` most frequent violation types (desc by count)."""
        return sorted(self.by_type.items(), key=lambda x: -x[1])[:n]


# Reuse the exact fallback wording from auditor.py so audit + route stay
# consistent for KiCad-less environments.
KICAD_CLI_ABSENT_NOTE = "kicad-cli not found; geometric DRC skipped"


def run_geometric_drc(
    pcb_path: Path | str,
    *,
    timeout: int = 120,
    kicad_cli: Path | None = None,
    refill_zones: bool = True,
    all_track_errors: bool = False,
) -> GeometricDRCResult:
    """Run ``kicad-cli pcb drc`` and summarize findings by severity.

    kicad-cli loads the sibling ``<board>.kicad_pro`` emitted by ``kct
    export`` (issue #3720), so error-severity findings are checked against
    the manufacturer's fab-accurate rules with ``lib_footprint_mismatch``
    / ``isolated_copper`` already downgraded below error severity.

    Issue #3969: the invocation passes ``--refill-zones`` so kicad-cli
    re-fills the copper pours from scratch *before* evaluating clearance,
    exactly as a fab-house DRC (and ``kicad-cli pcb drc --refill-zones``,
    the acceptance command boards are gated against) does.  Without it,
    kicad-cli judges the *stale* zone-fill polygons persisted in the
    ``.kicad_pcb``; those can differ from a fresh fill by sub-micron
    rounding and manufacture a phantom ``clearance`` violation (board 03
    reported ``zone clearance 0.3000 mm; actual 0.2996 mm`` -- a 0.4 µm
    gap that vanishes on refill).  This phantom was latent in the
    committed board too and only surfaced once PR #3950 wired this
    cross-check into the post-route gate; refilling makes both engines and
    the shipped manufacturing bundle judge the same geometry.

    This never raises: every failure mode (kicad-cli absent, timeout, no
    report, parse error) returns a :class:`GeometricDRCResult` with
    ``ran=False`` and an explanatory ``note`` so callers can degrade
    gracefully without ever silently overstating cleanliness.

    Issue #6724: a kicad-cli killed by a signal (e.g. the OOM killer) or
    exiting non-zero returns ``reason=REASON_CRASH``; a missing, empty,
    truncated or non-DRC report returns ``reason=REASON_NO_REPORT``.  Both
    set :attr:`GeometricDRCResult.failed`.  The report file is no longer
    pre-created, so an empty file can never pass for a clean run.

    ``--refill-zones`` only exists from KiCad 10, so it is passed only when
    the kicad-cli advertises it (``_kicad_drc_supports_refill``, memoised).
    KiCad 8/9 run the check without it instead of rejecting the command line.
    A command line kicad-cli does reject (exit 1) returns
    ``reason=REASON_UNSUPPORTED`` naming the option, never a crash.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file to check.
        timeout: Seconds before the kicad-cli invocation is abandoned.
        kicad_cli: Explicit kicad-cli path (auto-detected when ``None``).
        refill_zones: Recompute zones before checking by default. False checks
            the saved copper exactly, for deliberately preserved fixed fills.
            This does not attest to what a later full refill would produce.
            Ignored (no ``--refill-zones``) on a kicad-cli older than KiCad
            10, which does not have the option.
        all_track_errors: Pass ``--all-track-errors`` so kicad-cli reports
            every violation on a track instead of only the first one per
            track (issue #6420).  Off by default: the production gates that
            call this keep their existing invocation; research harnesses
            that count violations opt in.

    Returns:
        A :class:`GeometricDRCResult` summarizing the run.
    """
    from kicad_tools.cli.runner import find_kicad_cli

    pcb_path = Path(pcb_path)

    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        from kicad_tools.cli.runner import last_kicad_cli_lookup

        lookup = last_kicad_cli_lookup()
        if lookup is not None and lookup.probe_failed:
            # Installed but unresponsive (#5932): say so instead of "not found".
            note = f"{lookup.reason}; geometric DRC skipped"
            return GeometricDRCResult(ran=False, note=note, reason=REASON_ABSENT)
        return GeometricDRCResult(ran=False, note=KICAD_CLI_ABSENT_NOTE, reason=REASON_ABSENT)

    # Issue #6724: ``--refill-zones`` only exists from KiCad 10.  KiCad 8/9's
    # ``pcb drc`` rejects it (exit 1, usage text, no report), so passing it
    # unconditionally meant the geometric DRC never ran there.  Pass it only
    # when this kicad-cli advertises it (memoised probe).
    from kicad_tools.cli.runner import _kicad_drc_supports_refill

    use_refill = refill_zones and _kicad_drc_supports_refill(Path(kicad_cli))
    if refill_zones and not use_refill:
        logger.info(
            "kicad-cli %s has no --refill-zones; running geometric DRC without it", kicad_cli
        )

    # Issue #6724: never pre-create the report.  A pre-created empty file
    # survived a killed kicad-cli and parsed as a clean, zero-violation
    # report.  kicad-cli writes into a private directory instead, so the file
    # exists only if kicad-cli created it.
    try:
        with tempfile.TemporaryDirectory(prefix="kct-geodrc-") as td:
            report_path = Path(td) / "drc.json"
            cmd = [
                str(kicad_cli),
                "pcb",
                "drc",
                *(["--refill-zones"] if use_refill else []),
                *(["--all-track-errors"] if all_track_errors else []),
                "--format",
                "json",
                # Request all severities so warning-only native checks such as
                # copper_sliver can be reconciled.  ``by_type`` below remains
                # error-only for backward compatibility with existing gates.
                "--severity-all",
                "--units",
                "mm",
                "--output",
                str(report_path),
                str(pcb_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)

            # Issue #6724: without ``--exit-code-violations`` kicad-cli exits 0
            # whether or not it finds violations (verified on 10.0.6: a board
            # with 36 unconnected items exits 0; a load failure exits 3; the
            # OOM killer gives -9).  So any non-zero status is a failure,
            # except kicad-cli's documented "violations found" code, which
            # still needs a complete report below.
            returncode = getattr(proc, "returncode", 0)
            if isinstance(returncode, int) and returncode not in KICAD_CLI_DRC_OK_EXIT_CODES:
                reason = REASON_CRASH
                detail = describe_returncode(returncode)
                if returncode == KICAD_CLI_ERR_ARGS:
                    # A usage error, not a crash: name the rejected option.
                    reason = REASON_UNSUPPORTED
                    option = unknown_argument(
                        getattr(proc, "stdout", None), getattr(proc, "stderr", None)
                    )
                    if option:
                        detail = (
                            f"this kicad-cli does not support {option} (exit 1, usage "
                            "error); a newer KiCad version is required for it"
                        )
                else:
                    stderr = _stderr_tail(getattr(proc, "stderr", None))
                    if stderr:
                        detail = f"{detail}: {stderr}"
                logger.warning("Geometric DRC (kicad-cli) failed: %s", detail)
                return GeometricDRCResult(
                    ran=False,
                    note=f"{detail} (geometric DRC NOT performed)",
                    reason=reason,
                )

            problem = load_kicad_cli_json_report(report_path)
            if problem is not None:
                logger.warning("Geometric DRC (kicad-cli) failed: %s", problem)
                return GeometricDRCResult(
                    ran=False,
                    note=f"{problem} (geometric DRC NOT performed)",
                    reason=REASON_NO_REPORT,
                )

            from kicad_tools.drc import DRCReport

            report = DRCReport.load(report_path)

        cli_errors = [v for v in report.violations if v.is_error]

        by_type: dict[str, int] = {}
        for v in cli_errors:
            by_type[v.type_str] = by_type.get(v.type_str, 0) + 1

        all_by_type: dict[str, int] = {}
        for v in report.violations:
            all_by_type[v.type_str] = all_by_type.get(v.type_str, 0) + 1

        return GeometricDRCResult(
            ran=True,
            error_count=len(cli_errors),
            by_type=by_type,
            all_by_type=all_by_type,
            note=None,
            reason=REASON_OK,
            unconnected_items=list(report.unconnected_items),
            error_violations=cli_errors,
        )
    except subprocess.TimeoutExpired:
        return GeometricDRCResult(
            ran=False,
            note="kicad-cli DRC timed out (geometric DRC skipped)",
            reason=REASON_TIMEOUT,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Geometric DRC (kicad-cli) failed: %s", e)
        return GeometricDRCResult(ran=False, note=f"geometric DRC failed: {e}", reason=REASON_CRASH)


def _stderr_tail(stderr: object, limit: int = 300) -> str:
    """The last non-empty line(s) of kicad-cli's stderr, for a failure note."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not isinstance(stderr, str):
        return ""
    text = " ".join(line.strip() for line in stderr.strip().splitlines()[-3:] if line.strip())
    return text[-limit:]


# kicad-cli finding types that show a saved zone fill whose copper is split
# apart (Issue #6078).  A fresh fill from KiCad never produces more of these
# than the board's routing already implies.  So when the SAVED fill has more
# of them than a ``--refill-zones`` run, the extra findings come from the
# saved copper itself.
SAVED_FILL_CONNECTIVITY_TYPES: tuple[str, ...] = ("isolated_copper", "copper_sliver")


def saved_fill_regressions(saved: GeometricDRCResult, refilled: GeometricDRCResult) -> list[str]:
    """Explain where the SAVED fill is worse-connected than a fresh fill.

    ``saved`` is ``kicad-cli pcb drc`` *without* ``--refill-zones``: it reads
    the copper stored in the board, which ``kicad-cli pcb export gerbers``
    also plots without refilling.  ``refilled`` is the same board with
    ``--refill-zones``, the view every refill-based gate checks.  Issue
    #6078: a post-fill edit to the saved pours (the #3711 clearance carve)
    split board 03's F.Cu GND pour into 16 pieces.  The refill-based gates
    reported 1 GND link, but the copper sent to the fab had 17.

    Returns one human-readable line per regressed measure (unconnected items,
    ``isolated_copper``, ``copper_sliver``).  Returns ``[]`` when either run
    did not happen, because a check that was not performed has no verdict.
    """
    if not (saved.ran and refilled.ran):
        return []
    out: list[str] = []
    if saved.unconnected_count > refilled.unconnected_count:
        out.append(
            f"unconnected_items: {saved.unconnected_count} on the saved fill vs "
            f"{refilled.unconnected_count} after a refill"
        )
    for type_str in SAVED_FILL_CONNECTIVITY_TYPES:
        n_saved = saved.all_by_type.get(type_str, 0)
        n_refilled = refilled.all_by_type.get(type_str, 0)
        if n_saved > n_refilled:
            out.append(f"{type_str}: {n_saved} on the saved fill vs {n_refilled} after a refill")
    return out


@dataclass
class SavedFillCheck:
    """Verdict of :func:`check_saved_fill` (Issue #6078).

    Attributes:
        ran: ``True`` when every DRC run the verdict needs actually ran.
        regressions: :func:`saved_fill_regressions` lines; empty when clean.
        note: Why the check did not run (``ran`` is ``False``).
        failed: ``True`` when a DRC run it needed was attempted but kicad-cli
            crashed, was killed or left no usable report (Issue #6724), as
            opposed to kicad-cli being absent.
    """

    ran: bool
    regressions: list[str] = field(default_factory=list)
    note: str | None = None
    failed: bool = False

    @property
    def ok(self) -> bool:
        """``True`` only when the check ran and found no regression."""
        return self.ran and not self.regressions


def check_saved_fill(
    pcb_path: Path | str,
    *,
    refilled: GeometricDRCResult | None = None,
    timeout: int = 120,
    kicad_cli: Path | None = None,
) -> SavedFillCheck:
    """Check that the zone copper SAVED in ``pcb_path`` is as connected as a refill.

    Gerber export plots the saved fill (it does not pass ``--check-zones``),
    while ``kicad-cli pcb drc --refill-zones`` checks a fresh one.  This
    check makes the difference visible (Issue #6078).  It runs kicad-cli DRC
    without refill and compares the result with the refilled run.  Pass
    ``refilled`` if you already have that run, so it is not repeated.

    Fast path: if the saved run shows no unconnected items and none of the
    :data:`SAVED_FILL_CONNECTIVITY_TYPES`, nothing can have regressed, and
    the refill run is skipped.  The board file is never modified: neither
    run passes ``--save-board``.
    """
    saved = run_geometric_drc(pcb_path, timeout=timeout, kicad_cli=kicad_cli, refill_zones=False)
    if not saved.ran:
        return SavedFillCheck(ran=False, note=saved.note, failed=saved.failed)
    if refilled is None:
        if saved.unconnected_count == 0 and not any(
            saved.all_by_type.get(t, 0) for t in SAVED_FILL_CONNECTIVITY_TYPES
        ):
            return SavedFillCheck(ran=True)
        refilled = run_geometric_drc(pcb_path, timeout=timeout, kicad_cli=kicad_cli)
    if not refilled.ran:
        return SavedFillCheck(ran=False, note=refilled.note, failed=refilled.failed)
    return SavedFillCheck(ran=True, regressions=saved_fill_regressions(saved, refilled))
