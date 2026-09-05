"""The `dns` layer: the record model, the record tables, and the rewrites.

`stacks/dns.py` is the program; this package is what it declares from.
Records are plain data (`record`, `base`, `legacy`), grouped into blocks — the
records that appear together, in every zone of one set — and the per-zone view
Cloudflare's API takes is derived from them by `zone_records`. The
split-horizon rewrites are derived from the shared route census by
`rewrites.rewrites`, over `conventions.routes.ROUTES`, and the two things that
turn data into resources are `zone.ManagedZone` and
`rewrites.ResolverRewrites`, the latter over the custom provider in
`kluster.providers.adguard_rewrites`.

The derivation is imported from the module rather than re-exported here: a
package attribute of that name would shadow the `rewrites` module that defines
it, and the shadowing is silent — `from kluster.components.dns import rewrites`
would bind the function, and the first attribute lookup on the module would be
the only thing to say so.
"""

from __future__ import annotations

from kluster.components.dns.base import BASE_RECORDS
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.record import Block, Record, zone_records
from kluster.components.dns.rewrites import ResolverRewrites, Rewrite
from kluster.components.dns.zone import ManagedZone
from kluster.providers.adguard_rewrites import AdGuardRewrite

__all__ = (
    'BASE_RECORDS',
    'LEGACY',
    'AdGuardRewrite',
    'Block',
    'ManagedZone',
    'Record',
    'ResolverRewrites',
    'Rewrite',
    'zone_records',
)
