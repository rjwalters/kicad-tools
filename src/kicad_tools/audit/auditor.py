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
    blocking_error_count: int = 0  # Only electrical errors that block readiness
    passed: bool = True
    skipped: bool = False
    details: str = ""
    report_path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "blocking_error_count": self.blocking_error_count,
            "passed": self.passed,
            "skipped": self.skipped,
            "details": self.details,
        }


@dataclass
class DRCStatus:
    """DRC check results.

    ``geometric_drc_ran`` records whether the native ``kicad-cli pcb drc``
    engine actually executed and produced a report (issue #3817).  It is
    *distinct* from ``passed``: a board can be ``passed=True`` per the
    internal ``--mfr`` rule engine while the geometric engine did not run
    (kicad-cli absent / timed out / crashed), in which case the verdict is
    **not authoritative** -- the internal engine is structurally blind to
    several KiCad violation classes (shorts, ``copper_edge_clearance``,
    ``solder_mask_bridge``, ``silk_*`` overlaps).  Defaults to ``False`` so
    an un-reconciled status is never silently treated as authoritative.
    """

    error_count: int = 0
    warning_count: int = 0
    blocking_count: int = 0  # Violations that block manufacturing
    passed: bool = True
    details: str = ""
    report_path: Path | None = None
    violations_by_type: dict[str, int] = field(default_factory=dict)
    # True only when kicad-cli geometric DRC actually ran and produced a
    # report.  When False, a ``passed=True`` verdict is NOT authoritative.
    geometric_drc_ran: bool = False
    # Human-readable note describing why geometric DRC did not run (skip
    # path) -- mirrors GeometricDRCResult.note.  None when it ran cleanly.
    geometric_drc_note: str | None = None

    def to_dict(self) -> dict:
        result = {
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "blocking_count": self.blocking_count,
            "passed": self.passed,
            "details": self.details,
            "geometric_drc_ran": self.geometric_drc_ran,
            "geometric_drc_note": self.geometric_drc_note,
        }
        if self.violations_by_type:
            result["violations_by_type"] = dict(self.violations_by_type)
        return result


@dataclass
class SyncStatus:
    """Schematic <-> PCB synchronization check results.

    Captures all four axes of drift between schematic and PCB:
    - schematic_only_count: refs in schematic but missing from PCB
      (unbuildable BOM - hard fail).
    - pcb_only_count: refs on PCB but missing from schematic
      (orphan footprints - warning).
    - value_mismatch_count: same ref in both with different values.
    - footprint_mismatch_count: same ref with different footprints.

    See ``kicad_tools.sync.reconciler.SyncAnalysis`` for the underlying
    source of truth.
    """

    schematic_only_count: int = 0
    pcb_only_count: int = 0
    value_mismatch_count: int = 0
    footprint_mismatch_count: int = 0
    passed: bool = True
    skipped: bool = False
    details: str = ""
    # Up to ~10 example refs per axis (for surfacing in reports). Kept
    # small so JSON manifest does not balloon for boards with hundreds
    # of mismatches.
    schematic_only_refs: list[str] = field(default_factory=list)
    pcb_only_refs: list[str] = field(default_factory=list)
    value_mismatch_refs: list[str] = field(default_factory=list)
    footprint_mismatch_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schematic_only_count": self.schematic_only_count,
            "pcb_only_count": self.pcb_only_count,
            "value_mismatch_count": self.value_mismatch_count,
            "footprint_mismatch_count": self.footprint_mismatch_count,
            "passed": self.passed,
            "skipped": self.skipped,
            "details": self.details,
            "schematic_only_refs": list(self.schematic_only_refs),
            "pcb_only_refs": list(self.pcb_only_refs),
            "value_mismatch_refs": list(self.value_mismatch_refs),
            "footprint_mismatch_refs": list(self.footprint_mismatch_refs),
        }


