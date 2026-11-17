#!/usr/bin/env python3
"""Re-derive the native fill-fragment bonding table behind Issue #5362.

Emits the same board bytes as ``tests/test_fill_fragment_bonding_5362.py``,
runs native ``kicad-cli pcb drc`` on each inside the pinned KiCad 10.0.5
image (digest below), and prints native ``unconnected_items`` beside this repo's
:class:`NetStatusAnalyzer` / :meth:`ConnectivityValidator.extract_pad_partition`
verdicts.  This is the measurement the #5362 fix is derived from, kept runnable
so a reviewer can reproduce it rather than take the table on trust.

The series answers one question: when are two ``filled_polygon`` fragments of a
*single* zone one piece of copper?  Native KiCad's answer is neither "always"
(this repo's pre-#5362 behaviour, which unioned every fragment of a zone by
zone identity) nor "when they touch" — it depends on the fill *encoding*:

KiCad initializes the fill mode from the file version: versions before
``20250210`` use stroked outlines; versions at or after it use solid outlines.
An explicit ``(filled_areas_thickness no)`` clears the stroked state, while
explicit ``yes`` is a no-op. An absent token preserves the version default.

* Stroked mode: the stored outline is the centre-line of
  ``min_thickness``-wide copper, so fragments bond when their outlines are
  within ``min_thickness``.
* Solid mode: the stored outline is the copper, and fill outlines of one zone
  do not bond directly at a shared corner, shared edge, or overlapping band.

In both encodings a real conductor (pad, via, track) reaching into two
fragments bonds them.

DRC is run with **no** ``--refill-zones`` and no ``--save-board``, and each
board's SHA256 is checked before and after so the verdict is known to be about
the bytes on disk.

Usage
-----
    uv run python scripts/check_fill_fragment_bonding_vs_native.py

Requires ``docker`` (directly or via passwordless ``sudo``) with the pinned
image below available — KiCad 10.0.5, the exact digest recorded in
#5358/#5362.  Exits non-zero if any row disagrees with native, including a
row whose native invocation itself failed or produced a malformed report.
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
KICAD_IMAGE = "kicad/kicad@sha256:182c8005cb775a2c448a4c18681d489f1ff472a761885eba3e08b07e3c0564de"

VERIFY_IN_CONTAINER = r"""
import hashlib, json, pathlib, subprocess, sys
out = {}
for pcb in sorted(pathlib.Path("/w").glob("*.kicad_pcb")):
    before = hashlib.sha256(pcb.read_bytes()).hexdigest()
    report_path = pcb.with_suffix(".drc.json")
    report_path.unlink(missing_ok=True)
    proc = subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "--output", str(report_path), str(pcb)],
        capture_output=True,
    )
    after = hashlib.sha256(pcb.read_bytes()).hexdigest()
    if proc.returncode != 0:
        out[pcb.stem] = {"error": f"kicad-cli exited {proc.returncode}: {proc.stderr.decode(errors='replace')}"}
        continue
    try:
        report = json.loads(report_path.read_text())
        unconnected_items = report["unconnected_items"]
        if not isinstance(unconnected_items, list):
            raise TypeError(f"unconnected_items is {type(unconnected_items).__name__}, not list")
    except Exception as exc:  # pragma: no cover - surfaced in the printed table
        out[pcb.stem] = {"error": str(exc)}
        continue
    out[pcb.stem] = {
        "unconnected": len(unconnected_items),
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
        entry = native.get(case.name)
        sha = hashlib.sha256(pcb.read_bytes()).hexdigest()

        status = NetStatusAnalyzer(pcb, strict=True).analyze().get_net("GND")
        islands = status.island_count if status is not None else -1
        partition = ConnectivityValidator(pcb).extract_pad_partition()
        component = next((c for c in partition if "R13.1" in c), frozenset())
        together = "R14.1" in component

        if entry is None or "error" in entry:
            failures += 1
            reason = entry["error"] if entry else "no native result reported"
            print(
                f"{case.name:24s} {'ERR':>7s} {case.native_unconnected:>9d} "
                f"{islands:>8d} {str(together):>6s}  MISMATCH (native invocation failed: {reason})"
            )
            continue

        measured = entry["unconnected"]
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
