---
name: "org-policy"
description: "Preview or install canonical rjwalters/repo preferences into a client's GitHub owner/.github repository"
domain: repo
type: command
user-invocable: true
---

# /repo:org-policy — Organization Preferences

Manage an organization's policy from any of its client repositories. The
authoritative files live in **rjwalters/repo**, on its default branch:

- `policies/default.json`: shared defaults.
- `policies/organizations/<lowercase-owner>.json`: optional owner overrides.

Objects merge recursively; arrays and scalar values replace the shared value.
Use the same convention for a personal GitHub account. Change preferences in
the canonical source, then redeploy; organization copies and client installs
are not independent sources of truth.

## Usage

```text
/repo:org-policy                     # Preview source, effective policy, and org drift
/repo:org-policy --check             # Same read-only report
/repo:org-policy --install           # Preview, then publish the authorized policy PR
/repo:org-policy --install --repo OWNER/PROJECT
```

The deterministic implementation is `scripts/repo/repo-org-policy.py` in the
source checkout, installed to `.claude/skills/repo/scripts/repo-org-policy.py`.
It needs Python 3.9+, git, and authenticated `gh`. It always uses github.com;
other forges are unsupported. A local source checkout is not required on the
client: the helper fetches canonical JSON at one immutable GitHub commit.

## Preview and publish

1. Derive the client owner from `origin`, or use an explicitly supplied
   `--repo OWNER/PROJECT`. Display the target **OWNER/.github**: this operation
   affects shared organization policy, not just the invoking repository.
2. Run the helper's `plan` command. For `--check`, omit `--output` and stop after
   the report. An absent/inaccessible target is not proof that creation is
   needed; verify access first. The helper does that disambiguation itself (see
   "Absent versus inaccessible target" below) and reports which case its
   evidence favors — read its verdict rather than re-deriving it by hand, and
   treat a `cannot tell which`/`inconclusive` verdict as "still unverified". If
   creating `.github` is intended, establish public/private visibility and use
   `--create-repository public|private`. Shared policy contains no credentials.
   A public preset is easiest to reuse across public and private clients;
   private presets require appropriate app access and cannot be consumed from
   public repositories — so `--create-repository private` is **refused** when
   the invoking client is public, rather than producing an `extends` reference
   that fails at Renovate runtime. When the client's own visibility cannot be
   read, the helper warns instead of refusing; confirm it by hand before
   continuing.
3. For an install, save a plan at a new temporary path. Show its complete diff,
   source revision, target, and repository creation/visibility if applicable.
   Use existing authorization when it covers that target and these changes;
   otherwise request approval of this concrete plan. Do not repeatedly ask for
   steps already authorized. `--install` never authorizes unrelated settings,
   app installation, or migrating every client repository.
4. Apply that saved plan with `--yes`. It rechecks source and target before any
   mutation. A changed source or target requires a fresh preview. It preserves
   unrelated files, creates a separate branch, and opens a policy PR. A retry
   reuses an unchanged existing branch/PR; it never force-pushes. If repository
   creation succeeded but publishing failed, report that partial result and
   generate a fresh plan before continuing.
5. Report the PR URL and whether it is merged. The policy becomes active on
   the default branch after merge; opening a PR is not an installed policy.
   Follow the repository's normal merge workflow when merging is authorized.
   `apply` closes its own output with the next command for the invoking client
   (`/repo:deps --install`) and whether the organization PR still has to merge
   first — include that line in the report, and do not substitute a
   hand-written client `renovate.json` for it (that skips Dependabot
   reconciliation, `ignorePaths` for installer-owned roots, and security-PR
   ownership). The same closing line appears on a no-op apply, saying no
   organization PR is pending.
6. Every `plan` and `apply` invocation also prints a Renovate GitHub App
   installation line (see "Renovate App installation check" below) — include
   it verbatim in the final report. A published/merged policy proves nothing
   about whether the App that reads it is actually installed on the target
   organization/user; treat "policy published" and "policy published, app
   NOT installed" as distinct outcomes, not the same success.

```bash
# Run from the client repository. Use a new directory so plan.json is absent.
policy_preview_dir=$(mktemp -d)
python3 .claude/skills/repo/scripts/repo-org-policy.py plan \
  --output "$policy_preview_dir/plan.json"
# After reviewing the plan, within the authorized install scope:
python3 .claude/skills/repo/scripts/repo-org-policy.py apply \
  --plan "$policy_preview_dir/plan.json" --yes
```

`--source-dir /path/to/repo` on `plan` previews unpublished canonical changes.
Those plans cannot be applied: publish the source and regenerate from GitHub.
Do not use a stale client-bundled template as a fallback when GitHub is
unavailable. Authentication/rate-limit failures are errors, not missing policy.

## Absent versus inaccessible target

GitHub answers 404 both for a repository that does not exist and for a private
one the caller cannot see, so `OWNER/.github` returning 404 never proves that
creation is the right next step. Rather than naming both cases and stopping,
`plan` runs the same check an operator would run by hand — the account type of
`OWNER`, and whether the caller has admin on the invoking client, a known
sibling repository in that same account — and reports which case its evidence
favors:

