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

### Version-source declaration (a source-constant version, not a seam)

Separately from the seams above, a repo whose version lives in an **arbitrary
source constant** — one no Phase 2 heuristic can discover (a Swift
`AppVersion.current` assignment, a `CFBundle*` string in a build script, a
module-level `__version__`) — declares how to read and bump it with a
`## version-source` section in the same file. This is **data** (a source location
plus two shell one-liners), **not** a `## seam:` hook: the command *reads* the two
commands and runs them at Phases 3 and 5, it does not *run the section* as
procedural steps, and it has no augment/replace semantics. Parse and validate it
here, alongside the seam check:

```bash
VS_READ="" ; VS_BUMP=""
if [ -f "$POLICY_FILE" ] && grep -Eq '^##[[:space:]]+version-source[[:space:]]*$' "$POLICY_FILE"; then
  # Section body = lines between the '## version-source' header and the next '## ' header.
  VS_BODY="$(awk '
    /^##[[:space:]]+version-source[[:space:]]*$/ {f=1; next}
    /^##[[:space:]]/ {f=0}
    f' "$POLICY_FILE")"
  # Each command is the backtick-fenced inline code on its own '- read:' / '- bump:' line.
  VS_READ="$(printf '%s\n' "$VS_BODY" | sed -nE 's/^-[[:space:]]*read:[[:space:]]*`(.*)`[[:space:]]*$/\1/p' | head -1)"
  VS_BUMP="$(printf '%s\n' "$VS_BODY" | sed -nE 's/^-[[:space:]]*bump:[[:space:]]*`(.*)`[[:space:]]*$/\1/p' | head -1)"
  if [ -n "$VS_READ" ] && [ -n "$VS_BUMP" ]; then
    echo "version-source declared — read + bump both present (Phase 2 will select VERSION_TOOL='declared-policy')"
  elif [ -n "$VS_READ" ]; then
    echo "  WARNING: '## version-source' declares 'read:' but no 'bump:' — it can detect the current version but never apply a bump. Add the 'bump:' line or remove the section."
  elif [ -n "$VS_BUMP" ]; then
    echo "  WARNING: '## version-source' declares 'bump:' but no 'read:' — it can apply a bump but never detect the current version. Add the 'read:' line or remove the section."
  else
    echo "  WARNING: '## version-source' section present but neither a 'read:' nor a 'bump:' line parsed — expected two inline-code lines: '- read: \`…\`' and '- bump: \`…\`'."
  fi
fi
```

An **asymmetric** declaration (one of `read:`/`bump:` missing) is a near-certain
typo, so it warns exactly like an unknown seam — a `read` with no `bump` can
detect but never apply; a `bump` with no `read` can apply but never detect.
Surface any `WARNING:` line to the operator before Phase 1 proceeds, same as the
seam warnings above, and carry `VS_READ`/`VS_BUMP` into Phases 2, 3, and 5.

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
# Open PRs, over REST. Any `gh pr`/`gh issue` subcommand invoked with --json is
# GraphQL-backed and spends the much smaller GraphQL rate-limit budget, which on
# a busy multi-agent host is routinely exhausted while the REST `core` bucket
# sits nearly unused.
gh api "repos/{owner}/{repo}/pulls?state=open" --paginate --jq '.[] | "#\(.number) \(.title)"'
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

Detect the host repo's bump mechanism. **First match wins**, in this order. A
repo-authored `## version-source` declaration (parsed in Phase 0) is
repo-authored ground truth and is honored **first**, ahead of every heuristic;
after it, an explicit `scripts/version.sh` is honored, and a plain `VERSION` file
is the most-general fallback. Because `npm` is matched on any version-bearing
`package.json` — before the `VERSION` fallback — the result is **provisional**
whenever both files coexist: reconcile it against the root `VERSION` file (see
*Cross-source reconciliation* below) before treating `VERSION_TOOL` as final.

