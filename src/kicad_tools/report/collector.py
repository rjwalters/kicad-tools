"""Data snapshot collection for report generation.

Gathers board summary, DRC, BOM, audit, cost, net connectivity, and analysis
results into JSON snapshots. Each sub-collector is fault-tolerant: failures
produce a warning log and null result rather than propagating exceptions.

Example:
    >>> from kicad_tools.report import ReportDataCollector
    >>> collector = ReportDataCollector(Path("board.kicad_pcb"))
    >>> files = collector.collect_all(Path("output/data"))
    >>> for name, path in files.items():
    ...     print(f"{name}: {path}")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.report.utils import find_schematic

if TYPE_CHECKING:
    from kicad_tools.audit.auditor import AuditResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _make_envelope(data: dict[str, Any] | None, pcb_path: Path) -> dict[str, Any]:
    """Wrap data in a standard envelope with schema version and timestamp."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pcb_path": str(pcb_path),
        "data": data,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON file with consistent formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


class ReportDataCollector:
    """Collects data snapshots for report generation.

    Orchestrates calls to existing analysis APIs and serializes their
    results as JSON files. ManufacturingAudit is run once and its results
    are distributed to both the DRC and audit snapshots.

    Args:
        pcb_path: Path to .kicad_pcb file.
        manufacturer: Target manufacturer ID (default: "jlcpcb").
        quantity: Quantity for cost estimation (default: 5).
        skip_erc: Skip ERC check (default: False).
    """

    def __init__(
        self,
        pcb_path: Path,
        manufacturer: str = "jlcpcb",
        quantity: int = 5,
        skip_erc: bool = False,
    ) -> None:
        self.pcb_path = Path(pcb_path)
        self.manufacturer = manufacturer
        self.quantity = quantity
        self.skip_erc = skip_erc

    def collect_all(self, output_dir: Path) -> dict[str, Path]:
        """Run all collectors, write JSON files, return mapping of name to path.

        Runs ManufacturingAudit once and reuses the result for DRC, audit,
        and cost snapshots. Each sub-collector is wrapped in try/except so
        a failure in one does not prevent the others from running.

        Args:
            output_dir: Directory to write JSON snapshot files into.

        Returns:
            Mapping of snapshot name to file path for each successfully
            written file.
        """
        from kicad_tools.schema.pcb import PCB

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        files: dict[str, Path] = {}
        pcb = PCB.load(self.pcb_path)

        # Run ManufacturingAudit once and reuse for DRC + audit snapshots.
        audit_result = None
        try:
            from kicad_tools.audit.auditor import ManufacturingAudit

            audit = ManufacturingAudit(
                self.pcb_path,
                manufacturer=self.manufacturer,
                quantity=self.quantity,
                skip_erc=self.skip_erc,
            )
            audit_result = audit.run()
        except Exception:
            logger.warning(
                "ManufacturingAudit failed; DRC, audit, and cost snapshots will be null",
                exc_info=True,
            )

        # Board summary
        self._safe_collect(
            "board_summary",
            output_dir,
            files,
            lambda: self.collect_board_summary(pcb),
        )

        # DRC summary (from audit result)
        self._safe_collect(
            "drc_summary",
            output_dir,
            files,
            lambda: self.collect_drc(audit_result),
        )

        # ERC summary (from audit result)
        self._safe_collect(
            "erc_summary",
            output_dir,
            files,
            lambda: self.collect_erc(audit_result),
        )

        # BOM
        sch_path = find_schematic(self.pcb_path)
        if sch_path is not None:
            self._safe_collect(
                "bom",
                output_dir,
                files,
                lambda: self.collect_bom(sch_path),
            )

            # Narrative (design description, interfaces, power rails, assembly notes)
            self._safe_collect(
                "narrative",
                output_dir,
                files,
                lambda: self.collect_narrative(sch_path, pcb),
            )
        else:
            logger.warning(
                "No schematic found for %s; skipping BOM and narrative collection. "
                "Use --sch to specify explicitly.",
                self.pcb_path,
            )

        # Audit
        self._safe_collect(
            "audit",
            output_dir,
            files,
            lambda: self.collect_audit(audit_result),
        )

        # Cost (from audit result, with field names normalised for template)
        self._safe_collect(
            "cost",
            output_dir,
            files,
            lambda: self.collect_cost(audit_result),
        )

        # Net status
        self._safe_collect(
            "net_status",
            output_dir,
            files,
            lambda: self.collect_net_status(pcb),
        )

        # Analysis (congestion + SI + thermal)
        self._safe_collect(
            "analysis",
            output_dir,
            files,
            lambda: self.collect_analysis(pcb),
        )

        # Analog components
        self._safe_collect(
            "analog_components",
            output_dir,
            files,
            lambda: self.collect_analog_components(pcb),
        )

        # Stackup
        self._safe_collect(
            "stackup",
            output_dir,
            files,
            lambda: self.collect_stackup(pcb),
        )

        return files

    # ------------------------------------------------------------------
    # Individual sub-collectors
    # ------------------------------------------------------------------

    def collect_board_summary(self, pcb: Any) -> dict[str, Any]:
        """Collect board summary: layers, footprints, nets, traces, vias, dimensions.

        Args:
            pcb: Loaded PCB object.

        Returns:
            Dictionary with board summary data.
        """
        # Layer info
        copper_layers = pcb.copper_layers
        layer_names = [layer.name for layer in copper_layers]

        # Footprint breakdown
        total_fp = len(pcb.footprints)
        smd_count = sum(1 for fp in pcb.footprints if fp.attr == "smd")
        tht_count = sum(1 for fp in pcb.footprints if fp.attr == "through_hole")
        other_count = total_fp - smd_count - tht_count

        # Board dimensions via Edge.Cuts parsing (same pattern as ManufacturingAudit)
        board_width, board_height = self._get_board_dimensions(pcb)

        return {
            "layer_count": len(copper_layers),
            "layer_names": layer_names,
            "footprint_count": total_fp,
            "footprint_smd": smd_count,
            "footprint_tht": tht_count,
            "footprint_other": other_count,
            "net_count": len(pcb.nets),
            "segment_count": pcb.segment_count,
            "via_count": pcb.via_count,
            "board_width_mm": round(board_width, 2),
            "board_height_mm": round(board_height, 2),
        }

    def collect_drc(self, audit_result: AuditResult | None) -> dict[str, Any] | None:
        """Extract DRC sub-section from a pre-run AuditResult.

        Args:
            audit_result: Result from ManufacturingAudit.run(), or None if
                the audit failed.

        Returns:
            DRC data dictionary, or None if audit_result is None.
        """
        if audit_result is None:
            return None
        return audit_result.drc.to_dict()

    def collect_erc(self, audit_result: AuditResult | None) -> dict[str, Any] | None:
        """Extract ERC sub-section from a pre-run AuditResult.

        Args:
            audit_result: Result from ManufacturingAudit.run(), or None if
                the audit failed.

        Returns:
            ERC data dictionary, or None if audit_result is None.
        """
        if audit_result is None:
            return None
        return audit_result.erc.to_dict()

    def collect_bom(self, sch_path: Path) -> dict[str, Any]:
        """Collect BOM grouped by value+footprint with LCSC numbers.

        Args:
            sch_path: Path to .kicad_sch file.

        Returns:
            Dictionary with BOM data.
        """
        from kicad_tools.schema.bom import extract_bom

        bom = extract_bom(str(sch_path))
        groups = bom.grouped()

        return {
            "total_components": bom.total_components,
            "unique_parts": bom.unique_parts,
            "dnp_count": bom.dnp_count,
            "groups": [g.to_dict() for g in groups],
        }

    def collect_audit(self, audit_result: AuditResult | None) -> dict[str, Any] | None:
        """Full manufacturing audit snapshot.

        Args:
            audit_result: Result from ManufacturingAudit.run(), or None if
                the audit failed.

        Returns:
            Audit data dictionary, or None if audit_result is None.
        """
        if audit_result is None:
            return None
        return audit_result.to_dict()

    def collect_cost(self, audit_result: AuditResult | None) -> dict[str, Any] | None:
        """Extract and normalise cost data from a pre-run AuditResult.

        Returns a dictionary with separate ``pcb_cost``,
        ``component_cost`` (nullable), ``assembly_cost`` (nullable),
        and ``total`` fields so the template can render labelled
        sub-groups.  Legacy ``per_unit``, ``batch_qty``, and
        ``batch_total`` keys are preserved for backward compatibility.

        Args:
            audit_result: Result from ManufacturingAudit.run(), or None if
                the audit failed.

        Returns:
            Normalised cost dictionary for the template, or None if
            audit_result is None.
        """
        if audit_result is None:
            return None
        ce = audit_result.cost
        per_unit = round(ce.total_cost / ce.quantity, 2) if ce.quantity else 0.0
        pcb_per_unit = round(ce.pcb_cost / ce.quantity, 2) if ce.quantity else 0.0

        result: dict[str, Any] = {
            # Per-board breakdown
            "pcb_cost": pcb_per_unit,
            "component_cost": (
                round(ce.component_cost / ce.quantity, 2)
                if ce.component_cost is not None and ce.quantity
                else None
            ),
            "assembly_cost": (
                round(ce.assembly_cost / ce.quantity, 2)
                if ce.assembly_cost is not None and ce.quantity
                else None
            ),
            "total": per_unit,
            # Legacy / batch fields
            "per_unit": per_unit,
            "batch_qty": ce.quantity,
            "batch_total": round(ce.total_cost, 2),
            "currency": ce.currency,
        }
        return result

    # Maximum number of incomplete net names included in the snapshot.
    _INCOMPLETE_NET_NAMES_CAP = 50

    def collect_net_status(self, pcb: Any) -> dict[str, Any]:
        """Routing completion summary.

        Args:
            pcb: Loaded PCB object.

        Returns:
            Dictionary with net status data including totals, completion
            percentage, names of incomplete/unrouted nets, and per-type
            breakdowns (signal, zone-connected, single-pad).
        """
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        analyzer = NetStatusAnalyzer(pcb)
        result = analyzer.analyze()

        completion_percent = 0.0
        if result.total_nets > 0:
            completion_percent = round(100.0 * result.complete_count / result.total_nets, 1)

        # Classify nets by type for richer reporting.
        # Zone-connected: plane nets that the trace-level analyzer may mark
        # incomplete but are actually connected via copper zones.
        zone_connected_nets = sorted(
            n.net_name for n in result.nets if n.is_plane_net and n.status != "complete"
        )
        # Also include plane nets that *are* complete (they are still zone-connected).
        all_zone_nets = sorted(n.net_name for n in result.nets if n.is_plane_net)

        # Single-pad nets: nets with exactly one pad (no routing needed).
        single_pad_nets = sorted(n.net_name for n in result.nets if n.total_pads == 1)

        # Signal nets: everything that is not a plane net and not a single-pad net.
        signal_nets = [
            n for n in result.nets if not n.is_plane_net and n.total_pads != 1
        ]
        signal_net_count = len(signal_nets)
        signal_complete_count = sum(1 for n in signal_nets if n.status == "complete")
        signal_completion_percent = 0.0
        if signal_net_count > 0:
            signal_completion_percent = round(
                100.0 * signal_complete_count / signal_net_count, 1
            )

        # Collect names of nets that are not fully routed (incomplete or unrouted),
        # excluding zone-connected and single-pad nets since those are reported
        # separately.  Fall back to the full list for backward compatibility.
        zone_set = set(all_zone_nets)
        single_set = set(single_pad_nets)
        incomplete_net_names = sorted(
            n.net_name
            for n in result.nets
            if n.status != "complete"
        )[: self._INCOMPLETE_NET_NAMES_CAP]

        # Signal-only incomplete list for the new template section.
        signal_incomplete_net_names = sorted(
            n.net_name
            for n in result.nets
            if n.status != "complete"
            and n.net_name not in zone_set
            and n.net_name not in single_set
        )[: self._INCOMPLETE_NET_NAMES_CAP]

        # Split signal incomplete nets into named (human-assigned) vs
        # auto-generated (KiCad default names like "Net-(...)" and
        # "unconnected-(...)").  Named nets have higher information value
        # and are listed individually; auto-generated nets are reduced to
        # a count to avoid noise in the report.
        _AUTO_NET_PREFIXES = ("Net-(", "unconnected-(")

        signal_incomplete_named = sorted(
            n.net_name
            for n in result.nets
            if n.status != "complete"
            and n.net_name not in zone_set
            and n.net_name not in single_set
            and not n.net_name.startswith(_AUTO_NET_PREFIXES)
        )[: self._INCOMPLETE_NET_NAMES_CAP]

        signal_incomplete_auto_count = sum(
            1
            for n in result.nets
            if n.status != "complete"
            and n.net_name not in zone_set
            and n.net_name not in single_set
            and n.net_name.startswith(_AUTO_NET_PREFIXES)
        )

        return {
            # Existing keys (backward compatible)
            "total_nets": result.total_nets,
            "complete_count": result.complete_count,
            "incomplete_count": result.incomplete_count,
            "unrouted_count": result.unrouted_count,
            "total_unconnected_pads": result.total_unconnected_pads,
            "completion_percent": completion_percent,
            "incomplete_net_names": incomplete_net_names,
            # New keys for per-type breakdown
            "signal_net_count": signal_net_count,
            "signal_complete_count": signal_complete_count,
            "signal_completion_percent": signal_completion_percent,
            "signal_incomplete_net_names": signal_incomplete_net_names,
            "signal_incomplete_named": signal_incomplete_named,
            "signal_incomplete_auto_count": signal_incomplete_auto_count,
            "zone_connected_count": len(all_zone_nets),
            "zone_connected_nets": all_zone_nets,
            "single_pad_count": len(single_pad_nets),
            "single_pad_nets": single_pad_nets,
        }

    def collect_analysis(self, pcb: Any) -> dict[str, Any]:
        """Combined congestion, signal integrity, and thermal snapshots.

        Each section is collected independently. If any analyzer raises,
        that section is set to None with a warning log.

        Args:
            pcb: Loaded PCB object.

        Returns:
            Dictionary with congestion, signal_integrity, and thermal
            sections. Each section may be None on error.
        """
        result: dict[str, Any] = {
            "congestion": None,
            "signal_integrity": None,
            "thermal": None,
        }

        # Congestion
        try:
            from kicad_tools.analysis.congestion import CongestionAnalyzer

            reports = CongestionAnalyzer().analyze(pcb)
            result["congestion"] = {
                "hotspot_count": len(reports),
                "severity_breakdown": self._severity_breakdown(reports),
                "hotspots": [r.to_dict() for r in reports],
            }
        except Exception:
            logger.warning("Congestion analysis failed", exc_info=True)

        # Signal integrity
        try:
            from kicad_tools.analysis.signal_integrity import TraceIntegrityAnalyzer

            si = TraceIntegrityAnalyzer()
            crosstalk = si.analyze_crosstalk(pcb)
            impedance = si.analyze_impedance(pcb)
            result["signal_integrity"] = {
                "crosstalk_risk_count": len(crosstalk),
                "impedance_discontinuity_count": len(impedance),
                "crosstalk_risks": [r.to_dict() for r in crosstalk],
                "impedance_discontinuities": [d.to_dict() for d in impedance],
            }
        except Exception:
            logger.warning("Signal integrity analysis failed", exc_info=True)

        # Thermal
        try:
            from kicad_tools.analysis.thermal import ThermalAnalyzer

            hotspots = ThermalAnalyzer().analyze(pcb)
            result["thermal"] = {
                "hotspot_count": len(hotspots),
                "hotspots": [h.to_dict() for h in hotspots],
            }
        except Exception:
            logger.warning("Thermal analysis failed", exc_info=True)

        return result

    def collect_analog_components(self, pcb: Any) -> dict[str, Any] | None:
        """Detect analog-sensitive components on the board.

        Returns a dictionary with count and component details, or None
        if no analog components are detected.

        Args:
            pcb: Loaded PCB object.

        Returns:
            Dictionary with analog component data, or None if none found.
        """
        from kicad_tools.analysis.analog_detect import detect_analog_components

        components = detect_analog_components(pcb)
        if not components:
            return None
        return {
            "count": len(components),
            "components": [c.to_dict() for c in components],
        }

    def collect_stackup(self, pcb: Any) -> list[dict[str, Any]] | None:
        """Collect layer stackup from the PCB setup data.

        Returns a list of layer dicts filtered to copper, dielectric, and
        solder mask layers (silkscreen and paste layers are excluded to
        reduce noise).  Returns ``None`` when no stackup data is available.

        Args:
            pcb: Loaded PCB object.

        Returns:
            List of layer dicts with name, type, thickness_mm, and material,
            or None if no stackup is available.
        """
        setup = getattr(pcb, "setup", None)
        if setup is None or not getattr(setup, "stackup", None):
            return None

        # Layer types worth showing in a manufacturing report
        _INCLUDE_TYPES = {"copper", "prepreg", "core", "Top Solder Mask", "Bottom Solder Mask"}

        layers = []
        for layer in setup.stackup:
            layer_type = getattr(layer, "type", "") or ""
            if layer_type not in _INCLUDE_TYPES:
                continue
            layers.append({
                "name": getattr(layer, "name", ""),
                "type": layer_type,
                "thickness_mm": getattr(layer, "thickness", 0.0),
                "material": getattr(layer, "material", ""),
            })

        return layers if layers else None

    def collect_narrative(self, sch_path: Path, pcb: Any) -> dict[str, Any]:
        """Collect design narrative from schematic metadata.

        Extracts title-block comments, hierarchical sheet names,
        interface labels, power rail symbols, and assembly guidance.
        Each sub-section is collected independently; failures in one
        do not prevent the others from populating.

        Args:
            sch_path: Path to root ``.kicad_sch`` file.
            pcb: Loaded PCB object (used for assembly note heuristics).

        Returns:
            Dictionary with ``design_narrative``, ``functional_blocks``,
            ``interfaces``, ``power_architecture``, and ``assembly_notes``
            keys. Any section may be ``None`` on error or when no data
            is available.
        """
        from kicad_tools.schema.schematic import Schematic

        sch = Schematic.load(sch_path)
        result: dict[str, Any] = {
            "design_narrative": None,
            "functional_blocks": None,
            "interfaces": None,
            "power_architecture": None,
            "assembly_notes": None,
        }

        # --- Title-block narrative ---
        try:
            result["design_narrative"] = self._extract_design_narrative(
                sch, sch_path
            )
        except Exception:
            logger.warning("Design narrative extraction failed", exc_info=True)

        # --- Functional blocks from hierarchical sheets ---
        try:
            result["functional_blocks"] = self._extract_functional_blocks(sch)
        except Exception:
            logger.warning("Functional block extraction failed", exc_info=True)

        # --- Interface detection from labels ---
        try:
            result["interfaces"] = self._detect_interfaces(sch, sch_path)
        except Exception:
            logger.warning("Interface detection failed", exc_info=True)

        # --- Power architecture from symbols ---
        try:
            result["power_architecture"] = self._extract_power_architecture(
                sch, sch_path
            )
        except Exception:
            logger.warning("Power architecture extraction failed", exc_info=True)

        # --- Assembly notes from PCB footprints ---
        try:
            result["assembly_notes"] = self._extract_assembly_notes(pcb)
        except Exception:
            logger.warning("Assembly note extraction failed", exc_info=True)

        return result

    # ------------------------------------------------------------------
    # Narrative sub-extractors
    # ------------------------------------------------------------------

    def _extract_design_narrative(
        self, sch: Any, sch_path: Path
    ) -> str | None:
        """Build a narrative string from title-block comments.

        Concatenates the root title-block title and numbered comments,
        then appends title-block comments from sub-sheets.

        Returns ``None`` when no narrative text is found.
        """
        from kicad_tools.schema.schematic import Schematic

        parts: list[str] = []

        # Root title block
        tb = sch.title_block
        if tb.title:
            parts.append(tb.title)
        for _num in sorted(tb.comments.keys()):
            text = tb.comments[_num].strip()
            if text:
                parts.append(text)

        # Sub-sheet title blocks
        for sheet in sch.sheets:
            if not sheet.filename:
                continue
            sub_path = sch_path.parent / sheet.filename
            if not sub_path.exists():
                continue
            try:
                sub_sch = Schematic.load(sub_path)
                sub_tb = sub_sch.title_block
                for _num in sorted(sub_tb.comments.keys()):
                    text = sub_tb.comments[_num].strip()
                    if text:
                        parts.append(text)
            except Exception:
                logger.debug(
                    "Could not load sub-sheet %s for narrative",
                    sub_path,
                    exc_info=True,
                )

        return "\n\n".join(parts) if parts else None

    def _extract_functional_blocks(self, sch: Any) -> list[dict[str, str]] | None:
        """Return hierarchical sheet names as functional block summaries.

        Returns ``None`` when the schematic has no hierarchical sheets.
        """
        if not sch.sheets:
            return None

        blocks = []
        for sheet in sch.sheets:
            name = sheet.name or sheet.filename or "Unnamed"
            blocks.append({"name": name, "filename": sheet.filename})
        return blocks if blocks else None

    # Interface detection patterns (protocol -> list of signal name patterns).
    # Each pattern is matched as a case-insensitive substring in label text.
    _INTERFACE_PATTERNS: dict[str, list[str]] = {
        "I2C": ["SDA", "SCL"],
        "SPI": ["MOSI", "MISO", "SCK", "SCLK", "SDI", "SDO"],
        "I2S": ["BCLK", "LRCLK", "DOUT", "DIN", "MCLK"],
        "UART": ["TX", "RX"],
        "USB": ["D+", "D-", "VBUS", "USB_D"],
    }

    def _detect_interfaces(
        self, sch: Any, sch_path: Path
    ) -> list[dict[str, Any]] | None:
        """Heuristically detect communication interfaces from label names.

        Scans global labels and local labels in the root schematic and
        sub-sheets.  A protocol is reported when at least two of its
        characteristic signal names are found.

        Returns ``None`` when no interfaces are detected.
        """
        from kicad_tools.schema.schematic import Schematic

        all_labels: set[str] = set()

        # Collect labels from root schematic
        for lbl in sch.global_labels:
            all_labels.add(lbl.text.upper())
        for lbl in sch.labels:
            all_labels.add(lbl.text.upper())

        # Collect labels from sub-sheets
        for sheet in sch.sheets:
            if not sheet.filename:
                continue
            sub_path = sch_path.parent / sheet.filename
            if not sub_path.exists():
                continue
            try:
                sub_sch = Schematic.load(sub_path)
                for lbl in sub_sch.global_labels:
                    all_labels.add(lbl.text.upper())
                for lbl in sub_sch.labels:
                    all_labels.add(lbl.text.upper())
            except Exception:
                logger.debug(
                    "Could not load sub-sheet %s for interface detection",
                    sub_path,
                    exc_info=True,
                )

        detected: list[dict[str, Any]] = []
        for protocol, patterns in self._INTERFACE_PATTERNS.items():
            matched = []
            for pattern in patterns:
                for label_text in all_labels:
                    if pattern in label_text:
                        matched.append(label_text)
                        break  # one match per pattern is enough
            if len(matched) >= 2:
                detected.append(
                    {"protocol": protocol, "signals": sorted(set(matched))}
                )

        return detected if detected else None

    def _extract_power_architecture(
        self, sch: Any, sch_path: Path
    ) -> list[dict[str, str | None]] | None:
        """Enumerate power rails and regulators.

        Finds symbols with ``power:`` library prefix and regulator
        components (``Regulator_Linear:`` or ``Regulator_Switching:``
        lib_id prefix).

        Returns ``None`` when no power information is found.
        """
        from kicad_tools.schema.schematic import Schematic

        rails: set[str] = set()
        regulators: list[dict[str, str | None]] = []

        def _scan_schematic(s: Any) -> None:
            for sym in s.symbols:
                lib_id = sym.lib_id or ""
                if lib_id.startswith("power:"):
                    # Extract rail name from Value property
                    value = ""
                    if "Value" in sym.properties:
                        value = sym.properties["Value"].value
                    if value:
                        rails.add(value)
                elif lib_id.startswith(
                    ("Regulator_Linear:", "Regulator_Switching:")
                ):
                    ref = sym.reference or ""
                    value = ""
                    if "Value" in sym.properties:
                        value = sym.properties["Value"].value
                    regulators.append({"reference": ref, "value": value})

        _scan_schematic(sch)

        # Scan sub-sheets
        for sheet in sch.sheets:
            if not sheet.filename:
                continue
            sub_path = sch_path.parent / sheet.filename
            if not sub_path.exists():
                continue
            try:
                sub_sch = Schematic.load(sub_path)
                _scan_schematic(sub_sch)
            except Exception:
                logger.debug(
                    "Could not load sub-sheet %s for power architecture",
                    sub_path,
                    exc_info=True,
                )

        if not rails and not regulators:
            return None

        result: list[dict[str, str | None]] = []
        for rail in sorted(rails):
            result.append({"rail": rail, "type": "power_symbol"})
        for reg in regulators:
            result.append(
                {
                    "rail": reg["reference"],
                    "type": "regulator",
                    "value": reg["value"],
                }
            )
        return result if result else None

    def _extract_assembly_notes(self, pcb: Any) -> dict[str, Any] | None:
        """Generate assembly guidance from PCB footprint analysis.

        Detects fine-pitch packages (QFP, BGA), thermal pads, and
        polarized components. Returns ``None`` when no noteworthy
        assembly observations are found.
        """
        import re

        fine_pitch_count = 0
        thermal_pad_count = 0
        polarized_count = 0
        fine_pitch_parts: list[str] = []
        _FINE_PITCH_CAP = 10  # cap listed parts for readability

        for fp in pcb.footprints:
            fp_name = getattr(fp, "name", "") or ""
            ref = getattr(fp, "reference", "") or ""

            # Fine-pitch detection: QFP, BGA, QFN patterns
            if re.search(r"(QFP|BGA|QFN)", fp_name, re.IGNORECASE):
                fine_pitch_count += 1
                if len(fine_pitch_parts) < _FINE_PITCH_CAP:
                    fine_pitch_parts.append(ref or fp_name)

            # Thermal pad detection
            if re.search(
                r"(ThermalVia|ExposedPad|Thermal)", fp_name, re.IGNORECASE
            ):
                thermal_pad_count += 1

            # Polarized component detection (electrolytic caps, diodes, LEDs)
            if ref.startswith("D") or re.search(
                r"(CP_Elec|Polarized|LED)", fp_name, re.IGNORECASE
            ):
                polarized_count += 1

        if fine_pitch_count == 0 and thermal_pad_count == 0 and polarized_count == 0:
            return None

        summary_parts: list[str] = []
        if fine_pitch_count:
            summary_parts.append(
                f"{fine_pitch_count} fine-pitch component{'s' if fine_pitch_count != 1 else ''}"
            )
        if thermal_pad_count:
            summary_parts.append(
                f"{thermal_pad_count} thermal pad{'s' if thermal_pad_count != 1 else ''}"
            )
        if polarized_count:
            summary_parts.append(
                f"{polarized_count} polarized component{'s' if polarized_count != 1 else ''}"
            )

        return {
            "fine_pitch_count": fine_pitch_count,
            "fine_pitch_parts": fine_pitch_parts,
            "thermal_pad_count": thermal_pad_count,
            "polarized_count": polarized_count,
            "summary": "; ".join(summary_parts),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_collect(
        self,
        name: str,
        output_dir: Path,
        files: dict[str, Path],
        collector_fn: Any,
    ) -> None:
        """Run a collector function, wrap in envelope, and write JSON.

        If the collector raises, the file is still written with
        ``data: null`` so downstream consumers can distinguish between
        'not collected' (file absent) and 'collection failed' (data null).
        """
        try:
            data = collector_fn()
        except Exception:
            logger.warning("Collector '%s' failed", name, exc_info=True)
            data = None

        path = output_dir / f"{name}.json"
        _write_json(path, _make_envelope(data, self.pcb_path))
        files[name] = path

    def _get_board_dimensions(self, pcb: Any) -> tuple[float, float]:
        """Get board dimensions (width, height) in mm from Edge.Cuts.

        Skips circle elements (mounting hole cutouts) whose ``start``
        coordinates are parser artifacts (typically ``(0, 0)``).
        """
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")

        for item in pcb.graphic_items:
            if item.layer != "Edge.Cuts":
                continue
            # Skip circles — their start/end don't represent board outline
            gtype = getattr(item, "graphic_type", None)
            if gtype == "circle":
                continue
            if hasattr(item, "start"):
                min_x = min(min_x, item.start[0])
                min_y = min(min_y, item.start[1])
                max_x = max(max_x, item.start[0])
                max_y = max(max_y, item.start[1])
            if hasattr(item, "end"):
                min_x = min(min_x, item.end[0])
                min_y = min(min_y, item.end[1])
                max_x = max(max_x, item.end[0])
                max_y = max(max_y, item.end[1])

        if min_x != float("inf"):
            return (max_x - min_x, max_y - min_y)

        return (0.0, 0.0)

    @staticmethod
    def _severity_breakdown(reports: list[Any]) -> dict[str, int]:
        """Count congestion reports by severity level."""
        breakdown: dict[str, int] = {}
        for r in reports:
            key = r.severity.value
            breakdown[key] = breakdown.get(key, 0) + 1
        return breakdown
