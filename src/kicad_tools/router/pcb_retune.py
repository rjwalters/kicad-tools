"""Length-retune selected groups/pairs on an already-routed PCB (Issue #6885).

Applies the existing match-group (:func:`tune_match_group_v2`) and diff-pair
skew (:func:`tune_diff_pair_skew`) tuners to *named* groups of a board that is
already routed, preserving every reviewed or hand-made repair outside the
selected nets.  It is an adapter, not a router: the board is loaded with
``load_pcb_for_routing(load_existing_routes=True)``, which parks all existing
copper in ``router.existing_routes`` (fixed obstacles) where the tuners cannot
see it.  :func:`_adopt_existing_routes` moves that copper into
``router.routes`` -- selected nets editable, every other net flagged
``is_escape`` so the tuners treat it as fixed copper that still counts for
foreign-clearance checks.

Guarantees (each one is enforced in code and covered by tests):

* The input file is only ever read.  The output is a distinct path, written
  atomically (temp file in the output directory, then rename) and only after
  every check below passed.  Any failure leaves no output file.
* Only ``(segment ...)``/``(via ...)`` blocks of the selected nets are
  spliced; every other byte of the file is carried over.  Unchanged selected
  blocks keep their original text (uuid, ``locked`` ...).  The written text
  is verified to equal the input once the selected nets' copper is removed
  from both.
* "Achieved" is measured, not assumed: before/after skew is re-derived from
  the *written* board by the checker's own functions
  (``derive_group_skew_data`` / ``derive_skew_limits``), against the limit
  ``kct check`` enforces.

Honest limits: tuners edit straight ``(segment ...)`` copper.  A selected net
carrying ``(arc ...)`` copper, or a board without a net-class map, copper
layers or a thickness, is refused rather than guessed at.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import signal
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Fixed seed for the deterministic-uuid mode used while serialising new copper.
UUID_SEED = 6885

#: Default wall-clock budget for the tuning step (seconds).
DEFAULT_TIMEOUT_S = 300.0

_EPS = 1e-9


class RetuneError(Exception):
    """A retune that must not produce an output file.

    ``code`` is a stable machine-readable token (``unknown_group``,
    ``ambiguous_group``, ``unattainable``, ``timeout``, ...).
    """

    def __init__(
        self, code: str, message: str, *, targets: list[TargetReport] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: ``unattainable`` only: every selected target, measured on the
        #: board the run would have written (Issue #7815).
        self.targets: list[TargetReport] = list(targets or [])

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class TargetReport:
    """One selected match group or diff pair, measured before and after."""

    kind: str  # "match_group" | "diffpair"
    name: str
    nets: list[str]
    tolerance_mm: float
    tolerance_source: str
    before_skew_mm: float | None
    after_skew_mm: float | None
    achieved: bool
    status: str  # "already_satisfied" | "tuned" | "unattainable"
    tuner_reason: str = ""
    #: Issue #7815: the members that kept the target over its limit, with the
    #: tuner's own reason for each (empty when the target was achieved).
    tuner_detail: str = ""
    #: Diff pairs only: the separate, tighter stretch target (not the gate).
    stretch_target_mm: float | None = None
    stretch_achieved: bool | None = None
    #: Diff pairs only (Issue #7893): the derived budget the pair is judged
    #: against in time, and its skew in ps before / after (``None`` when the
    #: pair has no budget; ``tolerance_mm`` is then its error cap).
    budget_ps: float | None = None
    before_skew_ps: float | None = None
    after_skew_ps: float | None = None


@dataclass
class RetuneReport:
    """Result of a successful :func:`retune_pcb` (failures raise)."""

    input_path: str
    output_path: str
    provenance_path: str
    input_sha256: str
    output_sha256: str
    tool_version: str
    #: "tuned" | "already_satisfied" | "partial" (``keep_achieved`` only:
    #: some targets are written, the ``unattainable`` ones are left as routed).
    status: str
    targets: list[TargetReport] = field(default_factory=list)
    changed_nets: list[str] = field(default_factory=list)
    net_lengths_mm: dict[str, dict[str, float]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tool_version() -> str:
    from kicad_tools import __version__

    return str(__version__)


@contextlib.contextmanager
def _deadline(timeout_s: float) -> Iterator[None]:
    """Raise :class:`RetuneError` ``timeout`` if the body outlives ``timeout_s``.

    Uses ``SIGALRM`` where it can interrupt the tuner (POSIX main thread);
    elsewhere the elapsed time is checked after the body returns, so an
    overrun still fails -- it just cannot be cut short.
    """
    started = time.monotonic()
    use_alarm = (
        hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    )
    previous = None

    def _on_alarm(_signum: int, _frame: Any) -> None:
        raise RetuneError("timeout", f"tuning exceeded the {timeout_s:g}s budget")

    if use_alarm:
        previous = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        yield
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    if time.monotonic() - started > timeout_s:
        raise RetuneError("timeout", f"tuning exceeded the {timeout_s:g}s budget")


# ---------------------------------------------------------------------------
# Selection and constraint resolution
# ---------------------------------------------------------------------------


@dataclass
class _Selection:
    groups: list[Any]  # MatchGroup
    pairs: list[Any]  # DetectedPair
    net_names: set[str]
    tolerance: dict[str, tuple[float, str]]  # target key -> (mm, source)
    #: Issue #7893: each selected diff pair's skew target (``_retune_once``).
    pair_targets: dict[str, Any] = field(default_factory=dict)


def _target_key(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def _group_tolerance_source(group: Any, net_names: dict[int, str], net_class_map: dict) -> str:
    first = (
        group.net_ids[0] if group.net_ids else (group.pair_ids[0][0] if group.pair_ids else None)
    )
    name = net_names.get(first) if first is not None else None
    nc = net_class_map.get(name) if name else None
    if nc is not None and getattr(nc, "length_match_tolerance_mm", None) is not None:
        return f"class:{nc.name}.length_match_tolerance_mm"
    return "default:match_group"


def _resolve_selection(
    groups: Sequence[str],
    diffpairs: Sequence[tuple[str, str]],
    net_names: dict[int, str],
    net_class_map: dict,
    length_tracker: Any,
) -> _Selection:
    """Resolve names to detected groups/pairs, refusing anything not exact."""
    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs
    from kicad_tools.router.match_group_detection import detect_match_groups
    from kicad_tools.validate.match_group_skew import resolve_group_tolerance_mm

    if not groups and not diffpairs:
        raise RetuneError(
            "no_selection",
            "select at least one match group (--group) or diff pair (--diffpair); "
            "there is no whole-board fallback",
        )

    net_to_class: dict[str, str] = {}
    synth: dict = dict(net_class_map)
    for net_name, nc in net_class_map.items():
        net_to_class[net_name] = nc.name
        synth.setdefault(nc.name, nc)

    detected_groups = detect_match_groups(
        net_names=net_names,
        net_class_routing=synth,
        net_to_class=net_to_class,
        length_tracker=length_tracker,
        enable_suffix_inference=False,
    )
    class_routing, class_of = build_synth_routing(net_class_map)
    detected_pairs = detect_diff_pairs(
        net_names=net_names,
        net_class_routing=class_routing or None,
        net_to_class=class_of or None,
    )

    chosen_groups: list[Any] = []
    for name in groups:
        matches = [g for g in detected_groups if g.name == name]
        if not matches:
            known = ", ".join(sorted(g.name for g in detected_groups)) or "none"
            raise RetuneError(
                "unknown_group", f"match group {name!r} is not declared (declared: {known})"
            )
        if len(matches) > 1:
            raise RetuneError("ambiguous_group", f"match group name {name!r} is declared twice")
        if matches[0] not in chosen_groups:
            chosen_groups.append(matches[0])

    chosen_pairs: list[Any] = []
    for pos, neg in diffpairs:
        pair_matches = [
            p
            for p in detected_pairs
            if (p.pair.positive.net_name, p.pair.negative.net_name) == (pos, neg)
            or (p.pair.positive.net_name, p.pair.negative.net_name) == (neg, pos)
        ]
        if not pair_matches:
            raise RetuneError("unknown_diffpair", f"no detected diff pair {pos!r}/{neg!r}")
        if len(pair_matches) > 1:
            raise RetuneError("ambiguous_diffpair", f"diff pair {pos!r}/{neg!r} is ambiguous")
        if pair_matches[0] not in chosen_pairs:
            chosen_pairs.append(pair_matches[0])

    tolerance: dict[str, tuple[float, str]] = {}
    selected: set[str] = set()
    for g in chosen_groups:
        tolerance[_target_key("match_group", g.name)] = (
            resolve_group_tolerance_mm(g, net_names, net_class_map),
            _group_tolerance_source(g, net_names, net_class_map),
        )
        ids = list(g.net_ids) + [n for pair in g.pair_ids for n in pair]
        selected.update(net_names[i] for i in ids if i in net_names)
    for p in chosen_pairs:
        # Issue #7893: a pair's target (the derived ps budget and the error
        # caps) is resolved by ``_retune_once`` from the router, which has the
        # board's stackup; ``tolerance`` gets its length form there.
        selected.update((p.pair.positive.net_name, p.pair.negative.net_name))
    return _Selection(chosen_groups, chosen_pairs, selected, tolerance)


# ---------------------------------------------------------------------------
# Checker-side measurement (same functions ``kct check`` uses)
# ---------------------------------------------------------------------------


def _measure(
    pcb_path: Path, net_class_map: dict
) -> tuple[
    dict[str, float],
    dict[tuple[str, str], float],
    dict[str, float],
    dict[tuple[str, str], float | None],
]:
    """``(group_skew, pair_skew, net_length, pair_skew_ps)`` of a board file, checker-exact.

    Issue #7893: ``pair_skew_ps`` is each pair's delay difference exactly as
    ``kct check`` measures it (:func:`~kicad_tools.validate.diffpair_skew.derive_pair_skews`),
    ``None`` for a pair without per-layer delays.
    """
    from kicad_tools.router.kicad_length import KicadLengthModel
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_skew import derive_pair_skews, derive_skew_limits
    from kicad_tools.validate.match_group_skew import derive_group_skew_data

    pcb = PCB.load(str(pcb_path))
    group_skew, _groups, _thr = derive_group_skew_data(pcb, net_class_map)
    pair_skew, _limits = derive_skew_limits(pcb, net_class_map)
    layers = len(getattr(pcb, "copper_layers", None) or ()) or 2
    pair_ps = {
        (m.p_name, m.n_name): m.skew_ps
        for m in derive_pair_skews(pcb, net_class_map, num_copper_layers=layers)
    }
    model = KicadLengthModel.from_pcb(pcb)
    lengths = {
        net.name: model.net_length_pcb(pcb, net_id)
        for net_id, net in pcb.nets.items()
        if net_id and net.name
    }
    return group_skew, dict(pair_skew), lengths, pair_ps


def _pair_skew(pair_skew: dict[tuple[str, str], float], p: str, n: str) -> float | None:
    for key in ((p, n), (n, p)):
        if key in pair_skew:
            return pair_skew[key]
    return None


# ---------------------------------------------------------------------------
# Board text splice
# ---------------------------------------------------------------------------


def _seg_key(x1: float, y1: float, x2: float, y2: float, layer: str, width: float) -> tuple:
    a, b = sorted([(round(x1, 4), round(y1, 4)), (round(x2, 4), round(y2, 4))])
    return ("seg", a, b, layer, round(width, 4))


def _via_key(x: float, y: float, size: float, layers: tuple[str, ...]) -> tuple:
    return ("via", round(x, 4), round(y, 4), round(size, 4), layers)


def _block_keys(text: str, net_ids: dict[str, int], names_by_id: dict[int, str]) -> list:
    """``[(kind, start, end, net_name, key)]`` for every segment/via/arc block."""
    from kicad_tools.router.optimizer import pcb as opt

    out = []
    for kind in ("segment", "via", "arc"):
        for start, end, block in opt._extract_balanced_blocks(text, kind):
            ident = opt._resolve_block_net(block, names_by_id, net_ids)
            name = ident[1] if ident else ""
            key = None
            if kind == "segment":
                s, e = opt._RE_START.search(block), opt._RE_END.search(block)
                w, ly = opt._RE_WIDTH.search(block), opt._RE_LAYER.search(block)
                if s and e and w and ly:
                    key = _seg_key(
                        float(s.group(1)),
                        float(s.group(2)),
                        float(e.group(1)),
                        float(e.group(2)),
                        ly.group(1),
                        float(w.group(1)),
                    )
            elif kind == "via":
                at = re.search(r"\(at\s+([\d.eE+-]+)\s+([\d.eE+-]+)\)", block)
                sz = re.search(r"\(size\s+([\d.eE+-]+)\)", block)
                lys = re.search(r"\(layers\s+((?:\"[^\"]+\"\s*)+)\)", block)
                if at and sz and lys:
                    key = _via_key(
                        float(at.group(1)),
                        float(at.group(2)),
                        float(sz.group(1)),
                        tuple(re.findall(r'"([^"]+)"', lys.group(1))),
                    )
            out.append((kind, start, end, name, key))
    return out


def _block_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a block span to its whole line(s) so removal leaves no blank line."""
    s = start
    while s > 0 and text[s - 1] in " \t":
        s -= 1
    e = end
    if e < len(text) and text[e] == "\n":
        e += 1
    return s, e


