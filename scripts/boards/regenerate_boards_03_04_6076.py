"""Deterministic-UUID regeneration of boards 03 and 04, with their evidence (Issue #6076).

The companion of ``regenerate_uuids_6076.py`` (boards 05-07).  It reuses that
script's UUID masking/mapping helpers and ``refresh_readiness.py``'s
deterministic archive and manifest helpers rather than duplicating them.
Neither board fits ``refresh_readiness.py`` as a whole, because its gates
require a ``ready`` report with zero warnings:

* **Board 03** is driven by its own recipe.  ``generate_design.py`` replays
  the reviewed ``routing-plan.json``, so the routed PCB keeps its copper by
  construction.  The recipe is run, then ``check_manufacturing.py``, then the
  recipe again so the bundle manifest covers the fresh check report.  The
  outer archive is rebuilt deterministically and ``kct readiness --verify``
  issues the receipt.  The receipt is promoted to ``output/readiness.json``
  with the additive exact-source bindings main carried.  It is *blocked*
  today by two pre-existing J1 ``silk_edge_clearance`` warnings (#6269),
  and it is published as blocked.
* **Board 04**: the schematic and unrouted PCB come from ``generate_design.py``.
  The committed routed PCB gets a pure UUID remap with no re-route.
  Footprints are mapped by reference, and generator silk is mapped by
  position within each footprint.  KiCad-invented pad and property UUIDs are
  re-derived from the new parent UUID the way ``canonical_uuids`` does
  (#6109).  Router copper and zones are untouched.  The evidence is then
  refreshed:
  - the recipe's ``run_drc`` and ``write_lvs_report``;
  - native ERC (local KiCad, plus the ``kicad/kicad:10.0.5`` Linux image for
    ``native-erc-linux.json``);
  - native DRC;
  - fill consistency, from readiness' own refill and area code;
  - ``design-source/`` and ``kicad_project.zip``;
  - the manifest and ``check_manufacturing.py``.

  Its legacy ``readiness.json`` is **not** re-pinned.  No producer writes that
  format, and its "zero native DRC violations" claim no longer holds on
  main.  It is deferred to #6020 and tracked in #6269.

Gerbers, renders, PDFs, the README and the BOM/CPL do not depend on UUIDs
and are not regenerated here.

Rerunning is idempotent.  Once the board is regenerated, every UUID map is
the identity.

usage (repository root, KiCad 10 ``kicad-cli`` and docker available)::

    uv run --with shapely python scripts/boards/regenerate_boards_03_04_6076.py \\
        [--board 03] [--board 04] [--base-commit SHA]
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARDS = REPO_ROOT / "boards"
HERE = Path(__file__).resolve().parent
LINUX_ERC_IMAGE = "kicad/kicad:10.0.5"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


uuid_tools = _load("regenerate_uuids_6076", HERE / "regenerate_uuids_6076.py")
refresh_tools = _load("refresh_readiness", HERE / "refresh_readiness.py")

from kicad_tools.core.canonical_uuids import canonicalize_board_uuids  # noqa: E402
from kicad_tools.core.sexp_file import load_pcb  # noqa: E402
from kicad_tools.sexp import parse_file, serialize_sexp  # noqa: E402

COPPER = ("segment", "via", "arc")


def log(message: str) -> None:
    print(message, flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(map(str, cmd))} exited {proc.returncode}:\n{proc.stderr[-2000:]}"
        )
    return proc


def drop_bytecode(board: Path) -> None:
    """Never let an interpreter cache under output/ into a hashed bundle."""
    for cache in (board / "output").rglob("__pycache__"):
        shutil.rmtree(cache)


def refresh_design_source(board: Path) -> None:
    source = board / "output/manufacturing/design-source"
    for path in sorted(source.iterdir()):
        shutil.copy2(board / path.name, path)


# --------------------------------------------------------------------------- board 04 remap
def _uid(node) -> str | None:
    child = node.find_child("uuid")
    return child.get_string(0).lower() if child is not None else None


def _fps(doc) -> dict:
    """Footprints by reference; generated boards spell it ``fp_text reference``."""
    out = {}
    for fp in doc.find_children("footprint"):
        refs = [
            c.get_string(1)
            for c in fp.children
            if (c.name == "property" and c.get_string(0) == "Reference")
            or (c.name == "fp_text" and c.get_string(0) == "reference")
        ]
        assert len(refs) == 1 and refs[0] not in out, refs
        out[refs[0]] = fp
    return out


def _children_by_tag(fp) -> dict[str, list]:
    out: dict[str, list] = {}
    for child in fp.children:
        if child.name is not None and _uid(child):
            out.setdefault(child.name, []).append(child)
    return out


def remap_routed(old_unrouted: Path, new_unrouted: Path, routed: Path) -> dict:
    """Rewrite ``routed``'s generator-sourced UUIDs in place; copper/zones untouched."""
    positional = uuid_tools.positional_map([(old_unrouted, new_unrouted)])
    new_doc, doc = parse_file(new_unrouted), load_pcb(routed)
    new_fps, fps = _fps(new_doc), _fps(doc)
    assert set(new_fps) == set(fps), "footprint references differ"
    rename: dict[str, str] = {}
    for ref, fp in fps.items():
        rename[_uid(fp)] = _uid(new_fps[ref])
        # Generator silk the routed board inherited from an unrouted build.
        for tag, nodes in _children_by_tag(fp).items():
            for node in nodes:
                if _uid(node) in positional:
                    rename[_uid(node)] = positional[_uid(node)]
    tops = [c for c in doc.children if c.name == "gr_rect"]
    new_tops = [c for c in new_doc.children if c.name == "gr_rect"]
    assert len(tops) == len(new_tops) == 1
    rename[_uid(tops[0])] = _uid(new_tops[0])
    assert len(set(rename.values())) == len(rename), "non-injective footprint map"

    text = uuid_tools.UUID_RE.sub(
        lambda m: rename.get(m.group(0).lower(), m.group(0)), serialize_sexp(doc)
    )
    from kicad_tools.sexp import parse_string

    staged = parse_string(text)
    copper_order = [c for c in staged.children if c.name in COPPER]
    keep = set(rename.values()) | {_uid(c) for c in staged.children if c.name in (*COPPER, "zone")}
    canonicalize_board_uuids(staged, keep=keep)
    # canonicalize also sorts top-level copper by UUID (#6052); a pure remap
    # must not move copper, so restore the committed order.
    slots = [i for i, c in enumerate(staged.children) if c.name in COPPER]
    for i, node in zip(slots, copper_order, strict=True):
        staged.children[i] = node

    # Apply the resulting old->new map to the committed bytes, so nothing but
    # UUID strings changes.
    olds = uuid_tools.UUID_RE.findall(serialize_sexp(load_pcb(routed)))
    news = uuid_tools.UUID_RE.findall(serialize_sexp(staged))
    final: dict[str, str] = {}
    for old, new in zip(olds, news, strict=True):
        assert final.setdefault(old.lower(), new.lower()) == new.lower()
    assert len(set(final.values())) == len(final)
    original = routed.read_text()
    remapped = uuid_tools.UUID_RE.sub(lambda m: final[m.group(0).lower()], original)
    assert uuid_tools.mask(remapped) == uuid_tools.mask(original)
    routed.write_text(remapped)
    changed = {k: v for k, v in final.items() if k != v}
    return {"uuids": len(final), "changed": len(changed)}


