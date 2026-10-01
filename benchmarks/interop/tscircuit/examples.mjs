/**
 * Representative tscircuit designs for the interop gate (Issue #5847).
 *
 * These five designs are OUR source, not tscircuit's -- nothing third-party is
 * committed to this repo (see README.md). They are written with
 * `createElement` rather than JSX so the harness needs no transpiler or bundler:
 * plain `node` can run them.
 *
 * Selection follows the issue's own criteria: a trivial RC, an MCU board with a
 * QFN, and designs with copper pours / multiple net groups, so zone and
 * netclass handling is exercised and not just two-layer traces.
 */

import { createElement as h } from "@tscircuit/core";

/** Trivial RC low-pass: two 0402 passives behind a 3-pin header, two layers. */
function rcLowpass() {
  return h(
    "board",
    { width: "24mm", height: "18mm", autorouter: "auto" },
    h("pinheader", {
      name: "J1",
      pinCount: 3,
      footprint: "pinrow3",
      pcbX: -7,
      pcbY: 0,
    }),
    h("resistor", {
      name: "R1",
      resistance: "10k",
      footprint: "0402",
      pcbX: 0,
      pcbY: 3,
    }),
    h("capacitor", {
      name: "C1",
      capacitance: "100nF",
      footprint: "0402",
      pcbX: 5,
      pcbY: -2,
    }),
    h("trace", { from: ".J1 > .pin1", to: ".R1 > .pin1" }),
    h("trace", { from: ".R1 > .pin2", to: ".C1 > .pin1" }),
    h("trace", { from: ".R1 > .pin2", to: ".J1 > .pin2" }),
    h("trace", { from: ".C1 > .pin2", to: ".J1 > .pin3" }),
  );
}

/** LED indicator fed from a 2-pin header, with named power/ground nets. */
function ledIndicator() {
  return h(
    "board",
    { width: "22mm", height: "16mm", autorouter: "auto" },
    h("net", { name: "VCC" }),
    h("net", { name: "GND" }),
    h("pinheader", {
      name: "J1",
      pinCount: 2,
      footprint: "pinrow2",
      pcbX: -8,
      pcbY: 0,
    }),
    h("resistor", {
      name: "R1",
      resistance: "330",
      footprint: "0603",
      pcbX: 0,
      pcbY: 3,
    }),
    h("led", { name: "LED1", footprint: "0603", pcbX: 6, pcbY: 3 }),
    h("trace", { from: ".J1 > .pin1", to: "net.VCC" }),
    h("trace", { from: ".J1 > .pin2", to: "net.GND" }),
    h("trace", { from: "net.VCC", to: ".R1 > .pin1" }),
    h("trace", { from: ".R1 > .pin2", to: ".LED1 > .anode" }),
    h("trace", { from: ".LED1 > .cathode", to: "net.GND" }),
  );
}

/**
 * MCU-style board: a 32-pin QFN with four decoupling caps and a pin header,
 * i.e. the "MCU board with a QFN/QFP" case the issue asks for.
 */
function qfnMcu() {
  const decouplers = [
    { name: "C1", x: -6, y: 6 },
    { name: "C2", x: 6, y: 6 },
    { name: "C3", x: -6, y: -6 },
    { name: "C4", x: 6, y: -6 },
  ];
  return h(
    "board",
    { width: "30mm", height: "30mm", autorouter: "auto" },
    h("net", { name: "VDD" }),
    h("net", { name: "GND" }),
    // NOTE: the bare `qfn32` footprinter form is used deliberately. The
    // explicit-dimension form (`qfn32_w5_h5_p0.5mm`) emits corner pads that
    // overlap at 0mm clearance -- an upstream footprinter defect reported
    // separately, with its reproducer in docs/research/tscircuit-evaluation.md.
    h("chip", {
      name: "U1",
      footprint: "qfn32",
      pcbX: 0,
      pcbY: 0,
      pinLabels: { 1: "VDD", 2: "GND", 3: "IO0", 4: "IO1", 5: "IO2", 6: "IO3" },
    }),
    ...decouplers.map((d) =>
      h("capacitor", {
        name: d.name,
        capacitance: "100nF",
        footprint: "0402",
        pcbX: d.x,
        pcbY: d.y,
      }),
    ),
    h("pinheader", {
      name: "J1",
      pinCount: 4,
      footprint: "pinrow4",
      pcbX: 0,
      pcbY: -12,
    }),
    h("trace", { from: ".U1 > .VDD", to: "net.VDD" }),
    h("trace", { from: ".U1 > .GND", to: "net.GND" }),
    ...decouplers.flatMap((d) => [
      h("trace", { from: `.${d.name} > .pin1`, to: "net.VDD" }),
      h("trace", { from: `.${d.name} > .pin2`, to: "net.GND" }),
    ]),
    h("trace", { from: ".U1 > .IO0", to: ".J1 > .pin1" }),
    h("trace", { from: ".U1 > .IO1", to: ".J1 > .pin2" }),
    h("trace", { from: ".U1 > .IO2", to: ".J1 > .pin3" }),
    h("trace", { from: ".U1 > .IO3", to: ".J1 > .pin4" }),
  );
}

