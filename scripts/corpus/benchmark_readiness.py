#!/usr/bin/env python3
"""Benchmark-readiness census for corpus boards (issue #4830, slice 4).

Slices 1-3 answered *"can this repo's parsers read real-world KiCad files?"*.
This slice answers the next question, which is the one the two proposed
follow-on capabilities actually depend on:

1. **Capacity-predictor calibration** (feeds #4799) needs, per board, a feature
   vector (area, layers, pad/net density, ...) **and a routability label**
   ("a human finished this board").
2. **A route-vs-human benchmark harness** needs, per board, everything above
   *plus* usable reference copper to diff our router's output against.

"It parsed" is nowhere near sufficient for either. A board can load cleanly and
still expose **zero footprints and zero pads** (the corpus is full of KiCad-5-era
``(module ...)`` boards, which this repo's parser accepts without reading a
single component), or carry no net bindings, or have no outline, or ship
half-routed. Each of those makes the board useless as a benchmark input in a
*different* way, so this module scores each precondition separately and reports
which one is the dominant blocker.

The module is **offline and importable**: it reads payloads that
``check_manifest.py`` already cached (or any local ``.kicad_pcb`` you point it
at), does no network I/O, and writes only to a caller-supplied output directory.
That is what lets ``tests/`` import it, in line with the "no network in pytest"
rule that keeps the fetching scripts out of the suite.

Usage::

    # census the cached corpus sample (run check_manifest.py once first)
    uv run python scripts/corpus/benchmark_readiness.py

    # add local boards for comparison against the in-repo fleet
    uv run python scripts/corpus/benchmark_readiness.py \\
        --board boards/03-esp32-devkit/*.kicad_pcb
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from corpus_manifest import (
    DEFAULT_CACHE_DIR,
    DEFAULT_MANIFEST,
    ManifestError,
    cache_path_for,
    load_manifest,
)
from parse_taxonomy import declared_version as sniff_declared_version

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "scripts" / "corpus" / ".cache"

# --------------------------------------------------------------------------
# Thresholds. Deliberately few, deliberately named, deliberately documented:
# a readiness verdict that depends on unexplained magic numbers is not a
# measurement, it is an opinion.
# --------------------------------------------------------------------------

#: How many *routable nets* (nets with >= 2 pads) a board must have before
#: "our router vs. the human" means anything. Note the two levels: a net needs
#: >= 2 pads to be routable at all, and a board needs at least this many such
#: nets. Two *such nets* is the floor at which a comparison is arithmetically
#: possible, not the floor at which it is interesting -- the census reports
#: net counts so a harness can raise it.
MIN_MULTI_PAD_NETS = 2

#: Fraction of multi-pad nets that must carry copper before the board counts as
#: a *completed* human reference. Not 1.0: real boards legitimately leave a
#: mounting-hole or test-point net bare, and net-level copper presence is a
#: coarse proxy for connectivity (see ``routed_net_fraction``).
COMPLETE_REFERENCE_FRACTION = 0.95

# --------------------------------------------------------------------------
# Blocker codes -- one per *distinct engineering action*, same discipline as
# the parse taxonomy. Do not add a code that would be fixed the same way as an
# existing one.
# --------------------------------------------------------------------------

#: Parsed, but no footprints/pads came out: no component graph to featurize.
BLOCK_NO_PAD_GRAPH = "no-pad-graph"
#: The specific, fixable cause a ``no-pad-graph`` board *used* to have: the file
#: uses the pre-KiCad-6 ``(module ...)`` token, which the parser silently
#: ignored (it read ``(footprint ...)`` only). Issue #4873 taught
#: ``PCB._parse()`` the alias, so this blocker should now be empty on the
#: pinned corpus; it is kept as a live diagnostic in case some other legacy
#: dialect still yields no footprints at all -- naming the dialect keeps that
#: case actionable as parser work instead of hiding it in the generic bucket.
BLOCK_LEGACY_MODULE_SCHEMA = "legacy-module-schema"
#: Pads exist but the board has fewer than ``MIN_MULTI_PAD_NETS`` routable
#: nets -- usually none carry a net at all (a panel or a mechanical board), but
#: a board with exactly one multi-pad net also lands here: nothing to route,
#: or too little to compare.
BLOCK_NO_NET_BINDING = "no-net-binding"
#: No Edge.Cuts outline with positive area: no area, hence no density feature
#: and no routing region.
BLOCK_NO_OUTLINE = "no-outline"
#: The outline has area (its bounding box is fine) but cannot be reconstructed
#: as a polygon -- measured, not theoretical: a rounded rectangle whose corner
#: arcs use the pre-KiCad-6 centre+angle form (no ``mid`` token, so
#: ``GraphicArc.from_sexp`` defaults ``mid`` to the origin) yields 2 points,
#: which makes ``kct route`` false-positive its off-board preflight and then
#: die inside shapely with "A linearring requires at least 4 coordinates".
#: The equivalent KiCad-6 ``(start)(mid)(end)`` board chains fine. Distinct
#: from ``no-outline`` because the fix is legacy arc *parsing*, not a missing
#: Edge.Cuts.
BLOCK_OUTLINE_NOT_POLYGONAL = "outline-not-polygonal"
#: No copper at all: usable as an unrouted *input*, never as a human reference.
BLOCK_NO_REFERENCE_COPPER = "no-reference-copper"
#: Copper exists but a meaningful share of multi-pad nets is still bare --
#: scoring "we completed 80%" against a reference that is itself 60% routed
#: measures nothing.
BLOCK_PARTIAL_REFERENCE_ROUTING = "partial-reference-routing"

BLOCKER_CODES = (
    BLOCK_NO_PAD_GRAPH,
    BLOCK_LEGACY_MODULE_SCHEMA,
    BLOCK_NO_NET_BINDING,
    BLOCK_NO_OUTLINE,
    BLOCK_OUTLINE_NOT_POLYGONAL,
    BLOCK_NO_REFERENCE_COPPER,
    BLOCK_PARTIAL_REFERENCE_ROUTING,
)

#: Minimum reconstructed outline vertices for the routing region to be usable
#: (shapely needs 4 ring coordinates, i.e. 3 distinct points plus closure).
MIN_OUTLINE_POINTS = 3

#: Routing-completeness label attached to a board once its features are usable.
LABEL_COMPLETE = "human-routed"  # a positive routability label
LABEL_PARTIAL = "partially-routed"
LABEL_UNROUTED = "unrouted-input"  # features only, no label
LABEL_NONE = "unusable"  # features not extractable

# Deliberately a raw token scan, not a parse: it also matches a "(module "
# occurrence inside a quoted string or a comment. Harmless -- it is only a
# cause *hint*, read behind an already-failed pad-graph gate, never a gate of
# its own.
_MODULE_TOKEN_RE = re.compile(r"\(\s*module\s", re.MULTILINE)


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------
@dataclass
class BoardFeatures:
    """What a corpus board offers a benchmark, measured rather than assumed."""

    board_id: str
    group: str = "corpus"  # "corpus" | "local"
    source: str = ""
    declared_version: str | None = None
    size_bytes: int = 0

    # component / connectivity graph
    footprints: int = 0
    pads: int = 0
    netted_pads: int = 0
    nets_declared: int = 0
    multi_pad_nets: int = 0
    max_net_degree: int = 0

    # geometry
    copper_layers: int = 0
    board_mm: list[float] = field(default_factory=list)
    area_cm2: float = 0.0
    outline_points: int = 0

    # existing copper (the would-be human reference)
    segments: int = 0
    vias: int = 0
    zones: int = 0
    pour_zones_with_net: int = 0
    trace_length_mm: float = 0.0
    nets_with_copper: int = 0
    multi_pad_nets_with_copper: int = 0
    multi_pad_nets_with_trace_copper: int = 0

    # schema signal
    legacy_module_tokens: int = 0

    @property
    def pad_density_per_cm2(self) -> float:
        return round(self.pads / self.area_cm2, 3) if self.area_cm2 else 0.0

    @property
    def net_density_per_cm2(self) -> float:
        return round(self.multi_pad_nets / self.area_cm2, 3) if self.area_cm2 else 0.0

    @property
    def avg_pads_per_net(self) -> float:
        return round(self.netted_pads / self.multi_pad_nets, 2) if self.multi_pad_nets else 0.0

    @property
    def routed_net_fraction(self) -> float:
        """Share of multi-pad nets carrying copper (a trace, a via, or a pour).

        A coarse proxy for "the human finished it": net-level copper presence,
        not pad-level connectivity. A net whose copper connects 3 of its 4 pads
        counts as routed here. Deliberate -- a true connectivity check needs a
        DRC-grade connectivity solve per board, which is a harness's job, not a
        census's. The proxy is *optimistic*, so a board it rejects is certainly
        unusable while a board it accepts still needs the harness to confirm.

        Pours count. Omitting them is the single most misleading thing this
        metric could do: a ground net served entirely by a filled zone has no
        segments at all, and a trace-only definition scores a perfectly routed
        board as two-thirds finished (measured on this repo's own boards -- see
        ``pour_served_nets``).
        """
        if not self.multi_pad_nets:
            return 0.0
        return round(self.multi_pad_nets_with_copper / self.multi_pad_nets, 4)

    @property
    def pour_served_nets(self) -> int:
        """Multi-pad nets whose only copper is a pour, not a trace or via.

        A route-vs-human harness must decide what to do with these before it
        can score anything: our router serves such nets by pour too, so
        counting them as "unrouted by us" would understate completion, while
        counting the human's pour as a route would overstate wirelength.
        """
        return max(0, self.multi_pad_nets_with_copper - self.multi_pad_nets_with_trace_copper)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            {
                "pad_density_per_cm2": self.pad_density_per_cm2,
                "net_density_per_cm2": self.net_density_per_cm2,
                "avg_pads_per_net": self.avg_pads_per_net,
                "routed_net_fraction": self.routed_net_fraction,
                "pour_served_nets": self.pour_served_nets,
            }
        )
        return data


def extract_features(
    board: Any,
    text: str,
    *,
    board_id: str,
    group: str = "corpus",
    source: str = "",
    declared_version: str | None = None,
) -> BoardFeatures:
    """Derive :class:`BoardFeatures` from a parsed ``PCB`` and its source text.

    ``text`` is needed for the two signals that only exist *before* parsing:
    the declared file-format version and the legacy ``(module ...)`` token
    count that explains an empty component graph.
    """
    pads = [pad for fp in board.footprints for pad in fp.pads]
    netted = [pad for pad in pads if pad.net_number > 0]

    pads_per_net: Counter[int] = Counter(pad.net_number for pad in netted)
    multi_pad_nets = {net for net, count in pads_per_net.items() if count >= 2}

    trace_nets: set[int] = set()
    for seg in board.segments:
        if seg.net_number > 0:
            trace_nets.add(seg.net_number)
    for via in board.vias:
        if via.net_number > 0:
            trace_nets.add(via.net_number)

    # Pours carry copper too. ``rule_areas`` (keepouts) do not -- they are
    # constraints, not conductors -- so they are excluded here.
    rule_area_ids = {id(zone) for zone in board.rule_areas}
    pour_zones = [
        zone for zone in board.zones if zone.net_number > 0 and id(zone) not in rule_area_ids
    ]
    pour_nets = {zone.net_number for zone in pour_zones}
    copper_nets = trace_nets | pour_nets

    try:
        width, height = board.board_size
        board_mm = [round(float(width), 2), round(float(height), 2)]
    except Exception:  # a missing/degenerate outline is data, not an error
        board_mm = [0.0, 0.0]
    area_cm2 = round(board_mm[0] * board_mm[1] / 100.0, 3)

    try:
        outline_points = len(board.get_board_outline())
    except Exception:
        outline_points = 0

    try:
        trace_length = round(float(board.total_trace_length()), 2)
    except Exception:
        trace_length = 0.0

    return BoardFeatures(
        board_id=board_id,
        group=group,
        source=source,
        declared_version=declared_version or sniff_declared_version(text),
        size_bytes=len(text.encode("utf-8")),
        footprints=len(board.footprints),
        pads=len(pads),
        netted_pads=len(netted),
        nets_declared=len(board.nets),
        multi_pad_nets=len(multi_pad_nets),
        max_net_degree=max(pads_per_net.values(), default=0),
        copper_layers=len(board.copper_layers),
        board_mm=board_mm,
        area_cm2=area_cm2,
        outline_points=outline_points,
        segments=board.segment_count,
        vias=board.via_count,
        zones=board.zone_count,
        pour_zones_with_net=len(pour_zones),
        trace_length_mm=trace_length,
        nets_with_copper=len(copper_nets),
        multi_pad_nets_with_copper=len(multi_pad_nets & copper_nets),
        multi_pad_nets_with_trace_copper=len(multi_pad_nets & trace_nets),
        legacy_module_tokens=len(_MODULE_TOKEN_RE.findall(text)),
    )


def features_for_file(
    path: Path,
    *,
    board_id: str | None = None,
    group: str = "local",
    declared_version: str | None = None,
) -> BoardFeatures:
    """Load a ``.kicad_pcb`` and featurize it (parse errors propagate)."""
    from kicad_tools.schema import PCB

    text = path.read_text(encoding="utf-8", errors="replace")
    board = PCB.load(path)
    return extract_features(
        board,
        text,
        board_id=board_id or path.stem,
        group=group,
        source=str(path),
        declared_version=declared_version,
    )


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------
@dataclass
class Readiness:
    """Per-board verdict for each of the two proposed capabilities."""

    board_id: str
    group: str = "corpus"
    label: str = LABEL_NONE
    #: features extractable: pad graph + net bindings + geometry
    capacity_features: bool = False
    #: features *and* a positive routability label -- a calibration example
    capacity_labeled: bool = False
    #: usable as a route-vs-human benchmark case
    harness_candidate: bool = False
    capacity_blockers: list[str] = field(default_factory=list)
    harness_blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate(features: BoardFeatures) -> Readiness:
    """Score one board against both capabilities' preconditions."""
    blockers: list[str] = []

    if features.footprints == 0 or features.pads == 0:
        blockers.append(BLOCK_NO_PAD_GRAPH)
        if features.footprints == 0 and features.legacy_module_tokens > 0:
            # Name the cause, not just the symptom: a legacy-dialect board that
            # yields *no footprints at all* is parser work, not corpus noise.
            # Since #4873 the plain ``(module ...)`` rename is understood, so
            # this fires only for a dialect we still cannot read -- keyed on
            # zero footprints (not zero pads), because a legacy board whose
            # modules simply carry no pads is a mechanical board, not a parse
            # gap.
            blockers.append(BLOCK_LEGACY_MODULE_SCHEMA)
    elif features.multi_pad_nets < MIN_MULTI_PAD_NETS:
        blockers.append(BLOCK_NO_NET_BINDING)

    if features.area_cm2 <= 0.0:
        blockers.append(BLOCK_NO_OUTLINE)
    elif features.outline_points < MIN_OUTLINE_POINTS:
        blockers.append(BLOCK_OUTLINE_NOT_POLYGONAL)

    capacity_features = not blockers

    label = LABEL_NONE
    if capacity_features:
        fraction = features.routed_net_fraction
        if fraction >= COMPLETE_REFERENCE_FRACTION:
            label = LABEL_COMPLETE
        elif fraction > 0.0:
            label = LABEL_PARTIAL
        else:
            label = LABEL_UNROUTED

    harness_blockers = list(blockers)
    if capacity_features:
        if label == LABEL_UNROUTED:
            harness_blockers.append(BLOCK_NO_REFERENCE_COPPER)
        elif label == LABEL_PARTIAL:
            harness_blockers.append(BLOCK_PARTIAL_REFERENCE_ROUTING)

    return Readiness(
        board_id=features.board_id,
        group=features.group,
        label=label,
        capacity_features=capacity_features,
        capacity_labeled=capacity_features and label == LABEL_COMPLETE,
        harness_candidate=not harness_blockers,
        capacity_blockers=blockers,
        harness_blockers=harness_blockers,
    )


