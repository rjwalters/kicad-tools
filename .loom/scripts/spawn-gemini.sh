#!/usr/bin/env bash
# spawn-gemini.sh - Tier-3 "generic passthrough" adapter instantiation for the
# Google Gemini CLI (https://github.com/google-gemini/gemini-cli).
#
# This is deliberately the THINNEST possible adapter: it pins the runtime
# name and execs the shared `spawn-generic-launch.sh`, which resolves
# `defaults/runtimes/gemini.json`'s "launch" object (binary name, prompt flag,
# the `--yolo --skip-trust` unattended pins) before exec'ing `spawn-generic.sh`
# (issues #8671, #13559). The pin rationale lives in that manifest's `note`.
#
# Gemini is unverified by Loom; its manifest declares every capability "no", so
# Builder, Doctor and Judge are refused by construction (runtime-adapters.md's
# tier-3 section). Like spawn-aider.sh, it needs a loom-daemon that supports
# `runtime-launch-env`; without one it exits 78 with remediation text.
#
# Usage:
#   .loom/scripts/spawn-gemini.sh -p "your prompt"
#   LOOM_RUNTIME=gemini .loom/scripts/spawn-worker.sh -p "your prompt"
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${_SCRIPT_DIR}/spawn-generic-launch.sh" gemini "$@"
