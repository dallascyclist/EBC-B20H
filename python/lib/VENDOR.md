# Vendored third-party code

Nothing in this tree is installed with pip. Third-party code is copied in, pinned, and
checksummed so the tool does not drift with the host's toolchain.

| Package | Version | Source | SHA-256 of sdist | Vendored |
|---|---|---|---|---|
| pyserial | 3.5 | https://pypi.org/project/pyserial/3.5/ (`pyserial-3.5.tar.gz`) | `3c77e014170dfffbd816e6ffc205e9842efb10be9f58ec16d3e8675b4925cddb` | 2026-09-30 |

pyserial is BSD-3 licensed; its LICENSE.txt is inside `serial/`. It is pure Python and is
imported only through `serial.Serial` and `serial.tools.list_ports`.

## Updating

1. Download the new sdist, record its SHA-256 here, and verify it with `shasum -a 256 -c`.
2. Replace `python/lib/serial/` with the `serial/` directory from the sdist and copy LICENSE.txt.
3. Update `tests/test_vendor.py` to the new version string.
4. Run `bin/test.sh`, then `bin/ebc.sh discover` against the tester before relying on it.
