# Release Reminders (example)

This file is injected when a prompt mentions release-related keywords (default
pattern: filename "release"). Use it for project-specific reminders, conventions,
and procedural overrides that must execute at the named seams in `/loom:release`.

## Advisory reminders (any phase)

- Bump the protocol version constant when the wire format changes.
- Update the migration guide for any deletion-class change.
- Verify the CI badge in README.md still points at the right branch.

## Procedural overrides at named seams

The `/loom:release` skill exposes named seams (HTML-comment markers) at
well-chosen phase boundaries. To inject content at a specific seam, write the
override in prose and reference the seam name. The agent reading the skill and
this topic file will compose them at runtime.

Available seams (see the "Operator extension points" section at the bottom of
`/loom:release` for the authoritative list):

- `pre-changelog-style` — before Phase 1.5 (CHANGELOG style overrides). Content
  authored here is in scope for **both** Phase 1.5's detection regex and
  Phase 4's drafting prose by contract.
- `pre-push` — before Phase 6 (irreversibility prompts, final gates).
- `post-push` — inside Phase 6 after `git push --tags` (post-push polling).
- `pre-github-release` — inside Phase 6 before `gh release create` (release
  gating on external workflows).
- `post-summary` — after Phase 7 (project-specific follow-ups).

### Composition semantics: augment vs replace

Each override below uses one of two prose prefixes to signal whether it adds
to the skill's default behavior at the seam (augment) or substitutes for it
(replace):

- `At extension point <seam>: <directive>` — **augment**. The skill's default
  at the seam still runs; the directive layers alongside it.
- `At extension point <seam>, replacing default behavior: <directive>` —
  **replace**. The directive substitutes for the default; the default does
  not run.

Prefer augment unless the project's flow is structurally incompatible with
the default. See the "Operator extension points" section in `/loom:release`
for the full convention.

### Example (augment): override CHANGELOG style at `pre-changelog-style`

> At extension point `pre-changelog-style`: this project does NOT follow
> Keep-a-Changelog "Added/Changed/Fixed/Removed" grouping. Instead, group
> entries under thematic headers: "User-facing", "Internals", "Docs". Match
> the existing CHANGELOG.md for examples. The thematic-header convention
> applies to BOTH Phase 1.5 detection (headers still match `^## \[X.Y.Z\]`
> for gap-finding) and Phase 4 drafting (use thematic sub-sections instead
> of Keep-a-Changelog groups).

This is an augment because Phase 1.5's regex still applies to the standard
`## [X.Y.Z]` header pattern — only Phase 4's grouping is project-specific.

### Example (augment): add an irreversibility prompt at `pre-push`

> At extension point `pre-push`: ask the operator to confirm the irreversibility
> of pushing tag `vX.Y.Z` (this will trigger downstream publish workflows that
> cannot be unwound). Proceed only after explicit confirmation.

Phase 6 still runs its default `git push origin main --tags` after the
confirmation — the prompt augments, it does not replace.

### Example (replace): gate release on multiple workflows at `pre-github-release`

> At extension point `pre-github-release`, replacing default behavior: do NOT
> run `gh release create` immediately. Instead, poll BOTH of the following
> workflows for the just-pushed tag until each reports `success`:
>   - `.github/workflows/publish-crate.yml`
>   - `.github/workflows/publish-npm.yml`
> Poll with `gh run list --workflow=<file> --limit 1 --json conclusion`. Time
> out after 15 minutes and ask the operator. Once both succeed, run
> `gh release create vX.Y.Z --title "vX.Y.Z" --notes-file -` with the
> CHANGELOG excerpt as the release notes.

This is a replace because the default behavior at `pre-github-release` (run
`gh release create` immediately) is structurally incompatible with the
"wait for upstream workflows first" requirement — one must yield, and the
directive owns the GitHub Release creation entirely.
