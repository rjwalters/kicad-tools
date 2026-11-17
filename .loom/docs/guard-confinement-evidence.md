# Guard-confinement evidence: outcome table (#13777)

Upstream home for the guard-confinement and sweep material kept in
`2AMLogic/gf180-opamp` (package listed in gf180-opamp#313; handoff #214/#216)
and `2AMLogic/gf180-rcosc` (`docs/guard-confinement-investigation*`, #149/#310).
Each item gets one outcome: **covered** (an existing Loom test/doc), **taken**
(added here), or **not needed** (with the reason). No guard behaviour is changed
by this page; a discrepancy is pinned by a test or recorded, never silently
"fixed" here.

## Evidence classes (kept distinct)

1. **Historical observation**: the auditor's two claimed
   `worktree-write-confinement` denials of 2026-10-11 (00:02:39Z, 01:58:49Z).
   Unverified and attributed; no command text was recovered. **Not reproduced**
   by any case below, and not evidence of current behaviour.
2. **Synthetic cases**: the 29 payloads (`provenance: "synthetic"`) of the
   opamp fixture, written from audit prose and documented rules.
3. **Reproduced findings**: measured on *this* repo at `fcdbcdc` (2026-11-11),
   by running the opamp runner and fixture unmodified against this tree's
   `defaults/hooks/` (`guard-destructive.sh` -> `guard-destructive-generic.sh`,
   `guard-loom-workflow.sh`, `guard-worktree-paths.sh`; no Repo Skills guard
   installed, so the vendored generic guard decides). Totals
   `{"DISCREPANCY":4,"MATCH":15,"RECORDED":10}`, case-for-case identical to the
   2026-10-11 run on opamp commit `47dd65c`. The old run is history; the numbers
   above are the current measurement. Re-run to refresh them: they describe a
   commit, not a contract.
4. **Limitations**: hook-only (settings wiring and `LOOM_WORKTREE_PATH`-set
   sessions not exercised); the default Claude `PreToolUse` chain only.

## Inventory (8 items)

| # | Item | Outcome | Upstream location |
|---|---|---|---|
| 1 | opamp `docs/guard-confinement-reproduction.md` | **taken** (as this page) + **not needed** for the 2026-10-11 run log | This page, "Evidence classes" and the case table below. The old per-run tables are superseded by the current-commit measurement. |
| 2 | opamp `docs/fixtures/guard-confinement-cases.json` (29 cases) | **covered / taken**, per case | Case table below: every id maps to an existing suite or `tests/hooks/test-guard-confinement-evidence.sh`. The JSON is not copied: two overlapping runners/fixtures would drift from the guard suites. |
| 3 | opamp `scripts/guard-confinement-repro.sh` | **not needed** | Superseded by the hermetic suites in `tests/hooks/` (isolated fixture repo, real linked worktrees, stdin-only payloads, ambient `LOOM_*` overrides scrubbed). The runner's classifier is reproduced by `tests/hooks/lib/guard-destructive-harness.sh` (`assert_allow` / `assert_deny`). |
| 4 | opamp `docs/guard-confinement-handoff.md` | **taken** | The owner mapping is in "Owner notes" below. Handoff umbrella `rjwalters/loom#11113` is closed; it is a structural-analysis umbrella and does not receive these cases. F1-F4 and C2/C4 are dispositioned here instead. |
| 5 | opamp `docs/sweep-baseline.md` | **covered** | The main-clean baseline helper is `defaults/scripts/check-main-clean.sh --snapshot/--baseline` with tests in `defaults/scripts/tests/test-check-main-clean.sh`. The recipe's rule (baseline path outside the repo, literal absolute path) is the A1-A3/D4 "allow" family below. |
| 6 | opamp `scripts/test-sweep-baseline.sh` | **covered** | `defaults/scripts/tests/test-check-main-clean.sh` (snapshot/baseline detection and exit codes) plus the main-checkout deny controls E4-E6 in `tests/hooks/test-guard-confinement-evidence.sh` and `tests/hooks/test-guard-destructive-write-confinement.sh`. |
| 7 | opamp README "Orchestration notes" (lines 142-152) | **not needed** | A pointer to items 5, 6 and 4 only; it has no content of its own. |
| 8 | gf180-rcosc `docs/guard-confinement-investigation.md` + `docs/guard-confinement-investigation/repro.sh` | **covered** + one **recorded** finding | See "gf180-rcosc findings" below. |

## Case table (all 29 ids; decisions measured on `fcdbcdc`)

"Pinned in" names the test that fails if the decision changes.
`write-confinement` = `tests/hooks/test-guard-destructive-write-confinement.sh`;
`evidence` = `tests/hooks/test-guard-confinement-evidence.sh` (added by #13777);
`worktree-paths` = `defaults/hooks/tests/test-guard-worktree-paths.sh`.

| Case | Shape | Current decision | Outcome | Pinned in |
|---|---|---|---|---|
| A1 | literal redirect to external scratch | allow (MATCH) | covered | `write-confinement`: "echo > target in /tmp allows" |
| A2 | quoted literal scratch body path, printf | allow (MATCH) | covered | `write-confinement` (#6444 quoted-literal-assignment and /tmp-target cases) |
| A3 | heredoc body to literal scratch path | allow (MATCH) | covered | `write-confinement` (#5232 tee/heredoc to /tmp cases) |
| B1 | same-command literal assignment, redirect via `$S` | allow (MATCH) | covered | `write-confinement` (#6940/#6444 same-command literal assignment) |
| B2 | quoted assignment and quoted use | allow (MATCH) | covered | `write-confinement` (#6444) |
| C1 | `D=$(mktemp -d); > $D/x` | allow (RECORDED) | covered | `write-confinement` (#6949 same-command mktemp), `test-guard-destructive-mktemp-canon.sh` |
| C2 | `D=$(mktemp -d <template>); cp ... $D/x` | **deny** `...-unresolved-var` (RECORDED) | taken | `evidence` (C2: pinned as deny; no contract presumed) |
| C3 | `D=$(mktemp -d); printf > "$D/x"` | allow (RECORDED) | covered | `write-confinement` (#6953/#6949) |
| C4 | `> $(mktemp -d)/x` inline | **deny** `...-unresolved-var` (RECORDED) | taken | `evidence` (C4); consistent with `write-confinement` (#6444/#6940 inline `$(mktemp)` targets deny) |
| D1 | `sweep-checkpoint.sh delete N` alone | allow (RECORDED) | not needed | The guard scans write idioms, not helper identity (see rcosc finding). Lex-level: no write idiom present. |
| D2 | `sweep-lease-renew.sh release N` alone | allow (RECORDED) | not needed | same |
| D3 | `sweep-run-registry.sh cleanup` alone | allow (RECORDED) | not needed | same |
| D4 | literal-scratch follow-up body, printf | allow (MATCH) | covered | as A2 |
| D5 | D1-D4 bundled with `&&` | allow (RECORDED) | not needed | The only write idiom is the D4 literal-scratch redirect, already allowed (A2). The 2026-10-11 denial of this bundle is **not reproduced** (evidence class 1). |
| D6 | D5 with cwd at main checkout | allow (RECORDED) | not needed | same; cwd does not matter for a literal scratch target |
| E1 | `> $V/fresh.json` | deny `-unresolved-var` (MATCH) | taken | `evidence` E1 |
| E2 | `S=scratch; S=main; > $S/x` | deny (MATCH) | taken | `evidence` E2 |
| E3 | `S=main; S=scratch; > $S/x` | deny `-unresolved-var` (RECORDED: fails closed) | taken | `evidence` E3 |
| E4 | literal redirect into main checkout | deny `worktree-write-confinement` (MATCH) | taken | `evidence` E4; `write-confinement` first block |
| E5 | `mkdir -p <main>/...` | deny (MATCH) | taken | `evidence` E5; `test-guard-destructive-mkdir-confinement.sh` |
| E6 | relative redirect, cwd = main checkout | deny (MATCH) | taken | `evidence` E6; `write-confinement` ("relative target + cwd at main root denies") |
| E7 | scratch write then unresolved write | deny `-unresolved-var` (MATCH) | taken | `evidence` E7 |
| E8 | scratch write `&&` main write | deny (MATCH) | taken | `evidence` E8 |
| E9 | mktemp handling then main write | deny (MATCH) | taken | `evidence` E9 |
| F1 | Bash redirect into another agent's worktree | **allow**, expected deny: DISCREPANCY | taken (pinned as known gap) | `evidence` F1 |
| F2 | Bash redirect via symlink to another agent's worktree | **allow**, expected deny: DISCREPANCY | taken (pinned as known gap) | `evidence` F2 |
| F3 | Write tool into another agent's worktree | **allow**, expected deny: DISCREPANCY | covered + pinned | `worktree-paths` ("cross-issue worktree write is allowed (not the guarded failure mode)") and `evidence` F3 |
| F4 | Write tool via symlink to another agent's worktree | **allow**, expected deny: DISCREPANCY | taken (pinned as known gap) | `evidence` F4 |
| F5 | Write tool inside own worktree (control) | allow (MATCH) | covered | `evidence` F5; `worktree-paths` |

### The four DISCREPANCY rows (F1-F4)

All four are a deliberate design limit, not a regression: the guards only
prevent writes from landing in the **main** checkout. A path under any
`.loom/worktrees/issue-N` with a `.loom-managed` sentinel counts as "some managed
worktree", and no ambient signal tells the hook which issue the acting session
owns (#3719; trust boundary note in `defaults/hooks/guard-worktree-paths.sh`,
"TRUST BOUNDARY"). The `guard-worktree-paths.sh` suite already states this
("cross-issue worktree write is allowed ... by design"). The F-family tests are
**characterization pins** with "known gap" in their names: if a future guard
change starts denying cross-worktree writes, they fail and force a decision to
update them. Changing the behaviour is a separate issue (it needs a session-owner
signal); nothing here changes a guard.

## Owner notes (from the opamp handoff, re-checked)

- Bash write-confinement decisions live in `defaults/hooks/guard-destructive-generic.sh`
  in this repo (consumers receive it by resync). A canonical Repo Skills copy
  (`.claude/skills/repo/hooks/guard-destructive.sh`) takes over only if it passes
  the dispatcher probes; this tree has none, so every measurement above is of the
  vendored guard. Cases A-E and F1-F2 are therefore pinned here, and no separate
  `rjwalters/repo` filing is required for them to have a named location.
- Edit/Write decisions (F3-F5) are `guard-worktree-paths.sh`, Loom-owned.
- Scan-tier safety of the masking copies the write-confinement reads is governed
  by `defaults/docs/guard-scan-contracts.md` and ADR-0016
  (`docs/adr/0016-write-target-confinement-approach.md`).

## gf180-rcosc findings (#149/#310)

Files: `docs/guard-confinement-investigation.md` (157 lines) and
`docs/guard-confinement-investigation/repro.sh` (98 lines). The harness was run
unmodified against this tree on `fcdbcdc` and every verdict matches the report.

| Finding | Current decision here | Outcome | Location |
|---|---|---|---|
| `mkdir [-p] .loom/sweep-checkpoint` from the main checkout | deny `worktree-write-confinement`; allow from inside a worktree | covered (guard working as designed; same shape as E5) | `evidence` E5; `test-guard-destructive-mkdir-confinement.sh` |
| `check-main-clean.sh --snapshot <literal / `$VAR` path>` | allow, from main and from a worktree | not needed (not a write idiom the guard lexes) | this table |
| Redirect/mkdir controls into main (arbitrary write, checkpoint dir, other dir, extra `;` redirect, process substitution) | deny | covered | `write-confinement`, `evidence` E4/E5/E8 |
| Helper-mediated writes via `--snapshot` with `..`, symlink, unknown `$VAR`, `$(pwd)`, or another helper name | **allow** | **recorded** (not a guard defect to fix here): the guard confines the write idioms it can lex, not writes performed by a helper; helper identity is not a safety property and a "trusted helper" allowlist would add none | this page. Any change belongs to a new issue about `check-main-clean.sh` constraining its own `--snapshot` path, not to the guard. |
| Synthetic download loops (`/tmp/e85/$f.rst`, literal list, with/without `cd`, `curl -o`, `curl >`) | allow | covered | `write-confinement` #13608/#13609 (`loop-literal-tmp-cwd`, `fn-unknown-arg`, ...) |
| Download controls (unknown var, `../x`, `sub/x`, absolute main path, `$(ls)`, `$LIST`, `*`, same-command `ln -s`, `read` rebind, `/tmp/../home`) | deny `-unresolved-var` / main checkout | covered | `write-confinement` (#13608/#13609 six downstream cases, #6444/#6940 unresolved-variable cases) |
| Original download-loop denial (`/tmp/e85/$f.rst`) | not reproduced | **historical, unrecovered**; do not reopen without the original command | this page (evidence class 1) |
| Caller-side suggestion: `sweep-wave-lifecycle.md` step 0 should not `mkdir .loom/sweep-checkpoint` from the main checkout | n/a | recorded as an upstream observation; no change made here | this page |

## How the numbers were produced

The opamp fixture and runner were executed unmodified from a scratch directory
(outside this repository) with `LOOM_FORCE_SCOPE` and `LOOM_GUARD_DECISION_LOG`
unset, against copies of this tree's `defaults/hooks/` and `defaults/scripts/`.
They are not vendored here. The pins in
`tests/hooks/test-guard-confinement-evidence.sh` are the repeatable check.
