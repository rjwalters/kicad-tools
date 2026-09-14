# GitHub Authentication Guide

Loom uses the `gh` CLI for all GitHub interactions — label management, PR creation, reviews, merges, and issue coordination. By default, `gh auth login` grants access to all repositories the authenticated user can reach. For tighter security, you can scope Loom's access to a single repository using a fine-grained personal access token (PAT).

## Quick Start

```bash
# 1. Create a fine-grained PAT (see steps below)
# 2. Export it before running Loom
export GH_TOKEN=github_pat_xxx

# 3. Verify
gh auth status
```

## Required Token Permissions

A fine-grained PAT scoped to the target repository needs these permissions:

| Permission | Level | Used By | Purpose |
|---|---|---|---|
| Issues | Read & Write | Builder, Curator, Champion, Shepherd | Label coordination, issue creation and editing |
| Pull requests | Read & Write | Builder, Judge, Champion, Doctor | PR creation, reviews, merges |
| Contents | Read & Write | Builder, Champion | Push branches, merge PRs, delete branches |
| Checks | Read | Auditor, Judge | CI status verification |
| Metadata | Read | All roles | Implicit, always granted with any other permission |

## Creating a Fine-Grained PAT

1. Go to [GitHub token settings](https://github.com/settings/tokens?type=beta)
2. Click **Generate new token**
3. Set a descriptive name (e.g., `loom-<repo-name>`)
4. Set an expiration (90 days recommended; renew before it expires)
5. Under **Repository access**, select **Only select repositories** and choose the target repo
6. Under **Permissions**, expand **Repository permissions** and set:
   - **Contents**: Read and write
   - **Issues**: Read and write
   - **Pull requests**: Read and write
   - **Checks**: Read-only
7. Click **Generate token** and copy the value immediately — it won't be shown again

## Using the Token

The `gh` CLI checks for `GH_TOKEN` (or `GITHUB_TOKEN`) before using its default credential store. Set the variable in the shell session where Loom runs:

```bash
# Option A: Export in current session
export GH_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx

# Option B: Add to shell profile (~/.zshrc, ~/.bashrc)
export GH_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx

# Option C: Use a secrets manager or .env file (not committed)
source .env  # where .env contains: export GH_TOKEN=github_pat_xxx
```

When using Daemon Mode, set the variable before launching the daemon so all spawned terminals inherit it.

## Verifying Authentication

```bash
# Check which auth method is active
gh auth status

# Expected output with a fine-grained PAT:
#   github.com
#     ✓ Logged in to github.com account <user> (GH_TOKEN)
#     ...
#     Token scopes: (none)   ← fine-grained PATs show no classic scopes

# Test repository access
gh repo view <owner>/<repo> --json name

# Test issue access
gh issue list --repo <owner>/<repo> --limit 1

# Test PR access
gh pr list --repo <owner>/<repo> --limit 1
```

If `gh auth status` shows the default credential instead of `GH_TOKEN`, verify the variable is exported in the same shell session.

## Headless and SSH-only daemon operation (#4005)

`loom-daemon`'s own forge calls (claim reconciliation, the main-health gate,
metrics collection, the work finder, …) all shell out to `gh`, which resolves
credentials the same way an interactive shell does: `GH_TOKEN` env var →
`GITHUB_TOKEN` env var → `gh`'s own credential store (the macOS login
**keychain**, or `~/.config/gh/hosts.yml` on Linux). The keychain only unlocks
for processes running in the user's **GUI login session** — a daemon started
over SSH with a clean environment, or from a headless server with no
interactive login, cannot unlock it. Without an env-var token, every `gh` call
the daemon makes will `401`.

**The fix is an exported token, not a new credential store.** Loom does not
provision a separate daemon-managed PAT file — `export GH_TOKEN` (or
`GITHUB_TOKEN`) before starting the daemon, and the existing forwarding
mechanism carries it the rest of the way:

```bash
# On the headless / SSH-only host, before starting the daemon:
export GH_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
./.loom/scripts/cli/loom-daemon-start.sh
```

