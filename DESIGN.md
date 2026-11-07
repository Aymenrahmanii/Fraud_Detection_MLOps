---
name: AIOps Control Room
description: Operator console for the six-agent DevOps / MLOps platform, set as a Swiss operating manual.
colors:
  paper: "#f4f5f3"
  paper-frame: "#e8eae6"
  paper-sheet: "#ffffff"
  ink: "#121314"
  ink-secondary: "#44484c"
  ink-tertiary: "#61666a"
  rule: "#c9cdc8"
  rule-strong: "#a9aea8"
  signal: "#d52b1e"
  signal-ink: "#b5231a"
  signal-wash: "#fbebe9"
  verified: "#1d6b40"
  verified-wash: "#e6f0ea"
  ink-reversed-muted: "#c4c8c3"
typography:
  display:
    fontFamily: "Schibsted Grotesk, Helvetica Neue, Helvetica, Arial, sans-serif"
    fontSize: "2rem"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Schibsted Grotesk, Helvetica Neue, Helvetica, Arial, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.45
  body-strong:
    fontFamily: "Schibsted Grotesk, Helvetica Neue, Helvetica, Arial, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 700
    lineHeight: 1.45
  label:
    fontFamily: "Schibsted Grotesk, Helvetica Neue, Helvetica, Arial, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "0.06em"
  section-title:
    fontFamily: "Schibsted Grotesk, Helvetica Neue, Helvetica, Arial, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 800
    lineHeight: 1.45
    letterSpacing: "0.08em"
  data:
    fontFamily: "Schibsted Grotesk, Helvetica Neue, Helvetica, Arial, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.45
  mono:
    fontFamily: "ui-monospace, Cascadia Mono, SF Mono, Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
rounded:
  none: "0px"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "24px"
  s-6: "32px"
  s-7: "48px"
components:
  button:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.body-strong}"
    rounded: "{rounded.none}"
    padding: "8px 16px"
  button-hover:
    backgroundColor: "{colors.paper-frame}"
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.body-strong}"
    rounded: "{rounded.none}"
    padding: "8px 16px"
  button-primary-hover:
    backgroundColor: "#33373a"
  button-small:
    typography: "{typography.label}"
    padding: "4px 12px"
  segmented-option:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "6px 12px"
  segmented-option-selected:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
  input:
    backgroundColor: "{colors.paper-sheet}"
    textColor: "{colors.ink}"
    typography: "{typography.data}"
    rounded: "{rounded.none}"
    padding: "6px 12px"
  title-band:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    height: "52px"
    padding: "0 24px"
  loop-strip:
    backgroundColor: "{colors.paper-frame}"
    textColor: "{colors.ink}"
    height: "82px"
  loop-stage-hot:
    backgroundColor: "{colors.signal}"
    textColor: "#ffffff"
  register-row-selected:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
  sheet:
    backgroundColor: "{colors.paper-sheet}"
    textColor: "{colors.ink}"
    padding: "24px"
  decision-band:
    backgroundColor: "{colors.signal}"
    textColor: "#ffffff"
    padding: "12px 24px"
  decision-body:
    backgroundColor: "{colors.signal-wash}"
    textColor: "{colors.ink}"
  outcome-verified:
    backgroundColor: "{colors.verified}"
    textColor: "#ffffff"
    padding: "16px 24px"
  rail-cell:
    size: "14px"
    rounded: "{rounded.none}"
---

# Design System: AIOps Control Room

## Overview

**Creative North Star: "The Swiss Operating Manual"**

The closed loop is the layout. Every workflow is a numbered process set on one rigorous 12-column grid, so the viewer reads a record's position in the loop (01 Detect, 02 Diagnose, 03 Decide, 04 Approve, 05 Execute, 06 Verify, 07 Close) before reading the record itself. The world is the International Typographic Style turned into an operating document: cool paper, two inks, one grotesque, hairline rules, and black bands with reversed type standing in for every heavier device a dashboard would normally reach for.

