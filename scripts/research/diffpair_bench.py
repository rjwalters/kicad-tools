#!/usr/bin/env python3
"""Router-produced diff-pair benchmark (Issue #6442, Phase 0 of epic #6441).

Every later diff-pair phase is measured with this harness, so it has to be
reproducible, honest about where the copper came from, and read the same way
whether the router, ``kct check`` or kicad-cli is looking at the result.

Run it through the experiment harness (one entry point) or directly::

    uv run kct build-native --check
    uv run python scripts/research/expand_route_experiment.py pairs \\
        --boards 03,06b,06rf --engines lattice,grid --seeds 42,43 --timeout 600 \\
        --label main --work-dir /tmp/pairs --out-dir data/research/diffpair_bench

    # are two runs identical, pair for pair?
    uv run python scripts/research/diffpair_bench.py compare A.json B.json

Rows
----
``03`` (USB_D, USB_MCU_D), ``06b`` (board 06 with ``LVDS\\d_[PN]`` copper
ripped, ported from ``krt_compare.py``) and ``06rf`` (the 9-pair regression
fixture). Engines: ``lattice`` (``--route-engine lattice --strategy basic
--differential-pairs``) and ``grid`` (``--route-engine grid
--differential-pairs --deterministic-rescue``). Both add the deterministic-budget
flags so a row does not depend on host load, and both hand ``kct route`` the
board's *committed* ``net_class_map.json`` so the router and ``kct check`` resolve
the same per-pair thresholds.

Router-produced guard
---------------------
A pair result only counts if the router drew it:

* the input must carry **zero copper** on every pair net (``06b`` is stripped
  first; a row whose input still has pair copper is refused before routing);
* ``routing_plan.apply_plan`` / any saved-plan replay flag is refused, and a log
  that shows one marks the row invalid;
* the row records the input SHA-256 and the git SHA;
* a row whose pair copper is identical to the committed board's is invalid (the
  committed routing came back, nothing was routed).

Board 06 gap-error root cause
-----------------------------
See ``docs/research/diffpair-bench-phase0.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import expand_route_experiment as ere  # noqa: E402

REPO = ere.REPO

#: ``kct route`` flags that replay saved copper instead of routing. Any of them
#: in a benchmark command line is refused outright. ``--resume PATH`` is the one
#: real flag that seeds routing from saved copper; the plan-replay names this
#: tuple used to list do not exist on ``kct route`` (issue #6475).
REPLAY_FLAGS = ("--resume",)

#: Log text that shows a saved plan or cached copper was replayed.
_REPLAY_LOG = re.compile(
    r"apply_plan|applying (?:saved )?(?:routing )?plan|replay(?:ing|ed) (?:the )?(?:saved )?plan"
    r"|cache hit|loaded (?:routed )?copper from cache",
    re.I,
)

SEEDS = (42, 43)

#: ``kct route`` exits whose ``-o`` file (if any) is not a finished route.
CRASHED_EXIT_CODES = ere.CRASHED_EXIT_CODES

_DET_FLAGS = ("--deterministic-budget", "--per-net-iterations", "1000000")


def engine_args(engine: str, layers: int) -> list[str]:
    """The ``kct route`` flags of one benchmark engine."""
    common = [
        "--differential-pairs",
        "--layers",
        str(layers),
        "--no-auto-layers",
        *_DET_FLAGS,
        "--no-placement-delta-feedback",
    ]
    if engine == "lattice":
        return ["--route-engine", "lattice", "--strategy", "basic", *common]
    if engine == "grid":
        return ["--route-engine", "grid", *common, "--deterministic-rescue"]
    raise ValueError(f"unknown engine {engine!r}; known: lattice, grid")


ENGINES = ("lattice", "grid")


class PairBenchError(RuntimeError):
    """The input or command line would not produce a router-produced result."""


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def check_no_replay(args: Sequence[str]) -> None:
    """Refuse any ``kct route`` argument that replays saved copper or a saved plan."""
    for arg in args:
        flag = arg.split("=", 1)[0]
        # argparse accepts any unambiguous prefix (``--resu``), so refuse those too.
        if any(
            len(flag) > 2 and flag.startswith("--") and replay.startswith(flag)
            for replay in REPLAY_FLAGS
        ):
            raise PairBenchError(
                f"refusing {flag}: the benchmark measures what the router produces, "
                "not a replayed routing plan or cached copper"
            )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_net_class_map(path: Path | None, pcb_path: Path | None = None):
    """The committed net-class map, loaded the way every consumer must."""
    if path is None or not path.is_file():
        return {}
    from kicad_tools.router.rules import net_class_map_from_path

    return net_class_map_from_path(path, pcb_path=pcb_path)


def detect_pairs(pcb, net_class_map) -> list:
    """Every diff pair the layered detector finds on *pcb* (engaged or not)."""
    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs

    names = {n.number: n.name for n in pcb.nets.values() if n.name}
    synth, net_to_class = build_synth_routing(net_class_map or {})
    return detect_diff_pairs(names, net_class_routing=synth, net_to_class=net_to_class)


def pair_net_names(pcb, net_class_map) -> set[str]:
    out: set[str] = set()
    for d in detect_pairs(pcb, net_class_map):
        out.add(d.pair.positive.net_name)
        out.add(d.pair.negative.net_name)
    return out


def pair_copper_counts(pcb, nets: set[str]) -> dict[str, dict[str, int]]:
    """``{net: {"segments": n, "vias": n}}`` for the nets that carry any copper."""
    counts: dict[str, dict[str, int]] = {}
    for seg in pcb.segments:
        if seg.net_name in nets:
            counts.setdefault(seg.net_name, {"segments": 0, "vias": 0})["segments"] += 1
    for via in pcb.vias:
        if via.net_name in nets:
            counts.setdefault(via.net_name, {"segments": 0, "vias": 0})["vias"] += 1
    return counts


def assert_zero_pair_copper(pcb_path: Path, net_class_map) -> set[str]:
    """Refuse an input that carries copper on any pair net; returns the pair nets."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    nets = pair_net_names(pcb, net_class_map)
    copper = pair_copper_counts(pcb, nets)
    if copper:
        shown = ", ".join(
            f"{n} ({c['segments']} seg, {c['vias']} via)" for n, c in sorted(copper.items())
        )
        raise PairBenchError(
            f"input {pcb_path.name} already has copper on pair nets: {shown}; "
            "strip it first -- a pair result must be router-produced"
        )
    return nets


