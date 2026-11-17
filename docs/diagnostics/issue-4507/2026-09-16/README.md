# Retained planner-run attribution (2026-09-16)

Part of #4507; not convergence or fabrication approval. These JSON files are
verbatim copies of retained local evidence. `SHA256SUMS.json` binds the shipped
files. Paths inside the records identify the original local-only artifacts;
the PCB, voltage maps and full routing logs are intentionally not redistributed.

- `producer.json`: composite source identities and producer environment.
- `acceptance-audit.json`: stage-2/3 pairwise, creepage and connectivity results,
  including nonidentical input/output sidecars.
- `stage-origin.json`: all 13 governing witnesses, exact conductor identities,
  prior copper overlap fractions, input/stage hashes and limitations.
- `policy-attribution.json`: per-witness matrix, threshold and attachment-zone
  attribution using the original voltage map.
- `no-attach.json`: replay with only attachment exemptions disabled.
- `native-refill.json`: exact native command, tool version, hashes and findings.
- `source-verification.json`: checked analysis files versus pinned Git blobs.
- `stage2-process.json`: original completion command and terminal exit.

Local reproductions and full reports remain in
`.loom/sweep-checkpoint/evidence/issue-4507/` under `planner-full/`,
`creepage-stage-origin/`, `independent-creepage-attribution/` and
`policy-attribution-sweep-merge/`. The last three contain the geometry/attribution
scripts; run them with the pinned source and retained local inputs, not a later
installed package. Their output files bind the exact board and voltage-map bytes.

Public provenance: [stage origins](https://github.com/rjwalters/kicad-tools/issues/4507#issuecomment-5691290962)
and [independent policy attribution](https://github.com/rjwalters/kicad-tools/issues/4507#issuecomment-5691294807).