```bash
VERSION_TOOL="" ; WHY=""
if [ -n "$VS_READ" ] && [ -n "$VS_BUMP" ]; then
  VERSION_TOOL="declared-policy"; WHY=".repo/release-policy.md version-source"
elif [ -x ./scripts/version.sh ]; then
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
elif [ -f pyproject.toml ] && grep -qE '^\[project\]' pyproject.toml && \
     awk '/^\[project\]/{f=1;next} /^\[/{f=0} f' pyproject.toml | grep -qE '^version[[:space:]]*='; then
  VERSION_TOOL="pyproject"; WHY="pyproject.toml [project].version (PEP 621)"
elif command -v npm >/dev/null 2>&1 && [ -f package.json ] && grep -q '"version"' package.json; then
  VERSION_TOOL="npm"; WHY="npm + package.json"
elif [ -f VERSION ]; then
  VERSION_TOOL="version-file"; WHY="plain VERSION file at repo root"
fi
echo "${VERSION_TOOL:-<none>} — ${WHY:-no tool detected}"
if [ "$VERSION_TOOL" = "declared-policy" ] && [ -x ./scripts/version.sh ]; then
  echo "  WARNING: a '## version-source' declaration AND an executable scripts/version.sh both exist — the declaration wins, but this pairing usually signals a stale/leftover declaration. Confirm the declaration is current before proceeding."
fi
```

Three branch details are load-bearing:

- **A declared `## version-source` outranks every heuristic.** It is
  repo-authored ground truth, not a guess, so it wins ahead of even
  `scripts/version.sh`. The one caveat is the combination warning above: a
  declaration *and* an executable `scripts/version.sh` usually means the
  declaration is stale, so the branch warns rather than silently shadowing the
  script.
- **`poetry` stays ahead of `pyproject`.** Modern poetry projects also carry a
  `[project]` table, so ordering is what keeps `poetry version` (the correct
  apply path for those repos) winning over a raw TOML edit.
- **The PEP 621 check is scoped to the `[project]` table.** The `awk`
  block-extractor feeds `grep` only the lines *inside* `[project]`, so an
  unrelated `version = …` in e.g. `[tool.poetry.dependencies]` can't
  false-positive the branch. Likewise `npm` now requires a `"version"` field
  rather than the mere presence of `package.json` — a version-less scaffold
  (`{"private": true}`) cannot be the version source, so it falls through the
  chain instead of misdirecting the bump.

**Surface the detected tool to the user.** If none is detected, do not proceed
silently — offer: **[m]** manual (they edit manifests, you commit + tag), or
**[a]** abort.

On the **[m]** manual path, once the operator identifies **where the version
actually lives** — often from the repo's CLAUDE.md, as with a source-constant
version no heuristic could find — **offer to record it** as a `## version-source`
block in `.repo/release-policy.md`, so the next release detects it automatically
instead of rediscovering it from scratch. Confirm the two commands with the
operator (a `read:` that prints the current version, a `bump:` that rewrites it
in place with the new version arriving as `$1`) and, on a yes, append the block —
creating `.repo/release-policy.md` if absent:

```bash
# Run only after the operator confirms the read:/bump: commands for their source.
mkdir -p .repo
[ -f .repo/release-policy.md ] || printf '# Release policy\n' > .repo/release-policy.md
printf '\n## version-source\n\n- read: `%s`\n- bump: `%s`\n' \
  "$VS_READ_CONFIRMED" "$VS_BUMP_CONFIRMED" >> .repo/release-policy.md
```

The written block is exactly the shape Phase 0 parses back (a `## version-source`
header, one `` - read: `…` `` line, one `` - bump: `…` `` line), so the next
release's Phase 0 picks it up and Phase 2 selects `declared-policy` with no manual
step — closing the loop the mechanism above opens.

### Cross-source reconciliation (VERSION vs package.json)

`npm` is matched on any `package.json` carrying a `version` field, so a repo that
keeps a plain root `VERSION` file **and** a versioned `package.json` detects as
`npm` even when `VERSION` is the maintained source of truth — a blind `npm
version` would then bump and tag the wrong line. Whenever the provisional tool is
`npm` **and** a root `VERSION` file also exists, read both and reconcile before
finalizing `VERSION_TOOL`:

