# Semantic Search Over Sweep History (`loom-search`) — Retired

`loom-search` was an opt-in, off-by-default CLI (SQLite FTS5 + BM25 index,
plus an optional local ONNX embeddings layer) over past sweep summaries and
merged-PR history. It was the last Python in Loom — carved out of epic
#4081's Phase 4 `loom-tools` deletion (ADR-0013) pending a port-or-retire
decision.

**That decision was made: RETIRE.** Recorded by the operator on
[#4608](https://github.com/rjwalters/loom/issues/4608) (2026-07-31) and
implemented in [#4970](https://github.com/rjwalters/loom/issues/4970), which
deleted `loom-tools/` in full. `loom-search` had zero demonstrated usage —
never installed, no index, on any host including the primary operator host —
so there was nothing to port.

The feature's source (and its history — #4339, Tier B embeddings #4370) is
still available in git history at any commit before #4970 merged, under
`loom-tools/src/loom_tools/semantic_search.py` and `embedders.py`.

## Successor direction

If searchable fleet memory is wanted again, the recommended path is no
longer a local Python index — it's the fleet's own telemetry query surface,
which already durably records sweep outcomes and merged-PR history
server-side:

- [#4704](https://github.com/rjwalters/loom/issues/4704) — sweep outcome
  records
- [#4705](https://github.com/rjwalters/loom/issues/4705) — outcome exporter
- [#4726](https://github.com/rjwalters/loom/issues/4726) — query API + D1

A search-over-history feature built on that stack lives in the existing
Rust/Workers dashboard backend, not a re-introduced Python toolchain.

See [ADR-0013](https://github.com/rjwalters/loom/blob/main/docs/adr/0013-loom-tools-python-retirement.md)
for the full retirement history of `loom-tools/`.
