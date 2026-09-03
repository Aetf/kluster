"""The zones this installation publishes in, and the anchors app records point at."""

from __future__ import annotations

ZONE_PRIMARY = 'unlimited-code.works'
#: The shortest mirror, and the zone the retiring LAN names sit inside
#: (`conventions.gateway.ZONE_LEGACY`). It has a name of its own because three
#: declarations have to agree on it: that constant, the wildcard the gateway's
#: proxy holds under it, and the scope of the token that buys the wildcard.
ZONE_SHORT = 'ucw.phd'
#: Mirrors of the primary zone: the same app records, fanned out by the
#: route helpers instead of copy-pasted.
ZONE_MIRRORS = ('unlimitedcodeworks.xyz', 'peifeng.phd', ZONE_SHORT)
#: Family zones — estate records only, never app fan-out targets.
ZONE_FAMILY = ('jiahui.id', 'jiahui.love')

#: Every zone an app may publish in without further thought. Membership is a
#: promise that the zone is a *full* mirror: it carries the shared estate block
#: (`dns.zones.MIRRORED_ESTATE`), so a name fanned out across the set resolves
#: in all of it. Adding a zone here means making it a mirror first.
PUBLIC_ALL = (ZONE_PRIMARY, *ZONE_MIRRORS)
PRIMARY_ONLY = (ZONE_PRIMARY,)
ALL_ZONES = (*PUBLIC_ALL, *ZONE_FAMILY)

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
