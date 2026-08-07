"""Shared softstart rev-C fixture pinning for the lattice proofs (issue #4670).

``boards/external/softstart`` is a local-only relative symlink to a sibling
checkout of the external softstart repo; it dangles in CI and in fresh
worktrees, so both softstart lattice proof modules skip when the board file
is absent.

Because the fixture lives in an *external* repo, it can drift underneath the
pinned assertions (issue #4670: the NRST star-break rework, softstart PR #26,
grew the anchor-star topology 287 -> 295 connections and the old pin failed
with a bare topology assert).  To convert that confusing failure into a
self-explanatory skip, the board artifact is pinned by content hash: present
but hash-mismatched -> skip with a message naming both hashes.

Re-pin procedure (when the softstart board legitimately moves):

1. Update ``FIXTURE_SHA256`` below (``shasum -a 256 <board>``).
2. Re-measure the routing proof (``pytest -s
   tests/router/lattice/test_softstart_routing.py``) and update its topology
   count and floors from the printed census.
3. Re-run ``test_softstart_memory.py`` and refresh its narrative if the
   board outline changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
SOFTSTART_DIR = _REPO / "boards/external/softstart/output_revc"
SOFTSTART_BOARD = SOFTSTART_DIR / "softstart_revc.kicad_pcb"
SIDECAR = SOFTSTART_DIR / "net_class_map.json"

# Content hash of the pinned board artifact (softstart commit 7800b04,
# measured 2026-08-07).  A content hash beats a git sha because the symlink
# target need not be a git checkout -- the artifact, not the repo state, is
# what the proofs are pinned to.
FIXTURE_SHA256 = "87ebe3af1bd36bde47bc6566d8b0bd9f970ef6183f51b2903ae08e790950beea"


def fixture_skip_reason() -> str | None:
    """Why the softstart proofs must skip, or ``None`` to run them.

    Absent fixture (CI, fresh worktrees) and drifted fixture (newer local
    softstart) both SKIP -- an external repo moving must never red this
    suite; the pin's job is to make the drift self-explanatory.
    """
    if not SOFTSTART_BOARD.exists():
        return "local-only softstart fixture absent"
    actual = hashlib.sha256(SOFTSTART_BOARD.read_bytes()).hexdigest()
    if actual != FIXTURE_SHA256:
        return (
            f"softstart fixture drifted: pinned sha256 {FIXTURE_SHA256}, "
            f"observed {actual} -- re-pin the proofs per the procedure in "
            f"tests/router/lattice/softstart_fixture.py (issue #4670)"
        )
    return None
