---
name: "deps"
description: "Third-party dependency currency — reconcile organization policy, Renovate or Dependabot setup, and bot PRs; report-only under --check"
domain: repo
type: command
user-invocable: true
---

# /repo:deps — Third-Party Dependency Currency

Keep the repo's **third-party dependencies** current: npm / pip / cargo / Go
packages and GitHub Actions. Two halves, usually run together:

1. **Resolve organization policy and verify the updater** — Renovate or
   Dependabot, independently of GitHub vulnerability alerts and security PRs.
2. **Triage dependency PRs** — actual changes, age eligibility, security
   relevance, CI coverage, and merge policy.

This is the companion to [[update-tools]], not a part of it. `update-tools`
compares *installer-managed tool packages* (Loom, Anvil, Repo Skills) against a
local source clone; there is no source clone to diff for Dependabot, and
"triage incoming bot PRs" is a different activity from "update an installed
package." Keeping them separate keeps `update-tools`' comparison model intact.

Everything here either writes repo config, flips a repository setting, or
merges a PR — so like `release`, `remote`, `followups`, and `update-tools`,
this command requires authorization for writes and merges. Apply existing
authorization when it covers the concrete changes; otherwise show the changes
and confirm first. `--check` is always report-only.

## Usage

```
/repo:deps                  # Report policy, updater status, and open dependency PRs
/repo:deps --check          # Report only — never writes, never merges
/repo:deps --install        # Only setup reconciliation (policy + config + settings)
/repo:deps --review         # Only the PR-triage half
/repo:deps --review 123     # Triage one PR in depth
/repo:deps --all-repos               # Fan-out survey: migration state across the current repo's org, report-only
/repo:deps --all-repos --owner OWNER # Same, for an explicit org/owner
```

`--all-repos` is a distinct mode from everything else above: it surveys many
repos instead of acting on the invoking one, and it is **always** report-only
— see "Fan-out survey" below. All other flags operate on the single repo
`/repo:deps` is run from, as before.

## Prerequisites

This command currently supports GitHub. Confirm the repo is on GitHub before doing
anything else — if `origin` points at Gitea or another forge, say so and stop
rather than scaffolding config that will never run:

```bash
git config --get remote.origin.url    # → derive OWNER/REPO; must be a GitHub host
gh auth status
```

## 0. Resolve policy and choose the provider

