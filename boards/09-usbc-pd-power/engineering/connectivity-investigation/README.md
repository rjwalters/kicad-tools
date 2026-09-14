# Board 09 connectivity disagreement

Diagnostic files are not manufacturing artifacts. `before-split.kicad_pcb`
is the fully routed board before explicit mid-track junction splitting.
`before-split-native-drc.json` records zero native violations and opens;
`before-split-copper-lvs.json` records five false opens on +3V3 and PMOS_SOURCE.
Splitting those segments at existing branch/pad centers changed the buffered
trace union by exactly 0.0 mm² on every net/layer (Shapely comparison).
The canonical `../../output/copper-lvs.json` passes with 153 bound pads.
Tracked in https://github.com/rjwalters/kicad-tools/issues/5060.

To rerun, copy the canonical output project, schematic, and local libraries
to a temporary directory; replace its PCB with this snapshot, keeping the
`usbc_pd_power` basename. Refill using `kicad-cli pcb drc --refill-zones
--save-board --format json --output drc.json usbc_pd_power.kicad_pcb`.
Use `compare_copper_netlist(schematic_path, pcb_path)` from
`kicad_tools.lvs.copper_lvs` for the physical comparison.

The canonical strict `output/net-status.json` separately reports four USB
GND pads unconnected despite native zero opens and two filled inner GND
planes. Earlier capacitor array contacts also triggered two false opens;
explicit pad-center stubs removed those. USB shield-annulus trace splitting
did not remove the remaining disagreement. Copper LVS suppresses plane opens;
it cannot be used as independent proof for these contacts. Tracked in
https://github.com/rjwalters/kicad-tools/issues/5061. Native DRC remains the
connectivity authority for this development checkpoint; the disagreement
is retained as a release blocker.
