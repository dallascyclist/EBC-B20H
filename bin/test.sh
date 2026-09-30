#!/bin/sh
# Run the unit tests with the same interpreter and PYTHONPATH as bin/ebc.sh.
# Usage: bin/test.sh                     -> discover and run everything
#        bin/test.sh tests.test_protocol -> one module (or tests.test_protocol.Class.test_name)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$ROOT"
if [ -x "$ROOT/venv/bin/python" ]; then PY="$ROOT/venv/bin/python"; else PY=python3; fi
export PYTHONPATH="$ROOT/python/lib"
if [ $# -gt 0 ]; then
    exec "$PY" -m unittest "$@"
else
    exec "$PY" -m unittest discover -s tests -t "$ROOT" -v
fi
