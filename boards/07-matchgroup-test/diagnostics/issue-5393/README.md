# Board07 DQ3 investigation: retained evidence

Part of #5393 and #3438. This report records a failed full-board control,
failed DDR-only control, and bounded diagnostic comparisons. It does not
qualify Board07 or restore its CI gates.

## Result and next investigation

DQ3 first fails during initial negotiated search, before later recipe
postprocessing. The corrected `probe-none-v3/probe.json` capture retains all
120 fixed escape routes and records 1,000,000 expansions in approximately
4.00 seconds (`3.998611378017813`), with zero returned routes. The earlier
3.73-second v2 capture omitted those escape routes and is invalid for copper
comparison, as recorded in `INVALID-CAPTURES.md`. The unchanged initial
routing completes 30/31 signal nets. DDR-only routing independently completes 10/11, with
DQ3 open in both saved and independently refilled native connectivity.

Removing only the already-routed DM0 or DQS_N channel from the initial
search prefix permits a native-connected DQ3 route. Removing DQ4 or DQS_P
does not. Putting DQ3 before DM0 transfers the open to DM0. Ordering alone
is therefore not a demonstrated solution.

The corrected append-only DQS_N restoration preserves DQ3 connectivity
without a native short/clearance/hole finding. That is evidence against
claiming that this prefix has no geometrically legal DQ3 path. It does
not distinguish grid representation from search cost or expansion order.
The DM0 restoration introduces a hole-to-hole finding and cannot be used
as a legal passing control.

**One next investigation:** replay the retained DQS_N-restored DQ3 path
against the unchanged failing search grid and cost state. Identify its
first rejected transition, or show every transition is accepted and
localize why the expansion budget fails to reach it. Only then implement
the demonstrated grid/search defect. Keep the failing prefix, legal
restoration, illegal DM0 restoration, and DQ4/DQS_P negative controls.
Do not infer a corridor-reservation or bundle-ordering fix from these data.

## Provenance and comparison limits

- Source: `a6c016e2c3fa32f4db5b5f3f2334b02dde2a1db0`.
  All 3,777 source files were hash-verified; `audit-result.json` records no
  source mismatch after the full recipe.
- Historical output `3ff7fbd9156cb01b5f0e842d553035e8cece46fc0465f5c0e2604246771462d3`
  and its original runtime image were unavailable. This is a **new
  exact-source control**, not recovered historical output.
- Original image: `sha256:96b2e23399bfd0dce7e52973f072e99a8efe46b9ac8a9a2c727e5bae6b4cd738`.
  Replacement image: `sha256:0d6887c861dd9926a02cdb57e3d649b72fc547ff2aef6605c5416131794d2115`.
  Native KiCad 10.0.5, Python 3.12.14; all three compiled extension hashes
  are in `preflight.json`. Runtime equality with the historical run is not claimed.
- Worker1, 4 CPU and 12 GiB container limits, seed/PYTHONHASHSEED 42;
  search 600 seconds, two placement probes of 600 seconds, total 2,400
  seconds. Host observations are retained in both monitor logs. Those
  samples are observations, not proof of zero contention between samples.
- DDR-only uses the same source and input geometry; its explicit non-DDR
  skip list and full argv are in `ddr-only/invocation.json`. Input hash:
  `a9089c137e5932e5c02bce0beb607bc9cd25ed292d175378ed38395d34767234`.
- Historical #3434 is not a matched performance/causal control: different
  revisions (82d1bc7d/3dc88588), a 600-second total budget with skipped DRC,
  an older 28/31 baseline and a contended host. This run cannot attribute
  its difference from that result to one router change.

## Measurements

