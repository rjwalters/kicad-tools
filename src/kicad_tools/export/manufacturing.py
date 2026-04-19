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

    # When True, flatten the latest vN/ report directory into a ``report/``
    # subdirectory and remove all vN/ directories from the output.
    # When False (default), versioned directories are preserved as-is.
    latest_report_only: bool = False


@dataclass
class ManufacturingResult:
    """Result of manufacturing package generation."""

    output_dir: Path
    assembly_result: AssemblyPackageResult | None = None
    report_path: Path | None = None
    project_zip_path: Path | None = None
    manifest_path: Path | None = None
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
        if self.project_zip_path:
            files.append(self.project_zip_path)
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
        if self.project_zip_path:
            lines.append(f"  Project ZIP: {self.project_zip_path.name}")
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


def _create_project_zip(
    pcb_path: Path,
    output_dir: Path,
    zip_name: str = "kicad_project.zip",
) -> Path:
    """Create a ZIP containing the KiCad project files.

    Includes .kicad_pcb, .kicad_sch, and .kicad_pro files found in
    the same directory as the PCB file.
    """
    project_dir = pcb_path.parent
    zip_path = output_dir / zip_name

    extensions = {".kicad_pcb", ".kicad_sch", ".kicad_pro"}
    project_files = [f for f in project_dir.iterdir() if f.is_file() and f.suffix in extensions]

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

        # Step 0: Pre-flight validation
        preflight_cfg = self.config.preflight
        if preflight_cfg is None or not preflight_cfg.skip_all:
            preflight_results = self._run_preflight()
            result.preflight_results = preflight_results

            if PreflightChecker.has_failures(preflight_results):
                # Collect failure messages
                for pr in preflight_results:
                    if pr.status == "FAIL":
                        msg = f"Preflight FAIL [{pr.name}]: {pr.message}"
                        if pr.details:
                            msg += f" ({pr.details})"
                        if self.config.strict_preflight:
                            result.errors.append(msg)
                        else:
                            result.warnings.append(msg)

                # In strict mode, block export on preflight failures
                if self.config.strict_preflight:
                    return result

        out_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: BOM + CPL + Gerbers via AssemblyPackage
        self._generate_assembly(out_dir, result)

        # Step 2: Report (optional)
        if self.config.include_report:
            self._generate_report(out_dir, result)

        # Step 2.5: Flatten report into report/ when latest_report_only is set
        if self.config.latest_report_only:
            self._flatten_latest_report(out_dir, result)

        # Step 3: KiCad project ZIP (optional)
        if self.config.include_project_zip:
            self._generate_project_zip(out_dir, result)

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
                from ..report.renderers import _weasyprint_available

                if _weasyprint_available():
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

    def _generate_assembly(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Run BOM + CPL + Gerber generation."""
        try:
            assembly = AssemblyPackage(
                pcb_path=self.pcb_path,
                schematic_path=self.schematic_path,
                manufacturer=self.manufacturer,
                config=self.config,  # ManufacturingConfig extends AssemblyConfig
            )
            result.assembly_result = assembly.export(out_dir)
            if result.assembly_result.errors:
                result.errors.extend(result.assembly_result.errors)
        except Exception as e:
            result.errors.append(f"Assembly generation failed: {e}")
            logger.error(f"Assembly generation failed: {e}")

    def _generate_report(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Generate a Markdown design report."""
        try:
            from ..report.collector import ReportDataCollector
            from ..report.generator import ReportGenerator
            from ..report.models import ReportData
        except ImportError:
            logger.warning(
                "Report generation skipped: required dependency not installed "
                "(e.g. jinja2)"
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
    def _render_report_pdf(
        report_path: Path,
        version_dir: Path,
        result: ManufacturingResult,
    ) -> None:
        """Render the Markdown report to PDF via HTML.

        Degrades gracefully: if ``weasyprint`` or ``markdown`` are not
        installed, a warning is logged and only the ``.md`` file remains.
        """
        try:
            from ..report.renderers import render_html, render_pdf
        except ImportError:
            logger.warning(
                "PDF report rendering skipped: install 'kicad-tools[report]' "
                "for PDF output"
            )
            return

        try:
            md_content = report_path.read_text(encoding="utf-8")
            figures_dir = version_dir / "figures"
            html_content = render_html(
                md_content,
                figures_dir=figures_dir if figures_dir.is_dir() else None,
            )
            pdf_path = report_path.with_suffix(".pdf")
            render_pdf(html_content, pdf_path)
            result.report_path = pdf_path
            logger.info(f"Generated PDF report: {pdf_path}")
        except ImportError:
            logger.warning(
                "PDF report rendering skipped: weasyprint or markdown not "
                "installed (hint: pip install 'kicad-tools[report]')"
            )
        except Exception as exc:
            logger.warning(f"PDF report rendering failed: {exc}")

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

        return result

    def _flatten_latest_report(self, out_dir: Path, result: ManufacturingResult) -> None:
        """Copy the latest ``vN/`` contents into ``report/`` and remove version dirs.

        When ``config.latest_report_only`` is ``True``, this post-processing
        step replaces versioned directories with a single flat ``report/``
        subdirectory containing only the latest version's contents.
        """
        try:
            from ..report.generator import ReportGenerator
        except ImportError:
            return

        latest = ReportGenerator.latest_version_dir(out_dir)
        if latest is None:
            # No versioned directories (e.g. --no-report was also set)
            return

        report_dest = out_dir / "report"
        if report_dest.exists():
            shutil.rmtree(report_dest)
        shutil.copytree(latest, report_dest)

        # Remove all vN/ directories from the output
        for child in list(out_dir.iterdir()):
            if child.is_dir() and re.fullmatch(r"v\d+", child.name):
                shutil.rmtree(child)

        # Update result.report_path to point to the flattened location,
        # preferring PDF over MD when both exist.
        flat_pdf = report_dest / "report.pdf"
        flat_report = report_dest / "report.md"
        if flat_pdf.exists():
            result.report_path = flat_pdf
        elif flat_report.exists():
            result.report_path = flat_report

        logger.info(f"Flattened latest report into {report_dest}")

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
