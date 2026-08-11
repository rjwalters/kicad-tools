"""Gated same-host determinism smoke test for board-06 regen (Issue #4536).

Two consecutive same-seed ``generate_design.py --step route --seed 42``
runs against the *same committed unrouted PCB* must produce identical
routed artifacts after uuid normalization.

Why this exists
===============

Issue #4536: two flag-OFF (shadow-OFF) regens of unmodified main differed
by 9525 lines, making the "flag-OFF run must produce a byte-identical
committed artifact" scope-guard convention undecidable for board-06.
The instrumented run matrices (see the PR for #4536) localized THREE
sources:

1. **Dominant -- wall-clock truncation.** The negotiated phase's 360 s
   backstop straddled the phase's own natural runtime (363-367 s
   measured), firing mid-iteration-4 at a load-dependent net boundary in
   every run; with any finite stage timeout the #3989 remaining-budget
   per-net wall-clock caps also stayed active.  Four baseline runs
   serialized four artifacts with *different copper counts* (1599-1602
   segments).  Fixed with ``timeout=None`` (the stage is bounded by its
   deterministic iteration exits instead) plus ``PYTHONHASHSEED`` pinned
   by construction at the regen entry point.  ``max_iterations=3``
   replaces the truncation the backstop used to perform, in deterministic
   units: every instrumented run cut at the very start of iteration 4,
   and iterations 4+ are provably discarded by the end-of-loop lex
   restore.

2. **Reach-deciding -- the relief rescue's 10 s sub-search wall clock.**
   A rescue is a transaction: it rolls back unless every displaced
   victim re-lands, so the budget those re-lands get decides routed
   reach, not just runtime.  The flat 10 s budget straddles their 8-12 s
   natural time on GitHub runners -- the same commit landed 21/21 and
   20/21 on different runners from line-identical logs.  Fixed with
   ``route_all_negotiated(deterministic_rescue=True)``, which bounds the
   sub-searches by the deterministic per-net node-expansion cap.  Board 06
   opted in first (#4536) and still opts in explicitly: #4730 proposed
   making it the fleet default and withdrew the flip on a measured
   board-07 regression, so ``DETERMINISTIC_RESCUE_DEFAULT`` is ``False``
   and the kwarg in ``generate_design.py`` is the only thing switching the
   bound on for this board.

3. **Residual -- UUID-sorted file order.** With the wall clock removed,
   runs produced byte-identical logs and identical copper *multisets*,
   yet still ~2300 differing artifact lines: ``kicad-cli`` (invoked by
   every ``kct zones fill`` round) re-saves the board with tracks
   ordered by UUID, and the stitch/pour-repair emitters minted random
   ``uuid.uuid4()`` values -- so each run's refills sorted the identical
   copper into a different file order.  Fixed by minting deterministic
   UUIDs (content-derived uuid5 in ``kct stitch``, sequence-derived
   uuid5 in board-06's ``_generate_uuid``).

Normalization convention (documented per the #4536 acceptance criteria)
=======================================================================

Byte-identity is asserted **after normalizing uuid values** and nothing
else:

* Pad / property UUIDs inside footprints are minted by ``kicad-cli``
  itself on its first re-save (the generator emits them without UUID
  fields), so they legitimately differ run-to-run without any copper
  difference and without affecting element order.  Each ``(uuid "...")``
  is replaced with ``(uuid "X")`` before comparison.  Positional identity
  is still required: the *number and location* of uuid-bearing nodes must
  match exactly -- which, per source 3 above, only holds because
  stitch/repair copper UUIDs are now deterministic.
* No timestamps or dates are present in the emitted ``kicad_pcb``
  (verified against the artifact header), so nothing else is masked.

See also ``tests/router/test_board06_determinism.py`` (#3144), the
complementary gated integration test asserting DRC-count identity across
N re-routes.

Gating
======

A full route-step regen takes ~12 minutes, so this can never be a
default-run unit test.  It is skipped unless ``KCT_BOARD06_DETERMINISM=1``
is exported:

.. code-block:: bash

    KCT_BOARD06_DETERMINISM=1 uv run pytest tests/test_board06_determinism.py -v -s

Budget ~25 minutes for the two sequential regens, and run on an
otherwise-quiet host: under extreme concurrent load (load-average ~90)
a stagnation-recovery reroute can flip and produce different valid
copper -- a resource-exhaustion sensitivity tracked separately as #4724,
not a regression of the #4536 fixes.  The test prints the host
load-average before, between and after the regens (and repeats it in any
failure message) so a flaked run is attributable to load rather than
argued about (#4724).  See
``boards/06-diffpair-test/README.md`` ("Measuring Changes") for how this
test slots into the byte-identity scope-guard convention.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KCT_BOARD06_DETERMINISM") != "1",
    reason=(
        "board-06 determinism smoke test runs two ~12-minute regens; "
        "set KCT_BOARD06_DETERMINISM=1 to enable (issue #4536)"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"
UNROUTED_PCB = BOARD_DIR / "output" / "diffpair_test.kicad_pcb"

_UUID_RE = re.compile(r'\(uuid "[0-9a-fA-F-]+"\)')


def _loadavg_line(when: str) -> str:
    """One-line host load-average record for run attribution (Issue #4724).

    The residual divergence #4724 tracks is LOAD-CORRELATED: the single
    diverging run of the #4536 verification matrix was taken on a host at
    load-average ~90 (two parallel pytest suites plus a mypy pass), and its
    stagnation recovery re-landed one fewer net than the five quiet-host
    runs.  Without the load recorded next to the result, a future flake of
    this test is unattributable -- "the fix regressed" and "the host was
    saturated" look identical in the failure message.

    ``os.getloadavg`` is absent on Windows, where the record degrades to a
    stated unavailability rather than failing the test.
    """
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):  # pragma: no cover - platform-dependent
        return f"[board06-determinism] load-average {when}: unavailable on this platform"
    return (
        f"[board06-determinism] load-average {when}: "
        f"{one:.2f} {five:.2f} {fifteen:.2f} (cpus={os.cpu_count()})"
    )


def _normalize_uuids(text: str) -> str:
    """Mask every ``(uuid "...")`` value for content comparison.

    This masks *all* UUIDs -- both the random uuid4s KiCad mints for
    footprints/zones and the deterministic uuid5 copper UUIDs from the
    #4536 fix -- not just uuid4.  Identity is still enforced positionally:
    two artifacts only compare equal if their UUID fields occur at the same
    offsets in the same order, so a UUID-driven reorder still shows up as a
    diff even though the values themselves are masked.
    """
    return _UUID_RE.sub('(uuid "X")', text)


def _run_route_regen(out_dir: Path) -> str:
    """Run one ``--step route --seed 42`` regen; return the routed artifact text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(UNROUTED_PCB, out_dir / "diffpair_test.kicad_pcb")
    # No PYTHONHASHSEED in the env on purpose: the entry point must pin it
    # itself (the #4536 fix re-execs with PYTHONHASHSEED=<seed>), so this
    # test also guards that pinning.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}
    proc = subprocess.run(
        [
            sys.executable,
            str(BOARD_DIR / "generate_design.py"),
            "--step",
            "route",
            "--seed",
            "42",
            str(out_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    routed = out_dir / "diffpair_test_routed.kicad_pcb"
    assert proc.returncode == 0, (
        f"route regen failed (rc={proc.returncode}):\n"
        f"stdout tail:\n{proc.stdout[-4000:]}\nstderr tail:\n{proc.stderr[-4000:]}"
    )
    assert routed.exists(), f"routed artifact not written to {routed}"
    return routed.read_text()


def _copper_counts(text: str) -> tuple[int, int]:
    """(segment, via) node counts -- the count-identity invariant.

    Substring counting is safe for the current KiCad s-expression grammar:
    no ``(segments``/``(via_...`` (or any other ``(segment*``/``(via*``)
    token exists in a ``.kicad_pcb``, so these prefixes match only the
    copper element headers.  If KiCad ever adds such a token this needs to
    become a token-aware count.
    """
    return text.count("(segment"), text.count("(via")


def test_two_same_seed_route_regens_are_identical(tmp_path: Path) -> None:
    """Two consecutive same-seed regens produce identical normalized artifacts.

    Asserts the #4536 acceptance invariants in increasing strictness so a
    failure report names the loosest broken level:

    1. identical segment and via counts (count-identity), then
    2. byte-identity of the uuid-normalized artifacts.
    """
    # Issue #4724: record the host load around each regen (visible under
    # ``-s``, and captured in the failure message below) so a divergence can
    # be attributed to -- or cleared of -- host saturation.
    load_lines = [_loadavg_line("before run-a")]
    print(load_lines[-1], flush=True)
    first = _run_route_regen(tmp_path / "run-a")
    load_lines.append(_loadavg_line("between runs"))
    print(load_lines[-1], flush=True)
    second = _run_route_regen(tmp_path / "run-b")
    load_lines.append(_loadavg_line("after run-b"))
    print(load_lines[-1], flush=True)
    load_report = "\n".join(load_lines)

    counts_a = _copper_counts(first)
    counts_b = _copper_counts(second)
    assert counts_a == counts_b, (
        f"segment/via count mismatch between same-seed runs: "
        f"run-a={counts_a} run-b={counts_b} (issue #4536 regression)\n"
        f"{load_report}"
    )

    norm_a = _normalize_uuids(first)
    norm_b = _normalize_uuids(second)
    if norm_a != norm_b:
        # Produce a bounded, useful failure message instead of a megabyte diff.
        lines_a = norm_a.splitlines()
        lines_b = norm_b.splitlines()
        first_div = next(
            (i for i, (la, lb) in enumerate(zip(lines_a, lines_b, strict=False)) if la != lb),
            min(len(lines_a), len(lines_b)),
        )
        context_a = lines_a[first_div : first_div + 3]
        context_b = lines_b[first_div : first_div + 3]
        pytest.fail(
            "uuid-normalized artifacts differ between same-seed runs "
            f"(first divergence at line {first_div + 1}; "
            f"{len(lines_a)} vs {len(lines_b)} lines).\n"
            f"run-a: {context_a}\nrun-b: {context_b}\n"
            f"{load_report}\n"
            "See issue #4536 -- a wall-clock, hash-order, or random-UUID "
            "file-order dependence has re-entered the board-06 route pipeline. "
            "Check the load-average lines first: a saturated host is the "
            "known #4724 sensitivity, not a #4536 regression."
        )
