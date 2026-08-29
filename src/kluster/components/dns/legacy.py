"""App records that have not yet moved to an app: the transitional half.

Every row here names a service the legacy VPS still serves, and every row
here is a row the co-location principle says should live beside its app
(dns.md §1). They live in the `dns` stack for exactly as long as the app
lives on the VPS: at migration the app's component declares the same name
against `kluster.hosts`, and the row is deleted from this module. Wave F
checks that nothing still references `archvps.hosts`, which is the same
statement as "this module is empty" (dns.md §6, migration.md).

They are ported rather than left behind because the DNSControl program is
retiring: a record nobody declares is a record nobody previews.

The import census dropped two of them outright -- `jupyter` and `mc`. Both
were proxied to services that no longer exist (jupyter's backend was the
retired Abacus host; the Minecraft server is gone, though `mcmap` survives).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from kluster import conventions
from kluster.components.dns.model import Record, cname, srv
from kluster.components.dns.zones import ANCHOR_ARCHVPS

__all__ = ('LEGACY',)

#: label → whether Cloudflare proxies it. Proxy off is never an oversight: it
#: is either a non-HTTP port on the name or a body size the proxy would cap.
_MIRRORED: tuple[tuple[str, bool, str], ...] = (
    ('auth', True, 'Authelia portal'),
    ('login', True, 'SSO login page'),
    ('k8s', True, 'cluster portal'),
    ('mon', True, 'cluster monitoring'),
    ('photos', False, 'immich; unproxied for large uploads'),
    ('bt', True, 'qbittorrent web UI'),
    ('files', True, 'nextcloud'),
    ('dav', True, 'nextcloud WebDAV'),
    ('sync', True, 'syncthing web UI'),
    ('syncapi', False, 'syncthing discovery and relay, non-HTTP ports'),
    ('matrix', False, 'matrix homeserver, federation port'),
    ('test', False, 'scratch name'),
    ('haos', True, 'home assistant'),
    ('tube', True, 'jellyfin'),
    ('spool', True, 'spoolman'),
    ('mcmap', True, 'minecraft map renderer'),
)

#: Only the primary zone carries these; the mirrors never did.
_PRIMARY_ONLY: tuple[tuple[str, bool, str], ...] = (
    ('archvps.stats', False, 'VPS stats'),
    ('sync-nas', True, 'syncthing on the NAS, UI only'),
    ('split', True, 'splitpro'),
)

#: unlimitedcodeworks.xyz carries a handful of its own.
_XYZ: tuple[tuple[str, bool, str], ...] = (
    ('btsync', False, 'syncthing, non-HTTP ports'),
    ('games', True, ''),
    ('game', True, ''),
)


def _cnames(rows: Sequence[tuple[str, bool, str]]) -> tuple[Record, ...]:
    return tuple(cname(label, ANCHOR_ARCHVPS, proxied=proxied, comment=comment) for label, proxied, comment in rows)


def _matrix_identity() -> Record:
    return srv(
        '_matrix-identity._tcp',
        priority=10,
        weight=0,
        port=443,
        target=f'matrix.{conventions.ZONE_PRIMARY}',
    )


def _legacy() -> Mapping[str, Sequence[Record]]:
    records: dict[str, Sequence[Record]] = {
        conventions.ZONE_PRIMARY: (*_cnames(_MIRRORED), *_cnames(_PRIMARY_ONLY), _matrix_identity()),
        'unlimitedcodeworks.xyz': _cnames(_XYZ),
    }
    for zone in ('peifeng.phd', 'ucw.phd'):
        records[zone] = (*_cnames(_MIRRORED), _matrix_identity())
    return records


#: Zone → the app records still served by the VPS.
LEGACY: Mapping[str, Sequence[Record]] = _legacy()
