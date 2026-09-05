"""The census as data: what it carries, what it dropped, what stays unique.

These assertions are about the declaration, not about Pulumi, so they need no
runtime — which is the point of keeping records as plain data. The unit under
test is the block: records that appear together, in every zone of one set, and
the per-zone view derived from them.
"""

import subprocess
import sys
from collections import Counter

import pytest

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
from kluster.components.dns.legacy import LEGACY
from kluster.components.dns.record import Block, Record, a, cname, txt, zone_records

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


@pytest.mark.parametrize('label', ['abacus.hosts', 'jupyter', 'mc'])
def test_the_import_census_dropped_its_dead_weight(label: str) -> None:
    # The Abacus host is gone; jupyter's backend was that host, and the
    # Minecraft server it fronted is gone too.
    for zone in conventions.ALL_ZONES:
        assert label not in _labels(zone), zone


@pytest.mark.parametrize('label', ['login', 'k8s', 'test', 'files', 'mcmap', 'archvps.stats'])
def test_a_name_no_application_owns_is_published_by_nobody(label: str) -> None:
    """Six names the VPS served that nothing will claim after it retires.

    A legacy block is deleted when its application migrates and declares the
    same name against the cluster anchor; a block with no application to do
    that would never be deleted by anything. Each of these is unpublished
    outright instead, which is a deletion in its own right rather than tidying.
    """
    for zone in conventions.ALL_ZONES:
        assert label not in _labels(zone), zone


@pytest.mark.parametrize('label', ['mon', 'bt'])
def test_a_name_whose_owner_exists_but_whose_component_does_not_is_kept(label: str) -> None:
    """The other half of the cut: a block with an owner only wants an address.

    Neither name has a component declaring it yet — monitoring is rebuilt
    fresh rather than migrated, and the host qbittorrent claims `bt` when it
    migrates — so without this nothing would notice either being deleted along
    with the six that have no owner at all. Deleting one unpublishes a name
    something is about to claim, which is the mirror of the drop above.
    """
    assert label in _labels(conventions.ZONE_PRIMARY)


def test_every_legacy_block_carries_exactly_one_applications_names() -> None:
    """The module is cut so that a migration deletes one block, whole.

    Every block here is the primary's own except the website co-host's, which
    are its own and always were; a block spanning both would be one no single
    migration could delete.
    """
    for block in LEGACY:
        assert block.zones in (conventions.PRIMARY_ONLY, ('unlimitedcodeworks.xyz',)), block.zones


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
    labels = {record.label for record in overlay_records()}

    assert (f'{conventions.overlay.MEMBER_UDM}.{conventions.OVERLAY_LABEL}' in labels) == (
        conventions.overlay.MEMBER_UDM in {entry.name for entry in conventions.overlay.ROSTER}
    )


def test_a_members_record_carries_the_address_its_roster_entry_holds() -> None:
    # The roster is the only source: an entry has a concrete address whichever
    # of the two shapes it is, so nothing about this block waits on a run.
    member = 'Aetf-Arch-Homelab'
    label = f'{overlay_label(member)}.{conventions.OVERLAY_LABEL}'

    record = next(record for record in overlay_records() if record.label == label)

    assert record.content == str(conventions.overlay.member(member).address)
    assert record.ttl == conventions.ANCHOR_TTL


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

    loaded = subprocess.run([sys.executable, '-c', probe], capture_output=True, check=True, text=True)

    assert loaded.stdout.split() == []


def test_overlay_labels_are_dns_labels() -> None:
    # Central's names are display names: they carry case and spaces, and two
    # members on the roster today carry both. The label the block sits under
    # is published, so our own identifier for it may be renamed and its value
    # may not: moving it renames one live record per rostered member.
    assert conventions.OVERLAY_LABEL == 'zt'
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
    for key in (
        f'{conventions.ANCHOR_CLUSTER}-a',
        f'{conventions.ANCHOR_CLUSTER}-aaaa',
        f'{conventions.ANCHOR_VIP1}-a',
    ):
        assert anchors[key].ttl == conventions.ANCHOR_TTL


def test_the_spf_record_is_quoted() -> None:
    """An unquoted SPF string is split on its spaces by the API.

    It then comes back as several character-strings and stops matching.
    """
    spf = next(record for record in _records(conventions.ZONE_PRIMARY) if record.resource_key == 'spf')

    assert spf.content == '"v=spf1 include:_spf.google.com ~all"'


def test_the_mail_zones_carry_one_identical_block() -> None:
    """Both Workspace domains take the same exchangers, SPF, DKIM and DMARC.

    The zones are named once, on the block, rather than the block being
    rebuilt per zone with a parameter — which is what blurred that they are
    identical. The one mail record that is genuinely per-zone is the Workspace
    DKIM key, because one is issued per domain.
    """
    shared = {record.resource_key for record in WORKSPACE_MAIL}

    assert shared
    for zone in MAIL_ZONES:
        assert shared <= _keys(zone), zone
        assert 'dkim-google' in _keys(zone), zone
    google_dkim = {
        zone: next(record.content for record in _records(zone) if record.resource_key == 'dkim-google')
        for zone in MAIL_ZONES
    }
    assert len(set(google_dkim.values())) == len(MAIL_ZONES)


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
    """jiahui.id is a Google Site: nothing here knows what may issue for it.

    It has no CAA in production, and inventing a pin that current issuance
    does not satisfy would break the site at its next renewal.
    """
    assert 'jiahui.id' not in ZONE_ISSUERS
    assert [record for record in _records('jiahui.id') if record.type == 'CAA'] == []


def test_every_zone_is_classified_or_deliberately_unpinned() -> None:
    """A new zone must not silently inherit someone else's issuance policy."""
    assert set(ZONE_ISSUERS) <= set(conventions.ALL_ZONES)
    assert set(conventions.ALL_ZONES) - set(ZONE_ISSUERS) == {'jiahui.id'}


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


@pytest.mark.parametrize('label', ['photos', 'matrix', 'syncapi'])
def test_the_unproxied_records_stay_unproxied(label: str) -> None:
    """Proxy-off is a requirement, not an oversight.

    Large uploads (photos) and non-HTTP ports (matrix federation, syncthing's
    discovery and relay) do not survive the proxy.
    """
    record = next(record for record in _records(conventions.ZONE_PRIMARY) if record.label == label)

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
    origin = {record.resource_key for record in _web_origin().records}

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
    origin = {record.resource_key for record in _web_origin().records}

    assert origin
    for zone in (*conventions.WEB_ZONES, *conventions.PARKED_ZONES):
        assert origin <= _keys(zone), zone


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
    copied = {f'archvps.{conventions.ANCHOR_LABEL}-a'} | {record.resource_key for record in overlay_records()}

    assert copied
    for zone in conventions.ALL_ZONES:
        if zone in conventions.PRIMARY_ONLY:
            assert copied <= _keys(zone), zone
        else:
            assert copied & _keys(zone) == set(), zone
