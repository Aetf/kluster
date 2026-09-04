"""The census as data: what it carries, what it dropped, what stays unique.

These assertions are about the declaration, not about Pulumi, so they need no
runtime — which is the point of keeping records as plain data.
"""

import subprocess
import sys
from collections import Counter

import pytest

from kluster import conventions
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.model import Record
from kluster.components.dns.routes import Rewrite, rewrites
from kluster.components.dns.zones import (
    CLOUDFLARE_ISSUERS,
    CLUSTER_ISSUERS,
    ESTATE,
    LEGACY_ANCHOR,
    WEB_ORIGIN,
    ZONE_ISSUERS,
    zt_label,
    zt_records,
)
from kluster.conventions.routes import Exposure, Route


def _zt() -> tuple[Record, ...]:
    return zt_records()


def _records(zone: str) -> tuple[Record, ...]:
    # The overlay block is not in `ESTATE` -- it is derived from the roster and
    # added by the stack program -- but it is part of what the primary zone
    # carries, so the census assertions below have to see it.
    overlay = _zt() if zone in conventions.PRIMARY_ONLY else ()
    return (*ESTATE[zone], *LEGACY.get(zone, ()), *overlay)


def _labels(zone: str) -> set[str]:
    return {record.label for record in _records(zone)}


def test_every_zone_is_declared() -> None:
    # A zone the program does not know about is a zone nobody previews.
    assert set(ESTATE) == set(conventions.ALL_ZONES)


def test_state_keys_are_unique_within_a_zone() -> None:
    """Two records sharing a key would be one resource, silently.

    The apex holds several TXT records and five MX; label and type alone do
    not identify them, so the model carries an explicit key and this is the
    check that it was set.
    """
    for zone in ESTATE:
        keys = Counter(record.resource_key for record in _records(zone))
        assert [key for key, count in keys.items() if count > 1] == [], zone


@pytest.mark.parametrize('label', ['abacus.hosts', 'jupyter', 'mc'])
def test_the_import_census_dropped_its_dead_weight(label: str) -> None:
    # The Abacus host is gone; jupyter's backend was that host, and the
    # Minecraft server it fronted is gone too.
    for zone in ESTATE:
        assert label not in _labels(zone), zone


def test_every_member_of_the_overlay_roster_is_published() -> None:
    """The roster is the census, so a member without a record cannot exist.

    The block used to be a hand-maintained table beside the roster, and it had
    drifted from it in both directions — members with no record, and a record
    whose address the overlay had reassigned. Deriving is what makes that a
    state the program cannot be in rather than one somebody has to notice.
    """
    published = {record.label for record in _zt()}

    assert published == {f'{zt_label(entry.name)}.{conventions.ZT_LABEL}' for entry in conventions.overlay.ROSTER}
    assert len(_zt()) == len(conventions.overlay.ROSTER)


def test_the_retired_members_are_published_by_nobody() -> None:
    """Leaving the roster is what retires a record; nothing else does.

    Three members were dropped when the census was imported and one more when
    the roster was reconciled against Central, all for the same reason: the
    overlay no longer knows them.
    """
    members = {entry.name for entry in conventions.overlay.ROSTER}

    assert not members & {'Abacus', 'Aetf-Arch-Mac', 'Aetf-MacbookPro', 'Aetf-Laptop'}


def test_the_gateway_has_no_record_for_as_long_as_it_has_no_roster_entry() -> None:
    """`udm.zt` appears when the member does, by the same edit and no other.

    The gateway's ZeroTier identity is minted by the daemon the `physical`
    stack delivers to it, so until the bring-up ceremony has read that id onto
    the roster there is no member — and a record for a member that does not
    exist would resolve to an address nothing answers at. Both come from the
    one entry, so neither can arrive without the other.
    """
    labels = {record.label for record in _zt()}

    assert (f'{conventions.overlay.MEMBER_UDM}.{conventions.ZT_LABEL}' in labels) == (
        conventions.overlay.MEMBER_UDM in {entry.name for entry in conventions.overlay.ROSTER}
    )


