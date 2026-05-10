#!/usr/bin/env python3
"""Fetch chorus-test-revA PCB fixture for the routing benchmark (Issue #2611).

The chorus-test-revA board lives in a separate (private) hardware
repository because the kicad-tools repo does not ship real-world PCBs.
This script makes the fixture available on a CI runner by cloning the
upstream repo (or copying from a local path) into
``boards/external/chorus-test-revA/``.

The script is intentionally best-effort: when the fixture source is
unavailable (no env var, no local copy, no network), it exits 0 with a
clear "skipped" message rather than failing.  The benchmark runner
itself also skips gracefully when the file is missing, so a
fetch-failure on CI manifests as the chorus case being absent from the
nightly report rather than a red workflow.

Sources are tried in order:

1. ``$CHORUS_TEST_LOCAL_PATH`` -- a filesystem path to an existing
   chorus-test-revA directory (used in local dev and self-hosted
   runners with the board pre-staged).
2. ``$CHORUS_TEST_GIT_URL`` -- a git URL to clone into a temp dir.
   When set together with ``$CHORUS_TEST_GIT_REF`` the script checks
   out that ref; otherwise it uses the default branch.

Both paths converge on copying the ``chorus-test-revA/`` directory
into ``boards/external/`` of the working tree.  The benchmark case
in ``src/kicad_tools/benchmark/cases.py`` points at
``boards/external/chorus-test-revA/kicad/chorus-test-revA_v18.kicad_pcb``.

Exit codes
----------
0 -- Fixture present (either already on disk or newly fetched), or
     fetch deliberately skipped because no source was configured.
1 -- Fetch attempted and failed (unexpected, non-graceful error).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TARGET_DIR = REPO_ROOT / "boards" / "external" / "chorus-test-revA"
EXPECTED_PCB = TARGET_DIR / "kicad" / "chorus-test-revA_v18.kicad_pcb"


def _log(msg: str) -> None:
    """Emit a status line.  Uses ::notice:: on GH Actions for visibility."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice::fetch_chorus_test: {msg}")
    else:
        print(f"[fetch_chorus_test] {msg}")


def already_present() -> bool:
    """Return True when the v18 PCB is already in place."""
    return EXPECTED_PCB.exists()


def fetch_from_local_path(local_path: Path) -> bool:
    """Copy a chorus-test-revA directory tree into TARGET_DIR.

    Args:
        local_path: Existing on-disk copy of the chorus-test-revA dir,
            either pointing at the ``chorus-test-revA/`` directory
            itself or its parent containing it.

    Returns:
        True on success, False if the path does not yield the expected
        PCB.
    """
    if not local_path.exists():
        _log(f"local path does not exist: {local_path}")
        return False

    # Accept either chorus-test-revA/ or its containing directory.
    src = local_path
    if not (src / "kicad").exists() and (src / "chorus-test-revA" / "kicad").exists():
        src = src / "chorus-test-revA"

    if not (src / "kicad" / "chorus-test-revA_v18.kicad_pcb").exists():
        _log(f"local path has no chorus-test-revA_v18 PCB: {src}")
        return False

    TARGET_DIR.parent.mkdir(parents=True, exist_ok=True)
    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    shutil.copytree(src, TARGET_DIR)
    _log(f"copied chorus-test-revA from {src} -> {TARGET_DIR}")
    return already_present()


def fetch_from_git(url: str, ref: str | None = None) -> bool:
    """Clone the upstream repo into a temp dir, then copy the board out."""
    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "chorus"
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [url, str(clone_dir)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            _log(f"git clone failed: {e.stderr.strip()}")
            return False

        # The chorus repo layout is .../hardware/chorus-test-revA.
        candidate = clone_dir / "hardware" / "chorus-test-revA"
        if not candidate.exists():
            # Fall back: search anywhere in the clone for the board.
            matches = list(clone_dir.rglob("chorus-test-revA"))
            if not matches:
                _log("clone succeeded but no chorus-test-revA dir found")
                return False
            candidate = matches[0]

        return fetch_from_local_path(candidate)


def main() -> int:
    if already_present():
        _log(f"chorus-test-revA already present at {EXPECTED_PCB} -- nothing to do")
        return 0

    local = os.environ.get("CHORUS_TEST_LOCAL_PATH")
    if local:
        _log(f"attempting fetch from CHORUS_TEST_LOCAL_PATH={local}")
        if fetch_from_local_path(Path(local)):
            return 0
        _log("local-path fetch did not yield the expected PCB")

    url = os.environ.get("CHORUS_TEST_GIT_URL")
    if url:
        ref = os.environ.get("CHORUS_TEST_GIT_REF") or None
        _log(f"attempting git fetch from CHORUS_TEST_GIT_URL={url} ref={ref or 'default'}")
        if fetch_from_git(url, ref):
            return 0
        _log("git fetch failed")

    # Neither source produced the fixture; skip rather than fail.  The
    # nightly job will note the case is absent and move on; the
    # benchmark runner itself also skips missing fixtures gracefully.
    _log(
        "chorus-test-revA fixture not configured (set CHORUS_TEST_LOCAL_PATH "
        "or CHORUS_TEST_GIT_URL).  Skipping fixture fetch -- the benchmark "
        "runner will skip the case if the PCB remains absent."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
