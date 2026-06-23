#!/usr/bin/env bash
# Live integration tests — REAL deps, REAL sockets, REAL MCP protocol, REAL Walrus
# testnet. No robot/keys needed (a local HTTP server emulates the harness; Walrus
# is a public testnet). Contrast with `python3 tests/run_all.py` (hermetic, stubbed).
set -euo pipefail
cd "$(dirname "$0")/../.."

python3 -m venv .venv 2>/dev/null || true
.venv/bin/pip install -q --disable-pip-version-check httpx mcp anthropic Pillow numpy

echo "== live: harness (real httpx round-trip) ==";  .venv/bin/python tests/live/test_live_harness.py
echo "== live: mcp (real protocol → dispatch) ==";   .venv/bin/python tests/live/test_live_mcp.py
echo "== live: track_color (real HSV visual servo) =="; .venv/bin/python tests/live/test_live_track_color.py
echo "== live: navigate (real GPS waypoint controller) =="; .venv/bin/python tests/live/test_live_navigate.py
echo "== live: navstack (fused estimator+pursuit vs bang-bang, noisy sim) =="; .venv/bin/python tests/live/test_live_navstack.py
echo "== live: heading (GPS-course fusion rescues a biased magnetometer) =="; .venv/bin/python tests/live/test_live_heading.py
echo "== live: planner (A* + regulated pursuit routes around obstacle) =="; .venv/bin/python tests/live/test_live_planner.py
echo "== live: dwa (dynamic-window local planner avoids moving obstacle) =="; .venv/bin/python tests/live/test_live_dwa.py
echo "== live: montecarlo (domain-randomized validation of the real nav stack) =="; .venv/bin/python tests/live/test_live_montecarlo.py
echo "== live: ellipsoid (full hard+soft-iron mag calibration, numpy) =="; .venv/bin/python tests/live/test_live_ellipsoid.py
echo "== live: solana (real httpx → emulated clanker5000 sidecar) =="; .venv/bin/python tests/live/test_live_solana.py
echo "== live: walrus (real testnet) ==";            .venv/bin/python tests/live/test_live_walrus.py
echo "ALL LIVE TESTS PASSED"
