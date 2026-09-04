"""The base records: what every zone carries before an application publishes.

A census by any reading — one row per record, read top to bottom, asserted on
by tests — and one only the `dns` program decides and declares from, so it is
data in this area rather than a convention (style/pulumi.md, dns.md §1). What
is here is what has nothing to co-locate with: mail, site verifications,
certificate authority authorization, the family zones, the anchors, and the
apex/`www` pair a web server rather than an application serves.

**The unit is the block: records that appear together, in every zone of one
set.** The zone set belongs to the block rather than to the record, which is
the illegal-states rule applied to data — a mail exchanger that had wandered
into a different zone set is a state a per-record field would make writable,
and a block header does not. Cloudflare's API is per zone, so the per-zone
view a zone component receives is derived in front of it by
`record.zone_records`, never by filtering inside it.

Application records are not here. The ones that have not yet moved to a
component are in `legacy.py` and leave one block at a time; the ones that have
moved are beside their application in the `apps` stack (dns.md §1). This
module reads `legacy.py` for the retiring VPS's address and anchor name, so
that both retire with the module that empties at Wave F.

The one block whose contents another stack decides is the cluster anchors:
their labels, families, TTLs and comments are here with the rest of the
census, and the three addresses behind them come across the `dns` stack's one
StackReference. The overlay block reaches no other stack — it is derived from
the roster in `conventions`, the same table `physical` admits members by, and
a roster entry carries the member's address itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pulumi

from kluster import conventions
from kluster.components.dns import legacy
from kluster.components.dns.record import TTL_HOUR, Block, Record, a, aaaa, caa, cname, mx, txt

__all__ = (
    'BASE_RECORDS',
    'CLOUDFLARE_ISSUERS',
    'CLUSTER_ISSUERS',
    'MAIL_ZONES',
    'WORKSPACE_MAIL',
    'ZONE_ISSUERS',
    'AnchorAddresses',
    'blocks',
    'caa_blocks',
    'overlay_label',
    'overlay_records',
)

#: The two Google Workspace domains, which carry one identical mail block. It
#: is named here rather than in `conventions` because exactly one block names
#: it: the zone sets a second program has to agree on are conventions, and this
#: one is read by the row below it and by nothing else.
MAIL_ZONES: tuple[str, ...] = (conventions.ZONE_PRIMARY, 'unlimitedcodeworks.xyz')

#: Where jiahui.id's Google Site is served from.
IP_JIAHUI_SITE = '173.194.206.121'


def overlay_label(member: str) -> str:
    """A member name as a DNS label: lowercase, spaces become hyphens.

    Central's names are display names and several contain spaces. They are
    normalized here rather than renamed there, so the roster keeps reading
    the way the ZeroTier UI shows it.
    """
    return '-'.join(member.lower().split())


def overlay_records() -> tuple[Record, ...]:
    """The overlay host block (dns.md §2): one A record per rostered member.

    The census is `conventions.overlay.ROSTER`, the same table the `physical`
    stack declares the membership from, and the address is the roster entry's
    own — so this block reaches no other stack. Publishing is therefore not a
    second list that can fall behind the first: a device joins the overlay and
    gets its name under `*.zt` by the same declaration, and a device that
    leaves loses both. The gateway is the one member the roster may not carry
    yet, and it has no record here for exactly as long.
    """
    return tuple(
        a(
            f'{overlay_label(entry.name)}.{conventions.OVERLAY_LABEL}',
            str(entry.address),
            ttl=conventions.ANCHOR_TTL,
            comment='ZeroTier member',
        )
        for entry in conventions.overlay.ROSTER
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

#: Google Workspace inbound routing, identical in both mail zones. The
#: Workspace DKIM key is not here: one is issued per domain, so it is the one
#: mail record that is per-zone by nature.
WORKSPACE_MAIL: tuple[Record, ...] = (
    mx('@', 'aspmx.l.google.com', 1, ttl=TTL_HOUR),
    mx('@', 'alt1.aspmx.l.google.com', 5, ttl=TTL_HOUR),
    mx('@', 'alt2.aspmx.l.google.com', 5, ttl=TTL_HOUR),
    mx('@', 'alt3.aspmx.l.google.com', 10, ttl=TTL_HOUR),
    mx('@', 'alt4.aspmx.l.google.com', 10, ttl=TTL_HOUR),
    # One include and no flattening, which is why this is a literal rather
    # than an SPF builder: there is nothing to flatten.
    txt('@', 'v=spf1 include:_spf.google.com ~all', ttl=TTL_HOUR, key='spf'),
    txt('k8s._domainkey', DKIM_K8S, key='dkim-k8s', comment='DKIM key held by the mail sender in-cluster'),
    txt(
        '_dmarc',
        'v=DMARC1; p=quarantine; adkim=s; aspf=s; rua=mailto:dmarc-reports@unlimited-code.works',
        key='dmarc',
    ),
)


def _workspace_dkim(key: str) -> Record:
    """The zone's own Workspace DKIM key: one is issued per domain."""
    return txt('google._domainkey', key, key='dkim-google', comment='Workspace DKIM, unique per domain')


