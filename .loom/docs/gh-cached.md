# `gh-cached` — short-TTL caching for hot forge reads

`defaults/scripts/gh-cached` (installed as `.loom/scripts/gh-cached`) is a
drop-in `gh` wrapper that caches read-only responses in a file-backed TTL/LRU
cache under `/tmp/gh-cache/`. Because the cache is on disk, it is shared
**across processes on one host** — the N sweep / Judge / Champion sessions
running concurrently against the same repo hit the same entries.

The cache directory is further scoped **per repo** (#5224): the wrapper
resolves `git rev-parse --show-toplevel` once per invocation (cheap, local, no
network) and hashes it into a subdirectory of `GH_CACHE_DIR`, so two different
repos on the same host issuing the textually identical `gh` invocation never
read (or invalidate) each other's cached entries — the sessions sharing
entries above are always sessions against the *same* repo. `GH_CACHE_REPO_ID`
overrides the resolved identity directly when needed (tests, or a working
directory that isn't a git repo).

This document is the policy reference for **which** reads in the
`/loom:sweep`, `/loom:judge`, and Champion-PR-merge skills go through the
wrapper and which are deliberately left uncached (#4667). The motivating
problem is #4665: concurrent sessions share **one** personal `gh` rate-limit
budget, and every session independently re-polling the same PR/issue listings
burns that shared budget on identical answers.

## Interface

```bash
# Resolve the wrapper once per session; fall back to plain `gh` when it is
# absent or its Python runtime is broken (the same probe merge-pr.sh uses).
GH_READ="gh"
_ghc="$(git rev-parse --show-toplevel 2>/dev/null)/.loom/scripts/gh-cached"
if [[ -x "$_ghc" ]] && "$_ghc" --version >/dev/null 2>&1; then GH_READ="$_ghc"; fi

"$GH_READ" pr list --label "loom:review-requested" --state open --limit 500
"$GH_READ" --no-cache pr view 4560 --json labels    # explicit bypass
"$GH_READ" --cache-stats                            # hits/misses/bypasses/invalidations
"$GH_READ" --clear-cache                            # drop every entry
```

The fallback matters: on a host without the wrapper (or with a broken
`python3`), every documented `"$GH_READ" …` command degrades to the exact
`gh …` command it replaced. **Nothing in the skills depends on the cache
existing** — it is a budget optimization, never a correctness mechanism.

`--no-cache` is a *wrapper* flag. Plain `gh` rejects it (#3547), which is why
the carve-outs below are written as "run plain `gh`" rather than
"run `$GH_READ --no-cache`" — the plain form is correct under either
resolution.

### What it caches

| Command shape | Behavior | TTL |
|---|---|---|
| `gh issue view` | cached | 30s |
| `gh pr view` | cached | 30s |
| `gh issue list` / `pr list` (cacheable shape) | **ETag/REST (#5056)** — free 304, never stale; TTL fallback | 0s (revalidated) |
| `gh label list`, `gh <x> search/status` | cached | 30s (default) |
| `gh api` **without** `-X <non-GET>` / `-f` | cached | 30s |
| `gh … edit/create/delete/close/reopen/merge/review/comment/label` | **bypassed**, and invalidates (but issue writes as literal `gh` — see below) | — |
| `gh pr checks`, `gh pr diff`, `gh repo view`, `gh auth …` | **passthrough**, never cached | — |

Only **successful** (`rc == 0`) responses are cached, so a transient forge
error is never memoized. TTL knobs: `GH_CACHE_TTL` (default 30s),
`GH_CACHE_MAX_SIZE` (256 entries, LRU), `GH_CACHE_DIR`, `GH_CACHE_DISABLE=1`
(hard off), `GH_CACHE_DEBUG=1` (per-call HIT/MISS/INVALIDATE lines on stderr).

### ETag/REST cached listings — free, never-stale `issue list` / `pr list` (#5056)

`gh issue list` / `gh pr list` are **GraphQL**, which has no conditional-request
mechanism, so every call burns the shared GraphQL rate-limit pool even when the
queue is unchanged. (Measured 2026-08-03: `graphql` at 1378/5000 in ~16 minutes
while REST `core` sat at 19/5000 — `gh issue create` was already failing while
the REST pool was 99.6% idle.) The daemon's own polling loops avoided this via
`forge_listing::list_issues_cached` — a REST `GET` with `If-None-Match` that a
matching ETag answers with a **304 at zero rate-limit cost** — but agents had no
access to it.

The wrapper now closes that gap. For an `issue list` / `pr list` whose shape the
REST issues endpoint can serve, it first tries loom-daemon's **disk-persistent**
ETag cache (`loom-daemon forge <issue|pr> list --cached …`, backed by
`forge_listing::list_issues_cached_persistent`):

- **Free on repeat.** A validated `304` costs zero rate-limit units, so the
  second and later readers on a host pay nothing when the queue is unchanged.
  The ETag + last-good body persist on disk (`${TMPDIR:-/tmp}/loom-forge-listing-cache`,
  override `LOOM_LISTING_CACHE_DIR`), so this holds across the *separate*
  short-lived agent processes — not just within one long-running daemon.
- **Separate pool.** It draws on REST `core`, not the exhausted GraphQL pool.
- **Never stale.** Unlike the 30s TTL cache below, a `304` is positive proof
  nothing changed — so this path is safe even for claim-arbitration reads, and
  is tried *before* the TTL cache.
- **Degrades gracefully.** When loom-daemon is unreachable (binary absent) or
  the shape is not cacheable, the daemon exits non-zero and the wrapper falls
  through to its normal path (TTL cache / plain `gh`) — the same fallback
  contract as the rest of this wrapper. `LOOM_ETAG_LIST_DISABLE=1` turns the
  whole layer off.

**Which shapes route here** (everything else declines to `gh`, so a repoint is
always safe): a `list` with `--json` limited to
`{number,title,state,body,labels,createdAt,updatedAt,closedAt,author}`; `--label`
(AND) and `--search` restricted to `label:` / `-label:` include/exclude terms;
`--state open|closed|all` for issues, `--state open` for PRs; not a
possibly-truncated full page (>= 100 rows). A bare `list` (human table, no
`--json`), a freeform `--search` (`head:…`, `in:body`, text), a PR-only field
(`mergedAt`, `files`), or a merged/closed PR listing all fall back to `gh`.

> **`loom-daemon forge issue` / `forge pr` WITHOUT `--cached` is NOT this path.**
> The bare passthrough is a byte-identical GraphQL exec of `gh` and inherits its
> full GraphQL cost. Only the explicit `--cached` flag (reached for you by this
> wrapper) uses the ETag/REST cache. Never reach for the bare passthrough
> expecting caching.

### Mutation-triggered invalidation — and why writes still use plain `gh`

On a successful mutation issued *through the wrapper*, it deletes every cached
entry that references the mutated resource — matching first on the cached
command's own argv, then falling back to a substring match on the cached
response body (so a cached `pr list` containing `"number": 4560` is dropped
when PR 4560 is edited). When it cannot identify a resource id, it clears the
whole cache.

**Skills must nevertheless keep issuing their writes as literal `gh …`, not
`"$GH_READ" …`.** The destructive-command guard hooks pattern-match the
*literal* command text — e.g. `guard-destructive-generic.sh`'s
`gh[[:space:]]+(pr|issue)[[:space:]]+comment` rule that hard-denies
`--body @path` (the anti-pattern that destroyed a Judge review in PR #4457,
and recurred through variable indirection on PR #4600). A wrapped
`"$GH_READ" pr comment …` does not match those patterns and would slip past
the guard. Trading a *safety* guard for a *budget* optimization is never the
right trade.

**So close the loop explicitly instead:** after a mutation you made in this
pass (verdict label, comment, merge), drop the cache before your next cached
read:

```bash
gh pr edit "$N" --add-label "loom:pr"     # literal `gh` — guard hooks see it
"$GH_READ" --clear-cache                  # local fs op, zero API cost
```

`--clear-cache` is a `/tmp` directory sweep — it costs no API calls, and the
worst case for a concurrent session is one extra miss. This is what guarantees
a skill never reads its own pre-write state back out of the cache (e.g. a
Judge that labels `loom:pr` and then re-lists `loom:review-requested`).
Skipping it bounds the staleness at one TTL (≤30s) rather than being
unbounded, but do not rely on that — clear after your own writes.

## The policy: cache observation, never arbitration

Route a read through `$GH_READ` when it is a **repeated observation** whose
worst case under 30s of staleness is a wasted pass or a re-poll:

- broad candidate/queue discovery (`gh issue list`, `gh pr list`, `gh label list`)
- read-only surveys of a candidate set that no decision mutates (dry-run plans)
- idempotency-marker greps over a PR's comments

Keep a read on plain `gh` (uncached) when a stale answer can cause an
**incorrect irreversible action** — a duplicate builder, a stomped verdict, a
merge that should not have happened, or a test that observes its own stale
"before" value:

- **claim arbitration** — reads that decide "is someone else already working
  on this?" (`loom:building` / `loom:reviewing` label reads, claim timelines).
  A 30s-stale label is exactly the window a competing claim lands in.
- **verdict-time CAS rechecks** — the whole point is to observe writes that
  landed *during* your review; a cached label set defeats the mechanism.
- **merge gating** — the 6 Champion safety criteria, `mergeStateStatus` /
  `mergeable`, `gh pr checks`, and the paginated changed-file list (#4613).
  These are the last read before an irreversible action.
- **liveness probes** — the Judge's `gh repo view` environment check must
  observe the live environment, not a cached success from a healthy session.
- **before/after differential checks** — e.g. sweep's `--dry-run`
  "nothing mutated" verification, which runs the *identical* command twice
  around the operation under test. A cache hit would return the "before"
  value and make the check vacuously pass.

`gh pr checks` and `gh repo view` are passthrough in the wrapper already, so
those carve-outs hold even if a caller wraps them by accident. The rest are
enforced by the skills documenting the plain `gh` form at those call sites.

## Per-skill call-site inventory

### `/loom:sweep` (`sweep.md`)

| Cached | Uncached (and why) |
|---|---|
| Mode B/C candidate resolution (`gh issue list` / `gh pr list` translations, `all`-sentinel backlog query) | Per-issue pre-flight state/label read + existing-PR probe (timeline + `gh pr view`) — **claim arbitration** |
| The one-per-invocation `gh label list --limit 200` token-validation query | Step 5 checkpoint-divergence label recheck — must see a concurrent process's verdict |
| `--dry-run` Stage 0 per-candidate surveys (`gh issue view` / `gh pr view`) | Step 7 overlap probe (`--json files`) and `mergeStateStatus` recheck — **merge gating** |
| | `--dry-run` "nothing mutates" before/after reads — **differential check** |

### `/loom:judge` (`judge.md`)

| Cached | Uncached (and why) |
|---|---|
| `gh pr list --label loom:review-requested` queue discovery (every occurrence) | Pre-Iteration Environment Check (`gh repo view`) — **liveness probe** |
| Fallback-queue unlabeled-PR listing | Stale `loom:reviewing` claim check (timeline + comments) — **claim arbitration** |
| `gh issue list --search` when repairing a PR description | Verdict-Time CAS Recheck (`gh pr view --json labels`) — **CAS** |
| | `gh pr checks` + `mergeStateStatus` before a verdict — **verdict gating** |

### Champion PR merge (`champion-pr-merge.md`)

| Cached | Uncached (and why) |
|---|---|
| Idempotency-marker comment greps (janitor / hold / stale / park / close markers, prior grants) | All 6 safety criteria reads — **merge gating** |
| `gh issue list --label loom:blocked` unblock scan and its per-issue **body** reads | Verdict-State Janitor label read — decides whether to merge |
| Follow-on-issue duplicate search (`gh issue list --search`) | Paginated changed-file list (`gh api .../files --paginate`) — #4613 demands a fresh full read |
| Parked-PR listing (`gh pr list --label …`) | Pre-merge comment's data gathering — must not restate a stale criterion result |
| | Post-merge linked-issue **state** reads and the dependency-`state` loop — they gate `gh issue close` / removing `loom:blocked` |

## Verification

### Automated

`defaults/scripts/tests/test-gh-cached.sh` (CI-wired) drives the wrapper with a
stub `gh` on `PATH` and a temp `GH_CACHE_DIR`, asserting: repeated `pr list` /
`issue list` / `pr view` reads hit the cache; `--no-cache` bypasses; `gh pr
checks` and `gh repo view` are never cached; a wrapped `gh pr edit` /
`gh pr comment` invalidates the cached reads of that PR (including the cached
listing that contains it); a state change is observed once the TTL expires;
and (#5224) two different repo identities — both via `GH_CACHE_REPO_ID` and
via two real, distinct git toplevels — never share a cache read or an
invalidation for the textually identical `gh` args, even when both repos have
a numerically-identical resource id.

### Manual (inherently runtime — cannot be unit-tested)

1. **Hit rate.** `"$GH_READ" --clear-cache`, run a Judge or sweep pass, then
   `"$GH_READ" --cache-stats`. Expect a non-zero hit count with the misses
   concentrated on first-touch reads. Repeating the same pass within 30s
   should be nearly all hits. `GH_CACHE_DEBUG=1` prints per-call
   `HIT`/`MISS`/`EXPIRED`/`INVALIDATE` lines if you need the call-by-call view.
2. **State change within one TTL.** With a PR whose CI is pending, poll the
   cached discovery read every few seconds while CI completes. The new state
   must appear within one TTL window (≤30s by default) — not indefinitely
   masked. (CI status itself is read through plain `gh pr checks`, which is
   never cached; this checks the *listing* reads that surround it.)
3. **Same-session mutation invalidation.** Read a PR through the wrapper
   (`"$GH_READ" pr view <N> --json labels`), mutate it through the wrapper
   (`"$GH_READ" pr edit <N> --add-label …`), then re-read immediately. The
   re-read must show the new label — a cached pre-write answer here means the
   write was issued through plain `gh` instead of the wrapper.

## Scope note

This is the *existing* Python/`/tmp`-backed wrapper, extended in adoption
only. The forge-neutral `CachedForgeClient` (#3149) — which would also cover
Gitea — remains unimplemented and is deliberately out of scope here; on Gitea
`FORGE_TYPE`, every command above resolves to plain `gh`/`gitea_api` and this
document is a no-op.
