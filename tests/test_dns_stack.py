"""The dns program as a whole, declared against mocks.

What it catches is wiring rather than data: that every zone is declared with
its records, that the anchors carry the physical stack's addresses rather
than literals, and that each resolver is handed the one configuration the
censuses imply, through one resource of each kind.

Every run here is under the parent backstop `kluster.main` installs before a
real run declares anything, so a resource the program leaves unparented fails
the run here rather than in `pulumi preview`. The route census is set by each
run rather than inherited: one run holds it empty and one holds a single row,
because what the stack declares in the rule list is a function of that state.
"""

import re
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

import pulumi
import pytest
import pytest_asyncio
import yaml
from mock_monitor import Recorder, declaring, run_under_backstop

from kluster import conventions
from kluster.components.dns.base import overlay_label
from kluster.components.dns.resolver import ResolverConfiguration
from kluster.components.dns.rewrites import overlay_rewrites, user_rules
from kluster.components.dns.zone import ManagedZone
from kluster.components.gateway.container import ADGUARD_GATEWAY_ZONES
from kluster.providers import adguard, configured

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VIP1_ADDRESS = '203.0.113.20'
API_TOKEN = 'a-zones-token'

ZONE = 'cloudflare:index/zone:Zone'
DNSSEC = 'cloudflare:index/zoneDnssec:ZoneDnssec'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'
RESOLVER_CONFIGURATION = ResolverConfiguration.__pulumi_type__
MANAGED_ZONE = ManagedZone.__pulumi_type__


def _dynamic_type(cls: type[Any]) -> str:
    """The type a dynamic resource is declared under; the SDK keeps the stated half on this attribute and nowhere public."""
    return f'pulumi-python:{cls._resource_type_name}'


#: Every kind one instance is configured through, by the suffix its logical name carries.
KINDS: dict[str, str] = {
    'setup': _dynamic_type(adguard.AdGuardSetup),
    'user-rules': _dynamic_type(adguard.AdGuardUserRules),
    'dns-server': _dynamic_type(adguard.AdGuardDnsServer),
    'filtering': _dynamic_type(adguard.AdGuardFiltering),
    'filter-lists': _dynamic_type(adguard.AdGuardFilterLists),
    'clients': _dynamic_type(adguard.AdGuardClients),
    'log-settings': _dynamic_type(adguard.AdGuardLogSettings),
}
SETUP = KINDS['setup']
USER_RULES = KINDS['user-rules']
DNS_SERVER = KINDS['dns-server']
CLIENTS = KINDS['clients']

#: A rewrite as the rule list renders one: its name and what it answers with.
REWRITE_RULE = re.compile(r'\|(?P<name>[^|^]+)\^\$dnsrewrite=NOERROR;(?P<type>[A-Z]+);(?P<answer>\S+)')
#: A blocking rule, and the client it names.
CLIENT_RULE = re.compile(r'\|\|[^^]+\^\$client=(?P<client>\S+)')
#: A per-domain upstream line: the zone, and where its queries go.
FORWARDED = re.compile(r'\[/(?P<zone>[^/]+)/\](?P<upstream>\S+)')

#: The one row the routed run declares: a name answered on both sides,
#: published in the primary zone alone.
ROUTE = conventions.routes.Route(
    host='photos', exposure=conventions.routes.Exposure.SPLIT, zones=conventions.PRIMARY_ONLY
)


