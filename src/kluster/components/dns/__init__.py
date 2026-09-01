"""The `dns` layer: the record model, the estate census, and the rewrites.

`stacks/dns.py` is the program; this package is what it declares from.
Records are plain data (`model`, `zones`, `legacy`), the rewrites are derived
from the route census `apps` authors (`routes`, over `conventions.ROUTES`),
and the two things that turn data into resources are `zone.ManagedZone` and
`adguard.declare_rewrites`, the latter over the custom provider in
`kluster.providers.adguard_rewrites`.
"""

from __future__ import annotations

from kluster.components.dns.adguard import declare_rewrites
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.model import Record
from kluster.components.dns.routes import Rewrite, rewrites
from kluster.components.dns.zone import ManagedZone
from kluster.components.dns.zones import ESTATE
from kluster.providers.adguard_rewrites import AdGuardRewrite

__all__ = (
    'ESTATE',
    'LEGACY',
    'AdGuardRewrite',
    'ManagedZone',
    'Record',
    'Rewrite',
    'declare_rewrites',
    'rewrites',
)
