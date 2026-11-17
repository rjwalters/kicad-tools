"""Optional bridge to KiCad's own generators, run strictly as an external tool.

KiCad's 3D generators live in ``kicad-library-tools`` and are
**GPL-3.0-or-later**.  kicad-tools is MIT, so it must never import or vendor
that code (issue #7687; a license-scan test guards ``src/``).  It may *run* it:
this module launches a user-installed generator as a subprocess -- typically
from its own virtualenv -- and records provenance for whatever STEP files it
writes.

There is no built-in knowledge of that tool's command line.  The operator
supplies a command template (``--bridge-cmd`` or ``KCT_KICAD_LIBRARY_TOOLS_CMD``)
with ``{target}``, ``{out_dir}`` and ``{params_json}`` placeholders, e.g.::

    KCT_KICAD_LIBRARY_TOOLS_CMD='/opt/klt/venv/bin/python /opt/klt/gen.py \\
        --family {target} --params {params_json} --output {out_dir}'

Output of the KiCad generator is treated as CC-BY-SA-4.0-style collection
data: the provenance records the attribution requirement, and the operator
decides whether it may be committed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BRIDGE_CMD_ENV",
    "BRIDGE_LICENSE",
    "BRIDGE_VERSION_ENV",
    "BridgeError",
    "BridgeResult",
    "run_external_generator",
]

BRIDGE_CMD_ENV = "KCT_KICAD_LIBRARY_TOOLS_CMD"
BRIDGE_VERSION_ENV = "KCT_KICAD_LIBRARY_TOOLS_VERSION"

BRIDGE_LICENSE = (
    "output of kicad-library-tools (GPL-3.0-or-later), run as an external "
    "subprocess and never imported; treat as CC-BY-SA-4.0-style collection "
    "data with attribution to the KiCad Library Team"
)


class BridgeError(RuntimeError):
    """The external generator is not configured, failed, or wrote nothing."""


@dataclass
class BridgeResult:
    files: list[Path] = field(default_factory=list)
    provenance_paths: list[Path] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    stdout: str = ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_external_generator(
    target: str,
    params: dict[str, Any],
    out_dir: Path,
    *,
    command: str | None = None,
    timeout: float = 600.0,
) -> BridgeResult:
    """Run the configured external generator; record provenance per STEP file."""
    template = command or os.environ.get(BRIDGE_CMD_ENV)
    if not template:
        raise BridgeError(
            "the kicad-library-tools bridge is not configured: install it in its own "
            f"virtualenv and set {BRIDGE_CMD_ENV} (or --bridge-cmd) to a command template "
            "using {target}, {out_dir} and {params_json}"
        )
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kct-bridge-") as scratch:
        scratch_dir = Path(scratch)
        params_json = scratch_dir / "params.json"
        params_json.write_text(json.dumps(params, sort_keys=True))
        work = scratch_dir / "out"
        work.mkdir()
        argv = [
            part.format(target=target, out_dir=str(work), params_json=str(params_json))
            for part in shlex.split(template)
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - operator-supplied command, no shell
                argv,
                cwd=work,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError(f"external generator failed to run: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
            raise BridgeError(f"external generator exited {proc.returncode}: " + " | ".join(tail))
        produced = sorted(
            p for p in work.rglob("*") if p.is_file() and p.suffix.lower() in (".step", ".stp")
        )
        if not produced:
            raise BridgeError("external generator exited 0 but wrote no STEP file")

        result = BridgeResult(command=argv, stdout=proc.stdout)
        for src in produced:
            dest = out_dir / src.name
            dest.write_bytes(src.read_bytes())
            prov = {
                "generator": "kicad-library-tools (external bridge)",
                "generator_version": os.environ.get(BRIDGE_VERSION_ENV, "unknown"),
                "backend": "external subprocess",
                "command": [
                    a.replace(str(params_json), "<params_json>").replace(str(work), "<out_dir>")
                    for a in argv
                ],
                "family": target,
                "params": params,
                "placeholder": False,
                "license": BRIDGE_LICENSE,
                "step": {"file": dest.name, "sha256": _sha256(dest)},
                "issue": 7687,
            }
            prov_path = dest.with_name(dest.stem + ".provenance.json")
            prov_path.write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n")
            result.files.append(dest)
            result.provenance_paths.append(prov_path)
        return result
