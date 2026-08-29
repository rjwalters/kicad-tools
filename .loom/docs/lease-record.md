# Lease Record Format (Epic #6165, Phase 1: #6179)

Epic #6165 gives the `loom:building` claim a liveness dimension — a
**lease**. `loom:building` on its own only says "someone claimed this issue
at some point"; it carries no signal about whether that someone is still
alive and working, or crashed/hung hours ago. The lease record is the
missing liveness signal, layered on top of the existing label claim without
changing what the label itself means.

This document defines the record's on-forge shape. It is **write-only**:
this phase (#6179) writes the record at dispatch time and nothing else. No
reclamation or dispatch-decision logic reads it back yet — that is Phase 2
of the epic, a future issue. Phase 3 (fencing) is the phase after that. Both
are expected to consume this exact format without re-deriving it.

The sibling issue #6180 (`defaults/docs/lease-renewal.md`) implements the
other half: a sweep-owned background loop that keeps a lease fresh for the
lifetime of the sweep holding the claim, reusing the identical marker shape
documented here.

## What a lease record is

A lease record is an ordinary issue (or PR) **comment**, posted on the
number a dispatch just claimed, at the moment `loom-daemon`'s dispatch path
successfully flips that issue's label from `loom:issue` to `loom:building`.
It follows the same HTML-comment-marker idiom already used elsewhere in this
repo — `<!-- loom:standdown claim=… -->` (peer-claim standdown),
`<!-- champion:hold-state head=… -->` (Champion's merge-risk hold) — so it
is grep/dedup-detectable the same way those markers are, without needing a
dedicated forge field.

### Shape

The comment body's literal **first line** is the marker:

```
<!-- loom:lease host=<host-id> sweep=<sweep-id> -->
```

- `<host-id>` — **an opaque id by default (Issue #6322), not a raw
  hostname.** A lease record is an ordinary issue/PR comment: on a *public*
  repo it is permanent, world-readable, public record, and a raw machine
  name commonly embeds a person's name — data this mechanism has no business
  publishing just because a claim happened to land there. So
  `SweepRegistry::published_host_id` (`guards.rs`) publishes
  `opaque_host_id(host_identity())` — `host-` followed by the first 8
  lowercase hex chars of `sha256("loom-lease-host-id-v1:" + host_identity())`
  — instead of the raw `sweep_registry::host_identity()` value
  (`LOOM_HOST_ID` env > `$HOSTNAME` > the `hostname` binary >
  `unknown-host`).
  - **This id is still directly `==`-comparable** across every reader in
    this subsystem — [claim-then-verify-order](#phase-2-dispatch-time-half-claim-then-verify-order-6287)
    (`resolve_lease_order`) and sweep-side [fencing](#phase-3-issue-6309-has-now-shipped-sweep-side-fencing-before-pushpr-open)
    both only ever need equality, never readability, and every writer/reader
    in this repo (`guards.rs`'s `published_host_id`,
    `sweep-lease-fence.sh`'s `opaque_host_id`/`resolve_published_host`)
    derives the exact same value from the exact same salt, so "recognize my
    own claim" keeps working unchanged.
  - **It is NOT directly comparable** to the raw host identity used by
    peer-claim advertisements (#4028) or cross-host collision-detection log
    lines (#4085) — those are internal channels (the safehouse room, this
    daemon's own log file), not a public forge comment, so they are
    unaffected by #6322 and remain raw.
  - **Operator resolution.** An id is resolved back to a hostname locally by
    recomputing the same function against a candidate hostname and comparing
    — there is no reverse lookup, by design (a salted SHA-256 truncated to 8
    hex chars is not meant to be inverted). `loom-daemon` also logs the
    mapping once per process (`sweep_registry: loom:lease forge comments
    publish the opaque id …`) at `info` level the first time it publishes
    one, so grepping this host's own daemon log is the fastest path.
  - **Escape hatch.** `LOOM_LEASE_PUBLISH_HOSTNAME=1` (`true`/`yes`/`on`,
    case-insensitive) restores the pre-#6322 raw-hostname publishing
    behavior for anyone who prefers a readable name on a private tracker.
    Env-only, deliberately — there is no `.loom/config.json` key, because the
    shell-side fencing check (`sweep-lease-fence.sh`) has no access to the
    daemon's own config resolution and must derive the identical answer the
    Rust writer did; env is the only source both sides can agree on without
    risking a silent writer/reader mismatch.
- `<sweep-id>` — the dispatching sweep's own `SweepId`
  (`generate_sweep_id`'s output), the same identifier the daemon's registry,
  logs, and outcome journal already key sweeps by.

Everything **after** the marker's closing `-->` is free-form, human-readable
prose (who claimed it, when, and pointers to this doc and the renewal doc).
Machine readers — present and future — must locate the record via
`.starts_with("<!-- loom:lease host=")` only, and must **never** parse or
depend on anything in the prose that follows.

### The liveness signal is the comment's `updated_at`, not embedded text

This is the load-bearing design decision, so it is worth stating plainly:
**a reader determines freshness from the comment's own forge-assigned
`updated_at` timestamp — never from a timestamp written into the marker or
prose text.**

This differs deliberately from `peer_claims.rs`'s existing TTL approach,
which timestamps a claim at local receipt and corrects for clock skew
between hosts because there is no shared clock in that channel. A forge
comment does not have that problem: every host reads the *same* `updated_at`
value, assigned by the forge server itself, for the same comment. Using it
as the sole liveness signal gives every host a shared clock for free, with
no skew-correction logic needed — a reader (Phase 2) just compares
"now minus this comment's `updated_at`" against a threshold.

This is also why the marker's first line is written once and never rewritten
byte-for-byte identical on renewal — see `lease-renewal.md` for why an
idempotent PATCH still needs to change *something* in the body for a forge
to reliably advance `updated_at`.

### Example

```
<!-- loom:lease host=host-a3f9c1d2 sweep=sweep-2026-08-13T23-01-04Z-a1b2c3 -->
This issue's `loom:building` claim was acquired by sweep
`sweep-2026-08-13T23-01-04Z-a1b2c3` on host `host-a3f9c1d2` at
2026-08-13T23:01:04Z. This comment is a lease record (Issue #6179, Epic
#6165) — its liveness signal is this comment's own forge-assigned
`updated_at`, never a timestamp embedded in this text. See
`defaults/docs/lease-record.md` for the format contract this establishes,
and `defaults/docs/lease-renewal.md` for how the owning sweep keeps it
fresh for the lifetime of its claim. Nothing reads this record yet
(write-only, Phase 1) — a future phase will use it to decide reclamation
of an abandoned claim.
```

(`host-a3f9c1d2` is an opaque id, not a hostname — see "Shape" above.
Earlier examples pre-dating Issue #6322 showed a raw hostname like
`studio-host` here; existing already-published comments on the forge are not
rewritten, so both shapes can be found in the wild — a reader must treat
`host=` as an opaque token to compare, never assume either shape.)

The embedded `at=...` timestamp in that prose is for human debugging only —
it is what the dispatcher *believed* the time was when it wrote the comment,
not an authoritative value any reader may rely on.

## When it is written

`loom-daemon`'s `SweepRegistry::dispatch_inner` (in
`loom-daemon/src/sweep_registry/dispatch.rs`) writes the lease record
immediately after a **confirmed successful** `flip_label_to_building` call —
never before, and never when the flip itself failed or was skipped (e.g.
`skip_label_flip` test fixtures). No claim, no lease: a lease record only
ever exists for an issue this host actually just flipped to
`loom:building`.

The write itself (`SweepRegistry::write_lease_comment` in
`loom-daemon/src/sweep_registry/guards.rs`) is **best-effort and fail-open**,
matching every other forge mutation on the dispatch path (`gh` calls
throughout `guards.rs`/`watchdog.rs`): a failed or timed-out `gh issue
comment` only logs a warning and never fails, retries, or unwinds the
dispatch. The claim (`loom:building`) is authoritative regardless of whether
its lease record made it onto the forge — a lost lease comment degrades a
future reclamation decision's evidence, not the claim's own validity.

## What this phase explicitly does not do

- **No reading.** Nothing in `loom-daemon`'s reclamation or dispatch-decision
  path parses, locates, or reasons about lease comments in this phase. This
  is a pure addition with zero behavior change to any existing decision.
- **No renewal from the daemon.** The daemon writes exactly one lease
  comment per successful dispatch and never touches it again. Keeping a
  lease fresh for the sweep's entire runtime is the sweep-owned renewal loop
  documented separately in `lease-renewal.md` (#6180) — the daemon process
  that dispatched a sweep routinely does not outlive it (#6129), so daemon-
  owned renewal would be the wrong owner.
- **No reclamation or fencing logic.** Deciding what to do with a lease that
  has gone stale (Phase 2) and bounding the cost of the underlying
  acquisition race #4028 describes (Phase 3) are both out of scope here.

## For Phase 2 (reclamation) and Phase 3 (fencing)

A reader should:

1. Locate the most recent comment on a `loom:building` issue whose body
   starts with `<!-- loom:lease host=`.
2. Parse `host=` and `sweep=` out of that first line only (a simple prefix
   strip + space-split is sufficient — the format is intentionally flat,
   not a general key-value grammar).
3. Use the comment's own `updated_at` (not any embedded timestamp) as the
   freshness signal, compared against whatever staleness threshold that
   phase defines.
4. Treat an issue with `loom:building` but **no** lease comment as a claim
   predating this feature (or one whose lease write failed) — not evidence
   of anything either way; Phase 2 must define its own fallback for that
   case rather than assuming absence means abandonment.

**Phase 2 (Issue #6286) has now shipped this contract.**
`loom-daemon`'s `claim_reconciliation::forge::fetch_freshest_lease_updated_at`
(the periodic/startup reconciliation pass,
`claim_reconciliation::forge::reconcile_workspace`) and
`worktree_ops::gh::freshest_lease_updated_at` (the `recover-orphans` CLI's
`check_untracked_building`) both implement exactly the four steps above —
locate via `LEASE_MARKER_PREFIX`, freshness from the REST comments
endpoint's `updated_at` only, TTL = 3x the ~5-minute renewal interval (15
minutes, `claim_reconciliation::resolve_lease_ttl_minutes`), and a missing
lease comment falls through to whatever the pre-existing host-scoped
evidence (journal / run-registry / label-age) already decided. Both call
sites consult the lease as the LAST gate, immediately before a reclaim would
otherwise fire — see `claim_reconciliation.rs`'s "Lease-record freshness"
section and its top-of-file doc comment for the full before/after picture.
**Phase 4 (Issue #6317) then removed the peer-claim-coordination-degraded
gate (Issue #6157) that used to run alongside this one** — the lease is now
the sole fleet-scoped reclamation gate; `reconcile_workspace` no longer
accepts (or needs) any injected peer-coordination evidence.

See also: [`lease-renewal.md`](lease-renewal.md) for the renewal mechanism
this format was co-designed with, and
[`lease-renewal-measurement.md`](lease-renewal-measurement.md) for the
write-volume measurement methodology and a projected (not yet measured)
estimate against this design's rate-limit headroom (#6181).

## Phase 2, dispatch-time half: claim-then-verify-order (#6287)

Issue #6287 implements one half of Phase 2 — the operator-directed
claim-then-verify-order dedup at dispatch time (2026-08-15), landed
alongside the reclamation-guard half (#6286). It follows this doc's own
reader recipe above with one refinement: rather than locating only the
*most recent* lease comment, `SweepRegistry::read_lease_comments`
(`loom-daemon/src/sweep_registry/guards.rs`) reads back **every** live
lease comment on the issue via `gh api .../issues/N/comments`, and
`SweepRegistry::resolve_lease_order` compares their forge-assigned comment
`id`s (never a locally-recorded timestamp) to decide whether *this*
dispatcher's own comment is the earliest. A dispatcher that loses — a peer's
lease comment has an earlier `id` — yields before spawning a builder or
touching a worktree: it retracts its own peer-claim advertisement, releases
its own claim lock, and posts a `<!-- loom:lease-yield ... -->` standdown
annotation, but deliberately leaves the shared `loom:building` label alone
(it is already correct — idempotent across both racing flips, and reverting
it would destroy the winning claimant's only cross-host mutex out from under
its still-live sweep). The comparison is bounded to comments created within
a short lookback window of the dispatch attempt's own pre-flip instant
(`LEASE_ORDER_LOOKBACK_SECS`), so a long-completed prior claim's lease
comment — an issue accumulates one per dispatch over its whole lifetime,
never deleted — can never out-rank a normal, uncontested re-dispatch.

## Phase 3 (Issue #6309) has now shipped: sweep-side fencing before push/PR-open

Phase 2 (above) is the *daemon's* reclamation-side check; Phase 3 is the
*sweep's own*, symmetric check — fencing, not reclamation. The sweep checks
its own lease, never the daemon, for the identical reason Phase 1's renewal
loop is sweep-owned: role agents routinely outlive the daemon that spawned
them (#6129), so only the sweep itself, at the moment of action, can know
whether it is still the intended owner.

`defaults/scripts/sweep-lease-fence.sh check <issue>` implements this doc's
reader recipe from a shell/orchestration context (rather than
`loom-daemon`'s Rust): it fetches every lease-marker comment on `<issue>` via
the REST comments endpoint (NDJSON output across `--paginate` pages, the same
#4637 workaround `SweepRegistry::read_lease_comments` uses), locally picks
the one with the freshest `updated_at`, and confirms BOTH (a) that comment is
still within `ttl_minutes` of now (default 15, same TTL Phase 2 uses) and (b)
its `host=` field still names this sweep's own host. It is wired into the
Builder phase immediately before `git push` + opening the PR
(`defaults/roles/builder-pr.md` § "Lease Fencing: Confirm You Still Own the
Claim") — on either failure (expired AND owned by this sweep's own host,
exit `3`; superseded by a *fresh* lease from a different host, exit `4`) the
Builder aborts before doing anything externally-visible, without touching
the `loom:building` label or contesting the peer's claim. Absence of a
matching lease comment, a malformed marker, a `gh` fetch failure, or an
EXPIRED lease owned by a *different* host all fail OPEN (exit `0`, proceed)
— this doc's own "no lease comment == no evidence either way" contract,
applied identically to this new reader.

**Issue #6783: an abandoned, never-yielded lease no longer fences forever.**
Prior to #6783, `check` aborted with exit `3` (EXPIRED) whenever the
*freshest* lease comment was stale, regardless of which host wrote it — but
EXPIRED is an abort, not a pass, so a dead sweep's lease record (never
renewed again, never yielded) never stopped being the freshest comment on
the issue and permanently fenced out every future dispatch that did not
itself write a lease (observed on issue #6694 / PR #6773: a dead sweep's
lease from a different, dead host blocked a legitimate successor sweep with
zero live competitors). An expired lease is, by definition, not evidence of
a *live* peer — closer to "no evidence" (this doc's own reader contract)
than to "a peer owns this". `check` now distinguishes the two cases: expired
**and** owned by *this* sweep's own host still aborts (exit `3` — a sweep
whose own renewal loop died should not trust its claim), while expired **and**
owned by a *different* host now PASSes (exit `0`) as no-longer-live evidence.
Exit `4` (SUPERSEDED — a different, *fresh* lease from a live peer) is
unaffected.

## Yield/renewal/fence coordination gap, closed (Issue #6485)

Phase 1 (renewal), Phase 2 (yield), and Phase 3 (fencing) above were each
implemented and tested independently, and a real incident (#6470, 2026-08-18)
showed they were never actually cross-wired: a dispatcher that lost the
Phase 2 claim-then-verify-order tie-break and posted its own
`<!-- loom:lease-yield ... -->` standdown record still had a lease comment
that kept looking freshly renewed, while the tie-break WINNER's own lease
comment never advanced — so Phase 3's `sweep-lease-fence.sh check`, which
only ever compared the single freshest `updated_at` across all lease
comments with no notion of yield status, fenced the winner out of its own
push/PR-open (`ABORT: SUPERSEDED`) even though it was the host that actually
did the work.

**Root cause, more precisely than "the loser kept renewing its own lease".**
Reading `loom-daemon/src/sweep_registry/dispatch.rs`'s dispatch flow shows a
losing tie-break dispatcher returns *before* spawning a builder or entering
`sweep.md`'s Step 1a (where `sweep-lease-renew.sh start` is invoked) — so in
the ordinary single-dispatch-attempt path, a yielded dispatcher never starts
a renewal loop of its own at all. What was actually happening in the
#6470 incident: `sweep-lease-renew.sh start`, invoked with no
`--host`/`--sweep-id`, uses `renew-once`'s "newest wins" fallback — it
PATCHes whichever lease comment on the issue currently has the highest
comment `id`, regardless of which host posted it. When a peer's dispatch
posted a lease comment with a higher `id` shortly after this sweep's own
(the exact shape of a near-simultaneous acquisition race), THIS sweep's own
renewal loop silently started renewing the PEER's comment on every cycle
instead of its own — so the winner's own lease never advanced, while the
peer's (soon-to-be-yielded) lease kept looking fresh, renewed by the
winner's own, correctly-running loop.

Both `sweep-lease-renew.sh` and `sweep-lease-fence.sh` were fixed together
(defense in depth):

- **`sweep-lease-renew.sh`**: `start` now defaults to resolving its OWN
  `--host`/`--sweep-id` (from `$LOOM_HOST_ID`'s opaque form and
  `$LOOM_TERMINAL_ID`'s `daemon-<sweep-id>` shape) whenever both can be
  resolved, so `renew-once` uses exact-match targeting instead of "newest
  wins" by default — closing the actual misdirection observed above. As a
  second, independent layer, `renew-once` also refuses to PATCH ANY
  candidate lease (whether selected by exact match or by "newest wins")
  whose own `(host, sweep)` has a matching `loom:lease-yield` record on the
  same issue (a new exit code, `4`), and `start`'s loop stops renewing
  immediately once a cycle reports that outcome, rather than waiting for its
  watched PID to die.
- **`sweep-lease-fence.sh`**: `check` now excludes, from the "freshest
  lease" candidate pool, any lease comment whose own `(host, sweep)` has a
  LATER `loom:lease-yield` record on the same issue — matched by the exact
  `(host, sweep)` pair, not by host alone, so a host that legitimately
  re-claims the same issue later (a brand new lease comment, a different
  `sweep=`) is never excluded by an unrelated, older yield from a past claim
  episode. A lease that was never yielded still ages out purely through the
  ordinary TTL/EXPIRED path (see "Issue #6783" above for what "ages out"
  now means: ABORT only for this sweep's own expired lease, PASS for a
  different host's) — this does not weaken that path.

With either fix alone the #6470 incident's fence failure would not have
recurred; both are applied together because the fence-side fix is the
correctness backstop (defends even if a future code path re-introduces a
misdirected or intentionally-left-running renewal loop) and the renew-side
fix addresses the actual mechanism observed in the incident.
