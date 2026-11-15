"""Duo-band masters: the pack step and the template choice."""

from __future__ import annotations

import numpy as np
import pytest

import nodes.basic  # noqa: F401  registers node kinds
from nodes.basic.narrowband_extract import copy_registration
from nodes.basic.narrowband_pack import measure_shift, pack, shift
from server.masters import DUO_TEMPLATE_ID, TEMPLATE_ID, template_for
from server.templates import load_template


def test_pack_puts_ha_in_red_and_oiii_in_green_and_blue() -> None:
    ha = np.full((4, 5), 0.2, np.float32)
    oiii = np.full((5, 5), 0.1, np.float32)
    out, used = pack(ha, oiii, align=False)
    assert used == (0.0, 0.0)
    assert out.shape == (3, 4, 5)
    assert (out[0] == 0.2).all() and (out[1] == 0.1).all() and (out[2] == 0.1).all()


def test_duo_filters_get_the_split_template() -> None:
    assert template_for("HaOIII") == DUO_TEMPLATE_ID
    assert template_for("Duo") == DUO_TEMPLATE_ID
    assert template_for("Astro") == TEMPLATE_ID
    assert template_for(None) == TEMPLATE_ID


def test_duo_template_splits_before_debayer_and_shares_finishing() -> None:
    duo = load_template(DUO_TEMPLATE_ID)
    plain = load_template(TEMPLATE_ID)
    nodes = {n.id: n for n in duo.nodes}
    assert nodes["calibrate"].params["debayer"] is False
    # The pedestal: dark subtraction leaves CFA frames below zero.
    assert nodes["offset"].params["pedestal"] > 0
    assert nodes["extract"].inputs == {"sequence": "offset.sequence"}
    assert nodes["extract"].params["shared_registration"] is True
    assert nodes["combine"].inputs == {"images": "pack.image"}
    assert nodes["channel_map"].params["enabled"] is True
    assert nodes["color_balance"].params["enabled"] is False
    # Finishing node ids match, so a master's edits carry across the switch.
    finishing = [n.id for n in plain.nodes if not n.per_session]
    assert [n.id for n in duo.nodes if not n.per_session] == finishing


def test_pack_moves_oiii_onto_ha_by_a_subpixel_offset() -> None:
    rng = np.random.default_rng(1)
    h, w = 300, 400
    ha = rng.normal(0.01, 0.0005, (h, w)).astype(np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    for y, x in rng.uniform(40, 260, (60, 2)):
        ha += 0.2 * np.exp(-((yy - y) ** 2 + (xx - x * 1.3) ** 2) / (2 * 1.8**2)).astype(np.float32)
    oiii = shift(ha, -0.3, 1.4)  # OIII sits 1.4 px right, 0.3 px up
    assert measure_shift(ha, oiii) == pytest.approx((0.3, -1.4), abs=0.1)
    rgb, used = pack(ha, oiii)
    assert used == pytest.approx((0.3, -1.4), abs=0.1)
    assert max(abs(v) for v in measure_shift(rgb[0], rgb[1])) < 0.1


def test_copy_registration_renames_and_drops_stats(tmp_path) -> None:
    src = tmp_path / "OIII_pp_light_.seq"
    src.write_text(
        "#Siril sequence file\n"
        "S 'OIII_pp_light_' 1 3 2 5 1 6 0 0 0\n"
        "L 1\nI 1 1\nI 2 0\nI 3 1\n"
        "R0 7 18 0.68 0 0.007 43 H 1 0 43 0 1 47 0 0 1\n"
        "M0-0 1 2 3\n"
    )
    dst = tmp_path / "Ha_pp_light_.seq"
    copy_registration(src, dst, "Ha_pp_light_")
    lines = dst.read_text().splitlines()
    assert "S 'Ha_pp_light_' 1 3 2 5 1 6 0 0 0" in lines
    assert "I 2 0" in lines and any(x.startswith("R0 7 18") for x in lines)
    assert not any(x.startswith("M") for x in lines)
