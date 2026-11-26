"""Write docs/openapi.json and docs/mcp-tools.json from the live app.

Usage: python -m scripts.export_spec [--check]
--check exits 1 when the committed files differ from the generated ones.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from server.api import app
from server.endpoints import tool_manifest

DOCS = Path(__file__).resolve().parent.parent / "docs"


def generated() -> dict[str, str]:
    def dump(obj: object) -> str:
        return json.dumps(obj, indent=2, sort_keys=True) + "\n"

    return {
        "openapi.json": dump(app.openapi()),
        "mcp-tools.json": dump(tool_manifest()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, text in generated().items():
        path = DOCS / name
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(name)
        else:
            path.write_text(text)
    if stale:
        print(f"stale: {', '.join(stale)}. Run: python -m scripts.export_spec", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
