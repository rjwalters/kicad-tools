"""Report-driven rip-up and iterative dangling removal for ``kct pcb strip``.

Two planners, both returning plain removal lists so the CLI handler only has
to parse arguments, apply and verify (issue #8309):

* :func:`plan_from_report` -- for each ``clearance`` / ``shorting_items``
  finding in a kicad-cli DRC JSON (or each located row of a ``kct creepage``
  JSON) pick one victim track/via and cut it.  Pads, zones and footprint
  items are never removed.
* :func:`strip_dangling` -- repeatedly delete ``track_dangling`` /
  ``via_dangling`` copper until a pass removes nothing, refusing any pass
  that would increase the unconnected count.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.pcb import PCB, Segment, Via

_CUT_TYPES = {"clearance", "shorting_items"}


@dataclass
class Finding:
    """One cut request: a location, the uuids/nets it names."""

    source: str  # "drc" | "creepage"
    kind: str
    location: tuple[float, float] | None
    uuids: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)


@dataclass
class FindingResult:
    finding: Finding
    status: str  # "cut" | "skipped"
    reason: str = ""
    removed: list[str] = field(default_factory=list)
    nets_cut: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        loc = self.finding.location
        return {
            "source": self.finding.source,
            "type": self.finding.kind,
            "location": list(loc) if loc else None,
            "status": self.status,
            "reason": self.reason,
            "removed": self.removed,
            "nets_cut": self.nets_cut,
        }


@dataclass
class StripPlan:
    segments: list[Segment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    results: list[FindingResult] = field(default_factory=list)

    @property
    def nets_cut(self) -> list[str]:
        return sorted({n for r in self.results for n in r.nets_cut})


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------


def load_findings(path: Path | str) -> list[Finding]:
    """Load findings from a kicad-cli DRC JSON or a ``kct creepage`` JSON."""
    path = Path(path)
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "violations" in data:
        return _drc_findings(path)
    return _creepage_findings(data)


def _drc_findings(path: Path) -> list[Finding]:
    from kicad_tools.drc.report import DRCReport

    report = DRCReport.load(path)
    out: list[Finding] = []
    for v in report.violations:
        if v.type_str not in _CUT_TYPES:
            continue
        loc = (v.locations[0].x_mm, v.locations[0].y_mm) if v.locations else None
        out.append(Finding("drc", v.type_str, loc, list(v.item_uuids), list(v.nets)))
    return out


def _creepage_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("rows", "pairs", "results", "violations", "findings"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    return []


def _creepage_findings(data: Any) -> list[Finding]:
    out: list[Finding] = []
    for row in _creepage_rows(data):
        wit = row.get("witness")
        loc: tuple[float, float] | None = None
        uuids: list[str] = []
        if isinstance(wit, dict):
            pt = wit.get("point") or wit.get("position")
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                loc = (float(pt[0]), float(pt[1]))
            elif "x" in wit and "y" in wit:
                loc = (float(wit["x"]), float(wit["y"]))
            for key in ("uuid", "item_uuid"):
                if wit.get(key):
                    uuids.append(str(wit[key]))
            uuids.extend(str(u) for u in wit.get("uuids", []) or [])
        nets = [str(row[k]) for k in ("net_a", "net_b") if row.get(k)]
        out.append(Finding("creepage", "creepage", loc, uuids, nets))
    return out


# ---------------------------------------------------------------------------
# Report-driven planning
# ---------------------------------------------------------------------------


def _net_of(item: Segment | Via) -> str:
    return item.net_name or (str(item.net_number) if item.net_number else "")


def _seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    ln = dx * dx + dy * dy
    t = 0.0 if ln == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / ln))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _item_dist(item: Segment | Via, p: tuple[float, float]) -> float:
    if isinstance(item, Via):
        return max(0.0, math.dist(item.position, p) - item.size / 2.0)
    return max(0.0, _seg_dist(p, item.start, item.end) - item.width / 2.0)


def _describe(item: Segment | Via) -> str:
    if item.uuid:
        return item.uuid
    if isinstance(item, Via):
        return f"via@({item.position[0]:.3f},{item.position[1]:.3f})"
    return f"seg({item.start[0]:.3f},{item.start[1]:.3f})-({item.end[0]:.3f},{item.end[1]:.3f})"


def _candidates(pcb: PCB, finding: Finding, protect: set[str]) -> tuple[list[Segment | Via], bool]:
    """Ordered victim candidates (side A first); bool = a candidate was protected."""
    by_uuid: dict[str, Segment | Via] = {}
    for s in pcb.segments:
        if s.uuid:
            by_uuid[s.uuid] = s
    for v in pcb.vias:
        if v.uuid:
            by_uuid[v.uuid] = v
    found: list[Segment | Via] = []
    for u in finding.uuids:
        item = by_uuid.get(u)
        if item is not None and item not in found:
            found.append(item)
    if not found and finding.location is not None:
        # Location + net fallback: copper touching the finding point whose net
        # is one of the finding's nets (when it names any).
        loc = finding.location
        named = set(finding.nets)
        near = []
        for item in [*pcb.segments, *pcb.vias]:
            if named and _net_of(item) not in named:
                continue
            d = _item_dist(item, loc)
            if d <= 0.05:
                near.append((d, item))
        near.sort(key=lambda t: t[0])
        found = [i for _, i in near]
    protected_hit = any(_net_of(i) in protect for i in found)
    return [i for i in found if _net_of(i) not in protect], protected_hit


def plan_from_report(
    pcb: PCB,
    findings: list[Finding],
    protect: set[str] | None = None,
    radius: float = 0.0,
) -> StripPlan:
    """Plan one victim removal per finding (deterministic: first side wins)."""
    protect = protect or set()
    plan = StripPlan()
    seen: set[int] = set()
    for f in findings:
        if f.source == "creepage" and f.location is None and not f.uuids:
            plan.results.append(FindingResult(f, "skipped", "no location"))
            continue
        cands, hit = _candidates(pcb, f, protect)
        if not cands:
            reason = "protected" if hit else "item not found"
            plan.results.append(FindingResult(f, "skipped", reason))
            continue
        victim = cands[0]
        group: list[Segment | Via] = [victim]
        if radius > 0 and f.location is not None:
            vnet = _net_of(victim)
            for item in [*pcb.segments, *pcb.vias]:
                if item is victim or _net_of(item) != vnet:
                    continue
                if _item_dist(item, f.location) <= radius:
                    group.append(item)
        removed: list[str] = []
        for item in group:
            if id(item) in seen:
                continue
            seen.add(id(item))
            (plan.vias if isinstance(item, Via) else plan.segments).append(item)
            removed.append(_describe(item))
        plan.results.append(
            FindingResult(f, "cut", removed=removed, nets_cut=[_net_of(victim)] if removed else [])
        )
    return plan


# ---------------------------------------------------------------------------
# Dangling removal
# ---------------------------------------------------------------------------


def unconnected_count(pcb: PCB) -> int:
    """Terminal components beyond the first, summed over nets (pad/zone islands)."""
    from kicad_tools.pcb_prune import _build_net_models

    total = 0
    for model in _build_net_models(pcb).values():
        terms = model.terminals
        if len(terms) < 2:
            continue
        seen: set[int] = set()
        comps = 0
        for t in terms:
            if t in seen:
                continue
            comps += 1
            stack = [t]
            seen.add(t)
            while stack:
                n = stack.pop()
                for m in model.adj[n]:
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
        total += max(0, comps - 1)
    return total


@dataclass
class DanglingResult:
    passes: int = 0
    segments: int = 0
    vias: int = 0
    kept_vias: int = 0
    nets_cut: list[str] = field(default_factory=list)
    refused: bool = False
    note: str = ""


def _dangling_victims(pcb: PCB, keep_via_min: float | None) -> tuple[list[Segment], list[Via], int]:
    from kicad_tools.validate.rules.dangling_copper import DanglingCopperRule

    rule = DanglingCopperRule()
    results = rule.check(pcb, design_rules=None)  # type: ignore[arg-type]
    segs: list[Segment] = []
    vias: list[Via] = []
    kept = 0
    by_uuid = {s.uuid: s for s in pcb.segments if s.uuid}
    for v in [*results.violations]:
        rid = getattr(v, "rule_id", "")
        if rid == "track_dangling":
            seg = next((by_uuid[i] for i in v.items if i in by_uuid), None)
            if seg is not None:
                segs.append(seg)
        elif rid == "via_dangling":
            loc = v.location
            via = next((x for x in pcb.vias if loc and math.dist(x.position, loc) < 1e-6), None)
            if via is None:
                continue
            if keep_via_min is not None and (via.drill >= keep_via_min or via.size >= keep_via_min):
                kept += 1
                continue
            vias.append(via)
    return segs, vias, kept


def _trial_unconnected(pcb: PCB, segs: list[Segment], vias: list[Via]) -> int:
    """Unconnected count after removing ``segs``/``vias`` from a scratch reload."""
    import tempfile

    seg_idx = {id(s): i for i, s in enumerate(pcb.segments)}
    via_idx = {id(v): i for i, v in enumerate(pcb.vias)}
    with tempfile.TemporaryDirectory(prefix="kct_strip_") as tmp:
        path = Path(tmp) / "trial.kicad_pcb"
        pcb.save(path)
        trial = PCB.load(path)
        tsegs = [trial.segments[seg_idx[id(s)]] for s in segs]
        tvias = [trial.vias[via_idx[id(v)]] for v in vias]
        trial.remove_segments(tsegs)
        trial.remove_vias(tvias)
        return unconnected_count(trial)


def strip_dangling(
    pcb: PCB, keep_via_min: float | None = None, max_passes: int = 100
) -> DanglingResult:
    """Remove dangling copper in place until stable.

    A pass that would raise :func:`unconnected_count` is not applied; the run
    stops with ``refused=True``.
    """
    res = DanglingResult()
    nets: set[str] = set()
    for _ in range(max_passes):
        segs, vias, kept = _dangling_victims(pcb, keep_via_min)
        res.kept_vias = kept
        if not segs and not vias:
            break
        before = unconnected_count(pcb)
        after = _trial_unconnected(pcb, segs, vias)
        if after > before:
            res.refused = True
            res.note = (
                f"pass {res.passes + 1} would raise unconnected {before}->{after}; not applied"
            )
            break
        nets.update(_net_of(i) for i in [*segs, *vias])
        pcb.remove_segments(segs)
        pcb.remove_vias(vias)
        res.passes += 1
        res.segments += len(segs)
        res.vias += len(vias)
    res.nets_cut = sorted(n for n in nets if n)
    return res
