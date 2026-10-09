"""Gated readiness refresh for recipe-finalised demo boards (Issue #6076).

Boards 05, 06 and 07 ship readiness evidence that ``kct readiness`` cannot
regenerate: ``--verify`` never replaces the release report, and ``--generate``
refuses packages whose manifest it did not produce.  Their ``readiness.json``
also records board-specific checks (firmware, 45-degree alignment, timing,
impedance) that a generic run would drop.  After a design file changes
without any change to its copper or circuit -- for example the
deterministic-UUID regeneration in #6076 -- this script re-pins that
evidence.  It does not write the report by hand.

It works on a staged copy of the board directory, and publishes nothing
unless every gate passes:

1. Re-run native ERC (``kicad-cli sch erc``) for every schematic the board
   reports on, and fail if there is any violation.  The report embeds the
   root-sheet UUID, so it must be re-run after a schematic regeneration.
2. Rebuild ``kicad_project.zip`` and ``design-source.zip`` from the current
   files, keeping each archive's existing member list.  Rebuild the outer
   ``manufacturing.zip`` the same way.  Every archive is written
   deterministically: entries sorted, fixed ``date_time``, fixed mode and
   compression.
3. Re-hash ``manufacturing/manifest.json``.  Board 05 uses its own
   ``redesign/package.py:refresh_manifest``.  Only ``files`` may change.
4. Gates, all run on the staged board:
   * ``kicad-cli pcb drc --refill-zones``: 0 errors, 0 unconnected items;
   * ``kct check --mfr <tier>``: 0 errors, 0 warnings, and the drc, erc,
     lvs and manifest meta-checks all PASSED;
   * ``kct net-status``: every net complete;
   * ``kct validate --sync``: 0 schematic/PCB sync errors.
5. Refuse if any measured metric (``drc_violations``, ``nets_routed_pct``,
   ``lvs_clean``, ``lvs_mismatches``) differs from what ``readiness.json``
   records.
6. Re-hash every ``readiness.json`` input, set ``checked_at``, and validate
   the staged report with ``read_readiness()``.  Only then copy the changed
   files back into the board.

Usage (from the repository root, KiCad 10 ``kicad-cli`` on PATH)::

    uv run --with shapely python scripts/boards/refresh_readiness.py \\
        boards/05-bldc-motor-controller --checked-at 2026-10-09T00:05:00+00:00

``--checked-at`` is explicit so that two runs produce the same report.
KiCad stamps every ERC JSON with its own run date, so ERC reports, and the
archives, manifest and readiness file that hash them, differ between runs
only by that date.  For a byte-for-byte audit, ``--save-erc DIR`` keeps the
ERC reports and ``--reuse-erc DIR`` replays them.  ``--dry-run`` runs every
gate and publishes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: Fixed archive member timestamp (the earliest ZIP can represent).
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
#: Regular file, rw-r--r--.
ZIP_EXTERNAL_ATTR = 0o100644 << 16

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ErcJob:
    """One native ERC run and every committed copy of its report."""

    key: str
    directory: str  # board-relative directory holding the schematic + project
    schematic: str
    targets: tuple[str, ...]  # board-relative report paths


@dataclass(frozen=True)
class BoardConfig:
    routed_pcb: str
    schematic: str
    tier: str
    erc: tuple[ErcJob, ...]
    manifest_refresher: str | None = None  # "path/to/module.py:function"
    archives: tuple[tuple[str, tuple[str, ...]], ...] = field(
        default=(
            ("output/manufacturing/kicad_project.zip", ("output", ".")),
            ("output/manufacturing/design-source.zip", (".", "output")),
        )
    )


BOARDS: dict[str, BoardConfig] = {
    "05-bldc-motor-controller": BoardConfig(
        routed_pcb="output/bldc_controller_routed.kicad_pcb",
        schematic="output/bldc_controller.kicad_sch",
        tier="jlcpcb",
        erc=(
            ErcJob(
                "05-native-erc.json",
                "output",
                "bldc_controller.kicad_sch",
                (
                    "output/erc_report.json",
                    "output/readiness/native-erc.json",
                    "output/manufacturing/native-erc.json",
                ),
            ),
        ),
        manifest_refresher="redesign/package.py:refresh_manifest",
    ),
    "06-diffpair-test": BoardConfig(
        routed_pcb="output/diffpair_test_routed.kicad_pcb",
        schematic="output/diffpair_test.kicad_sch",
        tier="jlcpcb-tier1",
        erc=(
            ErcJob(
                "06-native-erc.json",
                "output",
                "diffpair_test.kicad_sch",
                (
                    "output/erc_report.json",
                    "output/readiness/native-erc.json",
                    "output/manufacturing/native-erc.json",
                ),
            ),
        ),
    ),
    "07-matchgroup-test": BoardConfig(
        routed_pcb="output/matchgroup_test_routed.kicad_pcb",
        schematic="output/matchgroup_test.kicad_sch",
        tier="jlcpcb",
        erc=(
            ErcJob(
                "07-native-erc.json",
                "output",
                "matchgroup_test.kicad_sch",
                (
                    "output/erc_report.json",
                    "output/readiness/native-erc.json",
                    "output/manufacturing/native-erc.json",
                ),
            ),
            ErcJob(
                "07-checked-erc.json",
                "output/readiness",
                "checked.kicad_sch",
                (
                    "output/readiness/erc.json",
                    "real_design/reviewed-routing/evidence/erc.json",
                ),
            ),
        ),
    ),
}


class GateFailure(RuntimeError):
    """A gate failed; nothing is published."""


def log(message: str) -> None:
    print(message, flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- archives
def write_deterministic_zip(archive: Path, members: dict[str, bytes]) -> None:
    """Write ``members`` sorted by name with fixed metadata (byte-reproducible)."""
    with zipfile.ZipFile(archive, "w") as out:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = ZIP_EXTERNAL_ATTR
            out.writestr(info, members[name], compresslevel=9)


def rebuild_archive(archive: Path, search: list[Path]) -> list[str]:
    """Refill ``archive``'s existing members from the first matching current file."""
    with zipfile.ZipFile(archive) as existing:
        old = {name: existing.read(name) for name in existing.namelist() if not name.endswith("/")}
    members: dict[str, bytes] = {}
    for name in old:
        for base in search:
            candidate = base / name
            if candidate.is_file():
                members[name] = candidate.read_bytes()
                break
        else:
            raise GateFailure(f"{archive.name}: no current file for member {name}")
    write_deterministic_zip(archive, members)
    return sorted(name for name in members if members[name] != old[name])


