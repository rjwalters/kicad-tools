# Pinned-main #4507 qualification, 2026-09-16

This package records the terminal original-recipe run on `robb-pro` at source
`9dfbbdb59cc89ef490274d22cd7d862a0ca9e7b0`. It is not a passing qualification.
See the [dated proof section](../../../hv-pairwise-softstart-proof.md#2026-09-16-pinned-main-original-recipe-and-terminal-qualification).

`source-pin.json` binds the native27 import to its binary hash. The extension was
built at `6c20e7f3` and reused only because the C++ tree is identical at the pinned
source; both required and imported versions were verified. The extracted source
contained all 3996 regular archive files. Five non-source archive symlinks to
external/historical fixtures were omitted; this is not a complete symlink clone.
`producer.json` retains the original producer receipt; `source-pin.json` supplies
the more precise build/reuse provenance.

`recipe-processes.json` records exact commands, times, PIDs and terminal exits.
`input-hashes.json` covers eleven staged files: the nine original fixture files,
the provenance manifest and retained DRU. They are unchanged. The local-only
PCB/schematic fixtures are not copied into this package.

`acceptance-audit-summary.json`, `connectivity-residuals.json` and
`completion-report.json` preserve distinct audit results. `native-audits.json`
contains all four saved/refilled native receipts and original/copy hashes.
Generated project/DRU sidecars differ from the input contexts, as explicitly
recorded; zero native errors is not an assertion of original-rule equivalence.

`census-attribution.json` contains all fifteen final residuals and closest
primitive-pair ties. `attribute-census.py.txt` preserves the exact read-only script as source text used on
the retained worker artifacts and pinned source. It asserts copper unions equal
the census geometry and preserves the board hash. Its absolute paths identify
the diagnostic workspace; running it elsewhere requires the same retained inputs
and source. This is policy attribution, not independent standards certification.

`retained-artifact-hashes.json` binds 119 retained files under
`.loom/sweep-checkpoint/evidence/issue-4507/current-main-20260916/terminal/`.
`SHA256SUMS.json` binds this committed package. Raw boards and full census/native
reports remain in that evidence workspace and the worker directory
`/private/tmp/4507-current-main-20260916`.

All original acceptance criteria remain outstanding unless individually proven;
this evidence does not close #4507 or approve the DO_NOT_FAB fixture.
