"""The `dns` layer: the record model, the record tables, and the resolvers' configuration.

`stacks/dns.py` is the program; this package is what it declares from.
Records are plain data (`record`, `base`, `legacy`), grouped into blocks — the
records that appear together, in every zone of one set — and the per-zone view
Cloudflare's API takes is derived from them by `zone_records`. The rule list
both resolvers answer from is derived from the shared censuses by the
functions in `rewrites` and rendered by `rewrites.user_rules`; the rest of
what the resolvers are configured with is data beside it (`resolver_settings`,
`aliases`, `blocklists`). The two things that turn data into resources are
`zone.ManagedZone` and `resolver.ResolverConfiguration`, the latter over the
custom provider in `kluster.providers.adguard`.

The derivations are imported from their module rather than re-exported here: a
package attribute named `rewrites` would shadow the module that defines the
function of that name, and the shadowing is silent — `from
kluster.components.dns import rewrites` would bind the function, and the first
attribute lookup on the module would be the only thing to say so.
"""

from __future__ import annotations

from kluster.components.dns.base import BASE_RECORDS
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.record import Block, Record, zone_records
from kluster.components.dns.resolver import AdGuardConfiguration, ResolverConfiguration
from kluster.components.dns.rewrites import Blocklist, Rewrite, RuleBlock
from kluster.components.dns.zone import ManagedZone

__all__ = (
    'BASE_RECORDS',
    'LEGACY',
    'AdGuardConfiguration',
    'Block',
    'Blocklist',
    'ManagedZone',
    'Record',
    'ResolverConfiguration',
    'Rewrite',
    'RuleBlock',
    'zone_records',
)