def refresh_manifest_files(destination: Path) -> None:
    """Re-hash every bundle file into ``manifest.json["files"]``."""
    path = destination / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"] = {
        p.relative_to(destination).as_posix(): {
            "sha256": sha256(p),
            "size": p.stat().st_size,
        }
        for p in sorted(destination.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def load_refresher(board: Path, spec: str | None):
    if spec is None:
        return refresh_manifest_files
    module_path, function = spec.split(":")
    module_file = board / module_path
    sys.path.insert(0, str(module_file.parent))
    try:
        loaded = importlib.util.spec_from_file_location(f"refresh_{board.name}", module_file)
        module = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(module)
    finally:
        sys.path.remove(str(module_file.parent))
    return getattr(module, function)


# --------------------------------------------------------------------------- gates
def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def kct(*args: str) -> list[str]:
    return [sys.executable, "-m", "kicad_tools.cli", *args]


def native_erc(board: Path, job: ErcJob, reuse: Path | None, save: Path | None) -> bytes:
    if reuse is not None:
        data = (reuse / job.key).read_bytes()
    else:
        with tempfile.TemporaryDirectory(prefix="refresh-readiness-erc-") as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(
                board / job.directory,
                work,
                ignore=shutil.ignore_patterns("*.zip", "manufacturing"),
            )
            report = Path(tmp) / "erc.json"
            proc = run(
                [
                    "kicad-cli",
                    "sch",
                    "erc",
                    str(work / job.schematic),
                    "--format",
                    "json",
                    "--output",
                    str(report),
                ]
            )
            if proc.returncode != 0 or not report.is_file():
                raise GateFailure(f"native ERC did not run for {job.schematic}: {proc.stderr}")
            data = report.read_bytes()
    findings = sum(len(sheet["violations"]) for sheet in json.loads(data)["sheets"])
    if findings:
        raise GateFailure(f"native ERC {job.schematic}: {findings} violation(s)")
    if save is not None:
        save.mkdir(parents=True, exist_ok=True)
        (save / job.key).write_bytes(data)
    return data


def gate_native_drc(board: Path, cfg: BoardConfig) -> int:
    with tempfile.TemporaryDirectory(prefix="refresh-readiness-drc-") as tmp:
        report = Path(tmp) / "drc.json"
        proc = run(
            [
                "kicad-cli",
                "pcb",
                "drc",
                str(board / cfg.routed_pcb),
                "--refill-zones",
                "--format",
                "json",
                "--output",
                str(report),
            ]
        )
        if not report.is_file():
            raise GateFailure(f"kicad-cli DRC did not run: {proc.stderr}")
        data = json.loads(report.read_text())
    errors = [v for v in data["violations"] if v.get("severity") == "error"]
    unconnected = data.get("unconnected_items", [])
    log(
        f"  gate kicad-cli DRC (refilled): {len(errors)} error(s), "
        f"{len(data['violations']) - len(errors)} warning(s), {len(unconnected)} unconnected"
    )
    if errors or unconnected:
        raise GateFailure("native DRC has errors or unconnected items")
    return len(errors)


def gate_kct_check(board: Path, cfg: BoardConfig) -> tuple[bool, int]:
    with tempfile.TemporaryDirectory(prefix="refresh-readiness-check-") as tmp:
        report = Path(tmp) / "check.json"
        run(
            kct(
                "check",
                str(board / cfg.routed_pcb),
                "--mfr",
                cfg.tier,
                "--format",
                "json",
                "--output",
                str(report),
            )
        )
        if not report.is_file():
            raise GateFailure("kct check did not produce a report")
        data = json.loads(report.read_text())
    summary, meta = data["summary"], data.get("meta_checks", {})
    statuses = {k: meta.get(k, {}).get("status") for k in ("drc", "erc", "lvs", "manifest")}
    lvs = meta.get("lvs", {})
    mismatches = len(lvs.get("mismatches", [])) + len(lvs.get("copper_mismatches", []))
    log(
        f"  gate kct check --mfr {cfg.tier}: {summary['errors']} error(s), "
        f"{summary['warnings']} warning(s), meta {statuses}"
    )
    if summary["errors"] or summary["warnings"] or any(s != "PASSED" for s in statuses.values()):
        raise GateFailure("kct check failed")
    return statuses["lvs"] == "PASSED", mismatches


def gate_net_status(board: Path, cfg: BoardConfig) -> float:
    proc = run(kct("net-status", str(board / cfg.routed_pcb), "--format", "json"))
    try:
        summary = json.loads(proc.stdout)["summary"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise GateFailure(f"net-status did not report: {proc.stderr}") from exc
    total, complete = summary["total_nets"], summary["complete"]
    log(f"  gate net-status: {complete}/{total} nets complete")
    if not total or complete != total:
        raise GateFailure("net-status reports incomplete nets")
    return round(100.0 * complete / total, 1)


def gate_sync(board: Path, cfg: BoardConfig) -> None:
    proc = run(
        kct(
            "validate",
            "--sync",
            "--schematic",
            str(board / cfg.schematic),
            "--pcb",
            str(board / cfg.routed_pcb),
            "--format",
            "json",
        )
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise GateFailure(f"sync check did not report: {proc.stderr}") from exc
    errors = data.get("summary", {}).get("errors")
    log(f"  gate validate --sync: {errors} error(s), in_sync={data.get('in_sync')}")
    if errors is None or errors or data.get("in_sync") is not True:
        raise GateFailure("schematic/PCB sync check failed")


def compare_metrics(recorded: dict, measured: dict) -> list[str]:
    """Names of metrics whose measured value differs from the recorded one."""
    return sorted(
        name for name in recorded if name in measured and measured[name] != recorded[name]
    )


# --------------------------------------------------------------------------- refresh
def refresh(
    board: Path,
    checked_at: str,
    *,
    reuse_erc: Path | None = None,
    save_erc: Path | None = None,
    dry_run: bool = False,
) -> list[str]:
    from kicad_tools.cli.board_readiness import read_readiness

    cfg = BOARDS[board.name]
    with tempfile.TemporaryDirectory(prefix="refresh-readiness-") as tmp:
        staged = Path(tmp) / board.name
        shutil.copytree(board, staged)
        out = staged / "output"
        mfg = out / "manufacturing"

        for job in cfg.erc:
            data = native_erc(staged, job, reuse_erc, save_erc)
            for target in job.targets:
                (staged / target).write_bytes(data)
            log(f"  native ERC {job.schematic}: 0 violations -> {len(job.targets)} report copies")

        for archive, search in cfg.archives:
            changed = rebuild_archive(staged / archive, [staged / s for s in search])
            log(f"  {archive}: rebuilt; members with new content {changed}")

        before = json.loads((mfg / "manifest.json").read_text())
        load_refresher(staged, cfg.manifest_refresher)(mfg)
        after = json.loads((mfg / "manifest.json").read_text())
        if {k: v for k, v in before.items() if k != "files"} != {
            k: v for k, v in after.items() if k != "files"
        }:
            raise GateFailure("manifest refresh changed fields other than 'files'")
        changed = rebuild_archive(out / "manufacturing.zip", [mfg])
        log(f"  output/manufacturing.zip: rebuilt; members with new content {changed}")

        drc_errors = gate_native_drc(staged, cfg)
        lvs_clean, lvs_mismatches = gate_kct_check(staged, cfg)
        routed_pct = gate_net_status(staged, cfg)
        gate_sync(staged, cfg)

        report_path = out / "readiness.json"
        report = json.loads(report_path.read_text())
        measured = {
            "drc_violations": drc_errors,
            "nets_routed_pct": routed_pct,
            "lvs_clean": lvs_clean,
            "lvs_mismatches": lvs_mismatches,
        }
        differs = compare_metrics(report.get("metrics", {}), measured)
        if differs:
            raise GateFailure(
                f"measured metrics differ from readiness.json: "
                f"{ {k: (report['metrics'][k], measured[k]) for k in differs} }"
            )
        if report.get("status") != "ready" or report.get("blockers"):
            raise GateFailure("readiness.json is not a ready report; refusing to re-pin")

        for name in report["inputs"]:
            report["inputs"][name] = sha256(staged / name)
        report["checked_at"] = checked_at
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        verdict = read_readiness(staged)
        if verdict["status"] != "ready":
            raise GateFailure(f"staged report does not validate: {verdict['blockers']}")
        log(f"  read_readiness(staged): ready, metrics {verdict.get('metrics')}")

        published = sorted(
            p.relative_to(staged).as_posix()
            for p in staged.rglob("*")
            if p.is_file()
            and (board / p.relative_to(staged)).is_file()
            and p.read_bytes() != (board / p.relative_to(staged)).read_bytes()
        )
        if not dry_run:
            for name in published:
                shutil.copy2(staged / name, board / name)
        log(f"  {'would publish' if dry_run else 'published'} {len(published)} file(s)")
        return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("boards", nargs="+", type=Path, help="board directories")
    parser.add_argument("--checked-at", required=True, help="ISO timestamp for readiness.json")
    parser.add_argument("--reuse-erc", type=Path, help="replay ERC reports saved by --save-erc")
    parser.add_argument("--save-erc", type=Path, help="keep the native ERC reports here")
    parser.add_argument("--dry-run", action="store_true", help="run every gate, publish nothing")
    args = parser.parse_args(argv)
    status = 0
    for board in args.boards:
        board = board.resolve()
        if board.name not in BOARDS:
            parser.error(f"{board.name}: no refresh configuration (known: {sorted(BOARDS)})")
        log(f"== {board.name}")
        try:
            for name in refresh(
                board,
                args.checked_at,
                reuse_erc=args.reuse_erc,
                save_erc=args.save_erc,
                dry_run=args.dry_run,
            ):
                log(f"    {name}")
        except GateFailure as exc:
            log(f"  REFUSED: {exc} -- nothing published for {board.name}")
            status = 1
    return status


if __name__ == "__main__":
    os.environ["PATH"] = os.pathsep.join(
        [os.environ.get("KICAD_CLI_DIR", "/Applications/KiCad/KiCad.app/Contents/MacOS")]
        + os.environ.get("PATH", "").split(os.pathsep)
    )
    raise SystemExit(main())
