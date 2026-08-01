---
name: "release"
description: "Cut a release — pre-flight checks, semver decision, CHANGELOG, version bump, tag, and GitHub Release"
domain: repo
type: command
user-invocable: true
---

# /repo:release — Cut a Release

Guide a careful, interactive release of this repository. Every phase requires
confirmation before proceeding — **do not rush, and never push or tag without
an explicit yes.** The version-bearing files and the bump tool are *discovered*
at release time, never hardcoded, so this works in any repo.

## Usage

```
/repo:release                  # Interactive release from the default branch
```

## Phase 0 — Load project release policy

Before Phase 1, load the repo's **release policy** if it has one. This is the
single supported way for a project to inject its own procedural steps (gates,
extra manifest edits, post-release deploys) at named phase boundaries without
forking this command — see **Extension points — per-project release policy**
below for the full seam contract and semantics. Projects migrating off Loom's
removed `/loom:release` skill re-home their policy here.

The policy lives in **one** file at the repo root: **`.repo/release-policy.md`**.
Each seam is an H2 section headed `## seam: <name>`. Read it, then **validate the
declared seam names and surface any that don't bind**, so a typo'd or orphaned
seam fails loudly instead of silently doing nothing:

```bash
POLICY_FILE=".repo/release-policy.md"
KNOWN_SEAMS="pre-flight pre-changelog-style pre-apply pre-push post-push pre-github-release post-summary"
AUGMENT_ONLY="post-push post-summary"   # no default action → '(replace)' is meaningless
if [ ! -f "$POLICY_FILE" ]; then
  echo "(no $POLICY_FILE — running with the built-in phases only, no behavior change)"
else
  echo "Project release policy: $POLICY_FILE"
  # Enumerate declared seams from '## seam: <name>' headers (dropping an optional
  # '(replace)' suffix) and check each against the known set.
  grep -E '^##[[:space:]]+seam:[[:space:]]*' "$POLICY_FILE" | while IFS= read -r header; do
    name="$(printf '%s' "$header" | sed -E 's/^##[[:space:]]+seam:[[:space:]]*//; s/[[:space:]]*\(replace\)[[:space:]]*$//; s/[[:space:]]+$//')"
    mode="augment"
    printf '%s' "$header" | grep -Eq '\(replace\)[[:space:]]*$' && mode="replace"
    case " $KNOWN_SEAMS " in
      *" $name "*)
        case " $AUGMENT_ONLY " in
          *" $name "*)
            if [ "$mode" = replace ]; then
              echo "  WARNING: seam '$name' is augment-only — '(replace)' is meaningless here and will be ignored"
            else
              echo "  bound: $name (augment)"
            fi ;;
          *) echo "  bound: $name ($mode)" ;;
        esac ;;
      *)
        echo "  WARNING: unknown seam '$name' — it matches no phase boundary and will NOT run. Fix the name (see the seam table) or remove the section." ;;
    esac
  done
fi
```

