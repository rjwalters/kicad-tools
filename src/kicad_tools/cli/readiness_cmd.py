"""Scriptable manufacturing-readiness runner (``kct readiness``, issue #4977).

This module is the **producer** side of the hash-bound gallery evidence that
:func:`kicad_tools.cli.board_readiness.read_readiness` validates.  Until now
nothing under ``src/`` wrote ``output/readiness.json`` — a complete report had
to be assembled by hand, gate by gate, by an agent following the
``.claude/commands/kct/manufacturing-readiness.md`` and
``.claude/commands/kct/tapeout.md`` checklists.  This command scripts those two
contracts.

Design constraints (from the issue and the two skill contracts):

* **Orchestrate, never duplicate.**  Every gate delegates to an engine that
  already exists — ``kct check`` (:mod:`kicad_tools.cli.check_cmd`),
  ``kicad-cli pcb drc --refill-zones``
  (:func:`kicad_tools.drc.geometric.run_geometric_drc`), ``kct export``
  (:mod:`kicad_tools.cli.export_cmd`) and ``kicad-cli sch|pcb export pdf``.
  Those engines are reached through an injectable :class:`Engines` bundle so
  the orchestration itself is testable without KiCad installed.
* **Parse findings, not exit codes.**  ``kicad-cli pcb drc`` exits 0 even with
  error-severity violations, and a "ran" flag is not a pass (#4973).  Every
  gate reads the engine's actual report.
* **Ordering is load-bearing.**  The canonical PCB is refilled *and saved*
  before BOTH the final native check AND the export, because a saved-vs-refilled
  copper divergence can survive two zero-error reports (see the issue's
  first follow-up comment).  A per-layer fill-equivalence check is retained as
  evidence.
* **Never silently ready.**  There is no bypass flag.  A gate that cannot run
  produces ``not_run`` plus a named blocker and a non-zero exit; a gate that
  fails produces ``failed`` plus a named blocker.  ``ready`` requires every
  applicable gate to have passed.
* **Bind the engine, not just the board.**  Each report records an
  :class:`EngineFingerprint` (release/commit + dirty-source digest, native
  KiCad version, resolved rule/profile identity) so a changed checker engine
  cannot silently imply re-qualification of old evidence.  See
  ``docs/board-json-schema.md`` → "Engine fingerprint".

The emitted document is the readiness-v1 schema documented in
``docs/board-json-schema.md``; the runner re-reads its own output through
``read_readiness()`` before returning, so a report this command calls ``ready``
is a report the gallery's validator accepts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .board_readiness import read_readiness
from .format_options import FORMAT_JSON, add_format_flag, emit_json, stdout_to_stderr_when

logger = logging.getLogger(__name__)

__all__ = [
    "ASSEMBLY_AFFECTING_RULES",
    "CheckOutcome",
    "EngineFingerprint",
    "EngineRun",
    "Engines",
    "ReadinessOptions",
    "ReadinessResult",
    "build_report",
    "main",
    "resolve_options",
    "run_readiness",
]

SCHEMA_VERSION = 1

STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"
STATUS_UNVERIFIED = "unverified"

PASSED = "passed"
FAILED = "failed"
NOT_RUN = "not_run"

#: The four checks ``read_readiness()`` requires by name for a ``ready`` verdict.
REQUIRED_CHECKS = ("kct_check", "native_drc", "artifacts", "bom")

#: Warning rules that are genuine assembly risks (tapeout Gate 4b).  A floor,
#: not a ceiling — a nonzero count here must be explicitly acknowledged with
#: ``--ack-warnings`` before the bundle can be called upload-ready (#4614).
ASSEMBLY_AFFECTING_RULES = frozenset(
    {
        "silk_over_copper",
        "silkscreen_line_width",
        "silk_edge_clearance",
        "silk_overlap",
    }
)

#: Values that look like a part number but carry no procurement identity.
#: An assembly BOM containing any of these is not orderable.
PLACEHOLDER_PART_IDS = frozenset(
    {
        "",
        "-",
        "--",
        "?",
        "???",
        "0",
        "fixme",
        "n/a",
        "na",
        "none",
        "null",
        "placeholder",
        "tba",
        "tbc",
        "tbd",
        "todo",
        "x",
        "xx",
        "xxx",
        "xxxx",
    }
)

#: Column headers that carry a fab-orderable procurement identifier.
PART_ID_COLUMNS = ("lcsc part #", "lcsc", "lcsc part", "mpn", "part number", "supplier part")

#: Per-layer filled-copper area (mm^2) by which a saved board may differ from a
#: freshly refilled copy before the two are considered out of sync.
DEFAULT_FILL_TOLERANCE_MM2 = 0.01

_ARCHIVE_NAME = "manufacturing.zip"
_EVIDENCE_DIRNAME = "readiness"
_REPORT_NAME = "readiness.json"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Return the SHA256 hex digest of *path*'s bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _polygon_area(points: Sequence[tuple[float, float]]) -> float:
    """Absolute shoelace area of a closed polygon in mm^2."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _read_json(path: Path) -> dict:
    """Load a JSON object, returning ``{}`` for any unreadable input."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Engine plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineRun:
    """Outcome of one engine invocation.

    ``ok`` means *the engine executed and produced its artifact* — never "the
    design passed".  Verdicts are derived by the gates from the engine's
    report, never from this flag alone.
    """

    ok: bool
    detail: str = ""
    returncode: int = 0


def _run_kct_check(
    pcb: Path, manufacturer: str, report_path: Path, extra: Sequence[str]
) -> EngineRun:
    """Run ``kct check --mfr <tier> --output <report>`` in-process."""
    from . import check_cmd

    argv = [str(pcb), "--mfr", manufacturer, "--output", str(report_path), *extra]
    try:
        with stdout_to_stderr_when(True):
            code = check_cmd.main(argv)
    except SystemExit as exc:  # pragma: no cover - argparse bail-out
        code = int(exc.code or 0)
    except Exception as exc:  # pragma: no cover - defensive
        return EngineRun(ok=False, detail=f"kct check raised {type(exc).__name__}: {exc}")
    if not report_path.is_file():
        return EngineRun(ok=False, detail="kct check wrote no JSON report", returncode=code)
    return EngineRun(ok=True, returncode=code)


def _run_native_drc(pcb: Path, report_path: Path) -> EngineRun:
    """Run the mandatory ``kicad-cli pcb drc --refill-zones`` cross-gate.

    Delegates to :func:`kicad_tools.drc.geometric.run_geometric_drc` — the one
    in-repo implementation of that invocation — and persists a machine-readable
    summary so the finding counts (not merely a "ran" flag) become evidence.
    """
    from kicad_tools.drc.geometric import run_geometric_drc

    result = run_geometric_drc(pcb, timeout=600)
    payload = {
        "ran": result.ran,
        "error_count": result.error_count if result.ran else None,
        "by_type": dict(sorted(result.by_type.items())),
        "all_by_type": dict(sorted(result.all_by_type.items())),
        "reason": result.reason,
        "note": result.note,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if not result.ran:
        return EngineRun(ok=False, detail=result.note or "native DRC did not run")
    return EngineRun(ok=True)


def _run_export(pcb: Path, manufacturer: str, output_dir: Path, assembly: bool) -> EngineRun:
    """Run ``kct export`` in-process against the already-refilled PCB."""
    from . import export_cmd

    argv = [str(pcb), "--mfr", manufacturer, "--output", str(output_dir)]
    if not assembly:
        argv.extend(["--no-bom", "--no-cpl"])
    try:
        with stdout_to_stderr_when(True):
            code = export_cmd.main(argv)
    except SystemExit as exc:  # pragma: no cover - argparse bail-out
        code = int(exc.code or 0)
    except Exception as exc:  # pragma: no cover - defensive
        return EngineRun(ok=False, detail=f"kct export raised {type(exc).__name__}: {exc}")
    if code != 0:
        return EngineRun(ok=False, detail=f"kct export exited {code}", returncode=code)
    return EngineRun(ok=True)


def _refill_and_save(pcb: Path) -> EngineRun:
    """Refill copper pours and persist them into *pcb*."""
    from .runner import run_refill_zones

    result = run_refill_zones(pcb)
    if not result.success:
        return EngineRun(ok=False, detail=result.stderr or "zone refill failed")
    return EngineRun(ok=True)


def _run_kicad_cli(args: Sequence[str], output: Path) -> EngineRun:
    """Invoke ``kicad-cli`` and require it to have produced *output*."""
    from .runner import find_kicad_cli

    binary = find_kicad_cli()
    if binary is None:
        return EngineRun(ok=False, detail="kicad-cli not found on PATH")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(binary), *args], capture_output=True, text=True, timeout=600, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return EngineRun(ok=False, detail=f"kicad-cli failed: {exc}")
    if not output.is_file() or output.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return EngineRun(
            ok=False,
            detail=f"kicad-cli produced no {output.name}" + (f": {detail[-1]}" if detail else ""),
            returncode=completed.returncode,
        )
    return EngineRun(ok=True, returncode=completed.returncode)


def _export_schematic_pdf(schematic: Path, output: Path) -> EngineRun:
    return _run_kicad_cli(["sch", "export", "pdf", str(schematic), "--output", str(output)], output)


def _export_assembly_pdf(pcb: Path, layers: Sequence[str], output: Path) -> EngineRun:
    return _run_kicad_cli(
        [
            "pcb",
            "export",
            "pdf",
            str(pcb),
            "--layers",
            ",".join(layers),
            "--output",
            str(output),
        ],
        output,
    )


def _fill_areas(pcb: Path) -> dict[str, float]:
    """Per-copper-layer filled zone area (mm^2) persisted in *pcb*."""
    from kicad_tools.schema.pcb import PCB

    board = PCB.load(str(pcb))
    areas: dict[str, float] = {}
    for zone in board.zones:
        if zone.keepout is not None:
            continue
        for index, polygon in enumerate(zone.filled_polygons):
            layer = zone.filled_polygon_layer(index) or zone.layer
            areas[layer] = areas.get(layer, 0.0) + _polygon_area(polygon)
    return {layer: round(area, 6) for layer, area in sorted(areas.items())}


def _kicad_cli_version() -> str | None:
    """Return the installed ``kicad-cli`` version string, or ``None``."""
    from .runner import find_kicad_cli

    binary = find_kicad_cli()
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
        return None
    return (completed.stdout or completed.stderr or "").strip() or None


def _through_hole_refs(pcb: Path) -> set[str]:
    """Reference designators that require hand soldering (THT)."""
    from kicad_tools.export.pnp import is_through_hole_footprint
    from kicad_tools.schema.pcb import PCB

    board = PCB.load(str(pcb))
    return {
        fp.reference for fp in board.footprints if fp.reference and is_through_hole_footprint(fp)
    }


def _net_metrics(pcb: Path) -> dict[str, Any]:
    """Routing-completion metrics for the emitted ``metrics`` block."""
    from kicad_tools.analysis.net_status import NetStatusAnalyzer
    from kicad_tools.schema.pcb import PCB

    board = PCB.load(str(pcb))
    result = NetStatusAnalyzer(board).analyze()
    return {"nets_routed_pct": round(float(result.connection_completion_percentage), 2)}


@dataclass
class Engines:
    """Injectable bundle of the engines the runner orchestrates.

    Every field defaults to the production implementation.  Tests replace
    individual callables to exercise the orchestration (gate ordering, verdict
    composition, refusal conditions) without KiCad on the host.
    """

    kct_check: Callable[[Path, str, Path, Sequence[str]], EngineRun] = _run_kct_check
    native_drc: Callable[[Path, Path], EngineRun] = _run_native_drc
    export: Callable[[Path, str, Path, bool], EngineRun] = _run_export
    refill: Callable[[Path], EngineRun] = _refill_and_save
    schematic_pdf: Callable[[Path, Path], EngineRun] = _export_schematic_pdf
    assembly_pdf: Callable[[Path, Sequence[str], Path], EngineRun] = _export_assembly_pdf
    fill_areas: Callable[[Path], dict[str, float]] = _fill_areas
    through_hole_refs: Callable[[Path], set[str]] = _through_hole_refs
    net_metrics: Callable[[Path], dict[str, Any]] = _net_metrics
    kicad_cli_version: Callable[[], str | None] = staticmethod(_kicad_cli_version)


# ---------------------------------------------------------------------------
# Engine fingerprint (additive readiness-v1 field)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineFingerprint:
    """Identity of the *checker* that produced a report.

    A version string alone does not identify check semantics: local rule fixes
    (#5012, #5016–#5018) changed sign-off outcomes with no board edit while
    every report still said ``kicad-tools 0.20.0``.  Recording the commit, a
    digest of the working source when it is dirty, the native KiCad version and
    the resolved rule/profile identity lets a consumer tell "re-qualified under
    the current engine" from "historical evidence from an older engine".
    """

    kicad_tools_version: str
    commit: str | None = None
    dirty: bool = False
    source_digest: str | None = None
    kicad_cli_version: str | None = None
    manufacturer: str = ""
    rules_digest: str | None = None
    recipe: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kicad_tools_version": self.kicad_tools_version,
            "dirty": self.dirty,
            "manufacturer": self.manufacturer,
        }
        for key, value in (
            ("commit", self.commit),
            ("source_digest", self.source_digest),
            ("kicad_cli_version", self.kicad_cli_version),
            ("rules_digest", self.rules_digest),
            ("recipe", self.recipe),
        ):
            if value is not None:
                payload[key] = value
        return payload


@lru_cache(maxsize=8)
def _git_describe(repo_root: Path) -> tuple[str | None, bool]:
    """Return ``(commit, dirty)`` for the kicad-tools checkout, if available."""
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
        return None, False
    if head.returncode != 0:
        return None, False
    return head.stdout.strip() or None, bool(status.stdout.strip())


@lru_cache(maxsize=4)
def _source_digest(package_root: Path) -> str | None:
    """Digest every ``.py`` under the installed package, in sorted order.

    This is what makes a *dirty* engine distinguishable: two runs from the same
    commit but different working trees produce different digests.
    """
    if not package_root.is_dir():  # pragma: no cover - defensive
        return None
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        digest.update(path.relative_to(package_root).as_posix().encode())
        try:
            digest.update(path.read_bytes())
        except OSError:  # pragma: no cover - defensive
            return None
    return digest.hexdigest()


def _rules_digest(paths: Iterable[Path]) -> str | None:
    """Digest the resolved rule/profile inputs in a stable order."""
    digest = hashlib.sha256()
    seen = False
    for path in sorted({p.resolve() for p in paths if p.is_file()}):
        digest.update(path.name.encode())
        digest.update(_sha256_file(path).encode())
        seen = True
    return digest.hexdigest() if seen else None


def build_fingerprint(
    manufacturer: str,
    rule_files: Iterable[Path],
    *,
    kicad_cli_version: str | None,
    recipe: str | None = None,
) -> EngineFingerprint:
    """Assemble the engine fingerprint recorded in the report."""
    import kicad_tools

    package_root = Path(kicad_tools.__file__).resolve().parent
    commit, dirty = _git_describe(package_root.parent.parent)
    return EngineFingerprint(
        kicad_tools_version=getattr(kicad_tools, "__version__", "unknown"),
        commit=commit,
        dirty=dirty,
        source_digest=_source_digest(package_root),
        kicad_cli_version=kicad_cli_version,
        manufacturer=manufacturer,
        rules_digest=_rules_digest(rule_files),
        recipe=recipe,
    )


# ---------------------------------------------------------------------------
# Options + results
# ---------------------------------------------------------------------------


@dataclass
class CheckOutcome:
    """One readiness-v1 ``checks[]`` entry plus the blockers it contributes."""

    name: str
    status: str
    detail: str
    evidence: str | None = None
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ReadinessOptions:
    """Fully resolved inputs for one readiness run."""

    board_dir: Path
    pcb: Path
    schematic: Path | None
    project: Path | None
    manufacturer: str
    mode: str
    output_dir: Path
    evidence_dir: Path
    ack_warnings: frozenset[str] = frozenset()
    include_tht: bool = False
    archive: bool = True
    net_class_map: Path | None = None
    hv_net_class: str = "HV"
    hv_requirement: str | None = None
    fill_tolerance_mm2: float = DEFAULT_FILL_TOLERANCE_MM2
    check_args: tuple[str, ...] = ()
    recipe: str | None = None

    @property
    def assembly(self) -> bool:
        return self.mode == "assembly"


@dataclass
class ReadinessResult:
    """The emitted report plus where it was written."""

    report: dict[str, Any]
    report_path: Path
    exit_code: int

    @property
    def status(self) -> str:
        return str(self.report.get("status", STATUS_UNVERIFIED))


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------


def _find_pcb(board_dir: Path) -> Path | None:
    """Pick the canonical routed board under ``<board_dir>/output`` (or the dir)."""
    for root in (board_dir / "output", board_dir):
        if not root.is_dir():
            continue
        candidates = [
            p for p in sorted(root.glob("*.kicad_pcb")) if not p.name.endswith("-bak.kicad_pcb")
        ]
        routed = [p for p in candidates if p.name.endswith("_routed.kicad_pcb")]
        if routed:
            return routed[0]
        if candidates:
            return candidates[0]
    return None


def _find_sibling(pcb: Path, suffix: str) -> Path | None:
    """Find a sibling file for *pcb*, preferring the exact stem match."""
    exact = pcb.with_suffix(suffix)
    if exact.is_file():
        return exact
    stem = pcb.stem.removesuffix("_routed")
    trimmed = pcb.parent / f"{stem}{suffix}"
    if trimmed.is_file():
        return trimmed
    candidates = sorted(pcb.parent.glob(f"*{suffix}"))
    return candidates[0] if candidates else None


def resolve_options(args: argparse.Namespace) -> tuple[ReadinessOptions | None, str | None]:
    """Resolve CLI arguments into :class:`ReadinessOptions`.

    Returns ``(options, error)`` — exactly one is non-``None``.  Resolution
    refuses rather than guessing: an unknown fab tier, a missing board, or a
    board with no paired ``.kicad_pro`` are all refusals (tapeout "Refusal
    conditions"), because the sign-off tier would otherwise be untrustworthy.
    """
    from kicad_tools.manufacturers import get_all_manufacturer_names

    target = Path(args.board).resolve()
    if not target.exists():
        return None, f"board path not found: {target}"

    if target.is_dir():
        board_dir = target
        pcb = _find_pcb(board_dir)
        if pcb is None:
            return None, f"no .kicad_pcb found under {board_dir}"
    else:
        if target.suffix != ".kicad_pcb":
            return None, f"expected a .kicad_pcb file or board directory, got {target.name}"
        pcb = target
        board_dir = target.parent.parent if target.parent.name == "output" else target.parent

    manufacturer = args.manufacturer or _discover_tier(board_dir, pcb)
    if manufacturer is None:
        return None, (
            "could not resolve a fabrication tier from the board recipe; "
            "pass --mfr explicitly (see `kct export --help` for the tier set)"
        )
    if manufacturer not in get_all_manufacturer_names():
        return None, f"unknown fabrication tier: {manufacturer}"

    project = _find_sibling(pcb, ".kicad_pro")
    if project is None:
        return None, (
            f"{pcb.name} has no paired .kicad_pro; the sign-off tier is not "
            "trustworthy without project rules (see #4097)"
        )

    schematic = (
        Path(args.schematic).resolve() if args.schematic else _find_sibling(pcb, ".kicad_sch")
    )

    output_dir = Path(args.output).resolve() if args.output else pcb.parent / "manufacturing"
    evidence_dir = board_dir / "output" / _EVIDENCE_DIRNAME
    if not (board_dir / "output").is_dir():
        evidence_dir = board_dir / _EVIDENCE_DIRNAME

    net_class_map = (
        Path(args.net_class_map).resolve() if args.net_class_map else _find_net_class_map(pcb)
    )

    ack = frozenset(rule.strip() for rule in (args.ack_warnings or "").split(",") if rule.strip())

    return (
        ReadinessOptions(
            board_dir=board_dir,
            pcb=pcb,
            schematic=schematic,
            project=project,
            manufacturer=manufacturer,
            mode="pcb_only" if args.pcb_only else "assembly",
            output_dir=output_dir,
            evidence_dir=evidence_dir,
            ack_warnings=ack,
            include_tht=bool(args.include_tht),
            archive=not args.no_archive,
            net_class_map=net_class_map,
            hv_net_class=args.hv_net_class,
            hv_requirement=args.hv_requirement,
            fill_tolerance_mm2=float(args.fill_tolerance),
        ),
        None,
    )


def _discover_tier(board_dir: Path, pcb: Path) -> str | None:
    """Read the fab tier from the board's own recipe/manifest, never a default."""
    manifest = pcb.parent / "manufacturing" / "manifest.json"
    tier = _read_json(manifest).get("manufacturer")
    if isinstance(tier, str) and tier:
        return tier
    spec = board_dir / "project.kct"
    if spec.is_file():
        for line in spec.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("manufacturer") and "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip("\"'") or None
    return None


def _find_net_class_map(pcb: Path) -> Path | None:
    for candidate in (
        pcb.parent / "net_class_map.json",
        pcb.parent / "output" / "net_class_map.json",
        pcb.parent.parent / "output" / "net_class_map.json",
    ):
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _gate_refill(options: ReadinessOptions, engines: Engines) -> CheckOutcome:
    """Refill the pours and commit them to the canonical PCB, transactionally.

    The refill runs on a **copy**; only a refill that actually produced a
    parseable board is promoted over the canonical source.  Then a second,
    independent refill of the saved board is compared per layer: equal copper
    is the only proof that the bytes the export reads are the bytes the
    checkers judged.
    """
    evidence_rel = _rel(options, options.evidence_dir / "fill-consistency.json")
    with tempfile.TemporaryDirectory(prefix="kct-readiness-") as tmp:
        staged = Path(tmp) / options.pcb.name
        shutil.copy2(options.pcb, staged)
        run = engines.refill(staged)
        if not run.ok:
            return CheckOutcome(
                name="zone_fill",
                status=NOT_RUN,
                detail=f"Zone refill did not run: {run.detail}",
                blockers=[
                    "Zone refill unavailable; copper could not be synchronized "
                    f"before checking ({run.detail})."
                ],
            )
        try:
            refilled_areas = engines.fill_areas(staged)
        except Exception as exc:
            return CheckOutcome(
                name="zone_fill",
                status=FAILED,
                detail=f"Refilled board could not be parsed: {exc}",
                blockers=[f"Refilled board is unreadable: {exc}"],
            )
        # Commit the refill to the canonical source deliberately, before both
        # the final native check and the export read it.
        shutil.copy2(staged, options.pcb)

    try:
        saved_areas = engines.fill_areas(options.pcb)
    except Exception as exc:  # pragma: no cover - defensive
        return CheckOutcome(
            name="zone_fill",
            status=FAILED,
            detail=f"Saved board could not be parsed: {exc}",
            blockers=[f"Saved board is unreadable: {exc}"],
        )

    deltas = {
        layer: round(abs(saved_areas.get(layer, 0.0) - refilled_areas.get(layer, 0.0)), 6)
        for layer in sorted(set(saved_areas) | set(refilled_areas))
    }
    options.evidence_dir.mkdir(parents=True, exist_ok=True)
    (options.evidence_dir / "fill-consistency.json").write_text(
        json.dumps(deltas, indent=2, sort_keys=True) + "\n"
    )

    divergent = {k: v for k, v in deltas.items() if v > options.fill_tolerance_mm2}
    if divergent:
        detail = ", ".join(f"{layer} {delta:.4f} mm^2" for layer, delta in divergent.items())
        return CheckOutcome(
            name="zone_fill",
            status=FAILED,
            detail=f"Saved copper differs from a fresh refill: {detail}",
            evidence=evidence_rel,
            blockers=[f"Saved-vs-refilled copper diverges ({detail})."],
        )
    return CheckOutcome(
        name="zone_fill",
        status=PASSED,
        detail=(
            f"Pours refilled and saved to {options.pcb.name}; "
            f"saved copper matches a fresh refill on {len(deltas)} layer(s)."
        ),
        evidence=evidence_rel,
    )


def _warning_counts(report: dict) -> dict[str, int]:
    """Group ``violations[]`` by ``rule_id``, warnings only (tapeout Gate 4b)."""
    counts: dict[str, int] = {}
    for violation in report.get("violations", []):
        if not isinstance(violation, dict) or violation.get("severity") != "warning":
            continue
        rule = str(violation.get("rule_id") or "unknown")
        counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items()))


