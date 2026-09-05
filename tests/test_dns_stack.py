"""The dns program as a whole, declared against mocks.

What it catches is wiring rather than data: that every zone is declared with
its records, that the anchors carry the physical stack's addresses rather
than literals, and that the rewrites are emitted from the route census.
"""

from collections import Counter
from typing import Any

import pulumi
import pytest_asyncio
import yaml
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.components.dns.base import overlay_label

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VIP1_ADDRESS = '203.0.113.20'
API_TOKEN = 'a-zones-token'

ZONE = 'cloudflare:index/zone:Zone'
DNSSEC = 'cloudflare:index/zoneDnssec:ZoneDnssec'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'
RESOLVER_REWRITES = 'kluster:components:dns:rewrites:ResolverRewrites'


class AppliedPhysical(Recorder):
    """A `physical` that has run: its stack reference hands out the addresses."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {
                'outputs': {
                    'cluster_endpoint': LB_ADDRESS,
                    'cluster_endpoint_v6': LB_ADDRESS_V6,
                    'vip1': VIP1_ADDRESS,
                }
            }
        return {}


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> AppliedPhysical:
    from kluster.stacks import dns
    from kluster.stacks.dns import CLOUDFLARE_API_TOKEN

    pulumi.runtime.set_all_config({f'kluster:{CLOUDFLARE_API_TOKEN}': API_TOKEN})
    monitor = await run_with(AppliedPhysical(), stack='dns')
    async with declaring():
        await dns.main()
    return monitor


def records_of(stack: AppliedPhysical, zone: str) -> dict[str, dict[str, Any]]:
    """Every record the program declared in one zone, by logical name."""
    return {name: inputs for name, inputs in stack.by_name(RECORD).items() if name.startswith(f'{zone}-')}


def test_every_zone_is_declared_once(stack: AppliedPhysical) -> None:
    """Once each, counted rather than gathered into a set.

    A second declaration of one zone is a real failure mode -- two `ManagedZone`
    components for one name -- and it is invisible to every record keyed by the
    logical name, the second declaration merely overwriting the first. Only the
    declarations themselves carry it.
    """
    assert Counter(declaration.name for declaration in stack.of_type(ZONE)) == {
        zone: 1 for zone in conventions.ALL_ZONES
    }
    account = conventions.CLOUDFLARE_ACCOUNT.account_id
    assert all(inputs['account'] == {'id': account} for inputs in stack.by_name(ZONE).values())


def test_every_zone_is_signed(stack: AppliedPhysical) -> None:
    # DNSSEC is per-zone and free; a zone that quietly lacks it is the zone
    # nobody notices.
    signed = stack.by_name(DNSSEC)

    assert set(signed) == {f'{zone}-dnssec' for zone in conventions.ALL_ZONES}
    assert all(inputs['status'] == 'active' for inputs in signed.values())


def test_records_are_declared_by_their_fully_qualified_name(stack: AppliedPhysical) -> None:
    # Cloudflare's API takes the full name; a relative one silently becomes
    # `label.zone.zone`.
    for zone in conventions.ALL_ZONES:
        for inputs in records_of(stack, zone).values():
            assert inputs['name'] == zone or inputs['name'].endswith(f'.{zone}'), zone


def test_the_cluster_anchor_carries_the_load_balancer_address(stack: AppliedPhysical) -> None:
    """The anchor is the only record whose content is a machine fact.

    Every app record is a CNAME to it, so this is the one edge where the
    physical stack's output reaches DNS.
    """
    anchor = records_of(stack, conventions.ZONE_PRIMARY)[f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-a']

    assert anchor['content'] == LB_ADDRESS
    assert anchor['ttl'] == conventions.ANCHOR_TTL


def test_the_cluster_anchor_is_dual_stack(stack: AppliedPhysical) -> None:
    """The load balancer answers on both families, and this is what says so.

    An A-only anchor would publish an IPv4-only front door: every app record
    is a CNAME to this name, so the families it carries are the families the
    whole installation is reachable on.
    """
    anchor = records_of(stack, conventions.ZONE_PRIMARY)[
        f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-aaaa'
    ]

    assert anchor['type'] == 'AAAA'
    assert anchor['content'] == LB_ADDRESS_V6
    assert anchor['ttl'] == conventions.ANCHOR_TTL


def test_the_anchors_live_only_in_the_primary_zone(stack: AppliedPhysical) -> None:
    # A rebuild moves one record, not one per zone: every application record
    # in every zone is a CNAME to the primary's anchor. Names are fully
    # qualified, so this asks about the name a copy would have.
    for zone in conventions.ALL_ZONES:
        if zone == conventions.ZONE_PRIMARY:
            continue
        declared_names = {record['name'] for record in records_of(stack, zone).values()}
        for anchor in (conventions.ANCHOR_CLUSTER, conventions.ANCHOR_VIP1):
            assert f'{anchor}.{zone}' not in declared_names, zone


def test_the_vip_anchor_is_declared_and_is_v4_only(stack: AppliedPhysical) -> None:
    """The dedicated VIP has no IPv6 counterpart to publish.

    It is a reserved public IPv4 that OCI 1:1-NATs onto a secondary private
    address (architecture.md §3.2); no such mechanism exists for v6, so an
    AAAA here would name an address nothing answers on.
    """
    records = records_of(stack, conventions.ZONE_PRIMARY)
    anchor = records[f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-a']

    assert anchor['content'] == VIP1_ADDRESS
    assert f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-aaaa' not in records


def overlay_record(stack: AppliedPhysical, zone: str, member: str) -> dict[str, Any]:
    return records_of(stack, zone)[f'{zone}-{overlay_label(member)}.{conventions.OVERLAY_LABEL}-a']


def test_the_overlay_block_is_the_roster_and_reaches_across_no_reference(stack: AppliedPhysical) -> None:
    """Names and addresses alike come from the roster, which is code.

    The anchors are the only edge where a `physical` output reaches this
    stack. A member is a name and an address in the same entry, so this whole
    block is known while previewing, and a `physical` that has never run costs
    it nothing.
    """
    member = 'Aetf-Arch-Homelab'

    published = {
        name.removeprefix(f'{conventions.ZONE_PRIMARY}-').removesuffix(f'.{conventions.OVERLAY_LABEL}-a')
        for name in records_of(stack, conventions.ZONE_PRIMARY)
        if name.endswith(f'.{conventions.OVERLAY_LABEL}-a')
    }

    assert published == {overlay_label(entry.name) for entry in conventions.overlay.ROSTER}
    assert overlay_record(stack, conventions.ZONE_PRIMARY, member)['content'] == str(
        conventions.overlay.member(member).address
    )


def test_the_overlay_block_is_declared_in_the_primary_zone_alone(stack: AppliedPhysical) -> None:
    """Private addresses in public DNS are published once, not once per zone.

    Every overlay name a configuration file anywhere in this installation
    holds is the primary's, so a copy in another zone is a second publication
    of the same private address with no reader.
    """
    for zone in conventions.ALL_ZONES:
        names = {record['name'] for record in records_of(stack, zone).values()}
        expected = {
            f'{overlay_label(entry.name)}.{conventions.OVERLAY_LABEL}.{zone}' for entry in conventions.overlay.ROSTER
        }

        assert (expected <= names) is (zone in conventions.PRIMARY_ONLY), zone
        assert (expected & names == set()) is (zone not in conventions.PRIMARY_ONLY), zone


def test_a_parked_zone_is_declared_with_its_web_origin_and_its_caa(stack: AppliedPhysical) -> None:
    """The whole of what the program declares in a parked zone, by record type.

    A parked zone holds nothing of this installation's: its apex and `www` are
    addressed at the legacy VPS and answered by that machine's catch-all, and
    its CAA authorizes the certificate the edge mints for a zone it hosts.
    Read as a set of types, this is the shape that says no application name
    and no host address is declared there.
    """
    for zone in conventions.PARKED_ZONES:
        declared = records_of(stack, zone).values()
        by_type = {inputs['type'] for inputs in declared}

        assert by_type == {'A', 'CNAME', 'CAA'}, zone
        assert {inputs['name'] for inputs in declared if inputs['type'] != 'CAA'} == {zone, f'www.{zone}'}, zone


def test_structured_records_travel_as_data_not_content(stack: AppliedPhysical) -> None:
    # SRV and CAA are the two types Cloudflare refuses as a content string.
    structured = [inputs for inputs in stack.by_name(RECORD).values() if inputs['type'] in ('SRV', 'CAA')]

    assert structured
    for inputs in structured:
        assert inputs.get('content') is None
        assert inputs['data']


def test_no_record_still_points_at_the_retired_host(stack: AppliedPhysical) -> None:
    """The import census dropped Abacus and everything that named it.

    Its address surviving anywhere would mean a record was ported by hand.
    """
    contents = {str(inputs.get('content')) for inputs in stack.by_name(RECORD).values()}

    assert not any('141.212.111.192' in content for content in contents)


def test_a_rewrite_component_is_declared_for_every_resolver_the_census_names(stack: AppliedPhysical) -> None:
    """One per instance, unconditionally, and named after the instance.

    Their independence is the design: an instance that is down fails its own
    resources and leaves the other's converged, and as two sibling components
    that is what the resource tree says. Which instances there are is the
    gateway census's answer, so the program spells out neither.
    """
    declared = stack.names(RESOLVER_REWRITES)

    assert declared == {f'rewrites-{resolver.name}' for resolver in conventions.gateway.RESOLVERS}


def test_no_rewrite_is_declared_while_no_app_declares_a_route(stack: AppliedPhysical) -> None:
    """The rewrites follow the route census, and it is empty until `apps` lands.

    With no row to write, the components declare no dynamic resource, so the
    provider process never starts and the AdGuard login is never read — which
    is what lets the stack deploy before that login exists, the state it is in
    today.
    """
    assert not stack.by_name('pulumi-python:dynamic:Resource')


def test_every_zone_and_record_is_signed_by_one_explicit_provider(stack: AppliedPhysical) -> None:
    """The zones token is one credential over a set of zones, so one provider.

    A provider built inside a zone component would be reached into by the
    other zones, which is the test rfc-002 §8.1 gives for what a stack program
    owns. Every record and every DNSSEC state below inherits it through its
    zone, and none of them names it.
    """
    zone_provider = f'{conventions.CLUSTER_NAME}-cloudflare'

    signed = [d for d in stack.declared if d.typ.startswith('cloudflare:index/')]

    assert signed, 'the program declared no Cloudflare resources at all'
    for declaration in signed:
        assert zone_provider in declaration.provider, f'{declaration.name} is not signed by the zones provider'


def test_the_zones_token_is_read_where_that_provider_is_built(stack: AppliedPhysical) -> None:
    """The credential and the provider it opens are one thing, at one line.

    The key is this project's rather than the provider package's. A
    `cloudflare:` entry in a committed stack file is indistinguishable from the
    ambient configuration this repository has removed everywhere else, and an
    unqualified key cannot be mistaken for one.
    """
    from kluster.stacks import dns

    built = stack.of_type('pulumi:providers:cloudflare')[0].inputs
    assert built['apiToken']['value'] == API_TOKEN
    assert ':' not in dns.CLOUDFLARE_API_TOKEN


def test_the_mint_writes_the_key_the_stack_reads() -> None:
    """One key, named in three places, held equal here.

    The value is delivered by `credentials derived cloudflare-zones mint`, so a
    key renamed in the stack alone leaves the command filling a slot nothing
    reads while the stack refuses by name for a value that is present under its
    old one. Neither half has to be run to see that they agree: the mint's key,
    the key the program asks its configuration for, and the key the committed
    stack file carries are compared directly.
    """
    from kluster.scripts.credentials import derived, pulumi_config
    from kluster.stacks import dns

    assert dns.CLOUDFLARE_API_TOKEN == derived.API_TOKEN_KEY
    committed = (pulumi_config.project_dir() / f'Pulumi.{derived.ZONES_STACK}.yaml').read_text()
    assert f'\n  {_project_name()}:{derived.API_TOKEN_KEY}:\n' in committed


def test_the_account_the_zones_are_declared_against_is_not_configuration() -> None:
    """A fact with one home is not copied into a second.

    The account names the account rather than opening it, so it is code beside
    the tenancy OCID and the B2 region, and it is not among the committed
    stack's configuration keys. The name it used to be addressed as answers
    with where the fact went rather than with "no such row".
    """
    from kluster.scripts.credentials import derived, pulumi_config, slots

    committed = (pulumi_config.project_dir() / f'Pulumi.{derived.ZONES_STACK}.yaml').read_text()
    assert 'cloudflareAccountId' not in committed
    assert 'cloudflareAccountId' in slots.RETIRED


def test_default_providers_stay_disabled_for_the_package_this_key_left() -> None:
    """The list names `cloudflare` and never becomes `*`.

    Naming the package is what makes an explicit provider the only Cloudflare
    provider there is, which is half of why the token no longer sits in that
    package's namespace. It cannot widen to everything: the dynamic rewrites
    are declared through the `pulumi-python` default provider (rfc-002 §8.1),
    and disabling that one would leave them undeclarable.
    """
    from kluster.scripts.credentials import derived, pulumi_config

    committed = (pulumi_config.project_dir() / f'Pulumi.{derived.ZONES_STACK}.yaml').read_text()
    config = yaml.safe_load(committed)['config']

    assert config['pulumi:disable-default-providers'] == ['cloudflare']


def _project_name() -> str:
    """What `pulumi config set` prefixes an unqualified key with, out of `Pulumi.yaml`."""
    from kluster.scripts.credentials import pulumi_config

    manifest = (pulumi_config.project_dir() / 'Pulumi.yaml').read_text()
    return next(line.removeprefix('name:').strip() for line in manifest.splitlines() if line.startswith('name:'))
