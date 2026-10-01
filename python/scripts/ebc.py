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
    try:
        code = main()
    finally:
        # Console output is best effort (R38). When stdout's reader has gone (a `| tee` killed by
        # the same Ctrl-C, `| head`), the last flush fails; send what is left to /dev/null so the
        # interpreter does not report it at exit or replace the exit code with 120. Any OSError
        # here (EBADF, ENOSPC, ...) is handled the same way, keeping main's exit code (R41).
        try:
            if sys.stdout is not None:
                sys.stdout.flush()
        except OSError:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
    sys.exit(code)
