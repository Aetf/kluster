"""The census as data: what it carries, what it dropped, what stays unique.

These assertions are about the declaration, not about Pulumi, so they need no
runtime — which is the point of keeping records as plain data. The exception
is the case that holds a field the declaration alone carries to what the zone
component declares, which only a run can show. The unit under test is the
block: records that appear together, in every zone of one set, and the
per-zone view derived from them.
"""

import ipaddress
import sys
from collections import Counter
from collections.abc import Iterable
from typing import Any

import process_sessions
import pytest
from mock_monitor import Recorder, declaring, run_under_backstop

from kluster import conventions
from kluster.components.dns import base
from kluster.components.dns.base import (
    CLOUDFLARE_ISSUERS,
    CLUSTER_ISSUERS,
    MAIL_ZONES,
    WORKSPACE_MAIL,
    ZONE_ISSUERS,
    overlay_label,
    overlay_records,
)
from kluster.components.dns.legacy import ANCHOR_ARCHVPS, LEGACY
from kluster.components.dns.record import Block, Record, a, aaaa, caa, cname, mx, srv, txt, zone_records
from kluster.components.dns.zone import ManagedZone

RECORD = 'cloudflare:index/dnsRecord:DnsRecord'

#: Three addresses stand in for the `physical` stack's, so the anchor block is
#: part of what the census assertions see.
ANCHORS = base.AnchorAddresses(cluster_v4='203.0.113.10', cluster_v6='2001:db8::10', vip1_v4='203.0.113.20')


def _blocks() -> tuple[Block, ...]:
    """Everything the program declares from, in the order it composes it."""
    return (*base.blocks(anchors=ANCHORS), *LEGACY)


def _records(zone: str) -> tuple[Record, ...]:
    return zone_records(zone, _blocks())


def _keys(zone: str) -> set[str]:
    return {record.resource_key for record in _records(zone)}


def _labels(zone: str) -> set[str]:
    return {record.label for record in _records(zone)}


def _anchor_aliases(blocks: Iterable[Block]) -> list[Record]:
    """Every CNAME to an anchor.

    That is what an application name is (`conventions.ANCHOR_LABEL`), and it
    is the website's `www` too, which is the web origin's.
    """
    anchors = f'.{conventions.ANCHOR_LABEL}.{conventions.ZONE_PRIMARY}'
    return [
        record
        for block in blocks
        for record in block.records
        if record.type == 'CNAME' and isinstance(record.content, str) and record.content.endswith(anchors)
    ]


def _web_origin() -> Block:
    """The one block the four zones that answer for a website share.

    Selected rather than named, and selected strictly: a second block over
    that same set would be a second answer to "what is the web origin", and
    picking the first would hide it.
    """
    served = {*conventions.WEB_ZONES, *conventions.PARKED_ZONES}
    matched = [block for block in base.BASE_RECORDS if set(block.zones) == served]

    assert len(matched) == 1, [block.zones for block in matched]
    return matched[0]


def test_every_zone_a_block_names_is_a_zone_the_program_declares() -> None:
    """A block header is the only place a zone name is written, so it is checked.

    The program loops over `ALL_ZONES` and derives each zone's records from the
    blocks: a zone named in a header and nowhere else is a block that declares
    nothing, silently, and a zone in `ALL_ZONES` that no block names is an
    empty zone. Both are invisible without this.
    """
    named = {zone for block in _blocks() for zone in block.zones}

    assert named == set(conventions.ALL_ZONES)


def test_a_zones_records_are_the_blocks_that_name_it_and_nothing_else() -> None:
    """`zone_records` is the whole of the per-zone view Cloudflare's API takes.

    A zone is not written down anywhere but in the headers of the blocks it
    appears in, so a zone no block names carries nothing rather than falling
    back to something.
    """
    apex = a('@', '203.0.113.1')
    shared = cname('www', 'example.test')
    only = txt('_x', 'one', key='x')
    blocks = (Block(('one.test', 'two.test'), (apex, shared)), Block(('two.test',), (only,)))

    assert zone_records('one.test', blocks) == (apex, shared)
    assert zone_records('two.test', blocks) == (apex, shared, only)
    assert zone_records('three.test', blocks) == ()


