"""Interactive HTML report generator with embedded PCB visualization.

Generates single-file HTML reports with an interactive Canvas 2D board
viewer and a DRC violation browser. All CSS and JavaScript are inlined
so the output has no external dependencies.

Example:
    >>> from kicad_tools.report.interactive import render_interactive_html
    >>> html = render_interactive_html(pcb_path=Path("board.kicad_pcb"))
    >>> Path("report.html").write_text(html)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_interactive_html(
    pcb_path: Path,
    drc_violations: list[dict[str, Any]] | None = None,
    project_name: str | None = None,
    date: str | None = None,
) -> str:
    """Generate a self-contained interactive HTML report.

    The output is a single HTML file with inlined CSS and JavaScript.
    It includes a Canvas 2D PCB viewer with pan/zoom and a DRC violation
    table. Clicking a violation in the table pans the viewer to its
    location on the board.

    Args:
        pcb_path: Path to ``.kicad_pcb`` file.
        drc_violations: Optional list of DRC violation dicts (from
            ``DRCViolation.to_dict()``). When None, DRC is run
            automatically if available.
        project_name: Project display name. Defaults to the PCB
            filename stem.
        date: Report date string. Defaults to today's date.

    Returns:
        Complete HTML document string.
    """
    from kicad_tools.report.pcb_data import extract_pcb_data_from_path

    pcb_path = Path(pcb_path)

    if project_name is None:
        project_name = pcb_path.stem

    if date is None:
        from datetime import date as dt_date

        date = dt_date.today().isoformat()

    # Extract PCB geometry
    pcb_data = extract_pcb_data_from_path(pcb_path)

    # Gather DRC violations
    drc_data = _gather_drc_data(pcb_path, drc_violations)

    # Build the HTML from template
    title = f"{project_name} - Interactive DRC Report"
    return _build_html(title, pcb_data, drc_data, project_name, date)


def _gather_drc_data(
    pcb_path: Path,
    drc_violations: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Gather DRC data, either from provided violations or by running DRC.

    Args:
        pcb_path: Path to PCB file.
        drc_violations: Pre-computed violations or None.

    Returns:
        Dictionary with ``violations``, ``error_count``, ``warning_count``.
    """
    if drc_violations is not None:
        violations = drc_violations
    else:
        violations = _try_auto_drc(pcb_path)

    error_count = sum(1 for v in violations if v.get("severity", "error") == "error")
    warning_count = sum(1 for v in violations if v.get("severity", "error") == "warning")

    return {
        "violations": violations,
        "error_count": error_count,
        "warning_count": warning_count,
    }


def _try_auto_drc(pcb_path: Path) -> list[dict[str, Any]]:
    """Attempt to run DRC and return violations as dicts.

    Returns an empty list on failure.
    """
    try:
        from kicad_tools.drc.runner import run_drc

        report = run_drc(pcb_path)
        return [v.to_dict() for v in report.violations]
    except Exception:
        logger.debug("Auto-DRC failed; producing report with no violations", exc_info=True)
        return []


def _build_html(
    title: str,
    pcb_data: dict[str, Any],
    drc_data: dict[str, Any],
    project_name: str,
    date: str,
) -> str:
    """Assemble the HTML document from template and data.

    Loads CSS and JS from the templates directory and injects them
    along with JSON data payloads into the HTML template.
    """
    css = (_TEMPLATES_DIR / "interactive.css").read_text(encoding="utf-8")
    js = (_TEMPLATES_DIR / "interactive.js").read_text(encoding="utf-8")
    template_str = (_TEMPLATES_DIR / "interactive.html").read_text(encoding="utf-8")

    # Build data injection scripts
    pcb_json = json.dumps(pcb_data, separators=(",", ":"))
    drc_json = json.dumps(drc_data, separators=(",", ":"))
    meta_json = json.dumps(
        {"project_name": project_name, "date": date},
        separators=(",", ":"),
    )

    pcb_data_script = f"window.PCB_DATA = {pcb_json};"
    drc_data_script = f"window.DRC_DATA = {drc_json};"
    report_meta_script = f"window.REPORT_META = {meta_json};"

    # Simple template substitution (avoiding Jinja2 dependency for this)
    html = template_str
    html = html.replace("{{ title }}", _escape_html(title))
    html = html.replace("{{ css }}", css)
    html = html.replace("{{ js }}", js)
    html = html.replace("{{ pcb_data_script }}", pcb_data_script)
    html = html.replace("{{ drc_data_script }}", drc_data_script)
    html = html.replace("{{ report_meta_script }}", report_meta_script)

    return html


def _escape_html(s: str) -> str:
    """Minimal HTML escaping for template values."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
