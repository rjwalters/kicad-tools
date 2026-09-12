#!/usr/bin/env bash
# Shared by CI lint and local-gate.sh; no native build or board routing.
# When adding a Python consumer of excluded content, extend this selection
# or restore its full-Test trigger. See docs/contributing/development.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec uv run --extra dev --frozen pytest -q -o addopts= --no-cov --timeout=60 \
  tests/test_content_contract_gate.py \
  tests/test_docs_source_citations.py \
  tests/test_diffpair_docs.py \
  tests/test_match_group_docs.py \
  tests/test_kct_skills_consumer_generic.py \
  tests/install/test_install_kct.py \
  tests/test_check_doc_drift.py \
  tests/test_board_07_matchgroup_test.py::TestBoardsReadmeUpdated
