"""The estate census: every record that belongs to no app.

Ported from the DNSControl program this stack replaces
(github.com/Aetf/dns), one row per record, with the drops the import census
called for (dns.md §2, gateway.md §2.1) already applied. What is here is
what has nothing to co-locate with: mail, the ZeroTier host block, site
verifications, certificate authority authorization, the family zones, and
the apex/`www` pair that a web server rather than an app serves.

App records are not here. The ones that have not yet moved to a component
live in `legacy.py` and leave one at a time; the ones that have moved live
beside their app in the `apps` stack (dns.md §1).

The anchors are not here either: `kluster.hosts` and `vip1.hosts` carry
addresses the `physical` stack hands out, so they are built from that
stack's outputs in `stacks/dns.py`. `archvps.hosts` is a literal precisely
because it is the one anchor Pulumi does not create -- it names the legacy
VPS, and it retires with it (migration.md Wave F).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from kluster import conventions
from kluster.dns.model import TTL_HOUR, Record, a, caa, cname, mx, txt

__all__ = ('ESTATE', 'ZT_ROSTER', 'zt_label', 'zt_records')

#: The legacy VPS, still the origin of every record that has not migrated.
IP_ARCHVPS = '45.77.144.92'
ANCHOR_ARCHVPS = f'archvps.{conventions.ANCHOR_LABEL}.{conventions.ZONE_PRIMARY}'

#: Where jiahui.id's Google Site is served from.
IP_JIAHUI_SITE = '173.194.206.121'

#: The ZeroTier host block (dns.md §2). Addresses are ZeroTier Central's
#: managed assignments; the roster in `physical` becomes their source of
#: truth once it declares the members, and this table collapses into it.
#: Three records were dropped at import because Central no longer knows the
#: member (Abacus, Aetf-Arch-Mac, Aetf-MacbookPro).
ZT_ROSTER: tuple[tuple[str, str], ...] = (
    ('udm', str(conventions.ZT_UDM)),
    ('Aetf-Arch-XPS', '10.144.175.24'),
    ('Aetf-Arch-Homelab', '10.144.180.10'),
    ('Aetf-Arch-VPS', '10.144.160.212'),
    ('Aetf-Laptop', '10.144.127.147'),
    ('OnePlus6T', '10.144.160.97'),
    ('haos', '10.144.84.129'),
)


def zt_label(member: str) -> str:
    """A member name as a DNS label: lowercase, spaces become hyphens.

    Central's names are display names and several contain spaces. They are
    normalized here rather than renamed there, so the roster keeps reading
    the way the ZeroTier UI shows it.
    """
    return '-'.join(member.lower().split())


def zt_records() -> tuple[Record, ...]:
    return tuple(
        a(
            f'{zt_label(member)}.{conventions.ZT_LABEL}',
            address,
            ttl=conventions.ANCHOR_TTL,
            comment='ZeroTier member',
        )
        for member, address in ZT_ROSTER
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


def _caa_records() -> tuple[Record, ...]:
    """Issuance pinned to Let's Encrypt, the only CA this estate asks.

    Every certificate the cluster and the gateway hold is issued over DNS-01
    against this account (cluster-infra.md §1.1), so the pin costs nothing
    and turns a mis-issuance elsewhere into a refusal.
    """
    return (
        caa('@', tag='issue', value='letsencrypt.org'),
        caa('@', tag='issuewild', value='letsencrypt.org'),
    )


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


def _mirrored_estate() -> tuple[Record, ...]:
    """What the primary zone and its mirrors carry identically."""
    return (
        a(
            f'archvps.{conventions.ANCHOR_LABEL}',
            IP_ARCHVPS,
            ttl=conventions.ANCHOR_TTL,
            comment='legacy VPS anchor; retires with the VPS',
        ),
        *zt_records(),
        *_web_origin(),
        *_caa_records(),
    )


def _primary() -> tuple[Record, ...]:
    return (
        *_mirrored_estate(),
        *_mail_records(dkim_google=DKIM_GOOGLE_UCW),
        *_verifications(
            'u5QSDhgnrgdr-ojW6yDGKD9fM3jJIzFnYxElzH9DNDI',
            'ITwgKBtamT013cCC7wPlM0N2Rloca0feIiYV4Q11dyI',
        ),
    )


def _unlimitedcodeworks_xyz() -> tuple[Record, ...]:
    # A partial mirror: it carries mail and the web origin, never the host
    # namespaces.
    return (
        *_web_origin(),
        *_caa_records(),
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
        *_caa_records(),
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
        *_caa_records(),
        *(cname(label, 'jiahui.love', proxied=True) for label in ('www', 'gift', 'ji', 'peifeng')),
    )


def _estate() -> Mapping[str, Sequence[Record]]:
    census: dict[str, Sequence[Record]] = {
        conventions.ZONE_PRIMARY: _primary(),
        'unlimitedcodeworks.xyz': _unlimitedcodeworks_xyz(),
        'jiahui.id': _jiahui_id(),
        'jiahui.love': _jiahui_love(),
    }
    for zone in ('peifeng.phd', 'ucw.phd'):
        census[zone] = _mirrored_estate()
    return census


#: Zone → the estate records it carries. Every zone this program knows about
#: appears, so a zone with no estate records would still be declared.
ESTATE: Mapping[str, Sequence[Record]] = _estate()
