#!/bin/zsh
# Runs every PencilPad test (server, real-browser gestures, guided tour) in dry-run mode.
# Nothing is posted to this Mac, and a PencilPad already running on port 8765 is left
# alone: each test starts its own server on a free port and stops only that one.
# The first run creates .testvenv here and downloads Chromium for Playwright.
#   ./run_tests.sh                 everything
#   ./run_tests.sh -k tour         only tests whose name matches
cd "${0:A:h}" || exit 1          # (the real folder, even through a link)
source ./find_python.sh
venv_ok .testvenv || rm -f .testvenv/.ready       # made with an older Python, or half made: again
if [ ! -f .testvenv/.ready ]; then
  make_venv .testvenv \
    && .testvenv/bin/pip install -q pytest aiohttp pyobjc-framework-Quartz \
         pyobjc-framework-Cocoa segno cryptography "playwright==1.59.0" \
    && .testvenv/bin/python -m playwright install chromium \
    && touch .testvenv/.ready || exit 1
fi
export PYTHONDONTWRITEBYTECODE=1       # no __pycache__ in the project
exec .testvenv/bin/python -m pytest -p no:cacheprovider -q "$@" tests