def native_erc_linux(output: Path, schematic: str, target: Path) -> None:
    run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{output}:/w", "-w", "/w", LINUX_ERC_IMAGE,
            "kicad-cli", "sch", "erc", "--format", "json",
            "--severity-error", "--severity-warning", "--units", "mm",
            "-o", f"/w/{target.relative_to(output).as_posix()}", f"/w/{schematic}",
        ]
    )  # fmt: skip


def fill_consistency(pcb: Path) -> dict[str, float]:
    from kicad_tools.cli.readiness_cmd import _copy_fill_context, _fill_areas, _refill_and_save

    with tempfile.TemporaryDirectory(prefix="regen-04-fill-") as tmp:
        refilled = _copy_fill_context(pcb, Path(tmp))
        result = _refill_and_save(refilled)
        if not result.ok:
            raise RuntimeError(f"refill failed: {result.detail}")
        saved, fresh = _fill_areas(pcb), _fill_areas(refilled)
    assert set(saved) == set(fresh), (saved, fresh)
    return {layer: abs(saved[layer] - fresh[layer]) for layer in sorted(saved)}


def board04() -> None:
    log("== board 04")
    board = BOARDS / "04-stm32-devboard"
    out, mfg = board / "output", board / "output/manufacturing"
    drop_bytecode(board)
    sys.path.insert(0, str(board))
    recipe = runpy.run_path(str(board / "generate_design.py"), run_name="regen_04")
    with tempfile.TemporaryDirectory(prefix="regen-04-") as tmp:
        old_unrouted = Path(tmp) / "old_unrouted.kicad_pcb"
        shutil.copy2(out / "stm32_devboard.kicad_pcb", old_unrouted)
        # Pad pin-type annotation is #6025's step, not this one.
        recipe["create_stm32_schematic"](out)
        recipe["create_stm32_pcb"](out)
        pcb_delta = uuid_tools.masked_delta(old_unrouted, out / "stm32_devboard.kicad_pcb")
        assert not pcb_delta, f"unrouted PCB changed beyond UUIDs: {pcb_delta[:5]}"
        stats = remap_routed(
            old_unrouted, out / "stm32_devboard.kicad_pcb", out / "stm32_devboard_routed.kicad_pcb"
        )
    log(f"  unrouted PCB identical modulo UUIDs; routed remap {stats}")

    routed, sch = out / "stm32_devboard_routed.kicad_pcb", out / "stm32_devboard.kicad_sch"
    log(f"  recipe run_drc: {recipe['run_drc'](routed)}")
    from kicad_tools.lvs import write_lvs_report

    log(
        f"  copper/label LVS: {write_lvs_report(sch, routed, out, require_clean=True, run_copper=True, run_label=False)}"
    )

    readiness = out / "readiness"
    run(["kicad-cli", "sch", "erc", "--format", "json", "--severity-error", "--severity-warning",
         "--units", "mm", "-o", str(readiness / "native-erc.json"), str(sch)])  # fmt: skip
    native_erc_linux(out, sch.name, readiness / "native-erc-linux.json")
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-error",
                    "--severity-warning", "--schematic-parity", "--units", "mm",
                    "-o", str(readiness / "native-drc.json"), str(routed)],
                   capture_output=True, check=False)  # fmt: skip
    delta = fill_consistency(routed)
    (readiness / "fill-consistency.json").write_text(json.dumps(delta, indent=2) + "\n")
    log(f"  fill consistency (saved vs independent refill): {delta}")
    for name in (
        "native-erc.json",
        "native-erc-linux.json",
        "native-drc.json",
        "fill-consistency.json",
    ):
        shutil.copy2(readiness / name, mfg / name)

    refresh_design_source(board)
    changed = refresh_tools.rebuild_archive(mfg / "kicad_project.zip", [out, board])
    log(f"  kicad_project.zip rebuilt; changed members {changed}")
    refresh_tools.refresh_manifest_files(mfg)

    check = _load("board04_check_manufacturing", board / "check_manufacturing.py")
    with tempfile.TemporaryDirectory(prefix="regen-04-check-") as tmp:
        data = check.check(routed, Path(tmp) / "check-report.json")
        report = (Path(tmp) / "check-report.json").read_bytes()
    log(f"  check_manufacturing: {data['summary']['errors']} errors, "
        f"{data['summary']['warnings']} warnings, overall {data['meta_checks']['overall']}")  # fmt: skip
    (mfg / "check-report.json").write_bytes(report)
    (readiness / "kct-check.json").write_bytes(report)
    refresh_tools.refresh_manifest_files(mfg)
    refresh_tools.rebuild_archive(out / "manufacturing.zip", [mfg])
    log("  readiness.json NOT re-pinned (legacy format, no producer; see #6269 / #6020)")


