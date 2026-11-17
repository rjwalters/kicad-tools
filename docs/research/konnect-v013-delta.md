# Konnect v0.8 to v0.13: what changed since #4880

**Issue**: [#6911](https://github.com/rjwalters/kicad-tools/issues/6911)
**Checked against**: `origin/main` at `efa31fbf3` (2026-10-17)
**License**: Konnect is AGPL-3.0, so this note is **ideas only**. Nothing was
copied or read from its source; the sources are its public
[release notes](https://github.com/mixelpixx/Konnect/releases) and
[ROADMAP.md](https://github.com/mixelpixx/Konnect/blob/main/ROADMAP.md),
paraphrased. Prior work: #4880 and the three notes listed by
`kct ecosystem show konnect`.

Konnect is now at v0.13.0 (2026-10-02), advertising 228 tools in 21 on-demand
toolsets. Evidence in the symbol citations below comes from `git grep` and
`git show` on `origin/main`; leads that were only skimmed are marked.

## Verdicts

| # | Topic | Verdict | Follow-up |
|---|-------|---------|-----------|
| 1 | Placement scoring and automation | already-have | none |
| 2 | Visual baseline loop | **adopt** | #6912 |
| 3 | Specctra/Freerouting round-trip | decline (one **adopt** idea) | #6915 |
| 4 | Truth contract | **adopt** (audit) | #6913 |
| 5 | Honest schemas enforced by CI | **adopt** | #6914 |
| 6 | Bounded mutation recovery | already-have | none |
| 7 | Canonical design-state hashing | already-have | none |
| 8 | Release artifacts tested before the tag | decline | none |
| 9 | Everything else | skim, below | none |

## 1. Placement scoring and automation: already-have

Konnect scores a board with named deductions and adds decoupling-cap pairing,
BGA fanout planning, connectivity clustering and a deterministic force-directed
refinement, all dry-run by default, with planners that refuse to move locked
parts. We have the equivalents: `src/kicad_tools/placement/cost.py` produces a
`CostBreakdown` (named terms, combined by `_weighted_sum_score` /
`_lexicographic_score`), `src/kicad_tools/placement/decoupling.py` scores
decoupling placement, and `src/kicad_tools/placement/route_check.py` asserts
locked parts never move (`locked_indices`). `kct optimize-placement`
(`cli/optimize_placement_cmd.py`) is the front end. We already studied whether a
pre-route score should gate acceptance and decided against it in
`docs/research/placement_score_calibration.md`, using a bounded real route
instead. BGA fanout and schematic-driven clustering were not traced in depth;
`mcp/tools/routing.py` and `cli/sch_add_bypass_cap.py` are the starting
points if someone wants a gap check. No new issue: nothing here is a clear gap.

## 2. Visual feedback loop: adopt (#6912)

Konnect renders a schematic to an image, stores it as a baseline, and later
reports drift, flagging baselines made by a different renderer. We render
(`cli/render_cmd.py`, `cli/screenshot_cmd.py`, `mcp/tools/screenshot.py`,
`report/figures.py`) but a search for baseline or compare verbs found nothing.
An agent that edits a schematic cannot ask "did my edit change the picture?".
This is cheap and fits our machine-readable-JSON stance. The renderer-identity
check is the part worth copying as an idea, since it prevents false drift.

## 3. Specctra / Freerouting round-trip: decline, with one idea (#6915)

`kct ecosystem where-we-sit` records "No Specctra DSN/SES round-trip" as a
deliberate non-goal, yet `export/dsn.py` and `export/ses.py` exist as an
escape hatch for external routers. We keep freerouting as `complementary`. We
decline Konnect's process-ownership and single-transaction machinery. The one
idea worth taking is provenance: tie the DSN to the board revision it came
from and refuse an SES for a different one. Today a stale session could merge
onto an edited board. Tracked as #6915.

## 4. Truth contract: adopt as an audit (#6913)

Konnect's rule is that response fields come from observation, never from the
request; a verdict with no evidence is INCOMPLETE; an unrunnable safety check
is BLOCKED; and live, saved-file and CLI evidence are not blurred. That matches
our two-engine DRC honesty (`kct check --mfr` plus `kicad-cli pcb drc
--refill-zones`). We have part of the machinery (`validate/evidence.py` binds
findings to evidence), but we have not audited `kct mcp` and `kct check`
responses against the contract. That audit is the follow-up. We did not
verify any specific violation, so no claim is made that one exists.

## 5. Honest schemas: adopt (#6914)

The MCP server serves each tool's `parameters` as `inputSchema` and validates
arguments with `jsonschema` (`mcp/server.py`). That checks the input shape but
not that every advertised parameter is actually used, which is the failure
Konnect's CI guard targets. A test walking the registry (`mcp/tools/registry.py`)
would catch dead parameters. Whether unknown arguments are rejected was not
confirmed; the follow-up decides it.

## 6. Bounded mutation recovery: already-have

Our interactive placement sessions have explicit transaction semantics:
`start_session`, `commit_session` and `rollback_session` in
`mcp/tools/session.py`. File-based edits are atomic by construction, since we
write the board file rather than driving a live editor, so the retry-after-
uncertain-IPC problem does not arise. Konnect's "refusal writes nothing" tests
are a good habit but not a gap we can point to.

## 7. Canonical design-state hashing: already-have

`validate/evidence.py` defines `evidence_hash` and stable finding keys used
for evidence-bound waivers, and several analyses record `sha256` of the inputs
they read (`analysis/resistor_rating.py`, `analysis/drive_budget.py`,
`cli/board_metrics_cmd.py`). We did not find a single "canonical hash of the
whole design with LF normalisation", but nothing we do currently needs one, so
this is not worth a ticket yet.

## 8. Release artifacts tested before the tag: decline

Konnect has a non-publishing dry run that builds all binaries and runs real
acceptance before tagging. Our flow in `RELEASING.md` is PR-based: the version
bump merges first, CI gates run on it, then tagging, publish and a PyPI check
that both wheel and sdist exist. It already has a dry-run mode
(`AUTO_RELEASE=dry-run`). We could not confirm that a built wheel is
smoke-tested pre-tag, so this verdict is soft; revisit if a bad release
happens.

## 9. Skim of other v0.8 to v0.13 changes

- Atomic batch placement (`set_component_placements`): already-have in spirit via session commit/rollback.
- Junction and no-connect survival across moves and rotations: not checked; schematic editing is outside this note.
- Guidance drift reported without overwriting user files: decline; we vendor skills and do not rewrite local edits.
- On-demand toolsets to keep the tool list small: already covered by #4896 (tool economy audit).
- Predefined sizes, stackup readback, pad zone-connect, 3D-model editing: decline; live-editor conveniences, not our file-based surface.
- Per-release real-KiCad acceptance: already-have via the `kicad-cli` round-trip smoke jobs in CI.
