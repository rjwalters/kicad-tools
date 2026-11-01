# Champion's Ratification Hold — Rationale and History

Reference material for
`defaults/.claude/commands/loom/champion-ratification-hold.md`, which carries the
normative rules and the tick. Nothing here is an instruction (same split as
`critical-file-hold.md`: the prompt is inlined into every Champion session, so
the incident narrative lives here).

## The observed miss (#12992)

In chip repos a target spec or decision record can say it is "ratified on
merge", "ratified by the operator's approval of the PR that lands it", or
"ratified only on human merge approval". Merging such a PR **is** the
ratification. Champion had no rule for it; it held these PRs only when its
merge-risk judgment happened to notice an "authority act".

The 2026-10-20 shadow evaluation of the new Champion (128 PRs) found judgment
can miss it: it would have merged a private design repo's spec PR, a spec the
operator had explicitly ruled not to ratify, because every mechanical criterion
passed. Operator direction (2026-10-20, attended `/fleet:decide`): "ratifying a
spec is likely to stick around as we design chips and I think [it is] best held
for an operator."

Fixtures from that run, all held by the detector when run against their heads:
sky130-dsp-fft#6, sg13g2-noc#6, sky130-droop-detector#8, sky130-lvds-tx#9,
sg13g2-dll#8, gf180-antifuse#61 and #92, gf180-sram#228. Their contents live in
private repos and are not copied; `ratification/tests.rs` carries minimal
reconstructions in the same path layouts.

## Why a mechanical detector, not judgment

The miss was a judgment miss, so the fix cannot be more judgment. The detector
(`loom-daemon champion-hold ratification`) is a pure function of the PR's head
contents: the same head always gives the same answer, and a push that removes
the clause clears the hold on the next tick.

It holds only files the PR adds, modifies or renames on a spec/decision path
**and** whose head content carries a ratification phrase. A bare path match does
not hold (a PR that edits `spec/README.md` without declaring ratification), nor
does a bare `Status: Proposed`. A fenced or quoted example of the phrase still
holds: a false hold costs one operator comment, a false pass ratifies a spec.

## Why it is distinct from the critical-file hold

Same shape (`loom:blocked` + reason-only park record + marker comments, so the
janitor's `loom:pr` + `loom:blocked` exception and `merge-pr.sh`'s #8112 guard
need no change), two deliberate differences:

1. **Never waivable.** `standing_auth_eval` runs inside `merge-risk` and `critical-files` only;
   this hold is evaluated outside it, so no `champion.standingAuthorizations`
   class (any flag combination) applies. The #12950 self-release path skips it
   because it keys on the merge-risk hold's marker, not this one.
2. **Release is a person's comment, not label absence.** The critical-file hold
   treats a hand-removed `loom:blocked` as the release (#9016). Here the
   operator must *act*, so a removed label is re-asserted by the next tick, and
   only a trusted non-bot, non-Champion comment opening with `clear the hold —`
   (the #12599 trusted-person rule; narrower than `merge-risk`'s phrases:
   `merge anyway` / `ok to merge` do not release a ratification) ends it.

A release is scoped to a head. A later push that still declares ratification
re-arms the hold: the text the operator released may not be the text now on the
PR.

## Config

`champion.ratificationPaths` (globs, additive to `spec/**`, `ratification/**`,
`decisions/**`, `docs/decisions/**`, `*decision*.md`) and `champion.ratificationPhrases`
(case-insensitive regexes, additive to the built-ins). Neither can remove a
built-in. An explicit path alone does not hold; the phrase test still applies.
Invalid phrase regexes degrade to literals.

## Rejected

- A new `loom:ratification-hold` label: needs a fleet-wide label and a
  `merge-pr.sh` guard change; the existing marker and label carry the state.
- Relying on merge-risk judgment: that is the observed miss.

Out of scope: judging whether a spec *should* be ratified; changing the
critical-file hold, the kept set or `merge-pr.sh`.
