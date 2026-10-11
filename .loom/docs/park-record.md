# Park Record Format (`loom:blocked`, Issue #8925)

`loom:blocked` says "waiting on a dependency, but still automatable once that
clears" (`defaults/docs/label-state-machine.md`). Nothing said *which*
dependency in a form a machine can read. A role that applies the label
routinely writes its reasoning as prose, in a comment:

- PR #8314 was parked on #8322 by a Doctor stand-down whose only record was
  *"filed #8322 to track it, standing down"*. Correct reasoning, 155h parked,
  invisible to every automated lane — `guide.md`'s unblock sweep and
  `loom-daemon check-stale-blocked` both read an artifact's **body**, never
  its comment trail.
- Issue #8852 was parked on #8860. #8860 closed; #8852 stayed parked, because
  the only place the dependency was ever stated was a Curator comment.

A **park record** is the fix: one line, in the artifact's **body**, naming
one blocker. `loom-daemon park-record render`/`parse` (below) is the
canonical writer/reader; `loom_daemon::park_record` is the Rust module both
sit on top of.

## The grammar

```
<!-- loom:park Blocked by: #8322 by=doctor at=2026-09-19T12:09:00Z reason="needs an architecture ruling" -->
```

- `<!-- loom:park ` … ` -->` — an HTML comment, so it renders invisibly in the
  rendered issue/PR, and a **sentinel** that distinguishes a declaration from
  prose that merely *mentions* a blocker elsewhere in the body. Same pattern,
  same reason, as the lease record (`<!-- loom:lease host=… sweep=… -->`,
  `lease-record.md`) and the verdict-SHA / `loom:ac-verified` markers.
- `Blocked by: #N` — **deliberately the existing vocabulary**, not a new one.
  `guide.md`'s `parse_dependencies`, `dep_recheck::extract`'s
  `DEPENDENCY_PHRASES`, `warn-operator-gated.sh` and
  `detect-dependency-cycle.sh` already match `Blocked by`. A park record is
  therefore readable by every parser already in the fleet the moment it is
  written, and this format adds no second dependency vocabulary.
- `by=<role>` / `at=<RFC3339>` — provenance, so a park can be attributed and
  aged without reading the comment trail.
- `reason="…"` — optional free text, quoted so it can contain spaces. A
  `"` inside the reason is downgraded to `'`, and a `--` run is collapsed to a
  single `-` — an HTML comment cannot contain `--`, so an un-sanitised reason
  quoting a CLI flag (`--force`) would terminate the marker early and leak the
  rest of the reason into the visible body.

**One record per blocker**, not a comma-separated list — this is forced by the
two parsers this format promises to stay compatible with, which do not agree
on how to split a multi-reference line: `guide.md`'s two-stage
`parse_dependencies` returns every `#N` on the line, but
`dep_recheck::extract`'s single-capture-group regex returns only the first. A
comma-separated record would silently declare only its first blocker to the
second parser — a park that reads as cleared while a real blocker is still
open. `loom-daemon park-record render --blocked-by` therefore emits one line
per blocker when given more than one:

```
<!-- loom:park Blocked by: #8322 by=doctor at=2026-09-19T12:09:00Z -->
<!-- loom:park Blocked by: #8323 by=doctor at=2026-09-19T12:09:00Z -->
```

A hand-written record naming more than one `#N` on a single line is still
tolerated by the reader (`park_record::parse` expands it into one record per
reference) — the one-line-per-blocker rule binds the *writer*, not the parser.

## Who writes one, and when — `park-record apply` (#10152)