def test_a_members_record_carries_the_address_its_roster_entry_holds() -> None:
    # The roster is the only source: an entry has a concrete address whichever
    # of the two shapes it is, so nothing about this block waits on a run.
    member = 'Aetf-Arch-Homelab'

    record = next(record for record in _zt() if record.label == f'{zt_label(member)}.{conventions.ZT_LABEL}')

    assert record.content == str(conventions.overlay.member(member).address)
    assert record.ttl == conventions.ANCHOR_TTL


#: What comes with the module that declares the ZeroTier resources: the two
#: bridged provider SDKs the gateway is built from, and the SSH client it is
#: configured over. Named here so the cost of reading the roster from that
#: module instead of from `conventions` is stated rather than implied.
GATEWAY_IMPORTS = ('asyncssh', 'pulumi_unifi', 'pulumi_zerotier')


def test_the_records_import_without_the_gateway_behind_them() -> None:
    """The overlay roster is a convention, so declaring records loads no provider.

    `dns.zones` reads the roster out of `conventions`, which is plain data.
    Reading it out of the module that admits members by it would put every
    package above into the import graph of a program that only declares DNS
    records — a package dependency on the gateway, bought for a table of names
    and roles.

    A subprocess is the only honest place to ask: by the time this runs, the
    rest of the suite has imported all three anyway.
    """
    probe = (
        'import sys, kluster.components.dns.zones; '
        f'print(" ".join(sorted(name for name in {GATEWAY_IMPORTS!r} if name in sys.modules)))'
    )

    loaded = subprocess.run([sys.executable, '-c', probe], capture_output=True, check=True, text=True)

    assert loaded.stdout.split() == []


def test_zerotier_labels_are_dns_labels() -> None:
    # Central's names are display names: they carry case and spaces, and two
    # members on the roster today carry both.
    assert zt_label('S26 Ultra') == 's26-ultra'
    assert zt_label('Pixel 7 Pro') == 'pixel-7-pro'
    for entry in conventions.overlay.ROSTER:
        label = zt_label(entry.name)
        assert label == label.lower()
        assert ' ' not in label


def test_the_survivors_of_the_dropped_pair_are_still_declared() -> None:
    # `mcmap` outlived the Minecraft server that `mc` pointed at.
    assert 'mcmap' in _labels(conventions.ZONE_PRIMARY)


def test_the_spf_record_is_quoted() -> None:
    """An unquoted SPF string is split on its spaces by the API.

    It then comes back as several character-strings and stops matching.
    """
    spf = next(record for record in ESTATE[conventions.ZONE_PRIMARY] if record.resource_key == 'spf')

    assert spf.content == '"v=spf1 include:_spf.google.com ~all"'


def _issuers(zone: str, tag: str) -> set[str]:
    return {
        str(record.data['value'])
        for record in ESTATE[zone]
        if record.type == 'CAA' and record.data is not None and record.data['tag'] == tag
    }


@pytest.mark.parametrize('zone', sorted(ZONE_ISSUERS))
def test_a_cloudflare_served_zone_authorizes_the_edge_ca_set(zone: str) -> None:
    """A proxied name is served by a Cloudflare-issued certificate.

    Authorizing only the CA the cluster itself uses would leave the edge
    unable to renew, so the whole partner set is named -- and Let's Encrypt is
    in it, which covers the cluster's own DNS-01 certificates too.
    """
    assert _issuers(zone, 'issue') == set(CLOUDFLARE_ISSUERS)
    assert _issuers(zone, 'issuewild') == set(CLOUDFLARE_ISSUERS)


def test_the_cluster_ca_is_authorized_everywhere_caa_is_declared() -> None:
    """Whatever else a pinned zone allows, cert-manager must still issue."""
    for zone in ZONE_ISSUERS:
        assert set(CLUSTER_ISSUERS) <= _issuers(zone, 'issue'), zone
        assert set(CLUSTER_ISSUERS) <= _issuers(zone, 'issuewild'), zone


