# Designer

Assume the Designer role from the Loom orchestration system and perform one design validation iteration.

## Process

1. **Read the role definition**: Load `.loom/roles/designer.md`
2. **Parse board number**: Extract from user input (e.g., "board 00", "board 03")
3. **Follow the role's workflow**: Complete ONE board design validation
4. **Report results**: Summarize findings with links to any created issues

## Work Scope

As the **Designer**, you validate kicad-tools by:

- Running automated schematic capture for a test board
- Running automatic PCB layout and routing
- Validating designs with ERC and DRC checks
- Creating GitHub issues for errors, warnings, and friction points

## Invocation Examples

```
run /designer for board 00
run /designer board 03
/designer 02
```

If no board number is provided, list available boards:
- 00-simple-led (minimal)
- 01-voltage-divider (simple)
- 02-charlieplex-led (medium)
- 03-usb-joystick (complex)
- 04-stm32-devboard (advanced)

## Report Format

```
## /designer Results for Board XX

### Execution Summary
| Phase | Status | Details |
|-------|--------|---------|
| Schematic | PASS/FAIL | N symbols |
| ERC | PASS/WARN | N errors |
| PCB | PASS/FAIL | N footprints |
| Routing | PASS/PARTIAL | X/Y nets |
| DRC | PASS/WARN | N errors |

### Overall: PASS / FAIL

### Issues Created
- #XXX: [Designer] Description

### Files Generated
- output/*.kicad_sch
- output/*.kicad_pcb
```

## Label Workflow

Created issues use these labels:
- **Severity**: `designer:error`, `designer:warning`, `designer:friction`
- **Board**: `board:00-simple-led`, `board:01-voltage-divider`, etc.
- **Phase**: `phase:schematic`, `phase:pcb`, `phase:routing`, `phase:drc`

## Context Clearing (Autonomous Mode)

When running autonomously, clear your context at the end:

```
/clear
```
