# Example Workflows

Common usage patterns and example conversations for the kicad-tools MCP server.

## Workflow 1: Design Review

Get a comprehensive analysis of your PCB design quality.

### Conversation Example

```
User: "Review my PCB design at ~/projects/audio-amp/audio-amp.kicad_pcb"

Claude: I'll analyze your PCB design for placement quality.

[Calls placement_analyze with pcb_path="~/projects/audio-amp/audio-amp.kicad_pcb"]

Here's your design review:

**Overall Score: 78/100**

| Category | Score | Status |
|----------|-------|--------|
| Wire Length | 85/100 | Good |
| Congestion | 72/100 | Needs attention |
| Thermal | 90/100 | Excellent |
| Signal Integrity | 75/100 | Fair |
| Manufacturing | 80/100 | Good |

**Issues Found:**

1. **Warning - Routing Congestion** near U1 (audio codec)
   - 12 vias in 5mm x 5mm area
   - Affected: U1, C3, C4, R12, R13
   - Suggestion: Spread decoupling capacitors around U1 perimeter

2. **Warning - Signal Integrity** on I2S bus
   - BCLK and LRCLK running parallel for 15mm at 0.15mm spacing
   - Crosstalk risk between digital audio signals
   - Suggestion: Increase spacing to 0.3mm or add ground traces between

3. **Suggestion - Wire Length** for analog section
   - Audio input traces longer than optimal
   - Consider moving J1 closer to U1

**Detected Clusters:**
- Power cluster around U2 (voltage regulator): Compactness 92%
- Audio codec cluster around U1: Compactness 78%

**Routability Estimate: 75%** - Some congestion areas may require manual intervention

Would you like me to help optimize any of these issues?
```

### When to Use

- Before sending a board for manufacturing
- After significant layout changes
- When troubleshooting routing difficulties
- For design reviews and quality checks

---

## Workflow 2: Manufacturing Preparation

Prepare a complete manufacturing package for PCB assembly services.

### Conversation Example

```
User: "Prepare my board for JLCPCB assembly. PCB is at ~/projects/board.kicad_pcb
and schematic at ~/projects/board.kicad_sch"

Claude: I'll generate a complete assembly package for JLCPCB.

[Calls export_assembly with:
  pcb_path="~/projects/board.kicad_pcb"
  schematic_path="~/projects/board.kicad_sch"
  output_dir="~/projects/board/manufacturing"
  manufacturer="jlcpcb"]

Manufacturing package generated successfully!

**Output:** ~/projects/board/manufacturing/board-jlcpcb-assembly.zip

**Package Contents:**

| File | Description |
|------|-------------|
| Gerbers (8 files) | 4-layer copper, masks, silk, outline |
| board_bom_jlcpcb.csv | Bill of materials (JLCPCB format) |
| board_cpl_jlcpcb.csv | Component placement list |

**Statistics:**
- Total components: 127
- Unique parts: 45
- SMD placements: 115 (top), 12 (bottom)

**Action Required:**
3 parts are missing LCSC part numbers:
- U3 (STM32F405RGT6) - Search LCSC for equivalent
- J2 (USB-C connector) - Consider C2814855
- SW1 (Tactile switch) - Consider C221880

The zip file is ready to upload directly to JLCPCB's assembly service.

Would you like me to help find LCSC alternatives for the missing parts?
```

### When to Use

- Ready to order PCBs with assembly
- Need manufacturer-specific file formats
- Want to identify missing part numbers before ordering

---

## Workflow 3: Interactive Placement Refinement

Iteratively improve component placement with AI assistance.

### Conversation Example

```
User: "Help me optimize the bypass capacitor placement for my microcontroller"

Claude: I'll start a placement session and analyze your bypass capacitors.

[Calls start_session with pcb_path="~/projects/board.kicad_pcb"]

Session started (ID: a1b2c3d4)
- 127 components loaded
- Initial placement score: 78.5

Let me analyze the bypass capacitor positions relative to U1 (your microcontroller).

[Calls placement_analyze]

Current bypass capacitor positions:
- C1 (100nF): 4.2mm from U1.VDD1 - Too far
- C2 (100nF): 2.1mm from U1.VDD2 - Acceptable
- C3 (10uF): 8.5mm from U1.VDD3 - Too far
- C4 (100nF): 1.8mm from U1.AVDD - Good

Let me query an improvement for C1:

[Calls query_move with ref="C1", x=45.2, y=32.1]

Moving C1 to (45.2, 32.1) would:
- Reduce distance to VDD1 from 4.2mm to 1.5mm
- Improve wire length score by 3 points
- No new violations created
- Estimated score improvement: +2.5 points

Shall I apply this move?

User: "Yes, apply it"

Claude: [Calls apply_move with ref="C1", x=45.2, y=32.1]

Applied! C1 is now at (45.2, 32.1)
- New placement score: 81.0 (+2.5)
- 1 pending move

Now let me check C3:

[Calls query_move with ref="C3", x=47.0, y=35.5]

Moving C3 to (47.0, 35.5) would:
- Reduce distance to VDD3 from 8.5mm to 2.0mm
- Improve thermal distribution
- Score improvement: +3.2 points

Should I apply this move?

User: "Yes"

Claude: [Calls apply_move with ref="C3", x=47.0, y=35.5]

Done! C3 moved.
- New placement score: 84.2 (+3.2)
- 2 pending moves

User: "Looks good, save the changes"

Claude: [Calls commit_session]

Changes committed to ~/projects/board.kicad_pcb

**Summary:**
- Moves applied: 2
- Initial score: 78.5
- Final score: 84.2
- Improvement: +5.7 points

Bypass capacitors are now optimally placed for your microcontroller.
```