def test_an_externally_issued_zone_carries_no_caa() -> None:
    """jiahui.id is a Google Site: nothing here knows what may issue for it.

    It has no CAA in production, and inventing a pin that current issuance
    does not satisfy would break the site at its next renewal.
    """
    assert 'jiahui.id' not in ZONE_ISSUERS
    assert [record for record in ESTATE['jiahui.id'] if record.type == 'CAA'] == []


def test_every_zone_is_classified_or_deliberately_unpinned() -> None:
    """A new zone must not silently inherit someone else's issuance policy."""
    assert set(ZONE_ISSUERS) <= set(ESTATE)
    assert set(ESTATE) - set(ZONE_ISSUERS) == {'jiahui.id'}


def test_caa_keys_survive_a_value_with_parameters() -> None:
    """`pki.goog; cansignhttpexchanges=yes` must not become a state name.

    The key names the authority alone, so adding or dropping a parameter is
    an update rather than a replace.
    """
    record = next(
        record
        for record in ESTATE[conventions.ZONE_PRIMARY]
        if record.type == 'CAA' and record.data is not None and str(record.data['value']).startswith('pki.goog')
    )

    assert record.resource_key == 'caa-issue-pki-goog'


@pytest.mark.parametrize('label', ['photos', 'matrix', 'syncapi'])
def test_the_unproxied_records_stay_unproxied(label: str) -> None:
    """Proxy-off is a requirement, not an oversight.

    Large uploads (photos) and non-HTTP ports (matrix federation, syncthing's
    discovery and relay) do not survive the proxy.
    """
    record = next(record for record in LEGACY[conventions.ZONE_PRIMARY] if record.label == label)

    assert record.proxied is False


def test_a_parked_zone_carries_the_web_origin_and_its_caa_and_nothing_else() -> None:
    """A parked zone serves nothing of this installation's, so it publishes nothing.

    What resolves in one is the apex and `www`, addressed at the legacy VPS
    and answered by that machine's catch-all; those retire with the machine
    and leave the zone dark but for its CAA. CAA is the exception by
    construction -- the policy is a property of the zone, not of what serves
    it -- and the edge mints a certificate for a zone it hosts whether or not
    anything of ours answers in it.
    """
    origin = {record.resource_key for record in WEB_ORIGIN}

    assert origin
    for zone in conventions.PARKED_ZONES:
        assert {record.resource_key for record in _records(zone) if record.type != 'CAA'} == origin, zone


@pytest.mark.parametrize('label', ['auth', 'photos', 'matrix', 'tube', '_matrix-identity._tcp'])
def test_no_application_name_is_published_in_a_parked_zone(label: str) -> None:
    """A name a parked zone answered reached the VPS catch-all, never the app.

    No certificate has ever been issued at an origin for one, and no cookie
    domain, redirect URI or absolute origin of any application names one. So
    the name resolved and then served the wrong thing, which is the promise
    dns.md §2 forbids making.
    """
    assert label in _labels(conventions.ZONE_PRIMARY)
    for zone in conventions.PARKED_ZONES:
        assert label not in _labels(zone), zone


def test_every_zone_that_answers_for_a_website_carries_the_web_origin() -> None:
    """The apex and `www` are the one block all four public zones share.

    What answers differs, which is why the two sets are separate: the served
    pair is answered by the website, the parked pair by the legacy VPS's own
    catch-all. A zone in either set that lost the block would stop answering
    at its own apex, and nothing else here would notice.
    """
    origin = {record.resource_key for record in WEB_ORIGIN}

    assert origin
    for zone in (*conventions.WEB_ZONES, *conventions.PARKED_ZONES):
        assert origin <= {record.resource_key for record in _records(zone)}, zone


