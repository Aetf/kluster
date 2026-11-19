"""The dash the suite runs the device's scripts under is the device's own package, at the version the device runs.

`mise.toml`'s `http:dash` pin installs the amd64 build of the device's
`dash` package, and its version is that package's Debian version, revision
included. `DEVICE_BASE.dash_version` is what `gateway-health` reads the
device's package against, so the two are one fact stated twice: the reading
says when the device moved, and the pin is what has to move with it. Moved
alone, either one leaves the suite running a dash the device does not, with
the reading silent or the pin stale.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from kluster.components import gateway

ROOT = Path(__file__).parent.parent


def test_the_pin_is_the_package_the_device_base_declares() -> None:
    pin = tomllib.loads((ROOT / 'mise.toml').read_text())['tools']['http:dash']

    assert pin['version'] == gateway.DEVICE_BASE.dash_version
