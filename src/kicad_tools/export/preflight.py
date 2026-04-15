"""
Pre-flight validation checklist for manufacturing export.

Validates PCB design readiness before generating manufacturing packages.
Each check returns OK/WARN/FAIL status. Any FAIL blocks the export.

Example::

    checker = PreflightChecker(
        pcb_path="board.kicad_pcb",
        schematic_path="board.kicad_sch",
        manufacturer="jlcpcb",
    )
    results = checker.run_all()
    for r in results:
        print(f"[{r.status}] {r.name}: {r.message}")

    if checker.has_failures(results):
        print("Export blocked!")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    """Result of a single pre-flight check."""

    name: str
    status: Literal["OK", "WARN", "FAIL"]
    message: str
    details: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d: dict = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.details:
            d["details"] = self.details
        return d


@dataclass
class PreflightConfig:
    """Configuration for which pre-flight checks to run."""

    skip_all: bool = False
    skip_drc: bool = False
    skip_erc: bool = False

    # Paths to pre-existing report files (avoids re-running kicad-cli)
    drc_report_path: str | Path | None = None
    erc_report_path: str | Path | None = None


class PreflightChecker:
    """
    Run pre-flight validation checks before manufacturing export.

    Validates PCB file integrity, schematic availability, board outline,
    component footprints, BOM fields, DRC status, and board dimensions.

    Example::

        checker = PreflightChecker(
            pcb_path="board.kicad_pcb",
            schematic_path="board.kicad_sch",
            manufacturer="jlcpcb",
        )
        results = checker.run_all()
        if checker.has_failures(results):
            print("Cannot proceed with export")
    """

    def __init__(
        self,
        pcb_path: str | Path,
        schematic_path: str | Path | None = None,
        manufacturer: str = "jlcpcb",
        output_dir: str | Path | None = None,
        config: PreflightConfig | None = None,
    ):
        self.pcb_path = Path(pcb_path)
        self.schematic_path = Path(schematic_path) if schematic_path else None
        self.manufacturer = manufacturer.lower()
        self.output_dir = Path(output_dir) if output_dir else None
        self.config = config or PreflightConfig()

        # Lazy-loaded objects
        self._pcb = None
        self._bom = None

    def run_all(self) -> list[PreflightResult]:
        """Run all applicable pre-flight checks.

        Returns:
            List of PreflightResult, one per check.
        """
        if self.config.skip_all:
            return []

        results: list[PreflightResult] = []

        # Always check PCB file first -- most other checks depend on it
        results.append(self._check_pcb_parseable())

        # Schematic check (needed for BOM/CPL)
        results.append(self._check_schematic_exists())

        # Board outline
        if self._pcb is not None:
            results.append(self._check_board_outline_closed())
            results.append(self._check_footprints_present())
            results.append(self._check_board_dimensions())
            results.append(self._check_drill_holes())

        # BOM checks (only if schematic is available)
        if self.schematic_path and self.schematic_path.exists():
            results.append(self._check_bom_fields())
            results.append(self._check_bom_footprint_match())

        # DRC check
        if not self.config.skip_drc:
            results.append(self._check_drc())

        # ERC check
        if not self.config.skip_erc:
            results.append(self._check_erc())

        return results

    @staticmethod
    def has_failures(results: list[PreflightResult]) -> bool:
        """Check if any result is a FAIL."""
        return any(r.status == "FAIL" for r in results)

    @staticmethod
    def has_warnings(results: list[PreflightResult]) -> bool:
        """Check if any result is a WARN."""
        return any(r.status == "WARN" for r in results)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_pcb_parseable(self) -> PreflightResult:
        """Verify the PCB file exists and can be parsed."""
        if not self.pcb_path.exists():
            return PreflightResult(
                name="pcb_file",
                status="FAIL",
                message=f"PCB file not found: {self.pcb_path}",
            )

        try:
            from ..schema.pcb import PCB

            self._pcb = PCB.load(str(self.pcb_path))
            return PreflightResult(
                name="pcb_file",
                status="OK",
                message="PCB file parsed successfully",
                details=f"Footprints: {self._pcb.footprint_count}",
            )
        except Exception as e:
            return PreflightResult(
                name="pcb_file",
                status="FAIL",
                message=f"PCB file could not be parsed: {e}",
            )

    def _check_schematic_exists(self) -> PreflightResult:
        """Check that the schematic file exists (needed for BOM/CPL)."""
        if self.schematic_path is None:
            # Try auto-detection
            auto_sch = self.pcb_path.with_suffix(".kicad_sch")
            if auto_sch.exists():
                self.schematic_path = auto_sch
                return PreflightResult(
                    name="schematic_file",
                    status="OK",
                    message=f"Schematic auto-detected: {auto_sch.name}",
                )
            return PreflightResult(
                name="schematic_file",
                status="WARN",
                message="No schematic file specified or auto-detected; BOM/CPL generation may fail",
            )

        if not self.schematic_path.exists():
            return PreflightResult(
                name="schematic_file",
                status="WARN",
                message=f"Schematic file not found: {self.schematic_path}",
                details="BOM and CPL generation will be skipped",
            )

        return PreflightResult(
            name="schematic_file",
            status="OK",
            message=f"Schematic file found: {self.schematic_path.name}",
        )

    def _check_board_outline_closed(self) -> PreflightResult:
        """Verify the board outline on Edge.Cuts is a closed polygon."""
        if self._pcb is None:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="Cannot check board outline: PCB not loaded",
            )

        outline = self._pcb.get_board_outline()
        if not outline:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="No board outline found on Edge.Cuts layer",
                details="A closed board outline is required for manufacturing",
            )

        # Check if outline is closed (first point ~= last point)
        if len(outline) < 3:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="Board outline has fewer than 3 points",
                details=f"Found {len(outline)} points; a valid outline needs at least 3",
            )

        first = outline[0]
        last = outline[-1]
        closed = self._pcb._points_close(first, last)

        if not closed:
            return PreflightResult(
                name="board_outline",
                status="FAIL",
                message="Board outline is not closed",
                details=(
                    f"Gap between first point ({first[0]:.3f}, {first[1]:.3f}) "
                    f"and last point ({last[0]:.3f}, {last[1]:.3f})"
                ),
            )

        return PreflightResult(
            name="board_outline",
            status="OK",
            message=f"Board outline is closed ({len(outline)} points)",
        )

    def _check_footprints_present(self) -> PreflightResult:
        """Check that all components have footprints."""
        if self._pcb is None:
            return PreflightResult(
                name="footprints",
                status="FAIL",
                message="Cannot check footprints: PCB not loaded",
            )

        count = self._pcb.footprint_count
        if count == 0:
            return PreflightResult(
                name="footprints",
                status="WARN",
                message="No footprints found in PCB",
                details="The board has no placed components",
            )

        # Check for footprints with empty or placeholder footprint names
        missing_name = []
        for fp in self._pcb.footprints:
            if not fp.name or fp.name.strip() == "":
                missing_name.append(fp.reference)

        if missing_name:
            refs = ", ".join(missing_name[:10])
            suffix = f" (and {len(missing_name) - 10} more)" if len(missing_name) > 10 else ""
            return PreflightResult(
                name="footprints",
                status="WARN",
                message=f"{len(missing_name)} footprint(s) missing library footprint name",
                details=f"References: {refs}{suffix}",
            )

        return PreflightResult(
            name="footprints",
            status="OK",
            message=f"All {count} footprints have library footprint names",
        )

    def _check_board_dimensions(self) -> PreflightResult:
        """Check that board dimensions are within manufacturer limits."""
        if self._pcb is None:
            return PreflightResult(
                name="board_dimensions",
                status="FAIL",
                message="Cannot check dimensions: PCB not loaded",
            )

        outline = self._pcb.get_board_outline()
        if not outline:
            return PreflightResult(
                name="board_dimensions",
                status="WARN",
                message="Cannot determine board dimensions: no outline found",
            )

        # Compute bounding box
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)

        # Get manufacturer limits
        max_w, max_h = self._get_manufacturer_limits()

        details = f"Board size: {width:.2f} x {height:.2f} mm"

        if width > max_w or height > max_h:
            return PreflightResult(
                name="board_dimensions",
                status="FAIL",
                message=f"Board exceeds manufacturer limits ({max_w:.0f} x {max_h:.0f} mm)",
                details=details,
            )

        if width < 5.0 or height < 5.0:
            return PreflightResult(
                name="board_dimensions",
                status="WARN",
                message="Board is very small (< 5 mm on one side)",
                details=details,
            )

        return PreflightResult(
            name="board_dimensions",
            status="OK",
            message=f"Board dimensions within limits ({width:.2f} x {height:.2f} mm)",
        )

    def _check_drill_holes(self) -> PreflightResult:
        """Check that the board has drill holes (vias or through-hole pads)."""
        if self._pcb is None:
            return PreflightResult(
                name="drill_holes",
                status="FAIL",
                message="Cannot check drill holes: PCB not loaded",
            )

        via_count = 0
        th_pad_count = 0

        # Count vias
        try:
            vias = list(self._pcb.vias)
            via_count = len(vias)
        except (AttributeError, TypeError):
            pass

        # Count through-hole pads on footprints
        for fp in self._pcb.footprints:
            for pad in fp.pads:
                if pad.type in ("thru_hole", "through_hole"):
                    th_pad_count += 1

        total = via_count + th_pad_count

        if total == 0:
            # A board with no drill holes is unusual but not necessarily wrong
            return PreflightResult(
                name="drill_holes",
                status="WARN",
                message="No drill holes found (no vias or through-hole pads)",
                details="Verify this is an all-SMD design with no mounting holes",
            )

        return PreflightResult(
            name="drill_holes",
            status="OK",
            message=f"Drill holes present ({via_count} vias, {th_pad_count} TH pads)",
        )

    def _check_bom_fields(self) -> PreflightResult:
        """Check that BOM items have required fields for manufacturing."""
        try:
            bom = self._load_bom()
        except Exception as e:
            return PreflightResult(
                name="bom_fields",
                status="WARN",
                message=f"Could not extract BOM: {e}",
            )

        if not bom.items:
            return PreflightResult(
                name="bom_fields",
                status="WARN",
                message="BOM has no items",
            )

        # Check for items missing footprint
        missing_fp = [item for item in bom.items if not item.footprint and not item.is_virtual]
        # Check for items missing value
        missing_val = [item for item in bom.items if not item.value and not item.is_virtual]
        # Check for LCSC part numbers (warn only)
        missing_lcsc = [
            item for item in bom.items if not item.lcsc and not item.is_virtual and not item.dnp
        ]

        issues: list[str] = []
        status: Literal["OK", "WARN", "FAIL"] = "OK"

        if missing_fp:
            refs = ", ".join(item.reference for item in missing_fp[:5])
            issues.append(f"{len(missing_fp)} item(s) missing footprint: {refs}")
            status = "FAIL"

        if missing_val:
            refs = ", ".join(item.reference for item in missing_val[:5])
            issues.append(f"{len(missing_val)} item(s) missing value: {refs}")
            if status != "FAIL":
                status = "WARN"

        if missing_lcsc:
            count = len(missing_lcsc)
            total = len([i for i in bom.items if not i.is_virtual and not i.dnp])
            issues.append(f"{count}/{total} active item(s) missing LCSC part number")
            if status != "FAIL":
                status = "WARN"

        if not issues:
            active_count = len([i for i in bom.items if not i.is_virtual])
            return PreflightResult(
                name="bom_fields",
                status="OK",
                message=f"All {active_count} BOM items have required fields",
            )

        return PreflightResult(
            name="bom_fields",
            status=status,
            message=f"BOM field issues: {len(issues)} problem(s)",
            details="; ".join(issues),
        )

    def _check_bom_footprint_match(self) -> PreflightResult:
        """Check that BOM component count matches PCB footprint count."""
        if self._pcb is None:
            return PreflightResult(
                name="bom_pcb_match",
                status="WARN",
                message="Cannot check BOM/PCB match: PCB not loaded",
            )

        try:
            bom = self._load_bom()
        except Exception as e:
            return PreflightResult(
                name="bom_pcb_match",
                status="WARN",
                message=f"Cannot check BOM/PCB match: {e}",
            )

        # BOM references (non-virtual, non-DNP)
        bom_refs = {item.reference for item in bom.items if not item.is_virtual and not item.dnp}

        # PCB footprint references
        pcb_refs = {fp.reference for fp in self._pcb.footprints}

        in_bom_not_pcb = bom_refs - pcb_refs
        in_pcb_not_bom = pcb_refs - bom_refs

        issues: list[str] = []
        if in_bom_not_pcb:
            refs = ", ".join(sorted(in_bom_not_pcb)[:10])
            issues.append(f"{len(in_bom_not_pcb)} in BOM but not on PCB: {refs}")
        if in_pcb_not_bom:
            refs = ", ".join(sorted(in_pcb_not_bom)[:10])
            issues.append(f"{len(in_pcb_not_bom)} on PCB but not in BOM: {refs}")

        if issues:
            return PreflightResult(
                name="bom_pcb_match",
                status="WARN",
                message="BOM/PCB reference mismatch",
                details="; ".join(issues),
            )

        return PreflightResult(
            name="bom_pcb_match",
            status="OK",
            message=f"BOM and PCB references match ({len(bom_refs)} components)",
        )

    def _check_drc(self) -> PreflightResult:
        """Check DRC status from a pre-existing report file."""
        report_path = self.config.drc_report_path

        if report_path is None:
            # Try to find a DRC report next to the PCB
            auto_path = self.pcb_path.parent / "drc_report.json"
            if not auto_path.exists():
                auto_path = self.pcb_path.parent / "drc_report.txt"
            if auto_path.exists():
                report_path = auto_path
            else:
                return PreflightResult(
                    name="drc",
                    status="WARN",
                    message="No DRC report found; run kicad-cli or kct check first",
                    details="Use --drc-report to specify a report file, or --skip-drc to skip",
                )

        report_path = Path(report_path)
        if not report_path.exists():
            return PreflightResult(
                name="drc",
                status="WARN",
                message=f"DRC report not found: {report_path}",
            )

        try:
            from ..drc.report import DRCReport

            report = DRCReport.load(report_path)
            error_count = report.error_count
            warning_count = report.warning_count

            if error_count > 0:
                errors = report.errors
                return PreflightResult(
                    name="drc",
                    status="FAIL",
                    message=f"DRC has {error_count} error(s)",
                    details="; ".join(f"{v.type_str}: {v.message}" for v in errors[:5]),
                )

            if warning_count > 0:
                return PreflightResult(
                    name="drc",
                    status="WARN",
                    message=f"DRC passed with {warning_count} warning(s)",
                )

            return PreflightResult(
                name="drc",
                status="OK",
                message="DRC: 0 errors, 0 warnings",
            )
        except Exception as e:
            return PreflightResult(
                name="drc",
                status="WARN",
                message=f"Could not parse DRC report: {e}",
            )

    def _check_erc(self) -> PreflightResult:
        """Check ERC status from a pre-existing report file."""
        report_path = self.config.erc_report_path

        if report_path is None:
            # Try to find an ERC report next to the schematic
            search_dir = self.schematic_path.parent if self.schematic_path else self.pcb_path.parent
            auto_path = search_dir / "erc_report.json"
            if not auto_path.exists():
                auto_path = search_dir / "erc_report.txt"
            if auto_path.exists():
                report_path = auto_path
            else:
                return PreflightResult(
                    name="erc",
                    status="WARN",
                    message="No ERC report found; run kicad-cli first",
                    details="Use --erc-report to specify a report file, or --skip-erc to skip",
                )

        report_path = Path(report_path)
        if not report_path.exists():
            return PreflightResult(
                name="erc",
                status="WARN",
                message=f"ERC report not found: {report_path}",
            )

        try:
            from ..erc.report import ERCReport

            report = ERCReport.load(report_path)
            error_count = report.error_count
            warning_count = report.warning_count

            if error_count > 0:
                errors = report.errors
                return PreflightResult(
                    name="erc",
                    status="FAIL",
                    message=f"ERC has {error_count} error(s)",
                    details="; ".join(f"{v.type_str}: {v.description}" for v in errors[:5]),
                )

            if warning_count > 0:
                return PreflightResult(
                    name="erc",
                    status="WARN",
                    message=f"ERC passed with {warning_count} warning(s)",
                )

            return PreflightResult(
                name="erc",
                status="OK",
                message="ERC: 0 errors, 0 warnings",
            )
        except Exception as e:
            return PreflightResult(
                name="erc",
                status="WARN",
                message=f"Could not parse ERC report: {e}",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_bom(self):
        """Load and cache BOM data from schematic."""
        if self._bom is not None:
            return self._bom

        if self.schematic_path is None or not self.schematic_path.exists():
            raise FileNotFoundError("Schematic file not available for BOM extraction")

        from ..schema.bom import extract_bom

        self._bom = extract_bom(str(self.schematic_path))
        return self._bom

    def _get_manufacturer_limits(self) -> tuple[float, float]:
        """Get max board dimensions for the configured manufacturer.

        Returns:
            Tuple of (max_width_mm, max_height_mm).
        """
        try:
            from ..manufacturers import get_profile

            profile = get_profile(self.manufacturer)
            rules = profile.get_design_rules(layers=2)
            return (rules.max_board_width_mm, rules.max_board_height_mm)
        except Exception:
            # Fall back to generous defaults
            return (400.0, 500.0)
