"""
PCB Block base class.

A PCBBlock is a virtual component - a group of components with internal routing.
Think of this as a "macro component" that encapsulates:
- Multiple physical components (IC + bypass caps, etc.)
- Internal trace routing (critical short traces)
- External ports (where to connect from outside)

The block can be placed and rotated as a unit, and external
routing only needs to connect to the ports.
"""

from ..geometry import Layer, Point, Rectangle
from ..placement import ComponentPlacement, get_footprint_pads
from ..primitives import Port, TraceSegment, Via


class PCBBlock:
    """
    A virtual component - a group of components with internal routing.

    Think of this as a "macro component" that encapsulates:
    - Multiple physical components (IC + bypass caps, etc.)
    - Internal trace routing (critical short traces)
    - External ports (where to connect from outside)

    The block can be placed and rotated as a unit, and external
    routing only needs to connect to the ports.
    """

    def __init__(self, name: str = "block"):
        self.name = name

        # Block placement (set by place())
        self.origin: Point = Point(0, 0)
        self.rotation: float = 0
        self.placed: bool = False

        # Internal elements (positions relative to block origin)
        self.components: dict[str, ComponentPlacement] = {}
        self.traces: list[TraceSegment] = []
        self.vias: list[Via] = []

        # External interface
        self.ports: dict[str, Port] = {}

        # Computed after components added
        self._bounding_box: Rectangle | None = None

    def add_component(
        self,
        ref: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float = 0,
        pads: dict[str, tuple] | None = None,
        layer: Layer = Layer.F_CU,
    ) -> ComponentPlacement:
        """Add a component to the block."""
        pad_points = {}
        if pads:
            pad_points = {name: Point(p[0], p[1]) for name, p in pads.items()}

        comp = ComponentPlacement(
            ref=ref,
            footprint=footprint,
            position=Point(x, y),
            rotation=rotation,
            layer=layer,
            pads=pad_points,
        )
        self.components[ref] = comp
        self._bounding_box = None  # Invalidate cache
        return comp

    def add_trace(
        self,
        start: tuple | Point,
        end: tuple | Point,
        width: float = 0.25,
        layer: Layer = Layer.F_CU,
        net: str | None = None,
    ) -> TraceSegment:
        """Add an internal trace segment."""
        if isinstance(start, tuple):
            start = Point(start[0], start[1])
        if isinstance(end, tuple):
            end = Point(end[0], end[1])

        trace = TraceSegment(start=start, end=end, width=width, layer=layer, net=net)
        self.traces.append(trace)
        return trace

    def add_via(
        self, x: float, y: float, net: str | None = None, drill: float = 0.3, size: float = 0.6
    ) -> Via:
        """Add an internal via."""
        via = Via(position=Point(x, y), drill=drill, size=size, net=net)
        self.vias.append(via)
        return via

    def add_port(
        self,
        name: str,
        x: float,
        y: float,
        direction: str = "inout",
        internal_pad: str | None = None,
        layer: Layer = Layer.F_CU,
    ) -> Port:
        """
        Add an external port to the block.

        Args:
            name: Port name (e.g., "VDD", "PA0")
            x, y: Position relative to block origin
            direction: "in", "out", "inout", or "power"
            internal_pad: What this connects to inside (e.g., "U1.VDD")
        """
        port = Port(
            name=name,
            position=Point(x, y),
            layer=layer,
            direction=direction,
            internal_pad=internal_pad,
        )
        self.ports[name] = port
        return port

    def route_to_port(
        self,
        pad_ref: str,
        port_name: str,
        width: float = 0.25,
        layer: Layer = Layer.F_CU,
        net: str | None = None,
    ):
        """
        Add trace from internal pad to external port.

        Args:
            pad_ref: "REF.PAD" format (e.g., "U1.VDD", "C12.1")
            port_name: Name of port to route to
        """
        # Parse pad reference
        ref, pad_name = pad_ref.split(".")
        if ref not in self.components:
            raise KeyError(f"Component '{ref}' not found in block")

        comp = self.components[ref]
        pad_pos = comp.pad_position(pad_name)

        if port_name not in self.ports:
            raise KeyError(f"Port '{port_name}' not found in block")

        port_pos = self.ports[port_name].position

        self.add_trace(pad_pos, port_pos, width=width, layer=layer, net=net)

    def place(self, x: float, y: float, rotation: float = 0):
        """Place the block on the PCB."""
        self.origin = Point(x, y)
        self.rotation = rotation
        self.placed = True

    def port(self, name: str) -> Point:
        """Get absolute position of a port after placement."""
        if name not in self.ports:
            available = list(self.ports.keys())
            raise KeyError(f"Port '{name}' not found. Available: {available}")

        # Get port position relative to block
        rel_pos = self.ports[name].position

        # Apply block rotation
        if self.rotation != 0:
            rel_pos = rel_pos.rotate(self.rotation)

        # Apply block origin
        return rel_pos + self.origin

    def component_position(self, ref: str) -> Point:
        """Get absolute position of a component after placement."""
        if ref not in self.components:
            raise KeyError(f"Component '{ref}' not found in block")

        comp = self.components[ref]
        rel_pos = comp.position

        # Apply block rotation
        if self.rotation != 0:
            rel_pos = rel_pos.rotate(self.rotation)

        return rel_pos + self.origin

    @property
    def bounding_box(self) -> Rectangle:
        """Get bounding box of all components."""
        if self._bounding_box is not None:
            return self._bounding_box

        if not self.components:
            return Rectangle(0, 0, 0, 0)

        # Simple bbox from component positions
        # TODO: Include actual footprint sizes
        xs = [c.position.x for c in self.components.values()]
        ys = [c.position.y for c in self.components.values()]

        self._bounding_box = Rectangle(
            min(xs) - 2,
            min(ys) - 2,  # 2mm margin
            max(xs) + 2,
            max(ys) + 2,
        )
        return self._bounding_box

    def get_placed_components(self) -> list[dict]:
        """Get components with absolute positions for PCB export."""
        result = []
        for ref, comp in self.components.items():
            pos = self.component_position(ref)
            result.append(
                {
                    "ref": ref,
                    "footprint": comp.footprint,
                    "x": pos.x,
                    "y": pos.y,
                    "rotation": (comp.rotation + self.rotation) % 360,
                    "layer": comp.layer.value,
                }
            )
        return result

    def get_placed_traces(self) -> list[dict]:
        """Get traces with absolute positions for PCB export."""
        result = []
        for trace in self.traces:
            start = trace.start
            end = trace.end

            # Apply block transformation
            if self.rotation != 0:
                start = start.rotate(self.rotation)
                end = end.rotate(self.rotation)

            start = start + self.origin
            end = end + self.origin

            result.append(
                {
                    "start": start.tuple(),
                    "end": end.tuple(),
                    "width": trace.width,
                    "layer": trace.layer.value,
                    "net": trace.net,
                }
            )
        return result

    def __repr__(self):
        placed = f" at ({self.origin.x}, {self.origin.y})" if self.placed else ""
        return f"PCBBlock({self.name}, {len(self.components)} components, {len(self.ports)} ports{placed})"


# Re-export get_footprint_pads for convenience in subclasses
__all__ = ["PCBBlock", "get_footprint_pads"]
