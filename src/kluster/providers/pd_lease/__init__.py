"""The site's delegated prefix, read off the gateway's PD lease (physical/gateway.md §4.2).

The ISP delegates a prefix to the gateway's WAN, and the firmware numbers one
/64 of it onto each network that takes its IPv6 from that prefix. The
controller reports the per-network /64s and nothing that carries the
delegated prefix or its length; the one place that holds both is the
DHCPv6 client's lease on the device, `LEASES`. So this package is one
resource, `DelegatedPrefixLease`, that reads that file on every run and
reports what it holds: `prefix`, the delegated prefix with its length, and
`subnets`, the /64 each bridge holds on it.

**The read writes nothing.** It is an observation, and the observation is
made in `check`, which the engine runs in every preview and every update: the
two values are added to the checked inputs, which is what the engine compares
and what a preview renders (framework/pulumi.md §5.3 E4, E8), so a prefix
the ISP moved is a diff a plain preview shows, with no refresh.

-   `check` reads the file over the device's SSH session, with the transport's
    `read` verb alone, and parses it. A preview whose declared inputs are
    still unknown reads nothing and reports both values unknown.
-   `diff` compares the declared inputs, the stamps and the two observed
    values (`COMPARED`), and plans an update for any of them; never a
    replacement, since the resource puts nothing anywhere.
-   `create` and `update` send nothing and return the checked inputs; `read`
    returns the state as stored, so `check` is the one place that observes;
    `delete` does nothing.

**The session is the device's own**: the address, the user and the pinned
host key are declared inputs (`Connection`, as every device resource declares
them), the client key is `PRIVATE_KEY_CONFIG`, read in this provider's own
`configure` and held by nothing else (framework/pulumi.md §5.2), and the
transport is `kluster.providers.device_files.ssh`. The resource's identity is
the WAN interface the prefix is delegated on: the address moving is the same
device dialed elsewhere.

**The lease is internal to UBIOS, so its format is an observation, not a
contract.** Read on UniFi OS 6.0.17 with Network 10.6.109, every line is
`<wan> <bridge> <delegated prefix> <the bridge's /64> <valid> <preferred>`,
the lifetimes being absolute expiries in milliseconds. The parser relies on
exactly this, and refuses by name, with the path and the line number,
anything else:

-   every non-empty line has six whitespace-separated fields;
-   the third field is an IPv6 network no longer than a /64, and the fourth is
    a /64 inside it;
-   every line for the declared WAN names the same third field;
-   no two lines name the same /64, and no two lines on the declared WAN name
    the same bridge;
-   the fifth and sixth fields are integers.

A missing file, and a file with no line for the declared WAN, are refused by
name too. The lifetimes are parsed only to check the line's shape and are not
kept: they move on every renewal, and a renewal that keeps the prefix is not a
change.

**This package is a layer that comes out whole.** It works around the
controller reporting no held prefix; the interface it is built toward is the
controller reporting the held delegated prefix with its length beside the
per-network /64s it already reports. The layer is this package, the one
construction in `components/gateway/prefix.py`, and `gateway-health`'s reading
of the lease; nothing else knows the file, its path or its fields. It goes once
a controller field carries the held prefix with its length, reached through a
data source of the pinned provider or a controller read of this repository's
own: the construction reads that field instead, this resource leaves state
with `pulumi state delete` (it wrote nothing, so the order carries no hazard),
and the package and the health reading are deleted. Its tripwires are the
refusals above, at every preview and update; `gateway-health`'s daily reading
of the lease; the suite's verbatim copy of the lease; and the step in
physical/gateway.md §3's "The device's base moved" that re-reads the file and
looks again for that controller field (declarative/physical.md §6).
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from ipaddress import IPv6Network
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic
from pulumi.runtime import rpc

from kluster.providers.configured import STAMPS, ConfiguredProvider, declared_change, has_unknowns
from kluster.providers.device_files import ssh
from kluster.providers.device_files.provider import (
    ADDRESS,
    PIN,
    Connection,
    endpoint,
    run_sync,
)

__all__ = (
    'COMPARED',
    'DECLARED',
    'LEASES',
    'OBSERVED',
    'PREFIX',
    'PRIVATE_KEY_CONFIG',
    'SUBNETS',
    'VERSION',
    'DelegatedPrefixLease',
    'DelegatedPrefixLeaseProvider',
    'Lease',
    'LeaseMisread',
    'NoDelegatedPrefix',
    'NoLease',
    'open_transport',
    'parse',
)

#: Where the device's DHCPv6 client keeps the delegation it holds.
LEASES = '/data/udapi-config/pd.leases'

#: The stack-configuration key the session's credential is read from
#: (`configured`): the device's own key, the one every device resource's
#: session opens with, named here beside the provider that reads it.
PRIVATE_KEY_CONFIG = 'gatewayPrivateKey'

#: The interface the prefix is delegated on: the resource's identity.
WAN = 'wan'

#: What a caller declares: where the device answers, the key it must present,
#: and the WAN whose delegation is read.
DECLARED = (WAN, *ADDRESS, *PIN)

#: The delegated prefix with its length, as `check` read it.
PREFIX = 'prefix'

#: Bridge to the /64 it holds on that prefix, as `check` read it.
SUBNETS = 'subnets'

#: What `check` adds to the checked inputs.
OBSERVED = (PREFIX, SUBNETS)

#: What `diff` compares, and the whole of it.
COMPARED = (*DECLARED, *STAMPS, *OBSERVED)

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '1'

#: How many fields a line of the lease has.
_FIELDS = 6

#: The longest prefix a delegation can be: one /64, which is what each
#: network takes.
_SUBNET_PREFIXLEN = 64


class NoLease(RuntimeError):
    """The device holds no lease file at `LEASES`."""


class NoDelegatedPrefix(RuntimeError):
    """The lease holds no line for the declared WAN: no prefix is delegated on it."""


class LeaseMisread(ValueError):
    """A line of the lease is not of the shape the parser knows, named with the path and the line number."""


@final
@dataclass(frozen=True)
class Lease:
    """What the lease holds for one WAN: the delegated prefix, and the /64 each bridge holds on it."""

    prefix: IPv6Network
    subnets: Mapping[str, IPv6Network]


def _network(text: str, where: str, what: str) -> IPv6Network:
    try:
        return IPv6Network(text)
    except ValueError as exc:
        raise LeaseMisread(f'{where}: {what} {text!r} is not an IPv6 network: {exc}') from None


def parse(text: str, wan: str, *, path: str = LEASES) -> Lease:
    """The delegation `text` holds for `wan`, or a refusal naming `path` and the line (the module docstring)."""
    prefixes: set[IPv6Network] = set()
    subnets: dict[str, IPv6Network] = {}
    seen: dict[IPv6Network, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        fields = line.split()
        if not fields:
            continue
        where = f'{path} line {number}'
        if len(fields) != _FIELDS:
            raise LeaseMisread(f'{where} has {len(fields)} fields, where a lease line has {_FIELDS}: {line!r}')
        interface, bridge, delegated, held, valid, preferred = fields
        prefix = _network(delegated, where, 'the delegated prefix')
        if prefix.prefixlen > _SUBNET_PREFIXLEN:
            raise LeaseMisread(f'{where}: the delegated prefix {prefix} is longer than a /{_SUBNET_PREFIXLEN}')
        subnet = _network(held, where, "the bridge's subnet")
        if subnet.prefixlen != _SUBNET_PREFIXLEN or not subnet.subnet_of(prefix):
            raise LeaseMisread(f'{where}: {subnet} is not a /{_SUBNET_PREFIXLEN} inside {prefix}')
        for lifetime in (valid, preferred):
            if not lifetime.isdigit():
                raise LeaseMisread(f'{where}: the lifetime {lifetime!r} is not an integer')
        if subnet in seen:
            raise LeaseMisread(f'{where} names {subnet}, which line {seen[subnet]} names too')
        seen[subnet] = number
        if interface != wan:
            continue
        if bridge in subnets:
            raise LeaseMisread(f'{where} is a second line for {bridge} on {wan}')
        prefixes.add(prefix)
        if len(prefixes) > 1:
            raise LeaseMisread(
                f'{where}: {wan} holds two delegated prefixes, {" and ".join(map(str, sorted(prefixes)))}'
            )
        subnets[bridge] = subnet
    if not prefixes:
        raise NoDelegatedPrefix(f'{path} holds no line for {wan}, so no prefix is delegated on it')
    return Lease(prefix=prefixes.pop(), subnets=subnets)


def open_transport(device: ssh.Device) -> AbstractAsyncContextManager[ssh.Transport]:
    """Open the session the read runs through.

    The one seam between the provider and a real device: a test replaces this
    module attribute with a fake, and everything above it is the code that runs
    in production.
    """
    return ssh.connect(device)


@final
class DelegatedPrefixLeaseProvider(ConfiguredProvider):
    """The delegation the device holds, observed in `check`; every other operation sends nothing."""

    private_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.private_key = str(config.require(PRIVATE_KEY_CONFIG))

    def _credential(self) -> str:
        return self.private_key

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return endpoint(props)

    def _version(self) -> str:
        return VERSION

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        declared = {key: news.get(key) for key in DECLARED}
        if has_unknowns(declared):
            observed: dict[str, Any] = dict.fromkeys(OBSERVED, rpc.UNKNOWN)
        else:
            lease = run_sync(self._read(news))
            observed = {PREFIX: str(lease.prefix), SUBNETS: {bridge: str(net) for bridge, net in lease.subnets.items()}}
        return self._stamp({**news, **observed}, [])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        return dynamic.CreateResult(id_=str(props[WAN]), outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return dynamic.ReadResult(id_=id_, outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        return dynamic.DiffResult(changes=declared_change(olds, news, COMPARED), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        # The outs replace the stored output bag (framework/pulumi.md §5.3 E9),
        # so state holds what the latest `check` read.
        return dynamic.UpdateResult(outs=news)

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        return None

    async def _read(self, props: Mapping[str, Any]) -> Lease:
        device = ssh.Device(
            host=str(props['host']),
            port=int(props['port']),
            username=str(props['username']),
            private_key=self.private_key,
            host_key=str(props['host_key']),
        )
        pulumi.log.info(f'reading the delegated prefix on {props[WAN]} from {LEASES} on {endpoint(props)}')
        async with open_transport(device) as transport:
            text = await transport.read(LEASES)
        if text is None:
            raise NoLease(f'{endpoint(props)} holds no {LEASES}, so its delegated prefix cannot be read')
        return parse(text.decode(errors='replace'), str(props[WAN]))


@final
class DelegatedPrefixLease(dynamic.Resource, module='pd_lease', name='DelegatedPrefixLease'):
    """The prefix delegated on one WAN of the device, and the /64 each bridge holds on it, read on every run."""

    #: The delegated prefix with its length, `2001:db8:83:1710::/60` in form.
    prefix: pulumi.Output[str]
    #: Bridge to the /64 it holds.
    subnets: pulumi.Output[dict[str, str]]

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        wan: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the read of `wan`'s delegation on the device `connection` names.

        The observed values are `None` here, which the engine never sees: the
        SDK sends no property that is `None`, and `check` adds both.
        """
        super().__init__(
            DelegatedPrefixLeaseProvider(),
            name,
            {**connection.props(), WAN: wan, PREFIX: None, SUBNETS: None},
            opts,
        )