def _format_warning_table(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{rule}={count}" for rule, count in counts.items())


def _gate_kct_check(options: ReadinessOptions, engines: Engines) -> tuple[CheckOutcome, dict]:
    """Gate 1 — ``kct check`` at the resolved fab tier."""
    report_path = options.evidence_dir / "kct-check.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    run = engines.kct_check(options.pcb, options.manufacturer, report_path, options.check_args)
    evidence_rel = _rel(options, report_path)
    if not run.ok:
        return (
            CheckOutcome(
                name="kct_check",
                status=NOT_RUN,
                detail=f"kct check did not produce a report: {run.detail}",
                blockers=[f"kct check could not run ({run.detail})."],
            ),
            {},
        )
    report = _read_json(report_path)
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    errors = int(summary.get("errors", 0) or 0)
    meta = report.get("meta_checks", {}) if isinstance(report.get("meta_checks"), dict) else {}
    counts = _warning_counts(report)
    table = _format_warning_table(counts)

    # The rolled-up ``overall`` cannot be used directly, for two reasons that
    # belong to *this* runner rather than to ``kct check``:
    #
    # * ``manifest`` is NOT RUN on a first sign-off because the bundle this
    #   command is about to produce does not exist yet.  Deferring it is safe
    #   only because the ``artifacts`` gate below re-verifies the manifest
    #   *and* the archived source provenance — strictly more than the
    #   sub-check would have.
    # * ``lvs`` has its own dedicated gate, which additionally rejects a
    #   vacuous comparison (#4006) that the sub-check reports as PASSED.
    gating = {
        name: str((meta.get(name) or {}).get("status", "NOT RUN"))
        for name in ("drc", "erc")
        if isinstance(meta.get(name), dict)
    }
    failed = sorted(name for name, status in gating.items() if status == "FAILED")
    not_run = sorted(name for name, status in gating.items() if status == "NOT RUN")
    deferred = " Manifest verification is deferred to the artifacts gate."

    if errors or failed:
        named = ", ".join(failed) or "none"
        return (
            CheckOutcome(
                name="kct_check",
                status=FAILED,
                detail=(
                    f"{errors} electrical/manufacturing error(s); failed sub-checks: "
                    f"{named}; warning counts: {table}."
                ),
                evidence=evidence_rel,
                blockers=[f"kct check reports {errors} error(s); failed sub-checks: {named}."],
            ),
            report,
        )
    if not_run:
        named = ", ".join(not_run)
        return (
            CheckOutcome(
                name="kct_check",
                status=NOT_RUN,
                detail=f"Sub-checks did not run: {named}; warning counts: {table}.",
                evidence=evidence_rel,
                blockers=[f"kct check sub-check(s) did not run: {named}."],
            ),
            report,
        )
    return (
        CheckOutcome(
            name="kct_check",
            status=PASSED,
            detail=(
                f"0 electrical/manufacturing errors; DRC and ERC sub-checks passed; "
                f"warning counts: {table}.{deferred}"
            ),
            evidence=evidence_rel,
        ),
        report,
    )


def _gate_native_drc(options: ReadinessOptions, engines: Engines) -> tuple[CheckOutcome, dict]:
    """Gate 2 — the mandatory independent ``kicad-cli pcb drc --refill-zones``."""
    report_path = options.evidence_dir / "native-drc.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    run = engines.native_drc(options.pcb, report_path)
    evidence_rel = _rel(options, report_path)
    payload = _read_json(report_path)
    if not run.ok or not payload.get("ran"):
        detail = run.detail or str(payload.get("note") or "native DRC did not run")
        return (
            CheckOutcome(
                name="native_drc",
                status=NOT_RUN,
                detail=f"Independent native DRC did not run: {detail}",
                evidence=evidence_rel if report_path.is_file() else None,
                blockers=[
                    "Independent native DRC (kicad-cli pcb drc --refill-zones) "
                    f"could not run ({detail}); this is a hard blocker, not a pass."
                ],
            ),
            payload,
        )
    errors = int(payload.get("error_count") or 0)
    by_type = payload.get("by_type") if isinstance(payload.get("by_type"), dict) else {}
    warn_table = _format_warning_table({k: int(v) for k, v in (by_type or {}).items()})
    if errors:
        return (
            CheckOutcome(
                name="native_drc",
                status=FAILED,
                detail=f"{errors} error-level finding(s) after zone refill: {warn_table}.",
                evidence=evidence_rel,
                blockers=[f"Native DRC reports {errors} error-level finding(s)."],
            ),
            payload,
        )
    return (
        CheckOutcome(
            name="native_drc",
            status=PASSED,
            detail="0 error-level findings after zone refill.",
            evidence=evidence_rel,
        ),
        payload,
    )


def _gate_lvs(check_report: dict) -> tuple[CheckOutcome, dict]:
    """Require *meaningful* LVS evidence, not merely a green sub-check.

    A vacuous comparison (#4006 — the schematic bound zero pins, so the
    comparator observed nothing) can never be clean; it is "not run".
    """
    meta = (
        check_report.get("meta_checks", {})
        if isinstance(check_report.get("meta_checks"), dict)
        else {}
    )
    lvs = meta.get("lvs") if isinstance(meta.get("lvs"), dict) else None
    if not lvs:
        return (
            CheckOutcome(
                name="lvs",
                status=NOT_RUN,
                detail="No LVS evidence in the kct check report.",
                blockers=["LVS evidence is missing; layout-vs-schematic was not verified."],
            ),
            {},
        )
    status = str(lvs.get("status", "NOT RUN"))
    bound = lvs.get("copper_bound_pad_count")
    vacuous = lvs.get("copper_vacuous") is True or bound == 0
    mismatches = len(lvs.get("mismatches") or []) + len(lvs.get("copper_mismatches") or [])
    metrics = {
        "lvs_clean": status == "PASSED" and not vacuous and mismatches == 0,
        "lvs_mismatches": mismatches,
    }
    if vacuous:
        return (
            CheckOutcome(
                name="lvs",
                status=NOT_RUN,
                detail="LVS comparison is vacuous (0 schematic pins bound); no evidence.",
                blockers=["LVS evidence is vacuous (0 bound pins); nothing was compared."],
            ),
            {"lvs_clean": False, "lvs_mismatches": mismatches},
        )
    if status != "PASSED" or mismatches:
        return (
            CheckOutcome(
                name="lvs",
                status=FAILED if status == "FAILED" or mismatches else NOT_RUN,
                detail=f"LVS {status} with {mismatches} mismatch(es).",
                blockers=[f"LVS reports {mismatches} mismatch(es) (status {status})."],
            ),
            metrics,
        )
    return (
        CheckOutcome(
            name="lvs",
            status=PASSED,
            detail=f"LVS PASSED over {bound if bound is not None else 'all'} bound pad(s).",
        ),
        metrics,
    )


def _gate_warning_review(
    options: ReadinessOptions, check_report: dict
) -> tuple[CheckOutcome, dict[str, int]]:
    """Gate 4b — per-rule warning table + assembly-affecting acknowledgement."""
    counts = _warning_counts(check_report)
    table = _format_warning_table(counts)
    unacknowledged = sorted(
        rule
        for rule, count in counts.items()
        if count and rule in ASSEMBLY_AFFECTING_RULES and rule not in options.ack_warnings
    )
    if unacknowledged:
        named = ", ".join(f"{rule}={counts[rule]}" for rule in unacknowledged)
        return (
            CheckOutcome(
                name="warning_review",
                status=FAILED,
                detail=f"Unacknowledged assembly-affecting warnings: {named}. Table: {table}.",
                blockers=[
                    "Unacknowledged assembly-affecting warnings "
                    f"({named}); acknowledge with --ack-warnings or fix them."
                ],
            ),
            counts,
        )
    acked = sorted(options.ack_warnings & set(counts))
    suffix = f" Acknowledged: {', '.join(acked)}." if acked else ""
    return (
        CheckOutcome(
            name="warning_review",
            status=PASSED,
            detail=f"Per-rule warning table reviewed: {table}.{suffix}",
        ),
        counts,
    )


def _gate_hv_isolation(options: ReadinessOptions) -> CheckOutcome | None:
    """Gate 4 (conditional) — refuse to sign off an un-evaluated HV path.

    Returns ``None`` for boards that declare no HV net class, so the check is
    omitted entirely rather than recorded as a vacuous pass.
    """
    if options.net_class_map is None or not options.net_class_map.is_file():
        return None
    classes = _read_json(options.net_class_map)
    hv_nets = sorted(
        net
        for net, spec in classes.items()
        if isinstance(spec, dict)
        and str(spec.get("name", "")).strip().lower() == options.hv_net_class.strip().lower()
    )
    if not hv_nets:
        return None
    if not options.hv_requirement:
        return CheckOutcome(
            name="hv_isolation",
            status=NOT_RUN,
            detail=(
                f"{len(hv_nets)} HV net(s) present with no isolation requirement "
                "specified (--hv-requirement)."
            ),
            blockers=[
                f"HV nets present ({', '.join(hv_nets[:5])}) but no isolation "
                "requirement was supplied; run `kct audit --hv-standard ...` and "
                "record the verdict with --hv-requirement."
            ],
        )
    return CheckOutcome(
        name="hv_isolation",
        status=PASSED,
        detail=f"{len(hv_nets)} HV net(s) gated against: {options.hv_requirement}.",
    )


# ---------------------------------------------------------------------------
# Packaging gates
# ---------------------------------------------------------------------------


def _bundle_files(output_dir: Path, exclude: Sequence[str] = ()) -> list[Path]:
    excluded = set(exclude)
    return sorted(
        p
        for p in output_dir.rglob("*")
        if p.is_file() and p.relative_to(output_dir).as_posix() not in excluded
    )


def _write_full_manifest(options: ReadinessOptions, warning_counts: dict[str, int]) -> Path:
    """Regenerate ``manifest.json`` so it checksums the ENTIRE bundle.

    ``kct export``'s manifest only covers what export itself produced; the
    drawings and README added afterwards would otherwise ride along
    unchecksummed, breaking the "upload as-is" contract (tapeout Gate 8).
    """
    manifest_path = options.output_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    files = {
        path.relative_to(options.output_dir).as_posix(): {
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in _bundle_files(options.output_dir, exclude=("manifest.json",))
    }
    manifest.update(
        {
            "version": manifest.get("version", "1.0"),
            "generated_at": _iso_now(),
            "manufacturer": options.manufacturer,
            "mode": options.mode,
            "files": files,
            "board": {"name": options.pcb.stem, "pcb_file": options.pcb.name},
            "warning_counts_by_rule": dict(sorted(warning_counts.items())),
            "acknowledged_warning_rules": sorted(options.ack_warnings),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def _verify_project_provenance(options: ReadinessOptions) -> list[str]:
    """Confirm the archived project sources ARE the checked sources.

    Manifest integrity alone proves the bundle is internally consistent; it
    says nothing about whether the PCB inside ``kicad_project.zip`` is the one
    that was checked.  That is exactly the defect this gate exists to catch.
    """
    archive = options.output_dir / "kicad_project.zip"
    if not archive.is_file():
        return ["kicad_project.zip is missing from the manufacturing bundle"]
    problems: list[str] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            members = {Path(name).name: name for name in zf.namelist()}
            for source in (options.pcb, options.schematic):
                if source is None or not source.is_file():
                    continue
                member = members.get(source.name)
                if member is None:
                    problems.append(f"{source.name} is absent from kicad_project.zip")
                    continue
                if hashlib.sha256(zf.read(member)).hexdigest() != _sha256_file(source):
                    problems.append(
                        f"{source.name} inside kicad_project.zip differs from the checked source"
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"kicad_project.zip could not be read: {exc}"]
    return problems


def _write_readme(
    options: ReadinessOptions,
    warning_counts: dict[str, int],
    tht_refs: Sequence[str],
) -> Path:
    """Gate 7 — the human-facing bundle index."""
    lines = [
        f"Manufacturing package for {options.pcb.name}",
        "",
        f"Fabrication tier : {options.manufacturer}",
        f"Mode             : {options.mode}",
        f"Generated        : {_iso_now()}",
        "",
    ]
    if options.assembly:
        lines.append("Assembly package: BOM and CPL are included.")
    else:
        lines.append("Bare-board package: BOM and CPL are intentionally absent.")
    lines.extend(["", "Contents", "--------"])
    for path in _bundle_files(options.output_dir, exclude=("README.txt",)):
        lines.append(f"  {path.relative_to(options.output_dir).as_posix()}")

    lines.extend(["", "Hand-solder / through-hole items", "--------------------------------"])
    if not options.assembly:
        lines.append("  n/a (bare-board order; no assembly performed)")
    elif tht_refs:
        lines.append(
            "  The following parts are EXCLUDED from the SMT CPL and must be hand-soldered:"
        )
        lines.append(f"  {', '.join(tht_refs)}")
    else:
        lines.append("  none (every placed part is SMT)")

    lines.extend(["", "Warning review (per rule)", "-------------------------"])
    if warning_counts:
        for rule, count in warning_counts.items():
            lines.append(f"  {rule}: {count}")
    else:
        lines.append("  no warnings reported")
    acked = sorted(options.ack_warnings & set(warning_counts))
    assembly_affecting = {
        rule: count
        for rule, count in warning_counts.items()
        if count and rule in ASSEMBLY_AFFECTING_RULES
    }
    if not assembly_affecting:
        lines.append("  All assembly-affecting warning counts are zero.")
    for rule in acked:
        lines.append(
            f"  ACCEPTED RISK: {rule} ({warning_counts[rule]} warning(s)) "
            "explicitly acknowledged via --ack-warnings."
        )
    lines.extend(["", "Accepted-risk waivers", "---------------------"])
    lines.append(
        "  no accepted-risk waivers" if not acked else f"  see the {len(acked)} line(s) above"
    )
    lines.append("")

    readme = options.output_dir / "README.txt"
    readme.write_text("\n".join(lines))
    return readme


def _gate_artifacts(
    options: ReadinessOptions,
    engines: Engines,
    warning_counts: dict[str, int],
    tht_refs: Sequence[str],
) -> CheckOutcome:
    """Gate 3/5/6/7/8 — export, drawings, README, full manifest, provenance."""
    from kicad_tools.export.manufacturing import verify_manifest

    options.output_dir.mkdir(parents=True, exist_ok=True)
    run = engines.export(options.pcb, options.manufacturer, options.output_dir, options.assembly)
    if not run.ok:
        return CheckOutcome(
            name="artifacts",
            status=NOT_RUN,
            detail=f"kct export did not produce a bundle: {run.detail}",
            blockers=[f"Manufacturing export could not run ({run.detail})."],
        )

    problems: list[str] = []

    # Gate 5 — schematic PDF (full hierarchy).
    if options.schematic is None or not options.schematic.is_file():
        problems.append("no schematic source found; the schematic PDF cannot be produced")
    else:
        pdf = options.output_dir / "schematic.pdf"
        sch_run = engines.schematic_pdf(options.schematic, pdf)
        if not sch_run.ok:
            problems.append(f"schematic PDF not produced ({sch_run.detail})")

    # Gate 6 — assembly views, front and back.
    for name, layers in (
        ("assembly-front.pdf", ("F.Cu", "F.SilkS", "Edge.Cuts")),
        ("assembly-back.pdf", ("B.Cu", "B.SilkS", "Edge.Cuts")),
    ):
        pdf = options.output_dir / name
        pdf_run = engines.assembly_pdf(options.pcb, layers, pdf)
        if not pdf_run.ok:
            problems.append(f"{name} not produced ({pdf_run.detail})")

    # Gate 7 — README, written before the manifest so it is checksummed.
    _write_readme(options, warning_counts, tht_refs)

    # Gate 8 — the manifest must cover the finished directory, and be newest.
    manifest_path = _write_full_manifest(options, warning_counts)
    problems.extend(verify_manifest(manifest_path))

    manifest_files = set(_read_json(manifest_path).get("files", {}))
    on_disk = {
        p.relative_to(options.output_dir).as_posix()
        for p in _bundle_files(options.output_dir, exclude=("manifest.json",))
    }
    for missing in sorted(on_disk - manifest_files):
        problems.append(f"{missing}: present in the bundle but absent from the manifest")

    # Source provenance: the archived project must BE the checked design.
    problems.extend(_verify_project_provenance(options))

    if problems:
        return CheckOutcome(
            name="artifacts",
            status=FAILED,
            detail="; ".join(problems[:4]) + (" ..." if len(problems) > 4 else ""),
            blockers=[f"Manufacturing bundle is incomplete: {problem}" for problem in problems],
        )
    return CheckOutcome(
        name="artifacts",
        status=PASSED,
        detail=(
            f"Fresh {'assembly' if options.assembly else 'bare-board'} bundle of "
            f"{len(on_disk)} file(s); SHA256 integrity and source ZIP provenance verified."
        ),
    )


def _read_csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    import csv

    with path.open(newline="") as handle:
        rows = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _gate_bom(options: ReadinessOptions, engines: Engines, tht_refs: Sequence[str]) -> CheckOutcome:
    """Gate 4 — procurement identity (assembly) / explicit no-claim (pcb_only)."""
    if not options.assembly:
        return CheckOutcome(
            name="bom",
            status=PASSED,
            detail="PCB-only fabrication; assembly not included.",
        )

    bom = next(iter(sorted(options.output_dir.glob("bom*.csv"))), None)
    if bom is None:
        return CheckOutcome(
            name="bom",
            status=NOT_RUN,
            detail="No BOM was produced for an assembly package.",
            blockers=["Assembly mode requires a BOM; none was produced."],
        )
    header, rows = _read_csv_rows(bom)
    lowered = [column.strip().lower() for column in header]
    part_index = next(
        (lowered.index(name) for name in PART_ID_COLUMNS if name in lowered),
        None,
    )
    if part_index is None:
        return CheckOutcome(
            name="bom",
            status=FAILED,
            detail=f"{bom.name} has no procurement-identifier column.",
            blockers=[
                f"{bom.name} carries no procurement-identifier column; the "
                "part-number matcher did not run (see #4104)."
            ],
        )
    designator_index = next(
        (lowered.index(name) for name in ("designator", "designators", "reference", "references")),
        None,
    )

    unresolved: list[str] = []
    for index, row in enumerate(rows):
        value = row[part_index].strip() if part_index < len(row) else ""
        if value.lower() in PLACEHOLDER_PART_IDS:
            label = (
                row[designator_index].strip()
                if designator_index is not None and designator_index < len(row)
                else f"row {index + 2}"
            )
            unresolved.append(label or f"row {index + 2}")

    if rows and len(unresolved) == len(rows):
        return CheckOutcome(
            name="bom",
            status=FAILED,
            detail=(
                f"{bom.name}: every one of {len(rows)} line(s) has an empty "
                "procurement identifier — the matcher is broken, not the parts exotic."
            ),
            blockers=[
                "BOM part-number resolution machinery is unavailable (the entire "
                "column is empty); refusing to call an unpopulated BOM resolved (#4104)."
            ],
        )
    if unresolved:
        shown = ", ".join(unresolved[:8]) + (" ..." if len(unresolved) > 8 else "")
        return CheckOutcome(
            name="bom",
            status=FAILED,
            detail=f"{len(unresolved)} BOM line(s) lack a procurement identifier: {shown}.",
            blockers=[f"{len(unresolved)} BOM line(s) require human part selection: {shown}."],
        )

    cpl = next(iter(sorted(options.output_dir.glob("cpl*.csv"))), None)
    if cpl is None:
        return CheckOutcome(
            name="bom",
            status=NOT_RUN,
            detail="No CPL was produced for an assembly package.",
            blockers=["Assembly mode requires a CPL; none was produced."],
        )
    cpl_header, cpl_rows = _read_csv_rows(cpl)
    cpl_lowered = [column.strip().lower() for column in cpl_header]
    cpl_ref_index = next(
        (
            cpl_lowered.index(name)
            for name in ("designator", "reference", "ref")
            if name in cpl_lowered
        ),
        0,
    )
    placed = {
        row[cpl_ref_index].strip()
        for row in cpl_rows
        if cpl_ref_index < len(row) and row[cpl_ref_index].strip()
    }
    leaked = sorted(placed & set(tht_refs))
    if leaked and not options.include_tht:
        return CheckOutcome(
            name="bom",
            status=FAILED,
            detail=f"CPL lists through-hole parts the SMT line cannot place: {', '.join(leaked)}.",
            blockers=[
                "CPL includes through-hole parts "
                f"({', '.join(leaked)}) that must be hand-soldered; exclude them "
                "or pass --include-tht deliberately."
            ],
        )

    readme = options.output_dir / "README.txt"
    readme_text = readme.read_text() if readme.is_file() else ""
    missing_instructions = [ref for ref in tht_refs if ref not in readme_text]
    if tht_refs and missing_instructions and not options.include_tht:
        return CheckOutcome(
            name="bom",
            status=FAILED,
            detail=(
                "Manual through-hole parts are not named in the bundle README: "
                f"{', '.join(missing_instructions)}."
            ),
            blockers=[
                "Through-hole parts excluded from the CPL are not documented as "
                f"hand-solder items: {', '.join(missing_instructions)}."
            ],
        )

    tht_note = (
        f"SMT CPL excludes {len(tht_refs)} manual through-hole component(s)."
        if tht_refs
        else "every placed part is SMT."
    )
    return CheckOutcome(
        name="bom",
        status=PASSED,
        detail=(f"{len(rows)} BOM line(s) carry verified procurement identities; {tht_note}"),
    )


def _build_archive(options: ReadinessOptions) -> Path | None:
    """Zip the whole manufacturing directory OUTSIDE it (no checksum cycle)."""
    archive = options.output_dir.parent / _ARCHIVE_NAME
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _bundle_files(options.output_dir):
            zf.write(path, Path(options.output_dir.name) / path.relative_to(options.output_dir))
    return archive


# ---------------------------------------------------------------------------
# Report composition
# ---------------------------------------------------------------------------


def _rel(options: ReadinessOptions, path: Path) -> str:
    """Board-relative POSIX path, as the readiness ``inputs`` contract requires."""
    return path.resolve().relative_to(options.board_dir.resolve()).as_posix()


def _hashable_inputs(options: ReadinessOptions) -> list[Path]:
    """Every file whose bytes the verdict depends on.

    ``read_readiness()`` additionally *requires* that every current
    ``output/*.kicad_pro``, ``output/*.kicad_dru``, ``output/*net_class_map.json``
    and ``output/fab_profile.json`` appear here, so evidence cannot omit the
    rule files it was computed against.
    """
    root = options.board_dir.resolve()
    paths: set[Path] = set()

    for candidate in (options.pcb, options.schematic, options.project, options.net_class_map):
        if candidate is not None and candidate.is_file():
            paths.add(candidate.resolve())

    output_dir = options.board_dir / "output"
    if output_dir.is_dir():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            if (
                path.suffix in {".kicad_pro", ".kicad_dru"}
                or path.name.endswith("net_class_map.json")
                or path.name == "fab_profile.json"
            ):
                paths.add(path.resolve())

    for directory in (options.output_dir, options.evidence_dir):
        if directory.is_dir():
            paths.update(p.resolve() for p in directory.rglob("*") if p.is_file())

    archive = options.output_dir.parent / _ARCHIVE_NAME
    if archive.is_file():
        paths.add(archive.resolve())

    spec = options.board_dir / "project.kct"
    if spec.is_file():
        paths.add(spec.resolve())

    return sorted(p for p in paths if p.is_relative_to(root) and p.name != _REPORT_NAME)


def build_report(
    options: ReadinessOptions,
    checks: Sequence[CheckOutcome],
    metrics: dict[str, Any],
    fingerprint: EngineFingerprint,
) -> dict[str, Any]:
    """Compose the readiness-v1 document from the gate outcomes."""
    blockers: list[str] = []
    for check in checks:
        blockers.extend(check.blockers)

    if any(check.status == FAILED for check in checks):
        status = STATUS_BLOCKED
    elif any(check.status == NOT_RUN for check in checks):
        status = STATUS_UNVERIFIED
    else:
        status = STATUS_READY

    # Defence in depth: `ready` is only ever reachable with every required
    # check named AND passed AND no blockers.  There is no bypass flag.
    named = {check.name for check in checks if check.status == PASSED}
    if status == STATUS_READY and (blockers or not set(REQUIRED_CHECKS).issubset(named)):
        status = STATUS_BLOCKED
        missing = sorted(set(REQUIRED_CHECKS) - named)
        if missing:
            blockers.append(f"Required checks did not pass: {', '.join(missing)}.")

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": _iso_now(),
        "mode": options.mode,
        "status": status,
        "blockers": blockers,
        "checks": [check.to_dict() for check in checks],
        "engine": fingerprint.to_dict(),
    }

    evidence = {check.name: check.evidence for check in checks if check.evidence}
    if evidence:
        report["evidence"] = evidence
    if metrics and status != STATUS_UNVERIFIED:
        report["metrics"] = metrics

    report["inputs"] = {
        _rel(options, path): _sha256_file(path) for path in _hashable_inputs(options)
    }
    return report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_readiness(options: ReadinessOptions, engines: Engines | None = None) -> ReadinessResult:
    """Run every applicable gate in contract order and emit the report.

    The ordering is not cosmetic:

    1. Refill the pours and **save the canonical PCB** — so the checkers and
       the export all read the same copper (the issue's saved-vs-refilled
       divergence defect).
    2. ``kct check`` at the resolved tier (writes the machine-readable report
       the warning review reuses; no second check run).
    3. The mandatory independent native DRC cross-gate, on the saved board.
    4. LVS evidence, warning review, HV isolation.
    5. Export + drawings + README + full-bundle manifest + provenance, from
       that same saved board.
    6. BOM / CPL procurement identity.
    7. Package the archive outside the checksummed directory, then hash
       everything into the report.
    """
    engines = engines or Engines()
    options.evidence_dir.mkdir(parents=True, exist_ok=True)

    checks: list[CheckOutcome] = []
    metrics: dict[str, Any] = {}

    fill = _gate_refill(options, engines)
    checks.append(fill)

    kct_check, check_report = _gate_kct_check(options, engines)
    checks.append(kct_check)

    native, native_report = _gate_native_drc(options, engines)
    checks.append(native)

    lvs, lvs_metrics = _gate_lvs(check_report)
    checks.append(lvs)
    metrics.update(lvs_metrics)

    warning_review, warning_counts = _gate_warning_review(options, check_report)
    checks.append(warning_review)

    hv = _gate_hv_isolation(options)
    if hv is not None:
        checks.append(hv)

    try:
        tht_refs = sorted(engines.through_hole_refs(options.pcb))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not determine through-hole parts: %s", exc)
        tht_refs = []

    checks.append(_gate_artifacts(options, engines, warning_counts, tht_refs))
    checks.append(_gate_bom(options, engines, tht_refs))

    if options.archive:
        _build_archive(options)

    summary = (
        check_report.get("summary", {}) if isinstance(check_report.get("summary"), dict) else {}
    )
    drc_counts = [int(summary.get("errors", 0) or 0)]
    if native_report.get("ran"):
        drc_counts.append(int(native_report.get("error_count") or 0))
    # Max, never sum: the two engines detect overlapping populations.
    metrics["drc_violations"] = max(drc_counts)
    try:
        metrics.update(engines.net_metrics(options.pcb))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not compute routing metrics: %s", exc)

    rule_files = [p for p in (options.project, options.net_class_map) if p is not None]
    rule_files.extend(sorted((options.board_dir / "output").glob("*.kicad_dru")))
    fingerprint = build_fingerprint(
        options.manufacturer,
        rule_files,
        kicad_cli_version=engines.kicad_cli_version(),
        recipe=options.recipe,
    )

    report = build_report(options, checks, metrics, fingerprint)
    report_path = options.board_dir / "output" / _REPORT_NAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")

    # Round-trip through the consumer-side validator: a report this runner
    # calls `ready` must be one the gallery accepts, or it is not ready.
    validated = read_readiness(options.board_dir)
    if report["status"] == STATUS_READY and validated.get("status") != STATUS_READY:
        report["status"] = STATUS_BLOCKED
        report["blockers"] = [
            *report["blockers"],
            *validated.get("blockers", ["Readiness evidence failed validation."]),
        ]
        report_path.write_text(json.dumps(report, indent=2) + "\n")

    exit_code = 0 if report["status"] == STATUS_READY else 1
    return ReadinessResult(report=report, report_path=report_path, exit_code=exit_code)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(result: ReadinessResult, options: ReadinessOptions) -> None:
    report = result.report
    print(f"Readiness: {report['status'].upper()}  ({options.mode}, {options.manufacturer})")
    print(f"  Board  : {options.pcb}")
    print(f"  Report : {result.report_path}")
    print()
    for check in report["checks"]:
        print(f"  [{check['status']:>8s}] {check['name']}: {check['detail']}")
    if report["blockers"]:
        print()
        print(f"  {len(report['blockers'])} blocker(s):")
        for blocker in report["blockers"]:
            print(f"    - {blocker}")


def build_parser() -> argparse.ArgumentParser:
    """Standalone parser for ``python -m kicad_tools.cli.readiness_cmd``."""
    parser = argparse.ArgumentParser(
        prog="kct readiness",
        description=(
            "Run the manufacturing-readiness / tapeout gates and emit hash-bound "
            "output/readiness.json evidence."
        ),
    )
    add_readiness_arguments(parser)
    return parser


def add_readiness_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the readiness arguments on *parser* (shared with the unified CLI)."""
    parser.add_argument(
        "board",
        help="Board directory or routed .kicad_pcb to sign off",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        dest="manufacturer",
        default=None,
        help="Fabrication tier (default: discovered from the board's recipe/manifest)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--assembly",
        action="store_true",
        help="Full assembly package: gerbers + drill + BOM/CPL with procurement identities (default)",
    )
    mode.add_argument(
        "--pcb-only",
        action="store_true",
        help="Bare-board package: gerbers + drill only; makes no assembly claim",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Manufacturing bundle directory (default: <pcb-dir>/manufacturing/)",
    )
    parser.add_argument(
        "--sch",
        dest="schematic",
        default=None,
        help="Path to the .kicad_sch (auto-detected by default)",
    )
    parser.add_argument(
        "--net-class-map",
        default=None,
        help="Net-class map sidecar (auto-discovered by default)",
    )
    parser.add_argument(
        "--ack-warnings",
        default="",
        help=(
            "Comma-separated rule_ids whose assembly-affecting warnings are "
            "explicitly accepted; each becomes an accepted-risk line in README.txt"
        ),
    )
    parser.add_argument(
        "--include-tht",
        action="store_true",
        help="Accept through-hole parts in the CPL (they are excluded by default)",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip building output/manufacturing.zip",
    )
    parser.add_argument(
        "--hv-net-class",
        default="HV",
        help="Net-class name identifying high-voltage nets (default: HV)",
    )
    parser.add_argument(
        "--hv-requirement",
        default=None,
        help=(
            "Record the isolation requirement an HV board was gated against "
            "(e.g. 'iec60664 250Vrms PD2 MGII'). Required when HV nets exist."
        ),
    )
    parser.add_argument(
        "--fill-tolerance",
        type=float,
        default=DEFAULT_FILL_TOLERANCE_MM2,
        help=(
            "Per-layer filled-copper area tolerance in mm^2 for the "
            f"saved-vs-refilled equivalence check (default: {DEFAULT_FILL_TOLERANCE_MM2})"
        ),
    )
    add_format_flag(parser)


def main(argv: list[str] | None = None, engines: Engines | None = None) -> int:
    """Entry point for ``kct readiness``.

    Exit code is ``0`` only for a ``ready`` verdict; every incomplete or failing
    gate exits non-zero.  There is deliberately no flag that produces ``ready``
    on a partial run.
    """
    args = build_parser().parse_args(argv)
    return run_from_args(args, engines=engines)


def run_from_args(args: argparse.Namespace, engines: Engines | None = None) -> int:
    """Shared dispatch body for both the standalone and unified CLI paths."""
    as_json = getattr(args, "format", "text") == FORMAT_JSON
    options, error = resolve_options(args)
    if options is None:
        payload = {
            "command": "readiness",
            "success": False,
            "error": error,
            "status": STATUS_UNVERIFIED,
        }
        if as_json:
            emit_json(payload)
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2

    with stdout_to_stderr_when(as_json):
        result = run_readiness(options, engines)

    if as_json:
        emit_json(
            {
                "command": "readiness",
                "board": str(options.board_dir),
                "pcb": str(options.pcb),
                "mode": options.mode,
                "manufacturer": options.manufacturer,
                "report_path": str(result.report_path),
                "readiness": result.report,
                "success": result.exit_code == 0,
            }
        )
    else:
        _print_report(result, options)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
