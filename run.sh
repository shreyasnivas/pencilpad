#!/bin/zsh
# Starts PencilPad. Creates the virtualenv on first run (on Python 3.10 or newer) and
# installs anything missing.
cd "${0:A:h}" || exit 1          # (the real folder, even through a link)
source ./find_python.sh
make_venv .venv || exit 1
.venv/bin/python -c "import aiohttp, Quartz, AppKit, segno, cryptography" 2>/dev/null \
  || .venv/bin/pip install -q -r requirements.txt || exit 1
exec .venv/bin/python pencilpad.py "$@"
