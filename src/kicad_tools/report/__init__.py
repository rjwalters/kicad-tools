"""Report generation and rendering for KiCad design reports.

This package provides tools for generating professional design reports
from KiCad project data, with support for Markdown, HTML, and PDF output formats.
Includes figure generation (PCB renders, schematic screenshots) and
structured manifests for design review documents.
"""

from __future__ import annotations

from kicad_tools.report.figures import FigureEntry, ReportFigureGenerator
from kicad_tools.report.renderers import render_html, render_pdf

__all__ = ["FigureEntry", "ReportFigureGenerator", "render_html", "render_pdf"]
