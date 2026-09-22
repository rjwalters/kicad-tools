# Champion: Idempotency Guard for Unrevised Epics — Maintainer Notes

Editor-facing notes for `champion-epic.md` → "Idempotency Guard for Unrevised
Epics": why the marker keys on a body hash, what each counter counts, the
cycle-by-cycle trace, and the invariants a future edit must preserve. **A running
Champion pass needs nothing here** — the guard's own code and outcome table are
complete without it. Split out so `champion-epic.md` stays under its
markdown-token ratchet (#7725), the same move #7666 made for Step 0.5. Read this
before editing the guard or Step 4.

## Why a hash of title + body, and NOT the epic's `updatedAt`

`updatedAt` is **self-invalidating** — posting the verdict comment bumps it, so
the marker could never match and every pass would re-comment. A title + body hash
changes if and only if the epic is actually edited. Full derivation:
`champion-issue-promo.md` → "Why a body hash and NOT the issue's `updatedAt`
(#4966)".

## The counters, and why the skip must cost something

| Mechanism | Counts | Written by | Survives a silent skip? |
|---|---|---|---|
| `PRIOR_REJECTIONS` | posted `Champion Review: Epic Needs Revision` comments (any revision) | Step 4's reject branch | Yes, but **frozen** while skipping — it cannot advance on its own |
| `SKIP_STREAK` | silent skips recorded for the **current** body hash | the skip path's in-place `PATCH` of the existing verdict comment | **Yes — this is the counter that keeps advancing** |
| `UNREVISED_EVALS` = `PRIOR_REJECTIONS + SKIP_STREAK` | evaluation cycles spent on an unrevised epic | derived | Yes — the single escalation gate, used identically by the skip path and Step 4 |

Suppressing duplicate comments must never suppress the escalation that puts a
stuck epic in front of a human (#4967). Traced against an epic that fails at
body hash H1 and is never revised:

| Cycle | Marker match? | `PRIOR_REJECTIONS` | `SKIP_STREAK` | `UNREVISED_EVALS` | Outcome | Comments posted |
|---|---|---|---|---|---|---|
| 1 | no (H1 unseen) | 0 | 0 | 0 | evaluate → reject → post "Epic Needs Revision" carrying `VERDICT_MARKER` + `epic-unrevised-skips:H1:0` | 1 |
| 2 | yes (H1) | 1 | 0 | 1 < 2 | silent skip; `PATCH` the tally to `1` | 0 |
| 3 | yes (H1) | 1 | 1 | 2 ≥ 2 | `ESCALATE_UNREVISED=yes` → Step 4 escalation → `loom:operator-only` | 1 (escalation) |
| 4+ | — | — | — | — | `ALREADY_ROUTED=yes` drops it from every future pass | 0 |

Invariants a future edit must preserve:

- **Comment budget for an unrevised epic is exactly 2**: one "Epic Needs
  Revision", one escalation. The skip path may only ever *edit* the existing
  verdict comment (`gh api --method PATCH .../issues/comments/<id>` — no
  notification, no new timeline entry), never post.
- **A revision resets `SKIP_STREAK`, not `PRIOR_REJECTIONS`.** A new hash means a
  new marker, so the tally restarts at 0 for the new revision — but the rejection
  count keeps accumulating across revisions, so an epic revised-and-rejected twice
  still escalates on its third cycle. Both paths stay bounded.
- **`ALREADY_ROUTED=yes` short-circuits everything**, and here it is
  unconditional: there is no epic analogue of the #5664 self-healing
  un-escalation, because no epic criterion is a self-clearing dependency finding.
  (#7921 does not change this — Champion never removes the label itself; it only
  declines to re-apply one a human removed.)
- **A human hold ends the pass before any tally (#7734).** `ALREADY_ROUTED` is
  computed from the same three labels 0c's close-branch criterion 3 refuses to
  close past — `loom:operator-only`, `loom:blocked`, `loom:operator` — never from
  `loom:operator-only` alone. `loom:blocked` and `loom:operator` are an operator's
  park; the escalation must never re-apply `loom:operator-only` on top of one.
  Narrowing the set back to a single label reintroduces the incident.
- **An un-park is a ruling on the current revision (#7921).** When
  `loom:operator-only` was removed after this revision's own verdict comment was
  posted, `OPERATOR_RULED=yes` stands the epic down with no tally and no
  escalation. The comparison is against the verdict comment's `created_at` —
  never `updatedAt`, never the escalation comment — so a later revision (new
  hash, new verdict, newer than any old un-park) resumes the ladder on its own
  findings. The `unlabeled` event's actor is not inspected, on purpose: nothing
  automated removes `loom:operator-only` from an epic, and the incident's own
  reverts were bot-authored passes carrying out a human ruling. A structured
  "operator ruling" marker was rejected because it depends on the operator
  remembering to post one; the timeline records what they actually did.
- **That actor-blind read rests on a CROSS-FILE invariant (#7965).** "Nothing
  automated removes `loom:operator-only`" is not a property of this file. It holds
  because `defaults/scripts/classify-dependency-block.sh`'s `check_unescalate()` /
  `check_fact_unescalate()` (and its Rust port) bail `no-escalation-record` unless
  the issue carries a `<!-- champion:proposal-escalated -->` comment — and
  `champion-epic.md`'s Step 4 writes a **different** marker,
  `<!-- champion:epic-escalated -->`. **Relaxing that precondition, or making the
  epic ladder write the proposal marker, turns a bot un-escalation into a "human
  ruling" here.** Belt and braces meanwhile: `OPERATOR_RULED=yes` also requires
  `BOT_UNESCALATABLE=no` (that marker absent from the epic), so an issue carrying
  both `loom:epic` and a proposal label could not stand the ladder down on the
  bot's own label removal. **`champion-issue-promo.md`'s port of this guard
  (#8245) deliberately does NOT copy `BOT_UNESCALATABLE`**: on the proposal path
  `<!-- champion:proposal-escalated -->` is present by construction on every
  escalated proposal, so a presence check would make `OPERATOR_RULED` dead code
  there. It attributes by *time* instead — `UNPARKED_AT` must be newer than
  `BOT_UNESCALATED_AT`, the newest `champion:proposal-unescalated[-facts]:`
  marker comment, which `classify-dependency-block.sh --apply` posts right after
  removing the label. Same invariant, two spellings, because the two paths stand
  in different relations to that one marker.
- **Both unknown inputs fail open (#7965).** `OPERATOR_RULED=yes` also requires a
  non-empty `VERDICT_CREATED_AT`: `PRIOR_REJECTIONS ≥ 1` already proves a verdict
  exists, so an empty read is a failed REST re-read — and `[[ "$UNPARKED_AT" > "" ]]`
  matches *any* historical un-park, standing the ladder down on an API hiccup. A
  missing un-park event and a failed verdict read now both fail the same way.
- **Escalation requires a posted rejection to escalate about (#7666).**
  `PRIOR_REJECTIONS ≥ 1` is a precondition of both the skip tally and the
  escalation; every trace row satisfies it because `SKIP_STREAK` only advances by
  `PATCH`ing a comment Step 4 posted. A match with `PRIOR_REJECTIONS == 0` means
  something else wrote the marker — ignore it; counting it would escalate an epic
  nobody ever rejected. Any future verdict type (a pass, a stand-down, a status
  note) must carry its own marker name and idempotency rule — Step 0.5 is the
  worked example.

These are enforced statically, not by prose:
`defaults/scripts/tests/test-champion-epic-verdict-marker-scope.sh` (#7666,
single-writer rule and Step 0.5 routing),
`test-epic-label-preserved-on-escalation.sh` (#6715, the escalation edit is
add-only), and `test-champion-epic-escalation-respects-human-hold.sh` (#7734 /
#7921 / #7965, the hold labels, the un-park check, and its two fail-open
preconditions). The proposal-side port has its own suite,
`test-champion-issue-promo-escalation-respects-human-hold.sh` (#8245), which
additionally *executes* that prompt's guard block against stubbed forge reads.