class AppliedPhysical(Recorder):
    """A `physical` that has run: its stack reference hands out the addresses."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            outputs = conventions.PHYSICAL_OUTPUTS
            return {
                'outputs': {
                    outputs.cluster_endpoint: LB_ADDRESS,
                    outputs.cluster_endpoint_v6: LB_ADDRESS_V6,
                    outputs.vip1: VIP1_ADDRESS,
                }
            }
        return {}


async def declare_program(routes: tuple[conventions.routes.Route, ...]) -> AppliedPhysical:
    """The whole program, declared once against `routes` and under the backstop."""
    from kluster.stacks import dns
    from kluster.stacks.dns import CLOUDFLARE_API_TOKEN

    pulumi.runtime.set_all_config({f'kluster:{CLOUDFLARE_API_TOKEN}': API_TOKEN})
    monitor = await run_under_backstop(AppliedPhysical(), stack='dns')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(conventions.routes, 'ROUTES', routes)
        async with declaring():
            await dns.main()
    return monitor


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> AppliedPhysical:
    """The program with an empty route census."""
    return await declare_program(())


@pytest_asyncio.fixture(scope='module')
async def routed() -> AppliedPhysical:
    """The program with a census of one row, `ROUTE`."""
    return await declare_program((ROUTE,))


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
    assert Counter(declaration.name for declaration in stack.of_type(ZONE)) == dict.fromkeys(conventions.ALL_ZONES, 1)
    account = conventions.CLOUDFLARE_ACCOUNT.account_id
    assert all(inputs['account'] == {'id': account} for inputs in stack.by_name(ZONE).values())


def test_every_zone_is_protected(stack: AppliedPhysical) -> None:
    """The zone is the registrar-facing object, so a destroy must not reach it.

    Deleting a zone takes the delegation with it and every record below; a
    replace is a delete. Read off the registration request, where the option
    is, and under the zone's own type: the component that holds it carries
    the same name and no such option.
    """
    for zone in conventions.ALL_ZONES:
        assert stack.options_of(zone, ZONE).protect is True, zone


def test_every_zone_moves_from_the_type_state_holds_it_under(stack: AppliedPhysical) -> None:
    """The component carries one alias, to the type the `dns` stack's state holds.

    The zones and their records are imported into state under
    `kluster:dns:zone:ManagedZone`, which is not the type the component states
    (style/pulumi.md, a type token is chosen). A type is part of the URN of
    the component and of every resource beneath it, so without this alias the
    first `up` plans a delete and a create of every zone and record. The
    literal is what state holds, which no edit to this program moves.
    """
    for zone in conventions.ALL_ZONES:
        aliases = stack.options_of(zone, MANAGED_ZONE).aliases
        assert [alias.spec.type for alias in aliases] == ['kluster:dns:zone:ManagedZone'], zone


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


def resource(stack: AppliedPhysical, resolver: conventions.gateway.BridgedService, kind: str) -> dict[str, Any]:
    """What one instance's resource of one kind was declared with."""
    return stack.inputs_of(f'{resolver.name}-{kind}', KINDS[kind])


def answers(rules: list[str]) -> dict[str, set[tuple[str, str]]]:
    """Every name a rule list rewrites, with the record types and answers it gives the name."""
    found: dict[str, set[tuple[str, str]]] = {}
    for rule in rules:
        if match := REWRITE_RULE.fullmatch(rule):
            found.setdefault(match['name'], set()).add((match['type'], match['answer']))
    return found


def test_one_resolver_configuration_per_resolver_with_one_resource_of_each_kind_under_it(
    stack: AppliedPhysical,
) -> None:
    """Their independence is the design: an instance that is down fails its own resources and leaves the other's.

    One component per instance, named after it, and exactly one resource of
    each kind under each, so no endpoint of an instance has two writers.
    Counted per type rather than gathered by name, so a second resource of one
    kind for one instance fails here instead of collapsing into the first.
    """
    assert stack.names(RESOLVER_CONFIGURATION) == {resolver.name for resolver in conventions.gateway.RESOLVERS}
    for kind, typ in KINDS.items():
        declared = Counter(declaration.inputs['instance'] for declaration in stack.of_type(typ))
        assert declared == {resolver.name: 1 for resolver in conventions.gateway.RESOLVERS}, kind
        for resolver in conventions.gateway.RESOLVERS:
            parent = stack.options_of(f'{resolver.name}-{kind}', typ).parent
            assert parent.endswith(f'{RESOLVER_CONFIGURATION}::{resolver.name}'), (kind, parent)


def test_both_instances_are_handed_one_configuration(stack: AppliedPhysical) -> None:
    """alice and bob differ in no setting, so whatever one instance is declared with, the other is too.

    Compared kind by kind with only what names the instance taken out, so a
    rule list, a client or a setting handed to one instance alone fails here.
    """
    where = {'instance', 'endpoint', 'setup_endpoint'}
    alice, bob = conventions.gateway.RESOLVERS
    for kind in KINDS:
        declared = [
            {key: value for key, value in resource(stack, resolver, kind).items() if key not in where}
            for resolver in (alice, bob)
        ]
        assert declared[0] == declared[1], kind


def test_every_kind_depends_on_its_own_instances_setup(stack: AppliedPhysical) -> None:
    """An instance started with no configuration file holds no account until its setup has run."""
    for resolver in conventions.gateway.RESOLVERS:
        setup = stack.one(f'{resolver.name}-setup', SETUP)
        for kind, typ in KINDS.items():
            if kind == 'setup':
                continue
            depends = stack.depends_on(f'{resolver.name}-{kind}', typ)
            assert [urn for urn in depends if urn.endswith(f'::{setup.name}')], (resolver.name, kind, depends)
            assert all(f'::{resolver.name}-setup' in urn for urn in depends if '-setup' in urn), depends