If any `WARNING:` line prints, **stop and show it to the operator before Phase 1
proceeds.** An unknown or misused seam is almost always a typo, or policy written
against a seam this command doesn't expose — silently ignoring it is the exact
failure this mechanism exists to prevent. Offer **[c]** continue anyway (the
offending section simply won't run) or **[a]** abort to fix the policy. When the
policy is clean, note which seams are bound and carry that into the phases below.

## Phase 1 — Pre-flight

Confirm the repo is safe to cut from. The CI gate degrades gracefully when no
workflows exist.

> Seam `pre-flight` fires at the start of this phase — run any bound policy steps
> before (augment) or in place of (replace) the checks below.

```bash
# CI status, if CI exists at all
if [ -d ".github/workflows" ] && [ -n "$(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | head -1)" ]; then
  gh run list --branch "$(git symbolic-ref --short HEAD)" --limit 5 --json name,conclusion --jq '.[] | "\(.name): \(.conclusion)"'
else
  echo "No CI workflows — using clean tree + zero blocking PRs as the gate"
fi
gh pr list --state open --json number,title --jq '.[] | "#\(.number) \(.title)"'
git status --porcelain
```

- CI present and failing → stop, fix first.
- CI absent → clean `git status` + no blocking open PRs is the gate.
- Open PRs that should land first → ask.

## Phase 1.5 — CHANGELOG completeness gate

Before drafting this release's entry, verify recent shipped tags each have a
CHANGELOG entry — cheap to catch now, expensive to reconstruct later. **No-op if
`CHANGELOG.md` is absent** (Phase 4 bootstraps it).

```bash
if [ ! -f CHANGELOG.md ]; then
  echo "(no CHANGELOG.md — skipping gate)"
else
  # For each of the last ~5 tags, check CHANGELOG.md has a header for its
  # version. The accepted header shape is format-AGNOSTIC: this repo uses the
  # bracket-LESS "## 0.4.1 (2026-07-16)" form, but Keep-a-Changelog
  # "## [0.4.1] - 2026-07-16" is equally valid. Accept BOTH — with an optional
  # leading 'v' and optional surrounding brackets — and match the version's
  # dots LITERALLY (escape them) so "0.4.1" cannot spuriously match "0X4X1" or
  # "0.4.10". This mirrors the optional-bracket extraction Phase 6 uses
  # (sed "/^## \[\?$NEW/…") and additionally tolerates a leading 'v' on the
  # header, so the read side (this gate) and the write side (Phase 4 draft /
  # Phase 6 extract) never disagree about what a version header looks like.
  for tag in $(git tag --sort=-v:refname | head -5); do
    ver="${tag#v}"                                     # strip a leading 'v'
    ver_re="$(printf '%s' "$ver" | sed 's/\./\\./g')"  # escape dots -> literal
    if grep -Eq "^##[[:space:]]+v?\[?${ver_re}\]?([[:space:]]|\$)" CHANGELOG.md; then
      echo "ok: $ver"
    else
      echo "MISSING: $ver"
    fi
  done
fi
```

For any tag missing an entry, surface the gap and offer: **[b]** backfill it now
(draft via Phase 4 logic over the `<prev-tag>..<tag>` range, insert in
chronological order, commit separately — backfills do **not** join the new
release tag), **[c]** continue and leave the gap, or **[a]** abort.

## Phase 2 — Detect the version tool

Detect the host repo's bump mechanism. **First match wins**, in this order. An
explicit `scripts/version.sh` is honored first; a plain `VERSION` file is the
most-general fallback. Because `npm` is matched on `package.json` alone — before
the `VERSION` fallback — the result is **provisional** whenever both files
coexist: reconcile it against the root `VERSION` file (see *Cross-source
reconciliation* below) before treating `VERSION_TOOL` as final.

```bash
VERSION_TOOL="" ; WHY=""
if [ -x ./scripts/version.sh ]; then
  VERSION_TOOL="version.sh"; WHY="./scripts/version.sh is executable"
elif command -v cargo-release >/dev/null 2>&1 && [ -f Cargo.toml ]; then
  VERSION_TOOL="cargo-release"; WHY="cargo-release + Cargo.toml"
elif command -v cargo-set-version >/dev/null 2>&1 && [ -f Cargo.toml ]; then
  VERSION_TOOL="cargo-set-version"; WHY="cargo-edit + Cargo.toml"
elif [ -f Cargo.toml ] && grep -q '^\[workspace\.package\]' Cargo.toml; then
  VERSION_TOOL="cargo-workspace"; WHY="Cargo [workspace.package] direct-edit"
elif command -v bumpversion >/dev/null 2>&1 && { [ -f .bumpversion.cfg ] || [ -f setup.cfg ]; }; then
  VERSION_TOOL="bumpversion"; WHY="bumpversion + config"
elif command -v bump2version >/dev/null 2>&1 && [ -f .bumpversion.cfg ]; then
  VERSION_TOOL="bump2version"; WHY="bump2version + .bumpversion.cfg"
elif command -v poetry >/dev/null 2>&1 && [ -f pyproject.toml ] && grep -q '\[tool.poetry\]' pyproject.toml; then
  VERSION_TOOL="poetry"; WHY="poetry + [tool.poetry]"
elif command -v npm >/dev/null 2>&1 && [ -f package.json ]; then
  VERSION_TOOL="npm"; WHY="npm + package.json"
elif [ -f VERSION ]; then
  VERSION_TOOL="version-file"; WHY="plain VERSION file at repo root"
fi
echo "${VERSION_TOOL:-<none>} — ${WHY:-no tool detected}"
```

**Surface the detected tool to the user.** If none is detected, do not proceed
silently — offer: **[m]** manual (they edit manifests, you commit + tag), or
**[a]** abort.

### Cross-source reconciliation (VERSION vs package.json)

`npm` is matched on the mere presence of `package.json`, so a repo that keeps a
plain root `VERSION` file **and** a `package.json` detects as `npm` even when
`VERSION` is the maintained source of truth — a blind `npm version` would then
bump and tag the wrong line. Whenever the provisional tool is `npm` **and** a
root `VERSION` file also exists **and** `package.json` carries a `version` field,
read both and reconcile before finalizing `VERSION_TOOL`:

```bash
# Runs only when detection landed on npm but a root VERSION file also coexists.
if [ "$VERSION_TOOL" = "npm" ] && [ -f VERSION ] && grep -q '"version"' package.json; then
  PKG_VER="$(node -p "require('./package.json').version" 2>/dev/null \
    || grep -m1 '"version"' package.json | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
  FILE_VER="$(head -1 VERSION | tr -d '[:space:]')"
  TAG_VER="$(git tag --sort=-v:refname | head -1 | sed 's/^v//')"
  if [ "$PKG_VER" = "$FILE_VER" ]; then
    echo "VERSION and package.json agree ($FILE_VER) — keeping npm, no change"
  else
    echo "DRIFT: package.json=$PKG_VER  VERSION=$FILE_VER  (latest tag: ${TAG_VER:-<none>})"
    # Do NOT proceed on npm. Recommend the source that matches the latest tag.
  fi
fi
```

- **They agree** → behave exactly as today: keep `VERSION_TOOL="npm"`, no new
  prompt, no behavior change.
- **They disagree** → **do not silently select `npm`.** Surface the drift to the
  operator and confirm which source is authoritative before any bump. Use the
  **latest git tag as the tie-breaker**: whichever of `package.json` / `VERSION`
  matches `git tag --sort=-v:refname | head -1` (leading `v` stripped) is the
  recommended authoritative source — set `VERSION_TOOL` to `version-file` or
  `npm` accordingly once the operator confirms.
- **Tie-breaker unavailable** (no tags yet, or neither value matches the latest
  tag) → present both values and let the operator choose explicitly; never
  default to `npm`.

This keys off runtime file existence and values only — no repo-specific names or
numbers — consistent with *General by design* and *report first, act second*.

### Drift gate (multi-source)

Version-bearing state can disagree in two ways, and a blind bump would mis-delta
the drifted one. Before reading the current version, verify agreement:

- **Within a single tool** — tools with more than one version-bearing file
  (`./scripts/version.sh check`, fatal if it fails; a `bumpversion --dry-run
  --allow-dirty` probe, advisory; etc.). `cargo` inheritance and `poetry` keep
  their version in one place, so this within-tool check is a no-op for them.
- **Across sources** — `npm`/`package.json` and a plain root `VERSION` file are
  **separate sources that can disagree** (e.g. a vestigial `package.json` left at
  a placeholder version). Do **not** treat `npm` or `version-file` as
  unconditionally drift-free: when both files exist, run the *Cross-source
  reconciliation* above before bumping.

## Phase 3 — Gather changes & decide the bump

```bash
last=$(git tag --sort=-v:refname | head -1)
git log "${last}..HEAD" --oneline
git diff "${last}..HEAD" --stat
```

Read the current version per tool (`./scripts/version.sh`; `grep -m1 '^version'
Cargo.toml`; `poetry version -s`; `node -p "require('./package.json').version"`;
`cat VERSION`; …). If there are **zero** commits since the last tag, stop —
nothing to release.

Present a semver analysis (https://semver.org) against whatever public surface
the repo exposes (API, CLI, protocol, config, file formats):

- **MAJOR** — removed/renamed public API, CLI, flags; broken wire/config contracts.
- **MINOR** — new backward-compatible API, commands, flags, options.
- **PATCH** — bug fixes, perf with identical behavior, internal refactors, docs.

Use conventional-commit prefixes (`feat`/`fix`/`chore`…) as input. Recommend a
level and **ask the user to confirm or override.**

## Phase 4 — Draft the CHANGELOG

> Seam `pre-changelog-style` fires before drafting — run any bound policy steps
> to enforce a house changelog style (augment) or produce the entry itself
> (replace).

If `CHANGELOG.md` exists, study its format and draft a new entry matching it
(header with today's date, a summary line, grouped changes, issue refs). If it's
**absent**, offer to bootstrap a "Keep a Changelog" template. Present the draft
and iterate until approved. Omit empty sections.

### Fold an existing `## Unreleased` section into the draft

A CHANGELOG may already carry a `## Unreleased` section — a convention where
merged-but-unshipped changes accumulate under a placeholder heading at the top of
the file (directly under the `# Changelog` title, above the newest version entry)
until the next release names them. If one exists, this release **is** that
version, so its entries must be folded into the new entry rather than left
stranded below the freshly-inserted version heading. Do this **at draft time**,
as part of matching the file's format above — not as a separate Phase 5 step:

1. Check for an existing `^## Unreleased` heading in `CHANGELOG.md`. If none
   exists, skip the rest of this sub-section entirely and behave exactly as
   before — the fold path is strictly opt-in on the heading's presence and makes
   **no** change to the draft-from-git-log path when absent.
2. If present, capture its body — every line between that heading and the next
   `^## ` heading (the newest existing version entry).
3. Merge those captured items into the git-log-derived draft, **de-duplicating**
   against items this release already derived from its git-log range. Entries in
   this repo cite issue/PR numbers in their lead-ins (e.g. `(#38, PR #42)`,
   `(#43)`), so a simple `grep -oE '#[0-9]+'` per bullet and a set-intersection
   against the range's issue/PR references is a sufficient dedup key for v1 (fall
   back to a fuzzy text match only for bullets that cite no number). Keep a
   captured `## Unreleased` bullet only if none of its numbers already appear in
   the git-log-derived draft.
4. Produce **one** combined entry headed with the new version + today's date,
   positioned **where `## Unreleased` was** — since that heading conventionally
   sits at the very top, this is a rename-in-place (`## Unreleased` →
   `## X.Y.Z (date)`), not an append elsewhere and no reordering. The
   `## Unreleased` heading itself is removed as part of drafting.

> Seam interaction: under a `pre-changelog-style (replace)` policy the default
> draft heuristic — including this fold — is skipped per the seam's
> "policy steps produce the entry; skip the default draft heuristic" semantics
> (see the seam table below), so folding any `## Unreleased` section becomes the
> policy's responsibility. Under `augment` (the default), the seam's steps run
> first and then this fold runs, so there is no conflict.

## Phase 5 — Apply

> Seam `pre-apply` fires before this phase — run any bound policy steps (e.g.
> edit extra manifests) before (augment) or in place of (replace) the bump.

Once approved:

1. Insert the new entry into `CHANGELOG.md`. Phase 4 has already produced the
   fully-merged entry (folding any `## Unreleased` section into it at draft
   time), so this is a straight "write this string" — no fold/merge happens here.
   The `pre-apply` seam therefore needs **no** change for the fold: it fires
   before this insert, after drafting is complete, so it never observes an
   un-folded entry regardless of how the string was assembled.
2. **Show the version-bearing files** the tool will touch, then bump. Dispatch on
   the detected tool; each branch must produce a version commit **and** tag:

   ```bash
   case "$VERSION_TOOL" in
     version.sh)        ./scripts/version.sh bump <level> --tag ;;
     cargo-release)     cargo release <level> --execute --no-publish ;;
     cargo-set-version) cargo set-version --bump <level> --workspace && cargo update --workspace ;;  # then commit + tag
     cargo-workspace)   sed -i.bak -E 's/^version = "[0-9.]+"/version = "'"$NEW"'"/' Cargo.toml && rm -f Cargo.toml.bak && cargo update --workspace ;;  # then commit + tag
     bumpversion)       bumpversion <level> --tag --commit ;;
     bump2version)      bump2version <level> --tag --commit ;;
     poetry)            poetry version <level> ;;  # then commit + tag v$(poetry version -s)
     npm)               npm version <level> -m "chore: bump version to %s" ;;
     version-file)      printf '%s\n' "$NEW" > VERSION ;;  # then commit (with CHANGELOG) + tag v$NEW
   esac
   ```

   For tools that don't self-commit (`cargo-set-version`, `cargo-workspace`,
   `poetry`, `version-file`), stage the bumped files **plus `CHANGELOG.md`**,
   commit, and `git tag -a "v$NEW" -m "v$NEW"` — match the repo's existing
   commit/tag convention (check `git log` and `git tag`).
3. **Verify**: re-read the version and confirm the tag exists
   (`git tag --sort=-v:refname | head -1`). For cargo, `cargo check --workspace`.
   Also confirm the `## Unreleased` fold (Phase 4) actually landed: grep the
   freshly-written `CHANGELOG.md` for any surviving `## Unreleased` heading and
   **fail loudly** if one remains — a stray heading means the fold silently
   didn't happen (e.g. both headings were left in place, or dedup dropped the
   merge), which would strand this release's entries below the new version.

   ```bash
   if grep -Eq '^##[[:space:]]+Unreleased([[:space:]]|$)' CHANGELOG.md; then
     echo "ERROR: a '## Unreleased' heading survived — Phase 4 fold did not complete" >&2
     exit 1
   fi
   ```

