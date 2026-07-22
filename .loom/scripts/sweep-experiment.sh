#!/bin/bash

# sweep-experiment.sh - Thin stub delegating to loom_tools.sweep_experiment (#3725)
#
# The /loom:sweep skill shells out to this for the deterministic parts of the
# model-cost experiment: tri-state mode resolution, per-issue arm assignment, the
# startup banner, the durable JSONL append, and the harvest reader. Keeping the
# arithmetic in Python (not in the LLM-executed markdown) makes arm assignment
# byte-for-byte deterministic and resume-safe.
#
# Usage:
#   sweep-experiment.sh resolve-mode
#   sweep-experiment.sh assign-arm --issue N [--complexity complex|routine] [--format json]
#   sweep-experiment.sh banner --issue N [--complexity ...]
#   sweep-experiment.sh record --issue N --phase P --role R [--model M --arm A ...]
#   sweep-experiment.sh harvest [--archive-dir DIR] [--format text|json]
#
# The harvest subcommand is ALSO reachable via `agent-metrics.sh --model-experiment`
# (issue #3725 AC), which forwards here so operators find it next to the existing
# `--by-model` (#3482) cost dimension.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "sweep-experiment" "sweep_experiment" "$@"
