#!/usr/bin/env node
// Verifies that .github/workflows/ci.yml's `changes` job (the "Detect
// Changes" job's `dorny/paths-filter@v4` step) classifies a control matrix
// of paths the same way the pinned action itself would.
//
// This parses the *actual* `jobs.changes` YAML out of ci.yml (via js-yaml,
// the same YAML library the action uses) and matches with the *actual*
// picomatch@2.3.1 package -- the exact version `dorny/paths-filter@v4`
// (pinned to commit ceb8a2b8f2d89434be7ff52d3de7ec3738c5cc9d, see the
// action's own package-lock.json) bundles, with the same `{dot: true}`
// MatchOptions the action's src/filter.ts uses. The only "reimplemented"
// piece is the small `Filter.isMatch()` PredicateQuantifier.EVERY branch
// from that exact file -- quoted verbatim below -- rather than a
// hand-rolled glob engine, so this does not carry the "another unverified
// glob approximation" risk flagged in issue #5366.
//
//   private isMatch(file: File, patterns: FilterRuleItem[]): boolean {
//     const isStatusMatch = (rule) => rule.status === undefined || ...
//     const aPredicate = (rule) => isStatusMatch(rule) && rule.isMatch(file.filename)
//     switch (this.filterConfig?.predicateQuantifier) {
//       case PredicateQuantifier.EVERY:
//         return patterns.every(aPredicate)
//       ...
//     }
//   }
//
// None of this workflow's `code:` patterns use a `status:` prefix, so
// `isStatusMatch` is always true here and `aPredicate` reduces to
// `rule.isMatch(filename)` -- which for a single-pattern rule item is just
// `picomatch(pattern, {dot: true}, true)(filename)` (createRuleItem() in
// the same file). See issue #5366 for the incident (PR #5365) this
// regression-tests.
//
// Run: node scripts/ci/verify_changes_filter.mjs
// (expects `picomatch` and `js-yaml` installed -- see the "Install
// changes-filter verification deps" step in the `lint` job of
// .github/workflows/ci.yml for the exact pinned install command)

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import yaml from "js-yaml";
import picomatch from "picomatch";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const CI_YAML_PATH = path.join(REPO_ROOT, ".github", "workflows", "ci.yml");

// Same MatchOptions dorny/paths-filter's src/filter.ts uses for every matcher.
const MATCH_OPTIONS = { dot: true };

function loadChangesFilterStep() {
  const raw = fs.readFileSync(CI_YAML_PATH, "utf8");
  const doc = yaml.load(raw);
  const changesJob = doc && doc.jobs && doc.jobs.changes;
  if (!changesJob) {
    throw new Error(`jobs.changes not found in ${CI_YAML_PATH}`);
  }
  const step = (changesJob.steps || []).find(
    (s) => typeof s.uses === "string" && s.uses.startsWith("dorny/paths-filter"),
  );
  if (!step) {
    throw new Error("no dorny/paths-filter step found in jobs.changes.steps");
  }
  const withBlock = step.with || {};
  const predicateQuantifier = withBlock["predicate-quantifier"];
  const filtersYaml = withBlock.filters;
  if (typeof filtersYaml !== "string") {
    throw new Error("jobs.changes' paths-filter step has no string `with.filters`");
  }
  const filters = yaml.load(filtersYaml);
  if (!filters || !Array.isArray(filters.code)) {
    throw new Error("parsed `filters:` has no top-level `code:` pattern list");
  }
  return { predicateQuantifier, codePatterns: filters.code };
}

// Verbatim port of Filter.isMatch()'s PredicateQuantifier.EVERY branch:
// `patterns.every(aPredicate)`. Each pattern is matched with the real,
// pinned-version picomatch, exactly as createRuleItem() does.
function isCode(filename, codePatterns) {
  return codePatterns.every((pattern) => picomatch(pattern, MATCH_OPTIONS, true)(filename));
}

function classify(filename, predicateQuantifier, codePatterns) {
  if (predicateQuantifier !== "every") {
    throw new Error(
      `expected jobs.changes' paths-filter step to set predicate-quantifier: every, got ${JSON.stringify(
        predicateQuantifier,
      )} -- a single-pattern-list filter without this quantifier reverts to the "some" default and reopens issue #5366`,
    );
  }
  return isCode(filename, codePatterns);
}

