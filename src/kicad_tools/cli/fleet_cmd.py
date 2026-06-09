"""Fleet-wide PCB status CLI command.

Survey every board under a fleet root and report routing completion plus
manufacturing artifact readiness in a single shot. Designed to answer the
question "are all our boards manufacturing-ready?" without requiring agents or
humans to grep across multiple output directories.

Usage::

    kct fleet status
    kct fleet status --boards-dir boards/
    kct fleet status --format json
    kct fleet status --ship-only
    kct fleet status --include-stale
    kct fleet status --pattern '*_routed.kicad_pcb'

    kct fleet ship-ready                       # warn-only PASS/FAIL summary
    kct fleet ship-ready --strict              # exit 2 if any board fails
    kct fleet ship-ready --format json         # machine-readable
    kct fleet ship-ready --boards-dir boards/  # custom root

Exit Codes:
    0 - All surveyed boards are ship-ready (or warn-only mode regardless)
    1 - Argparse / IO error
    2 - ``status``: one or more boards are not ship-ready (default semantics,
        matches ``net-status``).
        ``ship-ready --strict``: one or more boards are not ship-ready.
        ``ship-ready`` (warn-only default): never returned -- always exit 0.

Warn-only semantics (``ship-ready`` default, per issue #3099 steering
decision 2026-05-21):
    The ``ship-ready`` subcommand exits 0 by default even when boards fail,
    so it can be wired into nightly CI as a non-blocking gate. Pass
    ``--strict`` to opt into non-zero exit semantics for human users.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.schema.pcb import canonicalize_power_nets

SCHEMA_VERSION = "1.1"

# Issue #3302: tolerate at most this many added-only nets (after
# power-rail alias normalisation) before flagging source drift.
#
# The kicad-tools schematic style routinely leaves short
# component-to-component nets unlabeled (e.g. ``LED_K`` between D1.K and
# R1, ``BOOT0`` between U2.44 and R2 on board 04). KiCad synthesises
# a net name during PCB sync, so the PCB-net set is a strict superset
# of the schematic-label set even when the design is in perfect sync.
# Without tolerance, every unlabeled local net becomes a false-positive
# drift signal.
#
# Bound at 2 (a conservative default chosen empirically from the
# in-repo board set): board 04 has exactly 2 such unlabeled local
# nets, while board 03's 3 real adds (``VBUS``, ``USB_CC1``,
# ``USB_CC2``) -- a genuine schematic-vs-PCB mismatch -- still
# trigger. Boards that grow more than two unlabeled local nets
# without re-routing will still surface the drift correctly.
_DRIFT_ADDED_ONLY_TOLERANCE = 2

# Default location for the per-board DRC tolerance allowlist (mirrors
# ``scripts/ci/check_routed_drc.py``). Boards listed here have a
# grandfathered non-zero error count; boards NOT listed must report 0.
_DRC_TOLERANCE_PATH = Path(".github/routed-drc-tolerance.yml")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RoutingStatus:
    """Routing completion details for a single board.

    ``incomplete_nets`` is the raw count (every net with one or more
    unconnected pads) and is preserved for diagnostic continuity. The
    ship-ready / ``routing_complete`` verdict uses
    ``blocking_incomplete_nets`` instead, which drops plane/pour
    stitching residuals that the audit pipeline already treats as
    advisory (``DRCChecker.ADVISORY_RULE_IDS = {"connectivity"}``).

    See ``scripts/ci/check_routed_drc.py:_count_blocking_errors`` for the
    reference filter that this field mirrors.
    """

    total_pads: int = 0
    connected_pads: int = 0
    total_nets: int = 0
    complete_nets: int = 0
    incomplete_nets: int = 0
    blocking_incomplete_nets: int = 0
    unrouted_nets: int = 0
    error: str | None = None
    # Issue #3280: when True, the routed PCB's net set disagrees with the
    # source schematic's named-label set, so any ``X/Y nets`` count derived
    # from the PCB is misleading as a current-routing signal. Filled in by
    # ``_survey_board`` via ``_detect_source_drift``.
    source_stale: bool = False
    # Distinct counts that produced the drift verdict above. ``None`` means
    # we could not extract them (no schematic alongside the PCB, parse
    # failure, etc.) -- treated as "not source-stale".
    schematic_net_count: int | None = None
    pcb_net_count: int | None = None
    # Sample of drifted nets (added/removed in PCB vs schematic), capped to
    # keep JSON output bounded. Empty when ``source_stale`` is False.
    drift_added: list[str] = field(default_factory=list)
    drift_removed: list[str] = field(default_factory=list)

    @property
    def completion_pct(self) -> float:
        if self.total_pads == 0:
            return 0.0
        return (self.connected_pads / self.total_pads) * 100.0

    @property
    def routing_complete(self) -> bool:
        if self.error is not None:
            return False
        # If the routed PCB is stale relative to its source schematic, its
        # routed-net count is not a trustworthy signal for "is the current
        # design routed?" -- treat as not complete (issue #3280).
        if self.source_stale:
            return False
        # A board is routing-complete iff every multi-pad net is fully
        # connected -- with one exception: plane/pour stitching residuals
        # (advisory connectivity per ``ADVISORY_RULE_IDS``) are excluded
        # because the CI gate at ``check_routed_drc`` already ignores
        # them. Single-pad nets are not counted by NetStatusAnalyzer.
        return (self.blocking_incomplete_nets + self.unrouted_nets) == 0 and self.total_nets > 0

    def to_dict(self) -> dict:
        data: dict = {
            "total_pads": self.total_pads,
            "connected_pads": self.connected_pads,
            "completion_pct": round(self.completion_pct, 2),
            "total_nets": self.total_nets,
            "complete_nets": self.complete_nets,
            "incomplete_nets": self.incomplete_nets,
            "blocking_incomplete_nets": self.blocking_incomplete_nets,
            "unrouted_nets": self.unrouted_nets,
            "routing_complete": self.routing_complete,
            "source_stale": self.source_stale,
            "schematic_net_count": self.schematic_net_count,
            "pcb_net_count": self.pcb_net_count,
        }
        if self.drift_added:
            data["drift_added"] = list(self.drift_added)
        if self.drift_removed:
            data["drift_removed"] = list(self.drift_removed)
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass
class ManufacturingStatus:
    """Manufacturing artifact presence/freshness for a single board."""

    dir_exists: bool = False
    has_gerbers: bool = False
    has_bom: bool = False
    has_cpl: bool = False
    has_manifest: bool = False
    manifest_mtime: float | None = None
    stale: bool = False

    @property
    def has_any(self) -> bool:
        return self.has_gerbers or self.has_bom or self.has_cpl or self.has_manifest

    @property
    def has_all(self) -> bool:
        return self.has_gerbers and self.has_bom and self.has_cpl and self.has_manifest

    def to_dict(self) -> dict:
        return {
            "dir_exists": self.dir_exists,
            "has_gerbers": self.has_gerbers,
            "has_bom": self.has_bom,
            "has_cpl": self.has_cpl,
            "has_manifest": self.has_manifest,
            "manifest_mtime": _iso_or_none(self.manifest_mtime),
            "stale": self.stale,
        }


@dataclass
class DRCStatus:
    """DRC report presence + error count for a single board.

    A board is DRC-clean when ``report_exists`` is True AND ``errors``
    does not exceed ``tolerance``. When ``report_exists`` is False the
    DRC step has not yet run for this board, so it must NOT block
    ship-ready (issue #2932 backwards-compat rule).

    Issue #3363: ``blocking_errors`` and ``advisory_errors_by_rule``
    split the raw ``errors`` count by ``DRCChecker.ADVISORY_RULE_IDS``
    so the fleet command can align its routing-completion verdict with
    the CI strict gate (``scripts/ci/check_routed_drc.py``). The CI gate
    treats ``connectivity`` rule violations -- which include both
    plane/pour stitching residuals AND signal nets the router refused
    (e.g. board 04's NRST after the post-#3286 clearance kernel closed
    the marginal U2.7/U2.8 corridor) -- as advisory. Without this split
    the fleet command would still emit a misleading ``incomplete
    routing (1/N nets)`` blocker for boards the CI gate considers
    ship-ready.

    When ``report_exists`` is False both new fields are 0 / empty;
    callers MUST treat the absence of a report as "unknown" rather than
    "advisory-only" so the pre-fix behaviour is preserved on boards
    that have not yet had ``kct check`` run.
    """

    report_exists: bool = False
    errors: int = 0
    tolerance: int = 0
    # Per ``.github/routed-drc-tolerance.yml`` schema, this is the
    # repo-relative path used to look up the tolerance. Surfaced so
    # JSON consumers can correlate.
    tolerance_key: str | None = None
    # Issue #3363: split of ``errors`` by ``DRCChecker.ADVISORY_RULE_IDS``.
    # ``blocking_errors`` is what the CI strict gate compares to tolerance;
    # ``advisory_errors_by_rule`` maps each advisory rule_id to its error
    # count (currently only ``connectivity``). Both default to 0/empty when
    # the violations array is missing (legacy report format) or when no
    # report exists.
    blocking_errors: int = 0
    advisory_errors_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def over_tolerance(self) -> bool:
        """True iff a real DRC report exists AND it exceeds tolerance.

        Issue #3363: uses ``blocking_errors`` (advisory-filtered) instead
        of the raw ``errors`` count so the fleet verdict matches the CI
        gate. Reports older than the violations-array format fall back
        to ``errors`` because ``blocking_errors == 0`` and the gate
        cannot distinguish blocking vs advisory without the per-rule
        breakdown (defensive: treat as blocking).
        """
        if not self.report_exists:
            return False
        # If we parsed per-rule splits, gate on the blocking subset only.
        # Otherwise (legacy / summary-only report) fall back to the raw
        # ``errors`` count so the gate stays strict.
        if self.blocking_errors > 0 or self.advisory_errors_by_rule:
            return self.blocking_errors > self.tolerance
        return self.errors > self.tolerance

    @property
    def advisory_only(self) -> bool:
        """True iff a real DRC report exists AND every error is advisory.

        Used by :func:`_compute_blockers` (issue #3363) to suppress the
        ``incomplete routing`` blocker when the only DRC findings are
        advisory ``connectivity``. ``errors == 0`` (clean report) is NOT
        treated as ``advisory_only`` because there is nothing to be
        advisory about -- callers should keep their incomplete-routing
        verdict from the structural net analysis in that case.
        """
        if not self.report_exists:
            return False
        # Require at least one advisory error AND zero blocking errors;
        # the latter ensures we don't suppress a legitimate signal-net
        # blocker when some advisory violations happen to ride along.
        if self.blocking_errors > 0:
            return False
        return sum(self.advisory_errors_by_rule.values()) > 0

    def to_dict(self) -> dict:
        return {
            "report_exists": self.report_exists,
            "errors": self.errors,
            "blocking_errors": self.blocking_errors,
            "advisory_errors_by_rule": dict(self.advisory_errors_by_rule),
            "tolerance": self.tolerance,
            "tolerance_key": self.tolerance_key,
            "over_tolerance": self.over_tolerance,
            "advisory_only": self.advisory_only,
        }


@dataclass
class BoardStatus:
    """Aggregated status for a single board."""

    name: str
    routed_pcb: Path | None
    routed_mtime: float | None
    routing: RoutingStatus
    manufacturing: ManufacturingStatus
    drc: DRCStatus = field(default_factory=DRCStatus)
    blockers: list[str] = field(default_factory=list)

    @property
    def ship_ready(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "routed_pcb": str(self.routed_pcb) if self.routed_pcb else None,
            "routed_mtime": _iso_or_none(self.routed_mtime),
            "routing": self.routing.to_dict(),
            "manufacturing": self.manufacturing.to_dict(),
            "drc": self.drc.to_dict(),
            "ship_ready": self.ship_ready,
            "blockers": list(self.blockers),
        }


# ---------------------------------------------------------------------------
# Ship-ready data classes (issue #3099)
# ---------------------------------------------------------------------------


@dataclass
class ERCStatus:
    """ERC report presence + violation counts for a single board.

    Mirrors :class:`DRCStatus` shape for symmetry. When ``report_exists``
    is False the ERC step has not yet run for this board, so it does NOT
    contribute a blocker (warn-only treats missing as unknown).
    """

    report_exists: bool = False
    errors: int = 0
    warnings: int = 0
    report_path: str | None = None

    @property
    def has_errors(self) -> bool:
        return self.report_exists and self.errors > 0

    def to_dict(self) -> dict:
        return {
            "report_exists": self.report_exists,
            "errors": self.errors,
            "warnings": self.warnings,
            "report_path": self.report_path,
            "has_errors": self.has_errors,
        }


@dataclass
class ShipReadyStatus:
    """Per-board aggregate for the ``kct fleet ship-ready`` gate.

    Composes :class:`BoardStatus` (which already aggregates routing,
    manufacturing, and DRC) with an :class:`ERCStatus` field and a
    PASS/FAIL verdict driven by the union of all blocker sources.

    Designed for warn-only nightly CI: every aspect is surfaced for
    humans to triage even when the workflow exits 0.
    """

    board: BoardStatus
    erc: ERCStatus = field(default_factory=ERCStatus)
    blockers: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.board.name

    @property
    def passed(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "name": self.board.name,
            "passed": self.passed,
            "blockers": list(self.blockers),
            "routed_pcb": (str(self.board.routed_pcb) if self.board.routed_pcb else None),
            "routing": self.board.routing.to_dict(),
            "manufacturing": self.board.manufacturing.to_dict(),
            "drc": self.board.drc.to_dict(),
            "erc": self.erc.to_dict(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_or_none(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    return _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _discover_routed_pcb(board_dir: Path, pattern: str) -> Path | None:
    """Find the first routed PCB inside a board's output/ directory."""
    output_dir = board_dir / "output"
    if not output_dir.is_dir():
        return None
    matches = sorted(output_dir.glob(pattern))
    if not matches:
        return None
    return matches[0]


def _detect_manufacturing(board_dir: Path) -> ManufacturingStatus:
    """Detect manufacturing artifacts for a single board.

    Prefer ``manifest.json``'s ``files`` keys when present; fall back to
    directory globs (so we tolerate manufacturer-name variations such as
    ``bom_jlcpcb.csv`` vs ``bom_pcbway.csv``).
    """
    mfg = ManufacturingStatus()
    mfg_dir = board_dir / "output" / "manufacturing"
    if not mfg_dir.is_dir():
        return mfg

    mfg.dir_exists = True
    manifest_path = mfg_dir / "manifest.json"

    if manifest_path.exists():
        mfg.has_manifest = True
        try:
            mfg.manifest_mtime = manifest_path.stat().st_mtime
        except OSError:
            mfg.manifest_mtime = None
        try:
            with manifest_path.open() as fh:
                manifest = json.load(fh)
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if isinstance(files, dict):
                for name in files.keys():
                    lower = name.lower()
                    if lower.startswith("bom_") and lower.endswith(".csv"):
                        mfg.has_bom = True
                    elif lower.startswith("cpl_") and lower.endswith(".csv"):
                        mfg.has_cpl = True
                    elif lower == "gerbers.zip":
                        mfg.has_gerbers = True
        except (OSError, json.JSONDecodeError):
            # Manifest unreadable: fall back to directory scan below.
            pass

    # Directory-scan fallback (also fills gaps if manifest's files list is
    # incomplete).
    if not mfg.has_gerbers and (mfg_dir / "gerbers.zip").is_file():
        mfg.has_gerbers = True
    if not mfg.has_bom and any(mfg_dir.glob("bom_*.csv")):
        mfg.has_bom = True
    if not mfg.has_cpl and any(mfg_dir.glob("cpl_*.csv")):
        mfg.has_cpl = True

    return mfg


def _load_drc_tolerances(
    tolerance_path: Path = _DRC_TOLERANCE_PATH,
) -> dict[str, int]:
    """Load per-board DRC tolerance allowlist.

    Mirrors the loader in ``scripts/ci/check_routed_drc.py`` but kept
    self-contained here to avoid importing CI utility code from the CLI
    surface. Missing/unreadable/malformed files yield an empty mapping
    (every board must report 0 errors), matching the safer default.
    """
    if not tolerance_path.exists():
        return {}
    try:
        import yaml  # local import: optional dep at call site
    except ImportError:  # pragma: no cover - pyyaml is a hard dep
        return {}
    try:
        data = yaml.safe_load(tolerance_path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    tolerances = data.get("tolerances", {})
    if not isinstance(tolerances, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in tolerances.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        result[key] = value
    return result


def _drc_tolerance_for(
    routed_pcb: Path,
    tolerances: dict[str, int],
) -> tuple[int, str | None]:
    """Look up the tolerance for ``routed_pcb`` in ``tolerances``.

    Tolerances are keyed by repo-relative path (e.g.
    ``boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb``).
    We accept any suffix match so the lookup is robust to absolute paths
    passed in by callers (the CI script writes repo-relative; the CLI
    typically resolves ``--boards-dir`` to absolute).

    Returns ``(tolerance, matched_key_or_None)``. When no entry matches
    we return ``(0, None)`` -- absence means strict 0-error gate per the
    allowlist's policy.
    """
    if not tolerances:
        return (0, None)
    # Normalize once for suffix comparison.
    pcb_str = str(routed_pcb)
    pcb_posix = routed_pcb.as_posix()
    for key, value in tolerances.items():
        if pcb_str.endswith(key) or pcb_posix.endswith(key):
            return (value, key)
    return (0, None)


def _detect_drc(
    routed_pcb: Path,
    tolerances: dict[str, int],
) -> DRCStatus:
    """Read ``<routed_pcb>.parent/drc_report.json`` and return DRC status.

    Backwards-compat rule (issue #2932): if the report does not exist,
    return a status with ``report_exists=False`` and ``errors=0``. The
    blocker computation must then NOT treat the board as failing DRC,
    so boards that have not yet been DRC'd retain their pre-fix
    classification.

    Issue #3363: when the report includes a ``violations`` array (the
    current ``kct check --output`` format), split the error-severity
    count by ``DRCChecker.ADVISORY_RULE_IDS`` so the fleet command's
    ``incomplete routing`` blocker can be suppressed for boards whose
    only routing-incompleteness findings are advisory ``connectivity``
    (matching ``scripts/ci/check_routed_drc.py:_count_blocking_errors``).
    Reports lacking the array (legacy summary-only format) keep
    ``blocking_errors == 0`` and ``advisory_errors_by_rule == {}``; the
    ``over_tolerance`` property then falls back to the raw ``errors``
    count so the gate stays strict on older reports.
    """
    from kicad_tools.validate.checker import DRCChecker

    drc = DRCStatus()
    report_path = routed_pcb.parent / "drc_report.json"
    tolerance, matched_key = _drc_tolerance_for(routed_pcb, tolerances)
    drc.tolerance = tolerance
    drc.tolerance_key = matched_key
    if not report_path.is_file():
        return drc
    try:
        with report_path.open() as fh:
            report = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # Treat a malformed report identically to a missing one --
        # don't regress ship-ready when the DRC stage is broken
        # for unrelated reasons.
        return drc
    summary = report.get("summary") if isinstance(report, dict) else None
    if isinstance(summary, dict):
        errors = summary.get("errors", 0)
        if isinstance(errors, int) and not isinstance(errors, bool):
            drc.errors = errors
    # Split errors by advisory rule classification when the violations
    # array is present (issue #3363). Mirrors
    # ``scripts/ci/check_routed_drc.py:_count_blocking_errors`` so the
    # fleet command's gate semantics align with the CI strict gate.
    violations = report.get("violations") if isinstance(report, dict) else None
    if isinstance(violations, list):
        blocking = 0
        advisory: dict[str, int] = {}
        for v in violations:
            if not isinstance(v, dict):
                continue
            severity = v.get("severity", "error")
            if severity != "error":
                continue
            rule_id = v.get("rule_id", "")
            if not isinstance(rule_id, str):
                continue
            if DRCChecker.is_advisory_rule(rule_id):
                advisory[rule_id] = advisory.get(rule_id, 0) + 1
            else:
                blocking += 1
        drc.blocking_errors = blocking
        drc.advisory_errors_by_rule = advisory
    drc.report_exists = True
    return drc


def _detect_erc(board_dir: Path) -> ERCStatus:
    """Look for an ERC report under common locations in ``board_dir``.

    Tries (in order):
      * ``<board>/output/erc_report.json``
      * ``<board>/output/<sch_stem>-erc.json`` for any sibling ``.kicad_sch``
      * ``<board>/erc_report.json``

    Returns ``ERCStatus`` with ``report_exists=False`` when nothing is
    found -- in warn-only mode this surfaces as ``ERC -`` in the table
    and does not block ship-ready (consistent with the DRC
    backwards-compat rule from issue #2932).
    """
    erc = ERCStatus()

    candidates: list[Path] = [
        board_dir / "output" / "erc_report.json",
        board_dir / "erc_report.json",
    ]
    # KiCad's own convention: ``<sch_stem>-erc.json`` next to the .kicad_sch.
    try:
        for sch in sorted((board_dir).glob("*.kicad_sch")):
            candidates.append(board_dir / "output" / f"{sch.stem}-erc.json")
            candidates.append(board_dir / f"{sch.stem}-erc.json")
    except OSError:
        pass

    report_path: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            report_path = candidate
            break
    if report_path is None:
        return erc

    erc.report_path = str(report_path)
    try:
        with report_path.open() as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # Malformed report behaves like missing -- do not block.
        return erc

    # Two shapes are common:
    #   1. Our ERCReport.to_dict() with ``violations``/``errors``/``warnings``
    #   2. KiCad's native dump with ``sheets[].violations[].severity``
    errors = 0
    warnings = 0
    if isinstance(data, dict):
        summary = data.get("summary")
        if isinstance(summary, dict):
            if isinstance(summary.get("errors"), int):
                errors = summary["errors"]
            if isinstance(summary.get("warnings"), int):
                warnings = summary["warnings"]

        if errors == 0 and warnings == 0:
            # Fall back to walking violations / sheets[].violations.
            violations: list = []
            top_violations = data.get("violations")
            if isinstance(top_violations, list):
                violations.extend(top_violations)
            sheets = data.get("sheets")
            if isinstance(sheets, list):
                for sheet in sheets:
                    if isinstance(sheet, dict):
                        sv = sheet.get("violations")
                        if isinstance(sv, list):
                            violations.extend(sv)
            for v in violations:
                if not isinstance(v, dict):
                    continue
                severity = str(v.get("severity", "")).lower()
                if severity == "error":
                    errors += 1
                elif severity == "warning":
                    warnings += 1

    erc.report_exists = True
    erc.errors = errors
    erc.warnings = warnings
    return erc


_SCH_LABEL_RE = re.compile(r'\(\s*(?:label|global_label|hierarchical_label)\s+"([^"]+)"')
_PCB_NET_RE = re.compile(r'\(net\s+\d+\s+"([^"]+)"\)')


def _extract_schematic_nets(sch_path: Path) -> set[str] | None:
    """Return the set of named labels declared in a KiCad schematic.

    The detection is intentionally lightweight: we only collect text from
    ``label`` / ``global_label`` / ``hierarchical_label`` s-expression
    leaves. Auto-generated ``Net-(...)`` placeholders (KiCad's default
    unconnected names) are skipped because they cannot be reliably
    mapped to a PCB net name without a full netlister pass.

    Returns ``None`` if the file cannot be read so callers can treat
    that as "unknown source state" and skip drift gating.
    """
    try:
        text = sch_path.read_text()
    except OSError:
        return None
    labels: set[str] = set()
    for m in _SCH_LABEL_RE.finditer(text):
        name = m.group(1)
        if not name:
            continue
        if name.startswith("Net-("):
            continue
        labels.add(name)
    return labels


def _extract_pcb_named_nets(pcb_path: Path) -> set[str] | None:
    """Return the set of non-empty named nets declared in a KiCad PCB.

    Only top-level ``(net N "name")`` declarations are matched; the
    empty net 0 is dropped. Returns ``None`` on read failure.
    """
    try:
        text = pcb_path.read_text()
    except OSError:
        return None
    nets: set[str] = set()
    for m in _PCB_NET_RE.finditer(text):
        name = m.group(1)
        if name:
            nets.add(name)
    return nets


def _find_source_schematic(board_dir: Path) -> Path | None:
    """Locate the source ``.kicad_sch`` for a board.

    Looks in ``board_dir/output/`` first (where generators emit) and
    falls back to ``board_dir/`` (legacy layout). Returns the first
    matching schematic by sort order, or ``None`` if none exists.
    """
    for candidate_dir in (board_dir / "output", board_dir):
        try:
            matches = sorted(candidate_dir.glob("*.kicad_sch"))
        except OSError:
            continue
        if matches:
            return matches[0]
    return None


def _detect_source_drift(
    routed_pcb: Path,
    board_dir: Path,
    *,
    sample_limit: int = 8,
    added_only_tolerance: int = _DRIFT_ADDED_ONLY_TOLERANCE,
) -> tuple[bool, int | None, int | None, list[str], list[str]]:
    """Detect schematic-vs-PCB net-set drift (issues #3280, #3302).

    Compares the set of named labels in the source ``.kicad_sch`` to the
    set of named nets in the routed PCB. When the sets differ, the routed
    PCB is treated as **source-stale**: its ``X/Y nets`` count is no
    longer a trustworthy signal of current-design routing progress.

    Two normalisation passes are applied before the symmetric difference
    is computed (issue #3302):

      1. **Power-rail alias canonicalisation.** KiCad's stock
         ``power:+3V3`` symbol publishes ``+3V3`` while kicad-tools'
         netlist-sync emits ``+3.3V``; both forms refer to the same
         electrical net. :func:`canonicalize_power_nets` rewrites both
         sides to a single canonical form (``+3.3V``) so fractional
         rails compare equal across the schematic-vs-PCB boundary.
         See ``src/kicad_tools/schema/pcb.py`` for the alias table.

      2. **Sub-threshold added-only tolerance.** kicad-tools schematics
         routinely leave short component-to-component nets unlabeled
         (e.g. ``LED_K`` on board 04). The PCB synthesises a name for
         each such net during sync, so the PCB-net set is a strict
         superset of the schematic-label set even when the design is in
         perfect sync. When the residual diff after canonicalisation
         has **no removals** AND at most ``added_only_tolerance`` added
         names, those adds are attributed to unlabeled-local-net
         synthesis and drift is *not* reported. Genuine schematic edits
         (adds beyond the threshold, OR any removals) still surface.

    The check is deliberately conservative:
      * If the schematic cannot be located or parsed, we return
        ``(False, None, None, [], [])`` -- "unknown source state, do not
        gate". This preserves the pre-fix behavior for boards where
        drift detection cannot run.
      * If the PCB nets cannot be parsed (extremely unlikely since
        NetStatusAnalyzer already loaded the same file), we likewise
        return "no drift".

    Returns:
        ``(source_stale, schematic_net_count, pcb_net_count,
        added_in_pcb, removed_from_pcb)``. The ``added`` / ``removed``
        samples are sorted and capped at ``sample_limit`` for bounded
        JSON output. Counts are the raw (pre-canonicalisation) set
        sizes so the JSON ``schematic_net_count`` /
        ``pcb_net_count`` keys keep their previous meaning.
    """
    sch_path = _find_source_schematic(board_dir)
    if sch_path is None:
        return (False, None, None, [], [])
    sch_nets = _extract_schematic_nets(sch_path)
    if sch_nets is None:
        return (False, None, None, [], [])
    pcb_nets = _extract_pcb_named_nets(routed_pcb)
    if pcb_nets is None:
        return (False, len(sch_nets), None, [], [])

    # Step 1: canonicalise power-rail aliases on both sides before the
    # set diff. Non-power names pass through unchanged.
    sch_canon = canonicalize_power_nets(sch_nets)
    pcb_canon = canonicalize_power_nets(pcb_nets)

    added = sorted(pcb_canon - sch_canon)
    removed = sorted(sch_canon - pcb_canon)

    # Step 2: sub-threshold added-only tolerance. Only applies when
    # there are NO removals (a removed schematic label is always a
    # real signal) AND the residual add count is within the
    # unlabeled-local-net headroom.
    if not removed and 0 < len(added) <= added_only_tolerance:
        source_stale = False
    else:
        source_stale = bool(added) or bool(removed)

    return (
        source_stale,
        len(sch_nets),
        len(pcb_nets),
        added[:sample_limit],
        removed[:sample_limit],
    )


def _compute_routing(routed_pcb: Path) -> RoutingStatus:
    """Run NetStatusAnalyzer on a routed PCB and tally pads/nets."""
    status = RoutingStatus()
    try:
        analyzer = NetStatusAnalyzer(routed_pcb)
        result = analyzer.analyze()
    except Exception as exc:  # pragma: no cover - defensive
        status.error = f"{type(exc).__name__}: {exc}"
        return status

    status.total_pads = sum(n.total_pads for n in result.nets)
    status.connected_pads = sum(n.connected_count for n in result.nets)
    status.total_nets = result.total_nets
    status.complete_nets = result.complete_count
    status.incomplete_nets = result.incomplete_count
    status.blocking_incomplete_nets = result.blocking_incomplete_count
    status.unrouted_nets = result.unrouted_count
    return status


def _compute_blockers(
    routing: RoutingStatus,
    mfg: ManufacturingStatus,
    routed_pcb: Path | None,
    drc: DRCStatus | None = None,
) -> list[str]:
    """First-failing list of reasons a board cannot ship.

    DRC handling (issue #2932): when a ``drc_report.json`` exists AND its
    error count exceeds the per-board tolerance allowlist (see
    ``.github/routed-drc-tolerance.yml``), a ``DRC errors: N`` blocker
    is appended. Boards with no DRC report keep their pre-#2932
    classification (no blocker added). The DRC blocker is ordered after
    routing/manufacturing-artifact blockers so the first-failure reason
    in the table stays consistent with prior behavior; DRC surfaces
    when those upstream gates already pass.

    Incomplete-routing handling (issue #3363, post-#3215 follow-up):
    when a ``drc_report.json`` exists AND classifies every error as
    advisory ``connectivity`` (per ``DRCChecker.ADVISORY_RULE_IDS``,
    via :attr:`DRCStatus.advisory_only`), the ``incomplete routing``
    blocker is suppressed. This aligns the fleet command's verdict
    with ``scripts/ci/check_routed_drc.py`` for the case where the
    router correctly refused a signal net at clearance (board 04's
    NRST after PR #3286) -- the CI gate treats the residual as
    advisory so the board ships, and the fleet command must agree to
    avoid a misleading ``incomplete routing (1/12 nets)`` blocker.
    Without a DRC report on disk the pre-fix behaviour is preserved
    (mirrors the issue-#2932 backwards-compat rule for ``_detect_drc``).
    """
    blockers: list[str] = []
    if routed_pcb is None:
        blockers.append("no routed PCB")
        return blockers
    if routing.error is not None:
        blockers.append(f"routing analysis failed: {routing.error}")
        return blockers
    if routing.source_stale:
        # Issue #3280: the routed PCB's net set disagrees with the source
        # schematic, so the ``X/Y nets`` figure derived from the PCB is
        # not a trustworthy current-routing signal. Emit a clearer blocker
        # and suppress the misleading count. The schematic net count is
        # surfaced when available so triage can see the actual target.
        if routing.schematic_net_count is not None:
            blockers.append(
                "routed PCB stale (schematic drift: "
                f"{routing.schematic_net_count} nets in schematic, "
                f"{routing.pcb_net_count} in PCB)"
            )
        else:
            blockers.append("routed PCB stale (schematic drift)")
    elif not routing.routing_complete:
        # Issue #3363: align with the CI strict gate. When a DRC report
        # exists and shows ONLY advisory ``connectivity`` errors (zero
        # blocking errors), the CI gate treats the board as ship-ready
        # and the fleet command must agree. Suppress the
        # ``incomplete routing`` blocker in that case so the verdict
        # matches ``scripts/ci/check_routed_drc.py``.
        if drc is not None and drc.advisory_only:
            # Advisory-only routing residuals are not a blocker; the
            # diagnostic count is still preserved in the routing JSON
            # for human triage.
            pass
        else:
            # Use the advisory-filtered count so the blocker message agrees
            # with the verdict (plane/pour stitching residuals do not show
            # up here even though `incomplete_nets` may be non-zero).
            incomplete = routing.blocking_incomplete_nets + routing.unrouted_nets
            blockers.append(f"incomplete routing ({incomplete}/{routing.total_nets} nets)")
    if not mfg.dir_exists:
        blockers.append("no manufacturing/ dir")
        return blockers
    if not mfg.has_gerbers:
        blockers.append("missing gerbers")
    if not mfg.has_bom:
        blockers.append("missing BOM")
    if not mfg.has_cpl:
        blockers.append("missing CPL")
    if not mfg.has_manifest:
        blockers.append("no manifest")
    if mfg.stale:
        blockers.append("artifacts stale")
    if drc is not None and drc.over_tolerance:
        if drc.tolerance > 0:
            blockers.append(f"DRC errors: {drc.errors} (allowed {drc.tolerance})")
        else:
            blockers.append(f"DRC errors: {drc.errors}")
    return blockers


def _survey_board(
    board_dir: Path,
    pattern: str,
    drc_tolerances: dict[str, int] | None = None,
) -> BoardStatus:
    """Build a BoardStatus for a single board directory."""
    routed_pcb = _discover_routed_pcb(board_dir, pattern)
    routed_mtime: float | None = None
    routing = RoutingStatus()
    mfg = ManufacturingStatus()
    drc = DRCStatus()

    if routed_pcb is not None:
        try:
            routed_mtime = routed_pcb.stat().st_mtime
        except OSError:
            routed_mtime = None
        routing = _compute_routing(routed_pcb)
        # Issue #3280: detect schematic-vs-PCB net-set drift so the
        # ``X/Y nets`` blocker does not lie when the routed PCB no longer
        # reflects the current schematic.
        (
            source_stale,
            sch_count,
            pcb_count,
            added,
            removed,
        ) = _detect_source_drift(routed_pcb, board_dir)
        routing.source_stale = source_stale
        routing.schematic_net_count = sch_count
        routing.pcb_net_count = pcb_count
        routing.drift_added = added
        routing.drift_removed = removed
        mfg = _detect_manufacturing(board_dir)
        if (
            routed_mtime is not None
            and mfg.manifest_mtime is not None
            and routed_mtime > mfg.manifest_mtime
        ):
            mfg.stale = True
        drc = _detect_drc(routed_pcb, drc_tolerances or {})

    blockers = _compute_blockers(routing, mfg, routed_pcb, drc)

    return BoardStatus(
        name=board_dir.name,
        routed_pcb=routed_pcb,
        routed_mtime=routed_mtime,
        routing=routing,
        manufacturing=mfg,
        drc=drc,
        blockers=blockers,
    )


def _discover_boards(
    boards_dir: Path,
    pattern: str,
    drc_tolerance_path: Path | None = None,
) -> list[BoardStatus]:
    """Survey every board sub-directory of ``boards_dir``.

    A board is discovered if it contains a routed PCB matching ``pattern``
    under ``<board>/output/``. Boards without a routed PCB are silently
    skipped (they haven't reached the routing stage yet).
    """
    if not boards_dir.is_dir():
        return []
    tolerances = _load_drc_tolerances(
        drc_tolerance_path if drc_tolerance_path is not None else _DRC_TOLERANCE_PATH
    )
    boards: list[BoardStatus] = []
    for entry in sorted(boards_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip dotfiles / hidden dirs and obvious non-board names.
        if entry.name.startswith("."):
            continue
        # A directory without an output/ sub-dir is not yet at routing
        # stage -- skip it silently.
        output_dir = entry / "output"
        if not output_dir.is_dir():
            continue
        # Skip directories with no routed PCB matching the pattern.
        if _discover_routed_pcb(entry, pattern) is None:
            continue
        boards.append(_survey_board(entry, pattern, tolerances))
    return boards


# ---------------------------------------------------------------------------
# Ship-ready survey + formatters (issue #3099)
# ---------------------------------------------------------------------------


def _compute_ship_ready_blockers(
    board: BoardStatus,
    erc: ERCStatus,
) -> list[str]:
    """Aggregate blockers from routing/manufacturing/DRC/ERC.

    Reuses ``board.blockers`` (already filled by ``_compute_blockers``)
    and appends ERC errors when the report exists and reports any
    severity-error rows. Missing reports never add a blocker -- they
    surface as ``-`` in the table per the warn-only philosophy.
    """
    blockers: list[str] = list(board.blockers)
    if erc.has_errors:
        blockers.append(f"ERC errors: {erc.errors}")
    return blockers


def _survey_board_ship_ready(
    board_dir: Path,
    pattern: str,
    drc_tolerances: dict[str, int] | None = None,
) -> ShipReadyStatus:
    """Build a ``ShipReadyStatus`` for a single board directory."""
    board = _survey_board(board_dir, pattern, drc_tolerances)
    erc = _detect_erc(board_dir)
    blockers = _compute_ship_ready_blockers(board, erc)
    return ShipReadyStatus(board=board, erc=erc, blockers=blockers)


def _discover_ship_ready(
    boards_dir: Path,
    pattern: str,
    drc_tolerance_path: Path | None = None,
) -> list[ShipReadyStatus]:
    """Survey every board sub-directory for ship-readiness.

    Mirrors :func:`_discover_boards` (same skip rules) but builds the
    richer :class:`ShipReadyStatus` per board.
    """
    if not boards_dir.is_dir():
        return []
    tolerances = _load_drc_tolerances(
        drc_tolerance_path if drc_tolerance_path is not None else _DRC_TOLERANCE_PATH
    )
    results: list[ShipReadyStatus] = []
    for entry in sorted(boards_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        output_dir = entry / "output"
        if not output_dir.is_dir():
            continue
        if _discover_routed_pcb(entry, pattern) is None:
            continue
        results.append(_survey_board_ship_ready(entry, pattern, tolerances))
    return results


def _erc_label(erc: ERCStatus) -> str:
    """Render the ERC column cell.

    ``-``       : no ERC report found (no signal yet, do not block).
    ``N``       : N errors found (any value -- a non-zero count drives the
                  ship-ready ``FAIL`` blocker).
    """
    if not erc.report_exists:
        return "-"
    return str(erc.errors)


def _format_ship_ready_table(
    statuses: list[ShipReadyStatus],
    boards_dir: Path,
    *,
    warn_only: bool,
) -> str:
    """Plain-ASCII per-board PASS/FAIL table for nightly summaries."""
    lines: list[str] = []
    header = f"{'Board':<28} {'Route':>7} {'DRC':>5} {'ERC':>5} {'Mfr':<8} {'Stale':<6} Verdict"
    sep = "-" * len(header)
    lines.append(header)
    lines.append(sep)

    if not statuses:
        lines.append("No boards found")
    else:
        for s in statuses:
            route_cell = f"{s.board.routing.connected_pads}/{s.board.routing.total_pads}"
            drc_cell = _drc_label(s.board.drc)
            erc_cell = _erc_label(s.erc)
            mfr_cell = _mfr_letters(s.board.manufacturing)
            stale_cell = _stale_label(s.board.manufacturing)
            if s.passed:
                verdict = "PASS"
            else:
                verdict = f"FAIL ({s.blockers[0]})"
            lines.append(
                f"{s.name[:28]:<28} {route_cell:>7} {drc_cell:>5} "
                f"{erc_cell:>5} {mfr_cell:<8} {stale_cell:<6} {verdict}"
            )

    # Footer.
    if statuses:
        lines.append("")
        passing = sum(1 for s in statuses if s.passed)
        failing = len(statuses) - passing
        mode = "warn-only" if warn_only else "strict"
        lines.append(
            f"{len(statuses)} boards surveyed, {passing} PASS, {failing} FAIL ({mode} mode)"
        )
        lines.append(f"boards-dir: {boards_dir}")
        if warn_only and failing > 0:
            lines.append(
                "::warning::ship-ready: "
                f"{failing}/{len(statuses)} board(s) FAIL "
                "(warn-only mode -- exit 0)"
            )

    return "\n".join(lines)


def _format_ship_ready_json(
    statuses: list[ShipReadyStatus],
    boards_dir: Path,
    *,
    warn_only: bool,
) -> str:
    summary = {
        "total": len(statuses),
        "passed": sum(1 for s in statuses if s.passed),
        "failed": sum(1 for s in statuses if not s.passed),
        "warn_only": warn_only,
    }
    doc = {
        "schema_version": SCHEMA_VERSION,
        "command": "ship-ready",
        "surveyed_at": _now_iso(),
        "boards_dir": str(boards_dir),
        "summary": summary,
        "boards": [s.to_dict() for s in statuses],
    }
    return json.dumps(doc, indent=2)


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _mfr_letters(mfg: ManufacturingStatus) -> str:
    if not mfg.dir_exists or not mfg.has_any:
        return "-"
    parts = []
    if mfg.has_bom:
        parts.append("B")
    if mfg.has_cpl:
        parts.append("C")
    if mfg.has_gerbers:
        parts.append("G")
    if mfg.has_manifest:
        parts.append("M")
    return "/".join(parts) if parts else "-"


def _stale_label(mfg: ManufacturingStatus) -> str:
    if not mfg.has_manifest:
        return "-"
    return "STALE" if mfg.stale else "fresh"


def _drc_label(drc: DRCStatus) -> str:
    """Render the DRC column cell.

    ``-``       : no ``drc_report.json`` yet (backwards-compat: do not
                  block ship-ready).
    ``N``       : N errors, within tolerance (N <= allowance).
    ``N!``      : N errors, exceeds tolerance (drives the ship-ready
                  ``NO`` blocker).
    """
    if not drc.report_exists:
        return "-"
    suffix = "!" if drc.over_tolerance else ""
    return f"{drc.errors}{suffix}"


def _format_table(
    boards: list[BoardStatus],
    boards_dir: Path,
    *,
    ship_only: bool,
) -> str:
    """Format a fixed-width plain-ASCII table."""
    lines: list[str] = []
    header = f"{'Board':<28} {'Pads':>7} {'%':>5} {'Mfr':<8} {'Stale':<6} {'DRC':>5} Ship?"
    sep = "-" * len(header)
    lines.append(header)
    lines.append(sep)

    visible = [b for b in boards if (not ship_only or b.ship_ready)]

    if not visible:
        if not boards:
            lines.append("No boards found")
        else:
            lines.append("(no ship-ready boards)")
    else:
        for b in visible:
            pads = f"{b.routing.connected_pads}/{b.routing.total_pads}"
            pct = f"{b.routing.completion_pct:.0f}%"
            mfr = _mfr_letters(b.manufacturing)
            stale = _stale_label(b.manufacturing)
            drc_cell = _drc_label(b.drc)
            if b.ship_ready:
                ship = "YES"
            else:
                ship = f"NO  ({b.blockers[0]})"
            lines.append(
                f"{b.name[:28]:<28} {pads:>7} {pct:>5} {mfr:<8} {stale:<6} {drc_cell:>5} {ship}"
            )

    # Footer (always uses full board list, not filtered view).
    if boards:
        lines.append("")
        ship_ready = sum(1 for b in boards if b.ship_ready)
        incomplete = sum(
            1 for b in boards if not b.routing.routing_complete and b.routing.error is None
        )
        stale_count = sum(1 for b in boards if b.manufacturing.stale)
        drc_failing = sum(1 for b in boards if b.drc.over_tolerance)
        lines.append(
            f"{len(boards)} boards surveyed, {ship_ready} ship-ready, "
            f"{incomplete} incomplete, {stale_count} artifacts stale, "
            f"{drc_failing} DRC over tolerance"
        )
        lines.append(f"boards-dir: {boards_dir}")

    return "\n".join(lines)


def _format_json(boards: list[BoardStatus], boards_dir: Path) -> str:
    summary = {
        "total": len(boards),
        "ship_ready": sum(1 for b in boards if b.ship_ready),
        "incomplete_routing": sum(
            1 for b in boards if not b.routing.routing_complete and b.routing.error is None
        ),
        "stale_artifacts": sum(1 for b in boards if b.manufacturing.stale),
        "missing_artifacts": sum(1 for b in boards if not b.manufacturing.has_all),
        "drc_over_tolerance": sum(1 for b in boards if b.drc.over_tolerance),
    }
    doc = {
        "schema_version": SCHEMA_VERSION,
        "surveyed_at": _now_iso(),
        "boards_dir": str(boards_dir),
        "summary": summary,
        "boards": [b.to_dict() for b in boards],
    }
    return json.dumps(doc, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kicad-fleet",
        description="Fleet-wide PCB status and operations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="fleet_command", help="Fleet commands")

    status_parser = subparsers.add_parser(
        "status",
        help="Survey routing + manufacturing status for all boards",
    )
    status_parser.add_argument(
        "--boards-dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    status_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    status_parser.add_argument(
        "--ship-only",
        action="store_true",
        help="Show only ship-ready boards (table only; JSON always lists all)",
    )
    status_parser.add_argument(
        "--include-stale",
        action="store_true",
        help=(
            "Reserved for future use: in the default behavior stale artifacts "
            "already demote ship-ready to NO. Currently a no-op flag retained "
            "for forward compatibility with --ship-only filter semantics."
        ),
    )
    status_parser.add_argument(
        "--pattern",
        default="*_routed.kicad_pcb",
        help="Glob to identify routed PCB inside output/ (default: *_routed.kicad_pcb)",
    )
    status_parser.add_argument(
        "--drc-tolerance-file",
        default=str(_DRC_TOLERANCE_PATH),
        help=(
            "Path to the per-board DRC tolerance allowlist (default: "
            ".github/routed-drc-tolerance.yml). Boards exceeding the listed "
            "tolerance -- or any board not listed with errors > 0 -- block "
            "ship-ready."
        ),
    )

    # ------------------------------------------------------------------
    # fleet ship-ready (issue #3099)
    # ------------------------------------------------------------------
    ship_parser = subparsers.add_parser(
        "ship-ready",
        help=(
            "Per-board PASS/FAIL gate (routing + DRC + ERC + manufacturing). "
            "Warn-only by default; pass --strict for non-zero exit on failure."
        ),
    )
    ship_parser.add_argument(
        "--boards-dir",
        default="boards",
        help="Root directory containing per-board subdirs (default: boards)",
    )
    ship_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    ship_parser.add_argument(
        "--pattern",
        default="*_routed.kicad_pcb",
        help="Glob to identify routed PCB inside output/ (default: *_routed.kicad_pcb)",
    )
    ship_parser.add_argument(
        "--drc-tolerance-file",
        default=str(_DRC_TOLERANCE_PATH),
        help=(
            "Path to the per-board DRC tolerance allowlist (default: "
            ".github/routed-drc-tolerance.yml)."
        ),
    )
    ship_parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero (2) if any board fails. Default is warn-only "
            "(always exit 0) so the command is safe to wire into a "
            "non-blocking nightly CI gate."
        ),
    )
    return parser


def run_status(args: argparse.Namespace) -> int:
    """Execute the ``fleet status`` sub-action."""
    boards_dir = Path(args.boards_dir)
    tolerance_path = Path(getattr(args, "drc_tolerance_file", _DRC_TOLERANCE_PATH))
    boards = _discover_boards(boards_dir, args.pattern, tolerance_path)

    if args.format == "json":
        print(_format_json(boards, boards_dir))
    else:
        print(_format_table(boards, boards_dir, ship_only=args.ship_only))

    if not boards:
        return 2  # No boards found counts as not-ship-ready.
    if all(b.ship_ready for b in boards):
        return 0
    return 2


def run_ship_ready(args: argparse.Namespace) -> int:
    """Execute the ``fleet ship-ready`` sub-action.

    Warn-only by default: prints PASS/FAIL summary and always returns 0.
    When ``--strict`` is passed, returns 2 if any board failed (or if no
    boards were found, mirroring ``status``).
    """
    boards_dir = Path(args.boards_dir)
    tolerance_path = Path(getattr(args, "drc_tolerance_file", _DRC_TOLERANCE_PATH))
    warn_only = not getattr(args, "strict", False)

    statuses = _discover_ship_ready(boards_dir, args.pattern, tolerance_path)

    if args.format == "json":
        print(_format_ship_ready_json(statuses, boards_dir, warn_only=warn_only))
    else:
        print(_format_ship_ready_table(statuses, boards_dir, warn_only=warn_only))

    if warn_only:
        # Warn-only: never block. Surface non-zero only via the table's
        # ::warning:: annotation (parsed by GitHub Actions).
        return 0

    if not statuses:
        return 2
    if all(s.passed for s in statuses):
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the fleet command."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.fleet_command:
        parser.print_help()
        return 0

    if args.fleet_command == "status":
        return run_status(args)
    if args.fleet_command == "ship-ready":
        return run_ship_ready(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
