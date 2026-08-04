# Multi-Account Token Pool & Rotation

Loom can rotate among multiple Claude OAuth accounts so load spreads across
accounts and a single weekly limit does not stall the pipeline. This document is
the full reference for provisioning, importing, health-probing, selecting, and
operating the token pool. `CLAUDE.md` carries only the operating summary and
points here.

## Provider-aware account inventory

The legacy `.loom/tokens` pool remains Loom's Claude compatibility backend:
its files, selection order, state formats, `SelectedToken` API, and
`loom-daemon tokens ...` commands are unchanged.

Provider-aware callers identify accounts by `(provider, name)`, so
`claude/alice` and `codex/alice` are separate identities. Claude inventory
continues to prefer a repo-local `.loom/tokens` pool and otherwise falls back
to `LOOM_SHARED_TOKENS_DIR` (default `~/.loom/tokens`).

Codex inventory uses named directories below `LOOM_CODEX_PROFILE_ROOT`
(default `~/.loom/codex-profiles`). With no repo metadata, each direct child
directory is an enabled shared profile. A repository may restrict or rename
eligible profiles with `.loom/accounts.json`:

```json
{
  "version": 1,
  "accounts": [
    {
      "provider": "codex",
      "name": "build",
      "credential_kind": "codex_home",
      "credential_reference": "team-primary",
      "enabled": true
    }
  ]
}
```

This file is metadata only. `credential_reference` is one logical directory
name, never an absolute or relative path, and resolves only beneath the
machine profile root. Loom rejects traversal, separators, symlink escapes,
non-directory targets, and a profile root located inside the repository.
Codex owns `<profile>/auth.json` and may refresh it in place; Loom's registry
does not open, parse, copy, serialize, or log that file. Operators should keep
profile directories `0700` and `auth.json` `0600`.

Selected accounts expose the non-secret observability identity
`LOOM_ACCOUNT_PROVIDER` and `LOOM_ACCOUNT_NAME`. Claude selections also retain
`LOOM_TOKEN_NAME`; Codex selections bind `CODEX_HOME` and never expose an
equivalent credential variable. Raw provider capacity counts enabled inventory
candidates only. Quota health, cooldown, ranking, and failover are layered on
later and do not alter this inventory contract.

> **Secrets**: `~/.claude-monitor/accounts.env`, the opt-in `~/.loom/accounts.env`,
> and the repo-local `.loom/accounts.env` all hold raw OAuth keys. The repo-local
> file and `.loom/tokens/` are gitignored (installer- and `loom-daemon init`–managed);
> keep any home-level master `0600` and outside any repo.

## Bootstrapping the pool

For environments that rotate among multiple Claude OAuth accounts, Loom can
bootstrap a per-account token pool at `.loom/tokens/` from numbered
`ACCOUNT_EMAIL_N` / `ACCOUNT_KEY_N` / `ACCOUNT_TOKEN_FILE_N` triples:

```env
ACCOUNT_EMAIL_1=user1@example.com
ACCOUNT_KEY_1=sk-ant-oat01-...
ACCOUNT_TOKEN_FILE_1=user1.token
```

Run `loom-daemon tokens bootstrap` to materialize the pool:

```bash
loom-daemon tokens bootstrap            # Idempotent — only writes new/missing tokens.
loom-daemon tokens bootstrap --dry-run  # Preview + print the effective merged account set.
loom-daemon tokens bootstrap --force    # Overwrite on-disk tokens that have drifted from source.
loom-daemon tokens bootstrap --shared   # Provision the shared machine-level pool at ~/.loom/tokens
```

Each account becomes `.loom/tokens/<file>.token` (mode `0600`). An `index.json`
manifest is written alongside with sha256 fingerprints (8 chars) for drift
detection plus each account's `source` (home/repo) — **no secret material is
stored in the manifest**. Numbering gaps are allowed; partial triples are skipped
with a warning.

`.loom/tokens/` is gitignored. The pool is consumed by external rotation logic
(e.g. a `claude-wrapper.sh` that picks the least-used token); only the bootstrap
step is provided here.

## Account sources: claude-monitor-first + per-repo (#3695, #3698, #3704)

Rather than re-declaring the same account triples in every repo's `.env`, declare
them **once** in the shared claude-monitor master and let each workspace add or
override on top of it. Sources are merged by account email in precedence order:

