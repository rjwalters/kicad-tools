#!/usr/bin/env python3
"""Interop gate for tscircuit-emitted KiCad projects (Issue #5847).

Provisions a gitignored npm workspace, emits one KiCad project per design in
``examples.mjs`` with ``circuit-json-to-kicad``, and runs each export through
this repo's manufacturability bar twice:

1. **as emitted** -- tscircuit's own autorouted copper, and
2. **kct-routed** -- the same board with all tracks/vias ripped up and
   re-routed by ``kct route``.

Each pass records ``kct check --mfr`` (which also carries ERC and LVS),
plus ``kicad-cli pcb drc`` for the second, independent DRC engine.

Nothing tscircuit produces or installs is ever written to a tracked path: the
npm workspace, the emitted projects and the results all live under
``.cache/kct-benchmarks/tscircuit/`` (gitignored), following the same run-time
fetch convention as ``benchmarks/external/`` -- see that directory's README for
why third-party board files are not committed here.

``kicad-cli`` is resolved in this order:

* ``--kicad-cli PATH`` / a ``kicad-cli`` on ``PATH``;
* otherwise a container image (``--kicad-docker-image``, default
  ``kicad/kicad:10.0``), which is what a macOS host needs when the native
  ``kicad-cli`` cannot start -- on this project's own host it blocks forever in
  ``LIBRARY_MANAGER::LoadGlobalTables`` because scanning ``~/Documents``
  triggers a macOS TCC consent prompt no headless agent can answer.

Usage::

    uv run python benchmarks/interop/tscircuit/run_gate.py
    uv run python benchmarks/interop/tscircuit/run_gate.py --only rc_lowpass
    uv run python benchmarks/interop/tscircuit/run_gate.py --skip-install

Like the rest of ``benchmarks/``, this does not run under pytest or CI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]

CACHE_DIR_ENV_VAR = "KCT_TSCIRCUIT_INTEROP_CACHE_DIR"
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "kct-benchmarks" / "tscircuit"

DEFAULT_KICAD_DOCKER_IMAGE = "kicad/kicad:10.0"

#: Pinned npm versions. ``polygon-clipping`` is NOT a transitive dependency we
#: chose to add: ``circuit-json-to-kicad`` imports it at run time but declares
#: it only under ``devDependencies``, so a clean install of the published
#: package cannot be imported at all without it (upstream packaging defect --
#: see docs/research/tscircuit-evaluation.md).
NPM_PINS = {
    "@tscircuit/core": "0.0.2031",
    "circuit-json-to-kicad": "0.0.228",
    "polygon-clipping": "0.15.7",
}

HARNESS_FILES = ("examples.mjs", "generate.mjs")


def cache_dir(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    if env := os.environ.get(CACHE_DIR_ENV_VAR):
        return Path(env).expanduser().resolve()
    return DEFAULT_CACHE_DIR


@dataclass
class CheckOutcome:
    """One ``kct check --mfr`` result."""

    ran: bool
    errors: int = 0
    warnings: int = 0
    drc: str = "NOT RUN"
    erc: str = "NOT RUN"
    lvs: str = "NOT RUN"
    overall: str = "NOT RUN"
    drc_detail: str = ""
    erc_detail: str = ""
    lvs_detail: str = ""
    #: ``"<rule_id> (<severity>)" -> count``, derived from the violation list --
    #: NOT from ``summary.rules_checked_by_rule``, which counts rules *run*.
    violations_by_rule: dict[str, int] = field(default_factory=dict)
    sample_messages: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "errors": self.errors,
            "warnings": self.warnings,
            "drc": self.drc,
            "erc": self.erc,
            "lvs": self.lvs,
            "overall": self.overall,
            "drc_detail": self.drc_detail,
            "erc_detail": self.erc_detail,
            "lvs_detail": self.lvs_detail,
            "violations_by_rule": self.violations_by_rule,
            "sample_messages": self.sample_messages,
            "note": self.note,
        }


@dataclass
class DrcOutcome:
    """One ``kicad-cli pcb drc`` result."""

    ran: bool
    errors: int = 0
    warnings: int = 0
    unconnected: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    refill_zones: bool = False
    #: True when a sibling ``<board>.kicad_dru`` was present, which KiCad picks
    #: up automatically. ``kct route`` emits one, so the two passes are not
    #: enforcing an identical rule set -- record it rather than hide it.
    kicad_dru_present: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "errors": self.errors,
            "warnings": self.warnings,
            "unconnected": self.unconnected,
            "by_type": self.by_type,
            "refill_zones": self.refill_zones,
            "kicad_dru_present": self.kicad_dru_present,
            "note": self.note,
        }


class KicadCli:
    """Runs ``kicad-cli`` natively or in a container, whichever is available."""

    def __init__(
        self,
        *,
        explicit: str | None = None,
        docker_image: str = DEFAULT_KICAD_DOCKER_IMAGE,
        allow_docker: bool = True,
    ) -> None:
        self.native: str | None = explicit or shutil.which("kicad-cli")
        self.docker_image = docker_image
        self.allow_docker = allow_docker
        self.mode = "native" if self.native else ("docker" if allow_docker else "none")
        self._supports_refill: bool | None = None

    @property
    def available(self) -> bool:
        return self.mode != "none"

    def describe(self) -> str:
        if self.mode == "native":
            return f"native {self.native}"
        if self.mode == "docker":
            return f"docker {self.docker_image}"
        return "unavailable"

    def _run(
        self, args: list[str], *, workdir: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if self.mode == "native":
            assert self.native is not None
            cmd = [self.native, *args]
        else:
            cmd = [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "-v",
                f"{workdir}:/work",
                "-w",
                "/work",
                "--entrypoint",
                "kicad-cli",
                self.docker_image,
                *args,
            ]
        return subprocess.run(
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def supports_refill_zones(self, workdir: Path) -> bool:
        if self._supports_refill is None:
            result = self._run(["pcb", "drc", "--help"], workdir=workdir, timeout=600)
            blob = result.stdout + result.stderr
            self._supports_refill = "--refill-zones" in blob
        return self._supports_refill

    def drc(self, board: Path, *, timeout: int = 900) -> DrcOutcome:
        if not self.available:
            return DrcOutcome(
                ran=False,
                note="no kicad-cli (native or container) available on this host",
            )
        workdir = board.parent
        report = workdir / f"{board.stem}.kicad-cli-drc.json"
        dru_present = board.with_suffix(".kicad_dru").exists()
        args = ["pcb", "drc", "--format", "json", "--severity-all"]
        refill = self.supports_refill_zones(workdir)
        if refill:
            args.append("--refill-zones")
        args += ["-o", report.name, board.name]
        try:
            result = self._run(args, workdir=workdir, timeout=timeout)
        except subprocess.TimeoutExpired:
            return DrcOutcome(
                ran=False,
                refill_zones=refill,
                kicad_dru_present=dru_present,
                note=f"timed out after {timeout}s",
            )
        if not report.exists():
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            return DrcOutcome(
                ran=False,
                refill_zones=refill,
                kicad_dru_present=dru_present,
                note=f"no report written (rc={result.returncode}): {' / '.join(tail)}",
            )
        data = json.loads(report.read_text())
        errors = warnings = 0
        by_type: dict[str, int] = {}
        for violation in data.get("violations", []):
            severity = violation.get("severity", "")
            if severity == "error":
                errors += 1
            elif severity == "warning":
                warnings += 1
            key = f"{violation.get('type', '?')} ({severity})"
            by_type[key] = by_type.get(key, 0) + 1
        return DrcOutcome(
            ran=True,
            errors=errors,
            warnings=warnings,
            unconnected=len(data.get("unconnected_items", []) or []),
            by_type=dict(sorted(by_type.items())),
            refill_zones=refill,
            kicad_dru_present=dru_present,
        )


def provision_workspace(workspace: Path, *, install: bool) -> dict[str, str]:
    """Create the npm workspace, install the pinned packages, copy the harness."""
    workspace.mkdir(parents=True, exist_ok=True)
    package_json = workspace / "package.json"
    if not package_json.exists():
        package_json.write_text(
            json.dumps(
                {
                    "name": "kct-tscircuit-interop-gate",
                    "private": True,
                    "type": "module",
                    "description": "Gitignored run-time workspace for Issue #5847.",
                },
                indent=2,
            )
            + "\n"
        )
    if install:
        specs = [f"{name}@{version}" for name, version in sorted(NPM_PINS.items())]
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", "--save-exact", *specs],
            cwd=str(workspace),
            check=True,
        )
    for filename in HARNESS_FILES:
        shutil.copy2(HERE / filename, workspace / filename)

    resolved: dict[str, str] = {}
    for name in NPM_PINS:
        manifest = workspace / "node_modules" / Path(name) / "package.json"
        if manifest.exists():
            resolved[name] = json.loads(manifest.read_text()).get("version", "?")
    return resolved


def emit_projects(workspace: Path, exports: Path) -> dict[str, Any]:
    """Run ``generate.mjs``; returns the parsed ``generate.json`` summary."""
    if exports.exists():
        shutil.rmtree(exports)
    result = subprocess.run(
        ["node", "generate.mjs", str(exports)],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    sys.stdout.write(result.stdout)
    summary_path = exports / "generate.json"
    if not summary_path.exists():
        raise SystemExit(
            f"generate.mjs produced no summary (rc={result.returncode})\n{result.stderr}"
        )
    return json.loads(summary_path.read_text())


def run_kct_check(board: Path, *, mfr: str, timeout: int = 1800) -> CheckOutcome:
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(board),
        "--mfr",
        mfr,
        "--format",
        "json",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckOutcome(ran=False, note=f"kct check timed out after {timeout}s")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        return CheckOutcome(ran=False, note=f"unparseable output: {' / '.join(tail)}")
    summary = data.get("summary", {})
    meta = data.get("meta_checks", {})
    by_rule: dict[str, int] = {}
    samples: dict[str, str] = {}
    for violation in data.get("violations", []) or []:
        if violation.get("waived"):
            continue
        key = f"{violation.get('rule_id') or violation.get('type') or '?'} ({violation.get('severity')})"
        by_rule[key] = by_rule.get(key, 0) + 1
        samples.setdefault(key, str(violation.get("message", ""))[:200])
    return CheckOutcome(
        ran=True,
        errors=int(summary.get("errors", 0)),
        warnings=int(summary.get("warnings", 0)),
        drc=_meta_status(meta.get("drc")),
        erc=_meta_status(meta.get("erc")),
        lvs=_meta_status(meta.get("lvs")),
        overall=_meta_status(meta.get("overall")),
        drc_detail=_meta_detail(meta.get("drc")),
        erc_detail=_meta_detail(meta.get("erc")),
        lvs_detail=_meta_detail(meta.get("lvs")),
        violations_by_rule=dict(sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0]))),
        sample_messages=samples,
    )


def _meta_status(value: Any) -> str:
    """``meta_checks`` entries are either a bare status or a dict carrying one."""
    if isinstance(value, dict):
        return str(value.get("status", "NOT RUN"))
    if value is None:
        return "NOT RUN"
    return str(value)


def _meta_detail(value: Any) -> str:
    return str(value.get("detail", "")) if isinstance(value, dict) else ""


def rip_up_and_route(
    source: Path,
    destination: Path,
    *,
    strategy: str,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Strip all copper from ``source`` and re-route it with ``kct route``.

    Footprints, pads, nets, zones and the board outline are preserved -- the
    same rip-up contract ``benchmarks/external/normalize.py`` uses.
    """
    from kicad_tools.schema.pcb import PCB

    destination.parent.mkdir(parents=True, exist_ok=True)
    # The sibling .kicad_sch/.kicad_pro must travel with the board so `kct
    # check` can still run ERC and LVS against the re-routed copy.
    for sibling in sorted(source.parent.iterdir()):
        if sibling.suffix in {".kicad_sch", ".kicad_pro"}:
            shutil.copy2(sibling, destination.parent / sibling.name)

    pcb = PCB.load(source)
    before = pcb.routing_status()
    removed_segments = pcb.remove_segments(list(pcb.segments))
    removed_vias = pcb.remove_vias(list(pcb.vias))
    pcb.save(destination)

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(destination),
        "-o",
        str(destination),
        "--strategy",
        strategy,
        "--format",
        "json",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ripped_segments": removed_segments,
            "ripped_vias": removed_vias,
            "routed": False,
            "note": f"kct route timed out after {timeout}s",
        }

    after = PCB.load(destination).routing_status()
    diagnostics: dict[str, Any] = {}
    try:
        diagnostics = json.loads(result.stdout)
    except json.JSONDecodeError:
        diagnostics = {}
    return {
        "ripped_segments": removed_segments,
        "ripped_vias": removed_vias,
        "routed": result.returncode == 0,
        "returncode": result.returncode,
        "tscircuit_segments": before["segments"],
        "tscircuit_vias": before["vias"],
        "kct_segments": after["segments"],
        "kct_vias": after["vias"],
        "kct_unrouted_pads": len(after["unrouted_pads"]),
        "completion": diagnostics.get("completion") or diagnostics.get("summary") or {},
        "stderr_tail": (result.stderr or "").strip().splitlines()[-3:],
    }


