# Third-party software bundled in the astrolab Docker image

astrolab shells out to several external programs to do its heaviest lifting.
This file credits the teams behind those tools and explains exactly how they're
used. Thank you for making your work available.

---

## Siril

**What astrolab uses it for:** Siril is the core image-processing engine.
astrolab calls `siril-cli` in headless mode to calibrate frames (bias, dark,
flat subtraction), register and align light sequences, plate-solve, and stack
into a master light. Without Siril, the pipeline cannot produce a stacked image.

**Project:** https://siril.org  
**Source:** https://gitlab.com/free-astro/siril  
**License:** GNU General Public License v3 (GPLv3)  
**Primary maintainer:** Cyril Richard and the Free Astronomy Software team

The Siril AppImage (extracted to `/opt/siril` in the container) is downloaded
unmodified from the official free-astro.org release page. astrolab invokes it as a
separate subprocess; no Siril source code is linked into astrolab.

Thank you to Cyril Richard, Cecile Melis, and everyone who has contributed to
Siril over the years. It is one of the few open-source tools that genuinely
matches commercial astrophotography software in what it can do.

---

## GraXpert

**What astrolab uses it for:** GraXpert handles background gradient extraction
(removing vignetting, sky gradients, and light pollution gradients from stacked
images) and ML-based denoise. It runs as a separate subprocess; astrolab passes
it a FITS file and parameters, then reads the result back.

**Project:** https://www.graxpert.com  
**Source:** https://github.com/Steffenhir/GraXpert  
**License:** GNU General Public License v3 (GPLv3)  
**Author:** Steffen Hirtle

The GraXpert binary (`graxpert-linux-amd64.zip`) is downloaded unmodified from
the official GitHub releases page. No GraXpert source code is linked into
astrolab.

Thank you to Steffen Hirtle for open-sourcing GraXpert and for maintaining a
stable CLI interface that tools like astrolab can rely on.

---

## StarNet++ v2

**What astrolab uses it for:** StarNet++ separates stars from the nebula/galaxy
background in a stacked image. astrolab's star-removal node calls the
`starnet++` binary with an input FITS and reads the star-free result, which can
then be recombined with the original at adjustable opacity.

**Project:** https://www.starnetastro.com  
**Author:** Nikita Misiura  
**License:** Proprietary freeware (free for personal, non-commercial use)

StarNet++ is **not** baked into the image by default. It is fetched at first
run only when you pass `ASTROLAB_ENABLE_STARNET=1` to the container. The binary
is downloaded from the author's own distribution URL
(`https://starnetastro.com/wp-content/uploads/2022/03/StarNetv2CLI_linux.zip`),
placed in the mounted `/data/tools/starnet/` directory, and is therefore subject
to the author's own terms of use. Please read them at
https://www.starnetastro.com before enabling this feature.

Thank you to Nikita Misiura for making StarNet++ freely available. Star removal
used to require fiddly manual masking; StarNet++ makes it a one-click operation
for any target.

---

## Naztronomy's Smart Telescope Preprocessing Script

**What astrolab uses it for:** The `calibrate_register_stack` template is shaped directly around the pipeline Naztronomy worked out in this script. The stage sequence -- convert, calibrate (CFA-aware, debayer), background extract, plate-solve, register, rejection stack -- is the same pipeline, in the same order, for the same reason: it's what actually produces clean stacks from smart-telescope OSC data. astrolab does not include any of Naz's Python source. What it took is the recipe: the understanding of which Siril commands to call, in which order, with which sequencing rationale. That recipe was independently reimplemented as a set of cacheable graph nodes in astrolab's DAG runner.

**Script:** https://github.com/naztronaut/siril-scripts/blob/main/Naztronomy-Smart_Telescope_PP.py  
**Project:** https://github.com/naztronaut/siril-scripts  
**Author:** Nazmus Nasir (Naztronomy) -- https://www.naztronomy.com  
**License:** GNU General Public License v3 or later (GPL-3.0-or-later); copyright (c) Nazmus Nasir 2025

When astrolab was taking shape, the hardest design question wasn't the caching or the DAG -- it was "what is the correct headless Siril command sequence for OSC smart-scope data?" Naz's script is the cleanest publicly available answer to that question. It's thoroughly commented, covers the edge cases you only learn by actually shooting with a Dwarf or Seestar, and it was written for the same goal: get good stacks out of data that didn't come from a "real" equatorial mount. Finding it saved a lot of trial-and-error spelunking through Siril's command documentation. astrolab's pipeline wouldn't look the way it does without it.

Thank you, Naz.

---

## OpenNGC catalog

**What astrolab uses it for:** Target name resolution and sky coordinate lookup.
The catalog is vendored under `catalogs/openngc/` as CSV files.

**Source:** https://github.com/mattiaverga/OpenNGC  
**Author:** Mattia Verga  
**License:** CC BY-SA 4.0

This catalog is covered by a separate license carve-out in astrolab's own
`LICENSE` file.