def test_a_block_is_carried_by_every_zone_of_its_set_and_no_other() -> None:
    """The derivation is the whole of what makes a block a block.

    Records that appear together do so in every zone of one set. A zone
    outside the set carrying one of its records, or a zone inside it missing
    one, is a per-zone table wearing a block's clothes. Identity is what is
    compared, because two zones legitimately hold different records under the
    same state key -- the family zone's apex and the web origin's, say.
    """
    blocks = _blocks()
    for block in blocks:
        for zone in conventions.ALL_ZONES:
            carried = {id(record) for record in block.records} <= {id(record) for record in zone_records(zone, blocks)}

            assert carried is (zone in block.zones), f'{zone}: {[record.resource_key for record in block.records]}'


def test_state_keys_are_unique_within_a_zone() -> None:
    """Two records sharing a key would be one resource, silently.

    The apex holds several TXT records and five MX; label and type alone do
    not identify them, so the model carries an explicit key and this is the
    check that it was set. Since the unit is the block, this also covers two
    blocks whose zone sets overlap colliding on a key — which a per-zone
    mapping could not do.
    """
    for zone in conventions.ALL_ZONES:
        keys = Counter(record.resource_key for record in _records(zone))
        assert [key for key, count in keys.items() if count > 1] == [], zone


def test_every_legacy_block_carries_exactly_one_applications_names() -> None:
    """The module is cut so that a migration deletes one block, whole.

    Every block here is the primary's own except the website co-host's, which
    are its own and always were; a block spanning both would be one no single
    migration could delete.
    """
    (cohost,) = (zone for zone in conventions.WEB_ZONES if zone != conventions.ZONE_PRIMARY)
    for block in LEGACY:
        assert block.zones in (conventions.PRIMARY_ONLY, (cohost,)), block.zones


def test_every_member_of_the_overlay_roster_is_published() -> None:
    """The roster is the census, so a member without a record cannot exist.

    The block used to be a hand-maintained table beside the roster, and it had
    drifted from it in both directions — members with no record, and a record
    whose address the overlay had reassigned. Deriving is what makes that a
    state the program cannot be in rather than one somebody has to notice.
    """
    published = {record.label for record in overlay_records()}

    assert published == {
        f'{overlay_label(entry.name)}.{conventions.OVERLAY_LABEL}' for entry in conventions.overlay.ROSTER
    }
    assert len(overlay_records()) == len(conventions.overlay.ROSTER)


def test_a_members_record_carries_the_address_its_roster_entry_holds() -> None:
    # The roster is the only source: an entry has a concrete address whichever
    # of the two shapes it is, so nothing about this block waits on a run.
    member = conventions.overlay.MEMBER_HOMELAB
    label = f'{overlay_label(member)}.{conventions.OVERLAY_LABEL}'

    record = next(record for record in overlay_records() if record.label == label)

    assert record.content == str(conventions.overlay.member(member).address)


#: What comes with the module that declares the ZeroTier resources: the two
#: bridged provider SDKs the gateway is built from, and the SSH client it is
#: configured over. Named here so the cost of reading the roster from that
#: module instead of from `conventions` is stated rather than implied.
GATEWAY_IMPORTS = ('asyncssh', 'pulumi_unifi', 'pulumi_zerotier')


