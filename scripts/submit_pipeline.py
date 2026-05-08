"""Submit the convert -> calibrate -> register -> stack pipeline via the API.

Run this against a live FastAPI server (eg `uvicorn server.api:app`) and then
open the UI at http://<host>:5173/jobs/<id> to watch the DAG turn green in
real time.

Usage:
  python scripts/submit_pipeline.py [<api_base>]

Defaults to http://127.0.0.1:8000 if no base is given.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

API_BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

src_session = Path(
    "/home/bscholer/Pictures/Siril/"
    "DWARF_RAW_TELE_NGC 7380_EXP_30_GAIN_60_2025-10-21-20-05-06-361"
)
master_dark = Path(
    "/home/bscholer/Pictures/Siril/CALI_FRAME/dark/cam_0/"
    "dark_exp_30.000000_gain_60_bin_1_34C_stack_10.fits"
)

# Stage a small subset under ~/.cache/ so this stays a smoke test.
import shutil  # noqa: E402

staging = Path.home() / ".cache" / "astrolab-ui-pipeline"
if staging.exists():
    shutil.rmtree(staging)
staging.mkdir(parents=True)
fits = sorted(p for p in src_session.iterdir() if p.suffix == ".fits"
              and not p.name.startswith(("stacked-", "img_")))
for f in fits[:10]:
    shutil.copy2(f, staging / f.name)

template = {
    "id": "ui_pipeline_smoke",
    "version": 1,
    "description": "convert + calibrate + register + stack (10 frames)",
    "nodes": [
        {"id": "convert", "kind": "convert_lights",
         "params": {"basename": "light", "fitseq": False}},
        {"id": "calibrate", "kind": "calibrate",
         "params": {"input_basename": "light", "fitseq": False, "cosmetic": True},
         "inputs": {"sequence": "convert.sequence"}},
        {"id": "register", "kind": "seq_register",
         "params": {"input_basename": "pp_light", "fitseq": False},
         "inputs": {"sequence": "calibrate.sequence"}},
        {"id": "stack", "kind": "seq_stack",
         "params": {"input_basename": "r_pp_light", "fitseq": False, "method": "rej"},
         "inputs": {"sequence": "register.sequence"}},
    ],
    "outputs": {"image": "stack.image"},
}
job = {
    "template_id": "ui_pipeline_smoke",
    "template_version": 1,
    "inputs": {
        "convert.lights": {
            "node_hash": "ext", "port": "lights",
            "path": str(staging), "type": "sequence/fits",
        },
        "calibrate.dark": {
            "node_hash": "ext", "port": "dark",
            "path": str(master_dark), "type": "master/fits",
        },
    },
}

req = urllib.request.Request(
    f"{API_BASE}/api/jobs",
    data=json.dumps({"template": template, "job": job}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    body = json.loads(resp.read())

print(f"submitted: {body['job_id']}")
print(f"open: {API_BASE.replace(':8000', ':5173')}/jobs/{body['job_id']}")
