#!/bin/zsh
# Runs every PencilPad test (server, real-browser gestures, guided tour) in dry-run mode.
# Nothing is posted to this Mac, and a PencilPad already running on port 8765 is left
# alone: each test starts its own server on a free port and stops only that one.
# The first run creates .testvenv here and downloads Chromium for Playwright.
#   ./run_tests.sh                 everything
#   ./run_tests.sh -k tour         only tests whose name matches
cd "$(dirname "$0")" || exit 1
if [ ! -f .testvenv/.ready ]; then
  { [ -x .testvenv/bin/python ] || python3 -m venv .testvenv; } \
    && .testvenv/bin/pip install -q pytest aiohttp pyobjc-framework-Quartz \
         pyobjc-framework-Cocoa segno cryptography "playwright==1.59.0" \
    && .testvenv/bin/python -m playwright install chromium \
    && touch .testvenv/.ready || exit 1
fi
export PYTHONDONTWRITEBYTECODE=1       # no __pycache__ in the project
exec .testvenv/bin/python -m pytest -p no:cacheprovider -q "$@" tests
