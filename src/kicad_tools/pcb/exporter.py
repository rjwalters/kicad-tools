"""
KiCad PCB Export - apply a :class:`PCBLayout` to a real ``.kicad_pcb``.

This module provides:

- :class:`KiCadPCBExporter`: the layout -> board bridge.  It moves the block
  footprints that already exist on the board to their layout placements and
  writes the block-internal traces, the inter-block traces and the vias as
  real copper on the board's **existing** nets, all through the
  :class:`~kicad_tools.schema.pcb.PCB` model (deterministic UUIDs, #6076;
  exact-duplicate dedupe, #4175; the board's net-name dialect, #4416).
- :func:`update_pcb_placements`: the lenient, placements-only update.

The bridge never creates footprints or nets.  Pad nets come from the
schematic-to-PCB step (``kct create-pcb`` / netlist import); the layout only
adds placement and copper on top of that (Issue #7022).

Coordinate contract: every layout coordinate is **sheet-absolute** (the
values a footprint's ``(at ...)`` holds in the file), the same contract
:func:`update_pcb_placements` has always used.  The bridge subtracts
``pcb.board_origin`` before handing coordinates to the board-relative PCB API.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .layout import PCBLayout

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)


class LayoutApplyError(ValueError):
    """A layout cannot be applied to a board without guessing.

    Raised by :meth:`KiCadPCBExporter.apply_to` / :meth:`KiCadPCBExporter.apply`
    in strict mode before anything is mutated or written.  ``problems`` lists
    **every** problem found, one human-readable string each.
    """

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            f"cannot apply layout to board ({len(self.problems)} problem(s)):\n{lines}"
        )


@dataclass
class LayoutApplyReport:
    """What :meth:`KiCadPCBExporter.apply_to` did to the board."""

    moved: list[str] = field(default_factory=list)
    """Refs whose footprint was moved, in layout order."""
    missing_refs: list[str] = field(default_factory=list)
    """Layout refs with no footprint on the board (``strict=False`` only)."""
    segments_added: int = 0
    """Segments actually added (exact duplicates already on the board are skipped)."""
    vias_added: int = 0
    """Vias actually added (exact duplicates already on the board are skipped)."""
    skipped: list[str] = field(default_factory=list)
    """Items skipped because they could not be applied (``strict=False`` only)."""
    footprint_mismatches: list[str] = field(default_factory=list)
    """Refs whose block footprint lib-id differs from the board's (moved anyway)."""


def _lib_ids_match(block_fp: str, board_fp: str) -> bool:
    """Compare lib-ids; only the part after ``:`` when either side has no nickname."""
    if ":" in block_fp and ":" in board_fp:
        return block_fp == board_fp
    return block_fp.split(":", 1)[-1] == board_fp.split(":", 1)[-1]


def _fmt_xy(xy: tuple[float, float]) -> str:
    return f"({xy[0]:g}, {xy[1]:g})"