| Control | Measured result | Scope |
|---|---|---|
| Unmodified full recipe | Exit 1; 30/31; DQ3 alone open; 16 routing-quality errors | Failed full board |
| Initial routing, all signals | DQ3 open | Captured before later postprocessing |
| Remove DM0 channel | DQ3 connected | Incomplete prefix, removed foreign channel |
| Remove DQS_N channel | DQ3 connected | Incomplete prefix, removed foreign channel |
| Remove DQ4 or DQS_P channel | DQ3 still open | Negative controls |
| DQ3 before DM0 | DM0 becomes open | No full-bus improvement |
| Corrected DM0 restoration | DQ3 connected; hole-to-hole finding | Illegal passing path control |
| Corrected DQS_N restoration | DQ3 connected; no short/clearance/hole finding | Legal prefix path, not complete board |
| DDR-only final | Exit 2; 10/11 DDR connected, DQ3 open | Both saved and refilled |

Full-board saved hash:
`9f8c6d12847e29997445590153c68e5b68eca639226c1c503ccb8bf04ca65588`.
Its independently refilled copy is
`842914dedd6931fdc975aa29f3464bf7661f32f604f621583bc1a3cdb60611d4`.
The full-board native reports and physical component records are retained,
including the other footprint-library, silk/text and dangling-via findings.
Native CLI exit 0 means the check executed, not that the board passed.

DDR-only saved hash:
`2eeb258cecbe9727a5a3e11ea7d398f21b89d5f8a912d00b6be47c62f626b905`;
refilled hash:
`3bee6f86fec7c1d9a1d345282f80a45ae6c434d4d5531eb29fdf97fcfbc2bdbd`.
Each has 244 physical pads. The complete open-net lists, including skipped
non-DDR signals and power nets, appear in `final-control-summary.json`.
**All three final diagnostic controls change overall components on refill.**
DQ3's result is stable; full saved/refilled partition equality is false.

## Evidence map and reproduction

`evidence.tar.gz` retains 311 files (about 9.6 MB compressed). Verify it
with `python3 verify_evidence.py`; this reads members without extraction.
Then unpack into a fresh directory. The original scripts use `/evidence`
as the container mount and `/tmp/5393-baseline-recovery` on the host.

- `source-manifest.json`, `board07-input-inventory.json`, `preflight.json`:
  source, inputs, native builds and runtime identity.
- `recipe.log`, `recipe.started`, `recipe.finished`, `recipe.exit` and
  `new-exact-source-control/`: unmodified control and full outputs.
- `observed-snapshots/`: polled immutable copper captures with their index.
  Polling is not exhaustive capture of every mutation; do not infer that
  two unseen adjacent stages are identical.
- `probe_initial_dq3.py`, `run_prefix_controls.py`, `run_order_controls.py`,
  `probe-*/`, prefix and ordering summaries: actual initial-search controls.
- `run_ddr_only.py`, `ddr-only/`: exact DDR-only command, outputs and exit.
- `native_components.py`, audit scripts and `final-control-audit/`:
  native reports, per-pad component partitions and context-complete refill copies.
- `INVALID-CAPTURES.md`: failed input recovery, the v2 snapshot that omitted
  120 escape routes, and the first restoration helper that erased copper.
  These invalid attempts are retained for audit and must not support a
  connectivity or legality claim. Corrected v3 and append-only v2 captures
  are explicitly distinguished.

The archive excludes the recoverable source tree/build outputs, duplicate
source tar, manufacturing exports and full container/host inspection dumps.
`audit-result.json` retains hashes of excluded manufacturing artifacts;
those entries are an inventory, not a claim they are archive members.
The authoritative archive contents are `evidence-manifest.json`.

## Remaining acceptance

The archive makes the completed measurements durable. #5393 still needs
review of the capture coverage and the bounded grid/cost replay above.
No production fix is included. #3438 retains 31/31 production, 11/11 DDR
and full physical/routing-quality qualification. #5286, #5333 and #5164
retain their integration and staged-refill requirements.

## Stage-boundary supplement (2026-09-15)

The [stage-boundary report](stage-capture/README.md) adds the missing exact
postprocessing and file-finishing captures, checks capture neutrality against
this retained control, and incorporates the newer real-geometry predicate
evidence. It supersedes the unresolved next-investigation recommendation
above with the single demonstrated #5410 mechanism; this original report and
archive remain unchanged historical measurements. Parent #3438 retains its
full production and DDR qualification requirements.