def _strip_selected(text: str, selected: set[str], net_ids: dict[str, int], names: dict) -> str:
    spans = [
        _block_span(text, st, en)
        for kind, st, en, name, _k in _block_keys(text, net_ids, names)
        if kind in ("segment", "via") and name in selected
    ]
    out, prev = [], 0
    for s, e in sorted(spans):
        out.append(text[prev:s])
        prev = e
    out.append(text[prev:])
    return "".join(out)


@contextlib.contextmanager
def _deterministic_uuids() -> Iterator[None]:
    """Seeded uuids for new copper; the global RNG and uuid mode are restored."""
    from kicad_tools.router.primitives import (
        enable_deterministic_uuids,
        is_deterministic_uuids_enabled,
        reset_deterministic_uuids,
    )

    was_on = is_deterministic_uuids_enabled()
    state = random.getstate()
    random.seed(UUID_SEED)
    enable_deterministic_uuids(True)
    try:
        yield
    finally:
        random.setstate(state)
        if not was_on:
            reset_deterministic_uuids()


def _serialize(items: Sequence[Any], name_only: bool) -> list[str]:
    return [item.to_sexp(name_only=name_only) for item in items]


def _splice(
    text: str,
    tuned: dict[str, Any],
    net_ids: dict[str, int],
    names_by_id: dict[int, str],
    name_only: bool,
) -> str:
    """Rewrite only the tuned nets' copper blocks; keep unchanged blocks verbatim."""
    from collections import Counter

    blocks = _block_keys(text, net_ids, names_by_id)
    removals: list[tuple[int, int]] = []
    inserts: dict[int, list[str]] = {}  # insertion offset -> new block text
    for net_name, route in sorted(tuned.items()):
        own = [b for b in blocks if b[3] == net_name and b[0] in ("segment", "via")]
        if not own:
            raise RetuneError("no_geometry", f"selected net {net_name!r} has no copper to retune")
        want_segs = Counter(
            _seg_key(s.x1, s.y1, s.x2, s.y2, s.layer.kicad_name, s.width) for s in route.segments
        )
        want_vias = Counter(
            _via_key(v.x, v.y, v.diameter, tuple(layer.kicad_name for layer in v.drilled_layers))
            for v in route.vias
        )
        keep = want_segs + want_vias
        for kind, st, en, _n, key in own:
            if key is not None and keep[key] > 0:
                keep[key] -= 1
            else:
                removals.append(_block_span(text, st, en))
        new_segs, new_vias = [], []
        have = Counter(b[4] for b in own if b[4] is not None)
        for seg in route.segments:
            k = _seg_key(seg.x1, seg.y1, seg.x2, seg.y2, seg.layer.kicad_name, seg.width)
            if have[k] > 0:
                have[k] -= 1
            else:
                new_segs.append(seg)
        for via in route.vias:
            k = _via_key(
                via.x, via.y, via.diameter, tuple(layer.kicad_name for layer in via.drilled_layers)
            )
            if have[k] > 0:
                have[k] -= 1
            else:
                new_vias.append(via)
        added = _serialize(new_segs, name_only) + _serialize(new_vias, name_only)
        if added:
            # After the net's last block of any kind by file offset (arcs
            # included); ``own[-1]`` is list-ordered, not file-ordered (#7796).
            last = max((b for b in blocks if b[3] == net_name), key=lambda b: b[2])
            anchor = _block_span(text, last[1], last[2])[1]
            inserts.setdefault(anchor, []).extend("\t" + a + "\n" for a in added)
    # An insert at an offset must precede a removal that starts there.
    events = [(s, 1, e, "") for s, e in removals]
    events += [(o, 0, o, "".join(chunks)) for o, chunks in inserts.items()]
    out, pos = [], 0
    for start, _order, end, payload in sorted(events, key=lambda x: (x[0], x[1])):
        out.append(text[pos:start])
        out.append(payload)
        pos = max(pos, end)
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Router adapter
# ---------------------------------------------------------------------------