def test_each_resource_names_its_instance_and_where_this_run_reaches_it(stack: AppliedPhysical) -> None:
    """The census name identifies the resource; the two addresses are the census entry's, derived the one way."""
    for resolver in conventions.gateway.RESOLVERS:
        for kind in KINDS:
            inputs = resource(stack, resolver, kind)
            assert inputs['instance'] == resolver.name
            assert inputs['endpoint'] == conventions.gateway.resolver_api_url(resolver)
            assert inputs['setup_endpoint'] == conventions.gateway.resolver_setup_url(resolver)


def test_the_setup_listens_where_the_stack_dials(stack: AppliedPhysical) -> None:
    """`listen.web.port` is the API endpoint's port, or the configured instance answers nowhere the stack asks."""
    for resolver in conventions.gateway.RESOLVERS:
        listen = resource(stack, resolver, 'setup')['listen']
        assert listen['web']['port'] == urlsplit(conventions.gateway.resolver_api_url(resolver)).port
        assert listen['web']['port'] != listen['dns']['port']


def test_a_resource_is_named_after_the_instance_and_never_after_its_address(stack: AppliedPhysical) -> None:
    """A logical name is half of the URN state is keyed by (style/pulumi.md).

    Naming a resource after the address it is written at would make moving an
    instance a delete and a create of everything on it. Neither the address
    nor any label spelled out of it appears -- an address with its dots
    swapped for hyphens is still an address.
    """
    for resolver in conventions.gateway.RESOLVERS:
        address = str(resolver.address)
        for typ in KINDS.values():
            for name in stack.names(typ):
                assert address not in name
                assert '-'.join(address.split('.')) not in name


def test_no_resource_declares_a_credential_or_a_stamp(stack: AppliedPhysical) -> None:
    """What the program declares, and no more.

    The login is the provider's, read in `configure`; the two stamps are added
    by `check` in the plugin's process. A resource that carried either would
    put it in state on both instances, for every kind.
    """
    for resolver in conventions.gateway.RESOLVERS:
        for kind in KINDS:
            inputs = resource(stack, resolver, kind)
            assert not {'username', 'password', configured.SESSION, configured.PROVIDER_VERSION} & set(inputs), kind


def test_the_rule_list_is_rendered_from_the_censuses(stack: AppliedPhysical) -> None:
    """The overlay members, the gateway's own names and the legacy names answer on both instances.

    Stated from the censuses rather than from the derivations, so a run that
    hands a derivation the wrong census, or drops a block, fails here.
    """
    proxy = str(conventions.gateway.CADDY.address)
    for resolver in conventions.gateway.RESOLVERS:
        answered = answers(resource(stack, resolver, 'user-rules')['rules'])
        for entry in conventions.overlay.ROSTER:
            name = f'{overlay_label(entry.name)}.{conventions.OVERLAY_DOMAIN}'
            assert answered[name] == {('A', str(entry.address))}, name
        for name in (
            conventions.gateway.VHOST_CONTROLLER,
            *(each.vhost for each in conventions.gateway.RESOLVERS if each.vhost),
            *(row.host for row in conventions.gateway.LEGACY_VHOSTS),
        ):
            assert answered[name] == {('A', proxy)}, name


def test_a_route_in_the_census_is_answered_on_every_resolver(routed: AppliedPhysical) -> None:
    """The wiring from the route census to the rule list, held on a census of one row.

    What a row implies is `rewrites`' subject (`test_dns_rewrites.py`); what
    is asserted here is that the program hands that derivation the census
    rather than anything else, and hands every instance the result beside the
    rest of the list. The name and the answers are stated independently of the
    derivation, so a run that derives nothing from the census fails here.
    """
    name = f'{ROUTE.host}.{conventions.ZONE_PRIMARY}'
    vip = conventions.LAN_POOL.default_vip
    for resolver in conventions.gateway.RESOLVERS:
        answered = answers(resource(routed, resolver, 'user-rules')['rules'])
        assert answered[name] == {('A', str(vip.v4)), ('AAAA', str(vip.v6))}


