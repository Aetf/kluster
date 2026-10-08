"""`AdGuardClients`: an instance's persistent clients, as a set keyed by name.

**A client is the API's own client object**, as `GET /control/clients` reports
it under `clients`. What the instance generates or measures is not read: the
runtime clients under `auto_clients`, `supported_tags`, a client's stored UID
and its WHOIS data, and the deprecated `safesearch_enabled`, which repeats
`safe_search.enabled`.

**Identifiers compare in the order the instance keeps them.** It parses each
identifier into one kind and reports them by kind -- addresses, then subnets,
then MAC addresses, then ClientIDs -- each kind sorted, so a declaration's own
order is not drift; tags it sorts too. `check` refuses an identifier that parses
as no kind, and one two clients share.

**Each endpoint addresses one client, so a write reconciles against a fresh
read**, never against the stored outputs:

1.  `POST /control/clients/delete` first, for a name nothing declares, since
    an add or an update whose name or identifiers clash with another client's
    is refused.
2.  `POST /control/clients/update` for a changed client. It is whole-object:
    the instance rebuilds the client from the body alone, so a flag left out
    becomes false, and keeps the stored UID. So every field is sent. A client
    that only gives up identifiers is updated before the rest, so an
    identifier moving to another declared client is free by the time that
    client is written; two clients swapping identifiers is still refused.
3.  `POST /control/clients/add` last.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from operator import itemgetter
from typing import Any, TypedDict, cast, final

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import Api
from kluster.providers.adguard.base import ENDPOINT, INSTANCE, SETUP_ENDPOINT, AdGuardProvider, missing_or_extra
from kluster.providers.adguard.filtering import SafeSearch

__all__ = ('CLIENTS', 'AdGuardClients', 'AdGuardClientsProvider', 'Client', 'canonical_ids')

#: The persistent clients.
CLIENTS = 'clients'


class Client(TypedDict):
    """A persistent client, in `/control/clients`' own names."""

    name: str
    #: Addresses, subnets, MAC addresses and ClientIDs.
    ids: list[str]
    tags: list[str]
    upstreams: list[str]
    use_global_settings: bool
    filtering_enabled: bool
    parental_enabled: bool
    safebrowsing_enabled: bool
    safe_search: SafeSearch
    use_global_blocked_services: bool
    blocked_services: list[str]
    #: The instance's weekly schedule object, as `AdGuardFiltering` takes one.
    blocked_services_schedule: dict[str, Any]
    ignore_querylog: bool
    ignore_statistics: bool
    upstreams_cache_enabled: bool
    upstreams_cache_size: int


_KEYS = Client.__required_keys__
_SAFE_SEARCH_KEYS = SafeSearch.__required_keys__
_LISTS = ('ids', 'tags', 'upstreams', 'blocked_services')

#: A MAC address as Go's `net.ParseMAC` takes one: 6, 8 or 20 octets, as pairs
#: of hex digits joined by `:` or `-`, or as groups of four joined by `.`.
_MAC = re.compile(
    r'(?P<pairs>[0-9a-f]{2}(?P<sep>[:-])[0-9a-f]{2}(?:(?P=sep)[0-9a-f]{2})*)|(?P<quads>[0-9a-f]{4}(?:\.[0-9a-f]{4})+)',
    re.IGNORECASE,
)

#: A ClientID: one hostname label.
_CLIENT_ID = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', re.IGNORECASE)


def _mac(value: str) -> bytes | None:
    match = _MAC.fullmatch(value)
    if match is None:
        return None
    octets = bytes.fromhex(re.sub(r'[:.-]', '', value))
    return octets if len(octets) in (6, 8, 20) else None


def _address(value: str) -> tuple[tuple[int, int, str], str] | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    scope = address.scope_id if isinstance(address, ipaddress.IPv6Address) else None
    return (address.version, int(address), scope or ''), str(address)


def _subnet(value: str) -> tuple[tuple[int, int, int], str] | None:
    head, slash, bits = value.partition('/')
    if not slash or not bits.isdigit() or _address(head) is None:
        return None
    try:
        subnet = ipaddress.ip_interface(value)
    except ValueError:
        return None
    # More bits sort first; within one length, by address.
    return (-subnet.network.prefixlen, subnet.version, int(subnet.ip)), str(subnet)


