"""
KiCad project handling.

Provides a unified interface to work with complete KiCad projects,
cross-referencing schematics and PCBs.

Example::

    from kicad_tools import Project

    # Create new project
    project = Project.create("my_board", directory="./projects/")
    # Creates: my_board.kicad_pro, my_board.kicad_sch, my_board.kicad_pcb

    # Load existing project
    project = Project.load("my_board.kicad_pro")

    # Access schematic and PCB
    sch = project.schematic
    pcb = project.pcb

    # High-level operations
    project.route(skip_nets=["GND", "+3.3V"])
    results = project.check_drc(manufacturer="jlcpcb", layers=4)
    project.export_gerbers("manufacturing/")
    project.export_assembly("manufacturing/", manufacturer="jlcpcb")

    # Save all files
    project.save()
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .schema.bom import BOM, extract_bom
from .schema.pcb import PCB
from .schema.schematic import Schematic

if TYPE_CHECKING:
    from .drc.checker import ManufacturerCheck
    from .export import AssemblyPackageResult
    from .validate.netlist import SyncResult


@dataclass
class RoutingResult:
    """Result of a routing operation."""

    routed_nets: int
    total_nets: int
    total_segments: int
    total_vias: int
    total_length_mm: float

    @property
    def success_rate(self) -> float:
        """Fraction of nets successfully routed."""
        if self.total_nets == 0:
            return 1.0
        return self.routed_nets / self.total_nets


logger = logging.getLogger(__name__)


@dataclass
class UnplacedSymbol:
    """A symbol in schematic that has no corresponding footprint on PCB."""

    reference: str
    value: str
    lib_id: str
    footprint_name: str  # Expected footprint


@dataclass
class OrphanedFootprint:
    """A footprint on PCB with no corresponding symbol in schematic."""

    reference: str
    value: str
    footprint_name: str
    position: tuple[float, float]


@dataclass
class MismatchedComponent:
    """A component where schematic and PCB data don't match."""

    reference: str
    schematic_value: str
    pcb_value: str
    schematic_footprint: str
    pcb_footprint: str
    mismatches: list[str] = field(default_factory=list)


