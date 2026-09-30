#!/usr/bin/env bash
# spawn-aider.sh - Tier-3 "generic passthrough" adapter instantiation for the
# Aider CLI (https://aider.chat), the worked example for issue #4780.
#
# This is deliberately the THINNEST possible adapter: it pins the runtime
# name and execs the shared `spawn-generic-launch.sh`, which resolves
# `defaults/runtimes/aider.json`'s "launch" object (binary name, prompt flag,
# `--yes-always`) before exec'ing `spawn-generic.sh` itself (issue #8671).
# Aider's own launch shape now lives entirely in that manifest -- this file
# carries none of it, which is the point: onboarding the NEXT tier-3 CLI is a
# manifest edit, not a new script like this one.
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
exec "${_SCRIPT_DIR}/spawn-generic-launch.sh" aider "$@"
