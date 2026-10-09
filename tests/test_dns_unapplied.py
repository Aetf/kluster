"""The dns program against a `physical` stack that has not handed out its addresses.

The anchors are where this stack reaches across a StackReference, and
`physical` is a placeholder until the cloud site is built: its stack exists and
publishes nothing. What a StackReference reads back in place of an address
then is not an address -- `None` for an output that is absent, an unknown in a
preview of a `physical` a targeted apply left holding Pulumi's unknown
sentinel, `None` for that sentinel in an update, `{}` for a secret the reader
cannot decrypt (framework/pulumi.md §1.4, §3.1) -- and a record's content is
whatever the program hands on. So the anchors refuse each of them by name, in
a preview as in an update, and no record is ever declared with one as its
content: neither an `up` in that window nor a preview read as the plan
publishes `None` as an address.

The ZeroTier host block and the rest of the census are here for the opposite
reason: they reach across nothing at all, so an unusable `physical` costs them
nothing, and this is where that is held.

Every run is under the parent backstop `kluster.main` installs, so a resource a
component leaves unparented fails it here as it would fail `pulumi preview`.
"""

import asyncio
import ipaddress
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_under_backstop
from pulumi.output import UNKNOWN

from kluster import conventions
from kluster.components.dns.base import overlay_label
from kluster.stacks.dns import UnusableAnchorAddress

RECORD = 'cloudflare:index/dnsRecord:DnsRecord'

#: The address records' types, by the IP version their content must be.
FAMILIES = {'A': 4, 'AAAA': 6}

OUTPUTS = conventions.PHYSICAL_OUTPUTS

#: Every output an anchor reads, with the family its record needs, and an
#: address of that family `physical` could publish under it.
ANCHOR_OUTPUTS: dict[str, tuple[int, str]] = {
    OUTPUTS.cluster_endpoint: (4, '203.0.113.10'),
    OUTPUTS.cluster_endpoint_v6: (6, '2001:db8::10'),
    OUTPUTS.vip1: (4, '203.0.113.20'),
}

#: The anchor records, by logical name, each with the output its address is read from.
ANCHOR_RECORDS = {
    f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-a': OUTPUTS.cluster_endpoint,
    f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_CLUSTER}-aaaa': OUTPUTS.cluster_endpoint_v6,
    f'{conventions.ZONE_PRIMARY}-{conventions.ANCHOR_VIP1}-a': OUTPUTS.vip1,
}

#: What a StackReference can read back in place of an IPv4 address: the value,
#: whether a preview or an update is where it is read back, and the part of the
#: refusal that names it. The string values carry a marker the refusal must not
#: quote.
UNUSABLE: dict[str, tuple[object, bool, str]] = {
    # An output `physical` has not published, and in an update the sentinel a
    # targeted apply wrote, plain or encrypted.
    'absent': (None, False, 'absent'),
    # Every output of a `physical` holding the sentinel anywhere, previewed.
    'unknown': (UNKNOWN, True, 'unknown'),
    # A secret the reader cannot decrypt, elided by the engine.
    'elided-secret': ({}, False, 'a mapping'),
    'empty': ('', False, 'an empty string'),
    'hostname': ('marker.example', False, 'not an address'),
    'not-a-string': ([], False, 'a list'),
}


class Physical(Recorder):
    """A `physical` whose stack exists and publishes `published`."""

    def __init__(self, published: dict[str, Any]) -> None:
        super().__init__()
        self.published = published

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': self.published}
        return {}


class Run:
    """One run of the program: what it registered, and the refusal that stopped it, if one did."""

    def __init__(self, monitor: Physical, refused: UnusableAnchorAddress | None) -> None:
        self.monitor = monitor
        self.refused = refused

    def records(self) -> dict[str, dict[str, Any]]:
        return self.monitor.by_name(RECORD)


async def declare(published: dict[str, Any], *, preview: bool) -> Run:
    """The whole program against `published`, the refusal caught and every registration settled.

    The refusal fails one record's registration, and the gather that surfaces
    it does not wait for the rest; they are waited for here, so a case reading
    the recorder reads the whole run rather than whatever had landed by then.
    """
    from kluster.stacks import dns
    from kluster.stacks.dns import CLOUDFLARE_API_TOKEN

    pulumi.runtime.set_all_config({f'kluster:{CLOUDFLARE_API_TOKEN}': 'a-zones-token'})
    monitor = await run_under_backstop(Physical(published), stack='dns', preview=preview)
    before = asyncio.all_tasks()
    refused = None
    try:
        async with declaring():
            await dns.main()
    except UnusableAnchorAddress as error:
        refused = error
    _ = await asyncio.gather(*(asyncio.all_tasks() - before - {asyncio.current_task()}), return_exceptions=True)
    return Run(monitor, refused)


