#!/usr/bin/env python3
"""Head-to-head benchmark: kicad-tools (``kct route``) vs KiCadRoutingTools (KRT).

Issues #5781 and #5790. Reproduces the benchmark table in
``docs/research/kicad-routing-tools-comparison.md``.

``--boards`` defaults to **every** key in ``BOARDS`` below, which is the exact
set the doc reports, so the doc's reproduce command (which passes no
``--boards``) and the measured board set cannot drift apart (#5790).

Research-only: this script is NOT wired into CI and KRT is NOT a dependency.
It drives an out-of-tree KRT checkout (``--krt-dir``) and this repo's ``kct``
on *copies* of the committed unrouted board inputs inside ``--work-dir``; it
never writes under ``boards/``.

Fairness rules the script enforces
----------------------------------

* Each tool runs its **documented default recipe** (see ``BOARDS`` below and
  the doc's "Recipes" section); nothing is tuned per board for either side.
* **One shared referee.** Every output (and the committed ``*_routed``
  reference) is scored by ``kicad-cli pcb drc --refill-zones`` against the
  board's ORIGINAL input ``.kicad_pro`` copied next to it, with no
  ``.kicad_dru``. Both tools rewrite the project file they emit (KRT lowers
  ``rules.min_*`` floors to what it routed at; kct emits a fab ``.kicad_pro`` +
  ``.kicad_dru``), so grading each output against its own emitted project would
  let each tool move its own goalposts. The "as-emitted" error count is
  reported alongside, for information only.
* Completion, via count and wirelength are re-measured from the refilled
  board file with this repo's external-benchmark harness
  (``kicad_tools.benchmark.external.metrics``), never taken from either
  tool's own log.
* Where a board declares length-match groups, **both** tools are handed the
  same groups *and the same declared skew budget* from the board's own
  sidecar, and the per-group length spread is measured the same way on both
  outputs (``measure_group_spread``).
* A run killed at ``--timeout`` is recorded and printed as a TIMEOUT, even
  when the tool left a partial board behind that could still be scored.

Usage (from a worktree with the C++ backend built)::

    uv run python scripts/research/krt_compare.py \\
        --krt-dir /path/to/KiCadRoutingTools --work-dir /tmp/krt-bench

``--krt-python`` defaults to ``<krt-dir>/.venv/bin/python`` (a venv with KRT's
``requirements.txt`` installed; ``python build_router.py`` run once).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Board:
    key: str
    src: str  # repo-relative unrouted .kicad_pcb (sibling .kicad_pro used)
    routed_ref: str  # repo-relative committed routed artifact (reference row)
    kct_args: tuple[str, ...] = ()
    # KRT steps: list of (script, extra args); each step's output feeds the next.
    # A step whose extra args contain the literal token ``{out}`` receives its
    # output path THERE (for a CLI like ``qfn_fanout.py`` that takes ``-o``);
    # otherwise the output is appended as the second positional argument.
    krt_steps: tuple[tuple[str, tuple[str, ...]], ...] = (("py_router/route.py", ()),)
    strip_nets: str | None = None  # regex of nets whose copper is ripped first
    layers: int = 2  # copper layers (for the secondary kct-check gate)
    note: str = ""
    # Which tools this row is defined for. A recipe variant that only changes
    # one tool's invocation (04f, 07m) declares just that tool, so the shared
    # rows are not re-measured and re-printed under a second key.
    tools: tuple[str, ...] = ("kct", "krt", "ref")
    # Board key whose kct run supplies the fab referee, when this variant does
    # not run kct itself (04f). Same source board => same emitted fab project.
    fab_from: str | None = None
    # repo-relative JSON declaring length-match groups as {"groups": {name: [nets]}}.
    # When set, every output is additionally scored for per-group length spread.
    match_groups: str | None = None


#: Board 07's own declared match groups (4 groups, 5 mm skew budget), written by
#: its generator. Used BOTH to build KRT's --length-match-group flags and to
#: score per-group length spread, so neither tool is told a different grouping.
SDRAM_GROUPS_JSON = "boards/07-matchgroup-test/output/sdram_constraints.json"


def load_match_groups(rel_path: str) -> dict[str, list[str]]:
    """Read ``{"groups": {name: [net, ...]}}`` from a repo-relative JSON file."""
    return json.loads((REPO / rel_path).read_text())["groups"]


def _krt_length_match_flags(*extra: str) -> tuple[str, ...]:
    """One ``--length-match-group <nets...>`` flag per declared group.

    Also passes the board's OWN declared skew budget (``group_skew_mm``) as
    ``--length-match-tolerance``. That is not per-board tuning of KRT: it is the
    same constraint kct reads from its net-class-map sidecar
    (``length_match_tolerance_mm``), so both tools are matched against one
    number the board itself states. KRT's unqualified default is 0.1 mm, which
    would hold it to a budget kct was never asked to meet.
    """
    spec = json.loads((REPO / SDRAM_GROUPS_JSON).read_text())
    flags: list[str] = []
    for members in spec["groups"].values():
        flags.append("--length-match-group")
        flags.extend(members)
    flags += ["--length-match-tolerance", str(spec["group_skew_mm"])]
    flags += extra
    return tuple(flags)


BOARDS: dict[str, Board] = {
    "00": Board(
        "00",
        "boards/00-simple-led/output/simple_led.kicad_pcb",
        "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
    ),
    "01": Board(
        "01",
        "boards/01-voltage-divider/output/voltage_divider.kicad_pcb",
        "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb",
    ),
    "02": Board(
        "02",
        "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb",
        "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb",
    ),
    "03": Board(
        "03",
        "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
        "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
        layers=4,
    ),
    # 04, 05 and 07 were deferred by #5781 and are measured here from #5790.
    # Every key below is part of the default `--boards` set (`",".join(BOARDS)`),
    # which is what keeps the doc's documented reproduce command -- which passes
    # no `--boards` -- equal to the measured board set by construction.
    "04": Board(
        "04",
        "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
        "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
        note="2 layers, no zones; LQFP-48 0.5 mm pitch",
    ),
    # 04f: the fanout row. KRT's documented QFN/QFP escape recipe is a separate
    # CLI run before route.py; kct has no fanout CLI -- its escape router runs
    # inside `kct route` and is auto-enabled for dense packages -- so the kct
    # and ref rows for this board are 04's, and only KRT is re-run here.
    "04f": Board(
        "04f",
        "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
        "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
        krt_steps=(
            ("py_router/qfn_fanout.py", ("--component", "U2", "-o", "{out}")),
            ("py_router/route.py", ()),
        ),
        note="KRT QFP fanout (U2) before route.py; compare against 04 kct/ref",
        tools=("krt",),
        fab_from="04",
    ),
    "05": Board(
        "05",
        "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb",
        "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb",
        note="4 layers, 4 zones; Kelvin/ISENSE current-sense returns",
        layers=4,
    ),
    # 05f: the second fanout row -- same KRT escape CLI as 04f, but on a
    # 4-layer board with zones and a TQFP-32 (U1). Two points, not one, is
    # what makes 04f's result readable as "the escape pre-pass helps KRT"
    # rather than "board 04 is odd". KRT only, for the same reason as 04f.
    "05f": Board(
        "05f",
        "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb",
        "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb",
        krt_steps=(
            ("py_router/qfn_fanout.py", ("--component", "U1", "-o", "{out}")),
            ("py_router/route.py", ()),
        ),
        note="KRT QFP fanout (U1) before route.py; compare against 05 kct/ref",
        layers=4,
        tools=("krt",),
        fab_from="05",
    ),
    # 06a: the board's real input -- the 4 LVDS pairs arrive PRE-ROUTED by the
    # generator (plus GND/+3V3 plane vias); only the 8 LVTTL nets are open.
    # Both tools must keep existing copper: KRT does by default; kct needs
    # --preserve-existing (its documented incremental-routing flag).
    "06a": Board(
        "06a",
        "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
        kct_args=("--preserve-existing",),
        note="LVDS pairs pre-routed in input; routes the 8 LVTTL nets",
        layers=4,
    ),
    # 06b: the diff-pair test proper -- LVDS copper ripped, plane vias kept.
    # Each tool's documented diff-pair recipe: kct --differential-pairs;
    # KRT route_diff.py then route.py.
    "06b": Board(
        "06b",
        "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
        kct_args=("--preserve-existing", "--differential-pairs"),
        krt_steps=(("py_router/route_diff.py", ("--nets", "LVDS*")), ("py_router/route.py", ())),
        strip_nets=r"LVDS\d_[PN]",
        note="LVDS copper ripped; routes 4 pairs + 8 LVTTL nets",
        layers=4,
    ),
    # 07a: the SDRAM exerciser's real input. 119/161 connections arrive routed
    # from the reviewed source, so -- like 06a -- this is the incremental case:
    # KRT keeps existing copper by default, kct needs --preserve-existing.
    "07a": Board(
        "07a",
        "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
        "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb",
        kct_args=("--preserve-existing",),
        note="6 layers; 119/161 connections pre-routed in the reviewed source",
        layers=6,
        match_groups=SDRAM_GROUPS_JSON,
    ),
    # 07m: the length-matching row. Both tools match lengths only on request,
    # and each is handed the SAME four groups the board itself declares
    # (output/sdram_constraints.json, 5 mm skew budget): kct through its
    # committed net-class-map sidecar, KRT through one --length-match-group
    # flag per group. The ref row is 07a's, so only the two tools re-run.
    "07m": Board(
        "07m",
        "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
        "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb",
        kct_args=(
            "--preserve-existing",
            "--length-match-groups",
            "--net-class-map",
            "boards/07-matchgroup-test/output/net_class_map.json",
        ),
        krt_steps=(("py_router/route.py", _krt_length_match_flags()),),
        note="length matching on: kct --length-match-groups, KRT --length-match-group",
        layers=6,
        tools=("kct", "krt"),
        match_groups=SDRAM_GROUPS_JSON,
    ),
    # 07t: KRT's TIME matching (--time-matching) on the same four groups. kct
    # has no propagation-delay matcher at all, so there is nothing to run on
    # our side; this row exists to say what KRT's time-domain mode does to a
    # board we also measure in the length domain (07m), not to compare tools.
    "07t": Board(
        "07t",
        "boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb",
        "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb",
        krt_steps=(("py_router/route.py", _krt_length_match_flags("--time-matching")),),
        note="KRT --time-matching on the same groups; kct has no time-domain matcher",
        layers=6,
        tools=("krt",),
        fab_from="07a",
        match_groups=SDRAM_GROUPS_JSON,
    ),
}


# ---------------------------------------------------------------------------
# Input preparation
# ---------------------------------------------------------------------------


def _iter_top_blocks(text: str, head: str):
    """Yield (start, end) spans of balanced ``(head ...)`` s-expressions."""
    for m in re.finditer(r"\(" + head + r"\b", text):
        depth, i = 0, m.start()
        in_str = False
        while i < len(text):
            c = text[i]
            if c == '"' and text[i - 1] != "\\":
                in_str = not in_str
            elif not in_str:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        yield m.start(), i + 1
                        break
            i += 1


def strip_net_copper(text: str, net_re: str) -> tuple[str, int]:
    """Remove segment/arc/via blocks whose net name matches ``net_re``."""
    pat = re.compile(net_re)
    names = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
    spans = []
    for head in ("segment", "arc", "via"):
        for s, e in _iter_top_blocks(text, head):
            blk = text[s:e]
            m = re.search(r'\(net (\d+)\)|\(net "([^"]*)"\)', blk)
            if not m:
                continue
            name = names.get(m.group(1), "") if m.group(1) else m.group(2)
            if pat.fullmatch(name):
                spans.append((s, e))
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + text[e:]
    return text, len(spans)


def prepare_input(board: Board, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = REPO / board.src
    stem = src.stem
    dst = dest_dir / f"{stem}.kicad_pcb"
    text = src.read_text()
    if board.strip_nets:
        text, n = strip_net_copper(text, board.strip_nets)
        print(f"  [{board.key}] stripped {n} copper items matching {board.strip_nets}")
    dst.write_text(text)
    shutil.copy2(src.with_suffix(".kicad_pro"), dest_dir / f"{stem}.kicad_pro")
    return dst


# ---------------------------------------------------------------------------
# Running the tools
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    tool: str
    board: str
    output: Path | None
    wall_s: float
    exit_codes: list[int] = field(default_factory=list)
    timed_out: bool = False
    commands: list[str] = field(default_factory=list)


def _run(cmd: list[str], cwd: Path, log: Path, timeout: float) -> tuple[int, bool]:
    """Run ``cmd`` under a hard wall-clock cap, killing the WHOLE process tree.

    ``subprocess.run(timeout=...)`` only kills its direct child. Every kct
    invocation here is ``uv run kct route ...``, so the direct child is ``uv``
    and the router itself is a *grandchild*: on timeout ``uv`` died and the
    router was reparented to init and kept running at full CPU, for as long as
    it wanted, straight through every later board in the sweep. That silently
    poisons the runtime column of every row measured after the first timeout
    (observed on this script: a 07a kct run still alive 31 minutes into a
    20-minute cap, contending with the 07a KRT and 07m runs). The fix is to put
    each run in its own process group and signal the group.
    """
    with log.open("a") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        fh.flush()
        p = subprocess.Popen(
            cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            return p.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            fh.write(f"\n!! TIMEOUT after {timeout}s\n")
            _kill_group(p)
            return -1, True


def _group_alive(pgid: int) -> bool:
    """True while any process remains in group ``pgid`` (zombies included)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours to signal
        return True
    return True


