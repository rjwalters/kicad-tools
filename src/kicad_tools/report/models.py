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
    """Board summary from collector: {layer_count, layer_names, footprint_count,
    footprint_smd, footprint_tht, footprint_other, net_count, segment_count,
    via_count, board_width_mm, board_height_mm}."""

    bom_groups: list[dict] | None = None
    """List of dicts: {value, footprint, qty, refs, mpn, lcsc}."""

    drc: dict | None = None
    """DRC summary: {error_count, warning_count, blocking_count, passed,
    violations_by_type (optional): {rule_id: count}}."""

    erc: dict | None = None
    """ERC summary: {error_count, warning_count, passed, skipped, details}.
    When ``skipped`` is true the ERC check was not executed (e.g. no
    schematic provided or ``--skip-erc`` flag used)."""

    audit: dict | None = None
    """Audit results: {verdict, action_items}."""

    net_status: dict | None = None
    """Net completion: {total_nets, complete_count, incomplete_count,
    unrouted_count, total_unconnected_pads, completion_percent,
    incomplete_net_names, signal_net_count, signal_complete_count,
    signal_completion_percent, signal_incomplete_net_names,
    signal_incomplete_named, signal_incomplete_auto_count,
    zone_connected_count, zone_connected_nets, single_pad_count,
    single_pad_nets}.

    ``signal_incomplete_named`` contains only human-assigned net names
    (excludes ``Net-(...)`` and ``unconnected-(...)`` auto-generated
    names).  ``signal_incomplete_auto_count`` is the count of
    auto-generated incomplete signal nets."""

    cost: dict | None = None
    """Cost estimate: {pcb_cost, component_cost (nullable),
    assembly_cost (nullable), total, per_unit, batch_qty, batch_total,
    currency}."""

    schematic_sheets: list[dict] | None = None
    """List of dicts: {name, figure_path}."""

    pcb_figures: dict | None = None
    """PCB renders: {front: path, back: path, copper: path, assembly: path}."""

    pcb_layer_figures: list[dict] | None = None
    """Per-copper-layer renders: [{name, figure_path}] in stackup order
    (e.g. F.Cu, In1.Cu, In2.Cu, B.Cu for a 4-layer board).  Issue #3497."""

    analog_components: list[dict] | None = None
    """Analog-sensitive components: [{reference, value, footprint, reason}]."""

    design_narrative: str | None = None
    """Free-text narrative assembled from title-block comments and sheet names."""

    functional_blocks: list[dict] | None = None
    """Hierarchical sheet summary: [{name, filename}]."""

    interfaces: list[dict] | None = None
    """Detected communication interfaces: [{protocol, signals}]."""

    power_architecture: list[dict] | None = None
    """Power rail summary: [{rail, voltage, regulator}]."""

    assembly_notes: dict | None = None
    """Assembly guidance: {fine_pitch_count, thermal_pad_count,
    polarized_count, summary}."""

    stackup: list[dict] | None = None
    """Layer stackup: [{name, type, thickness_mm, material}].
    Filtered to copper, dielectric, and mask layers."""

    off_board: dict | None = None
    """Off-board assemblies from the project spec (issue #3531b):
    {assemblies: [{name, description, connector, part, qty, voltage,
    capacitance, assembly, wiring}]}."""

    pcb_geometry: dict | None = None
    """PCB geometry data for interactive reports: {board_outline, bounds,
    footprints, segments, vias, layers}.  Populated by
    :func:`~kicad_tools.report.pcb_data.extract_pcb_data`."""

    notes: str = ""
    """Free-form notes section content."""

    # --- metadata ---
    tool_version: str = ""
    git_hash: str = ""
    _extra: dict = field(default_factory=dict)
    """Extra key-value pairs forwarded to the template context."""
