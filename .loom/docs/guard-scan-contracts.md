# Guard scan-string tier contracts

`guard-destructive-generic.sh` answers one question over and over: **is this
text executable code, or inert quoted data?** It answers it by building a chain
of **lossy derived copies** of the command string and matching patterns against
those copies instead of against the raw command.

Every derived copy carries a safety claim **scoped to a consumer tier**, and the
failure direction flips with the consumer:

| Consumer | A missed match costs… |
|---|---|
| `ask()` | a missed confirmation prompt — an accepted risk |
| `deny()` | a **silent ALLOW** where the guard is required to refuse |
| `ALWAYS_BLOCK` (`catastrophic:*`) | a missed **BLOCK** on the ungated denial floor |

Historically those claims were prose only. That reservation was violated exactly
once, and precisely this way: `COMMAND_ASK_SCAN`'s comment stripping was
justified as *"worst case is a missed ASK"*, then a later change routed the same
copy into `extract_write_targets()`'s hard-DENY worktree-write confinement. The
masking never changed — somebody **added a reader**. Root-caused and fixed in
#6252 / [ADR-0016](https://github.com/rjwalters/loom/blob/main/docs/adr/0016-write-target-confinement-approach.md).

Issue #7755 made the reservation machine-checkable:
`scripts/check-guard-scan-contracts.sh`, wired into CI, which fails when a
decision site reads a copy declared for a weaker tier than the site emits. This
document is the inventory it enforces against.

## The markers

Two comment markers live in `defaults/hooks/guard-destructive-generic.sh` and
are the authoritative, drift-free source (this file summarises them):

```sh
# One per derived copy, on its declaring assignment or the header block above it:
COMMAND_ASK_SCAN="$COMMAND_NO_COMMENT"   # scan-contract: COMMAND_ASK_SCAN=deny-safe from=COMMAND_NO_COMMENT

# One per deny()/ask() call, on the same line:
deny "BLOCKED: …" "sql-ddl"              # scan-reads: COMMAND_ASK_SCAN
ask  "Command requires…" "cloud-cli:…"   # scan-reads: COMMAND_CLOUD_ASK_SCAN
deny "BLOCKED: …" "gh-comment-…"         # scan-reads: none
```

- **`<TIER>`** is the *strictest consumer this copy may legitimately feed*:
  `ask-only` < `deny-safe` < `catastrophic-safe`.
- **`from=<PARENT>`** names the copy this one is derived from (`from=COMMAND`
  for a branch off the raw, unmasked command, which masks nothing and is
  therefore safe for every tier).
- **`scan-reads:`** records only *which copies the site reads* — the hand-written
  part that genuinely needs judgement. The site's **own** tier is derived
  mechanically from the line (`ask()` → `ask`; `deny()` with a
  `"catastrophic:…"` reason code → `catastrophic`; any other `deny()` → `deny`),
  so it cannot be mis-annotated.
- **`scan-waiver: <reason>`** appended to a `scan-reads:` comment records a
  deliberate, accepted exception. It still prints a `WARNING` — a waiver is
  visible, never silent.

## What the checker enforces

1. Every `COMMAND_*` derivation assignment has a contract; every `deny()`/`ask()`
   call has a `scan-reads:` line. A new copy or a new decision site cannot be
   added without consciously classifying it.
2. **No laundering**: a copy may never be declared safer than the copy it is
   derived from. Masking is monotonically lossy, so branching off an `ask-only`
   copy and declaring the branch `deny-safe` would be the obvious wrong way to
   silence a failure.
3. **The invariant**: a read site's emitted tier must never exceed the declared
   tier of any copy it reads.
4. No stale annotations, undeclared references, or a variable declared at two
   different tiers.

It deliberately does **not** adjudicate whether a declared tier is *factually*
correct — that a masking pass truly only narrows stays a human-reviewed
property, argued in each derivation's own header comment and regression-tested
by `tests/hooks/test-guard-destructive*.sh`'s "narrows, never widens" coverage.
ADR-0016 already rejected introducing a shell AST here, and #7755 did not
reopen that; this is a grep-level forcing function over a single file with a
small, enumerable set of copies and decision sites.

## Inventory: the derivation graph

```
COMMAND  (raw, masks nothing — implicitly catastrophic-safe)
 ├─ COMMAND_NO_LITERAL_TEXT      catastrophic-safe
 │   └─ COMMAND_RM_MKTEMP_SCAN   deny-safe
 │   └─ COMMAND_WT_MKTEMP_SCAN   deny-safe
 ├─ COMMAND_HEREDOC_MASKED       deny-safe
 │   └─ COMMAND_GH_API_RAWFIELD_SCAN   deny-safe
 └─ COMMAND_NO_COMMENT           deny-safe
     └─ COMMAND_ASK_SCAN         deny-safe
         ├─ COMMAND_CLOUD_ASK_SCAN      ask-only
         ├─ COMMAND_ASK_SCAN_PRINTENV   ask-only
         └─ COMMAND_STASH_SCAN          deny-safe
```

