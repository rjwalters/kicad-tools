# Config resolver conformance fixture (#4039)

A single `repo_root`-shaped tree used by the cross-language conformance test
required by #4039's acceptance criteria: "the same fixture tree resolves to
the same effective config from Rust, Python, and Bash". The Python resolver
and its test suite were retired along with the rest of `loom-tools/` (#4970,
per the operator's RETIRE decision on #4608); this fixture outlived that
package (it was carved out and relocated, not deleted) because its two
remaining consumers — Rust and Bash — are still alive and still need a shared
conformance target.

Deliberately exercises, in one tree:

- **Disjoint keys** at each tier (`legacyOnly`, `projectOnly`, `worktree.root`)
- **A key overridden across two tiers** (`overriddenByLocal`: set in
  `.loom-project/project.json`, overridden in `.loom-local/local.json`)
- **Nested-object recursive merge** (`autonomous.workFinder` gets fields from
  both the legacy and project tiers; `guards` gets fields from both legacy
  and project)
- **A key set at the lowest and highest tiers with an untouched middle tier**
  (`autonomous.perTokenConcurrency`: legacy=2, local=4, project doesn't
  mention it — local should win)

The private/shared-defaults tier is intentionally left out of this fixture
(every consumer sets `LOOM_CONFIG_DEFAULTS_FILE=""` before resolving it, to
keep the expected output host-independent) — that tier's soft-fail behavior
is already covered by each language's own unit tests.

`expected.json` is the canonical merged result all resolvers must
produce. Consumers:

- Rust: `loom-daemon/src/config_resolver.rs` (`test_conformance_fixture_*`)
- Bash: `defaults/scripts/tests/test-config-resolver.sh` (conformance-fixture
  test case)

(A Python resolver — `loom-tools/tests/test_config_resolver.py`'s
`TestConformanceFixture` — was a consumer until #4970 retired the package;
see git history for the module it pinned.)
