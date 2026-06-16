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

- `pre-changelog-style` — before Phase 1.5 (CHANGELOG style overrides).
- `pre-push` — before Phase 6 (irreversibility prompts, final gates).
- `post-push` — inside Phase 6 after `git push --tags` (post-push polling).
- `pre-github-release` — inside Phase 6 before `gh release create` (release
  gating on external workflows).
- `post-summary` — after Phase 7 (project-specific follow-ups).

### Example: override CHANGELOG style at `pre-changelog-style`

> At extension point `pre-changelog-style`: this project does NOT follow
> Keep-a-Changelog "Added/Changed/Fixed/Removed" grouping. Instead, group
> entries under thematic headers: "User-facing", "Internals", "Docs". Match
> the existing CHANGELOG.md for examples.

### Example: gate release on multiple workflows at `pre-github-release`

> At extension point `pre-github-release`: do NOT run `gh release create` until
> BOTH of the following workflows succeed for the just-pushed tag:
>   - `.github/workflows/publish-crate.yml`
>   - `.github/workflows/publish-npm.yml`
> Poll with `gh run list --workflow=<file> --limit 1 --json conclusion` until
> both report `success`. Time out after 15 minutes and ask the operator.
