"""
Manufacturing package generator.

Creates complete manufacturing packages including BOM, CPL, Gerbers,
KiCad project ZIP, and a manifest with SHA256 checksums.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import kicad_tools
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError

from .assembly import AssemblyConfig, AssemblyPackage, AssemblyPackageResult
from .preflight import PreflightChecker, PreflightConfig, PreflightResult

logger = logging.getLogger(__name__)


@dataclass
class ManufacturingConfig(AssemblyConfig):
    """Configuration for manufacturing package generation."""

    # Report settings
    include_report: bool = True

    # KiCad project ZIP settings
    include_project_zip: bool = True
    project_zip_name: str = "kicad_project.zip"

    # Manifest settings
    include_manifest: bool = True
    manifest_name: str = "manifest.json"

    # Pre-flight validation settings
    preflight: PreflightConfig | None = None

    # When True, preflight FAIL results block export (early return, no files).
    # When False (default), preflight failures are recorded as warnings but
    # export proceeds to generate output files.
    strict_preflight: bool = False

    # When True (default), the export refuses to proceed when the BOM
    # contains references with no matching PCB footprint -- the package
    # would be unbuildable because the fab cannot place components that
    # are not on the board.  This gates only the ``bom_pcb_match``
    # preflight check independently of ``strict_preflight``; users who
    # explicitly want to ship a partial package can opt out via
    # ``--allow-unbuildable-bom`` (which sets this to False).
    block_on_unbuildable_bom: bool = True

    # When True (default), flatten the latest vN/ report directory into a
    # ``report/`` subdirectory and remove all vN/ directories from the output.
    # When False, versioned directories are preserved as-is.
    latest_report_only: bool = True

    # When True, preserve intermediate report build files (markdown, figures,
    # JSON data snapshots) in a .build/report/ subdirectory.  Default False
    # means only report.pdf (or report.md) is kept at the package root.
    keep_build_artifacts: bool = False

    # When True (default), generate a README.txt explaining the output files.
    include_readme: bool = True

    # When True (default), emit a sibling ``<board>.kicad_pro`` (and
    # ``.kicad_dru``) next to the source PCB populated from the target
    # manufacturer profile, so ``kicad-cli pcb drc`` loads the fab's
    # relaxed built-in minimums instead of KiCad's stricter defaults.
    emit_drc_constraints: bool = True


@dataclass
class ManufacturingResult:
    """Result of manufacturing package generation."""

    output_dir: Path
    assembly_result: AssemblyPackageResult | None = None
    report_path: Path | None = None
    report_md_path: Path | None = None
    project_zip_path: Path | None = None
    manifest_path: Path | None = None
    readme_path: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    # Sibling DRC-constraint sources (.kicad_pro / .kicad_dru) written next
    # to the source board so kicad-cli loads the manufacturer profile.
    drc_constraint_paths: list[Path] = field(default_factory=list)
    preflight_results: list[PreflightResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Check if generation was successful (no errors)."""
        return len(self.errors) == 0

    @property
    def all_files(self) -> list[Path]:
        """Return list of all generated files."""
        files: list[Path] = []
        if self.assembly_result:
            if self.assembly_result.bom_path:
                files.append(self.assembly_result.bom_path)
            if self.assembly_result.pnp_path:
                files.append(self.assembly_result.pnp_path)
            if self.assembly_result.gerber_path:
                files.append(self.assembly_result.gerber_path)
        if self.report_path:
            files.append(self.report_path)
        if self.report_md_path:
            files.append(self.report_md_path)
        files.extend(self.image_paths)
        if self.project_zip_path:
            files.append(self.project_zip_path)
        if self.readme_path:
            files.append(self.readme_path)
        if self.manifest_path:
            files.append(self.manifest_path)
        return files

    def __str__(self) -> str:
        lines = [f"Manufacturing Package: {self.output_dir}"]
        if self.assembly_result:
            if self.assembly_result.bom_path:
                lines.append(f"  BOM: {self.assembly_result.bom_path.name}")
            if self.assembly_result.pnp_path:
                lines.append(f"  CPL: {self.assembly_result.pnp_path.name}")
            if self.assembly_result.gerber_path:
                lines.append(f"  Gerbers: {self.assembly_result.gerber_path.name}")
        if self.report_path:
            lines.append(f"  Report: {self.report_path.name}")
        if self.image_paths:
            lines.append(f"  Images: {len(self.image_paths)} file(s)")
        if self.project_zip_path:
            lines.append(f"  Project ZIP: {self.project_zip_path.name}")
        if self.readme_path:
            lines.append(f"  README: {self.readme_path.name}")
        if self.manifest_path:
            lines.append(f"  Manifest: {self.manifest_path.name}")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors:
                lines.append(f"    - {err}")
        return "\n".join(lines)


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(manifest_path: str | Path) -> list[str]:
    """Verify a manufacturing bundle's manifest.json against files on disk.

    Recomputes the SHA256 checksum and size of every file listed in the
    manifest and compares them to the recorded values.  Files are looked
    up first in the manifest's own directory; entries that aren't found
    there are searched recursively, because the manifest records bare
    filenames even for artifacts that live in subdirectories (e.g.
    ``gerbers.zip`` under ``gerbers/``).

    The manifest itself is excluded from verification (its hash can't
    contain itself).

    Args:
        manifest_path: Path to a ``manifest.json`` file.

    Returns:
        A list of human-readable mismatch descriptions.  Empty list
        means every listed file exists and matches its recorded
        checksum and size.

    This is the integrity check behind issue #3529: ``kct export`` used
    to write BOM/CPL CSVs with CRLF line endings and hash that content,
    while git normalization stored LF -- so a fresh checkout failed
    ``sha256sum`` verification against the manifest.
    """
    manifest_path = Path(manifest_path)
    bundle_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())

    problems: list[str] = []
    for name, info in manifest.get("files", {}).items():
        if name == manifest_path.name:
            continue
        fpath = bundle_dir / name
        if not fpath.exists():
            # Manifest stores bare filenames; some artifacts live in
            # subdirectories of the bundle (e.g. gerbers/gerbers.zip).
            candidates = sorted(bundle_dir.rglob(name))
            if not candidates:
                problems.append(f"{name}: listed in manifest but not found on disk")
                continue
            fpath = candidates[0]

        actual_sha = _sha256_file(fpath)
        actual_size = fpath.stat().st_size
        expected_sha = info.get("sha256")
        expected_size = info.get("size")
        if expected_sha is not None and actual_sha != expected_sha:
            problems.append(f"{name}: sha256 mismatch (manifest {expected_sha}, disk {actual_sha})")
        if expected_size is not None and actual_size != expected_size:
            problems.append(f"{name}: size mismatch (manifest {expected_size}, disk {actual_size})")

    return problems


