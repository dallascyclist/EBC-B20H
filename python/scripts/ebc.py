#!/usr/bin/env python3
"""Entry point. Normally started by bin/ebc.sh, which sets PYTHONPATH and the cwd."""
import os
import sys

if sys.version_info < (3, 12):
    sys.stderr.write("ebctool requires Python 3.12 or newer; found %s\n" % sys.version.split()[0])
    sys.exit(3)

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
if os.path.isdir(LIB) and LIB not in sys.path:
    sys.path.insert(0, os.path.normpath(LIB))

from ebctool.run import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