def test_the_records_import_without_the_gateway_behind_them() -> None:
    """The overlay roster is a convention, so declaring records loads no provider.

    `dns.base` reads the roster out of `conventions`, which is plain data.
    Reading it out of the module that admits members by it would put every
    package above into the import graph of a program that only declares DNS
    records — a package dependency on the gateway, bought for a table of names
    and roles.

    A subprocess is the only honest place to ask: by the time this runs, the
    rest of the suite has imported all three anyway.
    """
    probe = (
        'import sys, kluster.components.dns.base; '
        f'print(" ".join(sorted(name for name in {GATEWAY_IMPORTS!r} if name in sys.modules)))'
    )

    loaded = process_sessions.run([sys.executable, '-c', probe], check=True, text=True, timeout=20)

    assert loaded.stdout.split() == []


def test_overlay_labels_are_dns_labels() -> None:
    # Central's names are display names: they carry case and spaces, and two
    # members on the roster today carry both. What the block publishes is a DNS
    # label, so the normalization is the whole of what stands between them.
    assert overlay_label('S26 Ultra') == 's26-ultra'
    assert overlay_label('Pixel 7 Pro') == 'pixel-7-pro'
    for entry in conventions.overlay.ROSTER:
        label = overlay_label(entry.name)
        assert label == label.lower()
        assert ' ' not in label


def test_the_anchor_block_belongs_to_the_census_and_the_program_supplies_addresses() -> None:
    """Labels, families, TTLs and comments are census data like any other.

    What the program knows is three addresses out of the `physical` stack; the
    shape of the records they land in is here, so a reader of the table sees
    every record the primary zone carries in one place.
    """
    anchors = {record.resource_key: record for record in _records(conventions.ZONE_PRIMARY)}

    assert anchors[f'{conventions.ANCHOR_CLUSTER}-a'].content == ANCHORS.cluster_v4
    assert anchors[f'{conventions.ANCHOR_CLUSTER}-aaaa'].content == ANCHORS.cluster_v6
    assert anchors[f'{conventions.ANCHOR_VIP1}-a'].content == ANCHORS.vip1_v4
    assert f'{conventions.ANCHOR_VIP1}-aaaa' not in anchors


def test_the_spf_record_is_quoted() -> None:
    """An unquoted SPF string is split on its spaces by the API.

    It then comes back as several character-strings and stops matching.
    """
    spf = next(record for record in _records(conventions.ZONE_PRIMARY) if record.resource_key == 'spf')

    content = spf.content
    assert isinstance(content, str)
    assert content.startswith('"')
    assert content.endswith('"')
    assert ' ' in content.strip('"'), 'what the quoting protects is the spaces'


def test_the_mail_zones_carry_one_identical_block() -> None:
    """Both Workspace domains take the same exchangers, SPF, DKIM and DMARC.

    The zones are named once, on the block, rather than the block being
    rebuilt per zone with a parameter — which is what blurred that they are
    identical. The one mail record that is genuinely per-zone is the Workspace
    DKIM key, because one is issued per domain.
    """
    shared = {record.resource_key for record in WORKSPACE_MAIL}

    assert shared
    per_zone: dict[str, object] = {}
    for zone in MAIL_ZONES:
        assert shared <= _keys(zone), zone
        (per_zone[zone],) = (
            record.content
            for record in _records(zone)
            if record.resource_key not in shared and record.label.endswith('._domainkey')
        )
    assert len(set(per_zone.values())) == len(MAIL_ZONES)


