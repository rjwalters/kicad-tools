"""Fleet policy: every committed routed artifact is 100% 45-degree aligned.

Issue #3532: 204 arbitrary-angle segments (0.8-22.5 degrees off the
0/45/90/135 set) shipped on softstart's committed routed PCB, and every
fleet board carried the same class (board 07 was at 297/909).  The
emitters were fixed at source (pad-tail doglegs in both router backends,
optimizer terminal-restore/pull-tight guards, drc-nudge chain guards,
softstart step-10e quantization) and the committed artifacts were
quantized with ``kicad_tools.router.quantize.quantize_pcb_file``.

This test is the ratchet: any future PR that commits a routed artifact
with off-angle copper fails here.  45-only routing is a
manufacturability/quality convention -- acute-angle copper junctions can
etch poorly (acid traps).

Legitimate geometric exemptions (none currently): true arc primitives
and teardrop geometry would be exempt, but the fleet uses straight
``(segment ...)`` copper only -- arcs are a different s-expression
(``(arc ...)``) and are not counted by the census.

Documented residuals:

* a corridor chord on board 06 where BOTH dogleg variants (diag-first
  and axis-first) introduce a clearance violation against neighbouring
  copper -- the skewed chord is the only path that fits; pinned by uuid
  in ``DOCUMENTED_OFF_ANGLE``.  (Board 07 carried such a chord before
  issue #3617's filled re-route; it is gone now and the pour-repair
  emitter quantizes its own stubs/bridges, so board 07 needs no
  ``DOCUMENTED_OFF_ANGLE`` entry.)

Resolved by issue #3535 (no longer exempt):

* board 07's ``DDR_DATA_BYTE_0`` length-tuning meanders: the trombone
  emitter
  (:meth:`kicad_tools.router.optimizer.serpentine.SerpentineGenerator.generate_trombone`)
  now snaps the along-segment travel direction to the legal 8-direction
  set and quantizes its closing exit leg, so every emitted meander leg
  is 45-aligned by construction even when A* hands it an off-axis host
  segment; the pair-aware N-side mirror
  (``match_group_tuning._mirror_segments_about_centerline``) re-quantizes
  each reflected leg the same way.  The committed board 07 artifact
  carries 0 off-angle segments and ``match_group_length_skew`` stays
  clean, so ``EXEMPT_TUNED_NETS`` is now empty.

``EXEMPT_TUNED_NETS`` is kept (empty) with a stale-entry ratchet so any
future tuned-net exemption that the emitter fix makes unnecessary fails
here instead of silently masking off-angle copper.

Any NEW off-angle segment outside the pinned sets still fails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.quantize import segment_angle_census

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Per-artifact uuids of off-angle segments that CANNOT be doglegged in
#: place: both dogleg variants collide with adjacent copper (measured by
#: the issue-#3532 flip/skip search against the jlcpcb DRC baseline).
#: Exit clause: remove the entry when the owning net is re-routed (the
#: quantized emitters will not regenerate the skew).
DOCUMENTED_OFF_ANGLE: dict[str, dict[str, str]] = {
    # 2026-07-08 (fix/board06-gallery-ready): board 06's prior exemption
    # (864fb9ee-..., the USB2_D- diff-pair crossover chord at the J1
    # landing corridor) exercised its exit clause -- the gallery-ready
    # refresh re-routed the board from the recipe, and the one chord the
    # step-12 flip/skip resolver still had to skip (a GND pour-repair
    # bridge threading the USB3_RX2 via field) is now resolved by the
    # recipe's step-13 ``_split_offangle_chords`` mid-split pass (axis
    # leg + exact-45 diag + axis leg, clearance- AND hole-clearance-
    # validated).  The committed board-06 artifact carries 0 off-angle
    # segments, so no entry remains.
    #
    # Issue #3617: board 07's prior corridor-chord exemption
    # (351d1137-..., TMDS_D0) is gone -- the regenerated filled artifact
    # routes that area 45-aligned, and the pour-repair emitter now runs
    # through the #3532 quantizer, so no board-07 segment needs an
    # in-place dogleg exemption.  (Empty entries are omitted so the
    # ratchet check below cannot resurrect a stale uuid.)
}

#: Per-artifact nets whose off-angle segments were length-tuning
#: meanders.  Issue #3535 made the tuning emitter generate 45-aligned
#: trombones by construction, so this set is now EMPTY -- every committed
#: meander is on the {0,45,90,135} angle set.  The mapping is retained
#: (empty) so the stale-entry ratchet in
#: :func:`test_committed_artifact_is_45_aligned` keeps watch: if a future
#: change ever re-adds a tuned-net exemption that the artifact does not
#: actually need (no off-angle segment on that net), the ratchet fails.
EXEMPT_TUNED_NETS: dict[str, frozenset[str]] = {}


def _net_ids_by_name(pcb_path: Path, names: frozenset[str]) -> set[int]:
    """Resolve ``(net N "NAME")`` declarations for *names* in *pcb_path*."""
    import re

    text = pcb_path.read_text()
    return {
        int(m.group(1))
        for m in re.finditer(r'\(net (\d+) "([^"]+)"\)', text)
        if m.group(2) in names
    }


def _committed_routed_artifacts() -> list[Path]:
    """Every ``*_routed.kicad_pcb`` tracked by git under ``boards/``."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "boards"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        names = result.stdout.splitlines()
    except (subprocess.CalledProcessError, OSError):
        # The CI Test job runs inside the kicad/kicad:10.0 container
        # (PR #3525) where the checked-out workspace is owned by a
        # different uid, so git refuses with "detected dubious
        # ownership" (exit 128).  Fall back to a filesystem walk -- on
        # a clean CI checkout the on-disk tree IS the committed tree.
        # Local developer runs keep the git path so stray untracked
        # artifacts cannot widen (or accidentally gate) the census.
        names = [
            str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.glob("boards/**/*_routed.kicad_pcb")
        ]
    return sorted(REPO_ROOT / line for line in names if line.endswith("_routed.kicad_pcb"))