# --------------------------------------------------------------------------- board 03
SUPPLEMENTAL_BINDINGS = (
    "project.kct",
    "output/fabrication_overrides.json",
    "output/manufacturing-requirements.json",
    "routing-plan.json",
    "generate_design.py",
    "check_manufacturing.py",
    "routing_plan.py",
)


def board03(base_commit: str) -> None:
    log("== board 03")
    board = BOARDS / "03-usb-joystick"
    out, mfg = board / "output", board / "output/manufacturing"
    drop_bytecode(board)
    refresh_design_source(board)
    py = sys.executable
    run([py, str(board / "generate_design.py"), str(out)])
    log("  recipe: ERC/route-replay/fill/DRC/LVS/export PASS")
    proc = subprocess.run(
        [py, str(board / "check_manufacturing.py"), str(out / "usb_joystick_routed.kicad_pcb"),
         str(mfg / "check-report.json")], capture_output=True, text=True,
    )  # fmt: skip
    data = json.loads((mfg / "check-report.json").read_text())
    log(f"  check_manufacturing (exit {proc.returncode}): {data['summary']['errors']} errors, "
        f"{data['summary']['warnings']} warnings")  # fmt: skip
    run([py, str(board / "generate_design.py"), str(out)])  # manifest now covers check-report
    drop_bytecode(board)
    refresh_tools.rebuild_archive(out / "manufacturing.zip", [mfg])

    shutil.rmtree(out / "readiness-verification", ignore_errors=True)
    subprocess.run([py, "-m", "kicad_tools.cli", "readiness", str(board), "--verify",
                    "--format", "json"], capture_output=True, text=True)  # fmt: skip
    receipt = json.loads((out / "readiness-verification/readiness.json").read_text())
    for rel in SUPPLEMENTAL_BINDINGS:
        receipt["inputs"][rel] = sha256(board / rel)
    receipt["supplemental_factory_evidence"] = {
        "source_commit": base_commit,
        "report": "output/manufacturing/check-report.json",
        "binding_note": (
            "Additive bindings for exact-source factory/process inputs. Original corrected "
            "verifier receipt preserved in output/readiness-verification/readiness.json. "
            "Verdict and all verifier checks are unchanged."
        ),
    }
    (out / "readiness.json").write_text(json.dumps(receipt, indent=2) + "\n")
    log(f"  readiness: {receipt['status']} {receipt.get('blockers')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--board", action="append", choices=["03", "04"])
    parser.add_argument(
        "--base-commit",
        default="63813f418c55b68f53a31aaa6c80037563c6d5bf",
        help="commit recorded as board 03's supplemental source_commit",
    )
    parser.add_argument("--log", help=argparse.SUPPRESS)  # consumed by regenerate_uuids_6076
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    boards = args.board or ["04", "03"]
    if "04" in boards:
        board04()
    if "03" in boards:
        board03(args.base_commit)
    return 0


if __name__ == "__main__":
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("KICAD_CLI_DIR", "/Applications/KiCad/KiCad.app/Contents/MacOS")]
        + os.environ.get("PATH", "").split(os.pathsep)
    )
    raise SystemExit(main())
