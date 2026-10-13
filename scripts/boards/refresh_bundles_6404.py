"""Refill saved zones, re-plot and re-pin a board's hash-bound bundle (Issue #6404).

#6390 declared each demo board's router net classes in its routed sidecars,
and #6396 moved board 07's copper.  Both left the boards' saved zone fills
computed under the old rules (native DRC *without* ``--refill-zones`` reports
via/pad-to-zone clearance findings), and left each manufacturing bundle bound
to the old PCB.  ``refresh_readiness.py`` re-hashes evidence but cannot fix
either: its DRC gate refills a throwaway copy, and it carries the old
Gerbers forward.  Rebuilding only ``kicad_project.zip`` would make ``kct
check``'s manifest meta-check pass while the Gerbers, drawings and renders
still showed the old copper, so this driver re-plots them.

It works on a staged copy of the board and publishes nothing unless every
gate passes.  Stages, in order:

1. **Board evidence** (board 07): run ``real_design/build.py``, publish the
   routed PCB it validated, and copy its ``validation/`` evidence into
   ``output/readiness/`` and ``real_design/reviewed-routing/evidence/``.  The
   build must have filled and checked the board under the published routed
   sidecars (byte-identical ``.kicad_pro`` / ``.kicad_dru``); otherwise the
   evidence would describe different rules.  Evidence is copied only when it
   describes a different board than the committed evidence, so a rerun keeps
   the committed (date-stamped) reports.
2. **Refill**: refill the routed PCB's zones under its own sidecars with
   ``declare_router_netclasses_6390.refill_zones_in_place`` (a plain native
   refill, transplanting only the ``(zone ...)`` blocks).  Refuses if anything
   outside the zone blocks changes.  When the refilled zones are
   token-identical to the saved ones the PCB bytes are left alone.
3. **Saved-fill DRC gate**: ``kicad-cli pcb drc --severity-all`` *without*
   ``--refill-zones`` must report 0 errors, 0 zone clearance findings and 0
   unconnected items.  The existing gates only check refilled copper, which
   is how stale fills slipped through.
4. **Re-plot** (only when the bundle no longer binds the current routed
   project files, or with ``--force-replot``): a fresh ``kct export`` supplies
   ``gerbers/gerbers.zip`` (re-zipped deterministically), ``images/*`` and the
   report; ``kicad-cli`` re-plots the assembly PDFs; ``kct render`` the
   board renders.  Also re-written: the saved-fill native DRC report,
   ``fill-consistency.json``, ``refill-stability.json`` and the ``kct check``
   report.  The fresh export's CPL must equal the committed one: placement
   is not this driver's business.
5. ``refresh_readiness.py``'s pipeline: native ERC, deterministic archive
   rebuilds, manifest re-hash (only ``files`` may change), its gates
   (refilled native DRC, ``kct check``, ``net-status``, ``validate --sync``),
   metric comparison, and the ``readiness.json`` re-pin validated by
   ``read_readiness()``.

Usage (repository root, KiCad 10 ``kicad-cli`` on PATH or ``KICAD_CLI_DIR``)::

    uv run --with shapely python scripts/boards/refresh_bundles_6404.py \\
        boards/07-matchgroup-test --checked-at 2026-10-13T00:00:00+00:00 \\
        --save-erc /tmp/erc

A rerun with the same ``--checked-at`` and ``--reuse-erc`` publishes 0 files.
``--build-dir DIR`` keeps board 07's ``build.py`` output in DIR, and replays
it when DIR already holds one.  ``--dry-run`` runs every gate and publishes
nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import declare_router_netclasses_6390 as netclasses  # noqa: E402
import refresh_readiness as rr  # noqa: E402

GateFailure = rr.GateFailure
log = rr.log
sha256 = rr.sha256

REPO_ROOT = rr.REPO_ROOT


@dataclass(frozen=True)
class BundleConfig:
    """Where one board keeps its plotted artifacts and evidence copies."""

    manufacturer: str  # ``kct export --mfr``
    #: Bundle-relative members taken from a fresh ``kct export``.
    export_members: tuple[str, ...] = (
        "gerbers/gerbers.zip",
        "images/*.png",
        "report.md",
        "report.pdf",
    )
    #: Bundle-relative assembly drawings and their plotted layers.
    assembly_pdfs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("assembly-front.pdf", ("F.Cu", "F.SilkS", "Edge.Cuts")),
        ("assembly-back.pdf", ("B.Cu", "B.SilkS", "Edge.Cuts")),
    )
    #: Board-relative render directory re-plotted by ``kct render`` (or None).
    renders: str | None = "output/renders"
    #: Board-relative copies of the saved-fill native DRC report.
    saved_drc_reports: tuple[str, ...] = ("output/manufacturing/native-drc.json",)
    #: Board-relative copies of the saved-vs-refilled area deltas.
    fill_consistency: tuple[str, ...] = ("output/manufacturing/fill-consistency.json",)
    #: Board-relative copies of the refill-stability record.
    refill_stability: tuple[str, ...] = ()
    #: Board-relative copies of the ``kct check`` report on the published PCB.
    check_reports: tuple[str, ...] = ("output/manufacturing/check-report.json",)
    #: Board-specific evidence stage (a key of ``EVIDENCE_STAGES``).
    evidence: str | None = None
    #: Board-relative evidence PCB that must equal the published routed PCB.
    evidence_pcb: str | None = None


BUNDLES: dict[str, BundleConfig] = {
    "07-matchgroup-test": BundleConfig(
        manufacturer="jlcpcb",
        saved_drc_reports=(
            "output/drc_report.json",
            "output/readiness/native-drc.json",
            "output/manufacturing/native-drc.json",
        ),
        fill_consistency=(
            "output/readiness/fill-consistency.json",
            "output/manufacturing/fill-consistency.json",
        ),
        refill_stability=(
            "output/readiness/refill-stability.json",
            "real_design/reviewed-routing/evidence/refill-stability.json",
        ),
        check_reports=(
            "output/readiness/kct-check.json",
            "output/manufacturing/check-report.json",
        ),
        evidence="board07",
        evidence_pcb="output/readiness/checked.kicad_pcb",
    ),
}


# --------------------------------------------------------------------------- helpers
def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return rr.run(cmd, **kwargs)


def _tokens(text: str) -> str:
    """Whitespace-insensitive form of an s-expression (formatting-only edits vanish)."""
    return re.sub(r"\s+", " ", re.sub(r"\s*([()])\s*", r"\1", text)).strip()


def zone_blocks(text: str) -> list[str]:
    return [text[start:end] for start, end in netclasses._top_level_zones(text)]


def without_zones(text: str) -> str:
    """``text`` with every top-level ``(zone ...)`` block cut out."""
    pieces: list[str] = []
    cursor = 0
    for start, end in netclasses._top_level_zones(text):
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def archive_is_current(archive: Path, search: list[Path]) -> bool:
    """True when every member of ``archive`` equals its current file."""
    with zipfile.ZipFile(archive) as existing:
        for name in existing.namelist():
            if name.endswith("/"):
                continue
            for base in search:
                candidate = base / name
                if candidate.is_file():
                    if candidate.read_bytes() != existing.read(name):
                        return False
                    break
            else:
                return False
    return True


def _write_targets(staged: Path, targets: tuple[str, ...], data: bytes) -> None:
    for target in targets:
        path = staged / target
        if not path.is_file():
            raise GateFailure(f"{target}: not part of the committed board; refusing to add it")
        path.write_bytes(data)


def _json_bytes(data: object) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


# --------------------------------------------------------------------------- stage 1
#: ``validation/`` file -> board-relative copies (board 07).
BOARD07_EVIDENCE: dict[str, tuple[str, ...]] = {
    "check.json": (
        "output/readiness/check.json",
        "real_design/reviewed-routing/evidence/check.json",
    ),
    "check.log": ("output/readiness/check.log",),
    "checked.kicad_dru": ("output/readiness/checked.kicad_dru",),
    "checked.kicad_pcb": ("output/readiness/checked.kicad_pcb",),
    "checked.kicad_pro": ("output/readiness/checked.kicad_pro",),
    "checked.kicad_sch": ("output/readiness/checked.kicad_sch",),
    "clock-spacing.json": (
        "output/readiness/clock-spacing.json",
        "real_design/reviewed-routing/evidence/clock-spacing.json",
    ),
    "clock-spacing.log": ("output/readiness/clock-spacing.log",),
    "erc.json": (
        "output/readiness/erc.json",
        "real_design/reviewed-routing/evidence/erc.json",
    ),
    "lvs.json": (
        "output/lvs.json",
        "output/readiness/lvs.json",
        "output/manufacturing/lvs.json",
        "real_design/reviewed-routing/evidence/lvs.json",
    ),
    "native-drc-resolved.json": (
        "output/readiness/native-drc-resolved.json",
        "real_design/reviewed-routing/evidence/native-drc-resolved.json",
    ),
    "native-drc.json": ("real_design/reviewed-routing/evidence/native-drc.json",),
    "validation.json": (
        "output/readiness/validation.json",
        "real_design/reviewed-routing/evidence/validation.json",
    ),
}


def _board07_build(target: Path) -> Path:
    """``build.py`` output in ``target``: replayed if already built, else built fresh."""
    script = REPO_ROOT / "boards/07-matchgroup-test/real_design/build.py"
    if (target / "build.json").is_file():
        log(f"  board 07 build: replaying {target}")
        return target
    log(f"  board 07 build: running real_design/build.py -> {target}")
    proc = run([sys.executable, str(script), str(target)], cwd=REPO_ROOT)
    if proc.returncode != 0:
        raise GateFailure(f"board 07 build.py failed: {proc.stdout[-2000:]}{proc.stderr[-2000:]}")
    return target


def stage_board07_evidence(
    staged: Path, cfg: rr.BoardConfig, build_dir: Path | None = None
) -> list[str]:
    """Publish the routed PCB ``build.py`` validated, plus its evidence.

    ``build_dir`` keeps (or replays) the build; by default it is a temporary
    directory removed afterwards.  Returns the board-relative files rewritten
    (empty when the committed evidence already describes the build's board).
    """
    if build_dir is None:
        with tempfile.TemporaryDirectory(prefix="refresh-bundles-07-build-") as tmp:
            return _publish_board07_evidence(staged, cfg, _board07_build(Path(tmp) / "build"))
    return _publish_board07_evidence(staged, cfg, _board07_build(build_dir))


def _publish_board07_evidence(staged: Path, cfg: rr.BoardConfig, build: Path) -> list[str]:
    validation = build / "validation"
    report = json.loads((validation / "validation.json").read_text())
    if report.get("status") != "pass":
        raise GateFailure(f"board 07 validation is {report.get('status')}: {report.get('errors')}")
    routed = staged / cfg.routed_pcb
    candidate = build / "sdram_demo_routed.kicad_pcb"
    if candidate.read_bytes() != (validation / "checked.kicad_pcb").read_bytes():
        raise GateFailure("build.py published a PCB other than the one it validated")
    for suffix in (".kicad_pro", ".kicad_dru"):
        # The routed sidecars are #6390's; the evidence must be filled and
        # checked under exactly them, never the other way round.
        if candidate.with_suffix(suffix).read_bytes() != routed.with_suffix(suffix).read_bytes():
            raise GateFailure(
                f"build.py checked the board under a different {suffix} than the published "
                "routed sidecar; its evidence would describe other rules"
            )
    current = routed.read_bytes() == candidate.read_bytes() and all(
        (staged / targets[0]).read_bytes() == (validation / name).read_bytes()
        for name, targets in BOARD07_EVIDENCE.items()
        if name.startswith("checked.")
    )
    if current:
        log("  board 07 evidence: committed evidence already describes this build; kept")
        return []
    if without_zones(routed.read_text()) != without_zones(candidate.read_text()):
        log("  board 07 evidence: routed copper changed outside zone fills (reviewed routing)")
    written = [cfg.routed_pcb]
    shutil.copyfile(candidate, routed)
    for name, targets in BOARD07_EVIDENCE.items():
        _write_targets(staged, targets, (validation / name).read_bytes())
        written.extend(targets)
    log(f"  board 07 evidence: published routed PCB and {len(written) - 1} evidence copies")
    return written


EVIDENCE_STAGES: dict[str, Callable[..., list[str]]] = {"board07": stage_board07_evidence}


# --------------------------------------------------------------------------- stage 2
def _fill_context(pcb: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        source = pcb.with_suffix(suffix)
        if source.is_file():
            shutil.copyfile(source, (destination / pcb.name).with_suffix(suffix))
    return destination / pcb.name


def fill_areas(pcb: Path) -> dict[str, float]:
    """Per-copper-layer saved fill area (mm^2), as ``kct readiness`` measures it."""
    from kicad_tools.cli.readiness_cmd import _fill_areas

    return _fill_areas(pcb)


def stage_refill(staged: Path, cfg: rr.BoardConfig) -> dict:
    """Refill the routed PCB's zones under its own sidecars; zone blocks only.

    Returns the refill record (areas, hashes, whether anything changed).
    """
    routed = staged / cfg.routed_pcb
    original = routed.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-refill-") as tmp:
        work = _fill_context(routed, Path(tmp))
        netclasses.refill_zones_in_place(work)
        refilled = work.read_text(encoding="utf-8")
        saved_areas = fill_areas(routed)
        refilled_areas = fill_areas(work)
    if without_zones(refilled) != without_zones(original):
        raise GateFailure("zone refill changed the PCB outside its (zone ...) blocks")
    old_zones, new_zones = zone_blocks(original), zone_blocks(refilled)
    if len(old_zones) != len(new_zones):
        raise GateFailure("zone refill changed the number of zones")
    identical = all(_tokens(a) == _tokens(b) for a, b in zip(old_zones, new_zones, strict=True))
    before = sha256(routed)
    if not identical:
        routed.write_text(refilled, encoding="utf-8")
    log(
        f"  refill: {len(old_zones)} zone(s); saved fills "
        f"{'already current (PCB untouched)' if identical else 'replaced'}"
    )
    layers = sorted(set(saved_areas) | set(refilled_areas))
    return {
        "changed": not identical,
        "before_sha256": before,
        "after_sha256": sha256(routed),
        "fill_identical": identical,
        "deltas_mm2": {
            layer: round(abs(saved_areas.get(layer, 0.0) - refilled_areas.get(layer, 0.0)), 6)
            for layer in layers
        },
    }


# --------------------------------------------------------------------------- stage 3
def _is_zone_clearance(violation: dict) -> bool:
    return violation.get("type") == "clearance" and any(
        item.get("description", "").startswith("Zone ") for item in violation.get("items", [])
    )


def saved_fill_findings(report: dict) -> dict[str, int]:
    """Error, zone-clearance and unconnected counts of a no-refill DRC report."""
    violations = report.get("violations", [])
    return {
        "errors": sum(1 for v in violations if v.get("severity") == "error"),
        "zone_clearance": sum(1 for v in violations if _is_zone_clearance(v)),
        "unconnected": len(report.get("unconnected_items", [])),
    }


def gate_saved_fill_drc(staged: Path, cfg: rr.BoardConfig) -> bytes:
    """Native DRC on the *saved* fills (no ``--refill-zones``): must be clean."""
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-drc-") as tmp:
        work = _fill_context(staged / cfg.routed_pcb, Path(tmp) / "board")
        report = Path(tmp) / "native-drc.json"
        proc = run(
            [
                "kicad-cli",
                "pcb",
                "drc",
                str(work),
                "--severity-all",
                "--format",
                "json",
                "--output",
                str(report),
            ]
        )
        if not report.is_file():
            raise GateFailure(f"kicad-cli DRC (saved fills) did not run: {proc.stderr}")
        data = report.read_bytes()
    counts = saved_fill_findings(json.loads(data))
    log(
        f"  gate kicad-cli DRC (saved fills, no refill): {counts['errors']} error(s), "
        f"{counts['zone_clearance']} zone clearance, {counts['unconnected']} unconnected"
    )
    if any(counts.values()):
        raise GateFailure(
            f"saved zone fills fail native DRC without --refill-zones ({counts}); refusing to "
            "publish a bundle plotted from stale fills"
        )
    return data


# --------------------------------------------------------------------------- stage 4
def _project_archive(cfg: rr.BoardConfig) -> tuple[str, tuple[str, ...]]:
    for archive, search in cfg.archives:
        if archive.endswith("kicad_project.zip"):
            return archive, search
    raise GateFailure("board configuration has no kicad_project.zip archive")


def bundle_is_stale(staged: Path, cfg: rr.BoardConfig) -> bool:
    archive, search = _project_archive(cfg)
    return not archive_is_current(staged / archive, [staged / s for s in search])


def stage_replot(staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig) -> list[str]:
    """Re-plot every artifact drawn from the routed PCB's copper."""
    from kicad_tools.cli.readiness_cmd import _export_assembly_pdf
    from kicad_tools.cli.render_cmd import _render_pcb
    from kicad_tools.cli.runner import find_kicad_cli

    routed = staged / cfg.routed_pcb
    mfg = staged / "output" / "manufacturing"
    written: list[str] = []
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-plot-") as tmp:
        project = Path(tmp) / "output"
        shutil.copytree(
            routed.parent,
            project,
            ignore=shutil.ignore_patterns("manufacturing", "manufacturing.zip", "renders"),
        )
        export = Path(tmp) / "export"
        proc = run(
            rr.kct(
                "export",
                str(project / routed.name),
                "--mfr",
                bundle.manufacturer,
                "--output",
                str(export),
            )
        )
        if proc.returncode != 0:
            raise GateFailure(f"kct export failed: {proc.stdout[-2000:]}{proc.stderr[-2000:]}")
        if (project / routed.name).read_bytes() != routed.read_bytes():
            raise GateFailure("kct export modified the routed PCB it plotted")
        if (export / "cpl_jlcpcb.csv").is_file() and (mfg / "cpl_jlcpcb.csv").is_file():
            if (export / "cpl_jlcpcb.csv").read_bytes() != (mfg / "cpl_jlcpcb.csv").read_bytes():
                raise GateFailure("fresh CPL differs from the committed one: placement changed")
        for pattern in bundle.export_members:
            committed = sorted(p.relative_to(mfg).as_posix() for p in mfg.glob(pattern))
            fresh = sorted(p.relative_to(export).as_posix() for p in export.glob(pattern))
            if committed != fresh:
                raise GateFailure(f"{pattern}: export produced {fresh}, bundle has {committed}")
            for name in fresh:
                source = export / name
                if name.endswith(".zip"):
                    with zipfile.ZipFile(source) as z:
                        members = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
                    rr.write_deterministic_zip(mfg / name, members)
                else:
                    shutil.copyfile(source, mfg / name)
                written.append(f"output/manufacturing/{name}")
        for name, layers in bundle.assembly_pdfs:
            result = _export_assembly_pdf(routed, layers, mfg / name)
            if not result.ok:
                raise GateFailure(f"{name}: {result.detail}")
            written.append(f"output/manufacturing/{name}")
        if bundle.renders is not None:
            renders = Path(tmp) / "renders"
            status = _render_pcb(
                routed,
                renders,
                name=staged.name,
                do_3d=True,
                kicad_cli=find_kicad_cli(),
                quiet=True,
            )
            if status["status"] != "ok":
                raise GateFailure(f"kct render failed: {status['errors']}")
            for path in sorted((staged / bundle.renders).iterdir()):
                fresh_render = renders / path.name
                if not fresh_render.is_file():
                    raise GateFailure(f"kct render produced no {path.name}")
                shutil.copyfile(fresh_render, path)
                written.append(f"{bundle.renders}/{path.name}")
    log(f"  re-plot: {len(written)} artifact(s) from the published routed PCB")
    return written


def stage_check_report(staged: Path, cfg: rr.BoardConfig, bundle: BundleConfig) -> None:
    """Write the bundle's ``kct check`` report (board-relative path, so reproducible)."""
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-check-") as tmp:
        report = Path(tmp) / "check.json"
        run(
            rr.kct(
                "check",
                cfg.routed_pcb,
                "--mfr",
                cfg.tier,
                "--format",
                "json",
                "--output",
                str(report),
            ),
            cwd=staged,
        )
        if not report.is_file():
            raise GateFailure("kct check did not produce a report")
        data = report.read_bytes()
    _write_targets(staged, bundle.check_reports, data)


# --------------------------------------------------------------------------- driver
def refresh_bundle(
    board: Path,
    checked_at: str,
    *,
    reuse_erc: Path | None = None,
    save_erc: Path | None = None,
    build_dir: Path | None = None,
    force_replot: bool = False,
    dry_run: bool = False,
) -> list[str]:
    from kicad_tools.cli.board_readiness import read_readiness

    cfg = rr.BOARDS[board.name]
    bundle = BUNDLES[board.name]
    with tempfile.TemporaryDirectory(prefix="refresh-bundles-") as tmp:
        staged = Path(tmp) / board.name
        shutil.copytree(board, staged, ignore=shutil.ignore_patterns("__pycache__"))
        out = staged / "output"
        mfg = out / "manufacturing"

        if bundle.evidence is not None:
            EVIDENCE_STAGES[bundle.evidence](staged, cfg, build_dir)
        refill = stage_refill(staged, cfg)
        if bundle.evidence_pcb is not None and (
            (staged / bundle.evidence_pcb).read_bytes() != (staged / cfg.routed_pcb).read_bytes()
        ):
            raise GateFailure(
                f"{bundle.evidence_pcb} is not the published routed PCB: the board's evidence "
                "was filled under other rules than its sidecars"
            )
        saved_drc = gate_saved_fill_drc(staged, cfg)

        stale = force_replot or bundle_is_stale(staged, cfg)
        if stale:
            stage_replot(staged, cfg, bundle)
            _write_targets(staged, bundle.saved_drc_reports, saved_drc)
            _write_targets(staged, bundle.fill_consistency, _json_bytes(refill["deltas_mm2"]))
            routed = staged / cfg.routed_pcb
            stability = {
                "before_sha256": refill["before_sha256"],
                "after_sha256": refill["after_sha256"],
                "byte_identical": refill["before_sha256"] == refill["after_sha256"],
                "fill_identical": refill["fill_identical"],
                "native_violations": len(json.loads(saved_drc)["violations"]),
                "native_unconnected_items": len(json.loads(saved_drc)["unconnected_items"]),
                "native_rule_sha256": {
                    suffix: sha256(routed.with_suffix(suffix))
                    for suffix in (".kicad_pro", ".kicad_dru")
                },
                "post_check_rule_sources_identical_to_refill_proof": True,
            }
            if bundle.refill_stability:
                _write_targets(staged, bundle.refill_stability, _json_bytes(stability))
        else:
            log("  re-plot: bundle already binds the current routed project files; kept")

        for job in cfg.erc:
            data = rr.native_erc(staged, job, reuse_erc, save_erc)
            for target in job.targets:
                (staged / target).write_bytes(data)
            log(f"  native ERC {job.schematic}: 0 violations -> {len(job.targets)} report copies")

        for archive, search in cfg.archives:
            changed = rr.rebuild_archive(staged / archive, [staged / s for s in search])
            log(f"  {archive}: rebuilt; members with new content {changed}")

        refresher = rr.load_refresher(staged, cfg.manifest_refresher)
        before = json.loads((mfg / "manifest.json").read_text())
        refresher(mfg)
        if stale:
            # The bundle's check report records the manifest meta-check, so
            # it runs against the re-hashed bundle and is hashed in after.
            stage_check_report(staged, cfg, bundle)
            refresher(mfg)
        after = json.loads((mfg / "manifest.json").read_text())
        if {k: v for k, v in before.items() if k != "files"} != {
            k: v for k, v in after.items() if k != "files"
        }:
            raise GateFailure("manifest refresh changed fields other than 'files'")
        changed = rr.rebuild_archive(out / "manufacturing.zip", [mfg])
        log(f"  output/manufacturing.zip: rebuilt; members with new content {changed}")

        drc_errors = rr.gate_native_drc(staged, cfg)
        lvs_clean, lvs_mismatches = rr.gate_kct_check(staged, cfg)
        routed_pct = rr.gate_net_status(staged, cfg)
        rr.gate_sync(staged, cfg)

        report_path = out / "readiness.json"
        report = json.loads(report_path.read_text())
        measured = {
            "drc_violations": drc_errors,
            "nets_routed_pct": routed_pct,
            "lvs_clean": lvs_clean,
            "lvs_mismatches": lvs_mismatches,
        }
        differs = rr.compare_metrics(report.get("metrics", {}), measured)
        if differs:
            raise GateFailure(
                f"measured metrics differ from readiness.json: "
                f"{ {k: (report['metrics'][k], measured[k]) for k in differs} }"
            )
        if report.get("status") != "ready" or report.get("blockers"):
            raise GateFailure("readiness.json is not a ready report; refusing to re-pin")
        for name in report["inputs"]:
            report["inputs"][name] = sha256(staged / name)
        report["checked_at"] = checked_at
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        verdict = read_readiness(staged)
        if verdict["status"] != "ready":
            raise GateFailure(f"staged report does not validate: {verdict['blockers']}")
        log(f"  read_readiness(staged): ready, metrics {verdict.get('metrics')}")

        published = sorted(
            p.relative_to(staged).as_posix()
            for p in staged.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.relative_to(staged).parts
            and (board / p.relative_to(staged)).is_file()
            and p.read_bytes() != (board / p.relative_to(staged)).read_bytes()
        )
        if not dry_run:
            for name in published:
                shutil.copy2(staged / name, board / name)
        log(f"  {'would publish' if dry_run else 'published'} {len(published)} file(s)")
        return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("boards", nargs="+", type=Path, help="board directories")
    parser.add_argument("--checked-at", required=True, help="ISO timestamp for readiness.json")
    parser.add_argument("--reuse-erc", type=Path, help="replay ERC reports saved by --save-erc")
    parser.add_argument("--save-erc", type=Path, help="keep the native ERC reports here")
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="keep the board's build output here; replay it if the directory already has one",
    )
    parser.add_argument(
        "--force-replot", action="store_true", help="re-plot even if the bundle is current"
    )
    parser.add_argument("--dry-run", action="store_true", help="run every gate, publish nothing")
    args = parser.parse_args(argv)
    status = 0
    for board in args.boards:
        board = board.resolve()
        if board.name not in BUNDLES:
            parser.error(f"{board.name}: no bundle configuration (known: {sorted(BUNDLES)})")
        log(f"== {board.name}")
        try:
            for name in refresh_bundle(
                board,
                args.checked_at,
                reuse_erc=args.reuse_erc,
                save_erc=args.save_erc,
                build_dir=args.build_dir.resolve() if args.build_dir else None,
                force_replot=args.force_replot,
                dry_run=args.dry_run,
            ):
                log(f"    {name}")
        except GateFailure as exc:
            log(f"  REFUSED: {exc} -- nothing published for {board.name}")
            status = 1
    return status


if __name__ == "__main__":
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("KICAD_CLI_DIR", "/Applications/KiCad/KiCad.app/Contents/MacOS")]
        + os.environ.get("PATH", "").split(os.pathsep)
    )
    raise SystemExit(main())