def _kill_group(p: subprocess.Popen, term_grace: float = 10.0, kill_grace: float = 15.0) -> None:
    """SIGTERM then SIGKILL the process group ``p`` leads, and BLOCK until it is gone.

    Two properties matter, and neither is free:

    * **The second signal is not conditional on the direct child.** ``uv`` exits
      on SIGTERM while the router grandchild it spawned is still shutting down
      (or ignoring it), which is the leak this function exists to close.
    * **It waits for the group, not for ``p``.** Returning as soon as ``p`` is
      reaped is what let a "killed" run keep burning CPU into the next board's
      measurement. The caller's next row must start on an idle machine, so this
      returns only once the group is actually empty (or ``kill_grace`` expires,
      which is reported rather than hidden).

    ``start_new_session=True`` in ``_run`` makes ``p.pid`` the group id, and the
    group outlives ``p`` while any member remains, so signalling by that number
    stays correct after ``p`` itself has exited.
    """
    pgid = p.pid
    for sig, grace in ((signal.SIGTERM, term_grace), (signal.SIGKILL, kill_grace)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            break
        # Reap p first: a zombie leader keeps the group "alive" for killpg(0).
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=grace)
        deadline = time.monotonic() + grace
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not _group_alive(pgid):
            return
    if _group_alive(pgid):
        print(f"  !! process group {pgid} survived SIGKILL; later runs may contend", flush=True)


