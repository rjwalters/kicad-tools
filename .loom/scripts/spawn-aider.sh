#!/usr/bin/env bash
# spawn-aider.sh - Tier-3 "generic passthrough" adapter instantiation for the
# Aider CLI (https://aider.chat), the worked example for issue #4780.
#
# This is deliberately the THINNEST possible adapter: it pins the two facts
# `spawn-generic.sh` needs (the underlying binary name and its non-interactive
# prompt flag) and execs the shared template. It does NOT reimplement any of
# spawn-generic.sh's logic — see that file's header for the full contract
# (points 1 and 3 only) and for everything a tier-3 adapter deliberately does
# NOT implement.
#
# Aider is unverified by Loom: no guardrail-parity document, no CI smoke leg,
# no sandbox mapping. Its capability manifest (`defaults/runtimes/aider.json`)
# declares every capability "no", including `worktreeIsolation: "no"`
# EXPLICITLY, so `check-runtime-capabilities.sh` refuses Builder and Doctor by
# construction (see runtime-adapters.md's tier-3 section). It is admitted only
# for roles with no `runtimeRequirements` (Curator, Guide, Auditor today).
#
# Usage:
#   .loom/scripts/spawn-aider.sh -p "your prompt"
#   LOOM_RUNTIME=aider .loom/scripts/spawn-worker.sh -p "your prompt"

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Aider's non-interactive headless mode is `aider --message "<prompt>" --yes-always`
# (see https://aider.chat/docs/scripting.html). `--yes-always` auto-confirms
# every prompt aider would otherwise ask interactively -- required for
# unattended dispatch, same rationale as spawn-codex.sh's sandbox convention,
# just with no sandbox to widen (aider has none; this is exactly the
# "unverified" gap the capability manifest records).
LOOM_GENERIC_RUNTIME_NAME="aider" \
LOOM_GENERIC_CLI_BIN="${LOOM_GENERIC_CLI_BIN:-aider}" \
LOOM_GENERIC_PROMPT_FLAG="${LOOM_GENERIC_PROMPT_FLAG:---message}" \
    exec "${_SCRIPT_DIR}/spawn-generic.sh" --yes-always "$@"
