"""PCB stackup representation for electromagnetic calculations.

Provides the Stackup class for parsing and representing PCB layer stackups,
including copper thicknesses, dielectric properties, and manufacturer presets.

Example::

    from kicad_tools.physics import Stackup
    from kicad_tools.schema.pcb import PCB

    # Parse from KiCad board file
    pcb = PCB.load("board.kicad_pcb")
    stackup = Stackup.from_pcb(pcb)

    # Or use manufacturer preset
    stackup = Stackup.jlcpcb_4layer()

    # Get layer properties
    h = stackup.get_dielectric_height("F.Cu")  # Height to reference plane
    er = stackup.get_dielectric_constant("F.Cu")  # Dielectric constant
    t = stackup.get_copper_thickness("F.Cu")  # Copper thickness
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .constants import (
    COPPER_1OZ,
    COPPER_HALF_OZ,
    FR4_STANDARD,
    copper_thickness_from_oz,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class LayerType(Enum):
    """Type of layer in the stackup."""

    COPPER = "copper"
    DIELECTRIC = "dielectric"  # Prepreg or core
    SOLDER_MASK = "solder mask"
    SILK_SCREEN = "silk screen"


@dataclass
class StackupLayer:
    """Single layer in a PCB stackup.

    Attributes:
        name: Layer name (e.g., "F.Cu", "prepreg 1", "core")
        layer_type: Type of layer (copper, dielectric, etc.)
        thickness_mm: Layer thickness in millimeters
        material: Material name (e.g., "FR4", "copper")
        epsilon_r: Relative permittivity (for dielectrics)
        loss_tangent: Loss tangent tan(delta) (for dielectrics)
        copper_weight_oz: Copper weight in oz/ft^2 (for copper layers)
    """

    name: str
    layer_type: LayerType
    thickness_mm: float = 0.0
    material: str = ""
    epsilon_r: float = 0.0
    loss_tangent: float = 0.0
    copper_weight_oz: float | None = None

    @property
    def is_copper(self) -> bool:
        """Check if this is a copper layer."""
        return self.layer_type == LayerType.COPPER

    @property
    def is_dielectric(self) -> bool:
        """Check if this is a dielectric layer."""
        return self.layer_type == LayerType.DIELECTRIC

    @property
    def is_signal_layer(self) -> bool:
        """Check if this is a signal copper layer (F.Cu, B.Cu, In*.Cu)."""
        if not self.is_copper:
            return False
        name = self.name.lower()
        return name.endswith(".cu")


@dataclass
class Stackup:
    """Complete PCB layer stackup for electromagnetic calculations.

    The stackup is ordered from top to bottom:
    - layers[0] is the top layer (typically F.Cu or solder mask)
    - layers[-1] is the bottom layer (typically B.Cu or solder mask)

    Attributes:
        layers: Ordered list of layers from top to bottom
        board_thickness_mm: Total board thickness in mm
        copper_finish: Surface finish (e.g., "ENIG", "HASL")
    """

    layers: list[StackupLayer] = field(default_factory=list)
    board_thickness_mm: float = 1.6
    copper_finish: str = ""

    @classmethod
    def from_pcb(cls, pcb: PCB) -> Stackup:
        """Parse stackup from a KiCad PCB file.

        KiCad 7+ stores stackup information in the (setup (stackup ...)) section.
        For older files or files without explicit stackup, this creates a
        default 2-layer stackup based on the board's copper layers.

        Args:
            pcb: Loaded PCB object

        Returns:
            Stackup object with parsed or default layer information
        """
        setup = pcb.setup
        if not setup or not setup.stackup:
            # No stackup defined, create default based on copper layer count
            return cls._create_default_stackup(pcb)

        # Parse explicit stackup from KiCad file
        layers = []
        for layer_data in setup.stackup:
            layer_type = cls._parse_layer_type(layer_data.type)

            layer = StackupLayer(
                name=layer_data.name,
                layer_type=layer_type,
                thickness_mm=layer_data.thickness,
                material=layer_data.material,
                epsilon_r=layer_data.epsilon_r,
            )

            # Infer copper weight from thickness
            if layer_type == LayerType.COPPER and layer.thickness_mm > 0:
                # Approximate oz from thickness (35um = 1oz)
                layer.copper_weight_oz = layer.thickness_mm / 0.035

            layers.append(layer)

        # Calculate total board thickness
        total_thickness = sum(layer.thickness_mm for layer in layers)

        return cls(
            layers=layers,
            board_thickness_mm=total_thickness if total_thickness > 0 else 1.6,
            copper_finish=setup.copper_finish if hasattr(setup, "copper_finish") else "",
        )

    @classmethod
    def _create_default_stackup(cls, pcb: PCB) -> Stackup:
        """Create a default stackup for boards without explicit stackup data.

        Args:
            pcb: Loaded PCB object

        Returns:
            Default stackup based on copper layer count
        """
        copper_layers = pcb.copper_layers
        num_copper = len(copper_layers)

        if num_copper <= 2:
            return cls.default_2layer()
        elif num_copper == 4:
            return cls.jlcpcb_4layer()
        elif num_copper == 6:
            return cls.default_6layer()
        else:
            # Generic multi-layer
            return cls._create_generic_stackup(num_copper)

    @classmethod
    def _create_generic_stackup(cls, num_copper_layers: int) -> Stackup:
        """Create a generic stackup for N copper layers.

        Args:
            num_copper_layers: Number of copper layers

        Returns:
            Generic stackup with sensible defaults
        """
        layers = []

        # Outer layer copper (1oz)
        layers.append(
            StackupLayer(
                name="F.Cu",
                layer_type=LayerType.COPPER,
                thickness_mm=COPPER_1OZ.thickness_mm,
                material="copper",
                copper_weight_oz=1.0,
            )
        )

        # Inner layers with prepreg/core sandwich
        for i in range(1, num_copper_layers - 1):
            # Dielectric before inner layer
            dielectric_name = "core" if i % 2 == 0 else "prepreg"
            layers.append(
                StackupLayer(
                    name=f"{dielectric_name} {i}",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.2,
                    material="FR4",
                    epsilon_r=FR4_STANDARD.epsilon_r,
                    loss_tangent=FR4_STANDARD.loss_tangent,
                )
            )

            # Inner copper layer (0.5oz typical)
            layers.append(
                StackupLayer(
                    name=f"In{i}.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=COPPER_HALF_OZ.thickness_mm,
                    material="copper",
                    copper_weight_oz=0.5,
                )
            )

        # Final dielectric before bottom
        layers.append(
            StackupLayer(
                name="prepreg bottom",
                layer_type=LayerType.DIELECTRIC,
                thickness_mm=0.2,
                material="FR4",
                epsilon_r=FR4_STANDARD.epsilon_r,
                loss_tangent=FR4_STANDARD.loss_tangent,
            )
        )

        # Bottom copper (1oz)
        layers.append(
            StackupLayer(
                name="B.Cu",
                layer_type=LayerType.COPPER,
                thickness_mm=COPPER_1OZ.thickness_mm,
                material="copper",
                copper_weight_oz=1.0,
            )
        )

        total_thickness = sum(layer.thickness_mm for layer in layers)
        return cls(layers=layers, board_thickness_mm=total_thickness)

    @staticmethod
    def _parse_layer_type(type_str: str) -> LayerType:
        """Parse layer type from KiCad string.

        Args:
            type_str: Type string from KiCad (e.g., "copper", "prepreg", "core")

        Returns:
            LayerType enum value
        """
        type_lower = type_str.lower()
        if type_lower == "copper":
            return LayerType.COPPER
        elif type_lower in ("prepreg", "core", "dielectric"):
            return LayerType.DIELECTRIC
        elif "mask" in type_lower:
            return LayerType.SOLDER_MASK
        elif "silk" in type_lower:
            return LayerType.SILK_SCREEN
        else:
            return LayerType.DIELECTRIC  # Default to dielectric

    # Manufacturer presets

    @classmethod
    def default_2layer(cls, thickness_mm: float = 1.6) -> Stackup:
        """Create a generic 2-layer FR4 stackup.

        Standard 2-layer board with 1oz copper on both sides.

        Args:
            thickness_mm: Total board thickness (default 1.6mm)

        Returns:
            2-layer Stackup
        """
        dielectric_thickness = thickness_mm - 2 * COPPER_1OZ.thickness_mm

        return cls(
            layers=[
                StackupLayer(
                    name="F.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=COPPER_1OZ.thickness_mm,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
                StackupLayer(
                    name="core",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=dielectric_thickness,
                    material="FR4",
                    epsilon_r=FR4_STANDARD.epsilon_r,
                    loss_tangent=FR4_STANDARD.loss_tangent,
                ),
                StackupLayer(
                    name="B.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=COPPER_1OZ.thickness_mm,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
            ],
            board_thickness_mm=thickness_mm,
        )

    @classmethod
    def jlcpcb_4layer(cls) -> Stackup:
        """JLCPCB JLC04161H-3313 4-layer stackup.

        Standard 1.6mm 4-layer:
        - F.Cu: 35um (1oz)
        - Prepreg: 0.2104mm (7075), er=4.05
        - In1.Cu: 17.5um (0.5oz)
        - Core: 1.065mm, er=4.6
        - In2.Cu: 17.5um (0.5oz)
        - Prepreg: 0.2104mm (7075), er=4.05
        - B.Cu: 35um (1oz)

        Total: ~1.6mm

        Returns:
            JLCPCB 4-layer Stackup
        """
        return cls(
            layers=[
                StackupLayer(
                    name="F.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.035,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
                StackupLayer(
                    name="prepreg 1",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.2104,
                    material="FR4 7628",
                    epsilon_r=4.05,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="In1.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="core",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=1.065,
                    material="FR4",
                    epsilon_r=4.6,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="In2.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="prepreg 2",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.2104,
                    material="FR4 7628",
                    epsilon_r=4.05,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="B.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.035,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
            ],
            board_thickness_mm=1.6,
            copper_finish="HASL",
        )

    @classmethod
    def oshpark_4layer(cls) -> Stackup:
        """OSH Park 4-layer stackup.

        OSH Park 4-layer:
        - F.Cu: 35um (1oz)
        - Prepreg: 0.17mm, er=4.5
        - In1.Cu: 17.5um (0.5oz)
        - Core: 1.2mm, er=4.5
        - In2.Cu: 17.5um (0.5oz)
        - Prepreg: 0.17mm, er=4.5
        - B.Cu: 35um (1oz)

        Total: ~1.6mm

        Returns:
            OSH Park 4-layer Stackup
        """
        return cls(
            layers=[
                StackupLayer(
                    name="F.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.035,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
                StackupLayer(
                    name="prepreg 1",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.17,
                    material="FR408",
                    epsilon_r=4.5,
                    loss_tangent=0.012,
                ),
                StackupLayer(
                    name="In1.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="core",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=1.2,
                    material="FR408",
                    epsilon_r=4.5,
                    loss_tangent=0.012,
                ),
                StackupLayer(
                    name="In2.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="prepreg 2",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.17,
                    material="FR408",
                    epsilon_r=4.5,
                    loss_tangent=0.012,
                ),
                StackupLayer(
                    name="B.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.035,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
            ],
            board_thickness_mm=1.6,
            copper_finish="ENIG",
        )

    @classmethod
    def default_6layer(cls) -> Stackup:
        """Create a generic 6-layer FR4 stackup.

        Standard 6-layer with 1oz outer and 0.5oz inner copper.

        Returns:
            6-layer Stackup
        """
        return cls(
            layers=[
                StackupLayer(
                    name="F.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.035,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
                StackupLayer(
                    name="prepreg 1",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.18,
                    material="FR4",
                    epsilon_r=4.5,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="In1.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="core 1",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.36,
                    material="FR4",
                    epsilon_r=4.5,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="In2.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="prepreg 2",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.18,
                    material="FR4",
                    epsilon_r=4.5,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="In3.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="core 2",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.36,
                    material="FR4",
                    epsilon_r=4.5,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="In4.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.0175,
                    material="copper",
                    copper_weight_oz=0.5,
                ),
                StackupLayer(
                    name="prepreg 3",
                    layer_type=LayerType.DIELECTRIC,
                    thickness_mm=0.18,
                    material="FR4",
                    epsilon_r=4.5,
                    loss_tangent=0.02,
                ),
                StackupLayer(
                    name="B.Cu",
                    layer_type=LayerType.COPPER,
                    thickness_mm=0.035,
                    material="copper",
                    copper_weight_oz=1.0,
                ),
            ],
            board_thickness_mm=1.6,
        )

    # Query methods

    @property
    def copper_layers(self) -> list[StackupLayer]:
        """Get all copper layers in order from top to bottom."""
        return [layer for layer in self.layers if layer.is_copper]

    @property
    def dielectric_layers(self) -> list[StackupLayer]:
        """Get all dielectric layers in order."""
        return [layer for layer in self.layers if layer.is_dielectric]

    @property
    def num_copper_layers(self) -> int:
        """Get number of copper layers."""
        return len(self.copper_layers)

    def get_layer(self, name: str) -> StackupLayer | None:
        """Get a layer by name.

        Args:
            name: Layer name (e.g., "F.Cu", "In1.Cu", "core")

        Returns:
            StackupLayer if found, None otherwise
        """
        for layer in self.layers:
            if layer.name == name:
                return layer
        return None

    def get_layer_index(self, name: str) -> int:
        """Get the index of a layer in the stackup.

        Args:
            name: Layer name

        Returns:
            Index (0 = top), or -1 if not found
        """
        for i, layer in enumerate(self.layers):
            if layer.name == name:
                return i
        return -1

    def is_outer_layer(self, layer_name: str) -> bool:
        """Check if a layer is on the outside of the board (microstrip geometry).

        Outer layers have dielectric on only one side.

        Args:
            layer_name: Layer name (e.g., "F.Cu", "B.Cu")

        Returns:
            True if layer is on top or bottom of stackup
        """
        copper_layers = self.copper_layers
        if not copper_layers:
            return False

        # Check if this is the first or last copper layer
        return layer_name in (copper_layers[0].name, copper_layers[-1].name)

    def get_copper_thickness(self, layer_name: str) -> float:
        """Get copper thickness for a layer in mm.

        Args:
            layer_name: Layer name (e.g., "F.Cu")

        Returns:
            Thickness in mm, or 0.035 (1oz) as default
        """
        layer = self.get_layer(layer_name)
        if layer and layer.is_copper:
            if layer.thickness_mm > 0:
                return layer.thickness_mm
            if layer.copper_weight_oz:
                return copper_thickness_from_oz(layer.copper_weight_oz)
        return 0.035  # Default 1oz

    def get_dielectric_above(self, layer_name: str) -> StackupLayer | None:
        """Get the dielectric layer above a copper layer.

        For outer layers (F.Cu), this is the dielectric between the
        copper and the first reference plane.

        Args:
            layer_name: Copper layer name (e.g., "F.Cu")

        Returns:
            Dielectric layer above, or None if not found
        """
        layer_idx = self.get_layer_index(layer_name)
        if layer_idx < 0:
            return None

        # Search downward (higher index) for next dielectric
        for i in range(layer_idx + 1, len(self.layers)):
            if self.layers[i].is_dielectric:
                return self.layers[i]

        return None

    def get_dielectric_below(self, layer_name: str) -> StackupLayer | None:
        """Get the dielectric layer below a copper layer.

        Args:
            layer_name: Copper layer name

        Returns:
            Dielectric layer below, or None if not found
        """
        layer_idx = self.get_layer_index(layer_name)
        if layer_idx < 0:
            return None

        # Search upward (lower index) for dielectric
        for i in range(layer_idx - 1, -1, -1):
            if self.layers[i].is_dielectric:
                return self.layers[i]

        return None

    def get_dielectric_height(self, layer_name: str) -> float:
        """Get height from copper layer to nearest reference plane.

        For microstrip (outer layers), this is the dielectric thickness
        to the ground plane below.

        For stripline (inner layers), this returns the distance to the
        nearest reference plane (smaller of above/below distances).

        Args:
            layer_name: Copper layer name (e.g., "F.Cu", "In1.Cu")

        Returns:
            Height in mm to reference plane
        """
        if self.is_outer_layer(layer_name):
            # Microstrip: height to plane below
            dielectric = self.get_dielectric_above(layer_name)
            if dielectric:
                return dielectric.thickness_mm
        else:
            # Stripline: distance to nearest plane
            above = self.get_dielectric_above(layer_name)
            below = self.get_dielectric_below(layer_name)

            heights = []
            if above:
                heights.append(above.thickness_mm)
            if below:
                heights.append(below.thickness_mm)

            if heights:
                return min(heights)

        # Default fallback
        return 0.2

    def get_dielectric_constant(self, layer_name: str) -> float:
        """Get effective dielectric constant for a copper layer.

        For microstrip, uses the dielectric above (between trace and ground).
        For stripline, uses average of surrounding dielectrics.

        Args:
            layer_name: Copper layer name

        Returns:
            Dielectric constant (epsilon_r)
        """
        if self.is_outer_layer(layer_name):
            # Microstrip: use dielectric above
            dielectric = self.get_dielectric_above(layer_name)
            if dielectric and dielectric.epsilon_r > 0:
                return dielectric.epsilon_r
        else:
            # Stripline: average surrounding dielectrics
            above = self.get_dielectric_above(layer_name)
            below = self.get_dielectric_below(layer_name)

            eps_values = []
            if above and above.epsilon_r > 0:
                eps_values.append(above.epsilon_r)
            if below and below.epsilon_r > 0:
                eps_values.append(below.epsilon_r)

            if eps_values:
                return sum(eps_values) / len(eps_values)

        # Default FR4
        return FR4_STANDARD.epsilon_r

    def get_loss_tangent(self, layer_name: str) -> float:
        """Get dielectric loss tangent for a copper layer.

        Args:
            layer_name: Copper layer name

        Returns:
            Loss tangent (tan delta)
        """
        dielectric = self.get_dielectric_above(layer_name)
        if dielectric and dielectric.loss_tangent > 0:
            return dielectric.loss_tangent
        return FR4_STANDARD.loss_tangent

    def get_reference_plane_distance(self, layer_name: str) -> float:
        """Get distance from a signal layer to the nearest reference plane.

        This is equivalent to get_dielectric_height for most cases,
        but explicitly named for clarity in impedance calculations.

        Args:
            layer_name: Signal layer name (e.g., "F.Cu")

        Returns:
            Distance in mm to nearest reference plane (ground/power)
        """
        return self.get_dielectric_height(layer_name)

    def get_stripline_geometry(self, layer_name: str) -> tuple[float, float]:
        """Get distances to both reference planes for stripline geometry.

        For inner layers, returns the distance to both the upper and lower
        reference planes. For outer layers, returns (h, h) where h is the
        single dielectric height.

        Args:
            layer_name: Inner copper layer name (e.g., "In1.Cu")

        Returns:
            Tuple of (h1, h2) where h1 is distance to upper plane and
            h2 is distance to lower plane, both in mm.
        """
        if self.is_outer_layer(layer_name):
            # Outer layer - return same height for both
            h = self.get_dielectric_height(layer_name)
            return (h, h)

        # Inner layer - get both distances
        above = self.get_dielectric_above(layer_name)
        below = self.get_dielectric_below(layer_name)

        h1 = above.thickness_mm if above else 0.2
        h2 = below.thickness_mm if below else 0.2

        return (h1, h2)

    def summary(self) -> dict:
        """Get a summary of the stackup.

        Returns:
            Dictionary with stackup information
        """
        return {
            "board_thickness_mm": self.board_thickness_mm,
            "num_copper_layers": self.num_copper_layers,
            "copper_finish": self.copper_finish,
            "layers": [
                {
                    "name": layer.name,
                    "type": layer.layer_type.value,
                    "thickness_mm": layer.thickness_mm,
                    "material": layer.material,
                    "epsilon_r": layer.epsilon_r if layer.is_dielectric else None,
                    "copper_oz": layer.copper_weight_oz if layer.is_copper else None,
                }
                for layer in self.layers
            ],
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"Stackup(layers={self.num_copper_layers}L, thickness={self.board_thickness_mm}mm)"
