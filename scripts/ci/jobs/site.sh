#!/usr/bin/env bash
# scripts/ci/jobs/site.sh -- website checks (Issue #7665).
# The single copy of the `site` job's check commands; both
# .github/workflows/ci.yml's `site` job and scripts/ci/local-gate.sh run this
# file (2am#7561 ruling: commands written once, the workflow calls the script).
# SKIP, never a silent PASS, when node tooling is absent.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

command -v npm >/dev/null 2>&1 || die_skip "npm not found; skipping site job"

step "Install site dependencies (lockfile)"
npm --prefix site ci --no-audit --no-fund

step "Site tests"
npm --prefix site test

step "Astro check"
npm --prefix site run check

step "Site build"
npm --prefix site run build

step "Check internal links"
# check-links runs without --external (no network in CI).
npm --prefix site run check-links
