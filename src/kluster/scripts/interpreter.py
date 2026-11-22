"""`interpreter`: the Python that runs this environment, and the OpenSSL that build carries.

    mise x uv -- uv run interpreter

It prints `sys.version` and `ssl.OPENSSL_VERSION` on one line. Under
`uv run` that is the environment's interpreter, the build uv installs for
`.python-version`, and its OpenSSL is what every TLS connection the
environment's Python makes runs on. `checks.yml` runs it once the
environment is installed, so the log names both for every step after it.
"""

from __future__ import annotations

import ssl
import sys


def main() -> int:
    print(sys.version, ssl.OPENSSL_VERSION)
    return 0


if __name__ == '__main__':
    sys.exit(main())
