"""Minimal headless sirilpy spike.

Goal: prove that `siril-cli -s` -> `pyscript` -> sirilpy.connect() round-trips
on macOS without a GUI, so the astrolab pipeline runtime can drive Siril
from out-of-process Python.
"""

import os
import sys
import json
import traceback


def main() -> int:
    print(f"[spike] python {sys.version.split()[0]}", flush=True)
    print(f"[spike] cwd: {os.getcwd()}", flush=True)
    print(f"[spike] MY_SOCKET={os.environ.get('MY_SOCKET')!r}", flush=True)
    print(f"[spike] SIRIL_PYTHON_CLI={os.environ.get('SIRIL_PYTHON_CLI')!r}", flush=True)

    try:
        import sirilpy
    except Exception as exc:
        print(f"[spike] FAIL: import sirilpy: {exc}", flush=True)
        traceback.print_exc()
        return 2

    print(f"[spike] sirilpy module: {sirilpy.__file__}", flush=True)

    try:
        siril = sirilpy.SirilInterface()
        connected = siril.connect()
    except Exception as exc:
        print(f"[spike] FAIL: SirilInterface()/connect(): {exc}", flush=True)
        traceback.print_exc()
        return 3

    print(f"[spike] connected={connected}", flush=True)

    try:
        siril.cmd("requires", "1.4.0")
    except Exception as exc:
        print(f"[spike] FAIL: cmd('requires'): {exc}", flush=True)
        traceback.print_exc()
        return 4

    facts = {}
    for getter in ("get_siril_version", "get_siril_wd", "get_image_filename"):
        fn = getattr(siril, getter, None)
        if not callable(fn):
            facts[getter] = "<no such method>"
            continue
        try:
            facts[getter] = fn()
        except Exception as exc:
            facts[getter] = f"<error: {exc!r}>"

    print(f"[spike] facts={json.dumps(facts, default=str)}", flush=True)

    try:
        siril.disconnect()
    except Exception as exc:
        print(f"[spike] WARN: disconnect: {exc}", flush=True)

    print("[spike] OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
