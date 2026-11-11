# IncretinOS design system

For engineers building or changing the web application. It states the visual direction, the tokens, the shared
components and the rules they encode. The source of truth for values is `frontend/src/app/globals.css`; the
components are in `frontend/src/components/ui/`; this document explains what they are for.

Contents: [1 Direction](#1-direction) · [2 Principles](#2-principles) · [3 Colour](#3-colour) ·
[4 Typography](#4-typography) · [5 Spacing](#5-spacing) · [6 Dimensions](#6-dimensions) ·
[7 Border and radius](#7-border-and-radius) · [8 Elevation and layers](#8-elevation-and-layers) ·
[9 Interaction states](#9-interaction-states) · [10 Motion](#10-motion) · [11 Light and dark](#11-light-and-dark) ·
[12 Evidence-state language](#12-evidence-state-language) · [13 Components](#13-components) ·
[14 Data states and lists](#14-data-states-and-lists) · [15 Page structure](#15-page-structure) ·
[16 Accessibility](#16-accessibility) · [17 Skill application log](#17-skill-application-log) ·
[18 Screenshots](#18-screenshots)

---

## 1. Direction

**Design read.** A redesign that preserves an existing product: a dense clinical-evidence interface for research
scientists and reviewers, in a trust-first, calm and precise language, built on the stack already in the
repository (Tailwind v4, Radix primitives, lucide icons) with restrained CSS motion.

**Dials** (the vocabulary of the Taste skill, section 17): design variance 3, motion intensity 3, visual density 7.
Low variance and motion because the audience is checking numbers, not being persuaded. High density because the
data is the product.

**Identity.** IncretinOS should read like a well-kept dossier, not a dashboard:

- Cool neutral surfaces and a near-black ink with a slight blue cast. One accent, a registry blue, used only for
  interaction: links, the primary action, selection and focus. There is no second brand colour.
- Depth comes from a surface step and a hairline. Only layers that float (popover, drawer, modal, toast) cast a
  shadow.
- Sentence case everywhere. No tracked uppercase micro-labels above headings, stat tiles or table columns.
- Numbers are set in tabular figures wherever they are compared down a column, and every number carries its
  qualifiers: source tier, verification status, estimand.
- The one thing that is unique to this product is the **evidence-state language** (section 12): a small set of
  icons, labels and outline shapes that say how far a value has been checked and what kind of statement it is.
  It is the identity. Everything else stays quiet so it can be read.

Rules carried over from the first session, unchanged: charts use the validated series palette (blue `#2a78d6`,
orange `#eb6834`, green `#169b6b`, and a neutral grey for the control arm); bars start at zero; every chart has a
table equivalent; status is never conveyed by colour alone.

## 2. Principles

1. **The number is never alone.** A value appears with its unit, its interval when one was reported, its source
   tier and its verification status. If a component cannot show the qualifiers, it should not show the number.
2. **Missing is a state, not a zero.** A value the source does not report is written out ("Not reported") with the
   missing icon. It is never 0, never blank and never a dash that could be read as a minus sign.
3. **Only a person verifies.** Machine-extracted and quote-anchored are automatic checks and look like automatic
   checks. Only `HUMAN_VERIFIED` uses the green tone and a check mark. No summary may add mechanical states into a
   "verified" total.
4. **Loading, failure and empty are three different things.** A pending or failed request must never render as a
   confident empty state ("No FDA labeling is on record"). See section 14.
5. **Nothing is cut short silently.** Every list states how many rows exist and offers the rest.
6. **Colour is the last channel.** Every state is distinguishable by icon, text and shape first. The interface must
   survive greyscale printing and colour-vision deficiency.
7. **Calm over clever.** Motion confirms an action or shows where something went. It never decorates and never
   touches a scientific value or a chart mark.
8. **Keep what is not evidence visibly apart.** Hypotheses, simulations and synthetic demonstration data sit in a
   hatched, dashed container. A reader must not be able to mistake them for trial data at a glance.

## 3. Colour

All colours are CSS custom properties on `:root`, mapped to Tailwind utilities in `@theme` (`text-ink-3`,
`bg-accent-soft`, `border-control`). Use the utility or the variable. Do not write a hex value in a component.

Contrast ratios below are measured with the WCAG 2 relative-luminance formula from the values in `globals.css`.
Text needs 4.5:1 (3:1 at 18.66 px bold or 24 px); the edge of an interactive control and meaningful graphics need
3:1.

### Surfaces and ink

| Token | Value | Role |
|---|---|---|
| `--page` | `#f6f7f8` | Page plane behind everything |
| `--surface` | `#ffffff` | Panels, tables, controls, overlays |
| `--surface-sunken` | `#f9fafb` | Table header, row hover, inset blocks |
| `--surface-hover` | `#f1f3f6` | Hover on neutral controls and navigation |
| `--scrim` | `rgb(16 24 32 / 0.44)` | Behind a modal or drawer |
| `--ink` | `#101820` | Primary text, values, headings |
| `--ink-2` | `#414b57` | Secondary text, labels, table headers |
| `--ink-3` | `#5f6874` | Tertiary text, captions, placeholders. The lightest text allowed |

| Text on | `--surface` | `--page` | `--surface-sunken` | `--surface-hover` | `--accent-soft` | `--ok-soft` | `--warn-soft` | `--serious-soft` | `--critical-soft` |
|---|---|---|---|---|---|---|---|---|---|
| `--ink` | 17.89 | 16.68 | | | | | | | |
| `--ink-2` | 8.87 | 8.27 | 8.48 | 7.97 | 7.80 | | | | |
| `--ink-3` | 5.65 | 5.26 | 5.40 | 5.08 | 4.97 | 5.06 | 5.12 | 4.88 | 4.82 |

`--ink-3` was `#6b7480` and failed on the page plane (4.41) and on the tints (4.17 on `--accent-soft`). It now
clears 4.5:1 on every surface and tint in the system, so it can be used anywhere without a check. Placeholder
text was the browser default (current colour at 50%, 2.5 to 3.4:1); it is now `--ink-3` at full opacity.

### Lines

| Token | Value | Role | On `--surface` |
|---|---|---|---|
| `--hairline` | `#e4e7eb` | Panel borders, row dividers (decorative) | 1.24 |
| `--rule` | `#cfd4da` | Table header rule, stronger divider (decorative) | 1.49 |
| `--border-control` | `#858d98` | Edge of inputs, selects, secondary buttons, mechanical state chips | 3.35 (3.13 on `--page`, 3.21 on `--surface-sunken`) |
| `--hairline-strong` | alias of `--border-control` | Earlier name. Still used by existing pages and by the charts | |

Input and select borders were `#cfd4da` (1.49:1). `--hairline-strong` now resolves to the 3:1 value, so every
existing control and the charts' zero line became compliant without touching those files. Decorative dividers
that used to share that token moved to `--rule`.

### Accent

| Token | Value | Role | Contrast |
|---|---|---|---|
| `--accent` | `#1d5fbf` | Links, primary button, selection, focus ring | 6.10 on surface, 5.69 on page, 5.37 on `--accent-soft`; white on it 6.10 |
| `--accent-hover` | `#184f9f` | Primary button hover | white on it 7.90 |
| `--accent-active` | `#143f80` | Primary button pressed | white on it 10.23 |
| `--accent-soft` | `#eaf1fb` | Selected row, current navigation entry, informational tint | |
| `--accent-border` | `#cfdff5` | Edge of an informational tint (decorative) | |

`--teal` (`#0e7c75`) is retained only because two pages outside the foundation still reference it. It is not a
second accent and must not be used in new work.

### Status

Always with an icon and a text label. The tint and its border are decorative.

| Role | Text | Tint | Border | Text on tint | Text on surface |
|---|---|---|---|---|---|
| ok | `#0b7d0b` | `#e9f6e9` | `#cfe8cf` | 4.76 | 5.31 |
| warn | `#8a5a00` | `#fdf3dc` | `#f1dfb0` | 5.37 | 5.93 |
| serious | `#a8431b` | `#fcebe3` | `#f3d3c4` | 5.20 | 6.03 |
| critical | `#b3261e` | `#fbe9e7` | `#f2c9c5` | 5.58 | 6.54 |

### Chart series

| Token | Value | Use | On white |
|---|---|---|---|
| `--series-1` | `#2a78d6` | Investigational arm | 4.42 |
| `--series-2` | `#eb6834` | Active comparator | 3.20 |
| `--series-3` | `#169b6b` | Arm role not classified (outlined bar: an arm the data does not identify as study drug, comparator or placebo); treatment sequence (filled bar: arm role `SEQUENCE`, placebo and an active drug in different periods) | 3.54 |
| `--series-control` | `#8a8a84` | Placebo or control arm | 3.47 |
| `--grid` | `#eceef1` | Gridlines, meter track | |

`--series-control` was `#9a9a94` (2.83:1); it is now 3.47:1. Checked with the palette validator of the dataviz
skill against a white surface: the three hues pass the lightness band, chroma floor, colour-vision separation
(worst pair ΔE 9.2) and normal-vision floor (27.6). The control grey keeps its separation from blue and orange
(colour-vision ΔE 10.2 or more, normal vision 17.6 or more); it fails that tool's chroma floor on purpose, because
a control arm is meant to read as neutral.

`--series-3` was `#1baf7a` (2.82:1 on white, 2.58:1 on the hover surface) while it was unused. It now marks "Arm role
not classified", which is drawn as an outlined bar: the outline is the only edge of that mark, so it must meet
3:1 (1.4.11). The hue is kept and the lightness lowered to `#169b6b` (3.54:1 on white, 3.2:1 on `--surface-hover`).
Each such row also names its role in text ("Arm role not classified"), and every chart has its table view. A
treatment-sequence arm (stored arm role `SEQUENCE`) uses the same hue as a filled bar and is named "Treatment
sequence" in text: it is never drawn as placebo and never offered as a dose.

Series colours identify an arm. They are never used for status, and status colours are never used as a series.

## 4. Typography

**Families.** The system interface stack for text and the system monospace stack for identifiers:

```
--font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif
--font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace
```

No web font is loaded. Reasons: builds must work offline (a hosted font is fetched at build time), there is no
font swap and so no layout shift, and the platform's own face renders tabular figures well. If cross-platform
metric consistency becomes a requirement, add one self-hosted variable sans through `next/font/local` and keep the
scale below.

**Scale.** Named by role, in rem so it follows the reader's browser setting. Use the utility (`text-data`), not an
arbitrary size.

| Utility | Size / line height | Weight | Use |
|---|---|---|---|
| `text-micro` | 11 / 16 px | 600 | Navigation group labels. Nothing smaller exists |
| `text-caption` | 12 / 16 px | 400, 500, 600 | Badges, helper and error text, table column headers (600), captions |
| `text-data` | 13 / 20 px | 400 | Table cells, secondary paragraphs, dense lists, small buttons |
| `text-body` | 14 / 22 px | 400 | Default text, controls, panel titles (600) |
| `text-title` | 16 / 24 px | 600 | Section headings (h2), dialog titles |
| `text-heading` | 18 / 26 px | 600 | Reserved for a page with a second heading tier |
| `text-stat` | 22 / 28 px | 600 | Stat tile value; page title on a phone |
| `text-display` | 26 / 32 px | 600 | Page title (h1) from 640 px |
| `text-hero` | 30 / 34 px | 600 | The single headline figure of a view |

Rules:

- Hierarchy comes from weight and ink step before size. Three weights only: 400, 500 (labels, links in lists), 600.
- Headings use `text-wrap: balance` and a letter-spacing of -0.011em; paragraphs use `text-wrap: pretty`. Prose is
  limited to about 68 to 72 characters per line.
- **Tabular figures** (`.num`, and every `.tbl`) wherever numbers are compared in a column or change in place:
  table cells, counts in toolbars, pagination, axis ticks. **Proportional figures** for a standalone value
  (stat tile, hero figure): tabular digits look loose at display size.
- Monospace (`.mono`) only for identifiers: NCT numbers, compound codes, rule ids, hashes, source keys. Mark them
  `translate="no"`.
- Sentence case for headings, labels, buttons and table columns. Navigation entries keep their existing Title Case
  names. No uppercase with letter-spacing (none is left in the pages; the review screens are owned separately).
- **Recorded exception: arbitrary sizes in older pages.** The trial detail, compare, benefit–risk, safety, design
  and molecules pages were written before this scale (the assistant page was rebuilt on it in P3) and still use arbitrary `text-[Npx]` sizes (about
  160 uses, 14 distinct sizes, all between 11 and 30 px). They are converted when a page is next reworked; new
  code uses the named scale only. A heading keeps its element (`h2`, `h3`) whatever size class it carries.
- Typographic characters: `…` not `...`, curly quotes, a true minus sign for negative values (from `fmt`).

## 5. Spacing

A 4 px base, using Tailwind's scale. Prefer these steps; an odd value needs a reason.

| Step | px | Use |
|---|---|---|
| 0.5 / 1 | 2 / 4 | Inside a chip; icon to label |
| 1.5 / 2 | 6 / 8 | Between chips; label to control; list items |
| 3 | 12 | Between controls in a filter row; table cell padding (horizontal) |
| 4 | 16 | Panel padding; phone gutter; grid gap inside a panel |
| 5 | 20 | Roomy panel padding (`!p-5`); gap between panels |
| 6 / 7 | 24 / 28 | Tablet gutter (28) |
| 8 | 32 | Between sections (`Section` bottom margin) |
| 10 | 40 | Desktop gutter |

Page gutters: 16 px below 640, 28 px from 640, 40 px from 1024. Content column: 1240 px maximum, centred.

Any multi-column layout declares its single-column base in the same class list
(`grid grid-cols-1 lg:grid-cols-2`). `globals.css` also sets `min-width: 0` on every grid item, so one wide table
or chart cannot stretch its column and then the page.

## 6. Dimensions

| Thing | Value | Token or class |
|---|---|---|
| Control height, default | 36 px; 44 px on touch devices | `--control-h` (raised under `@media (pointer: coarse)`) |
| Control height, small | 28 px; 36 px on touch devices | `--control-h-sm` |
| Chip height | 24 px | `CHIP` in `badge.tsx` |
| Table header row | 32 px | `.tbl thead th` (8 + 16 + 8) |
| Table row, comfortable | from 38 px | `.tbl td` (9 + 20 + 9) |
| Table row, compact | from 30 px | `.tbl-compact` |
| Table scroll area | `min(72vh, 760px)` by default | `TableCard maxHeight` |
| Panel padding | 16 px; 20 px roomy | `Panel`, `!p-5` |
| Sidebar width | 240 px | `--sidebar-w` |
| Top bar height (below 1024 px) | 56 px | `--topbar-h` |
| Navigation drawer | 288 px, never more than viewport minus 48 px | `Drawer width` |
| Detail drawer | 544 px, same cap | `Drawer width` |
| Modal width | 384 / 512 / 672 px | `Modal size` sm / md / lg |
| Content column | 1240 px max | `Shell` |

**Breakpoints** (Tailwind defaults): `sm` 640, `md` 768, `lg` 1024, `xl` 1280, `2xl` 1536.

- Below `lg`: top bar with a menu button; navigation in a drawer.
- From `lg`: fixed sidebar. Its list scrolls on short screens; brand and service status stay in view.
- From `xl`: list and detail side by side (Sources). Below `xl` the detail opens in a drawer.

The interface is checked at 1440 x 900, 834 x 1112 and 390 x 844. No route may scroll sideways at any of them
(`e2e/foundation.spec.ts` asserts this at 390 and 834).

## 7. Border and radius

One radius per kind of object. This is the whole rule; do not mix.

| Token | Value | Object |
|---|---|---|
| `rounded-chip` | 4 px | Badges, chips, inline buttons, focus ring |
| `rounded-control` | 6 px | Buttons, inputs, selects, callouts, tooltips |
| `rounded-panel` | 8 px | Panels, table cards, popovers, toasts |
| `rounded-overlay` | 12 px | Modal |
| `rounded-full` | | Meter tracks and the navigation indicator only |

Borders are 1 px. A panel uses `--hairline`; a control uses `--border-control`. Outline **style** carries meaning
in the evidence-state language (section 12): solid, dashed, dotted, hatched and heavier outlines are reserved for
it and must not be used decoratively elsewhere.

## 8. Elevation and layers

| Level | Treatment | Used by |
|---|---|---|
| 0 | No border, no shadow | Page plane, text |
| 1 | `--surface` with a 1 px `--hairline` border | Panels, tables |
| 2 | `--elevation-raised` (`shadow-raised`) | Tooltip, popover, toast |
| 3 | `--elevation-overlay` (`shadow-overlay`) over `--scrim` | Modal, drawer |

Shadows are tinted with the ink hue, never pure black. A panel does not get a shadow, and a panel inside a panel
is avoided: the one accepted exception is a scrolling table frame inside a detail panel, where the frame is what
keeps the scrolling contained.

Layer order (`--z-*`): sticky table header 10, top bar 30, scrim 40, dialog, popover and tooltip 50, toast and
skip link 60. Do not introduce other z-index values.

## 9. Interaction states

Every interactive component implements all of these.

| State | Treatment |
|---|---|
| Rest | As specified per component |
| Hover | One step more contrast: primary button to `--accent-hover`; neutral control to `--surface-hover`; link gains an underline; control edge to `--ink-2`; table row to `--surface-sunken` |
| Focus-visible | 2 px `--accent` outline, 2 px offset, on every focusable element (global rule). Never removed without a replacement. Mouse clicks do not show it |
| Active (pressed) | Moves down 1 px; primary button to `--accent-active` |
| Selected / current | Never by fill alone: `--accent-soft` fill **and** a 2 px accent bar (table row, navigation), an underline (tab), or a 3:1 edge with heavier text (segmented control). Exposed as `aria-current`, `aria-selected`, `aria-pressed` or `aria-expanded` |
| Disabled | 55% opacity, `not-allowed` cursor, no hover or press. Say why nearby when it is not obvious |
| Loading (action) | Spinner in the button, `aria-busy`, further clicks ignored, label changes to the action in progress ("Recording…") |
| Loading (content) | Skeleton with the outline of the content, `role="status"`, label ending in "…" |
| Refreshing | Existing content stays, dimmed to 55% and marked `aria-busy`, while a newer answer loads |
| Invalid | Control edge `--critical`, message below the field with `role="alert"`, linked by `aria-describedby`; focus moves to the first invalid field on submit |

Target size: default controls are 36 px (44 px on touch devices), the smallest are 28 px, and chips and inline
buttons are at least 24 px high, which meets WCAG 2.5.8.

## 10. Motion

| Token | Value | Use |
|---|---|---|
| `--dur-fast` | 120 ms | Hover, press, colour and border changes, tooltip and popover entry |
| `--dur-base` | 160 ms | Tab and navigation indicators, disclosure, content entry, exits |
| `--dur-slow` | 220 ms | Drawer, modal and toast entry |
| `--easing-standard` (`ease-standard`) | `cubic-bezier(0.2, 0, 0, 1)` | Entries and moves |
| `--easing-exit` (`ease-exit`) | `cubic-bezier(0.4, 0, 1, 1)` | Exits |

**What animates**

- The navigation indicator and the tab underline slide to the current item (transform only).
- Drawer slides from its edge; modal fades and settles 8 px; scrim fades; toast rises 8 px.
- Popover and tooltip scale from 97%.
- Expanded content (table row detail, disclosure) fades in and settles 2 px; the chevron rotates.
- Content replacing a skeleton fades in once.
- Buttons, links, rows and controls change colour on hover and move 1 px on press.
- A list that is fetching again dims in place (filter feedback).

- A block or source reached through a citation marker or source number (`:target`) gains a 2 px accent edge; only
  the edge transitions (160 ms). An example question's arrow moves 2 px on hover (transform, 120 ms).

**What must never animate**

- A scientific value. No count-up, no ticker, no cross-fade between two numbers.
- A chart mark. No bars growing in, no points flying to position. A redraw must not suggest the data changed.
  (The Recharts line already has its animation switched off.)
- Table rows on sort or filter: they reorder at once.
- Layout properties: width, height, top, left, margin. Only `transform` and `opacity` move; colour, border colour
  and box-shadow may transition on interactive elements. `transition: all` is not used.
- Anything on a loop, except the skeleton shimmer and the spinner.

**Reduced motion.** Under `prefers-reduced-motion: reduce` every animation and transition ends at once (duration
0.01 ms, one iteration), indicators jump, the skeleton is a static block, the spinner stops and smooth scrolling is
off. This is a single global rule in `globals.css`; a new animation needs no extra work to honour it.

## 11. Light and dark

**Light is the only validated theme and the only one shipped.** `color-scheme: light` is set on `:root`, the
`theme-color` meta tag matches `--page`, and native controls state their own background and text colour so a
system dark setting cannot produce dark-on-dark text.

**Strategy for dark, when it is built.** Components never reference a raw colour, only the semantic tokens of
section 3, so a dark theme is a second set of values for the same token names:

```css
:root[data-theme="dark"] { color-scheme: dark; --page: …; --surface: …; --ink: …; /* same names */ }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* same block */ } }
```

- Surfaces step lighter with elevation (page darkest, panel, sunken, hover), not darker.
- Ink: an off-white primary, never pure white; `--ink-3` re-derived to hold 4.5:1 on every dark surface and tint.
- Status and accent are re-stepped, not inverted: lighter text steps on dark tints.
- Chart series use the palette's own dark steps (blue `#3987e5`, orange `#d95926`, aqua `#199e70`), chosen for a
  dark surface, not an automatic flip.
- The hatch texture switches to a light line.
- Shadows mostly disappear; elevation is carried by the surface step.

**Must be re-validated before dark ships** (none of this has been done):

1. Every pair in the contrast tables of section 3, measured again on the dark values.
2. `--border-control` at 3:1 against each dark surface.
3. The chart palette through the validator in dark mode against the dark chart surface, including the control grey.
4. Every evidence-state chip: icon, text and outline legible on dark panels and dark tints; dashed and dotted
   outlines still distinguishable; hatch still visible.
5. Focus ring at 3:1 against each dark surface.
6. Charts: gridlines, axis text, intervals (reported versus derived), tooltips.
7. Native selects and date inputs on Windows; scrollbars; `theme-color`.
8. The full screenshot set (section 18) at three sizes, reviewed by eye, and the browser tests in dark.

Do not add a `dark:` utility or a dark token block before that list is complete.

## 12. Evidence-state language

Three separate questions, three badge families. They are shown side by side and are never merged into one badge.

| Question | Component | Vocabulary |
|---|---|---|
| How far has this stored record been checked? | `VerificationBadge` | `lib/evidence.ts` `VERIFICATION` (mirrors `backend/incretinos/review/policy.py`) |
| What kind of source did it come from? | `TierBadge` | `TIERS` (mirrors `TIER_LABEL` in `backend/incretinos/enums.py`) |
| What kind of statement is it? | `EpistemicBadge`, `Missing`, `Hypothetical` | `EPISTEMIC` (mirrors `ClaimKind`) |

Every state has an icon, a text label and an outline shape. Colour only reinforces. Each badge explains itself in
plain English on hover, keyboard focus and tap (dismissed with Escape).

### Outline shapes

| Shape | Meaning |
|---|---|
| Solid thin outline, neutral | A mechanical state: code produced it and nobody has checked it |
| Dashed outline | A person has to look (pending review, recheck required), or it is not evidence (hypothesis) |
| Dotted outline | Calculated here from reported values |
| Dashed outline with a hatch texture | Synthetic or simulated |
| Heavier outline (1.5 px) with a tint | A recorded human decision (verified, rejected) |
| Heavier left edge, like a file tab | An evidence tier: a category of source, not a state |
| No outline, grey text | Missing |

### Verification status

| Status | Label | Icon (lucide) | Outline | Tone | Meaning shown to the reader |
|---|---|---|---|---|---|
| `MACHINE_EXTRACTED` | Machine-extracted | `Braces` | solid | neutral | Parsed by code from a structured registry record whose snapshot hash is stored. No person has checked it |
| `QUOTE_ANCHORED` | Quote-anchored | `Quote` | solid | neutral | The number and its quote were found word for word in the source. That shows the number exists there. It does not show it belongs to this study, arm, endpoint, estimand or population |
| `PENDING_HUMAN_REVIEW` (stored earlier as `CURATED_PENDING_REVIEW`) | Pending human review | `Hourglass` | dashed | warn text, no fill | No automatic support could be established. Waiting for a reviewer; not to be relied on yet |
| `HUMAN_VERIFIED` | Human-verified | `BadgeCheck` | heavier | ok, tinted | An authorized reviewer approved this exact version. The decision is in the audit log |
| `REJECTED` | Rejected in review | `Ban` | heavier | critical, tinted | A reviewer rejected it. Kept for audit; excluded from comparisons, summaries, calculations and reports |
| `SOURCE_CHANGED_RECHECK_REQUIRED` | Changed since review — recheck required | `RefreshCcwDot` | dashed | warn, tinted | A decision exists, but the record or its source changed afterwards. The decision does not cover this version |
| anything else | the raw status in sentence case | `CircleHelp` | dashed | neutral | Not recognised by the interface. Treat as unchecked |

Rules:

- **Only `HUMAN_VERIFIED` may look verified.** It is the only state with the green tone and the only one with a
  check mark. Quote-anchored is neutral by design (audit finding CUR-12): the anchor proves the number is in the
  source, not that it was attributed correctly.
- The interface never assigns a status. It displays what the API returns. Nothing in the frontend may present a
  record as human-verified unless the API says so.
- A summary lists the statuses separately, including those at zero. It does not add mechanical states into a
  "verified" figure. `countByStatus` returns unknown statuses too, so a count is never dropped.
- A legacy status name is mapped to its current name by `canonicalStatus`; an unknown one is shown as unknown,
  never dressed as a known state.
- The backend also sends the status as a qualifier label (`labels[]`) with tone `ok`. `Qualifiers` draws those
  through `VerificationBadge`, and draws any other `ok` label as plain information, so no qualifier can look like a
  completed verification whatever tone the API attaches. Pass `omitVerification` when a `VerificationBadge` is
  already next to it, to avoid showing the status twice.
- A rejected value, where it is shown at all, is struck through (`ValueCell rejected`) and carries the rejected
  badge.

### Evidence tier

All tiers share one neutral colour: a tier is a category of source, not a verdict. They differ by icon and label.

| Tier | Short label | Icon |
|---|---|---|
| `REGULATORY_LABEL` | FDA label | `Stamp` |
| `REGULATORY_REVIEW` | FDA review | `Landmark` |
| `PEER_REVIEWED` | Peer-reviewed | `BookOpenCheck` |
| `REGISTRY_RESULTS` | Registry results | `ClipboardCheck` |
| `REGISTRY_PROTOCOL` | Registry protocol | `ClipboardList` |
| `PRESS_RELEASE` | Press release | `Megaphone` |
| `SPONSOR_PIPELINE` | Sponsor pipeline | `Building2` |
| `SPONTANEOUS_REPORTS` | Spontaneous reports | `Siren` |
| `CONFERENCE` | Conference | `Presentation` |
| `GUIDANCE` | Guidance | `BookMarked` |
| `SECONDARY` | Secondary | `Newspaper` |

The explanation of each tier says what scrutiny that kind of source has had ("Results the sponsor posted on
ClinicalTrials.gov. Quality-controlled by the registry, not peer reviewed"). When the backend marks a tier
qualifier as a caution (a press release), `Qualifiers` keeps the caution tone on the same tab shape.

### Epistemic kind

| Kind | Label | Icon | Outline | Use |
|---|---|---|---|---|
| `DOCUMENTED` | Reported by the source | `FileText` | solid | Only where the contrast with an inference matters, for example "Stated by the source" next to an estimand |
| `INFERENCE` | Inference | `Sigma` | dotted | An interval derived from a reported standard error; an estimand inferred from the analysis description |
| `HYPOTHESIS` | Hypothesis | `Lightbulb` | dashed | Plausible but untested statements |
| `SIMULATION` | Synthetic | `FlaskConical` | dashed and hatched | Simulation output; synthetic demonstration records |
| `UNSUPPORTED` | No supporting source | `CircleHelp` | dashed, critical text | A statement for which no source was located |
| `MISSING` | Not reported | `CircleSlash` | none | A value the source does not report. Use `<Missing />` in a cell |

`Hypothetical` is the container form of "not evidence": dashed outline, hatch texture, flask icon. It replaced a
cream-coloured box, so the signal no longer depends on a hue.

## 12a. What may share a chart

These rules decide which numbers are drawn together. They are scientific rules, so they live in `lib/rows.ts`
and are tested in `e2e/presentation.spec.ts` and `e2e/review-fixes.spec.ts`.

- **One result set per chart or panel.** Rows share an axis and a caption only when they share the measure,
  estimand, evidence tier, source, time point, baseline definition and unit (`findingSets`). The caption is
  generated from the set; a fixed sentence never describes data it may not match. When several sets exist the
  reader picks one (`Result set` select); they are never mixed.
- **The measure is the measure as the source reports it, not the stored record.** Its key is the endpoint
  category, the endpoint title as text, the responder threshold and the sub-measure. One source table is sometimes
  stored as several endpoint records (the control arm in one record, the multiplicity-controlled active arms in
  another): those rows are one set, so the control arm stays beside its active arms. An arm never appears twice in
  one set. The trial page's endpoint list merges records that differ only in multiplicity control and prints each
  row's status.
- **Estimand chart.** One source's values at one time point and baseline; within it, at most one row per arm and
  estimand. Label tablet doses are never drawn as further arms of a capsule-dose trial: they are another source's
  set. The caption says whether this source reports both estimands (then the gap between an arm's two rows reflects
  only intercurrent-event handling) or only one.
- **Weight change is a percent change.** A row is plotted, scored or averaged as "percent change in body weight"
  only if its category is weight percent change **and** its unit is a percent change (`isWeightPercentChange`). A
  share of participants filed under that endpoint is listed, counted in a note, and kept out of the chart, the
  utility score and the dose scenario.
- **Arm roles come from data.** `armGroup` uses the API's `arm_role` wherever a row carries one, then the placebo
  flag, the registered arm type and the drug's role in the trial; never the wording of the arm label.
- **Dose scenarios** offer only the dose arms of one study drug (at least two doses, single drug, stated dose,
  percent weight change). Comparators, other drugs and combinations are never "doses". The default puts 100% on the
  highest dose. A stop-treatment share is offered only when a placebo arm is reported on the same basis, and its
  label says it is assigned the placebo arm's value.
- **Between-arm contrasts** take their scale (`effect_scale.scale`) and direction (`direction.established`,
  `status`, `basis`) from the API. "X − Y" is written only for an established difference, "X ÷ Y" only for an
  established ratio; otherwise the two groups are named and the API's reason is printed. The basis is printed as
  the API states it: registry-stated, arm-confirmed (both, when the registry names the contrast and the arm values
  confirm its sign), stated by the curated source, or not established.
- **Reported text.** Where the API gives the source's own text for a number (`value_text`, `estimate_text`) and it
  denotes the stored number, that text is printed, so "−25.0%" stays "−25.0%".
- **Units.** Every value carries its unit (`withUnit`, `unitLabel`): kg, %, % HbA1c, % of participants; a
  difference of two percentages is in percentage points (`differenceUnit`).

## 13. Components

Import from `@/components/ui`. Every export that existed before the split still exists with the same props.

| Component | Use it for | Notes |
|---|---|---|
| `Button`, `LinkButton`, `IconButton` | Actions; navigation that looks like a button; icon-only actions | Variants `primary`, `secondary` (`ghost` is its earlier name), `quiet`, `danger`; sizes `md`, `sm`; `loading`. `IconButton` requires `label`. A link navigates, a button acts |
| `TextField`, `SearchField`, `NumberField`, `TextArea`, `Select`, `Slider` | Form controls | Label above, hint below, error replaces hint. `TextField maxLength` states the longest value the service accepts (the assistant question: 1000). The label is tied by id, not by wrapping. Debounce a request driven by typing with `useDebounced`. A `Select` that holds a qualifier (time point, estimand, measure) is not given a fixed width: `w-full sm:w-auto` lets it grow to its longest option, so the selected value is never cut; a long parenthetical goes in the hint |
| `SecretField` | A token or other secret typed by the user | Uncontrolled password input: the secret is never written to the `value` attribute (so not into the DOM markup), no `name`, autocomplete off and the password-manager opt-out attributes; not placed in a `<form>` beside a name field. The page keeps the secret in memory only |
| `SegmentedControl` | Two to four exclusive choices | Native radio group: arrow keys and form semantics come from the browser |
| `Tabs`, `TabPanel` | Views of one subject | Radix tabs; sliding underline. Inactive panels unmount: keep state that must survive in the parent, and keep the tab in the address. The tab list scrolls sideways inside a box that keeps 4 px of room on every side, so a focused tab's ring is never clipped. Use these, not raw Radix tabs |
| `Badge`, `ClassificationBadge`, `RegulatoryBadge` | Generic status with a tone | Icon and text always. `title` adds an explanation that opens on hover, focus and tap |
| `VerificationBadge`, `TierBadge`, `EpistemicBadge`, `Missing`, `Qualifiers` | The evidence-state language | Section 12 |
| `Term` | A technical term with a plain-English explanation | `<Term k="estimand">estimand</Term>`. Definitions live in `lib/glossary.ts` and nowhere else |
| `PageHeader`, `Section`, `Panel` | Page scaffolding | h1, h2 and h3 respectively. `Panel title` renders a real heading; pass `level` |
| `ChartFrame`, `IntervalChart`, `FacetedChart`, `ChartTable` (`components/charts.tsx`) | Charts | The title is a heading (`level`, default 3; 2 when the chart sits directly under the page title). Every chart has its table view. Section 12a says what may share a chart |
| `StatTile` (`Stat`) | A headline count | Label in sentence case, proportional figures, no trend slot |
| `DataTable`, `TableCard`, `SortHeader`, `useSort`, `sortRows` | Tables | Sticky header, sideways scrolling inside the frame, `aria-sort`, density, caption, pinned first column, expanded row, footer slot. Missing values sort last in both directions |
| `Pagination` | "x to y of total" and paging | Also states a filtered-from count and a truncated result |
| `Loading`, `Skeleton`, `TableSkeleton`, `EmptyState`, `ErrorState`, `ErrorNote`, `Async` | Request states | Section 14 |
| `ErrorBoundary` | Containing a rendering failure | One wraps page content in the shell; `app/error.tsx` covers a route |
| `Modal`, `Drawer` | Blocking decisions; detail beside a list, navigation on small screens | Focus trapped, Escape closes, focus returns to the opener, background inert |
| `ToastProvider`, `useToast` | Confirming an action just taken | Polite live region; errors stay until dismissed. Not for anything the reader must act on |
| `Ledger`, `LedgerItem` | A row of headline counts in plain layout (no card per count), divided by hairlines from `lg` | A count that has a page listing exactly what it counts is a link to that page (`href`, `linkLabel`); a count without one is plain text. Zero is shown, never dropped |
| `Timeline`, `TimelineItem` | Dated entries on a vertical rule: registry completions, detected source changes | The date is printed as the source gives it (often partial), with its qualifier ("estimated", "actual", "detected"). The marker is a 1 px tick, not a coloured dot |
| `NextLink` | "Where to go next" under a section: text, then an arrow | Navigation only; never an action |
| `Callout`, `Hypothetical`, `Disclosure`, `Meter`, `KV`, `SourceLine`, `ValueCell` | Notes, non-evidence containers, collapsible detail, 0 to 1 scores, key and value lists, provenance, a value with its interval | `ValueCell` formats through `fmt`, which is owned with the charts |

Icons: lucide only, one family. Size 12 in chips, 13 to 15 in buttons, 16 in navigation. Decorative icons carry
`aria-hidden`; an icon-only control has a label.

### Page-level patterns (P3: command center and research assistant)

These live with their pages (`components/command/`, `components/assistant/`, `lib/assistant.ts`) because they encode
page logic, but the rules are system rules.

- **A count is a door.** On the command center every count that has a filtered page is a link to it, and the
  filtered page shows the same number: attention counts open `/review?status=…`, `/conflicts?class=…`,
  `/quality/records?metric=…` or the review queue; phase and status counts open `/trials?phase=…&status=…&results=…`
  (the trial atlas gained these three filters, kept in the address); gap natures open `/gaps?nature=…`; quality
  figures open their `/quality/records` list. The link's accessible name states the number and what it counts. A
  count that has not arrived shows "Not loaded" or a skeleton, never a number. Asserted in `e2e/priority.spec.ts`.
- **Separate dimensions, no score.** Evidence quality is a ledger of the six dimensions of `/api/quality`, each with
  its own figures ("1,862 of 1,889 results in analytic use (98.6%)"). Nothing is added across dimensions, and the
  result-level verification breakdown sits beside them, its universe named.
- **Derived is labelled derived.** The portfolio's development stage is "most advanced tracked trial", counted from
  the trial list by the atlas's own filter rules and explained by a `Term`; it is not the sponsor's declared stage.
  The regulatory status is the API's, including "Status withheld in review"; the review status of the statement it
  rests on is shown beside its source tier.
- **Heavy reads in sequence.** The command center asks for `/review/summary`, then `/conflicts`, then
  `/validation`, then `/quality`, each after the one before has answered (with data or an error). Fired together
  they competed for the server and all arrived after about 17 s; in sequence the page settles in about 4 s. Each
  block keeps its own loading and error state, so a failure costs one block, not the page.
- **Answer blocks keep the evidence-state outlines.** Reported evidence and model text: solid frame. Rule-based
  interpretation: inset (sunken) frame. Hypothesis: dashed frame. Refusal: critical frame and a ban icon. Not
  available: no frame, grey text, the missing icon. Every block names its kind in words with an icon; its
  verification chips are the API's statuses, one per distinct status.
- **Markers resolve.** Each block with a `[C#]` marker has the anchor `#card-C#`; the marker on the block and every
  marker in the rail's index, in a model summary and in a withheld-draft report links there. Source numbers link to
  `#source-n` in the rail. Each record behind a block links to Evidence Review by its evidence key
  (`/review?type=…&key=…`, from the API's `evidence_key`). Follow-ups are only links the answer already implies: the
  cited finding, an NCT number in the text, the first two reported findings compared, the molecules the question
  names.
- **A withheld model draft is never shown.** Only the API's per-sentence report: verdict, cited markers, reasons and
  the mismatched fields (claimed against stored).
- **The question is in the address** (`/assistant?q=…`), so an answer can be linked to and reloaded.

## 14. Data states and lists

### Request contract (`lib/api.ts`)

`useApi<T>(path)` returns `{ status, data, error, loading, refreshing, stale, reload }`.

| `status` | Means | Render |
|---|---|---|
| `idle` | No request (`path` is null) | Nothing, or a prompt to choose |
| `loading` | Nothing to show yet | A skeleton |
| `error` | The request failed | `ErrorState` with the message and a retry. Keep the page header and the filters |
| `success` | `data` is the answer | The content, or the empty state if the answer is empty |

- `data` is non-null only on `success`. When the path changes, the previous answer is dropped at once: a new
  molecule or trial never appears above the previous one's numbers.
- `keepPrevious: true` keeps showing the previous answer (`refreshing`, `stale`) while the next one loads. Use it
  only when both answers describe the same thing: the next page of a list, the same list re-ranked. Never across
  entities.
- The in-flight request is aborted when the path changes or the component unmounts.
- `Async` renders the four states and takes `isEmpty` and `empty` for the fifth. Empty is always an explicit
  decision made on a successful answer.
- **Every call site branches on `status === "error"`**, including secondary requests (a picker's option list, the
  worked examples, the service status, a term list). The failure is shown next to what it affects, with a retry
  (`ErrorNote onRetry` or `ErrorState`), and the wording that would follow a successful empty answer ("No
  arm-level results", "No language model is configured", "Loading terms…") is never shown for a failed one. A
  page whose selection depends on a failed list says so instead of showing a skeleton for ever.
- Pass a type: `useApi<StudyListItem[]>("/studies")`. Types are in `lib/types.ts`. The untyped default exists only
  so pages that have not been converted keep compiling; the linter rejects `any` in the foundation and in the
  pages built on it.

### Lists (`lib/paging.ts`)

No list is cut short silently. Use one of:

| Hook | For | Behaviour |
|---|---|---|
| `usePagedList(base, { pageSize, legacyLimit })` | Long lists (sources, review queue, audit log, registry index, gaps) | With the new API envelope `{ total, limit, offset, rows }` each page is one request. With an older shape (a bare array, or `{ total, rows }`) the whole list is fetched once at the endpoint's largest limit and paged in the browser. Either way the footer can say "26 to 50 of 207" |
| `useWholeList(base)` | Lists filtered or sorted in the browser (trials, the safety term selector) | Reads every page of a paged endpoint, up to a cap, and reports `truncated` if the cap is reached. Ask for the endpoint's largest page in `base` (`limit=5000` for safety terms) so most lists are one request. A selector over such a list offers a search box and says "n of total" |
| `useClientPaging(rows)` | Paging rows already in memory | |
| `rowsOf(payload)`, `readList(payload)` | Reading any of the three shapes | |

If the service reports more rows than it returned and offers no way to fetch them, `truncated` is true and
`Pagination` says so in words. A count that was not received is never displayed.

Filters, the active tab and the selected record live in the query string (`useQueryParams`), so a view can be
linked to and restored.

## 15. Page structure

- **Shell** (`components/Shell.tsx`): skip link, top bar or sidebar, `main#main`, footer with the research-use
  notice on every page and every screen size. Navigation entries and groups are in `lib/nav.ts`.
- **Headings**: exactly one `h1` per page (`PageHeader`); `Section` is `h2`; a titled `Panel` is `h3` unless told
  otherwise; nothing skips a level. A title is a heading element, never a bold paragraph.
- **Document title**: every route has its own, through the root template (`%s · IncretinOS`). A server `page.tsx`
  exports `metadata` and renders a client view; a route whose page is a client component gets a `layout.tsx` that
  exports the metadata; `/trials/[nct]` builds its title from the trial's display name.
- **Context line** (`PageHeader eyebrow`): one per page, sentence case, quiet. It is not repeated above sections.
- **Errors**: `app/error.tsx` (route), `app/not-found.tsx`, `app/global-error.tsx`, and an `ErrorBoundary` around
  page content.
- **Copy**: plain, active, second person. An error says what failed and what to do next. A button says what it
  does ("Record: Reject", not "Confirm"). Loading text ends with "…".

## 16. Accessibility

Target: WCAG 2.2 AA. What the foundation guarantees, and where it is asserted.

| Requirement | How |
|---|---|
| Text contrast 4.5:1 (1.4.3) | Token tables in section 3; `--ink-3` is safe on every surface and tint |
| Non-text contrast 3:1 (1.4.11) | `--border-control`; focus ring; selected states never by faint fill alone |
| Not colour alone (1.4.1) | Icon, text and outline on every state |
| Reflow (1.4.10) | No sideways page scroll at 320, 390 and 834 px, measured after the network is idle and in every tab state; tables scroll inside a positioned frame, so visually hidden cell text cannot widen the page (test: "no route scrolls sideways … in any tab") |
| Content on hover or focus (1.4.13) | Tooltips open on focus and tap as well, stay while hovered, close on Escape |
| Keyboard (2.1.1), no trap (2.1.2) | Native buttons and links throughout; dialogs close on Escape and return focus (tests: phone navigation, Term) |
| Bypass blocks (2.4.1) | Skip link, first tab stop (test: skip link) |
| Page titled (2.4.2) | Per-route titles (test: document titles) |
| Focus visible (2.4.7), not obscured (2.4.11) | Global focus ring; `scroll-padding-top` under the top bar and under sticky table headers; scroll boxes around focusable items (the tab list) keep room for the ring (test: "WEB-13") |
| Info and relationships (1.3.1), headings (2.4.6) | Panel, chart and answer-block titles are heading elements (test: "WEB-12") |
| Target size (2.5.8) | Section 9 |
| Labels and errors (3.3.1, 3.3.2) | Visible labels; errors in text, linked to the field |
| Name, role, value (4.1.2) | `aria-current`, `aria-sort`, `aria-expanded`, `aria-selected`, `aria-busy` (test: sortable headers) |
| Status messages (4.1.3) | `role="status"` on loading and paging; `role="alert"` on errors; toast live region |
| Reduced motion | Global rule (test: reduced motion) |

Known gaps are listed in the session report rather than here, so this document describes the system and not its
backlog.

## 17. Skill application log

Four design skills were read in full and used as design guidance. What follows says, for each, what was adopted
and what was deliberately set aside.

### Taste (`design-taste-frontend`)

The skill says of itself that it is for "landing pages, portfolios, and redesigns. Not dashboards, not data
tables, not multi-step product UI", and its section 13 lists dense product UI and data tables as out of scope.
IncretinOS is exactly that, so only the parts that hold for a product interface were used.

**Adopted**

- Brief inference before design (0.B) and the three dials (1): the design read and dials in section 1 here.
  "Quiet constraints override aesthetic preference" (0.A): a regulated, accessibility-critical subject sets the
  language.
- Redesign protocol (11): mode "redesign, preserve". Audit first (the Session 2 audit and the before screenshots).
  Routes, navigation labels, copy voice and existing accessibility wins were kept; the levers were applied in its
  order: typography, spacing and rhythm, colour recalibration, motion.
- Typography (4.1): hierarchy through weight and ink step, not raw size; no serif; emphasis within one family.
- Colour calibration (4.2): one accent, locked across every page (the teal eyebrow, a second accent, was retired);
  neutral base; one grey family (the warm cream "hypothetical" box was replaced by a neutral hatch).
- Materiality (4.4): a card only where it groups; one documented radius rule (shape consistency lock); shadows
  tinted to the ink hue and only on floating layers.
- Interactive states (4.5): skeletons shaped like the content instead of spinners; empty states that say how to
  populate; inline errors; 1 px press feedback; button and form contrast checks.
- Data and form patterns (4.6): label above, helper below, error below, no placeholder as label.
- Layout discipline (4.7), the parts that apply: explicit single-column collapse per layout; eyebrow restraint
  (one context line per page; uppercase tracked labels removed from stat tiles, panel titles and table headers).
- Content (4.9): copy self-audit of every new string; no invented numbers, every figure comes from the API.
- Theme lock (4.11): one theme for the whole application.
- Motion must be motivated (5), no scroll listeners (5.D), transform and opacity only for movement (6.A),
  reduced motion mandatory (6.B), a documented z-index scale (6.F).
- Its list of machine-made tells (9): no gradients, glows or gradient text; no pure black; no decorative status
  dots; no section-number labels; no version stamps; no filler verbs; the middle dot is no longer the default
  separator in new copy; generic three-equal-card rows avoided.
- Stack conventions that already held (3): Tailwind v4 with `@tailwindcss/postcss`, standard breakpoints, grid
  rather than flex percentages, `dvh` units, one icon family, no emoji, dependencies checked in `package.json`
  before use.

**Deliberately not adopted**

- "Reach for an official design system for dashboards" (2.A: Carbon, Fluent): the brief forbids replacing the
  component stack. Radix primitives with the project's own tokens are used instead.
- The Motion library and scroll choreography (3.A, 5.A to 5.C, 10): at motion intensity 3, CSS transitions are
  enough and add no dependency.
- A display web font through `next/font` (3.A, 4.1): see section 4. System stack kept for offline builds and zero
  layout shift.
- "Lucide is discouraged" (3.C): it is already the project's icon family, and "one family per project" wins.
- Hero, bento, zig-zag, logo wall, marquee, testimonial, image-generation and pricing rules (4.3, most of 4.7,
  4.8, 4.10, 12): landing-page material with no counterpart here.
- "No data-dump sections; long lists need a different component" (4.9): in this product the table is the content.
  The answer is pagination, sorting, sticky headers and pinned columns.
- "Mono for all numbers at high density" (7): tabular figures in the sans are used; mono is kept for identifiers.
- Dark mode as mandatory (6.C, 8): not shipped, by instruction. Section 11 specifies it.
- "No pure #ffffff" (8.B): the panel surface stays `#ffffff` because every contrast ratio and the chart palette are
  validated against it. The page plane is off-white.
- The em-dash ban (9.G): applied to new interface copy only. Existing headings and labels supplied by the backend
  ("Changed since review — recheck required") are unchanged: tests assert them and the copy voice is preserved.
- Core Web Vitals targets (6.D): no Lighthouse run was made; nothing is claimed.

### Web Interface Guidelines (`web-design-guidelines`)

The rule list the skill tells the reader to fetch (already fetched, not fetched again) was applied as a review
checklist to the foundation and to the five pages that own it.

**Adopted and enforced in the foundation**

- Accessibility: labels on every control; `aria-label` on icon-only buttons; decorative icons hidden; real
  buttons and links (a row click handler and a button that navigated were replaced); hierarchical headings; skip
  link; `scroll-margin-top`; live regions for asynchronous updates.
- Focus: `:focus-visible` ring everywhere; no outline removed without replacement; sticky bars do not cover the
  focused element.
- Forms: `name`, `autocomplete`, correct `type` and `inputmode`; paste never blocked; clickable labels;
  spellcheck off on identifiers; submit stays enabled and shows progress; inline errors with focus on the first;
  placeholders end with "…".
- Animation: reduced motion honoured; no `transition: all`; movement by transform and opacity; interruptible.
- Typography: "…", curly quotes, tabular numerals for columns, balanced headings.
- Content handling: long content wraps or scrolls; flex and grid children can shrink; empty states everywhere.
- Performance: lists are paged (never more than 100 rows mounted); no layout reads during render; debounced
  inputs.
- Navigation and state: filters, tabs and selection in the address; links for navigation; a confirmation step
  before a review decision.
- Touch and layout: `touch-action: manipulation`; an intentional tap highlight; `overscroll-behavior: contain` in
  modal and drawers; safe-area insets on the top bar, drawers and toasts; no stray scrollbars.
- Theming: `color-scheme`, `theme-color`, explicit colours on native controls.
- Locale: `Intl.NumberFormat` for counts; `translate="no"` on identifiers and brand names.
- Copy: active voice, numerals for counts, specific button labels, errors with a next step.

**Deliberately not adopted, or adapted**

- "Title Case for headings and buttons": the house style is sentence case (as in Carbon). Navigation entries keep
  their existing Title Case names.
- "Dates through `Intl.DateTimeFormat`": registry dates are shown exactly as the source reports them, in ISO form
  and often partial ("2026-10"). Reformatting would invent a precision the source does not give.
- "Animate transform and opacity only": applied to everything that moves. Colour, border and shadow still
  transition for 120 ms on hover and focus, which is feedback, not movement.
- "Non-breaking spaces in units": values and units arrive from the API as text; `white-space: nowrap` is used on
  the cell rather than rewriting strings.
- "Virtualize lists over 50 items": pagination was chosen instead, because the reader needs a stable "n of total".
- "Deep-link all stateful UI": page number, sort order and the expanded row are not in the address yet.
- "Warn before navigation with unsaved changes": the only unsaved text is the rationale inside a modal, which
  blocks navigation; closing the modal discards it without a prompt.

### Awesome Design (`awesome-design`)

Four brand references were studied for how they structure a system, not for how they look:
`ibm`, `linear.app`, `stripe` and `mercury` (`DESIGN.md` of each).

**Learned and applied**

- Token architecture (all four): colours named by role (canvas, a surface ladder, an ink ladder, a hairline
  ladder, one accent, semantic status) rather than by hue. Section 3 follows that shape.
- Depth by surface step and hairline, not shadow (IBM, Linear, Mercury); shadows only on floating layers and
  tinted with the ink hue (Stripe's two-level shadow).
- One accent, used scarcely (all four): here, interaction only.
- Tabular figures on every numeric cell (Stripe's "quiet financial-data signal"), as an explicit rule.
- Sentence-case labels instead of tracked uppercase eyebrows (IBM: "Carbon resists the all-caps tracked eyebrow").
- A 4 px spacing base and density as a deliberate choice (IBM: "content is dense by design").
- A radius scale with one documented use per step (Linear: chips 4, tags 6, controls 8, cards 12): section 7.
- Component states written down per component, including focus and pressed (IBM, Linear).
- Touch targets that grow on touch viewports (IBM 48 px, Linear 44 px): `--control-h` under `pointer: coarse`.
- Navigation that collapses to a menu below a breakpoint, documented as part of the system.

**Not copied**

- No brand colour: not IBM Blue, Linear's lavender, Stripe's indigo or Mercury's indigo. The accent is the blue
  the product already had.
- No brand typeface: not Plex, Söhne, arcadia or Linear's faces, and not the light-weight display type three of
  them rely on.
- No signature geometry: not IBM's square corners, Stripe's pill buttons and gradient mesh, Mercury's translucent
  capsules and night-and-day worlds, or Linear's dark canvas and screenshot-led pages.

What is IncretinOS's own is the evidence-state language of section 12, which none of the references has.

### Image-to-Code (`image-to-code`)

The skill's core rule is to generate design images first and build from them. **No image-generation tool was
available, so that step was not performed, and no generated image exists.** Its analysis and extraction rules
were applied instead to real screenshots: the eleven in `docs/screenshots/`, the thirty-six captured before any
change (`docs/design/before/`), and the captures taken after (`docs/design/after/`). Where a full-page capture was
too small to read, a region was captured again at full size from the running application. The skill prefers a
fresh image to a crop; a fresh capture of the same page is the closest equivalent available.

**Extraction from the before screenshots** (its sections 8, 9 and 21 to 25)

| Aspect | Observed | Decision |
|---|---|---|
| Text | Page title 26 px semibold; body 14 px; sixteen distinct arbitrary sizes in use | A nine-step named scale |
| Labels | Teal uppercase tracked eyebrow; uppercase stat labels, panel labels and table headers | Sentence case; one quiet context line per page |
| Colour | Cool grey page, white panels, blue accent, teal eyebrow, cream hypothetical box, eight raw hex values | One accent; tokens for every tint and border; hatch instead of cream |
| Spacing | 236 px sidebar, 40 px gutters, 16 to 20 px panel padding, 32 px between sections | Kept and written down |
| Buttons | 36 px, 6 px radius, hover as a raw hex | Tokens; pressed and loading states added |
| Components | Tier chips coloured green, blue and amber with no icon; verification status shown twice; equal-height cards with dead space | Neutral tier chips with icons; de-duplication; `items-start` |
| Structure | Sidebar turns into a clipped strip below 1024 px; tables widen the page on a phone (up to +431 px) | Menu button and drawer; scrolling inside the table frame |
| Still unclear from images | Focus order, live-region behaviour, reduced motion | Checked in the browser tests, not from images |

**Adopted**

- Clean, systematic analysis before code (8, 9), and extraction of type, spacing, components and colour (22 to 25).
- The anti-nested-box rule (16): no card inside a card, with the one exception noted in section 8.
- Reducing micro-UI clutter (17): duplicate badges removed, uppercase micro-labels removed, tier chips made quiet.
- A first view that is readable on a small laptop (15): the page title, the primary action and the headline
  counts are visible without scrolling at 1440 x 900.
- Multi-image consistency (34), applied to pages: one scale, one radius rule, one badge family across all routes.

**Deliberately not adopted**

- Image generation first (2, 10): not possible here, as stated above.
- Its baseline dials (variance 8, density 3, spacing generosity 9) and "the page should breathe": the opposite of
  a dense evidence tool.
- The combinatorial variation engine, hero rules, section packs and image system (12, 14, 19, 20, 33): marketing
  site material.

### Priority pages (P3): Research Command Center and Research Assistant

The same four skills were applied again to the two priority pages. The before captures are in
`docs/design/before-priority/` and were analysed before any change; the after captures are in
`docs/design/after-priority/`.

**Image-to-Code, applied to the before captures** (no image generation was available; its analysis rules were used
on real screenshots, sections 8, 9, 15 to 17 and 21 to 25)

| Aspect | Observed before | Decision |
|---|---|---|
| Hierarchy | Command center: four equal stat tiles, then validation, then a long worked-comparison list in the middle; upcoming completions last. Several API fields were never shown (trials by status, completed trials without results, quality, conflicts) | One reading order: what this is, how much evidence, the portfolio beside what needs attention, development, quality, gaps and changes, then where to go next |
| Micro-UI clutter | Five green "Pass" chips repeated for passing rules | Only failing rules are listed; "16 / 16 passing" and a link otherwise |
| Phone | The portfolio's regulatory-status column was off-screen behind the class column | Mechanism moved under the molecule name; status is the second column |
| Assistant colour | Block kinds told apart by left-border colours, one a raw hex and one the retired teal | Evidence-state outlines plus icon and words; no raw colour |
| Assistant controls | Example questions as six pills (a radius the system reserves for meters), wrapping to three rows | A two-column list of text buttons, folded away once a question is asked |
| Assistant resolution | `[C#]` were inert chips; the question type was a raw enum; no route to the review record | Markers are links; the question type is in words with the code beside it; records link to Evidence Review |
| Still unclear from images | Keyboard order, live regions, the withheld-draft path | Checked in `e2e/priority.spec.ts` |

**Taste** (product-UI parts only, as before)

- Redesign protocol (11): mode "redesign, preserve". Routes, navigation labels, the page title and the copy voice
  were kept; the hierarchy and layout were recomposed (lever 5) because the information architecture of the page,
  not its styling, was the problem.
- Layout discipline (4.7): an asymmetric two-column composition (a wide main column and a 21 rem rail) instead of a
  grid of identical cards; every multi-column block declares its single-column base; one eyebrow on each page.
- No three equal cards and no card per metric (4.4, 9.C): the ledger and the quality dimensions are plain rows with
  hairlines; panels are kept only for tables.
- Long lists (4.9): gap natures as a two-column table, worked comparisons in two columns, completions on a timeline.
- States (4.5): skeletons shaped like their blocks; an error per block with a retry; zeros written as zeros.
- The em-dash ban (9.G): applied to all new interface copy on both pages ("Hypothesis, untested", "No language model
  is configured. Answers are composed deterministically"). Text the API returns (gap-nature labels, block text) is
  printed as returned: it is scientific content, not interface copy.
- No decorative status dots (9.F): the timeline marker is a 1 px tick.
- Not adopted: "no filled-track bars" (9.F) for the verification breakdown, whose zero-based bars repeat the counts
  and are asserted by tests; the counts remain the content.

**Web Interface Guidelines** (the already fetched rule list, used as a checklist)

- URL reflects state: the assistant's question (`?q=`), the trial atlas's phase, status and results filters.
- Links for navigation (every count, marker and follow-up is an `<a>`), buttons for actions (Ask, examples).
- Forms: visible label, `name`, `maxLength`, `autocomplete="off"`, placeholder ending in "…", the submit button stays
  enabled and shows progress ("Asking…"), an inline error with `role="alert"` for a question that is too short.
- Async updates announced: a polite status line when an answer arrives; loading states end with "…".
- `translate="no"` on NCT numbers, marker ids and molecule codes; tabular figures on counts in columns.
- Flex and grid children shrink (`min-w-0`); no route scrolls sideways at 390, 834 or 1440 px.

**Awesome Design**

- `theverge`: its timeline rail (a dated feed on a vertical rule) became `Timeline` for registry completions and
  source changes, without its colours, type or radii.
- `ibm` (read earlier in this session): density as a decision. "Needs attention" is a dense list, one row per kind of
  work, with the count first and the label and its meaning beside it.
- Nothing was copied beyond structure: no brand colour, face or geometry.

## 18. Screenshots

`docs/design/before/` holds every route at 1440 x 900, 834 x 1112 and 390 x 844 before the Session 2 design work,
with `metrics.jsonl` (document title, sideways overflow, page height, heading outline per route and size).
`docs/design/after/` holds the same set afterwards, plus the three new routes, plus tab states that are not a
route's default view (the design workbench's other three tools, the sources page's changes and validation tabs).

To capture again (the script seeds its own database and starts its own servers on the given ports; it is not part
of `npm run test:e2e`):

```
cd frontend
INCRETINOS_API_PORT=8025 INCRETINOS_WEB_PORT=3025 CAPTURE_LABEL=after CAPTURE_EXTRA=1 npm run capture
```

`CAPTURE_LABEL` names the folder under `docs/design/`; `CAPTURE_EXTRA=1` adds `/review`, `/quality` and
`/conflicts`; `CAPTURE_ONLY=home,assistant,assistant-answer` restricts a run to the named captures
(`assistant-answer` is the assistant after its first example question). The priority-page captures were made with
`CAPTURE_LABEL=before-priority` (before any change) and `CAPTURE_LABEL=after-priority`.
