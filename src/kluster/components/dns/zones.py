"""The estate census: every record that belongs to no app.

Ported from the DNSControl program this stack replaces
(github.com/Aetf/dns), one row per record, with the drops the import census
called for (dns.md §2, gateway.md §2.1) already applied. What is here is
what has nothing to co-locate with: mail, site verifications, certificate
authority authorization, the family zones, and the apex/`www` pair that a web
server rather than an app serves.

App records are not here. The ones that have not yet moved to a component
live in `legacy.py` and leave one at a time; the ones that have moved live
beside their app in the `apps` stack (dns.md §1).

The two blocks whose contents another stack decides are not literals here.
The cluster anchors -- `kluster.hosts` and `vip1.hosts` -- carry addresses the
`physical` stack hands out, so they are built from that stack's outputs in
`stacks/dns.py`, and only in the primary zone. The ZeroTier host block is
built there too, from the overlay roster in `conventions` -- the same table
`physical` admits members by: `zt_records` below is its shape, and the
addresses the roster does not itself carry are read across the same
reference. `archvps.hosts` is a literal precisely because it is the
one anchor no stack output backs -- it names the legacy VPS, and it retires
with it (migration.md Wave F).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pulumi

from kluster import conventions
from kluster.components.dns.model import TTL_HOUR, Record, a, caa, cname, mx, txt

__all__ = (
    'ALIAS_ZONES',
    'CLOUDFLARE_ISSUERS',
    'CLUSTER_ISSUERS',
    'ESTATE',
    'MIRRORED_ESTATE',
    'ZONE_ISSUERS',
    'zt_label',
    'zt_records',
)

#: The legacy VPS, still the origin of every record that has not migrated.
IP_ARCHVPS = '45.77.144.92'
ANCHOR_ARCHVPS = f'archvps.{conventions.ANCHOR_LABEL}.{conventions.ZONE_PRIMARY}'

#: Where jiahui.id's Google Site is served from.
IP_JIAHUI_SITE = '173.194.206.121'


def zt_label(member: str) -> str:
    """A member name as a DNS label: lowercase, spaces become hyphens.

    Central's names are display names and several contain spaces. They are
    normalized here rather than renamed there, so the roster keeps reading
    the way the ZeroTier UI shows it.
    """
    return '-'.join(member.lower().split())


def zt_records(address: Callable[[str], pulumi.Input[str]]) -> tuple[Record, ...]:
    """The ZeroTier host block (dns.md §2): one A record per rostered member.

    The census is `conventions.ZT_ROSTER`, the same table the `physical` stack
    admits members by. Publishing is therefore not a second list that can fall
    behind the first: a device joins the overlay and gets its name under `*.zt`
    by the same declaration, and a device that leaves loses both.

    `address` is asked only for the members whose overlay address ZeroTier
    Central assigned -- a fact about a device that existed before this program,
    which reaches this stack as a `physical` output. The addresses this
    repository decides (the gateway's, and the two continuous-integration
    identities') are read off the roster entry instead, which is why the
    gateway's own `udm.zt` is a literal here and does not wait on the identity
    that is minted the first time its daemon runs.
    """
    return tuple(
        a(
            f'{zt_label(entry.name)}.{conventions.ZT_LABEL}',
            str(entry.address) if entry.address is not None else address(entry.name),
            ttl=conventions.ANCHOR_TTL,
            comment='ZeroTier member',
        )
        for entry in conventions.ZT_ROSTER
    )


#: Google Workspace inbound routing, shared by both mail zones.
def _mail_records(*, dkim_google: str) -> tuple[Record, ...]:
    return (
        mx('@', 'aspmx.l.google.com', 1, ttl=TTL_HOUR),
        mx('@', 'alt1.aspmx.l.google.com', 5, ttl=TTL_HOUR),
        mx('@', 'alt2.aspmx.l.google.com', 5, ttl=TTL_HOUR),
        mx('@', 'alt3.aspmx.l.google.com', 10, ttl=TTL_HOUR),
        mx('@', 'alt4.aspmx.l.google.com', 10, ttl=TTL_HOUR),
        # One include and no flattening, which is why this is a literal
        # rather than an SPF builder: there is nothing to flatten.
        txt('@', 'v=spf1 include:_spf.google.com ~all', ttl=TTL_HOUR, key='spf'),
        txt('k8s._domainkey', DKIM_K8S, key='dkim-k8s', comment='DKIM key held by the mail sender in-cluster'),
        txt('google._domainkey', dkim_google, key='dkim-google', comment='Workspace DKIM, unique per domain'),
        txt(
            '_dmarc',
            'v=DMARC1; p=quarantine; adkim=s; aspf=s; rua=mailto:dmarc-reports@unlimited-code.works',
            key='dmarc',
        ),
    )


DKIM_K8S = (
    'v=DKIM1; p='
    'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuzTvyPAmNw5A3UK+60qy'
    'FZ1bxydUZPqZ93+Y/iTQdYPK8GjHs/RpnbBwCUMuHqjcjgm6c2pCKPxIGPjBSfzT'
    'cX4KaMb3dG+dios0H9g8wgXT8k1uimMibfIkCir7gxWxPS+hDnUA3/WSbaLHqJIF'
    'Du/Wi+QtthXY16gzIVU+V7Z0UwB97uKZTypBDOT8USlwJwqe8GFSsQenqJ2YiQFf'
    'IeVrnRIeaNuhyi6zGdNIXSXslvZL4FOENzELciJ2WHOSXHattqJ5G/FiOWiA9QI+'
    '66KRIFQ7Hjc5DtUOURyfTykH6HgDxDUXHMqMl4qfY5UV5S83K+rLITWCCZGbz2HJ'
    'rQIDAQAB'
)

DKIM_GOOGLE_UCW = (
    'v=DKIM1; k=rsa; p='
    'MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCB9A/w8c0RjwW3q75z2gKp36XdkSJw/76R'
    'EGqcowEvFZMysz3JTsjCnErawdQytLTzs9a6Tz3i0Lgx1z9uCOD+xHIiE2zbTyY3Wyb8YZiX'
    '4K6nAbgjUoxtTS4BwiMrRpHjvtWJ3Kq4hAZyr9wyWaJ5Coglk4SQAhFW8DFz550HyQIDAQAB'
)

DKIM_GOOGLE_XYZ = (
    'v=DKIM1; k=rsa; p='
    'MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCOPxdlkUm47Ee6y/Y4Icg5OtwU0MzQhe/K'
    'g0eI8crEXiOwFw1pMmBBXwhaEdHGwj3dQJhsdvZzUGLgaSu6bK0zCZOGEISF8zUDrJD7SL9c'
    '+1hspRFvzzdrOnRnVsqz3ijxeg4Z6iIjLbdTvApAVZCWo05eZIDm4CZ8syLpnjYi5QIDAQAB'
)


def _verifications(*tokens: str) -> tuple[Record, ...]:
    return tuple(
        txt('@', f'google-site-verification={token}', ttl=TTL_HOUR, key=f'google-site-verification-{index}')
        for index, token in enumerate(tokens)
    )


#: The CA set Cloudflare issues its edge certificates from, as CAA values
#: (developers.cloudflare.com/ssl/reference/certificate-authorities/). A zone
#: with even one proxied name is served at the edge by a certificate from one
#: of these, so its CAA must name them all or Universal SSL stops renewing.
#: Let's Encrypt is a member, which is why a proxied zone needs no separate
#: entry for the certificates the cluster obtains itself.
CLOUDFLARE_ISSUERS: tuple[str, ...] = (
    'letsencrypt.org',
    'pki.goog; cansignhttpexchanges=yes',
    'sectigo.com',
    'ssl.com',
)

#: The only CA that issues for a name nothing but the cluster serves: every
#: certificate cert-manager and the gateway hold comes over DNS-01 from a
#: Let's Encrypt account (cluster-infra.md §1.1).
CLUSTER_ISSUERS: tuple[str, ...] = ('letsencrypt.org',)

#: The public zones whose estate is the mirrored block and nothing else. The
#: other two members of `conventions.PUBLIC_ALL` -- the primary and
#: unlimitedcodeworks.xyz -- carry the same block plus mail and their own site
#: verifications, which is the only way one full mirror's *estate* differs
#: from another. The app half is still transitional: `legacy.py` gives
#: unlimitedcodeworks.xyz none of the names the other mirrors carry and three
#: of its own, and the zone becomes a mirror in app names too only as apps
#: move into `apps` and take the default zone set (dns.md §2).
ALIAS_ZONES: tuple[str, ...] = ('peifeng.phd', 'ucw.phd')

#: Zone → the CA set its CAA names, keyed by who actually issues for names in
#: it (dns.md §1). A zone absent from this table carries no CAA at all, which
#: is the only safe answer for a zone something outside this estate issues
#: for: a pin that current issuance does not satisfy is an outage on the next
#: renewal.
ZONE_ISSUERS: Mapping[str, tuple[str, ...]] = {
    conventions.ZONE_PRIMARY: CLOUDFLARE_ISSUERS,
    'unlimitedcodeworks.xyz': CLOUDFLARE_ISSUERS,
    'peifeng.phd': CLOUDFLARE_ISSUERS,
    'ucw.phd': CLOUDFLARE_ISSUERS,
    'jiahui.love': CLOUDFLARE_ISSUERS,
    # jiahui.id is deliberately absent: its apex and `www` are a Google Site,
    # its certificates come from pki.goog, and it carries no CAA today.
}


def _caa_records(issuers: Sequence[str]) -> tuple[Record, ...]:
    """Both tags for each authorized CA; no CA means no record.

    `issuewild` is spelled out rather than left to inherit from `issue`
    because the LAN-only names are covered by per-zone wildcards (dns.md §4),
    and a zone that authorizes `issue` alone forbids exactly those.
    """
    return tuple(caa('@', tag=tag, value=value) for tag in ('issue', 'issuewild') for value in issuers)


def _web_origin() -> tuple[Record, ...]:
    """The apex and `www`, which a web server serves rather than an app.

    They are the estate records that still reference the VPS: they repoint at
    `kluster.hosts` when the site behind them moves, and Wave F checks that
    none is left doing so (dns.md §6).
    """
    return (
        a('@', IP_ARCHVPS, proxied=True, comment='web origin; repoints to kluster.hosts at migration'),
        cname('www', ANCHOR_ARCHVPS, proxied=True),
    )


#: What every full-mirror public zone carries identically as literals -- the
#: legacy VPS anchor and the web origin. The ZeroTier host block is mirrored
#: too, but it is derived rather than written down, so `stacks/dns.py` adds it
#: to the same set of zones (`zt_records`).
#:
#: This block, not a zone-against-zone comparison, is the definition of the
#: mirror: `conventions.PUBLIC_ALL` is exactly the set of zones that carry it,
#: so a name added here reaches all of them and a name added to one zone's own
#: function reaches only that zone.
#:
#: The cluster anchors are deliberately not in it. An app fanning a route
#: across `PUBLIC_ALL` publishes a CNAME in each zone, but every one of them
#: targets the anchor's name in the *primary* zone, so a node rebuild moves
#: one record instead of one per zone. The `archvps.hosts` copies here are
#: not an exception to that rule but estate data: nothing CNAMEs to a
#: mirror's copy -- the legacy rows target the primary's too -- they are the
#: live records ported verbatim, and they retire with the VPS in Wave F.
MIRRORED_ESTATE: tuple[Record, ...] = (
    a(
        f'archvps.{conventions.ANCHOR_LABEL}',
        IP_ARCHVPS,
        ttl=conventions.ANCHOR_TTL,
        comment='legacy VPS anchor; retires with the VPS',
    ),
    *_web_origin(),
)


def _primary() -> tuple[Record, ...]:
    return (
        *MIRRORED_ESTATE,
        *_mail_records(dkim_google=DKIM_GOOGLE_UCW),
        *_verifications(
            'u5QSDhgnrgdr-ojW6yDGKD9fM3jJIzFnYxElzH9DNDI',
            'ITwgKBtamT013cCC7wPlM0N2Rloca0feIiYV4Q11dyI',
        ),
    )


def _unlimitedcodeworks_xyz() -> tuple[Record, ...]:
    # A full mirror with mail of its own, the same shape as the primary: it is
    # in `conventions.PUBLIC_ALL`, so every app fan-out lands a name in it --
    # a CNAME to the primary's anchor, like the fan-out lands in every other
    # mirror.
    return (
        *MIRRORED_ESTATE,
        *_mail_records(dkim_google=DKIM_GOOGLE_XYZ),
        *_verifications(
            'N74Krrj_GYGUYgHSXUBX735CRdKwNKw736bDUnE-V2U',
            'UtkDDgsgiGtS-w7Fg4DyiaFFVQOgmM5nJvLnRbCFXjc',
        ),
    )


def _jiahui_id() -> tuple[Record, ...]:
    # A Google Site with mail forwarded by the registrar: no origin of ours
    # appears in it, which is why it is never an app fan-out target.
    return (
        a('@', IP_JIAHUI_SITE),
        a('www', IP_JIAHUI_SITE),
        *_verifications(
            'PyY9W6ikS_voZGQE3i_JGRNPvMUw5o2QNyGxpnRxSoU',
            'y6Df9QsIorwAdC8bsyCBMtlu3HpzPqppgM7syZsBTyo',
        ),
        mx('@', 'eforward1.registrar-servers.com', 10),
        mx('@', 'eforward2.registrar-servers.com', 10),
        mx('@', 'eforward3.registrar-servers.com', 10),
        mx('@', 'eforward4.registrar-servers.com', 15),
        mx('@', 'eforward5.registrar-servers.com', 20),
        txt('@', 'v=spf1 include:spf.efwd.registrar-servers.com ~all', key='spf'),
    )


def _jiahui_love() -> tuple[Record, ...]:
    # Everything is the apex; the labels are aliases of it rather than of the
    # origin, so a repoint touches one record.
    return (
        a('@', IP_ARCHVPS, proxied=True, comment='web origin; repoints to kluster.hosts at migration'),
        *(cname(label, 'jiahui.love', proxied=True) for label in ('www', 'gift', 'ji', 'peifeng')),
    )


def _estate() -> Mapping[str, Sequence[Record]]:
    census: dict[str, Sequence[Record]] = {
        conventions.ZONE_PRIMARY: _primary(),
        'unlimitedcodeworks.xyz': _unlimitedcodeworks_xyz(),
        'jiahui.id': _jiahui_id(),
        'jiahui.love': _jiahui_love(),
    }
    for zone in ALIAS_ZONES:
        census[zone] = MIRRORED_ESTATE
    # CAA is appended per zone rather than built into the record blocks: the
    # policy is a property of the zone (who issues for its names), not of the
    # block, and the mirrors share a block with the primary.
    return {zone: (*records, *_caa_records(ZONE_ISSUERS.get(zone, ()))) for zone, records in census.items()}


#: Zone → the estate records it carries. Every zone this program knows about
#: appears, so a zone with no estate records would still be declared.
ESTATE: Mapping[str, Sequence[Record]] = _estate()
