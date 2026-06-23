"""Congestion/escape-driven placement nudge (Milestone 3 of #3862, issue #3865).

This module closes the place-and-route loop for the failure mode that *no*
routing change can fix: ``PLACEMENT_BOUND`` (and, opportunistically,
``ESCAPE_BLOCKED``) nets identified by the M1 stuck-net classifier
(:mod:`kicad_tools.router.stuck_classifier`).  On the chorus stress fixture
these are the U5 codec/QFN analog cluster (AUDIO_R, U5-VCOM/DEMP, SDA, SCL,
DAC_FLT, I2S_DIN/LRCLK, LED3-A/LED4-A) and the no-rippable-copper control nets
(LATCH, SPI_MISO/MOSI/SCK, UART_TX, LED4-K).  They have no routing slack at the
current placement, so the only remedy is to *move a part*.

This MOVES PARTS on a real board -- the riskiest milestone -- so it is gated by
the same discipline M2 (``KCT_JOINT_REGION_RESOLVE``) uses:

* **Flag-gated, OFF by default.**  The chorus runner exposes
  ``--placement-nudge``; nothing on the default routing path calls this module,
  so default behaviour is byte-identical to main.

* **Net-positive rollback guard.**  The nudged placement + re-route is accepted
  ONLY if the total strict (fully-connected) signal-net count STRICTLY
  INCREASES *and* the blocking-DRC error count does not worsen.  Otherwise the
  board file is restored byte-for-byte from a pre-nudge snapshot.  A regression
  is impossible by construction.

* **Bounded nudge magnitude.**  Each component moves at most ``max_nudge_mm``
  (default 1.5 mm -- tight, per the EE "analog care" feedback near U5).

* **Respects locked parts, board outline, and mechanical connectors.**  Locked
  footprints, any ref in ``fixed_refs`` (default: the chorus HAT header J2 and
  J4), and any nudge that would push a footprint centre outside the Edge.Cuts
  outline (the #3804 board-outline fix) are skipped.

The stage operates on a routed ``.kicad_pcb`` file in place, mirroring
:func:`kicad_tools.router.partial_rescue.complete_unfinished_nets` so the chorus
runner can chain it after the completion passes.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.optim.board_outline import extract_board_outline
from kicad_tools.optim.geometry import Vector2D
from kicad_tools.router.partial_rescue import (
    RescueConfig,
    build_rescue_command,
    partially_connected_signal_nets,
    strip_net_copper,
)
from kicad_tools.router.stuck_classifier import (
    StuckClass,
    classify_stuck_nets_from_pcb,
)

if TYPE_CHECKING:
    from kicad_tools.optim.geometry import Polygon
    from kicad_tools.schema.pcb import PCB


__all__ = [
    "NudgeConfig",
    "PlacementNudge",
    "PlacementNudgeResult",
    "nudge_placement_bound_nets",
]


# --- tunable defaults -------------------------------------------------------

#: Hard cap on per-component displacement, in mm.  Kept tight (analog care, EE
#: feedback on chorus near U5): a nudge is a *relief* move, not a re-place.
DEFAULT_MAX_NUDGE_MM = 1.5

#: A footprint centre must stay at least this far inside the board outline
#: after a nudge (#3804: never push a part off-board / onto the edge cut).
DEFAULT_OUTLINE_MARGIN_MM = 0.5

#: Default refs that must NOT move -- mechanical interfaces on the chorus
#: fixture (J2 = 40-pin HAT header, J4).  Callers can override.
DEFAULT_FIXED_REFS = frozenset({"J2", "J4"})

#: Maximum number of distinct components nudged in a single stage.  Bounds the
#: blast radius of one accept/reject decision (the whole-board net-positive
#: guard still protects against multi-part regressions, but a small set keeps
#: the re-route fast and the diff reviewable).
DEFAULT_MAX_COMPONENTS = 4


@dataclass
class NudgeConfig:
    """Knobs for one placement-nudge stage."""

    rescue: RescueConfig = field(default_factory=RescueConfig)
    #: Hard cap on per-component displacement (mm).
    max_nudge_mm: float = DEFAULT_MAX_NUDGE_MM
    #: Minimum inside-outline margin for a nudged footprint centre (mm).
    outline_margin_mm: float = DEFAULT_OUTLINE_MARGIN_MM
    #: Refs that must never move (mechanical connectors etc.).
    fixed_refs: frozenset[str] = DEFAULT_FIXED_REFS
    #: Maximum number of components to nudge in one stage.
    max_components: int = DEFAULT_MAX_COMPONENTS
    #: Also act on ESCAPE_BLOCKED nets, not just PLACEMENT_BOUND.  Off by
    #: default -- escape failures are M4's domain; a nudge rarely opens a
    #: fine-pitch escape and the extra moves dilute the net-positive guard.
    include_escape_blocked: bool = False
    #: Wall budget for the re-route subprocess that validates the nudge.
    reroute_timeout_s: int = 600


@dataclass
class ComponentNudge:
    """A single proposed (and possibly applied) component move."""

    ref: str
    old_xy: tuple[float, float]
    new_xy: tuple[float, float]
    target_net: str

    @property
    def distance_mm(self) -> float:
        return math.hypot(self.new_xy[0] - self.old_xy[0], self.new_xy[1] - self.old_xy[1])

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "old_xy": [self.old_xy[0], self.old_xy[1]],
            "new_xy": [self.new_xy[0], self.new_xy[1]],
            "distance_mm": round(self.distance_mm, 4),
            "target_net": self.target_net,
        }


@dataclass
class PlacementNudgeResult:
    """Outcome of one placement-nudge stage."""

    accepted: bool
    strict_before: int
    strict_after: int
    drc_before: int
    drc_after: int
    nudges: list[ComponentNudge] = field(default_factory=list)
    reason: str = ""

    @property
    def strict_delta(self) -> int:
        return self.strict_after - self.strict_before

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "strict_before": self.strict_before,
            "strict_after": self.strict_after,
            "strict_delta": self.strict_delta,
            "drc_before": self.drc_before,
            "drc_after": self.drc_after,
            "reason": self.reason,
            "nudges": [n.to_dict() for n in self.nudges],
        }

    def summary(self) -> str:
        verdict = "ACCEPTED" if self.accepted else "rolled back"
        lines = [
            f"Placement nudge: {verdict} ({self.reason})",
            f"  strict: {self.strict_before} -> {self.strict_after} ({self.strict_delta:+d})",
            f"  blocking DRC: {self.drc_before} -> {self.drc_after}",
            f"  components nudged: {len(self.nudges)}",
        ]
        for n in self.nudges:
            lines.append(
                f"    {n.ref}: {n.old_xy} -> {n.new_xy} ({n.distance_mm:.2f}mm, for {n.target_net})"
            )
        return "\n".join(lines)


def _count_strict_signal_nets(pcb: PCB, excluded_nets: frozenset[str]) -> int:
    """Count fully-connected multi-pad signal nets (the strict-reach metric)."""
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analysis = NetStatusAnalyzer(pcb).analyze()
    return sum(
        1
        for n in analysis.nets
        if n.net_name not in excluded_nets
        and n.net_type == "signal"
        and n.total_pads >= 2
        and n.status == "complete"
    )


def _count_blocking_drc(pcb_path: Path, manufacturer: str) -> int:
    """Count non-connectivity error-severity DRC violations on the board."""
    import json

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(pcb_path),
            "--mfr",
            manufacturer,
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout)
    except (ValueError, KeyError):
        # A parse failure must not silently read as "0 DRC"; treat it as
        # worse-than-everything so the net-positive guard rejects rather
        # than accepts on missing measurement.
        return 1 << 30
    drc = 0
    for v in data.get("violations", data.get("errors", [])):
        rule = v.get("rule_id") or v.get("rule") or v.get("type")
        if rule == "connectivity":
            continue
        if v.get("severity") == "error":
            drc += 1
    return drc


class PlacementNudge:
    """Computes and applies bounded, outline-aware placement nudges.

    The class is split out from the file-level orchestration
    (:func:`nudge_placement_bound_nets`) so the nudge-vector geometry can be
    unit-tested on a synthetic in-memory board without re-routing.
    """

    def __init__(self, pcb: PCB, config: NudgeConfig | None = None):
        self.pcb = pcb
        self.config = config or NudgeConfig()
        # Frame reconciliation (the #3804/#3861 load-bearing fix):
        # ``extract_board_outline`` returns the Edge.Cuts polygon in raw
        # page/sexp coordinates, but ``Footprint.position`` and the
        # classifier's pad positions live in the BOARD frame (board origin
        # already subtracted).  Mixing the two would test containment of a
        # board-frame point against a page-frame outline and reject every
        # interior nudge (measured on chorus: outline bbox x=[118,181] vs a
        # genuine interior part at x=18).  Translate the outline by
        # ``-board_origin`` so it shares the frame the parts live in.
        raw_outline = extract_board_outline(pcb)
        self._outline: Polygon | None = raw_outline
        if raw_outline is not None:
            ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))
            if ox or oy:
                self._outline = raw_outline.translate(Vector2D(-ox, -oy))

    def _find_footprint(self, ref: str):
        for fp in getattr(self.pcb, "footprints", []):
            if getattr(fp, "reference", None) == ref:
                return fp
        return None

    def _is_fixed(self, ref: str) -> bool:
        if ref in self.config.fixed_refs:
            return True
        fp = self._find_footprint(ref)
        return bool(fp is not None and getattr(fp, "locked", False))

    def _within_outline(self, x: float, y: float) -> bool:
        """True if (x, y) sits inside the board outline with margin (#3804).

        When no Edge.Cuts outline can be parsed we conservatively return
        True only for the unmoved point; callers should not nudge a board
        with no outline, but the higher-level guard treats "no outline" as
        "cannot certify safe" and skips the move (see :meth:`propose`).
        """
        if self._outline is None:
            return False
        if not self._outline.contains_point(Vector2D(x, y)):
            return False
        margin = self.config.outline_margin_mm
        if margin <= 0.0:
            return True
        # Require the point to be at least ``margin`` mm inside the outline:
        # the nearest boundary point must be farther than the margin.
        nearest = self._outline.nearest_point_on_boundary(Vector2D(x, y))
        return math.hypot(x - nearest.x, y - nearest.y) >= margin

    def _net_pad_geometry(
        self, net_name: str
    ) -> tuple[list[tuple[str, tuple[float, float]]], tuple[float, float] | None]:
        """Return ((ref, pad_xy) for unconnected pads, connected-island centroid).

        The connected-island centroid is the mean of the net's *connected*
        pad positions -- the direction a stranded pad wants to move toward to
        shorten the unroutable gap.  ``None`` when the net has no connected
        pads (a fully-unrouted net: no island to aim at).
        """
        from kicad_tools.analysis.net_status import NetStatusAnalyzer

        analysis = NetStatusAnalyzer(self.pcb).analyze()
        status = analysis.get_net(net_name)
        if status is None:
            return [], None
        unconnected = [(p.reference, p.position) for p in status.unconnected_pads]
        if status.connected_pads:
            cx = sum(p.position[0] for p in status.connected_pads) / len(status.connected_pads)
            cy = sum(p.position[1] for p in status.connected_pads) / len(status.connected_pads)
            island = (cx, cy)
        else:
            island = None
        return unconnected, island

    def propose(self) -> list[ComponentNudge]:
        """Propose bounded, outline-aware nudges for placement-bound nets.

        Returns at most ``config.max_components`` distinct moves.  Each move:

        * targets a footprint owning an unconnected pad of a PLACEMENT_BOUND
          (or, if enabled, ESCAPE_BLOCKED) net;
        * is directed toward the net's connected-island centroid (closing the
          unroutable gap);
        * is capped at ``config.max_nudge_mm``;
        * keeps the footprint centre inside the board outline (#3804);
        * never touches a fixed/locked ref.

        No board mutation happens here -- :func:`nudge_placement_bound_nets`
        applies the returned moves, re-routes, and gates on the net-positive
        guard.
        """
        result = classify_stuck_nets_from_pcb(self.pcb)
        targets = {StuckClass.PLACEMENT_BOUND}
        if self.config.include_escape_blocked:
            targets.add(StuckClass.ESCAPE_BLOCKED)

        nudges: list[ComponentNudge] = []
        seen_refs: set[str] = set()

        # Process nets densest-first: the most congested placement-bound net is
        # the one most likely to yield strict gain when its part moves.
        diagnoses = sorted(
            (d for d in result.diagnoses if d.classification in targets),
            key=lambda d: -d.local_congestion,
        )

        for diag in diagnoses:
            if len(nudges) >= self.config.max_components:
                break
            unconnected, island = self._net_pad_geometry(diag.net_name)
            if island is None or not unconnected:
                continue
            for ref, pad_xy in unconnected:
                if len(nudges) >= self.config.max_components:
                    break
                if ref in seen_refs or self._is_fixed(ref):
                    continue
                fp = self._find_footprint(ref)
                if fp is None:
                    continue
                # Direction: from the stranded pad toward the connected
                # island, applied to the footprint centre.
                dx = island[0] - pad_xy[0]
                dy = island[1] - pad_xy[1]
                dist = math.hypot(dx, dy)
                if dist < 1e-6:
                    continue
                step = min(self.config.max_nudge_mm, dist)
                ux, uy = dx / dist, dy / dist
                old_x, old_y = fp.position[0], fp.position[1]
                new_x = old_x + ux * step
                new_y = old_y + uy * step
                if not self._within_outline(new_x, new_y):
                    # Try the largest in-outline step down to a small floor.
                    placed = False
                    s = step
                    while s > 0.2:
                        s *= 0.5
                        cx, cy = old_x + ux * s, old_y + uy * s
                        if self._within_outline(cx, cy):
                            new_x, new_y = cx, cy
                            placed = True
                            break
                    if not placed:
                        continue
                nudges.append(
                    ComponentNudge(
                        ref=ref,
                        old_xy=(old_x, old_y),
                        new_xy=(new_x, new_y),
                        target_net=diag.net_name,
                    )
                )
                seen_refs.add(ref)
        return nudges

    def apply(self, nudges: list[ComponentNudge]) -> None:
        """Apply proposed nudges to the in-memory PCB (mutates footprints)."""
        for n in nudges:
            fp = self._find_footprint(n.ref)
            if fp is not None:
                fp.position = (n.new_xy[0], n.new_xy[1])


def nudge_placement_bound_nets(
    routed_path: Path,
    config: NudgeConfig | None = None,
    *,
    quiet: bool = False,
) -> PlacementNudgeResult:
    """Nudge placement-bound parts and re-route, under a net-positive guard.

    Operates on the routed board at *routed_path* in place.  The full
    transaction:

    1. Measure the strict signal-net count and blocking-DRC count *before*.
    2. Classify stuck nets (M1) and propose bounded, outline-aware nudges for
       the placement-bound (and optionally escape-blocked) nets.
    3. Snapshot the board file bytes.  Apply the nudges, strip the target
       nets' stranded copper, and re-route the unfinished cohort together
       (the ``--preserve-existing`` rescue command, same recipe knobs).
    4. Measure strict + DRC *after*.  Accept ONLY if strict strictly
       increased AND DRC did not worsen; otherwise restore the byte-identical
       snapshot.

    Returns a :class:`PlacementNudgeResult` either way.  When no nudge is
    proposed (e.g. all boards' nets already strict, the no-op case the M3
    validation contract requires) the function is a no-op: zero parts move and
    the board file is untouched.
    """
    from kicad_tools.schema.pcb import PCB

    cfg = config or NudgeConfig()
    excluded = cfg.rescue.excluded_nets

    def _log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    pcb = PCB.load(str(routed_path))
    strict_before = _count_strict_signal_nets(pcb, excluded)
    drc_before = _count_blocking_drc(routed_path, cfg.rescue.manufacturer)

    nudger = PlacementNudge(pcb, cfg)
    nudges = nudger.propose()

    if not nudges:
        _log("Placement nudge: no placement-bound nets with a safe nudge; no-op.")
        return PlacementNudgeResult(
            accepted=False,
            strict_before=strict_before,
            strict_after=strict_before,
            drc_before=drc_before,
            drc_after=drc_before,
            nudges=[],
            reason="no_candidate",
        )

    _log(f"Placement nudge: proposing {len(nudges)} move(s):")
    for n in nudges:
        _log(f"    {n.ref}: {n.old_xy} -> {n.new_xy} ({n.distance_mm:.2f}mm, {n.target_net})")

    # Snapshot the board file BEFORE any mutation so rollback is byte-exact.
    snapshot = routed_path.with_suffix(routed_path.suffix + ".nudge_bak")
    shutil.copy2(routed_path, snapshot)

    try:
        # Apply nudges to the in-memory board and persist.
        nudger.apply(nudges)
        pcb.save(str(routed_path))

        # Re-route the unfinished cohort against the new placement.  Strip the
        # stranded stubs of the unfinished nets first (the #3470 lesson) so the
        # re-route starts clean, then route them together with
        # --preserve-existing (the strict nets' copper is protected).
        unfinished = partially_connected_signal_nets(
            routed_path,
            manufacturer=cfg.rescue.manufacturer,
            excluded_nets=excluded,
            include_unrouted=True,
        )
        if unfinished:
            strip_net_copper(routed_path, unfinished)

        strict_complete = [
            n for n in _strict_net_names(routed_path, excluded) if n not in unfinished
        ]
        reroute_cfg = RescueConfig(
            manufacturer=cfg.rescue.manufacturer,
            backend=cfg.rescue.backend,
            seed=cfg.rescue.seed,
            stage_timeout_s=cfg.reroute_timeout_s,
            per_net_timeout_s=cfg.rescue.per_net_timeout_s,
            starting_layers=cfg.rescue.starting_layers,
            max_layers=cfg.rescue.max_layers,
            excluded_nets=excluded,
            micro_via_in_pad_fallback=cfg.rescue.micro_via_in_pad_fallback,
            extra_args=cfg.rescue.extra_args,
        )
        cmd = build_rescue_command(
            routed_path,
            routed_path,
            skip_nets=strict_complete,
            config=reroute_cfg,
        )
        _log("Placement nudge: re-routing nudged board...")
        subprocess.run(cmd, timeout=cfg.reroute_timeout_s + 60, check=False)

        # Measure after.
        pcb_after = PCB.load(str(routed_path))
        strict_after = _count_strict_signal_nets(pcb_after, excluded)
        drc_after = _count_blocking_drc(routed_path, cfg.rescue.manufacturer)

        net_positive = strict_after > strict_before and drc_after <= drc_before
        if net_positive:
            reason = "net_positive"
            _log(
                f"Placement nudge ACCEPTED: strict {strict_before} -> {strict_after} "
                f"(+{strict_after - strict_before}), DRC {drc_before} -> {drc_after}"
            )
            snapshot.unlink(missing_ok=True)
            return PlacementNudgeResult(
                accepted=True,
                strict_before=strict_before,
                strict_after=strict_after,
                drc_before=drc_before,
                drc_after=drc_after,
                nudges=nudges,
                reason=reason,
            )

        # Roll back byte-for-byte.
        reason = "drc_worsened" if drc_after > drc_before else "no_strict_gain"
        _log(
            f"Placement nudge rolled back ({reason}): strict {strict_before} -> "
            f"{strict_after}, DRC {drc_before} -> {drc_after}"
        )
        shutil.copy2(snapshot, routed_path)
        snapshot.unlink(missing_ok=True)
        return PlacementNudgeResult(
            accepted=False,
            strict_before=strict_before,
            strict_after=strict_after,
            drc_before=drc_before,
            drc_after=drc_after,
            nudges=nudges,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 -- any failure must roll back
        _log(f"Placement nudge errored ({exc!r}); rolling back.")
        if snapshot.exists():
            shutil.copy2(snapshot, routed_path)
            snapshot.unlink(missing_ok=True)
        return PlacementNudgeResult(
            accepted=False,
            strict_before=strict_before,
            strict_after=strict_before,
            drc_before=drc_before,
            drc_after=drc_before,
            nudges=nudges,
            reason=f"error:{type(exc).__name__}",
        )


def _strict_net_names(pcb_path: Path, excluded: frozenset[str]) -> list[str]:
    """Names of fully-connected multi-pad signal nets on the board."""
    from kicad_tools.analysis.net_status import NetStatusAnalyzer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    analysis = NetStatusAnalyzer(pcb).analyze()
    return [
        n.net_name
        for n in analysis.nets
        if n.net_name not in excluded
        and n.net_type == "signal"
        and n.total_pads >= 2
        and n.status == "complete"
    ]
