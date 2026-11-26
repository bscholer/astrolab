"""A simulated DWARF 3 for development: a synthetic yard, weather, and the relay's API.

- scene.py: the yard (what blocks the sky) and the weather, seeded.
- core.py: the scope itself, driven by a real or fake clock.
- render.py: camera frames as JPEG.
- app.py / __main__.py: the relay's HTTP API, for staging.
- client.py: an in-process client with astrolab's ScopeServiceClient interface, for tests.
- sim.py: run astrolab's real scheduler against it for many nights in seconds.
"""
