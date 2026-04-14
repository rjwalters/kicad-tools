"""Jinja2-based Markdown report generator for KiCad design reports."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from kicad_tools import __version__

from .models import ReportData

__all__ = ["ReportGenerator"]

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class ReportGenerator:
    """Render a design report from a :class:`ReportData` instance.

    Parameters
    ----------
    template_path:
        Path to a custom Jinja2 template file.  When *None* the bundled
        ``design_report.md.j2`` template is used.
    """

    def __init__(self, template_path: Path | None = None) -> None:
        if template_path is not None:
            template_dir = template_path.parent
            template_name = template_path.name
        else:
            template_dir = _TEMPLATES_DIR
            template_name = "design_report.md.j2"

        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template_name = template_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        data: ReportData,
        output_dir: Path,
        version_dir: Path | None = None,
    ) -> Path:
        """Render the template and write ``report.md`` into a versioned sub-directory.

        Auto-versioning
        ~~~~~~~~~~~~~~~
        * When *version_dir* is ``None`` (default), scans *output_dir*
          for existing ``v<N>/`` directories and creates the next one.
        * When *version_dir* is provided, uses that directory directly
          (useful when figures have already been written there).
        * If the chosen directory already contains ``report.md``,
          raises :class:`FileExistsError` (immutability guard).

        Parameters
        ----------
        data:
            The report data to render.
        output_dir:
            Parent directory under which versioned sub-directories live.
        version_dir:
            When provided, use this directory instead of computing the next
            version automatically.  This avoids a race when the caller has
            already written data (e.g. collected snapshots) into a specific
            version directory.

        Returns the path to the written ``report.md``.
        """
        if version_dir is None:
            version_dir = self.next_version_dir(output_dir)
        version_dir.mkdir(parents=True, exist_ok=True)

        report_path = version_dir / "report.md"
        if report_path.exists():
            raise FileExistsError(
                f"Report already exists at {report_path}. "
                "Existing versions must not be overwritten."
            )

        rendered = self._render(data)
        report_path.write_text(rendered, encoding="utf-8")

        self._write_metadata(data, version_dir, rendered)

        return report_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _render(self, data: ReportData) -> str:
        """Build the template context and render to a string."""
        template = self._env.get_template(self._template_name)

        context = {
            "project_name": data.project_name,
            "revision": data.revision,
            "date": data.date,
            "manufacturer": data.manufacturer,
            "board_stats": data.board_stats,
            "bom_groups": data.bom_groups,
            "drc": data.drc,
            "audit": data.audit,
            "net_status": data.net_status,
            "cost": data.cost,
            "schematic_sheets": data.schematic_sheets,
            "pcb_figures": data.pcb_figures,
            "notes": data.notes,
            "tool_version": data.tool_version or __version__,
            "git_hash": data.git_hash,
        }
        # Forward any extra context variables
        context.update(data._extra)

        return template.render(**context)

    @staticmethod
    def next_version_dir(output_dir: Path) -> Path:
        """Determine the next ``vN`` sub-directory under *output_dir*."""
        output_dir = Path(output_dir)
        existing = []
        if output_dir.exists():
            for child in output_dir.iterdir():
                if child.is_dir():
                    match = re.fullmatch(r"v(\d+)", child.name)
                    if match:
                        existing.append(int(match.group(1)))
        next_version = max(existing, default=0) + 1
        return output_dir / f"v{next_version}"

    def _write_metadata(self, data: ReportData, version_dir: Path, rendered: str) -> None:
        """Write ``metadata.json`` alongside the report."""
        template_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()

        metadata = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kicad_tools_version": data.tool_version or __version__,
            "git_hash": data.git_hash,
            "template_sha256": template_sha256,
        }

        metadata_path = version_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