class KiCadPCBExporter:
    """
    Apply a :class:`PCBLayout` to an existing, netted ``.kicad_pcb``.

    A block's component refs *are* the board refs: the board must already
    hold those footprints (for example from schematic-to-PCB sync).  Block
    nets are generic names (``VIN``, ``VOUT``, ``LED_MID``...); ``net_map``
    maps them to the board's net names (``+3V3``, ``Net-(D1-K)``...).  A name
    absent from ``net_map`` is looked up as-is.

    Strict mode (the default) validates everything first and raises one
    :class:`LayoutApplyError` listing every problem before mutating or
    writing anything: a ref missing from the board, a ref in two blocks, a
    trace/via with ``net=None``, a net (after ``net_map``) the board does not
    have, or a layer that is not one of the board's copper layers.  With
    ``strict=False`` each offending item is skipped, logged as a warning and
    recorded in :attr:`LayoutApplyReport.skipped`.  No net is ever created.

    Usage:
        exporter = KiCadPCBExporter(layout, net_map={"VOUT": "+3V3"})
        report = exporter.apply("board.kicad_pcb", "board_placed.kicad_pcb")
    """

    def __init__(
        self,
        layout: PCBLayout,
        *,
        net_map: Mapping[str, str] | None = None,
        strict: bool = True,
    ):
        self.layout = layout
        self.net_map: dict[str, str] = dict(net_map or {})
        self.strict = strict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_to(
        self,
        pcb: PCB,
        *,
        placements: bool = True,
        traces: bool = True,
        vias: bool = True,
    ) -> LayoutApplyReport:
        """Apply the layout to ``pcb`` in memory (does not save).

        Placements are applied before copper.  In strict mode a
        :class:`LayoutApplyError` is raised before ``pcb`` is touched.
        """
        report = LayoutApplyReport()
        problems: list[str] = []
        ox, oy = pcb.board_origin
        copper = [layer.name for layer in pcb.copper_layers]

        # -- validate placements ---------------------------------------
        moves: list[dict[str, Any]] = []
        if placements:
            owner: dict[str, str] = {}
            for block_name, block in self.layout.blocks.items():
                for placement in block.get_placed_components():
                    ref = placement["ref"]
                    if ref in owner:
                        problems.append(
                            f"ref {ref} is in two blocks ({owner[ref]!r} and {block_name!r})"
                        )
                        continue
                    owner[ref] = block_name
                    fp = pcb.get_footprint(ref)
                    if fp is None:
                        problems.append(
                            f"ref {ref} (block {block_name!r}) has no footprint on the board"
                        )
                        report.missing_refs.append(ref)
                        continue
                    if not _lib_ids_match(placement["footprint"], fp.name):
                        report.footprint_mismatches.append(ref)
                        logger.warning(
                            "%s: block footprint %r differs from board footprint %r; "
                            "moving it anyway",
                            ref,
                            placement["footprint"],
                            fp.name,
                        )
                    moves.append(placement)

        # -- validate copper -------------------------------------------
        def resolve_net(kind: str, where: str, net: str | None) -> str | None:
            if net is None:
                problems.append(f"{kind} {where} has net=None (copper on net 0 would be an orphan)")
                return None
            board_net = self.net_map.get(net, net)
            if pcb.get_net_by_name(board_net) is None:
                mapped = f" (mapped from {net!r})" if board_net != net else ""
                problems.append(
                    f"{kind} {where} is on net {board_net!r}{mapped}, "
                    "which the board does not have (add it to net_map?)"
                )
                return None
            return board_net

        def layer_ok(kind: str, where: str, layer: str) -> bool:
            if layer in copper:
                return True
            if layer.endswith(".Cu"):
                problems.append(
                    f"{kind} {where} is on copper layer {layer}, which this "
                    f"{len(copper)}-layer board does not have ({', '.join(copper)})"
                )
            else:
                problems.append(f"{kind} {where} is on {layer}, which is not a copper layer")
            return False

        segments: list[tuple[dict[str, Any], str]] = []
        if traces:
            for trace in self.layout.export_traces():
                where = (
                    f"{_fmt_xy(trace['start'])}->{_fmt_xy(trace['end'])}"
                    f" ({_origin(trace['block_id'])})"
                )
                before = len(problems)
                board_net = resolve_net("trace", where, trace["net"])
                layer_ok("trace", where, trace["layer"])
                if len(problems) == before and board_net is not None:
                    segments.append((trace, board_net))

        via_items: list[tuple[dict[str, Any], str]] = []
        if vias:
            for via in self.layout.export_vias():
                where = f"at {_fmt_xy((via['x'], via['y']))} ({_origin(via['block_id'])})"
                before = len(problems)
                board_net = resolve_net("via", where, via["net"])
                for layer in via["layers"]:
                    layer_ok("via", where, layer)
                if len(problems) == before and board_net is not None:
                    via_items.append((via, board_net))

        if problems:
            if self.strict:
                raise LayoutApplyError(problems)
            # Lenient: every offending item was left out of the work lists.
            report.skipped = list(problems)
            for problem in problems:
                logger.warning("skipping: %s", problem)

        # -- mutate ----------------------------------------------------
        for placement in moves:
            if pcb.update_footprint_position(
                placement["ref"],
                float(placement["x"]) - ox,
                float(placement["y"]) - oy,
                float(placement["rotation"]),
            ):
                report.moved.append(placement["ref"])

        for trace, board_net in segments:
            sx, sy = trace["start"]
            ex, ey = trace["end"]
            added = pcb.add_trace(
                (float(sx) - ox, float(sy) - oy),
                (float(ex) - ox, float(ey) - oy),
                width=trace["width"],
                layer=trace["layer"],
                net=board_net,
            )
            report.segments_added += len(added)

        for via, board_net in via_items:
            added_via = pcb.add_via(
                float(via["x"]) - ox,
                float(via["y"]) - oy,
                size=via["size"],
                drill=via["drill"],
                layers=via["layers"],
                net=board_net,
            )
            if added_via is not None:
                report.vias_added += 1

        return report

    def apply(
        self,
        source_pcb: str | Path,
        output_pcb: str | Path,
        **kwargs: bool,
    ) -> LayoutApplyReport:
        """Load ``source_pcb``, :meth:`apply_to` it and save to ``output_pcb``.

        ``kwargs`` are :meth:`apply_to`'s ``placements`` / ``traces`` /
        ``vias`` switches.  In strict mode a :class:`LayoutApplyError` is
        raised before ``output_pcb`` is created or touched.
        """
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(source_pcb))
        report = self.apply_to(pcb, **kwargs)
        pcb.save(str(output_pcb))
        return report

    def generate_placement_update(self) -> dict[str, tuple]:
        """
        Generate component positions for updating an existing PCB.

        Returns dict of {ref: (x, y, rotation)} for each component.
        """
        result = {}
        for placement in self.layout.export_placements():
            result[placement["ref"]] = (placement["x"], placement["y"], placement["rotation"])
        return result