def canonical_ids(ids: Sequence[str]) -> list[str]:
    """`ids` as the instance reports them: grouped by kind in its order, each kind sorted.

    Raises `ValueError` naming an identifier that parses as no kind.
    """
    addresses: list[tuple[tuple[int, int, str], str]] = []
    subnets: list[tuple[tuple[int, int, int], str]] = []
    macs: list[tuple[bytes, str]] = []
    client_ids: list[str] = []
    for each in ids:
        if (address := _address(each)) is not None:
            addresses.append(address)
        elif (subnet := _subnet(each)) is not None:
            subnets.append(subnet)
        elif (mac := _mac(each)) is not None:
            macs.append((mac, ':'.join(f'{octet:02x}' for octet in mac)))
        elif _CLIENT_ID.fullmatch(each):
            client_ids.append(each.lower())
        else:
            raise ValueError(f'{each!r} is no address, subnet, MAC address or ClientID')
    return [
        *(text for _key, text in sorted(addresses, key=itemgetter(0))),
        *(text for _key, text in sorted(subnets, key=itemgetter(0))),
        *(text for _key, text in sorted(macs, key=itemgetter(0))),
        *sorted(client_ids),
    ]


def _client(value: Any) -> dict[str, Any]:
    held = cast('Mapping[str, Any]', value)
    comparable = {key: held.get(key) for key in _KEYS}
    for key in _LISTS:
        comparable[key] = list(comparable[key] or [])
    comparable['ids'] = canonical_ids(cast('list[str]', comparable['ids']))
    comparable['tags'] = sorted(cast('list[str]', comparable['tags']))
    safe_search = cast('Mapping[str, Any]', comparable['safe_search'] or {})
    comparable['safe_search'] = {key: safe_search.get(key) for key in _SAFE_SEARCH_KEYS}
    return comparable


def _clients(value: Any) -> dict[str, dict[str, Any]]:
    return {str(client['name']): client for client in (_client(each) for each in cast('Sequence[Any]', value or []))}


@final
class AdGuardClientsProvider(AdGuardProvider):
    kind = 'clients'
    sections = (CLIENTS,)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        declared = news.get(CLIENTS)
        if not isinstance(declared, Sequence) or isinstance(declared, str):
            failures.append(dynamic.CheckFailure(CLIENTS, 'clients is not a list'))
            return
        names: set[str] = set()
        owners: dict[str, str] = {}
        for each in cast('Sequence[Any]', declared):
            if not missing_or_extra(CLIENTS, each, _KEYS, failures, label='a client'):
                continue
            client = cast('Mapping[str, Any]', each)
            name = str(client['name'])
            missing_or_extra(CLIENTS, client['safe_search'], _SAFE_SEARCH_KEYS, failures, label=f'{name}: safe_search')
            if not name or name in names:
                failures.append(dynamic.CheckFailure(CLIENTS, f'client name {name!r} is empty or declared twice'))
            names.add(name)
            if not client['ids']:
                failures.append(dynamic.CheckFailure(CLIENTS, f'{name} has no identifier'))
            try:
                ids = canonical_ids(cast('Sequence[str]', client['ids']))
            except ValueError as refused:
                failures.append(dynamic.CheckFailure(CLIENTS, f'{name}: {refused}'))
                continue
            for each_id in ids:
                if (owner := owners.setdefault(each_id, name)) != name:
                    failures.append(dynamic.CheckFailure(CLIENTS, f'{each_id} is both {owner} and {name}'))

    def _comparable(self, section: str, value: Any) -> object:
        return _clients(value)

    def _read(self, api: Api) -> dict[str, Any]:
        listed = cast('Mapping[str, Any]', api.get('clients'))
        return {CLIENTS: list(_clients(listed.get('clients')).values())}

    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        live = _clients(self._read(api)[CLIENTS])
        declared = {str(client['name']): client for client in cast('Sequence[Mapping[str, Any]]', news[CLIENTS])}
        for name in live:
            if name not in declared:
                api.post('clients/delete', {'name': name})
        changed = [
            (name, client) for name, client in declared.items() if name in live and live[name] != _client(client)
        ]
        changed.sort(key=lambda pair: not set(_client(pair[1])['ids']) <= set(live[pair[0]]['ids']))
        for name, client in changed:
            api.post('clients/update', {'name': name, 'data': dict(client)})
        for name, client in declared.items():
            if name not in live:
                api.post('clients/add', dict(client))


@final
class AdGuardClients(dynamic.Resource, module='adguard', name='AdGuardClients'):
    """One instance's persistent clients, as a set."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    clients: pulumi.Output[list[dict[str, Any]]]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        setup_endpoint: pulumi.Input[str],
        clients: pulumi.Input[Sequence[Client]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `instance` holds exactly these persistent clients."""
        super().__init__(
            AdGuardClientsProvider(),
            name,
            {INSTANCE: instance, ENDPOINT: endpoint, SETUP_ENDPOINT: setup_endpoint, CLIENTS: clients},
            opts,
        )