Read root `repo-policy.json` from `OWNER/.github` on its default branch, if
accessible. This is the deployed organization policy. It records the canonical
source in **rjwalters/repo/policies/**, its revision, desired provider/settings,
and `dependencies.renovatePreset`. [[org-policy]] compares that installed copy
with the canonical source and prepares organization updates. Do not invent a
second organization preference file in a client or change an installed org copy
as if it were authoritative. An absent file, access failure, and invalid policy
are distinct states; do not silently fall back after a permissions/network or
validation error.

Also detect existing Renovate config, including `renovate.json`, `renovate.json5`,
`.github/renovate.json[5]`, `.renovaterc[.json]`, `.renovaterc.json5`,
`.gitlab/renovate.json[5]`, and the `renovate` key in `package.json`, alongside
Dependabot config. Inspect Renovate's current supported config locations if
none of these explains an active installation. Preserve an existing provider
unless migration is authorized. With no provider or organization policy, offer
[[org-policy]] before proposing new automation; do not silently assume that
missing `dependabot.yml` means dependencies are unmanaged.

Report separately: deployed policy source/revision and drift, provider/config,
**GitHub App installation status** (Renovate path only — see below), app
operation, dependency graph, vulnerability alerts, security PR provider,
effective release-age rules, automerge/required checks, and open bot PRs.
`dependabotSecurityUpdates: false` is expected when Renovate owns security PRs;
it is not a finding that should be "fixed" by enabling a second provider.

### Fan-out survey — org-wide migration report (`--all-repos`)

Everything else in this command operates on the single repo it is run from.
`--all-repos` is a different mode: it answers "where does the org-wide
Dependabot→Renovate migration stand, across every repo?" It is a **read-only
survey** — it never writes to, configures, or merges anything in any of the
repos it enumerates. To act on what the survey finds, run `/repo:deps`
(optionally `--install`/`--review`) against that one repo; this mode only ever
reports.

#### Enumerate the org's repos

Derive OWNER from an explicit `--owner OWNER`, or from `origin` of the
invoking repo when omitted — the same derivation [[org-policy]] uses for its
own client-owner default. Page through every repo; a single unpaged request
silently drops repos past the first page in a larger org:

```bash
# Organization account
gh api orgs/OWNER/repos --paginate --jq '.[] | select(.archived == false) | .full_name'

# Personal account — the org endpoint 404s for a user account, so fall back
# rather than guessing which kind of account OWNER is
gh api users/OWNER/repos --paginate --jq '.[] | select(.archived == false) | .full_name'
```

Exclude archived repos from the survey by default and say so in the report
header (e.g. "14 repos, 1 archived excluded") rather than silently shrinking
the count. A repo the invoking token cannot see at all never appears in this
listing — that is expected, not a gap to report. A repo that *is* listed but
then fails a per-repo read below (private repo whose contents/settings the
token can list but not read, a repo that went private after listing) is a
different failure and does need reporting — see "could not check" below.

#### Classify each repo's migration state

For every enumerated repo, run the same detection this command already
performs on the invoking repo in step 0 and step 1 above — `.github/dependabot.yml`
/ `.github/dependabot.yaml` presence, the dedicated `automated-security-fixes`
flag, and Renovate config presence (`renovate.json`, `renovate.json5`,
`.github/renovate.json[5]`, `.renovaterc[.json]`, `.renovaterc.json5`, the
`renovate` key in `package.json`, per step 0's detection list). Classify each
repo into exactly one state:

| State | Meaning |
|---|---|
| `dependabot-only` | Dependabot version-update config and/or the security-updates flag present; no active Renovate config |
| `both-active` | Both Dependabot (config or security flag) and an active Renovate config present — the dangerous mid-migration state where two updaters can race |
| `renovate-only` | Active Renovate config present; no Dependabot version-update config. The security-updates flag is independent (step 1) and does not by itself make a repo `both-active` |
| `unmanaged` | Neither provider configured |
| `could not check` | A per-repo read failed (403, network, or similar); report the failure explicitly rather than guessing a state or silently dropping the row |

Also report, per repo:

- **Preset adopted?** — whether the repo's Renovate config `extends` the
  deployed `github>OWNER/.github:renovate-config` preset (see [[org-policy]]
  "Installed files and client adoption"). Apply the ordering check below
  before reporting this column as a plain gap.
- **Open bot dependency PRs** — a count, using the same author-filter step 7
  already uses (`app/dependabot`, `app/renovate`, or the configured
  self-hosted identity for that repo).

#### Ordering check: the org preset must be merged before any client can adopt it

Compute this once per survey run, not once per repo. A client's
`extends: "github>OWNER/.github:renovate-config"` only resolves once
`OWNER/.github`'s organization policy PR — the one [[org-policy]] `--install`
opens, titled `chore: reconcile organization dependency policy` — has merged
on that repo's default branch. Confirm the preset is actually live rather than
assuming a merged PR:

```bash
gh api repos/OWNER/.github/contents/renovate-config.json --jq '.sha' 2>/dev/null
# present on the default branch → preset is live; absent → not yet published/merged
```

If the preset is **not yet live**, no repo in the survey can be "eligible to
adopt" it yet: report every repo's preset-adoption column as **"not adopted —
preset not yet available"**, distinct from an ordinary adoption gap. Once the
preset is confirmed live, report each repo's actual state — already adopted,
or a real adoption gap now that adoption is actually possible.

#### App installation is also a once-per-survey org-level fact

Run the App-installation check below once per survey too, against the survey's
OWNER, and put its result in the report header beside the preset line. The
failure this guards against is the single-repo one at org scale: a survey can
truthfully report a live preset and a column of adopted repos while **no repo
in it will ever receive a PR**, because the App was never installed. A
`renovate-only`, preset-adopted row is not evidence the App is installed.

```bash
python3 .claude/skills/repo/scripts/repo-org-policy.py check-app --owner OWNER
```

#### Report format

```
ORG MIGRATION SURVEY — OWNER (14 repos, 1 archived excluded)
==============================================================
Preset (OWNER/.github renovate-config.json): live on default branch
Renovate GitHub App: NOT installed — no PRs will be raised for ANY repo below
  until it is. Install: https://github.com/apps/renovate (organization
  owner/admin required).

| Repo          | State           | Preset adopted            | Open bot PRs |
|---------------|-----------------|----------------------------|--------------|
| OWNER/alpha   | renovate-only   | yes                        | 2            |
| OWNER/beta    | both-active     | no                         | 5            |
| OWNER/gamma   | dependabot-only | no                         | 1            |
| OWNER/delta   | unmanaged       | no                         | 0            |
| OWNER/epsilon | could not check | — (403 reading contents)  | —            |
```

Never propose or make a write from this mode, even with authorization already
covering single-repo writes elsewhere. Point at running `/repo:deps`
(optionally `--install`/`--review`) against a specific repo as the next step
for any row that needs action; a `both-active` row that needs a deliberate,
tracked Dependabot shutdown is a candidate for [[followups]]-style per-repo
tracking, filed against that repo, not this survey — which is exactly what the
Renovate path's "Deferred Dependabot shutdown" step below offers to do, one
repo at a time, when run against that repo.

`--all-repos` is unconditionally report-only regardless of `--check` — `--check`
has nothing further to restrict here, since this mode never writes in the
first place.

### Renovate setup path

When Renovate is selected, use this path instead of Dependabot steps 1, 4–6.
Reuse steps 2–3 below for ecosystem/ownership detection and label validation.

- Confirm the organization policy PR is merged and its preset is readable by
  the Renovate installation. Config presence alone does not prove an active
  GitHub App or a successful scan: inspect onboarding, the Dependency Dashboard,
  and job logs. **All three of those are simply absent, not contradictory,
  when the App was never installed at all** — so check installation directly
  rather than inferring it from their absence:

  ```bash
  python3 .claude/skills/repo/scripts/repo-org-policy.py check-app --repo OWNER/REPO
  ```

  This is the same [[org-policy]]-owned check `/repo:org-policy` runs on its
  own `plan`/`apply` — reused here rather than duplicated, since a client
  repo's Renovate installation lives on the same organization/user account
  `org-policy` already checks. Report the result as a distinct line, not
  folded into "policy: OK":
  - **Installed** — proceed normally.
  - **NOT installed** — state this plainly, e.g. `Renovate: policy published,
    app NOT installed — no PRs will be raised until it is`, name the install
    URL (`https://github.com/apps/renovate`), and note that installing it
    requires an organization owner/admin — who may not be the person running
    this command. Continue the rest of this path anyway (config reconciliation
    below is still correct groundwork), but do not report overall success.
  - **UNKNOWN** — this token cannot see the installations endpoint for that
    account (non-admin org member, or a personal account other than the
    caller's own). Report it as unresolved, not as either "installed" or "not
    installed".
- Update the existing active Renovate config (do not add a competing file) to
  extend the deployed `dependencies.renovatePreset`, usually
  `github>OWNER/.github:renovate-config`. Preserve intentional client exceptions
  and report any override that changes age or merge policy. Use a minimal
  `renovate.json` with that `extends` entry only when no config exists.
- Exclude the installer-owned roots discovered in step 2a through `ignorePaths`,
  preserving existing exclusions. Skip dependency-free manifests. Validate the
  resulting effective native config with `renovate-config-validator`.
- Keep the dependency graph and Dependabot alerts enabled and grant Renovate
  read access to alerts. Probe the dedicated alerts and security-fixes endpoints
  shown in step 1. Treat `dependencies.dependabotSecurityUpdates` as desired
  state: disable Dependabot PR generation only after Renovate's fix coverage is
  verified for this client's ecosystems and lockfiles. Report pending migration
  instead of temporarily removing all security-fix automation — and record that
  pending migration somewhere that outlives this session, per "Deferred
  Dependabot shutdown" below.
- During migration, remove/disable the overlapping Dependabot version-update
  entries once Renovate is operational. Retain any deliberately assigned
  Dependabot-only coverage and inspect old PRs individually; do not bulk-close
  them just because the provider changed.
- Verify release timestamps and package-manager age controls, including new
  transitive versions during lockfile generation. The baseline is 14 days for
  routine versions, one day for advisory-backed security fixes. Check that the
  explicit security age override is honored by the deployed bot; its default
  security behavior is zero delay. A changelog claim or patch version number is
  insufficient to automatically grant the security exception. An emergency
  override requires the affected advisory/dependency and explicit authorization;
  scope it to this client and remove it afterward.
- Eligibility and merging are separate. The shared baseline leaves automerge
  off. Before opting in, verify required checks cover the affected ecosystems;
  never bypass CI or assume all Actions majors are low-risk. Do not attach
  reserved Loom labels to route bot PRs into another merge pipeline.

Show concrete config/settings changes before applying authorized setup. Continue
with the common PR review below. Under `--check`, make no changes, including
organization deployments, client files, flags, or merges.

#### Deferred Dependabot shutdown — offer to file a tracking issue (confirm first)

"Report pending migration" above leaves the second half of the migration in
scrollback, and a terminal transcript is not a handoff. Once the session ends,
nothing in the repo, the organization policy, or an issue tracker records that
this repo is sitting in the `both-active` state the `--all-repos` survey calls
the dangerous one — a state this path reaches by being followed *correctly*,
not by anyone skipping a step. So close this path by **offering to file** that
deferral as an
issue, against the **client repo** (the repo `/repo:deps` is running against,
never upstream and never `OWNER/.github` — it is the client's migration to
finish). This is the per-repo version of the tracking the `--all-repos` survey
points at for a `both-active` row.

Offer, never file automatically: filing is an outward-facing write, so it takes
the same confirm-first posture as every other write in this command — show the
target repo, title, and full body, and file only what is approved.

**When to run it.** At the end of this path, after the reports above, when
**both** of the following hold:

- Renovate is the selected provider and an active Renovate config is present
  here, *and*
- Dependabot still generates — or is still authorized to generate — PRs, in
  either of these two independent forms (step 1 reports all three Dependabot
  signals separately; read them from their dedicated endpoints, not from
  `security_and_analysis`):
  - a tracked `.github/dependabot.yml[.yaml]` whose `updates:` entries overlap
    the ecosystems Renovate now covers (version updates), **or**
  - `automated-security-fixes` enabled while the deployed policy's
    `dependencies.dependabotSecurityUpdates` is `false` (security updates).
    `paused` counts as enabled here: the authorization is still in place and
    GitHub can resume it at any time, so the shutdown is still pending.

**The alerts flag is never a trigger on its own.** This path deliberately keeps
the dependency graph and Dependabot *alerts* enabled and grants Renovate read
access to them, so `vulnerability-alerts` being on is the intended end state,
not an outstanding migration step. A repo whose only remaining Dependabot
signal is alerts is `renovate-only` and gets no offer.

**Do not guess past an unresolved read.** If the deployed policy was absent or
unreadable (step 0 keeps those distinct from "policy says false"), or if
`automated-security-fixes` returned `403` so the flag is UNKNOWN (needs admin,
step 1), that signal is not evidence of a pending shutdown: report it as
unresolved and do not offer on the strength of it. A version-update config,
which is read from the git index rather than a permissioned endpoint, still
triggers the offer on its own.

**Under `--check`, skip this step entirely** — do not run the dedup search, do
not draft a body, do not offer. `--check` is report-only, and filing an issue
is a write to another repo. Report the pending migration as a finding and stop
there. `--all-repos` never reaches this step at all: that mode is
unconditionally report-only and acts on no repo it enumerates.

##### 1. Dedup first — never re-offer what is already tracked

Before drafting anything, check the client repo for an open item already
covering this migration, using the same REST search recipe [[followups]] uses
for dedup (`gh api search/issues`, not `gh issue list --search`, which is
GraphQL-backed and exhausts first on a busy agent host):

```bash
gh api "search/issues?q=repo:OWNER/REPO+state:open+dependabot+renovate+migration&per_page=30" \
  --jq '.items[] | "#\(.number) \(.title) \(.html_url)"'
```

Pull requests are deliberately in scope — `search/issues` returns both, and an
open PR that removes `.github/dependabot.yml` is a *stronger* dedup signal than
an open issue, because the work is already in flight. Check `html_url` for
`/pull/` vs `/issues/` to tell which, and say so when reporting a match.

- **Match found** — report it (`Dependabot shutdown: already tracked in #N`)
  and make no offer. Re-running `/repo:deps` on a repo with an open tracking
  item must never produce a second one.
- **Near-match** — flag it with its number/URL and let the user choose: file
  anyway, skip, or comment on the existing one. Never silently file over it and
  never silently drop it.
- **No match** — draft the body below and offer it.

Widen the terms if the first query is empty and a differently-worded issue is
plausible (`+disable+dependabot`, `+both-active`); search is a third rate-limit
bucket again, so an extra query costs nothing from the `core` budget the filing
step needs.

##### 2. The body must make acting on it later a decision, not a re-investigation

Whoever picks this up will not have this session's output. Draft the body with
all four of these, filled in from what this run actually observed — not as a
generic "finish the migration" stub:

- **Which Dependabot surfaces are still active**, as the three independent
  items step 1 reports, each with its observed value: the version-update config
  (which path, which ecosystems), the security-updates flag, and the alerts
  flag — marking alerts as **deliberately retained**, so a later reader does not
  "finish the job" by turning off the alerting Renovate depends on.
- **What must be verified before disabling anything**: that Renovate has
  actually opened PRs for *this* repo's ecosystems (not merely that its config
  is present and the App is installed), and that its fix coverage includes
  lockfile-only security updates — the case where the advisory is resolved by a
  transitive bump with no manifest change, which is exactly the coverage
  Dependabot is being kept around for.
- **The concrete commands/settings to flip them off**, so the follow-up is
  mechanical:

  ```bash
  # Version updates: delete the config, or drop only the overlapping
  # `updates:` entries if some coverage is deliberately Dependabot-only.
  git rm .github/dependabot.yml

  # Security updates: the same dedicated endpoint step 1 reads, DELETE to
  # disable (needs admin; a 403 means it needs a repo admin, not that it failed
  # silently).
  gh api --method DELETE repos/OWNER/REPO/automated-security-fixes

  # Alerts: leave ENABLED. Renovate reads them. Listed here only so nobody
  # reaches for `gh api --method DELETE repos/OWNER/REPO/vulnerability-alerts`
  # while "turning Dependabot off".
  ```

- **A pointer back to the deployed desired state**: the organization policy's
  `dependencies.dependabotSecurityUpdates: false` in root `repo-policy.json` on
  `OWNER/.github`, named with the revision this run read, plus the note that
  `/repo:deps` will re-report this repo as `both-active` until it is done.

Scrub the body before proposing it, per [[followups]]' authoring-time rule:
this body is drafted from a working session and filed into a repo that may be
public, and an issue body is only `removable-by-deletion` afterward. Keep it to
this repo's own observable configuration state — no session counts, no other
clients' names.

##### 3. File it with the REST recipe, not `gh issue create`

On approval, file exactly as [[followups]] step 5 does — write the body to a
**literal** scratch path (never a shell variable as the redirect target or the
`--input` argument; the destructive-write guard denies an unexpanded-variable
write target), then POST it through REST:

```bash
# Write the body to /tmp/deps-dependabot-shutdown.md with your own file-write
# capability — not a shell heredoc (a body containing backticks or `$(…)` is
# still shell input and gets tokenized).

jq -n --arg t "Finish Dependabot→Renovate migration: disable Dependabot PR generation" \
  --rawfile b /tmp/deps-dependabot-shutdown.md \
  '{title: $t, body: $b, labels: []}' > /tmp/deps-dependabot-shutdown-payload.json

gh api --method POST "repos/OWNER/REPO/issues" \
  --input /tmp/deps-dependabot-shutdown-payload.json --jq '.html_url'
```

`labels: []` is deliberate, matching [[followups]]: this is outward-facing
tracking, not pipeline work, so it applies no `loom:*` (or other) labels and
leaves triage to the client repo. Do not substitute `gh issue create` (GraphQL,
and the pool you least want to lose *after* the user has approved) and do not
pass the body as `--body @path` or `-f body=@path` — both post the literal
string `@path` rather than the file's contents. Print the resulting issue URL
in the report.

##### 4. Report an existing tracking issue as resolvable once the evidence is in

When a tracking item is already open (the dedup match above) and this run
observes the migration actually finished — no overlapping version-update
config, `automated-security-fixes` disabled, Renovate raising PRs for this
repo's ecosystems — say so: `Dependabot shutdown: #N appears resolvable
(dependabot.yml absent, security-updates off, N open Renovate PRs)`. Offer, on
the same confirm-first terms, to post that evidence as a comment via the same
REST shape (`gh api --method POST repos/OWNER/REPO/issues/<n>/comments --input
<payload>`; `gh issue comment` is GraphQL-backed). Never close the issue
automatically, and never post the comment under `--check`.

## Steps — Dependabot install / verify

Use this path when Dependabot remains the chosen provider. An organization's
Renovate policy is a migration proposal until adoption is authorized, not a
reason to silently replace a working Dependabot installation.

### 1. Report config and the security flag as two distinct items

Writing `.github/dependabot.yml` enables **version** updates only. Dependabot
**security** updates are a repository setting that is entirely independent — a
repo can have a perfectly good config file and still have CVE alerting off.
Check and report both:

```bash
git ls-files '.github/dependabot.yml' '.github/dependabot.yaml'   # version updates

# Security updates — a dedicated endpoint, NOT a security_and_analysis key.
# Read the WHOLE object, never `--jq '.enabled'` alone: it answers with TWO
# fields, {"enabled": bool, "paused": bool}, encoding three states (below);
# 403 → needs admin (see UNKNOWN note below)
gh api repos/OWNER/REPO/automated-security-fixes

# Alerts — likewise a dedicated endpoint, NOT a security_and_analysis key:
#   204 → enabled, 404 → disabled
gh api repos/OWNER/REPO/vulnerability-alerts -i 2>/dev/null | head -1
```

**Security updates are three states, not two.** `automated-security-fixes`
answers with two booleans, and they encode **three** states rather than an
on/off pair — report the one matching the payload, and never collapse `paused`
into `enabled`:

| Response | Report as | What it means |
|---|---|---|
| `{"enabled": true, "paused": false}` | `enabled` | Dependabot raises a fix PR when a new CVE lands |
| `{"enabled": true, "paused": true}` | `paused` | The flag is on, but **no fix PRs are being raised right now** |
| `{"enabled": false, "paused": …}` | `disabled` | No automatic CVE fix PRs, and none authorized |
| `403` | `UNKNOWN (needs admin)` | Not a state — a permission failure (see below) |

GitHub sets `paused` itself; it is not a setting anyone wrote. GitHub pauses
Dependabot on a repo it judges idle — typically one whose open Dependabot PRs
have gone untouched for an extended period — so `paused` is a state a repo
*drifts into*, which is exactly why reading only `.enabled` hides it. A paused
repo reads identically to an actively-patched one on `.enabled` alone, while
producing the same observable outcome as a disabled one: no fix PRs arriving.

**Do not prescribe step 5's enable write for `paused`.** The flag is already
`enabled`, so `PUT /automated-security-fixes` is a **no-op** against it. GitHub
resumes on its own once someone engages with the repo's Dependabot PRs (merge,
close, or comment on one), so report `paused` with that as the next action —
and if there are no open Dependabot PRs to engage with, say that too rather than
offering a write that will change nothing.

Read both flags from their dedicated endpoints, never from `security_and_analysis`.
That object is an unreliable source for either one, for two different reasons:

- `security_and_analysis.dependabot_alerts` is simply **absent** on many repos
  even when the object is otherwise fully populated, so a `// "UNKNOWN"`
  fallback on it reports "can't tell" for a repo you can read perfectly well.
  (Verified against `rjwalters/repo`: `security_and_analysis` returns
  `dependabot_security_updates` and the `secret_scanning*` keys with no
  `dependabot_alerts` among them.)
- On a **private repo without GitHub Advanced Security**, GitHub omits the
  **whole `security_and_analysis` object** regardless of token permissions —
  even a token with full admin sees it absent. So "object absent" and "no
  permission to see it" are different states, and only `automated-security-fixes`
  returning `403` is evidence of the latter; treating an absent
  `security_and_analysis` object itself as proof of missing admin is not
  reliable and misreports a plan/visibility limitation as a permission gap.

Report them on separate rows, never collapsed into one "Dependabot: on":

```
DEPENDABOT
==========
| Item                            | Status                                  |
|---------------------------------|-----------------------------------------|
| .github/dependabot.yml          | absent — no version updates configured  |
| vulnerability alerts (repo flag)| disabled (404)                          |
| security updates (repo flag)    | disabled — no automatic CVE fix PRs     |
| Open Dependabot PRs             | 0                                       |
```

The `security updates (repo flag)` row takes one of four values — one per row of
the state table above. The `paused` rendering is the one an `.enabled`-only read
cannot produce, and it is a distinct row value, never an annotation on
`enabled`:

```
| security updates (repo flag)    | enabled — fix PRs raised on new CVEs    |
| security updates (repo flag)    | paused — enabled, but GitHub is raising no fix PRs |
| security updates (repo flag)    | disabled — no automatic CVE fix PRs     |
| security updates (repo flag)    | UNKNOWN (needs admin) — endpoint returned 403 |
```

Reserve **UNKNOWN (needs admin)** for an actual permission failure — a `403`
from `/automated-security-fixes` (security updates) or `/vulnerability-alerts`
(alerts). Do **not** report a flag as `disabled` when the endpoint returned
`403`; "can't see it" and "it's off" are different answers and only one of
them justifies a write. Conversely, do **not** infer UNKNOWN from an absent
`security_and_analysis` object — as noted above, private repos without GitHub
Advanced Security omit that object even for a fully-admin token, so its
absence alone proves nothing about permissions; the dedicated endpoints are
the authoritative source either way.

### 2. Detect the ecosystems actually present

Scaffold from what the repo really contains, never from a fixed template. Look
for manifests at the root **and** in subdirectories (every distinct directory
needs coverage — either its own `updates:` entry with the right `directory:`
value, or a slot in one entry's plural `directories:` list, see below):

| Ecosystem | Detect via |
|---|---|
| `github-actions` | `.github/workflows/*.yml`, `.github/actions/*/action.yml` |
| `npm` | `package.json` (`pnpm-lock.yaml` / `yarn.lock` / `package-lock.json`) |
| `cargo` | `Cargo.toml` |
| `pip` | `requirements*.txt`, `pyproject.toml`, `Pipfile` |
| `gomod` | `go.mod` |
| `bundler` | `Gemfile` |
| `composer` | `composer.json` |
| `docker` | `Dockerfile`, `docker-compose.yml` |
| `gitsubmodule` | `.gitmodules` |

```bash
git ls-files '.github/workflows/*' '.github/actions/*' \
  '*package.json' '*Cargo.toml' '*go.mod' '*requirements*.txt' '*pyproject.toml' \
  '*Pipfile' '*Gemfile' '*composer.json' '.gitmodules' '*Dockerfile' \
  '*docker-compose.yml'
