#!/usr/bin/env bash
# build-gate.sh - buildGate.command for this repo: fast build+test backstop
# across Rust, Python, and bash. Runs in the worktree; exits non-zero on the
# first failing stage (set -e) so buildGate.command's single exit code is
# meaningful. See .loom/docs/build-gate.md.
#
# Scope decisions (issue #3749):
#   - cargo test --workspace covers the Rust crates (loom-daemon, loom-api).
#   - loom-tools pytest runs via `uv run` so the package is importable from
#     the project venv; scoped to exclude the live-network e2e suite
#     (tests/integration) and the known slow real-time bypass-poll integration
#     test file (already stubbed in-tree, but excluded here to keep the gate
#     fast and stable).
#   - bash scripts/test-installer.sh runs the 131-case installer suite.
#   - mcp-loom (TypeScript) is intentionally EXCLUDED: it needs npm install/ci
#     in a fresh worktree (no guaranteed warm node_modules), which adds
#     unpredictable latency to a gate that also runs once per PR. CI still
#     gates the mcp-loom build.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "[build-gate] cargo test --workspace"
cargo test --workspace

echo "[build-gate] loom-tools pytest (scoped, excludes network e2e + known slow poll test)"
(
  cd loom-tools
  uv run pytest tests/ -q \
    --ignore=tests/integration \
    --ignore=tests/tokens/test_agent_spawn_integration.py
)

echo "[build-gate] bash installer suite"
bash scripts/test-installer.sh

echo "[build-gate] all stages passed"
