"""Gated readiness refresh for recipe-finalised demo boards (Issue #6076).

Boards 04, 05, 06 and 07 ship readiness evidence that ``kct readiness`` cannot
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
   * saved copper equals an independent ``kicad-cli`` refill per copper layer
     within ``kct readiness``'s 0.01 mm^2 tolerance (Issue #7337);
   * ``kct check --mfr <tier>``: 0 errors, no warnings beyond the allowance
     the board's recorded ``known_issues`` declare (Issue #7261; none means
     0 warnings), and the drc, erc, lvs and manifest meta-checks all PASSED.  A board whose fabrication
     process the generic tier cannot model (board 04's paid 0.15 mm drill)
     sets ``check_script`` instead; that board-local check must meet the same
     bar (Issue #6269);
   * ``kct net-status``: every net complete;
   * ``kct validate --sync``: 0 schematic/PCB sync errors.
5. Refuse if any measured metric (``drc_violations``, ``nets_routed_pct``,
   ``lvs_clean``, ``lvs_mismatches``) differs from what ``readiness.json``
   records.
6. Re-hash every ``readiness.json`` input, set ``checked_at``, and validate
   the staged report with ``read_readiness()``.  Only then copy the changed
   files back into the board.

Every kicad-cli call runs on the pinned evidence toolchain (Issue #6962): an
isolated ``KICAD_CONFIG_HOME`` with the pinned library tables, and a refusal
(exit 2, nothing published) unless kicad-cli is the qualified version or
``KCT_KICAD_DOCKER=1`` routes it through CI's pinned image.  A native DRC
``lib_footprint_issues`` finding is refused the same way.

Usage (from the repository root, KiCad 10 ``kicad-cli`` on PATH)::

    uv run --with shapely python scripts/boards/refresh_readiness.py \\
        boards/05-bldc-motor-controller --checked-at 2026-10-09T00:05:00+00:00

``--checked-at`` is explicit so that two runs produce the same report.
The refresh is a fixed point on unchanged evidence (Issue #7384): re-running it
with the committed ``checked_at`` publishes nothing.  Two things used to break
that.  KiCad stamps every ERC/DRC JSON with its own run ``date``; a fresh report
whose content differs from the committed copy only by that field keeps the
committed bytes (``settle_report``), so the date records when the findings were
last produced.  And an archive whose members all keep their bytes keeps its
committed bytes (``rebuild_archive``), whichever producer last wrote its zip
metadata; an archive with any changed member is rewritten deterministically.
``--save-erc DIR`` keeps the ERC reports and ``--reuse-erc DIR`` replays them.
``--dry-run`` runs every gate and publishes nothing; the native test
``tests/test_board_evidence_reproduces_7384.py`` asserts it lists no file for
any board.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: Fixed archive member timestamp (the earliest ZIP can represent).
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
#: Regular file, rw-r--r--.
ZIP_EXTERNAL_ATTR = 0o100644 << 16

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_bundle_links():
    path = Path(__file__).resolve().with_name("_bundle_links.py")
    spec = importlib.util.spec_from_file_location("_bundle_links", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle_links = _load_bundle_links()

from kicad_tools.cli.kicad_toolchain import (  # noqa: E402
    KiCadEnvironmentError,
    evidence_toolchain,
    require_library_environment,
)


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
    # Board-relative copies of the native DRC report that the refresh rewrites
    # from the one staged kicad-cli run (Issue #7283).  Each is recorded in
    # ``readiness.json`` ``report_sources`` as produced from the routed PCB.
    drc_targets: tuple[str, ...] = ()
    # Board-local manufacturing check that replaces ``kct check --mfr <tier>``
    # (Issue #6269).  Run as ``python <check_script> <routed_pcb> <report>``;
    # it must write a ``kct check``-format JSON report, which is gated exactly
    # like ``gate_kct_check``'s.
    check_script: str | None = None
    # ``(mirror_dir, source_dir)``: every file already in ``mirror_dir`` is
    # refreshed from ``source_dir`` before the archives are rebuilt.
    mirrors: tuple[tuple[str, str], ...] = ()
    # Directories searched (relative to the board) when rebuilding
    # ``output/manufacturing.zip``.  Its member names are relative to the first
    # entry that holds them: boards 04-09 ship bare names (``README.txt``), the
    # ``kct readiness`` packages 00-02 prefix them with ``manufacturing/``, so
    # those search ``output`` instead (Issue #7273).
    bundle_search: tuple[str, ...] = ("output/manufacturing",)
    archives: tuple[tuple[str, tuple[str, ...]], ...] = field(
        default=(
            ("output/manufacturing/kicad_project.zip", ("output", ".")),
            ("output/manufacturing/design-source.zip", (".", "output")),
        )
    )


BOARDS: dict[str, BoardConfig] = {
    # Board 04 ships a paid 0.15 mm mechanical-drill process that only its own
    # check_manufacturing.py / manufacturing_process.py model; the generic
    # ``kct check --mfr jlcpcb-tier1`` reports its reviewed vias as errors.
    # The schematic is re-checked by native ERC (no report copies are
    # rewritten: its committed reports also carry a Linux-container run).
    # Its native DRC evidence is owned here (Issue #7435): the pintype
    # annotator no longer writes it (Issue #7409).
    "04-stm32-devboard": BoardConfig(
        routed_pcb="output/stm32_devboard_routed.kicad_pcb",
        schematic="output/stm32_devboard.kicad_sch",
        tier="jlcpcb-tier1",
        drc_targets=(
            "output/readiness/native-drc.json",
            "output/manufacturing/native-drc.json",
        ),
        erc=(ErcJob("04-native-erc.json", "output", "stm32_devboard.kicad_sch", ()),),
        check_script="check_manufacturing.py",
        mirrors=(("output/manufacturing/design-source", "."),),
        archives=(("output/manufacturing/kicad_project.zip", ("output", ".")),),
    ),
    # Boards 00-02 are ``kct readiness`` packages (no board-local check),
    # refreshed through the same gates (Issue #7273).  Their recorded
    # ``known_issues`` carry the warning allowance.  Board 00/01 ship no
    # native-ERC report copies, so ``erc`` is empty.
    "00-simple-led": BoardConfig(
        routed_pcb="output/simple_led_routed.kicad_pcb",
        schematic="output/simple_led.kicad_sch",
        tier="jlcpcb",
        erc=(),
        archives=(("output/manufacturing/kicad_project.zip", ("output", ".")),),
        bundle_search=("output",),
    ),
    "01-voltage-divider": BoardConfig(
        routed_pcb="output/voltage_divider_routed.kicad_pcb",
        schematic="output/voltage_divider.kicad_sch",
        tier="jlcpcb",
        erc=(),
        archives=(("output/manufacturing/kicad_project.zip", ("output", ".")),),
        bundle_search=("output",),
    ),
    "02-charlieplex-led": BoardConfig(
        routed_pcb="output/charlieplex_3x3_routed.kicad_pcb",
        schematic="output/charlieplex_3x3.kicad_sch",
        tier="jlcpcb",
        erc=(
            ErcJob(
                "02-native-erc.json",
                "output",
                "charlieplex_3x3.kicad_sch",
                (
                    "output/readiness/native-erc.json",
                    "output/manufacturing/native-erc.json",
                ),
            ),
        ),
        bundle_search=("output",),
    ),
    "05-bldc-motor-controller": BoardConfig(
        routed_pcb="output/bldc_controller_routed.kicad_pcb",
        schematic="output/bldc_controller.kicad_sch",
        tier="jlcpcb",
        drc_targets=("output/drc_report.json",),
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
        drc_targets=(
            "output/drc_report.json",
            "output/readiness/native-drc.json",
            "output/manufacturing/native-drc.json",
        ),
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
        drc_targets=(
            "output/drc_report.json",
            "output/readiness/native-drc.json",
            "output/manufacturing/native-drc.json",
        ),
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
    """Refill ``archive``'s existing members from the first matching current file.

    Returns the members whose content changed.  When none did, the archive is
    left byte-for-byte as it is (Issue #7384): its committed bytes may come
    from another deterministic writer (``kct readiness``, the bundle exporter)
    with different zip metadata, and re-encoding identical members would make
    every refresh rewrite the archive, the manifest that hashes it and the
    outer bundle.  Otherwise it is rewritten with ``write_deterministic_zip``.
    """
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
    changed = sorted(name for name in members if members[name] != old[name])
    if changed:
        write_deterministic_zip(archive, members)
    return changed


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


#: The one field of a ``kicad-cli`` ERC/DRC JSON report that records when it
#: ran rather than what it found.
KICAD_REPORT_RUN_FIELD = "date"


def settle_report(target: Path, fresh: bytes) -> bytes:
    """Write ``fresh`` to ``target`` unless the existing report says the same (Issue #7384).

    ``kicad-cli`` stamps each ERC/DRC JSON with its run ``date``, so two runs
    over the same design never match byte-for-byte.  When ``target`` already
    holds a report equal to ``fresh`` in every field but that one, its bytes are
    kept; any other difference (a finding, the KiCad version, the source, the
    layout) writes ``fresh``.  Returns the bytes now in ``target``.
    """
    if target.is_file():
        current = target.read_bytes()
        try:
            old, new = json.loads(current), json.loads(fresh)
        except (json.JSONDecodeError, UnicodeDecodeError):
            old = new = None
        if (
            isinstance(old, dict)
            and isinstance(new, dict)
            and {k: v for k, v in old.items() if k != KICAD_REPORT_RUN_FIELD}
            == {k: v for k, v in new.items() if k != KICAD_REPORT_RUN_FIELD}
        ):
            return current
    target.write_bytes(fresh)
    return fresh


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


def gate_native_drc(board: Path, cfg: BoardConfig) -> tuple[int, bytes]:
    """Run the refilled native DRC; return the error count and the raw report bytes."""
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
        raw = report.read_bytes()
        data = json.loads(raw)
    # Issue #6962: a library-table finding means the environment, not the
    # board, was checked.
    require_library_environment(data, source=f"native DRC of {cfg.routed_pcb}")
    errors = [v for v in data["violations"] if v.get("severity") == "error"]
    unconnected = data.get("unconnected_items", [])
    log(
        f"  gate kicad-cli DRC (refilled): {len(errors)} error(s), "
        f"{len(data['violations']) - len(errors)} warning(s), {len(unconnected)} unconnected"
    )
    if errors or unconnected:
        raise GateFailure("native DRC has errors or unconnected items")
    # Issue #7337: the run above refilled in memory only, so it certifies the
    # refilled copper, not the saved fills the package ships.
    gate_fill_identity(board, cfg)
    return len(errors), raw


def gate_fill_identity(board: Path, cfg: BoardConfig) -> dict[str, float]:
    """Saved copper must equal an independent native refill (Issue #7337).

    The DRC gate above refills a throwaway copy, so it passes whatever the
    saved fills hold.  ``kct readiness --verify`` compares the saved copper
    with a fresh ``kicad-cli`` refill; without the same comparison here a
    refresh could re-pin a ``ready`` record whose published fill diverges
    (board 04 after #7317's carved fill).  The comparison is the shared
    ``readiness_cmd.saved_vs_refilled`` (Issue #7356).
    """
    from kicad_tools.cli.readiness_cmd import (
        DEFAULT_FILL_TOLERANCE_MM2,
        FillRefillUnavailable,
        saved_vs_refilled,
    )

    try:
        result = saved_vs_refilled(board / cfg.routed_pcb, DEFAULT_FILL_TOLERANCE_MM2)
    except FillRefillUnavailable as exc:
        raise GateFailure(f"independent zone refill did not run: {exc.detail}") from exc
    log(
        f"  gate saved-vs-refilled copper: {len(result.saved_areas)} layer(s), "
        f"divergent {result.divergent or 'none'} (tolerance {DEFAULT_FILL_TOLERANCE_MM2} mm^2)"
    )
    if result.diverges:
        raise GateFailure(f"saved copper differs from an independent refill: {result.describe()}")
    return result.divergent


def warning_mismatch(
    data: dict, allowance: dict[tuple[str, str, str], int]
) -> dict[str, list[str]]:
    """Findings that break the identity *allowance*: ``{unaccepted, stale}`` (non-empty only).

    ``allowance`` maps ``(rule, ref, pad)`` to the issue that accounts for it.
    A finding the allowance does not name blocks, whatever the count; a listed
    identity that no longer fires is stale and blocks too (Issue #7389).
    """
    from kicad_tools.cli.known_issues import match_warning_allowance

    violations = [v for v in data.get("violations", []) if isinstance(v, dict)]
    active = sum(1 for v in violations if v.get("severity") == "warning" and not v.get("waived"))
    total = data.get("summary", {}).get("warnings")
    if total != active:
        # Summary and finding list disagree: never trust the allowance.
        return {"unaccepted": [f"<summary/violations mismatch: {total} vs {active}>"]}
    unaccepted, stale = match_warning_allowance(violations, allowance)
    return {k: v for k, v in (("unaccepted", unaccepted), ("stale", stale)) if v}


def check_report_verdict(
    data: dict, label: str, allowance: dict[tuple[str, str, str], int] | None = None
) -> tuple[bool, int]:
    """Gate a ``kct check``-format report: 0 errors, meta all PASSED, and no
    warnings beyond *allowance*.

    The allowance is the board's own recorded ``known_issues`` warning
    identities from ``readiness.json`` (Issues #7261, #7389): the same record
    that discloses the issue is the one that tolerates its warnings.  Without
    one, any warning refuses; a recorded identity that no longer fires refuses
    as stale.
    """
    summary, meta = data.get("summary", {}), data.get("meta_checks", {})
    statuses = {k: meta.get(k, {}).get("status") for k in ("drc", "erc", "lvs", "manifest")}
    lvs = meta.get("lvs", {})
    mismatches = len(lvs.get("mismatches", [])) + len(lvs.get("copper_mismatches", []))
    errors, warnings = summary.get("errors"), summary.get("warnings")
    log(f"  gate {label}: {errors} error(s), {warnings} warning(s), meta {statuses}")
    if not isinstance(warnings, int):
        raise GateFailure(f"{label} failed")
    extra = warning_mismatch(data, allowance or {})
    if extra:
        log(f"  warnings do not match the recorded known_issues allowance: {extra}")
    if errors != 0 or extra or any(s != "PASSED" for s in statuses.values()):
        raise GateFailure(f"{label} failed")
    return statuses["lvs"] == "PASSED", mismatches


def known_issue_allowance(board: Path) -> dict[tuple[str, str, str], int]:
    """The ``{(rule, ref, pad): issue}`` the board's recorded ``known_issues`` account for."""
    from kicad_tools.cli.known_issues import known_warning_allowance, validate_known_issues

    report = json.loads((board / "output" / "readiness.json").read_text())
    return known_warning_allowance(validate_known_issues(report.get("known_issues")))


def gate_kct_check(board: Path, cfg: BoardConfig) -> tuple[bool, int]:
    from kicad_tools.cli.check_categories import gate_skip_argv

    with tempfile.TemporaryDirectory(prefix="refresh-readiness-check-") as tmp:
        report = Path(tmp) / "check.json"
        run(
            kct(
                "check",
                str(board / cfg.routed_pcb),
                *gate_skip_argv(),  # host-dependent model_coverage, #7683/#7685
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
    return check_report_verdict(data, f"kct check --mfr {cfg.tier}", known_issue_allowance(board))


def gate_board_check(board: Path, cfg: BoardConfig) -> tuple[bool, int]:
    """Run the board's own manufacturing check in place of ``kct check`` (Issue #6269)."""
    assert cfg.check_script is not None
    script = board / cfg.check_script
    with tempfile.TemporaryDirectory(prefix="refresh-readiness-board-check-") as tmp:
        report = Path(tmp) / "check.json"
        proc = run(
            [sys.executable, str(script), str(board / cfg.routed_pcb), str(report)],
            cwd=board,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if not report.is_file():
            raise GateFailure(f"{cfg.check_script} did not produce a report: {proc.stderr[-2000:]}")
        data = json.loads(report.read_text())
    verdict = check_report_verdict(data, cfg.check_script, known_issue_allowance(board))
    if proc.returncode != 0:
        raise GateFailure(f"{cfg.check_script} exited {proc.returncode}")
    return verdict


def gate_check(board: Path, cfg: BoardConfig) -> tuple[bool, int]:
    """The board-local check hook when configured, else ``kct check --mfr <tier>``."""
    if cfg.check_script is not None:
        return gate_board_check(board, cfg)
    return gate_kct_check(board, cfg)


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
def apply_known_issues(
    staged: Path,
    checked_at: str,
    known_issue: list[str],
    resolve_known_issue: list[str],
) -> str | None:
    """Apply ``--known-issue`` / ``--resolve-known-issue`` to the staged report (Issue #7323).

    Uses the same merge as ``kct readiness`` (``cli/known_issues.py``) and keeps
    ``readiness.json`` and the hash-bound ``known-issues.json`` consistent: the
    evidence file is written (or removed when no entry remains) and its
    ``inputs`` entry added (or dropped).  Returns the evidence path relative to
    the board.  ``refresh`` re-hashes ``inputs`` afterwards.
    """
    from kicad_tools.cli.known_issues import KNOWN_ISSUES_FILENAME, KnownIssueError
    from kicad_tools.cli.readiness_cmd import _resolve_known_issues

    args = argparse.Namespace(known_issue=known_issue, resolve_known_issue=resolve_known_issue)
    try:
        merged = _resolve_known_issues(staged, args, checked_at)
    except KnownIssueError as exc:
        raise GateFailure(str(exc)) from None
    report_path = staged / "output" / "readiness.json"
    report = json.loads(report_path.read_text())
    inputs = report.setdefault("inputs", {})
    bound = [name for name in inputs if Path(name).name == KNOWN_ISSUES_FILENAME]
    if len(bound) > 1:
        raise GateFailure("known_issues is bound to more than one evidence file")
    rel = bound[0] if bound else f"output/readiness/{KNOWN_ISSUES_FILENAME}"
    evidence = staged / rel
    if merged:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({"known_issues": merged}, indent=2) + "\n")
        inputs[rel] = sha256(evidence)
        report["known_issues"] = merged
    else:
        evidence.unlink(missing_ok=True)
        inputs.pop(rel, None)
        report.pop("known_issues", None)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return rel


def refresh(
    board: Path,
    checked_at: str,
    *,
    reuse_erc: Path | None = None,
    save_erc: Path | None = None,
    dry_run: bool = False,
    known_issue: list[str] | None = None,
    resolve_known_issue: list[str] | None = None,
) -> list[str]:
    cfg = BOARDS[board.name]
    with tempfile.TemporaryDirectory(prefix="refresh-readiness-") as tmp:
        staged = Path(tmp) / board.name
        shutil.copytree(board, staged, ignore=shutil.ignore_patterns("__pycache__"))
        return repin_staged(
            board,
            staged,
            cfg,
            checked_at,
            reuse_erc=reuse_erc,
            save_erc=save_erc,
            dry_run=dry_run,
            known_issue=known_issue,
            resolve_known_issue=resolve_known_issue,
        )


def repin_staged(
    board: Path,
    staged: Path,
    cfg: BoardConfig,
    checked_at: str,
    *,
    reuse_erc: Path | None = None,
    save_erc: Path | None = None,
    dry_run: bool = False,
    known_issue: list[str] | None = None,
    resolve_known_issue: list[str] | None = None,
    after_manifest: Callable[[], None] | None = None,
) -> list[str]:
    """Gate a staged copy of ``board`` and re-pin its ``readiness.json`` (Issue #7353).

    The shared post-staging tail: ERC, native DRC, mirrors, archives, manifest
    re-hash, ``kct check`` / net-status / sync, the metric compare, the re-pin and
    the publish diff.  ``staged`` is a copy of ``board`` that the caller has
    already brought up to date (``refresh`` stages nothing; the bundle driver
    re-plots).  ``after_manifest`` runs once the manifest has been re-hashed, and
    the manifest is re-hashed again afterwards (the bundle's check report records
    the manifest meta-check).  Returns the paths that were (or, on ``dry_run``,
    would be) published.
    """
    from kicad_tools.cli.board_readiness import read_readiness

    out = staged / "output"
    mfg = out / "manufacturing"

    known_rel = None
    if known_issue or resolve_known_issue:
        # Before the gates: the kct check allowance reads the staged known_issues.
        known_rel = apply_known_issues(
            staged, checked_at, known_issue or [], resolve_known_issue or []
        )
        log(f"  known_issues updated -> {known_rel}")

    for job in cfg.erc:
        data = native_erc(staged, job, reuse_erc, save_erc)
        for target in job.targets:
            settle_report(staged / target, data)
        log(f"  native ERC {job.schematic}: 0 violations -> {len(job.targets)} report copies")

    # Issue #7283: run before the mirrors, archives and manifest, which
    # hash the report copies written here.
    drc_errors, drc_raw = gate_native_drc(staged, cfg)
    for target in cfg.drc_targets:
        settle_report(staged / target, drc_raw)
    if cfg.drc_targets:
        log(f"  native DRC report -> {len(cfg.drc_targets)} report copies")

    for mirror, source in cfg.mirrors:
        for path in sorted(p for p in (staged / mirror).rglob("*") if p.is_file()):
            origin = staged / source / path.relative_to(staged / mirror)
            if not origin.is_file():
                raise GateFailure(f"{mirror}: no current file for {path.name}")
            bundle_links.copy_into_bundle(origin, path, mfg)
        log(f"  {mirror}: refreshed from {source}")

    for archive, search in cfg.archives:
        changed = rebuild_archive(staged / archive, [staged / s for s in search])
        log(f"  {archive}: rebuilt; members with new content {changed}")

    before = json.loads((mfg / "manifest.json").read_text())
    refresher = load_refresher(staged, cfg.manifest_refresher)
    refresher(mfg)
    if after_manifest is not None:
        after_manifest()
        refresher(mfg)
    after = json.loads((mfg / "manifest.json").read_text())
    if {k: v for k, v in before.items() if k != "files"} != {
        k: v for k, v in after.items() if k != "files"
    }:
        raise GateFailure("manifest refresh changed fields other than 'files'")
    changed = rebuild_archive(out / "manufacturing.zip", [staged / s for s in cfg.bundle_search])
    log(f"  output/manufacturing.zip: rebuilt; members with new content {changed}")

    lvs_clean, lvs_mismatches = gate_check(staged, cfg)
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

    if cfg.drc_targets:
        # Issue #7283: record which PCB bytes the report copies were produced
        # from, so report binding sees provenance and not just co-pinning.
        pcb_sha = sha256(staged / cfg.routed_pcb)
        report.setdefault("report_sources", {}).update(dict.fromkeys(cfg.drc_targets, pcb_sha))
    # ``read_readiness`` requires every output rule sidecar to be an input; a
    # sidecar added since the report was issued (Issue #7206) is bound here.
    for p in (staged / "output").iterdir():
        if p.is_file() and p.name.endswith("net_class_map.json"):
            report["inputs"].setdefault(p.relative_to(staged).as_posix(), "")
    # Renders are presentation artifacts, not evidence (Issue #7678).
    from kicad_tools.cli.readiness_cmd import _is_render_artifact

    for name in [n for n in report["inputs"] if _is_render_artifact(Path(n))]:
        del report["inputs"][name]
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
        and "__pycache__" not in p.relative_to(staged).parts
        and (board / p.relative_to(staged)).is_file()
        and p.read_bytes() != (board / p.relative_to(staged)).read_bytes()
    )
    removed: list[str] = []
    if known_rel is not None:
        # The evidence file may be new (not yet in the board) or resolved away.
        if (staged / known_rel).is_file():
            if known_rel not in published and not (board / known_rel).is_file():
                published = sorted([*published, known_rel])
        elif (board / known_rel).is_file():
            removed.append(known_rel)
    if not dry_run:
        for name in published:
            (board / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged / name, board / name)
        for name in removed:
            (board / name).unlink()
    published = sorted([*published, *(f"{name} (removed)" for name in removed)])
    log(f"  {'would publish' if dry_run else 'published'} {len(published)} file(s)")
    return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("boards", nargs="+", type=Path, help="board directories")
    parser.add_argument("--checked-at", required=True, help="ISO timestamp for readiness.json")
    parser.add_argument("--reuse-erc", type=Path, help="replay ERC reports saved by --save-erc")
    parser.add_argument("--save-erc", type=Path, help="keep the native ERC reports here")
    parser.add_argument("--dry-run", action="store_true", help="run every gate, publish nothing")
    parser.add_argument(
        "--known-issue",
        action="append",
        default=[],
        metavar="N:SUMMARY[:KIND[:RULE=COUNT,...]]",
        help="record a known issue (repeatable; same syntax as `kct readiness`, Issue #7323)",
    )
    parser.add_argument(
        "--resolve-known-issue",
        action="append",
        default=[],
        metavar="N",
        help="drop a recorded known issue by number (repeatable, Issue #7323)",
    )
    args = parser.parse_args(argv)
    try:
        with evidence_toolchain(mounts=[REPO_ROOT, *(b.resolve() for b in args.boards)]):
            return _refresh_all(parser, args)
    except KiCadEnvironmentError as exc:
        log(f"REFUSED: {exc} -- nothing published")
        return 2


def _refresh_all(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
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
                known_issue=args.known_issue,
                resolve_known_issue=args.resolve_known_issue,
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