def test_the_overlay_rows_reach_both_instances_while_no_app_declares_a_route(stack: AppliedPhysical) -> None:
    """The roster's rewrites owe nothing to the route census, and reach both instances.

    The census is empty in this run because the run sets it so. Grouped by the
    instance a list is declared for, so an instance handed none of them, or a
    different set, fails here rather than disappearing into a union of both.
    """
    overlay = {(entry.domain, 'A', str(entry.answer)) for entry in overlay_rewrites(conventions.overlay.ROSTER)}
    for resolver in conventions.gateway.RESOLVERS:
        answered = answers(resource(stack, resolver, 'user-rules')['rules'])
        declared = {
            (name, typ, answer)
            for name, given in answered.items()
            if name.endswith(f'.{conventions.OVERLAY_DOMAIN}')
            for typ, answer in given
        }
        assert declared == overlay, resolver.name


def test_no_cname_target_is_a_name_a_rule_answers(stack: AppliedPhysical) -> None:
    """An instance resolves a CNAME's target upstream without applying its own rules to it again.

    A target that some rule answers would therefore resolve to the public or
    the device plane's answer for that name, not to the rule's, and the alias
    would point somewhere other than where the list says.
    """
    answered = answers(resource(stack, conventions.gateway.ADGUARD_ALICE, 'user-rules')['rules'])
    targets = {answer for given in answered.values() for typ, answer in given if typ == 'CNAME'}

    assert targets, 'the list declares no alias; the pattern is what broke'
    assert sorted(targets & set(answered)) == []


def test_every_cname_target_is_under_a_zone_forwarded_to_the_gateway(stack: AppliedPhysical) -> None:
    """The target is resolved upstream, and only the gateway's resolver answers the device plane.

    Read from what the DNS server is declared with, so a forwarded zone dropped
    from the settings fails here and not when a client asks for `nas`.
    """
    gateway = str(conventions.CONTAINER_VLAN.require_gateway())
    for resolver in conventions.gateway.RESOLVERS:
        answered = answers(resource(stack, resolver, 'user-rules')['rules'])
        upstreams = resource(stack, resolver, 'dns-server')['dns']['upstream_dns']
        forwarded = {
            match['zone'] for line in upstreams if (match := FORWARDED.fullmatch(line)) and match['upstream'] == gateway
        }
        targets = {answer for given in answered.values() for typ, answer in given if typ == 'CNAME'}
        assert targets
        for target in targets:
            assert any(target.endswith(f'.{zone}') for zone in forwarded), (target, forwarded)


def test_the_forwarded_zones_are_the_ones_physical_renders_into_the_initial_state(stack: AppliedPhysical) -> None:
    """Until the initial state shrinks, both programs declare the zones the gateway's resolver answers.

    `physical` renders them into the template from the same constant, so the
    two cannot name different zones.
    """
    gateway = str(conventions.CONTAINER_VLAN.require_gateway())
    upstreams = resource(stack, conventions.gateway.ADGUARD_ALICE, 'dns-server')['dns']['upstream_dns']

    assert [line for line in upstreams if FORWARDED.fullmatch(line)] == [
        f'[/{zone}/]{gateway}' for zone in ADGUARD_GATEWAY_ZONES
    ]


def test_the_client_set_holds_every_client_a_blocklist_rule_names(stack: AppliedPhysical) -> None:
    """A `$client=` rule naming a client the instance lacks matches nothing, silently.

    Read off the two declarations rather than off the row they are both drawn
    from, so a client set built from a separate list fails here.
    """
    for resolver in conventions.gateway.RESOLVERS:
        named = {
            match['client']
            for rule in resource(stack, resolver, 'user-rules')['rules']
            if (match := CLIENT_RULE.fullmatch(rule))
        }
        held = {client['name'] for client in resource(stack, resolver, 'clients')['clients']}
        assert named, 'the list declares no blocking rule; the pattern is what broke'
        assert named <= held, (named, held)


def test_the_rule_list_renders_what_the_derivation_renders(stack: AppliedPhysical) -> None:
    """The list a resource carries is the rendering whole, header first, and no line typed beside it."""
    rules = resource(stack, conventions.gateway.ADGUARD_ALICE, 'user-rules')['rules']

    assert rules[0] == user_rules(())[0]
    assert all(rule.startswith('! ') or REWRITE_RULE.fullmatch(rule) or CLIENT_RULE.fullmatch(rule) for rule in rules)


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
    package's namespace. It cannot widen to everything: the resolvers'
    configuration is declared through the `pulumi-python` default provider
    (rfc-002 §8.1), and disabling that one would leave it undeclarable.
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
