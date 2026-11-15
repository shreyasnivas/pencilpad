#!/bin/zsh
# Starts PencilPad. Creates the virtualenv on first run and installs anything missing.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv || exit 1
.venv/bin/python -c "import aiohttp, Quartz, AppKit, segno, cryptography" 2>/dev/null \
  || .venv/bin/pip install -q -r requirements.txt || exit 1
exec .venv/bin/python pencilpad.py "$@"