Show the result and get final confirmation.

## Phase 6 — Push & release

Three seams fire in this phase: `pre-push` (before the push), `post-push`
(immediately after it succeeds), and `pre-github-release` (before
`gh release create`). Run any bound policy steps at each boundary — e.g. a
`pre-github-release` gate that holds the Release until publish workflows finish.

After an explicit yes:

```bash
git push origin "$(git symbolic-ref --short HEAD)" --follow-tags
```

If a release workflow exists (`.github/workflows/release.yml`, typically
triggered on Release creation rather than tag push), create a GitHub Release so
it fires; use the CHANGELOG entry as the notes:

```bash
gh release create "v$NEW" --title "v$NEW" --notes-file <(sed -n "/^## \[\?$NEW/,/^## /p" CHANGELOG.md)
```

Otherwise the tag push alone completes the release.

## Phase 7 — Summary

```
RELEASE COMPLETE
================
Version:   v0.3.0
Tag:       v0.3.0 (pushed)
Tool:      version-file
CHANGELOG: 1 entry added
Release:   GitHub Release created  (or: tag push only — no release workflow)
```

> Seam `post-summary` fires after the summary — run any bound policy steps (e.g.
> deploy a docs site, notify a channel).

## Extension points — per-project release policy

The phases above are fixed, but a project can inject its own procedural steps at
**named phase boundaries** ("seams") without forking this command. This is what
projects migrating off Loom's removed `/loom:release` skill use to re-home
release policy — gates, extra manifest edits, post-release deploys.