```

Use `git ls-files` for **every** probe — including the workflow directory — not
`ls` with a glob. Two reasons: vendored and ignored trees can't produce phantom
ecosystems, and under `zsh` an unmatched glob like `ls .github/workflows/*.yaml`
is a **hard error that aborts the whole command line**, so a repo with `.yml`
workflows can end up reporting no Actions ecosystem at all. `2>/dev/null` does
not save you — zsh fails before `ls` ever runs.

If **nothing** is detected, say there is nothing to scaffold; do not guess an
ecosystem. Still report alerts/settings through the selected provider's path.

**One ecosystem in many directories**: when the same ecosystem appears in
several places (say, four independent crates, each with its own `Cargo.toml`
and `Cargo.lock`, under one directory tree), Dependabot's plural `directories:`
key collapses them into a **single** `updates:` entry instead of N
near-identical ones. Scans still open one PR per directory — the key changes
how the config is written, not how many PRs arrive. The trade-off is that one
entry means **one shared policy**: the same schedule, grouping, `labels:`, and
`exclude-patterns` apply to every listed directory. Keep separate
per-directory entries whenever two manifests genuinely need different grouping
or cadence. Syntax in step 4.

#### 2a. Classify each manifest as repo-owned or installer-owned

Presence alone is not ownership. A manifest that lives under a tool root
installed by Loom/Anvil/Repo-Skills-style installers is *vendored,
installer-owned* code — the next tool install/upgrade overwrites it, so a
Dependabot PR against it is churn, not value. This is the same signal
[[update-tools]] step 1 already sweeps for — use the same bounded `find`, not a
fixed path list, so a future tool family member is picked up without a doc
edit here too:

```bash
find . -maxdepth 4 -name "install-metadata.json" \
  -not -path "*/node_modules/*" -not -path "*/.venv/*" 2>/dev/null
```

Each hit establishes a **tool root** (the directory containing that
`install-metadata.json`, e.g. `.anvil/`, `.loom/`, `.kct/`,
`.claude/skills/repo/`). A manifest from step 2 is **installer-owned** when its
path falls under one of these roots; everything else is **repo-owned**.

Do this classification per manifest, not per ecosystem — an ecosystem can have
both a repo-owned and an installer-owned manifest at once (e.g. a root
`pyproject.toml` alongside `.anvil/pyproject.toml`), and only the latter is
excluded.

Report the two groups separately, and never fold an installer-owned manifest
into the scaffold proposal:

```
MANIFESTS
=========
| Manifest                | Ecosystem | Ownership                                        |
|--------------------------|-----------|---------------------------------------------------|
| pyproject.toml (root)   | pip       | repo-owned                                       |
| .anvil/pyproject.toml   | pip       | installer-owned (anvil); use /repo:update-tools  |
| .anvil/uv.lock          | pip       | installer-owned (anvil); use /repo:update-tools  |
| package.json (root)     | npm       | repo-owned — but dependency-free (see below)     |
```

A dependency-free manifest is also not scaffoldable: a `package.json` with no
entries in `dependencies`, `devDependencies`, `peerDependencies`, or
`optionalDependencies`, and no lockfile, has nothing for Dependabot to update.
Check it explicitly rather than assuming presence implies content:

```bash
jq '{dependencies, devDependencies, peerDependencies, optionalDependencies}' package.json
```

If every detected manifest for an ecosystem is either installer-owned or
dependency-free, that ecosystem drops out of the scaffold candidate list
entirely — it does not get an `updates:` entry.

If, after this filtering, **no ecosystem remains** — every detected manifest
was installer-owned, dependency-free, or both — do not propose a
`dependabot.yml` at all. Say so explicitly and why, and point installer-owned
findings at `/repo:update-tools` as the remediation path for their
dependencies (that command upgrades the vendored manifest itself; a Dependabot
PR against it would just be reverted by the next install). Still continue to
the selected provider's repo-level alert/settings checks — those are useful
independent of whether there is anything to scaffold. Use step 5 only on the
Dependabot path; Renovate must not re-enable duplicate security PRs.

### 3. Validate every label the config would reference — by description

A scaffolded config can attach labels to bot PRs (`labels:` in the `updates:`
entry). Before referencing **any** label, read its description and confirm a
bot may apply it:

```bash
# Labels reserved for a specific party — refuse every one of these, and name
# the party in the report
gh api repos/OWNER/REPO/labels --paginate \
  --jq '.[] | select(.description // "" | test("Applied by:")) | "REFUSE: \(.name) — reserved for \(((.description // "") | capture("Applied by: (?<party>[^.]+)").party // "unknown party") | sub(" only\\s*$"; "")) — \(.description)"'

# Remaining candidates
gh api repos/OWNER/REPO/labels --paginate \
  --jq '.[] | select((.description // "" | test("Applied by:")) | not) | .name'
```

Two details that matter in that jq: `.description // ""` is **required** — a
label with a null description makes a bare `.description | test(…)` abort with
`null (null) cannot be matched, as it is not a string`, which can drop the rest
of the label list mid-scan and silently shrink the set you validate against.
And prefer `gh api …/labels` over `gh label list --json` — the latter goes
through GraphQL, which shares a separate (and, on a busy agent host, routinely
exhausted) rate-limit bucket from REST.

Rules:

- The label must **exist**. Existence alone is not enough.
- **Refuse any label whose description reserves it for a party** — look for
  the literal substring `Applied by:`, not just `Applied by: humans`. The
  convention reserves labels for parties other than humans too — e.g.
  `loom:evaluating`: *"Champion is evaluating this proposal (claim label,
  stale after 15m). Applied by: Champion only."* is exactly as off-limits to
  Dependabot as a human-reserved label: it is a claim label with staleness
  semantics owned by a specific actor, and Dependabot applying it would feed
  automation that acts on that claim. Having Dependabot apply *any*
  `Applied by:` label violates that label's own contract, regardless of which
  party it names.
- Report which party each refused label is reserved for (e.g. "REFUSE:
  loom:evaluating — reserved for Champion").
- **Never create a label** to solve this. No `gh label create`, ever. If no
  suitable label exists, scaffold the config **without** a `labels:` key and
  say so in the report. This refuse-by-default posture is intentionally
  stricter than necessary — it can pass over a label that a repo owner would
  in fact consider fine for Dependabot to apply (e.g. an `Applied by: <bot>`
  label meant for automation) — but the safe fallback of no `labels:` key
  costs nothing, while silently applying a reserved label can violate its
  contract. If a repo wants a bot-applied label used here, that is a policy
  call for a human to make explicitly, not something this check should infer.
- A maintenance/chore-tier label (e.g. `tier:maintenance`, `dependencies`,
  `chore`) is the usual right answer when one is present and unrestricted.

Report the decision explicitly: which label was chosen, or which were rejected
and why.

Carry the outcome forward. If it is **no `labels:` key** — every candidate was
refused — and step 2a found a `.loom/` root, that fallback has a review-routing
consequence on this repo; report it under "Check how dependency PRs interact
with Loom" below rather than letting the run end on plain success.

### 4. Offer to scaffold the config (confirm first)

Only ecosystems that survived step 2a's filtering — repo-owned manifests with
real dependencies — are candidates here. If step 2a already concluded there is
nothing left to scaffold, skip straight to step 5 rather than proposing a
config anyway.

Grouping policy is **per-ecosystem**, not uniform. Reviewing every Actions bump
individually is noise; batching a breaking change into line 4 of a 12-package
PR is how a risk-bearing dependency slips through unreviewed.

| Ecosystem | Policy | Why |
|---|---|---|
| `github-actions` | group everything, majors included | low-risk, individually reviewing them is noise |
| package ecosystems (`npm`, `cargo`, `pip`, …) | group minor + patch; **majors ungrouped** | a breaking change deserves its own reviewable PR |

**Ask which dependencies are risk-bearing** rather than applying one policy to
everything — deps coupled to an external binary or service (e.g.
`playwright-core` and its browser binary, a database driver, a native
toolchain) should stay ungrouped even at minor/patch, via `exclude-patterns`.

Show the proposed file in full and get approval before writing:

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      github-actions:
        patterns: ["*"]
        update-types: ["major", "minor", "patch"]

  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      npm-minor-patch:
        patterns: ["*"]
        exclude-patterns: ["playwright-core"]   # risk-bearing → its own PR
        update-types: ["minor", "patch"]
    # majors are deliberately ungrouped: one reviewable PR each
```

For an ecosystem step 2 found in several directories, use the plural
`directories:` key **in place of** `directory:` — a list of paths (globs
allowed) sharing one entry's schedule, grouping, and labels:

```yaml
  - package-ecosystem: "cargo"
    directories:                       # plural — several manifests, one policy
      - "/crates/alpha"
      - "/crates/beta"
      - "/crates/gamma"
    schedule:
      interval: "weekly"
    groups:
      cargo-minor-patch:
        patterns: ["*"]
        update-types: ["minor", "patch"]
```

Expect one PR per directory on the first scan even though there is only one
entry — `directories:` shares the *policy*, not the PRs. Split it back into
per-directory entries the moment two of those crates need different grouping,
cadence, or labels: `directories:` buys brevity, and pays for it with a single
shared policy across every path listed.

Add `labels: ["<validated-label>"]` only if step 3 approved one. Write the file
only on explicit approval; under `--check`, stop here and show it as a proposal.

### 5. Offer to enable the repo-level flags (confirm first)

Independent of the file, and a separate confirmation. Alerts are a
**prerequisite** for automated security fixes — enable in this order:

```bash
gh api -X PUT repos/OWNER/REPO/vulnerability-alerts       # prerequisite
gh api -X PUT repos/OWNER/REPO/automated-security-fixes
```

Both need admin. On a 403, report that the flag needs a repo admin and move on
— never present a failed write as success. Re-read the flags afterward and show
the before/after.

**Do not offer the security-updates write when step 1 reported the flag as
already `enabled` but `paused`** — the `PUT` is a no-op there, and offering it
presents a resumption that will not happen as a fix. Report step 1's paused
next action instead.

### 6. Check for PRs immediately after the config lands

**Dependabot fires on config merge, not on schedule.** The first PRs typically
arrive within a couple of minutes of the config landing on the default branch,
regardless of `interval: weekly`. Never tell the user to "expect your first PR
Monday" — wait briefly, then run the PR review half below.

## Steps — review open dependency PRs

### 7. List the selected providers' open PRs

For hosted Renovate include `renovate[bot]` (CLI author `app/renovate`);
for self-hosted installations discover the configured bot identity rather than
assuming that login. During migration include both providers, verifying authors
against the actual installation. A `renovate/` branch name alone proves no bot
identity. The examples below show Dependabot; extend the author filter for the
providers actually present.

```bash
gh pr list --author "app/dependabot" --state open \
  --json number,title,headRefName,createdAt,statusCheckRollup,labels

# REST fallback when GraphQL's rate-limit bucket is exhausted. Note the author
# spelling differs: gh's --author filter wants "app/dependabot", the REST
# payload carries login "dependabot[bot]". This jq deliberately emits only
# number/title — if a later step starts consuming headRefName/createdAt/
# statusCheckRollup/labels, widen it, or the fallback path silently loses them
# (statusCheckRollup has no REST field: use `gh pr checks <N>` per PR instead).
gh api repos/OWNER/REPO/pulls --paginate \
  --jq '.[] | select(.user.login == "dependabot[bot]") | "#\(.number) \(.title)"'
```

If there are none, say so — and if the config was just written, note that PRs
land within minutes rather than on the stated interval.

### 8. Classify each PR

For every PR report: **ecosystem**, **update type** (major vs minor/patch —
majors flagged), **CI status**, **whether it's stale** (the manifest on the
base branch already satisfies it — see the sub-step below), **whether another
open PR supersedes it** (see "Sibling supersession" below), and what actually
changed.

Those two disqualifiers are **different findings with different actions**, and
every PR lands in exactly one of three buckets:

| Classification | Test | Action |
|---|---|---|
| **stale** | the base branch's manifests *and lockfiles* already satisfy every proposed change | drop from the pending tally; keep the count + evidence in the report; don't close it automatically |
| **superseded by a sibling** | another **open** bot PR proposes a version at least as new for every dependency this one touches | propose **closing it with a cross-reference to the superseding PR** — never merge it |
| **real** | neither of the above | propose for merge, subject to step 9 |

Update type comes from the title/branch (`bump X from 1.2.3 to 2.0.0` →
compare the leading version components) — confirm against the diff rather than
trusting the title alone:

```bash
# REST rather than `gh pr view --json` — same reason as the label lookup above:
# any `--json` flag on `gh pr`/`gh issue` forces a GraphQL query. `gh pr diff`
# and `gh pr checks` take no `--json` and are already REST-backed.
gh api repos/OWNER/REPO/pulls/<N> --jq '{title, body}'
gh api repos/OWNER/REPO/pulls/<N>/files --paginate --jq '[.[].filename]'
gh pr diff <N>
gh pr checks <N>
```

#### CI status caveat — green only means what CI actually exercises

**Scaffolding an ecosystem's Dependabot config does not imply the repo's CI
exercises that ecosystem's artifact.** `/repo:deps` scaffolds an ecosystem
purely from manifest presence (Safety Rule 4) — it never checks whether any CI
job builds or runs what that manifest produces. A bot PR's "CI status" is only
as meaningful as what CI does with the changed files: a `npm`/`cargo`/`pip`
bump that CI compiles and tests is well-verified by green; a `docker`-ecosystem
bump (base-image or layer change) is verified by green only if some job
actually runs `docker build`. This generalizes to any ecosystem whose
artifact CI neither builds nor runs — Terraform providers with no `terraform
plan` job, a Helm chart with no `helm template`/lint step, etc.

Before reporting a PR's CI status as reassuring, check whether the repo's
workflows actually build/run that ecosystem's artifact (`grep` the
`.github/workflows/*.yml` for the relevant command — `docker build`,
`terraform plan`, …). If they don't, report the PR's CI as **green but
unverifiable by this repo's checks** rather than plain "green" — e.g. a
`docker`-ecosystem PR when no workflow runs `docker build` against the
Dockerfile it touches. (As of this writing this repo's own `docker` entry —
the root `Dockerfile` — is covered: `.github/workflows/docker-build.yml` runs
`docker build .` on any PR/push touching it, `paths:`-filtered off unrelated
PRs — added in response to issue #231, after the first docker Dependabot PR
merged on a green check that had built nothing. Re-check this note against
the workflow files each run rather than trusting this parenthetical, since
either side of it can drift.)

#### Stale check — compare actual dependency state on the current base

Inspect every targeted manifest **and lockfile** from the current remote base
commit. A manifest range permitting the proposed version does not prove the
installed dependency already uses it: `^1.0.0` can still lock a vulnerable
`1.0.0` while a security PR changes only the lockfile to `1.0.1`.

A PR is stale only when every proposed change is already present or superseded
on the base. Use the ecosystem's version/range semantics, not lexical ordering
or a generic comparison of leading digits. For grouped/workspace updates check
all affected manifests and lockfiles. For security fixes also confirm the
resolved version is outside the advisory's affected range; a higher version
can still be vulnerable. Check digest changes independently of version tags.

For GitHub Actions, compare the pins in every workflow/action file the PR
changes. Two PRs with the same version pair can update different workflows;
a matching title never establishes duplication. Treat ambiguous or partially
satisfied PRs as pending work, not stale, and do not close them automatically.

Exclude truly stale PRs from the pending-major tally, but retain their separate
count and evidence in the report.

For **GitHub Actions** bumps specifically, check whether the update **clears a
deprecation annotation** — often the actual reason to take a scary-looking
major. Compare annotations on the base branch against the PR head:

```bash
gh api "repos/OWNER/REPO/commits/$SHA/check-runs" --jq '.check_runs[].id' \
  | while read -r id; do
      gh api "repos/OWNER/REPO/check-runs/$id/annotations" --jq '.[].message'
    done
```

Run it for `main`'s head SHA and the PR's head SHA and diff the two sets. A
major bump that removes a *"Node.js 20 is deprecated"* annotation, with CI
green on every matrix leg, is a much easier yes than "a major bump, seems
risky."

#### Sibling supersession — compare the open PRs against *each other*, not only the base

The stale check compares each PR against the base branch. It cannot see the
case where **another open PR already proposes a strictly newer version of the
same dependency** — both PRs are ahead of the base, so neither is stale, yet
merging both is guaranteed conflict for no gain and merging the older one first
is strictly worse. A bot produces this routinely: it opens a PR, the upstream
package releases again, and the next scan opens a second PR for the same
dependency without closing the first.

So after the per-PR pass, do one **cross-PR** pass: build the
dependency → proposed-version map for every open PR and compare the maps.

```bash
# Per PR, the (dependency, new version) pairs it proposes — from the manifest
# hunks, not the title, so grouped PRs are covered too. `gh pr diff` takes no
# pathspec (`cobra.MaximumNArgs(1)`), so filtering to one file has to go
# through the files API instead.
for n in <PR numbers>; do
  echo "== #$n"
  gh api "repos/OWNER/REPO/pulls/$n/files" --paginate \
    --jq '.[] | select(.filename == "package.json") | .patch'
done
```

A PR is **superseded** when, for *every* dependency it touches, some other open
PR proposes a version at least as new — i.e. it is wholly contained in that
sibling. Use the ecosystem's version semantics for "newer", as in the stale
check. Partial containment is **not** supersession: a PR that bumps `a` and `b`
where a sibling only covers `b` is still real work, and closing it loses the
`a` bump.

Worked example from a four-PR npm queue, where neither PR was stale (the base
lockfile held `wrangler` 4.120.1, below both):

| PR | `wrangler` | `sharp` |
|---|---|---|
| #205 | `^4.139.0` | 0.35.2 → 0.35.4 |
| #209 | `^4.136.1` | 0.35.2 → 0.35.4 |

#209 is wholly contained in #205 → **superseded by #205**; propose closing
#209 with a comment naming #205, and merge #205. Report supersession as its own
`Note`, with the superseding PR number in it, and count it separately from
stale — the reader needs to know a PR was dropped as redundant rather than as
already-satisfied, because only one of those is a merge-set decision. Closing
is still a write: it needs the same explicit confirmation as a merge, and never
happens under `--check`.

The `Note` column carries the `stale — already satisfied by manifest` flag
alongside the existing CI-status/diff notes, so a stale PR is visible as such at
a glance:

```
OPEN DEPENDENCY PRs
===================
| PR  | Ecosystem      | Update                     | Type  | CI    | Note                              |
|-----|----------------|----------------------------|-------|-------|-----------------------------------|
| #12 | github-actions | actions/checkout 4 → 5     | MAJOR | green | clears "Node.js 20 deprecated"    |
| #13 | npm            | 6 packages (minor + patch) | minor | green | grouped                           |
| #14 | npm            | playwright-core 1.4 → 2.0  | MAJOR | red   | browser binary coupling           |
| #15 | npm            | @biomejs/biome 2.5.5 → 2.5.6 | patch | green | stale — base lockfile already resolves 2.5.7 |
| #16 | npm            | wrangler ^4.136.1 + sharp    | minor | green | superseded by #13 — close, don't merge      |
```

Summarize the split explicitly below the table so callers (including
`/repo:all`) get the counts without re-deriving them —
**open**, **majors** (real forward majors only), **stale**, and
**superseded**:

```
5 open, 2 majors, 1 stale — already satisfied by manifest, 1 superseded by a sibling
```

The majors count excludes every stale **and** every superseded PR. A PR whose
title names a major bump but whose resolved dependency state already satisfies
it (stale) is **not** a major here — it is counted only in the stale total; the
same holds for a superseded PR, counted only in the superseded total. When
nothing is superseded, leave that clause out entirely rather than printing
`0 superseded` — the open/majors/stale counts keep their existing always-printed
form.

### 9. Offer to merge the safe ones (confirm first)

Propose a merge set and get explicit approval. **Never** merge a major without
its own separate confirmation, and never merge a PR whose CI is red or pending.
Every PR classified **superseded by a sibling** in step 8 is proposed for
closure with a cross-reference, not for merge.

#### A lockfile-bearing ecosystem merges one PR at a time

**Several PRs reporting `mergeable: MERGEABLE` / `mergeStateStatus: CLEAN` at
the same instant is not evidence that they are mutually compatible.** GitHub
evaluates each PR against the **base branch** independently and has no concept
of the other PRs in flight, so a whole queue can read `CLEAN` simultaneously and
still conflict pairwise. (This is the same GitHub behavior `/loom:sweep`
documents for its wave machinery — see the base-branch-only callout in
`loom/sweep-execution-model.md`, installed as
`.claude/commands/loom/sweep-execution-model.md` on a Loom-managed repo. One
behavior, two surfaces; don't build a second explanation of it here.)

For any ecosystem with a **single shared lockfile** — npm/pnpm/yarn
(`package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`), Cargo (`Cargo.lock`),
Poetry/uv (`poetry.lock`, `uv.lock`), Bundler, Go's `go.sum` — the overlap is
not a heuristic, it is a **certainty**: every PR in that ecosystem edits the
lockfile, so every pair conflicts. Therefore:

- **Merge strictly one at a time**, never a batch, and re-derive the next PR's
  state after each merge (the `UNKNOWN`-window poll below).
- **Order matters** — merge the *superseding* PR of any pair first, so the
  sibling you would otherwise rebase is one you are closing anyway.
- Ecosystems with **no** shared lockfile (GitHub Actions workflow pins, Docker
  base images) only conflict when two PRs touch the same file, so they can be
  batched — check the file lists from step 8 rather than assuming.

#### After each merge, re-check the survivors instead of trusting them

Each surviving PR's green CI was measured against a base that has now moved, so
it is evidence about a tree that no longer exists — not about what merging it
next would produce. In the four-PR run behind this guidance, three PRs had been
tested against a base five merges old and were reporting a bundle size 10 KB
below the current one. After every merge, for each remaining PR: re-read its
mergeability (below), and treat its checks as **needing a re-run on the new
base** before it counts as green — a rebase (next sub-step) triggers exactly
that.

In a Loom-managed repo (`.loom/scripts/merge-pr.sh` present) use that script
rather than `gh pr merge` — `gh pr merge` attempts a local checkout that fails
when the branch is linked to a worktree:

```bash
./.loom/scripts/merge-pr.sh <N>      # Loom repos
gh pr merge <N> --squash             # otherwise
```

**Re-poll mergeability between sequential merges.** Merging one bot PR that
touches a shared lockfile invalidates its siblings: GitHub recomputes their
mergeability asynchronously, and for ~20 s the remaining PRs report
`mergeable: null` / `mergeable_state: "unknown"` (`mergeStateStatus: UNKNOWN`
via `gh`) before settling back to `clean`. `merge-pr.sh` refuses inside that
window, which reads as a spurious failure mid-loop. After each merge, re-read
the next PR's state and wait for `clean` before merging it:

```bash
for _ in $(seq 1 12); do
  state=$(gh pr view <N> --json mergeStateStatus -q .mergeStateStatus)
  [ "$state" = "UNKNOWN" ] || break
  sleep 5
done
echo "$state"   # CLEAN → merge; DIRTY/BLOCKED → real conflict or failing checks, re-triage
```

A state that settles on `DIRTY` (or `BLOCKED`) is **not** a timing artifact —
the earlier merge produced a genuine lockfile conflict, so stop the loop and
report it rather than retrying.

#### Recovering a conflicted bot branch — ask the bot, not `gh`

A `DIRTY` bot PR after a sibling merge is the **expected** state for a
lockfile-bearing ecosystem, not a failure. It does not need closing, and it
cannot be fixed with `gh`:

```
$ gh pr update-branch 210
X Cannot update PR branch due to conflicts
```

`gh pr update-branch` asks GitHub to merge the base into the PR branch (or
rebase onto it with `--rebase`). Neither mode can accept a conflict
resolution — the API has no channel for one — so both fail outright on a
conflicted branch rather than recovering it. The supported recovery is to
**ask the bot to redo the branch**, which it does by recomputing the update
against the current base:

```bash
gh pr comment <N> --body "@dependabot rebase"     # Dependabot
# Renovate: tick the PR body's "Rebase/retry" checkbox, or comment the
#   configured rebase trigger for that installation
```

Two outcomes, both normal, and they differ in a way that matters for anything
holding a PR number:

- **Simple PR (one or a few packages)** — the bot **rebases in place**: same PR
  number, new head commit, CI re-runs against the new base.
- **Grouped PR (a `groups:` entry, many updates)** — the bot **closes it and
  opens a new PR under a new number**, having recomputed the group membership
  against the new base (so the update count can change too: a 19-update group
  came back as 18). Anything referencing the old number — your merge set, the
  report you already printed, a tracking comment — goes stale the moment this
  happens, so **re-list open bot PRs (step 7) after a grouped rebase** rather
  than continuing against the number you had.

Rebasing is a write to the PR, so it needs the same explicit authorization as a
merge, and never happens under `--check`. Expect one CI cycle per rebase: on a
queue of N lockfile-bearing PRs, merging all of them costs N sequential
cycles — worth saying out loud when you propose the merge set, and worth
weighing against merging the highest-value one or two now and letting the bot's
next scan regenerate the rest.

Under `--check`, stop at the report and merge nothing — no closures, no
rebase comments.

## Check how dependency PRs interact with Loom

State this in the report whenever a `.loom/` directory is present. It is the
natural wrong assumption, and it is safety-relevant:

- Inspect the actual bot PR labels and installed Loom routing. Do not assume
  either Dependabot or Renovate PRs are watched or ignored based on bot identity
  alone; unlabeled PR handling can vary with the installed Loom version.
- Report who is responsible for merging: a human, the configured updater, or an
  explicitly authorized Loom workflow. Eligibility is not merge authorization.
- Do **not** "fix" this by applying `loom:` labels to bot PRs. Routing bot PRs
  into an auto-merge pipeline is a policy decision for the repo's owner, not a
  side effect of a hygiene command — and any label used for it still has to
  pass the step 3 description check.
- **Report it as a finding when step 3 ended with no `labels:` key.** On a
  Loom-managed repo that fallback is not free: Loom routes PRs into review *by
  label*, and every Loom routing label is spelled `… Applied by: <party>`, so
  step 3 refuses all of them. The config that results is correct and inert —
  the bot's PRs carry no label, so nothing routes them for review. Do not let
  that end as plain success; state the consequence and who can resolve it:

  ```
  LOOM ROUTING
  ============
  Bot PRs on this repo will not be routed for review: every candidate routing
  label is reserved (`Applied by: ...`), so the config was scaffolded without a
  `labels:` key. Dependency PRs will therefore sit unreviewed by Loom.
  Resolution is a human's, not this command's: the repo owner can explicitly
  choose a bot-applied label to route on.
  ```

  This is **informational, never an error**, and never something to auto-fix:
  no `gh label create`, no applying a reserved label anyway, no failing the run
  over it — Safety Rule 3 is unchanged. Naming the human decision is the point,
  and it is the same one step 3 already states: if a repo wants a bot-applied
  label used here, "that is a policy call for a human to make explicitly."
  Report nothing when step 3 *did* approve a label (routing already works), and
  nothing on a repo with no `.loom/` root — like the rest of this section, the
  finding is gated on the `.loom/` tool root from step 2a's
  `install-metadata.json` scan.

## Safety Rules

1. **Writes and merges require authorization** — show the concrete scope and
   use existing authorization when it covers it. Closing a superseded PR and
   commenting `@dependabot rebase` are writes too, held to the same bar. Under
   `--check`, write nothing.
2. **Policy, updater, alerts, and security PRs are independent** — a present
   config says nothing about whether CVE alerting is on. Report
   UNKNOWN (not `disabled`) when the token can't read the setting.
3. **Never create a label**, and never reference one whose description reserves
   it for any party (`Applied by: <party>` — humans, Champion, a bot, …). No
   suitable label → no `labels:` key.
4. **Scaffold only detected ecosystems** — no fixed template, no guessing. Zero
   detected means nothing to scaffold.
5. **Never scaffold against an installer-owned manifest** — a manifest under a
   tool root that carries `install-metadata.json` (`.anvil/`, `.loom/`,
   `.claude/skills/*/`, …) is vendored code the next tool install/upgrade
   overwrites; propose `/repo:update-tools` for it instead. Also exclude
   dependency-free manifests (no deps in any block, no lockfile) — nothing for
   Dependabot to update. If every detected ecosystem is installer-owned or
   dependency-free, recommend not scaffolding and say why.
6. **Never auto-merge a major** — majors get their own confirmation, always.
   Red or pending CI is never merged.
7. **Never merge a lockfile-bearing ecosystem's PRs in a batch** — one at a
   time, re-checking each survivor afterwards. Simultaneous `CLEAN` across
   several PRs proves nothing about their mutual compatibility.
8. **Never push or merge under `--check`** — report-only means report-only.
9. **Never file an issue without confirmation, and never re-file one already
   tracked** — the deferred-Dependabot-shutdown issue is offered, with its full
   body shown, only after the [[followups]]-style dedup search comes back empty
   for the client repo, and never under `--check` or `--all-repos`.
