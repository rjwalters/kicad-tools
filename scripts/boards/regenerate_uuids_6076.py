"""One-shot deterministic-UUID regeneration of boards 05/06/07 design files (Issue #6076).

PR #6247 made the schematic library and the fleet generators mint uuid5
UUIDs, but the committed outputs still carried the old random ones.  This
script regenerates them once, without re-routing anything:

* the schematic (05, 06, 07) and unrouted PCB (05, 06) are rewritten from
  each board's own generator (05 ``redesign/hardware.py``, 06
  ``assembled-demo/generate.py``, 07 ``real_design/build_source.py``);
  each must equal the committed file modulo UUIDs, apart from the
  explained deltas it logs (05 pads gain UUIDs; 05/07 embedded
  ``lib_symbols`` property order from f33f4ddd);
* the committed routed PCB keeps its copper.  Only the UUIDs the generator
  sourced are remapped: footprints and pads by reference/number (05), or the
  positional UUID map of the unrouted PCB + schematic (06).  The remapped
  file must equal the committed one with UUIDs masked.  Board 07's PCBs are
  hash-bound reviewed fixtures and are not touched.

Evidence is NOT rewritten here.  Run ``scripts/boards/refresh_readiness.py``
afterwards; it re-runs ERC/DRC/check/net-status/sync and re-pins
readiness only if every gate passes.

usage: uv run python scripts/boards/regenerate_uuids_6076.py [--log DIR]
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
LOG = Path(sys.argv[sys.argv.index("--log") + 1]) if "--log" in sys.argv else None
if LOG:
    LOG.mkdir(parents=True, exist_ok=True)
BOARDS = WT / "boards"
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ID = ("uuid", "tstamp")

from kicad_tools.sexp import parse_file  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def mask(text: str) -> str:
    return UUID_RE.sub("UUID", text)


def masked_delta(old: Path, new: Path) -> list[str]:
    import difflib

    a = mask(old.read_text()).splitlines()
    b = mask(new.read_text()).splitlines()
    return [
        line
        for line in difflib.unified_diff(a, b, lineterm="", n=0)
        if not line.startswith(("---", "+++", "@@"))
    ]


# --------------------------------------------------------------------------- remaps
def positional_map(pairs: list[tuple[Path, Path]]) -> dict[str, str]:
    m: dict[str, str] = {}
    for old, new in pairs:
        a, b = old.read_text(), new.read_text()
        assert mask(a) == mask(b), f"{old} vs {new}: not identical modulo UUIDs"
        for x, y in zip(UUID_RE.findall(a), UUID_RE.findall(b), strict=True):
            x, y = x.lower(), y.lower()
            assert m.get(x, y) == y, f"inconsistent mapping {x}"
            m[x] = y
    assert len(set(m.values())) == len(m), "non-injective mapping"
    return m


def _uid(n):
    for c in n.children:
        if not c.is_atom and c.name in ID:
            return c.get_string(0).lower()
    return None


def _ref(fp):
    for c in fp.children:
        if not c.is_atom and c.name == "property" and c.get_string(0) == "Reference":
            return c.get_string(1)
    raise ValueError("footprint without Reference")


def _fps(doc):
    d = {}
    for c in doc.children:
        if not c.is_atom and c.name == "footprint":
            r = _ref(c)
            assert r not in d, r
            d[r] = c
    return d


def _fp_keys(fp):
    out = {("footprint",): fp}
    cnt: dict[str, int] = defaultdict(int)
    for c in fp.children:
        if not c.is_atom and c.name == "pad":
            num = c.get_string(0)
            out[("pad", num, cnt[num])] = c
            cnt[num] += 1
    return out


def _compact(node, tag):
    child = node.find(tag)
    if child is None:
        return None
    if tag == "net":  # numbering is file-local; compare the net name
        return [a.value for a in child.children if a.is_atom][-1]
    return child.to_string(compact=True)


def structural_map(old_unrouted: Path, new_unrouted: Path, routed: Path) -> dict[str, str]:
    """routed UUID -> new UUID for footprints (by reference) and pads (number+ordinal)."""
    o, n, r = parse_file(old_unrouted), parse_file(new_unrouted), parse_file(routed)
    NO, NN, NR = _fps(o), _fps(n), _fps(r)
    assert set(NO) == set(NN) == set(NR)
    m: dict[str, str] = {}
    for ref in sorted(NN):
        kn, kr = _fp_keys(NN[ref]), _fp_keys(NR[ref])
        assert set(kn) == set(kr), ref
        for key, nnode in kn.items():
            rnode = kr[key]
            if key[0] == "pad":
                for tag in ("at", "size", "net"):
                    assert _compact(nnode, tag) == _compact(rnode, tag), (ref, key, tag)
            nu, ru = _uid(nnode), _uid(rnode)
            if nu is None:
                continue
            assert ru is not None, (ref, key)
            assert m.get(ru, nu) == nu
            m[ru] = nu

    # top-level non-footprint UUID items: positional among same tag (old vs new unrouted)
    def tops(doc):
        d = defaultdict(list)
        for c in doc.children:
            if not c.is_atom and c.name not in ID and c.name != "footprint" and _uid(c):
                d[c.name].append(c)
        return d

    to, tn = tops(o), tops(n)
    assert set(to) == set(tn)
    for tag in to:
        assert len(to[tag]) == len(tn[tag]), tag
        for x, y in zip(to[tag], tn[tag], strict=True):
            m[_uid(x)] = _uid(y)
    assert len(set(m.values())) == len(m), "non-injective mapping"
    return m


def apply_map(m: dict[str, str], src: Path, dst: Path) -> tuple[int, int]:
    text = src.read_text()
    present = {u.lower() for u in UUID_RE.findall(text)}
    clash = {v for k, v in m.items() if k != v and v in present and v not in m}
    assert not clash, f"new UUIDs already in {src}: {sorted(clash)[:3]}"
    hits = [0, 0]

    def sub(mo):
        k = mo.group(0).lower()
        if k in m:
            hits[0] += 1
            return m[k]
        hits[1] += 1
        return mo.group(0)

    out = UUID_RE.sub(sub, text)
    dst.write_text(out)
    assert mask(out) == mask(text)
    return hits[0], hits[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def record(name: str, payload) -> None:
    if LOG:
        (LOG / name).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- board 05
def board05() -> None:
    log("== board 05")
    board = BOARDS / "05-bldc-motor-controller"
    out = board / "output"
    with tempfile.TemporaryDirectory(prefix="regen-c-05-") as tmp:
        gen = Path(tmp) / "gen"
        subprocess.run(
            [sys.executable, "hardware.py", str(gen)],
            cwd=board / "redesign",
            check=True,
            capture_output=True,
        )
        for name in (
            "bldc_controller.kicad_pro",
            "board05_revB.kicad_sym",
            "fp-lib-table",
            "sym-lib-table",
        ):
            log(
                f"  {name}: {'identical' if (gen / name).read_bytes() == (out / name).read_bytes() else 'DIFFERS (not committed; generator drift)'}"
            )
        pcb_delta = masked_delta(
            out / "bldc_controller.kicad_pcb", gen / "bldc_controller.kicad_pcb"
        )
        assert all(line[0] == "+" and line[1:].strip() == '(uuid "UUID")' for line in pcb_delta), (
            pcb_delta[:5]
        )
        log(f"  unrouted PCB masked delta: {len(pcb_delta)} added (uuid) lines, nothing else")
        sch_delta = masked_delta(
            out / "bldc_controller.kicad_sch", gen / "bldc_controller.kicad_sch"
        )
        log(
            f"  schematic masked delta: {len(sch_delta)} lines (lib_symbols property order of ATmega328P-A, f33f4ddd)"
        )
        record("05-sch-masked-delta.txt", sch_delta)
        m = structural_map(
            out / "bldc_controller.kicad_pcb",
            gen / "bldc_controller.kicad_pcb",
            out / "bldc_controller_routed.kicad_pcb",
        )
        record("05-routed-map.json", m)
        hits = apply_map(
            m, out / "bldc_controller_routed.kicad_pcb", Path(tmp) / "routed.kicad_pcb"
        )
        log(f"  routed PCB: {len(m)} UUIDs remapped ({hits[0]} occurrences), {hits[1]} kept")
        shutil.copy2(Path(tmp) / "routed.kicad_pcb", out / "bldc_controller_routed.kicad_pcb")
        shutil.copy2(gen / "bldc_controller.kicad_pcb", out / "bldc_controller.kicad_pcb")
        shutil.copy2(gen / "bldc_controller.kicad_sch", out / "bldc_controller.kicad_sch")


# --------------------------------------------------------------------------- board 06
def board06() -> None:
    log("== board 06")
    board = BOARDS / "06-diffpair-test"
    out = board / "output"
    with tempfile.TemporaryDirectory(prefix="regen-c-06-") as tmp:
        gen = Path(tmp) / "gen"
        subprocess.run(
            [sys.executable, str(board / "assembled-demo/generate.py"), str(gen)],
            check=True,
            capture_output=True,
        )
        for name in sorted(p.name for p in gen.iterdir()):
            if name in ("diffpair_test.kicad_pcb", "diffpair_test.kicad_sch"):
                continue
            c = out / name
            state = (
                "not committed in output/"
                if not c.exists()
                else (
                    "identical"
                    if c.read_bytes() == (gen / name).read_bytes()
                    else "DIFFERS (not committed; generator drift)"
                )
            )
            log(f"  {name}: {state}")
        m = positional_map(
            [
                (out / "diffpair_test.kicad_pcb", gen / "diffpair_test.kicad_pcb"),
                (out / "diffpair_test.kicad_sch", gen / "diffpair_test.kicad_sch"),
            ]
        )
        log(f"  unrouted PCB + schematic identical modulo UUIDs; {len(m)} UUIDs mapped")
        record("06-map.json", m)
        hits = apply_map(m, out / "diffpair_test_routed.kicad_pcb", Path(tmp) / "routed.kicad_pcb")
        log(f"  routed PCB: {hits[0]} occurrences remapped, {hits[1]} kept")
        shutil.copy2(Path(tmp) / "routed.kicad_pcb", out / "diffpair_test_routed.kicad_pcb")
        shutil.copy2(gen / "diffpair_test.kicad_pcb", out / "diffpair_test.kicad_pcb")
        shutil.copy2(gen / "diffpair_test.kicad_sch", out / "diffpair_test.kicad_sch")


# --------------------------------------------------------------------------- board 07
def board07() -> None:
    log("== board 07")
    board = BOARDS / "07-matchgroup-test"
    out = board / "output"
    with tempfile.TemporaryDirectory(prefix="regen-c-07-") as tmp:
        gen = Path(tmp) / "gen"
        builder = load("board07_build_source", board / "real_design/build_source.py")
        builder.build(gen)
        same = {
            "sdram_demo.kicad_pcb": "matchgroup_test.kicad_pcb",
            "sdram_demo.kicad_pro": "matchgroup_test.kicad_pro",
            "circuit.json": "circuit.json",
            "net_class_map.json": "net_class_map.json",
            "sdram_constraints.json": "sdram_constraints.json",
        }
        for g, c in same.items():
            assert (gen / g).read_bytes() == (out / c).read_bytes(), (
                f"{g} drifted from committed {c}"
            )
        log(f"  build_source outputs other than the schematic are byte-identical: {sorted(same)}")
        sch_delta = masked_delta(out / "matchgroup_test.kicad_sch", gen / "sdram_demo.kicad_sch")
        log(
            f"  schematic masked delta: {len(sch_delta)} lines (lib_symbols property order of IS42S16400J-xT and AP2112K-3.3, f33f4ddd)"
        )
        record("07-sch-masked-delta.txt", sch_delta)
        assert (out / "readiness/checked.kicad_sch").read_bytes() == (
            out / "matchgroup_test.kicad_sch"
        ).read_bytes()
        shutil.copy2(gen / "sdram_demo.kicad_sch", out / "matchgroup_test.kicad_sch")
        shutil.copy2(gen / "sdram_demo.kicad_sch", out / "readiness/checked.kicad_sch")


if __name__ == "__main__":
    os.chdir(WT)
    board05()
    board06()
    board07()
