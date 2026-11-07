"""Creepage-aware repair: translate movable copper to clear a failing HV pair (Issue #8545).

``kct repair-clearance`` is driven by DRC reports and DRC clearance floors, so
it cannot act on a *creepage* finding (``kct creepage``).  This module adds the
missing consumer: for every failing, unwaived census pair it translates the
movable copper (track segments, then vias) on one side of the pair along the
census **witness normal** by the shortfall plus a margin.

It deliberately reuses :class:`~kicad_tools.drc.repair_clearance.ClearanceRepairer`'s
copper index, trial-move journal and rollback ("Honest accounting": move,
re-measure, undo when the move did not help or hurt a third item) instead of
re-implementing them.  Differences from ``repair-clearance``:

* the target per pair is that pair's own *required* creepage/clearance,
* widths are never touched, and a track end inside a pad of its own net may
  slide with the move as long as the joint survives (the pad-attached riser
  case that motivated the issue); ends on a via or on locked copper stay put,
* a move is **kept only if** (a) the census pair now passes, (b) no other
  census pair got worse, and (c) the (optional) DRC check did not regress.
  Otherwise it is rolled back and the reason is reported.

The census and DRC are injected as callables so the engine has no opinion on
how thresholds are derived; the CLI wires them from ``kct creepage``'s flags.

.. warning::
   An engineering aid, NOT a certification.  Re-run ``kct creepage`` and the
   governing standard on the saved board.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.creepage.engine import _norm_net_key
from kicad_tools.drc.repair_clearance import ClearanceRepairer, _Copper, _is_locked
from kicad_tools.router.clearance_kernel import KVia, copper_gap
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string, serialize_sexp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.creepage.engine import CreepagePair, CreepageReport

CensusFn = Callable[[PCB], "CreepageReport"]
#: Count of DRC errors on a board's text, or ``None`` when DRC cannot run.
DrcFn = Callable[[str], "int | None"]

_EPS = 1e-4
#: How close (mm) a copper item must be to a witness point to be "the" item.
_WITNESS_TOL_MM = 0.01


@dataclass
class CreepageMove:
    """One translation the repairer kept (or, with ``--dry-run``, proposes)."""

    net_a: str
    net_b: str
    kind: str
    layer: str
    item_type: str  # "segment" | "via"
    item_net: str
    uuid: str
    dx_mm: float
    dy_mm: float
    witness: dict[str, Any]
    old_creepage_mm: float
    new_creepage_mm: float
    old_margin_mm: float
    new_margin_mm: float
    dragged: int = 0  # extra track ends that followed the move

    @property
    def distance_mm(self) -> float:
        return math.hypot(self.dx_mm, self.dy_mm)

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_a": self.net_a,
            "net_b": self.net_b,
            "kind": self.kind,
            "layer": self.layer,
            "item_type": self.item_type,
            "item_net": self.item_net,
            "uuid": self.uuid,
            "dx_mm": round(self.dx_mm, 4),
            "dy_mm": round(self.dy_mm, 4),
            "distance_mm": round(self.distance_mm, 4),
            "witness": self.witness,
            "old_creepage_mm": round(self.old_creepage_mm, 4),
            "new_creepage_mm": round(self.new_creepage_mm, 4),
            "old_margin_mm": round(self.old_margin_mm, 4),
            "new_margin_mm": round(self.new_margin_mm, 4),
            "dragged_track_ends": self.dragged,
        }

    def __str__(self) -> str:
        w = self.witness
        pa, pb = w.get("point_a_mm"), w.get("point_b_mm")
        loc = (
            f"witness A({pa[0]:.3f}, {pa[1]:.3f}) B({pb[0]:.3f}, {pb[1]:.3f})" if pa and pb else ""
        )
        return (
            f"{self.item_type} {self.uuid or '<no uuid>'} [{self.item_net}] "
            f"{self.net_a} vs {self.net_b}: move ({self.dx_mm:+.4f}, {self.dy_mm:+.4f}) mm, "
            f"creepage {self.old_creepage_mm:.4f} -> {self.new_creepage_mm:.4f} mm; {loc}"
        )


@dataclass
class RejectedMove:
    """A candidate move that was tried (or ruled out) and undone, with why."""

    net_a: str
    net_b: str
    item: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"net_a": self.net_a, "net_b": self.net_b, "item": self.item, "reason": self.reason}

    def __str__(self) -> str:
        return f"{self.net_a} vs {self.net_b} via {self.item}: {self.reason}"


@dataclass
class UnrepairedCreepage:
    """A failing pair no kept move cleared."""

    net_a: str
    net_b: str
    kind: str
    layer: str
    margin_mm: float
    reason: str
    witness: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_a": self.net_a,
            "net_b": self.net_b,
            "kind": self.kind,
            "layer": self.layer,
            "margin_mm": round(self.margin_mm, 4),
            "reason": self.reason,
            "witness": self.witness,
        }

    def __str__(self) -> str:
        return (
            f"{self.net_a} vs {self.net_b} ({self.kind}, margin {self.margin_mm:+.4f} mm): "
            f"{self.reason}"
        )


@dataclass
class CreepageRepairResult:
    """Outcome of :meth:`CreepageRepairer.repair`."""

    moves: list[CreepageMove] = field(default_factory=list)
    rejected: list[RejectedMove] = field(default_factory=list)
    unrepaired: list[UnrepairedCreepage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failing_before: int = 0
    failing_after: int = 0
    dry_run: bool = False

    @property
    def success(self) -> bool:
        """``True`` when no failing pair was left unrepaired."""
        return not self.unrepaired

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "failing_before": self.failing_before,
            "failing_after": self.failing_after,
            "success": self.success,
            "moves": [m.to_dict() for m in self.moves],
            "rejected": [r.to_dict() for r in self.rejected],
            "unrepaired": [u.to_dict() for u in self.unrepaired],
            "notes": list(self.notes),
        }


def _headroom(pair: CreepagePair) -> float:
    """Smaller of the creepage and (if derived) clearance margins; negative == fail."""
    cm = pair.clearance_margin_mm
    return pair.margin_mm if cm is None else min(pair.margin_mm, cm)


def _pair_key(net_a: str, net_b: str, kind: str) -> tuple[frozenset[str], str]:
    return (frozenset((_norm_net_key(net_a), _norm_net_key(net_b))), kind)


def _index_pairs(report: CreepageReport) -> dict[tuple[frozenset[str], str], CreepagePair]:
    return {_pair_key(p.net_a, p.net_b, p.kind): p for p in report.pairs}


def failing_rows_from_report_dict(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(net_a, net_b, kind)`` of every unwaived failing row of a ``kct creepage`` JSON."""
    rows = []
    for row in data.get("pairs", []):
        if row.get("pass", True) or row.get("waived"):
            continue
        rows.append((str(row["net_a"]), str(row["net_b"]), str(row.get("kind", "conductor"))))
    return rows


