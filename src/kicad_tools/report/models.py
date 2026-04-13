"""Data models for design report generation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReportData:
    """Typed input data for the design report template.

    All optional sections default to ``None`` so the template can
    conditionally omit them when data is unavailable.
    """

    # --- required header fields ---
    project_name: str
    revision: str
    date: str  # ISO-8601 date string
    manufacturer: str

    # --- optional section data ---
    board_stats: dict | None = None
    """Layer count, component count, board area, net count, etc."""

    bom_groups: list[dict] | None = None
    """List of dicts: {value, footprint, qty, refs, mpn, lcsc}."""

    drc: dict | None = None
    """DRC summary: {error_count, warning_count, blocking_count, passed}."""

    audit: dict | None = None
    """Audit results: {verdict, action_items}."""

    net_status: dict | None = None
    """Net completion: {completion_percent, unrouted_count, unrouted_nets}."""

    cost: dict | None = None
    """Cost estimate: {per_unit, batch_qty, batch_total, currency}."""

    schematic_sheets: list[dict] | None = None
    """List of dicts: {name, figure_path}."""

    pcb_figures: dict | None = None
    """PCB renders: {front: path, back: path, copper: path}."""

    notes: str = ""
    """Free-form notes section content."""

    # --- metadata ---
    tool_version: str = ""
    git_hash: str = ""
    _extra: dict = field(default_factory=dict)
    """Extra key-value pairs forwarded to the template context."""
