"""Thin wrappers around the optional ``kicad-cli pcb export`` outputs.

``kct export-formats`` (Issue #8017) produces PDF, SVG, STEP, IPC-2581 and
ODB++ from a ``.kicad_pcb``.  Each format is run independently and reported
honestly: a format is ``ok`` only when kicad-cli exited 0 *and* wrote a
non-empty file (kicad-cli can exit 0 without writing anything).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ..cli.kicad_toolchain import KICAD_DOCKER_ENV, docker_fallback_enabled
from ..cli.runner import (
    build_docker_kicad_cmd,
    find_kicad_cli,
    kicad_cli_unavailable_message,
)

#: Supported formats, in the order they are produced, with the output suffix.
FORMAT_SUFFIX: dict[str, str] = {
    "pdf": ".pdf",
    "svg": ".svg",
    "step": ".step",
    "ipc2581": ".xml",
    "odb": ".zip",
}

#: Default PDF/SVG layers: the fabrication-relevant copper, mask, silkscreen
#: and board-outline layers of a two-layer board.
DEFAULT_LAYERS = "F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"

#: Phrases kicad-cli prints when it does not know an export subcommand.
_UNSUPPORTED_MARKERS = ("did you mean", "Failed to parse", "Unknown argument")


@dataclass
class FormatResult:
    """Outcome of one requested export format."""

    format: str
    status: str  # "ok" | "failed"
    path: str | None = None
    size_bytes: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_formats(value: str) -> list[str]:
    """Split a comma-separated ``--format`` value; raise ValueError on unknowns."""
    formats: list[str] = []
    for raw in value.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in FORMAT_SUFFIX:
            raise ValueError(f"unknown format '{name}' (choose from {', '.join(FORMAT_SUFFIX)})")
        if name not in formats:
            formats.append(name)
    if not formats:
        raise ValueError("no formats requested")
    return formats


def build_export_args(fmt: str, pcb: str, output: str, layers: str) -> list[str]:
    """kicad-cli arguments (without the executable) for one format."""
    args = ["pcb", "export", fmt]
    if fmt in ("pdf", "svg"):
        args += ["--mode-single", "--layers", layers]
        if fmt == "svg":
            args += ["--page-size-mode", "2", "--fit-page-to-board"]
    elif fmt == "step":
        args += ["--force"]
    args += ["--output", output, pcb]
    return args


def _kicad_version(kicad_cli: Path) -> str:
    try:
        r = subprocess.run([str(kicad_cli), "version"], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or "unknown version"
    except (OSError, subprocess.SubprocessError):
        return "unknown version"


def export_formats(
    pcb_path: Path,
    output_dir: Path,
    formats: list[str],
    layers: str = DEFAULT_LAYERS,
    timeout: int = 300,
    kicad_cli: Path | None = None,
) -> list[FormatResult]:
    """Run ``kicad-cli pcb export`` for each format and report per-format results."""
    pcb_path = pcb_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    docker_workdir: Path | None = None
    if kicad_cli is None:
        kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        if docker_fallback_enabled() and shutil.which("docker"):
            docker_workdir = Path(os.path.commonpath([str(pcb_path.parent), str(output_dir)]))
        else:
            msg = f"{kicad_cli_unavailable_message()} (or set {KICAD_DOCKER_ENV}=1 to use a container)"
            return [FormatResult(f, "failed", error=msg) for f in formats]

    def _arg(p: Path) -> str:
        if docker_workdir is None:
            return str(p)
        return "/work/" + p.relative_to(docker_workdir).as_posix()

    results: list[FormatResult] = []
    for fmt in formats:
        out = output_dir / f"{pcb_path.stem}{FORMAT_SUFFIX[fmt]}"
        if out.exists():
            out.unlink()  # a stale file must not mask a silent kicad-cli failure
        args = build_export_args(fmt, _arg(pcb_path), _arg(out), layers)
        cmd = (
            build_docker_kicad_cmd(args, docker_workdir)
            if docker_workdir is not None
            else [str(kicad_cli), *args]
        )
        results.append(
            _run_one(fmt, cmd, out, timeout, kicad_cli if docker_workdir is None else None)
        )
    return results


def _run_one(
    fmt: str, cmd: list[str], out: Path, timeout: int, kicad_cli: Path | None
) -> FormatResult:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return FormatResult(fmt, "failed", error=f"{fmt} export timed out after {timeout}s")
    except (OSError, subprocess.SubprocessError) as e:
        return FormatResult(fmt, "failed", error=f"failed to run kicad-cli: {e}")

    stderr = (proc.stderr or "").strip()
    size = out.stat().st_size if out.exists() else 0
    if proc.returncode != 0:
        if any(m in stderr for m in _UNSUPPORTED_MARKERS) and size == 0:
            ver = _kicad_version(kicad_cli) if kicad_cli else "the container's kicad-cli"
            return FormatResult(fmt, "failed", error=f"unsupported by kicad-cli {ver}")
        detail = f": {stderr}" if stderr else ""
        return FormatResult(fmt, "failed", error=f"kicad-cli exited {proc.returncode}{detail}")
    if size == 0:
        return FormatResult(
            fmt, "failed", error="kicad-cli exited 0 but wrote no/empty output file"
        )
    return FormatResult(fmt, "ok", path=str(out), size_bytes=size)
