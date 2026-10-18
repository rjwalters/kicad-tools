"""Wire-endpoint primitives shared by the ``kct sch`` CLI and ``symbol_ops``.

Lives under ``operations/`` (not ``cli/``) so that ``operations.symbol_ops``
and ``cli.sch_wire_common`` can both import it without the operations layer
depending on the CLI layer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from kicad_tools.sexp import SExp


def iter_wire_endpoint_matches(
    sexp: SExp,
    matches: Callable[[float, float], bool],
) -> Iterator[tuple[SExp, int]]:
    """Yield ``(wire_sexp, endpoint_index)`` for wires with a matching endpoint.

    *sexp* is the raw schematic tree.  *matches* receives an endpoint's
    ``(x, y)`` and decides whether it counts as a hit; the first matching
    endpoint of each wire wins (a wire is never yielded twice).  Wires with
    fewer than two ``xy`` points are skipped.
    """
    for wire_sexp in sexp.find_all("wire"):
        pts_node = wire_sexp.find("pts")
        if not pts_node:
            continue

        xy_nodes = pts_node.find_all("xy")
        if len(xy_nodes) < 2:
            continue

        for idx, xy in enumerate(xy_nodes):
            x = xy.get_float(0) or 0.0
            y = xy.get_float(1) or 0.0
            if matches(x, y):
                yield wire_sexp, idx
                break


def find_wires_at_point_with_index(
    sexp: SExp,
    point: tuple[float, float],
    tolerance: float,
) -> list[tuple[SExp, int]]:
    """Find wires with an endpoint within *tolerance* (float, per axis) of *point*.

    Returns ``(wire_sexp, endpoint_index)`` tuples where *endpoint_index* is 0
    for the start or 1 for the end of the wire.
    """
    target_x, target_y = point
    return list(
        iter_wire_endpoint_matches(
            sexp,
            lambda x, y: abs(x - target_x) <= tolerance and abs(y - target_y) <= tolerance,
        )
    )
