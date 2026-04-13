#!/usr/bin/env bash
# Loom Label Cleanup - DEPRECATED
#
# This script has been deprecated per project policy (issue #2838):
#   Labels on closed/merged items are harmless — all agents filter by open state.
#   Skipping post-close label removal saves gh API calls.
#
# See: https://github.com/rjwalters/loom/issues/2838

echo "clean-labels.sh is deprecated."
echo "Labels on closed issues are intentionally not cleaned up."
echo "All Loom agents filter by open state, so stale labels are harmless."
echo "See: https://github.com/rjwalters/loom/issues/2838"
exit 0