```bash
# Runs only when detection landed on npm but a root VERSION file also coexists.
if [ "$VERSION_TOOL" = "npm" ] && [ -f VERSION ]; then
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
  --allow-dirty` probe, advisory; etc.). `cargo` inheritance, `poetry`, and
  `pyproject` keep their version in one place, so this within-tool check is a
  no-op for them — `pyproject`'s `uv.lock` mirror is a **derived cache**
  regenerated by the apply step (Phase 5), not an independent source requiring a
  pre-bump drift check.
- **Across sources** — `npm`/`package.json` and a plain root `VERSION` file are
  **separate sources that can disagree** (e.g. a vestigial `package.json` left at
  a placeholder version). Do **not** treat `npm` or `version-file` as
  unconditionally drift-free: when both files exist, run the *Cross-source
  reconciliation* above before bumping.
- **A declared `## version-source`** names a single source, so there is no
  cross-source coexistence to reconcile — the drift gate is intentionally a
  **no-op** for `declared-policy`. If such a repo *also* keeps a `VERSION` or
  `package.json`, auto-reconciling that combination is out of scope; treat it as a
  documented limitation (the declaration remains authoritative), not a silent
  skip.

## Phase 3 — Gather changes & decide the bump

```bash
last=$(git tag --sort=-v:refname | head -1)
git log "${last}..HEAD" --oneline
git diff "${last}..HEAD" --stat
```

Read the current version per tool. For `declared-policy`, run the `read:` command
captured in Phase 0 — `sh -c "$VS_READ"`, which prints the current version to
stdout. Otherwise use the tool-specific command (`./scripts/version.sh`; `grep -m1 '^version'
Cargo.toml`; `poetry version -s`; `python3 -c "import tomllib;
print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"` for
`pyproject` (Python 3.11+; on older Python fall back to the same
`awk`-scoped-then-`sed` one-liner the Phase 2 branch uses); `node -p
"require('./package.json').version"`; `cat VERSION`; …). If there are **zero**
commits since the last tag, stop — nothing to release.