```text
rulehunt/.github appears absent rather than hidden: rulehunt is an organization
and you have admin on the sibling repository rulehunt/rulehunt, so a private
rulehunt/.github would normally be visible. GitHub answers 404 for both an
absent and a hidden repository, so this is evidence, not proof. To create it,
plan again with --create-repository public (or private)
```

| Verdict | Meaning |
|---|---|
| `appears absent rather than hidden` | Evidence favors creation, but GitHub's 404 conflation means this is evidence, not proof. |
| `is absent or inaccessible, and this token cannot tell which` | A hidden repository cannot be ruled out (no admin on the sibling, or the sibling itself is invisible, or a third-party personal account). |
| `is absent or inaccessible, and the access check was inconclusive` | The check itself could not answer (the account or sibling could not be read). |

Only the first verdict is evidence for creating the repository. For the other
two, establish access before passing `--create-repository`; the message names
the specific gap it could not close.

## Renovate App installation check

Config presence proves nothing about whether Renovate ever runs: a policy PR
can merge cleanly, a client's `renovate.json` can extend the preset validly,
and the organization can still be entirely inert if the **Renovate GitHub
App** itself was never installed on that organization/user. Both `plan` and
`apply` verify this automatically and print one line, e.g.:

```text
Renovate GitHub App: NOT installed — policy published, but no PRs will be
raised until it is. Install: https://github.com/apps/renovate (requires an
organization owner/admin, who may not be the person running this command).
```

or, when it resolves to `installed.`/`UNKNOWN` instead. Absent is a report, not
an error — do not treat it as a failed `plan`/`apply`, and do not silently drop
it from the final summary. Run the check standalone (e.g. to re-verify after
an admin installs the App, without regenerating a policy plan) with:

```bash
python3 .claude/skills/repo/scripts/repo-org-policy.py check-app \
  --owner OWNER   # or: --repo OWNER/PROJECT to derive the owner from origin
```

`UNKNOWN` means this token cannot see the installations endpoint for that
account — a non-admin org member, or any personal account other than the
caller's own (`user/installations` only lists installations for whoever is
authenticated) — not that the App is confirmed present. Report `UNKNOWN`
as-is rather than treating it as either outcome.

## Installed files and client adoption

The organization PR manages exactly two root files in `OWNER/.github`:

- `renovate-config.json`: resolved native Renovate configuration.
- `repo-policy.json`: provider/settings preferences, the preset reference, and
  source repository, commit, and source-file SHA-256 digests.

The helper does **not** install the Renovate GitHub App, change repository flags,
enable auto-merge, or migrate client repositories — it only **reports** whether
the App is installed (see "Renovate App installation check" above). Installing
it at `https://github.com/apps/renovate` requires an organization owner/admin,
who may not be the person running this command. After the organization PR
merges, use [[deps]] to reconcile the invoking client. Its `renovate.json`
extends `github>OWNER/.github:renovate-config`; explicit client exceptions stay
in that client. Existing clients must adopt that reference once. Future
organization policy updates then reach those clients when Renovate resolves
the preset, without reinstalling Repo Skills.

A client's `extends` reference cannot resolve until this organization PR has
actually **merged** on `OWNER/.github`'s default branch — opening it is not
enough (step 5 above). Adopting the preset earlier is not a partial win; it is
a reference that fails to resolve. When migrating more than one client, use
[[deps]]'s `--all-repos` fan-out survey to see this ordering enforced across
every repo in the org at once — it reports every repo's preset-adoption state
as "not adopted — preset not yet available" (rather than a plain adoption gap)
until this PR is merged, and reports each repo's actual `dependabot-only` /
`both-active` / `renovate-only` / `unmanaged` migration state alongside it.
That survey is report-only; it never writes to a surveyed repo. Per-repo
adoption itself is still done with plain [[deps]] against that one client, as
described above — `--all-repos` only tells you where each client currently
stands.

Check for an existing `OWNER/renovate-config/default.json` before relying on
automatic onboarding: Renovate discovers it ahead of `.github` presets. Use
the explicit reference above for uniform client adoption. If the helper finds
different organization content, the preview shows that drift; do not treat a
hand edit to an installed copy as a new canonical preference.

## Dependency policy

The shipped policy requests 14 days for routine versions and one day for
advisory-backed security fixes. Both ages are measured from release time.
Keep that classification separate from patch/minor/major SemVer and from
claims in a changelog. A claim alone requires triage before expedited handling.
Native Renovate security behavior defaults to no release-age delay, so the
one-day override is explicit in `vulnerabilityAlerts.minimumReleaseAge`; verify
the deployed bot honors it during onboarding, including lockfile resolution.

Automerge is a separate preference and defaults to false. Before enabling it,
verify effective required checks actually cover the changed ecosystem. An
emergency zero-delay exception must identify the affected advisory/dependency
and have explicit authorization. A targeted manual fix PR can bypass the age
hold for that dependency without shortening the organization's general policy;
verify lockfile/package-manager age exceptions as part of that PR. Remove any
temporary client exception afterward. It does not waive CI or breaking-change
review.

For source layout, override examples, and validation, read `policies/README.md`
in rjwalters/repo. Native reference:
https://docs.renovatebot.com/config-presets/#grouporganization-level-presets