Density is high but calm. Information is ranked by weight, case and reversal rather than by size or colour, so the screen stays quiet until something needs a person. When it does, signal red appears, and it appears only for that, or for failure. Green appears only when recovery has been verified. Everything else is ink on paper.

The system refuses the category default of a dark sidebar, KPI cards and status pills scattered over tables. Depth comes from paper layers and ink rules, never from shadow; shapes are square, never rounded.

**Key Characteristics:**
- Cool paper ground, near-black ink, a second paper layer for the frame and a white sheet for the record.
- Signal red means "a human must decide" or failure; green means verified; nothing else is coloured.
- One self-hosted grotesque at exactly three sizes (13 / 15 / 32px), ranked by weight, case and reversal.
- 1px hairlines are the only dividers; ink rules mark structure, grey rules mark rows.
- The 7-cell stage rail is the signature: on every register row, enlarged, numbered and timed at the head of the sheet.

## Colors

Two inks on a cool paper ground, with one signal red and one quiet green carrying all meaning.

### Primary
- **Signal Red** (signal): fills for the waiting or failed stage cell, the hot loop stage, the decision band, the escalated outcome, and the decision block's top, bottom and footer rules. It always means a human must decide, or something failed.
- **Signal Ink** (signal-ink): the red used for text on paper (escalated totals, signal status text, field errors, failed chain verdict), darkened for 5.9:1 contrast.
- **Signal Wash** (signal-wash): the ground of the decision block and its sticky footer, so the whole decision reads as one red-ruled zone.

### Secondary
- **Verified Green** (verified): the verified rail cell, the verified outcome band, resolved totals, ok status text and the verified audit chain.
- **Verified Wash** (verified-wash): secondary text reversed out of a green stage cell.

### Neutral
- **Cool Paper** (paper): page ground and reversed text.
- **Frame Paper** (paper-frame): the loop strip, button and row hover, bar tracks, the skeleton.
- **Sheet White** (paper-sheet): the selected workflow's sheet, inputs and the patch box.
- **Ink** (ink): text, structural rules, primary buttons, selected rows, the title band, completed rail cells, focus rings.
- **Ink Secondary** (ink-secondary): column heads, stage names, secondary copy (8.6:1 on paper).
- **Ink Tertiary** (ink-tertiary): captions, timestamps, placeholders, definition terms (5.5:1 on paper).
- **Rule** (rule): row hairlines and cell dividers.
- **Rule Strong** (rule-strong): input borders, empty rail cells, scrollbar thumb, the struck-through "from" value.
- **Reversed Muted** (ink-reversed-muted): secondary text on ink (band status, roles, selected-row sub-lines, completed rail-step labels).

### Named Rules
**The Human-Decision Red Rule.** Signal red is used for exactly two meanings: a human must decide, or something failed. Never use it for emphasis, branding, or "new".

**The Verified-Only Green Rule.** Green means recovery or integrity was verified. It is never a generic "success" or "online" decoration on paper.

**The Two Inks Rule.** Every other surface is ink on paper. Rank is made with weight and reversal, not with additional hues.

## Typography

**Display Font:** Schibsted Grotesk, self-hosted variable woff2 (400–900, OFL), with Helvetica Neue, Helvetica, Arial fallback
**Body Font:** Schibsted Grotesk
**Label/Mono Font:** ui-monospace, Cascadia Mono, SF Mono, Consolas (patches, hashes and IDs only)

**Character:** One sturdy, slightly condensed grotesque doing all the work; the hierarchy comes from 400 against 600, 700 and 800, from uppercase tracked labels, and from black reversal, not from a scale.