def _issuers(zone: str, tag: str) -> set[str]:
    return {
        str(record.data['value'])
        for record in _records(zone)
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
    """Nothing here knows what may issue for it, so nothing here pins it.

    Its declaration says why. What is held here is that the CAA table and the
    zone's records agree with it: no row in the table, and no CAA in the zone
    by any other road.
    """
    zone = conventions.ZONE_EXTERNALLY_ISSUED

    assert zone not in ZONE_ISSUERS
    assert [record for record in _records(zone) if record.type == 'CAA'] == []


def test_every_zone_is_classified_or_deliberately_unpinned() -> None:
    """A new zone must not silently inherit someone else's issuance policy."""
    assert set(ZONE_ISSUERS) <= set(conventions.ALL_ZONES)
    assert set(conventions.ALL_ZONES) - set(ZONE_ISSUERS) == {conventions.ZONE_EXTERNALLY_ISSUED}


def test_caa_keys_survive_a_value_with_parameters() -> None:
    """`pki.goog; cansignhttpexchanges=yes` must not become a state name.

    The key names the authority alone, so adding or dropping a parameter is
    an update rather than a replace.
    """
    record = next(
        record
        for record in _records(conventions.ZONE_PRIMARY)
        if record.type == 'CAA' and record.data is not None and str(record.data['value']).startswith('pki.goog')
    )

    assert record.resource_key == 'caa-issue-pki-goog'


def _through_a_proxied_record(blocks: Iterable[Block], zones: Iterable[str]) -> list[str]:
    """Every direct CNAME, by name, whose target the census declares proxied."""
    blocks = tuple(blocks)
    declared: dict[str, list[Record]] = {}
    for zone in zones:
        for record in zone_records(zone, blocks):
            declared.setdefault(record.fqdn(zone), []).append(record)
    found: list[str] = []
    for name, records in declared.items():
        for record in records:
            target = record.content
            if (
                record.type == 'CNAME'
                and not record.proxied
                and isinstance(target, str)
                and any(hop.proxied for hop in declared.get(target, ()))
            ):
                found.append(name)
    return sorted(found)


def test_a_direct_name_resolves_through_no_proxied_record() -> None:
    """A reason not to proxy holds only while the chain behind the name is direct.

    Cloudflare proxies a name when any record on its CNAME chain is proxied
    (developers.cloudflare.com/dns/proxy-status/), so a direct CNAME whose
    target is proxied is served through the proxy whatever its own reason
    says. Every hop the census declares is checked, in every zone.
    """
    reason = 'a port the proxy does not forward'
    census = (
        Block(
            ('one.test',),
            (
                a('origin.hosts', '203.0.113.1'),
                cname('direct', 'origin.hosts.one.test', unproxied=reason),
                a('held.hosts', '203.0.113.2', unproxied=reason),
                cname('kept', 'held.hosts.one.test', unproxied=reason),
            ),
        ),
    )

    assert _through_a_proxied_record(_blocks(), conventions.ALL_ZONES) == []
    assert _through_a_proxied_record(census, ('one.test',)) == ['direct.one.test']


def _proxied_where_the_edge_cannot_reach(blocks: Iterable[Block]) -> list[str]:
    """Every proxied address record whose address is not globally routable."""
    return [
        record.label
        for block in blocks
        for record in block.records
        if record.type in ('A', 'AAAA')
        and record.proxied
        and isinstance(record.content, str)
        and not ipaddress.ip_address(record.content).is_global
    ]


def test_a_record_whose_address_the_edge_cannot_reach_is_published_direct() -> None:
    """The edge cannot reach a private address, so proxying one publishes a name that answers nothing.

    The overlay block is today's instance: its reason rests on its addresses,
    which the census holds, so the relation is checked rather than left to
    the reason's wording.
    """
    census = (Block(('one.test',), (a('member.zt', '10.144.0.9'), a('public', '45.77.144.92'))),)

    assert _proxied_where_the_edge_cannot_reach(_blocks()) == []
    assert _proxied_where_the_edge_cannot_reach(census) == ['member.zt']


def test_a_record_is_proxied_unless_it_says_why_not_and_only_where_the_proxy_can_front_it() -> None:
    # Cloudflare proxies A, AAAA and CNAME records alone; every other type is
    # always DNS-only (developers.cloudflare.com/dns/proxy-status/).
    assert a('@', '203.0.113.1').proxied
    assert aaaa('@', '2001:db8::1').proxied
    assert cname('www', 'example.test').proxied
    assert not cname('www', 'example.test', unproxied='a port the proxy does not forward').proxied
    for record in (
        txt('@', 'v=spf1 -all'),
        mx('@', 'mail.example.test', 10),
        srv('_x._tcp', priority=0, weight=0, port=1, target='example.test'),
        caa('@', tag='issue', value='letsencrypt.org'),
    ):
        assert not record.proxied, record.type


async def _rendered(record: Record) -> dict[str, Any]:
    """What the zone component declares for a zone holding `record` alone."""
    monitor = await run_under_backstop(Recorder(), stack='dns')
    async with declaring():
        ManagedZone('one', zone='one.test', account_id='an-account', records=(record,))
    (inputs,) = monitor.by_name(RECORD).values()
    return inputs


@pytest.mark.asyncio
@pytest.mark.parametrize('comment', ['', 'immich'])
async def test_a_reasons_wording_changes_nothing_a_zone_carries(comment: str) -> None:
    """The reason is the census's: it reaches no field of the record Cloudflare holds.

    Whether a record has one decides `proxied`; what it says reaches nothing,
    so rewording one is no change to the zone, and the dashboard's
    explanation stays the record's comment -- with a comment or without one,
    which is where a renderer falling back to the reason would show.
    """
    one = cname('photos', 'example.test', comment=comment, unproxied='uploads larger than the proxy accepts')
    other = cname('photos', 'example.test', comment=comment, unproxied='bodies the proxy would cap')

    assert await _rendered(one) == await _rendered(other)


def test_a_parked_zone_carries_the_web_origin_and_its_caa_and_nothing_else() -> None:
    """A parked zone serves nothing of this installation's, so it publishes nothing.

    What resolves in one is the apex and `www`, addressed at the legacy VPS
    and answered by that machine's catch-all; those retire with the machine
    and leave the zone dark but for its CAA. CAA is the exception by
    construction -- the policy is a property of the zone, not of what serves
    it -- and the edge mints a certificate for a zone it hosts whether or not
    anything of ours answers in it.
    """
    origin = {record.resource_key for record in _web_origin().records}

    assert origin
    for zone in conventions.PARKED_ZONES:
        assert {record.resource_key for record in _records(zone) if record.type != 'CAA'} == origin, zone


def test_the_website_co_host_carries_no_name_of_the_primarys_applications() -> None:
    """It answers for a website and stops there: no application name, ever.

    Every application here holds one SSO cookie domain and one registered
    redirect URI, so a second hostname for one is a login that loops. An
    application's name in this census is a CNAME to an anchor in the
    primary, other than the website's own `www`. The co-host's names of its
    own are not that, and neither is anything shared with the primary by
    block. The anchor and the overlay block, the other names a copy would
    bring, are held to the primary by the case below.
    """
    (cohost,) = (zone for zone in conventions.WEB_ZONES if zone != conventions.ZONE_PRIMARY)
    origin = {id(record) for record in _web_origin().records}
    primary = [block for block in _blocks() if conventions.ZONE_PRIMARY in block.zones]
    applications = {record.label for record in _anchor_aliases(primary) if id(record) not in origin}

    assert applications
    assert _labels(cohost)
    assert _labels(cohost) & applications == set()


def test_the_legacy_anchor_and_the_overlay_block_are_the_primary_zones_alone() -> None:
    """One record addresses the VPS, and one block publishes private addresses.

    Every CNAME that names the VPS -- in every zone -- targets the primary's
    copy, so a copy elsewhere is a record nothing reads; and an overlay host
    is named by no configuration outside the primary. Publishing either
    anywhere else costs the copies and buys nothing.
    """
    anchor = f'{ANCHOR_ARCHVPS.removesuffix(f".{conventions.ZONE_PRIMARY}")}-a'
    copied = {anchor} | {record.resource_key for record in overlay_records()}

    assert copied
    for zone in conventions.ALL_ZONES:
        if zone in conventions.PRIMARY_ONLY:
            assert copied <= _keys(zone), zone
        else:
            assert copied & _keys(zone) == set(), zone
