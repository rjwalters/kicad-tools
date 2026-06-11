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

Documented residuals (tracked by issue #3535):

* two corridor chords on boards 06/07 where BOTH dogleg variants
  (diag-first and axis-first) introduce a clearance violation against
  neighbouring copper -- the skewed chord is the only path that fits;
  pinned by uuid in ``DOCUMENTED_OFF_ANGLE``;
* board 07's ``DDR_DATA_BYTE_0`` length-tuning meanders (55 segments):
  the group is tuned to exactly-equal member lengths by SLOPED meander
  segments, and doglegging them changes lengths unevenly (DQ6
  +1.149 mm), tripping ``match_group_length_skew``.  Exempted per net
  in ``EXEMPT_TUNED_NETS`` until the tuning emitter generates
  45-degree trombones (issue #3535).

Any NEW off-angle segment outside these pinned sets still fails.
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
    "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb": {
        # (114.5, 110.0) -> (115.5, 108.0), 18.4 deg, net 7: threads the
        # diff-pair landing corridor; both dogleg bulges clip a via
        # (clearance_segment_via at PCB-local (14.1-15.7, 9.4-10.3)).
        "e9af299d-7f67-4eec-bda7-0577aee1e86b": "diffpair corridor chord",
    },
    "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb": {
        # (156.5, 155.0) -> (156.379, 155.5795), 11.8 deg, net 21
        # (TMDS_D0 pair area): both bulges violate intra-pair clearance
        # or clip the adjacent via at PCB-local (56.8, 55.0).
        "351d1137-d518-4b72-b0ac-cfda7f055b10": "TMDS_D0 pair corridor chord",
    },
}

#: Per-artifact nets whose off-angle segments are length-tuning meanders
#: (issue #3535): the ``DDR_DATA_BYTE_0`` match group is tuned to
#: exactly-equal member lengths by sloped meanders, so in-place
#: dogleg quantization breaks ``match_group_length_skew``.  Exempt until
#: the tuning emitter generates 45-degree trombones.
EXEMPT_TUNED_NETS: dict[str, frozenset[str]] = {
    "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb": frozenset(
        {"DQ0", "DQ1", "DQ2", "DQ3", "DQ4", "DQ5", "DQ6", "DQ7", "DM0"}
    ),
}


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
            str(p.relative_to(REPO_ROOT))
            for p in REPO_ROOT.glob("boards/**/*_routed.kicad_pcb")
        ]
    return sorted(
        REPO_ROOT / line for line in names if line.endswith("_routed.kicad_pcb")
    )


def _artifact_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


ARTIFACTS = _committed_routed_artifacts()


def test_fleet_has_routed_artifacts() -> None:
    """Sanity: the census below must actually cover the fleet."""
    assert len(ARTIFACTS) >= 8, (
        f"expected at least 8 committed routed artifacts, found "
        f"{[str(p) for p in ARTIFACTS]}"
    )


@pytest.mark.parametrize("artifact", ARTIFACTS, ids=_artifact_id)
def test_committed_artifact_is_45_aligned(artifact: Path) -> None:
    """No committed routed artifact may carry (new) off-angle segments."""
    total, bad = segment_angle_census(artifact)
    assert total > 0, f"{artifact}: census matched no segments (parser drift?)"
    allowed = DOCUMENTED_OFF_ANGLE.get(_artifact_id(artifact), {})
    tuned_names = EXEMPT_TUNED_NETS.get(_artifact_id(artifact), frozenset())
    tuned_ids = _net_ids_by_name(artifact, tuned_names) if tuned_names else set()
    unexpected = [
        b
        for b in bad
        if (b["uuid"] or "") not in allowed and b["net"] not in tuned_ids
    ]
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