/**
 * Copper pours on both outer layers plus a keepout: the edge case the issue's
 * test plan calls for ("an example with copper pours and multiple netclasses").
 */
function pouredPlanes() {
  return h(
    "board",
    { width: "26mm", height: "20mm", autorouter: "auto" },
    h("net", { name: "GND" }),
    h("net", { name: "V3P3" }),
    h("resistor", {
      name: "R1",
      resistance: "1k",
      footprint: "0603",
      pcbX: -6,
      pcbY: 4,
    }),
    h("capacitor", {
      name: "C1",
      capacitance: "10uF",
      footprint: "0805",
      pcbX: 6,
      pcbY: 4,
    }),
    h("capacitor", {
      name: "C2",
      capacitance: "1uF",
      footprint: "0603",
      pcbX: 0,
      pcbY: -5,
    }),
    h("trace", { from: ".R1 > .pin1", to: "net.V3P3" }),
    h("trace", { from: ".R1 > .pin2", to: ".C1 > .pin1" }),
    h("trace", { from: ".C1 > .pin2", to: "net.GND" }),
    h("trace", { from: ".C2 > .pin1", to: "net.V3P3" }),
    h("trace", { from: ".C2 > .pin2", to: "net.GND" }),
    h("copperpour", { layer: "bottom", connectsTo: "net.GND" }),
    h("copperpour", { layer: "top", connectsTo: "net.V3P3" }),
    h("keepout", {
      shape: "rect",
      pcbX: -10,
      pcbY: -7,
      width: "4mm",
      height: "4mm",
    }),
  );
}

/** SOIC-8 op-amp with feedback passives: a leaded (non-QFN) package case. */
function soicOpamp() {
  return h(
    "board",
    { width: "24mm", height: "18mm", autorouter: "auto" },
    h("net", { name: "GND" }),
    h("net", { name: "VS" }),
    h("chip", {
      name: "U1",
      footprint: "soic8",
      pcbX: 0,
      pcbY: 0,
      pinLabels: {
        1: "OUT",
        2: "INN",
        3: "INP",
        4: "VEE",
        8: "VCC",
      },
    }),
    h("resistor", {
      name: "R1",
      resistance: "100k",
      footprint: "0603",
      pcbX: 0,
      pcbY: 6,
    }),
    h("resistor", {
      name: "R2",
      resistance: "10k",
      footprint: "0603",
      pcbX: -7,
      pcbY: 6,
    }),
    h("capacitor", {
      name: "C1",
      capacitance: "100nF",
      footprint: "0402",
      pcbX: 7,
      pcbY: -5,
    }),
    h("trace", { from: ".U1 > .VCC", to: "net.VS" }),
    h("trace", { from: ".U1 > .VEE", to: "net.GND" }),
    h("trace", { from: ".C1 > .pin1", to: "net.VS" }),
    h("trace", { from: ".C1 > .pin2", to: "net.GND" }),
    h("trace", { from: ".U1 > .OUT", to: ".R1 > .pin2" }),
    h("trace", { from: ".R1 > .pin1", to: ".U1 > .INN" }),
    h("trace", { from: ".R2 > .pin2", to: ".U1 > .INN" }),
    h("trace", { from: ".R2 > .pin1", to: "net.GND" }),
  );
}

/**
 * Ordered so the simplest design is measured first: when the emitter breaks, it
 * is useful to know whether even the trivial case fails.
 */
export const EXAMPLES = [
  { slug: "rc_lowpass", title: "Trivial RC low-pass (2 passives)", build: rcLowpass },
  { slug: "led_indicator", title: "LED indicator with named power nets", build: ledIndicator },
  { slug: "qfn_mcu", title: "QFN-32 MCU, 4 decouplers, pin header", build: qfnMcu },
  { slug: "poured_planes", title: "Copper pours on both layers + keepout", build: pouredPlanes },
  { slug: "soic_opamp", title: "SOIC-8 op-amp with feedback network", build: soicOpamp },
];
