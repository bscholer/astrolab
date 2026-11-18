"""The whole-night reference planner never does worse than greedy."""

from experiments.bend_planner import night


def test_best_plan_beats_or_matches_greedy_and_emits_bend():
    loc, start, targets, n_slots = night.demo_night(3, 6)
    alt = night.alt_table(loc, start, targets, n_slots)
    gain = night.gain_table(n_slots)
    assert night.best_plan(alt, gain) >= night.greedy(alt, gain) > 0
    src = night.emit_bend(alt, gain)
    assert "def main()" in src and src.count("[") > n_slots
