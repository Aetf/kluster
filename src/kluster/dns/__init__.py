"""The `dns` layer: the record model, the estate census, and the rewrites.

`stacks/dns.py` is the program; this package is what it declares from.
Records are plain data (`model`, `zones`, `legacy`), routes are plain data
shared with `apps` (`routes`), and the two components that turn data into
resources are `zone.ManagedZone` and `adguard.AdGuardRewrite`.
"""

from __future__ import annotations

from kluster.dns.adguard import AdGuardRewrite, declare_rewrites
from kluster.dns.legacy import LEGACY
from kluster.dns.model import Record
from kluster.dns.routes import ROUTES, Exposure, Rewrite, Route, rewrites
from kluster.dns.zone import ManagedZone
from kluster.dns.zones import ESTATE

__all__ = (
    'ESTATE',
    'LEGACY',
    'ROUTES',
    'AdGuardRewrite',
    'Exposure',
    'ManagedZone',
    'Record',
    'Rewrite',
    'Route',
    'declare_rewrites',
    'rewrites',
)