### Hierarchy
- **Display** (800, 32px, 1.05–1.1, -0.02em): loop stage counts and totals, the sheet title, the decision action, the sign-in heading.
- **Body** (400, 15px, 1.45): running copy and leads (max 62ch), controls, textarea and inputs at sign-in; 700 for register service names and buttons, 600 for meta values.
- **Section title** (800, 13px, uppercase, 0.08em): the register heading and sheet block headings, including the decision band's heading.
- **Label** (600, 13px, uppercase, 0.06em): table column heads, stage names, total labels, operator roles.
- **Data** (400–800, 13px): table cells, key/value lists, timeline, agents, bars, the patch, stage numbers (800, 0.04em).

### Named Rules
**The Three Sizes Rule.** Only 13px, 15px and 32px exist. A new level of hierarchy is made with weight, case or reversal, never with a fourth size.

**The Aligned Figures Rule.** Tabular figures apply only where numbers line up in a column or a fixed slot (priority and opened columns, numeric data cells, stage counts, totals, meta values, bar values, band status). Clock times, the countdown and left-aligned values stay proportional.

**The Mono for Machines Rule.** Monospace is reserved for patches, hashes, IDs, tokens and keys. Never for numbers, labels or decoration.

## Layout

One 12-column grid (32px column gap, 24px side gutter) is shared by the loop strip and the workspace. The register spans columns 1–7 and the sheet spans 8–12; in the loop strip the seven stages sit over columns 1–7 and the four totals over 8–12, divided by the same 1px ink rule that edges the sheet, so the strip is registered to the workspace below it.

At desktop (above 1180px) the 52px title band and the 82px loop strip are sticky, and the sheet is sticky below them, filling the remaining viewport height and scrolling on its own. Spacing steps are 4 / 8 / 12 / 16 / 24 / 32 / 48px; sheet blocks use 24px padding, table cells 12px.

At 1180px and below, the grid collapses to one column: stages and totals stack full-width, the sheet follows the register with an ink top rule, and nothing is sticky but the band. At 720px and below, the gutter drops to 16px, the stage strip wraps to 4 columns showing numbers only (the hot stage keeps its name), totals become 2-column label/value rows at body size, the register rail moves under the service name, and the decision grid, footer and change rows stack.

### Named Rules
**The Registered Strip Rule.** The loop strip and the workspace share one 12-column grid; stages align over the register, totals over the sheet, and the dividing ink rule continues through both.

## Elevation & Depth

The system is flat. There are no shadows anywhere. Depth is conveyed by three paper layers (cool paper ground, frame paper for the strip and table head, white for the sheet), by 1px ink rules that mark structural edges, and by reversal: the title band, the selected row and the pressed stage are solid ink with paper text. Sticky elements (band, strip, sheet, decision footer) separate by their own ground colour and rule, not by elevation.

### Named Rules
**The Hairline Only Rule.** 1px rules are the only dividers: ink for structure (strip bottom, sheet edge, table heads, register head), grey rule for rows and cells, signal for the decision block. No shadows, no thick borders, no card outlines.

## Shapes

Every corner is square (0px radius), including buttons, inputs, the select, segmented controls, rail cells and status dots. Form comes from the rectangle and the rule: 14px square rail cells, 8px square status dots, 6px square bullets in reason lists, a 14px square wordmark with a 6px red corner block. The "agents working" cell is marked with a 2px ink border and 135° hatching; the enlarged rail's active step carries a 3px ink top bar.

## Components

