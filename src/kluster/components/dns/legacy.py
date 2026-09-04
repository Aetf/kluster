"""The names the legacy VPS still serves, one block per application.

Every row here names a service the retiring VPS answers for, and every row
here is a row the co-location principle says should live beside its
application (dns.md §1). They live in the `dns` stack for exactly as long as
the application lives on the VPS: at migration the application's component
declares the same name against `kluster.hosts`, and the block is deleted from
this module. Wave F checks that nothing still references `archvps.hosts`,
which is the same statement as "this module is empty" (dns.md §6,
migration.md).

**The unit is the application, because the unit of every remaining edit here
is a migration.** migration.md's waves are enumerated by application, so a
migration is one block deleted whole beside the route row and the component
declaration that replace it — three files in one pull request, and no row of
a shared tuple to pick out. Each block header names the owner that will
delete it and the wave it goes in.

The VPS's own address and anchor name are here too, and `base.py` reads them
from here: they retire with this module rather than outliving it, so the
module that empties at Wave F is the one that holds them.

Every block is in the primary zone alone, apart from the three names the
website co-host publishes of its own. The parked zones carry no application
name: what answered one there was the VPS's catch-all and never the
application, so the name was dead however it resolved (dns.md §2).
"""

from __future__ import annotations

from kluster import conventions
from kluster.components.dns.record import Block, a, cname, srv

__all__ = ('ANCHOR_ARCHVPS', 'IP_ARCHVPS', 'LEGACY')

#: The legacy VPS, still the origin of every record that has not migrated.
IP_ARCHVPS = '45.77.144.92'
ANCHOR_ARCHVPS = f'archvps.{conventions.ANCHOR_LABEL}.{conventions.ZONE_PRIMARY}'


def _vps(label: str, comment: str, *, proxied: bool = True) -> Block:
    """One name the VPS serves, in the primary zone alone.

    Proxy off is never an oversight: it is either a non-HTTP port on the name
    or a body size the proxy would cap, which is why it is spelled per block.
    """
    return Block(conventions.PRIMARY_ONLY, (cname(label, ANCHOR_ARCHVPS, proxied=proxied, comment=comment),))


#: The blocks the VPS still serves. Each is deleted whole when the application
#: named in its header migrates and declares the same name against the cluster
#: anchor.
LEGACY: tuple[Block, ...] = (
    # The VPS itself -- Wave F. Every CNAME that names the machine targets this
    # one record, in the primary zone alone, so the machine is addressed in one
    # place and retires from one place.
    Block(
        conventions.PRIMARY_ONLY,
        (
            a(
                f'archvps.{conventions.ANCHOR_LABEL}',
                IP_ARCHVPS,
                ttl=conventions.ANCHOR_TTL,
                comment='legacy VPS anchor; retires with the VPS',
            ),
        ),
    ),
    # authelia -- Wave A
    _vps('auth', 'Authelia portal'),
    # splitpro -- Wave A
    _vps('split', 'splitpro'),
    # matrix -- Wave A. The identity SRV rides with the homeserver: it names
    # the same host and is answered by the same process.
    Block(
        conventions.PRIMARY_ONLY,
        (
            cname('matrix', ANCHOR_ARCHVPS, proxied=False, comment='matrix homeserver, federation port'),
            srv(
                '_matrix-identity._tcp',
                priority=10,
                weight=0,
                port=443,
                target=f'matrix.{conventions.ZONE_PRIMARY}',
            ),
        ),
    ),
    # syncthing -- Wave A
    _vps('sync', 'syncthing web UI'),
    # stdiscosrv -- Wave A
    _vps('syncapi', 'syncthing discovery and relay, non-HTTP ports', proxied=False),
    # the WebDAV successor -- Wave A
    _vps('dav', 'nextcloud WebDAV'),
    # monitoring -- Wave B. Kept because the dashboard is in use; which
    # component declares the name afterwards is the monitoring design's answer
    # to give, and the ordering is safe either way because monitoring is
    # rebuilt fresh rather than migrated.
    _vps('mon', 'cluster monitoring'),
    # spoolman -- Wave B
    _vps('spool', 'spoolman'),
    # the haos.ucw LAN-device backend -- Wave B
    _vps('haos', 'home assistant'),
    # immich -- Wave C
    _vps('photos', 'immich; unproxied for large uploads', proxied=False),
    # jellyfin -- Wave C
    _vps('tube', 'jellyfin'),
    # syncthing-nas -- Wave C
    _vps('sync-nas', 'syncthing on the NAS, UI only'),
    # qbittorrent -- Wave D. Nothing on the VPS answers this name today; it is
    # kept because the host qbittorrent migrates and its component declares the
    # name, so deleting it here would unpublish a name that is about to be
    # claimed.
    _vps('bt', 'qbittorrent web UI'),
    # syncthing on the VPS, in the website co-host's zone -- Wave A
    Block(
        ('unlimitedcodeworks.xyz',),
        (cname('btsync', ANCHOR_ARCHVPS, proxied=False, comment='syncthing, non-HTTP ports'),),
    ),
    # the doors static sites, in the website co-host's zone -- Wave B
    Block(
        ('unlimitedcodeworks.xyz',),
        tuple(cname(label, ANCHOR_ARCHVPS, proxied=True) for label in ('games', 'game')),
    ),
)
