# Role-Prompt & Operator-Dispatch-Brief Authoring Discipline

Loom ratchets the **size** of its ten role prompts —
[file-size-policy.md](https://github.com/rjwalters/loom/blob/main/.loom/docs/file-size-policy.md)'s
markdown-token freeze (repo-local to the loom source repo),
`check-role-prompt-budget.sh`'s whole-prefix ceiling (#8073) — but has never
had a discipline for whether a prompt (or an ad-hoc operator dispatch brief,
which is a role prompt in everything but name) actually produces the intended
behaviour. We measure the bytes and eyeball the semantics. This document is
the counterpart: how to author or change one so a defect surfaces before
dispatch, not after (#8266).

A role prompt is a program. Treat a change to one the way you would treat a
change to any other program: something that can be wrong in ways review-by-
reading does not catch, and that gets a lightweight verification step before
it ships.

## The discipline

### 1. Pressure-test before shipping

Exercise a non-trivial prompt change against a **realistic scenario that
resists the instruction** — an ambiguous case, a conflicting rule already in
force, a stale premise the new text assumes — not a scenario built to confirm
the change works. A scenario constructed to succeed will always succeed; that
tells you nothing you did not already believe. Concretely: hand the changed
prompt (or brief) to a subagent along with the resisting scenario and read
what it actually does, not what you expect it to do.

### 2. Conflict audit

Every prompt change is checked against the rules it can collide with before
it ships — another role's mandate, a guard hook's tier, a sibling section in
the same file. "New instruction, unchecked against what's already there" is
the exact shape of every incident in "Worked examples" below. The audit is
cheap relative to the failure: grep the other role prompts and the guard-hook
catalog (`defaults/docs/guard-hooks.md`) for the behavior your change touches,
and name what you checked in the PR (see "Verification" below).

### 3. State the failure mode

A new rule records, in the prompt itself or in the PR that adds it, what it is
*preventing*. A rule with a recoverable rationale can be safely trimmed later
if the failure mode it guards against no longer applies (this is exactly how
#7959/#7962/#8092 removed dead weight from the ratchet without guessing); a
rule with no stated rationale is frozen forever by ignorance, because no later
reader can tell whether removing it is safe.

### 4. Load-gate by default

#8073 already ships the mechanism: a role prompt can defer a sibling file to
"load when needed" instead of "always loaded" by giving it a load-gate table
row that is **not** "Always" (see `sweep.md`'s reference file map or
`champion.md`'s "When to Load" column for two live examples). The authoring
default should be gating anything not needed on every single run — treat it
as the normal way to add a section, not a diet applied only once the ratchet
complains. An always-loaded addition is a recurring tax on every dispatch of
that role; a gated one costs nothing until the case that needs it actually
occurs.

### 5. Operator dispatch briefs count

A hand-written brief for a dispatched subagent is a role prompt with no
`defaults/.claude/commands/loom/` file and no ratchet watching it — which is
exactly why it was the weakest link in the incident that motivated this
document (see below). None of the four rules above are conditioned on the
instruction being committed to the repo. Pressure-test a non-trivial brief the
same way, audit it against the role prompt(s) it is layered on top of (a brief
does not replace `judge.md` or `builder.md`, it supplements them — see the
`post-verdict.sh` example below for what happens when a brief silently
contradicts the role prompt underneath it), and state what it assumes.

## Worked examples

These are the incidents from an operator session on 2026-09-16/17 that
surfaced the gap (~25 subagents dispatched across builder, judge, doctor and
curator roles, each driven by a hand-written brief). In every case an
independent, structural check caught the defect — nothing here was caught by
someone simply re-reading the instruction more carefully.

- **The `post-verdict.sh` override.** Three separate judge dispatches were
  briefed to use `gh pr comment` directly. All three overrode the brief and
  ran `post-verdict.sh` instead, because `judge.md` mandates it. This is a
  conflict-audit failure (rule 2): the brief was never checked against the
  role prompt it was layered on top of, and it was wrong three times running.
- **The `im_extract_subst()` false allow.** A Doctor was told to implement a
  prior Judge's suggested fix — reuse `im_extract_subst()` for a new check.
  It pressure-tested the suggestion first (rule 1) and found it would have
  shipped a false allow: `im_extract_subst()` skips single-quoted spans, but
  quote characters have no quoting meaning inside a heredoc body. A second
  Judge confirmed independently by building a one-line variant hook. The
  instruction was confidently wrong; only pressure-testing it caught that.
- **The "Security Scan" scope-limit claim.** A brief asserted that CI's
  "Security Scan" job gated a PR's change. Two judges independently
  established the job is path-filtered to `Cargo.lock` and runs `cargo audit`
  plus CodeQL with no secret scanning — it never applied to that PR at all.
  Nobody had checked the claim against what the job actually does; a
  conflict/premise audit (rule 2) before writing it into the brief would have
  caught it before two people had to re-derive the same negative.
- **The `git ls-files` repro gap.** A reproduction recipe said to `git
  archive` two trees and run the checker against them. It was insufficient:
  `discover_roles()` calls `git ls-files`, so outside a real git index it
  finds zero roles and fails for the wrong reason. A judge caught it and
  fixed the recipe (`git init && git add -A`) before it could waste a second
  reviewer's time on the same false negative — exactly the "pressure-test
  against a resisting scenario" rule 1 asks for: a recipe that has never been
  run against the actual failure mode it claims to reproduce is unverified,
  however plausible it reads.

## What this document is not

This document is **not** self-enforcing. It does not add a check that greps
for a phrase like "we pressure-test our prompts" and treats that phrase's
presence as evidence the discipline is followed — a prose assertion proves
only that someone wrote a sentence, not that anyone ran the pressure test it
describes (#7979). If a mechanical check is ever added for the reference in
"Verification" below, it must grep for the doc's actual presence/reference
(a path, a PR field), never for prose claiming compliance.

## Verification

The way to tell whether this discipline is actually followed, rather than
merely documented, is observable in the next non-trivial change: **a PR (or
commit) that changes a role prompt (`.loom/roles/*.md` /
`defaults/.claude/commands/loom/*.md`) or an operator dispatch brief should
name, in its description or commit message:**

1. The specific pressure-test scenario it was run against, and its outcome.
2. Which existing rules were checked in the conflict audit.

A role-prompt-or-brief change PR carrying neither is observable evidence the
discipline is not wired in — not merely undocumented — and is worth Judge
flagging on sight (see `judge.md` → "Measurable Claims Need Their
Measurement"). This is guidance for what to look for, not a new automated
gate: whether a given change is "non-trivial" enough to need it is a judgment
call, exactly the kind of call a prose-existence check cannot make (see
"What this document is not" above).