def _artifact_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


ARTIFACTS = _committed_routed_artifacts()


def test_fleet_has_routed_artifacts() -> None:
    """Sanity: the census below must actually cover the fleet."""
    assert len(ARTIFACTS) >= 8, (
        f"expected at least 8 committed routed artifacts, found {[str(p) for p in ARTIFACTS]}"
    )


@pytest.mark.parametrize("artifact", ARTIFACTS, ids=_artifact_id)
def test_committed_artifact_is_45_aligned(artifact: Path) -> None:
    """No committed routed artifact may carry (new) off-angle segments."""
    total, bad = segment_angle_census(artifact)
    assert total > 0, f"{artifact}: census matched no segments (parser drift?)"
    allowed = DOCUMENTED_OFF_ANGLE.get(_artifact_id(artifact), {})
    tuned_names = EXEMPT_TUNED_NETS.get(_artifact_id(artifact), frozenset())
    tuned_ids = _net_ids_by_name(artifact, tuned_names) if tuned_names else set()
    unexpected = [b for b in bad if (b["uuid"] or "") not in allowed and b["net"] not in tuned_ids]
    sample = [
        f"{b['start']} -> {b['end']} [{b['layer']} net {b['net']} "
        f"uuid {b['uuid']}] off by {b['off_deg']:.2f} deg"
        for b in unexpected[:10]
    ]
    assert not unexpected, (
        f"{artifact}: {len(unexpected)}/{total} segments off the "
        f"0/45/90/135 angle set (beyond the documented residuals).  A "
        f"post-route mutation pass is emitting arbitrary-angle copper "
        f"(issue #3532) -- fix the emitter, then repair the artifact "
        f"with kicad_tools.router.quantize.quantize_pcb_file. "
        f"First offenders: {sample}"
    )
    # Ratchet the documented residuals too: if a re-route removed one,
    # the allowlist entry must be deleted so it cannot silently return.
    present = {b["uuid"] for b in bad}
    stale = set(allowed) - present
    assert not stale, (
        f"{artifact}: documented off-angle exemption(s) {sorted(stale)} "
        f"no longer present -- remove them from DOCUMENTED_OFF_ANGLE to "
        f"lock in the improvement."
    )
    # Ratchet the tuned-net exemption the same way (issue #3535): a net
    # listed in EXEMPT_TUNED_NETS that carries NO off-angle segment is
    # stale -- the emitter fix made it 45-aligned, so the exemption must
    # be removed instead of silently masking a future regression.
    if tuned_names:
        off_angle_tuned_ids = {b["net"] for b in bad if b["net"] in tuned_ids}
        stale_tuned = sorted(tuned_ids - off_angle_tuned_ids)
        assert not stale_tuned, (
            f"{artifact}: tuned-net exemption(s) for net id(s) "
            f"{stale_tuned} carry no off-angle segment -- remove them "
            f"from EXEMPT_TUNED_NETS to lock in the improvement."
        )
