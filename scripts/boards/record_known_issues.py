"""Record known issues on a board's committed readiness report, gates unchanged (Issue #7261).

``kct readiness --known-issue`` records an issue when the evidence is
regenerated.  Some boards cannot be regenerated today: ``refresh_readiness.py``
refuses boards 04 and 05 because their ``kct check`` warnings (#7220) are not
yet on an allowlist (#7273), and their recipe-built packages are not
``kct readiness`` packages.  For those boards this script makes the smallest
honest change that discloses the issues:

1. ``read_readiness()`` must accept the committed report as it stands (every
   hashed input current).  A stale report is refused: re-pin it first.
2. The board's recorded ``known_issues`` are carried forward, ``--known-issue``
   entries added and ``--resolve-known-issue`` entries dropped, exactly as
   ``kct readiness`` does.
3. The list is written to ``known-issues.json`` in the board's evidence
   directory, and that one file's hash is added to ``inputs``.  No other hash
   changes, no gate is re-run, ``checked_at`` and every check stay as they
   were, and the report says so in ``known_issues_binding``.
4. ``manufacturer`` is copied from the hash-bound ``manufacturing/manifest.json``.
5. A ``defect`` makes the status ``blocked`` with a ``known defect #N`` blocker;
   resolving the last defect restores the status the gates gave.
6. ``read_readiness()`` must accept the result.  Otherwise nothing is written.

``--doc README.md`` additionally re-pins an edited board-level Markdown file
(Issue #7261 deletes board 04's stale README status block).  Only the named
``.md`` inputs may be stale beforehand; their byte copies under
``output/manufacturing/design-source/`` are refreshed, those ``manifest.json``
``files`` entries re-hashed and ``output/manufacturing.zip`` rebuilt with the
same members (``refresh_readiness.rebuild_archive``).  Exactly those files'
hashes change, which the script checks.

Usage (repository root)::

    uv run python scripts/boards/record_known_issues.py boards/04-stm32-devboard \\
        --recorded 2026-10-18 --known-issue "6269:paid 0.15 mm drill:exception"
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

from kicad_tools.cli.board_readiness import read_readiness
from kicad_tools.cli.known_issues import (
    KNOWN_ISSUES_FILENAME,
    KnownIssueError,
    defects,
    merge_known_issues,
    parse_known_issue,
    validate_known_issues,
)

BINDING_NOTE = (
    "known_issues and manufacturer were added by scripts/boards/record_known_issues.py "
    "(Issue #7261) without re-running any gate: only known-issues.json was hashed into "
    "inputs (plus any edited board .md file named with --doc and its design-source "
    "mirror, manifest entry and outer archive). The checks, metrics and checked_at "
    "are those of the original run."
)
_DEFECT_PREFIX = "known defect #"


def _load_refresh_tools():
    path = Path(__file__).resolve().with_name("refresh_readiness.py")
    spec = importlib.util.spec_from_file_location("refresh_readiness", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("refresh_readiness", module)  # dataclasses need it registered
    spec.loader.exec_module(module)
    return module


class Refused(RuntimeError):
    """The board's evidence cannot be rebound; nothing was written."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_dir(board: Path, report: dict) -> str:
    for name in report.get("inputs", {}):
        if Path(name).name == KNOWN_ISSUES_FILENAME:
            return Path(name).parent.as_posix()
    for candidate in ("output/readiness", "output/readiness-verification"):
        if (board / candidate).is_dir():
            return candidate
    raise Refused("no evidence directory (output/readiness or output/readiness-verification)")


def _stale_inputs(board: Path, report: dict) -> list[str]:
    return sorted(
        name
        for name, digest in report["inputs"].items()
        if not (board / name).is_file() or _sha256(board / name) != digest
    )


def _repin_docs(staged: Path, report: dict, docs: list[str]) -> set[str]:
    """Refresh *docs*' design-source mirrors, manifest entries and the outer archive."""
    mfg = staged / "output" / "manufacturing"
    manifest_path = mfg / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    tools = _load_refresh_tools()
    changed = set(docs)
    for doc in docs:
        mirror = mfg / "design-source" / doc
        if mirror.is_file():
            # Same link-rewriting copy refresh_readiness.py uses for mirrors.
            tools.bundle_links.copy_into_bundle(staged / doc, mirror, mfg)
            member = mirror.relative_to(mfg).as_posix()
            if member in manifest.get("files", {}):
                manifest["files"][member] = {
                    "sha256": _sha256(mirror),
                    "size": mirror.stat().st_size,
                }
            changed.add(mirror.relative_to(staged).as_posix())
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    changed.add(manifest_path.relative_to(staged).as_posix())
    archive = staged / "output" / "manufacturing.zip"
    if archive.is_file():
        members = tools.rebuild_archive(archive, [mfg])
        allowed = {"manifest.json", *(f"design-source/{doc}" for doc in docs)}
        if not set(members) <= allowed:
            raise Refused(f"manufacturing.zip members changed unexpectedly: {members}")
        changed.add(archive.relative_to(staged).as_posix())
    for name in changed:
        if name in report["inputs"]:
            report["inputs"][name] = _sha256(staged / name)
    return changed