`loom-daemon-start.sh` forwards any exported `GH_TOKEN` / `GITHUB_TOKEN` /
`GITEA_TOKEN` / `FORGE_TOKEN` into the launchd plist's `EnvironmentVariables`
(macOS) or the backgrounded process's inherited environment (`--no-launchd` /
Linux) — so the daemon **and every sweep child it dispatches** see the token,
with no per-sweep configuration needed. The daemon inherits its environment
**from the shell that started it** — export the token *before* invoking
`loom-daemon-start.sh`, not after. A later `loom-daemon-update.sh` restart
re-renders the plist from the *current* shell's environment, so an
already-running daemon does not silently lose a token that was exported only
in a now-closed session (the same footgun `LOOM_WORK_FINDER` / autonomy-flag
env replay has — see [`daemon-reference.md`](daemon-reference.md)).

**Startup credential preflight.** The daemon resolves its forge credential
once at boot, immediately before its first `gh` consumer (the claim
reconciliation startup pass), and reports the outcome — `info!` naming which
mechanism won (`GH_TOKEN`, `GITHUB_TOKEN`, or `gh`'s own credential store) plus
a non-secret fingerprint (never the token itself), or `error!` naming both
remedies (export a token, or unlock the login keychain from a GUI session)
when nothing resolves. This turns the pre-#4005 failure mode — a daemon that
boots clean and then 401s silently on every forge call for the life of the
process — into a single loud, actionable line. The result is also visible
without reading logs via `loom-daemon status` ("Forge credential: OK/DEGRADED
— …"); see [`daemon-reference.md`](daemon-reference.md) for the field shape.
The preflight is read-only, bounded (never blocks daemon startup), and never
logs, prints, or serializes a token value.

**GitHub only.** The preflight covers `GH_TOKEN`/`GITHUB_TOKEN` because the
daemon's own forge calls are exclusively `gh`-CLI-based. `GITEA_TOKEN` /
`FORGE_TOKEN` forwarding still happens (for dispatched sweep children targeting
a Gitea-backed repo — see [`forge-authentication.md`](forge-authentication.md))
but the daemon process itself never calls a Gitea API, so there is nothing to
preflight for it.

**Plist permissions.** Because the rendered launchd plist embeds the token
value verbatim in `EnvironmentVariables`, `loom-daemon-start.sh` hardens the
file to mode `0600` whenever it carries a `GH_TOKEN`/`GITEA_TOKEN`/
`FORGE_TOKEN`, so a local user other than the daemon's owner cannot read the
PAT out of `~/Library/LaunchAgents`.

**Starting the daemon headlessly over SSH (#4130 — resolved).** Earlier this
section noted that `launchctl bootstrap gui/$UID` (the domain
`loom-daemon-start.sh` originally hardcoded on macOS) fails over SSH with
`error 125: Domain does not support specified action`, because `gui/$UID` is a
per-GUI-login domain that does not exist in an SSH session — so a not-yet-running
daemon could not be *started* remotely on macOS. That gap is now closed: the
shared resolver `resolve_launchd_domain()` prefers `gui/<uid>` when a GUI login
is active and otherwise falls back to the background per-user `user/<uid>` domain
that `sshd` instantiates (running as the user, not root). So a headless / SSH-only
`loom-daemon-start.sh` now completes, and stop/update find the resulting job. Pin
the domain with `LOOM_LAUNCHD_DOMAIN` if needed; the rejected alternatives
(a root system `LaunchDaemon`, `launchctl asuser`) and the login-keychain / TCC
consequences of the non-Aqua domain are covered in
[`daemon-reference.md` → "launchd domain resolution (#4130)"](daemon-reference.md).
As before, export a `GH_TOKEN` for forge auth in a headless session (the login
keychain may be locked) — the #4005 credential preflight reports this loudly.

## GitHub App identity (#4430)

Every fleet host authenticating as the same personal account (or the same
long-lived fine-grained PAT) shares one 5,000/hr REST + 5,000/hr GraphQL
budget with **every other host and the operator's own interactive use** — a
busy fleet can exhaust it fleet-wide. A GitHub App gives each **installation**
(e.g. one per GitHub account/org the fleet operates against) its own
rate-limit bucket, centralizes repo access in one place (adding a repo to the
fleet is an installation edit, not a PAT rebuild per host), and mints
short-lived (~1h) tokens on-host from a private key instead of parking a
long-lived PAT on a cloud disk. Commits/comments made with a minted token
attribute to the app's bot identity (e.g. `example-loom[bot]`), not a personal
account.

**This is entirely opt-in and fallback-first.** With no app credentials
configured (the default on every host until an operator does the setup
below), `loom-daemon` behaves exactly as described above — `GH_TOKEN`/
`GITHUB_TOKEN` env, then `gh`'s own credential store. Configuring the app
changes nothing about that fallback; it only adds a mechanism that is tried
*first*, and that falls back to the same ambient path on any failure
(unreadable/revoked key, network hiccup, GitHub API error) rather than
hard-failing.

### Setup (operator, one-time per GitHub account/org)

1. Create a GitHub App (under whichever account/org owns the target repos)
   with **Contents: Read & write**, **Issues: Read & write**, **Pull
   requests: Read & write**, **Metadata: Read** permissions.
2. Generate a private key for the app (downloads a `.pem` file) and copy it to
   each fleet host that should mint tokens for that account/org — e.g.
   `~/.config/loom/github-app-key.pem`, readable only by the daemon's user
   (`chmod 600`).
3. Install the app on the account/org's repositories (all of them, or just the
   ones Loom manages).
4. Provision the app id + private-key path to the host, either as env vars or
   in `.loom/config.json`:

   ```bash
   # Env (highest precedence) — export before starting the daemon:
   export LOOM_GITHUB_APP_ID=123456
   export LOOM_GITHUB_APP_KEY_PATH=~/.config/loom/github-app-key.pem
   ./.loom/scripts/cli/loom-daemon-start.sh
   ```

   ```json
   // .loom/config.json — config beats nothing but env (env > config):
   {
     "forge": {
       "githubApp": {
         "appId": "123456",
         "privateKeyPath": "/home/loom/.config/loom/github-app-key.pem"
       }
     }
   }
   ```

**Installation selection is derivable, not configured further.** The daemon
resolves *which* installation covers the workspace it's running in from the
repo itself (`GET /repos/{owner}/{repo}/installation`, JWT-authed) — a fleet
spanning multiple accounts/orgs needs only the one app id + key path above;
each workspace's own git remote picks the right installation automatically.

### What happens once configured

At startup (and on a periodic refresh tick thereafter, well inside the
token's ~1h lifetime), the daemon mints a JWT from the app id + private key
(RS256, signed locally via `openssl` — the key never leaves the host) and
exchanges it for a short-lived installation access token. The token is
cached on disk (`0600`, keyed by installation) and re-minted whenever fewer
than 10 minutes of its lifetime remain, so a live daemon never has to wait on
a fresh mint mid-tick.

**Delivery mechanism (#4458).** Earlier releases exported the minted token as
this process's own `GH_TOKEN` on every refresh — a `std::env::set_var` from a
background task that recurred roughly every 5 minutes for the life of the
process, racing the `environ` reads every concurrently spawned `gh`/`git`
child performs (undefined behavior on POSIX; the reason `set_var` is
`unsafe` as of Rust edition 2024). The daemon now instead owns a dedicated
`GH_CONFIG_DIR` (`<workspace>/.loom/gh-config/`, `0700`) whose `hosts.yml` it
rewrites **atomically** (write a temp file, then rename into place) on every
rotation — a pure file operation with no process-env mutation at all. `gh`
re-reads its config from disk on every invocation, so every one of the
daemon's own `gh`/`git` children (`Command::new` without `env_clear`) picks
up a fresh token automatically, without any per-call-site change. The single
`std::env::set_var("GH_CONFIG_DIR", …)` that points the process at this
directory (clearing any ambient `GH_TOKEN`/`GITHUB_TOKEN` at the same time,
so the app token isn't outranked by an operator-exported one) fires at most
once per process lifetime, before any task that spawns `gh`/`git` exists to
race it.

`loom-daemon status` and the startup log line report this as mechanism
`github-app` with a **non-secret fingerprint** — `app <id> installation
<id>` — never the token, JWT, or key material itself:

```
Forge credential: OK — github-app (app 123456 installation 789)
```

### Troubleshooting the app path

- **Unreadable/revoked/rotated key**: the daemon logs the failure by name
  (`credential_preflight: github-app mint failed (…)`) and falls back to
  ambient `gh` auth rather than hard-failing — check `LOOM_GITHUB_APP_KEY_PATH`
  / `forge.githubApp.privateKeyPath` points at a file the daemon's user can
  read.
- **App not installed on this repo/org**: the installation-resolution call
  (`GET /repos/{owner}/{repo}/installation`) 404s; install the app on the
  repo, or verify the workspace's git remote points at the account/org the
  app is installed on.
- **Clock skew**: the minted JWT's `iat` is backdated 60 seconds per GitHub's
  own guidance, tolerating modest host clock drift without a manual fix.

### The cached-permission window: `403 … not accessible by integration` (#6074)

An installation token is minted with the permissions the installation held **at
mint time**, then reused from the on-disk cache for up to ~1h. So there is a
window — after a permission grant has already propagated on GitHub's side, before
the cached token turns over — in which one write scope is present and another is
not. Observed live on 2026-08-12: a Builder's `git push` **succeeded**
(`Contents:write` was in the cached token) and the very next `gh pr create`
returned

```
HTTP 403: Resource not accessible by integration
```

because `Pull-requests:write` was not. This is **not** rate-limit exhaustion (a
REST retry with the same token 403s identically) and **not** a mint failure (the
mint succeeded — `run_with_github_app`'s ambient-auth fallback only covers a
token that cannot be minted at all), so nothing upstream noticed. The sweep died
with no PR, the issue stayed ready, and the next dispatch **rebuilt the identical
work**, leaving an orphaned `feature/issue-N` branch behind each pass.

Every Loom write call site now escalates on — and only on — that one signature,
via `forge_gh_perm_safe` in `.loom/scripts/lib/forge-helpers.sh`:

| Rung | Credential | Why |
|---|---|---|
| 1 | whatever `gh` already resolves | the normal path; nothing changes when it works |
| 2 | a **force-minted** installation token (`github-app-token.sh get-token --force`) | bypasses the ~1h cache, so an already-propagated grant applies immediately instead of being waited out |
| 3 | a personal token — `LOOM_PERSONAL_GH_TOKEN`, else the operator's own `gh auth login` credential (reached by dropping the daemon-owned `GH_CONFIG_DIR`/`GH_TOKEN`) | a credential the App permission set cannot gate at all |

Rung 3 is skipped when it would be a verbatim replay of rung 1 (no
App-delivered credential in the environment and no `LOOM_PERSONAL_GH_TOKEN`).
Any other failure — including a genuine permission misconfiguration on a
personal token — propagates unretried, exactly as before.

Wired call sites: `create-pr.sh` (PR creation), `create-issue.sh` /
`forge_gh_create_issue_rl_safe` (issue filing), `forge_gh_comment_rl_safe`
(comments), `forge_gh_swap_label_rl_safe` (label edits), and the sweep's
own Builder-recovery PR creation.

**The merge itself is wired too (#6752).** `merge-pr.sh`'s primary merge path
calls the *native* `loom-daemon forge auto-merge`, whose forge-write code lives
in Rust entirely outside this bash ladder, and its synchronous path issued a
bare `gh api … -X PUT`. Both hard-failed on the integration-403 until #6752 —
observed on 2026-08-22 (`/loom:sweep 6746`, PR #6751), where comment/label
writes recovered through the ladder but the merge died, and the operator had to
`unset GH_CONFIG_DIR` by hand (rung 3, performed manually) to finish it. The
ladder is now command-agnostic: `forge_cmd_perm_safe <cmd> …` runs the same
three rungs around **any** command whose credential comes from the environment
(`loom-daemon forge …` shells out to `gh`, so the same `GH_TOKEN` /
`GH_CONFIG_DIR` swap reaches it), and `forge_gh_perm_safe` is now just its
`gh`-prefixed spelling — one implementation, so the two cannot drift. The
wrapped command's exit code is preserved verbatim, so `loom-daemon forge
auto-merge`'s meaningful codes (3 = forge declined → shell fallback, 4 =
head-SHA mismatch → re-queue) still reach `merge-pr.sh` unretried and
unrewritten.

**Builders never lose work to this window.** `create-pr.sh` adopts an
already-open PR for the head branch instead of creating a second one, and the
sweep's Builder validation now opens the PR from an **already-pushed** branch
rather than failing with "no uncommitted or unpushed changes" — so a 403 that
lands between `git push` and PR creation costs one retry, not a full rebuild.
If you find a `feature/issue-N` branch with no PR, re-run
`./.loom/scripts/create-pr.sh` from it; do not rebuild.

### Wrong-repo `GH_CONFIG_DIR`: `HTTP 404` / "could not resolve to a Repository" (2am#446)

A **different** failure from the cached-permission window above: the
credential isn't missing a scope, it's scoped to an entirely different
repository. Observed live 2026-08-21 (a daemon-dispatched sweep on
`loom-worker-2`): the dispatch environment's `GH_CONFIG_DIR` pointed at a
different owner's flat `<workspace>/.loom/gh-config/` (a daemon workspace's
own credential, minted for a different org), so every `gh` call against the
target repo failed outright with one of:

```
GraphQL: Could not resolve to a Repository with the name '<owner>/<repo>'. (repository)
gh: Not Found (HTTP 404)
```

`forge_gh_perm_safe`'s #6074 ladder above never fires for this signature — its
classifier only matches the "not accessible by integration" wording, so rung 1
(ambient) just fails outright. `sweep-lease-renew.sh`'s renewal loop failing
on every cycle as a direct result was the incident's original symptom.

`forge_gh_repo_safe` (also in `.loom/scripts/lib/forge-helpers.sh`) adds one
more rung **on top of** — not instead of — the #6074 ladder: it tries
`forge_gh_perm_safe` first (so a permission-scope 403 still recovers exactly
as before), and only on a confirmed `is_repo_mismatch_error` signature
escalates further:

| Rung | Credential | Why |
|---|---|---|
| 1–3 | `forge_gh_perm_safe`'s own ladder (table above), tried first | an app-permission 403 recovers here, unrelated to this rung |
| 4 | the **owner-partitioned** credential directory, `<daemon_workspace>/.loom/gh-config-by-owner/<owner>/` (owner parsed from `git remote get-url origin`, zero API calls) | a GitHub-App daemon workspace already maintains this directory *alongside* its flat `gh-config/` — see "Delivery mechanism (#4458)" above and FLEET.md's "Private-repo clones over ssh" contract — so the correctly scoped credential is usually already on disk right next to the wrong one that got exported |
| 5 | `env -u GH_CONFIG_DIR` (drop it entirely) | when rung 4's directory doesn't exist, or also fails |

Rung 4's directory is **derived from the current (wrong) `GH_CONFIG_DIR`
itself** — no `hosts.yml` lookup, no hardcoded `loom` — by recognizing the
flat `<X>/.loom/gh-config` shape and substituting
`gh-config-by-owner/<owner>` for its final segment
(`LOOM_GH_CONFIG_BY_OWNER_ROOT` overrides this for testing). The whole rung is
**skipped** when `GH_CONFIG_DIR` was never set to begin with — there is
nothing to escalate away from, and `env -u GH_CONFIG_DIR` would just replay
rung 1 verbatim, the same "don't replay an identical attempt" rule rung 3 of
the #6074 ladder already applies to itself.

`is_repo_mismatch_error` is intentionally loose on the REST side (a plain
`HTTP 404` on a genuinely missing issue/PR on a *correctly* scoped repo also
matches) — safe here because `forge_gh_repo_safe` only ever retries the SAME
call under a different credential and returns the last attempt's real error
on failure, so an over-firing match costs one extra local `gh` call, never a
wrong result.

Wired call sites: `resolve-tier-model.sh` (issue-body fetch, both the GraphQL
and REST-fallback attempts) — previously the one call site in this class that
did not source `forge-helpers.sh` at all, so a wrong-repo 404 there silently
fell through to the tier-3 default with a misleading "likely API quota"
diagnostic instead of recovering or naming the real cause.

### Long-running sweep children and credential snapshots (#4458)

The daemon's own forge calls (claim reconciliation, the main-health gate, the
refresh tick, work finder, …) all run inside the one long-lived daemon
process, so once `GH_CONFIG_DIR`/`hosts.yml` are set up they stay current for
every one of those calls, for the life of the process — the daemon's own
forge auth never goes stale between restarts.

**Dispatched sweep children are a different story.** A sweep worker (the
tmux-hosted Claude session `loom-daemon` spawns per issue/PR, often running
for an hour or more on a complex issue) inherits whatever forge-credential
environment is ambient **at the moment it is spawned** — an operator-exported
`GH_TOKEN`/`GITHUB_TOKEN`, or the daemon's own credential setup, whichever
resolves. That is a **snapshot**, not a live subscription: env beats the
ambient keyring/`gh`-config lookup inside that child's own process (the same
precedence order described throughout this doc), so if the snapshotted
reference stops being valid partway through a long sweep — a GitHub App
installation token's ~1h lifetime is the common case — the child does not
re-resolve credentials on its own. Its own `gh` calls start 401ing with **no
automatic fallback**, and the daemon rotating its own credential afterwards
does not reach an already-running child. If you run consistently long sweeps
against a GitHub-App-only host, prefer a long-lived PAT (`export GH_TOKEN`
before starting the daemon — see "Headless and SSH-only daemon operation"
above) for that workload, or expect to restart a sweep that 401s mid-run past
the ~1h mark.

## Fleet rate-limit protections are `loom-daemon`-internal (#4432)

Epic #4432 ("survive GitHub API rate limits at fleet scale") shipped real
mitigations for the shared-budget problem stated at the top of the GitHub App
section — a rate-limit circuit breaker, ETag-cached hot polls, per-installation
App tokens, and moving claim coordination off label polling. **Every one of them
lives inside the `loom-daemon` process.** None is wired into the spawn scripts:

```bash
grep -n 'rate_limit_breaker\|forge_listing\|github-app' \
  .loom/scripts/spawn-claude.sh .loom/scripts/claude-wrapper.sh
# → no matches
```

So an operator running the older hand-rolled per-issue pattern — one
`claude-wrapper.sh --dangerously-skip-permissions "/loom:sweep <N>"` shell per
in-flight issue, **with no `loom-daemon` running at all** — gets *zero* benefit
from any of it. That is not an oversight awaiting a wrapper flag: the breaker's
state, the ETag cache, and the minted-token refresh tick are all in-process
state of one long-lived daemon, with no cross-process protocol for independent
shells to join.

| Mechanism | Implementation | Daemon-dispatched sweeps | Hand-rolled `claude-wrapper.sh` loop |
|---|---|---|---|
| Rate-limit circuit breaker (#4429 → #4440) | `loom-daemon/src/rate_limit_breaker.rs` | Governs the **daemon's own** forge polls (work finder, claim/quarantine reconciliation, role runner, epic supervisor) — pauses them until the window resets | **None** |
| ETag-cached REST listings (#4428 → #4443) | `loom-daemon/src/forge_listing.rs` | Same scope — the daemon's hot polls; an unchanged poll returns 304 and costs no quota | **None** |
| GitHub App installation tokens (#4430 → #4454, #4578) | daemon credential preflight + daemon-owned `GH_CONFIG_DIR` | Reaches spawned children too: they inherit the daemon's `GH_CONFIG_DIR`, so their `gh` calls draw on the **per-installation** bucket instead of the operator's personal 5,000/hr | **None** — every session uses whatever ambient `gh auth` / `GH_TOKEN` the operator's shell carries, i.e. one shared personal budget |
| Claim/peer coordination off label polling (#4431) | safehouse ([`safehouse.md`](safehouse.md)) | Removes a whole class of repeated label reads from the poll path | **None** |

**Read the middle column precisely.** The breaker and the ETag cache bound the
daemon's *own* polling loops; they do **not** govern the `gh` calls a spawned
Claude session makes while it works an issue. What bounds those is admission
control — `autonomous.workFinder.maxConcurrent` and the token / disk terms
folded into the same `min(...)` ceiling (the fourth cpu/load term was deleted in
#4512 — see [`daemon-reference.md`](daemon-reference.md)) — which caps how many
sessions exist at once on a host. A hand-rolled loop has no equivalent: N
shells started by hand are N unbounded `gh` consumers, and nothing
coordinates them across the other projects sharing the same machine and the
same `gh auth` login.

### Recommendation: dispatch through the daemon

If you run several repos/projects concurrently on one machine under one GitHub
login, migrate off the hand-rolled pattern and dispatch via `loom-daemon`
(`mcp__loom__dispatch_sweep`) — one daemon per workspace, one admission ceiling
per host, and the table's middle column instead of its right-hand one. This is
also the already-documented direction of travel: `spawn-loop.sh`, the shipped
version of that pattern, was **removed in v0.11.0** in favor of
`mcp__loom__dispatch_sweep` (see `CLAUDE.md` → Migration History). Parallel
hand-started `claude-wrapper.sh` sweeps are not a supported scaling story; they
are the configuration reported in #4665 — 15+ concurrent sweep/judge sessions
for a single project, multiplied across every other project on the box, with
`loom-daemon status` confirming no daemon was running.

Before scaling out, confirm which regime you are in:

```bash
loom-daemon status   # "Forge credential: OK — github-app (app … installation …)"
                     #   ⇒ daemon-managed, per-installation bucket
                     # no daemon running ⇒ none of the above protections apply
gh api rate_limit --jq '.resources.core, .resources.graphql'
```

### Known limitation, even under daemon dispatch

There is **no per-sweep credential re-resolution** in the spawn path. A
dispatched sweep child works from whatever forge-credential reference was
ambient at spawn time. Where that reference is the daemon's own `GH_CONFIG_DIR`,
rotation does reach it (`gh` re-reads `hosts.yml` on every invocation — #4578);
where it is an inherited `GH_TOKEN`/`GITHUB_TOKEN` env value, env outranks `gh`
config and the value is **frozen for the life of the sweep**, so a sweep that
outlives a ~1h App-token lifetime starts 401ing with no automatic fallback. See
"Long-running sweep children and credential snapshots (#4458)" above for the
mechanics and the workaround (a long-lived PAT for consistently long workloads).

## Filing issues under GraphQL exhaustion

GitHub's GraphQL quota and REST quota are independent buckets, and `gh issue
create` is GraphQL-backed with no REST fallback of its own (#5047) — file new
issues with `./.loom/scripts/create-issue.sh`, never a bare `gh issue create`.
Full recipe, the atomic create+label requirement, the scripted
`forge_gh_create_issue_rl_safe` equivalent, and why `loom-daemon forge issue
create` is NOT a fallback: [`gh-issue-create-rest-fallback.md`](gh-issue-create-rest-fallback.md).

## Troubleshooting

### Token not being picked up

- Confirm `echo $GH_TOKEN` shows the token value
- The variable must be **exported**, not just set: `export GH_TOKEN=...`
- If using Daemon Mode, restart the daemon after setting the variable

### Permission errors (403 / insufficient scope)

- Verify the PAT is scoped to the correct repository
- Check that all required permissions are granted (see table above)
- Fine-grained PATs do not show classic scopes in `gh auth status` — this is expected

### Token expired

- Fine-grained PATs have an expiration date set at creation
- Generate a new token and update the `GH_TOKEN` value
- Consider setting a calendar reminder before expiration

## Security Notes

- **Never commit tokens** to the repository. Add `.env` to `.gitignore` if using an env file.
- Fine-grained PATs are more secure than classic tokens because they limit both repository and permission scope.
- Use the minimum permissions required. The table above lists exactly what Loom needs.
- Rotate tokens periodically — 90-day expiration is a reasonable default.
