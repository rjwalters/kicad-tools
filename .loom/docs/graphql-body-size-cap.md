# GraphQL issue/PR body-size cap (#6930) — not a rate limit, don't REST-fallback it

`gh issue edit --body`/`--body-file` and `gh pr edit --body`/`--body-file` are
**GraphQL-backed mutations** (`updateIssue`/`updatePullRequest`), and GitHub
enforces a **hard, permanent, size-based** limit on the `body` field: roughly
262144 bytes (256 KiB). A body edit that would exceed the cap is rejected with

```
GraphQL: Body is too long (updateIssue)
```

(or the `updatePullRequest` equivalent). This page is the **single source**
for how to respond to that specific rejection — role prompts link here rather
than repeating the explanation. If the guidance changes, change it here, not
in every role prompt that documents a REST fallback for issue/PR writes.

## This is NOT one of the five rate-limit signatures — do not route it to REST

`sweep.md`'s "GraphQL-exhaustion fallback" ladder and `doctor.md`/`judge.md`'s
"GraphQL Rate-Limit Exhaustion — REST Fallback for Labels/Comments" sections
exist for a **quota** problem: GraphQL's 5000/hr budget is temporarily
exhausted while REST's independent budget still has headroom, so re-issuing
the *same* mutation over REST succeeds. Those sections match failures against
a specific five-signature table (`api rate limit·exceeded` / `api rate
limit·already exceeded` / `secondary rate limit` / `abuse detection mechanism` /
`was submitted too quickly`).

**`Body is too long` does not appear in that table, and must never be treated
as if it did.** It is not a quota signal at all — it is GitHub validating the
payload size, and it fires identically whether GraphQL's rate limit is at
5000/5000 or 0/5000. Re-issuing the same write over REST does not encounter
the "same" rejection reformulated for a different transport — it **succeeds**,
because the REST `PATCH .../issues/{n}` (or `/pulls/{n}`) endpoint does not
enforce the same cap. That makes REST look like a working fallback, but it
isn't one: it just removes a safety backstop GraphQL was providing.

## Why the REST "fix" is worse than the rejection it works around

A rejected `gh issue edit --body-file …` is safe: nothing was written, and the
error is loud and immediate — the caller knows to change approach. Falling
back to `gh api -X PATCH repos/{owner}/{repo}/issues/{n} -F body=@file`
converts that safe rejection into a **silent lost-update race**: the PATCH
endpoint does a blind, unconditional overwrite of the `body` field, with no
concept of "the value I'm replacing is the one I last read." If a second
writer performs its own read-modify-write cycle on the same issue/PR body
before the first writer's PATCH is observed, the second PATCH fully
overwrites the first — discarding an edit that had already returned success —
and neither `gh api` call reports an error. The only way to detect the loss
after the fact is a byte-diff against a saved snapshot.

This was observed in production: `example-org/consumer-repo#404` — a
long-lived, append-only tracking issue in a downstream consumer repo whose body
had accumulated months of maintenance-pass entries — hit the 256 KiB
cap on 2026-08-24, and on 2026-08-25 two concurrently-dispatched `/loom:sweep`
sessions each did their own read-modify-write REST PATCH on the same issue
body within roughly 60-90 seconds of each other. The second PATCH silently
clobbered the first's already-"successful" maintenance-log entry — the live
body settled back near its original size with the first entry gone, and
nothing in either `gh api` call's output indicated a problem.

## The remedy: post the update as a comment, not a body edit

Once an issue/PR body is at or near the cap, stop editing the body for
routine append-only updates (status entries, maintenance-pass logs, "verified
corrections" trackers, etc.) and post them as a comment instead
(`gh issue comment` / `gh pr comment`, or their REST-fallback forms under the
five-signature rate-limit ladder if *that* is what you're separately hitting).
Comments have no equivalent size cap in practice and — critically — an
`issues/{n}/comments` POST **appends** rather than overwriting a shared field,
so it cannot lose a concurrent writer's comment the way a body PATCH can lose
a concurrent writer's body edit. This is exactly the convention
`example-org/consumer-repo#404`'s own maintainers independently arrived at once
they hit the cap, documented only as prose inside that issue's body until this
page existed.

## If a body-edit-via-REST workaround is ever still needed

There is no currently-documented legitimate case for it in this repo's
tooling — the remedy above (switch to a comment) covers every situation this
issue's evidence describes. If a future case genuinely requires writing the
`body` field itself via REST PATCH (not just appending), it MUST NOT be a
blind read-modify-write: read the current body immediately before the write
(no meaningful delay — a stale read reopens the same race) and treat a
mismatch between what you read and what you expect as a signal to abort and
re-read rather than overwrite. A true optimistic-concurrency primitive (e.g. a
conditional update keyed on an ETag or a last-known `updatedAt`) is preferable
to a manually-inserted re-read, but GitHub's REST issues/PRs API does not
expose one today, so the minimum acceptable substitute is the
read-verify-immediately-before-write step just described — never a blind
PATCH built from a snapshot read at the start of a long-running pass.

## Decision: no guard-hook restriction added (#6930)

`defaults/hooks/guard-loom-workflow.sh` already recognizes the `gh api -f
<field>=` shape for other purposes (the `--body @path` literal-string guard,
#5172/#5328). Extending that same recognition to hard-deny or ask on `gh api
-X PATCH .../issues/{n}` (or `/pulls/{n}`) targeting the `body` field was
considered and **deliberately not implemented** in this pass, for two
reasons:

1. **The guard can't distinguish the dangerous case from a legitimate one by
   command text alone.** A REST PATCH of the `body` field is exactly the
   pattern this page describes as acceptable *if* it does the
   read-verify-immediately-before-write step above — but that step happens in
   agent reasoning across multiple tool calls, not inside the single Bash
   invocation a `PreToolUse` guard inspects. A pattern-matching guard sees
   identical command text for the safe and unsafe forms and would have to
   either hard-deny both (blocking the one still-legitimate escape hatch this
   page documents) or ask on both (training agents to routinely dismiss the
   prompt, which is worse than no guard for a rare, high-consequence case).
2. **The actual failure mode is a reflexive fallback decision, not a command
   shape** — this page (and its references from `sweep.md`, `doctor.md`,
   `judge.md`) fixes it at the point the decision is made (which fallback
   ladder applies to which rejection text), which is more precise than a
   blunt command guard applied after the decision was already made.

If body-field REST PATCHes recur despite this documentation (i.e. the
decision-layer fix proves insufficient), revisit adding a narrowly-scoped ASK
guard in a follow-up issue — do not silently re-open this decision inline in
an unrelated PR.