Present a semver analysis (https://semver.org) against whatever public surface
the repo exposes (API, CLI, protocol, config, file formats):

- **MAJOR** — removed/renamed public API, CLI, flags; broken wire/config contracts.
- **MINOR** — new backward-compatible API, commands, flags, options.
- **PATCH** — bug fixes, perf with identical behavior, internal refactors, docs.

Use conventional-commit prefixes (`feat`/`fix`/`chore`…) as input. Recommend a
level and **ask the user to confirm or override.**

## Phase 3.5 — Version-citation check (advisory)

Now that the bump level is confirmed and `$NEW` (the version this run is
cutting) is known, check tracked markdown prose for citations of a version
that has **neither shipped** (no `## <version>` section in `CHANGELOG.md`)
**nor is the one about to ship** — that citation is either a stale/broken
reference or an honest forward reference nobody circled back to resolve once
the version actually shipped (repo#215, repo#228: `README.md` said "That was
not true before 0.9.0" and `SKILL.md` said "as of 0.9.0 it holds" while
`VERSION` was still `0.8.1` and neither guess was yet confirmed correct).
**Advisory only — report and continue, never block the release.** No-op if
`CHANGELOG.md` is absent, matching Phase 1.5.

```bash
if [ ! -f CHANGELOG.md ]; then
  echo "(no CHANGELOG.md — skipping version-citation check)"
else
  # This repo's own header-citation STYLE, derived from CHANGELOG.md itself:
  # does ANY header carry a leading 'v' (`## v1.2.3`)? If none do, this repo's
  # own version vocabulary is bare ("1.2.3"), and a prose citation written
  # WITH a leading 'v' ("since v0.10.0") is very likely naming something
  # ELSE's release history, not this repo's — a real false-positive class: a
  # workspace whose own docs extensively discuss a DIFFERENT tool's version
  # history (e.g. "removed in v0.10.0" naming an embedded orchestrator, not
  # this repo) would otherwise get flagged on every one of those references.
  # If this repo's own headers DO sometimes carry a 'v', both forms count as
  # this repo's own vocabulary.
  V_PREFIX=""
  grep -Eq '^##[[:space:]]+v[0-9]' CHANGELOG.md && V_PREFIX='v?'

  # Version-boundary phrasing that plausibly cites a version's ship status.
  # Anchoring the scan to this phrase list — rather than a bare
  # \d+\.\d+\.\d+ scan over all markdown — is what keeps dependency pins
  # ("loom 0.18.0"), image tags ("ubuntu:24.04"), and lockfile fields
  # ("lockfileVersion: '9.0'") out of the results: none of them are written
  # with this phrasing, and the two-dot/one-dot shapes of the latter two don't
  # match the X.Y.Z pattern below regardless.
  LEADIN='(before|since|after|until|prior to|as of|as early as|starting (in|with)|introduced in|added in|removed in|deprecated (in|since)|available (since|as of)|released in|shipped in|requires( at least)?)'

  FOUND=0
  for f in $(git ls-files '*.md' | grep -v -x 'CHANGELOG.md'); do
    while IFS=: read -r lineno match; do
      ver="$(printf '%s' "$match" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+')"
      [ "$ver" = "$NEW" ] && continue   # the version being cut is never flagged
      # Reuse Phase 1.5's exact header-matching regex (bracket-optional,
      # leading-'v'-optional, dots escaped literal) so the two checks can
      # never disagree about what counts as a "shipped" version header.
      ver_re="$(printf '%s' "$ver" | sed 's/\./\\./g')"
      if grep -Eq "^##[[:space:]]+v?\[?${ver_re}\]?([[:space:]]|\$)" CHANGELOG.md; then
        continue   # shipped — CHANGELOG.md already has a section for it
      fi
      echo "  CITED-UNSHIPPED: $f:$lineno: \"$match\""
      FOUND=1
    done < <(grep -inoE "${LEADIN}[[:space:]]+${V_PREFIX}[0-9]+\.[0-9]+\.[0-9]+" "$f")
  done
  [ "$FOUND" = 0 ] && echo "ok: no prose cites an unshipped, non-target version"
fi
```

If any `CITED-UNSHIPPED:` line prints, surface it to the operator — it's either
a stale/broken reference worth fixing now, or a legitimate forward reference to
a release that hasn't happened yet (leave it; it resolves itself once that
version ships, the way repo#215's two sentences did when 0.9.0 was cut). Either
way this check is **advisory**: report and continue — never block Phase 4.

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
4. **Coherence check — read the merged item list as one document.** Changelog
   entries are written serially, one PR at a time over the life of the release
   cycle, but this section is read all at once, from the vantage point of the
   finished release. That mismatch lets two individually-accurate entries land
   in the same `## Unreleased` section while jointly asserting something
   incoherent — a claim corrected and then the underlying thing fixed so the
   original claim becomes true again, a default flipped and flipped back, a
   limitation documented and then removed, a feature reworked or renamed
   between when it was logged and now. Before producing the combined entry
   below, read every item being folded — the captured `## Unreleased` bullets
   **and** the git-log-derived draft items, together, as the single document a
   release reader will see — and flag any pair that:
   - **Contradicts or supersedes another** — read for meaning, not keywords;
     this is a judgment call about what the entries jointly claim, not a
     regex or pattern match.
   - **Near-duplicates another** — the same underlying change logged twice
     because it landed across two different PRs.

   Report each flagged pair by quoting **both entries verbatim, side by
   side**, so the operator sees the contradiction directly instead of being
   told one exists — do not just name the entries or summarize the conflict.
   This is report-and-confirm only: never rewrite, merge, reorder, or drop an
   entry to resolve it. Ask the operator whether to hand-edit before
   continuing, or proceed with the draft exactly as captured — either answer
   is fine, and if the operator does nothing this check **does not block**
   moving on to the next step. A clean set of items produces **no output at
   all** — do not report "no contradictions found" or otherwise add
   commentary when there is nothing to flag; this check must stay silent in
   the common case.
5. Produce **one** combined entry headed with the new version + today's date,
   positioned **where `## Unreleased` was** — since that heading conventionally
   sits at the very top, this is a rename-in-place (`## Unreleased` →
   `## X.Y.Z (date)`), not an append elsewhere and no reordering. The
   `## Unreleased` heading itself is removed as part of drafting.

> Seam interaction: under a `pre-changelog-style (replace)` policy the default
> draft heuristic — including this fold and its coherence check (step 4 above)
> — is skipped per the seam's "policy steps produce the entry; skip the
> default draft heuristic" semantics (see the seam table below), so folding
> any `## Unreleased` section, and checking it for contradictions, becomes the
> policy's responsibility. Under `augment` (the default), the seam's steps run
> first and then this fold — including the coherence check — runs, so there is
> no conflict.

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
2. **Merged-work coverage check (advisory).** Cross-reference merged PRs since
   the last tag against the entry just inserted above — the forward-looking
   sibling of Phase 1.5's retrospective gate. Phase 1.5 catches a *shipped* tag
   that turns out to be missing an entry; by then the release is already out
   and the range is cold. This catches the same kind of gap *before* it ships:
   work that merged into this range but never made it into the entry at all
   (repo#229 — at v0.9.0, `aws_create()` shipped with `KeyName: None` (#182)
   and `down` could terminate a repurposed fleet host's disk with no guard
   (#171), and neither had a CHANGELOG line until the operator asked for the
   range to be re-checked by hand). **Advisory only — report and continue,
   never block**, matching Phase 1.5's and Phase 3.5's posture: plenty of
   merged commits legitimately have no entry (a `docs:` fix, a dependency
   bump, a revert pair), so the check must not let bookkeeping commits
   dominate the list.

   ```bash
   # $last is the same previous-tag reference Phase 3 captured; HEAD here is
   # still pre-tag (Phase 6 cuts the tag), so the range is exactly "everything
   # unshipped a moment ago". Scope to the conventional-commit prefixes most
   # likely to need a changelog line — feat/fix/security in scope by default,
   # docs/chore/test/build NOT in scope. This default is what keeps a
   # `docs: update WORK_LOG` commit, or a Dependabot `build(deps): …` bump,
   # off the list without a single one of them being special-cased by name.
   FILTER='^(feat|fix|security)(\(|:)'

   FOUND=0
   for sha in $(git log "${last}..HEAD" --format='%H'); do
     subject="$(git log -1 --format='%s' "$sha")"
     echo "$subject" | grep -Eq "$FILTER" || continue
     # Same grep -oE '#[0-9]+' key the Unreleased fold's dedup step (above)
     # uses, applied here to the FULL commit message rather than just the
     # subject's trailing PR number — a squash-merge body commonly carries a
     # "Closes #NNN" naming the ORIGINATING issue, which is what this repo's
     # own entries usually cite (the issue number), not the merge's own PR
     # number. A commit is counted as logged if ANY number it references
     # already appears in CHANGELOG.md.
     nums="$(git log -1 --format='%B' "$sha" | grep -oE '#[0-9]+' | tr -d '#' | sort -u)"
     [ -n "$nums" ] || continue   # no #N anywhere — nothing to cross-reference (e.g. a local/rebased commit)
     logged=0
     for n in $nums; do
       grep -Eq "#${n}([^0-9]|\$)" CHANGELOG.md && { logged=1; break; }
     done
     [ "$logged" = 1 ] && continue
     echo "  UNLOGGED: $subject"
     FOUND=1
   done
   [ "$FOUND" = 0 ] && echo "ok: every in-scope merged PR is referenced in this release's CHANGELOG entry"
   ```

   For each `UNLOGGED:` line, confirm with the operator, one at a time: draft
   a bullet for it now (folding it into the entry just inserted, the same
   move as a Phase 1.5 backfill) or confirm it's intentionally unlogged. Two
   things commonly explain a legitimate confirm-and-move-on: the PR is cited
   in `CHANGELOG.md` under the *issue* it closed rather than under its own PR
   number (a real gap in this check — the git history carries the mapping
   only when the squash-merge body happens to include a `Closes #N` line; when
   it doesn't, no automated cross-reference can find it), or the change is
   real but genuinely not release-note-worthy (an internal-only refactor typed
   `fix:`, a security-classified doc tweak). Either answer is fine — this is
   report-and-confirm only, never a blocker, and it runs once per release
   regardless of how many items it finds.
3. **Show the version-bearing files** the tool will touch, then bump. Dispatch on
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
     pyproject)         python3 -c 'import re,sys;p="pyproject.toml";t=open(p).read();n=re.subn(r"(?ms)(^\[project\][^\n]*\n(?:(?!^\[)[^\n]*\n)*?version[ \t]*=[ \t]*)([\"\x27])[^\"\x27\n]*\2",lambda m:m.group(1)+m.group(2)+sys.argv[1]+m.group(2),t,count=1);sys.exit("ERROR: no [project].version found in pyproject.toml") if n[1]==0 else open(p,"w").write(n[0])' "$NEW" && if [ -f uv.lock ] && command -v uv >/dev/null 2>&1; then uv lock; fi ;;  # then commit (with CHANGELOG) + tag v$NEW
     npm)               npm version <level> -m "chore: bump version to %s" ;;
     version-file)      printf '%s\n' "$NEW" > VERSION ;;  # then commit (with CHANGELOG) + tag v$NEW
     declared-policy)   sh -c "$VS_BUMP" _ "$NEW"; GOT="$(sh -c "$VS_READ")"; [ "$GOT" = "$NEW" ] || { echo "ERROR: declared bump: ran but the source still reads '$GOT', not '$NEW' — the bump: command did not take (regex/path mismatch?). Refusing to commit/tag." >&2; exit 1; } ;;  # declared bump: command, $NEW passed as $1, then post-verified against declared read:; then commit (with CHANGELOG) + tag v$NEW
   esac
   ```

   For tools that don't self-commit (`cargo-set-version`, `cargo-workspace`,
   `poetry`, `pyproject`, `version-file`, `declared-policy`), stage the bumped
   files **plus `CHANGELOG.md`**, commit, and `git tag -a "v$NEW" -m "v$NEW"` —
   match the repo's existing commit/tag convention (check `git log` and `git tag`).

   The `pyproject` branch rewrites only the `version` line **inside** the
   `[project]` table and leaves every other line byte-identical. Four details
   are load-bearing, each guarding a failure this branch would otherwise cause:

   - **Scoping is line-anchored** (`(?:(?!^\[)[^\n]*\n)*?`), matching lines up
     to the next table *header*, rather than "any run of non-`[` characters".
     The looser form silently matches nothing whenever a list-valued key
     (`classifiers`, `dependencies`, `keywords`) appears before `version` in
     `[project]` — a no-op bump that would still get committed and tagged.
   - **A zero-substitution result is fatal**, not a no-op: the branch exits
     non-zero with `ERROR: no [project].version found` so a mis-shaped
     `pyproject.toml` fails the apply instead of tagging a stale version.
   - **The quote style is captured, not assumed** (`([\"\x27])…\2`): TOML admits
     both `version = "1.0.0"` and `version = '1.0.0'`, and Phase 2 detection
     accepts either, so the rewrite captures whichever quote character opens the
     string and reuses it to close the replacement. A double-quote-only pattern
     would detect a single-quoted repo as `pyproject` and then fail it at apply
     on the zero-substitution guard above — detection and apply must agree. (The
     `\x27` escape is how the single quote is written without breaking out of the
     shell's single-quoted `python3 -c '…'` argument.)
   - **`uv.lock` is refreshed when present**, so `uv sync --locked` CI doesn't
     break on the now-stale locked version. It's gated by an `if` on both the
     lock file existing and `uv` being on `PATH`, so a flit- or setuptools-only
     PEP 621 repo with no lock file skips the block entirely (exit 0) rather than
     failing the branch on the gate's own false status — while a genuine `uv
     lock` failure (resolver conflict, corrupt lockfile, offline network) still
     propagates and aborts the apply. Do **not** collapse this back into an
     `A && B && uv lock || :` chain: the trailing `|| :` there masks the real
     failure too, and the branch would go on to stage an un-regenerated
     `uv.lock`. Stage the regenerated `uv.lock` alongside `pyproject.toml` and
     `CHANGELOG.md` in the version commit.

   The `declared-policy` branch runs the repo's declared `bump:` command with the
   new version passed as `$1` — `sh -c "$VS_BUMP" _ "$NEW"` sets `$0=_` and
   `$1=$NEW` — then joins the same stage-plus-`CHANGELOG.md`, commit, and tag path
   as the other non-self-committing tools. The declaration owns **only** the
   in-file edit; this command still owns the commit and the tag, so a repo
   declares just the one-line `sed`/edit, never the git plumbing.

   - **The bump is post-verified against the declared `read:`**, mirroring the
     `pyproject` branch's zero-substitution guard: after running `bump:`, the
     branch re-reads the version with `sh -c "$VS_READ"` and **fails loud**
     (`exit 1`, no commit, no tag) unless it now equals `$NEW`. This closes the
     one silent-mis-tag footgun unique to this branch — `bump:` runs an arbitrary
     repo-declared `sed`/edit, so a stale constant name, wrong path, or drifted
     regex leaves the file untouched while `sh -c` still exits 0. Without the
     re-read assertion the apply would commit and tag `v$NEW` over a source that
     still holds the old version; with it, a `bump:` that doesn't take fails the
     release instead of shipping a stale tag — the same guarantee `pyproject`
     gives on a zero-substitution rewrite.
4. **Verify**: re-read the version and confirm the tag exists
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

### Declaring a version source (a declaration, not a seam)

Some repos keep their version in a source constant **no Phase 2 heuristic can
discover** — a Swift `AppVersion.current` assignment, a `CFBundle*` string in a
build script, a module-level `__version__`. For these, `.repo/release-policy.md`
carries a `## version-source` section. It lives in the same file as the seams but
is deliberately **not** a seam: it is *data* the command reads, not procedural
steps bound to a phase boundary. It takes **no** `(replace)` suffix and has no
augment/replace mode — there is nothing to augment or replace, only two commands
to read and run.

```markdown
## version-source

- read: `sed -n 's/.*AppVersion\.current = "\(.*\)".*/\1/p' Sources/App/Version.swift`
- bump: `sed -i '' "s/AppVersion\.current = \".*\"/AppVersion.current = \"$1\"/" Sources/App/Version.swift`
```

- **`read:`** — a shell one-liner (backtick-fenced inline code) that prints the
  current version to stdout. Phase 3 runs it via `sh -c "$VS_READ"`.
- **`bump:`** — a shell one-liner that rewrites the version in place, with the new
  version arriving as `$1`. Phase 5 runs it via `sh -c "$VS_BUMP" _ "$NEW"`. The
  command owns only the in-file edit; `/repo:release` still stages, commits (with
  `CHANGELOG.md`), and tags — declare just the edit, not the git plumbing.

When both lines are present, Phase 2 sets `VERSION_TOOL="declared-policy"`, which
**outranks every heuristic** (even `scripts/version.sh`) because it is
repo-authored ground truth. Phase 0 validates the block: an **asymmetric**
declaration (only `read:` or only `bump:`) warns like an unknown seam, since a
read-only source can detect but never apply and a bump-only source can apply but
never detect. A `## version-source` declaration *alongside* an executable
`scripts/version.sh` also warns (the declaration still wins, but the pairing
usually means a stale leftover). Because it names a single source, the
multi-source drift gate is a no-op for `declared-policy`.

The first manual (`[m]`) release in a repo with such a source **records** it here:
after the operator supplies the version by hand, `/repo:release` offers to write
this block so every subsequent release detects it automatically. This keeps the
single-file design principle intact — one `.repo/release-policy.md`, holding both
seams (procedure) and the version-source declaration (data), with no second file.

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
