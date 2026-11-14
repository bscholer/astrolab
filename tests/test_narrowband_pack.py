"""Duo-band masters: the pack step and the template choice."""

from __future__ import annotations

import numpy as np

import nodes.basic  # noqa: F401  registers node kinds
from nodes.basic.narrowband_pack import pack
from server.masters import DUO_TEMPLATE_ID, TEMPLATE_ID, template_for
from server.templates import load_template


def test_pack_puts_ha_in_red_and_oiii_in_green_and_blue() -> None:
    ha = np.full((4, 5), 0.2, np.float32)
    oiii = np.full((5, 5), 0.1, np.float32)
    out = pack(ha, oiii)
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
    assert nodes["combine"].inputs == {"images": "pack.image"}
    assert nodes["channel_map"].params["enabled"] is True
    assert nodes["color_balance"].params["enabled"] is False
    # Finishing node ids match, so a master's edits carry across the switch.
    finishing = [n.id for n in plain.nodes if not n.per_session]
    assert [n.id for n in duo.nodes if not n.per_session] == finishing