def results_markdown(payload: dict[str, Any]) -> str:
    """Render the per-example results table recorded in the evaluation note."""
    lines = [
        "| Example | Pass | kct check errors/warnings | kct DRC | ERC | LVS | kicad-cli DRC (err/warn) |",
        "|---|---|---:|---|---|---|---|",
    ]
    for entry in payload["examples"]:
        for pass_name, key in (("as emitted", "as_emitted"), ("kct-routed", "kct_routed")):
            stage = entry.get(key) or {}
            check = stage.get("kct_check") or {}
            drc = stage.get("kicad_cli_drc") or {}
            if not check.get("ran"):
                cell = f"n/a ({check.get('note', 'not run')})"
                lines.append(f"| `{entry['slug']}` | {pass_name} | {cell} | - | - | - | - |")
                continue
            drc_cell = (
                f"{drc.get('errors', 0)}/{drc.get('warnings', 0)}"
                if drc.get("ran")
                else f"not run ({drc.get('note', '')[:40]})"
            )
            lines.append(
                f"| `{entry['slug']}` | {pass_name} | "
                f"{check['errors']}/{check['warnings']} | {check['drc']} | "
                f"{check['erc']} | {check['lvs']} | {drc_cell} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=None, help="Override the gitignored workspace root.")
    parser.add_argument(
        "--mfr", default="jlcpcb", help="Manufacturer profile for `kct check --mfr`."
    )
    parser.add_argument("--strategy", default="negotiated", help="`kct route --strategy` value.")
    parser.add_argument("--only", default=None, help="Comma-separated slugs to gate.")
    parser.add_argument(
        "--skip-install", action="store_true", help="Reuse the installed node_modules."
    )
    parser.add_argument(
        "--skip-emit", action="store_true", help="Reuse the previously emitted exports."
    )
    parser.add_argument(
        "--skip-route", action="store_true", help="Only gate the as-emitted copper."
    )
    parser.add_argument("--kicad-cli", default=None, help="Explicit kicad-cli path.")
    parser.add_argument("--kicad-docker-image", default=DEFAULT_KICAD_DOCKER_IMAGE)
    parser.add_argument("--no-docker", action="store_true", help="Never fall back to a container.")
    args = parser.parse_args(argv)

    root = cache_dir(args.cache_dir)
    workspace = root
    exports = root / "exports"
    results_dir = root / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    resolved = provision_workspace(workspace, install=not args.skip_install)
    print(f"npm versions: {resolved}")

    if args.skip_emit:
        generate_summary = json.loads((exports / "generate.json").read_text())
    else:
        generate_summary = emit_projects(workspace, exports)

    wanted = set(args.only.split(",")) if args.only else None
    kicad = KicadCli(
        explicit=args.kicad_cli,
        docker_image=args.kicad_docker_image,
        allow_docker=not args.no_docker,
    )
    print(f"kicad-cli: {kicad.describe()}")

    examples: list[dict[str, Any]] = []
    for emitted in generate_summary["results"]:
        slug = emitted["slug"]
        if wanted and slug not in wanted:
            continue
        entry: dict[str, Any] = {
            "slug": slug,
            "title": emitted["title"],
            "emitted_ok": emitted["ok"],
            "circuit_json_histogram": emitted.get("circuit_json_histogram", {}),
            "tscircuit_diagnostics": emitted.get("diagnostics", []),
        }
        if not emitted["ok"]:
            entry["emit_error"] = emitted.get("error")
            examples.append(entry)
            continue

        board = exports / slug / f"{slug}.kicad_pcb"
        print(f"\n=== {slug}: gating the tscircuit-routed copper")
        entry["as_emitted"] = {
            "kct_check": run_kct_check(board, mfr=args.mfr).to_dict(),
            "kicad_cli_drc": kicad.drc(board).to_dict(),
        }

        if not args.skip_route:
            print(f"=== {slug}: ripping up copper and re-routing with kct route")
            rerouted = exports / f"{slug}-kct-routed" / f"{slug}.kicad_pcb"
            route_info = rip_up_and_route(board, rerouted, strategy=args.strategy)
            entry["kct_routed"] = {
                "route": route_info,
                "kct_check": run_kct_check(rerouted, mfr=args.mfr).to_dict(),
                "kicad_cli_drc": kicad.drc(rerouted).to_dict(),
            }
        examples.append(entry)

    payload = {
        "npm_versions": resolved,
        "node": generate_summary.get("node"),
        "manufacturer": args.mfr,
        "route_strategy": args.strategy,
        "kicad_cli": kicad.describe(),
        "examples": examples,
    }
    (results_dir / "results.json").write_text(json.dumps(payload, indent=2) + "\n")
    (results_dir / "results.md").write_text(results_markdown(payload))
    print(f"\nWrote {results_dir / 'results.json'} and {results_dir / 'results.md'}")
    print()
    print(results_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