class CreepageRepairer(ClearanceRepairer):
    """Translate movable copper to clear creepage census shortfalls."""

    # -- pinning ------------------------------------------------------------

    def _pin_state(self, node: Any, via_points: list[tuple[float, float]]) -> tuple[bool, bool]:
        """A track end is held only on a via or on locked copper.

        Unlike ``repair-clearance`` an end inside a same-net pad is *not*
        pinned: the riser translates with its joint and the post-move
        connectivity check (``_lost_contact``) rejects the move if the joint
        would leave the pad.
        """
        sx, sy, ex, ey = self._segment_xy(node)
        tol = 0.001

        def on_via(x: float, y: float) -> bool:
            return any(math.hypot(x - vx, y - vy) <= tol for vx, vy in via_points)

        return (
            on_via(sx, sy) or self._joined_to_locked(node, sx, sy),
            on_via(ex, ey) or self._joined_to_locked(node, ex, ey),
        )

    # -- helpers ------------------------------------------------------------

    def _pcb(self) -> PCB:
        """The current (possibly trial-edited) document as an independent PCB."""
        text = serialize_sexp(self.doc, preserve_source=True)
        return PCB(parse_string(text), path=self.path)

    def _candidates(
        self, net: str, layer: str, point: tuple[float, float], item_label: str
    ) -> tuple[list[_Copper], str | None]:
        """Movable segments/vias of ``net`` touching a witness ``point``.

        Returns ``(candidates, blocker)``; ``blocker`` explains an empty list.
        """
        ox, oy = getattr(self, "_origin", (0.0, 0.0))
        probe = KVia(point[0] + ox, point[1] + oy, 0.0)
        wanted_uuid = item_label.split(":", 1)[1] if ":" in item_label else ""
        want = _norm_net_key(net)
        found: list[tuple[float, _Copper]] = []
        locked = False
        for copper in self._ensure_index():
            if copper.kind not in ("segment", "via") or _norm_net_key(copper.net) != want:
                continue
            layers = self._copper_layers(copper)
            if layer not in ("*", "") and layers is not None and layer not in layers:
                continue
            gap = copper_gap(self._shape_of(copper), probe)
            if gap > _WITNESS_TOL_MM:
                continue
            if _is_locked(copper.node):
                locked = True
                continue
            prefer = 0.0 if self._node_uuid(copper.node) == wanted_uuid else 1.0
            found.append((prefer * 1.0 + max(gap, 0.0), copper))
        found.sort(key=lambda t: (t[0], t[1].kind != "segment"))
        # Segments first, then vias (issue: "track segments, then vias").
        ordered = [c for _s, c in found]
        ordered.sort(key=lambda c: c.kind != "segment")
        if ordered:
            return ordered, None
        if locked:
            return [], "the copper at the witness is locked"
        return [], "the witness is on pad/zone/edge copper, which this tool never moves"

    # -- main entry ----------------------------------------------------------

    def repair(
        self,
        census_fn: CensusFn,
        *,
        only: list[tuple[str, str, str]] | None = None,
        margin: float = 0.05,
        max_displacement: float = 0.5,
        dry_run: bool = False,
        drc_fn: DrcFn | None = None,
    ) -> CreepageRepairResult:
        """One pass over the failing, unwaived census pairs.

        Args:
            census_fn: Builds the creepage census (waivers already applied)
                from a :class:`PCB`.
            only: Restrict to these ``(net_a, net_b, kind)`` rows (from
                ``--from-report``); ``None`` repairs every failing pair.
            margin: Extra distance (mm) added to the shortfall.
            max_displacement: Largest allowed move (mm) per pair.
            dry_run: Plan on an in-memory copy; the caller must not save.
            drc_fn: Returns the DRC error count of a board text, or ``None``
                when DRC cannot run (reported in ``notes``, never silent).
        """
        result = CreepageRepairResult(dry_run=dry_run)
        first = self._pcb()
        # Census witnesses are in the PCB's board-relative frame; the S-expression
        # document is in file coordinates.
        self._origin = tuple(first._board_origin)
        current = census_fn(first)
        failing = [p for p in current.pairs if not p.passed and not p.waived]
        if only is not None:
            wanted = {_pair_key(a, b, k) for a, b, k in only}
            failing = [p for p in failing if _pair_key(p.net_a, p.net_b, p.kind) in wanted]
            present = {_pair_key(p.net_a, p.net_b, p.kind) for p in current.pairs}
            for a, b, k in only:
                if _pair_key(a, b, k) not in present:
                    result.unrepaired.append(
                        UnrepairedCreepage(
                            a, b, k, "*", 0.0, "pair not found in the current census"
                        )
                    )
        result.failing_before = len(failing)

        drc_count: int | None = None
        if drc_fn is not None:
            drc_count = drc_fn(serialize_sexp(self.doc, preserve_source=True))
        if drc_count is None:
            result.notes.append(
                "DRC regression check NOT run (kicad-cli unavailable or DRC failed); "
                "moves were verified against the creepage census only."
            )

        for stale in failing:
            now = _index_pairs(current).get(_pair_key(stale.net_a, stale.net_b, stale.kind))
            if now is None or now.passed:
                continue  # cleared by an earlier move
            outcome = self._repair_pair(
                now, current, census_fn, margin, max_displacement, drc_fn, drc_count, result
            )
            if outcome is None:
                continue
            current, drc_count = outcome

        result.failing_after = len([p for p in current.pairs if not p.passed and not p.waived])
        if dry_run:
            result.notes.append("dry run: nothing was written")
        return result

    def _repair_pair(
        self,
        pair: CreepagePair,
        current: CreepageReport,
        census_fn: CensusFn,
        margin: float,
        max_displacement: float,
        drc_fn: DrcFn | None,
        drc_count: int | None,
        result: CreepageRepairResult,
    ) -> tuple[CreepageReport, int | None] | None:
        """Try to clear one pair; ``(new census, new drc count)`` when a move was kept."""
        row = pair.to_dict()
        witness = row.get("witness")
        shortfall = max(-pair.margin_mm, -(pair.clearance_margin_mm or 0.0), 0.0)

        def give_up(reason: str) -> None:
            result.unrepaired.append(
                UnrepairedCreepage(
                    pair.net_a, pair.net_b, pair.kind, pair.layer, _headroom(pair), reason, witness
                )
            )

        if not witness:
            give_up("the census recorded no witness for this pair")
            return None
        if shortfall > max_displacement + _EPS:
            give_up(
                f"shortfall {shortfall:.4f} mm exceeds --max-displacement {max_displacement:.4f} mm"
            )
            return None
        distance = min(shortfall + margin, max_displacement)

        reasons: list[str] = []
        for side in ("a", "b"):
            mine = tuple(witness[f"point_{side}_mm"])
            other = tuple(witness[f"point_{'b' if side == 'a' else 'a'}_mm"])
            net = pair.net_a if side == "a" else pair.net_b
            norm = math.hypot(mine[0] - other[0], mine[1] - other[1])
            if norm < 1e-9:
                reasons.append(f"{net}: witness points coincide (no direction to move)")
                continue
            dx = (mine[0] - other[0]) / norm * distance
            dy = (mine[1] - other[1]) / norm * distance
            cands, blocker = self._candidates(
                net, pair.layer, mine, str(witness.get(f"item_{side}", ""))
            )
            if not cands:
                reasons.append(f"{net}: {blocker}")
                continue
            for cand in cands:
                label = f"{cand.kind}:{self._node_uuid(cand.node) or '<no uuid>'}"
                verdict = self._trial(
                    cand, dx, dy, pair, current, census_fn, drc_fn, drc_count, witness, result
                )
                if isinstance(verdict, str):
                    result.rejected.append(RejectedMove(pair.net_a, pair.net_b, label, verdict))
                    reasons.append(f"{label}: {verdict}")
                    continue
                return verdict
        give_up("; ".join(reasons) if reasons else "no movable copper at the witness")
        return None

    def _trial(
        self,
        cand: _Copper,
        dx: float,
        dy: float,
        pair: CreepagePair,
        current: CreepageReport,
        census_fn: CensusFn,
        drc_fn: DrcFn | None,
        drc_count: int | None,
        witness: dict[str, Any],
        result: CreepageRepairResult,
    ) -> tuple[CreepageReport, int | None] | str:
        """Apply, verify, and keep or roll back one translation.

        The DRC gate is **count-only**: a move is rejected when the error count
        rises, so a move that fixes one violation while creating a different one
        (same count) still passes.  Re-run ``kct check`` on the saved board.
        """
        self._journal = []
        try:
            self._apply_nudge(cand.node, cand.kind, dx, dy, result=None, drag_connected=True)
        finally:
            journal, self._journal = self._journal, None
        if not journal:
            return "infeasible: both track ends are anchored (via or locked copper)"
        locked = next((n for n, _p, _x, _y in journal if _is_locked(n)), None)
        if locked is not None:
            self._rollback(journal)
            return (
                f"the move would drag locked {locked.name} {self._node_uuid(locked) or ''}".strip()
            )
        lost = self._lost_contact(journal)
        if lost is not None:
            self._rollback(journal)
            return f"the move would {lost}"
        # The census only measures HV-vs-other pairs, so a move that shorts or
        # under-clears two non-HV nets is invisible to it; reuse the
        # clearance repairer's foreign-copper check.
        collateral = self._collateral_damage(journal)
        if collateral is not None:
            self._rollback(journal)
            return f"the move would {collateral}"

        new = census_fn(self._pcb())
        old_pairs, new_pairs = _index_pairs(current), _index_pairs(new)
        key = _pair_key(pair.net_a, pair.net_b, pair.kind)
        target = new_pairs.get(key)
        if target is None:
            # A pair that vanished from the census cannot be shown to have
            # cleared (e.g. the move changed which nets are HV-adjacent), so it
            # is "not verified", never "cleared with margin 0".
            self._rollback(journal)
            return (
                f"not verified: {pair.net_a} vs {pair.net_b} is absent from the "
                "post-move census, so the move could not be shown to clear it"
            )
        if not (target.passed or target.waived):
            self._rollback(journal)
            island = ""
            via = getattr(target, "via_floating", None) or getattr(pair, "via_floating", None)
            if via:
                island = (
                    f"; the binding path runs through floating island {via} "
                    "(the witness marks the direct gap, not the island path)"
                )
            return (
                f"not cleared: {pair.net_a} vs {pair.net_b} still "
                f"{target.creepage_mm:.4f} mm creepage / {target.clearance_mm:.4f} mm clearance "
                f"(margin {_headroom(target):+.4f} mm){island}"
            )
        for okey, before in old_pairs.items():
            after = new_pairs.get(okey)
            if okey == key or after is None or after.waived or before.waived:
                continue
            if (before.passed and not after.passed) or (
                not before.passed and _headroom(after) < _headroom(before) - _EPS
            ):
                self._rollback(journal)
                return (
                    f"would worsen {after.net_a} vs {after.net_b} "
                    f"(margin {_headroom(before):+.4f} -> {_headroom(after):+.4f} mm)"
                )
        for nkey, after in new_pairs.items():
            if nkey == key or nkey in old_pairs or after.waived or after.passed:
                continue
            self._rollback(journal)
            return (
                f"would create a failing pair {after.net_a} vs {after.net_b} "
                f"(margin {_headroom(after):+.4f} mm)"
            )

        new_drc = drc_count
        if drc_fn is not None and drc_count is not None:
            new_drc = drc_fn(serialize_sexp(self.doc, preserve_source=True))
            if new_drc is None:
                # Never let the DRC gate switch itself off mid-run unnoticed.
                self._rollback(journal)
                return "DRC could not be run after the move; the move was not verified"
            if new_drc > drc_count:
                self._rollback(journal)
                return f"DRC regression: {drc_count} -> {new_drc} errors after the move"

        self.modified = True
        dragged = len({id(p) for _n, p, _x, _y in journal}) - (2 if cand.kind == "segment" else 1)
        result.moves.append(
            CreepageMove(
                net_a=pair.net_a,
                net_b=pair.net_b,
                kind=pair.kind,
                layer=pair.layer,
                item_type=cand.kind,
                item_net=cand.net,
                uuid=self._node_uuid(cand.node),
                dx_mm=dx,
                dy_mm=dy,
                witness=witness,
                old_creepage_mm=pair.creepage_mm,
                new_creepage_mm=target.creepage_mm,
                old_margin_mm=pair.margin_mm,
                new_margin_mm=target.margin_mm,
                dragged=max(dragged, 0),
            )
        )
        return new, new_drc


def load_report_rows(path: str | Path) -> list[tuple[str, str, str]]:
    """Failing rows of a ``kct creepage --format json`` file."""
    import json

    return failing_rows_from_report_dict(json.loads(Path(path).read_text()))