def run_kct(board: Board, inp: Path, out_dir: Path, timeout: float) -> RunResult:
    out = out_dir / f"{inp.stem}_routed.kicad_pcb"
    cmd = ["uv", "run", "kct", "route", str(inp), "-o", str(out), *board.kct_args]
    log = out_dir / "run.log"
    t0 = time.monotonic()
    rc, to = _run(cmd, REPO, log, timeout)
    wall = time.monotonic() - t0
    return RunResult(
        "kct", board.key, out if out.exists() else None, wall, [rc], to, [" ".join(cmd)]
    )


def run_krt(
    board: Board, inp: Path, out_dir: Path, timeout: float, krt_dir: Path, krt_python: str
) -> RunResult:
    log = out_dir / "run.log"
    cur = inp
    res = RunResult("krt", board.key, None, 0.0)
    t0 = time.monotonic()
    for i, (script, extra) in enumerate(board.krt_steps):
        nxt = out_dir / f"{inp.stem}_step{i}.kicad_pcb"
        # KRT reads netclasses from the sibling .kicad_pro of its input.
        cur_pro = cur.with_suffix(".kicad_pro")
        if not cur_pro.exists():
            shutil.copy2(inp.with_suffix(".kicad_pro"), cur_pro)
        if any("{out}" in a for a in extra):
            # CLI takes its output through a flag (qfn_fanout.py -o OUT).
            cmd = [krt_python, script, str(cur), *(a.replace("{out}", str(nxt)) for a in extra)]
        else:
            cmd = [krt_python, script, str(cur), str(nxt), *extra]
        remaining = timeout - (time.monotonic() - t0)
        rc, to = _run(cmd, krt_dir, log, max(remaining, 1))
        res.exit_codes.append(rc)
        res.commands.append(" ".join(cmd))
        if to or not nxt.exists():
            res.timed_out = to
            break
        cur = nxt
    res.wall_s = time.monotonic() - t0
    if cur != inp and not res.timed_out:
        final = out_dir / f"{inp.stem}_routed.kicad_pcb"
        shutil.copy2(cur, final)
        # Keep KRT's emitted project (it rewrites rules.min_* floors) with the
        # final board so the informational "as-emitted" grade sees it.
        for suf in (".kicad_pro", ".kicad_dru"):
            if cur.with_suffix(suf).exists():
                shutil.copy2(cur.with_suffix(suf), final.with_suffix(suf))
        res.output = final
    return res


