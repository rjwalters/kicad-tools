# Softstart rev-C diagnostic evidence, 2026-09-15

This package records saved-board scoring and closest-geometry attribution from
source af6b301478ca1416f59f0cc7f111b1eb10e95456. The fixture is local-only,
work-in-progress and DO_NOT_FAB. No PCB, project, schematic, full input bundle or
manufacturing artifact is published here.

`captured-scripts.tar.gz` retains the exact geometry, sidecar and copper-comparison
diagnostic scripts, including their original scratch paths. To replay,
restore the local evidence under those paths and use the immutable producer
source on PYTHONPATH. They are provenance records, not installed CLI commands.
The unmodified full census reports remain in the local evidence collection;
`census-failure-excerpts.json` includes every non-waived failure and binds each
full report by SHA256. `census-attribution.json` contains all closest ties and
net/layer-scoped attach zones in consistent coordinate frames.

The producer completion stage failed; its completion report is missing. These
independent scores do not turn that failure into route completion or board-level
clearance/creepage acceptance. See the dated proof section for limits.