async def declare_reading(output: str, value: object, *, preview: bool) -> Run:
    """The program against a `physical` whose `output` reads back as `value`, the other anchors' as addresses.

    The read is answered here rather than by the mock monitor for `output`,
    because the mock drops an output that is `None` or unknown on its way to
    the reader, and so can only present an absence.
    """
    original = pulumi.StackReference.get_output

    def read_back(reference: pulumi.StackReference, name: pulumi.Input[str]) -> pulumi.Output[Any]:
        return pulumi.Output.from_input(value) if name == output else original(reference, name)

    published = {name: address for name, (_, address) in ANCHOR_OUTPUTS.items()}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.StackReference, 'get_output', read_back)
        return await declare(published, preview=preview)


def non_addresses(run: Run) -> dict[str, object]:
    """Every address record the run declared whose content is not an address of its type's family."""
    found: dict[str, object] = {}
    for name, inputs in run.records().items():
        family = FAMILIES.get(inputs['type'])
        if family is None:
            continue
        content: object = inputs.get('content')
        if isinstance(content, str):
            try:
                if ipaddress.ip_address(content).version == family:
                    continue
            except ValueError:
                pass
        found[name] = content
    return found


@pytest_asyncio.fixture(scope='module', params=[True, False], ids=['preview', 'update'])
async def unapplied(request: pytest.FixtureRequest) -> Run:
    """The program against a `physical` that has never been applied: its stack publishes nothing."""
    preview: bool = request.param
    return await declare({}, preview=preview)


def test_an_unapplied_physical_stops_the_run_at_an_anchor_by_name(unapplied: Run) -> None:
    """The refusal names an output an anchor reads and says it is absent.

    Which of the three surfaces first is the order the reads resolve in, so
    the case asks for one of them rather than for a particular one.
    """
    assert unapplied.refused is not None
    message = str(unapplied.refused)
    assert any(repr(output) in message for output in ANCHOR_OUTPUTS), message
    assert 'absent' in message


def test_no_record_is_declared_with_content_that_is_not_an_address(unapplied: Run) -> None:
    """`str(None)` is `"None"`, and that is the record content a stringified absence would carry."""
    assert non_addresses(unapplied) == {}
    assert set(ANCHOR_RECORDS).isdisjoint(unapplied.records())


def test_the_overlay_block_is_the_whole_roster_and_waits_on_no_other_stack(unapplied: Run) -> None:
    """The `*.zt` block is decided by code, names and addresses alike.

    The roster crosses as a module and carries both halves of every entry, so
    an unapplied `physical` costs this block nothing — neither its shape nor
    its contents. A program that read the members out of the stack reference
    instead could declare nothing at all here.
    """
    records = unapplied.records()

    for entry in conventions.overlay.ROSTER:
        name = f'{conventions.ZONE_PRIMARY}-{overlay_label(entry.name)}.{conventions.OVERLAY_LABEL}-a'
        assert records[name]['content'] == str(entry.address), entry.name


def test_the_rest_of_the_census_is_declared_too(unapplied: Run) -> None:
    # The base records are literals: nothing about them should have been held
    # up by the one part of the program that reads another stack.
    names = set(unapplied.records())

    assert all(any(name.startswith(f'{zone}-') for name in names) for zone in conventions.ALL_ZONES)


@pytest.mark.parametrize('found', UNUSABLE)
@pytest.mark.asyncio
async def test_an_anchor_output_that_is_not_an_address_stops_the_run_by_name(found: str) -> None:
    """Each thing a StackReference can read back in place of an address is refused, and never quoted.

    The other two anchors read addresses, so the refusal is the one output's
    and the records they carry are declared.
    """
    value, preview, named = UNUSABLE[found]
    output = OUTPUTS.cluster_endpoint

    run = await declare_reading(output, value, preview=preview)

    assert run.refused is not None
    message = str(run.refused)
    assert repr(output) in message
    assert named in message
    if isinstance(value, str) and value:
        assert value not in message
    assert non_addresses(run) == {}
    declared = {name for name, read in ANCHOR_RECORDS.items() if read != output}
    assert set(ANCHOR_RECORDS) & set(run.records()) == declared


@pytest.mark.parametrize('output', ANCHOR_OUTPUTS)
@pytest.mark.asyncio
async def test_each_anchor_takes_only_its_own_family(output: str) -> None:
    """An address of the other family is refused under the output's own name.

    An A record cannot carry an IPv6 address nor an AAAA an IPv4 one, so the
    family is part of what the read checks, per output.
    """
    family, _ = ANCHOR_OUTPUTS[output]
    other_family, other = (6, '2001:db8::7') if family == 4 else (4, '198.51.100.7')

    run = await declare_reading(output, other, preview=True)

    assert run.refused is not None
    message = str(run.refused)
    assert repr(output) in message
    assert f'an IPv{other_family} address' in message
    assert other not in message
    assert non_addresses(run) == {}
