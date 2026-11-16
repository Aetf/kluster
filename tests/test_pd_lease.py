"""The PD lease read: its parser against the recorded lease, and its provider against a device that is not there.

The parser's cases hold it to the lease as the gateway wrote it
(`tests/data/pd.leases`: the device's file verbatim, its prefix rewritten
under `2001:db8::/32`) and to every refusal the format's observations make
(`kluster.providers.pd_lease`). The provider's cases run its real code
against a stand-in session that is a dictionary of files and a record of
what it was asked: what is doubled is the wire, and nothing above it.

A provider under test is configured first, because one in production is: the
plugin deserializes it and calls `configure` before any operation reaches it
(framework/pulumi.md §5.3 E2).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import IPv6Network
from pathlib import Path
from typing import Any, final

import pulumi.dynamic as dynamic
import pytest
from pulumi.runtime import rpc

from kluster import conventions
from kluster.components.gateway import NETWORK_BRIDGES
from kluster.components.gateway.prefix import WAN_INTERFACE
from kluster.providers import pd_lease
from kluster.providers.configured import PROVIDER_VERSION, SESSION
from kluster.providers.device_files import ssh

#: The lease as the gateway held it, with `2601:600:8583:` rewritten to
#: `2001:db8:83:`.
RECORDED = (Path(__file__).parent / 'data' / 'pd.leases').read_text()

#: The project the configuration key is namespaced by.
PROJECT = 'kluster'

#: The client credential, as configuration hands it over. Nothing parses it:
#: the stand-in session never authenticates.
CREDENTIAL = 'a-private-key'

#: Where the stand-in device answers, and the key it is pinned to.
HOST = 'gateway.invalid'
HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLE'


@final
@dataclass
class Device:
    """A device holding files, and a record of every verb a session asked of it."""

    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    asked: list[str] = field(default_factory=list[str])
    sessions: list[ssh.Device] = field(default_factory=list[ssh.Device])

    def open(self, device: ssh.Device) -> AbstractAsyncContextManager[ssh.Transport]:
        self.sessions.append(device)

        @asynccontextmanager
        async def session() -> AsyncGenerator[ssh.Transport]:
            yield Session(self)

        return session()


@final
class Session:
    """`ssh.Transport` against the stand-in device: a read answers, every other verb is recorded and refused."""

    def __init__(self, device: Device) -> None:
        self.device: Device = device

    async def read(self, path: str) -> bytes | None:
        self.device.asked.append(f'read {path}')
        return self.device.files.get(path)

    async def stat(self, path: str) -> ssh.FileStat | None:
        self.device.asked.append(f'stat {path}')
        raise AssertionError('the read stats nothing')

    async def write(self, path: str, data: bytes, *, mode: str, owner: str | None) -> str:
        self.device.asked.append(f'write {path}')
        raise AssertionError('the read writes nothing')

    async def remove(self, path: str, *, written: str | None = None) -> None:
        self.device.asked.append(f'remove {path}')
        raise AssertionError('the read removes nothing')

    async def run(self, command: str) -> ssh.CommandResult:
        self.device.asked.append(f'run {command}')
        raise AssertionError('the read runs nothing')


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> Device:
    """The device holding the recorded lease, reached instead of a real one."""
    fake = Device(files={pd_lease.LEASES: RECORDED.encode()})
    monkeypatch.setattr(pd_lease, 'open_transport', fake.open)
    return fake


def provider(credential: str = CREDENTIAL) -> pd_lease.DelegatedPrefixLeaseProvider:
    """A provider as an operation receives one: revived, then handed the configuration."""
    instance = pd_lease.DelegatedPrefixLeaseProvider()
    config = dynamic.Config({f'{PROJECT}:{pd_lease.PRIVATE_KEY_CONFIG}': credential}, PROJECT)
    instance.configure(dynamic.ConfigureRequest(config=config))
    return instance


def declared(**overrides: Any) -> dict[str, Any]:
    """What the program sends: the connection and the WAN, and no observed value, which the SDK leaves out."""
    return {'host': HOST, 'port': 22, 'username': 'root', 'host_key': HOST_KEY, 'wan': WAN_INTERFACE} | overrides


def checked(props: dict[str, Any] | None = None) -> dict[str, Any]:
    return provider().check({}, declared() if props is None else props).inputs


# -- the parser ---------------------------------------------------------------


def test_the_recorded_lease_reads_as_the_prefix_and_every_bridges_subnet() -> None:
    lease = pd_lease.parse(RECORDED, WAN_INTERFACE)

    assert lease.prefix == IPv6Network('2001:db8:83:1710::/60')
    assert lease.subnets == {
        'br0': IPv6Network('2001:db8:83:1710::/64'),
        'br2': IPv6Network('2001:db8:83:1711::/64'),
        'br5': IPv6Network('2001:db8:83:1712::/64'),
        'br7': IPv6Network('2001:db8:83:1713::/64'),
    }


def test_a_renewal_that_keeps_the_prefix_reads_the_same() -> None:
    """The lifetimes move on every renewal and say nothing about the prefix, so they are not read into it."""
    renewed = RECORDED.replace('1794796588156', '1794796590000')

    assert renewed != RECORDED
    assert pd_lease.parse(renewed, WAN_INTERFACE) == pd_lease.parse(RECORDED, WAN_INTERFACE)


def test_the_worker_address_derived_at_the_recorded_id_is_the_one_the_worker_formed() -> None:
    """The derivation against the recorded lease: id 3 under its /60 is its `br7` line, and the worker's address on it.

    The cluster network held id 3 when the lease was read, and the worker's
    address read on the cluster then was `2601:600:8583:1713:5054:ff:fe07:a`,
    here under the same rewrite as the lease. So the derivation the program
    uses at the declared id reproduces both readings at the id they were taken
    at.
    """
    lease = pd_lease.parse(RECORDED, WAN_INTERFACE)
    at_recorded_id = conventions.delegated_subnet(lease.prefix, 3)

    assert at_recorded_id == lease.subnets[NETWORK_BRIDGES[conventions.CLUSTER_VLAN]]
    assert str(conventions.eui64_address(at_recorded_id, conventions.HOMELAB_NODE_MAC)) == (
        '2001:db8:83:1713:5054:ff:fe07:a'
    )


def test_lines_for_another_wan_are_not_read_into_this_ones_delegation() -> None:
    other = RECORDED + 'eth9 br0 2001:db8:84::/48 2001:db8:84:10::/64 1 1\n'

    assert pd_lease.parse(other, WAN_INTERFACE) == pd_lease.parse(RECORDED, WAN_INTERFACE)
    assert pd_lease.parse(other, 'eth9').prefix == IPv6Network('2001:db8:84::/48')


def test_blank_lines_are_not_lines() -> None:
    assert pd_lease.parse(f'\n{RECORDED}\n\n', WAN_INTERFACE) == pd_lease.parse(RECORDED, WAN_INTERFACE)


def _line(number: int, replacement: str) -> str:
    """The recorded lease with its line `number` (from one) replaced."""
    lines = RECORDED.splitlines()
    lines[number - 1] = replacement
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize(
    ('text', 'refusal', 'named'),
    [
        (
            _line(2, 'eth8 br2 2001:db8:83:1710::/60 2001:db8:83:1711::/64 1794796588156'),
            pd_lease.LeaseMisread,
            'line 2',
        ),
        (_line(3, 'eth8 br5 2001:db8:83:1710::/60 2001:db8:83:1712::/64 1 1 extra'), pd_lease.LeaseMisread, 'line 3'),
        (_line(2, 'eth8 br2 not-a-prefix 2001:db8:83:1711::/64 1 1'), pd_lease.LeaseMisread, 'line 2'),
        (_line(2, 'eth8 br2 2001:db8:83:1710::/60 2001:db8:83:1711::/80 1 1'), pd_lease.LeaseMisread, 'line 2'),
        (_line(2, 'eth8 br2 2001:db8:83:1710::/60 2001:db8:83:1721::/64 1 1'), pd_lease.LeaseMisread, 'line 2'),
        (_line(2, 'eth8 br2 2001:db8:83:1711::/80 2001:db8:83:1711::/64 1 1'), pd_lease.LeaseMisread, 'line 2'),
        (_line(4, 'eth8 br7 2001:db8:83:1710::/60 2001:db8:83:1713::/64 soon 1'), pd_lease.LeaseMisread, 'line 4'),
        (_line(4, 'eth8 br7 2001:db8:83:1710::/60 2001:db8:83:1712::/64 1 1'), pd_lease.LeaseMisread, 'line 4'),
        (_line(4, 'eth8 br5 2001:db8:83:1710::/60 2001:db8:83:1713::/64 1 1'), pd_lease.LeaseMisread, 'line 4'),
        (_line(4, 'eth8 br7 2001:db8:83:1720::/60 2001:db8:83:1723::/64 1 1'), pd_lease.LeaseMisread, 'line 4'),
        ('eth9 br0 2001:db8:84::/48 2001:db8:84:10::/64 1 1\n', pd_lease.NoDelegatedPrefix, WAN_INTERFACE),
        ('', pd_lease.NoDelegatedPrefix, WAN_INTERFACE),
    ],
    ids=[
        'five fields',
        'seven fields',
        'a prefix that is no network',
        'a subnet that is no /64',
        'a subnet outside its prefix',
        'a prefix longer than a /64',
        'a lifetime that is no integer',
        'a /64 two lines name',
        'a bridge two lines name',
        'two prefixes on one WAN',
        'no line for the WAN',
        'an empty lease',
    ],
)
def test_a_lease_of_a_shape_the_parser_does_not_know_is_refused_by_name(
    text: str, refusal: type[Exception], named: str
) -> None:
    """A format the parser does not know is never read as one it does: the refusal names the path and the line."""
    with pytest.raises(refusal, match=named) as refused:
        _ = pd_lease.parse(text, WAN_INTERFACE)
    assert pd_lease.LEASES in str(refused.value)


# -- the provider -------------------------------------------------------------


def test_check_reads_the_lease_and_nothing_else(device: Device) -> None:
    inputs = checked()

    assert device.asked == [f'read {pd_lease.LEASES}']
    assert inputs[pd_lease.PREFIX] == '2001:db8:83:1710::/60'
    assert inputs[pd_lease.SUBNETS] == {
        'br0': '2001:db8:83:1710::/64',
        'br2': '2001:db8:83:1711::/64',
        'br5': '2001:db8:83:1712::/64',
        'br7': '2001:db8:83:1713::/64',
    }


def test_the_session_dials_the_declared_device_with_the_configured_credential_and_pin(device: Device) -> None:
    _ = checked()

    assert [(it.host, it.port, it.username, it.host_key, it.private_key) for it in device.sessions] == [
        (HOST, 22, 'root', HOST_KEY, CREDENTIAL)
    ]


def test_check_stamps_the_session_and_the_version(device: Device) -> None:
    inputs = checked()

    assert inputs[SESSION].startswith(f'root@{HOST}:22#')
    assert inputs[PROVIDER_VERSION] == pd_lease.VERSION


def test_a_device_without_the_lease_is_refused_by_name(device: Device) -> None:
    device.files.clear()

    with pytest.raises(pd_lease.NoLease, match=pd_lease.LEASES):
        _ = checked()


def test_a_preview_whose_device_is_not_known_yet_reads_nothing_and_knows_nothing(device: Device) -> None:
    inputs = checked(declared(host=rpc.UNKNOWN))

    assert device.asked == []
    assert inputs[pd_lease.PREFIX] == rpc.UNKNOWN
    assert inputs[pd_lease.SUBNETS] == rpc.UNKNOWN
    assert provider().diff('eth8', checked(), inputs).changes is None


def test_a_moved_prefix_is_an_update_and_nothing_is_replaced(device: Device) -> None:
    olds = checked()
    device.files[pd_lease.LEASES] = RECORDED.replace('2001:db8:83:171', '2001:db8:84:171').encode()
    news = checked()

    result = provider().diff('eth8', olds, news)
    assert (result.changes, result.replaces) == (True, [])


def test_a_renewal_that_keeps_the_prefix_is_no_change(device: Device) -> None:
    olds = checked()
    device.files[pd_lease.LEASES] = RECORDED.replace('1794796588156', '1794796590000').encode()

    assert provider().diff('eth8', olds, checked()).changes is False


def test_create_update_read_and_delete_open_no_session(device: Device) -> None:
    """The observation is `check`'s alone; every other operation hands back what it was given and dials nothing."""
    props = checked()
    device.asked.clear()
    device.sessions.clear()
    instance = provider()

    created = instance.create(props)
    updated = instance.update('eth8', props, props)
    read = instance.read('eth8', props)
    instance.delete('eth8', props)

    assert (created.id, created.outs, updated.outs, read.id, read.outs) == (WAN_INTERFACE, props, props, 'eth8', props)
    assert (device.asked, device.sessions) == ([], [])
