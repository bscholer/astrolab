# astrolab UI design guide

This is the working style guide for the astrolab web UI. The decisions
below are **prescriptive** — follow them unless you have a specific
reason not to. The /playground/themes route is the live reference;
keep it in sync if you change a token here.

---

## Theme

The theme is **Astrolab**: a deep-void dark mode with cyan/magenta
narrowband-inspired accents and Fraunces serif used sparingly for
display text. Dark-only — no light mode.

### Tokens

```
--bg            #02030a   page background
--bg-elev       #0a0f1c   surface (cards, panels, inputs)
--bg-elev-2     #111a2e   inset surface (sessions inside a target)
--fg            #e9f1ff   default foreground
--fg-mute       #6f8099   secondary text (meta, labels, muted)
--accent        #5eead4   teal — primary action, focus, running
--bad           #ec4899   magenta — danger, failed
--good          #34d399   green — success, exact match
--warn          #fbbf24   amber — approx match, fail %
--border        rgba(94, 234, 212, 0.14)
--border-strong rgba(236, 72, 153, 0.45)
--hairline      rgba(94, 234, 212, 0.06)
--radius        8px       inputs, small surfaces
--radius-card   12px      cards, panels, target rows
```

The page background is layered: `--bg` plus two soft radial gradients
(teal top-left, magenta bottom-right) to give the void a hint of
atmosphere. Don't replace this with a flat color.

### When to use which color

- **accent (teal)**: primary CTAs, focus rings, the running step's
  glow, the modified-params badge, hover-tints. Never on body text.
- **bad (magenta)**: destructive action hover, failed status, high
  failure-percentage, "no calibration match" badges. Never as a
  decorative accent.
- **good (green)**: completed status, `0% failed` (subtle), exact
  calibration match.
- **warn (amber)**: non-zero `% failed` under threshold, approx
  calibration match. Don't use it for "in progress" — that's accent.

---

## Type

Three families, all loaded from Google Fonts in `app.html`:

- **Inter** — body, buttons, meta, labels, status pills. The default.
- **JetBrains Mono** — numbers, paths, hashes, dates, code, status
  pill labels (because they're labels-as-data). Anything that benefits
  from tabular alignment.
- **Fraunces** — display only. **Use sparingly.**

### Where Fraunces goes

Fraunces appears **only** on:

- Page H1 titles (the target/project name on a detail page)
- Target names in the library list ("Andromeda Galaxy")
- Project names in the projects list ("Triangulum Galaxy")
- Job-target links in the jobs table
- The brand "astrolab" wordmark, italic

Set `font-weight: 500` and `letter-spacing: -0.01em`. The 500 weight
of Fraunces has good rhythm with Inter at the body sizes.

### Where Fraunces does NOT go

- Buttons (any kind)
- Body paragraphs
- Form labels and inputs
- Status pills and badges
- Section sub-headings (use Inter, uppercase, letter-spaced)
- Numbers (use JetBrains Mono)

If you find yourself reaching for Fraunces somewhere outside the
list above, the right answer is almost always Inter.

### Numbers are mono

Frame counts, sizes (`123 GiB`), versions (`v14`), durations
(`3m 45s`), percentages — all JetBrains Mono with
`font-variant-numeric: tabular-nums`. This makes them column-scan
across rows.

---

## Copy and naming

### Use friendly names with the catalog code muted

Targets have both a friendly name (Triangulum Galaxy) and a catalog
designation (M 33). **Always lead with the friendly name; show the
catalog code muted alongside or below.**

```
Triangulum Galaxy  M 33      ← friendly name primary, M 33 muted
```

Apply this in:
- Library target cards
- Project cards
- Jobs table
- Page H1s (target detail pages)

### No snake_case in the UI

Template ids like `calibrate_register_stack` are internal. The UI
shows display names: "Calibrate · Register · Stack" / "HOO Recombine"
/ etc. Maintain the id → display-name map in a single place
(`$lib/format.ts` or similar) and use it everywhere a template id
would otherwise leak.

Same rule for any other internal id that bleeds into copy.

### Frame counts: total + percentage failed

Don't use fractions like `108 / 111 frames`. Show the total and a
rounded failure percentage:

```
185 frames · 2% failed
79 frames · 0% failed
1,167 frames · 65% failed     ← high — uses fail-high color
```

**Round to whole percent.** Color treatment:

- `0% failed` → `--good`, slightly faded (positive but not loud)
- regular non-zero → `--warn`
- ≥ 25% (configurable) → `--bad` with `font-weight: 500`

Class hooks: `.fail-pct`, `.fail-pct.fail-zero`, `.fail-pct.fail-high`.

### Capitalization

- Section sub-headings (above a card group): UPPERCASE, letter-spaced
  0.08em, in `--fg-mute`.
- Status pill labels: lowercase.
- Friendly target names: title case as the catalog has them.
- Buttons: sentence case ("Run pipeline", not "Run Pipeline").

---

## Components

### Buttons

```
.btn         neutral pill, surface bg, hairline border
.btn.primary teal gradient, dark ink text, soft glow, lift on hover
.btn.ghost   transparent bg
.btn.danger  bad-color text, hairline border, magenta border on hover
```

Primary buttons have a `linear-gradient(135deg, accent, #34d399)`
background and a subtle outer glow. They're the "do the thing" CTA;
use one per panel/section at most.

Pill radius (`999px`) for buttons. Inputs use `--radius` (8px).

### Cards / rows

Three densities:

- **Target card**: `--pad-card` (1rem 1.15rem), `--radius-card` (12px),
  `--shadow` (subtle multi-layer). Hover: lift `translateY(-1px)` and
  `--border-strong`.
- **Session inside a target**: `--bg-elev-2`, hairline border, gap from
  parent.
- **Project card**: `--radius-card`, picture first. Cards sit in a grid
  (`minmax(300px, 1fr)`), so a phone shows one full-width card.

#### Project card layout

```
[ picture, 4:3, cover          v14 ]
[                                  ]
[ Name (serif)  NGC 2238           ]   <- over a dark gradient
[ 45m · 2 sessions · Nov 12–14 · HaOIII ]
[ Duo-band, per night · edited 6h ago   ]
[ 208 GiB +3 GiB          broom  trash ]
```

- The picture is the current job's preview (`preview_hash`/`preview_port`
  from `/api/projects`). Without one the frame stays empty with "No render
  yet" (or "Rendering…" while a job runs): never a placeholder gradient or
  synthesized art. A running job draws a 3px progress bar on the
  picture's bottom edge.
