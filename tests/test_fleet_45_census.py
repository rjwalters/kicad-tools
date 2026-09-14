"""Fleet policy: active routed artifacts are 100% 45-degree aligned.

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

Discovery (issue #5084): the original #3532 discovery rule matched only
``*_routed.kicad_pcb`` filenames.  Board 09's generator overwrites its
single committed output in place across the placement -> routed
lifecycle instead of keeping a separate ``_routed`` sibling next to an
unsuffixed pre-route snapshot (as boards 00-07 do), so its canonical
routed output -- ``boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb``
-- was silently excluded from the census by filename accident, not
policy.  ``_CANONICAL_ROUTED_OUTPUTS_WITHOUT_ROUTED_SUFFIX`` below is the
explicit, board-by-board metadata that closes that gap: entries are
added deliberately, never inferred from directory location alone.
Investigation/diagnostic PCB snapshots (board09's
``engineering/routing-investigation/`` and
``engineering/connectivity-investigation/`` fixtures) are excluded from
discovery outright by ``_is_investigation_fixture`` -- they are never a
board's release candidate, regardless of filename.

Board 09 is a *blocked development checkpoint*, not a historical fixture:
its README documents clean native ERC/DRC and full routing, but
explicitly blocks manufacturing release (51 ampacity errors, 22
unselected MPNs, no manufacturing ZIP).  Now that it is discovered, its
318-segment output carries 61 off-angle segments -- pending a source
emitter repair, it is an EXPLICIT, hash-pinned exemption from the strict
``ARTIFACTS`` enforcement below (``BLOCKED_DEVELOPMENT_CHECKPOINT``),
distinct from the ``HISTORICAL_WITNESS`` mechanism (which pins a
*historical*, intentionally-defective regression fixture).  Both
mechanisms are "discovered but explicitly carved out", never a silent
skip; see ``test_blocked_development_checkpoint_is_tracked`` for the
ratchet that keeps this exemption honest.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable
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


#: Directories that hold investigation/diagnostic PCB snapshots rather
#: than any board's canonical output -- e.g. board09's
#: ``engineering/routing-investigation/`` (issue #5072 benchmark/
#: experiment fixtures) and ``engineering/connectivity-investigation/``
#: (issue #5061 repro), plus board07's ``diagnostic-runs/`` notes.  A
#: ``.kicad_pcb`` under any of these path segments is never a discovery
#: candidate, no matter what its filename is -- an explicit, visible
#: exclusion (issue #5084 AC2), not an incidental filename miss.
_INVESTIGATION_DIR_SEGMENTS = frozenset({"engineering", "diagnostic-runs"})

#: Canonical active routed outputs whose filename does NOT end in
#: ``_routed.kicad_pcb`` -- because their generator overwrites the single
#: committed artifact in place across the placement -> routed lifecycle
#: instead of keeping a separate ``_routed`` sibling next to an
#: unsuffixed pre-route snapshot (boards 00-07 keep both; board09 does
#: not).  Each entry here is deliberate, board-by-board classification
#: metadata -- never inferred from directory location -- so this is the
#: explicit "artifact metadata/classification" signal the discovery
#: mechanism uses for non-``_routed``-suffixed canonical outputs (issue
#: #5084 AC1).  Add a board here only once its single ``output/*.kicad_pcb``
#: is confirmed to be its committed **routed** (not pre-route placement)
#: state.
_CANONICAL_ROUTED_OUTPUTS_WITHOUT_ROUTED_SUFFIX = frozenset(
    {
        "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb",
    }
)


def _is_investigation_fixture(rel_posix: str) -> bool:
    """True if *rel_posix* lives under an investigation/diagnostic dir."""
    return bool(set(Path(rel_posix).parts) & _INVESTIGATION_DIR_SEGMENTS)


def _match_routed_artifact_names(names: Iterable[str]) -> list[str]:
    """Pure matching logic: canonical active routed output names from *names*.

    Split out from :func:`_committed_routed_artifacts` so the discovery
    rule itself is directly unit-testable (issue #5084) without a real
    git checkout.  A name is a match when it is either the original
    ``_routed.kicad_pcb`` filename convention OR explicitly listed in
    ``_CANONICAL_ROUTED_OUTPUTS_WITHOUT_ROUTED_SUFFIX`` -- and, either
    way, is NOT under an investigation/diagnostic directory.
    """
    matched = {
        name
        for name in names
        if (
            name.endswith("_routed.kicad_pcb")
            or name in _CANONICAL_ROUTED_OUTPUTS_WITHOUT_ROUTED_SUFFIX
        )
        and not _is_investigation_fixture(name)
    }
    return sorted(matched)


def _committed_routed_artifacts() -> list[Path]:
    """Canonical active routed outputs tracked by git under ``boards/``.

    See :func:`_match_routed_artifact_names` for the discovery rule.
    """
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
        # artifacts cannot widen (or accidentally gate) the census.  The
        # explicit non-suffixed allowlist is unioned in by hand since it
        # cannot be recovered from a glob.
        names = [
            str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.glob("boards/**/*_routed.kicad_pcb")
        ] + [
            name
            for name in _CANONICAL_ROUTED_OUTPUTS_WITHOUT_ROUTED_SUFFIX
            if (REPO_ROOT / name).exists()
        ]
    return [REPO_ROOT / name for name in _match_routed_artifact_names(names)]


def _artifact_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# The redesign archives this synthetic match-group defect witness explicitly
# (see its README). Rewriting it to satisfy a release policy would erase the
# geometry used by regression tests. Pin its bytes instead; no active output
# or future archive path is excluded from the angle census. Issue #5044.
HISTORICAL_WITNESS = (
    REPO_ROOT / "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"
)
HISTORICAL_WITNESS_SHA256 = "ab3a2c2d4aea466f828e540189ac851ddb8c41505c8185be65924ec5ff92a9a6"

# Board09's routed development checkpoint (issue #5084): discovered by
# _committed_routed_artifacts() now that discovery no longer relies solely
# on the `_routed` filename suffix, but explicitly exempted from the
# strict 45-degree enforcement below pending a source-emitter repair of
# its 61 off-angle segments.  Unlike HISTORICAL_WITNESS this is NOT a
# frozen historical fixture -- it is board09's live, actively-developed
# routed output, currently blocked from manufacturing release for
# unrelated reasons (ampacity, MPN selection, no manufacturing ZIP; see
# boards/09-usbc-pd-power/README.md).  Pinned by hash + exact census
# counts so any change to the file (repair or regression) is caught by
# test_blocked_development_checkpoint_is_tracked below instead of being
# silently masked -- once the off-angle count reaches 0, delete this
# exemption and let board09 into the strict ARTIFACTS census.
BLOCKED_DEVELOPMENT_CHECKPOINT = (
    REPO_ROOT / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"
)
BLOCKED_DEVELOPMENT_CHECKPOINT_SHA256 = (
    "39bf81159243f6827792cc0ce1fa23d907a419595d200edf478f25819905bdc7"
)

ARTIFACTS = [
    p
    for p in _committed_routed_artifacts()
    if p not in (HISTORICAL_WITNESS, BLOCKED_DEVELOPMENT_CHECKPOINT)
]


def test_historical_matchgroup_witness_is_unchanged() -> None:
    assert hashlib.sha256(HISTORICAL_WITNESS.read_bytes()).hexdigest() == HISTORICAL_WITNESS_SHA256
    total, bad = segment_angle_census(HISTORICAL_WITNESS)
    assert (total, len(bad)) == (841, 12)


def test_blocked_development_checkpoint_is_tracked() -> None:
    """Board09's routed output is discovered, but deliberately exempted.

    This documents the issue #5084 policy decision explicitly: the file
    IS now found by discovery (closing the filename-suffix gap) but is
    NOT yet held to the strict fleet angle census, because board09 is a
    blocked development checkpoint whose 61 off-angle segments need a
    source-emitter repair, not a quantize-to-pass shortcut.  If this
    assertion's counts ever change, board09's routing changed -- update
    the pin, and if bad == 0, remove the exemption entirely.
    """
    assert BLOCKED_DEVELOPMENT_CHECKPOINT in _committed_routed_artifacts(), (
        "board09's routed output must be discovered (issue #5084) even "
        "though it is exempted from strict enforcement below"
    )
    assert (
        hashlib.sha256(BLOCKED_DEVELOPMENT_CHECKPOINT.read_bytes()).hexdigest()
        == BLOCKED_DEVELOPMENT_CHECKPOINT_SHA256
    )
    total, bad = segment_angle_census(BLOCKED_DEVELOPMENT_CHECKPOINT)
    assert (total, len(bad)) == (318, 61)


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


# --- Discovery-logic unit tests (issue #5084) -------------------------
#
# _match_routed_artifact_names() is the pure matching rule underneath
# _committed_routed_artifacts(); these cases exercise it directly with
# synthetic names so the discovery mechanism itself is tested in
# isolation from git/filesystem state.


def test_discovery_matches_ordinary_routed_suffix() -> None:
    """The original #3532 rule: a `_routed.kicad_pcb` sibling is found."""
    names = [
        "boards/00-simple-led/output/simple_led.kicad_pcb",
        "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
    ]
    assert _match_routed_artifact_names(names) == [
        "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
    ]


def test_discovery_matches_canonical_output_without_routed_suffix() -> None:
    """Board09's non-`_routed`-suffixed canonical output is found (issue #5084)."""
    names = ["boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"]
    assert _match_routed_artifact_names(names) == names


def test_discovery_excludes_investigation_fixtures_regardless_of_filename() -> None:
    """Investigation/diagnostic dirs are excluded even if named like a match."""
    names = [
        "boards/09-usbc-pd-power/engineering/routing-investigation/first.kicad_pcb",
        "boards/09-usbc-pd-power/engineering/routing-investigation/first_routed.kicad_pcb",
        "boards/09-usbc-pd-power/engineering/connectivity-investigation/before-split.kicad_pcb",
        "boards/07-matchgroup-test/diagnostic-runs/scratch_routed.kicad_pcb",
    ]
    assert _match_routed_artifact_names(names) == []


def test_discovery_does_not_widen_to_every_non_suffixed_output() -> None:
    """A non-`_routed` file NOT in the explicit allowlist stays excluded.

    Discovery is deliberate metadata, not "any .kicad_pcb under output/"
    -- this guards against re-introducing a silent, incidental widening
    of the census in the other direction.
    """
    names = ["boards/99-hypothetical-board/output/hypothetical_board.kicad_pcb"]
    assert _match_routed_artifact_names(names) == []


def test_discovery_is_a_pure_union_of_both_signals() -> None:
    """Both signals compose: suffix match + explicit metadata, sorted."""
    names = [
        "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
        "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb",
        "boards/09-usbc-pd-power/engineering/routing-investigation/first.kicad_pcb",
    ]
    assert _match_routed_artifact_names(names) == sorted(
        [
            "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
            "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb",
        ]
    )