def _create_project_zip(
    pcb_path: Path,
    output_dir: Path,
    zip_name: str = "kicad_project.zip",
) -> Path:
    """Create a ZIP containing the KiCad project files.

    Includes only the specific PCB being exported, plus .kicad_sch and
    .kicad_pro files.  Other .kicad_pcb variants (intermediate builds,
    working copies, etc.) and backup files are excluded to keep the
    manufacturing package clean.
    """
    project_dir = pcb_path.parent
    zip_path = output_dir / zip_name

    # Always include the specific PCB being exported
    project_files: list[Path] = []
    if pcb_path.exists():
        project_files.append(pcb_path)

    # Include schematics and project files (not PCBs — only the exported one)
    sch_pro_extensions = {".kicad_sch", ".kicad_pro"}
    project_files.extend(
        f
        for f in project_dir.iterdir()
        if f.is_file() and f.suffix in sch_pro_extensions and "_backup_" not in f.stem
    )

    if not project_files:
        raise FileNotFoundError(f"No KiCad project files found in {project_dir}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(project_files):
            zf.write(f, f.name)

    logger.info(f"Created project ZIP: {zip_path} ({len(project_files)} files)")
    return zip_path


def _build_manifest(
    result: ManufacturingResult,
    pcb_path: Path,
    manufacturer: str,
) -> dict:
    """Build the manifest dictionary with SHA256 checksums."""
    files_info: dict[str, dict] = {}
    for fpath in result.all_files:
        if fpath.exists():
            files_info[fpath.name] = {
                "sha256": _sha256_file(fpath),
                "size": fpath.stat().st_size,
            }

    manifest = {
        "version": "1.0",
        "kicad_tools_version": kicad_tools.__version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manufacturer": manufacturer,
        "files": files_info,
        "board": {
            "name": pcb_path.stem,
            "pcb_file": pcb_path.name,
        },
    }

    # Include preflight results if available
    if result.preflight_results:
        manifest["preflight"] = [pr.to_dict() for pr in result.preflight_results]

    return manifest


class ManufacturingPackage:
    """
    Generate a complete manufacturing package for PCB production.

    Orchestrates BOM, CPL, Gerber, report, KiCad project ZIP, and
    manifest generation into a single output directory.

    Example::

        pkg = ManufacturingPackage(
            pcb_path="board.kicad_pcb",
            manufacturer="jlcpcb",
        )
        result = pkg.export("manufacturing/")
        print(result)
    """

    def __init__(
        self,
        pcb_path: str | Path,
        schematic_path: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        config: ManufacturingConfig | None = None,
    ):
        self.pcb_path = Path(pcb_path)
        self.schematic_path = Path(schematic_path) if schematic_path else None
        self.manufacturer = manufacturer.lower()
        self.config = config or ManufacturingConfig()

    def export(
        self,
        output_dir: str | Path | None = None,
        *,
        dry_run: bool = False,
    ) -> ManufacturingResult:
        """Generate the full manufacturing package.

        Args:
            output_dir: Output directory (overrides config).
            dry_run: If True, report what would be generated without
                     actually writing files.

        Returns:
            ManufacturingResult with paths and any errors.
        """
        out_dir = Path(output_dir) if output_dir else self.config.output_dir
        result = ManufacturingResult(output_dir=out_dir)

        if dry_run:
            return self._dry_run(out_dir, result)

        # Pre-construct the assembly package so we surface fatal input
        # errors (e.g. a schematic-sourced BOM with no discoverable
        # .kicad_sch) *before* preflight runs or we create any output
        # files.  This guarantees that a schematic-missing failure
        # leaves nothing on disk -- avoiding the "5/8 files written,
        # exit code 1, partial package" gap.
        try:
            AssemblyPackage(
                pcb_path=self.pcb_path,
                schematic_path=self.schematic_path,
                manufacturer=self.manufacturer,
                config=self.config,
            )
        except KiCadFileNotFoundError as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return result

        # Step 0a: Emit DRC-constraint sources next to the source board so
        # that kicad-cli pcb drc (run here in preflight, in CI, or by the
        # gallery render pipeline) loads the manufacturer profile's relaxed
        # built-in minimums instead of KiCad's stricter hard-coded defaults.
        # Written before preflight so the constraints exist even when a
        # strict-preflight failure aborts the rest of the pipeline.
        if self.config.emit_drc_constraints:
            self._write_drc_constraints(result)

        # Step 0: Pre-flight validation
        preflight_cfg = self.config.preflight
        if preflight_cfg is None or not preflight_cfg.skip_all:
            preflight_results = self._run_preflight()
            result.preflight_results = preflight_results

            # The bom_pcb_match check returns FAIL when the BOM references
            # parts with no matching PCB footprint -- an unbuildable
            # package.  This is a targeted block that fires even when
            # ``strict_preflight`` is False, because it's the only check
            # whose failure mode is "the fab literally cannot make this".
            unbuildable_bom_failure: PreflightResult | None = None
            if self.config.block_on_unbuildable_bom:
                for pr in preflight_results:
                    if pr.name == "bom_pcb_match" and pr.status == "FAIL":
                        # Only treat as unbuildable if the schematic-only
                        # axis is involved (refs in BOM not on PCB).  PCB
                        # orphans are only a WARN, so we won't get here for
                        # them.
                        unbuildable_bom_failure = pr
                        break

            if PreflightChecker.has_failures(preflight_results):
                # Collect failure messages
                for pr in preflight_results:
                    if pr.status == "FAIL":
                        msg = f"Preflight FAIL [{pr.name}]: {pr.message}"
                        if pr.details:
                            msg += f" ({pr.details})"
                        # bom_pcb_match failures escalate to errors when
                        # block_on_unbuildable_bom is set; other preflight
                        # failures behave per strict_preflight.
                        is_unbuildable = (
                            unbuildable_bom_failure is not None and pr is unbuildable_bom_failure
                        )
                        if self.config.strict_preflight or is_unbuildable:
                            result.errors.append(msg)
                        else:
                            result.warnings.append(msg)

                # Block export on strict mode OR on unbuildable BOM
                if self.config.strict_preflight or unbuildable_bom_failure is not None:
                    return result

        out_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: BOM + CPL + Gerbers via AssemblyPackage.  When this
        # returns False the stage aborted before writing any artefacts
        # (e.g. schematic missing for a schematic-sourced BOM), so we
        # skip the rest of the pipeline to avoid producing a partial
        # package on disk.
        assembly_ok = self._generate_assembly(out_dir, result)
        if not assembly_ok:
            return result

        # Step 2: Report (optional)
        if self.config.include_report:
            self._generate_report(out_dir, result)

        # Step 2.5: Flatten report into report/ when latest_report_only is set
        if self.config.latest_report_only:
            self._flatten_latest_report(out_dir, result)

        # Step 3: KiCad project ZIP (optional)
        if self.config.include_project_zip:
            self._generate_project_zip(out_dir, result)

        # Step 3.5: README.txt (optional)
        if self.config.include_readme:
            self._generate_readme(out_dir, result)

        # Step 4: Manifest (always last -- needs checksums of other files)
        if self.config.include_manifest:
            self._generate_manifest(out_dir, result)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_preflight(self) -> list[PreflightResult]:
        """Run pre-flight validation checks."""
        preflight_cfg = self.config.preflight or PreflightConfig()

        # Pass explicit THT exclusion setting when the user provided a PnP config
        exclude_tht = None
        if self.config.pnp_config is not None:
            exclude_tht = self.config.pnp_config.exclude_tht

        checker = PreflightChecker(
            pcb_path=self.pcb_path,
            schematic_path=self.schematic_path,
            manufacturer=self.manufacturer,
            output_dir=self.config.output_dir,
            config=preflight_cfg,
            exclude_tht=exclude_tht,
            bom_source=self.config.bom_source,
        )
        return checker.run_all()

    def _detect_layer_config(self) -> tuple[int, float]:
        """Determine (copper-layer count, copper weight) for the board.

        The layer count is read from the PCB so the DRC rules selected
        from the manufacturer profile match the board (a 4-layer board
        uses finer via/drill minimums than a 2-layer board).  Copper
        weight is not encoded in the board file, so it defaults to 1oz.

        Returns:
            ``(layers, copper_oz)`` -- falls back to ``(2, 1.0)`` when the
            board cannot be parsed.
        """
        try:
            from ..schema.pcb import PCB

            pcb = PCB.load(str(self.pcb_path))
            layers = len(pcb.copper_layers)
            if layers < 1:
                layers = 2
        except Exception as e:
            logger.debug("Could not detect layer count from %s: %s", self.pcb_path, e)
            layers = 2
        return layers, 1.0

    def _write_drc_constraints(self, result: ManufacturingResult) -> None:
        """Emit sibling .kicad_pro / .kicad_dru from the manufacturer profile.

        Populates the project file's ``board.design_settings.rules`` block
        (and a companion .kicad_dru) from the target manufacturer profile
        so that ``kicad-cli pcb drc`` checks the board against the fab's
        actual capabilities rather than KiCad's stricter built-in defaults.
        """
        try:
            from ..manufacturers import get_profile, write_drc_constraints

            profile = get_profile(self.manufacturer)
        except Exception as e:
            logger.warning(
                "Skipping DRC-constraint emission (profile '%s' unavailable): %s",
                self.manufacturer,
                e,
            )
            result.warnings.append(
                f"Could not emit DRC constraints for manufacturer '{self.manufacturer}': {e}"
            )
            return

        layers, copper_oz = self._detect_layer_config()
        rules = profile.get_design_rules(layers=layers, copper_oz=copper_oz)

        try:
            written = write_drc_constraints(
                self.pcb_path,
                rules,
                manufacturer_id=profile.id,
                layers=layers,
                copper_oz=copper_oz,
            )
        except OSError as e:
            logger.warning("Could not write DRC-constraint files: %s", e)
            result.warnings.append(f"Could not write DRC constraints: {e}")
            return

        result.drc_constraint_paths = written

    def _dry_run(self, out_dir: Path, result: ManufacturingResult) -> ManufacturingResult:
        """Populate result with what *would* be generated."""
        bom_name = self.config.bom_filename.format(manufacturer=self.manufacturer)
        pnp_name = self.config.pnp_filename.format(manufacturer=self.manufacturer)

        result.assembly_result = AssemblyPackageResult(
            output_dir=out_dir,
            bom_path=out_dir / bom_name if self.config.include_bom else None,
            pnp_path=out_dir / pnp_name if self.config.include_pnp else None,
            gerber_path=(
                out_dir / self.config.gerbers_subdir / "gerbers.zip"
                if self.config.include_gerbers
                else None
            ),
        )
        if self.config.include_report:
            try:
                from ..report.renderers import pdf_renderer_available

                if pdf_renderer_available() is not None:
                    result.report_path = out_dir / "report.pdf"
                else:
                    result.report_path = out_dir / "report.md"
            except ImportError:
                result.report_path = out_dir / "report.md"
        if self.config.include_project_zip:
            result.project_zip_path = out_dir / self.config.project_zip_name
        if self.config.include_manifest:
            result.manifest_path = out_dir / self.config.manifest_name
        return result

    def _generate_assembly(self, out_dir: Path, result: ManufacturingResult) -> bool:
        """Run BOM + CPL + Gerber generation.

        Returns:
            ``True`` when the assembly stage was attempted successfully
            (whether or not its individual artefacts succeeded), ``False``
            when the stage aborted before producing any files -- in which
            case the caller should skip subsequent stages (Report, Project
            ZIP, Manifest) so we don't leave a partial package on disk.
        """
        try:
            assembly = AssemblyPackage(
                pcb_path=self.pcb_path,
                schematic_path=self.schematic_path,
                manufacturer=self.manufacturer,
                config=self.config,  # ManufacturingConfig extends AssemblyConfig
            )
        except KiCadFileNotFoundError as e:
            # Construction failed (e.g. schematic missing for a
            # schematic-sourced BOM).  No artefacts have been written.
            # Bail out hard so we don't produce a partial package.
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return False
        except Exception as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return False

        try:
            result.assembly_result = assembly.export(out_dir)
            if result.assembly_result.errors:
                result.errors.extend(result.assembly_result.errors)
        except Exception as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")
            return True  # files may already have been written

        return True

    def _generate_report(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Generate a Markdown design report."""
        try:
            from ..report.collector import ReportDataCollector
            from ..report.generator import ReportGenerator
            from ..report.models import ReportData
        except ImportError:
            logger.warning(
                "Report generation skipped: required dependency not installed (e.g. jinja2)"
            )
            return

        try:
            # Pre-determine the version directory so collected data and report
            # land in the same vN/ directory.
            version_dir = ReportGenerator.next_version_dir(out_dir)
            data_dir = version_dir / "data"

            # Collect design data snapshots
            collector = ReportDataCollector(
                pcb_path=self.pcb_path,
                manufacturer=self.manufacturer,
            )
            collector.collect_all(data_dir)

            # Load collected JSON snapshots into ReportData kwargs
            data_kwargs = self._load_report_data_dir(data_dir)

            # Generate figures (PCB renders + schematic screenshots)
            figures_data = self._generate_figures(version_dir, result)
            if figures_data:
                data_kwargs.update(figures_data)

            # Hand-solder (THT) set excluded from the SMT CPL (issue #3539):
            # surface the exact refs the assembler must hand-place.
            tht_components = self._tht_component_groups(result)
            if tht_components:
                data_kwargs["tht_components"] = tht_components

            project_name = self.pcb_path.stem
            data = ReportData(
                project_name=project_name,
                revision=data_kwargs.pop("revision", "1"),
                date=data_kwargs.pop(
                    "date",
                    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                ),
                manufacturer=self.manufacturer,
                **data_kwargs,
            )

            generator = ReportGenerator()
            report_path = generator.generate(data, out_dir, version_dir=version_dir)
            result.report_path = report_path
            logger.info(f"Generated report: {result.report_path}")

            # Attempt to render Markdown to HTML then PDF
            self._render_report_pdf(report_path, version_dir, result)
        except Exception as e:
            result.errors.append(f"Report generation failed: {e}")
            logger.error(f"Report generation failed: {e}")

    @staticmethod
    def _warn_figures_skipped(result: ManufacturingResult, reason: str) -> None:
        """Record a *loud* warning that the bundle will ship without figures.

        Figure generation degrading silently is how committed bundles ended
        up without per-layer renders / assembly views (issue #3583): the
        skip reason only went to the module logger, which `kct export`
        does not surface.  Recording it on ``result.warnings`` makes the
        CLI print it and JSON output include it.
        """
        msg = (
            f"Report figures skipped: {reason}. "
            "The bundle will NOT include images/ (per-copper-layer renders, "
            "front/back assembly views, schematic sheets). "
            "Figure generation needs kicad-cli (KiCad 8+) and cairosvg "
            "(pip install 'kicad-tools[screenshot]', or `uv sync` in a dev checkout)."
        )
        result.warnings.append(msg)
        logger.warning(msg)

    def _generate_figures(self, version_dir: Path, result: ManufacturingResult) -> dict | None:
        """Generate PCB and schematic figures for the report.

        Returns a dict with ``pcb_figures`` and/or ``schematic_sheets``
        keys suitable for merging into ReportData kwargs, or ``None``
        if figure generation is unavailable or fails.

        Every skip path records a warning on *result* (issue #3583) so
        missing figures are visible in the export output instead of the
        bundle silently shipping without its images/ directory.
        """
        try:
            from ..report.figures import ReportFigureGenerator
            from ..report.utils import find_schematic
        except ImportError as exc:
            self._warn_figures_skipped(result, f"dependency not available ({exc})")
            return None

        sch_path = self.schematic_path
        if sch_path is None:
            sch_path = find_schematic(self.pcb_path)
        if sch_path is None:
            self._warn_figures_skipped(
                result,
                f"no schematic found next to {self.pcb_path.name} (use --sch to specify one)",
            )
            return None

        figures_dir = version_dir / "figures"
        try:
            fig_gen = ReportFigureGenerator()
            entries = fig_gen.generate_all(self.pcb_path, sch_path, figures_dir)
        except (RuntimeError, OSError) as exc:
            self._warn_figures_skipped(result, str(exc))
            return None

        if not entries:
            self._warn_figures_skipped(result, "figure generator produced no images")
            return None

        # Convert FigureEntry list to ReportData-compatible dicts
        type_to_key = {
            "pcb_front": "front",
            "pcb_back": "back",
            "pcb_copper": "copper",
            "assembly": "assembly",
        }
        result: dict = {}

        pcb_figs: dict[str, str] = {}
        for entry in entries:
            key = type_to_key.get(entry.figure_type)
            if key is not None:
                pcb_figs[key] = f"figures/{entry.filename}"
        if pcb_figs:
            result["pcb_figures"] = pcb_figs

        # Per-copper-layer renders (issue #3497): one image per copper
        # layer, in stackup order.
        layer_figs = [
            {
                "name": entry.caption.removeprefix("Copper Layer "),
                "figure_path": f"figures/{entry.filename}",
            }
            for entry in entries
            if entry.figure_type == "pcb_layer"
        ]
        if layer_figs:
            result["pcb_layer_figures"] = layer_figs

        sch_sheets = [
            {"name": entry.caption, "figure_path": f"figures/{entry.filename}"}
            for entry in entries
            if entry.figure_type == "schematic"
        ]
        if sch_sheets:
            result["schematic_sheets"] = sch_sheets

        logger.info(
            f"Generated {len(entries)} figure(s): {len(pcb_figs)} PCB, "
            f"{len(layer_figs)} per-layer, {len(sch_sheets)} schematic"
        )
        return result or None

    @staticmethod
    def _render_report_pdf(
        report_path: Path,
        version_dir: Path,
        result: ManufacturingResult,
    ) -> None:
        """Render the Markdown report to PDF.

        Tries weasyprint (HTML→PDF) first, then pandoc+TeX as fallback.
        Degrades gracefully: if neither is available, a warning is logged
        and only the ``.md`` file remains.
        """
        try:
            from ..report.renderers import pdf_renderer_available
        except (ImportError, OSError):
            # OSError can surface here on hosts where the renderers module
            # eagerly probes a native library (libgobject/pango/cairo) at
            # import time. Treat it the same as a missing package: degrade
            # to Markdown-only and keep the manufacturing bundle successful.
            logger.warning(
                "PDF report rendering skipped: install 'kicad-tools[report]' for PDF output"
            )
            return

        renderer = pdf_renderer_available()
        if renderer is None:
            logger.warning(
                "PDF report rendering skipped: install weasyprint or pandoc+TeX for PDF output"
            )
            return

        pdf_path = report_path.with_suffix(".pdf")

        try:
            if renderer == "weasyprint":
                from ..report.renderers import render_html, render_pdf

                md_content = report_path.read_text(encoding="utf-8")
                figures_dir = version_dir / "figures"
                html_content = render_html(
                    md_content,
                    figures_dir=figures_dir if figures_dir.is_dir() else None,
                )
                render_pdf(html_content, pdf_path)
            else:
                from ..report.renderers import render_pdf_pandoc

                render_pdf_pandoc(report_path, pdf_path)

            result.report_path = pdf_path
            logger.info(f"Generated PDF report via {renderer}: {pdf_path}")
        except Exception as exc:
            logger.warning(f"PDF report rendering via {renderer} failed: {exc}")

    @staticmethod
    def _load_report_data_dir(data_dir: Path) -> dict:
        """Load JSON snapshot files from *data_dir* into ReportData kwargs.

        Mirrors the logic in ``cli/report_cmd.py:_load_data_dir`` but is
        self-contained so the manufacturing exporter has no dependency on the
        CLI layer.
        """
        import json as _json

        mappings = {
            "board_summary.json": "board_stats",
            "bom.json": "bom_groups",
            "drc_summary.json": "drc",
            "erc_summary.json": "erc",
            "audit.json": "audit",
            "net_status.json": "net_status",
            "cost.json": "cost",
            "analog_components.json": "analog_components",
            "narrative.json": "_narrative",
            "stackup.json": "stackup",
            "off_board.json": "off_board",
        }

        result: dict = {}
        for filename, field_name in mappings.items():
            json_path = data_dir / filename
            if json_path.exists():
                with open(json_path, encoding="utf-8") as f:
                    raw = _json.load(f)
                # Unwrap the envelope written by ReportDataCollector
                data = raw.get("data") if isinstance(raw, dict) else raw
                if data is None:
                    continue
                result[field_name] = data

        # BOM: collector nests group list under ``groups`` key;
        # ReportData.bom_groups expects a plain list[dict].
        if "bom_groups" in result and isinstance(result["bom_groups"], dict):
            result["bom_groups"] = result["bom_groups"].get("groups", [])

        # Analog components: collector nests list under ``components``;
        # ReportData.analog_components expects a plain list[dict].
        if "analog_components" in result and isinstance(result["analog_components"], dict):
            result["analog_components"] = result["analog_components"].get("components", [])

        # Narrative: the collector writes a single dict with sub-keys;
        # unpack into individual ReportData fields.
        if "_narrative" in result and isinstance(result["_narrative"], dict):
            narrative = result.pop("_narrative")
            for key in (
                "design_narrative",
                "functional_blocks",
                "interfaces",
                "power_architecture",
                "assembly_notes",
            ):
                val = narrative.get(key)
                if val is not None:
                    result[key] = val
        else:
            result.pop("_narrative", None)

        return result

    def _flatten_latest_report(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Promote the latest report to the package root and clean up.

        When ``config.latest_report_only`` is ``True``, this post-processing
        step copies the latest ``vN/`` contents into a temporary staging area,
        promotes ``report.pdf`` (or ``report.md`` if PDF is unavailable) to
        the package root, and removes all ``vN/`` directories.

        Report figures (per-layer renders, assembly view, schematic
        sheets) are promoted to an ``images/`` subdirectory so the
        package ships the visuals alongside ``report.md`` (issue #3497),
        and the markdown's relative ``figures/`` references are rewritten
        to ``images/`` so they resolve inside the package.

        If ``config.keep_build_artifacts`` is ``True``, intermediate files
        (markdown source, figures, data, metadata) are preserved in a
        ``.build/report/`` subdirectory.  Otherwise they are discarded.
        """
        try:
            from ..report.generator import ReportGenerator
        except ImportError:
            return

        latest = ReportGenerator.latest_version_dir(out_dir)
        if latest is None:
            # No versioned directories (e.g. --no-report was also set)
            return

        # Stage the latest version contents in a temporary directory
        report_staging = out_dir / "report"
        if report_staging.exists():
            shutil.rmtree(report_staging)
        shutil.copytree(latest, report_staging)

        # Remove all vN/ directories from the output
        for child in list(out_dir.iterdir()):
            if child.is_dir() and re.fullmatch(r"v\d+", child.name):
                shutil.rmtree(child)

        # Promote the report file (PDF preferred, MD fallback) to package root
        staged_pdf = report_staging / "report.pdf"
        staged_md = report_staging / "report.md"

        if staged_pdf.exists():
            promoted = out_dir / "report.pdf"
            shutil.copy2(staged_pdf, promoted)
            result.report_path = promoted
            # Also preserve the markdown source alongside the PDF
            if staged_md.exists():
                promoted_md = out_dir / "report.md"
                shutil.copy2(staged_md, promoted_md)
                result.report_md_path = promoted_md
        elif staged_md.exists():
            promoted = out_dir / "report.md"
            shutil.copy2(staged_md, promoted)
            result.report_path = promoted

        # Promote report figures to images/ at the package root so the
        # bundle ships per-layer + assembly renders (issue #3497).
        self._promote_report_images(out_dir, report_staging, result)

        # Handle build artifacts
        if self.config.keep_build_artifacts:
            build_dir = out_dir / ".build" / "report"
            if build_dir.exists():
                shutil.rmtree(build_dir)
            build_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(report_staging), str(build_dir))
            logger.info(f"Preserved build artifacts in {build_dir}")
        else:
            shutil.rmtree(report_staging)

        logger.info(f"Promoted report to {result.report_path}")

    @staticmethod
    def _promote_report_images(
        out_dir: Path,
        report_staging: Path,
        result: ManufacturingResult,
    ) -> None:
        """Copy staged report figures into ``<out_dir>/images/``.

        The report generator writes figures (per-copper-layer renders,
        front/back/assembly views, schematic sheets) into the versioned
        report's ``figures/`` subdirectory.  This step promotes those PNGs
        to a package-root ``images/`` directory, rewrites the promoted
        ``report.md``'s relative ``figures/`` image references to
        ``images/``, and records the image paths on *result* so the
        manifest includes their checksums (issue #3497).

        When the staged report has no figures (e.g. figure generation
        was skipped because kicad-cli/cairosvg are missing), any
        pre-existing ``images/`` directory from an earlier export is
        REMOVED rather than left behind: stale renders that no longer
        match the gerbers are worse than absent ones, and silently
        keeping them is how the softstart bundle shipped pre-repair
        visuals (issue #3583).
        """
        images_dir = out_dir / "images"
        staged_figures = report_staging / "figures"
        pngs = (
            sorted(p for p in staged_figures.iterdir() if p.suffix.lower() == ".png")
            if staged_figures.is_dir()
            else []
        )
        if not pngs:
            if images_dir.is_dir():
                shutil.rmtree(images_dir)
                msg = (
                    "Removed stale images/ from a previous export: this run "
                    "generated no report figures, and stale renders must not "
                    "ship alongside regenerated gerbers"
                )
                result.warnings.append(msg)
                logger.warning(msg)
            return

        if images_dir.exists():
            shutil.rmtree(images_dir)
        images_dir.mkdir(parents=True)

        for png in pngs:
            dest = images_dir / png.name
            shutil.copy2(png, dest)
            result.image_paths.append(dest)

        # Rewrite relative figure references in the promoted markdown so
        # they resolve against the package layout (figures/ -> images/).
        promoted_md = out_dir / "report.md"
        if promoted_md.exists():
            text = promoted_md.read_text(encoding="utf-8")
            rewritten = text.replace("](figures/", "](images/")
            if rewritten != text:
                promoted_md.write_text(rewritten, encoding="utf-8")

        logger.info(f"Promoted {len(pngs)} report image(s) to {images_dir}")

    @staticmethod
    def _tht_component_groups(result: ManufacturingResult) -> list[dict]:
        """Group the CPL's excluded THT components for the report.

        Returns BOM-style rows ``[{value, footprint, qty, refs}]`` built
        from ``assembly_result.tht_excluded`` (issue #3539), or an empty
        list when no THT parts were excluded (SMD-only board, or
        ``exclude_tht=False``).
        """
        if result.assembly_result is None or not result.assembly_result.tht_excluded:
            return []

        from .pnp import group_tht_exclusions

        return group_tht_exclusions(result.assembly_result.tht_excluded)

    def _generate_readme(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Generate a README.txt describing the manufacturing package contents."""
        lines = [
            f"Manufacturing Package for {self.pcb_path.stem}",
            "=" * 50,
            "",
            f"Manufacturer: {self.manufacturer}",
            f"Generated by: kicad-tools {kicad_tools.__version__}",
            f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "Contents:",
            "---------",
        ]

        file_descriptions = {
            "bom": ("BOM (Bill of Materials)", "Component list for PCB assembly ordering"),
            "pnp": (
                "CPL (Component Placement List)",
                "Pick-and-place coordinates for SMT assembly",
            ),
            "gerber": (
                "Gerber files",
                "PCB fabrication data (copper layers, silkscreen, solder mask, drill)",
            ),
            "report": ("Design report", "Manufacturing readiness report with DRC/ERC results"),
            "images": (
                "Report images",
                "Board renders: per-copper-layer, front/back, assembly view, schematic sheets",
            ),
            "project_zip": (
                "KiCad project archive",
                "Source KiCad project files (.kicad_pcb, .kicad_sch, .kicad_pro)",
            ),
            "manifest": ("Manifest", "SHA256 checksums for all output files"),
        }

        if result.assembly_result:
            if result.assembly_result.bom_path:
                desc = file_descriptions["bom"]
                lines.append(f"  {result.assembly_result.bom_path.name}")
                lines.append(f"    {desc[0]}: {desc[1]}")
                lines.append("")
            if result.assembly_result.pnp_path:
                desc = file_descriptions["pnp"]
                lines.append(f"  {result.assembly_result.pnp_path.name}")
                lines.append(f"    {desc[0]}: {desc[1]}")
                tht_excluded = result.assembly_result.tht_excluded
                if tht_excluded:
                    refs = ", ".join(p.reference for p in tht_excluded)
                    lines.append(
                        f"    Note: {len(tht_excluded)} through-hole component(s) are"
                        " excluded from this file and must be hand-soldered"
                        f" after SMT assembly: {refs}"
                    )
                lines.append("")
            if result.assembly_result.gerber_path:
                desc = file_descriptions["gerber"]
                lines.append(f"  {result.assembly_result.gerber_path.name}")
                lines.append(f"    {desc[0]}: {desc[1]}")
                lines.append("")

        if result.report_path:
            desc = file_descriptions["report"]
            lines.append(f"  {result.report_path.name}")
            lines.append(f"    {desc[0]}: {desc[1]}")
            lines.append("")

        if result.image_paths:
            desc = file_descriptions["images"]
            lines.append(f"  images/ ({len(result.image_paths)} files)")
            lines.append(f"    {desc[0]}: {desc[1]}")
            lines.append("")

        if result.project_zip_path:
            desc = file_descriptions["project_zip"]
            lines.append(f"  {result.project_zip_path.name}")
            lines.append(f"    {desc[0]}: {desc[1]}")
            lines.append("")

        desc = file_descriptions["manifest"]
        lines.append("  manifest.json")
        lines.append(f"    {desc[0]}: {desc[1]}")
        lines.append("")

        readme_path = out_dir / "README.txt"
        readme_path.write_text("\n".join(lines), encoding="utf-8")
        result.readme_path = readme_path
        logger.info(f"Generated README: {readme_path}")

    def _generate_project_zip(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Create ZIP of KiCad project files."""
        try:
            result.project_zip_path = _create_project_zip(
                self.pcb_path, out_dir, self.config.project_zip_name
            )
        except Exception as e:
            result.errors.append(f"Project ZIP creation failed: {e}")
            logger.error(f"Project ZIP creation failed: {e}")

    def _generate_manifest(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Write manifest.json with SHA256 checksums."""
        try:
            manifest = _build_manifest(result, self.pcb_path, self.manufacturer)
            manifest_path = out_dir / self.config.manifest_name
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            result.manifest_path = manifest_path
            logger.info(f"Generated manifest: {manifest_path}")
        except Exception as e:
            result.errors.append(f"Manifest generation failed: {e}")
            logger.error(f"Manifest generation failed: {e}")