### Accepted lossiness, per copy

| Copy | Tier | Masked out (accepted lossiness) | Why that tier |
|---|---|---|---|
| `COMMAND_NO_LITERAL_TEXT` | `catastrophic-safe` | for-loop word lists, grep/rg/jq positional args, `NAME='…'` dead assignments, flag-keyed literal text, comment lines — each masked only when provably non-executing, and spans carrying `$(`/backtick are left intact | the only copy the ungated `ALWAYS_BLOCK_PATTERNS` scan reads; deliberately does **not** get `#`-comment stripping |
| `COMMAND_RM_MKTEMP_SCAN` | `deny-safe` | inherits the above, plus selective heredoc-body masking | narrow branch feeding the `rm-*` denies only |
| `COMMAND_WT_MKTEMP_SCAN` | `deny-safe` | inherits the above, plus selective heredoc-body masking | narrow branch feeding the write-confinement denies only |
| `COMMAND_HEREDOC_MASKED` | `deny-safe` | closed, quoted-delimiter heredoc bodies; **interpreter-fed** bodies (`bash <<EOF`, `cat <<EOF \| sh`) stay visible (#5198) | feeds a hard deny, not the ALWAYS_BLOCK floor |
| `COMMAND_GH_API_RAWFIELD_SCAN` | `deny-safe` | the above, plus `check-duplicate.sh` positional args and flag-keyed literal text | `gh api … -f body=@path` deny only |
| `COMMAND_NO_COMMENT` | `deny-safe` | `#…EOL` shell comments, **quote-aware** since #6252 — a `#` inside a quoted span is never a comment start | quote-awareness is exactly what promoted this copy from ask-tier to deny-tier; under-strips rather than over-strips on an unterminated quote |
| `COMMAND_ASK_SCAN` | `deny-safe` | the above, plus heredoc bodies (selective + unquoted-`cat` with no `$(`/backtick), `check-duplicate.sh` positional args, flag-keyed literal text | every pass masks only provably-non-executing text; a real invocation chained after a heredoc, or smuggled through `bash -c`, still reaches the deny sites |
| `COMMAND_CLOUD_ASK_SCAN` | **`ask-only`** | the above, plus for-loop word lists, grep/rg/jq positional args, `NAME='…'` dead assignments | justified *because* `CLOUD_ASK_PATTERNS` is a toggleable (`guards.cloudCli`) **ask** tier, not the denial floor — the masking is more aggressive than any deny consumer may accept |
| `COMMAND_ASK_SCAN_PRINTENV` | **`ask-only`** | the above, plus the two documented non-secret `LOOM_TOKEN_{NAME,MODE}` pointer vars (#6245) | an allowlist carve-out is a deliberate false-negative; only sound for an ask |
| `COMMAND_STASH_SCAN` | `deny-safe` | the `COMMAND_ASK_SCAN` set, plus grep/egrep/fgrep/rg/awk quoted positional **search patterns** carrying no `$(`/backtick | search-pattern text is inert by construction; feeds the `stash-scope:create-redirect` deny as well as the stash asks |

### Read sites, by decision tier

Keyed by **reason code** rather than line number, so this table cannot go stale
as the file moves. Grep the guard for `scan-reads:` for the live, authoritative
list.

**`catastrophic` tier** (ungated `ALWAYS_BLOCK` floor)

| Reason code | Reads |
|---|---|
| `catastrophic:<pattern>` | `COMMAND_NO_LITERAL_TEXT` |

**`deny` tier**

| Reason code | Reads |
|---|---|
| `gh-comment-body-literal-at` | *none* (raw `$COMMAND`) |
| `gh-edit-body-literal-at` | *none* (raw `$COMMAND`) |
| `gh-comment-body-literal-at-var` | *none* (raw `$COMMAND`) |
| `gh-edit-body-literal-at-var` | *none* (raw `$COMMAND`) |
| `gh-api-rawfield-body-literal-at` | `COMMAND_GH_API_RAWFIELD_SCAN` |
| `lifecycle` | `COMMAND_ASK_SCAN` |
| `sql-ddl` | `COMMAND_ASK_SCAN` |
| `sql-delete-no-where` | `COMMAND_NO_COMMENT` |
| `rm-protected-path` (×2) | `COMMAND_ASK_SCAN`, `COMMAND_RM_MKTEMP_SCAN` |
| `rm-scope-unresolved-var` | `COMMAND_ASK_SCAN`, `COMMAND_RM_MKTEMP_SCAN` |
| `rm-scope-outside-repo` | `COMMAND_ASK_SCAN`, `COMMAND_RM_MKTEMP_SCAN` |
| `worktree-write-confinement-unresolved-var` (×3) | `COMMAND_ASK_SCAN`, `COMMAND_WT_MKTEMP_SCAN` |
| `worktree-write-confinement` | `COMMAND_ASK_SCAN`, `COMMAND_WT_MKTEMP_SCAN` |
| `stash-scope:create-redirect` | `COMMAND_STASH_SCAN` |

**`ask` tier**

| Reason code | Reads |
|---|---|
| `cloud-delete-ask` | `COMMAND_ASK_SCAN` |
| `force-op:all`, `force-op:detached`, `force-op:protected` | `COMMAND_ASK_SCAN` |
| `ask:<pattern>` (ASK_PATTERNS loop) | `COMMAND_ASK_SCAN` |
| `ask:<systemctl reason>` | `COMMAND_NO_COMMENT` |
| `ask:<ssh-cat reason>` | `COMMAND_ASK_SCAN` |
| `ask:<printenv reason>` | `COMMAND_ASK_SCAN_PRINTENV` |
| `cargo-clean-scope-outside-repo` | `COMMAND_ASK_SCAN` |
| `reversible-gh:<pattern>` | `COMMAND_ASK_SCAN` |
| `git-read-tree` | `COMMAND_NO_COMMENT` |
| `stash-scope:main-checkout` | `COMMAND_STASH_SCAN` |
| `stash-scope:worktree-collision` | `COMMAND_STASH_SCAN` |
| `stash-scope:cd-unresolved` | `COMMAND_NO_COMMENT`, `COMMAND_STASH_SCAN` |
| `cloud-cli:<pattern>` | `COMMAND_CLOUD_ASK_SCAN` |

### Waivers

**None.** Every deny-emitting read in the table above reads a copy declared
`deny-safe` or stricter, so no `scan-waiver:` was needed for the backfill.

Two read/tier relationships deserve their reasoning recorded, because they are
the ones a future change is most likely to get wrong:

- **`COMMAND_ASK_SCAN` is `deny-safe`, not `ask-only`.** The prose reservation
  #7755 quotes (*"explicitly reserved for the ASK/DDL tier only"*) describes the
  copy as it was **before** #6252/#5216/#6519. Those changes deliberately
  promoted it: `mask_comment()` was made quote-aware so comment stripping can
  only under-strip, and `lifecycle`, `sql-ddl`, the `rm-*` denies and the
  write-confinement denies were each pointed at it on purpose. The contract now
  records the promotion instead of contradicting it — and the checker enforces
  the part of the old reservation that is still live: this copy still must
  never reach the `catastrophic` floor.
- **`COMMAND_CLOUD_ASK_SCAN` / `COMMAND_ASK_SCAN_PRINTENV` stay `ask-only`.**
  Their extra masking is justified in-file *by the tier*: "a TOGGLEABLE tier
  (`guards.cloudCli`), not the catastrophic tier's ungated denial floor", and an
  explicit allowlist carve-out for two non-secret environment variables. Routing
  either into a `deny()` is the #6252 shape and fails CI.

## Running it

```sh
scripts/check-guard-scan-contracts.sh --self-test   # regression-test the checker itself
scripts/check-guard-scan-contracts.sh               # check the real guard
scripts/check-guard-scan-contracts.sh <path>        # check any copy of it
```

Both run in CI (`.github/workflows/ci.yml`, job `guard-scan-contracts`). The
checker is bash 3.2 clean — it has to run where the mistake is made, and macOS
ships `/bin/bash` 3.2.57 (#7751, #7728).

## If the check fails

| Message | What it means | Fix |
|---|---|---|
| `VIOLATION: … declared 'X' but is read by a 'Y'-tier decision site` | the invariant — a lossy copy now decides a stricter tier than its lossiness was justified for | point the site at a copy declared for its tier; **or** prove and re-declare the copy at the stricter tier (as #6252 did, by making the masking sound for that tier) and re-check its `from=` parent; **or** record `scan-waiver: <reason>` if the exposure is deliberate |
| `LAUNDERING: …` | a copy is declared safer than the copy it was derived from | lower the copy's tier, or derive it from a stricter parent |
| `… is assigned but never classified` | a new derived copy has no contract | add `# scan-contract: <VAR>=<TIER> from=<PARENT>` |
| `… has no '# scan-reads:' annotation` | a new `deny()`/`ask()` site | add `# scan-reads: <VAR>[,…]`, or `none` if it matches only raw `$COMMAND` |
| `… calls neither deny() nor ask()` | a stale annotation left behind by a refactor | move or delete it |

**Do not "fix" a violation by widening a tier you have not actually made
sound.** The declaration is a claim about the masking, and the whole point of
the marker is that raising it is a reviewed act.
