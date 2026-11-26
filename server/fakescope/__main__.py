"""python -m server.fakescope [--port 9902]: serve the fake scope over HTTP."""

import argparse

import uvicorn

from server.fakescope.app import create_app

parser = argparse.ArgumentParser(prog="server.fakescope")
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=9902)
args = parser.parse_args()
uvicorn.run(create_app(), host=args.host, port=args.port)
