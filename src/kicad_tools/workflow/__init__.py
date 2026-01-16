"""
Workflow module for high-level PCB generation from schematics.

This module provides a streamlined API for creating PCBs programmatically
from KiCad schematics. It ties together netlist extraction, PCB creation,
footprint placement, and net assignment into a cohesive workflow.

Example usage:

    # One-liner for simple cases
    from kicad_tools.workflow import create_pcb_from_schematic

    pcb = create_pcb_from_schematic(
        schematic="project.kicad_sch",
        board_size=(160, 100),
        layers=4,
    )
    pcb.save("project.kicad_pcb")

    # Step-by-step for more control
    from kicad_tools.workflow import PCBFromSchematic

    workflow = PCBFromSchematic("project.kicad_sch")
    components = workflow.get_components()
    pcb = workflow.create_pcb(width=160, height=100, layers=4)
    workflow.place_component("U1", x=50, y=30)
    workflow.assign_nets()
    workflow.save("project.kicad_pcb")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..operations.netlist import Netlist, NetlistComponent, export_netlist
from ..schema.pcb import PCB

if TYPE_CHECKING:
    from ..schema.pcb import Footprint


@dataclass
class ComponentInfo:
    """Information about a component from the netlist.

    Contains all the data needed to place a component on a PCB.
    """

    reference: str
    """Reference designator (e.g., 'U1', 'C1')"""

    value: str
    """Component value (e.g., '100nF', '10k')"""

    footprint: str
    """Footprint identifier (e.g., 'Capacitor_SMD:C_0805_2012Metric')"""

    lib_id: str
    """Library identifier for the symbol"""

    pins: list[str] = field(default_factory=list)
    """List of pin numbers/names on this component"""

    nets: dict[str, str] = field(default_factory=dict)
    """Mapping of pin number to net name"""

    @classmethod
    def from_netlist_component(cls, comp: NetlistComponent, netlist: Netlist) -> ComponentInfo:
        """Create ComponentInfo from a NetlistComponent and Netlist."""
        info = cls(
            reference=comp.reference,
            value=comp.value,
            footprint=comp.footprint,
            lib_id=comp.lib_id,
        )

        # Get pin information from connected nets
        for net in netlist.nets:
            for node in net.nodes:
                if node.reference == comp.reference:
                    info.pins.append(node.pin)
                    info.nets[node.pin] = net.name

        return info


@dataclass
class PlacementResult:
    """Result of placing components on the PCB."""

    placed: list[str] = field(default_factory=list)
    """List of successfully placed component references"""

    failed: list[tuple[str, str]] = field(default_factory=list)
    """List of (reference, error_message) for components that failed to place"""

    @property
    def success_count(self) -> int:
        """Number of components successfully placed."""
        return len(self.placed)

    @property
    def failure_count(self) -> int:
        """Number of components that failed to place."""
        return len(self.failed)


@dataclass
class NetAssignmentResult:
    """Result of assigning nets to pads."""

    assigned: list[str] = field(default_factory=list)
    """List of successfully assigned pads (format: 'REF.PIN')"""

    missing_footprints: list[str] = field(default_factory=list)
    """List of component references not found in PCB"""

    missing_pads: list[str] = field(default_factory=list)
    """List of pads not found (format: 'REF.PIN')"""

    @property
    def success_count(self) -> int:
        """Number of pads successfully assigned."""
        return len(self.assigned)


class PCBFromSchematic:
    """
    Workflow class for creating a PCB from a schematic.

    This class provides a step-by-step workflow for:
    1. Extracting component and connectivity information from a schematic
    2. Creating a blank PCB with the desired dimensions
    3. Adding footprints for each component
    4. Assigning nets to pads based on schematic connectivity
    5. Optionally placing components according to a strategy

    Example:
        >>> workflow = PCBFromSchematic("project.kicad_sch")
        >>> components = workflow.get_components()
        >>> print(f"Found {len(components)} components")
        >>>
        >>> pcb = workflow.create_pcb(width=160, height=100, layers=4)
        >>> result = workflow.place_all_components()
        >>> print(f"Placed {result.success_count} components")
        >>>
        >>> nets = workflow.assign_nets()
        >>> print(f"Assigned {nets.success_count} net connections")
        >>>
        >>> workflow.save("project.kicad_pcb")
    """

    def __init__(
        self,
        schematic: str | Path,
        netlist_path: str | Path | None = None,
    ):
        """
        Initialize the workflow from a schematic file.

        Args:
            schematic: Path to the .kicad_sch schematic file
            netlist_path: Optional path for the exported netlist.
                         If not provided, uses <schematic>-netlist.kicad_net
        """
        self.schematic_path = Path(schematic)
        if not self.schematic_path.exists():
            raise FileNotFoundError(f"Schematic not found: {schematic}")

        self._netlist_path = (
            Path(netlist_path)
            if netlist_path
            else self.schematic_path.parent / f"{self.schematic_path.stem}-netlist.kicad_net"
        )

        self._netlist: Netlist | None = None
        self._pcb: PCB | None = None
        self._components: list[ComponentInfo] | None = None

    @property
    def netlist(self) -> Netlist:
        """Get the netlist, exporting from schematic if needed."""
        if self._netlist is None:
            self._netlist = export_netlist(
                self.schematic_path,
                output_path=self._netlist_path,
            )
        return self._netlist

    @property
    def pcb(self) -> PCB | None:
        """Get the PCB if one has been created."""
        return self._pcb

    def get_components(self) -> list[ComponentInfo]:
        """
        Get all components from the schematic with their connectivity.

        Returns:
            List of ComponentInfo objects containing reference, value,
            footprint, and net connectivity for each component.
        """
        if self._components is None:
            netlist = self.netlist
            self._components = [
                ComponentInfo.from_netlist_component(comp, netlist) for comp in netlist.components
            ]
        return self._components

    def create_pcb(
        self,
        width: float = 100.0,
        height: float = 100.0,
        layers: int = 2,
        title: str = "",
        revision: str = "1.0",
        company: str = "",
    ) -> PCB:
        """
        Create a new blank PCB.

        Args:
            width: Board width in mm (default 100.0)
            height: Board height in mm (default 100.0)
            layers: Number of copper layers (2 or 4, default 2)
            title: Board title for title block
            revision: Board revision (default "1.0")
            company: Company name for title block

        Returns:
            The created PCB instance
        """
        self._pcb = PCB.create(
            width=width,
            height=height,
            layers=layers,
            title=title or self.schematic_path.stem,
            revision=revision,
            company=company,
        )
        return self._pcb

    def add_component(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        layer: str = "F.Cu",
    ) -> Footprint | None:
        """
        Add a single component to the PCB at the specified position.

        The footprint is determined from the netlist component data.

        Args:
            reference: Component reference designator (e.g., "U1", "C1")
            x: X position in mm
            y: Y position in mm
            rotation: Rotation angle in degrees (default 0)
            layer: Layer to place on ("F.Cu" or "B.Cu", default "F.Cu")

        Returns:
            The Footprint object if successful, None if component not found

        Raises:
            ValueError: If no PCB has been created yet
        """
        if self._pcb is None:
            raise ValueError("No PCB created. Call create_pcb() first.")

        # Find the component in netlist
        comp = self.netlist.get_component(reference)
        if comp is None:
            return None

        if not comp.footprint:
            return None

        try:
            return self._pcb.add_footprint(
                library_id=comp.footprint,
                reference=reference,
                x=x,
                y=y,
                rotation=rotation,
                layer=layer,
                value=comp.value,
            )
        except (FileNotFoundError, ValueError):
            return None

    def place_component(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        layer: str = "F.Cu",
    ) -> Footprint | None:
        """
        Alias for add_component() for more intuitive naming.

        See add_component() for full documentation.
        """
        return self.add_component(reference, x, y, rotation, layer)

    def place_all_components(
        self,
        start_x: float = 10.0,
        start_y: float = 10.0,
        spacing: float = 15.0,
        columns: int = 10,
    ) -> PlacementResult:
        """
        Place all components in a grid pattern.

        This provides a simple default placement that can be refined later.
        Components are placed left-to-right, top-to-bottom in a grid.

        Args:
            start_x: Starting X position in mm (default 10.0)
            start_y: Starting Y position in mm (default 10.0)
            spacing: Spacing between components in mm (default 15.0)
            columns: Number of columns in the grid (default 10)

        Returns:
            PlacementResult with lists of placed and failed components
        """
        if self._pcb is None:
            raise ValueError("No PCB created. Call create_pcb() first.")

        result = PlacementResult()
        components = self.get_components()

        for i, comp in enumerate(components):
            col = i % columns
            row = i // columns
            x = start_x + col * spacing
            y = start_y + row * spacing

            if not comp.footprint:
                result.failed.append((comp.reference, "No footprint assigned"))
                continue

            try:
                fp = self.add_component(comp.reference, x, y)
                if fp:
                    result.placed.append(comp.reference)
                else:
                    result.failed.append((comp.reference, "Failed to add footprint"))
            except Exception as e:
                result.failed.append((comp.reference, str(e)))

        return result

    def assign_nets(self) -> NetAssignmentResult:
        """
        Assign nets to all footprint pads based on netlist connectivity.

        This reads the connectivity from the netlist and assigns the
        appropriate net to each pad on each footprint in the PCB.

        Returns:
            NetAssignmentResult with statistics about the assignment

        Raises:
            ValueError: If no PCB has been created yet
        """
        if self._pcb is None:
            raise ValueError("No PCB created. Call create_pcb() first.")

        stats = self._pcb.assign_nets_from_netlist(self.netlist)

        return NetAssignmentResult(
            assigned=stats["assigned"],
            missing_footprints=stats["missing_footprints"],
            missing_pads=stats["missing_pads"],
        )

    def save(self, path: str | Path) -> None:
        """
        Save the PCB to a file.

        Args:
            path: Output path for the .kicad_pcb file

        Raises:
            ValueError: If no PCB has been created yet
        """
        if self._pcb is None:
            raise ValueError("No PCB created. Call create_pcb() first.")
        self._pcb.save(path)

    def summary(self) -> dict:
        """
        Get a summary of the current workflow state.

        Returns:
            Dictionary with component count, net count, placement status, etc.
        """
        components = self.get_components()
        netlist = self.netlist

        summary = {
            "schematic": str(self.schematic_path),
            "component_count": len(components),
            "net_count": len(netlist.nets),
            "power_net_count": len(netlist.power_nets),
            "pcb_created": self._pcb is not None,
        }

        if self._pcb:
            summary.update(
                {
                    "footprints_placed": self._pcb.footprint_count,
                    "nets_defined": self._pcb.net_count,
                }
            )

        return summary


def create_pcb_from_schematic(
    schematic: str | Path,
    board_size: tuple[float, float] = (100.0, 100.0),
    layers: int = 2,
    title: str = "",
    revision: str = "1.0",
    company: str = "",
    auto_place: bool = True,
    placement_spacing: float = 15.0,
    placement_columns: int = 10,
) -> PCB:
    """
    Create a PCB from a schematic file in one step.

    This is a convenience function that combines the entire workflow:
    1. Export netlist from schematic
    2. Create blank PCB with specified dimensions
    3. Add footprints for all components
    4. Assign nets to pads based on connectivity

    Args:
        schematic: Path to the .kicad_sch schematic file
        board_size: Board (width, height) in mm (default (100, 100))
        layers: Number of copper layers (2 or 4, default 2)
        title: Board title (default: schematic filename)
        revision: Board revision (default "1.0")
        company: Company name for title block
        auto_place: Whether to automatically place components (default True)
        placement_spacing: Spacing between auto-placed components in mm
        placement_columns: Number of columns for auto-placement grid

    Returns:
        The fully populated PCB object ready to save

    Example:
        >>> pcb = create_pcb_from_schematic(
        ...     "project.kicad_sch",
        ...     board_size=(160, 100),
        ...     layers=4,
        ... )
        >>> pcb.save("project.kicad_pcb")

    Note:
        The auto-placement uses a simple grid layout. For production boards,
        you'll want to use PCBFromSchematic for more control over placement.
    """
    workflow = PCBFromSchematic(schematic)

    # Create PCB
    width, height = board_size
    workflow.create_pcb(
        width=width,
        height=height,
        layers=layers,
        title=title,
        revision=revision,
        company=company,
    )

    # Place components
    if auto_place:
        workflow.place_all_components(
            spacing=placement_spacing,
            columns=placement_columns,
        )

    # Assign nets
    workflow.assign_nets()

    # Return the PCB
    assert workflow.pcb is not None
    return workflow.pcb


__all__ = [
    "PCBFromSchematic",
    "ComponentInfo",
    "PlacementResult",
    "NetAssignmentResult",
    "create_pcb_from_schematic",
]