def _adopt_existing_routes(router: Any, selected: set[str]) -> None:
    """Make loaded copper visible to the tuners.

    ``load_pcb_for_routing(load_existing_routes=True)`` files all copper in
    ``router.existing_routes`` and marks it on the grid once.  Moving the
    same objects into ``router.routes`` keeps that single marking valid
    (``restore_route_snapshot`` unmarks/re-marks routes in ``self.routes``).
    Non-selected nets are flagged ``is_escape`` so every tuner treats them as
    fixed -- they stay in the foreign-clearance view but are never edited.
    """
    adopted = list(router.existing_routes)
    for route in adopted:
        if route.net_name not in selected:
            route.is_escape = True
    router.existing_routes[:] = []
    router.routes[:] = adopted


def _geometry_signature(routes: Sequence[Any]) -> dict[str, tuple]:
    """``{net_name: (sorted segments, sorted vias)}`` over every route fragment."""
    segs: dict[str, list] = {}
    vias: dict[str, list] = {}
    for route in routes:
        segs.setdefault(route.net_name, []).extend(
            (s.x1, s.y1, s.x2, s.y2, s.layer.kicad_name, s.width) for s in route.segments
        )
        vias.setdefault(route.net_name, []).extend(
            (v.x, v.y, v.diameter, v.drill) for v in route.vias
        )
    return {n: (sorted(segs[n]), sorted(vias[n])) for n in segs}


