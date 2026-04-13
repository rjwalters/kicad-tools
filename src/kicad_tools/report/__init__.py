"""Report generation and rendering for KiCad design reports.

This package provides tools for generating professional design reports
from KiCad project data, with support for Markdown, HTML, and PDF output formats.
Includes Jinja2-based Markdown generation, figure generation (PCB renders,
schematic screenshots), and structured manifests for design review documents.
Also provides data collection for report generation, gathering
board summary, DRC, BOM, audit, net connectivity, and analysis results
into JSON snapshots.

Jinja2-based report generation requires the ``report`` extra::

    pip install kicad-tools[report]
"""

from __future__ import annotations

from kicad_tools.report.collector import ReportDataCollector
from kicad_tools.report.figures import FigureEntry, ReportFigureGenerator
from kicad_tools.report.renderers import render_html, render_pdf

__all__ = [
    "FigureEntry",
    "ReportDataCollector",
    "ReportFigureGenerator",
    "render_html",
    "render_pdf",
]

try:
    import jinja2 as _jinja2  # noqa: F401

    from .generator import ReportGenerator
    from .models import ReportData

    __all__ += ["ReportData", "ReportGenerator"]
except ImportError:
    pass  # jinja2 not installed; Jinja2 report generation unavailable