def _verifications(*tokens: str) -> tuple[Record, ...]:
    # The state keys are positional, so a zone's tokens keep their order: a
    # token inserted ahead of another renames both.
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

#: Zone → the CA set its CAA names, keyed by who actually issues for names in
#: it (dns.md §1.1). A zone absent from this table carries no CAA at all, which
#: is the only safe answer for a zone something outside this installation
#: issues for: a pin that current issuance does not satisfy is an outage on
#: the next renewal.
#:
#: This is the one table where "which zones" is the answer rather than the
#: premise, so it stays zone-first and `caa_blocks` groups it into the block
#: grammar the rest of the census is written in.
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


def caa_blocks(zone_issuers: Mapping[str, tuple[str, ...]]) -> tuple[Block, ...]:
    """The zone-first CAA table, as one block per issuer set."""
    by_issuers: dict[tuple[str, ...], list[str]] = {}
    for zone, issuers in zone_issuers.items():
        by_issuers.setdefault(issuers, []).append(zone)
    return tuple(Block(tuple(zones), _caa_records(issuers)) for issuers, zones in by_issuers.items())


@dataclass(frozen=True)
class AnchorAddresses:
    """The three addresses the cluster anchors carry.

    They are the `physical` stack's, so they arrive as inputs that may still
    be unresolved: an address that stack has not published yet travels into
    the record rather than raising, and the same records are declared before
    and after it is applied.
    """

    cluster_v4: pulumi.Input[str]
    cluster_v6: pulumi.Input[str]
    vip1_v4: pulumi.Input[str]


#: Both families of the cluster anchor say the same thing to a reader of the
#: Cloudflare dashboard, so they say it in the same words.
ANCHOR_CLUSTER_COMMENT = 'cluster ingress; every app record is a CNAME here'


def _anchor_records(anchors: AnchorAddresses) -> tuple[Record, ...]:
    """The anchor namespace, over the addresses the program supplies.

    `kluster.hosts` is the cluster's front door — the network load balancer,
    which is dual-stack (architecture.md §3.2), so the anchor carries both an
    A and an AAAA and an application CNAME to it inherits both families.
    `vip1.hosts` is the dedicated VIP, which nothing resolves in anger: it is
    there so an operator can name the address without looking it up. It is
    IPv4 only by construction — the VIP is a reserved public IPv4 that OCI
    1:1-NATs onto a secondary private address, and that mechanism has no IPv6
    counterpart. The state backend deliberately has no anchor: its clients pin
    its IP, and its hot path must not depend on this stack.
    """
    return (
        a(conventions.ANCHOR_CLUSTER, anchors.cluster_v4, ttl=conventions.ANCHOR_TTL, comment=ANCHOR_CLUSTER_COMMENT),
        aaaa(
            conventions.ANCHOR_CLUSTER, anchors.cluster_v6, ttl=conventions.ANCHOR_TTL, comment=ANCHOR_CLUSTER_COMMENT
        ),
        a(
            conventions.ANCHOR_VIP1,
            anchors.vip1_v4,
            ttl=conventions.ANCHOR_TTL,
            comment='dedicated VIP, operator convenience; IPv4 only by construction',
        ),
    )


