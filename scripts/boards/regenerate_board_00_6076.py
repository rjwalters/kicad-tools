"""Deterministic-UUID regeneration of board 00-simple-led, with its package (Issue #6076).

The last of the #6076 regenerations; companion of ``regenerate_uuids_6076.py``
(05-07) and ``regenerate_boards_03_04_6076.py`` (03/04), whose UUID masking
and mapping helpers it reuses.

1. **Generator.**  ``generate_design.py``'s ``create_project``,
   ``create_led_schematic`` and ``create_led_pcb`` rewrite the project,
   schematic and unrouted PCB.  The project and unrouted PCB must equal the
   committed files modulo UUIDs.  The schematic may differ only in its
   ``generator_version`` header ("9.0" -> "10.0", centralised by #4390 after
   the schematic was last regenerated in #3962).
2. **Routed PCB: pure UUID remap, no re-route.**  The recipe's
   ``route_pcb`` -> ``_fill_zones_after_route`` canonicalises the routed
   board with an *empty* keep-set (#6052), so every non-copper UUID in the
   committed routed board is ``canonicalize(old unrouted)``'s, and a fresh
   build would carry ``canonicalize(new unrouted)``'s.  Both are computed,
   paired positionally (the two unrouted boards are identical modulo UUIDs)
   and substituted into the committed routed bytes.  Every structural UUID
   in the routed board must be explained by that map; the rest is top-level
   copper and zones (content-keyed, unchanged) and footprint fields KiCad
   added after canonicalisation (random v4), which are re-derived from their
   new parent the way ``canonical_uuids`` does.  The result must equal the
   committed board with UUIDs masked, and a further ``canonicalize`` pass
   must be a no-op.
3. **LVS.**  The recipe's ``run_lvs`` rewrites ``output/lvs.json`` and must be
   clean on both comparators.
4. **Package.**  ``boards/00-simple-led/package_release.py`` (``kct readiness
   --generate``) is re-run until the routed PCB it refills and saves stops
   changing, so the committed routed board is producer-settled and the
   evidence, manifest, ``manufacturing.zip`` and ``readiness.json`` are all
   written by the producer in one final run, after the last PCB change.
5. **Bar.**  On a scratch copy: ``kicad-cli pcb drc --refill-zones``,
   ``kct check --mfr jlcpcb``, ``kct net-status``, ``kct validate --sync``; and
   ``read_readiness()`` on the board itself.

``output/erc_report.json`` and ``output/drc_report.json`` have no producer
(the recipe no longer writes them) and are left as they are.

Rerunning is idempotent: once regenerated, every UUID map is the identity.

usage (repository root, KiCad 10 ``kicad-cli`` on PATH)::

    uv run --with shapely python scripts/boards/regenerate_board_00_6076.py
    uv run --with shapely python scripts/boards/regenerate_board_00_6076.py --measure [BOARD_DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD = REPO_ROOT / "boards" / "00-simple-led"
HERE = Path(__file__).resolve().parent
TIER = "jlcpcb"
ROUTED = "simple_led_routed.kicad_pcb"
UNROUTED = "simple_led.kicad_pcb"
SCHEMATIC = "simple_led.kicad_sch"
PROJECT = "simple_led.kicad_pro"
COPPER = ("segment", "via", "arc")
#: The only schematic delta allowed beyond UUIDs (see module docstring).
SCHEMATIC_HEADER_DELTA = ['-\t(generator_version "9.0")', '+\t(generator_version "10.0")']


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


uuid_tools = _load("regenerate_uuids_6076", HERE / "regenerate_uuids_6076.py")

from kicad_tools.core.canonical_uuids import canonicalize_board_uuids  # noqa: E402
from kicad_tools.sexp import parse_string, serialize_sexp  # noqa: E402

UUID_RE, mask = uuid_tools.UUID_RE, uuid_tools.mask


def log(message: str) -> None:
    print(message, flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ids(text: str) -> list[str]:
    return [u.lower() for u in UUID_RE.findall(text)]


def _pair(a: str, b: str) -> dict[str, str]:
    """Positional old->new map between two texts identical modulo UUIDs."""
    assert mask(a) == mask(b), "texts differ beyond UUIDs"
    out: dict[str, str] = {}
    for x, y in zip(_ids(a), _ids(b), strict=True):
        assert out.setdefault(x, y) == y, f"inconsistent mapping for {x}"
    assert len(set(out.values())) == len(out), "non-injective mapping"
    return out


def _canonical(text: str) -> str:
    doc = parse_string(text)
    canonicalize_board_uuids(doc, keep=())  # what _fill_zones_after_route(args=None) does
    return serialize_sexp(doc)


def _top_level_ids(text: str, tags: tuple[str, ...]) -> set[str]:
    out = set()
    for child in parse_string(text).children:
        if child.name in tags:
            node = child.find_child("uuid")
            if node is not None:
                out.add(node.get_string(0).lower())
    return out


def _parent_derived(text: str, ids: set[str]) -> bool:
    """True if canonicalisation re-mints exactly ``ids`` once they are removed."""
    if not ids:
        return True
    doc = parse_string(text)

    def strip(node) -> None:  # type: ignore[no-untyped-def]
        for child in list(node.children):
            if child.name == "uuid" and child.get_string(0).lower() in ids:
                node.children.remove(child)
            elif child.name is not None:
                strip(child)

    strip(doc)
    canonicalize_board_uuids(doc, keep=set(_ids(text)) - ids)
    return set(_ids(serialize_sexp(doc))) == set(_ids(text))


# --------------------------------------------------------------------------- remap
def remap_routed(old_unrouted: str, new_unrouted: str, routed: Path) -> dict[str, int]:
    """Carry the regenerated generator UUIDs onto the committed routed board, in place."""
    original = routed.read_text()
    structural = _pair(_canonical(old_unrouted), _canonical(new_unrouted))
    content_keyed = _top_level_ids(original, (*COPPER, "zone"))
    unexplained = {
        u
        for u in set(_ids(original)) - content_keyed - set(structural)
        if uuid.UUID(u).version != 4
    }
    # On a rerun the KiCad-added fields already carry the UUID derived from
    # their (regenerated) parent; anything else is unexplained.
    assert _parent_derived(original, unexplained), (
        f"routed UUIDs not derived from the unrouted board: {sorted(unexplained)[:5]}"
    )
    step1 = UUID_RE.sub(lambda m: structural.get(m.group(0).lower(), m.group(0)), original)

    # KiCad-added footprint fields (v4): re-derive from their new parent UUID.
    before = serialize_sexp(parse_string(step1))
    staged = parse_string(step1)
    copper_order = [c for c in staged.children if c.name in COPPER]
    canonicalize_board_uuids(staged, keep={u for u in _ids(step1) if uuid.UUID(u).version != 4})
    slots = [i for i, c in enumerate(staged.children) if c.name in COPPER]
    for i, node in zip(slots, copper_order, strict=True):  # a remap never moves copper
        staged.children[i] = node
    invented = {k: v for k, v in _pair(before, serialize_sexp(staged)).items() if k != v}
    assert all(uuid.UUID(k).version == 4 for k in invented), "re-derived a non-invented UUID"
    final = UUID_RE.sub(lambda m: invented.get(m.group(0).lower(), m.group(0)), step1)

    assert mask(final) == mask(original), "routed board changed beyond UUIDs"
    # What a fresh build would ship: canonicalising again changes no UUID.
    assert set(_ids(_canonical(final))) == set(_ids(final)), "a canonicalize pass is not a no-op"
    routed.write_text(final)
    changed = set(_ids(original)) - set(_ids(final))
    return {
        "uuids": len(set(_ids(final))),
        "changed": len(changed),
        "kicad_invented_rederived": len(invented),
    }


def regenerate() -> None:
    out = BOARD / "output"
    for cache in out.rglob("__pycache__"):
        shutil.rmtree(cache)
    old = {name: (out / name).read_text() for name in (PROJECT, SCHEMATIC, UNROUTED)}
    sys.path.insert(0, str(BOARD))
    recipe = runpy.run_path(str(BOARD / "generate_design.py"), run_name="regen_00")
    recipe["create_project"](out, "simple_led")
    recipe["create_led_schematic"](out)
    recipe["create_led_pcb"](out)

    with tempfile.TemporaryDirectory(prefix="regen-00-") as tmp:
        for name, text in old.items():
            (Path(tmp) / name).write_text(text)
        assert (out / PROJECT).read_text() == old[PROJECT], "project file changed"
        pcb_delta = uuid_tools.masked_delta(Path(tmp) / UNROUTED, out / UNROUTED)
        assert not pcb_delta, f"unrouted PCB changed beyond UUIDs: {pcb_delta[:5]}"
        sch_delta = uuid_tools.masked_delta(Path(tmp) / SCHEMATIC, out / SCHEMATIC)
        assert sch_delta in ([], SCHEMATIC_HEADER_DELTA), f"schematic delta: {sch_delta[:5]}"
    log(f"  project identical; unrouted PCB identical modulo UUIDs; schematic delta {sch_delta}")

    stats = remap_routed(old[UNROUTED], (out / UNROUTED).read_text(), out / ROUTED)
    log(f"  routed PCB remapped (no re-route): {stats}")
    assert recipe["run_lvs"](out / SCHEMATIC, out / ROUTED, out), "LVS not clean"
    log("  LVS (label + copper) clean; output/lvs.json rewritten")


def package(max_runs: int = 4) -> None:
    """Run the package producer to a fixed point of the routed PCB it saves."""
    routed = BOARD / "output" / ROUTED
    for attempt in range(1, max_runs + 1):
        before = sha256(routed)
        proc = subprocess.run(
            [sys.executable, str(BOARD / "package_release.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"package_release.py failed:\n{proc.stdout[-3000:]}{proc.stderr[-3000:]}"
            )
        settled = sha256(routed) == before
        log(f"  package_release.py run {attempt}: routed PCB {'settled' if settled else 'changed'}")
        if settled:
            return
    raise RuntimeError("routed PCB did not settle under the package producer")


# --------------------------------------------------------------------------- bar
def _kct(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "kicad_tools.cli", *args], capture_output=True, text=True
    )


def measure(board: Path) -> dict:
    from kicad_tools.cli.board_readiness import read_readiness

    bar: dict = {"readiness": read_readiness(board).get("status")}
    with tempfile.TemporaryDirectory(prefix="regen-00-bar-") as tmp:
        copy = Path(tmp) / board.name
        shutil.copytree(board, copy)
        out = copy / "output"
        routed, sch = out / ROUTED, out / SCHEMATIC

        report = Path(tmp) / "drc.json"
        subprocess.run(
            ["kicad-cli", "pcb", "drc", str(routed), "--refill-zones", "--format", "json",
             "--severity-error", "--severity-warning", "--output", str(report)],
            capture_output=True, text=True,
        )  # fmt: skip
        data = json.loads(report.read_text())
        bar["kicad_cli_drc_errors"] = sum(v.get("severity") == "error" for v in data["violations"])
        bar["kicad_cli_drc_warnings"] = len(data["violations"]) - bar["kicad_cli_drc_errors"]
        bar["kicad_cli_unconnected"] = len(data.get("unconnected_items", []))

        check = Path(tmp) / "check.json"
        _kct("check", str(routed), "--mfr", TIER, "--format", "json", "--output", str(check))
        data = json.loads(check.read_text())
        bar["kct_check_errors"] = data["summary"]["errors"]
        bar["kct_check_warnings"] = data["summary"]["warnings"]

        summary = json.loads(_kct("net-status", str(routed), "--format", "json").stdout)["summary"]
        bar["nets_complete"] = f"{summary['complete']}/{summary['total_nets']}"
        bar["routed_pct"] = round(100.0 * summary["complete"] / summary["total_nets"], 1)

        sync = json.loads(
            _kct("validate", "--sync", "--schematic", str(sch), "--pcb", str(routed),
                 "--format", "json").stdout
        )  # fmt: skip
        bar["sync_errors"] = sync.get("summary", {}).get("errors")
        bar["in_sync"] = sync.get("in_sync")
    return bar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--measure",
        nargs="?",
        const=str(BOARD),
        metavar="BOARD_DIR",
        help="only measure the bar for BOARD_DIR (default: board 00) and exit",
    )
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    if args.measure:
        print(json.dumps(measure(Path(args.measure).resolve()), indent=2))
        return 0
    log("== board 00-simple-led")
    regenerate()
    package()
    bar = measure(BOARD)
    log(f"  bar: {json.dumps(bar)}")
    ok = (
        bar["readiness"] == "ready"
        and bar["kicad_cli_drc_errors"] == 0
        and bar["kicad_cli_unconnected"] == 0
        and bar["kct_check_errors"] == 0
        and bar["routed_pct"] == 100.0
        and bar["sync_errors"] == 0
        and bar["in_sync"] is True
    )
    log(f"  bar {'MET' if ok else 'NOT MET'}")
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("KICAD_CLI_DIR", "/Applications/KiCad/KiCad.app/Contents/MacOS")]
        + os.environ.get("PATH", "").split(os.pathsep)
    )
    raise SystemExit(main())
