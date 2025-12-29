"""
Layer stack and via definitions for PCB routing.

This module provides:
- Layer: Enum for routing layers (F.Cu, In1.Cu, etc.)
- LayerType: Signal, plane, or mixed layer types
- LayerDefinition: Individual layer configuration
- LayerStack: Complete PCB stackup with presets
- ViaType: Through, blind, buried, micro via types
- ViaDefinition: Via manufacturing parameters
- ViaRules: Via placement and spacing rules
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Layer(Enum):
    """Routing layers - supports up to 6 layers."""

    F_CU = 0  # Top copper (outer)
    IN1_CU = 1  # Inner 1
    IN2_CU = 2  # Inner 2
    IN3_CU = 3  # Inner 3
    IN4_CU = 4  # Inner 4
    B_CU = 5  # Bottom copper (outer)

    @property
    def kicad_name(self) -> str:
        return {
            Layer.F_CU: "F.Cu",
            Layer.IN1_CU: "In1.Cu",
            Layer.IN2_CU: "In2.Cu",
            Layer.IN3_CU: "In3.Cu",
            Layer.IN4_CU: "In4.Cu",
            Layer.B_CU: "B.Cu",
        }[self]

    @property
    def is_outer(self) -> bool:
        """Check if this is an outer (component) layer."""
        return self in (Layer.F_CU, Layer.B_CU)


class LayerType(Enum):
    """Layer function type."""

    SIGNAL = "signal"  # Routable signal layer
    PLANE = "plane"  # Power/ground plane (no routing, only antipads)
    MIXED = "mixed"  # Plane with limited routing (split planes)


@dataclass
class LayerDefinition:
    """Definition of a single PCB layer."""

    name: str  # KiCad name: "F.Cu", "In1.Cu", etc.
    index: int  # Layer index (0 = top)
    layer_type: LayerType  # SIGNAL, PLANE, or MIXED
    is_outer: bool = False  # True for F.Cu and B.Cu
    plane_net: str = ""  # Net name if this is a plane (e.g., "GND")
    reference_plane: str = ""  # Adjacent plane for impedance control
    copper_weight_oz: float = 1.0  # Copper thickness (1oz = 35µm)

    @property
    def layer_enum(self) -> Layer:
        """Get corresponding Layer enum value."""
        return Layer(self.index)

    @property
    def is_routable(self) -> bool:
        """Check if signals can be routed on this layer."""
        return self.layer_type in (LayerType.SIGNAL, LayerType.MIXED)


@dataclass
class LayerStack:
    """Complete PCB layer stackup configuration."""

    layers: List[LayerDefinition]
    name: str = "Custom"
    description: str = ""

    def __post_init__(self) -> None:
        # Validate layer indices are sequential
        indices = [layer.index for layer in self.layers]
        if indices != list(range(len(self.layers))):
            raise ValueError("Layer indices must be sequential starting from 0")

    @property
    def num_layers(self) -> int:
        return len(self.layers)

    @property
    def signal_layers(self) -> List[LayerDefinition]:
        """Get all routable signal layers."""
        return [layer for layer in self.layers if layer.is_routable]

    @property
    def plane_layers(self) -> List[LayerDefinition]:
        """Get all plane layers."""
        return [layer for layer in self.layers if layer.layer_type == LayerType.PLANE]

    @property
    def outer_layers(self) -> List[LayerDefinition]:
        """Get outer (component) layers."""
        return [layer for layer in self.layers if layer.is_outer]

    def get_layer(self, index: int) -> Optional[LayerDefinition]:
        """Get layer by index."""
        for layer in self.layers:
            if layer.index == index:
                return layer
        return None

    def get_layer_by_name(self, name: str) -> Optional[LayerDefinition]:
        """Get layer by KiCad name."""
        for layer in self.layers:
            if layer.name == name:
                return layer
        return None

    def layer_enum_to_index(self, layer: Layer) -> int:
        """Map Layer enum to grid index for this stackup."""
        kicad_name = layer.kicad_name
        for layer_def in self.layers:
            if layer_def.name == kicad_name:
                return layer_def.index
        raise ValueError(f"Layer {layer.name} not in stack {self.name}")

    def index_to_layer_enum(self, index: int) -> Layer:
        """Map grid index to Layer enum for this stackup."""
        if index < 0 or index >= len(self.layers):
            raise ValueError(f"Index {index} out of range for {self.num_layers}-layer stack")
        layer_def = self.layers[index]
        for layer in Layer:
            if layer.kicad_name == layer_def.name:
                return layer
        raise ValueError(f"No Layer enum for {layer_def.name}")

    def get_routable_indices(self) -> List[int]:
        """Get grid indices of all routable layers."""
        return [layer.index for layer in self.layers if layer.is_routable]

    def is_plane_layer(self, index: int) -> bool:
        """Check if grid index is a plane layer."""
        layer = self.get_layer(index)
        return layer is not None and layer.layer_type == LayerType.PLANE

    # =========================================================================
    # STANDARD LAYER STACK PRESETS
    # =========================================================================

    @classmethod
    def two_layer(cls) -> "LayerStack":
        """Standard 2-layer board: Signal-Signal."""
        return cls(
            name="2-Layer",
            description="Standard 2-layer PCB",
            layers=[
                LayerDefinition("F.Cu", 0, LayerType.SIGNAL, is_outer=True),
                LayerDefinition("B.Cu", 1, LayerType.SIGNAL, is_outer=True),
            ],
        )

    @classmethod
    def four_layer_sig_gnd_pwr_sig(cls) -> "LayerStack":
        """Standard 4-layer: Signal-GND-PWR-Signal."""
        return cls(
            name="4-Layer SIG-GND-PWR-SIG",
            description="Standard 4-layer with GND and PWR planes",
            layers=[
                LayerDefinition(
                    "F.Cu", 0, LayerType.SIGNAL, is_outer=True, reference_plane="In1.Cu"
                ),
                LayerDefinition("In1.Cu", 1, LayerType.PLANE, plane_net="GND"),
                LayerDefinition("In2.Cu", 2, LayerType.PLANE, plane_net="+3.3V"),
                LayerDefinition(
                    "B.Cu", 3, LayerType.SIGNAL, is_outer=True, reference_plane="In2.Cu"
                ),
            ],
        )

    @classmethod
    def four_layer_sig_sig_gnd_pwr(cls) -> "LayerStack":
        """4-layer with 2 signal + 2 plane: Signal-Signal-GND-PWR."""
        return cls(
            name="4-Layer SIG-SIG-GND-PWR",
            description="4-layer with 2 signal layers",
            layers=[
                LayerDefinition("F.Cu", 0, LayerType.SIGNAL, is_outer=True),
                LayerDefinition("In1.Cu", 1, LayerType.SIGNAL, reference_plane="In2.Cu"),
                LayerDefinition("In2.Cu", 2, LayerType.PLANE, plane_net="GND"),
                LayerDefinition("B.Cu", 3, LayerType.MIXED, is_outer=True, plane_net="+3.3V"),
            ],
        )

    @classmethod
    def six_layer_sig_gnd_sig_sig_pwr_sig(cls) -> "LayerStack":
        """6-layer high-density: Signal-GND-Signal-Signal-PWR-Signal."""
        return cls(
            name="6-Layer SIG-GND-SIG-SIG-PWR-SIG",
            description="6-layer high-density with 4 signal layers",
            layers=[
                LayerDefinition(
                    "F.Cu", 0, LayerType.SIGNAL, is_outer=True, reference_plane="In1.Cu"
                ),
                LayerDefinition("In1.Cu", 1, LayerType.PLANE, plane_net="GND"),
                LayerDefinition("In2.Cu", 2, LayerType.SIGNAL, reference_plane="In1.Cu"),
                LayerDefinition("In3.Cu", 3, LayerType.SIGNAL, reference_plane="In4.Cu"),
                LayerDefinition("In4.Cu", 4, LayerType.PLANE, plane_net="+3.3V"),
                LayerDefinition(
                    "B.Cu", 5, LayerType.SIGNAL, is_outer=True, reference_plane="In4.Cu"
                ),
            ],
        )

    def __repr__(self) -> str:
        layer_strs = []
        for layer in self.layers:
            type_str = layer.layer_type.value[0].upper()  # S/P/M
            net_str = f" ({layer.plane_net})" if layer.plane_net else ""
            layer_strs.append(f"L{layer.index + 1}:{layer.name}[{type_str}]{net_str}")
        return f"LayerStack({self.name}: {' | '.join(layer_strs)})"


class ViaType(Enum):
    """Via types by layer span."""

    THROUGH = "through"  # Spans all layers (standard)
    BLIND_TOP = "blind_top"  # Top to inner layer
    BLIND_BOT = "blind_bot"  # Bottom to inner layer
    BURIED = "buried"  # Inner to inner only
    MICRO = "micro"  # Single layer span (HDI)


@dataclass
class ViaDefinition:
    """Definition of a via type with manufacturing parameters."""

    via_type: ViaType
    drill_mm: float  # Drill diameter
    annular_ring_mm: float  # Ring width around drill
    start_layer: int = 0  # Starting layer index
    end_layer: int = -1  # Ending layer index (-1 = bottom)
    cost_multiplier: float = 1.0  # Relative routing cost
    name: str = ""  # Optional name

    @property
    def diameter(self) -> float:
        """Total via pad diameter."""
        return self.drill_mm + 2 * self.annular_ring_mm

    def spans_layer(self, layer: int, num_layers: int) -> bool:
        """Check if this via passes through the given layer."""
        end = self.end_layer if self.end_layer >= 0 else num_layers - 1
        return self.start_layer <= layer <= end

    def blocks_layer(self, layer: int, num_layers: int) -> bool:
        """Check if via creates an obstacle on given layer."""
        return self.spans_layer(layer, num_layers)


@dataclass
class ViaRules:
    """Manufacturing rules for via types."""

    # Via type availability (depends on PCB fab capabilities)
    allow_blind: bool = False  # Blind vias (outer to inner)
    allow_buried: bool = False  # Buried vias (inner to inner)
    allow_micro: bool = False  # Micro vias (HDI, laser drilled)
    allow_stacked: bool = False  # Stacked vias (via on via)
    allow_via_in_pad: bool = False  # Via in SMD pad (needs fill+plate)

    # Spacing rules
    min_via_to_via_mm: float = 0.2  # Minimum via-to-via spacing
    min_via_to_trace_mm: float = 0.15  # Minimum via-to-trace spacing
    min_via_to_plane_mm: float = 0.2  # Antipad clearance in planes

    # Standard via definitions
    through_via: ViaDefinition = field(
        default_factory=lambda: ViaDefinition(
            ViaType.THROUGH,
            drill_mm=0.3,
            annular_ring_mm=0.15,
            cost_multiplier=1.0,
            name="Standard Through",
        )
    )

    # Optional via types (used if allow_* is True)
    blind_via: Optional[ViaDefinition] = None
    buried_via: Optional[ViaDefinition] = None
    micro_via: Optional[ViaDefinition] = None

    def get_available_vias(self, num_layers: int) -> List[ViaDefinition]:
        """Get list of available via types for this stackup."""
        vias = [self.through_via]

        if self.allow_blind and self.blind_via:
            vias.append(self.blind_via)
        if self.allow_buried and self.buried_via and num_layers >= 4:
            vias.append(self.buried_via)
        if self.allow_micro and self.micro_via:
            vias.append(self.micro_via)

        return vias

    def get_best_via(
        self, from_layer: int, to_layer: int, num_layers: int
    ) -> Optional[ViaDefinition]:
        """Get the lowest-cost via that can connect two layers."""
        candidates = []

        for via in self.get_available_vias(num_layers):
            end = via.end_layer if via.end_layer >= 0 else num_layers - 1
            start = via.start_layer

            if start <= from_layer <= end and start <= to_layer <= end:
                candidates.append(via)

        if not candidates:
            return None

        return min(candidates, key=lambda v: v.cost_multiplier)

    @classmethod
    def standard_2layer(cls) -> "ViaRules":
        """Standard rules for 2-layer PCB."""
        return cls(
            through_via=ViaDefinition(
                ViaType.THROUGH,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                start_layer=0,
                end_layer=1,
                name="Through",
            )
        )

    @classmethod
    def standard_4layer(cls) -> "ViaRules":
        """Standard rules for 4-layer PCB (through vias only)."""
        return cls(
            through_via=ViaDefinition(
                ViaType.THROUGH,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                start_layer=0,
                end_layer=3,
                name="Through",
            )
        )

    @classmethod
    def hdi_4layer(cls) -> "ViaRules":
        """HDI rules for 4-layer with blind and micro vias."""
        return cls(
            allow_blind=True,
            allow_micro=True,
            through_via=ViaDefinition(
                ViaType.THROUGH,
                drill_mm=0.25,
                annular_ring_mm=0.1,
                start_layer=0,
                end_layer=3,
                cost_multiplier=1.0,
                name="Through",
            ),
            blind_via=ViaDefinition(
                ViaType.BLIND_TOP,
                drill_mm=0.15,
                annular_ring_mm=0.1,
                start_layer=0,
                end_layer=1,
                cost_multiplier=1.5,
                name="Blind Top",
            ),
            micro_via=ViaDefinition(
                ViaType.MICRO,
                drill_mm=0.1,
                annular_ring_mm=0.075,
                start_layer=0,
                end_layer=1,
                cost_multiplier=0.5,
                name="Micro",
            ),
        )

    @classmethod
    def standard_6layer(cls) -> "ViaRules":
        """Standard rules for 6-layer PCB."""
        return cls(
            through_via=ViaDefinition(
                ViaType.THROUGH,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                start_layer=0,
                end_layer=5,
                name="Through",
            )
        )
