#!/usr/bin/env python3
"""Routed-PCB DRC gate for CI (issue #2546).

Runs ``kct check <file> --mfr <mfr> --errors-only --format json`` against
each PCB path passed on the command line, and compares the resulting error
count against a per-board tolerance allowlist (``.github/routed-drc-tolerance.yml``).

By default the gate uses ``--mfr jlcpcb`` (the strictest tier most boards
target).  A board whose design intentionally requires a different
manufacturer profile (e.g. board-04 routes with micro-vias under
``jlcpcb-tier1``'s Capability-Plus process) can override the profile by
adding an entry under the optional ``manufacturers:`` top-level mapping in
the same YAML file.  See ``.github/routed-drc-tolerance.yml`` for the schema.

A file fails the gate if its actual error count exceeds the allowed value;
files not listed in the allowlist must report 0 errors. This implements the
"allowed minus epsilon" semantic: regressions (count going UP) are caught,
even on boards that are grandfathered in with non-zero counts.

Advisory-rule classification (issue #3074):
    The gate's verdict mirrors the audit pipeline's classification
    (``src/kicad_tools/audit/auditor.py``).  Rules registered in
    ``DRCChecker.ADVISORY_RULE_IDS`` (currently just ``connectivity``) are
    surfaced in the printed report but excluded from the count that gates
    the build.  This keeps the gate's verdict aligned with what blocks
    manufacturability per ``ManufacturingAudit._check_drc`` -- the
    standalone ``kct check`` CLI still reports the unfiltered count, so a
    PR may see ``kct check`` report N+1 errors while CI counts N.

Exit codes:
    0 -- All inputs within tolerance (job passes).
    1 -- Tool failure (allowlist parse error, kct check crash, etc.).
    2 -- One or more inputs exceed their tolerance (job fails).

GitHub-Actions annotations (``::error file=...::``) are emitted to stdout for
each offending file so the PR Files-changed view surfaces the failure inline.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# The CI gate mirrors the audit pipeline's advisory-rule classification
# (``src/kicad_tools/audit/auditor.py``) so any rule classified as advisory
# by :class:`DRCChecker.ADVISORY_RULE_IDS` does NOT block the gate.  Today
# that is just ``connectivity`` (partial-route reports surface in the
# diagnostic output but do not gate manufacturability).  The classifier
# import now lives inside :func:`net_class_map_resolver.count_blocking_errors`
# (Issue #4008 hoisted the shared counter), so this gate no longer imports
# ``DRCChecker`` directly.
#
# Issue #3151: the strict error-count gate must count the diff-pair and
# match-group rule families, which only fire when ``kct check`` is given a
# ``--net-class-map`` sidecar (the rules no-op without one -- the documented
# graceful-degradation contract).  ``net_class_map_resolver`` lives next to
# this script in ``scripts/ci``; add that directory to ``sys.path`` so the
# import works whether the gate is launched as ``scripts/ci/check_routed_drc.py``
# or imported as a module by the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from net_class_map_resolver import (  # noqa: E402
    count_blocking_errors,
    resolve_net_class_map_sidecar,
)

DEFAULT_ALLOWLIST = Path(".github/routed-drc-tolerance.yml")
DEFAULT_MANUFACTURER = "jlcpcb"


def load_allowlist(allowlist_path: Path) -> dict[str, int]:
    """Load and validate the per-board tolerance allowlist.

    Args:
        allowlist_path: Path to the YAML file. Missing file is treated as
            an empty allowlist (every board must report 0 errors).

    Returns:
        Mapping of repo-relative PCB path -> max allowed error count.

    Raises:
        ValueError: If the file exists but is malformed.
    """
    if not allowlist_path.exists():
        return {}

    try:
        data = yaml.safe_load(allowlist_path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed allowlist YAML at {allowlist_path}: {e}") from e

    if data is None:
        return {}

    if not isinstance(data, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )

    tolerances = data.get("tolerances", {})
    if not isinstance(tolerances, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} 'tolerances' field must be a mapping, "
            f"got {type(tolerances).__name__}"
        )

    result: dict[str, int] = {}
    for key, value in tolerances.items():
        if not isinstance(key, str):
            raise ValueError(f"Allowlist {allowlist_path}: key {key!r} must be a string path")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                f"Allowlist {allowlist_path}: value for {key!r} must be a "
                f"non-negative integer, got {value!r}"
            )
        result[key] = value

    return result


def load_manufacturers(allowlist_path: Path) -> dict[str, str]:
    """Load optional per-board manufacturer overrides from the allowlist YAML.

    Some boards intentionally target a non-default manufacturer profile
    (e.g. board-04 routes with micro-vias that are only legal under
    ``jlcpcb-tier1`` / Capability-Plus, even though most boards target the
    stricter ``jlcpcb`` standard tier).  Reading the per-board profile from
    the same YAML keeps the CI gate aligned with what each board's
    ``generate_design.py`` actually produces.

    The schema is intentionally a separate top-level ``manufacturers:``
    mapping (not merged into the ``tolerances:`` entries) so the original
    ``load_allowlist`` API and its tests stay backward-compatible.

    Args:
        allowlist_path: Path to the YAML file.  Missing file or absent
            ``manufacturers:`` key both return an empty mapping (the
            default profile applies to every board).

    Returns:
        Mapping of repo-relative PCB path -> manufacturer-profile name to
        pass via ``--mfr``.  Boards not listed fall back to
        ``DEFAULT_MANUFACTURER``.

    Raises:
        ValueError: If the file exists but is malformed at the
            ``manufacturers:`` key.
    """
    if not allowlist_path.exists():
        return {}

    try:
        data = yaml.safe_load(allowlist_path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed allowlist YAML at {allowlist_path}: {e}") from e

    if data is None or not isinstance(data, dict):
        return {}

    manufacturers = data.get("manufacturers", {})
    if not isinstance(manufacturers, dict):
        raise ValueError(
            f"Allowlist {allowlist_path} 'manufacturers' field must be a mapping, "
            f"got {type(manufacturers).__name__}"
        )

    result: dict[str, str] = {}
    for key, value in manufacturers.items():
        if not isinstance(key, str):
            raise ValueError(
                f"Allowlist {allowlist_path}: manufacturers key {key!r} must be a string path"
            )
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Allowlist {allowlist_path}: manufacturers value for {key!r} "
                f"must be a non-empty string profile name, got {value!r}"
            )
        result[key] = value

    return result


def _count_blocking_errors(data: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """Filter advisory rules out of a ``kct check --format json`` payload.

    Issue #4008: the implementation now lives in
    :func:`net_class_map_resolver.count_blocking_errors` so this gate and
    ``check_matchgroup_coverage.py`` share ONE blocking-vs-advisory counter.
    This thin wrapper is retained for the module's existing callers and unit
    tests; it delegates verbatim to the shared helper.

    See :func:`net_class_map_resolver.count_blocking_errors` for the full
    contract.
    """
    return count_blocking_errors(data)


def count_errors(pcb_path: Path, mfr: str = DEFAULT_MANUFACTURER) -> tuple[int, dict[str, int]]:
    """Run ``kct check`` and return the (blocking) error count.

    Advisory-rule violations (per :attr:`DRCChecker.ADVISORY_RULE_IDS`,
    currently ``connectivity``) are excluded from the returned count so the
    gate's verdict matches the audit pipeline's blocking-vs-advisory
    classification.  Advisory findings are returned separately so the gate
    can still surface them in diagnostic output without gating on them.

    Net-class-map awareness (Issue #3151):
        ``kct check``'s diff-pair (``diffpair_length_skew``,
        ``diffpair_routing_continuity``) and match-group
        (``match_group_length_skew``) rules re-derive their working state
        from a ``net_class_map`` and no-op when none is supplied -- the
        documented graceful-degradation contract for external-router
        boards.  ``generate_design.py``'s in-pipeline DRC counts those
        families because it passes the board's sidecar; the bare ``kct
        check`` this gate used to run did not, so the strict gate silently
        missed 3 rule families on routed boards.  This function now
        resolves a sidecar per board (committed ``net_class_map.json``
        preferred, in-process ``build_net_class_map`` fallback for boards
        like 06 that don't commit one) and threads it via ``--net-class-map``
        so the gate counts the same errors the pipeline does.  Boards with
        no derivable map (e.g. 01-05) run bare and the rules correctly
        no-op -- the standalone-CLI contract is untouched.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.
        mfr: Manufacturer-profile name to pass via ``--mfr`` (defaults to
            ``DEFAULT_MANUFACTURER`` = "jlcpcb").  Per-board overrides
            come from ``load_manufacturers``.

    Returns:
        Tuple ``(blocking_errors, advisory_by_rule)``.  ``blocking_errors``
        is what the gate compares to the per-board allowlist;
        ``advisory_by_rule`` maps each advisory ``rule_id`` to its
        error-severity count for diagnostic surfacing.

    Raises:
        RuntimeError: If kct check fails to run (exit code 1) or emits
            unparseable output.
    """
    cmd = [
        "uv",
        "run",
        "kct",
        "check",
        str(pcb_path),
        "--mfr",
        mfr,
        "--errors-only",
        "--format",
        "json",
    ]
    with resolve_net_class_map_sidecar(pcb_path) as sidecar:
        if sidecar is not None:
            cmd.extend(["--net-class-map", str(sidecar)])
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    # Exit 1 = tool-level failure (file not found, parse error). Exit 0 = no
    # errors. Exit 2 = errors found. Both 0 and 2 produce valid JSON on stdout.
    if proc.returncode == 1:
        raise RuntimeError(
            f"kct check failed on {pcb_path} (exit 1). stderr:\n{proc.stderr.strip()}"
        )

    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"kct check returned unexpected exit code {proc.returncode} on "
            f"{pcb_path}. stderr:\n{proc.stderr.strip()}"
        )

    # Defensive: ``kct check``'s advisory drift banner is now routed to stderr
    # (see ``_emit_drift_banner`` in ``check_cmd.py``), but historically it
    # printed to stdout ahead of the JSON body and broke this parser.  Strip
    # any leading non-``{`` lines so that an older ``kct`` (or a future
    # regression) cannot re-introduce the same CI flake.  The JSON document
    # ``kct check --format json`` emits is always a single top-level object,
    # so the first ``{`` reliably marks the payload start.
    raw_stdout = proc.stdout
    first_brace = raw_stdout.find("{")
    json_stdout = raw_stdout[first_brace:] if first_brace > 0 else raw_stdout
    try:
        data: dict[str, Any] = json.loads(json_stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"kct check produced invalid JSON on {pcb_path}: {e}\n"
            f"stdout (first 500 chars):\n{raw_stdout[:500]}"
        ) from e

    try:
        return _count_blocking_errors(data)
    except RuntimeError as e:
        raise RuntimeError(f"{e} (source: {pcb_path})") from e


def annotate_error(file: str, message: str) -> None:
    """Emit a GitHub-Actions ``::error file=...::`` annotation."""
    # The %0A escape is not strictly required for single-line messages; we
    # keep the message on one line so it surfaces cleanly in the Files-changed
    # view.
    print(f"::error file={file}::{message}", flush=True)


def annotate_drift_warning(file: str, errors: int, allowed: int) -> None:
    """Emit a GitHub-Actions ``::warning file=...::`` for a stale allowlist entry.

    Called when a routed PCB's actual error count is strictly less than the
    allowlist value (slack > 0). The warning surfaces in the PR Files-changed
    view alongside ``::error::`` annotations so reviewers don't miss it
    (issue #2590).

    Args:
        file: Repo-relative path to the routed PCB (used as the annotation's
            ``file=`` target so GitHub anchors the warning to the file).
        errors: Actual error count returned by ``kct check``.
        allowed: Current allowlist value from
            ``.github/routed-drc-tolerance.yml``.

    Note:
        TODO(#2590): Cross-PR drift detection (i.e., warning on stale entries
        for files NOT touched in the current PR) is deferred to a future
        scheduled-audit job. v1 only inspects files in the diff so the
        warning attaches to a file the reviewer is actively looking at, and
        we don't pay the per-board ``kct check`` cost for every entry on
        every PR. See the issue's "Scope question" section for the rationale.
    """
    slack = allowed - errors
    print(
        f"::warning file={file}::Allowlist for `{file}` is {allowed} but "
        f"actual is {errors} (slack={slack}). Tighten to {errors} in this "
        f"PR or a follow-up to lock in the new floor (see "
        f".github/routed-drc-tolerance.yml).",
        flush=True,
    )


def _format_advisory_suffix(advisory_by_rule: dict[str, int]) -> str:
    """Render an advisory-rule summary for log messages.

    Per issue #3074: the gate excludes advisory rules from its verdict but
    must still surface them in the printed report so reviewers can see the
    connectivity findings.  Returns an empty string when no advisory
    violations are present so the common case (no advisories) leaves the
    OK/FAIL message unchanged.
    """
    if not advisory_by_rule:
        return ""
    parts = ", ".join(f"{rule_id}={count}" for rule_id, count in sorted(advisory_by_rule.items()))
    return f" [advisory (excluded from gate): {parts}]"


def check_file(
    pcb_path: Path, allowed: int, mfr: str = DEFAULT_MANUFACTURER
) -> tuple[bool, str, int]:
    """Check a single PCB against its allowed error count.

    The gate's verdict excludes advisory rules (see
    :attr:`DRCChecker.ADVISORY_RULE_IDS`, currently ``connectivity``) so
    advisory findings do not block CI even when they appear in the per-
    violation list emitted by ``kct check --format json``.  This mirrors
    the audit pipeline's blocking-vs-advisory classification.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.
        allowed: Maximum (blocking) error count this PCB may report before
            failing.  Advisory-rule errors are excluded from the count
            being compared, so a board listed at ``allowed=4`` with
            ``4 blocking + N connectivity`` passes regardless of ``N``.
        mfr: Manufacturer-profile name to pass via ``--mfr``.

    Returns:
        ``(passed, message, errors)`` tuple. ``passed`` is True if
        ``errors <= allowed`` (advisory-filtered count).  ``message`` is a
        human-readable summary suitable for both stdout and GitHub
        annotation; when advisory violations are present it includes a
        ``[advisory (excluded from gate): ...]`` suffix so reviewers see
        them.  ``errors`` is the blocking-only count so callers compute
        drift slack against the gate's actual floor.
    """
    errors, advisory_by_rule = count_errors(pcb_path, mfr=mfr)
    advisory_suffix = _format_advisory_suffix(advisory_by_rule)
    if errors <= allowed:
        if allowed == 0:
            msg = f"OK: {pcb_path} -- 0 errors (strict gate, --mfr {mfr}).{advisory_suffix}"
        else:
            msg = (
                f"OK: {pcb_path} -- {errors} errors (--mfr {mfr}, allowlist "
                f"max {allowed}; reduce the allowlist value in "
                f".github/routed-drc-tolerance.yml if this count drops further)."
                f"{advisory_suffix}"
            )
        return True, msg, errors

    if allowed == 0:
        msg = (
            f"DRC errors detected by `kct check --mfr {mfr} --errors-only`: "
            f"{errors} blocking error(s) (advisory rules excluded). Boards NOT "
            f"in .github/routed-drc-tolerance.yml must report 0 errors. Either "
            f"fix the routing or, if grandfathering is justified, add an "
            f"explicit allowlist entry with reviewer sign-off.{advisory_suffix}"
        )
    else:
        msg = (
            f"DRC regression: {errors} blocking error(s) (--mfr {mfr}, "
            f"advisory rules excluded) exceeds allowlist value {allowed} in "
            f".github/routed-drc-tolerance.yml. Either fix the new violations, "
            f"or (if intentional) raise the allowlist value with reviewer "
            f"sign-off.{advisory_suffix}"
        )
    return False, msg, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_routed_drc",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Paths to *_routed.kicad_pcb files to check. If empty, the gate "
        "is a no-op and exits 0.",
    )
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST),
        help=f"Path to the tolerance allowlist YAML (default: {DEFAULT_ALLOWLIST}).",
    )
    args = parser.parse_args(argv)

    if not args.files:
        print("No routed PCBs to check -- gate is a no-op.")
        return 0

    try:
        allowlist = load_allowlist(Path(args.allowlist))
        manufacturers = load_manufacturers(Path(args.allowlist))
    except ValueError as e:
        print(f"::error::{e}", flush=True)
        return 1

    overall_failed = 0
    for raw in args.files:
        pcb_path = Path(raw)
        if not pcb_path.is_file():
            # GitHub Actions runs the gate after `git diff` has already
            # reported these files as modified; a file that no longer exists
            # was deleted in the PR (legitimate). Skip with a warning.
            print(
                f"::warning file={pcb_path}::file not found on disk "
                f"(deleted in PR?) -- skipping DRC check",
                flush=True,
            )
            continue

        # Use the repo-relative form for allowlist lookup. The CI workflow
        # passes paths in repo-relative form already; tolerate absolute paths
        # by stripping the cwd if it matches.
        lookup_key = str(pcb_path)
        try:
            rel = pcb_path.resolve().relative_to(Path.cwd())
            lookup_key = str(rel)
        except ValueError:
            # Path is outside cwd (unlikely in CI); fall back to the raw form.
            pass

        allowed = allowlist.get(lookup_key, 0)
        mfr = manufacturers.get(lookup_key, DEFAULT_MANUFACTURER)

        try:
            passed, message, errors = check_file(pcb_path, allowed, mfr=mfr)
        except RuntimeError as e:
            annotate_error(str(pcb_path), f"kct check failed: {e}")
            overall_failed = 1
            continue

        if passed:
            print(message, flush=True)
            # Issue #2590: surface stale allowlist entries (slack > 0) as a
            # GitHub-Actions warning annotation so reviewers see them in the
            # PR Files-changed view, not buried in stdout. The ``allowed > 0``
            # guard avoids noise on the common case of an unlisted board with
            # 0 errors (allowed defaults to 0 -> slack=0 anyway, but be
            # explicit). The gate's exit code is unchanged: warnings are
            # advisory, matching the precedent at the deleted-file branch
            # above.
            if allowed > 0 and errors < allowed:
                annotate_drift_warning(lookup_key, errors, allowed)
        else:
            annotate_error(str(pcb_path), message)
            overall_failed = 2

    if overall_failed:
        print(
            f"\nGate failed (exit {overall_failed}). See ::error:: annotations "
            "above; offending files are also surfaced in the PR Files-changed "
            "view.",
            flush=True,
        )
    return overall_failed


if __name__ == "__main__":
    sys.exit(main())
