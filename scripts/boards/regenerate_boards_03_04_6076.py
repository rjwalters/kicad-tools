"""Deterministic-UUID regeneration of board 03, with its evidence (Issue #6076).

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
* **Board 04** is no longer regenerated here: its evidence is refreshed and
  re-pinned by ``refresh_bundles_6404.py`` (Issue #7357).  The UUID remap
  (``remap_routed``) stays, and is exercised by this module's tests.

Gerbers, renders, PDFs, the README and the BOM/CPL do not depend on UUIDs
and are not regenerated here.

Rerunning is idempotent.  Once the board is regenerated, every UUID map is
the identity.

usage (repository root, KiCad 10 ``kicad-cli`` and docker available)::

    uv run --with shapely python scripts/boards/regenerate_boards_03_04_6076.py \\
        [--board 03] [--base-commit SHA] [--checked-at ISO]

Every kicad-cli call runs on the pinned evidence toolchain (Issue #6962):
an isolated ``KICAD_CONFIG_HOME`` holding the pinned library tables, and a
refusal unless ``kicad-cli`` is the qualified version.  On a host whose KiCad
is another release, set ``KCT_KICAD_DOCKER=1`` to route kicad-cli through
CI's pinned image.

``--board 03 --receipt-only`` re-issues only board 03's readiness receipt
(``kct readiness --verify`` plus the promotion to ``output/readiness.json``),
for a board whose design files are already current.  With a fixed
``--checked-at`` two runs produce byte-identical evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARDS = REPO_ROOT / "boards"
HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


uuid_tools = _load("regenerate_uuids_6076", HERE / "regenerate_uuids_6076.py")
refresh_tools = _load("refresh_readiness", HERE / "refresh_readiness.py")
bundle_links = _load("_bundle_links", HERE / "_bundle_links.py")

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


def require_manifest_passed(report: dict, what: str) -> None:
    """Evidence must be computed against the final bundle, never a stale one."""
    status = report.get("meta_checks", {}).get("manifest", {}).get("status")
    if status != "PASSED":
        raise RuntimeError(f"{what}: manifest meta-check is {status!r}, expected 'PASSED'")


def drop_bytecode(board: Path) -> None:
    """Never let an interpreter cache under output/ into a hashed bundle."""
    for cache in (board / "output").rglob("__pycache__"):
        shutil.rmtree(cache)


def refresh_design_source(board: Path) -> None:
    source = board / "output/manufacturing/design-source"
    for path in sorted(source.iterdir()):
        bundle_links.copy_into_bundle(board / path.name, path, board / "output/manufacturing")


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


def fill_consistency(pcb: Path) -> dict[str, float]:
    """Per-layer saved-vs-refilled deltas; raises if the saved fill diverges (Issue #7356)."""
    from kicad_tools.cli.readiness_cmd import require_saved_fill_matches_refill

    return require_saved_fill_matches_refill(pcb).deltas


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


def board03(base_commit: str, checked_at: str | None = None, known: tuple[str, ...] = ()) -> None:
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
    require_manifest_passed(data, "board 03 check-report.json")
    log(f"  check_manufacturing (exit {proc.returncode}): {data['summary']['errors']} errors, "
        f"{data['summary']['warnings']} warnings")  # fmt: skip
    run([py, str(board / "generate_design.py"), str(out)])  # manifest now covers check-report
    drop_bytecode(board)
    refresh_tools.rebuild_archive(out / "manufacturing.zip", [mfg])
    board03_receipt(base_commit, checked_at, known)


def board03_receipt(base_commit: str, checked_at: str | None, known: tuple[str, ...] = ()) -> None:
    """``kct readiness --verify`` board 03 and promote the receipt.

    *known* is forwarded to ``kct readiness`` (``--known-issue`` /
    ``--resolve-known-issue`` pairs, Issue #7261).  Issues already recorded in
    ``output/readiness.json`` carry forward without it.
    """
    from kicad_tools.cli import readiness_cmd

    board = BOARDS / "03-usb-joystick"
    out = board / "output"
    drop_bytecode(board)
    argv = [str(board), "--verify", "--format", "json"]
    if checked_at:
        argv += ["--checked-at", checked_at]
    argv += list(known)
    verification = out / "readiness-verification"
    with tempfile.TemporaryDirectory(prefix="regen-03-receipt-") as tmp:
        previous = Path(tmp) / "previous"
        if verification.exists():
            shutil.move(str(verification), previous)
        # In-process through the gated path (Issue #7410): the pinned-toolchain
        # refusal (#6962) lives in readiness_cmd, not in a spawned CLI.
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            returncode = readiness_cmd.main(argv)
        if returncode == 2:
            # Environment refusal (#6962): nothing was written, so put the
            # committed receipt back rather than leaving it deleted.
            shutil.rmtree(verification, ignore_errors=True)
            if previous.exists():
                shutil.move(str(previous), verification)
            raise RuntimeError(f"kct readiness refused board 03:\n{stderr.getvalue()[-2000:]}")
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
    parser.add_argument("--board", action="append", choices=["03"])
    parser.add_argument(
        "--base-commit",
        default="63813f418c55b68f53a31aaa6c80037563c6d5bf",
        help="commit recorded as board 03's supplemental source_commit",
    )
    parser.add_argument("--checked-at", help="ISO timestamp for board 03's receipt (default: now)")
    parser.add_argument(
        "--receipt-only",
        action="store_true",
        help="board 03: only re-issue the readiness receipt (design files already current)",
    )
    parser.add_argument(
        "--known-issue",
        action="append",
        default=[],
        metavar="N:SUMMARY:KIND",
        help="board 03: record a known issue in the receipt (forwarded to kct readiness, #7261)",
    )
    parser.add_argument(
        "--resolve-known-issue",
        action="append",
        default=[],
        metavar="N",
        help="board 03: drop a recorded known issue from the receipt (#7261)",
    )
    parser.add_argument("--log", help=argparse.SUPPRESS)  # consumed by regenerate_uuids_6076
    args = parser.parse_args()
    os.chdir(REPO_ROOT)
    boards = args.board or ["03"]
    if args.receipt_only and boards != ["03"]:
        parser.error("--receipt-only applies to --board 03 alone")
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    known: tuple[str, ...] = (
        *(arg for spec in args.known_issue for arg in ("--known-issue", spec)),
        *(arg for n in args.resolve_known_issue for arg in ("--resolve-known-issue", n)),
    )

    try:
        with evidence_toolchain(mounts=[REPO_ROOT]):
            if args.receipt_only:
                board03_receipt(args.base_commit, args.checked_at, known)
                return 0
            if "03" in boards:
                board03(args.base_commit, args.checked_at, known)
    except KiCadEnvironmentError as exc:
        log(f"REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("KICAD_CLI_DIR", "/Applications/KiCad/KiCad.app/Contents/MacOS")]
        + os.environ.get("PATH", "").split(os.pathsep)
    )
    raise SystemExit(main())
