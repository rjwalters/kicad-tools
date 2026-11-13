# kicad-tools Roadmap

**Mission**: enable AI agents to design PCBs programmatically, as the layer
between agent reasoning and KiCad files. We complement KiCad; we do not replace it.

Status as of 2026-11-13, `main` @ `3d78694e1` (v0.29.0 released 2026-11-09).
The generated queue is [WORK_PLAN.md](WORK_PLAN.md), merged work is in
[WORK_LOG.md](WORK_LOG.md), shipped behaviour in [CHANGELOG.md](CHANGELOG.md).
Issue states below were read from the tracker on 2026-11-13.

**What changed in this revision.** The previous text described v0.14.0
(2026-06-16) and an empty backlog. Its per-release checklists for v0.9 to v0.11
are dropped because CHANGELOG.md carries them; its unstarted "v0.12.0 typed
interfaces" plan moved to Later, since v0.12.0 shipped the manufacturing export
pipeline instead; "3D rendering" left the non-goals because `kct render`
shipped in v0.14.0.

## Design philosophy

Agent-first API with structured I/O; validation that returns actionable
findings; round-trip fidelity on every edit; kicad-cli as the referee that
`kct check` must agree with; circuit blocks rather than only primitives.

## Where it stands

The v0.14.0 workflow (parse, schematic, place, route, validate, export, MCP)
is unchanged in outline. Since then the releases from v0.15.0 to v0.29.0
moved the work from "can produce a board" to "can prove a board is buildable":