### Where policy lives

**One** file, at the repo root: **`.repo/release-policy.md`**. There is a single
supported lookup path by design — no per-user, per-branch, or fallback locations.
Advisory *reminders* (e.g. "bump the protocol version when the API changes") still
belong in the repo's CLAUDE.md, which this command already reads as context; the
policy file is specifically for **procedural steps bound to a seam**.

### Policy file format

Each seam is an H2 section whose header is `## seam: <name>`, optionally suffixed
with `(replace)`. The section body is the prose/steps the command runs at that
boundary. Non-seam prose (a title, notes) is ignored — only `## seam:` headers
bind.

```markdown
# Release policy — my-project

## seam: pre-github-release

Hold the GitHub Release until both publish workflows finish:
- `gh run watch <npm-publish-run>` and `<crates-publish-run>` must both be green.

## seam: post-summary

Deploy the docs site: `gh workflow run deploy-site.yml`.

## seam: pre-push (replace)

Push via the release-bot identity instead of the default push:
`git -c user.name="release-bot" push origin "$(git symbolic-ref --short HEAD)" --follow-tags`
```

### Seams and semantics

Steps **augment** by default — they run *in addition to* the phase's built-in
action, at the boundary. Appending `(replace)` to the header makes the policy
steps stand in for the phase's default action instead. `(replace)` is only
meaningful where the boundary has a default action to replace:

| Seam | Fires | Augment (default) | `(replace)` |
|------|-------|-------------------|-------------|
| `pre-flight` | start of Phase 1 | run policy steps, then the standard pre-flight checks | policy steps become the pre-flight gate; skip the built-in CI/clean-tree checks |
| `pre-changelog-style` | before Phase 4 drafts the entry | run policy steps (e.g. enforce a house changelog style), then draft — the default draft still folds any existing `## Unreleased` section into the new entry | policy steps produce the entry; skip the default draft heuristic — **including** the `## Unreleased` fold, so a `(replace)` policy owns folding any `## Unreleased` section itself or it will be left stranded |
| `pre-apply` | before Phase 5 applies | run policy steps (e.g. edit extra manifests), then bump + commit + tag | policy steps perform the bump/commit/tag; skip the default apply dispatch |
| `pre-push` | before the `git push` in Phase 6 | run policy steps (a final gate), then push | policy steps perform the push; skip the default `git push` |
| `post-push` | immediately after the push succeeds | run policy steps | **augment-only** — no default action; a `(replace)` marker is ignored with a warning |
| `pre-github-release` | before `gh release create` | run policy steps (e.g. wait for publish workflows), then create the Release | policy steps create the Release (or intentionally skip it); skip the default `gh release create` |
| `post-summary` | after the Phase 7 summary | run policy steps (e.g. deploy a site) | **augment-only** — no default action; a `(replace)` marker is ignored with a warning |

