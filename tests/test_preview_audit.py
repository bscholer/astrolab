"""Per-node preview audit.

This file exists because narrowband_extract was shipping a never-resolving
preview spinner in the UI. Two cooperating problems landed it:

  1. The UI's `pickPreviewPort` guessed `image` for every node not on a
     hard-coded list, so for narrowband_extract (`ha`, `oiii`),
     starnet_extract (`starless`, `stars`), and the seq_* nodes that emit
     `sequence`, the preview request 404'd and the IMG element hung in
     loading state.
  2. The server's `_locate_artifact` (the no-manifest fallback) only knew
     about `<port>.<ext>` exact matches, so legacy entries written by
     narrowband_extract (`r_results_ha.fit`) would miss even when the
     correct port was requested.

Tests below pin both behaviours: the registry's declared output ports must
be discoverable end-to-end (via manifest OR the broadened locator), and the
locator's fallback patterns specifically cover the on-disk naming each
basic node uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

import nodes.basic  # noqa: F401  registers every basic node on import
from server.cache import DONE_MARKER, OUTPUTS_MANIFEST, ContentCache
from server.preview import PreviewError, _locate_artifact, render_preview
from server.registry import all_kinds
from server.registry import lookup as registry_lookup

# Map (kind, variant) -> {port: filename_or_dir} of what the node actually
# writes inside its cache entry. Sourced by reading each node's run()
# method. When a node grows a new on-disk shape, add it here and the
# parametrized tests will catch any locator regression.
NODE_LAYOUT: dict[tuple[str, str | None], dict[str, str]] = {
    ("auto_bp_shift", None): {"image": "image.fit"},
    ("calibrate", None): {"sequence": "sequence/"},
    ("color_balance", None): {"image": "image.fit"},
    ("convert_lights", None): {"sequence": "sequence/"},
    ("crop", None): {"image": "image.fit"},
    ("downscale", None): {"image": "image.png"},
    ("graxpert", None): {"image": "image.fit"},
    ("narrowband_compose", None): {"image": "image.fit"},
    ("narrowband_extract", None): {
        "ha": "r_results_ha.fit",
        "oiii": "r_results_oiii.fit",
    },
    ("save_image", None): {"image": "image.png"},
    ("seq_bg_extract", None): {"sequence": "sequence/"},
    ("seq_offset", None): {"sequence": "sequence/"},
    ("seq_register", None): {"sequence": "sequence/"},
    ("seq_resample", None): {"sequence": "sequence/"},
    ("seq_stack", None): {"image": "image.fit"},
    ("starnet_extract", None): {
        "starless": "starless.fit",
        "stars": "stars.fit",
    },
    ("starnet_recombine", None): {"image": "image.fit"},
    ("starnet_replace", None): {"image": "image.fit"},
    ("stretch", None): {"image": "image.fit"},
}


def _make_fits(path: Path, shape: tuple[int, int] = (32, 32), seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    data = rng.normal(loc=1000.0, scale=50.0, size=shape).astype(np.float32)
    data[shape[0] // 2, shape[1] // 2] = 5000.0
    fits.PrimaryHDU(data=data).writeto(path, overwrite=True)


def _make_png(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (32, 32), (12, 34, 56)).save(path)


def _materialize_artifact(entry: Path, rel_target: str) -> None:
    """Create a plausible artifact at `entry / rel_target`.

    rel_target is either:
      - "name/" for a sequence-style directory (gets a tiny FITS frame so the
        preview renderer's "pick middle frame" path has something to chew on),
      - "name.fit" / "name.fits" for a FITS file, or
      - "name.png" for a passthrough PNG.
    """
    target = entry / rel_target.rstrip("/")
    if rel_target.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
        _make_fits(target / "r_pp_light_00001.fit")
        return
    if target.suffix.lower() == ".png":
        _make_png(target)
    else:
        _make_fits(target)


def test_node_layout_covers_every_registered_node() -> None:
    """Audit guardrail: when a new node lands, force this table to be
    updated so the locator regression test below actually exercises it.

    Test-only kinds (`__test_*__`) are excluded; they register transiently
    while another test module is loaded and shouldn't drag the audit table
    around."""
    real_registered = {(k, v) for k, v in all_kinds() if not k.startswith("__")}
    catalogued = set(NODE_LAYOUT)
    missing = real_registered - catalogued
    extra = catalogued - real_registered
    assert not missing, (
        f"NODE_LAYOUT missing entries for newly-registered nodes: "
        f"{sorted(missing)}. Add the (kind, variant) -> {{port: filename}} "
        f"mapping so the preview audit covers them."
    )
    assert not extra, (
        f"NODE_LAYOUT has stale entries for unknown (kind, variant) pairs: "
        f"{sorted(extra)}. Remove them."
    )


@pytest.mark.parametrize("key", sorted(NODE_LAYOUT))
def test_locate_artifact_finds_each_node_output(
    tmp_path: Path, key: tuple[str, str | None]
) -> None:
    """For every node, build a cache entry shaped the way the node actually
    writes (no manifest), then assert `_locate_artifact` finds every
    declared output port.

    Catches both the historical convention-based hits (`image.fit`) and
    the port-named-by-suffix case the narrowband_extract bug exposed
    (`r_results_ha.fit` for port `ha`)."""
    kind, variant = key
    cls = registry_lookup(kind, variant)
    declared_ports = set(cls.outputs)
    catalogued_ports = set(NODE_LAYOUT[key])
    assert declared_ports == catalogued_ports, (
        f"{kind}: declared outputs {declared_ports} drifted from NODE_LAYOUT "
        f"{catalogued_ports}; update the test table."
    )

    cache = ContentCache(root=tmp_path / "cache")
    entry = cache.reserve(f"hash_{kind}")
    for rel in NODE_LAYOUT[key].values():
        _materialize_artifact(entry, rel)
    (entry / DONE_MARKER).touch()

    for port in declared_ports:
        located = _locate_artifact(entry, port)
        assert located is not None, (
            f"{kind}: _locate_artifact returned None for port '{port}'. "
            f"Entry contents: {sorted(p.name for p in entry.iterdir())}"
        )


def test_render_preview_uses_manifest_for_narrowband_extract(tmp_path: Path) -> None:
    """The committed manifest is the authoritative port->file map; even when
    the on-disk name doesn't match the port (narrowband_extract writes
    `r_results_ha.fit` for port `ha`), the preview must find it."""
    cache = ContentCache(root=tmp_path / "cache")
    entry = cache.reserve("h_nb")
    _make_fits(entry / "r_results_ha.fit", seed=1)
    _make_fits(entry / "r_results_oiii.fit", seed=2)
    (entry / OUTPUTS_MANIFEST).write_text(
        json.dumps(
            {
                "ha": {"path": "r_results_ha.fit", "type": "image/fits"},
                "oiii": {"path": "r_results_oiii.fit", "type": "image/fits"},
            }
        )
    )
    (entry / DONE_MARKER).touch()

    ha_png = render_preview(cache, "h_nb", "ha")
    oiii_png = render_preview(cache, "h_nb", "oiii")
    assert ha_png.exists() and oiii_png.exists()
    assert ha_png != oiii_png  # distinct outputs, distinct previews


def test_locate_artifact_finds_port_suffixed_legacy_filenames(tmp_path: Path) -> None:
    """Legacy narrowband_extract entries (pre-manifest) have files named
    `r_results_<port>.fit` with no _outputs.json. The fallback locator
    must still resolve them via the port-suffix pattern, otherwise the
    UI gets a 404 and spins indefinitely."""
    cache = ContentCache(root=tmp_path / "cache")
    entry = cache.reserve("h_legacy")
    _make_fits(entry / "r_results_ha.fit")
    _make_fits(entry / "r_results_oiii.fit")
    (entry / DONE_MARKER).touch()

    # No manifest written: simulates an entry committed before _outputs.json
    # was introduced.
    assert not (entry / OUTPUTS_MANIFEST).exists()

    ha = _locate_artifact(entry, "ha")
    oiii = _locate_artifact(entry, "oiii")
    assert ha is not None and ha.name == "r_results_ha.fit"
    assert oiii is not None and oiii.name == "r_results_oiii.fit"


def test_locate_artifact_skips_preview_artifacts(tmp_path: Path) -> None:
    """The cached `_preview_<port>.png` and the `_done` marker live in the
    same dir as the real outputs; the locator must skip both so a request
    for port `ha` doesn't accidentally return `_preview_ha.png` when the
    real artifact happens to be missing."""
    cache = ContentCache(root=tmp_path / "cache")
    entry = cache.reserve("h_preview")
    # Only a stray preview file + the done marker — no real output yet.
    (entry / "_preview_ha.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (entry / DONE_MARKER).touch()
    assert _locate_artifact(entry, "ha") is None


def test_locate_artifact_prefers_directory_over_file(tmp_path: Path) -> None:
    """Sequence outputs live as `<port>/...`. If both a directory and a
    same-named file existed (shouldn't but just in case), the directory
    wins so the sequence preview path engages."""
    cache = ContentCache(root=tmp_path / "cache")
    entry = cache.reserve("h_seq")
    (entry / "sequence").mkdir()
    _make_fits(entry / "sequence" / "r_pp_light_00001.fit")
    # Stray file with the same stem; locator must prefer the directory.
    _make_fits(entry / "sequence.fit")
    (entry / DONE_MARKER).touch()

    located = _locate_artifact(entry, "sequence")
    assert located is not None
    assert located.is_dir()


def test_unknown_port_still_returns_none(tmp_path: Path) -> None:
    """Sanity: locator stays strict for ports that don't match anything.
    A bogus port should not silently grab whichever file sorts first."""
    cache = ContentCache(root=tmp_path / "cache")
    entry = cache.reserve("h_strict")
    _make_fits(entry / "image.fit")
    (entry / DONE_MARKER).touch()
    assert _locate_artifact(entry, "definitely_not_a_port") is None
    with pytest.raises(PreviewError, match="not found"):
        render_preview(cache, "h_strict", "definitely_not_a_port")
