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
        else:
            logger.warning(
                "No schematic found for %s; skipping BOM collection. "
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
            "segment_count": len(pcb.segments),
            "via_count": len(pcb.vias),
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
            percentage, and names of incomplete/unrouted nets.
        """
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        analyzer = NetStatusAnalyzer(pcb)
        result = analyzer.analyze()

        completion_percent = 0.0
        if result.total_nets > 0:
            completion_percent = round(100.0 * result.complete_count / result.total_nets, 1)

        # Collect names of nets that are not fully routed (incomplete or unrouted),
        # sorted alphabetically and capped to keep JSON snapshots manageable.
        incomplete_net_names = sorted(n.net_name for n in result.nets if n.status != "complete")[
            : self._INCOMPLETE_NET_NAMES_CAP
        ]

        return {
            "total_nets": result.total_nets,
            "complete_count": result.complete_count,
            "incomplete_count": result.incomplete_count,
            "unrouted_count": result.unrouted_count,
            "total_unconnected_pads": result.total_unconnected_pads,
            "completion_percent": completion_percent,
            "incomplete_net_names": incomplete_net_names,
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
            from kicad_tools.analysis.signal_integrity import SignalIntegrityAnalyzer

            si = SignalIntegrityAnalyzer()
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
        """Get board dimensions (width, height) in mm from Edge.Cuts."""
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")

        for item in pcb.graphic_items:
            if item.layer == "Edge.Cuts":
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
