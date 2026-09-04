"""The zones this installation publishes in, and the anchors app records point at."""

from __future__ import annotations

ZONE_PRIMARY = 'unlimited-code.works'
#: The shortest zone, and the one the retiring LAN names sit inside
#: (`conventions.gateway.ZONE_LEGACY`). It has a name of its own because three
#: declarations have to agree on it: that constant, the wildcard the gateway's
#: proxy holds under it, and the scope of the token that buys the wildcard.
ZONE_SHORT = 'ucw.phd'

#: The primary alone: every route's default, both anchor blocks, the overlay
#: block, and every application name the legacy VPS still serves. A public
#: record is published in a zone only where a listener and a certificate answer
#: for the name (declarative/dns.md §2), and the primary is the only zone where
#: both are true of an application.
PRIMARY_ONLY = (ZONE_PRIMARY,)

#: The zones whose apex and `www` are served: the primary and the website
#: co-host. The co-host serves that website and nothing else — it carries no
#: application name and could not carry one, because every application here
#: holds one SSO cookie domain, one portal URL and one registered redirect URI.
WEB_ZONES = (ZONE_PRIMARY, 'unlimitedcodeworks.xyz')

#: The two zones this installation holds and does not serve. What resolves in
#: them is copies addressed at the legacy VPS, answered by that machine's
#: catch-all rather than by anything of this installation's, and those names
#: retire with the machine; each zone then carries the CAA set and nothing
#: else (declarative/dns.md §2). Parked is a state and not a stage: `ZONE_SHORT`
#: outlives its last record, because it is the zone the gateway's proxy holds
#: `*.lan.ucw.phd` under.
PARKED_ZONES = ('peifeng.phd', ZONE_SHORT)

#: Family zones — taxonomy only, so that `ALL_ZONES` reads. Nothing is
#: declared against this set.
ZONE_FAMILY = ('jiahui.id', 'jiahui.love')

#: Every zone the `dns` stack declares, which is what the program loops over.
ALL_ZONES = (*WEB_ZONES, *PARKED_ZONES, *ZONE_FAMILY)

#: IP literals live only under the anchor namespace, with low TTLs; apps are
#: CNAMEs to an anchor, so a node rebuild touches exactly one record.
ANCHOR_LABEL = 'hosts'
ANCHOR_CLUSTER = f'kluster.{ANCHOR_LABEL}'
ANCHOR_VIP1 = f'vip1.{ANCHOR_LABEL}'
ANCHOR_TTL = 300

#: The ZeroTier host block — private addresses in public DNS, an existing
#: deliberate practice. Its contents are the overlay roster, one record per
#: member.
ZT_LABEL = 'zt'