### Unknown seams are surfaced, never silently ignored

Phase 0 enumerates every `## seam: <name>` in the policy file and checks each name
against the table above. A header naming no known seam — a typo like
`pre-changelog-styl`, or policy written against a seam this command doesn't expose
— prints a `WARNING` shown to the operator **before Phase 1 proceeds**, rather
than binding to nothing. Likewise, `(replace)` on an augment-only seam
(`post-push`, `post-summary`) warns. This is the guarantee that migrated policy
cannot silently stop firing.

### Migrating from `/loom:release`

Loom's removed `/loom:release` skill exposed five seams. **All five carry over
under the same names**, so policy targeting them binds unchanged:

| `/loom:release` seam | `/repo:release` seam | Change |
|----------------------|----------------------|--------|
| `pre-changelog-style` | `pre-changelog-style` | none (identity) |
| `pre-push` | `pre-push` | none (identity) |
| `post-push` | `post-push` | none (identity) |
| `pre-github-release` | `pre-github-release` | none (identity) |
| `post-summary` | `post-summary` | none (identity) |

`/repo:release` adds two boundaries Loom didn't have — `pre-flight` and
`pre-apply` — for gates that must run before the pre-flight checks or before the
version bump. To migrate, move the policy text into `.repo/release-policy.md`
under a `## seam: <name>` header for each old seam name. Nothing is dropped in the
rename.

## Principles

Cutting a release is irreversible and outward-facing, so unlike the safe-fix
hygiene commands it stays **report first, act second** — nothing is committed,
tagged, or pushed without a yes. **General by design** — the tool and the file
set are discovered, never assumed. If the repo needs a release-time *reminder*
(e.g. "bump the protocol version when the API changes"), keep it in the repo's
own CLAUDE.md; this command reads that context at runtime. Procedural policy that
must *run* at a specific point — a gate, an extra manifest edit, a post-release
deploy — goes in `.repo/release-policy.md` bound to a named seam instead (see
**Extension points — per-project release policy** above).
