# Support-net experiment — rejected, 2026-09-10

This diagnostic snapshot is not the board's canonical output or a release.
`result.json` binds the source PCB to its SHA-256. `command.json` records the
exact command with its temporary paths; substitute the current source and a
new experiment directory to reproduce. Copy the source project sidecar to
the output basename before running to avoid the known #5032 rule-loss issue.
Copy the local symbol/footprint libraries and library tables alongside it
when opening this snapshot or repeating native DRC.

The run selected 14 support nets, preserved existing copper and used strict
pad/in-pad clearance, a 0.1 mm grid and a 90 s routing budget. It finished in
102.8 s, reporting 10/14 nets connected and four partial. Native KiCad found
44 opens, a +3V3 via short to the existing VIN trunk and another +3V3-via/VIN
clearance violation (0.1407 mm versus 0.15 mm). All 129 existing trace
geometries and all 16 critical-net connections survived. These native defects
are additional evidence for #4991; lower open count is not an acceptance gate.

The run omitted `--strict-layers`, so advisory `avoid_layers` entries allowed
2,455 signal segments on the planned inner ground layers. This is not a claim
that the hard layer constraint failed: the next experiment must explicitly
enable it. Native shorting/clearance defects independently reject this result.

The canonical generated checkpoint remains at zero native geometry violations
and 68 opens. Ground planes, raw VBUS and support routing remain incomplete.
