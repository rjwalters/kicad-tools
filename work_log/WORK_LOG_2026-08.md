# Work Log Archive — 2026-08

Sections moved byte-for-byte from [WORK_LOG.md](../WORK_LOG.md) by `loom-daemon work-docs archive`.

### 2026-08-06

- **Release**: v0.20.0 — development since v0.19.0: a targeted `kct route --complete` completion pass, route-time enforcement of high-voltage isolation across every routing engine, a general `.kct_waivers.json` waiver mechanism for `kct check`, and a broad correctness sweep (KiCad-10 net dialect, export manifests, LVS identity, diff-pair shadow constructor). Headline behavior change: `net-status` strict is now the default. Board-edge keepout is auto-resolved from manufacturer limits for in-process API callers (#4568), and a mypy version-drift guard + `post-worktree.sh` env sync closed the phantom-baseline-error trap (#4558). Shipped as a 13-PR train through a GitHub Actions outage (content-judge + local integration preview + operator-approved single-gate merges). Version bumped 0.19.0 → 0.20.0 via PR #4669; tag `v0.20.0` → `publish.yml` to PyPI. No breaking changes.

