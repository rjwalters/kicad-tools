"""Pre-route bundle crossing report (Issue #6339).

A *pure* analysis of an (un)routed board: which bundles of nets are forced to
cross, per entry side, and whether the forced crossings can be separated onto
the available signal layers.  No router, grid or ``Autorouter`` is built, so the
report runs on an unrouted board in well under a second.  It is surfaced as
``kct net-status <pcb> --crossings`` (text, or JSON with ``--format json``) and
never changes routing behaviour.

Three views are produced:

``bundles``
    For every pair of parts joined by >= :data:`MIN_BUNDLE_NETS` two-terminal
    signal nets (each net has pads on exactly those two parts; a net with
    several pads on one part contributes the pad centroid), the nets are
    grouped by *(side of part A, side of part B)* so each group is one pin row
    facing one pin row.  Each row is ordered along its own axis.  The
    inversion count is computed per candidate **entry side** of the
    destination row with
    :func:`kicad_tools.router.bundle_river.compute_facing_row_inversions`
    (reused, not re-implemented):

    * same-axis rows (a straight facing pair, or a same-side U-turn) have one
      natural entry, so one count;
    * different-axis rows (an L-shaped bundle, where a 1-D projection cannot
      tell chirality) are scored for both orders -- ``aligned`` (both rows
      read in ascending order) and ``mirrored`` (destination read in the
      opposite direction, i.e. the bundle entering from the other end of the
      row; its count is ``C(n, 2) - aligned``) -- and the minimum is reported.

    ``min_pin_swaps`` is the inversion count at the minimum (the number of
    adjacent pin transpositions that makes the bundle planar).  The conflict
    graph of the inverted pairs at the minimum is coloured with
    :func:`kicad_tools.router.bundle_river._min_graph_colouring`; a bundle whose
    chromatic number exceeds the signal-layer count is ``oversized``.  The
    exact search is bounded (:data:`EXACT_COLOURING_MAX_NODES`); above it the
    clique lower bound and greedy upper bound are reported and the flag uses
    the (sound) lower bound only.

``hubs``
    For every pin column (one side of one part) with >= :data:`MIN_HUB_NETS`
    signal nets that have a pad on some other part: the exit direction of each
    pad (toward the centroid of the net's pads on the other parts, bucketed to
    ``left`` / ``right`` / ``up`` / ``down``), in column order.  A direction
    whose pads are separated by a pad leaving a different way is
    ``interleaved`` and the separating nets are listed.

The straight-chord board-wide crossing graph is deliberately *not* reported:
it over-reports (a bundle that looks fully crossed with straight chords is
planar when it enters the row from the other side).

JSON schema (``BundleCrossingReport.to_dict()``)::

    {
      "signal_layers": int,
      "bundles": [{
        "ref_a": str, "ref_b": str,          # part keys (reference designators)
        "side_a": str, "side_b": str,        # left|right|up|down
        "axis_a": "x"|"y", "axis_b": "x"|"y",
        "net_count": int,
        "order_a": [net names along row A, ascending],
        "order_b": [net names along row B, ascending],
        "entries": {"aligned": int, "mirrored": int},   # only candidate sides
        "min_entry": "aligned"|"mirrored",
        "min_inversions": int,
        "inverted_pairs": [[net, net], ...],            # at min_entry
        "min_pin_swaps": int,
        "chromatic_lower": int, "chromatic_upper": int, "chromatic_exact": bool,
        "oversized": bool
      }],
      "hubs": [{
        "ref": str, "side": str, "net_count": int,
        "pads": [{"net": str, "exit": str}, ...],       # column order
        "interleaved": [{"exit": str, "nets": [str], "separated_by": [str]}]
      }],
      "skipped": {"multi_part_nets": int}
    }

Only the bundle ``min_inversions`` / ``oversized`` and hub ``interleaved``
fields are findings; the rest is context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools.router.bundle_river import (
    RowMember,
    _min_graph_colouring,
    compute_facing_row_inversions,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

#: A part pair needs at least this many two-terminal signal nets to be a bundle.
MIN_BUNDLE_NETS = 3
#: A pin column needs at least this many routable signal nets to be a hub.
MIN_HUB_NETS = 3
#: Exact colouring is only attempted up to this many conflict-graph nodes.
EXACT_COLOURING_MAX_NODES = 10

_SIDES = ("left", "right", "up", "down")
_AXIS_OF_SIDE = {"left": "y", "right": "y", "up": "x", "down": "x"}
_OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}


# --------------------------------------------------------------------------
# Report dataclasses
# --------------------------------------------------------------------------


@dataclass
class BundleReport:
    ref_a: str
    ref_b: str
    side_a: str
    side_b: str
    axis_a: str
    axis_b: str
    net_count: int
    order_a: list[str]
    order_b: list[str]
    entries: dict[str, int]
    min_entry: str
    min_inversions: int
    inverted_pairs: list[tuple[str, str]]
    min_pin_swaps: int
    chromatic_lower: int
    chromatic_upper: int
    chromatic_exact: bool
    oversized: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_a": self.ref_a,
            "ref_b": self.ref_b,
            "side_a": self.side_a,
            "side_b": self.side_b,
            "axis_a": self.axis_a,
            "axis_b": self.axis_b,
            "net_count": self.net_count,
            "order_a": list(self.order_a),
            "order_b": list(self.order_b),
            "entries": dict(self.entries),
            "min_entry": self.min_entry,
            "min_inversions": self.min_inversions,
            "inverted_pairs": [list(p) for p in self.inverted_pairs],
            "min_pin_swaps": self.min_pin_swaps,
            "chromatic_lower": self.chromatic_lower,
            "chromatic_upper": self.chromatic_upper,
            "chromatic_exact": self.chromatic_exact,
            "oversized": self.oversized,
        }

    def net_names(self) -> set[str]:
        return set(self.order_a)


@dataclass
class HubReport:
    ref: str
    side: str
    pads: list[tuple[str, str]]  # (net name, exit direction) in column order
    interleaved: list[dict[str, Any]] = field(default_factory=list)

    @property
    def net_count(self) -> int:
        return len({n for n, _ in self.pads})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "side": self.side,
            "net_count": self.net_count,
            "pads": [{"net": n, "exit": d} for n, d in self.pads],
            "interleaved": [dict(i) for i in self.interleaved],
        }

    def net_names(self) -> set[str]:
        return {n for n, _ in self.pads}


@dataclass
class BundleCrossingReport:
    signal_layers: int
    bundles: list[BundleReport] = field(default_factory=list)
    hubs: list[HubReport] = field(default_factory=list)
    multi_part_nets: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_layers": self.signal_layers,
            "bundles": [b.to_dict() for b in self.bundles],
            "hubs": [h.to_dict() for h in self.hubs],
            "skipped": {"multi_part_nets": self.multi_part_nets},
        }

    @property
    def flagged_bundles(self) -> list[BundleReport]:
        return [b for b in self.bundles if b.min_inversions > 0 or b.oversized]

    @property
    def flagged_hubs(self) -> list[HubReport]:
        return [h for h in self.hubs if h.interleaved]

    def flagged_nets(self) -> set[str]:
        """Nets named by any finding (inverted pair or interleave)."""
        nets: set[str] = set()
        for b in self.bundles:
            for a, c in b.inverted_pairs:
                nets.update((a, c))
        for h in self.hubs:
            for item in h.interleaved:
                nets.update(item["nets"])
                nets.update(item["separated_by"])
        return nets

    def filtered(self, net: str) -> BundleCrossingReport:
        """Copy keeping only the bundles / hubs that include *net*."""
        return BundleCrossingReport(
            signal_layers=self.signal_layers,
            bundles=[b for b in self.bundles if net in b.net_names()],
            hubs=[h for h in self.hubs if net in h.net_names()],
            multi_part_nets=self.multi_part_nets,
        )


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------


def default_signal_layers(pcb: PCB) -> int:
    """Copper layers of type ``signal`` (at least 1)."""
    return max(1, sum(1 for layer in pcb.copper_layers if layer.type == "signal"))


def _is_signal_net(name: str) -> bool:
    """Routable signal net by name: not unnamed, not a bare power/ground rail."""
    from kicad_tools.router.net_class import (
        _has_signal_qualifier,
        _rail_base_name,
        is_ground_rail_name,
        is_power_rail_name,
    )

    if not name:
        return False
    if is_power_rail_name(name):
        return False
    if is_ground_rail_name(name):
        base = _rail_base_name(name)
        # _rail_base_name returns None when the name has no rail base;
        # `bool(base)` would not narrow the Optional for mypy.
        if not base:
            return False
        # GND_SENSE-style names are signals that merely start with GND.
        return _has_signal_qualifier(base)
    return True


def _pad_points(pcb: PCB) -> list[tuple[str, int, float, float]]:
    from kicad_tools.router.stuck_classifier import _iter_board_pads

    return [
        (ref, net, x, y) for ref, net, (x, y), _size in _iter_board_pads(pcb) if net and net > 0
    ]


def _part_boxes(pcb: PCB) -> dict[str, tuple[float, float, float, float]]:
    """Pad bounding box ``(xmin, ymin, xmax, ymax)`` per part key (all pads)."""
    from kicad_tools.router.stuck_classifier import _iter_board_pads

    boxes: dict[str, list[float]] = {}
    for ref, _net, (x, y), _size in _iter_board_pads(pcb):
        b = boxes.setdefault(ref, [x, y, x, y])
        b[0] = min(b[0], x)
        b[1] = min(b[1], y)
        b[2] = max(b[2], x)
        b[3] = max(b[3], y)
    return {k: (v[0], v[1], v[2], v[3]) for k, v in boxes.items()}


#: A pad box narrower than this along an axis has no left/right (or up/down)
#: distinction of its own (single-column connectors, two-pin parts).
_DEGENERATE_EXTENT_MM = 1.0


def _side_of(
    box: tuple[float, float, float, float],
    x: float,
    y: float,
    toward: tuple[float, float] | None = None,
) -> str:
    """Side of the part's pad bounding box that (x, y) is nearest to.

    A part whose pads are all in one line (a single-column header) has no
    intrinsic outside; when *toward* (the partner's position) is given, the
    degenerate axis resolves to the side facing the partner instead of an
    arbitrary tie.
    """
    xmin, ymin, xmax, ymax = box
    dist = {"left": x - xmin, "right": xmax - x, "up": y - ymin, "down": ymax - y}
    if toward is not None:
        wide_x = xmax - xmin >= _DEGENERATE_EXTENT_MM
        wide_y = ymax - ymin >= _DEGENERATE_EXTENT_MM
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        tx, ty = toward[0] - cx, toward[1] - cy
        if not wide_x and wide_y:
            return "right" if tx > 0 else "left"
        if not wide_y and wide_x:
            return "down" if ty > 0 else "up"
        if not wide_x and not wide_y:
            return _direction(tx, ty) or "left"
    # Deterministic tiebreak by _SIDES order.
    return min(_SIDES, key=lambda s: (round(dist[s], 6), _SIDES.index(s)))


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _direction(dx: float, dy: float) -> str | None:
    if max(abs(dx), abs(dy)) < 1e-6:
        return None
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


# --------------------------------------------------------------------------
# Colouring bounds
# --------------------------------------------------------------------------


def _chromatic_bounds(nodes: list[int], edges: list[tuple[int, int]]) -> tuple[int, int, bool]:
    """``(lower, upper, exact)`` chromatic number of a small conflict graph."""
    if not nodes:
        return 0, 0, True
    if not edges:
        return 1, 1, True
    adj: dict[int, set[int]] = {n: set() for n in nodes}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)

    # Greedy clique (lower bound) seeded from every node.
    clique_best = 1
    for seed in nodes:
        clique = [seed]
        cands = set(adj[seed])
        while cands:
            nxt = max(sorted(cands), key=lambda c: len(adj[c] & cands))
            clique.append(nxt)
            cands &= adj[nxt]
        clique_best = max(clique_best, len(clique))

    # Greedy colouring (upper bound), largest degree first.
    colour: dict[int, int] = {}
    for n in sorted(nodes, key=lambda n: (-len(adj[n]), n)):
        used = {colour[m] for m in adj[n] if m in colour}
        colour[n] = next(c for c in range(len(nodes)) if c not in used)
    upper = max(colour.values()) + 1

    if clique_best == upper:
        return clique_best, upper, True
    if len(nodes) <= EXACT_COLOURING_MAX_NODES:
        exact = _min_graph_colouring(sorted(nodes), edges)
        k = max(exact.values()) + 1
        return k, k, True
    return clique_best, upper, False


# --------------------------------------------------------------------------
# Bundles
# --------------------------------------------------------------------------


def _row_members(entries: list[tuple[int, str, float]]) -> list[RowMember]:
    return [RowMember(net_id=i, net_name=n, projection=p) for i, n, p in entries]


def _analyse_bundle(
    ref_a: str,
    ref_b: str,
    side_a: str,
    side_b: str,
    nets: list[tuple[int, str, tuple[float, float], tuple[float, float]]],
    signal_layers: int,
) -> BundleReport | None:
    axis_a = _AXIS_OF_SIDE[side_a]
    axis_b = _AXIS_OF_SIDE[side_b]
    ia = 0 if axis_a == "x" else 1
    ib = 0 if axis_b == "x" else 1

    row_a = _row_members([(nid, name, pa[ia]) for nid, name, pa, _pb in nets])
    row_b_asc = _row_members([(nid, name, pb[ib]) for nid, name, _pa, pb in nets])
    # Mirrored = destination row read from its other end.
    row_b_mir = _row_members([(nid, name, -pb[ib]) for nid, name, _pa, pb in nets])

    inv_aligned = compute_facing_row_inversions(row_a, row_b_asc)
    inv_mirrored = compute_facing_row_inversions(row_a, row_b_mir)

    if axis_a != axis_b:
        entries = {"aligned": inv_aligned, "mirrored": inv_mirrored}
    elif side_a == side_b:  # same-side U-turn nests: order reverses
        entries = {"mirrored": inv_mirrored}
    else:  # straight facing rows
        entries = {"aligned": inv_aligned}

    min_entry = min(entries, key=lambda k: (len(entries[k]), k))
    inv = entries[min_entry]
    counts = {k: len(v) for k, v in entries.items()}

    pairs = [(p.net_a_name, p.net_b_name) for p in inv]
    nodes = sorted({p.net_a_id for p in inv} | {p.net_b_id for p in inv})
    edges = [(p.net_a_id, p.net_b_id) for p in inv]
    lower, upper, exact = _chromatic_bounds(nodes, edges)
    # The conflict graph is empty for a planar bundle (chromatic 1 by convention
    # for "needs no layer change"); only the lower bound can flag soundly.
    oversized = lower > signal_layers

    sorted_a = sorted(nets, key=lambda t: (t[2][ia], t[1]))
    sorted_b = sorted(nets, key=lambda t: (t[3][ib], t[1]))
    return BundleReport(
        ref_a=ref_a,
        ref_b=ref_b,
        side_a=side_a,
        side_b=side_b,
        axis_a=axis_a,
        axis_b=axis_b,
        net_count=len(nets),
        order_a=[t[1] for t in sorted_a],
        order_b=[t[1] for t in sorted_b],
        entries=counts,
        min_entry=min_entry,
        min_inversions=len(inv),
        inverted_pairs=pairs,
        min_pin_swaps=len(inv),
        chromatic_lower=max(lower, 1),
        chromatic_upper=max(upper, 1),
        chromatic_exact=exact,
        oversized=oversized,
    )


def _net_pad_table(
    pcb: PCB,
) -> tuple[dict[int, dict[str, list[tuple[float, float]]]], dict[int, str]]:
    names = {nid: net.name for nid, net in pcb.nets.items() if net.name}
    table: dict[int, dict[str, list[tuple[float, float]]]] = {}
    for ref, net, x, y in _pad_points(pcb):
        if net not in names or not _is_signal_net(names[net]):
            continue
        table.setdefault(net, {}).setdefault(ref, []).append((x, y))
    return table, names


def compute_bundle_crossings(pcb: PCB, signal_layers: int | None = None) -> BundleCrossingReport:
    """Analyse *pcb* and return the bundle / hub crossing report."""
    layers = signal_layers if signal_layers and signal_layers > 0 else default_signal_layers(pcb)
    report = BundleCrossingReport(signal_layers=layers)
    table, names = _net_pad_table(pcb)
    boxes = _part_boxes(pcb)

    # ---- bundles -------------------------------------------------------
    groups: dict[
        tuple[str, str, str, str],
        list[tuple[int, str, tuple[float, float], tuple[float, float]]],
    ] = {}
    for net, by_ref in table.items():
        if len(by_ref) > 2:
            report.multi_part_nets += 1
            continue
        if len(by_ref) < 2:
            continue
        (r1, p1), (r2, p2) = sorted(by_ref.items())
        c1, c2 = _centroid(p1), _centroid(p2)
        s1 = _side_of(boxes[r1], *c1, toward=c2)
        s2 = _side_of(boxes[r2], *c2, toward=c1)
        groups.setdefault((r1, r2, s1, s2), []).append((net, names[net], c1, c2))

    pair_totals: dict[tuple[str, str], int] = {}
    for (r1, r2, _s1, _s2), nets in groups.items():
        pair_totals[(r1, r2)] = pair_totals.get((r1, r2), 0) + len(nets)

    for (r1, r2, s1, s2), nets in sorted(groups.items()):
        if len(nets) < MIN_BUNDLE_NETS:
            continue
        bundle = _analyse_bundle(r1, r2, s1, s2, sorted(nets, key=lambda t: t[1]), layers)
        if bundle is not None:
            report.bundles.append(bundle)

    # ---- hubs ----------------------------------------------------------
    cols: dict[tuple[str, str], list[tuple[float, str, str]]] = {}
    for net, by_ref in table.items():
        if len(by_ref) < 2:
            continue
        for ref, pts in by_ref.items():
            others = [p for r, ps in by_ref.items() if r != ref for p in ps]
            ox, oy = _centroid(others)
            for x, y in pts:
                d = _direction(ox - x, oy - y)
                if d is None:
                    continue
                side = _side_of(boxes[ref], x, y, toward=(ox, oy))
                key = y if _AXIS_OF_SIDE[side] == "y" else x
                cols.setdefault((ref, side), []).append((key, names[net], d))

    for (ref, side), items in sorted(cols.items()):
        items.sort(key=lambda t: (t[0], t[1]))
        pads = [(n, d) for _k, n, d in items]
        hub = HubReport(ref=ref, side=side, pads=pads)
        if hub.net_count < MIN_HUB_NETS:
            continue
        hub.interleaved = _interleaves(pads)
        report.hubs.append(hub)
    return report


def _interleaves(pads: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Directions whose pads in column order are split by another direction."""
    out: list[dict[str, Any]] = []
    for d in _SIDES:
        idx = [i for i, (_n, dd) in enumerate(pads) if dd == d]
        if len(idx) < 2:
            continue
        between = [pads[i] for i in range(idx[0], idx[-1] + 1) if pads[i][1] != d]
        if not between:
            continue
        out.append(
            {
                "exit": d,
                "nets": sorted({pads[i][0] for i in idx}),
                "separated_by": sorted({n for n, _ in between}),
            }
        )
    return out


# --------------------------------------------------------------------------
# Text rendering
# --------------------------------------------------------------------------


def format_report_text(report: BundleCrossingReport) -> str:
    lines = [
        "Bundle crossing report (pre-route, read-only)",
        f"  Signal layers assumed: {report.signal_layers}",
        f"  Bundles: {len(report.bundles)}  with forced crossings: "
        f"{len(report.flagged_bundles)}  hub columns: {len(report.hubs)}  "
        f"interleaved: {len(report.flagged_hubs)}",
        "",
    ]
    for b in report.bundles:
        flag = ""
        if b.oversized:
            flag = f"  [OVERSIZED: needs > {report.signal_layers} layers]"
        elif b.min_inversions:
            flag = "  [forced crossings]"
        lines.append(
            f"Bundle {b.ref_a}.{b.side_a} -> {b.ref_b}.{b.side_b} ({b.net_count} nets){flag}"
        )
        lines.append(f"  order {b.ref_a}: {' '.join(b.order_a)}")
        lines.append(f"  order {b.ref_b}: {' '.join(b.order_b)}")
        ent = ", ".join(f"{k}={v}" for k, v in b.entries.items())
        lines.append(f"  inversions per entry side: {ent}; min={b.min_inversions} ({b.min_entry})")
        if b.inverted_pairs:
            lines.append("  inverted pairs: " + ", ".join(f"{a}/{c}" for a, c in b.inverted_pairs))
            exact = "" if b.chromatic_exact else " (bounds)"
            chi = (
                str(b.chromatic_lower)
                if b.chromatic_lower == b.chromatic_upper
                else f"{b.chromatic_lower}..{b.chromatic_upper}"
            )
            lines.append(
                f"  min pin swaps: {b.min_pin_swaps}; conflict-graph colours: {chi}{exact}"
            )
    if not report.bundles:
        lines.append(f"No bundles of >= {MIN_BUNDLE_NETS} two-terminal signal nets.")
    lines.append("")
    flagged = report.flagged_hubs
    lines.append(f"Hub pin columns with interleaved exit directions: {len(flagged)}")
    for h in flagged:
        lines.append(f"  {h.ref}.{h.side} ({h.net_count} nets)")
        lines.append("    column order: " + " ".join(f"{n}>{d}" for n, d in h.pads))
        for item in h.interleaved:
            lines.append(
                f"    exit {item['exit']}: {', '.join(item['nets'])} "
                f"interleaved with {', '.join(item['separated_by'])}"
            )
    if report.multi_part_nets:
        lines.append("")
        lines.append(
            f"Skipped {report.multi_part_nets} signal net(s) spanning 3+ parts "
            "(not two-terminal bundle members)."
        )
    return "\n".join(lines)


__all__ = [
    "BundleCrossingReport",
    "BundleReport",
    "HubReport",
    "compute_bundle_crossings",
    "default_signal_layers",
    "format_report_text",
]