# --------------------------------------------------------------------------
# Census
# --------------------------------------------------------------------------
def census(
    features: list[BoardFeatures],
    verdicts: list[Readiness],
    *,
    unavailable: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-board verdicts into a machine-readable readiness report."""
    unavailable = unavailable or []
    total = len(verdicts)
    by_label = Counter(v.label for v in verdicts)

    blocker_counts: Counter[str] = Counter()
    for verdict in verdicts:
        for code in set(verdict.harness_blockers):
            blocker_counts[code] += 1

    labeled = [f for f, v in zip(features, verdicts, strict=True) if v.capacity_labeled]
    candidates = [f for f, v in zip(features, verdicts, strict=True) if v.harness_candidate]

    def _rate(count: int) -> float | None:
        return round(count / total, 4) if total else None

    return {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thresholds": {
            "min_multi_pad_nets": MIN_MULTI_PAD_NETS,
            "complete_reference_fraction": COMPLETE_REFERENCE_FRACTION,
            "min_outline_points": MIN_OUTLINE_POINTS,
        },
        "totals": {
            "boards": total,
            "unavailable": len(unavailable),
            "capacity_features": sum(1 for v in verdicts if v.capacity_features),
            "capacity_labeled": len(labeled),
            "harness_candidates": len(candidates),
            "capacity_feature_rate": _rate(sum(1 for v in verdicts if v.capacity_features)),
            "capacity_label_rate": _rate(len(labeled)),
            "harness_candidate_rate": _rate(len(candidates)),
            # Candidates where at least one net is served by a pour alone: the
            # harness has to define how it scores those before it scores at all.
            "pour_dependent_candidates": sum(1 for f in candidates if f.pour_served_nets > 0),
        },
        "by_label": dict(sorted(by_label.items())),
        "blockers": dict(sorted(blocker_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "candidate_profile": _profile(candidates),
        "labeled_profile": _profile(labeled),
        "boards": [
            {**f.to_dict(), **v.to_dict()}
            for f, v in sorted(
                zip(features, verdicts, strict=True), key=lambda pair: pair[0].board_id
            )
        ],
        "unavailable_boards": unavailable,
    }


def _profile(features: list[BoardFeatures]) -> dict[str, Any]:
    """Range/median summary of a board subset (empty subset -> empty dict)."""
    if not features:
        return {}

    def _span(values: list[float]) -> dict[str, float]:
        # "median" here is the upper-middle order statistic, not an interpolated
        # median: for an even-sized subset it is the higher of the two middle
        # values. Good enough for a distribution sketch over ~8 boards; do not
        # read it as a statistic.
        ordered = sorted(values)
        middle = ordered[len(ordered) // 2]
        return {
            "min": round(ordered[0], 3),
            "median": round(middle, 3),
            "max": round(ordered[-1], 3),
        }

    return {
        "count": len(features),
        "pads": _span([float(f.pads) for f in features]),
        "multi_pad_nets": _span([float(f.multi_pad_nets) for f in features]),
        "copper_layers": _span([float(f.copper_layers) for f in features]),
        "area_cm2": _span([f.area_cm2 for f in features]),
        "pad_density_per_cm2": _span([f.pad_density_per_cm2 for f in features]),
        "trace_length_mm": _span([f.trace_length_mm for f in features]),
    }


def render_census(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`census` output."""
    totals = report["totals"]
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("corpus benchmark-readiness census")
    lines.append("=" * 78)
    lines.append(f"generated  : {report['generated_utc']}")
    lines.append(
        "thresholds : "
        f"multi-pad nets >= {report['thresholds']['min_multi_pad_nets']}, "
        f"complete reference >= {report['thresholds']['complete_reference_fraction']:.0%} "
        "of multi-pad nets routed, "
        f"outline >= {report['thresholds']['min_outline_points']} points"
    )
    lines.append("")
    lines.append("-- totals " + "-" * 68)
    lines.append(f"boards evaluated       : {totals['boards']} ({totals['unavailable']} skipped)")
    for key, rate_key, caption in (
        ("capacity_features", "capacity_feature_rate", "featurizable (#4799)"),
        ("capacity_labeled", "capacity_label_rate", "labeled examples"),
        ("harness_candidates", "harness_candidate_rate", "route-vs-human cases"),
    ):
        rate = totals[rate_key]
        suffix = f" ({rate:.1%})" if rate is not None else ""
        lines.append(f"{caption:<23}: {totals[key]}{suffix}")
    lines.append(
        f"{'  of which pour-served':<23}: {totals['pour_dependent_candidates']} "
        "(>= 1 net whose only copper is a pour)"
    )
    lines.append("")
    lines.append("-- routing labels " + "-" * 60)
    for label, count in report["by_label"].items():
        lines.append(f"  {label:<22} {count:>4}")
    lines.append("")
    lines.append("-- blockers (boards disqualified, by cause) " + "-" * 34)
    if not report["blockers"]:
        lines.append("  (none -- every board is benchmark-ready)")
    for code, count in report["blockers"].items():
        lines.append(f"  {code:<28} {count:>4}")
    for key, caption in (
        ("candidate_profile", "route-vs-human candidates"),
        ("labeled_profile", "labeled calibration examples"),
    ):
        profile = report[key]
        if not profile:
            continue
        lines.append("")
        lines.append(f"-- {caption} (n={profile['count']}) " + "-" * 30)
        for metric in (
            "pads",
            "multi_pad_nets",
            "copper_layers",
            "area_cm2",
            "pad_density_per_cm2",
            "trace_length_mm",
        ):
            span = profile[metric]
            lines.append(
                f"  {metric:<22} min={span['min']:<10} median={span['median']:<10} "
                f"max={span['max']}"
            )
    lines.append("")
    lines.append("-- per board " + "-" * 65)
    lines.append(
        f"  {'board':<24} {'ver':<9} {'pads':>5} {'nets':>5} {'lay':>4} "
        f"{'area':>8} {'routed':>7} {'pour':>5}  label / blockers"
    )
    for board in report["boards"]:
        blockers = ",".join(board["harness_blockers"]) or "-"
        lines.append(
            f"  {board['board_id']:<24} {str(board['declared_version'] or '?'):<9} "
            f"{board['pads']:>5} {board['multi_pad_nets']:>5} {board['copper_layers']:>4} "
            f"{board['area_cm2']:>8.1f} {board['routed_net_fraction']:>7.2f} "
            f"{board['pour_served_nets']:>5}  {board['label']} / {blockers}"
        )
    if report["unavailable_boards"]:
        lines.append("")
        lines.append("-- skipped " + "-" * 67)
        for item in report["unavailable_boards"]:
            lines.append(f"  {item['board_id']:<24} {item['reason']}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def collect_manifest_boards(
    manifest_path: Path,
    cache_dir: Path,
) -> tuple[list[BoardFeatures], list[dict[str, str]]]:
    """Featurize every cached PCB entry of a manifest.

    Entries that were never fetched are reported as unavailable rather than
    counted as failures: a cold cache is an operator state, not a data property.
    """
    manifest = load_manifest(manifest_path)
    features: list[BoardFeatures] = []
    unavailable: list[dict[str, str]] = []

    for entry in manifest.by_kind("pcb"):
        path = cache_path_for(entry, cache_dir)
        if not path.is_file():
            unavailable.append(
                {
                    "board_id": entry.id,
                    "reason": "not cached (run check_manifest.py first)",
                }
            )
            continue
        try:
            features.append(
                features_for_file(
                    path,
                    board_id=entry.id,
                    group="corpus",
                    declared_version=entry.declared_version,
                )
            )
        except Exception as exc:  # a parse failure is check_manifest.py's beat
            unavailable.append(
                {
                    "board_id": entry.id,
                    "reason": f"{type(exc).__name__}: {exc}"[:160],
                }
            )
    return features, unavailable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score corpus boards for capacity-predictor calibration and "
        "route-vs-human benchmarking (issue #4830, slice 4).",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--board",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="extra local .kicad_pcb to score (repeatable) -- e.g. an in-repo board, "
        "for comparison against the corpus distribution",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="score only the --board paths, ignoring the cached manifest",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    features: list[BoardFeatures] = []
    unavailable: list[dict[str, str]] = []

    if not args.no_manifest:
        try:
            manifest_features, unavailable = collect_manifest_boards(args.manifest, args.cache_dir)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        features.extend(manifest_features)

    for path in args.board:
        try:
            features.append(features_for_file(path, group="local"))
        except Exception as exc:
            unavailable.append({"board_id": path.stem, "reason": f"{type(exc).__name__}: {exc}"})

    if not features:
        print(
            "error: no boards to score -- run check_manifest.py to populate the "
            "payload cache, or pass --board PATH",
            file=sys.stderr,
        )
        return 2

    verdicts = [evaluate(f) for f in features]
    report = census(features, verdicts, unavailable=unavailable)
    text = render_census(report)
    if not args.quiet:
        print(text)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.out / f"benchmark-readiness-{stamp}.json"
    text_path = args.out / f"benchmark-readiness-{stamp}.txt"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    print(f"JSON report : {json_path}")
    print(f"text report : {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
