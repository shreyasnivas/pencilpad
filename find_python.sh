# Sourced by run.sh, run_tests.sh and app/build.sh. PencilPad needs Python 3.10 or newer;
# a Mac's own python3 is often 3.9. Not a script to run by itself.
#   find_python      sets PY to the first suitable interpreter, or says what is needed
#   make_venv DIR    makes DIR a virtualenv on a suitable Python (again, if it was older)
# PENCILPAD_PYTHON_DIRS: where to look beyond PATH, separated by colons (the tests change it).

python_ok() { [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }
# a virtualenv that is whole: a suitable Python, and pip in it (a half-made one has none)
venv_ok() { python_ok "$1/bin/python" && "$1/bin/python" -m pip --version >/dev/null 2>&1; }

find_python() {
  local name dir
  local -a dirs=("${(@s.:.)${PENCILPAD_PYTHON_DIRS-/opt/homebrew/bin:/usr/local/bin}}")
  for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    python_ok "$(command -v "$name" 2>/dev/null)" && { PY="$(command -v "$name")"; return 0; }
    for dir in "${dirs[@]}"; do
      python_ok "$dir/$name" && { PY="$dir/$name"; return 0; }
    done
  done
  print -u2 "PencilPad needs Python 3.10 or newer, and this Mac does not have it."
  print -u2 "Install it with Homebrew (brew install python) or from https://www.python.org/downloads/macos/,"
  print -u2 "then run this again."
  return 1
}

make_venv() {
  venv_ok "$1" && return 0
  find_python || return 1
  [ -e "$1" ] && print "Making $1 again with $("$PY" --version 2>&1) (it was made with an older Python, or not finished)."
  rm -rf "$1" && "$PY" -m venv "$1"
}
