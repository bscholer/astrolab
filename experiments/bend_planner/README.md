# Whole-night planner (Bend experiment)

Not wired into the server. `server/scope_plan.py` picks greedily, the best target for
now. This asks what the best whole night would be (half-hour slots, same score as
`pick_target`: noise drop from another hour times sin² altitude) and how far greedy is from it.

    uv run python -m experiments.bend_planner.night              # greedy vs best, in Python
    uv run python -m experiments.bend_planner.night --emit-bend  # also writes night.bend
    bend run-c experiments/bend_planner/night.bend               # should print the same best

`night.py` is the reference and does the astronomy; `planner.bend` is the tree search.
**`planner.bend` has never been run** (no Bend install where it was written), so expect
syntax fixes. Bend's `f24` floats are coarse, so match the Python total to about 3 digits.

Caveats: the real scheduler also keeps the current target (hysteresis) and pays for slews;
neither is modelled here, so the gap overstates what a planner would win in practice.
The search is (targets+1)^slots, so it only fits small nights, which is the Bend part of the fun.
