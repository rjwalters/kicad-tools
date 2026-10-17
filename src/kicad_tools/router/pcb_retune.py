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

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

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
    #: Diff pairs only: the separate, tighter stretch target (not the gate).
    stretch_target_mm: float | None = None
    stretch_achieved: bool | None = None


@dataclass
class RetuneReport:
    """Result of a successful :func:`retune_pcb` (failures raise)."""

    input_path: str
    output_path: str
    provenance_path: str
    input_sha256: str
    output_sha256: str
    tool_version: str
    status: str  # "tuned" | "already_satisfied"
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
    from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds
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
        p_name, n_name = p.pair.positive.net_name, p.pair.negative.net_name
        th = resolve_pair_thresholds(net_class_map, p_name, n_name)
        tolerance[_target_key("diffpair", f"{p_name}/{n_name}")] = (th.skew_mm, th.skew_source)
        selected.update((p_name, n_name))
    return _Selection(chosen_groups, chosen_pairs, selected, tolerance)


# ---------------------------------------------------------------------------
# Checker-side measurement (same functions ``kct check`` uses)
# ---------------------------------------------------------------------------


def _measure(
    pcb_path: Path, net_class_map: dict
) -> tuple[dict[str, float], dict[tuple[str, str], float], dict[str, float]]:
    """``(group_skew, pair_skew, net_length)`` of a board file, checker-exact."""
    from kicad_tools.router.kicad_length import KicadLengthModel
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_skew import derive_skew_limits
    from kicad_tools.validate.match_group_skew import derive_group_skew_data

    pcb = PCB.load(str(pcb_path))
    group_skew, _groups, _thr = derive_group_skew_data(pcb, net_class_map)
    pair_skew, _limits = derive_skew_limits(pcb, net_class_map)
    model = KicadLengthModel.from_pcb(pcb)
    lengths = {
        net.name: model.net_length_pcb(pcb, net_id)
        for net_id, net in pcb.nets.items()
        if net_id and net.name
    }
    return group_skew, dict(pair_skew), lengths


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
            anchor = _block_span(text, own[-1][1], own[-1][2])[1]
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
    _rekey_name_only_routes(router)
    adopted = list(router.existing_routes)
    for route in adopted:
        if route.net_name not in selected:
            route.is_escape = True
    router.existing_routes[:] = []
    router.routes[:] = adopted


def _rekey_name_only_routes(router: Any) -> None:
    """Give KiCad-10 name-only copper its real net id (Issue #5798).

    The routing loader reads ``(segment ... (net "DQ0"))`` copper with
    ``net=0`` -- the optimizer parser only knows numeric references -- so on a
    board saved by KiCad 10 every loaded route arrives under net 0 and the
    tuners, which look members up by net id, see each one as unrouted.  Board
    07's committed routed artifact is such a board: every group came back
    "not tuned (a member is unrouted)" and nothing was written.

    The route keeps its geometry; only its owner changes, so the grid is
    unmarked under the old owner and re-marked under the new one.
    """
    from dataclasses import replace

    ids = {name: nid for nid, name in router.net_names.items() if name}
    rekeyed = []
    for route in router.existing_routes:
        nid = ids.get(route.net_name)
        if route.net != 0 or nid is None:
            rekeyed.append(route)
            continue
        router.grid.unmark_route(route)
        route = replace(
            route,
            net=nid,
            segments=[replace(seg, net=nid) for seg in route.segments],
            vias=[replace(via, net=nid) for via in route.vias],
        )
        router.grid.mark_route(route)
        router.grid._mark_route_on_cpp_cells(route)
        rekeyed.append(route)
    router.existing_routes[:] = rekeyed


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


def retune_pcb(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    groups: Sequence[str] = (),
    diffpairs: Sequence[tuple[str, str]] = (),
    net_class_map: dict | None = None,
    provenance_path: str | Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RetuneReport:
    """Retune the selected groups/pairs of ``input_pcb`` into ``output_pcb``.

    Raises :class:`RetuneError` (and writes nothing) on an unknown or
    ambiguous selection, missing rules/stackup, unsupported copper, a timeout,
    a target the tuners could not reach, or any change outside the selection.
    """
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

    group_before, pair_before, len_before = _measure(src, net_class_map)

    def violated(
        g_skew: dict[str, float], p_skew: dict[tuple[str, str], float]
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
    with _deadline(timeout_s):
        if selection.groups:
            results = router.apply_match_group_tuning(
                selection.groups, verbose=False, engage="checker_limit"
            )
            for name, per_net in results.items():
                reasons = sorted({res.reason for (_r, res) in per_net.values()})
                tuner_reasons[_target_key("match_group", name)] = (
                    ",".join(reasons) or "within_limit"
                )
        if selection.pairs:
            pair_results = router.apply_diffpair_length_tuning(
                selection.pairs, verbose=False, engage="checker_skew_limit"
            )
            for (p_name, n_name), res in pair_results.items():
                tuner_reasons[_target_key("diffpair", f"{p_name}/{n_name}")] = res.reason

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

    # Measure the *written* board, with the checker's own functions.
    with tempfile.TemporaryDirectory(prefix="kct-retune-") as scratch:
        probe = Path(scratch) / dst.name
        probe.write_text(new_text, encoding="utf-8")
        group_after, pair_after, len_after = _measure(probe, net_class_map)

    targets = violated(group_after, pair_after)
    for t in targets:
        key = _target_key(t.kind, t.name)
        t.tuner_reason = tuner_reasons.get(key, "")
        assert t.before_skew_mm is not None
        if t.after_skew_mm is None:
            raise RetuneError("unmeasurable", f"{t.name}: cannot re-measure the written board")
        t.achieved = t.after_skew_mm <= t.tolerance_mm + _EPS
        if t.kind == "diffpair" and t.stretch_target_mm is not None:
            t.stretch_achieved = t.after_skew_mm <= t.stretch_target_mm + _EPS
        if t.before_skew_mm <= t.tolerance_mm + _EPS and not changed:
            t.status = "already_satisfied"
        else:
            t.status = "tuned" if t.achieved else "unattainable"
    failed = [t for t in targets if not t.achieved]
    if failed:
        detail = "; ".join(
            f"{t.name}: skew {t.before_skew_mm:.4f} -> {t.after_skew_mm:.4f} mm, "
            f"limit {t.tolerance_mm:g} mm ({t.tuner_reason or 'no tuner result'})"
            for t in failed
        )
        raise RetuneError("unattainable", f"target not reached; no output written. {detail}")

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


def _uses_name_only(text: str) -> bool:
    return re.search(r'\((?:segment|via)\b[^()]*(?:\([^()]*\)[^()]*)*?\(net\s+"', text) is not None
