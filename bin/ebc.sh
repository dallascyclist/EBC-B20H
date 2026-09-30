#!/bin/sh
# Launcher for ebctool.
# Runs from the repo root so the relative paths in config/config.ini
# (logs/, db/) resolve, uses ./venv when present, and
# puts the vendored libraries on PYTHONPATH. No packages are ever pip-installed.
set -eu
# A minimal environment (cron, launchd, a bare shell) may lack these: give it a PATH,
# a private umask for the logs and database, and UTF-8 regardless of the locale.
PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin${PATH:+:$PATH}"; export PATH
umask 027
PYTHONUTF8=1; export PYTHONUTF8
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$ROOT"
if [ -x "$ROOT/venv/bin/python" ]; then PY="$ROOT/venv/bin/python"; else PY=python3; fi
export PYTHONPATH="$ROOT/python/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" "$ROOT/python/scripts/ebc.py" "$@"