# ---------------------------------------------------------------------------
# Scoring (shared referee)
# ---------------------------------------------------------------------------


def kicad_cli_drc(pcb: Path, report: Path, save_board: bool) -> dict:
    cmd = [
        "kicad-cli",
        "pcb",
        "drc",
        "--refill-zones",
        "--severity-all",
        "--format",
        "json",
        "--units",
        "mm",
        "--output",
        str(report),
    ]
    if save_board:
        cmd.append("--save-board")
    cmd.append(str(pcb))
    subprocess.run(cmd, capture_output=True, timeout=900, check=False)
    data = json.loads(report.read_text())
    errors: Counter[str] = Counter()
    warnings = 0
    for v in data.get("violations", []):
        if v.get("severity") == "error":
            errors[v.get("type", "?")] += 1
        elif v.get("severity") == "warning":
            warnings += 1
    return {
        "errors_total": sum(errors.values()),
        "errors_by_type": dict(sorted(errors.items())),
        "warnings": warnings,
        "unconnected": len(data.get("unconnected_items", [])),
    }


def _seg_point_dist(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def measure_coupling(pcb_path: Path, pitch_mm: float, step: float = 0.05) -> dict:
    """Share of each diff pair's P-leg copper that runs coupled to its N leg.

    Samples every ``step`` mm along the P leg's segments and counts a sample
    as coupled when a same-layer N segment's centerline lies within
    ``1.5 * pitch_mm`` (pitch = declared diff_pair_width + diff_pair_gap).
    Completion alone cannot distinguish a coupled route from two independent
    single-ended legs; this does. Pairs are discovered by ``*_P``/``*_N``.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    by_net: dict[str, list] = {}
    for seg in pcb.segments:
        by_net.setdefault(seg.net_name or "", []).append(seg)
    thresh = 1.5 * pitch_mm
    pairs = {}
    for name in sorted(by_net):
        if not name.endswith("_P") or name[:-2] + "_N" not in by_net:
            continue
        n_segs = by_net[name[:-2] + "_N"]
        total = coupled = 0.0
        for seg in by_net[name]:
            length = math.dist(seg.start, seg.end)
            k = max(1, int(length / step))
            for i in range(k):
                t = (i + 0.5) / k
                pt = (
                    seg.start[0] + t * (seg.end[0] - seg.start[0]),
                    seg.start[1] + t * (seg.end[1] - seg.start[1]),
                )
                total += length / k
                if any(
                    n.layer == seg.layer and _seg_point_dist(pt, n.start, n.end) <= thresh
                    for n in n_segs
                ):
                    coupled += length / k
        pairs[name[:-2]] = round(100 * coupled / total, 1) if total else 0.0
    return pairs


def measure_group_spread(pcb_path: Path, groups: dict[str, list[str]]) -> dict:
    """Per-group routed-length spread (mm) for declared length-match groups.

    Length is 2-D track length per net, summed over segments on the refilled
    board -- **not** including via barrels, and identical for both tools, so
    the spread is comparable even though KRT's own matcher counts barrels.
    ``spread`` is max-minus-min over the group members that carry copper;
    ``members`` reports how many of the declared nets that was.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    length: dict[str, float] = {}
    for seg in pcb.segments:
        name = seg.net_name or ""
        length[name] = length.get(name, 0.0) + math.dist(seg.start, seg.end)
    out: dict[str, dict] = {}
    for group, members in groups.items():
        routed = [length[m] for m in members if length.get(m, 0.0) > 0.0]
        if not routed:
            continue
        out[group] = {
            "min_mm": round(min(routed), 2),
            "max_mm": round(max(routed), 2),
            "spread_mm": round(max(routed) - min(routed), 2),
            "members": f"{len(routed)}/{len(members)}",
        }
    return out


def measure_plane_intrusion(pcb_path: Path) -> float:
    """Track length (mm) of foreign-net copper on INNER layers that carry a zone.

    Boards 03 and 06 declare In1/In2 as GND/PWR reference planes; a signal
    track there cuts the plane. Neither tool is told to reserve those layers
    (both default to "all copper layers"), so this is measured, not enforced.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    plane_nets: dict[str, set[str]] = {}
    for zone in pcb.zones:
        for layer in [zone.layer, *(zone.layers or [])]:
            if layer and layer.startswith("In"):
                plane_nets.setdefault(layer, set()).add(zone.net_name)
    total = 0.0
    for seg in pcb.segments:
        nets = plane_nets.get(seg.layer)
        if nets is not None and seg.net_name not in nets:
            total += math.dist(seg.start, seg.end)
    return round(total, 1)


def _grade_with_project(
    pcb: Path, project_src: Path | None, dru_src: Path | None, grade_dir: Path, save_board: bool
) -> dict:
    """Copy ``pcb`` into ``grade_dir`` with the given project/rules and run the gate."""
    grade_dir.mkdir(parents=True, exist_ok=True)
    stem = "graded"
    ref_pcb = grade_dir / f"{stem}.kicad_pcb"
    shutil.copy2(pcb, ref_pcb)
    if project_src is not None and project_src.exists():
        shutil.copy2(project_src, grade_dir / f"{stem}.kicad_pro")
    if dru_src is not None and dru_src.exists():
        shutil.copy2(dru_src, grade_dir / f"{stem}.kicad_dru")
    return kicad_cli_drc(ref_pcb, grade_dir / "drc.json", save_board=save_board)


def score(
    pcb: Path,
    orig_input: Path,
    score_dir: Path,
    emitted: bool,
    fab_project: Path | None,
    layers: int = 2,
    match_groups: str | None = None,
) -> dict:
    """Grade ``pcb`` under every referee.

    * ``shared_referee`` (HEADLINE): the board's original input ``.kicad_pro``
      -- the designer's declared net classes/rules, with KiCad's built-in
      defaults for any Board Setup floor the project leaves unset. No
      ``.kicad_dru``. The refilled board it saves is what completion, vias
      and wirelength are measured on.
    * ``fab_referee``: kct's emitted JLCPCB-floor project (+``.kicad_dru``) for
      the same board, applied identically to every tool's output -- separates
      "below KiCad's default floors" from "below the fab's capability".
    * ``as_emitted`` (information only): the tool's own emitted project.
    """
    from kicad_tools.benchmark.external.metrics import (
        measure_completion,
        measure_copper,
        run_kct_check,
    )

    if score_dir.exists():
        shutil.rmtree(score_dir)
    shared_dir = score_dir / "shared"
    drc = _grade_with_project(
        pcb, orig_input.with_suffix(".kicad_pro"), None, shared_dir, save_board=True
    )
    graded = shared_dir / "graded.kicad_pcb"
    comp = measure_completion(graded).to_dict()
    cop = measure_copper(graded).to_dict()
    out = {
        "shared_referee": drc,
        "completion": comp,
        "via_count": cop["via_count"],
        "wirelength_mm": cop["wirelength_mm"],
        "plane_layer_signal_mm": measure_plane_intrusion(graded),
        # Secondary gate: this repo's own engine (JLCPCB standard tier). It
        # sees rules kicad-cli does not grade by default, e.g. via-in-pad.
        "kct_check": run_kct_check(graded, manufacturer="jlcpcb", layers=layers).to_dict(),
    }
    pro = json.loads(orig_input.with_suffix(".kicad_pro").read_text())
    default_cls = next(
        (c for c in pro.get("net_settings", {}).get("classes", []) if c.get("name") == "Default"),
        {},
    )
    if default_cls.get("diff_pair_gap") and default_cls.get("diff_pair_width"):
        pitch = float(default_cls["diff_pair_gap"]) + float(default_cls["diff_pair_width"])
        coupling = measure_coupling(graded, pitch)
        if coupling:
            out["diff_pair_coupled_pct"] = coupling
    if match_groups is not None:
        spread = measure_group_spread(graded, load_match_groups(match_groups))
        if spread:
            out["group_length_spread"] = spread
    if fab_project is not None and fab_project.exists():
        out["fab_referee"] = _grade_with_project(
            pcb,
            fab_project,
            fab_project.with_suffix(".kicad_dru"),
            score_dir / "fab",
            save_board=False,
        )
    if emitted:
        out["as_emitted"] = _grade_with_project(
            pcb,
            pcb.with_suffix(".kicad_pro"),
            pcb.with_suffix(".kicad_dru"),
            score_dir / "emitted",
            save_board=False,
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--krt-dir", type=Path, required=True)
    ap.add_argument("--krt-python", default=None)
    ap.add_argument("--work-dir", type=Path, required=True)
    ap.add_argument("--boards", default=",".join(BOARDS))
    ap.add_argument("--tools", default="kct,krt,ref")
    ap.add_argument("--timeout", type=float, default=1200.0, help="per board+tool cap, s")
    ap.add_argument(
        "--rescore",
        action="store_true",
        help="re-grade existing outputs in --work-dir without re-routing",
    )
    args = ap.parse_args()

    krt_python = args.krt_python or str(args.krt_dir / ".venv/bin/python")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    results_path = work / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else {}

    for key in args.boards.split(","):
        board = BOARDS[key]
        stem = Path(board.src).stem
        # kct's emitted JLC-floor project for this board = the fab referee.
        fab_project = work / (board.fab_from or key) / "kct" / "run" / f"{stem}_routed.kicad_pro"
        for tool in args.tools.split(","):
            if tool not in board.tools:
                continue  # recipe variant that does not redefine this tool's run
            tag = f"{key}/{tool}"
            print(f"== {tag}", flush=True)
            base = work / key / tool
            if args.rescore:
                entry = results.get(tag)
                if entry is None:
                    continue
                inp = base / "input" / f"{stem}.kicad_pcb"
                out = (
                    REPO / board.routed_ref
                    if tool == "ref"
                    else base / "run" / f"{stem}_routed.kicad_pcb"
                )
                if out.exists() and not entry.get("timed_out"):
                    entry.update(
                        score(
                            out,
                            inp,
                            base / "score",
                            tool != "ref",
                            fab_project,
                            board.layers,
                            board.match_groups,
                        )
                    )
                results[tag] = entry
                results_path.write_text(json.dumps(results, indent=2))
                continue
            if base.exists():
                shutil.rmtree(base)
            inp = prepare_input(board, base / "input")
            if tool == "ref":
                routed = REPO / board.routed_ref
                entry = {
                    "tool": "ref",
                    "board": key,
                    "wall_s": None,
                    "commands": [f"(committed artifact) {board.routed_ref}"],
                }
                entry.update(
                    score(
                        routed,
                        inp,
                        base / "score",
                        False,
                        fab_project,
                        board.layers,
                        board.match_groups,
                    )
                )
                results[tag] = entry
                results_path.write_text(json.dumps(results, indent=2))
                continue
            out_dir = base / "run"
            out_dir.mkdir(parents=True)
            if tool == "kct":
                rr = run_kct(board, inp, out_dir, args.timeout)
            else:
                rr = run_krt(board, inp, out_dir, args.timeout, args.krt_dir.resolve(), krt_python)
            entry = {
                "tool": tool,
                "board": key,
                "wall_s": round(rr.wall_s, 2),
                "exit_codes": rr.exit_codes,
                "timed_out": rr.timed_out,
                "commands": rr.commands,
                "note": board.note,
            }
            if rr.output is not None:
                entry.update(
                    score(
                        rr.output,
                        inp,
                        base / "score",
                        True,
                        fab_project,
                        board.layers,
                        board.match_groups,
                    )
                )
            else:
                entry["failed"] = "no output board written"
            results[tag] = entry
            results_path.write_text(json.dumps(results, indent=2))
            print(
                json.dumps({k: entry.get(k) for k in ("wall_s", "exit_codes", "timed_out")}),
                flush=True,
            )

    print_table(results)
    return 0


def _err(r: dict, key: str) -> str:
    g = r.get(key)
    if not g:
        return "n/a"
    by = ", ".join(f"{k} {v}" for k, v in g["errors_by_type"].items())
    return f"{g['errors_total']}" + (f" ({by})" if by else "")


def _kct(r: dict) -> str:
    k = r.get("kct_check")
    if not k or not k.get("ran"):
        return "n/a"
    by = ", ".join(f"{a} {b}" for a, b in k["errors_by_rule"].items())
    return f"{k['error_count']}" + (f" ({by})" if by else "")


def _runtime(r: dict) -> str:
    """Runtime cell. A run killed at ``--timeout`` is ALWAYS marked TIMEOUT.

    A tool that writes a partial board before the cap fires still gets scored
    (that is useful -- it says how far it had got), so the timeout must be
    visible on the row rather than inferred from a suspiciously round runtime.
    """
    wall = r.get("wall_s")
    cell = "n/a" if wall is None else str(wall)
    return f"{cell} (TIMEOUT)" if r.get("timed_out") else cell


def print_table(results: dict) -> None:
    cols = [
        "Board", "Tool", "Nets complete", "Connections", "kicad-cli unconnected",
        "DRC errors, shared referee", "DRC errors, fab referee", "DRC errors, as emitted",
        "kct check errors (jlcpcb)",
        "Vias", "Wirelength mm", "Signal on plane layers mm", "Pair coupling % (min)",
        "Group length spread mm (max)",
        "Runtime s",
    ]  # fmt: skip
    print("\n| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for tag in sorted(results):
        r = results[tag]
        if "completion" not in r:
            status = "TIMEOUT" if r.get("timed_out") else r.get("failed", "?")
            cells = [r["board"], r["tool"], status] + ["--"] * (len(cols) - 4)
            print("| " + " | ".join(cells + [_runtime(r)]) + " |")
            continue
        c = r["completion"]
        coupling = r.get("diff_pair_coupled_pct")
        spread = r.get("group_length_spread")
        cells = [
            r["board"], r["tool"],
            f"{c['nets_complete']}/{c['nets_total']}",
            f"{c['connections_routed']}/{c['connections_total']}",
            str(r["shared_referee"]["unconnected"]),
            _err(r, "shared_referee"), _err(r, "fab_referee"), _err(r, "as_emitted"),
            _kct(r),
            str(r["via_count"]), str(r["wirelength_mm"]),
            str(r.get("plane_layer_signal_mm", "n/a")),
            f"{min(coupling.values())}" if coupling else "--",
            f"{max(g['spread_mm'] for g in spread.values())}" if spread else "--",
            _runtime(r),
        ]  # fmt: skip
        print("| " + " | ".join(cells) + " |")


if __name__ == "__main__":
    sys.exit(main())
