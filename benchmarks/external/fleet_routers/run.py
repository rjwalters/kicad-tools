#!/usr/bin/env python3
"""Three-router fleet protocol: TraceMaker vs fastroute vs ``kct route`` (Issue #6334).

Research-only (part of #5943). Nothing here is wired into CI or the ``kct`` CLI,
and neither upstream router is a dependency. Both are GPL-3.0 projects that the
ecosystem registry marks ``copyleft-ideas-only``: this driver only *executes*
binaries built from an out-of-tree checkout in a scratch directory. It never
vendors, links or copies their code.

What the protocol guarantees
----------------------------

* **Frozen inputs.** ``manifest.json`` lists every fleet board with the sha256
  of its unrouted ``.kicad_pcb``, its ``.kicad_pro``, its manufacturer rule set
  (the ``.kicad_dru`` beside the committed routed reference, or an explicit
  ``absent``) and its schematic. ``verify`` re-hashes the tree and refuses to
  run on drift; ``freeze`` regenerates the board section deliberately.
* **One stripping policy for every board** (``strip-tracks-and-vias``): every
  top-level ``segment``/``arc``/``via`` is removed; footprints, pads and their
  nets, the outline, zones, rule areas and the layer stack are kept byte for
  byte. Nothing else is rewritten.
* **Fixed placement.** Every output is fingerprinted (footprint positions,
  rotations and sides, pad nets, outline, copper layers, zones) against its
  prepared input. Any drift makes the row ``invalid-fixed-placement``.
* **Frozen referee.** Each output is copied into a fresh grading directory
  beside copies of the *frozen* ``.kicad_pro``/``.kicad_dru`` -- never the
  rules a router emitted -- and graded by ``kicad-cli pcb drc --refill-zones``,
  ``kct check --mfr`` and ``kct validate --sync``. A referee that did not run is
  ``unavailable`` (``None``), never zero, and blocks a clean-pass claim.
* **Distinguishable outcomes.** See ``OUTCOMES``. A run killed at the wall cap is
  ``timeout`` even when it left a (partial) board behind; that board is kept as
  ``partial_artifact`` and never scored as a result.
* **DSN comparability.** fastroute routes a Specctra view of the board. Every
  fastroute row carries an audit of what the DSN export and the SES import
  dropped; any loss makes it ``non-comparable`` with the concrete constraints.

Usage::

    uv run python benchmarks/external/fleet_routers/run.py verify
    uv run python benchmarks/external/fleet_routers/run.py run \\
        --work-dir /tmp/fleet --boards 00 \\
        --tracemaker-dir /scratch/TraceMaker --fastroute-dir /scratch/fastroute

See ``README.md`` beside this file for the full procedure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MANIFEST_PATH = HERE / "manifest.json"
KRT_COMPARE = REPO / "scripts" / "research" / "krt_compare.py"

TOOLS = ("kct", "tracemaker", "fastroute")

#: Every outcome a row can carry. They are mutually exclusive by construction
#: (see :func:`classify`), so a table can never collapse two of them.
OUTCOMES = (
    "success",  # ran, output scored, fixed placement held, no known rule loss
    "non-comparable",  # ran and scored, but the tool saw a lossy view of the rules
    "invalid-fixed-placement",  # output moved parts / changed nets / outline
    "timeout",  # killed at the wall cap (a partial board is never promoted)
    "tool-failure",  # build/pin/executable problem, or non-zero exit without output
    "missing-output",  # exit 0 but the expected output was not written
    "scorer-failure",  # our side failed: SES import, headline DRC unavailable, ...
    "unsupported",  # row cannot be run (excluded board, tool not requested)
    "reference",  # the normalized input itself, scored for pre-existing errors
)

#: Board keys -> (repo-relative dir, file stem). ``None`` stem: no PCB committed.
BOARD_SPECS: dict[str, tuple[str, str | None]] = {
    "00": ("boards/00-simple-led", "simple_led"),
    "01": ("boards/01-voltage-divider", "voltage_divider"),
    "02": ("boards/02-charlieplex-led", "charlieplex_3x3"),
    "03": ("boards/03-usb-joystick", "usb_joystick"),
    "04": ("boards/04-stm32-devboard", "stm32_devboard"),
    "05": ("boards/05-bldc-motor-controller", "bldc_controller"),
    "06": ("boards/06-diffpair-test", "diffpair_test"),
    "07": ("boards/07-matchgroup-test", "matchgroup_test"),
    "08": ("boards/08-precision-acquisition", None),
    "09": ("boards/09-usbc-pd-power", "usbc_pd_power"),
}

STRIP_HEADS = ("segment", "arc", "via")
KCT_DRU_MARKER = "BEGIN kct fab floors"


# ---------------------------------------------------------------------------
# Shared process helpers (reused from the KRT experiment, not duplicated)
# ---------------------------------------------------------------------------


def _load_krt_compare() -> Any:
    """Load ``scripts/research/krt_compare.py`` by path for its process helpers.

    ``_run`` / ``_kill_group`` put each tool in its own process group and block
    until the whole group is gone -- the fix for routers outliving their cap
    (#5790). Reusing them keeps one implementation of that guarantee.
    """
    name = "krt_compare_for_fleet_routers"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, KRT_COMPARE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_bounded(cmd: list[str], cwd: Path, log: Path, timeout: float) -> tuple[int, bool, float]:
    """Run ``cmd`` under a hard wall cap; kill and reap the whole process group.

    Returns ``(exit_code, timed_out, wall_s)``. ``exit_code`` is ``-1`` on
    timeout. A missing executable is reported as exit code 127 with the error
    in the log, so it classifies as a tool failure rather than raising.
    """
    krt = _load_krt_compare()
    t0 = time.monotonic()
    try:
        rc, timed_out = krt._run(cmd, cwd, log, timeout)
    except OSError as exc:  # executable missing / not executable
        with log.open("a") as fh:
            fh.write(f"!! could not start: {exc}\n")
        rc, timed_out = 127, False
    return rc, timed_out, time.monotonic() - t0


# ---------------------------------------------------------------------------
# Hashing and the manifest
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_entry(rel: str | None, repo: Path) -> dict[str, Any]:
    if rel is None or not (repo / rel).exists():
        return {"path": None, "absent": True}
    return {"path": rel, "sha256": sha256_file(repo / rel)}


def board_paths(key: str) -> dict[str, str | None]:
    """Repo-relative path each manifest slot of board ``key`` is frozen from.

    Shared by :func:`freeze_board` and :func:`verify_manifest`, so a slot the
    manifest records as absent can be re-checked for a file that later appeared.
    """
    bdir, stem = BOARD_SPECS[key]
    if stem is None:
        return {"input": None, "project": None, "dru": None, "schematic": None}
    out = f"{bdir}/output"
    return {
        "input": f"{out}/{stem}.kicad_pcb",
        "project": f"{out}/{stem}.kicad_pro",
        "dru": f"{out}/{stem}_routed.kicad_dru",
        "schematic": f"{out}/{stem}.kicad_sch",
    }


def freeze_board(key: str, repo: Path = REPO) -> dict[str, Any]:
    """Compute one board's manifest entry from the tree as it is now."""
    bdir, stem = BOARD_SPECS[key]
    out = f"{bdir}/output"
    slots = board_paths(key)
    pcb_rel = slots["input"]
    if pcb_rel is None or not (repo / pcb_rel).exists():
        return {
            "key": key,
            "dir": bdir,
            "status": "excluded",
            "exclusion_reason": "no committed .kicad_pcb at this revision",
        }
    text = (repo / pcb_rel).read_text()
    counts = Counter(head for head, _, _ in iter_forms(text, level=1))
    dru_rel = slots["dru"]
    dru = _file_entry(dru_rel, repo)
    if dru.get("absent"):
        dru["provenance"] = "absent"
        dru["note"] = "no .kicad_dru at this revision; graded on .kicad_pro alone"
    else:
        assert dru_rel is not None
        managed = KCT_DRU_MARKER in (repo / dru_rel).read_text()
        dru["provenance"] = "kct-managed fab floors" if managed else "board-authored"
        dru["note"] = (
            "sibling of the committed routed reference; manufacturer rules, not router tuning"
        )
    sidecars = sorted(
        str(p.relative_to(repo))
        for pat in ("net_class_map.json", "*constraints*.json")
        for p in (repo / out).glob(pat)
    )
    layers = len(re.findall(r'\(\d+\s+"[^"]*\.Cu"\s+(?:signal|power|mixed|jumper)', text))
    return {
        "key": key,
        "dir": bdir,
        "status": "included",
        "stem": stem,
        "input": _file_entry(pcb_rel, repo),
        "input_copper": {
            "segments": counts.get("segment", 0),
            "arcs": counts.get("arc", 0),
            "vias": counts.get("via", 0),
            "zones": counts.get("zone", 0),
        },
        "copper_layers": layers,
        "project": _file_entry(slots["project"], repo),
        "dru": dru,
        "schematic": _file_entry(slots["schematic"], repo),
        "routing_sidecars": [_file_entry(s, repo) for s in sidecars],
    }


def freeze_all(repo: Path = REPO) -> list[dict[str, Any]]:
    return [freeze_board(k, repo) for k in BOARD_SPECS]


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def verify_manifest(manifest: dict[str, Any], repo: Path = REPO) -> list[str]:
    """Every frozen file must still hash the same, and every absence still hold.

    An absence is a manifest slot with ``path: None`` (board 09's DRU) or an
    excluded board (08, no PCB). If the file the slot would be frozen from now
    exists, the manifest no longer describes the tree and that is drift too.
    """
    problems: list[str] = []
    for board in manifest["boards"]:
        expected = board_paths(board["key"])
        if board["status"] != "included":
            rel = expected["input"]
            if rel is not None and (repo / rel).exists():
                problems.append(f"{board['key']}: {rel} now exists but the board is excluded")
            continue
        for slot in ("input", "project", "dru", "schematic"):
            rel = expected[slot]
            if board[slot].get("path") is None and rel is not None and (repo / rel).exists():
                problems.append(f"{board['key']}: {rel} now exists but is recorded absent")
        entries = [board["input"], board["project"], board["dru"], board["schematic"]]
        entries += board.get("routing_sidecars", [])
        for entry in entries:
            rel = entry.get("path")
            if rel is None:
                continue
            path = repo / rel
            if not path.exists():
                problems.append(f"{board['key']}: {rel} is missing")
            elif sha256_file(path) != entry["sha256"]:
                problems.append(f"{board['key']}: {rel} hash drifted")
    return problems


# ---------------------------------------------------------------------------
# S-expression spans (text level, so untouched bytes stay untouched)
# ---------------------------------------------------------------------------


def iter_forms(text: str, level: int) -> Iterator[tuple[str, int, int]]:
    """Yield ``(head, start, end)`` for every form at nesting ``level``.

    ``level=0`` is the file's own top-level forms (a ``.kicad_dru``),
    ``level=1`` the children of the root form (a ``.kicad_pcb``'s ``segment``,
    ``via``, ``footprint`` ...). Strings, including escaped quotes, are skipped.
    Only depth decides membership, so a ``(arc ...)`` inside a zone polygon is
    never mistaken for a board-level arc track.
    """
    depth = 0
    in_str = False
    start = -1
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            if depth == level:
                start = i
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == level and start >= 0:
                m = re.match(r"\(\s*([^\s()\"]+)", text[start : i + 1])
                yield (m.group(1) if m else ""), start, i + 1
                start = -1
        i += 1


def strip_copper(text: str) -> tuple[str, dict[str, int]]:
    """Apply the ``strip-tracks-and-vias`` policy to a ``.kicad_pcb`` text."""
    spans = [(s, e, h) for h, s, e in iter_forms(text, level=1) if h in STRIP_HEADS]
    counts = Counter(h for _, _, h in spans)
    for s, e, _ in sorted(spans, reverse=True):
        # Also drop the indentation/newline the removed form leaves behind.
        line_start = text.rfind("\n", 0, s) + 1
        if text[line_start:s].strip() == "":
            s = line_start
            if e < len(text) and text[e] == "\n":
                e += 1
        text = text[:s] + text[e:]
    return text, {h: counts.get(h, 0) for h in STRIP_HEADS}


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    board: str
    pcb: Path  # normalized input (copper stripped)
    frozen_dir: Path  # frozen .kicad_pro / .kicad_dru / .kicad_sch copies
    stem: str
    stripped: dict[str, int]
    sha256: str


def prepare_board(board: dict[str, Any], dest: Path, repo: Path = REPO) -> Prepared:
    """Normalize one manifest board into ``dest`` (wiped first)."""
    if dest.exists():
        shutil.rmtree(dest)
    frozen = dest / "frozen"
    frozen.mkdir(parents=True)
    stem = board["stem"]
    src = repo / board["input"]["path"]
    if sha256_file(src) != board["input"]["sha256"]:
        raise RuntimeError(f"board {board['key']}: input hash drifted from the manifest")
    text, stripped = strip_copper(src.read_text())
    pcb = dest / f"{stem}.kicad_pcb"
    pcb.write_text(text)
    for key, suffix in (
        ("project", ".kicad_pro"),
        ("dru", ".kicad_dru"),
        ("schematic", ".kicad_sch"),
    ):
        rel = board[key].get("path")
        if rel is not None:
            shutil.copy2(repo / rel, frozen / f"{stem}{suffix}")
    # Routers read the board's own project for net classes; give them the
    # frozen one (and only that) beside the normalized input.
    if (frozen / f"{stem}.kicad_pro").exists():
        shutil.copy2(frozen / f"{stem}.kicad_pro", dest / f"{stem}.kicad_pro")
    return Prepared(board["key"], pcb, frozen, stem, stripped, sha256_file(pcb))


# ---------------------------------------------------------------------------
# Fixed-placement fingerprint
# ---------------------------------------------------------------------------


def _r(v: float, nd: int = 3) -> float:
    return round(float(v), nd) + 0.0  # +0.0 folds -0.0 into 0.0


def fingerprint(pcb_path: Path) -> dict[str, Any]:
    """What a fixed-placement router must not change."""
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    seen: Counter[str] = Counter()
    footprints: dict[str, list[Any]] = {}
    pad_nets: dict[str, str] = {}
    for fp in pcb.footprints:
        ref = fp.reference or "?"
        seen[ref] += 1
        key = ref if seen[ref] == 1 else f"{ref}#{seen[ref]}"
        footprints[key] = [
            _r(fp.position[0]),
            _r(fp.position[1]),
            _r(fp.rotation % 360, 2),
            fp.layer,
        ]
        pcount: Counter[str] = Counter()
        for pad in fp.pads:
            pcount[pad.number] += 1
            pkey = f"{key}:{pad.number}" + (
                "" if pcount[pad.number] == 1 else f"#{pcount[pad.number]}"
            )
            pad_nets[pkey] = pad.net_name or ""
    outline = sorted(
        (_r(a[0]), _r(a[1]), _r(b[0]), _r(b[1])) for a, b in pcb.get_board_outline_segments()
    )
    zones = sorted(
        f"{z.net_name or ''}@{'/'.join([z.layer or '', *(z.layers or [])])}"
        f"{'[keepout]' if getattr(z, 'keepout', None) else ''}"
        for z in pcb.zones
    )
    return {
        "footprints": footprints,
        "pad_nets": pad_nets,
        "outline": [list(s) for s in outline],
        "copper_layers": sorted(layer.name for layer in pcb.copper_layers),
        "zones": zones,
    }


def compare_fingerprints(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable violations; empty means the placement held."""
    out: list[str] = []
    for key in ("footprints", "pad_nets"):
        a, b = before[key], after[key]
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{key}: {k} removed")
            elif k not in a:
                out.append(f"{key}: {k} added")
            elif a[k] != b[k]:
                out.append(f"{key}: {k} {a[k]!r} -> {b[k]!r}")
    for key in ("outline", "copper_layers"):
        if before[key] != after[key]:
            out.append(f"{key} changed")
    # Every input zone (copper or keepout) must survive unchanged. A router
    # ADDING a pour is routing output, not a placement change: see added_zones.
    remaining = Counter(after["zones"])
    for z in before["zones"]:
        if remaining[z] > 0:
            remaining[z] -= 1
        else:
            out.append(f"zones: input zone {z} removed or changed")
    return out


def added_zones(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Zones present in the output but not the input (informational)."""
    remaining = Counter(after["zones"])
    remaining.subtract(Counter(before["zones"]))
    return sorted(z for z, n in remaining.items() for _ in range(max(n, 0)))


# ---------------------------------------------------------------------------
# DSN / SES comparability audit (fastroute)
# ---------------------------------------------------------------------------


def _dru_rules(text: str) -> list[dict[str, Any]]:
    rules = []
    for head, s, e in iter_forms(text, level=0):
        if head != "rule":
            continue
        blk = text[s:e]
        m = re.match(r'\(rule\s+(?:"([^"]*)"|(\S+))', blk)
        name = (m.group(1) or m.group(2)) if m else "?"
        cond = re.search(r'\(condition\s+"((?:[^"\\]|\\.)*)"', blk)
        constraints = []
        for cm in re.finditer(r"\(constraint\s+(\w+)((?:\s*\([^()]*\))*)\s*\)", blk):
            mn = re.search(r"\(min\s+([-\d.]+)\s*(mm|mil)?\)", cm.group(2))
            val = None
            if mn:
                val = float(mn.group(1)) * (0.0254 if mn.group(2) == "mil" else 1.0)
            constraints.append({"type": cm.group(1), "min_mm": val})
        rules.append(
            {
                "name": name,
                "conditioned": "(condition" in blk,
                "condition": cond.group(1) if cond else None,
                "layer_scoped": "(layer" in blk,
                "constraints": constraints,
            }
        )
    return rules


def _dsn_unit_mm(dsn: str) -> float:
    """Millimetres per DSN coordinate unit (``(unit ...)``, else ``(resolution ...)``)."""
    m = re.search(r"\(unit\s+(\w+)\)", dsn) or re.search(r"\(resolution\s+(\w+)", dsn)
    return {"um": 0.001, "mm": 1.0, "mil": 0.0254, "inch": 25.4, "cm": 10.0}.get(
        m.group(1) if m else "um", 0.001
    )


def _dsn_atoms(form: str) -> list[str]:
    """The bare (non-list) tokens directly inside ``form``, quotes stripped."""
    atoms: list[str] = []
    depth = 0
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)', form):
        if m.group(2) == "(":
            depth += 1
        elif m.group(2) == ")":
            depth -= 1
        elif depth == 1:
            atoms.append(m.group(1) if m.group(1) is not None else m.group(3))
    return atoms


def _dsn_classes(dsn: str) -> dict[str, dict[str, Any]]:
    """Each emitted ``(class ...)``: member nets, rule width/clearance (mm), via."""
    scale = _dsn_unit_mm(dsn)
    # The header's ``(string_quote ")`` is a bare quote character, not the
    # start of a string; neutralise it so the form scanner stays balanced.
    dsn = re.sub(r'\(string_quote\s+"\s*\)', "(string_quote _)", dsn)
    out: dict[str, dict[str, Any]] = {}
    for head, s, e in iter_forms(dsn, level=2):
        if head != "class":
            continue
        blk = dsn[s:e]
        atoms = _dsn_atoms(blk)[1:]  # drop the "class" keyword
        if not atoms:
            continue
        rule = re.search(r"\(rule\b(.*)", blk, re.S)
        rule_txt = rule.group(1) if rule else ""
        width = re.search(r"\(width\s+([-\d.]+)\)", rule_txt)
        clear = re.search(r"\(clearance\s+([-\d.]+)\)", rule_txt)
        via = re.search(r'\(use_via\s+(?:"([^"]*)"|([^\s()]+))', blk)
        out[atoms[0]] = {
            "nets": set(atoms[1:]),
            "width": float(width.group(1)) * scale if width else None,
            "clearance": float(clear.group(1)) * scale if clear else None,
            "via": (via.group(1) or via.group(2)) if via else None,
        }
    return out


def _dsn_padstack_diameter(dsn: str, name: str | None) -> float | None:
    """The circle diameter (mm) of padstack ``name``, if it is defined."""
    if not name:
        return None
    m = re.search(
        r'\(padstack\s+"?' + re.escape(name) + r'"?\s+\(shape\s+\(circle\s+\S+\s+([-\d.]+)',
        dsn,
    )
    return float(m.group(1)) * _dsn_unit_mm(dsn) if m else None


def _dsn_layer_shapes(dsn: str, heads: tuple[str, ...]) -> Counter[tuple[str, str, str]]:
    """``(head, name, layer)`` counts for planes/keepouts in the DSN structure."""
    pat = r"\((" + "|".join(heads) + r')\s+(?:"([^"]*)"|([^\s()]+))\s*\(polygon\s+"?([^\s"()]+)'
    return Counter(
        (m.group(1), m.group(2) if m.group(2) is not None else m.group(3), m.group(4))
        for m in re.finditer(pat, dsn)
    )


def _zone_copper_layers(zone: Any, board_layers: list[str]) -> list[str]:
    """The board copper layers a zone/rule area covers (wildcards expanded)."""
    specs = list(zone.layers or ([zone.layer] if zone.layer else []))
    out: list[str] = []
    for spec in specs:
        if spec == "*.Cu":
            matched = list(board_layers)
        elif spec == "F&B.Cu":
            matched = [lay for lay in ("F.Cu", "B.Cu") if lay in board_layers]
        else:
            matched = [spec] if spec in board_layers else []
        out.extend(lay for lay in matched if lay not in out)
    return out


# DRU constraint types no Specctra router consumes (fab/silk/edge geometry).
# The referee ``kicad-cli`` DRC grades them, so they are not DSN losses.
NOT_ROUTING_CONSTRAINTS = frozenset(
    {
        "silk_clearance",
        "text_thickness",
        "text_height",
        "annular_width",
        "edge_clearance",
        "physical_hole_clearance",
        "hole_clearance",
    }
)


def _condition_classes(condition: str | None) -> list[str]:
    """Net class names a DRU condition selects via ``NetClass == '...'``."""
    if not condition:
        return []
    return [
        m.replace("\\'", "'").replace("\\\\", "\\")
        for m in re.findall(r"NetClass\s*==\s*'((?:[^'\\]|\\.)*)'", condition)
    ]


def dsn_audit(
    pcb: Path,
    project: Path | None,
    dru: Path | None,
    dsn_path: Path,
    not_routing: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """List every board/rule constraint the exported DSN does not carry.

    DRU constraints no router consumes (``NOT_ROUTING_CONSTRAINTS``) are not
    losses; when ``not_routing`` is given they are appended to it instead.

    Exports with this repo's ``KiCadToDSNExporter`` (if ``dsn_path`` does not
    exist yet) and compares what it *emitted* against what the board, its
    frozen project and its frozen DRU declare. Each entry is
    ``{"kind", "detail"}``; an empty list means no loss was detected.
    """
    from kicad_tools.export.dsn import KiCadToDSNExporter
    from kicad_tools.schema.pcb import PCB

    exp = KiCadToDSNExporter(pcb)
    if not dsn_path.exists():
        exp.export(dsn_path)
    exp._load()
    dsn = dsn_path.read_text()
    lost: list[dict[str, str]] = []

    def loss(kind: str, detail: str) -> None:
        lost.append({"kind": kind, "detail": detail})

    dsn_clear = exp._default_clearance
    dsn_width = exp._default_trace_width
    dsn_via = exp._default_via_size
    dsn_drill = exp._default_via_drill
    dsn_classes = _dsn_classes(dsn)
    board = PCB.load(str(pcb))
    pad_nets = {p.net_name for fp in board.footprints for p in fp.pads if p.net_name}

    if project is not None and project.exists():
        from kicad_tools.router.board_clearance_rules import BoardClearanceRules

        pro = json.loads(project.read_text())
        ns = pro.get("net_settings", {})
        # Issue #6642: compare each class against ITS OWN emitted (class ...)
        # block, not the DSN's global defaults -- since #6594 the exporter
        # writes per-class rules and membership.
        resolver = BoardClearanceRules.from_board(pcb)
        members: dict[str, set[str]] = {}
        for net in pad_nets:
            for c in resolver.netclasses_of(net) or ("Default",):
                members.setdefault(c, set()).add(net)
        for cls in ns.get("classes", []):
            name = cls.get("name", "?")
            dsn_name = "kicad_default" if name == "Default" else name
            got = dsn_classes.get(dsn_name)
            if got is None:
                if name == "Default" or members.get(name):
                    loss("netclass", f"net class {name!r} not exported")
                continue
            for field_, dsn_val in (
                ("clearance", got["clearance"]),
                ("track_width", got["width"]),
                ("via_diameter", _dsn_padstack_diameter(dsn, got["via"])),
                # Specctra padstacks carry no drill; only the global default
                # is implied, so a class drill that differs from it is lost.
                ("via_drill", dsn_drill),
            ):
                want = cls.get(field_)
                if want is None:
                    continue
                if dsn_val is None:
                    loss("netclass_rule", f"{name}.{field_} {want} mm not exported")
                    continue
                if abs(float(want) - dsn_val) < 1e-6:
                    continue
                direction = "relaxed" if dsn_val < float(want) else "tightened"
                loss(
                    "netclass_rule",
                    f"{name}.{field_} {want} mm exported as {dsn_val:g} mm ({direction})",
                )
        for net in sorted(pad_nets):
            declared = sorted(c for c, nets in members.items() if net in nets)
            exported_in = sorted(
                ("Default" if c == "kicad_default" else c)
                for c, info in dsn_classes.items()
                if net in info["nets"]
            )
            if exported_in != declared:
                loss(
                    "netclass_assignment",
                    f"net {net!r} is in class(es) {', '.join(declared)} but exported in "
                    f"{', '.join(exported_in) or 'none'}",
                )
        rules = pro.get("board", {}).get("design_settings", {}).get("rules", {})
        edge = rules.get("min_copper_edge_clearance") or 0.0
        if float(edge) > 0:
            loss("board_rule", f"min_copper_edge_clearance {edge} mm not exported")
        for key, dsn_val in (("min_clearance", dsn_clear), ("min_track_width", dsn_width)):
            v = rules.get(key)
            if v is not None and float(v) > dsn_val + 1e-9:
                loss("board_rule", f"{key} {v} mm stricter than exported {dsn_val} mm")
    else:
        loss("project", "no frozen .kicad_pro: project rules unknown")

    if dru is not None and dru.exists():
        implied = {
            "clearance": dsn_clear,
            "track_width": dsn_width,
            "via_diameter": dsn_via,
            "hole_size": dsn_drill,
        }
        for rule in _dru_rules(dru.read_text()):
            for c in rule["constraints"]:
                if c["type"] in NOT_ROUTING_CONSTRAINTS:
                    if not_routing is not None:
                        not_routing.append(
                            {
                                "rule": rule["name"],
                                "type": c["type"],
                                "min_mm": c["min_mm"],
                            }
                        )
                    continue
                if (
                    c["type"] == "clearance"
                    and c["min_mm"] is not None
                    and rule["conditioned"]
                    and not rule["layer_scoped"]
                ):
                    # A class-scoped clearance is carried by that class's
                    # emitted (class ...) block, which the exporter writes
                    # without the same-footprint / coupled-pair exemptions.
                    names = _condition_classes(rule.get("condition"))
                    covered = [
                        dsn_classes.get("kicad_default" if n == "Default" else n) for n in names
                    ]
                    if names and all(
                        g is not None
                        and g["clearance"] is not None
                        and g["clearance"] >= c["min_mm"] - 1e-9
                        for g in covered
                    ):
                        continue
                floor = implied.get(c["type"])
                if (
                    floor is not None
                    and c["min_mm"] is not None
                    and not rule["layer_scoped"]
                    and floor >= c["min_mm"] - 1e-9
                ):
                    continue  # the DSN's global value is at least as strict
                loss("dru", f"rule {rule['name']!r}: {c['type']} min {c['min_mm']} mm not in DSN")

    # Issue #6642: since #6594 the exporter writes planes and keepouts, so a
    # mere "(keepout" / "(plane" substring no longer means nothing is lost.
    # Match each rule area / copper zone, per copper layer, against an
    # emitted DSN shape so a single exported item cannot mask an omitted one.
    board_layers = [layer.name for layer in board.copper_layers]
    shapes = _dsn_layer_shapes(dsn, ("plane", "keepout", "wire_keepout", "via_keepout"))

    def take(head: str, name: str | None, layer: str) -> bool:
        for key in shapes:
            if key[0] == head and key[2] == layer and (name is None or key[1] == name):
                if shapes[key] > 0:
                    shapes[key] -= 1
                    return True
        return False

    for area in [*board.rule_areas, *board.footprint_rule_areas]:
        ko = area.keepout
        if ko.tracks_allowed and ko.vias_allowed:
            continue  # no routing restriction for a router to honour
        need = (
            "keepout"
            if not ko.tracks_allowed and not ko.vias_allowed
            else ("wire_keepout" if not ko.tracks_allowed else "via_keepout")
        )
        label = area.name or area.uuid or "unnamed"
        owner = getattr(area, "parent_reference", None)
        where = f" in footprint {owner}" if owner else ""
        missing = [
            layer
            for layer in _zone_copper_layers(area, board_layers)
            if not (take(need, None, layer) or (need != "keepout" and take("keepout", None, layer)))
        ]
        if missing:
            loss(
                "keepout",
                f"rule area {label!r}{where} ({need}) not exported on {', '.join(missing)}",
            )
    for z in board.zones:
        if getattr(z, "keepout", None):
            continue
        missing = [
            layer
            for layer in _zone_copper_layers(z, board_layers)
            if not (z.net_name and take("plane", z.net_name, layer))
        ]
        if missing:
            net = z.net_name or "<no net>"
            loss("zone", f"zone {net}@{'/'.join(missing)} not exported")
    text = pcb.read_text()
    copper_text = 0
    for head, s, e in iter_forms(text, level=1):
        if head in ("gr_text", "gr_text_box") and re.search(r'\(layer\s+"[^"]*\.Cu"', text[s:e]):
            copper_text += 1
    if copper_text:
        loss("copper_text", f"{copper_text} copper-layer text item(s) not exported")
    board_layers = {layer.name for layer in board.copper_layers}
    missing_layers = sorted(board_layers - set(exp.layers))
    if missing_layers:
        loss("layer", f"copper layers not exported: {', '.join(missing_layers)}")
    exported_pads = {fp.reference: len(fp.pads) for fp in exp.footprints}
    for fp in board.footprints:
        n = sum(1 for p in fp.pads if any(str(lay).endswith(".Cu") for lay in (p.layers or [])))
        if exported_pads.get(fp.reference, 0) < n:
            loss(
                "pad",
                f"{fp.reference}: {exported_pads.get(fp.reference, 0)}/{n} copper pads exported",
            )
    pad_nets = {p.net_name for fp in board.footprints for p in fp.pads if p.net_name}
    exported_nets = set(exp.nets.values())
    for net in sorted(pad_nets - exported_nets):
        loss("net", f"net {net!r} not exported")
    return lost


def ses_counts(ses_text: str) -> dict[str, Any]:
    """Independent count of what a Specctra session actually contains."""
    start = ses_text.find("(routes")
    routes = ses_text[start:] if start >= 0 else ""
    res = re.search(r"\(resolution\s+(\w+)\s+([\d.]+)\)", routes)
    return {
        "wires": len(re.findall(r"\(wire\b", routes)),
        "vias": len(re.findall(r"\(via\b", routes)),
        "resolution_unit": res.group(1) if res else None,
        "resolution": float(res.group(2)) if res else None,
    }


def ses_import(ses: Path, prepared: Path, out: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Merge ``ses`` into the prepared board with this repo's SES importer.

    The original board (placement, pads, outline, zones) is the merge base, so
    board context is restored by construction. Returns the import losses (the
    importer's parse compared with :func:`ses_counts`) and the counts.
    """
    from kicad_tools.export.ses import SESToKiCadImporter

    imp = SESToKiCadImporter(ses)
    imp.parse()
    merged = imp.merge_into(prepared, out)
    if not out.exists():  # merge_into returns early, writing nothing, on 0 routes
        out.write_text(merged)
    raw = ses_counts(ses.read_text())
    counts = {"ses": raw, "imported_wires": len(imp.wires), "imported_vias": len(imp.vias)}
    lost: list[dict[str, str]] = []
    if len(imp.wires) < raw["wires"]:
        lost.append(
            {"kind": "ses_import", "detail": f"{len(imp.wires)}/{raw['wires']} SES wires imported"}
        )
    if len(imp.vias) < raw["vias"]:
        lost.append(
            {"kind": "ses_import", "detail": f"{len(imp.vias)}/{raw['vias']} SES vias imported"}
        )
    # Since #6593 the importer honours ``(resolution UNIT N)``; a loss only
    # remains if what it applied differs from what the session declares.
    if raw["resolution"] is not None and (
        (imp._resolution_unit, imp._resolution) != (raw["resolution_unit"], raw["resolution"])
    ):
        lost.append(
            {
                "kind": "ses_import",
                "detail": (
                    f"SES resolution {raw['resolution_unit']} {raw['resolution']:g}: "
                    f"importer applied {imp._resolution_unit} {imp._resolution:g}"
                ),
            }
        )
    return lost, counts


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def kicad_cli_drc(
    pcb: Path, report: Path, kicad_cli: str = "kicad-cli", timeout: int = 900
) -> dict[str, Any]:
    """``kicad-cli pcb drc --refill-zones``; ``ran: False`` on ANY failure."""
    cmd = [
        kicad_cli, "pcb", "drc", "--refill-zones", "--severity-all", "--all-track-errors",
        "--format", "json", "--units", "mm", "--save-board", "--output", str(report), str(pcb),
    ]  # fmt: skip
    if report.exists():
        report.unlink()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ran": False, "note": f"kicad-cli did not run: {exc}", "command": cmd}
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError) as exc:
        return {
            "ran": False,
            "note": f"no DRC JSON (exit {proc.returncode}): {exc}",
            "stderr": proc.stderr[-2000:],
            "command": cmd,
        }
    errors: Counter[str] = Counter()
    warnings = 0
    for v in data.get("violations", []):
        if v.get("severity") == "error":
            errors[v.get("type", "?")] += 1
        elif v.get("severity") == "warning":
            warnings += 1
    return {
        "ran": True,
        "errors_total": sum(errors.values()),
        "errors_by_type": dict(sorted(errors.items())),
        "warnings": warnings,
        "unconnected": len(data.get("unconnected_items", [])),
        "kicad_version": data.get("kicad_version"),
        "command": cmd,
    }


def kct_sync(pcb: Path, sch: Path | None, timeout: int = 600) -> dict[str, Any]:
    """``kct validate --sync`` (schematic vs PCB netlist); unavailable without a schematic."""
    if sch is None or not sch.exists():
        return {"ran": False, "note": "no frozen schematic"}
    kct = Path(sys.executable).with_name("kct")
    cmd = [str(kct), "validate", "--sync", "-s", str(sch), "-p", str(pcb), "--format", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"ran": False, "note": f"sync check failed to run: {exc}", "command": cmd}
    return {
        "ran": True,
        "in_sync": bool(data.get("in_sync")),
        "summary": data.get("summary", {}),
        "command": cmd,
    }


def kct_check(pcb: Path, layers: int) -> dict[str, Any]:
    from kicad_tools.benchmark.external.metrics import run_kct_check

    try:
        return run_kct_check(pcb, manufacturer="jlcpcb", layers=layers).to_dict()
    except Exception as exc:  # noqa: BLE001 - an unavailable referee, never a pass
        return {"ran": False, "note": f"kct check raised: {exc!r}"}


def clean_pass(score: dict[str, Any]) -> bool | None:
    """True only when every referee RAN and found nothing.

    ``False`` as soon as any referee reports a problem; otherwise ``None``
    (unavailable) if any referee did not run. A missing referee can never
    produce ``True``.
    """
    drc, chk, sync = score.get("drc", {}), score.get("kct_check", {}), score.get("sync", {})
    comp = score.get("completion") or {}
    failures = [
        drc.get("ran") and (drc["errors_total"] or drc["unconnected"]),
        chk.get("ran") and chk.get("passed") is False,
        sync.get("ran") and not sync.get("in_sync"),
        bool(comp) and comp.get("connections_routed") != comp.get("connections_total"),
    ]
    if any(failures):
        return False
    if not (drc.get("ran") and chk.get("ran") and sync.get("ran") and comp):
        return None
    return True


def score(
    output: Path, prep: Prepared, layers: int, score_dir: Path, kicad_cli: str = "kicad-cli"
) -> dict[str, Any]:
    """Grade ``output`` beside the FROZEN rules (fresh dir, nothing tool-emitted)."""
    from kicad_tools.benchmark.external.metrics import measure_completion, measure_copper

    if score_dir.exists():
        shutil.rmtree(score_dir)
    score_dir.mkdir(parents=True)
    graded = score_dir / f"{prep.stem}.kicad_pcb"
    shutil.copy2(output, graded)
    rules: dict[str, str | None] = {}
    for suffix in (".kicad_pro", ".kicad_dru"):
        src = prep.frozen_dir / f"{prep.stem}{suffix}"
        if src.exists():
            shutil.copy2(src, graded.with_suffix(suffix))
            rules[suffix] = sha256_file(graded.with_suffix(suffix))
        else:
            rules[suffix] = None
    drc = kicad_cli_drc(graded, score_dir / "drc.json", kicad_cli=kicad_cli)
    out: dict[str, Any] = {"graded_rules_sha256": rules, "drc": drc}
    # With --save-board the graded copy now carries the refilled zones.
    try:
        out["completion"] = measure_completion(graded).to_dict()
        cop = measure_copper(graded).to_dict()
        out["via_count"] = cop["via_count"]
        out["wirelength_mm"] = cop["wirelength_mm"]
    except Exception as exc:  # noqa: BLE001
        out["completion"] = None
        out["measure_note"] = f"completion/copper measurement failed: {exc!r}"
    out["kct_check"] = kct_check(graded, layers)
    sch = prep.frozen_dir / f"{prep.stem}.kicad_sch"
    out["sync"] = kct_sync(graded, sch if sch.exists() else None)
    out["clean_pass"] = clean_pass(out)
    return out


# ---------------------------------------------------------------------------
# Run records and classification
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    tool: str
    board: str
    outcome: str = "unsupported"
    reason: str | None = None
    stage: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    wall_s: float | None = None
    commands: list[list[str]] = field(default_factory=list)
    log: str | None = None
    output: str | None = None
    partial_artifact: str | None = None
    lost_constraints: list[dict[str, str]] = field(default_factory=list)
    not_routing_rules: list[dict[str, Any]] = field(default_factory=list)
    placement_violations: list[str] = field(default_factory=list)
    tool_config: dict[str, Any] = field(default_factory=dict)
    interchange: dict[str, Any] | None = None
    score: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(rec: RunRecord, output_exists: bool) -> str | None:
    """Pre-scoring outcome, or None when the output should be scored.

    Order matters: a timeout wins over an existing (partial) output, and a
    non-zero exit without output is the tool's failure, not a missing file.
    """
    if rec.timed_out:
        return "timeout"
    if not output_exists:
        return "tool-failure" if rec.exit_code not in (0, None) else "missing-output"
    return None


def _fresh(path: Path, since: float) -> bool:
    """Output exists AND was written by this run (stale files never count)."""
    return path.exists() and path.stat().st_mtime >= since - 1.0


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    executable: Path | None
    checkout: Path | None = None
    pin: str | None = None
    build_log: Path | None = None


def validate_pin(spec: ToolSpec) -> tuple[bool, str]:
    """The checkout must be at exactly the manifest pin."""
    if spec.pin is None:
        return True, "no pin"
    if spec.checkout is None:
        return False, "no --<tool>-dir checkout given; the pinned revision cannot be validated"
    try:
        head = subprocess.run(
            ["git", "-C", str(spec.checkout), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as exc:
        return False, f"cannot read checkout revision: {exc}"
    if head != spec.pin:
        return False, f"checkout at {head}, manifest pins {spec.pin}"
    return True, head


def preflight(spec: ToolSpec, rec: RunRecord, run_dir: Path) -> bool:
    """Pin + executable checks. On failure fills ``rec`` and returns False."""
    ok, detail = validate_pin(spec)
    rec.tool_config["pin_check"] = detail
    if not ok:
        rec.outcome, rec.stage, rec.reason = "tool-failure", "pin", detail
        return False
    exe = spec.executable
    if exe is None or not exe.exists() or not os.access(exe, os.X_OK):
        if spec.build_log is not None and spec.build_log.exists():
            dst = run_dir / f"{spec.name}-build.log"
            shutil.copy2(spec.build_log, dst)
            rec.log = str(dst)
            rec.outcome, rec.stage = "tool-failure", "build"
            rec.reason = f"no executable; pinned build failed (log: {dst.name})"
        else:
            rec.outcome, rec.stage = "tool-failure", "executable"
            rec.reason = f"executable not found: {exe}"
        return False
    rec.tool_config["executable"] = str(exe)
    rec.tool_config["executable_sha256"] = sha256_file(exe)
    return True


def kct_command(prep: Prepared, out: Path, budgets: dict[str, Any]) -> list[str]:
    kct = Path(sys.executable).with_name("kct")
    return [
        str(kct), "route", str(prep.pcb), "-o", str(out),
        "--backend", "cpp", "--seed", str(budgets["seed"]),
    ]  # fmt: skip


def tracemaker_command(
    exe: Path, prep: Prepared, out: Path, run_dir: Path, budgets: dict[str, Any]
) -> list[str]:
    # Flags verified against src/app/main.cpp at the pinned revision.
    return [
        str(exe), "route", str(prep.pcb), "-o", str(out),
        "--work", str(budgets["tracemaker_work"]), "--seed", str(budgets["seed"]),
        "--threads", str(budgets["threads"]), "--no-gpu", "--no-kb",
        "--json", str(run_dir / "tracemaker-summary.json"),
    ]  # fmt: skip


def fastroute_command(
    exe: Path, dsn: Path, ses: Path, run_dir: Path, budgets: dict[str, Any]
) -> list[str]:
    # Flags verified against `fastroute --help` at the pinned revision.
    t = str(budgets["threads"])
    return [
        str(exe), "-de", str(dsn), "-do", str(ses),
        f"--router.autorouter.max_threads={t}", f"--router.optimizer.max_threads={t}",
        f"--report={run_dir / 'fastroute-report.json'}",
    ]  # fmt: skip


def run_tool(
    tool: str,
    spec: ToolSpec | None,
    prep: Prepared,
    board: dict[str, Any],
    base: Path,
    budgets: dict[str, Any],
    kicad_cli: str = "kicad-cli",
) -> RunRecord:
    """Run one router on one prepared board and classify/score the result."""
    rec = RunRecord(tool=tool, board=prep.board)
    run_dir = base / "run"
    if run_dir.exists():
        shutil.rmtree(run_dir)  # a rerun can never see a previous run's files
    run_dir.mkdir(parents=True)
    # The router works on its OWN copy of the normalized input + frozen project.
    work_in = run_dir / f"{prep.stem}.kicad_pcb"
    shutil.copy2(prep.pcb, work_in)
    if prep.pcb.with_suffix(".kicad_pro").exists():
        shutil.copy2(prep.pcb.with_suffix(".kicad_pro"), work_in.with_suffix(".kicad_pro"))
    local = Prepared(prep.board, work_in, prep.frozen_dir, prep.stem, prep.stripped, prep.sha256)
    log = run_dir / "run.log"
    rec.log = str(log)
    out = run_dir / f"{prep.stem}_routed.kicad_pcb"
    cap = float(budgets["wall_cap_s"])
    rec.tool_config["wall_cap_s"] = cap

    if tool == "kct":
        from kicad_tools.benchmark.external.metrics import probe_backend

        backend = probe_backend()
        rec.tool_config["backend"] = backend.to_dict()
        import kicad_tools

        pkg = Path(kicad_tools.__file__).resolve()
        rec.tool_config["kicad_tools_package"] = str(pkg)
        if not backend.available or REPO not in pkg.parents:
            rec.outcome, rec.stage = "tool-failure", "native-backend"
            rec.reason = (
                "kct timing requires the C++ backend built in the active worktree "
                f"(available={backend.available}, package={pkg})"
            )
            return rec
        cmd = kct_command(local, out, budgets)
    elif spec is None:
        rec.outcome, rec.reason = "unsupported", f"{tool} not configured"
        return rec
    elif not preflight(spec, rec, run_dir):
        return rec
    elif tool == "tracemaker":
        assert spec.executable is not None
        cmd = tracemaker_command(spec.executable, local, out, run_dir, budgets)
        rec.tool_config["learned_state"] = (
            "isolated: --no-kb (knowledge base neither read nor written)"
        )
        rec.tool_config["gpu"] = "disabled: --no-gpu"
    else:
        assert spec.executable is not None
        dsn = run_dir / f"{prep.stem}.dsn"
        try:
            rec.lost_constraints = dsn_audit(
                local.pcb,
                prep.frozen_dir / f"{prep.stem}.kicad_pro",
                prep.frozen_dir / f"{prep.stem}.kicad_dru",
                dsn,
                rec.not_routing_rules,
            )
        except Exception as exc:  # noqa: BLE001
            rec.outcome, rec.stage, rec.reason = "scorer-failure", "dsn-export", repr(exc)
            return rec
        cmd = fastroute_command(
            spec.executable, dsn, run_dir / f"{prep.stem}.ses", run_dir, budgets
        )

    rec.commands.append(cmd)
    t0 = time.time()
    rc, timed_out, wall = run_bounded(cmd, run_dir, log, cap)
    rec.exit_code, rec.timed_out, rec.wall_s = rc, timed_out, round(wall, 2)

    produced = out
    if tool == "fastroute":
        produced = run_dir / f"{prep.stem}.ses"
    exists = _fresh(produced, t0)
    pre = classify(rec, exists)
    if pre is not None:
        rec.outcome = pre
        if exists:
            rec.partial_artifact = str(produced)
        rec.reason = {
            "timeout": f"killed at the {cap:g}s wall cap",
            "tool-failure": f"exit {rc} without output",
            "missing-output": "exit 0 but no output written",
        }[pre]
        if pre == "tool-failure":
            rec.stage = "exec"
        return rec

    # The router's working copy lives in run_dir, which the router can write. A
    # tool that moved footprints in its input AND its output would compare
    # clean against that copy, and the SES merge below uses it as its base, so
    # it must still be byte-identical to the prepared input.
    work_sha = sha256_file(work_in) if work_in.exists() else None
    if work_sha != prep.sha256:
        rec.outcome = "invalid-fixed-placement"
        rec.reason = "router modified its input"
        rec.placement_violations = [
            f"input: {work_in.name} sha256 {work_sha or 'missing'} != prepared {prep.sha256}"
        ]
        return rec

    if tool == "fastroute":
        try:
            ses_lost, counts = ses_import(produced, local.pcb, out)
        except Exception as exc:  # noqa: BLE001
            rec.outcome, rec.stage, rec.reason = "scorer-failure", "ses-import", repr(exc)
            return rec
        rec.interchange = counts
        rec.lost_constraints += ses_lost
    rec.output = str(out)

    try:
        # Baseline from the prepared input outside run_dir, never the router's copy.
        fp_in, fp_out = fingerprint(prep.pcb), fingerprint(out)
        violations = compare_fingerprints(fp_in, fp_out)
        rec.tool_config["zones_added_by_tool"] = added_zones(fp_in, fp_out)
    except Exception as exc:  # noqa: BLE001
        rec.outcome, rec.stage, rec.reason = "scorer-failure", "fingerprint", repr(exc)
        return rec
    if violations:
        rec.placement_violations = violations
        rec.outcome = "invalid-fixed-placement"
        rec.reason = f"{len(violations)} placement/net/outline change(s)"
        return rec

    rec.score = score(out, prep, int(board["copper_layers"]), base / "score", kicad_cli)
    if not rec.score["drc"].get("ran"):
        rec.outcome, rec.stage = "scorer-failure", "drc"
        rec.reason = "headline kicad-cli DRC unavailable: " + str(rec.score["drc"].get("note"))
    elif rec.lost_constraints:
        rec.outcome = "non-comparable"
        rec.reason = f"{len(rec.lost_constraints)} constraint(s) lost at the DSN/SES boundary"
    else:
        rec.outcome = "success"
    return rec


def score_input(prep: Prepared, board: dict[str, Any], base: Path, kicad_cli: str) -> RunRecord:
    rec = RunRecord(tool="input", board=prep.board, outcome="reference")
    rec.reason = "normalized input; pre-existing errors stay in every routed row's headline"
    rec.output = str(prep.pcb)
    rec.score = score(prep.pcb, prep, int(board["copper_layers"]), base / "score", kicad_cli)
    return rec


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _cmd_out(cmd: list[str]) -> str | None:
    try:
        return (
            subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, check=False
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def environment(budgets: dict[str, Any], kicad_cli: str) -> dict[str, Any]:
    from kicad_tools.benchmark.external.metrics import probe_backend

    dirty = _cmd_out(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"])
    return {
        "kct_revision": _cmd_out(["git", "-C", str(REPO), "rev-parse", "HEAD"]),
        "kct_tree_dirty": bool(dirty),
        "kct_worktree": str(REPO),
        "native_backend": probe_backend().to_dict(),
        "kicad_cli": kicad_cli,
        "kicad_cli_version": _cmd_out([kicad_cli, "--version"]),
        "python": sys.version.split()[0],
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
        },
        "budgets": budgets,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _tool_spec(name: str, args: argparse.Namespace, manifest: dict[str, Any]) -> ToolSpec:
    pin = manifest["pins"][name]
    checkout = getattr(args, f"{name}_dir")
    exe = getattr(args, f"{name}_exe")
    if exe is None and checkout is not None:
        exe = checkout / pin["default_executable"]
    return ToolSpec(
        name=name,
        executable=exe,
        checkout=checkout,
        pin=pin["commit"],
        build_log=getattr(args, f"{name}_build_log"),
    )


def cmd_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    problems = verify_manifest(manifest)
    if problems:
        print("manifest drift -- refusing to run (re-freeze deliberately):", *problems, sep="\n  ")
        return 2
    budgets = dict(manifest["budgets"])
    if args.wall_cap is not None:
        budgets["wall_cap_s"] = args.wall_cap
    work = args.work_dir.resolve()
    if REPO in work.parents or work == REPO:
        print("--work-dir must be outside the repository (scratch only)")
        return 2
    work.mkdir(parents=True, exist_ok=True)
    tools = [t for t in args.tools.split(",") if t]
    specs = {t: _tool_spec(t, args, manifest) for t in ("tracemaker", "fastroute")}
    env = environment(budgets, args.kicad_cli)
    env["pins"] = manifest["pins"]
    env["manifest_sha256"] = sha256_file(args.manifest)
    rows: list[dict[str, Any]] = []
    boards = {b["key"]: b for b in manifest["boards"]}
    for key in args.boards.split(","):
        board = boards[key]
        if board["status"] != "included":
            for tool in tools:
                rows.append(
                    RunRecord(
                        tool=tool,
                        board=key,
                        outcome="unsupported",
                        reason=board["exclusion_reason"],
                    ).to_dict()
                )
            continue
        prep = prepare_board(board, work / key / "input")
        print(f"== {key}: stripped {prep.stripped}, input sha256 {prep.sha256[:12]}", flush=True)
        ref = score_input(prep, board, work / key / "input-score", args.kicad_cli)
        ref.tool_config["normalized_input_sha256"] = prep.sha256
        ref.tool_config["stripped"] = prep.stripped
        rows.append(ref.to_dict())
        for tool in tools:
            print(f"   {key}/{tool} ...", flush=True)
            rec = run_tool(
                tool, specs.get(tool), prep, board, work / key / tool, budgets, args.kicad_cli
            )
            rec.tool_config["normalized_input_sha256"] = prep.sha256
            rows.append(rec.to_dict())
            print(f"   {key}/{tool}: {rec.outcome} {rec.reason or ''}", flush=True)
    results = {"environment": env, "stripping_policy": manifest["stripping_policy"], "rows": rows}
    (work / "results.json").write_text(json.dumps(results, indent=2, default=str))
    print_table(rows)
    print(f"\nresults: {work / 'results.json'}")
    return 0


def _cell(v: Any) -> str:
    return "unavailable" if v is None else str(v)


def print_table(rows: list[dict[str, Any]]) -> None:
    cols = [
        "Board", "Tool", "Outcome", "Connections", "DRC errors (total)", "DRC warnings",
        "Unconnected", "kct check (jlcpcb)", "Sync", "Vias", "Wirelength mm", "Clean pass",
        "Wall s", "Lost constraints",
    ]  # fmt: skip
    print("\n| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        s = r.get("score") or {}
        drc = s.get("drc") or {}
        comp = s.get("completion") or {}
        chk = s.get("kct_check") or {}
        sync = s.get("sync") or {}
        cells = [
            r["board"], r["tool"], r["outcome"],
            f"{comp['connections_routed']}/{comp['connections_total']}" if comp else "--",
            _cell(drc.get("errors_total")) if s else "--",
            _cell(drc.get("warnings")) if s else "--",
            _cell(drc.get("unconnected")) if s else "--",
            (_cell(chk.get("error_count")) if chk.get("ran") else "unavailable") if s else "--",
            (("in sync" if sync.get("in_sync") else "OUT OF SYNC") if sync.get("ran") else "unavailable") if s else "--",
            _cell(s.get("via_count")) if s else "--",
            _cell(s.get("wirelength_mm")) if s else "--",
            _cell(s.get("clean_pass")) if s else "--",
            _cell(r.get("wall_s")) + (" (TIMEOUT)" if r.get("timed_out") else ""),
            str(len(r.get("lost_constraints") or [])),
        ]  # fmt: skip
        print("| " + " | ".join(cells) + " |")


def cmd_freeze(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    manifest["boards"] = freeze_all()
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"froze {len(manifest['boards'])} boards into {args.manifest}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_manifest(load_manifest(args.manifest))
    for p in problems:
        print(p)
    print("manifest OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="re-hash the tree against the manifest")
    sub.add_parser("freeze", help="regenerate the manifest's board section from the tree")
    rp = sub.add_parser("run", help="prepare, route and score")
    rp.add_argument("--work-dir", type=Path, required=True, help="scratch dir outside the repo")
    rp.add_argument("--boards", default="00")
    rp.add_argument("--tools", default=",".join(TOOLS))
    rp.add_argument("--wall-cap", type=float, default=None, help="override budgets.wall_cap_s")
    rp.add_argument("--kicad-cli", default="kicad-cli")
    for name in ("tracemaker", "fastroute"):
        rp.add_argument(f"--{name}-dir", type=Path, default=None, help="pinned upstream checkout")
        rp.add_argument(
            f"--{name}-exe", type=Path, default=None, help="executable (default: from checkout)"
        )
        rp.add_argument(
            f"--{name}-build-log", type=Path, default=None, help="log of a failed pinned build"
        )
    args = ap.parse_args(argv)
    return {"run": cmd_run, "freeze": cmd_freeze, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
