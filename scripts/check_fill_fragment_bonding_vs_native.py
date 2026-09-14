#!/usr/bin/env python3
"""Re-derive the native fill-fragment bonding table behind Issue #5362.

Emits the same board bytes as ``tests/test_fill_fragment_bonding_5362.py``,
runs native ``kicad-cli pcb drc`` on each inside the pinned ``kicad/kicad:10.0``
image, and prints native ``unconnected_items`` beside this repo's
:class:`NetStatusAnalyzer` / :meth:`ConnectivityValidator.extract_pad_partition`
verdicts.  This is the measurement the #5362 fix is derived from, kept runnable
so a reviewer can reproduce it rather than take the table on trust.

The series answers one question: when are two ``filled_polygon`` fragments of a
*single* zone one piece of copper?  Native KiCad's answer is neither "always"
(this repo's pre-#5362 behaviour, which unioned every fragment of a zone by
zone identity) nor "when they touch" — it depends on the fill *encoding*:

* ``filled_areas_thickness`` absent (the KiCad format default, ``yes``): the
  stored outline is the centre-line of ``min_thickness``-wide copper, so two
  fragments bond exactly when their outlines are within ``min_thickness``.
* ``(filled_areas_thickness no)``: the stored outline *is* the copper, and two
  fill outlines of one zone never bond to each other — not at a shared corner,
  a shared edge, or across an overlapping band.

In both encodings a real conductor (pad, via, track) reaching into two
fragments bonds them.

DRC is run with **no** ``--refill-zones`` and no ``--save-board``, and each
board's SHA256 is checked before and after so the verdict is known to be about
the bytes on disk.

Usage
-----
    uv run python scripts/check_fill_fragment_bonding_vs_native.py

Requires ``docker`` (directly or via passwordless ``sudo``) with
``kicad/kicad:10.0`` available — digest ``sha256:182c8005cb77...``, KiCad
10.0.5, the image recorded in #5358/#5362.  Exits non-zero if any row
disagrees with native.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KICAD_IMAGE = "kicad/kicad:10.0"

VERIFY_IN_CONTAINER = r"""
import hashlib, json, pathlib, subprocess, sys
out = {}
for pcb in sorted(pathlib.Path("/w").glob("*.kicad_pcb")):
    before = hashlib.sha256(pcb.read_bytes()).hexdigest()
    subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "--output", "/tmp/o.json", str(pcb)],
        capture_output=True,
    )
    try:
        report = json.load(open("/tmp/o.json"))
    except Exception as exc:  # pragma: no cover - surfaced in the printed table
        out[pcb.stem] = {"error": str(exc)}
        continue
    after = hashlib.sha256(pcb.read_bytes()).hexdigest()
    out[pcb.stem] = {
        "unconnected": len(report.get("unconnected_items", [])),
        "sha256": before,
        "hash_stable": before == after,
    }
print(json.dumps(out))
"""


def _docker_cmd() -> list[str] | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    if subprocess.run([docker, "info"], capture_output=True).returncode == 0:
        return [docker]
    if subprocess.run(["sudo", "-n", docker, "info"], capture_output=True).returncode == 0:
        return ["sudo", "-n", docker]
    return None


def run_native(board_dir: Path, docker: list[str]) -> dict[str, dict]:
    """DRC every board in ``board_dir`` inside the pinned KiCad image."""
    script = board_dir / "_verify.py"
    script.write_text(VERIFY_IN_CONTAINER)
    proc = subprocess.run(
        [
            *docker,
            "run",
            "--rm",
            "-u",
            "0:0",
            "-v",
            f"{board_dir}:/w",
            "-w",
            "/w",
            KICAD_IMAGE,
            "python3",
            "/w/_verify.py",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"native DRC run failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--keep",
        type=Path,
        default=None,
        help="Write the generated boards here instead of a temporary directory.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from test_fill_fragment_bonding_5362 import CASES  # noqa: PLC0415

    from kicad_tools.analysis.net_status import NetStatusAnalyzer  # noqa: PLC0415
    from kicad_tools.validate.connectivity import ConnectivityValidator  # noqa: PLC0415

    board_dir = args.keep or Path(tempfile.mkdtemp(prefix="kct-5362-"))
    board_dir.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        (board_dir / f"{case.name}.kicad_pcb").write_text(case.board())

    docker = _docker_cmd()
    if docker is None:
        print(
            f"SKIP: {KICAD_IMAGE} is not reachable via docker or passwordless sudo "
            "docker; cannot measure native ground truth.",
            file=sys.stderr,
        )
        return 2
    native = run_native(board_dir, docker)

    print(f"{'case':24s} {'native':>7s} {'recorded':>9s} {'islands':>8s} {'lvs':>6s}  verdict")
    failures = 0
    for case in CASES:
        pcb = board_dir / f"{case.name}.kicad_pcb"
        entry = native.get(case.name, {})
        measured = entry.get("unconnected")
        sha = hashlib.sha256(pcb.read_bytes()).hexdigest()

        status = NetStatusAnalyzer(pcb, strict=True).analyze().get_net("GND")
        islands = status.island_count if status is not None else -1
        partition = ConnectivityValidator(pcb).extract_pad_partition()
        component = next((c for c in partition if "R13.1" in c), frozenset())
        together = "R14.1" in component

        native_connected = measured == 0
        ok = (
            measured == case.native_unconnected
            and entry.get("hash_stable") is True
            and entry.get("sha256") == sha
            and islands == (1 if native_connected else 2)
            and together is native_connected
        )
        failures += 0 if ok else 1
        print(
            f"{case.name:24s} {measured!s:>7s} {case.native_unconnected:>9d} "
            f"{islands:>8d} {str(together):>6s}  {'ok' if ok else 'MISMATCH'}"
        )

    print(f"\nboards: {board_dir}")
    print(f"rows disagreeing with native: {failures}/{len(CASES)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