def copper_signature(pcb_path: Path, nets: set[str]) -> frozenset:
    """Order- and direction-independent signature of the copper on *nets*."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    sig: set = set()
    for s in pcb.segments:
        if s.net_name in nets:
            a = (round(s.start[0], 3), round(s.start[1], 3))
            b = (round(s.end[0], 3), round(s.end[1], 3))
            sig.add(("seg", s.layer, min(a, b), max(a, b), round(s.width, 3), s.net_name))
    for v in pcb.vias:
        if v.net_name in nets:
            sig.add(("via", (round(v.position[0], 3), round(v.position[1], 3)), v.net_name))
    return frozenset(sig)


def committed_copper_matches(routed: Path, board: ere.Board, nets: set[str]) -> str | None:
    """Name of the committed board whose pair copper equals *routed*'s, else ``None``.

    Empty copper never matches (an empty routed result is a failed route, not a
    replay).
    """
    src = REPO / board.src
    candidates = [src, src.with_name(f"{src.stem}_routed.kicad_pcb")]
    ours = copper_signature(routed, nets)
    if not ours:
        return None
    for cand in candidates:
        if cand.is_file() and copper_signature(cand, nets) == ours:
            return str(cand.relative_to(REPO))
    return None


def prepare_input(board: ere.Board, run_dir: Path, committed_map: bool = True) -> tuple[Path, dict]:
    """Copy *board*'s input into *run_dir* with pair copper stripped, then guard it.

    Returns ``(path, facts)``; raises :class:`PairBenchError` if pair copper survives.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import krt_compare

    src = REPO / board.src
    run_dir.mkdir(parents=True, exist_ok=True)
    dst = run_dir / src.name
    text = src.read_text()
    stripped = 0
    if board.strip_nets:
        text, stripped = krt_compare.strip_net_copper(text, board.strip_nets)
    dst.write_text(text)
    for suffix in (".kicad_pro", ".kicad_dru"):
        sib = src.with_suffix(suffix)
        if sib.exists():
            shutil.copyfile(sib, dst.with_suffix(suffix))
    ncm_path = REPO / board.net_class_map if (board.net_class_map and committed_map) else None
    ncm = load_net_class_map(ncm_path, pcb_path=dst)
    nets = assert_zero_pair_copper(dst, ncm)
    return dst, {
        "input_sha256": sha256_file(dst),
        "source": board.src,
        "stripped_items": stripped,
        "pair_nets": sorted(nets),
    }


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

_THRESHOLD_LINE = re.compile(
    r"^\s+(\S+)/(\S+): continuity threshold ([\d.]+)%, skew tolerance ([\d.]+) mm", re.M
)
_LATTICE_OUTCOME = re.compile(
    r"^ {4}(\S+): (coupled|partially-coupled\S*|uncoupled-fallback\S*|\S+)$"
)
_SEGMENT_LINE = re.compile(r"^\s*(\S+)\[(\d+)\] (\S+): (\S.*)$", re.M)
_GRID_REPORT = re.compile(r"\[coupled-pair-report\] pair=(\S+) class=(\S+) coupled=(True|False)")