def _file_order_fn(blocks: list) -> Any:
    """``Route -> Route`` with its copper in the order ``_splice`` will write it.

    Issue #7790: the checker and kicad-cli resolve track junctions in board-file
    order, while the length model's default (canonical UUID order) assumes a
    freshly routed net.  On a board whose file order is not canonical the two
    disagree by ~0.07 mm per net, which made the tuner call DQS "already
    within tolerance" at a checker skew of 0.1194 mm.  Copper that already
    exists keeps its file position; copper the tuner adds is appended after it
    (``_splice`` inserts after the net's last block).
    """
    from dataclasses import replace

    positions: dict[tuple[str, tuple], list[int]] = {}
    for idx, (kind, _st, _en, net, key) in enumerate(sorted(blocks, key=lambda b: b[1])):
        if key is not None and kind in ("segment", "via"):
            positions.setdefault((net, key), []).append(idx)

    def ordered(items: Sequence[Any], net: str, key_of: Any) -> list[Any]:
        taken: dict[tuple, int] = {}
        kept: list[tuple[int, Any]] = []
        added: list[Any] = []
        for item in items:
            key = key_of(item)
            slots = positions.get((net, key), [])
            n = taken.get(key, 0)
            if n < len(slots):
                taken[key] = n + 1
                kept.append((slots[n], item))
            else:
                added.append(item)
        return [i for _p, i in sorted(kept, key=lambda t: t[0])] + added

    def order(route: Any) -> Any:
        segs = ordered(
            route.segments,
            route.net_name,
            lambda s: _seg_key(s.x1, s.y1, s.x2, s.y2, s.layer.kicad_name, s.width),
        )
        vias = ordered(
            route.vias,
            route.net_name,
            lambda v: _via_key(
                v.x, v.y, v.diameter, tuple(layer.kicad_name for layer in v.drilled_layers)
            ),
        )
        return replace(route, segments=segs, vias=vias)

    return order


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _require_board_prerequisites(text: str, net_class_map: dict | None) -> None:
    if not net_class_map:
        raise RetuneError(
            "missing_rules",
            "a net-class map (the sidecar kct check uses) is required: "
            "tolerances are never inferred",
        )
    if not re.search(r"\(layers\b", text) or '"F.Cu"' not in text:
        raise RetuneError("missing_stackup", "board declares no copper layers")
    if not re.search(r"\(general\b[^()]*(?:\([^()]*\)[^()]*)*\(thickness\s+[\d.]+\)", text) and (
        "(stackup" not in text
    ):
        raise RetuneError(
            "missing_stackup",
            "board has neither (general (thickness ...)) nor a (setup (stackup ...)); "
            "the kicad-cli length model needs one",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _limiting_members(
    per_net: dict[int, tuple[Any, Any]], net_names: dict[int, str], tolerance_mm: float
) -> str:
    """The group members still short of the longest one, with the tuner's reason.

    Issue #7815: the group-level reason set (``already_within_tolerance,
    improved_still_over_limit``) does not say which member stopped short or
    why.  A member limits the group when its tuned length is more than
    ``tolerance_mm`` below the longest member; members that share a reason and
    message (the two rails of a pair lane) are reported together.
    """
    if not per_net:
        return ""
    lengths = {nid: res.length_after_mm for nid, (_r, res) in per_net.items()}
    longest = max(lengths.values())
    entries: dict[tuple[str, str], list[str]] = {}
    spans: dict[tuple[str, str], tuple[float, float]] = {}
    for nid, (_r, res) in sorted(per_net.items()):
        if res.success or longest - lengths[nid] <= tolerance_mm + _EPS:
            continue
        if res.reason in ("reference", "already_within_tolerance", "not_length_critical"):
            continue
        key = (res.reason, res.message)
        entries.setdefault(key, []).append(net_names.get(nid, str(nid)))
        spans.setdefault(key, (res.length_before_mm, res.length_after_mm))
    return "; ".join(
        f"{'/'.join(names)} {spans[key][0]:.3f} -> {spans[key][1]:.3f} mm "
        f"of {longest:.3f} mm [{key[0]}]: {key[1]}"
        for key, names in entries.items()
    )


def _failure_detail(failed: Sequence[TargetReport]) -> str:
    def skew(t: TargetReport) -> str:
        assert t.before_skew_mm is not None and t.after_skew_mm is not None
        text = f"skew {t.before_skew_mm:.4f} -> {t.after_skew_mm:.4f} mm"
        if t.budget_ps is not None and t.before_skew_ps is not None and t.after_skew_ps is not None:
            # Issue #7893: the pair is judged in time.
            text = (
                f"skew {t.before_skew_ps:.3f} -> {t.after_skew_ps:.3f} ps "
                f"({t.before_skew_mm:.4f} -> {t.after_skew_mm:.4f} mm), "
                f"budget {t.budget_ps:.2f} ps"
            )
        return text

    return "; ".join(
        f"{t.name}: {skew(t)}, "
        f"limit {t.tolerance_mm:g} mm ({t.tuner_reason or 'no tuner result'})"
        + (f"; limited by {t.tuner_detail}" if t.tuner_detail else "")
        for t in failed
        if t.before_skew_mm is not None and t.after_skew_mm is not None
    )


def retune_pcb(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    groups: Sequence[str] = (),
    diffpairs: Sequence[tuple[str, str]] = (),
    net_class_map: dict | None = None,
    provenance_path: str | Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    keep_achieved: bool = False,
) -> RetuneReport:
    """Retune the selected groups/pairs of ``input_pcb`` into ``output_pcb``.

    Raises :class:`RetuneError` (and writes nothing) on an unknown or
    ambiguous selection, missing rules/stackup, unsupported copper, a timeout,
    a target the tuners could not reach, or any change outside the selection.

    ``keep_achieved`` (Issue #7815): when some targets are ``unattainable``,
    re-run the retune on the *unchanged input* with those targets dropped, so
    the targets that do reach their limit are still written (in one output,
    by one uuid-seeded splice).  The dropped targets stay in the report with
    status ``"unattainable"``, re-measured on the written board, and the
    report's status is ``"partial"``.  Every other refusal still raises, and
    so does a run in which no target is achieved.  Each re-run gets its own
    ``timeout_s``.
    """
    kw: dict[str, Any] = {
        "net_class_map": net_class_map,
        "provenance_path": provenance_path,
        "timeout_s": timeout_s,
    }
    if not keep_achieved:
        return _retune_once(input_pcb, output_pcb, groups=groups, diffpairs=diffpairs, **kw)

    group_list = list(groups)
    pair_list: list[tuple[str, str]] = [(p[0], p[1]) for p in diffpairs]
    dropped: dict[str, TargetReport] = {}
    while True:
        try:
            report = _retune_once(
                input_pcb, output_pcb, groups=group_list, diffpairs=pair_list, **kw
            )
            break
        except RetuneError as exc:
            failed = [t for t in exc.targets if not t.achieved]
            if exc.code != "unattainable" or not failed:
                raise
            for t in failed:
                dropped[_target_key(t.kind, t.name)] = t
            group_list = [g for g in group_list if _target_key("match_group", g) not in dropped]
            pair_list = [
                p for p in pair_list if _target_key("diffpair", f"{p[0]}/{p[1]}") not in dropped
            ]
            if not group_list and not pair_list:
                # Nothing achievable is left: report every dropped target.
                raise RetuneError(
                    "unattainable",
                    "target not reached; no output written. "
                    + _failure_detail(list(dropped.values())),
                    targets=list(dropped.values()),
                ) from exc
    if not dropped:
        return report

    # The dropped targets as they stand on the written board: their nets may
    # be shared with a written target (a pair that is also a group lane).
    assert net_class_map is not None
    group_now, pair_now, _, ps_now = _measure(Path(report.output_path), net_class_map)
    for t in dropped.values():
        attempted = t.after_skew_mm
        p_name, _, n_name = t.name.partition("/")
        t.after_skew_mm = (
            group_now.get(t.name)
            if t.kind == "match_group"
            else _pair_skew(pair_now, p_name, n_name)
        )
        if t.kind == "diffpair" and t.budget_ps is not None:
            t.after_skew_ps = _pair_skew(ps_now, p_name, n_name)
        t.achieved = False
        t.status = "unattainable"
        best = f"best attempt {attempted:.4f} mm, not written" if attempted is not None else ""
        t.tuner_detail = "; limited by ".join(x for x in (best, t.tuner_detail) if x)
    report.targets = [*report.targets, *dropped.values()]
    report.status = "partial"
    report.settings["unattainable_targets"] = [t.name for t in dropped.values()]
    prov_bytes = (json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_atomic(Path(report.provenance_path), prov_bytes)
    return report


def _retune_once(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    groups: Sequence[str] = (),
    diffpairs: Sequence[tuple[str, str]] = (),
    net_class_map: dict | None = None,
    provenance_path: str | Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RetuneReport:
    """One all-or-nothing retune (see :func:`retune_pcb`)."""
    from dataclasses import replace

    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.optimizer.pcb import parse_net_names

    src = Path(input_pcb)
    dst = Path(output_pcb)
    prov = Path(provenance_path) if provenance_path else dst.with_name(dst.name + ".retune.json")
    if not src.is_file():
        raise RetuneError("missing_input", f"input board {src} does not exist")
    if dst.resolve() == src.resolve() or (dst.exists() and os.path.samefile(dst, src)):
        raise RetuneError("output_is_input", "the output path must differ from the input board")
    for target in (dst, prov):
        if target.exists():
            raise RetuneError("output_exists", f"refusing to overwrite existing {target}")
        if not target.parent.is_dir():
            raise RetuneError("bad_output", f"output directory {target.parent} does not exist")
    if prov.resolve() in (src.resolve(), dst.resolve()):
        raise RetuneError("bad_output", "provenance path collides with the board paths")

    input_bytes = src.read_bytes()
    input_hash = hashlib.sha256(input_bytes).hexdigest()
    text = input_bytes.decode("utf-8")
    _require_board_prerequisites(text, net_class_map)
    assert net_class_map is not None

    names_by_id = parse_net_names(text)
    net_ids = {name: nid for nid, name in names_by_id.items()}

    try:
        router, _ = load_pcb_for_routing(
            str(src),
            use_pcb_rules=True,
            validate_drc=False,
            strict_drc=False,
            load_existing_routes=True,
            force_python=True,
        )
    except Exception as exc:  # noqa: BLE001 - any loader failure is an unsupported PCB
        raise RetuneError("unsupported_pcb", f"could not load {src.name}: {exc}") from exc
    router.net_class_map = dict(net_class_map)

    selection = _resolve_selection(
        groups, diffpairs, router.net_names, net_class_map, router.length_tracker
    )

    blocks = _block_keys(text, net_ids, names_by_id)
    # Issue #7790: measure in the order the written file will hold the copper.
    router.route_copper_order = _file_order_fn(blocks)
    arcs = sorted({b[3] for b in blocks if b[0] == "arc" and b[3] in selection.net_names})
    if arcs:
        raise RetuneError(
            "unsupported_pcb",
            f"selected net(s) {arcs} carry (arc ...) copper the tuners cannot edit",
        )
    missing = sorted(
        n for n in selection.net_names if not any(b[3] == n and b[0] != "arc" for b in blocks)
    )
    if missing:
        raise RetuneError("no_geometry", f"selected net(s) {missing} are not routed")

    group_before, pair_before, len_before, ps_before = _measure(src, net_class_map)
    # Issue #7893: each selected pair's skew target -- the derived ps budget
    # and the error-level caps, from the resolver ``kct check`` reads.
    for p in selection.pairs:
        p_name, n_name = p.pair.positive.net_name, p.pair.negative.net_name
        key = _target_key("diffpair", f"{p_name}/{n_name}")
        target = router._pair_skew_target(p_name, n_name)
        selection.pair_targets[key] = target
        tol_mm = target.tolerance_mm
        selection.tolerance[key] = (
            tol_mm if tol_mm is not None else float("inf"),
            target.describe(),
        )

    def violated(
        g_skew: dict[str, float],
        p_skew: dict[tuple[str, str], float],
        p_ps: dict[tuple[str, str], float | None] | None = None,
    ) -> list[TargetReport]:
        reports: list[TargetReport] = []
        for g in selection.groups:
            tol, source = selection.tolerance[_target_key("match_group", g.name)]
            reports.append(
                TargetReport(
                    kind="match_group",
                    name=g.name,
                    nets=sorted(
                        router.net_names[i]
                        for i in list(g.net_ids) + [n for pr in g.pair_ids for n in pr]
                    ),
                    tolerance_mm=tol,
                    tolerance_source=source,
                    before_skew_mm=group_before.get(g.name),
                    after_skew_mm=g_skew.get(g.name),
                    achieved=False,
                    status="",
                )
            )
        for p in selection.pairs:
            p_name, n_name = p.pair.positive.net_name, p.pair.negative.net_name
            tol, source = selection.tolerance[_target_key("diffpair", f"{p_name}/{n_name}")]
            from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

            th = resolve_pair_thresholds(net_class_map, p_name, n_name)
            target = selection.pair_targets[_target_key("diffpair", f"{p_name}/{n_name}")]
            in_time = target.budget_ps is not None
            reports.append(
                TargetReport(
                    kind="diffpair",
                    name=f"{p_name}/{n_name}",
                    nets=[p_name, n_name],
                    tolerance_mm=tol,
                    tolerance_source=source,
                    before_skew_mm=_pair_skew(pair_before, p_name, n_name),
                    after_skew_mm=_pair_skew(p_skew, p_name, n_name),
                    achieved=False,
                    status="",
                    stretch_target_mm=th.skew_stretch_mm,
                    budget_ps=target.budget_ps,
                    before_skew_ps=_pair_skew(ps_before, p_name, n_name) if in_time else None,
                    after_skew_ps=(
                        _pair_skew(p_ps, p_name, n_name) if in_time and p_ps is not None else None
                    ),
                )
            )
        return reports

    # Targets that are unmeasurable (unrouted member) cannot be judged.
    unmeasured = [t.name for t in violated(group_before, pair_before) if t.before_skew_mm is None]
    if unmeasured:
        raise RetuneError(
            "unmeasurable",
            f"cannot measure {unmeasured}: a member is unrouted or has no checker data",
        )

    _adopt_existing_routes(router, selection.net_names)
    before_geometry = _geometry_signature(router.routes)

    tuner_reasons: dict[str, str] = {}
    tuner_details: dict[str, str] = {}
    with _deadline(timeout_s):
        # Issue #7766: pairs first.  A pair retune lengthens the short leg
        # of its lane, which can make that lane the group's reference, so the
        # group is matched against lengths that are final.
        if selection.pairs:
            pair_results = router.apply_diffpair_length_tuning(
                selection.pairs,
                verbose=False,
                engage="checker_skew_limit",
                # Issue #7760: the written board is DRC-checked against the
                # input below, so the tuner's strict foreign-clearance gate
                # (which rejects every leg off a host already under the class
                # clearance) is replaced by that check.  Issue #7767: foreign
                # pads are still gated, against the host's existing gap, so a
                # bump is never laid onto a pad the DRC then refuses.
                foreign_clearance_host_tolerant=True,
            )
            for (p_name, n_name), res in pair_results.items():
                tuner_reasons[_target_key("diffpair", f"{p_name}/{n_name}")] = res.reason
        if selection.groups:
            results = router.apply_match_group_tuning(
                selection.groups, verbose=False, engage="checker_limit"
            )
            for name, per_net in results.items():
                reasons = sorted({res.reason for (_r, res) in per_net.values()})
                tuner_reasons[_target_key("match_group", name)] = (
                    ",".join(reasons) or "within_limit"
                )
                key = _target_key("match_group", name)
                tuner_details[key] = _limiting_members(
                    per_net, router.net_names, selection.tolerance.get(key, (0.0, ""))[0]
                )

    # Which nets actually changed?
    merged: dict[str, Any] = {}
    for route in router.routes:
        if route.net_name in selection.net_names:
            cur = merged.get(route.net_name)
            merged[route.net_name] = (
                route
                if cur is None
                else replace(
                    cur, segments=cur.segments + route.segments, vias=cur.vias + route.vias
                )
            )
    after_geometry = _geometry_signature(router.routes)
    stray = sorted(
        n
        for n in set(before_geometry) | set(after_geometry)
        if n not in selection.net_names and before_geometry.get(n) != after_geometry.get(n)
    )
    if stray:
        raise RetuneError("scope_violation", f"tuning touched unselected net(s): {stray}")
    changed = {
        n: route for n, route in merged.items() if before_geometry.get(n) != after_geometry.get(n)
    }

    name_only = _uses_name_only(text)
    with _deterministic_uuids():
        new_text = _splice(text, changed, net_ids, names_by_id, name_only) if changed else text

    # Scope proof: with the selected nets' copper removed, nothing else differs.
    if _strip_selected(new_text, selection.net_names, net_ids, names_by_id) != _strip_selected(
        text, selection.net_names, net_ids, names_by_id
    ):
        raise RetuneError("scope_violation", "output differs from input outside the selected nets")

    if changed:
        _require_no_clearance_regression(src, new_text, selection.net_names, net_class_map)

    # Measure the *written* board, with the checker's own functions.
    with tempfile.TemporaryDirectory(prefix="kct-retune-") as scratch:
        probe = Path(scratch) / dst.name
        probe.write_text(new_text, encoding="utf-8")
        group_after, pair_after, len_after, ps_after = _measure(probe, net_class_map)

    _require_no_pair_skew_regression(
        pair_before,
        pair_after,
        net_class_map,
        ps_before=ps_before,
        ps_after=ps_after,
        targets=_targets_by_pair(router, pair_after),
    )

    targets = violated(group_after, pair_after, ps_after)
    for t in targets:
        key = _target_key(t.kind, t.name)
        t.tuner_reason = tuner_reasons.get(key, "")
        t.tuner_detail = tuner_details.get(key, "")
        assert t.before_skew_mm is not None
        if t.after_skew_mm is None:
            raise RetuneError("unmeasurable", f"{t.name}: cannot re-measure the written board")
        pair_target = selection.pair_targets.get(key) if t.kind == "diffpair" else None
        if pair_target is not None:
            # Issue #7893: judged as ``kct check`` judges it -- the ps budget
            # and every error cap.
            t.achieved = pair_target.within(t.after_skew_ps, t.after_skew_mm)
            satisfied_before = pair_target.within(t.before_skew_ps, t.before_skew_mm)
        else:
            t.achieved = t.after_skew_mm <= t.tolerance_mm + _EPS
            satisfied_before = t.before_skew_mm <= t.tolerance_mm + _EPS
        if t.kind == "diffpair" and t.stretch_target_mm is not None:
            t.stretch_achieved = t.after_skew_mm <= t.stretch_target_mm + _EPS
        if satisfied_before and not changed:
            t.status = "already_satisfied"
        else:
            t.status = "tuned" if t.achieved else "unattainable"
    failed = [t for t in targets if not t.achieved]
    if failed:
        raise RetuneError(
            "unattainable",
            f"target not reached; no output written. {_failure_detail(failed)}",
            targets=targets,
        )

    out_bytes = new_text.encode("utf-8")
    report = RetuneReport(
        input_path=str(src),
        output_path=str(dst),
        provenance_path=str(prov),
        input_sha256=input_hash,
        output_sha256=hashlib.sha256(out_bytes).hexdigest(),
        tool_version=_tool_version(),
        status="tuned" if changed else "already_satisfied",
        targets=targets,
        changed_nets=sorted(changed),
        net_lengths_mm={
            n: {"before": len_before.get(n, 0.0), "after": len_after.get(n, 0.0)}
            for n in sorted(selection.net_names)
        },
        settings={
            "timeout_s": timeout_s,
            "uuid_seed": UUID_SEED,
            "length_model": "router.kicad_length (kicad-cli exact, #6725)",
            "group_engage": "checker_limit",
            "diffpair_engage": "checker_skew_limit",
            "selected_groups": [g.name for g in selection.groups],
            "selected_diffpairs": [t.name for t in targets if t.kind == "diffpair"],
        },
    )
    prov_bytes = (json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_atomic(dst, out_bytes)
    try:
        _write_atomic(prov, prov_bytes)
    except BaseException:
        with contextlib.suppress(OSError):
            dst.unlink()
        raise
    return report


def _targets_by_pair(
    router: Any, pairs: dict[tuple[str, str], float]
) -> dict[tuple[str, str], Any]:
    """Every measured pair's skew target, from the router's resolver (Issue #7893)."""
    return {(p, n): router._pair_skew_target(p, n) for (p, n) in pairs}


def _require_no_pair_skew_regression(
    pair_before: dict[tuple[str, str], float],
    pair_after: dict[tuple[str, str], float],
    net_class_map: dict,
    *,
    ps_before: dict[tuple[str, str], float | None] | None = None,
    ps_after: dict[tuple[str, str], float | None] | None = None,
    targets: dict[tuple[str, str], Any] | None = None,
) -> None:
    """Reject a retune that pushed any diff pair off its skew target (Issue #6447).

    The post-pass pair-skew invariant, on the checker's own numbers: every
    pair on the board -- selected or not -- is compared before and after on
    the written copper.  A pair now off its target that met it, or that got
    more than 0.01 mm (in ps: that length at the slowest layer delay) further
    off it, fails the whole retune (nothing is written).  The tuners carry
    their own guards (#6664's never-worse backstop, #6675's group invariant);
    this is the board-level backstop behind them.

    Issue #7893: ``targets`` maps each pair to its
    :class:`~kicad_tools.router.pair_skew_target.PairSkewTarget` (the ps
    budget and the error caps) and ``ps_before`` / ``ps_after`` carry its
    skew in ps; without ``targets`` each pair's target is resolved from
    ``net_class_map`` on the default stackup for a 2-layer board.
    """
    from .pair_postpass_invariant import PairSkew, classify_target_change
    from .pair_skew_target import resolve_pair_skew_target, skew_stackup

    ps_before = ps_before or {}
    ps_after = ps_after or {}
    stackup = None if targets is not None else skew_stackup(None, 2)
    broken: list[str] = []
    for (p_name, n_name), after_mm in sorted(pair_after.items()):
        target = (targets or {}).get((p_name, n_name)) or resolve_pair_skew_target(
            net_class_map, p_name, n_name, stackup
        )
        if not target.enforced:
            continue
        before_mm = _pair_skew(pair_before, p_name, n_name)
        if before_mm is None:
            continue
        before = PairSkew(_pair_skew(ps_before, p_name, n_name), before_mm)
        after = PairSkew(_pair_skew(ps_after, p_name, n_name), after_mm)
        kind = classify_target_change(target, before, after)
        if kind is not None:
            if before.ps is not None and after.ps is not None:
                moved = (
                    f"{before.ps:.3f} -> {after.ps:.3f} ps ({before_mm:.3f} -> {after_mm:.3f} mm)"
                )
            else:
                moved = f"{before_mm:.3f} -> {after_mm:.3f} mm"
            broken.append(f"{p_name}/{n_name} {moved} (target {target.short()}, {kind})")
    if broken:
        raise RetuneError(
            "pair_skew_regression",
            "tuning broke diff-pair skew; no output written: " + "; ".join(broken),
        )


def _clearance_findings(
    pcb_path: Path, net_class_map: dict, nets: set[str]
) -> dict[tuple[str, tuple[str, ...], str], float]:
    """Smallest measured gap per (rule, net pair, layer) of clearance errors on ``nets``.

    Copper-to-copper only.  Zone fills are deliberately not compared: the
    retune does not regenerate them, so a bump laid across a committed foreign
    fill is stale-fill noise that the next zone refill carves around (Board 07's
    ``+1V8`` pour); segment-vs-zone errors would refuse every such bump.
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    pcb = PCB.load(str(pcb_path))
    layers = len(pcb.copper_layers) or 2
    checker = DRCChecker(pcb, layers=layers, net_class_map=net_class_map)
    found: dict[tuple[str, tuple[str, ...], str], float] = {}
    for v in checker.check_clearances().errors:
        if not nets.intersection(v.nets):
            continue
        key = (v.rule_id, tuple(sorted(v.nets)), v.layer or "")
        gap = v.actual_value if v.actual_value is not None else 0.0
        found[key] = min(found.get(key, gap), gap)
    return found


def _require_no_clearance_regression(
    src: Path, new_text: str, nets: set[str], net_class_map: dict
) -> None:
    """Refuse a retune whose output has a clearance error the input did not (Issue #7760).

    Both boards are DRC-checked; only errors touching a selected net count.  A
    finding already in the input (same rule, nets and layer) is the board's own
    and passes unless the output makes it worse.  Nothing has been written yet.
    """
    with tempfile.TemporaryDirectory(prefix="kct-retune-drc-") as scratch:
        probe = Path(scratch) / "output.kicad_pcb"
        probe.write_text(new_text, encoding="utf-8")
        before = _clearance_findings(src, net_class_map, nets)
        after = _clearance_findings(probe, net_class_map, nets)
    worse = sorted(
        f"{rule} {'/'.join(pair)} on {layer or '?'}: {gap:.4f} mm"
        + (f" (input {before[key]:.4f} mm)" if key in before else "")
        for key, gap in after.items()
        for rule, pair, layer in [key]
        if key not in before or gap + 1e-3 < before[key]
    )
    if worse:
        raise RetuneError(
            "drc_regression",
            "the retune introduces clearance error(s) the input board did not have; "
            "no output written: " + "; ".join(worse),
        )


def _uses_name_only(text: str) -> bool:
    return re.search(r'\((?:segment|via)\b[^()]*(?:\([^()]*\)[^()]*)*?\(net\s+"', text) is not None
