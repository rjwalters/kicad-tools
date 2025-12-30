"""
PCB Commands - High-level actions for LLM-driven layout.

Commands represent strategic decisions, not geometric operations.
The interpreter translates these into precise PCB modifications.

Examples:
    "Route MCLK from oscillator to GPIO header, avoiding analog section"
    → RouteNetCommand(net="MCLK", avoid_regions=["analog"], ...)

    "Move U2 closer to C3 for better decoupling"
    → PlaceComponentCommand(ref="U2", near="C3", ...)

    "Delete the traces causing shorts near the GPIO header"
    → DeleteTraceCommand(near=(68, 54), net="MCLK", ...)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CommandType(Enum):
    """Types of PCB commands."""

    # Placement
    PLACE_COMPONENT = "place_component"
    MOVE_COMPONENT = "move_component"
    ROTATE_COMPONENT = "rotate_component"
    SWAP_COMPONENTS = "swap_components"

    # Routing
    ROUTE_NET = "route_net"
    ROUTE_DIRECT = "route_direct"  # Point to point
    ROUTE_ESCAPE = "route_escape"  # Escape from dense area

    # Modification
    DELETE_TRACE = "delete_trace"
    DELETE_VIA = "delete_via"
    DELETE_NET_ROUTING = "delete_net_routing"
    REROUTE_NET = "reroute_net"

    # Via operations
    ADD_VIA = "add_via"
    CHANGE_LAYER = "change_layer"

    # Zone operations
    DEFINE_ZONE = "define_zone"
    MODIFY_ZONE = "modify_zone"

    # Analysis
    CHECK_DRC = "check_drc"
    FIND_PATH = "find_path"
    MEASURE_CLEARANCE = "measure_clearance"


@dataclass
class CommandResult:
    """Result of executing a command."""

    success: bool
    command_type: CommandType
    message: str
    details: dict = field(default_factory=dict)

    # For routing commands
    path: Optional[list[tuple[float, float]]] = None
    vias_added: int = 0
    trace_length: float = 0.0

    # For placement commands
    new_position: Optional[tuple[float, float]] = None
    new_rotation: Optional[float] = None

    # For violations
    violations_created: int = 0
    violations_resolved: int = 0

    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"[{status}] {self.command_type.value}: {self.message}"


class Command(ABC):
    """Base class for PCB commands."""

    @property
    @abstractmethod
    def command_type(self) -> CommandType:
        """Return the command type."""
        ...

    @abstractmethod
    def describe(self) -> str:
        """Return a human-readable description of the command."""
        ...

    @abstractmethod
    def to_dict(self) -> dict:
        """Serialize command to dictionary."""
        ...

    @classmethod
    @abstractmethod
    def from_dict(cls, data: dict) -> "Command":
        """Deserialize command from dictionary."""
        ...


# =============================================================================
# Placement Commands
# =============================================================================


@dataclass
class PlaceComponentCommand(Command):
    """Place or move a component.

    Strategic placement based on relationships, not just coordinates.

    Examples:
        PlaceComponentCommand(ref="C3", near="U2", offset=(2, 0))
        PlaceComponentCommand(ref="U1", region="analog", rotation=90)
        PlaceComponentCommand(ref="J1", at=(68.5, 54.3), fixed=True)
    """

    ref: str  # Component reference

    # Position specification (one of these)
    at: Optional[tuple[float, float]] = None  # Absolute position
    near: Optional[str] = None  # Near another component
    region: Optional[str] = None  # In named region

    # Relative positioning
    offset: tuple[float, float] = (0, 0)  # Offset from 'near' or region center

    # Orientation
    rotation: Optional[float] = None  # Absolute rotation
    face: Optional[str] = None  # "north", "south", "east", "west"

    # Constraints
    fixed: bool = False  # Mark as fixed after placement

    @property
    def command_type(self) -> CommandType:
        return CommandType.PLACE_COMPONENT

    def describe(self) -> str:
        parts = [f"Place {self.ref}"]
        if self.at:
            parts.append(f"at ({self.at[0]:.1f}, {self.at[1]:.1f})")
        elif self.near:
            parts.append(f"near {self.near}")
            if self.offset != (0, 0):
                parts.append(f"offset ({self.offset[0]:.1f}, {self.offset[1]:.1f})")
        elif self.region:
            parts.append(f"in {self.region} region")
        if self.rotation is not None:
            parts.append(f"rotated {self.rotation}°")
        if self.fixed:
            parts.append("(fixed)")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "type": "place_component",
            "ref": self.ref,
            "at": self.at,
            "near": self.near,
            "region": self.region,
            "offset": self.offset,
            "rotation": self.rotation,
            "face": self.face,
            "fixed": self.fixed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlaceComponentCommand":
        return cls(
            ref=data["ref"],
            at=tuple(data["at"]) if data.get("at") else None,
            near=data.get("near"),
            region=data.get("region"),
            offset=tuple(data.get("offset", (0, 0))),
            rotation=data.get("rotation"),
            face=data.get("face"),
            fixed=data.get("fixed", False),
        )


# =============================================================================
# Routing Commands
# =============================================================================


@dataclass
class RouteNetCommand(Command):
    """Route a net with strategic constraints.

    This is the primary routing command. It specifies WHAT to route and
    WHERE to avoid, letting the interpreter figure out HOW.

    Examples:
        RouteNetCommand(net="MCLK", avoid_regions=["analog"])
        RouteNetCommand(net="GND", prefer_layer="In1.Cu", use_plane=True)
        RouteNetCommand(net="SPI_CLK", prefer_direction="north")
    """

    net: str  # Net name

    # Path constraints
    avoid_regions: list[str] = field(default_factory=list)  # Regions to avoid
    prefer_regions: list[str] = field(default_factory=list)  # Regions to prefer
    avoid_nets: list[str] = field(default_factory=list)  # Nets to stay away from

    # Direction preference
    prefer_direction: Optional[str] = None  # "north", "south", "east", "west"

    # Layer preference
    prefer_layer: Optional[str] = None  # "F.Cu", "B.Cu", etc.
    avoid_layers: list[str] = field(default_factory=list)

    # Via constraints
    minimize_vias: bool = True
    max_vias: Optional[int] = None

    # Special routing modes
    use_plane: bool = False  # For power/ground
    length_match: Optional[str] = None  # Match length to another net
    max_length: Optional[float] = None  # Maximum trace length

    # Trace parameters
    trace_width: Optional[float] = None  # Override default width
    clearance: Optional[float] = None  # Override default clearance

    @property
    def command_type(self) -> CommandType:
        return CommandType.ROUTE_NET

    def describe(self) -> str:
        parts = [f"Route net {self.net}"]
        if self.avoid_regions:
            parts.append(f"avoiding {', '.join(self.avoid_regions)}")
        if self.prefer_direction:
            parts.append(f"via {self.prefer_direction}")
        if self.prefer_layer:
            parts.append(f"on {self.prefer_layer}")
        if self.minimize_vias:
            parts.append("minimizing vias")
        if self.use_plane:
            parts.append("using copper pour")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "type": "route_net",
            "net": self.net,
            "avoid_regions": self.avoid_regions,
            "prefer_regions": self.prefer_regions,
            "avoid_nets": self.avoid_nets,
            "prefer_direction": self.prefer_direction,
            "prefer_layer": self.prefer_layer,
            "avoid_layers": self.avoid_layers,
            "minimize_vias": self.minimize_vias,
            "max_vias": self.max_vias,
            "use_plane": self.use_plane,
            "length_match": self.length_match,
            "max_length": self.max_length,
            "trace_width": self.trace_width,
            "clearance": self.clearance,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RouteNetCommand":
        return cls(
            net=data["net"],
            avoid_regions=data.get("avoid_regions", []),
            prefer_regions=data.get("prefer_regions", []),
            avoid_nets=data.get("avoid_nets", []),
            prefer_direction=data.get("prefer_direction"),
            prefer_layer=data.get("prefer_layer"),
            avoid_layers=data.get("avoid_layers", []),
            minimize_vias=data.get("minimize_vias", True),
            max_vias=data.get("max_vias"),
            use_plane=data.get("use_plane", False),
            length_match=data.get("length_match"),
            max_length=data.get("max_length"),
            trace_width=data.get("trace_width"),
            clearance=data.get("clearance"),
        )


# =============================================================================
# Deletion Commands
# =============================================================================


@dataclass
class DeleteTraceCommand(Command):
    """Delete trace segments.

    Can target traces by:
    - Net name
    - Location
    - Causing specific violations
    """

    # Target specification
    net: Optional[str] = None  # Delete traces of this net
    near: Optional[tuple[float, float]] = None  # Near this location
    radius: float = 2.0  # Search radius in mm
    layer: Optional[str] = None  # On this layer only

    # Delete all traces for net
    delete_all_routing: bool = False

    # Reason for deletion (for logging)
    reason: str = ""

    @property
    def command_type(self) -> CommandType:
        if self.delete_all_routing:
            return CommandType.DELETE_NET_ROUTING
        return CommandType.DELETE_TRACE

    def describe(self) -> str:
        parts = ["Delete traces"]
        if self.net:
            if self.delete_all_routing:
                parts.append(f"all routing for {self.net}")
            else:
                parts.append(f"of {self.net}")
        if self.near:
            parts.append(f"near ({self.near[0]:.1f}, {self.near[1]:.1f})")
        if self.layer:
            parts.append(f"on {self.layer}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "type": "delete_trace",
            "net": self.net,
            "near": self.near,
            "radius": self.radius,
            "layer": self.layer,
            "delete_all_routing": self.delete_all_routing,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeleteTraceCommand":
        return cls(
            net=data.get("net"),
            near=tuple(data["near"]) if data.get("near") else None,
            radius=data.get("radius", 2.0),
            layer=data.get("layer"),
            delete_all_routing=data.get("delete_all_routing", False),
            reason=data.get("reason", ""),
        )


# =============================================================================
# Via Commands
# =============================================================================


@dataclass
class AddViaCommand(Command):
    """Add a via for layer transition."""

    net: str
    position: tuple[float, float]
    from_layer: str = "F.Cu"
    to_layer: str = "B.Cu"
    size: Optional[float] = None  # Use default if not specified
    drill: Optional[float] = None

    @property
    def command_type(self) -> CommandType:
        return CommandType.ADD_VIA

    def describe(self) -> str:
        return (
            f"Add via for {self.net} at ({self.position[0]:.1f}, {self.position[1]:.1f}) "
            f"connecting {self.from_layer} to {self.to_layer}"
        )

    def to_dict(self) -> dict:
        return {
            "type": "add_via",
            "net": self.net,
            "position": self.position,
            "from_layer": self.from_layer,
            "to_layer": self.to_layer,
            "size": self.size,
            "drill": self.drill,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AddViaCommand":
        return cls(
            net=data["net"],
            position=tuple(data["position"]),
            from_layer=data.get("from_layer", "F.Cu"),
            to_layer=data.get("to_layer", "B.Cu"),
            size=data.get("size"),
            drill=data.get("drill"),
        )


# =============================================================================
# Zone Commands
# =============================================================================


@dataclass
class DefineZoneCommand(Command):
    """Define a copper pour zone."""

    net: str
    layer: str
    region: Optional[str] = None  # Use named region bounds
    bounds: Optional[tuple[float, float, float, float]] = None  # Or explicit bounds
    priority: int = 0
    min_thickness: float = 0.2

    @property
    def command_type(self) -> CommandType:
        return CommandType.DEFINE_ZONE

    def describe(self) -> str:
        if self.region:
            return f"Define {self.net} zone on {self.layer} in {self.region} region"
        elif self.bounds:
            return (
                f"Define {self.net} zone on {self.layer} at "
                f"({self.bounds[0]:.1f}, {self.bounds[1]:.1f}) to "
                f"({self.bounds[2]:.1f}, {self.bounds[3]:.1f})"
            )
        return f"Define {self.net} zone on {self.layer}"

    def to_dict(self) -> dict:
        return {
            "type": "define_zone",
            "net": self.net,
            "layer": self.layer,
            "region": self.region,
            "bounds": self.bounds,
            "priority": self.priority,
            "min_thickness": self.min_thickness,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DefineZoneCommand":
        return cls(
            net=data["net"],
            layer=data["layer"],
            region=data.get("region"),
            bounds=tuple(data["bounds"]) if data.get("bounds") else None,
            priority=data.get("priority", 0),
            min_thickness=data.get("min_thickness", 0.2),
        )


# =============================================================================
# Command Parsing
# =============================================================================


def parse_command(data: dict) -> Command:
    """Parse a command from dictionary."""
    cmd_type = data.get("type", "")

    parsers = {
        "place_component": PlaceComponentCommand.from_dict,
        "route_net": RouteNetCommand.from_dict,
        "delete_trace": DeleteTraceCommand.from_dict,
        "add_via": AddViaCommand.from_dict,
        "define_zone": DefineZoneCommand.from_dict,
    }

    parser = parsers.get(cmd_type)
    if parser:
        return parser(data)

    raise ValueError(f"Unknown command type: {cmd_type}")


def parse_natural_language(text: str) -> Optional[Command]:
    """Attempt to parse a natural language command.

    This is a simple pattern-matching parser. For real use,
    the LLM should output structured commands directly.
    """
    text_lower = text.lower()

    # Route patterns
    if text_lower.startswith("route "):
        # "Route MCLK avoiding analog section"
        import re

        match = re.match(r"route\s+(\S+)", text, re.IGNORECASE)
        if match:
            net = match.group(1)
            avoid = []
            if "avoid" in text_lower:
                # Extract region names after "avoiding"
                avoid_match = re.search(r"avoid(?:ing)?\s+(.+?)(?:\s+via|\s*$)", text, re.IGNORECASE)
                if avoid_match:
                    avoid = [r.strip() for r in avoid_match.group(1).split(",")]

            direction = None
            for d in ["north", "south", "east", "west"]:
                if f"via {d}" in text_lower or f"through {d}" in text_lower:
                    direction = d
                    break

            return RouteNetCommand(
                net=net,
                avoid_regions=avoid,
                prefer_direction=direction,
            )

    # Delete patterns
    if "delete" in text_lower and "trace" in text_lower:
        import re

        net_match = re.search(r"(?:of|for)\s+(\S+)", text, re.IGNORECASE)
        net = net_match.group(1) if net_match else None

        return DeleteTraceCommand(
            net=net,
            delete_all_routing="all" in text_lower,
            reason="user request",
        )

    return None