### Buttons
Square, ruled and plain.
- **Shape:** square corners (0px), 1px ink border.
- **Default:** transparent on paper, ink text, 15px/700, 8px 16px padding; hover fills with frame paper.
- **Primary:** solid ink with paper text; hover lightens to graphite (#33373a).
- **Small:** 13px, 4px 12px padding (scenario inject controls).
- **Disabled:** 45% opacity, not-allowed cursor. Transitions are 160ms on the shared ease-out curve.
- **Link button:** underlined text, no border, 13px; paper-coloured on the band.

### Segmented filter
A single 1px ink-bordered strip of 13px/600 options divided by ink rules. The checked option reverses to ink. The "Needs decision" option carries a solid red count block (white 800 type) only while decisions are pending.

### Inputs / Fields
- **Style:** 1px strong-rule border, white ground, square, 6px 12px padding at 13px (sign-in field and comment at 15px).
- **Hover / Focus:** border turns ink; focus adds a 2px ink outline.
- **Error:** signal-ink text, 600, below the field.

### Navigation (title band)
A 52px solid ink band with the bold wordmark, a live status line led by an 8px square (green when live, red when stale, grey otherwise), and operator name (700) with uppercase roles. Status and roles hide at 720px.

### Register
A borderless table at 13px: uppercase label column heads over an ink rule, rows on grey hairlines, frame-paper hover. The selected row reverses to ink and every other row recedes to secondary ink. Service names are 15px/700 with a tertiary sub-line carrying the mono ID.

### Stage Rail (signature)
Seven 14px square cells with 4px gaps, one per loop stage, identical on every register row. Empty: strong-rule outline. Done: solid ink. Now: 2px ink border with hatching. Waiting for a human: solid signal. Verified: solid green. Failed or escalated: solid signal. On a selected row the inks invert to paper. A legend under the register explains each cell.

In the sheet header the rail is enlarged to a 7-column numbered strip between an ink top rule and a grey bottom rule: each step shows its number (800), stage name and time, and fills with the same state colour, with reversed text. Each step is a tab (arrow keys, Home/End, Escape back to the overview) that opens its phase. The selected step carries a 2px inner frame: ink on paper cells, paper inset 5px on filled cells, so the state colour is never replaced.

### Phase View
Opened from the enlarged rail. An ink band repeats the phase number and name with Previous / Next / Overview. Below it: one sentence on what the phase does, a facts row (who, started to the millisecond, time in phase, record count), then one ruled output per agent result or decision, with no cards: a 15px/800 title with its event as a grey subtitle, a kv list of the typed output (hypotheses, patches and before/after states as tables, SLO checks as pass/fail tags), the evidence it cites, and a collapsed "Raw output" in a frame-paper mono box with a copy button. The phase ends with its hash-chained audit records; each opens to its full JSON. An open decision comes first in the Approve phase; while one is pending, every other phase carries a red one-line strip that leads to it.

### Loop Strip
The frame-paper strip of seven stage buttons (number, uppercase name, 32px count) and four totals. A pressed stage reverses to ink; the stage that holds a waiting decision fills signal red. Zero counts drop to 600 grey. Resolved totals are green, escalated totals signal-ink.

### Decision Block
The memorable moment. A signal-wash zone ruled in signal red top and bottom. It opens with a solid red band carrying the uppercase heading, the countdown (15px/800) and the permission line. Below: the action at display size with a body-size target, the exact from→to patch in a white ruled box (from struck through in secondary ink, to in 700 with an arrow icon), a one-line precondition with a green check, then reasons beside risk bars in a two-column grid. The footer is sticky at desktop: a one-row comment field with Approve and Reject, over a red rule on the same wash.

### Outcome Band
A full-width solid band with an icon and 600 text: green for verified, red for escalated, ink for other closures.

## Do's and Don'ts

### Do:
- **Do** keep every new surface on the shared 12-column grid and register any strip to it.
- **Do** rank with weight (400/600/700/800), uppercase tracked labels, and ink reversal before reaching for anything else.
- **Do** show a workflow's loop position with the 7-cell stage rail wherever a workflow is listed.
- **Do** use signal red only when a human must decide or something failed, and green only when it is verified.
- **Do** use tabular figures only for numbers aligned in columns or fixed slots.
- **Do** use inline stroked SVG icons at 16px (1.6 stroke, square caps) from the shared sprite.

### Don't:
- **Don't** add shadows, rounded corners, cards or pills; the system is flat, square and ruled.
- **Don't** introduce a fourth type size or a second display face.
- **Don't** use monospace for anything but patches, hashes, IDs, tokens and keys.
- **Don't** use red or green as decoration, emphasis or branding.
- **Don't** use a dark sidebar or KPI card grid; the loop strip carries the totals.
