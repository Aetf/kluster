"""The census as data: what it carries, what it dropped, what stays unique.

These assertions are about the declaration, not about Pulumi, so they need no
runtime — which is the point of keeping records as plain data.
"""

from collections import Counter

import pytest

from kluster import conventions
from kluster.dns.legacy import LEGACY
from kluster.dns.model import Record
from kluster.dns.routes import Exposure, Rewrite, Route, rewrites
from kluster.dns.zones import (
    ALIAS_ZONES,
    CLOUDFLARE_ISSUERS,
    CLUSTER_ISSUERS,
    ESTATE,
    MIRRORED_ESTATE,
    ZONE_ISSUERS,
    ZT_ROSTER,
    zt_label,
)


def _records(zone: str) -> tuple[Record, ...]:
    return (*ESTATE[zone], *LEGACY.get(zone, ()))


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


def test_the_dead_zerotier_members_are_gone_and_the_udm_arrived() -> None:
    members = {member for member, _ in ZT_ROSTER}

    assert not members & {'Abacus', 'Aetf-Arch-Mac', 'Aetf-MacbookPro'}
    assert ('udm', str(conventions.ZT_UDM)) in ZT_ROSTER


def test_zerotier_labels_are_dns_labels() -> None:
    # Central's names are display names: they carry case and spaces.
    assert zt_label('S26 Ultra') == 's26-ultra'
    for member, _ in ZT_ROSTER:
        label = zt_label(member)
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
        Rewrite(domain='photos.ucw.phd', answer=str(conventions.VIP_LAN_V4)),
        Rewrite(domain='photos.ucw.phd', answer=str(conventions.VIP_LAN_V6)),
        Rewrite(domain='photos.peifeng.phd', answer=str(conventions.VIP_LAN_V4)),
        Rewrite(domain='photos.peifeng.phd', answer=str(conventions.VIP_LAN_V6)),
    )


def test_both_families_are_rewritten() -> None:
    """A LAN client that prefers IPv6 must not fall through to the public answer.

    AdGuard answers a rewrite only for the family of its answer, so a v4-only
    rewrite leaves AAAA resolving to the cloud path (RFC 6724).
    """
    answers = {entry.answer for entry in rewrites([Route(host='tube', exposure=Exposure.SPLIT, zones=('ucw.phd',))])}

    assert answers == {str(conventions.VIP_LAN_V4), str(conventions.VIP_LAN_V6)}


def test_an_iot_route_is_answered_by_the_media_vip() -> None:
    # Attaching to the media gateway *is* the "IoT may reach this" decision.
    route = Route(host='tube', exposure=Exposure.IOT, zones=('ucw.phd',))

    assert {entry.answer for entry in rewrites([route])} == {
        str(conventions.VIP_MEDIA_V4),
        str(conventions.VIP_MEDIA_V6),
    }


def test_a_lan_only_route_is_rewrite_only() -> None:
    """No public record, but the name still has to resolve on the LAN.

    Publishing nothing is what keeps the LAN service census out of public
    resolvers; the rewrite is the only thing that makes the name work.
    """
    route = Route(host='golinks', exposure=Exposure.LAN_ONLY, zones=('ucw.phd',))

    assert route.public is False
    assert len(rewrites([route])) == 2