def rebind(
    board: Path,
    added: list[dict],
    resolved: list[int],
    docs: list[str] = (),
) -> dict:
    """Return the rebound report after writing it, or raise :class:`Refused`."""
    report_path = board / "output" / "readiness.json"
    report = json.loads(report_path.read_text())
    docs = list(docs)
    if any(not doc.endswith(".md") or doc not in report.get("inputs", {}) for doc in docs):
        raise Refused("--doc must name hashed board-level .md inputs")
    stale = _stale_inputs(board, report)
    if not set(stale) <= set(docs):
        raise Refused(f"committed evidence is stale beyond --doc: {stale}")
    if not docs:
        before = read_readiness(board)
        if before["status"] not in {"ready", "blocked"}:
            raise Refused(f"committed evidence does not validate: {before['blockers']}")

    previous = validate_known_issues(report.get("known_issues"))
    known = merge_known_issues(previous, added, resolved)

    manifest = json.loads((board / "output/manufacturing/manifest.json").read_text())
    tier = manifest.get("manufacturer")
    if not isinstance(tier, str) or not tier:
        raise Refused("manufacturing/manifest.json records no manufacturer")

    # Status the gates gave, without any earlier known-defect blockers.
    gate_blockers = [b for b in report["blockers"] if not b.startswith(_DEFECT_PREFIX)]
    had_defects = bool(defects(previous))
    gate_status = report["status"]
    if had_defects and not gate_blockers and gate_status == "blocked":
        if all(check.get("status") == "passed" for check in report.get("checks", [])):
            gate_status = "ready"

    evidence = _evidence_dir(board, report)
    rel = f"{evidence}/{KNOWN_ISSUES_FILENAME}"
    with tempfile.TemporaryDirectory(prefix="record-known-issues-") as tmp:
        staged = Path(tmp) / board.name
        shutil.copytree(board, staged, ignore=shutil.ignore_patterns("__pycache__"))
        repinned = _repin_docs(staged, report, docs) if docs else set()
        target = staged / rel
        report["inputs"].pop(rel, None)
        if known:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"known_issues": known}, indent=2) + "\n")
            report["inputs"][rel] = _sha256(target)
            report["known_issues"] = known
        else:
            target.unlink(missing_ok=True)
            report.pop("known_issues", None)
        report["manufacturer"] = tier
        report["blockers"] = gate_blockers + [
            f"{_DEFECT_PREFIX}{entry['issue']}: {entry['summary']}" for entry in defects(known)
        ]
        report["status"] = "blocked" if defects(known) else gate_status
        report["known_issues_binding"] = BINDING_NOTE
        (staged / "output/readiness.json").write_text(json.dumps(report, indent=2) + "\n")

        after = read_readiness(staged)
        if after["status"] != report["status"]:
            raise Refused(f"rebound report does not validate: {after['blockers']}")

        for name in sorted(repinned - set(docs)):
            shutil.copy2(staged / name, board / name)
        (board / rel).parent.mkdir(parents=True, exist_ok=True)
        if known:
            shutil.copy2(target, board / rel)
        else:
            (board / rel).unlink(missing_ok=True)
        shutil.copy2(staged / "output/readiness.json", report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("board", type=Path, help="board directory")
    parser.add_argument("--recorded", required=True, help="ISO date recorded on new entries")
    parser.add_argument("--known-issue", action="append", default=[], metavar="N:SUMMARY:KIND")
    parser.add_argument("--resolve-known-issue", action="append", default=[], type=int)
    parser.add_argument(
        "--doc",
        action="append",
        default=[],
        help="board-relative edited .md input to re-pin with its design-source mirror",
    )
    args = parser.parse_args(argv)
    board = args.board.resolve()
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    try:
        added = [parse_known_issue(spec, args.recorded) for spec in args.known_issue]
        # No kicad-cli runs here, but like every board evidence script it
        # writes committed evidence only on the pinned toolchain (#6962).
        with evidence_toolchain(mounts=[board]):
            report = rebind(board, added, args.resolve_known_issue, args.doc)
    except (KnownIssueError, Refused, KiCadEnvironmentError) as exc:
        print(f"REFUSED: {exc} -- nothing written for {board.name}", file=sys.stderr)
        return 2
    print(f"{board.name}: {report['status']}, {len(report.get('known_issues', []))} known issue(s)")
    for entry in report.get("known_issues", []):
        print(f"  #{entry['issue']} [{entry['kind']}] {entry['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
