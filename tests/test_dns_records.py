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
from kluster.components.dns.routes import Exposure, Rewrite, Route, rewrites
from kluster.components.dns.zones import (
    ALIAS_ZONES,
    CLOUDFLARE_ISSUERS,
    CLUSTER_ISSUERS,
    ESTATE,
    MIRRORED_ESTATE,
    ZONE_ISSUERS,
    zt_label,
    zt_records,
)


def _zt() -> tuple[Record, ...]:
    return zt_records()


def _records(zone: str) -> tuple[Record, ...]:
    # The overlay block is not in `ESTATE` — it is derived from the roster and
    # added by the stack program — but it is part of what a mirror carries, so
    # the census assertions below have to see it.
    overlay = _zt() if zone in conventions.PUBLIC_ALL else ()
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

    assert published == {f'{zt_label(entry.name)}.{conventions.ZT_LABEL}' for entry in conventions.ZT_ROSTER}
    assert len(_zt()) == len(conventions.ZT_ROSTER)


def test_the_retired_members_are_published_by_nobody() -> None:
    """Leaving the roster is what retires a record; nothing else does.

    Three members were dropped when the census was imported and one more when
    the estate was reconciled against Central, all for the same reason: the
    overlay no longer knows them.
    """
    members = {entry.name for entry in conventions.ZT_ROSTER}

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

    assert (f'{conventions.ZT_MEMBER_UDM}.{conventions.ZT_LABEL}' in labels) == (
        conventions.ZT_MEMBER_UDM in {entry.name for entry in conventions.ZT_ROSTER}
    )


def test_a_members_record_carries_the_address_its_roster_entry_holds() -> None:
    # The roster is the only source: an entry has a concrete address whichever
    # of the two shapes it is, so nothing about this block waits on a run.
    member = 'Aetf-Arch-Homelab'

    record = next(record for record in _zt() if record.label == f'{zt_label(member)}.{conventions.ZT_LABEL}')

    assert record.content == str(conventions.zt_member(member).address)
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
    for entry in conventions.ZT_ROSTER:
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


def test_the_mirrors_carry_the_same_app_names_as_the_primary() -> None:
    # An alias zone publishes nothing of its own, so every name in it is a
    # name the primary publishes too.
    primary = _labels(conventions.ZONE_PRIMARY)

    for zone in ALIAS_ZONES:
        assert _labels(zone) <= primary, zone


def _estate_keys(zone: str) -> set[str]:
    return {record.resource_key for record in ESTATE[zone]}


def test_every_public_zone_carries_the_whole_mirrored_estate() -> None:
    """`PUBLIC_ALL` membership is the claim that the zone is a full mirror.

    The check is against the block itself rather than zone against zone,
    because the zones legitimately differ elsewhere -- two of them carry mail
    and site verifications of their own -- and because that makes editing the
    block the only way to add a mirrored name.

    The cluster anchors are not part of the block: they are declared in the
    primary zone alone, and an app fanning a route across the set publishes a
    CNAME per zone that targets the primary's anchor
    (`test_the_anchors_live_only_in_the_primary_zone`).
    """
    mirrored = {record.resource_key for record in MIRRORED_ESTATE}

    assert mirrored
    for zone in conventions.PUBLIC_ALL:
        assert mirrored <= _estate_keys(zone), zone


def test_an_alias_zone_holds_the_mirrored_estate_and_its_own_caa() -> None:
    """Nothing else may accumulate in a zone that exists only to alias.

    CAA is the exception by construction: the policy is a property of the
    zone, so it is appended per zone instead of living in the shared block.
    """
    mirrored = {record.resource_key for record in MIRRORED_ESTATE}

    for zone in ALIAS_ZONES:
        assert {record.resource_key for record in ESTATE[zone] if record.type != 'CAA'} == mirrored, zone


def test_a_public_route_needs_no_rewrite() -> None:
    # LAN clients take the cloud path for it, which is the whole difference.
    assert rewrites([Route(host='www', exposure=Exposure.PUBLIC)]) == ()


def test_a_split_route_is_rewritten_in_every_zone_it_is_published_in() -> None:
    route = Route(host='photos', exposure=Exposure.SPLIT, zones=('ucw.phd', 'peifeng.phd'))

    assert rewrites([route]) == (
        Rewrite(domain='photos.ucw.phd', answer=str(conventions.LAN_POOL.default_vip.v4)),
        Rewrite(domain='photos.ucw.phd', answer=str(conventions.LAN_POOL.default_vip.v6)),
        Rewrite(domain='photos.peifeng.phd', answer=str(conventions.LAN_POOL.default_vip.v4)),
        Rewrite(domain='photos.peifeng.phd', answer=str(conventions.LAN_POOL.default_vip.v6)),
    )


def test_both_families_are_rewritten() -> None:
    """A LAN client that prefers IPv6 must not fall through to the public answer.

    AdGuard answers a rewrite only for the family of its answer, so a v4-only
    rewrite leaves AAAA resolving to the cloud path (RFC 6724).
    """
    answers = {entry.answer for entry in rewrites([Route(host='tube', exposure=Exposure.SPLIT, zones=('ucw.phd',))])}

    assert answers == {str(conventions.LAN_POOL.default_vip.v4), str(conventions.LAN_POOL.default_vip.v6)}


def test_an_iot_route_is_answered_by_the_media_vip() -> None:
    # Attaching to the media gateway *is* the "IoT may reach this" decision.
    route = Route(host='tube', exposure=Exposure.IOT, zones=('ucw.phd',))

    assert {entry.answer for entry in rewrites([route])} == {
        str(conventions.LAN_POOL.media_vip.v4),
        str(conventions.LAN_POOL.media_vip.v6),
    }


def test_a_lan_only_route_is_rewrite_only() -> None:
    """No public record, but the name still has to resolve on the LAN.

    Publishing nothing is what keeps the LAN service census out of public
    resolvers; the rewrite is the only thing that makes the name work.
    """
    route = Route(host='golinks', exposure=Exposure.LAN_ONLY, zones=('ucw.phd',))

    assert route.public is False
    assert len(rewrites([route])) == 2
