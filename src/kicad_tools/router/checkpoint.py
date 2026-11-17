"""Best-so-far routing checkpoints, resume metadata and pass rollback (Issue #5945).

Long ``kct route`` / ``kct route-auto`` runs are killed by session limits, CI
timeouts and supervisor deadlines.  This module gives both routing paths a
shared, engine-agnostic way to make them *anytime-safe*:

* :class:`RouteScore` -- the ordering used to decide whether a pass improved
  the board: nets complete (descending), then DRC/clearance count, then
  overflow (both ascending), then wirelength and via count (ascending).
* :class:`BestCheckpointWriter` -- tracks the best score seen so far and, each
  time a pass beats it, atomically writes the routed board to the checkpoint
  path plus a JSON sidecar (score, pass number, source board).  A pass that
  does not beat the best is never written, so a killed run always leaves the
  best result on disk, never a later regression.
* :func:`read_checkpoint_meta` / :func:`checkpoint_identity_mismatch` -- the
  ``--resume`` side: read the sidecar and refuse a checkpoint whose footprints
  or nets do not match the board being routed (an LVS-inconsistent seed).

Ideas only from fastroute (GPL-3.0) -- no code was copied.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: Bumped when the sidecar layout changes incompatibly.
CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RouteScore:
    """Comparable quality of one routed state.

    Lex order (best first): more ``nets_complete``, fewer ``drc_violations``,
    lower ``overflow``, more ``nets_routed`` (partial progress), shorter
    ``wirelength_mm``, fewer ``vias``.  ``wirelength_mm`` is compared at
    micrometre resolution so float noise never counts as an improvement.
    """

    nets_complete: int
    drc_violations: int = 0
    overflow: int = 0
    nets_routed: int = 0
    wirelength_mm: float = 0.0
    vias: int = 0

    @property
    def sort_key(self) -> tuple[int, int, int, int, int, int]:
        """Smallest tuple is the best state."""
        return (
            -self.nets_complete,
            self.drc_violations,
            self.overflow,
            -self.nets_routed,
            round(self.wirelength_mm * 1000),
            self.vias,
        )

    def is_better_than(self, other: RouteScore | None) -> bool:
        """Strictly better than *other* (``None`` = nothing yet, always beaten)."""
        return other is None or self.sort_key < other.sort_key

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["wirelength_mm"] = round(self.wirelength_mm, 4)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteScore:
        return cls(
            nets_complete=int(data.get("nets_complete", 0)),
            drc_violations=int(data.get("drc_violations", 0)),
            overflow=int(data.get("overflow", 0)),
            nets_routed=int(data.get("nets_routed", 0)),
            wirelength_mm=float(data.get("wirelength_mm", 0.0)),
            vias=int(data.get("vias", 0)),
        )

    @classmethod
    def from_routes(
        cls,
        routes: Iterable[Any],
        metrics: Any = None,
        *,
        complete_offset: int = 0,
        baseline: RouteScore | None = None,
    ) -> RouteScore:
        """Score an in-memory route snapshot from the negotiated router.

        *metrics* is an :class:`~kicad_tools.router.core.IterationMetrics`
        (or ``None``).  Its survivable connectivity (``effective_connected``)
        is the primary key.  *complete_offset* adds nets the router never saw
        because they were already complete and held out of the routable set
        (``--resume`` / ``--preserve-existing``), so resumed scores stay
        comparable with the checkpoint they started from.

        *baseline* is the score of copper that is on the board but not in
        *routes* (the ``--resume`` checkpoint's preserved copper, re-emitted
        verbatim by every write).  Its wirelength and vias are added so a
        resumed pass is compared like-for-like with a seed scored by
        :meth:`from_board`, which counts all copper on the board (the routed
        count becomes a lower bound: the larger of the pass's and the
        baseline's).
        """
        routes = list(routes)
        wirelength = 0.0
        vias = 0
        nets: set[int] = set()
        for route in routes:
            nets.add(getattr(route, "net", id(route)))
            for seg in getattr(route, "segments", ()) or ():
                wirelength += math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
            vias += len(getattr(route, "vias", ()) or ())
        connected = 0
        violations = 0
        overflow = 0
        routed = len(nets)
        if metrics is not None:
            connected = int(
                getattr(metrics, "effective_connected", getattr(metrics, "nets_fully_connected", 0))
                or 0
            )
            violations = int(getattr(metrics, "clearance_violations", 0) or 0)
            overflow = int(getattr(metrics, "overflow", 0) or 0)
            routed = int(getattr(metrics, "routed_count", routed) or routed)
        if baseline is not None:
            wirelength += baseline.wirelength_mm
            vias += baseline.vias
            # Every net with preserved copper is still routed on the board.
            # Which of this pass's nets already had copper is unknown here, so
            # take the lower bound: a pass that adds nothing ties the seed.
            routed = max(routed, baseline.nets_routed)
        else:
            routed += complete_offset
        return cls(
            nets_complete=connected + complete_offset,
            drc_violations=violations,
            overflow=overflow,
            nets_routed=routed,
            wirelength_mm=wirelength,
            vias=vias,
        )

    @classmethod
    def from_board(cls, pcb_path: Path | str) -> RouteScore:
        """Score a routed ``.kicad_pcb`` on disk (used by ``route-auto``).

        Nets complete come from the same connectivity model ``kct check`` and
        ``--preserve-existing`` use; wirelength and vias from the board's
        ``(segment ...)`` / ``(via ...)`` copper.  DRC is not run here (too
        slow per pass), so ``drc_violations`` is 0 for board scores.
        """
        from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias
        from kicad_tools.router.preserve_existing import fully_connected_nets

        path = Path(pcb_path)
        text = path.read_text(encoding="utf-8")
        segments = parse_segments(text)
        vias = parse_vias(text)
        wirelength = sum(
            math.hypot(s.x2 - s.x1, s.y2 - s.y1) for segs in segments.values() for s in segs
        )
        try:
            complete = len(fully_connected_nets(path))
        except Exception:  # noqa: BLE001 - an unscorable board scores as empty
            complete = 0
        return cls(
            nets_complete=complete,
            nets_routed=len(
                {n for n, s in segments.items() if s} | {n for n, v in vias.items() if v}
            ),
            wirelength_mm=wirelength,
            vias=sum(len(v) for v in vias.values()),
        )

    def describe(self) -> str:
        return (
            f"complete={self.nets_complete}, drc={self.drc_violations}, "
            f"overflow={self.overflow}, routed={self.nets_routed}, "
            f"length={self.wirelength_mm:.2f}mm, vias={self.vias}"
        )


def sidecar_path(checkpoint: Path | str) -> Path:
    """``board.kicad_pcb`` -> ``board.checkpoint.json``."""
    return Path(checkpoint).with_suffix(".checkpoint.json")


def read_checkpoint_meta(checkpoint: Path | str) -> dict[str, Any] | None:
    """Return the checkpoint sidecar as a dict, or ``None`` if absent/corrupt."""
    path = sidecar_path(checkpoint)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_REF_RE = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')
_LEGACY_REF_RE = re.compile(r"\(fp_text\s+reference\s+\"?([^\s\")]+)")


def board_identity(pcb_text: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(footprint references, net names)`` -- what LVS consistency rests on."""
    from kicad_tools.router.optimizer.pcb import parse_net_names

    refs = set(_REF_RE.findall(pcb_text)) | set(_LEGACY_REF_RE.findall(pcb_text))
    nets = {name for name in parse_net_names(pcb_text).values() if name}
    return frozenset(refs), frozenset(nets)


def checkpoint_identity_mismatch(source: Path | str, checkpoint: Path | str) -> str | None:
    """Explain why *checkpoint* cannot seed a route of *source*, or ``None``.

    A checkpoint is a routed copy of the source board, so its footprint
    references and net names must match exactly; anything else would seed
    the run with copper for a different design.
    """
    src_refs, src_nets = board_identity(Path(source).read_text(encoding="utf-8"))
    ck_refs, ck_nets = board_identity(Path(checkpoint).read_text(encoding="utf-8"))
    problems: list[str] = []
    for label, a, b in (("footprint", src_refs, ck_refs), ("net", src_nets, ck_nets)):
        missing = sorted(a - b)
        extra = sorted(b - a)
        if missing:
            problems.append(
                f"{len(missing)} {label}(s) missing from checkpoint: {', '.join(missing[:5])}"
            )
        if extra:
            problems.append(
                f"{len(extra)} unexpected {label}(s) in checkpoint: {', '.join(extra[:5])}"
            )
    return "; ".join(problems) or None


class BestCheckpointWriter:
    """Write the board to *path* every time a pass beats the best so far.

    ``offer(score, pass_index, write_fn)`` is the whole protocol: when *score*
    strictly beats the best seen, ``write_fn(path)`` must atomically write
    the routed board to ``path``; the sidecar is then written atomically
    after it, so a sidecar always describes a board that is already on disk.
    A non-improving offer writes nothing.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        command: str,
        source_pcb: Path | str | None = None,
        quiet: bool = False,
        printer: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.command = command
        self.source_pcb = str(source_pcb) if source_pcb is not None else None
        self.quiet = quiet
        self._print = printer or print
        self.best: RouteScore | None = None
        self.best_pass: int | None = None
        self.best_label: str | None = None
        self.writes = 0
        #: Added to ``nets_complete`` by :meth:`RouteScore.from_routes` callers
        #: for nets held out of the routable set (``--resume``).
        self.complete_offset = 0
        #: Score of preserved copper absent from route snapshots (``--resume``);
        #: passed as ``baseline`` to :meth:`RouteScore.from_routes`.
        self.baseline: RouteScore | None = None

    def seed(self, score: RouteScore, pass_index: int | None, label: str | None = None) -> None:
        """Treat *score* as already on disk (resuming onto the same path)."""
        self.best = score
        self.best_pass = pass_index
        self.best_label = label

    def offer(
        self,
        score: RouteScore,
        pass_index: int,
        write_fn: Callable[[Path], None],
        *,
        label: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        if not score.is_better_than(self.best):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_fn(self.path)
        meta: dict[str, Any] = {
            "schema": CHECKPOINT_SCHEMA_VERSION,
            "command": self.command,
            "source_pcb": self.source_pcb,
            "checkpoint": str(self.path),
            "pass": pass_index,
            "pass_label": label,
            "score": score.to_dict(),
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            # Mid-run copper has not been through the terminal DRC/repair
            # passes; it is the best *search* state, not a verified board.
            "verified": False,
        }
        if extra:
            meta.update(extra)
        from kicad_tools.core.atomic_write import atomic_write_text

        atomic_write_text(
            sidecar_path(self.path), json.dumps(meta, indent=2, sort_keys=True) + "\n"
        )
        self.best = score
        self.best_pass = pass_index
        self.best_label = label
        self.writes += 1
        if not self.quiet:
            what = label or f"pass {pass_index}"
            self._print(f"  checkpoint: new best at {what} ({score.describe()}) -> {self.path}")
        return True
