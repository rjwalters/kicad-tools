# Sweep — Scheduling signals — overlap partitioning, capability lane, operator-gate scan

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** after Stage -1 resolves the subagent path and before the confirmation gate / first wave. Modes A/B and the `all` sentinel need all three sections; Mode C needs the capability-aware `loom:operator-mechanical` lane (C0 pre-flight references it).
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Overlap-aware wave partitioning (file-surface scheduling signal, #4161)](#overlap-aware-wave-partitioning-file-surface-scheduling-signal-4161)
- [Capability-aware `loom:operator-mechanical` lane (#6885 Part 2, #6893)](#capability-aware-loomoperator-mechanical-lane-6885-part-2-6893)
- [Operator-gate advisory scan (body-phrase + title-prefix detection, #5137, extended #6391)](#operator-gate-advisory-scan-body-phrase--title-prefix-detection-5137-extended-6391)

---

## Overlap-aware wave partitioning (file-surface scheduling signal, #4161)

Wave partitioning (Execution Model above) picks candidates into waves **in list order** with no awareness of which files each candidate is likely to touch. When two candidates in the **same** wave edit the same file, their builders branch off the same pre-wave `main` snapshot, each PR passes Judge independently, and both report `MERGEABLE`/`CLEAN` — but only until the first merges (see the base-branch-only callout in Execution Model). The reactive step 7 revalidation then pays a Doctor rebase cost that a smarter *partition* could have avoided. This section adds the **proactive** complement: estimate each candidate's file surface cheaply, and keep overlapping candidates out of the same wave (or, when that is impossible, warn loudly at the confirmation gate).

**File overlap is a *scheduling* signal only — never a *topology* signal.** Overlap decides **which wave** a candidate lands in (or produces a warning). It **must never** create a `--depends-on`/`--auto-stack` stacking edge: #3729 explicitly rejected file paths as a stacking-topology signal (only the authoritative `Depends on #A` / `Requires #A` body text creates stacks — see "Auto-stack detection and wave ordering"). Overlap-aware partitioning and auto-stacking are distinct uses of the same raw data; do not conflate them.

### 1. Estimate each candidate's file surface (cheap, non-blocking)

Per-issue pre-flight already reads each candidate's issue body (dry-run survey step 1 under `--auto-stack`; live Wave Lifecycle step 2 always). Add `body` to the survey read unconditionally — one extra `--json` field, **no extra API call** — and parse the issue's `## Affected Files` section:

- Collect the **backtick-quoted paths** from the bullets under an `## Affected Files` heading (the exact format Curators emit — see `curator.md`). A bullet like `` - `defaults/.claude/commands/loom/sweep.md` — … `` contributes the path `defaults/.claude/commands/loom/sweep.md`.
- **Missing `## Affected Files` section, or a section that reads "To be determined" / has no backtick paths → the candidate's surface is *unknown*.** An unknown-surface candidate is **excluded from overlap analysis entirely**: it never triggers a warning, never forces a reorder, and never blocks. Optionally note "surface unknown" beside it in the plan. Never serialize or block on missing data — a candidate without a parseable surface plans byte-for-byte as it does today.

Surface estimation is a heuristic on curated prose, not a diff inspection; it is deliberately cheap and best-effort. A candidate whose real diff touches a file its `## Affected Files` omitted is caught by the reactive step 7 / step 8 gates, which stay the backstop.

### 2. Adjust the partition to separate overlapping candidates

After the wave partition is computed — and, under `--auto-stack`, **after** the #3759 parent-before-child topological reorder — scan for **same-wave pairs whose estimated surfaces share ≥1 path**:

- **Reorder greedily.** For an overlapping same-wave pair, swap one member with a later **non-overlapping** candidate so the two land in different waves. Prefer swaps that preserve input order where possible; the algorithm is deterministic (same candidate list → same partition), single-pass, and uses no graph machinery.
- **Never break an auto-stack ordering constraint.** A swap must keep every parent's wave at or before its child's wave. A stacked child *intentionally* shares files with its parent, so **exclude any pair already related via `DEPENDS_ON[N]`** (either direction) from overlap detection — that is expected sharing, not an accidental collision, and it is already handled by the stacked-branch mechanics.
- **Unavoidable overlap → warn, don't loop.** If an overlapping group has more members than there are waves to spread them across (so no reorder can separate them all), **leave the placement as-is** and emit the confirmation-gate warning below. Do not enter a reorder loop; a single warning is the contract.

The reorder only changes **wave assignment**; it never adds/removes candidates and never creates a stacking edge.

### 3. Surface the analysis (dry-run block + confirmation-gate warning)

- **`--dry-run` overlap-analysis block** — the dry-run plan (Modes A/B, "Issue-set output spec") gains an `Overlap analysis` block naming each overlapping group's shared file(s), the resulting wave moves, and any unavoidable-overlap warning. This is exactly where an operator wants to catch a collision. See the block spec in "Procedure — Modes A and B".
- **Confirmation-gate warning** — both the Mode B candidate-set gate and the `all`-sentinel **mandatory** confirmation gate print any **unavoidable-overlap** warning **above** the candidate listing, naming the shared files and the specific candidates, so the operator can reorder manually or drop to `--builders-per-wave 1` in seconds before dispatch.

### 4. Reactive fallback (do not rebuild — cross-reference)

Whatever overlap the partition **could not** avoid (unavoidable groups, or a real diff that exceeded its `## Affected Files` estimate) is caught reactively at merge time by **Wave Lifecycle step 7 (Intra-wave overlap revalidation, #3647)** — it re-checks each about-to-merge PR's changed-file set against `WAVE_MERGED_FILES`, updates an overlapping branch onto the just-merged `main`, and routes `DIRTY` to an inline Doctor→Judge cycle. **Step 8 (post-wave integration gate, #3647)** additionally catches cross-file semantic coupling (source-vs-test) that file-path overlap cannot see. Proactive partitioning reduces how often that reactive cost is paid; it does not replace it. This section adds no new reactive machinery — step 7/8 already exist and are the fallback.

## Capability-aware `loom:operator-mechanical` lane (#6885 Part 2, #6893)

`loom:operator-only` is a hard skip for all four of its sub-kinds. One of them — `loom:operator-mechanical` — means "needs host/admin access or a credential, but **no judgement**" (`.loom/docs/label-state-machine.md` → "`loom:operator-only` sub-kinds"). This section is the *only* place that treats it differently, and it does so under four gates that all have to pass. **Nothing here weakens `loom:operator-only` for any other item**: `loom:operator-decision`, `loom:operator-blocked`, `loom:operator-objective`, `loom:needs-capability` and `loom:blocked` are byte-for-byte unchanged, and the routing tables above land every non-matching case on the ordinary hard-skip row.

### Eligibility (all four gates, in order — any failure means "skip exactly as today")

1. **This worker declares capabilities.** `LOOM_WORKER_CAPABILITIES` is set in the environment, e.g. `export LOOM_WORKER_CAPABILITIES="host-sudo,cloud-profile:prod-aws"`. Unset (the default on every host) ⇒ the lane is inert and this whole section is a no-op. It is read from the **environment only, never from `.loom/config.json`** — a capability is a property of the machine and its credentials, and a file committed to git must not be able to assert that the host running it has root.
2. **The labels are exactly the mechanical shape.** Both `loom:operator-only` and `loom:operator-mechanical` are present, and **none** of `loom:operator-decision` / `loom:operator-blocked` / `loom:operator-objective` / `loom:needs-capability` / `loom:blocked` / `loom:operator` is. A contradictory pairing (mechanical *and* a judgement sub-kind) resolves in favour of the judgement sub-kind — skip.
3. **The body declares at least one capability, and every declared value is recognized.** Parse the item body with the reference parser — never a hand-rolled regex, so this side and the daemon's (`loom-daemon/src/capability.rs`) cannot diverge:
   ```bash
   CAPS=$(gh issue view N --json body --jq .body | ./.loom/scripts/extract-capability-markers.sh); RC=$?
   ```
   `RC=1` (no marker at all) and `RC=2` (an unrecognized value — fail closed, per `.loom/docs/label-state-machine.md`) both mean **skip, unchanged, no comment**. Only `RC=0` continues.
4. **This worker holds every declared capability.** Markers are **ANDed**. Holding some but not all is a miss, and lands on the capability-request path below.

### Behavior when a declared capability is NOT held (AC1)

Do **not** stall silently — turn the park into a named request. Post one comment (idempotent: if a comment already names the same missing capability, skip re-posting) and move on to the next candidate:

```
Capability request (#6893): this item is `loom:operator-mechanical` and declares
`<!-- loom:capability=<missing> -->`, which this worker does not hold.

Held by this worker: <the LOOM_WORKER_CAPABILITIES list, or "(none)">
Missing:             <the specific declared capabilities that are absent>

The item stays parked. To unblock it, either run a sweep on a host that holds the
missing capability, or have an operator perform the mechanical step directly.
```

Never add, remove, or swap a label on this path. The item keeps `loom:operator-only` + `loom:operator-mechanical` exactly as it was.

### Behavior when every capability IS held: propose mode only (AC4)

**The lane's output is a proposal, never a live credentialed action.** This first rollout has no live-execution mode at all, by design — the daemon's own routing enum has exactly one dispatchable variant and it is `ProposeDispatch` (`loom-daemon/src/capability.rs`). Concretely, the phase that runs for such an item MUST:

- Produce **the exact commands** an operator would run (a fenced block, copy-pasteable, with real values substituted — not a sketch), **or** a PR containing the mechanical change, whichever fits the item.
- Post that proposal as a comment (or the PR body) and **stop**. The item stays `loom:operator-only` + `loom:operator-mechanical` and is not closed; an operator's approval and execution is the next step.
- **Never** execute against a credential, run the proposed commands, `sudo`, mutate cloud/infra state, or use an admin-scoped token — even though gate 1 asserts the worker *holds* the capability. Holding it is what makes the proposal accurate; it is not permission to use it.

Adding a live-execution mode is out of scope here and requires its own explicit, separate opt-in — file it as its own issue rather than extending this section.

### Conservative and auditable (AC3)

Three rules, all mandatory, all reported in the proposal itself:

- **Report what was done.** Every action taken under this lane is named in the comment/PR body — what was read, what is proposed, and what was deliberately not touched. An operator reading only the comment must be able to reconstruct the whole pass.
- **Never widen scope.** Do exactly the item's stated task. A neighbouring cleanup, a "while I'm here" fix, or a second mechanical step the item did not ask for is out of bounds — note it and file a separate issue via `./.loom/scripts/create-issue.sh` instead.
- **Hard-stop on any judgement call.** The moment anything encountered requires a *ruling* rather than mechanical execution — an ambiguous target, a choice between defensible options, a risk to accept on someone's behalf, a value nobody has specified — **stop immediately**, post what was found and why it is a judgement call, and relabel:
  ```bash
  gh issue edit N --remove-label "loom:operator-mechanical" --add-label "loom:operator-decision"
  ```
  (or `loom:operator-objective` when the blocker is a missing objective rather than an authority call — see the "classifying question" table in `.loom/docs/label-state-machine.md`). The relabel is what makes the stop durable: the item is no longer eligible for this lane on any subsequent pass, by gate 2. **Do not** finish the mechanical part first and flag the judgement afterwards — a partially-completed mechanical action plus an open question is exactly the state this rule exists to prevent.

### Scope

This lane applies under the `all` sentinel's "Aggressive candidate taxonomy" (issue-set path) and Mode C's C0 per-PR pre-flight, which are the two surfaces that resolve items regardless of label. **Mode A/B's conservative pre-flight skip rules are unchanged** (Wave Lifecycle step 1 still hard-skips `loom:operator-only` outright) — an explicitly-named issue in Mode A is not a whole-backlog routing decision, and narrowing the blast radius of a first rollout is worth more than uniformity here.

## Operator-gate advisory scan (body-phrase + title-prefix detection, #5137, extended #6391)

The `loom:operator-only` and `loom:needs-capability` **labels** are the hard exclusions the "Aggressive candidate taxonomy" table enforces for the `all` sentinel. In practice, some issues declare operator-gating in **body text** without ever getting either label — "this acquisition step is operator-gated", "Operator decision: hold for credentials", "requires operator authorization" — so the aggressive whole-backlog taxonomy would otherwise silently plan them for automated build. Some declare it in their **title** instead — a title starting with `Operator:` (e.g. "Operator: visit the county archive and photograph the 1850 census page") whose body contains none of the phrase vocabulary below; a real 2026-08-17 sweep run missed exactly this shape (#6391). This section adds **advisory-only** signals layered on top of the label check: a scan implemented by `./.loom/scripts/warn-operator-gated.sh --candidates "<resolved candidate numbers>"` (structured like the sibling `warn-out-of-set-deps.sh`, #3747 v2 item 4 — detect-and-warn, never mutates, dedups per candidate), run in Stage 0 step 1c over the same `body` the survey already reads for "Overlap-aware wave partitioning" and `--auto-stack` edge detection — **no extra survey read** — plus one extra `--json title` read per candidate for the title-prefix signal (#6391). It runs **unconditionally for every `all`-sentinel run** (it does not require `--auto-stack`).

**Advisory only — never a hard skip, never a label mutation, never blocking.** Unlike the `loom:operator-only` / `loom:needs-capability` label rows (a hard `Skip`), a body-text or title-prefix match here changes nothing about the candidate's planned action: it still shows its normal `would build` (or whatever the taxonomy routing already decided) — annotated with a `⚠` warning so the **operator** can decide at the `all` sentinel's mandatory confirmation gate. Nothing here removes a label, closes an issue, or skips a candidate — only a human decision (or a follow-up `loom:operator-only`/`loom:needs-capability` label applied by a human) does that. The script always exits `0`.

**1. Phrase scan — instruction-shaped fragments, not bare substrings.** Case-insensitive match against `body`, mirroring the `loom:blocked` row's hold/defer-phrase discipline (#4505 — instruction-shaped fragments, not a naive substring search) rather than flagging on a bare word that appears constantly in ordinary engineering prose (a bare `operator` or `credentials` substring would false-positive on "operator precedence" or "rotate credentials in CI"). The vocabulary (the script's `PHRASES` array, matched in declared order so the annotation is deterministic):

- `operator-gated`
- `operator-only in substance`
- `operator authorization required`
- `operator decision:`
- `operator task` (#6198 — the framing agents most often open an operator-task body with: "**Operator task — requires human action, not automation.**")
- `requires operator` (catches "requires operator authorization", "requires operator input", "requires operator action", …)
- `requires human action` (#6198 — `requires operator` misses this by one word; deliberately **not** the bare word `human`, which would false-positive on "the human-in-the-loop design")
- `needs human action` (same narrowing)
- `login-walled`
- `paid gpu`
- `requires credentials` (deliberately **not** the bare word `credentials` alone — see the false-positive rationale above)
- `needs credentials` (same narrowing)

A candidate whose body matches ≥1 phrase is flagged with the **first** matched vocabulary phrase (the phrase itself, quoted verbatim — e.g. a body "acquisition is operator-gated" yields `"operator-gated"`), dedup to one phrase annotation per candidate even when multiple phrases match.

**2. Dependency-declared operator-gating (the `#87 → #4` shape).** Parse the same `body` for a `Depends on #A` / `Requires #A` reference, reusing the exact `(Depends on|Requires)[*_:[:space:]]*#[0-9]+` vocabulary the `--auto-stack` edge-detection pass and `warn-out-of-set-deps.sh` already use (see "Auto-stack detection and wave ordering" / "Out-of-set dependency detect-and-warn") — deliberately excluding `Blocked by` (that phrase drives the distinct `loom:blocked` machinery) — but run this lookup **unconditionally**, independent of whether `--auto-stack` was passed. This check only *reads* the reference to look up `#A`'s labels; it never populates `DEPENDS_ON[N]` or creates a stacking edge, so it is exempt from the `--auto-stack` flag gating that scopes actual stack *topology* (#3729 kept that opt-in). If any declared `#A` currently carries the `loom:operator-only` **or** `loom:needs-capability` **label**, flag the **child**: its declared dependency cannot itself be completed by automation, so building the child now would build against a base nobody can finish — exactly the `#87 → #4` shape from #5137 (the sweep skips `#4` as operator-only, then dispatches `#87` which needs it), extended in #5817 to the same shape when `#4` instead carries `loom:needs-capability`. Unlike `--auto-stack`'s same-candidate-set edge rule, this is a plain label lookup on whatever issue number the body names, **in-set or not** — there is no stacking edge being created.

**3. Title-prefix scan — a distinct signal from the body-phrase vocabulary (#6391).** Independently of `body`, the script also reads the candidate's `title` (`gh issue view N --json title`) and checks whether it — after stripping leading whitespace and markdown decoration (heading `#`, emphasis `*`/`_`, blockquote `>`, list marker `-`) — **starts with** one of `Operator:` / `Operator —` / `Operator-only` (case-insensitive). This is a **prefix** match, not a substring-anywhere match — deliberately narrower than the body-phrase discipline: a title that merely *mentions* "operator" mid-sentence ("Fix operator dashboard rendering") must not match, only a title that opens by declaring operator-gating up front. It is checked independently of signal 1 and fires even when the body is empty or matches none of the `PHRASES` vocabulary — the exact gap that let #6391's title-prefixed, unlabeled issue through the confirmation gate unflagged.

**4. Surfacing — inline annotation plus a summary block.** The script emits one tab-separated line per matching candidate per signal (`<N>\t⚠ body declares operator-gating: "<phrase>"`, `<N>\t⚠ depends on #A, which is loom:operator-only` — or `loom:needs-capability`, whichever label `#A` actually carries — or `<N>\t⚠ title declares operator-gating: "<prefix>"`); a candidate matching more than one signal emits one line per signal. Sweep renders those matches two ways in the `all` sentinel's mandatory gate (and the `--dry-run` plan, which shares the same listing format):

- **Inline `⚠` suffix** appended to the matching candidate's own row, after its normal planned action (see "Per-candidate fields"):
  ```
  #87  "Acquire login-walled census index"  labels: loom:issue  → would build  ⚠ body declares operator-gating: "operator-gated"
  #87  "Acquire login-walled census index"  labels: loom:issue  → would build  ⚠ depends on #4, which is loom:operator-only
  #94  "Operator: photograph the 1850 census page"  labels: loom:issue  → would build  ⚠ title declares operator-gating: "Operator:"
  ```
- **A summary `Operator-gate advisory` block** above the wave listing when ≥1 candidate matched (see "Operator-gate advisory block"), one line per match, so the operator sees every flag in one place before scrolling the wave grouping.

The operator reads the annotation and decides — hold, `--depends-on`, manual dispatch, or proceed anyway; the sentinel never decides for them.

**5. Zero matches ⇒ byte-for-byte unchanged.** When no candidate's body matches any phrase, no candidate's title matches a declared prefix, and no candidate declares a `loom:operator-only` or `loom:needs-capability` dependency, the script emits zero lines, no `⚠` suffix and no summary block are printed anywhere, and the candidate-set / `--dry-run` output is identical to a run with this scan absent — no new "found nothing" line, exactly like the "Overlap analysis" and "Detected stacking pairs" blocks on a zero-match run.

**6. `--dry-run` composes.** The `--dry-run` plan ("Issue-set output spec") shows the same `⚠` annotations and summary block the confirmation gate does — this is a **read-only** scan (a body regex, a title-prefix check, plus label reads), so it runs identically whether or not `--dry-run` was supplied; nothing here is gated behind the dry-run/mutation boundary the way the orphaned-claim recovery pass is.

**What this scan does NOT catch (#6197) — a zero-match result is not an all-clear.** The signals above are phrase-based (item 1), label-based (item 2), and title-prefix-based (item 3); all three are good at catching "a human must *do* something first" — supply a credential, provision hardware, grant access, visit a physical location. None can catch **decision-shaped acceptance criteria**, where the gap lives in the *semantics* of the body text rather than in any fixed vocabulary — e.g. "a shortlist agreed from the above rather than all of it — this is a menu, not a backlog" names no operator-gating phrase, no `loom:operator-only`/`loom:needs-capability` dependency, and no `Operator:`-prefixed title, yet only a human can satisfy it; a Builder can either implement everything (violating the criterion) or guess. The scan also cannot catch **credential/verification-shaped criteria** that require a check only a human (or a live secret a Builder worktree doesn't have, e.g. a gitignored `.dev.vars` key) can perform, such as "a **verified** Amazon browse node" — a Builder that cannot verify can still emit a plausible value and mark the criterion done, which is a worse failure mode than a wasted build. Do not extend the `PHRASES` vocabulary to chase this class (see item 1's false-positive rationale — "agreed" or "decide" would false-positive on ordinary prose while still missing the next phrasing); this is a semantic gap, not a missing phrase. The mandatory confirmation gate and the **operator's own judgement** remain the intended backstop for both cases — the same gate this scan's `⚠` annotations feed (see "Mandatory confirmation gate" above) is where a human reads the full candidate body and catches what no scan can. This scan narrowing its own claim composes with, and does not replace, that judgement.

**Scope.** This scan runs only under the `all` sentinel's aggressive candidate survey (`SWEEP_ALL_AGGRESSIVE=true`, issue-set path) — the incident it closes (#5137) is specific to the aggressive whole-backlog taxonomy's hard-skip exclusions. Mode B's curated NL-filtered candidate sets and Mode C's PR-set path do not run it (a PR's own `loom:operator-only` / `loom:needs-capability` label check, C0, is unchanged).