### When to Use

- Optimizing specific component groups
- Learning about placement best practices
- Making incremental improvements with immediate feedback
- When you want to preview changes before applying

---

## Workflow 4: Clearance Analysis

Check spacing between components or nets.

### Conversation Example

```
User: "Check the clearance between my high-voltage section and the rest of the board"

Claude: I'll measure the clearances around your high-voltage components.

[Calls measure_clearance with:
  pcb_path="~/projects/board.kicad_pcb"
  item1="HV_IN"  # High voltage net
]

**Clearance Analysis for HV_IN:**

Minimum clearance to nearest element: **0.45mm** to GND trace

| From | To | Clearance | Layer | Status |
|------|-----|-----------|-------|--------|
| HV_IN trace | GND trace | 0.45mm | F.Cu | Warning |
| HV_IN trace | U1-12 | 0.82mm | F.Cu | OK |
| HV_IN via | C3-1 | 0.65mm | F.Cu | OK |
| D1-1 (HV_IN) | R5-2 | 1.20mm | F.Cu | OK |

**Design Rule Check:**
- Required clearance: 0.5mm (for >50V)
- Actual minimum: 0.45mm
- Status: **FAIL**

The HV_IN trace near coordinates (25.4, 18.2) is 0.05mm too close to the GND plane.

**Recommendation:**
Move the GND trace or adjust the HV_IN routing to achieve at least 0.5mm clearance for safety compliance.

Would you like me to suggest alternative routing?

User: "Check clearance between U1 and U2"

Claude: [Calls measure_clearance with item1="U1", item2="U2"]

**Clearance between U1 and U2:**

Minimum clearance: **0.32mm** (pad to pad)

| U1 Pin | U2 Pin | Clearance | Layer |
|--------|--------|-----------|-------|
| U1-24 | U2-1 | 0.32mm | F.Cu |
| U1-25 | U2-2 | 0.38mm | F.Cu |
| U1-23 | U2-3 | 0.55mm | F.Cu |

Design rule requirement: 0.15mm
Status: **PASS**

The ICs have adequate clearance for manufacturing.
```

### When to Use

- Verifying high-voltage isolation
- Checking component spacing before ordering
- Debugging DRC violations
- Ensuring manufacturing clearances

---

## Workflow 5: BOM Generation and Analysis

Generate and analyze bill of materials.

### Conversation Example

```
User: "Generate a BOM for my project and identify any issues"

Claude: I'll generate the BOM and analyze it for potential issues.

[Calls export_bom with:
  schematic_path="~/projects/board.kicad_sch"
  format="jlcpcb"
  group_by="value+footprint"]

**Bill of Materials Generated**

| Category | Count |
|----------|-------|
| Total components | 127 |
| Unique part types | 45 |
| Parts with LCSC # | 42 |
| Missing LCSC # | 3 |

**Component Summary:**

| Type | Quantity | Unique Values |
|------|----------|---------------|
| Resistors | 45 | 12 |
| Capacitors | 38 | 8 |
| ICs | 8 | 8 |
| Connectors | 12 | 6 |
| Inductors | 4 | 2 |
| Other | 20 | 9 |

**Parts Missing LCSC Numbers:**

1. **U3** - STM32F405RGT6 (LQFP-64)
   - Suggestion: C15742 (basic part, in stock)

2. **J2** - USB-C Receptacle
   - Suggestion: C2814855 (16-pin, mid-mount)

3. **Y1** - 8MHz Crystal (HC49)
   - Suggestion: C12674 (basic part)

**Cost Estimate (LCSC):**
- Parts: ~$12.50 per board
- Note: Some parts may have minimum order quantities

Would you like me to update the schematic with these LCSC part numbers?
```

### When to Use

- Preparing for PCB assembly orders
- Cost estimation
- Identifying missing part numbers
- Comparing with alternative suppliers

---

## Tips for Best Results

### Be Specific with File Paths
Use absolute paths or paths relative to your home directory:
```
Good: "~/projects/board.kicad_pcb"
Good: "/Users/name/projects/board.kicad_pcb"
Avoid: "board.kicad_pcb" (ambiguous)
```

### Provide Context
Help the AI understand your goals:
```
Good: "Review the power section of my board for thermal issues"
Good: "I'm designing for JLCPCB 4-layer, check if it meets their rules"
Less helpful: "Check my board"
```

### Use Sessions for Multiple Changes
When making several placement changes, use sessions to:
- Preview changes before applying
- Undo mistakes easily
- See cumulative score improvements
- Save all changes at once

### Combine Tools for Comprehensive Analysis
Ask for multiple analyses in one conversation:
```
"Review my board for placement quality, then check clearances around the
high-voltage section, and finally generate a BOM for JLCPCB"
```