- **Routing.** Feasibility certificates, lattice and mesh engines (default
  off), region-bounded routing and `kct route --complete` shipped in v0.15 to
  v0.20 (release table in WORK_PLAN.md). The diff-pair redesign epic #6441 is
  closed except for one measurement (#7856).
- **HV isolation.** `kct creepage`, HV-aware placement and keepouts (v0.18,
  v0.19); pairwise clearance in the router (PR #4454; #4506, #4507, #4508
  closed). Epic #4431 stays open: the acceptance re-run recorded on it
  (2026-10-23) reached 80% of signal nets, not the 100% it requires.
- **One clearance kernel (#5509).** 13 of 19 consumer groups are gated against
  kicad-cli (`MIGRATED_GROUPS` in `tests/conformance/report.py`); groups 3, 11,
  12, 13, 14 and 19 are report-only. The audit is
  `docs/research/clearance-kernel-acceptance-audit.md`.
- **Harness neutrality (#5952).** Phases 1 and 2 and the AGENTS.md parity item
  are checked off: `kct mcp setup --client codex|opencode`,
  `kct skills install --harness ...`, `kct agent-guide`. Only the validation
  run (#7776) remains.
- **Manufacturing sign-off.** `kct readiness`, tapeout sign-off, waivers,
  `pcb reinforce`, and `kct submit-jlcpcb` exist under `src/kicad_tools/cli/`.
- **Fab DFM parity (#8838).** Opened 2026-11-12; five of seven children are
  closed (#8839, #8841, #8842, #8843, #8845): a versioned threshold table,
  default-on `mask_to_copper` under `--mfr jlcpcb`, the net-agnostic fab-view
  pass, mask-expansion rules, and the parity section in `kct check`.
- **Parts and boards.** `kct parts stock` for DigiKey and Mouser merged
  2026-11-13 (#8892). Demo boards are `boards/00` to `boards/09`; board 08's
  README says schematic, procurement and PCB are pending.

**Health.** The last completed CI run on `main` (2026-11-13, `cebe5919a`) is
red in three jobs: Test (extended), Board 03 End-to-End, and Board 05 Routing
Regression. The automated release has skipped v0.30.0 on a red gate since
2026-11-10 (#7560). `main` has moved on since that run; re-check before
acting. The tracker has no `loom:issue` (ready) items.

## Now (startable today, one PR each)

| What | Done when | Issue | Blocked by |
|---|---|---|---|
| Board 03 gates fail on default-on `mask_to_copper` (334 errors in the CI run above); boards 05, 06, 07 gates already skip the rule in `scripts/ci/` | Board 03 End-to-End job and `tests/test_board_03_regression.py` pass on `main`, with the interim skip tied to #8858 | none (seed 1) | nothing |
| Test (extended): 60 s timeouts in `tests/test_check_emit_dru.py`, `test_mfr_profile_consistency.py`, `test_fabrication_overrides.py`; board 02 length baseline reads 315.15 against a pinned 315.21 | Test (extended) passes on `main`; the baseline change is attributed to a commit before it is re-pinned | none (seed 2) | nothing |
| Fab DFM parity table is stale: `PARITY_TABLE` in `src/kicad_tools/validate/fab_dfm_parity.py` still says "no rule" for THT-to-SMD and both mask-expansion categories, and "opt-in" for `mask_to_copper`, after #8849, #8852 and #8853 merged | Each row names the shipped rule; a test fails when a registered rule's category still reads `not_covered` | none (seed 3) | nothing |
| Sharp trace corner, missing plated hole, fiducial parity | PR #8848 merged; rows updated in the parity table | #8840 | in review, `loom:operator` hold on the PR |
| Silk-to-pad bands, silk-to-hole, silk line width | PR #8854 merged | #8844 | `loom:operator` hold on the PR |
| BOM lines with no DigiKey/Mouser stock at build quantity | PR #8898 merged; `kct bom --check-distributor-stock` documented | #8893 | in review |

## Next

| What | Done when | Issue | Unblocked by |
|---|---|---|---|
| Close the fab DFM parity epic | Its four acceptance boxes are checked; no danger-capable row reads `not_covered` | #8838 | #8840, #8844, seed 3 |
| Annular-ring floor that reads copper weight (the one parity row with no child issue) | Row `annular_ring` is `covered` or `partial` with a rule named | none | threshold source in the #8839 table |
| `pcb reinforce`: restate what is left after the ends mode, courtyard and mask-strip fixes (#8780, #8797, #8821, #8886 closed) | #8741 body lists only open items, each checkable | #8741 | curation |
| Baseline or fix `mask_to_copper` on boards 05, 06, 07 and remove the CI skips | Skips gone from `scripts/ci/`; per-board counts recorded | #8858 | a host with a qualified kicad-cli plot profile |
| Board 07 evidence refresh chain | `redundant_copper` reads 0; routing hash re-pinned | #8603, then #8604, #8399, #8385 | maintainer ruling requested on #8603 |
| Clearance kernel: survivor list and a decision per report-only group | `docs/clearance-conformance.md` carries the list (audit rows C3, E1) | none; proposed in #5509's body | ruling on whether groups 12/13 must be kernel-driven |
| `optimize-traces`: collision-check each rewrite | Per-rewrite acceptance replaces whole-net rollback | #8214 | `loom:operator` |
| Changelog check re-runs on every label change | Workflow filters label events to `changelog:skip` | #8434 | `loom:operator` (workflow file) |

## Later

- **Class-pair clearance as a general router input (#4431).** A second,
  low-voltage use case was recorded on 2026-11-10. Question: does the router
  read class-pair rules from `.kicad_dru`, or keep the voltage map as the only
  source?
- **Dense mains-board routing (#8610, #8486).** Question: is the remaining gap
  search budget or placement? #8486's re-benchmark found no time saving.
- **Finish the clearance kernel (#5509).** Rows P2b, P2c, P4a and P3b each
  need a fixture or a ruling; start with a non-vacuous `kct check` versus
  kicad-cli clearance count (P4a).
- **Board 08 to a manufacturing release.** Question: which step is next,
  schematic or procurement? No issue exists.
- **Typed interfaces and constraint-based part selection** (the old v0.12.0
  plan). `src/kicad_tools/intent/interfaces/` exists; question: is type-checked
  connection at schematic time still wanted, and on which block API?
- **v1.0.0: API stability.** Question: which modules are public API? Nothing
  in the repo states it.
- **Package registry, LSP server, SPICE orchestration helpers.** Carried from
  the previous roadmap, unstarted. Each needs a user before a design.

## Blocked and waiting

- **Maintainer-run checks (`loom:operator`)**: live verifications that need a
  real distributor, fab site, pinned kicad-cli host or a Mac: #8896, #8640,
  #8629, #8620, #8585, #8573, #8563, #8478, #8425, #7727, #7440.
- **#8530** (fiducials and board info): children #8531 to #8533 are closed;
  waits on a close decision.
- **#7688** (embedded 3D models): waits on a decision to file a KiCad upstream
  request.
- **#7776** (non-Claude harness pilot): parked; no seats or spend authorized.
- **#8033, #7856**: long routing measurements, parked until a suitable
  full-route host is available.
- **#8609** tracks #8620. **#7487**: board 03 via-in-pad exception, extended
  to 2027-10-19 by PR #8025; re-rule or remove before then.
- **PRs #8623, #7453** (site dependency bumps): held; #8623 is approved and
  waits on the critical-file check for `site/package.json`.

## Not planned

A schematic capture GUI (use KiCad); a full SPICE engine (ngspice or LTspice;
only orchestration helpers are in scope); our own Gerber generator (kicad-cli
produces fabrication output); placing orders (`kct submit-jlcpcb` prepares a
submission and never orders); replacing KiCad.