// Control matrix from issue #5366's "Acceptance and validation" list.
const CASES = [
  // --- PR #5365 regression: the exact six paths from the incident, plus
  // the two single-level .loom/* paths that were already (correctly)
  // excluded before this fix, kept as a non-regression control.
  { path: ".loom/docs/lease-record.md", code: false, group: "pr-5365-regression" },
  { path: ".loom/scripts/sweep-lease-publish.sh", code: false, group: "pr-5365-regression" },
  { path: ".loom/scripts/tests/ci-wired.txt", code: false, group: "pr-5365-regression" },
  { path: ".loom/scripts/tests/test-sweep-lease-publish.sh", code: false, group: "pr-5365-regression" },
  { path: ".loom/CLAUDE.md", code: false, group: "pr-5365-regression" },
  { path: ".loom/install-metadata.json", code: false, group: "pr-5365-regression" },

  // --- nested + root exclusions across every documented exclusion prefix
  { path: "docs/foo.md", code: false, group: "exclusions" },
  { path: "docs/sub/foo.md", code: false, group: "exclusions" },
  { path: "docs/a/b/c/deep.md", code: false, group: "exclusions" },
  { path: "site/foo.js", code: false, group: "exclusions" },
  { path: "site/sub/foo.js", code: false, group: "exclusions" },
  { path: ".claude/foo.md", code: false, group: "exclusions" },
  { path: ".claude/sub/agent.md", code: false, group: "exclusions" },
  { path: ".github/ISSUE_TEMPLATE/bug.md", code: false, group: "exclusions" },
  { path: ".github/ISSUE_TEMPLATE/sub/bug.md", code: false, group: "exclusions" },

  // --- markdown, root and nested anywhere in the tree
  { path: "README.md", code: false, group: "markdown" },
  { path: "RELEASING.md", code: false, group: "markdown" },
  { path: "sub/deep/NOTES.md", code: false, group: "markdown" },
  { path: "src/kicad_tools/README.md", code: false, group: "markdown" },

  // --- exact root dotfile/exclusion-file matches
  { path: "LICENSE", code: false, group: "exact-root-files" },
  { path: ".gitignore", code: false, group: "exact-root-files" },
  { path: ".gitattributes", code: false, group: "exact-root-files" },

  // --- existing asymmetric behavior, preserved on purpose: nested
  // dotfiles of the same *name* are NOT excluded, because the pattern is a
  // literal root filename (`.gitignore`), not `**/.gitignore`. This is
  // unchanged, pre-existing behavior -- not something issue #5366 should
  // fix.
  { path: "sub/.gitignore", code: true, group: "nested-dotfile-not-excluded" },
  { path: "boards/01-nucleo-blinky/.gitattributes", code: true, group: "nested-dotfile-not-excluded" },

  // --- positive controls: real code/config/build/test/board/workflow files
  { path: "src/kicad_tools/router/router.py", code: true, group: "positive" },
  { path: "tests/test_router.py", code: true, group: "positive" },
  { path: "boards/01-nucleo-blinky/generate_design.py", code: true, group: "positive" },
  { path: "scripts/ci/check-content-contracts.sh", code: true, group: "positive" },
  { path: "pyproject.toml", code: true, group: "positive" },
  { path: "uv.lock", code: true, group: "positive" },
  { path: ".github/workflows/ci.yml", code: true, group: "positive" },

  // --- prefix lookalikes: share a name prefix with an excluded directory
  // but are not actually under it
  { path: "docs-code/generate.py", code: true, group: "prefix-lookalike" },
  { path: "sitewide.js", code: true, group: "prefix-lookalike" },

  // --- unknown file type outside any exclusion
  { path: "random/unknown.xyz", code: true, group: "unknown-extension" },
];

function main() {
  const { predicateQuantifier, codePatterns } = loadChangesFilterStep();

  const nonNegated = codePatterns.filter((p) => typeof p !== "string" || !p.startsWith("!"));
  if (nonNegated.length > 0) {
    console.error(
      `FAIL: jobs.changes' \`code\` filter must be one negated (!pattern) list item per exclusion, ` +
        `found non-negated entries: ${JSON.stringify(nonNegated)}`,
    );
    process.exit(1);
  }

  let failures = 0;
  for (const { path: filename, code: expected, group } of CASES) {
    const actual = classify(filename, predicateQuantifier, codePatterns);
    const ok = actual === expected;
    if (!ok) failures += 1;
    console.log(`${ok ? "PASS" : "FAIL"} [${group}] ${filename} -> code=${actual} (expected ${expected})`);
  }

  // Mixed doc+source change: paths-filter's `code` output is an OR across
  // every changed file in a real diff, so simulate that directly rather
  // than only asserting per-file.
  const mixedFiles = ["docs/foo.md", "src/kicad_tools/bar.py"];
  const mixedCode = mixedFiles.some((f) => classify(f, predicateQuantifier, codePatterns));
  const mixedOk = mixedCode === true;
  if (!mixedOk) failures += 1;
  console.log(
    `${mixedOk ? "PASS" : "FAIL"} [mixed-doc-and-source] ${JSON.stringify(mixedFiles)} -> code=${mixedCode} (expected true)`,
  );

  // PR #5365 regression, aggregated: a real PR diff yields one `code`
  // boolean across all changed files in that PR, so also assert the
  // aggregate OR across all six incident paths together (not just each
  // path individually) is false.
  const pr5365Files = CASES.filter((c) => c.group === "pr-5365-regression").map((c) => c.path);
  const pr5365Code = pr5365Files.some((f) => classify(f, predicateQuantifier, codePatterns));
  const pr5365Ok = pr5365Code === false;
  if (!pr5365Ok) failures += 1;
  console.log(
    `${pr5365Ok ? "PASS" : "FAIL"} [pr-5365-regression-aggregate] all six paths together -> code=${pr5365Code} (expected false)`,
  );

  const total = CASES.length + 2;
  if (failures > 0) {
    console.error(`\n${failures}/${total} case(s) FAILED.`);
    process.exit(1);
  }
  console.log(`\nAll ${total} cases PASSED.`);
}

main();
