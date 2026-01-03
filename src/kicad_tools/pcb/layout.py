"""
PCB Layout - Container for placing and routing multiple PCB blocks.

This module provides the PCBLayout class for arranging blocks and
routing between them.
"""

from .blocks.base import PCBBlock
from .geometry import Layer
from .primitives import TraceSegment, Via


class PCBLayout:
    """
    Container for placing and routing multiple PCB blocks.
    """

    def __init__(self, name: str = "layout"):
        self.name = name
        self.blocks: dict[str, PCBBlock] = {}
        self.inter_block_traces: list[TraceSegment] = []
        self.inter_block_vias: list[Via] = []

    def add_block(self, block: PCBBlock, name: str | None = None) -> PCBBlock:
        """Add a block to the layout."""
        if name is None:
            name = block.name
        self.blocks[name] = block
        return block

    def route(
        self,
        from_block: str,
        from_port: str,
        to_block: str,
        to_port: str,
        width: float = 0.25,
        layer: Layer = Layer.F_CU,
        net: str | None = None,
    ):
        """
        Route between two block ports.

        This creates a simple direct trace. More complex routing
        would use waypoints or an autorouter.
        """
        start = self.blocks[from_block].port(from_port)
        end = self.blocks[to_block].port(to_port)

        trace = TraceSegment(start=start, end=end, width=width, layer=layer, net=net)
        self.inter_block_traces.append(trace)
        return trace

    def export_placements(self) -> list[dict]:
        """Export all component placements."""
        result = []
        for block in self.blocks.values():
            result.extend(block.get_placed_components())
        return result

    def export_traces(self) -> list[dict]:
        """Export all traces (internal + inter-block)."""
        result = []

        # Internal traces from each block
        for block in self.blocks.values():
            result.extend(block.get_placed_traces())

        # Inter-block traces
        for trace in self.inter_block_traces:
            result.append(
                {
                    "start": trace.start.tuple(),
                    "end": trace.end.tuple(),
                    "width": trace.width,
                    "layer": trace.layer.value,
                    "net": trace.net,
                }
            )

        return result

    def summary(self) -> str:
        """Print layout summary."""
        lines = [f"PCB Layout: {self.name}", "=" * 40]

        total_components = 0
        total_internal_traces = 0

        for name, block in self.blocks.items():
            n_comp = len(block.components)
            n_traces = len(block.traces)
            total_components += n_comp
            total_internal_traces += n_traces

            pos = f"({block.origin.x}, {block.origin.y})" if block.placed else "not placed"
            lines.append(f"\n{name}: {pos}")
            lines.append(f"  Components: {n_comp}")
            lines.append(f"  Internal traces: {n_traces}")
            lines.append(f"  Ports: {', '.join(block.ports.keys())}")

        lines.append(f"\n{'=' * 40}")
        lines.append(f"Total components: {total_components}")
        lines.append(f"Total internal traces: {total_internal_traces}")
        lines.append(f"Inter-block traces: {len(self.inter_block_traces)}")

        return "\n".join(lines)


__all__ = ["PCBLayout"]
