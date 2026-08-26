"""The dns program as a whole, declared against mocks.

What it catches is wiring rather than data: that every zone is declared with
its records, that the anchors carry the physical stack's addresses rather
than literals, and that the rewrites are emitted from the route census.
"""

import asyncio
from typing import Any, cast

import pulumi
import pytest_asyncio
from pulumi.runtime.stack import wait_for_rpcs

from kluster import conventions

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VIP1_ADDRESS = '203.0.113.20'
ACCOUNT_ID = 'cf-account'

ZONE = 'cloudflare:index/zone:Zone'
DNSSEC = 'cloudflare:index/zoneDnssec:ZoneDnssec'
RECORD = 'cloudflare:index/dnsRecord:DnsRecord'

#: Every resource the program declared: (type, logical name, inputs).
declared: list[tuple[str, str, dict[str, Any]]] = []


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        declared.append((args.typ, args.name, outputs))
        if args.typ == 'pulumi:pulumi:StackReference':
            outputs['outputs'] = {
                'cluster_endpoint': LB_ADDRESS,
                'cluster_endpoint_v6': LB_ADDRESS_V6,
                'vip1': VIP1_ADDRESS,
            }
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


@pytest_asyncio.fixture(scope='module', autouse=True)
async def stack() -> None:
    pulumi.runtime.set_all_config({'kluster:cloudflareAccountId': ACCOUNT_ID})
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='dns', preview=False)
    from kluster.stacks import dns

    # Declaring a resource only schedules its registration; without draining
    # those the mocks have seen nothing and every assertion below would pass
    # vacuously. This program declares without awaiting anything, so its
    # registrations are still unstarted tasks when it returns — and draining
    # them wholesale is not an option, because the queue is process-global and
    # holds the deliberately failing outputs another module built. Hence the
    # snapshot: await exactly the tasks this program added.
    before = asyncio.all_tasks()

    await dns.main()

    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def _all(typ: str) -> dict[str, dict[str, Any]]:
    return {name: inputs for kind, name, inputs in declared if kind == typ}


def _records_of(zone: str) -> dict[str, dict[str, Any]]:
    return {name: inputs for name, inputs in _all(RECORD).items() if name.startswith(f'{zone}-')}


def test_every_zone_is_declared_once() -> None:
    assert set(_all(ZONE)) == set(conventions.ALL_ZONES)
    assert all(inputs['account'] == {'id': ACCOUNT_ID} for inputs in _all(ZONE).values())


def test_every_zone_is_signed() -> None:
    # DNSSEC is per-zone and free; a zone that quietly lacks it is the zone
    # nobody notices.
    signed = _all(DNSSEC)

    assert set(signed) == {f'{zone}-dnssec' for zone in conventions.ALL_ZONES}
    assert all(inputs['status'] == 'active' for inputs in signed.values())


def test_records_are_declared_by_their_fully_qualified_name() -> None:
    # Cloudflare's API takes the full name; a relative one silently becomes
    # `label.zone.zone`.
    for zone in conventions.ALL_ZONES:
        for inputs in _records_of(zone).values():
            assert inputs['name'] == zone or inputs['name'].endswith(f'.{zone}'), zone


def test_the_cluster_anchor_carries_the_load_balancer_address() -> None:
    """The anchor is the only record whose content is a machine fact.

    Every app record is a CNAME to it, so this is the one edge where the
    physical stack's output reaches DNS.
    """
    anchor = _records_of(conventions.ZONE_PRIMARY)[f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-a']

    assert anchor['content'] == LB_ADDRESS
    assert anchor['ttl'] == conventions.ANCHOR_TTL


def test_the_cluster_anchor_is_dual_stack() -> None:
    """The load balancer answers on both families, and this is what says so.

    An A-only anchor would publish an IPv4-only front door: every app record
    is a CNAME to this name, so the families it carries are the families the
    whole estate is reachable on.
    """
    anchor = _records_of(conventions.ZONE_PRIMARY)[f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-aaaa']

    assert anchor['type'] == 'AAAA'
    assert anchor['content'] == LB_ADDRESS_V6
    assert anchor['ttl'] == conventions.ANCHOR_TTL


def test_the_anchors_live_only_in_the_primary_zone() -> None:
    # A rebuild moves one record, not one per zone: the mirrors' app records
    # are CNAMEs to the primary's anchor. Names are fully qualified, so this
    # asks about the name the mirror's own copy would have.
    for zone in conventions.ALL_ZONES:
        if zone == conventions.ZONE_PRIMARY:
            continue
        declared_names = {record['name'] for record in _records_of(zone).values()}
        for anchor in (conventions.ANCHOR_CLUSTER, conventions.ANCHOR_VIP1):
            assert f'{anchor}.{zone}' not in declared_names, zone


def test_the_vip_anchor_is_declared_and_is_v4_only() -> None:
    """The dedicated VIP has no IPv6 counterpart to publish.

    It is a reserved public IPv4 that OCI 1:1-NATs onto a secondary private
    address (architecture.md §3.2); no such mechanism exists for v6, so an
    AAAA here would name an address nothing answers on.
    """
    records = _records_of(conventions.ZONE_PRIMARY)
    anchor = records[f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-a']

    assert anchor['content'] == VIP1_ADDRESS
    assert f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-aaaa' not in records


def test_structured_records_travel_as_data_not_content() -> None:
    # SRV and CAA are the two types Cloudflare refuses as a content string.
    structured = [inputs for inputs in _all(RECORD).values() if inputs['type'] in ('SRV', 'CAA')]

    assert structured
    for inputs in structured:
        assert inputs.get('content') is None
        assert inputs['data']


def test_no_record_still_points_at_the_retired_host() -> None:
    """The import census dropped Abacus and everything that named it.

    Its address surviving anywhere would mean a record was ported by hand.
    """
    contents = {str(inputs.get('content')) for inputs in _all(RECORD).values()}

    assert not any('141.212.111.192' in content for content in contents)


def test_no_rewrite_is_declared_while_no_app_declares_a_route() -> None:
    """The rewrites follow the route census, and it is empty until `apps` lands.

    It also means the stack deploys before the AdGuard credential exists,
    which is the state it is in today.
    """
    assert not _all('pulumi-python:dynamic:Resource')