def parse_pair_log(text: str) -> dict:
    """Per-pair outcomes, per-segment lines and printed thresholds from a route log."""
    outcomes: dict[str, str] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "Diff pairs (lattice):" in line:
            for follow in lines[i + 1 :]:
                m = _LATTICE_OUTCOME.match(follow)
                if m:
                    outcomes[m.group(1)] = m.group(2)
                elif follow.startswith("      "):
                    continue  # a per-segment line of the pair above (#6440)
                else:
                    break
    for name, cls, coupled in _GRID_REPORT.findall(text):
        outcomes.setdefault(name, "coupled" if coupled == "True" else f"uncoupled:{cls}")
    segments: dict[str, list[dict]] = {}
    for name, idx, span, outcome in _SEGMENT_LINE.findall(text):
        segments.setdefault(name, []).append(
            {"segment": int(idx), "span": span, "outcome": outcome}
        )
    thresholds = {
        f"{p}/{n}": {"continuity_pct": float(c), "skew_mm": float(s)}
        for p, n, c, s in _THRESHOLD_LINE.findall(text)
    }
    return {"outcomes": outcomes, "segments": segments, "log_thresholds": thresholds}


def measure_pairs(routed: Path, net_class_map, layers: int, log_text: str = "") -> list[dict]:
    """One ``PairOutcome`` per engaged pair on the routed board.

    Fields: ``outcome`` (router log), ``segments`` (per-segment outcomes where the
    log has them), ``skew_mm`` (net-level; per-chain skew is ``null`` -- the board
    files carry no chain boundaries), ``uncoupled_mm`` (both nets, KiCad-equivalent:
    length not parallel-and-within-window of the partner), ``vias_p`` / ``vias_n``,
    ``gap_min_mm`` / ``gap_max_mm`` (edge-to-edge over coupled segments),
    ``coupled_trunk_mm`` (P-side coupled length), ``continuity_pct`` (info only),
    and the thresholds ``kct check`` applies.
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import (
        derive_engagement_state,
        resolve_pair_thresholds,
    )
    from kicad_tools.validate.diffpair_skew import derive_skew_data
    from kicad_tools.validate.rules.clearance import CopperElement, _segment_segment_clearance
    from kicad_tools.validate.rules.diffpair_routing_continuity import (
        DEFAULT_COUPLING_WINDOW_MM,
        DEFAULT_PARALLEL_TOLERANCE_DEG,
        DiffPairRoutingContinuityRule,
        _segment_coupled_overlap,
        _segment_length,
    )

    pcb = PCB.load(str(routed))
    log = parse_pair_log(log_text)
    engaged, _ = derive_engagement_state(pcb, net_class_map)
    # The board thickness ``kct check`` measures vias with: its own checker's value.
    thickness = None
    with contextlib.suppress(Exception):
        from kicad_tools.validate import DRCChecker

        thickness = DRCChecker(
            pcb, manufacturer="jlcpcb", layers=layers
        ).design_rules.board_thickness_mm
    skew_data, _ = derive_skew_data(
        pcb, net_class_map, board_thickness_mm=thickness, num_copper_layers=layers
    )
    segs: dict[int, list] = {}
    for s in pcb.segments:
        if s.net_number:
            segs.setdefault(s.net_number, []).append(CopperElement.from_segment(s))
    vias: dict[int, int] = {}
    for v in pcb.vias:
        vias[v.net_number] = vias.get(v.net_number, 0) + 1
    rule = DiffPairRoutingContinuityRule(engaged_pairs=set())
    from kicad_tools.validate.rules.diffpair_uncoupled_length import net_component_counts

    components = net_component_counts(pcb)
    out: list[dict] = []
    for d in sorted(detect_pairs(pcb, net_class_map), key=lambda d: d.pair.positive.net_name):
        pair = d.pair
        pid, nid = pair.positive.net_id, pair.negative.net_id
        key = (pid, nid) if pid <= nid else (nid, pid)
        if key not in engaged:
            continue
        p_segs, n_segs = segs.get(pid, []), segs.get(nid, [])
        p_tot = sum(_segment_length(s) for s in p_segs)
        n_tot = sum(_segment_length(s) for s in n_segs)
        p_cpl = rule._coupled_length(p_segs, n_segs)
        n_cpl = rule._coupled_length(n_segs, p_segs)
        gaps: list[float] = []
        for ps in p_segs:
            best = None
            for ns in n_segs:
                if (
                    _segment_coupled_overlap(
                        ps, ns, DEFAULT_COUPLING_WINDOW_MM, DEFAULT_PARALLEL_TOLERANCE_DEG
                    )
                    > 0
                ):
                    gap, _x, _y = _segment_segment_clearance(ps, ns)
                    best = gap if best is None else min(best, gap)
            if best is not None:
                gaps.append(best)
        if p_tot > 0 and n_tot > 0:
            frac = (min(p_cpl / p_tot, 1.0) + min(n_cpl / n_tot, 1.0)) / 2.0
        else:
            frac = None
        thr = resolve_pair_thresholds(net_class_map, pair.positive.net_name, pair.negative.net_name)
        name = pair.name or pair.positive.net_name
        pname, nname = pair.positive.net_name, pair.negative.net_name
        out.append(
            {
                "pair": name,
                "nets": [pname, nname],
                "outcome": log["outcomes"].get(name),
                "segment_outcomes": log["segments"].get(name),
                "routed_mm": [round(p_tot, 3), round(n_tot, 3)],
                "skew_mm": round(skew_data[(pname, nname)], 3)
                if (pname, nname) in skew_data
                else None,
                "chain_skew_mm": None,
                "uncoupled_mm": round((p_tot - p_cpl) + (n_tot - n_cpl), 3),
                "vias_p": vias.get(pid, 0),
                "vias_n": vias.get(nid, 0),
                "gap_min_mm": round(min(gaps), 4) if gaps else None,
                "gap_max_mm": round(max(gaps), 4) if gaps else None,
                "coupled_trunk_mm": round(p_cpl, 3),
                "continuity_pct": round(frac * 100, 1) if frac is not None else None,
                "continuity_threshold_pct": round(thr.continuity * 100, 1),
                "skew_tolerance_mm": round(thr.skew_mm, 3),
                "log_threshold": log["log_thresholds"].get(f"{pname}/{nname}"),
            }
        )
        # Issue #6443: where the limit came from, what a breach costs, and the
        # P5 stretch figure (reported, never a gate).
        row = out[-1]
        skew = row["skew_mm"]
        row.update(
            {
                "protocol": thr.protocol,
                "skew_source": thr.skew_source,
                "skew_severity": thr.skew_severity,
                "skew_pass": None if skew is None else skew <= thr.skew_mm + 1e-6,
                "skew_stretch_mm": thr.skew_stretch_mm,
                "skew_stretch_pass": None if skew is None else skew <= thr.skew_stretch_mm + 1e-6,
                "max_uncoupled_per_terminal_mm": thr.max_uncoupled_per_terminal_mm,
                "max_vias_per_net": thr.max_vias_per_net,
                # Continuity is a measurement since #6443, never a verdict.
                "continuity_label": "info",
            }
        )
        row.update(_uncoupled_columns(pcb, pid, nid, pname, nname, thr, skew, components))
    return out


def _uncoupled_columns(pcb, pid, nid, pname, nname, thr, skew, components) -> dict:
    """The #6443 columns of one pair: per-net uncoupled length, limits, kct verdicts.

    ``uncoupled_net_mm`` is each net's length beside no partner copper and
    ``uncoupled_kicad_mm`` the larger of the two -- the figure KiCad's
    ``diff_pair_uncoupled`` compares (``validate/rules/diffpair_uncoupled_length.py``).
    A pair with an unrouted half ("coupled outcome without copper", P0) is
    ``unrouted``: it has no uncoupled length, only no copper.  ``kct_verdict``
    is ``pass`` / ``fail`` / ``warn`` (a breach of an ``uncited`` limit) /
    ``None`` (nothing to judge).
    """
    from kicad_tools.validate.rules.diffpair_uncoupled_length import (
        measure_pair,
        uncoupled_limit,
    )

    m = measure_pair(pcb, pid, nid, pname, nname, components)
    if not m.routed:
        return {
            "unrouted": True,
            "uncoupled_net_mm": None,
            "uncoupled_kicad_mm": None,
            "components": m.components,
            "max_uncoupled_total_mm": None,
            "uncoupled_limit_source": None,
            "uncoupled_severity": thr.uncoupled_severity,
            "uncoupled_pass": None,
            "vias_pass": None,
            "vias_severity": thr.vias_severity,
            "kct_verdict": {"skew": None, "uncoupled": None, "vias": None},
        }
    limit, how = uncoupled_limit(thr, m.components)
    unc_pass = None if limit is None else m.uncoupled_mm <= limit + 1e-6
    cap = thr.max_vias_per_net
    vias_pass = None if cap is None else max(m.vias_p, m.vias_n) <= cap
    skew_pass = None if skew is None else skew <= thr.skew_mm + 1e-6

    def verdict(ok, severity):
        return (
            None if ok is None else ("pass" if ok else ("fail" if severity == "error" else "warn"))
        )

    return {
        "unrouted": False,
        "uncoupled_net_mm": [round(m.p_uncoupled_mm, 3), round(m.n_uncoupled_mm, 3)],
        "uncoupled_kicad_mm": round(m.uncoupled_mm, 3),
        "components": m.components,
        "max_uncoupled_total_mm": None if limit is None else round(limit, 3),
        "uncoupled_limit_source": how,
        "uncoupled_severity": thr.uncoupled_severity,
        "uncoupled_pass": unc_pass,
        "vias_pass": vias_pass,
        "vias_severity": thr.vias_severity,
        "kct_verdict": {
            "skew": verdict(skew_pass, thr.skew_severity),
            "uncoupled": verdict(unc_pass, thr.uncoupled_severity),
            "vias": verdict(vias_pass, thr.vias_severity),
        },
    }


#: kicad-cli violation type -> the rule the verdict is filed under.
_KICAD_PAIR_TYPES = {
    "skew_out_of_range": "skew",
    "diff_pair_uncoupled_length_too_long": "uncoupled",
    "too_many_vias": "vias",
    "diff_pair_gap_out_of_range": "gap",
}


def kicad_pair_verdicts(routed: Path, pairs: list[dict], timeout: int = 600) -> None:
    """Add ``kicad_cli_verdict`` to every pair row from one kicad-cli DRC pass.

    Per rule, ``fail`` when kicad-cli reports an error on either net of the
    pair, ``warn`` for a warning-severity report, else ``pass``.  A rule the
    ``.kicad_dru`` does not carry for the pair (limit absent, or a pair KiCad
    does not recognise by name) stays ``None``: kicad-cli did not judge it.
    Rows get ``None`` for every rule when kicad-cli cannot run.
    """
    import subprocess
    import tempfile

    from kicad_tools.cli.runner import find_kicad_cli

    exe = find_kicad_cli()
    found: dict[tuple[str, str], str] | None = None
    if exe is not None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "drc.json"
            subprocess.run(
                [
                    str(exe),
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--severity-all",
                    "-o",
                    str(report),
                    str(routed),
                ],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
            if report.is_file():
                found = {}
                for v in json.loads(report.read_text()).get("violations", []):
                    rule = _KICAD_PAIR_TYPES.get(v.get("type"))
                    if rule is None:
                        continue
                    for item in v.get("items", []):
                        net = item.get("description", "").rsplit("[", 1)[-1].split("]", 1)[0]
                        key = (net, rule)
                        # an error outranks a warning
                        if found.get(key) != "fail":
                            found[key] = "fail" if v.get("severity") == "error" else "warn"
    dru = routed.with_suffix(".kicad_dru")
    dru_text = dru.read_text() if dru.is_file() else ""
    for p in pairs:
        if found is None or p.get("unrouted"):
            p["kicad_cli_verdict"] = {"skew": None, "uncoupled": None, "vias": None, "gap": None}
            continue
        names = p["nets"]
        verdict = {}
        for rule, tag in (
            ("skew", "kct diff pair skew - "),
            ("uncoupled", "kct diff pair uncoupled - "),
            ("vias", "kct diff pair vias - "),
            ("gap", "kct diff pair gap - "),
        ):
            judged = any(f'"{tag}{a}/{b}"' in dru_text for a, b in (names, names[::-1]))
            hits = [found.get((n, rule)) for n in names]
            verdict[rule] = (
                None
                if not judged
                else ("fail" if "fail" in hits else ("warn" if "warn" in hits else "pass"))
            )
        p["kicad_cli_verdict"] = verdict


#: Keys that describe a pair's limits, not its copper.  They stay out of
#: :func:`pair_digest` so a rule change alone never reads as a routing change.
_LIMIT_KEYS = frozenset(
    {
        "log_threshold",
        "protocol",
        "skew_source",
        "skew_severity",
        "skew_pass",
        "skew_stretch_mm",
        "skew_stretch_pass",
        "max_uncoupled_per_terminal_mm",
        "max_vias_per_net",
        # Issue #6443 columns: derived from the same copper, kept out so a
        # baseline file's digest still matches the same copper.
        "continuity_label",
        "unrouted",
        "uncoupled_net_mm",
        "uncoupled_kicad_mm",
        "components",
        "max_uncoupled_total_mm",
        "uncoupled_limit_source",
        "uncoupled_severity",
        "uncoupled_pass",
        "vias_pass",
        "vias_severity",
        "kct_verdict",
        "kicad_cli_verdict",
    }
)


def pair_digest(pairs: list[dict]) -> str:
    """Hash of the pair metrics -- what two runs must share to be 'identical'."""
    keep = [{k: v for k, v in p.items() if k not in _LIMIT_KEYS} for p in pairs]
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_cell(
    board: ere.Board,
    engine: str,
    seed: int,
    timeout: float,
    work_dir: Path,
    rep: int = 0,
    router_src: Path | None = None,
    router_rev: str | None = None,
    committed_map: bool = True,
    extra_args: Sequence[str] = (),
) -> dict:
    """Route one (board, engine, seed) cell and measure it. Never raises on a bad row."""
    run_dir = work_dir / f"{board.key}-{engine}-s{seed}" / f"r{rep}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    row: dict = {
        "board": board.key,
        "engine": engine,
        "seed": seed,
        "rep": rep,
        "git_sha": git_sha(),
        # ``committed``: kct route gets the board's committed net_class_map.json (the
        # documented protocol). ``inferred``: no --net-class-map, the classifier's map --
        # how the #6440 doctor ran board 06.
        "net_class_map_mode": "committed" if committed_map else "inferred",
    }
    invalid: list[str] = []
    try:
        inp, facts = prepare_input(board, run_dir, committed_map)
        row.update(facts)
    except PairBenchError as exc:
        row["error"] = str(exc)
        row["valid"] = False
        row["invalid_reasons"] = ["input has pair-net copper"]
        return row
    ncm_path = REPO / board.net_class_map if (board.net_class_map and committed_map) else None
    extra = [*board.kct_args, *engine_args(engine, board.layers)]
    if ncm_path is not None:
        extra += ["--net-class-map", str(ncm_path)]
    extra += ["--seed", str(seed)]
    # Issue #6475: ``run --extra`` was checked but never passed to ``kct route``.
    extra += list(extra_args)
    try:
        check_no_replay(extra)
    except PairBenchError as exc:
        row["error"] = str(exc)
        row["valid"] = False
        row["invalid_reasons"] = ["replay flag"]
        return row
    extra = ere.with_deterministic_rescue(extra)
    row["deterministic_rescue"] = "--deterministic-rescue" in extra
    routed = run_dir / f"{inp.stem}_routed.kicad_pcb"
    log = run_dir / "route.log"
    # ``router_src``: route with another checkout's ``src/`` (``git archive`` of the
    # commit under test) while this script's own measurement stays on the current
    # tree, so main and a PR head are routed by exactly their own code.
    saved_pp = os.environ.get("PYTHONPATH")
    if router_src is not None:
        os.environ["PYTHONPATH"] = str(router_src) + (os.pathsep + saved_pp if saved_pp else "")
        row["router_src"] = str(router_src)
        row["router_rev"] = router_rev
    try:
        row.update(ere.run_route(inp, routed, extra, timeout, log))
    finally:
        if router_src is None:
            pass
        elif saved_pp is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = saved_pp
    log_text = log.read_text(errors="replace")
    row.update(ere.parse_route_log(log_text))
    row.update(ere.timeout_sidecar_facts(routed))
    artifact, kind = ere.pick_artifact(routed, row.get("exit_code"))
    if artifact is None:
        row["error"] = f"no routed output (exit_code={row['exit_code']})"
        row["valid"] = False
        row["invalid_reasons"] = ["no routed output"]
        return row
    row["routed"] = str(artifact)
    row["artifact"] = kind
    # Issue #6475: exit 1 (e.g. a refused save, #6351) and 5 leave the last
    # periodic checkpoint at ``-o``; grading it is not grading a finished route.
    if row.get("exit_code") in CRASHED_EXIT_CODES:
        invalid.append(
            f"route exited {row['exit_code']}; the output is a leftover checkpoint, not a result"
        )
    row.update(ere.grade(artifact, board))
    row["manufacturable"] = ere.manufacturable(row)
    if ncm_path is None:
        ncm_path = ere.find_net_class_map(artifact)  # the router-written sidecar
    ncm = load_net_class_map(ncm_path, pcb_path=artifact)
    pairs = measure_pairs(artifact, ncm, board.layers, log_text)
    kicad_pair_verdicts(artifact, pairs)
    row["pairs"] = pairs
    row["pair_digest"] = pair_digest(pairs)
    row["nets_routed"] = f"{row.get('nets_complete')}/{row.get('nets_total')}"
    row["kct_errors_by_rule"] = row.get("kct_check_by_rule")
    row["kicad_cli_errors_by_type"] = row.get("kicad_drc_by_type")
    nets = set(row["pair_nets"])
    if _REPLAY_LOG.search(log_text):
        invalid.append("route log shows a saved-plan / cache replay")
    match = committed_copper_matches(artifact, board, nets)
    if match:
        invalid.append(f"pair copper identical to committed {match}")
    row["valid"] = not invalid
    row["invalid_reasons"] = invalid
    return row


def render(rows: list[dict]) -> str:
    head = (
        "| Board | Engine | Seed | Map | Valid | Pairs coupled | Nets | kct err (by rule) | "
        "kicad-cli err (by type) | unconn | Manufacturable | Wall s |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    out = [head]
    for r in rows:
        pairs = r.get("pairs") or []
        coupled = sum(1 for p in pairs if (p.get("outcome") or "").startswith("coupled"))
        out.append(
            f"| {r['board']} | {r['engine']} | {r['seed']} | "
            f"{r.get('net_class_map_mode', 'committed')} | {r.get('valid')} | "
            f"{coupled}/{len(pairs)} | {r.get('nets_routed', '-')} | "
            f"{r.get('kct_errors_by_rule') or '-'} | {r.get('kicad_cli_errors_by_type') or '-'} | "
            f"{r.get('kicad_drc_unconnected', '-')} | {r.get('manufacturable')} | {r.get('wall_s', '-')} |"
        )
    return "\n".join(out)


def render_pairs(rows: list[dict]) -> str:
    """One line per pair: skew and uncoupled length against their limits, both engines' verdicts.

    ``Unc. P/N`` is each net's uncoupled length (KiCad's per-net figure; the
    pair's figure is the larger), ``Limit`` its derived limit.  The verdict
    cells read ``kct/kicad-cli`` per rule: ``pass``, ``FAIL`` (error), ``warn``
    (a breach of an ``uncited`` limit) or ``-`` (not judged).  Continuity is
    info only and is not shown as a verdict.
    """
    out = [
        "| Board | Engine | Seed | Pair | Outcome | Routed P/N mm | Skew mm | Limit mm | Source | "
        "Severity | Skew | Stretch (mm) | Unc. P/N mm | Unc. limit mm | Vias P/N | "
        "Skew kct/cli | Unc. kct/cli | Vias kct/cli |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    ]

    def verdict(value) -> str:
        return "-" if value is None else ("pass" if value else "FAIL")

    def cell(v) -> str:
        return "-" if v is None else {"fail": "FAIL"}.get(v, v)

    def both(p: dict, rule: str) -> str:
        return f"{cell((p.get('kct_verdict') or {}).get(rule))}/{cell((p.get('kicad_cli_verdict') or {}).get(rule))}"

    for r in rows:
        for p in r.get("pairs") or []:
            routed = p.get("routed_mm") or ["-", "-"]
            skew = p.get("skew_mm")
            stretch = p.get("skew_stretch_mm")
            stretch_cell = (
                "-" if stretch is None else f"{verdict(p.get('skew_stretch_pass'))} ({stretch})"
            )
            net_unc = p.get("uncoupled_net_mm")
            unc_cell = (
                "unrouted"
                if p.get("unrouted")
                else ("-" if net_unc is None else f"{net_unc[0]}/{net_unc[1]}")
            )
            out.append(
                f"| {r['board']} | {r['engine']} | {r['seed']} | {p['pair']} | "
                f"{p.get('outcome') or '-'} | {routed[0]}/{routed[1]} | "
                f"{'-' if skew is None else skew} | {p.get('skew_tolerance_mm')} | "
                f"{p.get('skew_source', '-')} | {p.get('skew_severity', '-')} | "
                f"{verdict(p.get('skew_pass'))} | "
                f"{stretch_cell} | "
                f"{unc_cell} | {p.get('max_uncoupled_total_mm') or '-'} | "
                f"{p.get('vias_p')}/{p.get('vias_n')} | "
                f"{both(p, 'skew')} | {both(p, 'uncoupled')} | {both(p, 'vias')} |"
            )
    return "\n".join(out)


def _cmd_run(args) -> int:
    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    for key in boards:
        if key not in ere.BOARDS:
            raise SystemExit(f"unknown board {key!r}")
    for e in engines:
        if e not in ENGINES:
            raise SystemExit(f"unknown engine {e!r}")
    check_no_replay(args.extra.split())
    sha = args.router_rev[:8] if args.router_rev else git_sha()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.label}-{sha}.json"
    rows: list[dict] = []
    for key in boards:
        board = ere.BOARDS[key]
        for engine in engines:
            for seed in seeds:
                for rep in range(args.rep_start, args.rep_start + args.repeat):
                    print(f"==> {key} {engine} seed {seed} rep {rep}", flush=True)
                    rows.append(
                        run_cell(
                            board,
                            engine,
                            seed,
                            args.timeout,
                            args.work_dir,
                            rep,
                            args.router_src,
                            args.router_rev,
                            not args.inferred_map,
                            args.extra.split(),
                        )
                    )
                    out_path.write_text(
                        json.dumps(
                            {
                                "label": args.label,
                                "git_sha": sha,
                                "timeout_s": args.timeout,
                                "rows": rows,
                            },
                            indent=2,
                        )
                        + "\n"
                    )
    print(render(rows))
    print(f"wrote {out_path}")
    return 0 if all(r.get("valid") for r in rows) else 1


def _key(row: dict) -> tuple:
    return (row["board"], row["engine"], row["seed"], row.get("net_class_map_mode", "committed"))


def _label(key: tuple) -> str:
    board, engine, seed, mode = key
    return f"{board}/{engine}/s{seed}/{mode}"


def compare(a: dict, b: dict) -> list[tuple[str, bool]]:
    """Per (board, engine, seed, map mode): do two result files agree on the pair metrics?"""

    def index(doc):
        return {_key(r): r.get("pair_digest") for r in doc["rows"] if r.get("rep", 0) == 0}

    ia, ib = index(a), index(b)
    return [
        (_label(k), ia[k] is not None and ia[k] == ib[k]) for k in sorted(ia.keys() & ib.keys())
    ]


def determinism(doc: dict) -> list[tuple[str, int, bool]]:
    """Cells run more than once in one file: ``(cell, runs, all digests identical)``."""
    digests: dict[tuple, list] = {}
    for r in doc["rows"]:
        digests.setdefault(_key(r), []).append(r.get("pair_digest"))
    return [
        (_label(k), len(v), None not in v and len(set(v)) == 1)
        for k, v in sorted(digests.items())
        if len(v) > 1
    ]


def merge(docs: list[dict]) -> dict:
    """Rows of several result files in one; a later file replaces an earlier cell."""
    rows: dict[tuple, dict] = {}
    for doc in docs:
        for r in doc["rows"]:
            rows[(*_key(r), r.get("rep", 0))] = r
    out = dict(docs[-1])
    out["rows"] = [rows[k] for k in sorted(rows)]
    return out


def _cmd_compare(args) -> int:
    results = compare(json.loads(args.a.read_text()), json.loads(args.b.read_text()))
    for name, same in results:
        print(f"{name}: {'identical' if same else 'DIFFERENT'}")
    return 0 if results and all(s for _, s in results) else 1


def _cmd_determinism(args) -> int:
    results = determinism(json.loads(args.file.read_text()))
    for name, runs, same in results:
        print(f"{name}: {runs} runs {'identical' if same else 'DIFFERENT'}")
    return 0 if results and all(s for _, _, s in results) else 1


def remeasure(doc: dict) -> dict:
    """Recompute ``pairs`` / ``pair_digest`` of every row from its routed board and log."""
    for row in doc["rows"]:
        routed = Path(row.get("routed", ""))
        if not routed.is_file():
            continue
        board = ere.BOARDS[row["board"]]
        committed = row.get("net_class_map_mode", "committed") == "committed"
        ncm_path = REPO / board.net_class_map if (board.net_class_map and committed) else None
        if ncm_path is None:
            ncm_path = ere.find_net_class_map(routed)
        ncm = load_net_class_map(ncm_path, pcb_path=routed)
        log = routed.parent / "route.log"
        text = log.read_text(errors="replace") if log.is_file() else ""
        row["pairs"] = measure_pairs(routed, ncm, board.layers, text)
        kicad_pair_verdicts(routed, row["pairs"])
        row["pair_digest"] = pair_digest(row["pairs"])
    return doc


def _cmd_remeasure(args) -> int:
    doc = remeasure(json.loads(args.file.read_text()))
    args.file.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"remeasured {len(doc['rows'])} rows in {args.file}")
    return 0


def _cmd_summary(args) -> int:
    for path in args.files:
        doc = json.loads(path.read_text())
        print(f"### {path.name}\n")
        rows = [r for r in doc["rows"] if r.get("rep", 0) == 0]
        print(render(rows))
        print()
        if args.pairs:
            print(render_pairs(rows))
            print()
    return 0


def _cmd_merge(args) -> int:
    merged = merge([json.loads(p.read_text()) for p in args.files])
    args.out.write_text(json.dumps(merged, indent=2) + "\n")
    print(f"wrote {args.out} ({len(merged['rows'])} rows)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="route and measure the benchmark cells")
    run.add_argument("--boards", default="03,06b,06rf")
    run.add_argument("--engines", default=",".join(ENGINES))
    run.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    run.add_argument("--timeout", type=float, default=600.0)
    run.add_argument("--repeat", type=int, default=1, help="runs per cell (determinism check: 2)")
    run.add_argument("--rep-start", type=int, default=0, help="first rep index (resume a repeat)")
    run.add_argument("--label", required=True, help="e.g. baseline-main")
    run.add_argument("--work-dir", type=Path, required=True)
    run.add_argument("--out-dir", type=Path, default=REPO / "data" / "research" / "diffpair_bench")
    run.add_argument("--extra", default="", help="(refused if it replays a plan)")
    run.add_argument(
        "--router-src",
        type=Path,
        help="a `src/` dir (git archive of the commit under test) to route with",
    )
    run.add_argument("--router-rev", help="commit the --router-src archive was taken from")
    run.add_argument(
        "--inferred-map",
        action="store_true",
        help="do not pass the committed net_class_map.json to kct route (the classifier's "
        "map: how the #6440 doctor ran board 06)",
    )
    run.set_defaults(func=_cmd_run)
    det = sub.add_parser(
        "determinism", help="did repeated runs of a cell give identical pair metrics?"
    )
    det.add_argument("file", type=Path)
    det.set_defaults(func=_cmd_determinism)
    rem = sub.add_parser("remeasure", help="recompute pair metrics from the routed boards")
    rem.add_argument("file", type=Path)
    rem.set_defaults(func=_cmd_remeasure)
    summ = sub.add_parser("summary", help="markdown table of one or more result files")
    summ.add_argument("files", type=Path, nargs="+")
    summ.add_argument(
        "--pairs",
        action="store_true",
        help="also print one line per pair: skew, limit, the limit's source, stretch figure",
    )
    summ.set_defaults(func=_cmd_summary)
    mrg = sub.add_parser("merge", help="combine result files (later files replace earlier cells)")
    mrg.add_argument("files", type=Path, nargs="+")
    mrg.add_argument("--out", type=Path, required=True)
    mrg.set_defaults(func=_cmd_merge)
    cmp_ = sub.add_parser("compare", help="are two result files pair-for-pair identical?")
    cmp_.add_argument("a", type=Path)
    cmp_.add_argument("b", type=Path)
    cmp_.set_defaults(func=_cmd_compare)
    # `pairs` invoked through expand_route_experiment passes bare run options.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] not in (
        "run",
        "compare",
        "determinism",
        "merge",
        "remeasure",
        "summary",
        "-h",
        "--help",
    ):
        raw = ["run", *raw]
    args = ap.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