#: Every record that belongs to no application, one row per block, with the
#: zone set in the first column. The anchors are the one block missing: their
#: addresses are the program's to supply, so they are added by `blocks`.
BASE_RECORDS: tuple[Block, ...] = (
    # The zones that answer for a website: the apex and `www`, served by a web
    # server rather than by an application. All four carry the same pair and
    # something answers in all four; the sets are separate because what
    # answers differs and one of the two is retiring. The apex is an address
    # rather than an alias because a zone apex cannot be a CNAME (dns.md §2).
    Block(
        (*conventions.WEB_ZONES, *conventions.PARKED_ZONES),
        (
            a(
                '@',
                legacy.IP_ARCHVPS,
                proxied=True,
                comment='web origin; repoints to kluster.hosts at migration',
            ),
            cname('www', legacy.ANCHOR_ARCHVPS, proxied=True),
        ),
    ),
    # The overlay host block, one record per roster member. Private addresses
    # in public DNS are an existing deliberate practice, and they are
    # published once: no configuration anywhere names an overlay host by any
    # zone but the primary.
    Block(conventions.PRIMARY_ONLY, overlay_records()),
    # The mail zones: the exchangers, SPF, the in-cluster DKIM key and DMARC,
    # identical in both.
    Block(MAIL_ZONES, WORKSPACE_MAIL),
    # What only the primary carries of its own: its Workspace DKIM key and its
    # site verifications.
    Block(
        conventions.PRIMARY_ONLY,
        (
            _workspace_dkim(DKIM_GOOGLE_UCW),
            *_verifications(
                'u5QSDhgnrgdr-ojW6yDGKD9fM3jJIzFnYxElzH9DNDI',
                'ITwgKBtamT013cCC7wPlM0N2Rloca0feIiYV4Q11dyI',
            ),
        ),
    ),
    # The website co-host, which answers for a website and stops there: it
    # carries no application name and could not carry one, because every
    # application here holds one SSO cookie domain and one registered redirect
    # URI. The three names it publishes of its own are in `legacy.py`.
    Block(
        ('unlimitedcodeworks.xyz',),
        (
            _workspace_dkim(DKIM_GOOGLE_XYZ),
            *_verifications(
                'N74Krrj_GYGUYgHSXUBX735CRdKwNKw736bDUnE-V2U',
                'UtkDDgsgiGtS-w7Fg4DyiaFFVQOgmM5nJvLnRbCFXjc',
            ),
        ),
    ),
    # jiahui.id: a Google Site, mail forwarded by the registrar. Nothing of
    # ours answers in it, so its apex and `www` are addresses with no name of
    # this installation's to alias (dns.md §2).
    Block(
        ('jiahui.id',),
        (
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
        ),
    ),
    # jiahui.love: everything is the apex; the labels alias it rather than the
    # web origin, so a repoint is one record. The apex is an address for the
    # same reason the web origin's is (dns.md §2).
    Block(
        ('jiahui.love',),
        (
            a('@', legacy.IP_ARCHVPS, proxied=True, comment='web origin; repoints to kluster.hosts at migration'),
            *(cname(label, 'jiahui.love', proxied=True) for label in ('www', 'gift', 'ji', 'peifeng')),
        ),
    ),
    # CAA: per zone, by who issues for it. One block per issuer set.
    *caa_blocks(ZONE_ISSUERS),
)


def blocks(*, anchors: AnchorAddresses) -> tuple[Block, ...]:
    """The base census, with the anchors the program supplies the addresses of."""
    return (*BASE_RECORDS, Block(conventions.PRIMARY_ONLY, _anchor_records(anchors)))
