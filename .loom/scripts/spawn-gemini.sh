#!/usr/bin/env bash
# spawn-gemini.sh - Tier-3 "generic passthrough" adapter instantiation for the
# Google Gemini CLI (https://github.com/google-gemini/gemini-cli).
#
# This is deliberately the THINNEST possible adapter: it pins the two facts
# `spawn-generic.sh` needs (the underlying binary name and its non-interactive
# prompt flag) and execs the shared template. It does NOT reimplement any of
# spawn-generic.sh's logic — see that file's header for the full contract
# (points 1 and 3 only) and for everything a tier-3 adapter deliberately does
# NOT implement.
#
# Gemini is unverified by Loom: no guardrail-parity document, no CI smoke leg,
# no sandbox mapping. Its capability manifest (`defaults/runtimes/gemini.json`)
# declares every capability "no", including `worktreeIsolation: "no"`
# EXPLICITLY, so `check-runtime-capabilities.sh` refuses Builder and Doctor by
# construction (see runtime-adapters.md's tier-3 section). It is admitted only
# for roles with no `runtimeRequirements` (Curator, Guide, Auditor today) —
# where it is intended to serve as the #8436 preference-list fall-through tap
# when the Claude token pool is exhausted.
#
# Unattended pins (the same category of decision as spawn-aider.sh's
# `--yes-always` — exactly the gap the capability manifest records as
# unverified, not a sandbox feature):
#   --yolo          auto-accept every tool call; headless dispatch has no
#                   operator to answer gemini's approval prompts.
#   --skip-trust    trust the dispatch worktree for this session; a fresh
#                   loom worktree has never been trusted and headless mode
#                   has no TTY to answer the prompt.
#
# Model mapping is deliberately NOT pinned: Loom's logical tiers (`sonnet`,
# `opus`, …) are Claude names gemini does not understand, so
# LOOM_GENERIC_MODEL_FLAG stays unset and `LOOM_MODEL` is dropped (the
# contract's "omit, no error" facet). gemini runs on its own default model;
# an operator who needs a specific one passes `--model` through as a
# passthrough arg.
#
# Usage:
#   .loom/scripts/spawn-gemini.sh -p "your prompt"
#   LOOM_RUNTIME=gemini .loom/scripts/spawn-worker.sh -p "your prompt"

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOOM_GENERIC_RUNTIME_NAME="gemini" \
LOOM_GENERIC_CLI_BIN="${LOOM_GENERIC_CLI_BIN:-gemini}" \
LOOM_GENERIC_PROMPT_FLAG="${LOOM_GENERIC_PROMPT_FLAG:---prompt}" \
    exec "${_SCRIPT_DIR}/spawn-generic.sh" --yolo --skip-trust "$@"
