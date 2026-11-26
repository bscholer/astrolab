# astrolab roadmap: the machine does the chores, you make the picture

Written 2026-11-13, after the first week of the scope shooting on its own.

## Where we are

The capture side works. The scope picks targets, shoots all night, copies its frames
here, and the catalog sorts them into sessions. In the last few days we also made the
stacking side dependable: only good frames are stacked, darks, flats and bias match
the scope's new firmware, plate-solving failures fall back to star matching, failed
frames get a second look, scrap sessions are cleaned up, and many nights of one
target can be stacked per night and combined.

What doesn't work yet is the reason the project exists: turning that data into a
picture is still slow and mostly invisible. The node-graph template editor was meant
to make processing flexible, and it didn't feel right. Looking back, nearly every
problem we hit this week was about inputs (which frames, which darks, registration),
and every quality complaint was about finishing (a stretch too dark, a gradient left
in). None of them needed a different pipeline shape. A graph editor puts the
pipeline's shape front and centre, and the shape is the part that almost never changes.

## Principles

1. **Automate the chores, keep the craft.** Sorting frames, judging quality, calibrating,
   registering and stacking are chores: they should happen on their own, overnight,
   with good statistics and no human attention. Finishing is the craft: the
   app should make it fast and fun, and leave the choices to me.
2. **Smart defaults, nothing locked.** Every automatic choice has a sensible default
   picked from what we know (object type, filter, exposure, sky), and every one can be
   overridden. The defaults should be good enough that the first picture of the morning
   is worth looking at.
3. **Two speeds of change.** Finishing edits take seconds and happen all the time.
   Stacking settings (drizzle, rejection, which nights) take minutes to hours and change
   rarely. They live in different places and feel different, but both are reachable.
4. **Quick and deep are the same thing.** "Make it pretty in two minutes" and "explore a
   new workflow" are two views of one editable recipe, not two tools.
5. **Human-made pictures.** AI helps build and test astrolab (the scope MCP is great for
   that). The app itself doesn't edit the picture for me. Deterministic diagnostics
   ("the background is green", "stars are clipped") are fine; an "AI, fix it" button
   is not, at least to start.
6. **Data should tell capture what to do.** Processing knows when a target needs more
   time. That knowledge should flow back into what the scope shoots.

## The shape

```
 capture (auto)          library (auto, overnight)              finishing (you)
 ─────────────           ───────────────────────────            ─────────────────────
 scheduler shoots   ──▶  triage, verdicts, calibration    ──▶   Quick: preset + a few
 mirror copies           per-night stacks (cached)              big sliders
 catalog sorts           target master = all kept nights        Workshop: full step stack,
                         combined, rebuilt when a night         variants, compare,
                         is added or a stacking setting         save your own recipe
                         changes
        ▲                                                                │
        └──────────── "needs ~2 more hours" recommendations ◀────────────┘
```

### Target masters (the overnight part)

Each target, per filter and camera, has a **master**: every kept night stacked on
its own (cached), then combined. This is the stack-per-night pipeline we just built,
made automatic and permanent.

- When a night ends and its sessions are kept, only the new night is stacked; the
  combine is cheap. By morning the master includes last night.
- The master replaces today's auto-stack, which stacks each scope session on its own
  and never joins it with earlier nights.
- Stacking settings belong to the target: drizzle, rejection, framing, frame-quality
  strictness, excluded nights. Changing one queues a rebuild (shown with an estimate,
  say "about 40 minutes, runs tonight") instead of happening inline.
- Frame and night quality are judged by statistics (star count and size, background,
  noise, the triage and verdicts we have now). The app shows the numbers and why a night
  was down-weighted, but doesn't ask a human to pick frames. Excluding a night by hand
  stays possible, as an override.
- Every rebuild records what changed: "+2.1 h from Nov 13, noise -9%".

### Recipes (smart defaults)

A recipe is the finishing steps for a kind of target, picked automatically from what
the catalog knows:

- emission nebula + duo-band filter: background extraction, HOO channel mapping,
  stretch, stars separated and reduced
- galaxy or cluster + broadband: background extraction, color calibration, gentle stretch
- Moon and planets: different stacking altogether (lucky imaging), later

Defaults are computed from the image, not fixed numbers: the stretch aims at a
target background level measured from the data, so the first render isn't black.

Stacking can also have object-type defaults (the Moon needs a different pipeline from
deep sky), so recipe choice happens before stacking as well as after.

### Finishing, two ways

**Quick.** Open the target, see this morning's picture made with the default recipe,
adjust a handful of intent sliders: brightness, background darkness, color strength,
star size, detail. Each slider drives several step parameters underneath. Export. Done
in two minutes.

**Workshop.** The same recipe shown as an ordered stack of steps, each with its own
controls, an on/off switch and live preview on a downscaled linear master:

- add, remove and reorder steps; branches are steps with lanes (stars | starless,
  or one lane per narrowband channel), never a free-form graph
- **variants**: "try this step three ways" renders a contact sheet; pick one
- compare any two versions with a slider; fork a version to try something else
- a **step catalog** for discovering tools, each shown as a before/after on your own image
- save a workshop stack as your own recipe, which then shows up in Quick

Edits are non-destructive and versioned; the content-addressed cache makes variants
and going back cheap. Templates stay as text files for power users, and the graph
becomes a **read-only view** of what actually ran (nights, cache hits, timings), which
is useful for debugging but no longer the place you edit.

### Closing the loop with capture

Noise falls with the square root of integration time, so we can estimate what more
data buys. Each target shows where it stands and what's next: "about 2 more hours would
cut noise by a third", or "diminishing returns, 10 more hours for 10%". A
recommendation can be added to Tonight's list with one click, and the scheduler can
weigh targets by expected improvement per hour instead of fixed goals.

## Roadmap

Each phase is useful by itself.

### Phase 0: dependable data (done this week)

Good-frame stacking, calibration matching for new firmware, plate-solve fallback,
failed-frame triage, scrap purge, results that cache eviction never deletes, stack per
night (closes #15).

### Phase 1: the morning after

- Target masters built and updated overnight from kept nights (stack per night + combine)
- Replace session auto-stack with master updates (#200)
- Morning summary on the home page: what was shot, what each master gained
- Image-based auto stretch so the first render is viewable

### Phase 2: Quick finishing

- Recipes chosen by object type and filter (HOO for emission nebulae, etc.)
- Fast preview engine on the downscaled linear master (numpy and GraXpert, no Siril
  round trip for every slider)
- Intent sliders that map onto recipe steps
- Export full resolution; versions per target

### Phase 3: the Workshop

- Ordered step stack editor with lanes (#159, #12)
- Variants and contact sheets, compare slider, forks
- Step catalog with before/after previews; new steps such as noise reduction,
  sharpening and local contrast (#159), photometric color calibration (#116)
- Save as recipe; retire the graph editor; read-only graph of what ran

### Phase 4: capture loop

- Noise and SNR per master, estimated gain per extra hour
- Recommendations on the target page and in Tonight; one click to queue
- Scheduler priority by expected improvement (with the quality gate, #204)

### Phase 5: stacking settings per target

- Drizzle, rejection, framing, quality strictness, excluded nights
- Rebuild queue with estimates, runs when the scope isn't capturing
- Moon and planetary pipeline (lucky imaging)

## Open questions

- Is a master per (target, filter, camera) right, or should mosaics and different focal
  setups split further?
- How far should Quick's intent sliders go before they hide too much?
- Where do finished pictures live: the target page, a gallery, both?
- Should recommendations ever queue capture on their own, or always wait for a click?
- How much of the Workshop's preview engine can be pure numpy, and what still needs Siril?
