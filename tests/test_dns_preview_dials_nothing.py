"""A preview of the `dns` program dials nothing, so no job that previews it joins the overlay.

A `pulumi preview` without `--refresh` hands a dynamic provider three calls:
`configure`, then `check` and `diff` for each resource it manages. A pull
request's previews of `dns` join no overlay on that premise (framework/ci.md
§2): if any of those calls dialed a resolver, the preview would need the
overlay to pass, and a provider that started to would fail every such preview
the day it landed. This suite turns that into a failure here instead.

**Defined over what the program declares.** The program is declared against
mocks with a census of one route, so that every kind of dynamic resource it
can declare is declared, and every provider a declared resource carries in
`__provider` is revived the way the plugin revives it -- unpickled, then
configured -- and handed `check` and `diff` for that resource. A provider kind
the program comes to declare is held without being named here.

**Every connection is refused.** Opening a socket to any address, and resolving
any name, raises as a refused connection does, and every attempt is recorded,
so a provider that catches the refusal and carries on is caught by the record
rather than by its error. The bridged providers the program uses are not
dynamic providers and are not called here: their plugins dial public endpoints,
which a runner reaches without the overlay.
"""

from __future__ import annotations

import base64
import pickle
import socket
from typing import TYPE_CHECKING, Any, NoReturn

import pulumi
import pulumi.dynamic as dynamic
import pytest
from mock_monitor import Recorder, declaring, run_under_backstop

from kluster import conventions

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The property a dynamic resource carries its pickled provider in, as the
#: plugin reads it (`pulumi.dynamic.__main__.PROVIDER_KEY`).
PROVIDER_KEY = '__provider'
#: The key of the wrapper a secret property is registered in, and where its
#: value sits inside it (the SDK's `rpc._special_sig_key`).
SIGNATURE_KEY = '4dabf18193072939515e22adb298388d'
SECRET_VALUE = 'value'

#: One route answered on both sides, so the census implies a rewrite on every
#: resolver and the program declares every dynamic kind it has.
ROUTE = conventions.routes.Route(host='photos', internet=conventions.routes.PROXIED, lan=conventions.routes.REWRITE)
EXPOSURE = conventions.routes.Exposure(routes=(ROUTE,))

#: The addresses the `physical` stack hands the anchors (documentation ranges).
PHYSICAL_OUTPUTS = {
    conventions.PHYSICAL_OUTPUTS.cluster_endpoint: '203.0.113.10',
    conventions.PHYSICAL_OUTPUTS.cluster_endpoint_v6: '2001:db8::10',
    conventions.PHYSICAL_OUTPUTS.vip1: '203.0.113.20',
}


class AppliedPhysical(Recorder):
    """A `physical` that has run: its stack reference hands out the anchors' addresses."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': PHYSICAL_OUTPUTS}
        return {}


class AnyKey(dict[str, Any]):
    """A stack configuration that holds every key, as the stack's own holds every key its providers read.

    Its values are stand-ins: what a provider reads in `configure` is a login
    or an address it dials with, and nothing it is handed here is dialed.
    """

    def get(self, key: str, default: Any = None) -> Any:
        return f'stand-in for {key}'


def _plain(value: Any) -> Any:
    """A registered property as the plugin hands it to an operation: a secret unwrapped, a number a float."""
    if isinstance(value, dict) and SIGNATURE_KEY in value:
        return _plain(value[SECRET_VALUE])
    if isinstance(value, dict):
        return {key: _plain(each) for key, each in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [_plain(each) for each in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


@pytest.fixture
def dials(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every connection and every name lookup refused, and recorded in the list this yields."""
    attempts: list[str] = []

    def refuse_connect(_socket: socket.socket, address: Any) -> NoReturn:
        attempts.append(f'connect {address!r}')
        raise ConnectionRefusedError(f'connecting to {address!r}: connection refused')

    def refuse_connect_ex(_socket: socket.socket, address: Any) -> int:
        attempts.append(f'connect {address!r}')
        return 111

    def refuse_lookup(host: Any, *_args: Any, **_kwargs: Any) -> NoReturn:
        attempts.append(f'resolve {host!r}')
        raise socket.gaierror(socket.EAI_NONAME, f'resolving {host!r}: refused')

    monkeypatch.setattr(socket.socket, 'connect', refuse_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse_connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse_lookup)
    yield attempts


@pytest.mark.asyncio
async def test_every_dynamic_provider_the_dns_program_declares_previews_without_dialing(dials: list[str]) -> None:
    """`configure`, `check` and `diff`, for every resource of every dynamic kind, open no connection.

    `check` is called as a create's is, with no old inputs, and as an update's
    is, with the checked inputs as the old ones; `diff` is the plain diff of a
    resource whose declaration has not moved, the one every preview with no
    change runs. Each call has to return, and no call may have tried to dial.
    """
    from kluster.stacks import dns
    from kluster.stacks.dns import CLOUDFLARE_API_TOKEN

    pulumi.runtime.set_all_config({f'kluster:{CLOUDFLARE_API_TOKEN}': 'a-zones-token'})
    monitor = await run_under_backstop(AppliedPhysical(), stack='dns', preview=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(conventions.routes, 'EXPOSURES', {'photos': EXPOSURE})
        async with declaring():
            await dns.main()

    dynamic_resources = [declared for declared in monitor.declared if PROVIDER_KEY in declared.inputs]
    # A read that stopped finding the rewrites would pass on nothing.
    assert dynamic_resources, 'the dns program declared no dynamic resource'

    called: list[str] = []
    for declared in dynamic_resources:
        inputs = _plain(declared.inputs)
        provider = pickle.loads(base64.b64decode(str(inputs.pop(PROVIDER_KEY))))
        assert isinstance(provider, dynamic.ResourceProvider), declared.name
        provider.configure(dynamic.ConfigureRequest(config=dynamic.Config(AnyKey(), pulumi.get_project())))
        created = provider.check({}, inputs)
        updated = provider.check(created.inputs, inputs)
        _ = provider.diff(declared.name, updated.inputs, updated.inputs)
        called.append(declared.name)

    assert dials == []
    assert called == [declared.name for declared in dynamic_resources]
