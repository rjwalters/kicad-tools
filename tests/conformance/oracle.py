"""Ground truth: ``kicad-cli pcb drc`` canonicalised into :class:`Verdict` sets.

This module is the only place in the harness that decides *what KiCad said*.
Everything downstream compares against its output, so its two jobs are:

1. **Run the real tool, once, through the existing wrapper.**  It shells out
   via :func:`kicad_tools.cli.runner.run_drc` rather than building its own
   command line -- Epic #5509 already ruled that a third kicad-cli shell-out
   is not wanted.  ``schematic_parity=False``: these boards have no schematic.

2. **Canonicalise.**  Reduce a kicad-cli report to the set of *pairs it
   flagged*, dropping everything that is not a clearance-family finding
   (dangling tracks, unconnected items, library-sync noise, silk, mask).  Pair
   identity comes from the ``[NetName]`` tokens KiCad puts in each item's
   description, which is why the generator gives every object its own net.

Fill state is explicit
    Zones are written unfilled, and an unfilled zone contributes no copper to
    KiCad's DRC.  :func:`run_oracle` therefore takes ``refill`` as a required
    keyword and records it on the result; zone verdicts from a non-refilled
    run are meaningless and callers filter them with
    :meth:`OracleResult.without_zones`.  ``run_refill_zones`` **rewrites the
    board in place**, so a refilled run always works on a copy -- a committed
    fixture is never mutated.

Absent kicad-cli is never a silent pass
    :class:`KiCadCliAbsent` carries the machine-readable reason
    ``kicad_cli_absent``, matching ``drc/geometric.py``'s ``REASON_ABSENT``.
    Tests skip on it; the report generator exits non-zero and writes nothing.
    A table of all-zero disagreement rates produced by a machine without
    KiCad would be worse than no table at all.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli.runner import find_kicad_cli, run_drc, run_refill_zones
from kicad_tools.drc.report import parse_json_report
from kicad_tools.drc.violation import DRCViolation
from tests.conformance.adapters import (
    BOARD_EDGE,
    KIND_CLEARANCE,
    KIND_COPPER_EDGE,
    KIND_HOLE_CLEARANCE,
    KIND_HOLE_TO_HOLE,
    Verdict,
)

__all__ = [
    "REASON_ABSENT",
    "RAW_TYPE_TO_KIND",
    "KiCadCliAbsent",
    "OracleResult",
    "canonicalise",
    "kicad_cli_available",
    "run_oracle",
]

REASON_ABSENT = "kicad_cli_absent"
"""Machine-readable skip reason; mirrors ``drc/geometric.py`` ``REASON_ABSENT``."""


class KiCadCliAbsent(RuntimeError):
    """Raised when the oracle is asked to measure without kicad-cli installed."""

    reason = REASON_ABSENT

    def __init__(self, message: str = "kicad-cli not installed") -> None:
        super().__init__(f"{message} [{REASON_ABSENT}]")


def kicad_cli_available() -> bool:
    """True when a kicad-cli binary can be resolved on this machine."""
    return find_kicad_cli() is not None


# kicad-cli's raw ``type`` strings -> the harness's canonical verdict kinds.
# Anything absent from this mapping is deliberately dropped: it is either a
# connectivity finding (``track_dangling``, ``via_dangling``,
# ``unconnected_items``), a library-sync artefact (``lib_footprint_issues``,
# ``lib_footprint_mismatch``), or a non-copper check (silk, mask, courtyard).
# ``shorting_items`` is dropped too -- it is a connectivity conclusion about
# copper that already overlaps, not a clearance measurement, and no adapter
# models it.
RAW_TYPE_TO_KIND: dict[str, str] = {
    "clearance": KIND_CLEARANCE,
    "hole_clearance": KIND_HOLE_CLEARANCE,
    "hole_near_hole": KIND_HOLE_TO_HOLE,
    "hole_to_hole": KIND_HOLE_TO_HOLE,
    "hole_to_hole_clearance": KIND_HOLE_TO_HOLE,
    "copper_edge_clearance": KIND_COPPER_EDGE,
}

_ZONE_ITEM_PREFIXES = ("Zone", "Rule Area", "Copper zone")


@dataclass(frozen=True)
class OracleResult:
    """One kicad-cli measurement of one board.

    Attributes:
        pcb_path: The board that was measured (a copy, for refilled runs).
        refill: Whether zones were refilled before the DRC run.
        verdicts: Canonical clearance-family findings.
        raw_type_counts: Every ``type`` string kicad-cli emitted, counted --
            kept so the report can show what was dropped rather than hiding it.
        dropped: Human-readable descriptions of rows that were clearance-family
            but could not be reduced to a net pair (should be empty for
            generated cases; a non-empty list is a harness bug, not a
            consumer disagreement).
        return_code: kicad-cli's exit status (non-zero simply means "found
            violations").
    """

    pcb_path: Path
    refill: bool
    verdicts: frozenset[Verdict]
    raw_type_counts: Counter[str] = field(default_factory=Counter)
    dropped: tuple[str, ...] = ()
    return_code: int = 0

    def without_zones(self) -> frozenset[Verdict]:
        """Verdicts not involving zone copper.

        The comparable set for a ``refill=False`` run, and the set the named
        fixtures require to be identical at both fill settings.
        """
        return frozenset(v for v in self.verdicts if not v.zone)

    def of_kind(self, kind: str) -> frozenset[Verdict]:
        return frozenset(v for v in self.verdicts if v.kind == kind)

    def describe(self) -> str:
        if not self.verdicts:
            return f"(no clearance-family findings; refill={self.refill})"
        lines = sorted(v.describe() for v in self.verdicts)
        return f"refill={self.refill}\n  " + "\n  ".join(lines)


def _is_zone_item(description: str) -> bool:
    return description.strip().startswith(_ZONE_ITEM_PREFIXES)


# kicad-cli 10 words its hole-to-hole message as "... min 0.4995 mm; actual
# 0.4500 mm".  The shared parser's extractors look for "clearance X" /
# "minimum X" / "width X" and so leave those two values unset.  The numbers are
# recorded for the report only (they are excluded from verdict identity), so
# this local fallback keeps the table complete without touching the shared
# ``drc/report.py`` extractor -- which other consumers depend on and which this
# report-only epic phase must not change.
_MIN_ACTUAL_RE = re.compile(
    r"\bmin(?:imum)?\s+([\d.]+)\s*mm.*?actual\s+(-?[\d.]+)\s*mm", re.IGNORECASE
)


def _violation_values(violation: DRCViolation) -> tuple[float | None, float | None]:
    """``(gap_mm, required_mm)`` for a violation, with a message fallback."""
    gap = violation.actual_value_mm
    required = violation.required_value_mm
    if gap is None or required is None:
        match = _MIN_ACTUAL_RE.search(violation.message)
        if match:
            required = required if required is not None else float(match.group(1))
            gap = gap if gap is not None else float(match.group(2))
    return gap, required


def _violation_nets(violation: DRCViolation) -> list[str]:
    """Distinct real nets named by a violation's items, in report order.

    ``DRCViolation.nets`` is populated from the ``[NetName]`` tokens in each
    item description by the shared parser; ``<no net>`` is already filtered
    there.
    """
    seen: list[str] = []
    for net in violation.nets:
        if net and net not in seen:
            seen.append(net)
    return seen


def canonicalise(report_json: str) -> tuple[frozenset[Verdict], Counter[str], tuple[str, ...]]:
    """Reduce a kicad-cli JSON DRC report to a canonical verdict set.

    Args:
        report_json: The raw contents of a ``--format json`` DRC report.

    Returns:
        ``(verdicts, raw_type_counts, dropped)``.
    """
    report = parse_json_report(report_json)
    raw_counts: Counter[str] = Counter()
    verdicts: set[Verdict] = set()
    dropped: list[str] = []

    for violation in report.violations:
        raw_counts[violation.type_str] += 1
        kind = RAW_TYPE_TO_KIND.get(violation.type_str)
        if kind is None:
            continue

        zone = any(_is_zone_item(item) for item in violation.items)
        nets = _violation_nets(violation)
        gap_mm, required_mm = _violation_values(violation)

        if kind == KIND_COPPER_EDGE:
            # One-sided: copper against the board outline.  Pair it with the
            # reserved edge pseudo-net so every verdict stays a 2-set.
            if not nets:
                dropped.append(f"{violation.type_str}: {violation.message}")
                continue
            verdicts.add(
                Verdict.pair(
                    kind,
                    nets[0],
                    BOARD_EDGE,
                    gap_mm=gap_mm,
                    required_mm=required_mm,
                    zone=zone,
                )
            )
            continue

        if len(nets) < 2:
            # Same-net finding (e.g. a via against its own track) or a row
            # whose items carried no net token.  Neither is a foreign-net
            # clearance pair, so it is outside what the adapters answer.
            dropped.append(f"{violation.type_str}: {violation.message} {violation.items}")
            continue

        verdicts.add(
            Verdict.pair(
                kind,
                nets[0],
                nets[1],
                gap_mm=gap_mm,
                required_mm=required_mm,
                zone=zone,
            )
        )

    return frozenset(verdicts), raw_counts, tuple(dropped)


def run_oracle(
    pcb_path: Path,
    *,
    refill: bool,
    work_dir: Path | None = None,
) -> OracleResult:
    """Run ``kicad-cli pcb drc`` over ``pcb_path`` and canonicalise the result.

    Args:
        pcb_path: Board to measure.  Neither it nor its directory is ever
            touched: the board and its sibling ``.kicad_pro`` are copied into
            a scratch subdirectory first, and kicad-cli only ever sees the
            copy.  Two separate reasons make this mandatory -- ``--refill-zones
            --save-board`` rewrites the board, and *any* kicad-cli run drops a
            ``.kicad_prl`` local-settings file next to the project.  Running
            the suite must not dirty the committed fixtures.
        refill: Refill zones before running DRC.  Required keyword -- fill
            state changes zone verdicts, so it must be a deliberate choice at
            every call site.
        work_dir: Parent directory for the scratch copy and the report.
            A temporary directory is used when omitted.

    Returns:
        The canonicalised :class:`OracleResult`.

    Raises:
        KiCadCliAbsent: When no kicad-cli binary can be found.
        RuntimeError: When kicad-cli produced no report (a board that failed
            to load) -- never reported as a clean result.
    """
    if not kicad_cli_available():
        raise KiCadCliAbsent()

    pcb_path = Path(pcb_path)
    owns_tmp = work_dir is None
    tmp = tempfile.mkdtemp(prefix="conformance_oracle_") if owns_tmp else None
    scratch = Path(tmp) if tmp is not None else Path(work_dir)  # type: ignore[arg-type]
    scratch.mkdir(parents=True, exist_ok=True)

    try:
        # A dedicated subdirectory per run, even when ``work_dir`` already is
        # the directory the board lives in.
        state = "refilled" if refill else "asis"
        run_dir = scratch / f"oracle-{state}-{pcb_path.stem}"
        run_dir.mkdir(parents=True, exist_ok=True)
        measured = run_dir / pcb_path.name
        shutil.copyfile(pcb_path, measured)
        project = pcb_path.with_suffix(".kicad_pro")
        if project.exists():
            shutil.copyfile(project, measured.with_suffix(".kicad_pro"))

        if refill:
            fill = run_refill_zones(measured)
            if not fill.success:
                raise RuntimeError(f"kicad-cli could not refill zones on {measured}: {fill.stderr}")

        report_path = run_dir / f"{measured.stem}-drc-{state}.json"
        result = run_drc(
            measured,
            output_path=report_path,
            format="json",
            schematic_parity=False,
        )
        if not result.success or not report_path.exists():
            raise RuntimeError(
                f"kicad-cli produced no DRC report for {measured} "
                f"(rc={result.return_code}): {result.stderr}"
            )

        verdicts, raw_counts, dropped = canonicalise(report_path.read_text())
        return OracleResult(
            pcb_path=measured,
            refill=refill,
            verdicts=verdicts,
            raw_type_counts=raw_counts,
            dropped=dropped,
            return_code=result.return_code,
        )
    finally:
        if owns_tmp and tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