**Cross-repo blockers (#10443).** A blocker in another repository is written
`OWNER/REPO#N` (`Blocked by: example-org/tool-repo#202`). `--blocked-by` accepts `N`,
`#N` (this repo) and `OWNER/REPO#N`, mixed and comma-separated; still one record
per blocker. A qualified reference is **never** resolved against the local repo:
`check-stale-blocked` reads its state in its own repo, `apply` refuses a closed
one by looking it up there, and it is a self-block only when it names this very
repo and number. `#9` and `o/r#9` are two distinct blockers. Records written
with a bare number parse exactly as before (`repo` is `None`).

**Every** role applying `loom:blocked` writes one, through one command — never
a bare label edit:

```bash
loom-daemon park-record apply --issue 8852 --blocked-by 8860 --by curator
loom-daemon park-record apply --pr 8314 --blocked-by 8322 --by doctor \
  --reason "needs an architecture ruling" --remove-label loom:treating
loom-daemon park-record apply --issue 8440 --reason operator --by human   # no blocker
```

`apply` reads the artifact fresh, then:

1. **Refuses** (exit 1, nothing changed) when there is neither `--blocked-by`
   nor an explicit `--reason`. A park with no named blocker reads as
   `blocked-unnamed` to star-liveness and UNDOCUMENTED to
   `check-stale-blocked`, and is never released automatically — so it must be
   a written choice (`--reason operator`, a quarantine/scope ruling …), which
   renders an attributable `Blocked by: (unstated)` record, never an omission.
2. **Refuses** a blocker that is already closed (#9102) — the unblock sweep
   would release the park at once — and a self-block.
3. Appends one record per blocker the body does **not** already declare in a
   park record (idempotent: re-running changes nothing).
4. Writes the **body first**, then adds `loom:blocked`, then removes each
   `--remove-label` (e.g. `loom:building`). A failed body write applies no
   label, so there is never a label-only park. Exit 4 on any forge failure.

`--dry-run` prints the planned body and label changes. Comment as usual to
explain *why* in prose — the marker is the part a machine can also read.

`park-record render` (below) remains for composing a body by hand; a park
record has no positional requirement in the body.

```bash
loom-daemon park-record render --blocked-by 8322 --by doctor \
  --reason "needs an architecture ruling"
# <!-- loom:park Blocked by: #8322 by=doctor at=2026-09-19T18:04:11Z reason="needs an architecture ruling" -->
```

### The daemon's own holds (#10161)

Two `loom:blocked` writers live inside `loom-daemon`, not in a role prompt: the
insta-crash quarantine (#3939) and the PR-less retry hold (#7972/#9239). Each
writes a reason-only record **before** its label edit:

```text
<!-- loom:park Blocked by: (unstated) by=daemon at=2026-10-06T12:00:00Z reason="insta-crash quarantine" -->
<!-- loom:park Blocked by: (unstated) by=daemon at=2026-10-06T12:00:00Z reason="pr-less hold" -->
```

`loom_daemon::sweep_registry::park_hold` owns the format: `DAEMON_HOLD_BY`,
`QUARANTINE_HOLD_REASON`, `PRLESS_HOLD_REASON`, and `is_daemon_hold(&record)`.
A reader that needs to tell a deliberate daemon hold from an undocumented park
should key on `is_daemon_hold`, not on comment prose.

These writers differ from `apply` in two documented ways:

- **A refused body write still applies the label.** For the PR-less hold, the
  label is the deliverable (#9239), and an unparked re-claim loop costs more
  than a park without a name. The fallback is logged at `warn`. A body read or
  write that **times out** stops the writer before its label edit, so a wedged
  `gh` costs one timeout on the `reap_once` read path (#3973).
- **Re-applying a hold replaces that hold's earlier record**, so `at=` dates
  the current park. Releases (`quarantine clear`, the TTL, reconciliation, a
  hand flip back to `loom:issue`) change only labels and leave the record in
  the body. Readers key on `loom:blocked` first, so a leftover record on an
  unblocked issue declares nothing.

## Who reads one

- `loom-daemon check-stale-blocked` (`defaults/scripts/check-stale-blocked.sh`,
  #8927/#8925) — the pre-wave advisory, **per-repository**: one invocation
  examines the one repository `--repo` / `--repo-root` resolves (#9191). Reads `park_record::blockers` from an
  artifact's body to populate `Evidence::declared`; an artifact whose only
  blocker reference is NOT inside a park record is reported as **PROSE-ONLY**
  (`stale_blocked::undeclared`), separate from **UNDOCUMENTED** (no blocker
  reference anywhere). Its forge reads are batched (#10480: one REST + ETag
  listing, REST blocker reads, one GraphQL query per 100 issues) and run under
  a **budget floor**: after the listing it reads the free `/rate_limit` probe,
  and if the run's projected cost would leave fewer than
  `--min-graphql-remaining` GraphQL points or `--min-core-remaining` core
  requests (default 1000 each; `0` disables) it gathers nothing and reports
  every artifact **NOT EVALUATED**, still exit 0. The same floors are
  re-checked between reads from the forge's own rate-limit answers, so a run
  stops part-way rather than draining the bucket. `--json` adds a
  `forge_cost` object (`graphql_queries`, `graphql_points` from
  `rateLimit.cost`, `rest_requests`, `rest_not_modified`, `budget_before`,
  `projected`, `floor`, `budget_refused`, `budget_stopped`). It first reads
  the repository's `archived` flag (#10562, one REST + ETag read shared with
  `release-stale-blocked` and the role runner): an archived repository is
  read-only, so nothing is listed, `--json` reports `archived: true` and the
  human report is one line. A probe that fails is NOT EVALUATED, never
  archived and never clear (`archived: null`). Champion's merge-risk hold
  digest issue (exact title, or body starting with its marker) is exempt from
  this pass, the notifier and the release pass: it is `loom:blocked` only to
  stay out of curation and has no blocker to cite (#9397).
  **Do not loop it over many repositories by hand.** The budget floor reads
  the hourly buckets, which a secondary rate limit never shows in, and a
  one-shot CLI invocation has no rate-limit breaker (it exists only in the
  running daemon), so nothing carries a refusal from one invocation to the
  next. A caller that needs several repositories must pace
  the invocations and stop at the first one that reports a rate-limit refusal
  (it still exits 0, so read the report, not the exit code). Why, and what to
  check instead: [github-authentication.md → Secondary rate limits and
  hand-rolled loops](github-authentication.md#secondary-rate-limits-and-hand-rolled-loops-9191).
- `guide.md`'s `check_and_unblock` / `check_and_unblock_prs` — the active
  unblock sweep. A rendered park record's `Blocked by: #N` line already
  matches `parse_dependencies`'s existing pattern, so no separate parser is
  needed; the sweep reads it exactly like any other body-declared dependency.
  A dependency stated only in a comment is invisible to this sweep by design
  — see "Problem: Stuck Blocked Issues" in `guide.md`.

```bash
# The inverse of render — read the declared blockers back out of a body:
gh issue view 8852 --json body --jq .body | loom-daemon park-record parse
# 8860
gh issue view 8852 --json body --jq .body | loom-daemon park-record parse --json
# {"records":[{"blocker":8860,"by":null,"at":null,"reason":null}],"blockers":[8860],"declared":true}
```

`park-record parse` exits `0` when at least one record was found and `1` when
none was — a shell caller can branch on "is this park declared?" without
parsing output, mirroring `forge check-open-pr`'s exit-code convention.

## The unblock sweep's PR-side functions

`gh issue list` never returns a pull request, so `guide.md`'s
`check_and_unblock` — scoped to issues only — was never a candidate list a
parked PR could appear on at all (#8925's first defect). `check_and_unblock`
calls `check_and_unblock_prs` once, after its own issue loop, to cover the PR
population; the three functions below are its full definition, read on demand
from `guide.md`'s "Unblocking Pull Requests (#8925)" section rather than
inlined there (the role-prompt size ratchet: guide.md is installed at two
depths and its whole prompt prefix is frozen at its current size).

A parked PR's "restore to the queue" differs from an issue's in two ways:
there is no `loom:issue` to restore (a PR was never curated), and the
superseding check reads the PR's OWN state rather than a *linked* PR's — the
parked PR **is** the implementation, so there is nothing else to look up.

```bash
check_and_unblock_prs() {
  "$GH_READ" pr list --label "loom:blocked" --state open --json number,body,title | jq -c '.[]' | while read -r pr_row; do
    local number=$(printf '%s\n' "$pr_row" | jq -r '.number')
    local body=$(printf '%s\n' "$pr_row" | jq -r '.body')
    local title=$(printf '%s\n' "$pr_row" | jq -r '.title')

    local deps=$(parse_dependencies "$body")

    if [ -z "$deps" ]; then
      # No parseable dependency in the PR's own BODY — a park record that was
      # never written, or one that lives only in a comment (#8925's second
      # defect). Skip rather than guess; `loom-daemon check-stale-blocked`
      # reports this case as UNDOCUMENTED or PROSE-ONLY for a human.
      continue
    fi

    local all_resolved=true
    local resolved_deps=""

    for dep in $deps; do
      # A declared blocker can itself be an issue or a PR — try both reads.
      local dn="${dep##*#}" dr=""; [[ "$dep" == */* ]] && dr="${dep%#*}"  # OWNER/REPO#N: own repo (#10443)
      local state
      state=$(gh issue view "$dn" ${dr:+--repo "$dr"} --json state --jq '.state' 2>/dev/null) \
        || state=$(gh pr view "$dn" ${dr:+--repo "$dr"} --json state --jq '.state' 2>/dev/null) \
        || state="UNKNOWN"
      if [ "$state" != "CLOSED" ] && [ "$state" != "MERGED" ]; then
        all_resolved=false
        break
      fi
      resolved_deps="$resolved_deps $dr#$dn"
    done

    if [ "$all_resolved" = true ]; then
      # #4634/#7267 gate, transposed (#8925): the parked PR's OWN state can
      # still supersede a cleared dependency — see pr_has_superseding_block.
      if [ "$(pr_has_superseding_block "$number")" = "true" ]; then
        echo "Skipped PR #$number (declared blocker resolved, but the PR itself cannot proceed — leaving loom:blocked): $title"
        continue
      fi

      local restore_label
      restore_label=$(previous_review_label "$number")
      gh pr edit "$number" --remove-label "loom:blocked" --add-label "$restore_label"
      gh pr comment "$number" --body "🔓 **Unblocked**: Declared blocker(s) resolved ($resolved_deps). Restored \`$restore_label\`."
      echo "Unblocked PR #$number (restored $restore_label): $title"
    fi
  done
}
```

**`pr_has_superseding_block`** — the PR-side transposition of `guide.md`'s
`has_superseding_block`. There is no linked implementation PR to read; the
parked PR's own labels and merge state answer the question instead.
`loom:changes-requested`/`loom:review-requested`/`loom:ci-failure` are
deliberately **excluded** — on a PR those are its normal review-lane state,
not a hold, and removing `loom:blocked` is exactly what hands it back to that
lane:

```bash
pr_has_superseding_block() {
  local number="$1"
  local pr_json
  pr_json=$(gh pr view "$number" --json state,labels,mergeable,mergeStateStatus 2>/dev/null) || { echo "true"; return; }  # fail safe: an unread PR is never reported ready

  local pr_state=$(printf '%s\n' "$pr_json" | jq -r '.state')
  if [ "$pr_state" != "OPEN" ]; then
    echo "true"  # merged/closed mid-check — not this routine's to act on
    return
  fi

  local pr_hold=$(printf '%s\n' "$pr_json" | jq -r \
    '[.labels[].name] | any(. == "loom:operator" or . == "loom:operator-only")')
  if [ "$pr_hold" = "true" ]; then
    echo "true"
    return
  fi

  local pr_mergeable=$(printf '%s\n' "$pr_json" | jq -r '.mergeable')
  local pr_merge_state=$(printf '%s\n' "$pr_json" | jq -r '.mergeStateStatus')
  if [ "$pr_mergeable" = "CONFLICTING" ] || [ "$pr_merge_state" = "DIRTY" ] || [ "$pr_merge_state" = "CONFLICTING" ]; then
    echo "true"
    return
  fi

  echo "false"
}
```

**`previous_review_label`** — which review-lane label to restore
(`loom:review-requested` or `loom:changes-requested`); the Proposal's own
"decide and document which". Reads the label event history for the more
recent of the two labels ever applied — a fragile heuristic in the same class
as `guide.md`'s own "Secondary heuristic" for issues, so it defaults to the
safer of the two outcomes when history is empty or ambiguous: re-entering
Judge's queue on a PR that turns out to still need work costs one more read;
silently skipping Doctor's queue on a PR that needs work does not correct
itself.

```bash
previous_review_label() {
  local number="$1"
  local label
  label=$(gh api "repos/{owner}/{repo}/issues/${number}/events" --jq \
    'map(select(.event == "labeled" and (.label.name == "loom:review-requested" or .label.name == "loom:changes-requested"))) | last | .label.name' \
    2>/dev/null)
  if [ "$label" = "loom:changes-requested" ]; then
    echo "loom:changes-requested"
  else
    echo "loom:review-requested"
  fi
}
```

### Worked example (#8925's own #8314 shape)

```bash
# PR #8314 has loom:blocked, body contains "<!-- loom:park Blocked by: #8322
# by=doctor at=2026-09-19T12:09:00Z reason=\"needs an architecture ruling\" -->"
# (a park record, not a comment).
gh pr view 8314 --json labels,body

gh issue view 8322 --json state              # → state: CLOSED ✓
pr_has_superseding_block 8314                # → false (no operator hold, clean merge state)
previous_review_label 8314                   # → loom:changes-requested (its most recent label event before the park)

gh pr edit 8314 --remove-label "loom:blocked" --add-label "loom:changes-requested"
gh pr comment 8314 --body "🔓 **Unblocked**: Declared blocker(s) resolved (#8322). Restored \`loom:changes-requested\`."
```

## What this format does not do

It never reads the forge and never writes a label. Rendering is the
responsibility of the role applying the park; parsing is for whoever later
asks whether the park still holds. Deciding whether a cleared park should
actually be unparked is `guide.md`'s `check_and_unblock`/`check_and_unblock_prs`
job (issues) and `check-stale-blocked`'s advisory report (both issues and
PRs) — this document defines the wire format both sit on top of, nothing more.

See also: [`label-state-machine.md`](label-state-machine.md) for where
`loom:blocked` sits relative to `loom:operator`/`loom:operator-only`, and
[`lease-record.md`](lease-record.md) for the sibling HTML-comment-marker
convention this format's shape was co-designed with.