def test_the_website_co_host_carries_a_website_its_own_mail_and_its_own_names() -> None:
    """It answers for a website and stops there: no application name, ever.

    Every application here holds one SSO cookie domain and one registered
    redirect URI, so a second hostname for one is a login that loops. The pin
    is the co-host's whole declared label set rather than a list of forbidden
    names, because what would arrive is the name nobody thought to forbid --
    an anchor copy, an overlay block, an application fanned across a set.
    """
    assert _labels('unlimitedcodeworks.xyz') == {
        # The web origin's apex, which the mail and verification records
        # share, and the `www` beside it.
        '@',
        'www',
        # Mail of its own: the Workspace DKIM key is issued per domain.
        'k8s._domainkey',
        'google._domainkey',
        '_dmarc',
        # Three names of its own, which the primary does not carry.
        'btsync',
        'games',
        'game',
    }


def test_the_legacy_anchor_and_the_overlay_block_are_the_primary_zones_alone() -> None:
    """One record addresses the VPS, and one block publishes private addresses.

    Every CNAME that names the VPS -- in every zone -- targets the primary's
    copy, so a copy elsewhere is a record nothing reads; and an overlay host
    is named by no configuration outside the primary. Publishing either
    anywhere else costs the copies and buys nothing.
    """
    copied = {record.resource_key for record in (*LEGACY_ANCHOR, *_zt())}

    assert copied
    for zone in conventions.ALL_ZONES:
        if zone in conventions.PRIMARY_ONLY:
            assert copied <= {record.resource_key for record in _records(zone)}, zone
        else:
            assert copied & {record.resource_key for record in _records(zone)} == set(), zone


def test_a_public_route_needs_no_rewrite() -> None:
    # LAN clients take the cloud path for it, which is the whole difference.
    assert rewrites([Route(host='www', exposure=Exposure.PUBLIC)]) == ()


def test_a_split_route_is_rewritten_in_every_zone_it_is_published_in() -> None:
    route = Route(host='photos', exposure=Exposure.SPLIT, zones=('ucw.phd', 'peifeng.phd'))

    assert rewrites([route]) == (
        Rewrite(domain='photos.ucw.phd', answer=conventions.LAN_POOL.default_vip.v4),
        Rewrite(domain='photos.ucw.phd', answer=conventions.LAN_POOL.default_vip.v6),
        Rewrite(domain='photos.peifeng.phd', answer=conventions.LAN_POOL.default_vip.v4),
        Rewrite(domain='photos.peifeng.phd', answer=conventions.LAN_POOL.default_vip.v6),
    )


def test_both_families_are_rewritten() -> None:
    """A LAN client that prefers IPv6 must not fall through to the public answer.

    AdGuard answers a rewrite only for the family of its answer, so a v4-only
    rewrite leaves AAAA resolving to the cloud path (RFC 6724).
    """
    entries = rewrites([Route(host='tube', exposure=Exposure.SPLIT, zones=('ucw.phd',))])

    assert {entry.answer for entry in entries} == {
        conventions.LAN_POOL.default_vip.v4,
        conventions.LAN_POOL.default_vip.v6,
    }
    assert {entry.answer.version for entry in entries} == {4, 6}


def test_an_iot_route_is_answered_by_the_media_vip() -> None:
    # Attaching to the media gateway *is* the "IoT may reach this" decision.
    route = Route(host='tube', exposure=Exposure.IOT, zones=('ucw.phd',))

    assert {entry.answer for entry in rewrites([route])} == {
        conventions.LAN_POOL.media_vip.v4,
        conventions.LAN_POOL.media_vip.v6,
    }


def test_a_lan_only_route_is_rewrite_only() -> None:
    """No public record, but the name still has to resolve on the LAN.

    Publishing nothing is what keeps the LAN service census out of public
    resolvers; the rewrite is the only thing that makes the name work.
    """
    route = Route(host='golinks', exposure=Exposure.LAN_ONLY, zones=('ucw.phd',))

    assert route.public is False
    assert len(rewrites([route])) == 2
