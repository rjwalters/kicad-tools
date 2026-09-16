# Champion: Step 0.5 — Tracking-Umbrella Stand-Down (#7666)

This file is the body of `champion-epic.md` → "Step 0.5". **Read it when that
step tells you to**, after Step 0's Completion-First Check has declined to act
and before Step 1. It answers the one question the 6 structural criteria
cannot: "has this epic already been **decomposed**, by someone other than
Step 3?"

The 6 criteria describe an epic *awaiting decomposition* — they check the
phases, sizing, and success criteria Step 3 needs in order to create Phase 1
issues. An epic whose children already exist and are being worked is past that
point. Those children may come from a **Curator** decomposition pass, native
GitHub sub-issues, or a hand-written `- [ ] #N` task list, none of which carries
the `<!-- loom:epic:$EPIC_NUMBER:phase:N -->` marker that Step 2.75 and
"Detecting Phase Completion" search for — so neither phase creation nor phase
progression can see them, and a literal reading of Step 2.75 would report
`EXISTING_COUNT=0` and create a **duplicate** Phase 1 set. Champion has nothing
left to do for such an epic except watch it finish, which Step 0 already does
on every pass. A passing epic is never a stuck epic: the correct handling is
one durable note and then silence — never a structural re-evaluation, never an
operator escalation.

## 0.5a. Detect the tracking umbrella

**Reuses Step 0a's discovery — do NOT re-run `discover_epic_children`.**

```bash
# Set by Step 0a: EPIC_CHILD_STRONG_{OPEN,CLOSED}, EPIC_CHILD_SOURCES.
# `phase-marker` in EPIC_CHILD_SOURCES means discovery source (a) contributed,
# i.e. Champion decomposed this epic itself and "Phase Progression" owns it
# from here — such an epic must fall through to Step 1 exactly as before.
case ",$EPIC_CHILD_SOURCES," in
  *,phase-marker,*)
    IS_TRACKING_UMBRELLA=no ;;                  # Champion's own decomposition
  *)
    if [ "$EPIC_CHILD_STRONG_OPEN" -gt 0 ]; then
      IS_TRACKING_UMBRELLA=yes                  # decomposed elsewhere, still in flight
    else
      IS_TRACKING_UMBRELLA=no                   # undecomposed (Step 1), or complete (Step 0 owned it)
    fi ;;
esac
```

| Discovery state | `IS_TRACKING_UMBRELLA` | Why |
|---|---|---|
| `EPIC_CHILD_SOURCES` contains `phase-marker` | `no` | Champion decomposed this epic; Step 2.75 + "Phase Progression" already handle it, and standing down here would freeze phase advancement |
| `STRONG_OPEN > 0`, no `phase-marker` source | **`yes`** | Children exist by containment and are still in flight, but Step 3 did not create them — nothing to decompose, nothing to evaluate |
| `STRONG_OPEN == 0` | `no` | Either undecomposed (all counters 0 → Step 1) or a completion candidate, which Step 0 already acted on before this step ran |
| Weak (prose) references only | `no` | Prose is never containment — Step 0's operator ask is the only thing weak evidence may reach |

## 0.5b. Stand down — once per body hash, no counter, no escalation

```bash
if [ "$IS_TRACKING_UMBRELLA" = "yes" ]; then
  # Keyed to the SAME $BODY_HASH the guard computed, and to NOTHING else: a
  # genuine revision (new phases, changed scope) earns one fresh note, while a
  # child changing state does not — "this epic is decomposed and being worked"
  # is what the note says, and that stays true as children move. Distinct
  # marker name; NEVER the reserved rejection marker (see the guard's "Hard
  # constraint" in champion-epic.md).
  UMBRELLA_MARKER="<!-- champion:epic-tracking-umbrella:body-$BODY_HASH -->"
  if printf '%s\n' "$EPIC_JSON" | jq -e --arg m "$UMBRELLA_MARKER" \
       '.comments[] | select(.body | contains($m))' >/dev/null; then
    echo "#$EPIC_NUMBER is a tracking umbrella already noted at body revision $BODY_HASH — standing down silently (no comment, no tally, no label change)"
  else
    gh issue comment "$EPIC_NUMBER" --body "$UMBRELLA_MARKER
**Champion: Epic Already Decomposed — Tracking Only**

This epic's children already exist ($EPIC_CHILD_STRONG_OPEN open / $EPIC_CHILD_STRONG_CLOSED closed, discovered via: $EPIC_CHILD_SOURCES) but were not created by Champion's own phase-issue flow, so it is a **tracking umbrella**, not an epic awaiting decomposition. Champion is not creating phase issues for it and is not re-running the pre-decomposition structural criteria against it.

Nothing is required of anyone. The epic stays open and keeps \`loom:epic\`; Champion re-checks it for completion on every pass and will close it automatically once its last child is closed and every deliverable it names is present on \`${DEFAULT_BRANCH:-main}\`.

---
*Automated by Champion role*"
  fi
  # END THE PASS FOR THIS EPIC either way: do not fall through to Step 1.
  # No label change, no tally, no escalation — continue to the next epic.
fi
```

## 0.5c. Behavioral checks and invariants

| Epic shape | Required outcome |
|---|---|
| Curator-decomposed epic, children open, first pass at this body hash | **One** "Epic Already Decomposed — Tracking Only" comment, then the pass ends |
| Same epic, every later pass with an unchanged body | Silent skip — no comment, no label, no counter. Step 0 still runs first, so it closes on its own when the last child lands |
| Same epic, body genuinely edited (new phases/scope) | New hash → one fresh stand-down note (still no evaluation, still no escalation) |
| Same epic, carrying an old, superseded "Epic Needs Revision" marker | Stand-down still wins: Step 0.5 runs **before** the guard's skip/escalate branches commit, so a rejected-then-decomposed epic never escalates on a stale finding |
| Champion-decomposed epic (phase-marker children) | Step 0.5 declines → Step 1 → unchanged Step 2/2.5/2.75/Phase Progression behavior |
| Undecomposed epic (no children) | Step 0.5 declines → Step 1 → Step 2's 6 criteria, byte-for-byte the pre-existing behavior |

**Invariants a future edit must preserve:**

- **A stand-down is never a rejection.** This step writes
  `champion:epic-tracking-umbrella:body-*` and nothing else. It must never write
  the Step 4 rejection marker, never touch `PRIOR_REJECTIONS` / `SKIP_STREAK`,
  and never apply `loom:operator-only` — a passing epic waiting on its own
  children is not an operator problem.
- **No escalation ladder, deliberately.** Same reasoning as "Phase
  Progression"'s own idempotency guard: an unchanged, in-progress epic is not
  stuck, it is *waiting*. Bounding a state that resolves itself when the last
  child closes would only reintroduce the noise this step removes.
- **Step 0 still runs on every pass.** The stand-down suppresses the *structural
  evaluation*, never the completion check — that is what lets the epic close
  automatically instead of sitting open forever.
- **The `phase-marker` carve-out is load-bearing.** Widening this step to fire
  on epics Champion decomposed itself would freeze phase progression at whatever
  phase was open when the stand-down first fired.

`defaults/scripts/tests/test-champion-epic-verdict-marker-scope.sh` checks this
file statically: it must define its own marker, dedupe on `BODY_HASH`, and
carry no escalation counter and no operator-only label application.

---

Return to `champion-epic.md` and continue with the next epic (the pass for this
one is over), or — if `IS_TRACKING_UMBRELLA=no` — with Step 1.
