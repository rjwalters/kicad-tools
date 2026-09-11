# Champion: Common Utilities

This file contains shared utilities, protocols, and information used across all Champion workflows.

---

## Completion Report

After evaluating both queues:

1. Report PRs evaluated and merged
2. Report issues evaluated and promoted
3. Report capped-PR recovery decisions (granted / kept parked / close recommended)
4. Report rejections with reasons
5. List merged PR numbers and promoted issue numbers with links
6. **Report the merge-risk hold census** — always, including when it is zero (#6720)
7. **Persist the per-PR merge-risk hold digest** — durably, across passes, not just in this transcript (#6851)

**Merge-risk hold census (mandatory line, #6720).** A held PR does not merge and
does not report itself; without a standing count, a pile grows unseen. On
`rjwalters/loom` it reached **21 held PRs, 20 of them conflicting**, and was
found only because an operator inspected PR labels by hand. Run the single
`gh pr list` query in `champion-pr-merge.md` → "Held-PR Census" **in this pass**
(never restate numbers from a previous one) and emit one line:

```
Merge-risk holds: <N> open PR(s) — <C> conflicting, <D> out at Doctor, oldest <A>d
```

Emit the line even when the set is empty (the census command already produces
`Merge-risk holds: 0 open PR(s) — 0 conflicting, 0 out at Doctor, oldest 0d`).
A line that only appears when something is wrong is a line nobody learns to read.

**Per-PR digest (mandatory, #6851) — additive to the census line above, not a
replacement for it.** The aggregate line is a transcript-only summary: it
tells the pipeline *how many* PRs are held, but a fresh read of it exists only
for the length of this pass's own session. Run `champion-pr-merge.md` →
"Held-PR Census" → "Per-PR Digest" **in this same pass** to also write PR
number / hold reason / `mergeable` status for every currently-held PR to the
pinned `Champion: Merge-Risk Hold Digest` tracking issue, so a human or another
role (e.g. Guide) can read the current pile without a Champion transcript or a
hand-run `loom:operator` label query. This is reporting/visibility only — it
changes no Safety Criteria evaluation and no merge decision.

**Example report**:

```
Role Assumed: Champion
Work Completed: Evaluated 2 PRs and 3 curated issues

PR Auto-Merge (2):
- PR #123: Fix typo in documentation
  https://github.com/owner/repo/pull/123
- PR #125: Update README with new feature
  https://github.com/owner/repo/pull/125

Issue Promotion (2):
- Issue #442: Add retry logic to API client
  https://github.com/owner/repo/issues/442
- Issue #445: Add worktree cleanup command
  https://github.com/owner/repo/issues/445

Capped-PR Recovery (2):
- PR #4543: granted 1 extra Doctor cycle (latest rejection is a distinct defect)
  https://github.com/owner/repo/pull/4543
- PR #4501: kept parked (same defect re-litigated across both rejections)
  https://github.com/owner/repo/pull/4501

Merge-risk holds: 3 open PR(s) — 2 conflicting, 1 out at Doctor, oldest 6d
Merge-risk hold digest updated: https://github.com/owner/repo/issues/6900

Rejected:
- PR #456: Too large (450 lines, limit is 200)
- Issue #443: Needs specific performance metrics

Next Steps: 2 PRs merged, 2 issues promoted, 1 capped PR returned to Doctor, 3 items await human review
```

---

## Safety Mechanisms

### Comment Trail

**Always leave a comment** explaining your decision, whether approving/merging or rejecting. This creates an audit trail for human review.

### Human Override

Humans can always:
- Hold a PR from auto-merge by removing its `loom:pr` label — Champion only merges PRs still labeled `loom:pr` — or add `loom:changes-requested` to send it back for changes
- Remove `loom:issue` and re-add `loom:curated` to reject issue promotion
- Add `loom:issue` directly to bypass Champion review
- Close issues/PRs marked for Champion review
- Manually merge or reject any PR

---

## Autonomous Operation

This role is designed for **autonomous operation** with a recommended interval of **10 minutes**.

**Default interval**: 600000ms (10 minutes)
**Default prompt**: "Check for safe PRs to auto-merge, quality issues to promote, and Doctor-cycle-capped PRs to reconsider"

### Autonomous Behavior

When running autonomously:
1. Check for `loom:pr` PRs (Priority 1)
2. Drain the queue — evaluate every qualifying PR (oldest first) and merge safe ones until the queue is empty (see `champion-pr-merge.md` §"PR Auto-Merge Batch Processing"; PR merging has no numeric per-iteration cap)
3. If no PRs, check for `loom:curated` issues (Priority 2)
4. Evaluate all qualifying issues (oldest first) and promote them, bounded only by the tier-based promotion limits in `champion-issue-promo.md` (Tier 1 unlimited / Tier 2 up to 2 per iteration / Tier 3 up to 1, gated at 5 backlog)
5. If no promotion work remains, run the capped-PR recovery pass over open `loom:blocked` + `loom:changes-requested` PRs (Priority 5) — one grant / keep-parked / recommend-close decision each, with a rationale comment (see `champion-pr-merge.md` §"Capped-PR Recovery Pass")
6. Report results and stop

### Quality Over Quantity

**Conservative bias is intentional.** It's better to defer borderline decisions than to flood the Builder queue with ambiguous work or merge risky PRs.

---

## Label Workflow Integration

```
Issue Lifecycle (Curated):
(created) -> loom:curated -> [Champion evaluates] -> loom:issue -> [Builder] -> (closed)

Issue Lifecycle (Architect Proposal):
(created by Architect) -> loom:architect -> [Champion evaluates] -> loom:issue -> [Builder] -> (closed)

Issue Lifecycle (Hermit Proposal):
(created by Hermit) -> loom:hermit -> [Champion evaluates] -> loom:issue -> [Builder] -> (closed)

PR Lifecycle:
(created) -> loom:review-requested -> [Judge] -> loom:pr -> [Champion merges] -> (merged)
```

---

## Notes

- **Champion = Human Avatar**: Empowered but conservative, makes final approval decisions
- **Dual Responsibility**: Both issue promotion and PR auto-merge
- **Transparency**: Always comment on decisions
- **Conservative**: When unsure, don't act
- **Audit trail**: Every action gets a detailed comment
- **Human override**: Humans have final say via labels or direct action
- **Reversible**: Git history preserved, can always revert merges

---

## Epic-Aware Blocker Check (#5211)

**Problem this section fixes**: every "Blocked by / Depends on / Requires #N"
check elsewhere in Champion (`champion-issue-promo.md`'s Technical Feasibility
criterion, `champion-epic.md`'s phase-creation gate, `champion-pr-merge.md`'s
Step 5 unblock scan) reduces the referenced issue to one boolean: `state ==
OPEN` means still blocked. That is correct for an ordinary issue and silently
wrong for an **epic** — an epic can sit `OPEN` for months after every one of
its capability/implementation children has closed and the feature has
shipped, simply because nobody ran `champion-epic.md`'s "Epic Completion"
step to close it. A dependent that cites that epic as a blocker then reads as
blocked forever. This is exactly what happened to example-org/downstream-repo#101
against example-org/tool-repo#202 (14/15 children closed, the feature
shipped, the epic still open) across two consecutive Champion passes, and it
compounded into an unrecoverable **cross-repo** deadlock because the epic's
one remaining phase happened to depend back on the blocked dependent (the
cycle itself is out of scope here — see #5213).

**Any Champion workflow that encounters a "Blocked by #N" / "Depends on #N" /
"Requires #N" style reference to another issue must run this check** — not a
bare `state == OPEN` read — whenever the referenced issue carries
`loom:epic`. This works without `LOOM_EPIC_SUPERVISOR` being enabled anywhere
(that daemon mechanism only reconciles epics inside its own registered
workspace and cannot see across repos — see
[`daemon-reference.md`'s "Epic supervisor"](https://github.com/rjwalters/loom/blob/main/defaults/docs/daemon-reference.md#epic-supervisor-3842)
section for how the two complement each other).

### Step 1 — parse the reference (cross-repo aware)

Blocking references in this fleet are frequently cross-repo (the
downstream-repo#101 → tool-repo#202 shape) — `owner/repo#N`, not just `#N` in
the current repo. `gh issue view` does **not** accept the bare `owner/repo#N`
positional form (`invalid issue format`) — it needs `-R owner/repo <N>` (or a
full URL), so parsing must split the two:

```bash
# $1 = a candidate reference string, e.g. "#202" or "example-org/tool-repo#202"
# $2 = "owner/repo" Champion is currently running in (used when $1 is bare)
parse_blocker_ref() {
  local ref="$1" this_repo="$2"
  if [[ "$ref" =~ ^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$ ]]; then
    BLOCKER_REPO="${BASH_REMATCH[1]}"
    BLOCKER_NUM="${BASH_REMATCH[2]}"
  elif [[ "$ref" =~ ^#([0-9]+)$ ]]; then
    BLOCKER_REPO="$this_repo"
    BLOCKER_NUM="${BASH_REMATCH[1]}"
  else
    return 1
  fi
}

# Extraction from a body: generalizes champion-pr-merge.md Step 5's regex to
# ALSO capture an optional "owner/repo" prefix ahead of the "#N" (#5211) — the
# existing `grep -Eo "#[0-9]+" | grep -Eo "[0-9]+"` pipeline used elsewhere
# silently drops any repo prefix and misreads a cross-repo reference as
# same-repo, which is exactly wrong for the incident this section fixes.
#
# Two-stage shape (matches the sibling parsers guide.md's parse_dependencies,
# sweep.md's --auto-stack detector, and warn-out-of-set-deps.sh): stage 1
# selects the whole matching LINE (grep -E, no -o) containing the phrase;
# stage 2 extracts every ref (bare #N or owner/repo#N) found on that line.
# A single-stage `grep -Eo` anchored to the phrase AND its immediately
# following #N can only ever match one ref per phrase occurrence, silently
# dropping every other comma-separated ref on the same line (e.g.
# "Blocked by: #1 (x), #3 (y)" would yield only #1) — under-parsing here is
# the highest-severity failure mode (see champion-pr-merge.md Step 5's own
# comment on ALL_DEPS), so the two-stage shape is load-bearing, not stylistic.
extract_blocker_refs() {
  local body="$1"
  printf '%s\n' "$body" \
    | grep -E '(Blocked by|Depends on|Requires)[*_:[:space:]]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+|#[0-9]+)' \
    | grep -Eo '([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+|#[0-9]+)' \
    | sort -u
}
```

### Step 1.5 — discover an epic's children (marker-independent, #6516)

The original form of Step 2 below asked one question — "which
`loom:epic-phase` issues carry `<!-- loom:epic:N:phase:M -->` in their body?"
— which sees **only** the children `champion-epic.md`'s Step 3 created itself.
An epic decomposed any other way (the GitHub sub-issues UI, a hand-written
task list, issues filed directly against the epic) has zero marker children
and reads as `blocked-not-started` no matter how finished it is. That is the
same class of blindness #5211 fixed one layer up, and it is what let a fully
executed epic sit open for days (#6516).

`discover_epic_children` unions the containment sources and keeps them graded,
because the two callers need different confidence:

| Tier | Sources | Who may act on it |
|---|---|---|
| **Strong** (containment) | (a) `loom:epic-phase` children carrying the phase marker, (b) native GitHub sub-issues, (c) `- [ ] #N` task-list entries in the epic body | Step 2's classification, and `champion-epic.md` → "Step 0" **including its autonomous close** |
| **Weak** (prose reference) | (d) issues whose body merely names `Epic #N` | Never on its own — it may only downgrade `champion-epic.md`'s Step 0 to an operator ask, and it never affects Step 2 |

```bash
# Sets EPIC_CHILD_STRONG_{OPEN,CLOSED}, EPIC_CHILD_WEAK_{OPEN,CLOSED},
# EPIC_CHILD_SOURCES (comma list of the sources that actually contributed).
# $1 = "owner/repo", $2 = epic number.
discover_epic_children() {
  local repo="$1" num="$2" marker="loom:epic:$2:phase:" srcs=""

  # (a) phase-marker children — the historical source (champion-epic.md Step 3).
  local a
  # `gh --jq` takes only an expression (no `--arg`), so the marker — digits and
  # literals only — is interpolated into the program directly.
  a=$(${GH_READ:-gh} issue list --repo "$repo" --label="loom:epic-phase" --state=all --limit=500 \
    --json number,state,body \
    --jq "[.[] | select(.body | contains(\"$marker\")) | {number, state}]" 2>/dev/null || echo '[]')

  # (b) native sub-issues. 404s (→ `[]`) on forges/repos without the API, which
  # is a legitimate "no evidence", never "no children".
  local b
  b=$(gh api "repos/$repo/issues/$num/sub_issues" --paginate \
    --jq '[.[] | {number, state: (.state | ascii_upcase)}]' 2>/dev/null | jq -s 'add // []')

  # (c) same-repo task-list entries in the epic body ("- [ ] #123" / "- [x] #123").
  # Resolve each ref's state; cap the fan-out so a pathological body cannot burn
  # the API budget (a >40-item hand-written epic is already an operator problem).
  local body refs c='[]'
  body=$(${GH_READ:-gh} issue view "$num" --repo "$repo" --json body --jq '.body // ""' 2>/dev/null)
  refs=$(printf '%s\n' "$body" \
    | grep -Eo '^[[:space:]]*[-*][[:space:]]*\[[ xX]\][[:space:]]*#[0-9]+' \
    | grep -Eo '[0-9]+' | sort -un | head -40)
  for r in $refs; do
    [ "$r" = "$num" ] && continue
    local st
    st=$(${GH_READ:-gh} issue view "$r" --repo "$repo" --json state --jq '.state' 2>/dev/null) || continue
    c=$(printf '%s' "$c" | jq --argjson n "$r" --arg s "$st" '. + [{number:$n, state:$s}]')
  done

  # (d) weak: prose references to this epic, no containment claimed.
  local d
  d=$(${GH_READ:-gh} issue list --repo "$repo" --state=all --limit=200 \
    --search "\"Epic #$num\" in:body" --json number,state --jq '.' 2>/dev/null || echo '[]')

  local strong weak
  strong=$(jq -s --argjson e "$num" 'add | map(select(.number != $e)) | unique_by(.number)' \
    <(printf '%s' "$a") <(printf '%s' "$b") <(printf '%s' "$c"))
  weak=$(jq -s --argjson e "$num" '(.[0] | map(.number)) as $s
    | .[1] | map(select(.number != $e and (.number | IN($s[]) | not))) | unique_by(.number)' \
    <(printf '%s' "$strong") <(printf '%s' "$d"))

  EPIC_CHILD_STRONG_OPEN=$(printf '%s' "$strong" | jq '[.[] | select(.state=="OPEN")] | length')
  EPIC_CHILD_STRONG_CLOSED=$(printf '%s' "$strong" | jq '[.[] | select(.state=="CLOSED")] | length')
  EPIC_CHILD_WEAK_OPEN=$(printf '%s' "$weak" | jq '[.[] | select(.state=="OPEN")] | length')
  EPIC_CHILD_WEAK_CLOSED=$(printf '%s' "$weak" | jq '[.[] | select(.state=="CLOSED")] | length')
  [ "$(printf '%s' "$a" | jq 'length')" -gt 0 ] && srcs="$srcs,phase-marker"
  [ "$(printf '%s' "$b" | jq 'length')" -gt 0 ] && srcs="$srcs,sub-issues"
  [ "$(printf '%s' "$c" | jq 'length')" -gt 0 ] && srcs="$srcs,task-list"
  [ "$(printf '%s' "$weak" | jq 'length')" -gt 0 ] && srcs="$srcs,prose-reference"
  EPIC_CHILD_SOURCES="${srcs#,}"
}
```

**A source finding nothing is never evidence of completion.** Every counter
starts at 0, so "no children anywhere" is `STRONG_CLOSED == 0` and both
callers must treat it as *undecomposed*, not *done* — the same reason
`CLOSED_COUNT == 0` gates `blocked-not-started` below.

### Step 2 — classify the referenced blocker

```bash
# BLOCKER_REPO / BLOCKER_NUM set by parse_blocker_ref above.
# Cached (${GH_READ:-gh}) — this classifies a dependency, it is not itself a
# merge/claim gate, so a cached read is fine. ${GH_READ:-gh} uses the caller's
# 30s-TTL cached reader when it has defined one (champion-pr-merge.md,
# champion-issue-promo.md) and falls back to plain `gh` otherwise — this file is
# shared, so it must not assume $GH_READ is always set.
BLOCKER_JSON=$(${GH_READ:-gh} issue view "$BLOCKER_NUM" --repo "$BLOCKER_REPO" --json state,labels,comments 2>/dev/null)
BLOCKER_STATE=$(printf '%s\n' "$BLOCKER_JSON" | jq -r '.state // "OPEN"')
IS_EPIC=$(printf '%s\n' "$BLOCKER_JSON" | jq -e '.labels[] | select(.name=="loom:epic")' >/dev/null && echo yes || echo no)

if [ "$IS_EPIC" != "yes" ]; then
  EPIC_BLOCK_STATE="not-epic"   # caller falls back to its own plain state==OPEN check
elif [ "$BLOCKER_STATE" = "CLOSED" ]; then
  EPIC_BLOCK_STATE="resolved"   # already closed — dependency satisfied
else
  # Walk EVERY phase's children, not just one — generalizes champion-epic.md's
  # "Detecting Phase Completion" (which deliberately scopes to a single PHASE
  # number for the "should I create phase N+1" question) to the "is this whole
  # epic's delivered capability done" question instead. Step 1.5's discovery is
  # also marker-independent, so an epic decomposed via sub-issues or a body task
  # list is classified on the same footing as one Champion decomposed itself
  # (#6516). Cached reads throughout (${GH_READ:-gh}) — a classification read,
  # same rationale as the BLOCKER_JSON read above.
  discover_epic_children "$BLOCKER_REPO" "$BLOCKER_NUM"
  # STRONG (containment) counts only: a prose "Epic #N" mention must never be
  # able to make a blocker look delivered.
  OPEN_COUNT="$EPIC_CHILD_STRONG_OPEN"
  CLOSED_COUNT="$EPIC_CHILD_STRONG_CLOSED"

  if [ "$OPEN_COUNT" -gt 0 ]; then
    EPIC_BLOCK_STATE="blocked-in-progress"        # genuinely still working — keep blocking, no change here
  elif [ "$CLOSED_COUNT" -eq 0 ]; then
    EPIC_BLOCK_STATE="blocked-not-started"        # no children found by ANY containment source — keep blocking, no change here
  else
    EPIC_BLOCK_STATE="epic-complete-unpromoted"   # OPEN_COUNT==0 AND CLOSED_COUNT>0: the trap state (#5211)
  fi
fi
```

`CLOSED_COUNT == 0` (not just `OPEN_COUNT == 0`) gates `blocked-not-started` —
an epic that has never been decomposed has `OPEN_COUNT == 0` vacuously and
must **not** be misread as complete. Widening discovery in Step 1.5 does not
weaken that: a source that returns nothing contributes nothing, so an
undecomposed epic still lands on `blocked-not-started` through every source
at once.

**Only `epic-complete-unpromoted` changes any caller's behavior.**
`blocked-not-started` and `blocked-in-progress` are the common, correct case
— a caller keeps blocking on those exactly as it did before this check
existed. This is what keeps AC "must not weaken the correct common case"
true: this section adds a new outcome, it does not touch the existing ones.

### Step 3 — fingerprint the blocker's observed state (idempotency key)

Key the marker to the **blocker's own state**, not the dependent's body — the
dependent's text is typically unchanged for weeks while a cross-repo epic
ships underneath it, so body-hash keying (as used elsewhere for
unrevised-*proposal* detection, see `champion-issue-promo.md`'s "Concurrency
Guard and Idempotency") would freeze on the wrong verdict forever if reused
here: the thing that must change to unstick this check is the *blocker's*
state, not the dependent's text (#5211).

```bash
# Portable sha256 — same fallback shape used elsewhere in this repo's scripts
# and in champion-issue-promo.md's idempotency check.
_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256
  else cksum; fi
}

BLOCKER_LABELS=$(printf '%s\n' "$BLOCKER_JSON" | jq -r '[.labels[].name] | sort | join(",")')
FINGERPRINT_SRC="$BLOCKER_REPO#$BLOCKER_NUM|$BLOCKER_STATE|$BLOCKER_LABELS|open=$OPEN_COUNT|closed=$CLOSED_COUNT"
FINGERPRINT=$(printf '%s' "$FINGERPRINT_SRC" | _sha256 | awk '{print substr($1,1,16)}')
EPIC_BLOCK_MARKER="<!-- champion:epic-block:$BLOCKER_REPO#$BLOCKER_NUM:$FINGERPRINT -->"
```

### Step 4 — act on the classification (idempotent, bounded escalation)

Only reached when `EPIC_BLOCK_STATE = "epic-complete-unpromoted"`. Run
against the **dependent** issue/PR being evaluated in the current repo
(`DEPENDENT_ISSUE`) — the caller (`champion-issue-promo.md` /
`champion-epic.md`) sets this before invoking Step 4.

```bash
DEPENDENT_ISSUE=<the issue/PR being evaluated, in the CURRENT repo>

# Terminal state check first — never re-comment or re-tally once a human owns this.
ALREADY_ROUTED=$(gh issue view "$DEPENDENT_ISSUE" --json labels --jq \
  '.labels[] | select(.name=="loom:operator-only")' 2>/dev/null)
if [ -n "$ALREADY_ROUTED" ]; then
  # Stays unconditional HERE (#5664). champion-issue-promo.md's equivalent
  # short-circuit became conditional so a dependency-only escalation can
  # self-heal when its blocker closes; this one must not, because the escalation
  # it guards says "an epic looks complete but is still open" — that resolves
  # only by a human closing or promoting the epic, never on its own. An
  # un-escalation here would loop the same escalation forever.
  echo "#$DEPENDENT_ISSUE already routed to loom:operator-only — skip silently"
else
  # REST, not `gh issue view` — only the REST payload has the numeric comment
  # id the PATCH below needs (the `gh issue view --json comments` id is a
  # GraphQL node id and cannot be PATCHed). Same rationale as
  # champion-issue-promo.md's idempotency check.
  FLAG_COMMENT=$(gh api "repos/{owner}/{repo}/issues/$DEPENDENT_ISSUE/comments" --paginate \
    --jq ".[] | select(.body | contains(\"$EPIC_BLOCK_MARKER\"))" | jq -s 'last')
  FLAG_COMMENT_ID=$(printf '%s\n' "$FLAG_COMMENT" | jq -r '.id // empty')
  FLAG_COMMENT_BODY=$(printf '%s\n' "$FLAG_COMMENT" | jq -r '.body // ""')

  if [ -n "$FLAG_COMMENT_ID" ]; then
    # Same fingerprint seen before — read the streak this marker has already
    # recorded (mirrors champion-issue-promo.md's unrevised-skips counter).
    STREAK=$(printf '%s' "$FLAG_COMMENT_BODY" \
      | sed -n "s|.*<!-- champion:epic-block-streak:$FINGERPRINT:\([0-9]\{1,\}\) -->.*|\1|p" | tail -n 1)
    STREAK=${STREAK:-1}
    NEXT_STREAK=$(( STREAK + 1 ))

    if [ "$NEXT_STREAK" -ge "${LOOM_MAX_UNCHANGED_EPIC_BLOCK_EVALS:-2}" ]; then
      # Budget exhausted: this unchanged state has now been observed
      # LOOM_MAX_UNCHANGED_EPIC_BLOCK_EVALS times without the epic being
      # closed/promoted — escalate instead of tallying again.
      gh issue comment "$DEPENDENT_ISSUE" --body "$EPIC_BLOCK_MARKER
**Champion: Escalating — Blocked on an Epic That Appears Complete**

\`$BLOCKER_REPO#$BLOCKER_NUM\` still carries \`loom:epic\` and is still open,
but $CLOSED_COUNT of its \`loom:epic-phase\` children are closed and none are
open — and this state has now been observed unchanged across $NEXT_STREAK
evaluations. This is not a live blocker; it needs an operator to close or
promote \`$BLOCKER_REPO#$BLOCKER_NUM\`.

Blocked by $BLOCKER_REPO#$BLOCKER_NUM

---
*Automated by Champion role*" \
        && gh issue edit "$DEPENDENT_ISSUE" --add-label "loom:operator-only,loom:operator-blocked"
    else
      # Still within budget: tally the streak IN PLACE (PATCH, no new
      # comment/notification) and keep not-gating on this reference.
      if printf '%s' "$FLAG_COMMENT_BODY" | grep -q "<!-- champion:epic-block-streak:$FINGERPRINT:"; then
        NEW_BODY=$(printf '%s' "$FLAG_COMMENT_BODY" \
          | sed "s|<!-- champion:epic-block-streak:$FINGERPRINT:[0-9]\{1,\} -->|<!-- champion:epic-block-streak:$FINGERPRINT:$NEXT_STREAK -->|")
      else
        NEW_BODY=$(printf '%s\n\n%s' "$FLAG_COMMENT_BODY" "<!-- champion:epic-block-streak:$FINGERPRINT:$NEXT_STREAK -->")
      fi
      gh api --method PATCH "repos/{owner}/{repo}/issues/comments/$FLAG_COMMENT_ID" \
        -f body="$NEW_BODY" >/dev/null
      echo "#$DEPENDENT_ISSUE: unchanged epic-block fingerprint $FINGERPRINT, streak $NEXT_STREAK/${LOOM_MAX_UNCHANGED_EPIC_BLOCK_EVALS:-2} — not gating, no new comment"
    fi
  else
    # First time seeing this exact fingerprint: flag it, but do not keep
    # gating on it — this is the mechanism AC #1/#2 (#5211) ask for.
    gh issue comment "$DEPENDENT_ISSUE" --body "$EPIC_BLOCK_MARKER
<!-- champion:epic-block-streak:$FINGERPRINT:1 -->
**Champion: Epic Blocker Appears Complete — Not Treated as a Live Block**

\`$BLOCKER_REPO#$BLOCKER_NUM\` is referenced as a blocker but all $CLOSED_COUNT
of its \`loom:epic-phase\` children are closed and none are open — the epic's
capability work looks delivered even though the epic issue itself is still
open (label state, not delivered capability, per #5211). Not gating on this
reference this pass. If \`$BLOCKER_REPO#$BLOCKER_NUM\` is genuinely still
incomplete, reopen the discussion there — otherwise it should be
closed/promoted.

---
*Automated by Champion role*"

    # Best-effort: also flag the epic itself for close/promote review, once —
    # a separate marker (not the fingerprint one, which is scoped to THIS
    # dependent) so repeated dependents referencing the same epic don't each
    # re-flag it.
    EPIC_FLAG_MARKER="<!-- champion:epic-appears-complete -->"
    if ! printf '%s\n' "$BLOCKER_JSON" | jq -e --arg m "$EPIC_FLAG_MARKER" \
         '.comments[] | select(.body | contains($m))' >/dev/null; then
      gh issue comment "$BLOCKER_NUM" --repo "$BLOCKER_REPO" --body "$EPIC_FLAG_MARKER
**Champion: This Epic Appears Complete**

All $CLOSED_COUNT \`loom:epic-phase\` children found for this epic are closed
and none are open, but this issue is still open and still carries
\`loom:epic\`. A dependent (\`$DEPENDENT_ISSUE\`, possibly in a different repo)
was blocked on this reference. Please review for close/promote (see
\`champion-epic.md\` → \"Epic Completion\").

---
*Automated by Champion role*"
    fi
  fi
fi
```

`LOOM_MAX_UNCHANGED_EPIC_BLOCK_EVALS` (default **2**) — bounds how many times
the *identical* blocker fingerprint can be silently re-observed before
escalating, mirroring `LOOM_MAX_UNREVISED_EVALUATIONS`'s naming/default
convention in `champion-issue-promo.md`. A **changed** fingerprint (the epic
closed, gained an open child again, etc.) computes a different
`EPIC_BLOCK_MARKER` entirely, so it is treated as new — the streak resets by
construction rather than by an explicit reset step.

**Invariants a future edit must preserve** (mirrors
`champion-issue-promo.md`'s "Bounding the silent skip" for the same reason):

- Comment budget for a persistently-unresolved `epic-complete-unpromoted`
  state is bounded, not unbounded: one flag comment, then only silent
  in-place `PATCH` tallies (no new comment, no notification) until
  `LOOM_MAX_UNCHANGED_EPIC_BLOCK_EVALS` (default 2) is reached, then exactly
  one escalation comment — total **2 comments on the dependent at the default
  threshold**, plus at most 1 on the epic itself (shared across every
  dependent that references it) — never a fresh comment every pass.
- `blocked-not-started` / `blocked-in-progress` never enter this step at all
  — they are not new behavior, so they carry none of this section's comment
  or label overhead.
- `ALREADY_ROUTED=yes` short-circuits everything — a dependent already
  carrying `loom:operator-only` from this mechanism is never re-tallied or
  re-escalated.
- The escalation always adds `loom:operator-blocked` alongside
  `loom:operator-only` (#5671) — this condition is, by construction,
  self-clearing once `$BLOCKER_REPO#$BLOCKER_NUM` closes or promotes, which is
  exactly the distinction #5664 needs from a genuine operator decision. See
  `.loom/docs/label-state-machine.md` "operator-only sub-kinds" for the full
  convention, including the required `Blocked by #N` machine-readable line.
- This check must run **independently of** any body-hash-keyed idempotency
  skip a caller applies to itself (e.g. `champion-issue-promo.md`'s unrevised-
  proposal skip). A dependent's own text can stay byte-identical for weeks
  while the *blocker's* fingerprint changes underneath it (an epic finishing
  its remaining phase) — a caller that lets its own body-hash skip suppress
  this check entirely would silently freeze on a stale verdict even after the
  epic resolves. Run this check first, every pass, then let the caller's own
  idempotency govern only the parts of its evaluation this check did not
  already resolve.

---

## Terminal Probe Protocol

When you receive a probe command, respond with: `AGENT:Champion:<brief-task>` — e.g. `AGENT:Champion:merging-PR-123`.

**The full probe protocol** (format, per-role examples, task-description conventions, and rationale) **lives in [`probe-protocol.md`](probe-protocol.md).**

---

