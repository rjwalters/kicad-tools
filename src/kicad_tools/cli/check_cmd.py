"""
Pure Python DRC check command for KiCad PCBs.

Runs design rule checks against manufacturer specifications without
requiring kicad-cli to be installed. Suitable for CI/CD pipelines.

Usage:
    kct check board.kicad_pcb                      # Run all checks
    kct check board.kicad_pcb --mfr jlcpcb         # With manufacturer rules
    kct check board.kicad_pcb --format json        # JSON output for CI
    kct check board.kicad_pcb --only clearance     # Run specific checks
    kct check board.kicad_pcb --skip silkscreen    # Exclude checks

Exit Codes:
    0 - All meta sub-checks PASSED (or --drc-only: no errors)
    1 - Command failure (file not found, parse error, etc.)
    2 - Any sub-check FAILED, or rollup is INCOMPLETE (any sub-check
        NOT RUN) without --allow-incomplete, or warnings found with --strict

Difference from `kct drc`:
    - kct drc: Uses kicad-cli to run DRC (requires KiCad)
    - kct check: Pure Python DRC (no external dependencies)
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kicad_tools.manufacturers import get_manufacturer_ids
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker, DRCResults, DRCViolation

# Issue #3750: meta-check status set.  ``NOT RUN`` is rendered with a space
# in human output and ``"NOT RUN"`` in JSON; we treat it as a single token
# so callers can compare against the literal.
SubCheckStatus = Literal["PASSED", "FAILED", "NOT RUN"]

# Issue #3924 AC1: the sidecar-gated length-skew / continuity rules that
# carry a measured ``actual_value`` (skew mm or coupled fraction) on every
# finding -- both passing (``info``) and failing (``error``).  ``output_table``
# collects these into a dedicated MEASUREMENT SUMMARY table so users see the
# measured values on the default (non-``--verbose``) path, distinct from the
# violation listing.
_MEASUREMENT_RULE_IDS: frozenset[str] = frozenset(
    {
        "match_group_length_skew",
        "diffpair_length_skew",
        "diffpair_routing_continuity",
    }
)

# Issue #4102: ``dimension_drill_clearance`` hole-to-hole findings carry both
# endpoints' resolved net names in ``nets=(net1, net2)``.  A user reading the
# report needs to separate the fab's real concern (different-net pairs, where a
# drill-wall break creates a short) from same-net pairs.  The data is
# already present per-finding and in ``--format json``; the report just needs to
# surface it (net names shown unconditionally, plus an explicit same-net /
# different-net qualifier).  This is presentational only -- severity is
# unchanged (see #2976: same-net drill overlap is still a manufacturing defect).
#
# Issue #4127: the classifier uses a floating-aware check (``_is_floating_net``),
# not naive string equality.  Every genuinely unconnected pad/via resolves to
# the *same* ``net:0`` placeholder (net 0 is a single canonical no-net sentinel
# in the PCB net table -- see ``PCB`` construction in schema/pcb.py), so a naive
# ``nets[0] == nets[1]`` would mislabel two *distinct* floating pins as
# ``same-net``.  Floating pins share no electrical identity, so any pair
# involving a floating endpoint is ``different-net``.
# Issue #4318: the copper-copper clearance rules carry the same
# ``nets=(net_a, net_b)`` pair (set in ``_create_violation``,
# ``validate/rules/clearance.py``), so an agent reading a ``clearance_segment_via``
# report needs the same same-net / different-net split ``dimension_drill_clearance``
# already gets -- a different-net 0.000mm coincidence is a genuine short to
# prioritize, while a same-net coincidence is a lower-risk (still-defective)
# malformed-copper artifact.  The classifier (``_net_relationship`` /
# ``_is_floating_net``) is floating-aware, so a floating (``net:0``) endpoint in a
# segment-via pair classifies as ``different-net`` (#4127) with no extra work.
_NET_RELATIONSHIP_RULE_IDS: frozenset[str] = frozenset(
    {
        "dimension_drill_clearance",
        "clearance_segment_via",
        "clearance_segment_segment",
        "clearance_via_via",
        "clearance_pad_via",
    }
)


def _is_floating_net(net: str) -> bool:
    """True for KiCad's unconnected/no-net sentinel as resolved by dimensions.py.

    Unconnected pads/vias always carry ``net_number == 0`` (a single canonical
    sentinel -- see ``PCB`` net-table construction, ``self._nets[0] = Net(0,
    "")``), which resolves to the empty string or, in dimensions.py's fallback,
    the literal placeholder ``"net:0"``.  Treat either spelling as floating so
    this stays correct if the empty-string form ever reaches here directly
    (e.g. a future caller that skips the ``f"net:{number}"`` fallback).
    """
    return net in ("", "net:0")


def _net_relationship(nets: tuple[str, ...]) -> str | None:
    """Classify a hole-to-hole finding's net pair as same-net / different-net.

    Returns ``"same-net"`` when both endpoints resolve to the same *named,
    non-floating* net, ``"different-net"`` when they differ, and ``None`` when
    the finding does not carry exactly two net names (nothing to compare).

    Floating/unconnected pins have no net identity to share: every one of them
    resolves to the *same* ``net:0`` placeholder upstream (net 0 is a single
    canonical no-net sentinel, not a per-pad unique ID -- see schema/pcb.py).
    So any pair with a floating endpoint -- including two distinct floating pins
    that collide on the ``net:0`` string -- is ``different-net``, never
    ``same-net`` (issue #4127).  A plain string equality would wrongly report
    ``net:0 == net:0`` as same-net; the floating-aware check below prevents that.
    """
    if len(nets) != 2:
        return None
    if _is_floating_net(nets[0]) or _is_floating_net(nets[1]):
        return "different-net"
    return "same-net" if nets[0] == nets[1] else "different-net"


@dataclass
class SubCheckResult:
    """Outcome of a single :mod:`kct check` sub-check (issue #3750).

    ``status`` is one of ``PASSED`` / ``FAILED`` / ``NOT RUN``.  ``detail``
    is the one-line human-readable summary that appears in parentheses on
    the human stanza and as the ``detail`` field in the JSON envelope.
    """

    status: SubCheckStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


@dataclass
class MetaCheckResult:
    """Aggregated meta-check rollup (issue #3750).

    Each of the four sub-checks (DRC, ERC, LVS, Manifest) has its own
    :class:`SubCheckResult`; ``overall`` is the rollup status that drives
    the exit code.
    """

    drc: SubCheckResult
    erc: SubCheckResult
    lvs: SubCheckResult
    manifest: SubCheckResult
    overall: Literal["PASSED", "FAILED", "INCOMPLETE"] = "PASSED"
    # Issue #4350: True when ERC/LVS are NOT RUN specifically because no
    # schematic could be discovered next to the PCB (as opposed to kicad-cli
    # being unavailable).  Drives the loud "skipped LVS hard gate" warning and
    # a machine-detectable JSON field so a skipped gate is never mistaken for a
    # clean one.
    schematic_missing: bool = False

    def _subs(self) -> tuple[SubCheckResult, ...]:
        return (self.drc, self.erc, self.lvs, self.manifest)

    def compute_overall(self) -> None:
        """Roll up the four sub-statuses into ``self.overall``.

        Rules (per issue #3750 acceptance criterion #3):

        * ``FAILED`` if any sub-check is ``FAILED``.
        * ``INCOMPLETE`` if any sub-check is ``NOT RUN`` (and none is
          ``FAILED``).
        * ``PASSED`` only when every sub-check is ``PASSED``.

        The rollup intentionally reports the truthful aggregate state and
        does not collapse ``INCOMPLETE`` into ``FAILED`` under ``--strict``
        -- the exit-code policy is the right place to make that decision
        (``INCOMPLETE`` is non-zero by default; ``--allow-incomplete``
        opts back in to exit 0 for boards that legitimately lack the
        inputs for a sub-check).
        """
        subs = self._subs()
        if any(s.status == "FAILED" for s in subs):
            self.overall = "FAILED"
        elif any(s.status == "NOT RUN" for s in subs):
            self.overall = "INCOMPLETE"
        else:
            self.overall = "PASSED"

    def to_dict(self) -> dict:
        return {
            "drc": self.drc.to_dict(),
            "erc": self.erc.to_dict(),
            "lvs": self.lvs.to_dict(),
            "manifest": self.manifest.to_dict(),
            "overall": self.overall,
            "schematic_missing": self.schematic_missing,
        }


# Available check categories


def _find_pcb_file(directory: Path) -> Path | None:
    """Find a .kicad_pcb file in the given directory.

    Searches recursively and filters out routed/backup files to find
    the primary unrouted PCB file.

    Args:
        directory: Directory to search

    Returns:
        Path to PCB file if found, None otherwise
    """
    pcb_files = list(directory.glob("**/*.kicad_pcb"))
    # Filter out routed and backup files
    pcb_files = [
        f
        for f in pcb_files
        if not f.name.endswith("_routed.kicad_pcb") and not f.name.endswith("-bak.kicad_pcb")
    ]
    if pcb_files:
        return pcb_files[0]
    return None


def _parse_copper_weight_arg(raw: str) -> tuple[float | None, float | None]:
    """Parse a ``--copper`` value into ``(outer_oz, inner_oz)`` (Issue #4326).

    Accepts two forms:

    - **Scalar** -- ``"2"`` / ``"1.0"`` -- applies to both the outer and
      inner layer classes, returning ``(oz, oz)``.
    - **Keyed** -- ``"outer=2,inner=0.5"`` -- sets each layer class
      independently.  A key omitted from the keyed form returns ``None`` for
      that class, meaning "fall back to the stackup / profile for that layer
      class" (so ``--copper outer=2`` overrides only the outer weight).

    Raises:
        ValueError: on empty input, unknown keys, duplicate keys, a
            non-numeric value, or a non-positive weight -- mirroring the
            ``--only`` / ``--skip`` category-validation contract (a clear
            ``Error:`` + exit 1 at the call site).
    """
    text = raw.strip()
    if not text:
        raise ValueError("--copper value is empty")

    if "=" not in text:
        # Scalar form: one number for both layer classes.
        try:
            oz = float(text)
        except ValueError:
            raise ValueError(
                f"invalid --copper value {raw!r} "
                "(expected a number like '2' or a keyed form 'outer=2,inner=0.5')"
            ) from None
        if oz <= 0:
            raise ValueError(f"--copper weight must be positive: {oz}")
        return (oz, oz)

    # Keyed form: comma-separated key=value tokens.
    outer: float | None = None
    inner: float | None = None
    seen: set[str] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"invalid --copper token {token!r} (expected 'key=value')")
        key, _, value = token.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key not in ("outer", "inner"):
            raise ValueError(f"unknown --copper key {key!r} (expected 'outer' or 'inner')")
        if key in seen:
            raise ValueError(f"duplicate --copper key {key!r}")
        seen.add(key)
        try:
            oz = float(value)
        except ValueError:
            raise ValueError(
                f"invalid --copper {key} value {value!r} (expected a number)"
            ) from None
        if oz <= 0:
            raise ValueError(f"--copper {key} weight must be positive: {oz}")
        if key == "outer":
            outer = oz
        else:
            inner = oz

    if outer is None and inner is None:
        raise ValueError(f"--copper keyed form set no values: {raw!r}")
    return (outer, inner)


def _discover_net_class_map_sidecar(pcb_path: Path) -> Path | None:
    """Probe conventional locations for a ``net_class_map.json`` sidecar.

    Issue #3917 Defect 2: ``kct route`` writes a ``net_class_map.json``
    sidecar next to the routed PCB (in the output directory).  ``kct
    check`` should auto-load it so the sidecar-gated skew / continuity
    rules fire without the user having to pass ``--net-class-map`` by
    hand -- mirroring the existing schematic auto-discovery.

    Candidate locations, in priority order, relative to the resolved
    PCB path:

    - ``<pcb_dir>/net_class_map.json`` (sidecar written alongside a
      routed board that lives in its own output directory)
    - ``<pcb_dir>/output/net_class_map.json`` (board dir with an
      ``output/`` subtree)
    - ``<pcb_dir>/../output/net_class_map.json`` (routed PCB inside
      ``output/`` with the sidecar as a sibling -- redundant with the
      first candidate but kept for the ``<board>/output/<pcb>`` layout)

    Returns:
        The first existing candidate path, or ``None`` when no sidecar
        is found.
    """
    pcb_dir = pcb_path.parent
    candidates = [
        pcb_dir / "net_class_map.json",
        pcb_dir / "output" / "net_class_map.json",
        pcb_dir.parent / "output" / "net_class_map.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _emit_drift_banner(pcb_path: Path, schematic: str | None) -> None:
    """Print the advisory schematic/PCB drift banner (non-blocking).

    No-op when no schematic can be resolved or the PCB is in sync.  This is
    advisory only and never affects the caller's exit code (issue #3154).

    The banner is routed to stderr so it does not pollute the stdout JSON
    body produced by ``--format json`` consumers (the CI gate at
    ``scripts/ci/check_routed_drc.py`` parses stdout as a single JSON document
    and was choking on the leading WARNING line; routing to stderr keeps the
    advisory visible in human/log output while leaving the structured payload
    clean).
    """
    from kicad_tools.sync.drift import analyze_drift, format_drift_banner

    analysis, _resolved = analyze_drift(pcb_path, schematic)
    if analysis is None:
        return
    banner = format_drift_banner(analysis, pcb_path)
    if banner:
        print(banner, file=sys.stderr)


def run_netlist_sync_gate(
    pcb_path: Path,
    schematic: str | None = None,
    strict: bool = False,
) -> int:
    """Run the blocking schematic/PCB netlist-sync gate (issue #3154).

    Reuses :class:`kicad_tools.sync.reconciler.Reconciler` (via the shared
    drift helpers) to compare the schematic component set against the PCB
    footprint set, then prints a full add/drop/orphan report.

    Exit codes (mirroring ``kct check``'s convention):
        0 - in sync, or only PCB-only/value/footprint drift without --strict
        1 - no schematic could be resolved (cannot run the gate)
        2 - components present in the schematic are missing from the PCB
            (unbuildable BOM), or any drift with --strict

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.
        schematic: Optional explicit schematic path override.
        strict: When True, any drift (including PCB-only/value/footprint)
            yields exit code 2.
    """
    from kicad_tools.sync.drift import analyze_drift, has_drift, render_drift_report

    analysis, resolved = analyze_drift(pcb_path, schematic)
    if analysis is None or resolved is None:
        print(
            "Error: --netlist-sync requires a schematic, but none was found "
            f"for {Path(pcb_path).name}.",
            file=sys.stderr,
        )
        print(
            "Hint: pass --schematic <path>.kicad_sch, or place a sibling "
            "<basename>.kicad_sch next to the PCB.",
            file=sys.stderr,
        )
        return 1

    print(render_drift_report(analysis, pcb_path, resolved))

    # Schematic-only drift == unbuildable BOM == blocking (mirrors the
    # auditor's NOT_READY verdict rule).  PCB-only / value / footprint drift
    # is advisory unless --strict.
    if analysis.schematic_orphans:
        return 2
    if strict and has_drift(analysis):
        return 2
    return 0


def _erc_subcheck(sch_path: Path | None, strict: bool) -> SubCheckResult:
    """Run kicad-cli ERC against the discovered schematic (issue #3750).

    Returns ``NOT RUN`` when no schematic is found.  Returns ``FAILED``
    when kicad-cli is missing, the schematic fails to load, or the report
    contains any errors (and, under ``strict``, any warnings).
    """
    if sch_path is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="no schematic discovered next to PCB",
        )

    from kicad_tools.cli.runner import find_kicad_cli, run_erc
    from kicad_tools.erc import ERCReport

    if find_kicad_cli() is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="kicad-cli not found in PATH; install KiCad 8+ to enable ERC",
        )

    cli_result = run_erc(sch_path, format="json")
    if not cli_result.success or cli_result.output_path is None:
        return SubCheckResult(
            status="FAILED",
            detail=f"kicad-cli ERC failed: {(cli_result.stderr or '').strip().splitlines()[-1] if cli_result.stderr else 'unknown error'}",
        )

    try:
        report = ERCReport.load(cli_result.output_path)
    except Exception as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"failed to parse ERC report: {e}",
        )

    err_count = report.error_count
    warn_count = report.warning_count
    detail = f"{err_count} error(s), {warn_count} warning(s)"
    if err_count > 0:
        return SubCheckResult(status="FAILED", detail=detail)
    if strict and warn_count > 0:
        return SubCheckResult(status="FAILED", detail=detail + " (strict)")
    return SubCheckResult(status="PASSED", detail=detail)


def _lvs_subcheck(sch_path: Path | None, pcb_path: Path) -> SubCheckResult:
    """Run live LVS (issue #3750, extended for independent copper LVS #3742).

    Runs *two* complementary comparisons and fails if **either** dirties:

    * **label-based** (:func:`board_lvs.compare_netlists`) — trusts each
      pad's declared ``(net ...)`` label; catches generator/router
      bookkeeping drift.
    * **copper-extracted** (:func:`copper_lvs.compare_copper_netlist`) —
      ignores pad labels entirely and diffs the *physical* copper partition
      against the schematic; catches shorts/opens a mislabeled router would
      hide from the label-based path (the board-00 soundness gap, #3742).

    Always recomputes -- never reads ``output/lvs.json`` -- so a fresh
    PCB edit that breaks LVS is surfaced immediately.  Returns
    ``NOT RUN`` when no schematic is found.
    """
    if sch_path is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="no schematic discovered; cannot compare",
        )

    try:
        from kicad_tools.lvs.board_lvs import compare_netlists

        result = compare_netlists(sch_path, pcb_path)
    except Exception as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"LVS comparator raised {type(e).__name__}: {e}",
        )

    try:
        from kicad_tools.lvs.copper_lvs import compare_copper_netlist

        copper = compare_copper_netlist(sch_path, pcb_path)
    except Exception as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"copper LVS comparator raised {type(e).__name__}: {e}",
        )

    if not copper.clean:
        # The copper-extracted gate is the soundness-critical one: surface
        # it first.  Show up to the first 3 records in a stable order.
        records = sorted(copper.mismatches, key=lambda m: (m.kind, m.pad_a, m.pad_b))
        preview = ", ".join(
            f"{m.kind} {m.pad_a}({m.net_a})/{m.pad_b}({m.net_b})" for m in records[:3]
        )
        suffix = "" if len(records) <= 3 else f" (+{len(records) - 3} more)"
        return SubCheckResult(
            status="FAILED",
            detail=f"copper: {len(records)} mismatch(es): {preview}{suffix}",
        )

    if result.clean:
        return SubCheckResult(
            status="PASSED",
            detail="label + copper: 0 mismatch(es)",
        )

    # Show up to the first 3 mismatches in stable (ref, pad) order so
    # the detail line is bounded but informative.
    mismatches = sorted(result.mismatches, key=lambda m: (m.ref, m.pad))
    preview = ", ".join(
        f"{m.ref}.{m.pad} sch={m.schematic_net!r} pcb={m.pcb_net!r}" for m in mismatches[:3]
    )
    suffix = "" if len(mismatches) <= 3 else f" (+{len(mismatches) - 3} more)"
    return SubCheckResult(
        status="FAILED",
        detail=f"label: {len(mismatches)} mismatch(es): {preview}{suffix}",
    )


# Issue #4096: the clearance rule_ids whose truthfulness depends entirely on
# the freshness of the committed `filled_polygon` geometry.  When any of these
# fire, the on-disk fills may be stale (board routed/refilled after the fills
# were last saved) and the findings may be phantom.
_ZONE_FILL_DEPENDENT_RULE_IDS: frozenset[str] = frozenset(
    {
        "clearance_segment_zone",
        "clearance_via_zone",
        "clearance_pad_zone",
    }
)


def _warn_stale_zone_fills(violations: list[DRCViolation], pcb_path: Path) -> None:
    """Warn loudly when zone-clearance findings may reflect stale fills (issue #4096).

    ``kct check`` measures segment/via/pad-to-zone clearance against the
    committed ``filled_polygon`` geometry in the ``.kicad_pcb`` — whatever
    KiCad last wrote to disk.  If the board was routed or refilled after those
    fills were saved, the clearance_*_zone rules produce phantom shorts that
    disappear once the fills are refreshed.  This advisory points the user at
    the authoritative fix so an honest-looking wall of clearance errors is not
    mistaken for an unmanufacturable board.  No-op when no such findings exist.
    """
    present = sorted({v.rule_id for v in violations if v.rule_id in _ZONE_FILL_DEPENDENT_RULE_IDS})
    if not present:
        return
    print(
        "WARNING: zone-clearance findings present "
        f"({', '.join(present)}) — kct check measures these against the "
        "committed zone fills in the .kicad_pcb, which may be STALE if the "
        "board was routed or refilled after the fills were last saved. "
        "Cross-gate / fix with:\n"
        f"    kicad-cli pcb drc --refill-zones --save-board {pcb_path}\n"
        "then re-run kct check, or pass --refill-zones to do this "
        "automatically (issue #4096).",
        file=sys.stderr,
    )


def _refill_zones_in_place(pcb_path: Path) -> None:
    """Refresh the on-disk zone fills before checking, if possible (issue #4096).

    Shells out to ``kicad-cli pcb drc --refill-zones --save-board`` via
    :func:`kicad_tools.cli.runner.run_refill_zones` so the pure-Python pipeline
    reads fills that are in sync with the copper.  This **mutates the board
    file in place** (the ``--refill-zones`` flag documents that side effect).

    Graceful degradation: a missing kicad-cli — or any refill failure — is
    reported as a warning and the check continues against the stored fills
    rather than aborting.  Never raises.
    """
    from kicad_tools.cli.runner import run_refill_zones

    result = run_refill_zones(pcb_path)
    if result.success:
        print(
            f"[INFO] refilled zones in place via kicad-cli: {pcb_path}",
            file=sys.stderr,
        )
    else:
        print(
            "WARNING: --refill-zones requested but the refill did not run "
            f"({result.stderr.strip() or 'unknown error'}); continuing against "
            "the stored (possibly stale) zone fills.  Install KiCad 8+ so "
            "kicad-cli is on PATH to enable the pre-check refill (issue #4096).",
            file=sys.stderr,
        )


def _manifest_subcheck(pcb_path: Path) -> SubCheckResult:
    """Compare ``output/manufacturing/manifest.json`` mtime against the PCB.

    Resolution path (issue #3750):

    * Look for ``<pcb-dir>/manufacturing/manifest.json`` first (recipes
      that place the routed PCB next to a ``manufacturing/`` peer).
    * Then ``<pcb-dir>/../manufacturing/manifest.json`` for layouts where
      the PCB is one level deeper.

    Returns ``NOT RUN`` when neither manifest is present, ``FAILED``
    (rendered as ``STALE`` in human output) when the routed PCB is newer
    than the manifest, and ``PASSED`` otherwise.
    """
    candidates = [
        pcb_path.parent / "manufacturing" / "manifest.json",
        pcb_path.parent.parent / "manufacturing" / "manifest.json",
    ]
    manifest_path: Path | None = None
    for cand in candidates:
        if cand.exists():
            manifest_path = cand
            break

    if manifest_path is None:
        return SubCheckResult(
            status="NOT RUN",
            detail="no manufacturing bundle; run `kct export` first",
        )

    try:
        pcb_mtime = pcb_path.stat().st_mtime
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError as e:
        return SubCheckResult(
            status="FAILED",
            detail=f"failed to stat manifest or PCB: {e}",
        )

    # Allow a small mtime tolerance so a fresh ``git checkout`` (which
    # writes files sequentially with sub-microsecond gaps) does not
    # spuriously flag the manifest as stale: the PCB and manifest are
    # written within milliseconds of each other by ``kct export``, while
    # a *real* stale manifest lags by minutes or longer (any rebuild of
    # the routed PCB that skipped ``kct export`` produces a multi-second
    # gap).  ``MANIFEST_FRESHNESS_TOLERANCE_S`` carves that gap.
    MANIFEST_FRESHNESS_TOLERANCE_S = 5.0
    delta = pcb_mtime - manifest_mtime
    if delta > MANIFEST_FRESHNESS_TOLERANCE_S:
        return SubCheckResult(
            status="FAILED",
            detail=f"STALE: routed PCB is {delta:.1f}s newer than manifest.json",
        )

    return SubCheckResult(
        status="PASSED",
        detail="manifest.json mtime within tolerance of routed PCB mtime",
    )


def run_meta_checks(
    pcb_path: Path,
    drc_status: SubCheckResult,
    schematic: str | None = None,
    strict: bool = False,
) -> MetaCheckResult:
    """Run the four meta sub-checks (DRC + ERC + LVS + Manifest).

    DRC is supplied by the caller (it has already run as part of the
    main check pipeline); this helper layers ERC, LVS, and manifest
    freshness on top and rolls them up into a single
    :class:`MetaCheckResult` (issue #3750).

    Args:
        pcb_path: Path to the routed ``.kicad_pcb`` under test.
        drc_status: Pre-computed DRC :class:`SubCheckResult` from the
            current invocation's DRC pipeline.  Folded in directly so the
            meta rollup doesn't redo the DRC work.
        schematic: Optional explicit ``.kicad_sch`` override.  When
            omitted, schematic discovery falls back to
            :func:`kicad_tools.sync.discover.resolve_schematic_for_pcb`
            (handles the ``_routed`` suffix strip used by recipes).
        strict: When True, ERC warnings become fatal (sub-check ``FAILED``).
            ``NOT RUN`` rollup behaviour is independent of ``strict`` --
            the exit-code policy in :func:`main` controls whether
            ``INCOMPLETE`` exits non-zero (the new default) or 0 (with
            ``--allow-incomplete``).
    """
    from kicad_tools.sync.discover import resolve_schematic_for_pcb

    resolved_sch: Path | None
    if schematic is not None:
        candidate = Path(schematic).resolve()
        resolved_sch = candidate if candidate.exists() else None
    else:
        resolved_sch = resolve_schematic_for_pcb(pcb_path)

    erc = _erc_subcheck(resolved_sch, strict)
    lvs = _lvs_subcheck(resolved_sch, pcb_path)
    manifest = _manifest_subcheck(pcb_path)

    # Issue #4350: when discovery turned up no schematic, ERC and (critically)
    # the LVS *manufacturing hard gate* are silently NOT RUN.  Emit a loud
    # one-line warning to stderr so a skipped hard gate is never mistaken for a
    # clean comparison, and record a machine-detectable flag for JSON consumers.
    schematic_missing = resolved_sch is None
    if schematic_missing:
        print(
            "WARNING: no schematic discovered next to "
            f"{pcb_path.name}; ERC and the LVS manufacturing hard gate were "
            "SKIPPED (not run) -- copper was NOT compared to any schematic. "
            "Pass --schematic <path.kicad_sch> to run them.",
            file=sys.stderr,
        )

    result = MetaCheckResult(
        drc=drc_status,
        erc=erc,
        lvs=lvs,
        manifest=manifest,
        schematic_missing=schematic_missing,
    )
    result.compute_overall()
    return result


def _format_meta_status_line(name: str, sub: SubCheckResult) -> str:
    """Render one human-output ``DRC: PASSED (...)`` line.

    ``STALE`` is rendered in place of ``FAILED`` for the Manifest
    sub-check when the detail starts with ``STALE:`` (issue #3750's
    human-clarity convention).  The JSON status is still ``FAILED``.
    """
    # ``display_status`` is intentionally widened to ``str`` so we can
    # substitute the human-only ``STALE`` token for the Manifest row
    # without violating the narrow ``SubCheckStatus`` literal type that
    # ``sub.status`` is annotated as.
    display_status: str = sub.status
    detail = sub.detail
    if name == "Manifest" and sub.status == "FAILED" and detail.startswith("STALE:"):
        display_status = "STALE"
        # Trim the "STALE: " prefix from the detail since the status
        # column already carries it.
        detail = detail[len("STALE: ") :]
    return f"{name + ':':10} {display_status:8} ({detail})"


def print_meta_check_stanza(result: MetaCheckResult) -> None:
    """Print the per-sub-check status block + overall rollup (issue #3750).

    Output goes to stdout in a stable column layout so humans can
    diff it across runs.  The ``Overall:`` line is the rollup that
    matches the exit-code decision.
    """
    print()
    print(_format_meta_status_line("DRC", result.drc))
    print(_format_meta_status_line("ERC", result.erc))
    print(_format_meta_status_line("LVS", result.lvs))
    print(_format_meta_status_line("Manifest", result.manifest))
    print(f"{'Overall:':10} {result.overall}")


CHECK_CATEGORIES = [
    "ampacity",
    "clearance",
    "connectivity",
    "segment_zone",
    "via_zone",
    "copper_sliver",
    "courtyard_overlap",
    "diffpair_clearance_intra",
    "diffpair_length_skew",
    "diffpair_routing_continuity",
    "dimensions",
    "edge",
    "impedance",
    "match_group_length_skew",
    "netlist",
    "pad_grid",
    "placement",
    "silkscreen",
    "single_pad_net",
    "solder_mask",
    "via_in_pad",
    "zones",
]


def _warn_unevaluated_ampacity(
    declared_ampacity_targets: dict[str, float],
    ampacity_resolution: object,
    pcb: PCB,
    only_set: set[str] | None,
    skip_set: set[str],
) -> None:
    """Warn when a declared ``target_ampacity`` was never actually evaluated.

    Issue #4321 (Tier 3).  ``AmpacityRule`` matches routed segments by net
    name, so a declared target contributes a violation *only* when the net
    both resolves to a real board net **and** carries at least one routed
    segment **and** the ``ampacity`` category ran.  When none of those hold
    the report shows 0 ampacity errors -- indistinguishable from a genuinely
    compliant board.  This emits a loud stderr warning naming every declared
    net whose rule never engaged, so a hand-authored ``--net-class-map`` that
    silently fails to match cannot pass green.

    Args:
        declared_ampacity_targets: ``{user_key: target_ampacity}`` for every
            net-class-map entry that declared a ``target_ampacity`` (keyed by
            the *original* user key, before board-name resolution).
        ampacity_resolution: the ``NetClassMapResolution`` returned by
            ``resolve_net_class_map_keys`` (``resolved`` is
            ``{board_net: user_key}``).
        pcb: the loaded board (source of routed-segment net names).
        only_set: optional ``--only`` whitelist of check categories.
        skip_set: ``--skip`` set of check categories.
    """
    ampacity_active = (only_set is None or "ampacity" in only_set) and "ampacity" not in skip_set

    # Invert {board_net: user_key} so we can look up each declared user key's
    # resolved board net (absent => the key matched no board net or matched
    # ambiguously, i.e. it was dropped from the resolved map).
    resolved = getattr(ampacity_resolution, "resolved", {}) or {}
    key_to_board = {user_key: board_net for board_net, user_key in resolved.items()}

    segment_nets = {getattr(seg, "net_name", "") for seg in getattr(pcb, "segments", [])}

    unevaluated: list[str] = []
    for user_key in sorted(declared_ampacity_targets):
        board_net = key_to_board.get(user_key)
        if board_net is None:
            # Resolution miss / ambiguous: the rule can never see this net.
            unevaluated.append(user_key)
        elif not ampacity_active:
            # Net resolved but the whole category was skipped/excluded.
            unevaluated.append(board_net)
        elif board_net not in segment_nets:
            # Net resolved but carries no routed segment for the rule to size.
            unevaluated.append(board_net)

    # Deduplicate while preserving first-seen order.
    unevaluated = list(dict.fromkeys(unevaluated))
    if not unevaluated:
        return

    print(
        "WARNING: ampacity rule declared but not evaluated for net(s): "
        f"{', '.join(unevaluated)}. "
        "A declared target_ampacity that matched zero routed segments is "
        "reported as 0 ampacity errors but was never actually checked "
        "(post-resolution key miss, unrouted net, or the ampacity category "
        "was excluded via --skip/--only).",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct check command."""
    parser = argparse.ArgumentParser(
        prog="kct check",
        description="Pure Python DRC for PCBs (no kicad-cli required)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pcb",
        help="Path to .kicad_pcb file or directory containing one",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Show only errors, not warnings",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error code 2 on warnings",
    )
    parser.add_argument(
        "--strict-connectivity",
        dest="strict_connectivity",
        action="store_true",
        help=(
            "Decide the connectivity DRC rule by REAL geometric copper contact "
            "(shapely polygon intersection) instead of the default 0.01mm "
            "endpoint-proximity tolerance. The default model unions a segment "
            "endpoint with a pad/via/segment whenever their reference points "
            "land within 0.01mm, even when the actual copper (segment width, "
            "pad shape) does not touch -- so it can pass a net that "
            "'kicad-cli pcb drc' reports as unconnected. --strict-connectivity "
            "matches KiCad's connectivity semantics (issue #4176). Requires "
            "shapely. (Distinct from --strict, which makes warnings fatal.)"
        ),
    )
    parser.add_argument(
        "--refill-zones",
        dest="refill_zones",
        action="store_true",
        help=(
            "Before checking, run `kicad-cli pcb drc --refill-zones "
            "--save-board` to bring the on-disk zone fills back in sync with "
            "the copper (issue #4096).  WARNING: this MUTATES the board file "
            "in place (--save-board rewrites <pcb>).  Fixes phantom "
            "clearance_*_zone findings caused by stale committed fills.  "
            "Requires kicad-cli (KiCad 8+); degrades gracefully with a "
            "warning if kicad-cli is not installed (the check still runs "
            "against the stored fills)."
        ),
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        default="jlcpcb",
        help="Target manufacturer for design rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=None,
        help="Number of copper layers (auto-detected from board if not specified)",
    )
    parser.add_argument(
        "--copper",
        "-c",
        default=None,
        metavar="OZ",
        help=(
            "Copper weight in oz for the ampacity gate. Scalar form "
            "'--copper 2' applies to both outer and inner layers; keyed "
            "form '--copper outer=2,inner=0.5' sets each layer class "
            "independently (e.g. a JLCPCB 2oz-outer / 0.5oz-inner order, "
            "where the inner stays 0.5oz even on a 2oz build). Precedence: "
            "explicit --copper (keyed > scalar) > the board's declared "
            "(setup (stackup ...)) copper weight > profile default "
            "(1oz outer / 0.5oz inner). When --copper is omitted, an "
            "explicit board stackup is the source of truth; a stackup that "
            "disagrees with an explicit --copper emits a WARNING and is "
            "fatal under --strict."
        ),
    )
    parser.add_argument(
        "--only",
        dest="only_checks",
        help=f"Run only specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--skip",
        dest="skip_checks",
        help=f"Skip specific checks (comma-separated: {', '.join(CHECK_CATEGORIES)})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Write JSON report to file (implies --format json for file output)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed violation information",
    )
    parser.add_argument(
        "--suppress-library",
        action="store_true",
        help="Suppress silkscreen warnings from standard KiCad library footprints",
    )
    parser.add_argument(
        "--drc-only",
        dest="drc_only",
        action="store_true",
        help=(
            "Legacy DRC-only mode (issue #3750).  Skips the ERC / LVS / "
            "Manifest meta sub-checks and preserves the pre-#3750 stdout "
            "and exit-code contract.  Intended for CI scripts and recipes "
            "that depend on the historical 'kct check' semantics (e.g. "
            "scripts/ci/check_routed_drc.py and the per-board allowlists "
            "in .github/routed-drc-tolerance.yml)."
        ),
    )
    parser.add_argument(
        "--allow-incomplete",
        dest="allow_incomplete",
        action="store_true",
        help=(
            "Treat ``Overall: INCOMPLETE`` (any sub-check is NOT RUN) as a "
            "passing run for exit-code purposes (issue #3750).  By default "
            "INCOMPLETE exits non-zero so consumers that read the exit code "
            "do not silently accept a board that was only partially verified.  "
            "Use this for boards / recipes that legitimately lack a sub-check "
            "input (e.g. no schematic next to the PCB, or a recipe that runs "
            "``kct check`` before ``kct export`` produces the manifest)."
        ),
    )
    parser.add_argument(
        "--netlist-sync",
        action="store_true",
        help=(
            "Run a blocking schematic/PCB netlist-sync gate (issue #3154). "
            "Compares the schematic component set against the PCB footprint set "
            "via the Reconciler and prints a full add/drop/orphan report. Exits "
            "with code 2 when components present in the schematic are missing "
            "from the PCB (unbuildable BOM). PCB-only/value/footprint drift is a "
            "warning unless --strict. Skips silently if no schematic is found."
        ),
    )
    parser.add_argument(
        "--schematic",
        default=None,
        help=(
            "Explicit path to the .kicad_sch file for the netlist-sync gate / "
            "advisory drift banner. When omitted, the schematic is "
            "auto-discovered from project.kct or the sibling <basename>.kicad_sch."
        ),
    )
    parser.add_argument(
        "--net-class-map",
        dest="net_class_map",
        default=None,
        help=(
            "Path to a JSON sidecar mapping net names to NetClassRouting "
            "fields (see kicad_tools.router.rules.NetClassRouting.to_dict). "
            "When supplied, enables the diff-pair routing_continuity and "
            "length_skew rules to fire on routed boards; without it those "
            "rules degrade to no-ops (Issue #2684)."
        ),
    )
    parser.add_argument(
        "--courtyard-waivers",
        dest="courtyard_waivers",
        default=None,
        help=(
            "Path to a .courtyard_waivers.json sidecar waiving specific "
            "courtyard-overlap pairs (see kicad_tools.validate.rules."
            "courtyard_waivers). When supplied, overlapping courtyard pairs "
            "matching a waiver entry report as WAIVED instead of failing the "
            "gate. Auto-discovered next to the board when this flag is "
            "omitted (Issue #4137)."
        ),
    )
    # Issue #3061: auto-derive the pad_grid tolerance from each board's
    # pad-offset histogram by default for the CLI.  Users can opt back into
    # the fixed-0.05mm behaviour with --pad-grid-strict, or pin a custom
    # value with --pad-grid-tolerance.
    pad_grid_group = parser.add_mutually_exclusive_group()
    pad_grid_group.add_argument(
        "--pad-grid-strict",
        action="store_true",
        help=(
            "Use the fixed 0.05mm pad_grid tolerance (PR #3057 default) "
            "instead of auto-deriving per-board from the pad-offset "
            "histogram (issue #3061).  Default: auto-derive."
        ),
    )
    pad_grid_group.add_argument(
        "--pad-grid-tolerance",
        type=float,
        default=None,
        metavar="MM",
        help=(
            "Override the pad_grid L2 tolerance with an explicit value "
            "in mm (e.g. ``--pad-grid-tolerance 0.02``).  Disables "
            "auto-derivation."
        ),
    )

    args = parser.parse_args(argv)

    # Parse and validate filter options
    only_set: set[str] | None = None
    skip_set: set[str] = set()

    if args.only_checks:
        only_set = set()
        for cat in args.only_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            only_set.add(cat)

    if args.skip_checks:
        for cat in args.skip_checks.split(","):
            cat = cat.strip().lower()
            if cat not in CHECK_CATEGORIES:
                print(f"Error: Unknown check category: {cat!r}", file=sys.stderr)
                print(f"Available: {', '.join(CHECK_CATEGORIES)}", file=sys.stderr)
                return 1
            skip_set.add(cat)

    # Load PCB - resolve to absolute path for reliable file access
    # Handles both file paths and directory paths (like kct build)
    input_path = Path(args.pcb).resolve()

    if not input_path.exists():
        print(f"Error: Path not found: {input_path}", file=sys.stderr)
        return 1

    if input_path.is_dir():
        # Auto-discover PCB file in directory (consistent with kct build)
        pcb_path = _find_pcb_file(input_path)
        if pcb_path is None:
            print(f"Error: No .kicad_pcb file found in directory: {input_path}", file=sys.stderr)
            print(
                "Hint: Specify a .kicad_pcb file directly, or ensure the directory contains one.",
                file=sys.stderr,
            )
            return 1
    elif input_path.suffix != ".kicad_pcb":
        print(f"Error: Expected .kicad_pcb file, got: {input_path.name}", file=sys.stderr)
        print("Hint: Provide a .kicad_pcb file or a directory containing one.", file=sys.stderr)
        return 1
    else:
        pcb_path = input_path

    # Netlist-sync gate (issue #3154): a dedicated, blocking schematic/PCB
    # drift check that runs *instead of* the DRC pipeline and returns its own
    # exit code.  Reuses the Reconciler via the shared drift helpers.
    if getattr(args, "netlist_sync", False):
        return run_netlist_sync_gate(
            pcb_path,
            schematic=getattr(args, "schematic", None),
            strict=args.strict,
        )

    # Optional pre-check zone refill (issue #4096).  When --refill-zones is
    # set, shell out to `kicad-cli pcb drc --refill-zones --save-board` so the
    # on-disk fills are refreshed *before* PCB.load reads them — otherwise the
    # pure-Python clearance rules measure copper against stale committed fills
    # and report phantom clearance_*_zone shorts.  Degrades gracefully: a
    # missing kicad-cli (or a failed refill) warns and continues against the
    # stored fills rather than aborting the check.
    if getattr(args, "refill_zones", False):
        _refill_zones_in_place(pcb_path)

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Advisory drift banner (issue #3154): when a schematic is discovered (or
    # passed via --schematic) and the component sets have drifted, print a
    # one-line, non-blocking warning before running DRC.  Never affects the
    # exit code on the default run -- the hard gate lives behind --netlist-sync.
    _emit_drift_banner(pcb_path, getattr(args, "schematic", None))

    # Auto-detect layer count from PCB if not explicitly provided
    if args.layers is not None:
        layers = args.layers
    else:
        detected = len(pcb.copper_layers)
        layers = detected if detected > 0 else 2

    # Load optional net-class-map sidecar (Issue #2684).  When supplied,
    # the diff-pair routing-continuity and length-skew rules can re-derive
    # engagement / skew state from the routed PCB and fire.  When omitted,
    # the rules degrade to no-ops (AC #3: graceful-degradation contract).
    net_class_map = None
    # Issue #3917 Defect 2: when the user did not pass --net-class-map,
    # auto-discover the conventional sidecar written by ``kct route`` next
    # to the routed PCB.  An explicit flag always wins and short-circuits
    # the probe (AC3: no double-load).
    ncm_explicit = args.net_class_map is not None
    if ncm_explicit:
        ncm_path: Path | None = Path(args.net_class_map).resolve()
    else:
        ncm_path = _discover_net_class_map_sidecar(pcb_path)

    if ncm_path is not None:
        from kicad_tools.router.rules import net_class_map_from_dict

        if not ncm_path.exists():
            # Only reachable via an explicit flag (the auto-probe returns
            # existing files only).
            print(f"Error: net-class-map file not found: {ncm_path}", file=sys.stderr)
            return 1
        ncm_load_error: str | None = None
        net_class_map = None
        try:
            ncm_data = json.loads(ncm_path.read_text())
            net_class_map = net_class_map_from_dict(ncm_data)
        except json.JSONDecodeError as e:
            ncm_load_error = f"parsing net-class-map JSON: {e}"
        except (TypeError, ValueError) as e:
            ncm_load_error = f"invalid net-class-map structure: {e}"

        if ncm_load_error is not None:
            if ncm_explicit:
                # An explicit path that fails to load is a hard error --
                # the user asked for it specifically.
                print(f"Error: {ncm_load_error}", file=sys.stderr)
                return 1
            # An auto-discovered sidecar that fails to load degrades
            # gracefully: warn and fall back to no-sidecar behaviour
            # rather than crashing the whole check (Issue #3917 edge case).
            print(
                f"WARNING: ignoring malformed net-class-map sidecar {ncm_path}: {ncm_load_error}",
                file=sys.stderr,
            )
            net_class_map = None
        elif not ncm_explicit:
            # Auto-loaded successfully: tell the user which file engaged
            # the sidecar-gated rules (AC2).
            print(
                f"[INFO] auto-loaded net-class-map sidecar: {ncm_path}",
                file=sys.stderr,
            )

    # Issue #3440: the skew rules (match_group_length_skew,
    # diffpair_length_skew, diffpair_routing_continuity) degrade to
    # silent no-ops without the --net-class-map sidecar -- "Rules
    # checked" excludes them and the check PASSES even with 15mm of
    # group skew on the board.  Warn LOUDLY when any of those rules is
    # selected but cannot engage, so a recipe that forgot the sidecar
    # doesn't sail through green.
    if net_class_map is None:
        _sidecar_dependent_rules = (
            "match_group_length_skew",
            "diffpair_length_skew",
            "diffpair_routing_continuity",
        )
        _inactive_rules = [
            rule
            for rule in _sidecar_dependent_rules
            if (only_set is None or rule in only_set) and rule not in skip_set
        ]
        if _inactive_rules:
            print(
                "WARNING: the following rules are INACTIVE without "
                "--net-class-map and will silently pass: "
                f"{', '.join(_inactive_rules)}.  Pass the routed board's "
                "sidecar (e.g. output/net_class_map.json) to validate "
                "length-match skew.",
                file=sys.stderr,
            )

    # Load optional courtyard-waivers sidecar (Issue #4137).  An explicit
    # --courtyard-waivers path always wins and a malformed explicit file is a
    # hard error; an auto-discovered sidecar that fails to parse degrades
    # gracefully (warn + zero waivers) -- mirroring the --net-class-map
    # contract above exactly.
    from kicad_tools.validate.rules.courtyard_waivers import (
        discover_courtyard_waivers_sidecar,
        load_courtyard_waivers,
    )

    courtyard_waivers = None
    cw_explicit = args.courtyard_waivers is not None
    if cw_explicit:
        cw_path: Path | None = Path(args.courtyard_waivers).resolve()
    else:
        cw_path = discover_courtyard_waivers_sidecar(pcb_path)

    if cw_path is not None:
        if not cw_path.exists():
            # Only reachable via an explicit flag (the auto-probe returns
            # existing files only).
            print(f"Error: courtyard-waivers file not found: {cw_path}", file=sys.stderr)
            return 1
        try:
            courtyard_waivers = load_courtyard_waivers(cw_path)
        except ValueError as e:
            if cw_explicit:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            print(
                f"WARNING: ignoring malformed courtyard-waivers sidecar {cw_path}: {e}",
                file=sys.stderr,
            )
            courtyard_waivers = None
        else:
            if not cw_explicit:
                print(
                    f"[INFO] auto-loaded courtyard-waivers sidecar: {cw_path}",
                    file=sys.stderr,
                )

    # Issue #4321 (Tier 1/2): resolve the loaded net-class-map's user keys
    # onto the board's actual net names *before* handing the map to
    # DRCChecker, mirroring ``route_cmd._apply_net_class_map_sidecar``.
    #
    # ``kct check`` reaches ampacity via
    # ``DRCChecker.check_ampacity`` -> ``derive_ampacity_specs(net_class_map)``
    # -> ``AmpacityRule.check``, which matches segments by
    # ``segment.net_name in self.specs`` -- i.e. only when the map's keys
    # land exactly on the board's segment net names.  ``route`` resolves the
    # map's keys onto board net names before use; ``check`` historically did
    # NOT.  A hand-authored ``--net-class-map`` (bare keys, or keys lacking
    # KiCad's hierarchical ``/`` sheet prefix) therefore matched ZERO
    # segments, so ``derive_ampacity_specs`` produced specs that never fired
    # -> 0 errors -> a silent false PASS on a dangerously under-width
    # high-current trace.  After resolution the ampacity verdict is a pure
    # function of ``(board segments, resolved map, DesignRules copper
    # weights)`` and no longer depends on how the board was produced (route
    # mode / ``.kicad_dru`` presence).  This applies to both the explicit
    # ``--net-class-map`` path and the auto-discovered route sidecar (whose
    # keys are already board-resolved, so re-resolution is idempotent).
    declared_ampacity_targets: dict[str, float] = {}
    ampacity_resolution = None
    if net_class_map is not None:
        from kicad_tools.router.net_names import resolve_net_class_map_keys

        for key, nc in net_class_map.items():
            target = getattr(nc, "target_ampacity", None)
            if target is not None:
                declared_ampacity_targets[key] = float(target)
        board_net_names = [net.name for net in pcb.nets.values() if net.name]
        ampacity_resolution = resolve_net_class_map_keys(net_class_map.keys(), board_net_names)
        net_class_map = {
            board_net: net_class_map[user_key]
            for board_net, user_key in ampacity_resolution.resolved.items()
        }

    # Issue #4326: resolve the ampacity gate's copper weights (outer / inner
    # oz).  Precedence, per layer class: an EXPLICIT ``--copper`` (keyed >
    # scalar) > the board's DECLARED ``(setup (stackup ...))`` copper weight
    # (Tier 1 -- the new default source of truth) > the profile default.
    #
    # This override is scoped to the ampacity copper weights ONLY
    # (design_rules.outer_copper_oz / inner_copper_oz); the preset's
    # min-trace-width / clearance rules stay governed by ``--copper`` /
    # ``--mfr`` exactly as before, so Tier 1 cannot flip unrelated verdicts.
    cli_copper_outer: float | None = None
    cli_copper_inner: float | None = None
    copper_explicit = args.copper is not None
    if copper_explicit:
        try:
            cli_copper_outer, cli_copper_inner = _parse_copper_weight_arg(args.copper)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Preset selection still keys on the scalar/outer ``--copper`` (or the
    # historical 1oz default) -- only the ampacity copper weights follow the
    # declared stackup.
    preset_copper_oz = cli_copper_outer if cli_copper_outer is not None else 1.0

    # Tier 1: derive copper weights from the board's declared stackup.
    stackup_outer: float | None = None
    stackup_inner: float | None = None
    try:
        from kicad_tools.physics.stackup import Stackup

        _derived = Stackup.from_pcb(pcb).outer_inner_copper_oz()
        if _derived is not None:
            stackup_outer, stackup_inner = _derived
    except Exception:  # noqa: BLE001 - never crash `check` on a stackup parse quirk
        stackup_outer = stackup_inner = None

    # Per-class resolution: explicit --copper wins, else the declared
    # stackup, else None (leave the profile default in place).
    resolved_copper_outer = cli_copper_outer if cli_copper_outer is not None else stackup_outer
    resolved_copper_inner = cli_copper_inner if cli_copper_inner is not None else stackup_inner

    # Tier 2: loud warning when an EXPLICIT --copper disagrees with the
    # board's declared stackup.  Explicit --copper WINS (a deliberate
    # operator override), but silently ignoring the board's declaration
    # would itself be a footgun, so we always warn -- and under --strict the
    # disagreement is fatal so an autonomous agent cannot sail past it.
    def _copper_disagrees(cli_oz: float | None, stk_oz: float | None) -> bool:
        return cli_oz is not None and stk_oz is not None and abs(cli_oz - stk_oz) > 1e-6

    copper_disagreement = False
    if copper_explicit:
        if _copper_disagrees(cli_copper_outer, stackup_outer):
            copper_disagreement = True
            print(
                f"WARNING: stackup declares {stackup_outer:g}oz outer but --copper "
                f"requests {cli_copper_outer:g}oz — ampacity is evaluating at "
                f"{cli_copper_outer:g}oz. Pass --copper {stackup_outer:g} or fix the stackup.",
                file=sys.stderr,
            )
        if _copper_disagrees(cli_copper_inner, stackup_inner):
            copper_disagreement = True
            print(
                f"WARNING: stackup declares {stackup_inner:g}oz inner but --copper "
                f"requests {cli_copper_inner:g}oz — ampacity is evaluating at "
                f"{cli_copper_inner:g}oz. Pass --copper inner={stackup_inner:g} or fix the stackup.",
                file=sys.stderr,
            )
        if copper_disagreement and args.strict:
            print(
                "ERROR: --strict: stackup-vs---copper copper-weight disagreement is "
                "fatal (exit 2). Reconcile --copper with the board's declared stackup.",
                file=sys.stderr,
            )

    # Create checker with manufacturer rules
    try:
        checker = DRCChecker(
            pcb,
            manufacturer=args.mfr,
            layers=layers,
            copper_oz=preset_copper_oz,
            copper_oz_outer=resolved_copper_outer,
            copper_oz_inner=resolved_copper_inner,
            suppress_library=args.suppress_library,
            net_class_map=net_class_map,
            # The CLI already prints its own up-front INACTIVE warning
            # below, so suppress the per-rule checker-level warning here to
            # avoid duplicating it (Issue #3917 Defect 3).
            warn_on_inactive_skew_rules=False,
            verbose=args.verbose,
            # Always collect the per-pair / per-group measured skew info
            # findings so ``output_table`` can render the measurement
            # summary at default verbosity (Issue #3924 AC1).  With no
            # sidecar the skew rules produce no info findings, so this is a
            # graceful no-op (AC5).
            emit_measurements=True,
            courtyard_waivers=courtyard_waivers,
            strict_connectivity=args.strict_connectivity,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Resolve pad_grid tolerance policy (issue #3061).
    # Precedence: explicit value > strict mode > auto-derive (CLI default).
    if args.pad_grid_tolerance is not None:
        pad_grid_threshold: float | None = args.pad_grid_tolerance
        pad_grid_auto_derive = False
    elif args.pad_grid_strict:
        pad_grid_threshold = None  # Falls through to DEFAULT_PAD_GRID_TOLERANCE_MM
        pad_grid_auto_derive = False
    else:
        pad_grid_threshold = None
        pad_grid_auto_derive = True

    # Run selected checks
    results = run_selected_checks(
        checker,
        only_set,
        skip_set,
        pad_grid_threshold=pad_grid_threshold,
        pad_grid_auto_derive=pad_grid_auto_derive,
    )

    # Issue #4321 (Tier 3): fail loud when an ampacity target was declared
    # but never evaluated.  A declared ``target_ampacity`` that matched zero
    # segments is indistinguishable, in the report, from "genuinely 0
    # violations" -- yet it usually means the rule never engaged: a
    # post-resolution key miss, an unrouted net, or the ``ampacity`` category
    # excluded via ``--skip``/``--only``.  Emit a loud stderr warning naming
    # the net(s) so a hand-authored map that silently fails to match cannot
    # sail through green (mirrors the INACTIVE-skew-rules warning above).
    if declared_ampacity_targets and ampacity_resolution is not None:
        _warn_unevaluated_ampacity(
            declared_ampacity_targets,
            ampacity_resolution,
            pcb,
            only_set,
            skip_set,
        )

    # Apply errors-only filter
    violations = list(results.violations)
    if args.errors_only:
        violations = [v for v in violations if v.is_error]

    # Fill-freshness advisory (issue #4096).  The clearance_*_zone rules
    # measure copper against the committed `filled_polygon` geometry, which is
    # only trustworthy if the on-disk fills are in sync with the copper.  A
    # board refilled/routed after its fills were last saved produces phantom
    # clearance_segment_zone / clearance_via_zone / clearance_pad_zone shorts
    # that vanish once the fills are refreshed.  Warn loudly whenever any of
    # those findings are present, pointing at the authoritative refill — unless
    # the user already asked for it via --refill-zones (in which case the fills
    # are already fresh and any residual findings are real).
    if not getattr(args, "refill_zones", False):
        _warn_stale_zone_fills(results.violations, pcb_path)

    # Issue #3750: build the DRC SubCheckResult that will feed both the
    # exit-code computation and the meta-check rollup (when not in
    # --drc-only mode).  DRC status mirrors the legacy exit-code rule:
    # PASSED iff 0 errors and (0 warnings under --strict).
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    drc_passed = error_count == 0 and not (warning_count > 0 and args.strict)
    drc_sub = SubCheckResult(
        status="PASSED" if drc_passed else "FAILED",
        detail=(
            f"{results.rules_checked} rules checked, "
            f"{error_count} error(s), {warning_count} warning(s)"
        ),
    )

    # Issue #3750: compute the meta-check rollup once and reuse it for
    # both the human stanza and the JSON envelope.  Skipped entirely
    # under --drc-only to preserve the legacy stdout/exit-code contract.
    drc_only = getattr(args, "drc_only", False)
    meta: MetaCheckResult | None = None
    if not drc_only:
        meta = run_meta_checks(
            pcb_path,
            drc_status=drc_sub,
            schematic=getattr(args, "schematic", None),
            strict=args.strict,
        )

    # Output results
    if args.format == "json":
        output_json(violations, results, pcb_path, args.mfr, layers, meta=meta)
    elif args.format == "summary":
        output_summary(violations, results, pcb_path)
        if meta is not None:
            print_meta_check_stanza(meta)
    else:
        output_table(violations, results, pcb_path, args.mfr, layers, args.verbose)
        if meta is not None:
            print_meta_check_stanza(meta)

    # Write JSON report to file if --output specified
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_report(violations, results, pcb_path, args.mfr, layers, output_path, meta=meta)

    # Determine exit code
    # Exit 2 = check ran successfully but found issues (errors, or warnings+strict)
    # Exit 1 = reserved for tool-level failures (file not found, parse error) above
    # Exit 0 = no errors (warnings may be present without --strict; infos
    #   never affect exit code -- they are advisory by definition).
    # Issue #3750: when the meta-check rollup is in play (default mode),
    # exit 2 also when any sub-check is FAILED, and -- per AC #3 --
    # exit 2 when the rollup is INCOMPLETE (any sub-check is NOT RUN)
    # unless the caller opted in to ``--allow-incomplete``.
    #
    # Issue #4326 (Tier 2 safety backstop): under --strict, a stackup-vs-
    # --copper copper-weight disagreement is fatal on its own (a warning
    # already fired above), so an autonomous agent cannot sail past an
    # ampacity evaluation that silently ignored the board's declared build.
    if args.strict and copper_disagreement:
        return 2

    if drc_only:
        if error_count > 0 or (warning_count > 0 and args.strict):
            return 2
        return 0

    # Default (meta) mode: PASSED -> 0, FAILED -> 2, INCOMPLETE -> 2
    # (exit 0 only with --allow-incomplete).  Issue #3750 AC #3:
    # honest exit codes mean "exit 0 only when every sub-check is
    # PASSED" -- silently exiting 0 on NOT RUN re-introduces the
    # false-positive class the issue exists to eliminate.
    assert meta is not None  # guaranteed by the branch above
    if meta.overall == "FAILED":
        return 2
    if meta.overall == "INCOMPLETE" and not getattr(args, "allow_incomplete", False):
        return 2
    return 0


def run_selected_checks(
    checker: DRCChecker,
    only_set: set[str] | None,
    skip_set: set[str],
    pad_grid_threshold: float | None = None,
    pad_grid_auto_derive: bool = True,
) -> DRCResults:
    """Run the selected DRC checks based on filters.

    Args:
        checker: The DRC checker pre-loaded with the PCB and rules.
        only_set: Optional whitelist of check category names.
        skip_set: Set of check category names to skip.
        pad_grid_threshold: Explicit pad_grid L2 tolerance in mm, or
            ``None`` to use the threshold-resolution policy below.
            Issue #3061.
        pad_grid_auto_derive: When ``True`` and ``pad_grid_threshold``
            is ``None``, the pad_grid check derives the threshold from
            the board's pad-offset histogram (issue #3061).  Defaults
            to ``True`` for the CLI; ``False`` preserves the PR #3057
            fixed-0.05mm behaviour.
    """
    results = DRCResults()

    # Build the pad_grid invocation as a thunk so the map below can
    # remain uniform (every value is a zero-arg callable).
    def _pad_grid_check() -> DRCResults:
        # Issue #3941: collapse a fixed-pitch footprint's per-pad warnings
        # into one aggregated warning per component ref by default; under
        # ``--verbose`` (surfaced as ``checker.verbose``) emit the full
        # per-pad detail instead.
        return checker.check_pad_grid_alignment(
            threshold=pad_grid_threshold,
            auto_derive_threshold=pad_grid_auto_derive,
            aggregate=not checker.verbose,
        )

    # Map of category to check method.  This dict MUST stay a superset
    # of the methods invoked by ``DRCChecker.check_all`` (i.e., every
    # name in ``DRCChecker.CHECK_ALL_METHODS`` must be referenced as a
    # value here).  The regression test in
    # ``tests/test_check_cmd_coverage.py`` enforces the invariant for
    # Issue #3046.
    check_methods = {
        "ampacity": checker.check_ampacity,
        "clearance": checker.check_clearances,
        "connectivity": checker.check_connectivity,
        "segment_zone": checker.check_segment_zone_clearances,
        "via_zone": checker.check_via_zone_clearances,
        "copper_sliver": checker.check_copper_slivers,
        "courtyard_overlap": checker.check_courtyard_overlap,
        "diffpair_clearance_intra": checker.check_diffpair_clearance_intra,
        "diffpair_length_skew": checker.check_diffpair_length_skew,
        "diffpair_routing_continuity": checker.check_diffpair_routing_continuity,
        "dimensions": checker.check_dimensions,
        "edge": checker.check_edge_clearances,
        "impedance": checker.check_impedance,
        "match_group_length_skew": checker.check_match_group_length_skew,
        "netlist": checker.check_netlist,
        "pad_grid": _pad_grid_check,
        "placement": checker.check_footprint_placement,
        "silkscreen": checker.check_silkscreen,
        "single_pad_net": checker.check_single_pad_nets,
        "solder_mask": checker.check_solder_mask_pads,
        "via_in_pad": checker.check_via_in_pad,
        "zones": checker.check_zones,
    }

    for category, method in check_methods.items():
        # Skip if --only specified and this category not in it
        if only_set is not None and category not in only_set:
            continue

        # Skip if this category is in --skip
        if category in skip_set:
            continue

        # Run the check
        category_results = method()
        results.merge(category_results)

    return results


def _print_measurement_summary(violations: list[DRCViolation]) -> None:
    """Print a per-group / per-pair length-measurement summary table.

    Issue #3924 AC1.  The sidecar-gated length-skew and routing-continuity
    rules (:data:`_MEASUREMENT_RULE_IDS`) attach the measured value
    (``actual_value``) and its tolerance (``required_value``) to every
    finding they emit -- passing findings are ``info`` severity (only
    produced when the checker was built with ``emit_measurements=True`` or
    ``verbose=True``) and failing findings are ``error`` severity.  This
    renders both into one compact table so a user running plain
    ``kct check`` (no ``--verbose``) can read the achieved skew / continuity
    values without wading through the info stream.

    The table is only printed when at least one measurement finding is
    present.  With no net-class-map sidecar the skew rules produce no
    findings, so this is a graceful no-op (Issue #3924 AC5).
    """
    measurements = [v for v in violations if v.rule_id in _MEASUREMENT_RULE_IDS]
    # Only rows that actually carry a measured value are renderable.
    measurements = [v for v in measurements if v.actual_value is not None]
    if not measurements:
        return

    def _subject(v: DRCViolation) -> str:
        # Match groups carry the group name in ``items``; diff pairs carry
        # their two net names in ``nets``.
        if v.items:
            return v.items[0]
        if v.nets:
            return "/".join(n for n in v.nets if n)
        return v.rule_id

    def _metric(rule_id: str) -> str:
        # diffpair_routing_continuity measures a coupled *fraction*, not a
        # length skew -- label the column accordingly.
        if rule_id == "diffpair_routing_continuity":
            return "continuity"
        return "skew"

    rows: list[tuple[str, str, str, str, str]] = []
    for v in sorted(measurements, key=lambda x: (x.rule_id, _subject(x))):
        subject = _subject(v)
        metric = _metric(v.rule_id)
        measured = f"{v.actual_value:.3f}" if v.actual_value is not None else "-"
        tol = f"{v.required_value:.3f}" if v.required_value is not None else "-"
        status = "FAIL" if v.is_error else "pass"
        rows.append((subject, metric, measured, tol, status))

    subject_w = max(len("Group/Pair"), *(len(r[0]) for r in rows))
    metric_w = max(len("Metric"), *(len(r[1]) for r in rows))

    print(f"\n{'-' * 60}")
    print("MEASUREMENT SUMMARY (length-match / continuity):")
    header = (
        f"  {'Group/Pair':<{subject_w}}  {'Metric':<{metric_w}}  "
        f"{'Measured':>10}  {'Tolerance':>10}  Status"
    )
    print(header)
    for subject, metric, measured, tol, status in rows:
        print(
            f"  {subject:<{subject_w}}  {metric:<{metric_w}}  {measured:>10}  {tol:>10}  {status}"
        )


def output_table(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    verbose: bool = False,
) -> None:
    """Output violations as a formatted table."""
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)
    waived_count = sum(1 for v in violations if v.is_waived)

    print(f"\n{'=' * 60}")
    print("PURE PYTHON DRC CHECK")
    print(f"{'=' * 60}")
    print(f"File: {pcb_path.name}")
    print(f"Manufacturer: {mfr.upper()}")
    print(f"Layers: {layers}")
    print(f"Rules checked: {results.rules_checked}")

    print("\nResults:")
    print(f"  Errors:     {error_count}")
    print(f"  Warnings:   {warning_count}")
    if info_count > 0:
        print(f"  Infos:      {info_count}")
    if waived_count > 0:
        print(f"  Waived:     {waived_count}")
    if results.suppressed_count > 0:
        print(f"  Suppressed: {results.suppressed_count} (standard library footprints)")

    if not violations:
        print(f"\n{'=' * 60}")
        print("DRC PASSED - No violations found")
        return

    # Issue #3924 AC1: render the length-match / continuity measurement
    # table before the violation listing so the measured values are visible
    # even on the default (non-``--verbose``) path.  No-op when the board
    # has no measurement findings (e.g. no net-class-map sidecar -- AC5).
    _print_measurement_summary(violations)

    # Group by rule_id summary.
    #
    # Issue #3924: the length-match / continuity measurement findings are
    # info-severity rows surfaced by the dedicated MEASUREMENT SUMMARY table
    # above.  On the default (non-``--verbose``) path we exclude them from the
    # BY RULE breakdown -- mirroring the INFOS-listing suppression below -- so
    # that default output stays backward-compatible for consumers that grep
    # ``BY RULE:`` severity-agnostically (e.g. the board03 baseline test).
    # Under ``--verbose`` they remain visible in BY RULE alongside INFOS.
    by_rule_source = (
        violations
        if verbose
        else [v for v in violations if not (v.is_info and v.rule_id in _MEASUREMENT_RULE_IDS)]
    )
    by_rule: dict[str, dict[str, int]] = {}
    # Issue #4102: for net-relationship rules (dimension_drill_clearance),
    # additionally tally a same-net / different-net breakdown so the BY RULE
    # summary line is immediately actionable -- directly answering the
    # "49-finding wall" complaint without scrolling the detail list.
    by_rule_relationship: dict[str, dict[str, int]] = {}
    for v in by_rule_source:
        if v.rule_id not in by_rule:
            by_rule[v.rule_id] = {"errors": 0, "warnings": 0, "infos": 0, "waived": 0}
        if v.is_waived:
            by_rule[v.rule_id]["waived"] += 1
        elif v.is_error:
            by_rule[v.rule_id]["errors"] += 1
        elif v.is_info:
            by_rule[v.rule_id]["infos"] += 1
        else:
            by_rule[v.rule_id]["warnings"] += 1

        if v.rule_id in _NET_RELATIONSHIP_RULE_IDS:
            relationship = _net_relationship(v.nets)
            if relationship is not None:
                counts = by_rule_relationship.setdefault(
                    v.rule_id, {"same-net": 0, "different-net": 0}
                )
                counts[relationship] += 1

    # ``by_rule`` can be empty when the only findings are measurement info
    # rows suppressed above (already surfaced in MEASUREMENT SUMMARY); skip the
    # empty BY RULE header in that case.
    if by_rule:
        print(f"\n{'-' * 60}")
        print("BY RULE:")
    for rule_id, counts in sorted(
        by_rule.items(),
        key=lambda x: -(x[1]["errors"] + x[1]["warnings"] + x[1]["infos"] + x[1].get("waived", 0)),
    ):
        parts = []
        if counts["errors"]:
            parts.append(f"{counts['errors']} error{'s' if counts['errors'] != 1 else ''}")
        if counts["warnings"]:
            parts.append(f"{counts['warnings']} warning{'s' if counts['warnings'] != 1 else ''}")
        if counts["infos"]:
            parts.append(f"{counts['infos']} info{'s' if counts['infos'] != 1 else ''}")
        if counts.get("waived"):
            parts.append(f"{counts['waived']} waived")
        line = f"  {rule_id}: {', '.join(parts)}"
        # Issue #4102: append the same-net / different-net breakdown for
        # net-relationship rules, e.g.
        #   dimension_drill_clearance: 49 errors (32 different-net, 17 same-net)
        rel = by_rule_relationship.get(rule_id)
        if rel:
            diff_n = rel["different-net"]
            same_n = rel["same-net"]
            line += f" ({diff_n} different-net, {same_n} same-net)"
        print(line)

    # Detailed output
    errors = [v for v in violations if v.is_error]
    warnings = [v for v in violations if v.is_warning]
    infos = [v for v in violations if v.is_info]
    waived = [v for v in violations if v.is_waived]

    if errors:
        print(f"\n{'-' * 60}")
        print("ERRORS (must fix):")
        for v in errors:
            _print_violation(v, verbose)

    if warnings:
        print(f"\n{'-' * 60}")
        print("WARNINGS (review recommended):")
        display_warnings = warnings if verbose else warnings[:10]
        for v in display_warnings:
            _print_violation(v, verbose)
        if len(warnings) > 10 and not verbose:
            print(f"\n  ... and {len(warnings) - 10} more warnings (use --verbose)")

    # Issue #3924 AC1/AC4: the per-pair / per-group measurement info findings
    # are shown in the dedicated MEASUREMENT SUMMARY table above at every
    # verbosity.  In the generic INFOS listing we keep them only under
    # ``--verbose`` (preserving PR #3948's advisory-line behaviour) and
    # suppress them on the default path to avoid duplicating the summary
    # and flooding plain output.
    display_infos_source = (
        infos if verbose else [v for v in infos if v.rule_id not in _MEASUREMENT_RULE_IDS]
    )
    if display_infos_source:
        print(f"\n{'-' * 60}")
        print("INFOS (advisory only):")
        display_infos = display_infos_source if verbose else display_infos_source[:10]
        for v in display_infos:
            _print_violation(v, verbose)
        if len(display_infos_source) > 10 and not verbose:
            print(f"\n  ... and {len(display_infos_source) - 10} more infos (use --verbose)")

    # Issue #4137: waived findings are visible and counted but non-blocking.
    # Render them in a dedicated WAIVED section so a reviewer can audit each
    # documented exception (with its reason / tracking issue).
    if waived:
        print(f"\n{'-' * 60}")
        print("WAIVED (documented exceptions, non-blocking):")
        for v in waived:
            print(f"\n  [W] {v.rule_id}")
            print(f"      {v.message}")
            if v.waiver_issue:
                print(f"      Waiver issue: {v.waiver_issue}")
            if verbose:
                if v.items:
                    print(f"      Items: {', '.join(v.items)}")
                if v.layer:
                    print(f"      Layer: {v.layer}")

    print(f"\n{'=' * 60}")
    if errors:
        print("DRC FAILED - Fix errors before manufacturing")
    elif warnings:
        print("DRC WARNING - Review warnings")
    else:
        print("DRC PASSED - Advisory infos only")


def _print_violation(v: DRCViolation, verbose: bool, indent: str = "  ") -> None:
    """Print a single violation."""
    if v.is_error:
        symbol = "X"
    elif v.is_info:
        symbol = "i"
    else:
        symbol = "!"

    # Issue #4102: for hole-to-hole clearance findings, tag the header line
    # with the net relationship (same-net / different-net) so a user scanning
    # a wall of findings can immediately tell the fab's real concern
    # (different-net drill-wall breakage -> short) from lower-risk same-net /
    # floating pairs -- without eyeballing two net names.  Presentational only;
    # severity is unchanged.
    relationship = _net_relationship(v.nets) if v.rule_id in _NET_RELATIONSHIP_RULE_IDS else None
    header = v.rule_id if relationship is None else f"{v.rule_id} ({relationship})"
    print(f"\n{indent}[{symbol}] {header}")
    print(f"{indent}    {v.message}")

    # Issue #4102: show the net endpoints unconditionally for net-relationship
    # rules (not only under --verbose), rendered as ``net1 / net2`` so the
    # same-net / different-net qualifier above is self-evident.
    net_shown = False
    if relationship is not None and v.nets:
        net_labels = [n if n else "<no net>" for n in v.nets]
        print(f"{indent}    Nets: {' / '.join(net_labels)}")
        net_shown = True

    if verbose:
        if v.location:
            print(f"{indent}    -> ({v.location[0]:.2f}, {v.location[1]:.2f}) mm")
        if v.layer:
            print(f"{indent}    Layer: {v.layer}")
        if v.actual_value is not None and v.required_value is not None:
            print(f"{indent}    Actual: {v.actual_value:.3f}mm, Required: {v.required_value:.3f}mm")
        if v.items:
            print(f"{indent}    Items: {', '.join(v.items)}")
        if v.nets and not net_shown:
            net_labels = [n if n else "<no net>" for n in v.nets]
            print(f"{indent}    Nets: {', '.join(net_labels)}")


def output_json(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    meta: MetaCheckResult | None = None,
) -> None:
    """Output violations as JSON.

    Issue #3750: when ``meta`` is provided (default mode), the envelope
    grows a top-level ``meta_checks`` field.  Legacy consumers that read
    ``summary.passed`` / ``summary.errors`` / ``violations`` are
    unaffected.  Under ``--drc-only`` the ``meta`` parameter is ``None``
    and the field is omitted (``OMIT-when-absent`` convention).
    """
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)
    waived_count = sum(1 for v in violations if v.is_waived)

    summary_data: dict = {
        "errors": error_count,
        "warnings": warning_count,
        "infos": info_count,
        # Issue #4137: waived findings are counted distinctly and never
        # contribute to ``passed`` (which keys off ``errors``).
        "waived": waived_count,
        "rules_checked": results.rules_checked,
        # Issue #2660 / Epic #2556 Phase 4N: per-rule check counter.
        # The single ``rules_checked`` integer cannot tell a CI consumer
        # WHICH rules ran -- only the aggregate.  Without this map, a
        # diff-pair CI gate cannot distinguish "rule X ran and reported
        # 0 violations" from "rule X did not run at all" (e.g., the rule
        # short-circuited because no engaged pairs were detected, which
        # would be a silent regression in detection).  Always emitted
        # (even when empty) so downstream consumers can rely on the
        # field being present.
        "rules_checked_by_rule": dict(results.rules_checked_by_rule),
        "passed": error_count == 0,
    }
    if results.suppressed_count > 0:
        summary_data["suppressed"] = results.suppressed_count

    data: dict = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": summary_data,
        "violations": [v.to_dict() for v in violations],
    }
    if meta is not None:
        data["meta_checks"] = meta.to_dict()
    print(json.dumps(data, indent=2))


def write_json_report(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
    mfr: str,
    layers: int,
    output_path: Path,
    meta: MetaCheckResult | None = None,
) -> None:
    """Write DRC results as a JSON report file.

    Issue #3750: ``meta_checks`` is added to the envelope when meta-mode
    is active.  Omitted under ``--drc-only`` to preserve the legacy
    on-disk schema.
    """
    error_count = sum(1 for v in violations if v.is_error)
    warning_count = sum(1 for v in violations if v.is_warning)
    info_count = sum(1 for v in violations if v.is_info)
    waived_count = sum(1 for v in violations if v.is_waived)

    summary_data: dict = {
        "errors": error_count,
        "warnings": warning_count,
        "infos": info_count,
        # Issue #4137: waived findings are counted distinctly and never
        # contribute to ``passed`` (which keys off ``errors``).
        "waived": waived_count,
        "rules_checked": results.rules_checked,
        # See ``output_json`` for the rationale on emitting this field
        # alongside the aggregate ``rules_checked`` integer.  Issue
        # #2660 / Epic #2556 Phase 4N.
        "rules_checked_by_rule": dict(results.rules_checked_by_rule),
        "passed": error_count == 0,
    }
    if results.suppressed_count > 0:
        summary_data["suppressed"] = results.suppressed_count

    data: dict = {
        "file": str(pcb_path),
        "manufacturer": mfr,
        "layers": layers,
        "summary": summary_data,
        "violations": [v.to_dict() for v in violations],
    }
    if meta is not None:
        data["meta_checks"] = meta.to_dict()
    output_path.write_text(json.dumps(data, indent=2) + "\n")


def output_summary(
    violations: list[DRCViolation],
    results: DRCResults,
    pcb_path: Path,
) -> None:
    """Output violation summary by rule."""
    if not violations:
        msg = f"  {results.rules_checked} rules checked, no violations found."
        if results.suppressed_count > 0:
            msg += (
                f"\n  ({results.suppressed_count} silkscreen warnings suppressed"
                f" -- standard library footprints)"
            )
        print(f"DRC PASSED: {pcb_path.name}")
        print(msg)
        return

    print(f"DRC Summary: {pcb_path.name}")
    print("=" * 50)

    # Group by rule_id
    by_rule: dict[str, dict[str, int]] = {}
    for v in violations:
        key = v.rule_id
        if key not in by_rule:
            by_rule[key] = {"errors": 0, "warnings": 0, "infos": 0}
        if v.is_error:
            by_rule[key]["errors"] += 1
        elif v.is_info:
            by_rule[key]["infos"] += 1
        else:
            by_rule[key]["warnings"] += 1

    print(f"{'Rule ID':<30} {'Errors':<8} {'Warnings':<10} {'Infos':<8}")
    print("-" * 60)

    for rule_id, counts in sorted(by_rule.items()):
        print(f"{rule_id:<30} {counts['errors']:<8} {counts['warnings']:<10} {counts['infos']:<8}")

    print("-" * 60)
    total_errors = sum(c["errors"] for c in by_rule.values())
    total_warnings = sum(c["warnings"] for c in by_rule.values())
    total_infos = sum(c["infos"] for c in by_rule.values())
    print(f"{'TOTAL':<30} {total_errors:<8} {total_warnings:<10} {total_infos:<8}")


if __name__ == "__main__":
    sys.exit(main())