- One line of facts: integration time (strong), sessions, the capture
  dates, the filter badge. Frames, exposure, gain, fail % and source
  bytes stay off the card (they are on the project page and in the facts
  line's tooltip).
- The recipe uses the short template name (`templateShortName`).
- Footer: the cache the project owns, then its actions, because the
  actions act on it ("how much will I free?").

### Editor

The Edit tab is its own full-screen app (`ui/src/lib/editor/`): one viewport,
no page scroll. Phones and tablets in portrait stack it; tablets in landscape
and desktops put the tools in a column to the right of the picture.

```
[ ‹  Name / catalog · status      Edit Pipeline Details  ↶ ↷  ⋯  Export ]
[ picture (contain), Compare pill, Rendering pill        | tool panel     ]
[                                                         | Looks Crop ... ]
```

- Tools: Looks (palette and style cards rendered on your own picture),
  Crop, Light, Colour, Detail, Versions. One tool's few controls show at a
  time; the panel scrolls inside itself only if it must.
- Controls run 0-100 (or -100..100 for contrast, highlights, shadows,
  balance), each with an icon, its value and a reset to the recipe's value.
  Controls the instant preview can't show carry a small RENDER tag.
- The picture is the instant preview (`/api/projects/{id}/quick-preview`).
  Compare splits it against how the session started; holding the picture
  shows the start.
- Edits in one sitting make one version; "Keep as a version" starts a new
  one. Undo and redo walk the session's edits.

### Pipeline strip card

16:9 aspect, `--radius-card` corners. Background is the live preview
image (or a placeholder gradient). The step name and status pill
overlay the image with a top-down dark gradient. The running step
has a breathing teal glow (`flow-pulse` 2.4s) and the progress bar
shimmers (`flow-shimmer` 1.6s).

### Inputs

Surface bg, hairline border, `--radius`. On focus, border becomes
`--accent` and an `accent-soft` glow ring sits behind it. Range
inputs use `accent-color: var(--accent);`.

### Status pills

`font-family: var(--font-mono)`, `font-size: 0.7rem`, uppercase,
letter-spaced. Mini variant (`.status-mini`) sits in the pipeline
strip overlay.

### Calibration badges (D/F/B)

24px circular outlined chips, `currentColor` for stroke and a 12%
tint for fill. `--good` for exact, `--warn` for approx, `--bad` for
none. Mono numerals.

### Filter chips (Astro / Duo-Band / etc.)

Outlined pill, `currentColor` border, 10–12% color fill. Distinct
color per filter family — broadband filters get a cool steel-blue,
narrowband (Duo-Band, Ha/OIII) get magenta. New filter? Add a
chip class, don't reuse an existing color.

---

## Iconography

Icons are **inline SVG**, stroke-only, `currentColor`. No emoji
anywhere except where the user explicitly types one. No icon font.

Conventions for SVG:
- 24×24 viewBox, rendered at 16px in icon buttons.
- `stroke-width="1.6"`, `stroke-linecap="round"`,
  `stroke-linejoin="round"`, `fill="none"`.
- Always include `aria-label` and `title` on the button (not the SVG).
- `aria-hidden="true"` on the SVG itself.

Established icons:
- **Free intermediates** (`.action-btn` with broom): a tilted broom —
  diagonal handle from top-right, fanned bristle wedge at bottom-left.
- **Delete** (`.action-btn.danger` with trash): standard trash can
  with two inner verticals.

Icon buttons get a small motion on hover: broom does a `rotate(-6deg)`
wiggle; danger icons do a `translateY(-1px)` lift. 180ms ease.

If you add a new icon, draw it from scratch as a stroke shape — don't
paste rasterized paths. Match the line weight and round-cap style.

---

## Animation

Movement should feel inviting, never distracting. All animations
respect `prefers-reduced-motion: reduce`.

### Patterns in use

- **Theme cross-fade** — when the theme switches, surfaces / borders /
  text colors transition (320ms / 220ms ease). The page doesn't snap.
- **Rise-in stagger** — list rows (`.prow`, `.jobs tbody tr`)
  fade-and-rise 6px on mount; delay = `var(--stagger) * 60ms + 80ms`.
  Every list that supports staggering should set the inline `--stagger`
  index per row.
- **Hover lift** — interactive cards (`.target`, `.prow`) do
  `translateY(-1px)` plus border tightening on hover (180ms).
- **Thumb saturate** — project thumbnail scales `1.04` and gets
  `saturate(1.15) brightness(1.05)` on row hover; isolated to the
  thumb so the rest of the row doesn't shimmy.
- **Breathing glow** — `.flow-card.flow-running` pulses its
  box-shadow on a 2.4s ease-in-out cycle.
- **Progress shimmer** — the flow progress bar has a moving sheen
  (`flow-shimmer`, 1.6s linear).
- **Button press** — primary buttons lift on hover, return to baseline
  on `:active`. Cheap and satisfying.

### Don't

- Don't spin loaders. Use the breathing glow + shimmer. Spinners
  steal attention; this app has long-running jobs and we don't want
  every step to feel emergency-room.
- Don't bounce. No springs. Use `cubic-bezier(0.2, 0.8, 0.2, 1)` for
  most transitions; ease-in-out for the breathing pulse.
- Don't animate page transitions globally. Stagger rows, lift cards,
  cross-fade themes — that's enough.

---

## Layout

- `.shell` max-width is **1080px** for most pages. The project detail
  page gets more (it's already wide). Don't go narrower than 980px on
  desktop; the old 980px container left dead space.
- Pad: `2rem 1.5rem 5rem` on desktop. Phone padding inherits.
- Phone-first: every component should reflow cleanly under 600px.

### Section sub-heads

Small uppercase muted label above a block (`PROJECT ROW`, `JOBS TABLE`,
etc.). Lives in the page itself, not in the component.

### Hierarchy

A page has at most one H1 (the page title, in Fraunces if it's a
named entity). Below that, sub-heads are uppercase Inter labels in
`--fg-mute`. Don't promote a sub-head to a serif heading.

---

## Where things go

This list is what the playground is wrestling toward. Treat it as a
spec, not a proposal.

- **Library page** is for browsing existing data. The capture-root
  text input belongs in **Settings**, not here. The library top has a
  small refresh icon button + muted "scanned Xh ago" relative time.
- **Settings page** owns: capture root (and a "Scan now" primary
  button next to it), cache budget, cache root display, anything else
  configuration-shaped.
- **Projects page** is a grid of project cards (see Components above).
- **Project detail page** has three tabs: Edit (the editor, below), Pipeline (every step) and Details (notes, sessions, what more data would buy). `?view=` keeps the tab.
- **Jobs page** is a thin debugging table; not a primary surface.

### Header bar

The top of every primary page is a slim header with:
- The brand wordmark (`astrolab`, italic Fraunces, gradient fill)
- The nav (Library / Projects / Settings)
- Page-specific affordances on the right (refresh icon, project count,
  storage pill, etc.)

Drop the "Phase 3" pill and the muted `Jobs` link in the nav. Jobs
remains reachable from project detail / direct URL but doesn't get
top-nav real estate.

---

## Reference: the playground

`/ui/src/routes/playground/themes/+page.svelte` is the live reference.
Open it (`vite dev` then visit `/playground/themes`) and pick the
**Astrolab** tab — that's the canonical theme. The other tabs are
explorations kept for comparison; don't ship them.

When you change a token, copy, or pattern in the real app, check it
against the playground. If the playground disagrees, update one or
the other so they stay in sync.