| Source | Default location | Override |
|--------|------------------|----------|
| **claude-monitor master** (primary) | `~/.claude-monitor/accounts.env` | `LOOM_CLAUDE_MONITOR_DIR` env var (directory) |
| **Repo-local** | `<repo>/.loom/accounts.env` if present, else legacy `<repo>/.env` | `--env <path>` on `bootstrap` |
| **Home master** (opt-in only, #3704) | *no default location* — read **only** when explicitly pointed at | `LOOM_ACCOUNTS_ENV` env var (a path enables it, `""` disables); `--home-env <path>` / `--no-home` on `bootstrap` |

**Default resolution is claude-monitor → repo `.env`.** The `~/.loom/accounts.env`
home master is **no longer auto-read** (#3704 retired the default location): it is
consulted only when an operator opts in via `LOOM_ACCOUNTS_ENV=<path>`
(conventionally `~/.loom/accounts.env`) or `--home-env <path>`. This retires the
default *location*, not the *capability*.

`loom-daemon tokens bootstrap` reads the available sources and **merges them by account
email** (`ACCOUNT_EMAIL`), with the higher-precedence source winning:

- An email present **only in a lower-precedence source** is inherited into the pool.
- An email present **only in a higher-precedence source** is added.
- An email present in **both** → the higher-precedence entry overrides (e.g. to
  rotate a key or repoint the token file).

To *exclude* an inherited account from one repo, pin the subset you want with
`loom-daemon tokens pin` — the merge only ever adds/overrides, never subtracts. The
effective merged set (and where each account came from) is printed by `bootstrap`
and `bootstrap --dry-run`. A repo with only a legacy `.env` and no other source
behaves exactly as before.

## Importing live tokens from claude-monitor (#4006)

`accounts.env` is a **snapshot** — a file someone wrote by hand at some point.
claude-monitor keeps the **live** credentials in its SQLite store
(`~/.claude-monitor/usage.db` → `oauth_credentials`) and refreshes them as
accounts are re-authenticated. The two drift, and the drift is silent and total:

```text
401 {"type":"authentication_error","message":"OAuth access token has been revoked."}
```

When that happens to every account at once, `loom-daemon tokens check` reports all
accounts `blocked`, the daemon's dynamic concurrency cap collapses to
`min(healthy 0 × per-token N, …) = 0`, and dispatch stops entirely. Crucially
**`bootstrap --force` does not fix it** — it faithfully rewrites the same revoked
tokens, because the snapshot itself is what went stale.

**`bootstrap` now detects this condition (#4030).** When `usage.db` is present and
the tokens `bootstrap` is about to write disagree with the live store (same email,
different fingerprint), it prints a warning naming the diverging accounts and
pointing at `import-from-monitor` — so the stale snapshot is caught automatically
instead of by hand-comparing fingerprints. The check is read-only, warns but never
auto-switches sources, and is silent when no `usage.db` is present or it is
unreadable; it prints emails and 8-char fingerprints only, never secret material.

`loom-daemon tokens import-from-monitor` reads the live store directly and is **the
standard way to populate a new host's pool** (it replaces hand-copying a pool
between machines):

```bash
loom-daemon tokens import-from-monitor                  # into <repo>/.loom/tokens
loom-daemon tokens import-from-monitor --shared         # into the machine-level pool (#3938)
loom-daemon tokens import-from-monitor --force          # apply ROLLED tokens (see below)
loom-daemon tokens import-from-monitor --dry-run        # preview
loom-daemon tokens import-from-monitor --prune          # drop accounts the monitor no longer reports
```

**`--force` is what applies a token roll.** Every rolled token legitimately
differs from what is on disk, so without `--force` each one is reported as drift
and left alone — deliberately, so a hand-pinned token is never silently clobbered.
The command exits `2` when drift was found and not applied, so a script can detect
"pool is still stale". After importing, refresh the ranking so the daemon sees the
recovered capacity:

```bash
loom-daemon tokens import-from-monitor --force && loom-daemon tokens check --ranking
```

Behavior notes:

- **Read-only** on `usage.db` (opened `mode=ro`) — the store belongs to
  claude-monitor; Loom never writes or migrates it.
- Only `is_active = 1` rows are imported; `expires_at` is **not** used as a filter
  (observed rows carry stale timestamps while still authenticating — health comes
  from `loom-daemon tokens check`).
- Token filenames use the same derivation as `bootstrap` (`robb@2amlogic.com` →
  `robb-2amlogic.token`), so an account keeps one identity across both paths and
  re-importing overwrites in place.
- Idempotent: unchanged tokens are left untouched. `index.json` records
  `source: monitor-db` (distinct from the `monitor` snapshot) and, as always,
  fingerprints only — never secret material.
- `--prune` removes only `*.token` files; pool state (`.ranking`, `.bad_tokens`,
  `.failure_counts`, `.allowlist`) is never touched.
- The importer takes **claude-monitor as authoritative for pool membership**, so
  it imports every active account — including any that `accounts.env` omitted. Use
  `loom-daemon tokens pin` to restrict which accounts the selector may actually pick.
- Absent claude-monitor, an absent `usage.db`, or an older schema without
  `oauth_credentials` all exit `1` with a message naming the path tried.

## Account health probe + ranking

Once bootstrapped, `loom-daemon tokens check` probes each account for current rate-limit
headers and (optionally) writes a JSON ranking that the spawn-time selector can
consume:

```bash
loom-daemon tokens check                  # Probe + print human table
loom-daemon tokens check --ranking        # Probe + write .loom/tokens/.ranking atomically
loom-daemon tokens check --json           # Emit full JSON report to stdout
loom-daemon tokens check --json    # Native Rust equivalent (issue #4108)
./.loom/scripts/probe-tokens.sh    # Cron-friendly wrapper for periodic invocation
```

`--source` (flag or `$LOOM_RANKING_SOURCE`) selects where the ranking comes
from: `auto` (default) prefers a fresh claude-monitor `ranking.json` and only
falls back to this CLI's own native probe when one isn't present; `monitor`
never falls back (an empty report when claude-monitor has nothing fresh);
`probe` always uses the native probe, ignoring claude-monitor entirely.

**After adding a new account** (via `bootstrap` or `import-from-monitor`), run
`loom-daemon tokens check --ranking --source probe` once. Under the `auto`
default, a fresh claude-monitor `ranking.json` short-circuits the whole probe
— including for an account claude-monitor itself has not ranked yet — so a
just-added account can be silently absent from `.ranking` (and therefore
never selected) until claude-monitor's own next probe cycle catches up.
`--source probe` reads every `*.token` file on disk directly, so the new
account is ranked immediately.

**`probe-tokens.sh` delegates to `loom-daemon tokens check`, not Python (#4080).**
It resolves a `loom-daemon` binary (`$LOOM_DAEMON_BIN` → `loom-daemon` on PATH →
build-output-relative candidates under the repo), capability-probes it with
`tokens check --help` to detect a stale pre-#4108 binary, and `exec`s `tokens
check "$@"` on success — the flags and exit codes above are unchanged either
way.

**It has no fallback tier at all** (epic #4081 Phase 4, #4557). Both historical
fallbacks are gone: the bare `python3 -m loom_tools.tokens.cli` tier went in
#4080, and the `loom-tokens`-console-script-on-PATH tier went when #4557 deleted
the Python package that shipped it. A `loom-tokens` still on PATH after that
deletion is by definition a stale editable-install leftover (the #4079 shadowing
incident), so dispatching to it would run frozen, months-old token logic against
the live pool. When no capable daemon binary resolves, `probe-tokens.sh` exits
`1` with an actionable message naming `loom-daemon-update.sh` /
`loom-daemon-start.sh` / `cargo build` — a loud failure, never a silent
degradation.

The probe sends a minimal `POST /v1/messages` request (1 input, 1 output token)
and parses rate-limit response headers. The header parser matches by **suffix**
(`-5h-utilization`, `-7d-utilization`, `-7d-reset`) so future renames of the
`anthropic-ratelimit-tokens-*` prefix still work; the full header set is logged on
the first probe of each run.

Status assignment: `available` (utilizations < 95%), `exhausted`
(`7d_utilization >= 0.95`), `rate_limited` (current 429), `blocked` (401 auth
failure or token listed in `.bad_tokens`). Probe failures (network, timeout, 5xx)
are logged and skipped — one bad account does not abort the run.

OAuth tokens shaped `sk-ant-oat01-*` are sent with `Authorization: Bearer` +
`anthropic-beta: oauth-2025-04-20`; plain API keys use `x-api-key`.

**The running `loom-daemon` self-refreshes `.ranking` (#3969)** — it invokes
**its own binary** (`std::env::current_exe()`) with `tokens check --ranking
--workspace <repo_root>` on its own periodic loop (default every 10 minutes,
`autonomous.tokenRankingRefresh` / `LOOM_TOKEN_RANKING_REFRESH*`, on by default
since it is read-only probing with no dispatch side effect) — as of #4080 this
is a direct daemon-to-daemon subcommand invocation, not a shell out to
`probe-tokens.sh`, so a standing cron for this is no longer required when the
daemon is running. See
[Token-ranking self-refresh](daemon-reference.md#token-ranking-self-refresh-3969)
for the config knobs.

A cron entry is now only needed as a **fallback for setups that don't run
`loom-daemon`** (e.g. pure `/loom:sweep` subagent dispatch with no daemon
process). Cron example (probe every 10 minutes):

```cron
*/10 * * * * cd /path/to/repo && ./.loom/scripts/probe-tokens.sh --ranking >> .loom/logs/probe-tokens.log 2>&1
```

## Token rotation setup (per-task spawn)

For Pro/Max plans, Loom supports rotating between multiple Claude Code OAuth
tokens. This spreads load across accounts and recovers automatically when a single
token hits its weekly limit.

1. Declare account credentials in a default source — the shared claude-monitor
   master `~/.claude-monitor/accounts.env` (primary) or per-repo in
   `<repo>/.loom/accounts.env` (falls back to legacy `<repo>/.env`). The
   `~/.loom/accounts.env` home master is **opt-in only** since #3704 (no longer
   auto-read); point `LOOM_ACCOUNTS_ENV=~/.loom/accounts.env` (or `--home-env
   <path>`) at it to enable:
   ```env
   ACCOUNT_EMAIL_1=account-one@example.com
   ACCOUNT_KEY_1=sk-ant-oat01-...
   ACCOUNT_TOKEN_FILE_1=account-one.token
   ACCOUNT_EMAIL_2=account-two@example.com
   ACCOUNT_KEY_2=sk-ant-oat01-...
   ACCOUNT_TOKEN_FILE_2=account-two.token
   ```
   The claude-monitor, repo-local, and (opt-in) home sources are **merged by
   email**, with the higher-precedence source overriding/adding. Keep any
   home-level master `0600` and outside any repo.
2. Run `loom-daemon tokens bootstrap` to materialize the merged set into per-account
   `.token` files in `.loom/tokens/` (mode 0600, parent dir 0700). See issues
   #3234, #3695. **If claude-monitor runs on this host, prefer `loom-daemon tokens
   import-from-monitor`** — it reads claude-monitor's live credential store instead
   of the `accounts.env` snapshot, so a new host needs no account file of its own
   and a token roll is picked up automatically (add `--force` to apply rolled
   tokens).
3. Spawn agents through `.loom/scripts/spawn-claude.sh` instead of invoking
   `claude` directly. The wrapper selects a token using a 3-tier algorithm
   (ranking → allowlist → random), exports `CLAUDE_CODE_OAUTH_TOKEN`, then `exec`s
   `claude` (or pass `--use-wrapper` to layer on top of `claude-wrapper.sh` for
   retry behavior).

This whole rotation scheme rests on one assumption: the installed Claude Code
CLI honors `CLAUDE_CODE_OAUTH_TOKEN` over a locally logged-in Keychain
account. `defaults/scripts/verify-token-precedence.sh` (#3236, operator-manual
-- run by hand once per Claude Code version, not wired into any automated
check) confirms that assumption still holds by comparing `claude auth status`
with a real Keychain login against the same command run with a deliberately
bogus env token.

## Selection algorithm (`loom-daemon tokens select`)

Three tiers, falling through to the next when the current tier yields nothing.
Native Rust (`loom-daemon/src/tokens_pool/select.rs`), invoked directly by
`spawn-claude.sh` / `claude-wrapper.sh` as of issue #4228 (epic #4081 Phase 2) —
byte-compatible with the historical `loom_tools.tokens.select` implementation,
which stays in-tree as reference/conformance material (`loom-tools/tests/tokens/`)
but is no longer on the runtime path:

1. **Ranking** — `.loom/tokens/.ranking` (pipe-delimited
   `name|status|5h_util|limit_reset`, refreshed every <10 min). A persistent
   rotation cursor spreads consecutive
   dispatches one-per-account across the eligible accounts (#3909;
   `LOOM_TOKEN_SPREAD_TOP_N` / `tokens.spreadTopN` optionally caps the window).
2. **Allowlist** — `.loom/tokens/.allowlist` (one name per line). Random pick from
   allowed accounts.
3. **Random** — uniform pick from all `*.token` files.

Tokens marked bad in `.loom/tokens/.bad_tokens` are skipped at every tier.
`loom-daemon tokens select --export` emits shell-evalable `export
CLAUDE_CODE_OAUTH_TOKEN=...` / `export LOOM_TOKEN_NAME=...` lines (plus a
non-exported `LOOM_TOKEN_MODE=...`) so callers `eval` the output directly
instead of round-tripping through a JSON parser; `--auto-unpin` runs the
pinned-account auto-recovery pre-flight (see below) before selecting.

### Ranking format: 5h-load field + soft gate (issue #4195)

The `.ranking` line format is `name|status|5h_util|limit_reset`, where the **third
field** is the account's 5h-window utilization (a fraction `0.0`–`1.0`, fixed at 2
decimals). It is **optional**: a legacy 2-field `name|status` line still parses,
and an account with no measured 5h utilization is written in the 2-field form
(the value is left off, never faked as `0.0`). All four ranking writers emit it
— the Python probe (`check.py`) and monitor (`monitor.py`) paths and their
byte-identical Rust ports (`tokens_pool::check` / `tokens_pool::monitor`).

The ranked tier layers a **soft load gate** on top of the rotation cursor: in
the preferred pass an account **at/above** the 5h threshold is excluded (so the
cursor rotates only across the lightly-loaded accounts), and the fallback pass
readmits them — the pool never hard-fails on load alone. A **missing or
unparseable** utilization is treated as *unknown* → never gated. The threshold
defaults to `0.70` and is overridable via the `LOOM_TOKEN_5H_LOAD_GATE` env var
(a value `> 1.0` disables the gate); it follows the `LOOM_TOKEN_SPREAD_TOP_N`
precedent and is honored identically by the Python selector (`select.py`) and
the Rust port (`tokens_pool::select`). This is the Option-A reconciliation of
the gpeyton-fork load-aware selection proposal (fork commits `283de8e3`,
`20961dd9`) with upstream's existing rotation-cursor spread — a load-aware gate
*layered on* #3909, not the fork's waterfall-fill replacement.

### Ranking format: limit-reset field (issue #4874)

The **fourth field** is the instant the account's **binding** limit window
resets — the answer to "when can I dispatch to this account again?" — written as
`%Y-%m-%dT%H:%M:%SZ`. Like `5h_util` it is **optional and additive**: a legacy
2- or 3-field line still parses, with the reset read back as *unknown*
(`None`) — never a fabricated date.

**Which window is binding depends on the status** (`check::limit_reset` is the
single place this is decided, and both writers call it):

| Status | Reset written | Why |
|---|---|---|
| `exhausted` | 7d | `exhausted` *is* 7d utilization ≥ `EXHAUSTED_THRESHOLD`; the 5h window rolling over releases nothing. |
| `rate_limited` | 5h | A 429 with 7d utilization below the threshold — the 5h window is what tripped. |
| anything else | 5h | Not gated at all; the 5h rollover is what the reported `5h_util` is racing. |

Reporting the 7d reset unconditionally would be *worse than an empty column*:
on a live host a `rate_limited` account had a 5h reset ~1.6h out and a 7d reset
**six days** out, so a 7d countdown would have told the operator the fleet was
stalled until Saturday. When the binding window's instant is unknown it is left
absent — never substituted with the other window's, because an absent countdown
reads as "unknown" and a wrong one reads as a fact.

Both ranking backends populate it:

- The **claude-monitor backend** (`tokens_pool::monitor`) reads
  `accounts[].resets["5h"]` and `["7d"]` from `~/.claude-monitor/ranking.json`
  and normalizes them to the instant format. It previously reported no reset at
  all, which is why the CLI's reset column was empty on every monitor-sourced
  run even though the data was on disk the whole time.
- The **native probe** (`tokens_pool::check`) parses the
  `anthropic-ratelimit-…-7d-reset` header (it already did, only to discard the
  value at the writer) plus `-5h-reset` opportunistically — the API does not
  always send the latter, in which case a `rate_limited` row carries no
  countdown rather than a misleading one.

`loom-daemon tokens check --ranking`'s table shows this same value in a
`Resets at` column that names its window, e.g. `2026-08-02T03:00:00Z (7d)`.

**Field-position rule**: a row that knows its reset but *not* its utilization
writes an empty third field (`name|status||limit_reset`) so the reset stays in
position 4. The reader parses that empty utilization back to `None`, never to
`0.0`. A reset containing a `|` or `#` (either would make the line unparseable)
is dropped rather than written — an absent reset beats a mangled row.

Selection **ignores** this field: it is telemetry, not an input to the tiered
pick, so adding it cannot perturb which account is chosen. Its consumer is
`tokens.snapshot`'s `limit_window_reset_at` (see `telemetry-schema.md`), which
feeds the dashboard's per-account reset countdown, its burn-curve segmentation
and forecasts, and the pool-level "capacity returns at" aggregate.

## Bad-token tracking (`loom-daemon tokens mark-bad`)

When a token returns `TOKEN_EXPIRED` or `TOKEN_EXHAUSTED`, callers append an entry
to `.loom/tokens/.bad_tokens` via `loom-daemon tokens mark-bad <name> --reason
<text>` (native Rust, `loom-daemon/src/tokens_pool/bad_tokens.rs`, exposed as a
CLI subcommand in #4228 — the historical Python `loom_tools.tokens.bad_tokens`
module was library-only, with no CLI of its own). Writes are guarded with a
`mkdir`-based lock (POSIX-atomic, macOS-compatible — `flock` is **not** used
because it isn't available on stock macOS). Reads use word-boundary regex so
`agent-1` and `agent-10` don't collide. The reason field's embedded
newlines/carriage-returns are sanitized to spaces so every `.bad_tokens` record
is exactly one line — byte-compatible with the Python implementation, and
conformance-tested against it in `loom-tools/tests/tokens/test_rust_conformance.py`.

How long an entry keeps blocking (auth = permanent, exhaustion = 6h TTL) and how
long it survives on disk (24h / 30d) are two different clocks — see
[Permanence: auth vs exhaustion](#permanence-auth-vs-exhaustion-at-read-time-and-on-disk)
below.

## Error classification (`.loom/scripts/lib/classify-error.sh`)

The `classify_error <output> <exit_code>` function returns one of `SUCCESS`,
`TIMEOUT`, `CWD_DELETED`, `TOKEN_EXPIRED`, `TOKEN_EXHAUSTED`, `RECOVERABLE`.
Critical fix from #3233: exit code is checked **before** output substring
matching — clean exits (`exit_code == 0`) always return `SUCCESS` regardless of
stdout content.

## Worktree handling

When invoked from a worktree, `spawn-claude.sh` resolves the canonical repo root
via `git rev-parse --git-common-dir` and locates `.loom/tokens/` there — never in
the worktree's path. This avoids each worktree maintaining its own bad-tokens
list.

## Shared machine-level pool fallback (#3938)

Token selection resolves the effective pool as: the **per-repo** pool
`<repo>/.loom/tokens/` when it holds `*.token` files, else the **shared
machine-level pool** `~/.loom/tokens/` (override `LOOM_SHARED_TOKENS_DIR`; set it
empty to disable the fallback). This lets a consumer repo the daemon dispatches
into — which has no pool of its own — spawn against the shared pool instead of
hard-failing with `EX_CONFIG`. Crucially, the pool **state** files (`.bad_tokens`,
`.failure_counts`, `.ranking`, `.allowlist`) are read/written in whichever pool was
selected, so state is **never forked per repo** (token-capacity backpressure sees
one truth). Provision the shared pool once per machine with `loom-daemon tokens bootstrap
--shared`. See [daemon-reference.md → Token pool provisioning for managed
repos](daemon-reference.md#token-pool-provisioning-for-managed-repos-3938).

**Native selection, zero Python package-path resolution needed (#4228)**: `#3938`
fixed the pool *location*; as of #4228 (epic #4081 Phase 2) token *selection*
itself is native too — `spawn-claude.sh` / `claude-wrapper.sh` shell out to
`loom-daemon tokens select` (resolved via `$LOOM_DAEMON_BIN` -> `loom-daemon` on
PATH -> build-output-relative candidates under the repo; see
`.loom/scripts/lib/locate-daemon-bin.sh`) instead of `python3 -m
loom_tools.tokens.select`. This retires the `LOOM_PACKAGE_PATH` bridge entirely
(script-side resolution AND the daemon-side `spawn_child`/role-runner forwarding
that used to derive it from the source tree the running binary was compiled
from) — a consumer repo with no loom checkout now selects a token successfully
with zero manual configuration and no Python package to locate at all.

### Full anchoring precedence, and machine-level daemon startup (#4292)

Everything above resolves the pool for a caller whose **workspace root is
already known** to be a real repo checkout (an explicit `--workspace`, a
worktree's own root, or a per-request-resolved dispatch target). A
machine-level daemon (#3835/#3926) is different: at startup its own primary
workspace is seeded from `LOOM_WORKSPACE` or, absent that, its **own process
cwd** — and under `systemd` with no `WorkingDirectory=` override that cwd
defaults to `$HOME`, which is not a repo checkout at all.

Feeding a non-workspace cwd straight into the `#3938` per-repo/shared fallback
is worse than "no tokens": the **default** shared pool is *also*
`~/.loom/tokens`, so `workspace_root == $HOME` makes the per-repo and shared
probes coincidentally check the exact same (usually empty) directory — masking
wherever the pool was actually bootstrapped (e.g. a per-repo pool at the
daemon's real, differently-located checkout) behind a plausible-looking but
wrong "no tokens found" warning.

The full precedence, in order, for every consumer (`loom-daemon status`, the
daemon's own dispatch-capacity accounting, `tokens check --ranking`'s
default-`--workspace` case, and the daemon's autonomous ranking-refresh loop):

1. **Explicit `--workspace <path>`** (a CLI flag naming a real, possibly
   unregistered, repo) — always wins outright, resolved via the per-repo/shared
   precedence above. Never redirected, so pointing at a specific repo is never
   silently overridden.
2. **Default (`--workspace` omitted / `.` / the daemon's own seeded cwd) and
   the candidate root falls under a registered workspace** — either because
   `loom-daemon workspace add` was never run at all (an empty registry trusts
   the candidate unconditionally, preserving `#3938`'s byte-for-byte
   single-workspace behavior for every repo-local install), or because the
   candidate matches a registered root — resolved via the same per-repo/shared
   precedence, anchored at the registered root when the candidate is a
   subdirectory of it.
3. **Default and the candidate matches no registered workspace** — the
   machine-level-daemon-at-`$HOME` case — skips the per-repo probe entirely and
   resolves straight to the shared machine-level pool (`~/.loom/tokens`,
   override `LOOM_SHARED_TOKENS_DIR`). Falls back to per-repo(candidate) only
   when the shared pool is itself disabled (`LOOM_SHARED_TOKENS_DIR=""`).

**Operational contract**: for step 3 to actually find tokens, a machine-level
daemon's pool must be bootstrapped at the shared location —
`loom-daemon tokens bootstrap --shared` (or `import-from-monitor --shared`) — rather
than per-repo at whatever directory happens to be the daemon's own checkout.
Registering the daemon's own checkout as a workspace
(`loom-daemon workspace add <checkout>`) is the alternative: that makes step 2
apply instead, so a per-repo pool bootstrapped there (without `--shared`) is
found too.

**No `WorkingDirectory=` needed**: with the pool bootstrapped per the
contract above, a `systemd`-managed daemon started with the unit's default
`$HOME` cwd (or any other non-workspace directory) now finds its token pool —
and reports accurate dispatch capacity — without an operator having to
discover and set an explicit `WorkingDirectory=` override (`.loom/docs/daemon-reference.md`
covers `#4268`/`#4319`/`#4321`, which already set `WorkingDirectory=` as
first-class in *generated* systemd units; this section is for bare/unmanaged
daemon startups and ad hoc CLI invocations from arbitrary cwds — the generated
units already cover the managed case).

Implementation: [`resolve_tokens_dir_anchored()`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/tokens_pool/paths.rs)
delegates step 2/3's "is this candidate a recognized Loom workspace" question
to the same registry-membership check `#4299` established for CLI
`--workspace` defaulting (`workspace_registry::resolve_client_workspace_default`)
rather than a second, parallel detection path.

### `loom-daemon health`'s daemon-CWD-vs-operator-repo distinction (#5269)

The precedence above governs **two separate mechanisms** that do not share a
scope, and conflating them was the root cause of a "5h-stale ranking" incident
where the documented remediation refreshed the wrong pool:

1. **The self-refresh loop is per-repo-correct.** The daemon's own
   `token_ranking_refresh.rs` background task re-runs `tokens check --ranking`
   for **every registered repo independently**, resolving each repo's own pool
   via the *unanchored* `resolve_tokens_dir(&repo.root)` (step 1/2 of the
   precedence above, evaluated separately per repo). On a multi-repo daemon
   this keeps every registered repo's OWN `.ranking` fresh on its own cadence,
   regardless of which repo the daemon process happens to be running in.
2. **`loom-daemon health`'s (and `status`'s) single machine-level tokens
   section is anchored to the daemon's OWN `fallback_root`** — its launch CWD
   or `LOOM_WORKSPACE`, resolved via `resolve_tokens_dir_anchored` (the full
   precedence above). On a daemon managing several repos from a launch CWD
   that is only one of them (e.g. a daemon started under `~/GitHub/anvil`
   managing `~/GitHub/loom` too), this reports staleness for whichever pool
   *that* anchoring resolves to — which is not necessarily any particular
   other registered repo's own pool, and was never designed to answer "is
   *my* repo's pool fresh" for an operator running the command from a
   different registered repo.

**The fix (#5269)**: `loom-daemon status`'s `per_repo` breakdown (see
[daemon-reference.md](daemon-reference.md)) now carries each registered repo's
own `token_pool_dir`/`ranking_present`/`ranking_age_secs`, populated with the
exact same unanchored `resolve_tokens_dir(&repo.root)` the self-refresh loop
already uses — so an operator asking about a specific repo gets that repo's
own answer, independent of the daemon's launch CWD. `loom-daemon health`'s
`tokens` section detail JSON (`--json`) surfaces this as `per_repo: [...]`
alongside the existing single-pool `pool_path`/`ranking_present`/
`ranking_age_secs` fields (which keep their original, narrower
`fallback_root`-anchored meaning — the top-level fields are NOT replaced,
only supplemented), and folds a bounded "`N of M` registered repos have their
own pool's `.ranking` stale/missing" note into the human summary line when any
repo is affected.

**The workaround this incident's remediation used** — refreshing from `$HOME`
happened to "fix" the daemon's top-level reading only because
`per_repo_tokens_dir($HOME)` collapses to the same path as the default shared
pool (`~/.loom/tokens`) — is now unnecessary for diagnosing (not necessarily
for actually refreshing) a specific repo's own staleness: read that repo's
line in `loom-daemon status --json`'s `per_repo` array, or
`loom-daemon health --json`'s `tokens.detail.per_repo`, instead.

## Hard-fail on missing pool

`spawn-claude.sh` exits `78` (`EX_CONFIG`) with a message instructing the user to
run `loom-daemon tokens bootstrap` (or `loom-daemon tokens bootstrap --shared` for the
machine-level pool) when **neither** the per-repo nor the shared pool has usable
tokens (absent, empty, or all bad). It does **not** silently fall back to
keychain — that path belongs in `loom-daemon` (#3236), and only when token
rotation has not been configured at all.

The `loom-daemon` role runner (`autonomous.roleRunner`, see
[daemon-reference.md](daemon-reference.md)) pre-checks
[`tokens::token_pool_size`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/tokens.rs) for exactly this
condition **before** it ever spawns `spawn-claude.sh` (issue #4642): a repo
with neither pool provisioned skips the doomed spawn entirely instead of
hitting this hard-fail on every single tick forever. The skip is logged once
(`WARN`) per root per role, then downgraded to `DEBUG` on repeats — grep a
role's log for `no token pool available` — and is re-checked every tick, so
provisioning either pool later resumes ticking with no daemon restart needed.

## Per-repo pool vs. shared machine-level pool: which to provision (#4642)

A newly-managed repo (added via `loom-daemon workspace add`, or picked up by
the multi-workspace role runner / work finder) starts with **neither** pool
provisioned, and stays that way until an operator deliberately runs one of the
two bootstrap commands below — this is an operator/billing decision, not
something Loom auto-applies, because it changes **whose usage counts against
which weekly ceiling**:

- **Per-repo pool** (`loom-daemon tokens bootstrap` from inside the repo,
  populating `<repo>/.loom/tokens/`): this repo's sweeps and role ticks draw
  from accounts dedicated to it alone. Usage against each account's weekly
  limit is isolated to this one repo — the right choice when a repo has its
  own budget/accounts, or when you want one repo's activity to never compete
  with another's for the same account's rotation slot.
- **Shared machine-level pool** (`loom-daemon tokens bootstrap --shared`,
  populating `~/.loom/tokens/` or the `LOOM_SHARED_TOKENS_DIR` override,
  resolved by [step 3 of the fallback chain](#shared-machine-level-pool-fallback-3938)
  above): every repo on the host that has **no per-repo pool of its own**
  falls back to this one pool and shares its accounts. This is convenient for
  low-traffic repos (e.g. the canary chip repos that motivated #4642 — several
  small repos, none busy enough on its own to justify dedicated accounts) but
  means **every one of those repos' sweeps and role ticks now compete for the
  same weekly ceiling** — a burst of activity in one consumer repo can exhaust
  accounts another consumer repo also depends on. A repo with a per-repo pool
  is never affected by this (the per-repo pool always wins when it holds
  tokens, `token_pool_size_resolved`'s precedence).

**Rule of thumb**: provision a per-repo pool for any repo whose activity level
or billing owner is distinct enough to want isolated usage accounting; use the
shared pool for a cluster of low-traffic repos willing to accept a shared
ceiling in exchange for zero per-repo token administration. Both can coexist
on one host — the fallback chain is per-repo-first, so adding a per-repo pool
to a repo previously riding the shared pool is a strictly additive, safe
change (it simply stops that one repo from drawing on the shared ceiling, with
no reconfiguration needed on the shared side).

## Operator CLI (`loom-daemon tokens pin/unpin/unblock`)

Operators can restrict the rotation pool to a subset of accounts (an "allowlist")
and manually un-blacklist accounts marked bad. Auto-recovery prevents pin-induced
lockouts.

```bash
loom-daemon tokens pin agent-3 agent-7   # Set allowlist to exactly these
loom-daemon tokens pin add agent-2       # Append (idempotent)
loom-daemon tokens pin remove agent-3    # Remove
loom-daemon tokens pin status            # Show current allowlist
loom-daemon tokens unpin                 # Delete allowlist (back to full pool)

loom-daemon tokens unblock agent-1              # Drop agent-1's AUTH entries only
loom-daemon tokens unblock agent-1 --all-reasons  # Also drop its exhausted/rate-limited entries
```

**Validation**: `pin` accepts only exact bootstrapped account names —
substring/fuzzy matches are rejected. The allowlist is sorted, deduplicated, and
`mkdir`-lock guarded so concurrent operator commands don't drop entries.

**`unblock` default scope fails loudly on left-behind entries (#4212)**: the
default scope removes only `auth` entries (a broken credential); transient
`exhausted`/`rate-limited` entries clear themselves. When the named account has
*only* a non-auth entry, the default scope leaves it in place — and rather than
print "No matching entries removed" and exit `0` (the pre-#4212 silent no-op that
let an operator dispatch onto a still-poisoned pool), `unblock` now **names the
still-blocked accounts and exits `3`**. Re-run with `--all-reasons` to drop them,
or wait for the cooldown to expire them automatically.

### Permanence: auth vs exhaustion, at read time and on disk

There are **two independent clocks**, and conflating them is what made the
2026-07-30 incident (#4643) hard to read. The table is the contract the error
text, the CLI, and this document all agree on:

| Reason class | Blocks selection until | Pruned from `.bad_tokens` after |
|---|---|---|
| `auth` (401 / OAuth / expired / blocked) | `loom-daemon tokens unblock <name>` — **never expires** | 30d (`AUTH_ENTRY_MIN_RETENTION_SECS`), a garbage-collection floor, *not* an expiry |
| non-auth (`exhausted` / `rate-limited`) | the **exhaustion cooldown** — `LOOM_TOKEN_EXHAUSTION_COOLDOWN_SECS`, default `21600` (6h) | 24h (`DEFAULT_CLEANUP_MAX_AGE_SECS`) |
| unparseable timestamp | never (**fail-closed** — a malformed line never silently un-blocks a token) | never (malformed lines are always retained) |

**Read time** (`bad_tokens::is_bad` / `blocking_entry`, enforced by the selector
on every tier): a non-auth entry stops blocking once it ages past the cooldown,
so a recovered account re-enters rotation with **no operator action** even
before the line is pruned from disk (#4122). Auth entries are exempt from that
TTL by design — a broken credential does not heal on a timer.

**On disk** (`bad_tokens::cleanup_bad_tokens`, wired in #4643 into `tokens
select` and `tokens check`): entries older than the max-age policy above are
dropped. The auth floor is deliberately far longer than the routine 24h policy,
because pruning an auth line *would* silently readmit a broken credential —
the on-disk clock must never become a back-door expiry for the permanent class.
Cleanup is best-effort and takes **no lock at all** when nothing is prunable, so
a burst of concurrent spawns never serializes on `.bad_tokens`. Before #4643
`cleanup_bad_tokens` had zero callers anywhere in the tree and pools accumulated
entries indefinitely.

**The oldest visible entry is usually not the deciding one.** Every failed spawn
appends a *new* line, so an account with a genuinely long-lived limit (e.g.
`hit your weekly limit`, which outlasts the 6h cooldown by days) is
continuously re-marked: the 13h-old lines at the top of the file expired long
ago, while a fresh line further down is what actually blocks. `blocking_entry`
reports the deciding line, and the empty-pool error prints its timestamp — read
that, not the head of the file.

### Empty-pool error detail (#4643)

When every account is excluded, `tokens select` no longer prints a bare "All N
tokens … are marked bad or empty". It enumerates, per account, the exclusion
cause, the reason class and its permanence, the deciding entry's own timestamp,
and the cooldown remaining — plus the **identity of the binary that decided**
(version, build commit, build timestamp):

```
error: All 2 tokens in ~/.loom/tokens are marked bad or empty.
  - agent-1: bad-marked [exhaustion, TTL] at 2026-07-30T16:58:32Z — "exhausted: hit your weekly limit"; clears in 5h48m
  - agent-2: bad-marked [auth, permanent] at 2026-07-29T21:33:41Z — "401 unauthorized"; needs `loom-daemon tokens unblock agent-2`
  deciding binary: loom-daemon 0.16.0 (commit 105f9c12, built 2026-07-30T05:23:19Z)
  exhaustion cooldown: 21600s (override LOOM_TOKEN_EXHAUSTION_COOLDOWN_SECS); auth entries never expire …
```

`spawn-claude.sh` additionally logs the resolved daemon binary path and its
`--version` at token-selection time, on every spawn:

```
spawn-claude: token-selection binary: /home/you/.local/bin/loom-daemon (loom-daemon 0.16.0 (commit 105f9c12, built 2026-07-30T05:23:19Z))
```

Both exist because the selection binary is resolved by `spawn-claude.sh` itself
(`$LOOM_DAEMON_BIN` → PATH → build-output candidates), **independently of any
running daemon** — so a long-running daemon can hand a sweep child a binary that
predates the last merged selection fix. That "stale binary at the selection
site" hypothesis is now answerable straight from `.loom/logs/sweep-issue-<N>.log`
instead of by reading Rust source. (For the 2026-07-30 incident itself the
hypothesis was **refuted** — the host's resolvable binaries were all descendants
of the cooldown fix — but confirming that required inspecting the host's
installed binaries after the fact, which is exactly the forensics this log line
removes.)

**Auto-unpin** (`failure_counts`): the wrapper tracks consecutive
`TOKEN_EXHAUSTED` failures per account in `.loom/tokens/.failure_counts` (JSON).
When **every** account in the allowlist hits the threshold (default 5), the
wrapper auto-clears `.allowlist` and `.failure_counts` with a loud stderr log
line. Operators can re-pin afterwards. The threshold is `>= 5`, so a 6th failure
does not silently exceed; it still triggers (idempotent at-or-above).

Counters are reset on:
- a successful spawn for that account, or
- any operator allowlist mutation (`pin`, `unpin`, `add`, `remove`).

**Empty-pool guard**: if the selector finds the allowlist minus `.bad_tokens` is
empty, `spawn-claude.sh` exits `78` (`EX_CONFIG`) with operator instructions. It
refuses to silently auto-clear `.bad_tokens` — that masks real auth problems.

## Tests

```bash
bash .loom/scripts/tests/test-spawn-claude.sh
```

## Codex provider health

Codex profiles use a separate, provider-aware health file at
`.loom/account-health.json`. Loom never rewrites Claude's `.ranking`,
`.bad_tokens`, `.failure_counts`, allowlist, or rotation cursor for Codex.
The health file contains account names and stable reason categories only—never
`auth.json`, credential contents, or raw child output—and is written atomically
under a sibling `mkdir` lock.

For managed headless runs, `spawn-codex.sh` asks the native selector for an
eligible profile and exports that profile as `CODEX_HOME`. Selection excludes
disabled profiles, persistent `reauth_required` holds, and unexpired cooldowns;
then prefers fewer recent transient failures and round-robins equal candidates.
If none are healthy, selection exits closed rather than using ambient
`~/.codex`.

Terminal policy is intentionally conservative:

- `TOKEN_EXPIRED` requires an explicit verified-reauth clear and never expires
  merely because time passed or an ordinary success was observed.
- `TOKEN_EXHAUSTED` cools down for
  `LOOM_CODEX_EXHAUSTED_COOLDOWN_SECS` (default five hours).
- `RECOVERABLE` and `SESSION_LIMIT` apply short temporary backoffs.
- Success records freshness and clears transient counters.
- Timeouts, fatal/configuration failures, refusals, and deleted-cwd outcomes do
  not poison account health.

Codex exposes no trustworthy quota-headroom percentage here. Status/capacity
therefore reports raw, enabled, healthy, cooldown, and reauth-required counts;
transcript token totals remain observability and are never presented as
remaining quota. Claude's existing global daemon concurrency cap is unchanged.