@dataclass
class CrossReferenceResult:
    """Result of cross-referencing schematic and PCB."""

    matched: int = 0
    unplaced: list[UnplacedSymbol] = field(default_factory=list)
    orphaned: list[OrphanedFootprint] = field(default_factory=list)
    mismatched: list[MismatchedComponent] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Check if cross-reference is clean (no issues)."""
        return len(self.unplaced) == 0 and len(self.orphaned) == 0 and len(self.mismatched) == 0

    def summary(self) -> dict[str, int]:
        """Get summary counts."""
        return {
            "matched": self.matched,
            "unplaced": len(self.unplaced),
            "orphaned": len(self.orphaned),
            "mismatched": len(self.mismatched),
        }


class Project:
    """
    High-level interface to a complete KiCad project.

    Provides methods for working with project files, cross-referencing
    schematics and PCBs, and generating manufacturing outputs.

    Example::

        from kicad_tools import Project

        # Load from .kicad_pro file
        project = Project.load("myproject.kicad_pro")

        # Or specify files directly
        project = Project(
            schematic="myproject.kicad_sch",
            pcb="myproject.kicad_pcb",
        )

        # Cross-reference
        result = project.cross_reference()
        if not result.is_clean:
            print(f"Unplaced: {len(result.unplaced)}")
            print(f"Orphaned: {len(result.orphaned)}")

        # Generate BOM
        bom = project.get_bom()

        # Export for manufacturing
        project.export_assembly("output/", manufacturer="jlcpcb")
    """

    def __init__(
        self,
        schematic: str | Path | None = None,
        pcb: str | Path | None = None,
        project_file: str | Path | None = None,
    ):
        """
        Initialize project.

        Args:
            schematic: Path to schematic file (.kicad_sch)
            pcb: Path to PCB file (.kicad_pcb)
            project_file: Path to project file (.kicad_pro)
        """
        self.project_file = Path(project_file) if project_file else None
        self._schematic_path = Path(schematic) if schematic else None
        self._pcb_path = Path(pcb) if pcb else None

        # Lazy-loaded instances
        self._schematic: Schematic | None = None
        self._pcb: PCB | None = None
        self._bom: BOM | None = None

    @classmethod
    def load(cls, project_path: str | Path) -> Project:
        """
        Load a KiCad project from .kicad_pro file.

        Args:
            project_path: Path to .kicad_pro file

        Returns:
            Project instance

        Raises:
            FileNotFoundError: If project file doesn't exist
        """
        project_path = Path(project_path)
        if not project_path.exists():
            raise FileNotFoundError(f"Project file not found: {project_path}")

        # Parse project file to find related files
        project_dir = project_path.parent
        project_name = project_path.stem

        # Look for schematic and PCB
        schematic_path = project_dir / f"{project_name}.kicad_sch"
        pcb_path = project_dir / f"{project_name}.kicad_pcb"

        return cls(
            schematic=schematic_path if schematic_path.exists() else None,
            pcb=pcb_path if pcb_path.exists() else None,
            project_file=project_path,
        )

    @classmethod
    def create(
        cls,
        name: str,
        directory: str | Path = ".",
        board_width: float = 100.0,
        board_height: float = 80.0,
    ) -> Project:
        """
        Create a new KiCad project with empty schematic and PCB.

        Args:
            name: Project name (used for file names)
            directory: Directory to create project in
            board_width: Initial board width in mm (default: 100mm)
            board_height: Initial board height in mm (default: 80mm)

        Returns:
            Project instance with all files created

        Example::

            project = Project.create("my_board", directory="./projects/")
            # Creates: my_board.kicad_pro, my_board.kicad_sch, my_board.kicad_pcb
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        project_path = directory / f"{name}.kicad_pro"
        schematic_path = directory / f"{name}.kicad_sch"
        pcb_path = directory / f"{name}.kicad_pcb"

        # Generate UUIDs for the files
        project_uuid = str(uuid.uuid4())
        schematic_uuid = str(uuid.uuid4())
        pcb_uuid = str(uuid.uuid4())

        # Create minimal .kicad_pro file (JSON format)
        project_data = {
            "meta": {
                "filename": f"{name}.kicad_pro",
                "version": 1,
            },
            "project": {
                "uuid": project_uuid,
            },
        }
        project_path.write_text(json.dumps(project_data, indent=2))

        # Create minimal .kicad_sch file (S-expression format)
        schematic_content = f'''(kicad_sch (version 20231120) (generator "kicad_tools") (generator_version "0.2.0")

  (uuid "{schematic_uuid}")

  (paper "A4")

  (lib_symbols
  )

  (symbol_instances
  )
)
'''
        schematic_path.write_text(schematic_content)

        # Create minimal .kicad_pcb file (S-expression format)
        pcb_content = f'''(kicad_pcb (version 20231014) (generator "kicad_tools") (generator_version "0.2.0")

  (general
    (thickness 1.6)
  )

  (paper "A4")

  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
    (50 "User.1" user)
    (51 "User.2" user)
    (52 "User.3" user)
    (53 "User.4" user)
    (54 "User.5" user)
    (55 "User.6" user)
    (56 "User.7" user)
    (57 "User.8" user)
    (58 "User.9" user)
  )

  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (layer "B.Paste" (type "Bottom Solder Paste"))
      (layer "B.SilkS" (type "Bottom Silk Screen"))
      (copper_finish "None")
    )
    (pad_to_mask_clearance 0)
  )

  (net 0 "")

  (uuid "{pcb_uuid}")

  (gr_rect (start 0 0) (end {board_width} {board_height})
    (stroke (width 0.15) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "{str(uuid.uuid4())}")
  )
)
'''
        pcb_path.write_text(pcb_content)

        logger.info(f"Created KiCad project: {project_path}")

        return cls(
            schematic=schematic_path,
            pcb=pcb_path,
            project_file=project_path,
        )

    @classmethod
    def from_pcb(cls, pcb_path: str | Path) -> Project:
        """
        Create project from PCB file, finding related schematic.

        Args:
            pcb_path: Path to .kicad_pcb file

        Returns:
            Project instance
        """
        pcb_path = Path(pcb_path)
        schematic_path = pcb_path.with_suffix(".kicad_sch")
        project_path = pcb_path.with_suffix(".kicad_pro")

        return cls(
            schematic=schematic_path if schematic_path.exists() else None,
            pcb=pcb_path,
            project_file=project_path if project_path.exists() else None,
        )

    @property
    def name(self) -> str:
        """Get project name."""
        if self.project_file:
            return self.project_file.stem
        if self._pcb_path:
            return self._pcb_path.stem
        if self._schematic_path:
            return self._schematic_path.stem
        return "unnamed"

    @property
    def directory(self) -> Path | None:
        """Get project directory."""
        if self.project_file:
            return self.project_file.parent
        if self._pcb_path:
            return self._pcb_path.parent
        if self._schematic_path:
            return self._schematic_path.parent
        return None

    @property
    def schematic(self) -> Schematic | None:
        """Get loaded schematic (lazy loaded)."""
        if self._schematic is None and self._schematic_path:
            if self._schematic_path.exists():
                self._schematic = Schematic.load(self._schematic_path)
        return self._schematic

    @property
    def pcb(self) -> PCB | None:
        """Get loaded PCB (lazy loaded)."""
        if self._pcb is None and self._pcb_path and self._pcb_path.exists():
            self._pcb = PCB.load(str(self._pcb_path))
        return self._pcb

    def get_bom(self, force_reload: bool = False) -> BOM | None:
        """
        Get bill of materials from schematic.

        Args:
            force_reload: Force reload from schematic

        Returns:
            BOM object, or None if no schematic
        """
        if self._bom is None or force_reload:
            if self._schematic_path and self._schematic_path.exists():
                self._bom = extract_bom(str(self._schematic_path))
        return self._bom

    def cross_reference(self) -> CrossReferenceResult:
        """
        Cross-reference schematic symbols with PCB footprints.

        Checks for:
        - Symbols without footprints (unplaced)
        - Footprints without symbols (orphaned)
        - Value/footprint mismatches

        Returns:
            CrossReferenceResult with findings
        """
        result = CrossReferenceResult()

        if not self.schematic or not self.pcb:
            logger.warning("Cannot cross-reference: missing schematic or PCB")
            return result

        # Build reference sets
        sch_refs: dict[str, dict[str, Any]] = {}
        for sym in self.schematic.symbols:
            if sym.reference and not sym.reference.startswith("#"):
                sch_refs[sym.reference] = {
                    "value": sym.value,
                    "lib_id": sym.lib_id,
                    "footprint": getattr(sym, "footprint", ""),
                }

        pcb_refs: dict[str, dict[str, Any]] = {}
        for fp in self.pcb.footprints:
            if fp.reference and not fp.reference.startswith("#"):
                pcb_refs[fp.reference] = {
                    "value": fp.value,
                    "footprint": fp.name,
                    "position": fp.position,
                }

        # Find matches, unplaced, and orphaned
        sch_set = set(sch_refs.keys())
        pcb_set = set(pcb_refs.keys())

        matched_refs = sch_set & pcb_set
        unplaced_refs = sch_set - pcb_set
        orphaned_refs = pcb_set - sch_set

        result.matched = len(matched_refs)

        # Check for mismatches in matched components
        for ref in matched_refs:
            sch_data = sch_refs[ref]
            pcb_data = pcb_refs[ref]

            mismatches = []
            if sch_data["value"] != pcb_data["value"]:
                mismatches.append("value")

            # Compare footprints (may have library prefix)
            sch_fp = sch_data.get("footprint", "")
            pcb_fp = pcb_data["footprint"]
            if sch_fp and pcb_fp:
                # Extract footprint name without library
                sch_fp_name = sch_fp.split(":")[-1] if ":" in sch_fp else sch_fp
                pcb_fp_name = pcb_fp.split(":")[-1] if ":" in pcb_fp else pcb_fp
                if sch_fp_name != pcb_fp_name:
                    mismatches.append("footprint")

            if mismatches:
                result.mismatched.append(
                    MismatchedComponent(
                        reference=ref,
                        schematic_value=sch_data["value"],
                        pcb_value=pcb_data["value"],
                        schematic_footprint=sch_data.get("footprint", ""),
                        pcb_footprint=pcb_data["footprint"],
                        mismatches=mismatches,
                    )
                )

        # Record unplaced symbols
        for ref in unplaced_refs:
            sch_data = sch_refs[ref]
            result.unplaced.append(
                UnplacedSymbol(
                    reference=ref,
                    value=sch_data["value"],
                    lib_id=sch_data["lib_id"],
                    footprint_name=sch_data.get("footprint", ""),
                )
            )

        # Record orphaned footprints
        for ref in orphaned_refs:
            pcb_data = pcb_refs[ref]
            result.orphaned.append(
                OrphanedFootprint(
                    reference=ref,
                    value=pcb_data["value"],
                    footprint_name=pcb_data["footprint"],
                    position=pcb_data["position"],
                )
            )

        return result

    def find_unplaced_symbols(self) -> list[UnplacedSymbol]:
        """
        Find symbols in schematic that aren't on PCB.

        Returns:
            List of unplaced symbols
        """
        return self.cross_reference().unplaced

    def find_orphaned_footprints(self) -> list[OrphanedFootprint]:
        """
        Find footprints on PCB that aren't in schematic.

        Returns:
            List of orphaned footprints
        """
        return self.cross_reference().orphaned

    def check_sync(self) -> SyncResult:
        """
        Check if schematic and PCB netlists are in sync.

        Validates that:
        - All schematic symbols have footprints on PCB
        - No orphaned footprints exist on PCB
        - Net names match between schematic and PCB
        - Pin-to-pad mappings are consistent

        Returns:
            SyncResult with all issues found

        Example::

            project = Project.load("my_board.kicad_pro")
            result = project.check_sync()

            if not result.in_sync:
                for issue in result.issues:
                    print(f"{issue.severity}: {issue.message}")
                    print(f"  Fix: {issue.suggestion}")
        """
        from .validate.netlist import NetlistValidator, SyncResult

        if not self.schematic or not self.pcb:
            logger.warning("Cannot check sync: missing schematic or PCB")
            return SyncResult()

        validator = NetlistValidator(self.schematic, self.pcb)
        return validator.validate()

    def export_assembly(
        self,
        output_dir: str | Path,
        manufacturer: str = "jlcpcb",
    ) -> AssemblyPackageResult:
        """
        Export complete assembly package.

        Args:
            output_dir: Output directory
            manufacturer: Manufacturer ID

        Returns:
            AssemblyPackageResult with generated files
        """
        from .export import create_assembly_package

        if not self._pcb_path:
            raise ValueError("PCB path required for assembly export")

        return create_assembly_package(
            pcb=self._pcb_path,
            schematic=self._schematic_path,
            manufacturer=manufacturer,
            output_dir=output_dir,
        )

    def export_gerbers(
        self,
        output_dir: str | Path,
        manufacturer: str = "generic",
    ) -> list[Path]:
        """
        Export Gerber files for manufacturing.

        Args:
            output_dir: Output directory for Gerber files
            manufacturer: Manufacturer ID for preset settings
                          (jlcpcb, pcbway, oshpark, seeed, generic)

        Returns:
            List of generated Gerber file paths

        Raises:
            ValueError: If no PCB file available
            RuntimeError: If KiCad CLI is not found

        Example::

            project = Project.load("my_board.kicad_pro")
            files = project.export_gerbers("gerbers/", manufacturer="jlcpcb")
        """
        from .export import export_gerbers

        if not self._pcb_path:
            raise ValueError("PCB path required for Gerber export")

        return export_gerbers(
            pcb_path=str(self._pcb_path),
            output_dir=str(output_dir),
            manufacturer=manufacturer,
        )

    def route(
        self,
        skip_nets: list[str] | None = None,
        rules: Any | None = None,
    ) -> RoutingResult:
        """
        Route the PCB using the autorouter.

        Args:
            skip_nets: Net names to skip (e.g., ["GND", "+3.3V"] for plane nets)
            rules: DesignRules for routing (optional, uses defaults if not provided)

        Returns:
            RoutingResult with routing statistics

        Raises:
            ValueError: If no PCB file available

        Example::

            project = Project.load("my_board.kicad_pro")
            result = project.route(skip_nets=["GND", "+3.3V"])
            print(f"Routed {result.routed_nets}/{result.total_nets} nets")
        """
        from .router import load_pcb_for_routing, merge_routes_into_pcb

        if not self._pcb_path:
            raise ValueError("PCB path required for routing")

        # Load PCB for routing
        router, net_map = load_pcb_for_routing(
            str(self._pcb_path),
            skip_nets=skip_nets,
            rules=rules,
        )

        # Get all nets that need routing
        nets_to_route: list[int] = []
        skip_nets = skip_nets or []
        for net_name, net_num in net_map.items():
            if net_name and net_name not in skip_nets and net_num in router.nets:
                if len(router.nets[net_num]) >= 2:
                    nets_to_route.append(net_num)

        total_nets = len(nets_to_route)

        # Route all nets
        routes = router.route_all(nets_to_route)

        # Get routing statistics
        stats = router.get_statistics()

        # Merge routes into PCB if any routes were created
        if routes:
            route_sexp = router.to_sexp()
            pcb_content = self._pcb_path.read_text()
            merged_content = merge_routes_into_pcb(pcb_content, route_sexp)
            self._pcb_path.write_text(merged_content)

            # Invalidate cached PCB since file was modified
            self._pcb = None

        # Create result object
        result = RoutingResult(
            routed_nets=stats.get("nets_routed", 0),
            total_nets=total_nets,
            total_segments=stats.get("segments", 0),
            total_vias=stats.get("vias", 0),
            total_length_mm=stats.get("total_length_mm", 0.0),
        )

        logger.info(
            f"Routed {result.routed_nets}/{result.total_nets} nets, "
            f"{result.total_segments} segments, {result.total_vias} vias"
        )

        return result

    def check_drc(
        self,
        manufacturer: str = "jlcpcb",
        layers: int = 2,
        copper_oz: float = 1.0,
        report_path: str | Path | None = None,
    ) -> list[ManufacturerCheck]:
        """
        Check design rules against manufacturer specifications.

        This method requires a DRC report from KiCad. If report_path is not
        provided, it will look for a .rpt file in the project directory.

        Args:
            manufacturer: Manufacturer ID (jlcpcb, pcbway, oshpark, etc.)
            layers: Layer count for rules lookup
            copper_oz: Copper weight in oz
            report_path: Path to KiCad DRC report file (.rpt)

        Returns:
            List of ManufacturerCheck results

        Raises:
            FileNotFoundError: If no DRC report found

        Example::

            project = Project.load("my_board.kicad_pro")
            checks = project.check_drc(manufacturer="jlcpcb", layers=4)
            for check in checks:
                if not check.is_compatible:
                    print(f"FAIL: {check}")
        """
        from .drc import DRCReport, check_manufacturer_rules

        # Find or use provided DRC report
        if report_path:
            report_file = Path(report_path)
        else:
            # Look for DRC report in project directory
            if not self.directory:
                raise FileNotFoundError("No project directory available")

            # Try common DRC report names
            report_file = None
            for pattern in [f"{self.name}-drc.rpt", f"{self.name}_drc.rpt", "drc.rpt"]:
                candidate = self.directory / pattern
                if candidate.exists():
                    report_file = candidate
                    break

            if not report_file:
                raise FileNotFoundError(
                    f"No DRC report found in {self.directory}. "
                    "Run DRC in KiCad and save the report first."
                )

        # Load and check the report
        report = DRCReport.load(str(report_file))
        checks = check_manufacturer_rules(
            report=report,
            manufacturer_id=manufacturer,
            layers=layers,
            copper_oz=copper_oz,
        )

        logger.info(f"DRC check: {len(checks)} violations checked against {manufacturer} rules")

        return checks

    def save(self) -> None:
        """
        Save all modified project files.

        Saves schematic and PCB if they have been loaded and modified.
        The project file (.kicad_pro) is not modified by this library.

        Example::

            project = Project.load("my_board.kicad_pro")
            # ... make modifications ...
            project.save()  # Saves schematic and PCB
        """
        if self._schematic is not None and self._schematic_path:
            self._schematic.save(self._schematic_path)
            logger.info(f"Saved schematic: {self._schematic_path}")

        if self._pcb is not None and self._pcb_path:
            self._pcb.save(self._pcb_path)
            logger.info(f"Saved PCB: {self._pcb_path}")

    def __repr__(self) -> str:
        parts = [f"Project({self.name!r}"]
        if self._schematic_path:
            parts.append(f"schematic={self._schematic_path.name!r}")
        if self._pcb_path:
            parts.append(f"pcb={self._pcb_path.name!r}")
        return ", ".join(parts) + ")"
