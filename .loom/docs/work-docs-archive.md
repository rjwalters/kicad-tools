# Work-Docs Archive: Rotating `WORK_LOG.md` (#12633, slice 1 of #12589)

`WORK_LOG.md` is an active document, kept short by moving older whole-day
sections into dated archives with nothing lost. `loom-daemon work-docs` is the
content-preserving move primitive. Wiring it into Guide is slice 2; `--plan`
(marker-driven WORK_PLAN archival, slice 3) is described under "WORK_PLAN".

## Layout

| Path | Holds |
|------|-------|
| `WORK_LOG.md` | preamble, `## Archives` index, recent `### YYYY-MM-DD` sections |
| `work_log/WORK_LOG_YYYY-MM.md` | one file per calendar month of archived sections, newest first |

Each archive starts with a `# Work Log Archive — YYYY-MM` preamble, then the
moved sections byte-for-byte in their original order. The index near the top
of `WORK_LOG.md` lists every archive newest first:

```markdown
## Archives

- 2026-08: work_log/WORK_LOG_2026-08.md
- 2026-07: work_log/WORK_LOG_2026-07.md
```

In the real file each entry is a markdown link (`[YYYY-MM]` text, target
`work_log/WORK_LOG_YYYY-MM.md`); the example is plain text because the install
link checker resolves link syntax even inside fences.

`WORK_PLAN.md` follows the same shape (`work_plan/WORK_PLAN_YYYY-MM.md` plus a
`## Archives` index); its rules are under "WORK_PLAN" below.

## Verbs

```bash
loom-daemon work-docs archive [--root <dir>] [--older-than-days N] [--dry-run]
loom-daemon work-docs check   [--root <dir>]
loom-daemon work-docs archive --plan [--root <dir>] [--dry-run]
loom-daemon work-docs check   --plan [--root <dir>]
```

`archive` moves every `### YYYY-MM-DD` section whose date is strictly older
than `today - N days` into its month's archive, regenerates the index, and
runs the conservation check before writing. Archives are written first (each
via temp file + rename), the active file last; a failed write restores the
archives it touched.

`check` verifies that every `- **PR #N**` and `- **Issue #N**` bullet appears
exactly once across the active file and all archives, and that every index
link resolves to a regular file.

## Eligibility floor

`--older-than-days` defaults to **60** and is rejected below **45**. Guide's
`update_work_log()` re-scans the last 30 days of merged PRs and closed issues
(`since=$(date -u -d '30 days ago' ...)`) and appends whatever its presence
checks (`work_log_has_pr` / `work_log_has_issue`) do not find in the active
file. A section rotated out of the active file while still inside that window
would be re-appended as a duplicate. A cutoff older than the lookback keeps
every candidate's entry in the active file. Slice 2 relies on this invariant.

## What is never moved

- the preamble above the first date heading;
- any heading that is not a parseable `### YYYY-MM-DD` (for example
  `### 2026-08-09 — Residual gap notice`) and its body;
- the `## Archives` index (regenerated in place, not rotated).

Several sections with the same date (the log has such days) move together,
keeping their relative order.

## Failure behaviour

`archive` exits non-zero and writes nothing when:

- an archive already holds a section with a date being moved (conflict);
- an archive path, or `work_log`, exists but is not a regular file or directory;
- conservation fails (bullet keys, bullet count or content line count differ
  from the pre-move state, or an entry would appear twice).

A log with no parseable date headings, or with nothing older than the cutoff,
exits 0 without writing. A second run is therefore a byte-identical no-op.
An existing archive holding only other dates is merged newest first.

## Recovery

Archives are plain Markdown. To undo a rotation, concatenate the archives'
sections back into `WORK_LOG.md` in date order (newest first) below the
preamble and drop the `## Archives` index.

## WORK_PLAN (`--plan`, #12635)

`WORK_PLAN.md` has no dated sections, so there is **no date logic**: an old
date alone never moves anything. A section moves only when it is explicitly
marked completed or superseded, by putting this marker on its own line directly
above its heading:

```markdown
<!-- loom:plan-archive -->
## Phase 1 (done)
```

- The section runs from the marker to the next heading of the same or higher
  level (or to the generated region, whichever comes first), and moves
  byte-for-byte, marker line included, into `work_plan/WORK_PLAN_YYYY-MM.md`
  for the month of the run. The `## Archives` index in `WORK_PLAN.md` is
  regenerated in place (newest first), or created where the first moved
  section was.
- **Never moved**: everything unmarked, whatever its age or whether it holds
  open `- [ ]` items; the `<!-- guide:plan-body:start -->` ..
  `<!-- guide:plan-body:end -->` generated region (Guide overwrites it, so it
  is not history); the `## Archives` index (marking it is an error).
- A marker inside the generated region or a fenced code block, or one not
  directly above a heading, is ignored (the last case with a warning).
- A marked section that still contains an open `- [ ]` item moves anyway,
  because a human marked it, with a warning in the report.
- Before writing, the move verifies the generated region is byte-identical and
  that every line outside the index is either still active or inside a moved
  section. An archive that already holds an identical section is a conflict
  (nothing written). With no marked sections the run is a byte-identical
  no-op; a rerun after a move is one too.
- `check --plan` verifies the generated-region markers are balanced, every
  index link resolves, and every archive consists only of marker-introduced
  sections (no unmarked section ever left the plan).

## Limitations

- A final section lacking a trailing newline gains one when something follows
  it in an archive (the only non-byte-identical case; line counts are unchanged).
- Pre-existing duplicate entries make `archive` refuse until they are resolved.