def _origin(block_id: str | None) -> str:
    return f"block {block_id!r}" if block_id is not None else "inter-block"


def update_pcb_placements(source_pcb: str, layout: PCBLayout, output_pcb: str):
    """
    Update component positions in an existing PCB file based on block layout.

    This reads an existing PCB, updates the (at x y rotation) for each
    component that exists in the layout, and writes the result.  Refs the
    board does not have are skipped with a warning.  For copper (traces and
    vias) as well as placements, use :meth:`PCBLayout.apply_to_pcb`.

    Args:
        source_pcb: Path to existing KiCad PCB file
        layout: PCBLayout with desired component positions (sheet-absolute)
        output_pcb: Path for output PCB file
    """
    # Get desired positions
    exporter = KiCadPCBExporter(layout)
    positions = exporter.generate_placement_update()

    # Through the PCB model rather than a regex over the file: the regex
    # could match from an earlier footprint up to this reference and rewrite
    # the wrong (at ...), and it never moved the footprint's board-absolute
    # geometry (embedded keepout zones, pad angles) -- Issue #6119.
    from kicad_tools.placement.writeback import write_footprint_placements
    from kicad_tools.schema.pcb import PCB

    board = PCB.load(str(source_pcb))
    missing = [ref for ref in positions if board.get_footprint(ref) is None]
    if missing:
        logger.warning(
            "update_pcb_placements: %d layout ref(s) not on the board, skipped: %s",
            len(missing),
            ", ".join(missing),
        )

    moved = write_footprint_placements(
        source_pcb,
        output_pcb,
        ((ref, x, y, rotation) for ref, (x, y, rotation) in positions.items()),
    )
    print(f"Updated PCB placements: {output_pcb}")
    print(f"  Updated {moved} components")


__all__ = [
    "KiCadPCBExporter",
    "LayoutApplyError",
    "LayoutApplyReport",
    "update_pcb_placements",
]
