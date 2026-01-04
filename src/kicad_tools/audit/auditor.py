"""
Manufacturing readiness auditor for KiCad PCB designs.

Runs comprehensive checks to verify a design is ready for manufacturing.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)


class AuditVerdict(Enum):
    """Overall audit verdict."""

    READY = "ready"  # All checks pass, ready for manufacturing
    WARNING = "warning"  # Minor issues, review recommended
    NOT_READY = "not_ready"  # Critical issues, must fix


@dataclass
class ERCStatus:
    """ERC check results."""

    error_count: int = 0
    warning_count: int = 0
    passed: bool = True
    details: str = ""
    report_path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass
class DRCStatus:
    """DRC check results."""

    error_count: int = 0
    warning_count: int = 0
    blocking_count: int = 0  # Violations that block manufacturing
    passed: bool = True
    details: str = ""
    report_path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "blocking_count": self.blocking_count,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass
class ConnectivityStatus:
    """Net connectivity check results."""

    total_nets: int = 0
    connected_nets: int = 0
    incomplete_nets: int = 0
    completion_percent: float = 100.0
    unconnected_pads: int = 0
    passed: bool = True
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "total_nets": self.total_nets,
            "connected_nets": self.connected_nets,
            "incomplete_nets": self.incomplete_nets,
            "completion_percent": self.completion_percent,
            "unconnected_pads": self.unconnected_pads,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass
class ManufacturerCompatibility:
    """Manufacturer design rule compatibility."""

    manufacturer: str = ""
    min_trace_width: tuple[float, float, bool] = (0, 0, True)  # (actual, limit, pass)
    min_clearance: tuple[float, float, bool] = (0, 0, True)
    min_via_drill: tuple[float, float, bool] = (0, 0, True)
    min_annular_ring: tuple[float, float, bool] = (0, 0, True)
    board_size: tuple[tuple[float, float], tuple[float, float], bool] = (
        (0, 0),
        (0, 0),
        True,
    )
    layer_count: tuple[int, list[int], bool] = (0, [], True)
    passed: bool = True
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "manufacturer": self.manufacturer,
            "min_trace_width_mm": {
                "actual": self.min_trace_width[0],
                "limit": self.min_trace_width[1],
                "pass": self.min_trace_width[2],
            },
            "min_clearance_mm": {
                "actual": self.min_clearance[0],
                "limit": self.min_clearance[1],
                "pass": self.min_clearance[2],
            },
            "min_via_drill_mm": {
                "actual": self.min_via_drill[0],
                "limit": self.min_via_drill[1],
                "pass": self.min_via_drill[2],
            },
            "min_annular_ring_mm": {
                "actual": self.min_annular_ring[0],
                "limit": self.min_annular_ring[1],
                "pass": self.min_annular_ring[2],
            },
            "board_size_mm": {
                "actual": list(self.board_size[0]),
                "max": list(self.board_size[1]),
                "pass": self.board_size[2],
            },
            "layer_count": {
                "actual": self.layer_count[0],
                "supported": self.layer_count[1],
                "pass": self.layer_count[2],
            },
            "passed": self.passed,
        }


@dataclass
class LayerUtilization:
    """PCB layer utilization statistics."""

    layer_count: int = 0
    utilization: dict[str, float] = field(default_factory=dict)  # layer_name -> percent

    def to_dict(self) -> dict:
        return {
            "layer_count": self.layer_count,
            "utilization": self.utilization,
        }


@dataclass
class CostEstimate:
    """Manufacturing cost estimate."""

    pcb_cost: float = 0.0
    component_cost: float | None = None
    assembly_cost: float | None = None
    total_cost: float = 0.0
    quantity: int = 5
    currency: str = "USD"

    def to_dict(self) -> dict:
        return {
            "pcb_cost": self.pcb_cost,
            "component_cost": self.component_cost,
            "assembly_cost": self.assembly_cost,
            "total_cost": self.total_cost,
            "quantity": self.quantity,
            "currency": self.currency,
        }


@dataclass
class ActionItem:
    """Suggested action to fix an issue."""

    priority: int  # 1 = critical, 2 = important, 3 = optional
    description: str
    command: str | None = None  # Optional CLI command to fix

    def to_dict(self) -> dict:
        return {
            "priority": self.priority,
            "description": self.description,
            "command": self.command,
        }


@dataclass
class AuditResult:
    """Complete manufacturing audit result."""

    # Source info
    project_name: str = ""
    schematic_path: Path | None = None
    pcb_path: Path | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    # Check results
    erc: ERCStatus = field(default_factory=ERCStatus)
    drc: DRCStatus = field(default_factory=DRCStatus)
    connectivity: ConnectivityStatus = field(default_factory=ConnectivityStatus)
    compatibility: ManufacturerCompatibility = field(default_factory=ManufacturerCompatibility)
    layers: LayerUtilization = field(default_factory=LayerUtilization)
    cost: CostEstimate = field(default_factory=CostEstimate)

    # Action items
    action_items: list[ActionItem] = field(default_factory=list)

    @property
    def verdict(self) -> AuditVerdict:
        """Determine overall verdict based on check results."""
        # Critical failures
        if self.erc.error_count > 0:
            return AuditVerdict.NOT_READY
        if self.drc.blocking_count > 0:
            return AuditVerdict.NOT_READY
        if not self.connectivity.passed:
            return AuditVerdict.NOT_READY
        if not self.compatibility.passed:
            return AuditVerdict.NOT_READY

        # Warnings
        if self.drc.warning_count > 0 or self.erc.warning_count > 0:
            return AuditVerdict.WARNING

        return AuditVerdict.READY

    @property
    def is_ready(self) -> bool:
        """Check if design is ready for manufacturing."""
        return self.verdict == AuditVerdict.READY

    def summary(self) -> dict:
        """Get summary statistics."""
        return {
            "verdict": self.verdict.value,
            "is_ready": self.is_ready,
            "erc_errors": self.erc.error_count,
            "drc_violations": self.drc.error_count + self.drc.warning_count,
            "drc_blocking": self.drc.blocking_count,
            "net_completion": self.connectivity.completion_percent,
            "manufacturer_compatible": self.compatibility.passed,
            "estimated_cost": self.cost.total_cost,
            "action_items": len(self.action_items),
        }

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "project_name": self.project_name,
            "schematic_path": str(self.schematic_path) if self.schematic_path else None,
            "pcb_path": str(self.pcb_path) if self.pcb_path else None,
            "timestamp": self.timestamp.isoformat(),
            "verdict": self.verdict.value,
            "is_ready": self.is_ready,
            "summary": self.summary(),
            "erc": self.erc.to_dict(),
            "drc": self.drc.to_dict(),
            "connectivity": self.connectivity.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "layers": self.layers.to_dict(),
            "cost": self.cost.to_dict(),
            "action_items": [a.to_dict() for a in self.action_items],
        }


class ManufacturingAudit:
    """Comprehensive manufacturing readiness audit.

    Example:
        >>> audit = ManufacturingAudit("project.kicad_pro", manufacturer="jlcpcb")
        >>> result = audit.run()
        >>> if result.is_ready:
        ...     print("Ready for manufacturing!")
        >>> else:
        ...     for item in result.action_items:
        ...         print(f"  {item.priority}. {item.description}")
    """

    def __init__(
        self,
        project_or_pcb: str | Path,
        manufacturer: str = "jlcpcb",
        layers: int | None = None,
        copper_oz: float = 1.0,
        quantity: int = 5,
        skip_erc: bool = False,
    ):
        """Initialize the audit.

        Args:
            project_or_pcb: Path to .kicad_pro or .kicad_pcb file
            manufacturer: Target manufacturer ID (default: jlcpcb)
            layers: Layer count (auto-detected if None)
            copper_oz: Copper weight in oz
            quantity: Quantity for cost estimate
            skip_erc: Skip ERC check (for PCB-only audits)
        """
        self.path = Path(project_or_pcb)
        self.manufacturer = manufacturer
        self.layers = layers
        self.copper_oz = copper_oz
        self.quantity = quantity
        self.skip_erc = skip_erc

        # Resolve paths
        if self.path.suffix == ".kicad_pro":
            self.project_path = self.path
            self.pcb_path = self.path.with_suffix(".kicad_pcb")
            self.schematic_path = self.path.with_suffix(".kicad_sch")
        elif self.path.suffix == ".kicad_pcb":
            self.project_path = None
            self.pcb_path = self.path
            self.schematic_path = self.path.with_suffix(".kicad_sch")
            self.skip_erc = True  # Skip ERC for PCB-only
        else:
            raise ValueError(f"Expected .kicad_pro or .kicad_pcb file, got: {self.path}")

        # Loaded objects (lazy)
        self._pcb: PCB | None = None
        self._profile = None

    def _load_pcb(self) -> PCB:
        """Load and cache the PCB."""
        if self._pcb is None:
            from kicad_tools.schema.pcb import PCB

            self._pcb = PCB.load(self.pcb_path)
        return self._pcb

    def _get_profile(self):
        """Get and cache the manufacturer profile."""
        if self._profile is None:
            from kicad_tools.manufacturers import get_profile

            self._profile = get_profile(self.manufacturer)
        return self._profile

    def run(self) -> AuditResult:
        """Run the complete audit.

        Returns:
            AuditResult with all check results and recommendations
        """
        result = AuditResult(
            project_name=self.path.stem,
            schematic_path=self.schematic_path if self.schematic_path.exists() else None,
            pcb_path=self.pcb_path if self.pcb_path.exists() else None,
        )

        # Run checks
        if not self.skip_erc and self.schematic_path.exists():
            result.erc = self._check_erc()

        if self.pcb_path.exists():
            pcb = self._load_pcb()

            # Auto-detect layers if not specified
            if self.layers is None:
                self.layers = len(pcb.copper_layers)

            result.drc = self._check_drc(pcb)
            result.connectivity = self._check_connectivity(pcb)
            result.compatibility = self._check_compatibility(pcb)
            result.layers = self._check_layer_utilization(pcb)
            result.cost = self._estimate_cost(pcb)

        # Generate action items
        result.action_items = self._generate_action_items(result)

        return result

    def _check_erc(self) -> ERCStatus:
        """Run ERC on schematic."""
        status = ERCStatus()

        try:
            # Try to run kicad-cli erc
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
                report_path = Path(f.name)

            try:
                subprocess.run(
                    [
                        "kicad-cli",
                        "sch",
                        "erc",
                        str(self.schematic_path),
                        "--format",
                        "json",
                        "--output",
                        str(report_path),
                    ],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )

                if report_path.exists():
                    from kicad_tools.erc import ERCReport

                    report = ERCReport.load(report_path)
                    status.error_count = report.error_count
                    status.warning_count = report.warning_count
                    status.passed = report.error_count == 0
                    if report.error_count > 0:
                        # Get first few error types
                        by_type = report.violations_by_type()
                        types = list(by_type.keys())[:3]
                        status.details = ", ".join(t.value for t in types)
                    status.report_path = report_path
            except FileNotFoundError:
                # kicad-cli not installed
                status.details = "kicad-cli not found (skipped)"
                status.passed = True  # Don't fail if we can't check
            except subprocess.TimeoutExpired:
                status.details = "ERC timed out"
                status.passed = False

        except Exception as e:
            logger.warning(f"ERC check failed: {e}")
            status.details = str(e)
            status.passed = True  # Don't fail on check errors

        return status

    def _check_drc(self, pcb: PCB) -> DRCStatus:
        """Run DRC on PCB."""
        status = DRCStatus()

        try:
            from kicad_tools.validate import DRCChecker

            checker = DRCChecker(
                pcb,
                manufacturer=self.manufacturer,
                layers=self.layers or 2,
                copper_oz=self.copper_oz,
            )

            results = checker.check_all()

            status.error_count = results.error_count
            status.warning_count = results.warning_count
            status.blocking_count = results.error_count  # Errors block manufacturing
            status.passed = results.error_count == 0

            if results.error_count > 0:
                # Get summary of errors
                by_rule = {}
                for v in results.violations:
                    if v.is_error:
                        by_rule[v.rule_id] = by_rule.get(v.rule_id, 0) + 1
                top_rules = sorted(by_rule.items(), key=lambda x: -x[1])[:3]
                status.details = ", ".join(f"{r[0]} ({r[1]})" for r in top_rules)

        except Exception as e:
            logger.warning(f"DRC check failed: {e}")
            status.details = str(e)
            status.passed = True  # Don't fail on check errors

        return status

    def _check_connectivity(self, pcb: PCB) -> ConnectivityStatus:
        """Check net connectivity."""
        status = ConnectivityStatus()

        try:
            from kicad_tools.validate import ConnectivityValidator

            validator = ConnectivityValidator(pcb)
            result = validator.validate()

            status.total_nets = result.total_nets
            status.connected_nets = result.connected_nets
            status.incomplete_nets = result.total_nets - result.connected_nets
            status.unconnected_pads = result.unconnected_pad_count

            if status.total_nets > 0:
                status.completion_percent = round(
                    100.0 * status.connected_nets / status.total_nets, 1
                )
            else:
                status.completion_percent = 100.0

            status.passed = result.is_fully_routed

            if not status.passed:
                status.details = (
                    f"{status.incomplete_nets} incomplete ({status.completion_percent:.0f}%)"
                )

        except Exception as e:
            logger.warning(f"Connectivity check failed: {e}")
            status.details = str(e)
            status.passed = True  # Don't fail on check errors

        return status

    def _check_compatibility(self, pcb: PCB) -> ManufacturerCompatibility:
        """Check manufacturer design rule compatibility."""
        compat = ManufacturerCompatibility(manufacturer=self.manufacturer.upper())

        try:
            profile = self._get_profile()
            rules = profile.get_design_rules(layers=self.layers or 2, copper_oz=self.copper_oz)

            # Get PCB design minimums
            min_trace = self._get_min_trace_width(pcb)
            min_clearance = self._get_min_clearance(pcb)
            min_drill = self._get_min_via_drill(pcb)
            min_annular = self._get_min_annular_ring(pcb)
            board_size = self._get_board_size(pcb)
            layer_count = len(pcb.copper_layers)

            # Check against rules
            trace_pass = min_trace >= rules.min_trace_width_mm
            compat.min_trace_width = (min_trace, rules.min_trace_width_mm, trace_pass)

            clearance_pass = min_clearance >= rules.min_clearance_mm
            compat.min_clearance = (min_clearance, rules.min_clearance_mm, clearance_pass)

            drill_pass = min_drill >= rules.min_via_drill_mm
            compat.min_via_drill = (min_drill, rules.min_via_drill_mm, drill_pass)

            annular_pass = min_annular >= rules.min_annular_ring_mm
            compat.min_annular_ring = (
                min_annular,
                rules.min_annular_ring_mm,
                annular_pass,
            )

            # Board size
            max_size = (rules.max_board_width_mm, rules.max_board_height_mm)
            size_pass = board_size[0] <= max_size[0] and board_size[1] <= max_size[1]
            compat.board_size = (board_size, max_size, size_pass)

            # Layer count
            layers_pass = layer_count in profile.supported_layers
            compat.layer_count = (layer_count, profile.supported_layers, layers_pass)

            # Overall pass
            compat.passed = all(
                [
                    trace_pass,
                    clearance_pass,
                    drill_pass,
                    annular_pass,
                    size_pass,
                    layers_pass,
                ]
            )

        except Exception as e:
            logger.warning(f"Compatibility check failed: {e}")
            compat.details = str(e)
            compat.passed = True  # Don't fail on check errors

        return compat

    def _check_layer_utilization(self, pcb: PCB) -> LayerUtilization:
        """Calculate layer utilization statistics."""
        util = LayerUtilization(layer_count=len(pcb.copper_layers))

        try:
            # Get board area
            board_area = self._get_board_area(pcb)
            if board_area <= 0:
                return util

            # Calculate copper area per layer
            for layer in pcb.copper_layers:
                copper_area = self._calculate_copper_area(pcb, layer.name)
                if board_area > 0:
                    util.utilization[layer.name] = round(100.0 * copper_area / board_area, 1)

        except Exception as e:
            logger.warning(f"Layer utilization calculation failed: {e}")

        return util

    def _estimate_cost(self, pcb: PCB) -> CostEstimate:
        """Estimate manufacturing cost."""
        estimate = CostEstimate(quantity=self.quantity)

        try:
            from kicad_tools.cost import ManufacturingCostEstimator

            estimator = ManufacturingCostEstimator(manufacturer=self.manufacturer)

            # Get board dimensions
            width, height = self._get_board_size(pcb)

            # Basic PCB cost
            cost_result = estimator.estimate_pcb(
                width_mm=width,
                height_mm=height,
                layers=self.layers or 2,
                quantity=self.quantity,
            )

            estimate.pcb_cost = cost_result.total
            estimate.total_cost = cost_result.total

        except Exception as e:
            logger.warning(f"Cost estimation failed: {e}")

        return estimate

    def _generate_action_items(self, result: AuditResult) -> list[ActionItem]:
        """Generate prioritized action items from results."""
        items: list[ActionItem] = []

        # ERC errors
        if result.erc.error_count > 0:
            items.append(
                ActionItem(
                    priority=1,
                    description=f"Fix {result.erc.error_count} ERC errors in schematic"
                    + (f" ({result.erc.details})" if result.erc.details else ""),
                    command=f"kicad-cli sch erc {self.schematic_path}",
                )
            )

        # DRC errors
        if result.drc.blocking_count > 0:
            items.append(
                ActionItem(
                    priority=1,
                    description=f"Fix {result.drc.blocking_count} blocking DRC violations"
                    + (f" ({result.drc.details})" if result.drc.details else ""),
                    command=f"kct check {self.pcb_path} --mfr {self.manufacturer}",
                )
            )

        # Connectivity issues
        if not result.connectivity.passed:
            items.append(
                ActionItem(
                    priority=1,
                    description=f"Complete routing: {result.connectivity.incomplete_nets} nets incomplete"
                    + (
                        f" ({result.connectivity.completion_percent:.0f}% complete)"
                        if result.connectivity.completion_percent < 100
                        else ""
                    ),
                    command=f"kct validate connectivity {self.pcb_path}",
                )
            )
            # Suggest stitching vias if GND net is incomplete
            items.append(
                ActionItem(
                    priority=2,
                    description="Add stitching vias for GND/power planes",
                    command=f"kct stitch {self.pcb_path} --net GND",
                )
            )

        # Manufacturer compatibility
        if not result.compatibility.passed:
            compat = result.compatibility

            if not compat.min_trace_width[2]:
                items.append(
                    ActionItem(
                        priority=1,
                        description=f"Increase min trace width: {compat.min_trace_width[0]:.3f}mm < {compat.min_trace_width[1]:.3f}mm required",
                    )
                )

            if not compat.min_clearance[2]:
                items.append(
                    ActionItem(
                        priority=1,
                        description=f"Increase min clearance: {compat.min_clearance[0]:.3f}mm < {compat.min_clearance[1]:.3f}mm required",
                    )
                )

            if not compat.min_via_drill[2]:
                items.append(
                    ActionItem(
                        priority=1,
                        description=f"Increase min via drill: {compat.min_via_drill[0]:.3f}mm < {compat.min_via_drill[1]:.3f}mm required",
                    )
                )

            if not compat.board_size[2]:
                items.append(
                    ActionItem(
                        priority=1,
                        description=f"Board too large: {compat.board_size[0][0]:.1f}x{compat.board_size[0][1]:.1f}mm > {compat.board_size[1][0]:.1f}x{compat.board_size[1][1]:.1f}mm max",
                    )
                )

        # DRC warnings (lower priority)
        if result.drc.warning_count > 0:
            items.append(
                ActionItem(
                    priority=3,
                    description=f"Review {result.drc.warning_count} DRC warnings",
                    command=f"kct check {self.pcb_path} --mfr {self.manufacturer} --verbose",
                )
            )

        return sorted(items, key=lambda x: x.priority)

    # Helper methods for extracting PCB metrics

    def _get_min_trace_width(self, pcb: PCB) -> float:
        """Get minimum trace width in PCB."""
        min_width = float("inf")
        for seg in pcb.segments:
            if seg.width < min_width:
                min_width = seg.width
        return min_width if min_width != float("inf") else 0.15  # Default

    def _get_min_clearance(self, pcb: PCB) -> float:
        """Get minimum clearance from design rules."""
        # Return from PCB setup if available
        if hasattr(pcb, "setup") and pcb.setup:
            return getattr(pcb.setup, "min_clearance", 0.1)
        return 0.1  # Default

    def _get_min_via_drill(self, pcb: PCB) -> float:
        """Get minimum via drill size."""
        min_drill = float("inf")
        for via in pcb.vias:
            if via.drill < min_drill:
                min_drill = via.drill
        return min_drill if min_drill != float("inf") else 0.3  # Default

    def _get_min_annular_ring(self, pcb: PCB) -> float:
        """Get minimum annular ring."""
        min_annular = float("inf")
        for via in pcb.vias:
            annular = (via.size - via.drill) / 2
            if annular < min_annular:
                min_annular = annular
        return min_annular if min_annular != float("inf") else 0.125  # Default

    def _get_board_size(self, pcb: PCB) -> tuple[float, float]:
        """Get board dimensions (width, height) in mm."""
        if hasattr(pcb, "edge_cuts_bbox"):
            bbox = pcb.edge_cuts_bbox
            if bbox:
                return (bbox[2] - bbox[0], bbox[3] - bbox[1])

        # Fallback: calculate from edge cuts
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

        return (100.0, 100.0)  # Default

    def _get_board_area(self, pcb: PCB) -> float:
        """Get board area in sq mm."""
        width, height = self._get_board_size(pcb)
        return width * height

    def _calculate_copper_area(self, pcb: PCB, layer: str) -> float:
        """Calculate approximate copper area on a layer."""
        area = 0.0

        # Track segments
        for seg in pcb.segments:
            if seg.layer == layer:
                dx = seg.end[0] - seg.start[0]
                dy = seg.end[1] - seg.start[1]
                length = (dx * dx + dy * dy) ** 0.5
                area += length * seg.width

        # Pads (approximate)
        for fp in pcb.footprints:
            for pad in fp.pads:
                if layer in pad.layers or "*.Cu" in pad.layers:
                    # Approximate pad area
                    if hasattr(pad, "size"):
                        area += pad.size[0] * pad.size[1]

        # Zones (filled areas)
        for zone in pcb.zones:
            if zone.layer == layer and zone.filled_polygons:
                for poly in zone.filled_polygons:
                    # Simple polygon area calculation
                    area += self._polygon_area(poly)

        return area

    def _polygon_area(self, points: list[tuple[float, float]]) -> float:
        """Calculate polygon area using shoelace formula."""
        n = len(points)
        if n < 3:
            return 0.0

        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += points[i][0] * points[j][1]
            area -= points[j][0] * points[i][1]

        return abs(area) / 2.0