@dataclass
class ConnectivityStatus:
    """Net connectivity check results."""

    total_nets: int = 0
    connected_nets: int = 0
    incomplete_nets: int = 0
    zone_connected_nets: int = 0
    pour_net_names: list[str] = field(default_factory=list)
    completion_percent: float = 100.0
    unconnected_pads: int = 0
    has_zones: bool = False
    passed: bool = True
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "total_nets": self.total_nets,
            "connected_nets": self.connected_nets,
            "incomplete_nets": self.incomplete_nets,
            "zone_connected_nets": self.zone_connected_nets,
            "pour_net_names": self.pour_net_names,
            "completion_percent": self.completion_percent,
            "unconnected_pads": self.unconnected_pads,
            "has_zones": self.has_zones,
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
    assembly_mode: str | None = None  # "none" when assembly is excluded

    def to_dict(self) -> dict:
        return {
            "pcb_cost": self.pcb_cost,
            "component_cost": self.component_cost,
            "assembly_cost": self.assembly_cost,
            "total_cost": self.total_cost,
            "quantity": self.quantity,
            "currency": self.currency,
            "assembly_mode": self.assembly_mode,
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
    sync: SyncStatus = field(default_factory=SyncStatus)
    connectivity: ConnectivityStatus = field(default_factory=ConnectivityStatus)
    compatibility: ManufacturerCompatibility = field(default_factory=ManufacturerCompatibility)
    layers: LayerUtilization = field(default_factory=LayerUtilization)
    cost: CostEstimate = field(default_factory=CostEstimate)

    # Action items
    action_items: list[ActionItem] = field(default_factory=list)

    @property
    def verdict(self) -> AuditVerdict:
        """Determine overall verdict based on check results."""
        # Critical failures — these always block READY
        if self.erc.blocking_error_count > 0:
            return AuditVerdict.NOT_READY
        if self.drc.blocking_count > 0:
            return AuditVerdict.NOT_READY
        if not self.compatibility.passed:
            return AuditVerdict.NOT_READY

        # Schematic refs missing from PCB == unbuildable BOM == hard fail.
        # The CPL/BOM that gets shipped to the fab will reference parts
        # that have no pads on the board, so the assembly cannot succeed.
        if self.sync.schematic_only_count > 0:
            return AuditVerdict.NOT_READY

        # Connectivity: advisory when core checks pass and board has zones.
        # Zone fills cannot be verified without running KiCad's zone filler,
        # so incomplete nets on a board with zone definitions are treated as
        # a warning rather than a hard block.
        if not self.connectivity.passed:
            if self.connectivity.has_zones:
                return AuditVerdict.WARNING
            return AuditVerdict.NOT_READY

        # Warnings
        if self.drc.warning_count > 0 or self.erc.warning_count > 0:
            return AuditVerdict.WARNING
        # Sync drift other than schematic-only: value/footprint mismatches
        # and PCB-only orphans are reviewable, not blocking.
        if (
            self.sync.value_mismatch_count > 0
            or self.sync.footprint_mismatch_count > 0
            or self.sync.pcb_only_count > 0
        ):
            return AuditVerdict.WARNING

        # Non-authoritative DRC gate (issue #3825): a board may be clean per
        # the internal --mfr rule engine (drc.passed, blocking_count == 0)
        # while the geometric engine (kicad-cli pcb drc) never actually ran
        # -- e.g. shapely ImportError or kicad-cli absent leaves
        # geometric_drc_ran=False.  #3820 fixed the DRC *line label* but the
        # overall verdict still returned READY, so kct audit / report.md
        # shipped "READY FOR MANUFACTURING" for a board whose DRC was never
        # authoritatively verified.  The internal engine is structurally
        # blind to several KiCad violation classes (shorts, copper-edge
        # clearance, mask bridges), so a clean-but-un-run DRC MUST NOT be
        # READY.  Downgrade to WARNING (not NOT_READY) so backend-less CI
        # does not regress to red, consistent with the #3820 policy.
        if self.drc.passed and not self.drc.geometric_drc_ran:
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
            "sync_schematic_only": self.sync.schematic_only_count,
            "sync_pcb_only": self.sync.pcb_only_count,
            "sync_value_mismatches": self.sync.value_mismatch_count,
            "sync_footprint_mismatches": self.sync.footprint_mismatch_count,
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
            "sync": self.sync.to_dict(),
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
        no_assembly: bool = False,
        pcb_override: Path | None = None,
        net_class_map_path: str | Path | None = None,
    ):
        """Initialize the audit.

        Args:
            project_or_pcb: Path to .kicad_pro or .kicad_pcb file
            manufacturer: Target manufacturer ID (default: jlcpcb)
            layers: Layer count (auto-detected if None)
            copper_oz: Copper weight in oz
            quantity: Quantity for cost estimate
            skip_erc: Skip ERC check (for PCB-only audits)
            no_assembly: Skip assembly cost calculation
            pcb_override: Explicit PCB path override (takes precedence over
                all auto-detection including project.kct)
            net_class_map_path: Optional path to a JSON sidecar mapping
                net names to ``NetClassRouting`` dicts (see
                :meth:`kicad_tools.router.rules.NetClassRouting.to_dict`).
                When supplied, the diff-pair routing-continuity and
                length-skew DRC rules can fire on routed boards.  Issue
                #2684 -- mirrors the ``kct check --net-class-map`` flag.
        """
        self.path = Path(project_or_pcb)
        self.manufacturer = manufacturer
        self.layers = layers
        self.copper_oz = copper_oz
        self.quantity = quantity
        self.skip_erc = skip_erc
        self.no_assembly = no_assembly
        self.net_class_map_path = (
            Path(net_class_map_path) if net_class_map_path is not None else None
        )

        # Resolve paths
        if self.path.suffix == ".kicad_pro":
            self.project_path = self.path
            if pcb_override is not None:
                pcb_override = Path(pcb_override)
                if not pcb_override.exists():
                    raise ValueError(f"PCB override file not found: {pcb_override}")
                logger.info(f"Using PCB override: {pcb_override}")
                self.pcb_path = pcb_override
            else:
                self.pcb_path = self._resolve_pcb_path()
            self.schematic_path = self._resolve_schematic_path()
        elif self.path.suffix == ".kicad_pcb":
            self.project_path = None
            self.pcb_path = self.path
            self.schematic_path = self.path.with_suffix(".kicad_sch")
            self.skip_erc = True  # Skip ERC for PCB-only
        else:
            raise ValueError(f"Expected .kicad_pro or .kicad_pcb file, got: {self.path}")

        # Read assembly mode from project.kct if not explicitly overridden
        self._assembly_mode: str | None = None
        if not self.no_assembly:
            self._assembly_mode = self._read_assembly_mode()
            if self._assembly_mode == "none":
                self.no_assembly = True

        # Loaded objects (lazy)
        self._pcb: PCB | None = None
        self._profile = None

    def _resolve_pcb_path(self) -> Path:
        """Resolve PCB path when given a project file.

        Resolution order:
        1. Check project.kct for artifacts.pcb
        2. When both base and *_routed exist, prefer the most recently modified
        3. Fall back to *_routed.kicad_pcb if only routed exists
        4. Default to <basename>.kicad_pcb
        """
        project_dir = self.path.parent
        basename = self.path.stem

        # 1. Try to find project.kct and use artifacts.pcb
        kct_path = project_dir / "project.kct"
        if not kct_path.exists():
            # Also check parent directory (for output/ subdirectory case)
            kct_path = project_dir.parent / "project.kct"

        if kct_path.exists():
            try:
                from kicad_tools.spec import load_spec

                spec = load_spec(kct_path)
                if spec.project and spec.project.artifacts and spec.project.artifacts.pcb:
                    # PCB path is relative to project.kct location
                    pcb_path = kct_path.parent / spec.project.artifacts.pcb
                    if pcb_path.exists():
                        logger.debug(f"Using PCB from project.kct: {pcb_path}")
                        return pcb_path
            except Exception as e:
                logger.debug(f"Failed to load project.kct: {e}")

        # 2. When both base and routed exist, prefer the most recently modified
        base_path = project_dir / f"{basename}.kicad_pcb"
        routed_path = project_dir / f"{basename}_routed.kicad_pcb"

        if base_path.exists() and routed_path.exists():
            if base_path.stat().st_mtime >= routed_path.stat().st_mtime:
                logger.info(f"Using base PCB (newer): {base_path}")
                return base_path
            else:
                logger.info(f"Using routed PCB (newer): {routed_path}")
                return routed_path
        elif routed_path.exists():
            logger.info(f"Using routed PCB: {routed_path}")
            return routed_path

        # 3. Default to <basename>.kicad_pcb
        return self.path.with_suffix(".kicad_pcb")

    def _resolve_schematic_path(self) -> Path:
        """Resolve schematic path when given a project file.

        Resolution order:
        1. Check project.kct for artifacts.schematic
        2. Default to <basename>.kicad_sch

        Delegates the project.kct + sibling lookup to the shared
        :func:`kicad_tools.sync.discover.resolve_schematic_for_pcb` helper so
        ``kct audit``, ``kct route``, and ``kct check`` agree on where the
        schematic lives.  When the helper finds nothing, falls back to the
        historical project-basename default so existing behaviour is preserved.
        """
        from kicad_tools.sync.discover import resolve_schematic_for_pcb

        resolved = resolve_schematic_for_pcb(self.pcb_path)
        if resolved is not None:
            return resolved

        # Fall back to <project-basename>.kicad_sch (historical default).
        return self.path.with_suffix(".kicad_sch")

    def _read_assembly_mode(self) -> str | None:
        """Read manufacturing.assembly from project.kct if available.

        Returns the assembly mode string (e.g., "smt", "none") or None
        if no project.kct exists or the field is not set.
        """
        project_dir = self.path.parent
        kct_path = project_dir / "project.kct"
        if not kct_path.exists():
            kct_path = project_dir.parent / "project.kct"

        if kct_path.exists():
            try:
                from kicad_tools.spec import load_spec

                spec = load_spec(kct_path)
                if (
                    spec.requirements
                    and spec.requirements.manufacturing
                    and spec.requirements.manufacturing.assembly
                ):
                    mode = spec.requirements.manufacturing.assembly
                    logger.debug(f"Assembly mode from project.kct: {mode}")
                    return mode
            except Exception as e:
                logger.debug(f"Failed to read assembly mode from project.kct: {e}")

        return None

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
        else:
            result.erc.skipped = True
            if self.skip_erc:
                result.erc.details = "ERC skipped by user request"
            else:
                result.erc.details = "ERC skipped (no schematic provided)"

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

        # Run schematic <-> PCB sync drift check before generating action
        # items so the action-item generator can read result.sync and emit
        # sync-related items with correct priorities.
        if self.pcb_path.exists() and self.schematic_path.exists():
            result.sync = self._check_sync_drift()
        else:
            result.sync.skipped = True
            if not self.schematic_path.exists():
                result.sync.details = "Sync check skipped (no schematic available)"
            else:
                result.sync.details = "Sync check skipped (no PCB available)"

        # Generate action items from check results
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
                    from kicad_tools.erc.violation import (
                        ERC_BLOCKING_TYPES,
                        ERC_NON_BLOCKING_TYPES,
                    )

                    report = ERCReport.load(report_path)
                    status.error_count = report.error_count  # raw total for reporting

                    # Split errors by blocking vs non-blocking type
                    blocking = [v for v in report.errors if v.type in ERC_BLOCKING_TYPES]
                    non_blocking = [v for v in report.errors if v.type in ERC_NON_BLOCKING_TYPES]
                    unknown_errors = [
                        v
                        for v in report.errors
                        if v.type not in ERC_BLOCKING_TYPES and v.type not in ERC_NON_BLOCKING_TYPES
                    ]

                    # Unknown error types default to blocking (conservative)
                    status.blocking_error_count = len(blocking) + len(unknown_errors)
                    # Demote non-blocking errors to warnings
                    status.warning_count = report.warning_count + len(non_blocking)
                    status.passed = status.blocking_error_count == 0

                    if status.blocking_error_count > 0:
                        # Get first few blocking error types for the details string
                        by_type = report.violations_by_type()
                        blocking_types = [
                            t
                            for t in by_type
                            if t in ERC_BLOCKING_TYPES or t not in ERC_NON_BLOCKING_TYPES
                        ][:3]
                        status.details = ", ".join(t.value for t in blocking_types)
                    status.report_path = report_path
            except FileNotFoundError:
                # kicad-cli not installed
                status.details = "kicad-cli not found (skipped)"
                status.passed = True  # Don't fail if we can't check
            except subprocess.TimeoutExpired:
                status.details = "ERC timed out"
                status.passed = False

        except Exception as e:
            # A genuine ERC checker crash must NOT be coerced into a clean
            # PASS (issue #3825, sibling of the #3817/#3820 DRC fix).  Surface
            # it as a fail-loud could-not-verify result so the verdict gate
            # (which reads blocking_error_count) cannot return a false READY.
            logger.warning(f"ERC check failed: {e}")
            status.details = f"ERC check could not run: {e}"
            status.passed = False
            status.error_count = max(status.error_count, 1)
            status.blocking_error_count = max(status.blocking_error_count, 1)

        return status

    def _check_drc(self, pcb: PCB) -> DRCStatus:
        """Run DRC on PCB."""
        status = DRCStatus()

        try:
            import json as _json

            from kicad_tools.router.rules import net_class_map_from_dict
            from kicad_tools.validate import DRCChecker

            # Load optional net-class-map sidecar (Issue #2684).  When
            # supplied, enables the diff-pair routing-continuity and
            # length-skew rules to re-derive engagement / skew state on
            # routed boards.  When absent, rules degrade to no-ops.
            net_class_map = None
            if self.net_class_map_path is not None:
                try:
                    ncm_data = _json.loads(self.net_class_map_path.read_text())
                    net_class_map = net_class_map_from_dict(ncm_data)
                except (OSError, _json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.warning(
                        "Failed to load net-class-map from %s: %s",
                        self.net_class_map_path,
                        e,
                    )

            checker = DRCChecker(
                pcb,
                manufacturer=self.manufacturer,
                layers=self.layers or 2,
                copper_oz=self.copper_oz,
                net_class_map=net_class_map,
            )

            # Match the ``kct check`` CLI's pad_grid tolerance policy
            # (auto-derive per-board, issue #3061) so the audit -- and the
            # manufacturing report's DRC section built from it -- cannot
            # disagree with the documented ``kct check --mfr <profile>``
            # gate (issue #3497).
            results = checker.check_all(pad_grid_auto_derive=True)

            # Some rules (e.g. ``connectivity`` from Issue #3041) report
            # at error severity for the standalone ``kct check`` CLI but
            # must NOT count toward manufacturability-blocking verdicts
            # because a sibling status field already classifies the same
            # defect with finer-grained logic.  For ``connectivity`` the
            # audit's own :meth:`_check_connectivity` step distinguishes
            # zone-bridged incomplete nets (advisory WARNING) from
            # genuinely-unrouted nets (blocking NOT_READY) based on zone
            # presence -- a board with zones may resolve the nominal gap
            # via zone fill on KiCad re-open.
            #
            # Issue #3044 lifted the per-call-site hardcoded
            # ``rule_id == "connectivity"`` filter (originally introduced
            # by PR #3060) into a central
            # :attr:`DRCChecker.ADVISORY_RULE_IDS` classifier so every
            # entry point can filter by severity instead of by literal
            # rule_id.  All non-advisory rules still drive the blocking
            # tally; advisory rules surface in ``violations_by_type`` for
            # introspection but never block.
            non_advisory_violations = [
                v for v in results.violations if not DRCChecker.is_advisory_rule(v.rule_id)
            ]
            non_advisory_errors = sum(1 for v in non_advisory_violations if v.is_error)
            non_advisory_warnings = sum(1 for v in non_advisory_violations if v.is_warning)

            status.error_count = non_advisory_errors
            status.warning_count = non_advisory_warnings
            status.blocking_count = non_advisory_errors  # Errors block manufacturing
            status.passed = non_advisory_errors == 0

            # Build per-type violation breakdown (all severities).  We
            # include advisory rule_ids in the breakdown so consumers can
            # still introspect that the rules ran -- they just do not
            # count toward the blocking verdict.
            by_rule: dict[str, int] = {}
            for v in results.violations:
                by_rule[v.rule_id] = by_rule.get(v.rule_id, 0) + 1
            status.violations_by_type = by_rule

            if non_advisory_errors > 0:
                # Get summary of errors for the details string
                error_rules: dict[str, int] = {}
                for v in non_advisory_violations:
                    if v.is_error:
                        error_rules[v.rule_id] = error_rules.get(v.rule_id, 0) + 1
                top_rules = sorted(error_rules.items(), key=lambda x: -x[1])[:3]
                status.details = ", ".join(f"{r[0]} ({r[1]})" for r in top_rules)

        except ImportError as e:
            # An optional geometry backend (e.g. shapely) is not installed,
            # so the internal rule engine could not run.  This is the same
            # class of "could not verify" as kicad-cli being absent: per
            # issue #3817 we must NOT report it as a hard FAIL (that would
            # regress backend-less CI), but it is equally NOT an
            # authoritative clean PASS.  Leave status.passed untouched and
            # let _merge_geometric_drc / audit_cmd render the verdict as
            # non-authoritative (geometric_drc_ran stays False).
            logger.warning(f"DRC check could not run (optional backend absent): {e}")
            note = f"internal DRC engine could not run: {e}"
            status.details = f"{status.details}; {note}" if status.details else note

        except Exception as e:
            # A genuine internal DRC checker crash must NOT be reported as a
            # clean PASS (issue #3817).  Surface it as a could-not-verify
            # failure so the verdict is fail-loud rather than a false green.
            logger.warning(f"DRC check failed: {e}")
            status.details = f"DRC check could not run: {e}"
            status.passed = False
            status.error_count = max(status.error_count, 1)
            status.blocking_count = max(status.blocking_count, 1)

        # Merge KiCad's geometric DRC (kicad-cli pcb drc) into the same
        # status.  The two engines catch different real defects: the --mfr
        # rule engine above catches manufacturer-capability violations
        # (e.g. clearance_pad_zone), while kicad-cli catches geometric
        # defects (starved_thermal, shorts, sub-spec traces, zones_intersect)
        # that the rule engine does not.  A board is manufacturable only
        # when BOTH report zero errors (issue #3721).
        self._merge_geometric_drc(status)

        return status

    def _merge_geometric_drc(self, status: DRCStatus) -> None:
        """Run ``kicad-cli pcb drc`` and fold its errors into ``status``.

        kicad-cli loads the sibling ``<board>.kicad_pro`` emitted by
        ``kct export`` (issue #3720), so a ``--severity-error`` run checks
        against the manufacturer's fab-accurate rules with
        ``lib_footprint_mismatch`` / ``isolated_copper`` already downgraded
        below error severity (they will not appear and so are not counted
        as blocking).

        Counts are *additive* on top of the ``--mfr`` engine's tally:
        ``error_count`` / ``blocking_count`` grow by the kicad-cli error
        count, and each kicad-cli violation type is recorded in
        ``violations_by_type`` under a ``kicad-cli:`` namespace so it is
        clearly attributed to the geometric engine and never collides with
        a ``--mfr`` rule_id.

        Graceful fallback: if kicad-cli is not on PATH (e.g. CI without
        KiCad), the report still generates -- a note is appended to
        ``status.details`` and only the ``--mfr`` count stands.  The
        ``status`` is mutated in place; this method never raises.

        The kicad-cli invocation/parsing is delegated to the shared
        :func:`kicad_tools.drc.run_geometric_drc` helper so that this
        path and the ``kct route`` post-route gate cannot drift (issue
        #3803).
        """
        from kicad_tools.drc import run_geometric_drc

        result = run_geometric_drc(self.pcb_path)

        if not result.ran:
            # kicad-cli absent / timed out / no report / crashed: the
            # geometric engine did NOT run, so a passing --mfr verdict is
            # not authoritative (issue #3817).  Record that fact distinctly
            # from passed -- do NOT flip status.passed here -- and surface
            # the note so audit_cmd.py can render a third verdict state.
            status.geometric_drc_ran = False
            note = result.note
            status.geometric_drc_note = note
            if note == "kicad-cli not found; geometric DRC skipped":
                # Preserve the audit-specific suffix for backward-compat.
                note = "kicad-cli not found; geometric DRC skipped (--mfr count only)"
            if note:
                status.details = f"{status.details}; {note}" if status.details else note
            return

        # The geometric engine ran -- the verdict is now authoritative.
        status.geometric_drc_ran = True

        if result.error_count > 0:
            status.error_count += result.error_count
            status.blocking_count += result.error_count
            status.passed = status.blocking_count == 0

            # Record per-type counts under a kicad-cli namespace so the
            # report's by-type table makes the engine explicit and the
            # geometric types never collide with --mfr rule_ids.
            by_type = status.violations_by_type
            for type_str, count in result.by_type.items():
                key = f"kicad-cli:{type_str}"
                by_type[key] = by_type.get(key, 0) + count
            status.violations_by_type = by_type

            top = result.top_types(3)
            cli_detail = "kicad-cli: " + ", ".join(f"{t} ({c})" for t, c in top)
            status.details = f"{status.details}; {cli_detail}" if status.details else cli_detail

    def _check_connectivity(self, pcb: PCB) -> ConnectivityStatus:
        """Check net connectivity.

        Includes a corruption guard: if the board has footprints but every
        pad is assigned to net 0 (unconnected), this indicates that inline
        net assignments were stripped (e.g. by kicad-cli).  In that case
        the check is marked as failed with a corruption diagnostic.
        """
        status = ConnectivityStatus()

        try:
            # Corruption guard: detect boards where all pad net assignments
            # have been zeroed out.  A board with footprints must have at
            # least some pads with non-zero net numbers.
            if pcb.footprint_count > 0:
                total_pads = 0
                pads_with_net = 0
                for fp in pcb.footprints:
                    for pad in fp.pads:
                        total_pads += 1
                        if pad.net_number != 0:
                            pads_with_net += 1

                if total_pads > 0 and pads_with_net == 0:
                    logger.warning(
                        "Data corruption detected: %d pads on %d footprints "
                        "but all pads assigned to net 0",
                        total_pads,
                        pcb.footprint_count,
                    )
                    status.passed = False
                    status.details = (
                        f"Possible data corruption: {total_pads} pads across "
                        f"{pcb.footprint_count} footprints all assigned to net 0"
                    )
                    return status

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

            # Record whether the board has zone definitions.  This is used
            # by the verdict logic to decide whether incomplete connectivity
            # should block the READY verdict (no zones) or be advisory (has
            # zones -- fills may resolve the gaps).
            status.has_zones = any(z.net_number > 0 for z in pcb.zones)

            # The ConnectivityValidator now performs geometric zone boundary
            # containment checks.  Nets where all pads fall inside zone
            # boundary polygons on matching layers are already counted as
            # connected in result.connected_nets and reported via
            # result.zone_connected_nets.  These are high-confidence
            # verified connections.
            geometrically_verified = result.zone_connected_nets

            # Post-process: identify zone-connected nets among incomplete nets.
            # Nets that still appear incomplete but have a zone definition are
            # reclassified as zone-connected (name-based, lower confidence)
            # and excluded from the pass/fail evaluation.
            if not result.is_fully_routed:
                zone_net_names = {z.net_name for z in pcb.zones if z.net_number > 0}
                error_net_names = {issue.net_name for issue in result.errors}
                zone_connected = error_net_names & zone_net_names
                truly_incomplete = error_net_names - zone_connected

                # Second pass: classify remaining incomplete nets as pour nets.
                # Nets with is_pour_net=True (power/ground) are expected to be
                # zone-filled even if no zone definition exists yet.
                classified_pour: set[str] = set()
                try:
                    from kicad_tools.router.net_class import classify_and_apply_rules

                    net_id_by_name = {
                        net.name: net_id for net_id, net in pcb.nets.items() if net_id > 0
                    }
                    pending_ids = {
                        net_id_by_name[n]: n for n in truly_incomplete if n in net_id_by_name
                    }
                    if pending_ids:
                        rules = classify_and_apply_rules(pending_ids)
                        classified_pour = {
                            n for n in truly_incomplete if rules.get(n) and rules[n].is_pour_net
                        }
                except Exception:
                    pass  # conservative: leave truly_incomplete unchanged

                if classified_pour:
                    truly_incomplete = truly_incomplete - classified_pour
                    zone_connected = zone_connected | classified_pour
                    status.pour_net_names = sorted(classified_pour)

                status.zone_connected_nets = len(zone_connected) + geometrically_verified
                status.incomplete_nets = len(truly_incomplete)
                status.passed = len(truly_incomplete) == 0

                if truly_incomplete:
                    status.details = (
                        f"{len(truly_incomplete)} incomplete"
                        f" ({status.completion_percent:.0f}% routed)"
                    )
                    if zone_connected:
                        status.details += (
                            f", {len(zone_connected)} connected via zone fill (unverified)"
                        )
                elif zone_connected:
                    status.details = (
                        f"{len(zone_connected)} nets connected via zone fill (verified)"
                    )
            else:
                status.passed = True
                if geometrically_verified > 0:
                    status.zone_connected_nets = geometrically_verified
                    status.details = (
                        f"{geometrically_verified} nets connected via zone fill (verified)"
                    )

        except Exception as e:
            # A genuine connectivity checker crash must NOT be coerced into a
            # clean PASS (issue #3825).  Mark it fail-loud as could-not-verify
            # so the verdict cannot silently return READY when net
            # connectivity was never actually checked.
            logger.warning(f"Connectivity check failed: {e}")
            status.details = f"Connectivity check could not run: {e}"
            status.passed = False

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
            # A genuine compatibility checker crash must NOT be coerced into a
            # clean PASS (issue #3825).  compat.passed feeds the verdict gate
            # directly (NOT_READY when False), so a false PASS here is exactly
            # the false-READY anti-pattern.  Fail loud as could-not-verify.
            logger.warning(f"Compatibility check failed: {e}")
            compat.details = f"Compatibility check could not run: {e}"
            compat.passed = False

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
        """Estimate manufacturing cost including components and assembly.

        Uses the full ``ManufacturingCostEstimator.estimate()`` method which
        derives a synthetic BOM from PCB footprints when no schematic BOM is
        available, providing component and assembly cost breakdowns alongside
        PCB fabrication cost.

        When ``self.no_assembly`` is True (from ``--no-assembly`` CLI flag or
        ``manufacturing.assembly: "none"`` in project.kct), assembly cost is
        excluded from the estimate.
        """
        assembly_mode = "none" if self.no_assembly else self._assembly_mode
        estimate = CostEstimate(quantity=self.quantity, assembly_mode=assembly_mode)

        try:
            from kicad_tools.cost import ManufacturingCostEstimator

            estimator = ManufacturingCostEstimator(manufacturer=self.manufacturer)

            # Use the full estimator which handles PCB dimensions, component
            # costs (from footprint-derived BOM), and assembly costs.
            full_result = estimator.estimate(
                pcb=pcb,
                quantity=self.quantity,
            )

            estimate.pcb_cost = full_result.pcb.total_cost
            estimate.component_cost = full_result.component_cost_per_unit * full_result.quantity

            if self.no_assembly:
                estimate.assembly_cost = 0.0
                # Total excludes assembly cost
                estimate.total_cost = (
                    full_result.total_for_quantity - full_result.assembly.total_cost
                )
            else:
                estimate.assembly_cost = full_result.assembly.total_cost
                estimate.total_cost = full_result.total_for_quantity

        except Exception as e:
            logger.warning(f"Cost estimation failed: {e}")

        return estimate

    def _check_sync_drift(self) -> SyncStatus:
        """Run schematic <-> PCB sync analysis via the Reconciler.

        Reuses :class:`kicad_tools.sync.reconciler.Reconciler` rather than
        re-implementing set logic so ``kct audit`` and ``kct sync --analyze``
        always agree on the four sync axes (schematic-only, PCB-only, value
        mismatch, footprint mismatch).

        Returns:
            SyncStatus populated with the four counts, example refs (up to
            10 per axis), and a human-readable details string.
        """
        status = SyncStatus()

        try:
            from kicad_tools.sync.reconciler import Reconciler

            reconciler = Reconciler(
                schematic=self.schematic_path,
                pcb=self.pcb_path,
            )
            analysis = reconciler.analyze()

            status.schematic_only_count = len(analysis.schematic_orphans)
            status.pcb_only_count = len(analysis.pcb_orphans)
            status.value_mismatch_count = len(analysis.value_mismatches)
            status.footprint_mismatch_count = len(analysis.footprint_mismatches)

            status.schematic_only_refs = list(analysis.schematic_orphans[:10])
            status.pcb_only_refs = list(analysis.pcb_orphans[:10])
            status.value_mismatch_refs = [m["reference"] for m in analysis.value_mismatches[:10]]
            status.footprint_mismatch_refs = [
                m["reference"] for m in analysis.footprint_mismatches[:10]
            ]

            # passed == every axis clean
            status.passed = (
                status.schematic_only_count == 0
                and status.pcb_only_count == 0
                and status.value_mismatch_count == 0
                and status.footprint_mismatch_count == 0
            )

            parts: list[str] = []
            if status.schematic_only_count:
                parts.append(f"{status.schematic_only_count} schematic-only")
            if status.pcb_only_count:
                parts.append(f"{status.pcb_only_count} PCB-only")
            if status.value_mismatch_count:
                parts.append(f"{status.value_mismatch_count} value mismatch(es)")
            if status.footprint_mismatch_count:
                parts.append(f"{status.footprint_mismatch_count} footprint mismatch(es)")
            if parts:
                status.details = "; ".join(parts)
            else:
                status.details = "Schematic and PCB are in sync"

        except Exception as e:
            logger.debug(f"Sync drift check skipped: {e}")
            status.skipped = True
            status.details = f"Sync check failed: {e}"

        return status

    def _check_orphaned_footprints(self) -> list[ActionItem]:
        """Legacy: orphan-footprint-only check kept for backward compatibility.

        New code should consult :meth:`_check_sync_drift` which surfaces
        all four sync axes.  This method still works against the schematic
        BOM directly so existing tests continue to pass.
        """
        items: list[ActionItem] = []

        try:
            from kicad_tools.schema.bom import extract_bom

            pcb = self._load_pcb()
            bom = extract_bom(str(self.schematic_path))

            # BOM references: non-virtual items (includes DNP since they
            # should still have footprints on the PCB)
            bom_refs = {item.reference for item in bom.items if not item.is_virtual}

            # PCB footprint references
            pcb_refs = {fp.reference for fp in pcb.footprints}

            orphaned = pcb_refs - bom_refs
            if orphaned:
                refs = ", ".join(sorted(orphaned)[:10])
                suffix = f" (and {len(orphaned) - 10} more)" if len(orphaned) > 10 else ""
                items.append(
                    ActionItem(
                        priority=2,
                        description=(
                            f"{len(orphaned)} orphaned footprint(s) on PCB "
                            f"but not in schematic: {refs}{suffix}"
                        ),
                    )
                )

        except Exception as e:
            logger.debug(f"Orphaned footprint check skipped: {e}")

        return items

    def _generate_action_items(self, result: AuditResult) -> list[ActionItem]:
        """Generate prioritized action items from results."""
        items: list[ActionItem] = []

        # Schematic <-> PCB sync drift.  Schematic-only refs are a hard
        # fail (priority 1) because they produce an unbuildable BOM: the
        # CPL/BOM shipped to the fab references parts with no pads on the
        # board.  Value/footprint mismatches and PCB-only orphans are
        # priority 2 (important).
        sync = result.sync
        if sync.schematic_only_count > 0:
            refs = ", ".join(sync.schematic_only_refs[:10])
            suffix = (
                f" (and {sync.schematic_only_count - 10} more)"
                if sync.schematic_only_count > 10
                else ""
            )
            items.append(
                ActionItem(
                    priority=1,
                    description=(
                        f"{sync.schematic_only_count} component(s) in schematic "
                        f"missing from PCB -- BOM will be unbuildable: {refs}{suffix}"
                    ),
                    command=f"kct sync --analyze {self.pcb_path}",
                )
            )

        if sync.pcb_only_count > 0:
            refs = ", ".join(sync.pcb_only_refs[:10])
            suffix = f" (and {sync.pcb_only_count - 10} more)" if sync.pcb_only_count > 10 else ""
            items.append(
                ActionItem(
                    priority=2,
                    description=(
                        f"{sync.pcb_only_count} orphaned footprint(s) on PCB "
                        f"but not in schematic: {refs}{suffix}"
                    ),
                    command=f"kct sync --analyze {self.pcb_path}",
                )
            )

        if sync.value_mismatch_count > 0:
            refs = ", ".join(sync.value_mismatch_refs[:10])
            suffix = (
                f" (and {sync.value_mismatch_count - 10} more)"
                if sync.value_mismatch_count > 10
                else ""
            )
            items.append(
                ActionItem(
                    priority=2,
                    description=(
                        f"{sync.value_mismatch_count} component(s) have different "
                        f"values in schematic vs PCB: {refs}{suffix}"
                    ),
                    command=f"kct sync --analyze {self.pcb_path}",
                )
            )

        if sync.footprint_mismatch_count > 0:
            refs = ", ".join(sync.footprint_mismatch_refs[:10])
            suffix = (
                f" (and {sync.footprint_mismatch_count - 10} more)"
                if sync.footprint_mismatch_count > 10
                else ""
            )
            items.append(
                ActionItem(
                    priority=2,
                    description=(
                        f"{sync.footprint_mismatch_count} component(s) have different "
                        f"footprints in schematic vs PCB: {refs}{suffix}"
                    ),
                    command=f"kct sync --analyze {self.pcb_path}",
                )
            )

        # Blocking ERC errors (electrical issues)
        if result.erc.blocking_error_count > 0:
            items.append(
                ActionItem(
                    priority=1,
                    description=f"Fix {result.erc.blocking_error_count} blocking ERC errors in schematic"
                    + (f" ({result.erc.details})" if result.erc.details else ""),
                    command=f"kicad-cli sch erc {self.schematic_path}",
                )
            )

        # Non-blocking ERC errors (demoted to warnings)
        non_blocking_count = result.erc.error_count - result.erc.blocking_error_count
        if non_blocking_count > 0:
            items.append(
                ActionItem(
                    priority=3,
                    description=f"Review {non_blocking_count} non-blocking ERC warnings (library/footprint checks)",
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
            if result.connectivity.has_zones:
                # Board has zone definitions — connectivity is advisory.
                # Zone fills may resolve the incomplete nets once refilled
                # in KiCad.
                items.append(
                    ActionItem(
                        priority=3,
                        description=(
                            f"Verify zone fill in KiCad: {result.connectivity.incomplete_nets}"
                            " nets appear incomplete but may be connected via zone fills"
                        ),
                        command=None,
                    )
                )
            else:
                # No zones — incomplete nets are a hard failure.
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

        # Zone-connected nets advisory (even when connectivity passes)
        if result.connectivity.zone_connected_nets > 0:
            items.append(
                ActionItem(
                    priority=3,
                    description=(
                        f"Verify zone fill in KiCad for"
                        f" {result.connectivity.zone_connected_nets} zone-connected nets"
                    ),
                    command=None,
                )
            )

        # Pour nets without zone definitions advisory
        if result.connectivity.pour_net_names:
            for net_name in result.connectivity.pour_net_names:
                items.append(
                    ActionItem(
                        priority=3,
                        description=(f"Add zone for {net_name} on appropriate copper layer"),
                        command=None,
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

        # Analog component advisory
        try:
            pcb = self._load_pcb()
            from kicad_tools.analysis.analog_detect import detect_analog_components

            analog = detect_analog_components(pcb)
            for component in analog:
                # Name each detected component (reference + value + reason)
                # so the engineer knows WHICH parts need analog layout care,
                # not just how many. Detection logic itself is unchanged --
                # this only surfaces the detail the detector already produced.
                ref = component.reference or "?"
                value = component.value.strip()
                ref_value = f"{ref} ({value})" if value else ref
                items.append(
                    ActionItem(
                        priority=3,
                        description=(
                            f"Analog-sensitive: {ref_value} — {component.reason}"
                            "; manual layout review recommended"
                        ),
                        command=None,
                    )
                )
        except Exception:
            logger.debug("Analog component detection skipped", exc_info=True)

        # Analog net advisory (Phase 2, issue #3170).
        #
        # Mirror the per-component block above at the NET level: name each
        # detected analog net (audio / analog supply / analog ground /
        # analog signal) so the engineer knows WHICH nets need analog layout
        # care, and flag an isolated analog ground whose discrete bridge
        # (ferrite / net-tie) to digital ground is missing.  Like the
        # component block, this degrades gracefully -- a detection failure is
        # logged at debug and never raises.
        try:
            pcb = self._load_pcb()
            from kicad_tools.analysis.analog_detect import (
                check_analog_ground_bridge,
                detect_analog_nets,
            )

            for net in detect_analog_nets(pcb):
                items.append(
                    ActionItem(
                        priority=3,
                        description=f"Analog net: {net.name} — {net.reason}",
                        command=None,
                    )
                )

            for warning in check_analog_ground_bridge(pcb):
                items.append(
                    ActionItem(
                        priority=3,
                        description=f"Audit: {warning}",
                        command=None,
                    )
                )
        except Exception:
            logger.debug("Analog net detection skipped", exc_info=True)

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
        """Get minimum clearance from design rules.

        Returns the minimum clearance from PCB setup, or a JLCPCB-compliant
        default of 0.2mm (well above 0.127mm minimum) when not available.
        """
        # Return from PCB setup if available
        if hasattr(pcb, "setup") and pcb.setup:
            return getattr(pcb.setup, "min_clearance", 0.2)
        return 0.2  # Default - JLCPCB min is 0.127mm

    def _get_min_via_drill(self, pcb: PCB) -> float:
        """Get minimum *through/blind* via drill size.

        Micro vias are excluded: they are laser-drilled and carry their own
        (much smaller) drill floor — e.g. jlcpcb-tier1 allows 0.10 mm
        microvia drills while requiring 0.20 mm mechanical via drills, and
        the exported ``.kicad_pro`` models them as separate
        ``min_via_hole`` / ``min_microvia_drill`` constraints.  Lumping
        microvias into this minimum compared a legal 0.15 mm in-pad rescue
        microvia against the 0.20 mm mechanical limit and produced a false
        "Increase min via drill" CRITICAL action item (and a NOT_READY
        verdict) on a board kicad-cli DRC passes at the same profile.
        """
        min_drill = float("inf")
        for via in pcb.vias:
            if getattr(via, "via_type", None) == "micro":
                continue
            if via.drill < min_drill:
                min_drill = via.drill
        return min_drill if min_drill != float("inf") else 0.3  # Default

    def _get_min_annular_ring(self, pcb: PCB) -> float:
        """Get minimum *through/blind* via annular ring.

        Returns the minimum annular ring from actual vias, or a JLCPCB-compliant
        default of 0.15mm (exactly at JLCPCB 2-layer minimum) when no vias exist.

        Micro vias are excluded for the same reason as in
        :meth:`_get_min_via_drill`: laser microvias carry their own annular
        floor (the tier-1 project generator emits ``min_via_annular_width``
        0.05 mm, which kicad-cli enforces), so comparing a legal 0.075 mm
        microvia annular against the 0.10 mm mechanical-via limit
        manufactured a false compatibility failure / NOT_READY verdict.
        """
        min_annular = float("inf")
        for via in pcb.vias:
            if getattr(via, "via_type", None) == "micro":
                continue
            annular = (via.size - via.drill) / 2
            if annular < min_annular:
                min_annular = annular
        return min_annular if min_annular != float("inf") else 0.15  # Default - JLCPCB min

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
