"""The AdGuard providers, against a stand-in for one AdGuard Home v0.107.79 instance.

**A provider under test is configured first**, because a provider in production
is: the plugin deserializes it out of a resource's `__provider` property and
calls `configure` before handing it any operation (framework/pulumi.md §5.3
E2). `configured_provider` below does that with a `ConfigureRequest` built the
way the plugin builds one -- the same class, the same project namespace -- so
what the tests exercise is the real ordering rather than attributes set by
hand.

**The stand-in is a model of the release, and it only tightens** (testing.md
§4). `Instance` serves every endpoint the providers reach, starts from what a
fresh v0.107.79 serves, and keeps what the release does that a careless caller
would miss:

1.  `set_rules` replaces the list whole, and answers a body that is not JSON
    with 415.
2.  `filtering/status`, `clients` and the access lists answer an empty list as
    `null`.
3.  `add_url` downloads first and refuses an unreachable or empty list (400). It
    refuses a URL present in either kind (400), and stores every list enabled.
4.  `remove_url` answers success for an absent URL, and `set_url` refuses one
    (400).
5.  `clients/update` rebuilds the client from the body: a `null` flag becomes
    false, and the stored UID is kept. `clients/add` refuses a clashing name or
    identifier, and `clients/delete` refuses an absent name. The instance
    reports identifiers grouped by kind, each kind sorted, and tags sorted.
6.  `dns_info` answers `upstream_mode: ""` for load balancing. `dns_config` sets
    only the fields present, and counts a restart for every request carrying a
    field it restarts on.
7.  A UI pause: `/control/protection` clears it, while `dns_config`'s
    `protection_enabled` leaves it, and `dns_info` reports `false` until it
    expires.
8.  A fresh instance holds v0.107.79's defaults, the two default lists among
    them.
9.  Every endpoint can refuse (401). A refused request changes nothing, and its
    body is plain text, never the payload.
10. A whole number written as a float -- `20.0` -- is refused (400) for every
    field the instance decodes as an integer.

A case catches an operation that reads a refusal as success by expecting the
operation to raise; what the instance holds afterwards is the stand-in's doing,
not the provider's.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pulumi.dynamic as dynamic
import pytest
import requests
from pulumi.runtime import rpc
from shimmed_serialization import serialized

from kluster.providers import adguard_rewrites, configured
from kluster.providers.adguard import base, clients, dns_server, filter_lists, filtering, log_settings, user_rules

INSTANCE = 'adguard-alice'
ENDPOINT = 'http://10.0.5.3:80'
MOVED = 'http://10.0.5.30:80'
USERNAME = 'admin'
PASSWORD = 'a-typed-secret'

#: The project the configuration keys below are namespaced by. An unqualified
#: key is resolved against the running project, which is how the plugin finds
#: it (framework/pulumi.md §5.3 E2).
PROJECT = 'kluster'

#: The status a refusing instance answers with: AdGuard Home's answer to a
#: `/control/` request whose login it does not accept. What a case asks is
#: that an error status is not read as success, so no case turns on which
#: error it is.
REFUSED = 401


# --------------------------------------------------------------------------
# The stand-in.

#: What a fresh v0.107.79 serves, from a throwaway instance started on nothing
#: but the initial state (`http.address`, `dns.bind_hosts`, `dns.port`,
#: `filtering.rewrites_enabled: false`, `schema_version: 34`). Lists the
#: instance computes or measures are left out.
FRESH_DNS: dict[str, Any] = {
    'upstream_dns': ['https://dns10.quad9.net/dns-query'],
    'upstream_dns_file': '',
    'bootstrap_dns': ['9.9.9.10', '149.112.112.10', '2620:fe::10', '2620:fe::fe:10'],
    'fallback_dns': [],
    'protection_enabled': True,
    'ratelimit': 20,
    'ratelimit_subnet_len_ipv4': 24,
    'ratelimit_subnet_len_ipv6': 56,
    'upstream_timeout': 10,
    'ratelimit_whitelist': [],
    'blocking_mode': 'default',
    'edns_cs_enabled': False,
    'edns_cs_use_custom': False,
    'dnssec_enabled': True,
    'disable_ipv6': False,
    'upstream_mode': 'load_balance',
    'blocked_response_ttl': 10,
    'cache_size': 4194304,
    'cache_ttl_min': 0,
    'cache_ttl_max': 0,
    'cache_enabled': True,
    'cache_optimistic': False,
    'resolve_clients': True,
    'use_private_ptr_resolvers': True,
    'local_ptr_upstreams': [],
    'blocking_ipv4': '',
    'blocking_ipv6': '',
    'edns_cs_custom_ip': '',
}
DNS_FILTER = 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt'
ADAWAY = 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt'
LIST_ONE = 'https://lists.test/one.txt'
#: A list no instance can download.
LIST_TWO = 'https://lists.test/two.txt'
ALLOW = 'https://lists.test/allow.txt'
FRESH_FILTERS: list[dict[str, Any]] = [
    {'url': DNS_FILTER, 'name': 'AdGuard DNS filter', 'id': 1, 'rules_count': 179384, 'enabled': True},
    {'url': ADAWAY, 'name': 'AdAway Default Blocklist', 'id': 2, 'rules_count': 0, 'enabled': False},
]
FRESH_SAFE_SEARCH: dict[str, Any] = {
    'enabled': False,
    'bing': True,
    'duckduckgo': True,
    'ecosia': True,
    'google': True,
    'pixabay': True,
    'yandex': True,
    'youtube': True,
}
SCHEDULE: dict[str, Any] = {'time_zone': 'UTC'}

#: The fields `dns_config` restarts the DNS server for whenever they are present
#: (`dnsforward/http.go`, `setConfigRestartable`). `ratelimit` and
#: `upstream_timeout` restart it only when they change.
RESTARTS_IF_PRESENT = frozenset(
    {
        'upstream_dns',
        'local_ptr_upstreams',
        'upstream_dns_file',
        'bootstrap_dns',
        'fallback_dns',
        'edns_cs_enabled',
        'edns_cs_use_custom',
        'cache_enabled',
        'cache_size',
        'cache_ttl_min',
        'cache_ttl_max',
        'cache_optimistic',
        'resolve_clients',
        'use_private_ptr_resolvers',
        'ratelimit_subnet_len_ipv4',
        'ratelimit_subnet_len_ipv6',
        'ratelimit_whitelist',
    }
)

#: The fields each endpoint decodes as an integer, which Go's decoder refuses as
#: `20.0`.
INTEGERS: dict[str, frozenset[str]] = {
    'dns_config': frozenset(
        {
            'ratelimit',
            'ratelimit_subnet_len_ipv4',
            'ratelimit_subnet_len_ipv6',
            'upstream_timeout',
            'blocked_response_ttl',
            'cache_size',
            'cache_ttl_min',
            'cache_ttl_max',
        }
    ),
    'filtering/config': frozenset({'interval'}),
    'protection': frozenset({'duration'}),
    'clients/add': frozenset({'upstreams_cache_size'}),
}

#: The safe-search keys a client or the global setting carries.
SAFE_SEARCH_KEYS = tuple(FRESH_SAFE_SEARCH)

#: The client fields the instance rebuilds a client from (`clientJSON`), with
#: what it takes for a `null`.
CLIENT_FLAGS = ('ignore_querylog', 'ignore_statistics', 'upstreams_cache_enabled')

DAY = 24 * 60 * 60 * 1000


class Refusal(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status: int = status
        self.reason: str = reason


class FakeResponse:
    def __init__(self, url: str, payload: object, status: int = 200, text: str = '') -> None:
        self.url: str = url
        self.payload: object = payload
        self.status_code: int = status
        self.text: str = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code} Client Error for url: {self.url}')

    def json(self) -> object:
        if self.status_code >= 400:
            # A refusal's body is empty or plain text, never the payload.
            raise requests.JSONDecodeError('Expecting value', '', 0)
        return self.payload


def _is_http(url: Any) -> bool:
    return isinstance(url, str) and re.fullmatch(r'https?://[^/\s]+(/\S*)?', url) is not None


def _sorted_ids(ids: list[str]) -> list[str]:
    """The identifiers as `client.Persistent` reports them (`client/persistent.go`, `SetIDs`, `Identifiers`)."""
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    subnets: list[ipaddress.IPv4Interface | ipaddress.IPv6Interface] = []
    macs: list[bytes] = []
    client_ids: list[str] = []
    for each in ids:
        if not each:
            raise Refusal(400, 'clientid is empty')
        try:
            addresses.append(ipaddress.ip_address(each))
            continue
        except ValueError:
            pass
        if '/' in each:
            subnets.append(ipaddress.ip_interface(each))
        elif re.fullmatch(r'([0-9a-f]{2}[:-]){5}[0-9a-f]{2}', each, re.IGNORECASE):
            macs.append(bytes.fromhex(re.sub('[:-]', '', each)))
        elif re.fullmatch(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?', each, re.IGNORECASE):
            client_ids.append(each.lower())
        else:
            raise Refusal(400, f'invalid clientid {each!r}')
    return [
        *(str(address) for address in sorted(addresses, key=lambda a: (a.version, int(a)))),
        *(str(subnet) for subnet in sorted(subnets, key=lambda s: (-s.network.prefixlen, s.version, int(s.ip)))),
        *(':'.join(f'{octet:02x}' for octet in mac) for mac in sorted(macs)),
        *sorted(client_ids),
    ]


@dataclass
class Instance:
    """One AdGuard instance's configuration, and a log of what was asked of it."""

    #: The rewrite list's rows, in the order the instance holds them.
    entries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: Every POST, as `(last path segment, body)`, refused ones included.
    posts: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    #: Every request, as `(method, path under /control/, body)`, refused ones included.
    requests: list[tuple[str, str, Any]] = field(default_factory=list[tuple[str, str, Any]])
    #: Every session opened onto it, so a test can ask what it authenticated as.
    opened: list[FakeSession] = field(default_factory=list['FakeSession'])
    #: The paths under `/control/` the instance refuses. A refused request
    #: changes nothing the instance holds.
    refusing: set[str] = field(default_factory=set[str])

    user_rules: list[str] | None = None
    filtering_enabled: bool = True
    interval: int = 24
    filters: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(FRESH_FILTERS))
    whitelist_filters: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: The lists `add_url` can download, by URL, with how many rules each holds.
    fetchable: dict[str, int] = field(
        default_factory=lambda: {DNS_FILTER: 179384, ADAWAY: 6540, LIST_ONE: 10, ALLOW: 2}
    )
    safebrowsing: bool = False
    parental: bool = False
    safe_search: dict[str, Any] = field(default_factory=lambda: dict(FRESH_SAFE_SEARCH))
    blocked_services: dict[str, Any] = field(default_factory=lambda: {'ids': [], 'schedule': dict(SCHEDULE)})
    dns: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(FRESH_DNS))
    #: A pause the UI set: protection reads as off until it expires.
    paused: bool = False
    #: How many times a `dns_config` request restarted the DNS server.
    restarts: int = 0
    access: dict[str, Any] = field(
        default_factory=lambda: {
            'allowed_clients': None,
            'disallowed_clients': None,
            'blocked_hosts': ['version.bind', 'id.server', 'hostname.bind'],
        }
    )
    #: The persistent clients, each as the API reports it plus its stored `uid`.
    clients: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    auto_clients: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {'whois_info': {}, 'ip': '192.168.80.1', 'name': '', 'source': 'ARP'},
            {'whois_info': {'country': 'AU', 'orgname': 'Example'}, 'ip': '203.0.113.9', 'name': '', 'source': 'WHOIS'},
        ]
    )
    querylog: dict[str, Any] = field(
        default_factory=lambda: {
            'ignored': [],
            'interval': 90 * DAY,
            'enabled': True,
            'ignored_enabled': False,
            'anonymize_client_ip': False,
        }
    )
    stats: dict[str, Any] = field(
        default_factory=lambda: {'ignored': [], 'interval': DAY, 'enabled': True, 'ignored_enabled': False}
    )
    next_id: int = 1700000000
    next_uid: int = 1

    def session(self) -> FakeSession:
        """A `requests.Session` onto this instance, as the provider builds one."""
        served = FakeSession(self)
        self.opened.append(served)
        return served

    def writes(self) -> list[tuple[str, str, Any]]:
        """Every request but a GET."""
        return [request for request in self.requests if request[0] != 'GET']

    def answer(self, method: str, url: str, body: Any, *, as_json: bool) -> FakeResponse:
        path = url.split('/control/', 1)[1]
        self.requests.append((method, path, body))
        if method == 'POST':
            self.posts.append((path.rsplit('/', 1)[-1], body))
        if path in self.refusing:
            return FakeResponse(url, None, REFUSED, 'Unauthorized')
        if method != 'GET' and not as_json:
            return FakeResponse(url, None, 415, 'only content-type application/json is allowed')
        try:
            self._integers(path, body)
            payload = self._route(method, path, copy.deepcopy(body))
        except Refusal as refused:
            return FakeResponse(url, None, refused.status, refused.reason)
        return FakeResponse(url, copy.deepcopy(payload), 200, 'OK')

    def _integers(self, path: str, body: Any) -> None:
        if not isinstance(body, dict):
            return
        fields = cast('dict[str, Any]', body)
        if path == 'clients/update':
            fields = cast('dict[str, Any]', fields.get('data') or {})
        for key in INTEGERS.get('clients/add' if path == 'clients/update' else path, frozenset()):
            if isinstance(fields.get(key), float):
                raise Refusal(400, f'json: cannot unmarshal number {fields[key]} into {key}')

    def _route(self, method: str, path: str, body: Any) -> object:
        if method == 'GET':
            return self._get(path)
        if path == 'filtering/set_rules':
            self.user_rules = body.get('rules')
        elif path == 'filtering/config':
            if not 0 <= body.get('interval', 0) <= 365 * 24:
                raise Refusal(400, 'Unsupported interval')
            self.filtering_enabled, self.interval = bool(body.get('enabled')), body.get('interval', 0)
        elif path == 'filtering/add_url':
            self._add_url(body)
        elif path == 'filtering/remove_url':
            kind = self.whitelist_filters if body.get('whitelist') else self.filters
            kind[:] = [each for each in kind if each['url'] != body.get('url')]
        elif path == 'filtering/set_url':
            self._set_url(body)
        elif path in ('safebrowsing/enable', 'safebrowsing/disable'):
            self.safebrowsing = path.endswith('enable')
        elif path in ('parental/enable', 'parental/disable'):
            self.parental = path.endswith('enable')
        elif path == 'safesearch/settings':
            self.safe_search = {key: bool(body.get(key)) for key in SAFE_SEARCH_KEYS}
        elif path == 'blocked_services/update':
            self.blocked_services = {'ids': body.get('ids'), 'schedule': body.get('schedule') or dict(SCHEDULE)}
        elif path == 'dns_config':
            self._dns_config(body)
        elif path == 'protection':
            if body.get('enabled') and body.get('duration', 0) > 0:
                raise Refusal(400, 'Setting a duration is only allowed with protection disabling')
            self.dns['protection_enabled'], self.paused = bool(body.get('enabled')), body.get('duration', 0) > 0
        elif path == 'access/set':
            self._access_set(body)
        elif path == 'clients/add':
            self._clients_add(body)
        elif path == 'clients/update':
            self._clients_update(body)
        elif path == 'clients/delete':
            self._clients_delete(body)
        elif path in ('querylog/config/update', 'stats/config/update'):
            self._log_config(path.split('/', 1)[0], body)
        elif path == 'rewrite/add':
            self.entries.append(body)
        elif path == 'rewrite/delete':
            # AdGuard removes every entry equal to the pair, and answers a
            # pair it does not hold with success all the same.
            self.entries = [entry for entry in self.entries if entry != body]
        else:
            raise AssertionError(f'the instance serves no {method} {path}')
        return None

    def _get(self, path: str) -> object:
        def listed(each: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            return each or None

        match path:
            case 'filtering/status':
                return {
                    'filters': listed(self.filters),
                    'whitelist_filters': listed(self.whitelist_filters),
                    'user_rules': self.user_rules or None,
                    'interval': self.interval,
                    'enabled': self.filtering_enabled,
                }
            case 'safebrowsing/status':
                return {'enabled': self.safebrowsing}
            case 'parental/status':
                return {'enabled': self.parental}
            case 'safesearch/status':
                return self.safe_search
            case 'blocked_services/get':
                return self.blocked_services
            case 'dns_info':
                reported = dict(self.dns)
                if reported['upstream_mode'] == 'load_balance':
                    reported['upstream_mode'] = ''
                reported['protection_enabled'] = self.dns['protection_enabled'] and not self.paused
                reported['protection_disabled_until'] = '2026-10-08T08:08:41Z' if self.paused else None
                reported['default_local_ptr_upstreams'] = ['8.8.8.8:53']
                return reported
            case 'access/list':
                return self.access
            case 'clients':
                return {
                    'clients': listed(
                        [{key: value for key, value in each.items() if key != 'uid'} for each in self.clients]
                    ),
                    'auto_clients': self.auto_clients,
                    'supported_tags': ['device_gameconsole', 'device_pc', 'os_linux', 'user_child'],
                }
            case 'querylog/config':
                return self.querylog
            case 'stats/config':
                return self.stats
            case 'rewrite/list':
                return [{**entry, 'enabled': entry.get('enabled', True)} for entry in self.entries]
            case _:
                raise AssertionError(f'the instance serves no GET {path}')

    def _add_url(self, body: dict[str, Any]) -> None:
        url = body.get('url')
        if not _is_http(url):
            raise Refusal(400, f'checking filter: {url!r} is not an HTTP(S) URL')
        if any(each['url'] == url for each in (*self.filters, *self.whitelist_filters)):
            raise Refusal(400, f'Filter with URL {url!r}: url already exists')
        if not self.fetchable.get(str(url)):
            raise Refusal(400, f'Filter with URL {url!r} is invalid (maybe it points to blank page?)')
        kind = self.whitelist_filters if body.get('whitelist') else self.filters
        self.next_id += 1
        kind.append(
            {
                'url': url,
                'name': body.get('name'),
                'id': self.next_id,
                'rules_count': self.fetchable[str(url)],
                'enabled': True,
            }
        )

    def _set_url(self, body: dict[str, Any]) -> None:
        data = body.get('data')
        if data is None:
            raise Refusal(400, 'data is absent')
        if not _is_http(data.get('url')):
            raise Refusal(400, 'invalid url')
        kind = self.whitelist_filters if body.get('whitelist') else self.filters
        held = next((each for each in kind if each['url'] == body.get('url')), None)
        if held is None:
            raise Refusal(400, "url doesn't exist")
        moved = data['url'] != held['url']
        if moved and any(each['url'] == data['url'] for each in (*self.filters, *self.whitelist_filters)):
            raise Refusal(400, 'url already exists')
        switched_on = bool(data.get('enabled')) and not held['enabled']
        if data.get('enabled') and (moved or switched_on) and not self.fetchable.get(data['url']):
            raise Refusal(400, f'Filter with URL {data["url"]!r} is invalid')
        held.update(name=data.get('name'), url=data['url'], enabled=bool(data.get('enabled')))

    def _dns_config(self, body: dict[str, Any]) -> None:
        mode = body.get('blocking_mode')
        if mode is not None and mode not in ('default', 'refused', 'nxdomain', 'null_ip', 'custom_ip'):
            raise Refusal(400, f'bad blocking mode {mode!r}')
        if mode == 'custom_ip':
            for key, version in (('blocking_ipv4', 4), ('blocking_ipv6', 6)):
                try:
                    valid = ipaddress.ip_address(body.get(key) or '').version == version
                except ValueError:
                    valid = False
                if not valid:
                    raise Refusal(400, f'{key} must be valid ipv{version} on custom_ip blocking_mode')
        upstream_mode = body.get('upstream_mode')
        if upstream_mode is not None and upstream_mode not in ('', 'load_balance', 'parallel', 'fastest_addr'):
            raise Refusal(400, f'upstream_mode: incorrect value {upstream_mode!r}')
        if body.get('upstream_timeout', 1) < 1:
            raise Refusal(400, 'upstream_timeout: less than 1')
        restart = bool(RESTARTS_IF_PRESENT & set(body)) or any(
            key in body and body[key] != self.dns[key] for key in ('ratelimit', 'upstream_timeout')
        )
        for key, value in body.items():
            if key in ('blocking_ipv4', 'blocking_ipv6') and mode != 'custom_ip':
                continue
            if key == 'edns_cs_custom_ip' and not body.get('edns_cs_use_custom'):
                continue
            self.dns[key] = 'load_balance' if key == 'upstream_mode' and value == '' else value
        self.restarts += restart

    def _access_set(self, body: dict[str, Any]) -> None:
        lists = {key: body.get(key) for key in ('allowed_clients', 'disallowed_clients', 'blocked_hosts')}
        for key, held in lists.items():
            if held is not None and len(set(held)) != len(held):
                raise Refusal(400, f'validating {key}: duplicated values')
        if set(lists['allowed_clients'] or ()) & set(lists['disallowed_clients'] or ()):
            raise Refusal(400, 'items in allowed and disallowed clients intersect')
        self.access = lists

    def _client(self, body: dict[str, Any]) -> dict[str, Any]:
        """A client rebuilt from a request body alone, as `jsonToClient` does with no previous client."""
        if not body.get('name'):
            raise Refusal(400, 'empty name')
        ids = _sorted_ids(list(body.get('ids') or []))
        if not ids:
            raise Refusal(400, 'id required')
        safe_search = body.get('safe_search')
        if safe_search is None:
            safe_search = {key: bool(body.get('safesearch_enabled')) and key != 'enabled' for key in SAFE_SEARCH_KEYS}
            safe_search['enabled'] = bool(body.get('safesearch_enabled'))
        rebuilt: dict[str, Any] = {
            'safe_search': {key: bool(safe_search.get(key)) for key in SAFE_SEARCH_KEYS},
            'blocked_services_schedule': body.get('blocked_services_schedule') or dict(SCHEDULE),
            'name': body['name'],
            'blocked_services': body.get('blocked_services'),
            'ids': ids,
            'tags': sorted(body.get('tags') or []) or None,
            'upstreams': body.get('upstreams'),
            'filtering_enabled': bool(body.get('filtering_enabled')),
            'parental_enabled': bool(body.get('parental_enabled')),
            'safebrowsing_enabled': bool(body.get('safebrowsing_enabled')),
            'safesearch_enabled': bool(safe_search.get('enabled')),
            'use_global_blocked_services': bool(body.get('use_global_blocked_services')),
            'use_global_settings': bool(body.get('use_global_settings')),
            'upstreams_cache_size': body.get('upstreams_cache_size', 0)
            if body.get('upstreams_cache_enabled') is not None
            else 0,
        }
        for flag in CLIENT_FLAGS:
            # A `null` NullBool takes the previous client's value, and the
            # instance hands it none: it becomes false.
            rebuilt[flag] = body.get(flag) is True
        return rebuilt

    def _clashes(self, client: dict[str, Any], uid: int | None) -> None:
        for other in self.clients:
            if other['uid'] == uid:
                continue
            if other['name'] == client['name']:
                raise Refusal(400, f'another client uses the same name {client["name"]!r}')
            if shared := set(other['ids']) & set(client['ids']):
                raise Refusal(400, f'another client {other["name"]!r} uses the same id {sorted(shared)[0]!r}')

    def _clients_add(self, body: dict[str, Any]) -> None:
        client = self._client(body)
        self._clashes(client, None)
        self.next_uid += 1
        self.clients.append({**client, 'uid': self.next_uid})
        self.clients.sort(key=lambda each: each['name'])

    def _clients_update(self, body: dict[str, Any]) -> None:
        if not body.get('name'):
            raise Refusal(400, 'Invalid request')
        client = self._client(body.get('data') or {})
        stored = next((each for each in self.clients if each['name'] == body['name']), None)
        if stored is None:
            raise Refusal(400, f'client {body["name"]!r} is not found')
        self._clashes(client, stored['uid'])
        stored.clear()
        stored.update(client, uid=stored.get('uid') or 0)
        self.clients.sort(key=lambda each: each['name'])

    def _clients_delete(self, body: dict[str, Any]) -> None:
        if not body.get('name'):
            raise Refusal(400, "client's name must be non-empty")
        if not any(each['name'] == body['name'] for each in self.clients):
            raise Refusal(400, 'Client not found')
        self.clients = [each for each in self.clients if each['name'] != body['name']]

    def _log_config(self, which: str, body: dict[str, Any]) -> None:
        if body.get('enabled') is None:
            raise Refusal(422, 'enabled is null')
        if which == 'querylog' and body.get('anonymize_client_ip') is None:
            raise Refusal(422, 'anonymize_client_ip is null')
        if not 60 * 60 * 1000 <= body.get('interval', 0) <= 365 * DAY:
            raise Refusal(422, 'unsupported interval')
        keys = ('ignored', 'interval', 'enabled', 'ignored_enabled') + (
            ('anonymize_client_ip',) if which == 'querylog' else ()
        )
        held = {key: body.get(key) for key in keys}
        held['ignored'] = held['ignored'] or []
        if held['ignored_enabled'] is None:
            held['ignored_enabled'] = bool(held['ignored'])
        setattr(self, which, held)


class FakeSession:
    def __init__(self, instance: Instance) -> None:
        self.instance: Instance = instance
        self.auth: tuple[str, str] | None = None

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        return self.instance.answer('GET', url, None, as_json=True)

    def post(
        self,
        url: str,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int = 0,
    ) -> FakeResponse:
        sent_json = data is None and (headers or {}).get('Content-Type', 'application/json') == 'application/json'
        return self.instance.answer('POST', url, data if json is None else json, as_json=sent_json)

    def put(self, url: str, json: Any = None, timeout: int = 0) -> FakeResponse:
        return self.instance.answer('PUT', url, json, as_json=True)


@pytest.fixture(autouse=True)
def instance(monkeypatch: pytest.MonkeyPatch) -> Instance:
    """The instance the provider reaches, fresh unless a case changes it."""
    served = Instance()
    monkeypatch.setattr(requests, 'Session', served.session)
    return served


# --------------------------------------------------------------------------
# What each kind declares, and a hand edit that makes the instance differ.

SAFE_SEARCH: dict[str, Any] = dict(FRESH_SAFE_SEARCH)

# Every section a kind declares below differs from what a fresh instance
# holds, so a create that skipped any one write would leave it unconverged; and
# each hand edit moves the instance to a third value.
DNS: dict[str, Any] = {
    **FRESH_DNS,
    'upstream_dns': ['https://dns.quad9.net/dns-query', '[/home.arpa/]10.0.5.1', '[/10.in-addr.arpa/]10.0.5.1'],
    'bootstrap_dns': ['9.9.9.9', '1.1.1.1'],
    'ratelimit': 30,
    'cache_optimistic': True,
}
ACCESS: dict[str, Any] = {
    'allowed_clients': [],
    'disallowed_clients': ['192.0.2.66'],
    'blocked_hosts': ['version.bind'],
}
FILTERING: dict[str, Any] = {
    'filtering': {'enabled': True, 'interval': 72},
    'safebrowsing': {'enabled': True},
    'parental': {'enabled': True},
    'safe_search': {**FRESH_SAFE_SEARCH, 'enabled': True, 'google': False},
    'blocked_services': {'ids': ['tiktok'], 'schedule': {'time_zone': 'Europe/London'}},
}
LOGS: dict[str, Any] = {
    'querylog': {
        'enabled': True,
        'interval': 30 * DAY,
        'anonymize_client_ip': True,
        'ignored': ['quiet.example'],
        'ignored_enabled': True,
    },
    'stats': {'enabled': True, 'interval': 7 * DAY, 'ignored': [], 'ignored_enabled': False},
}
PS4: dict[str, Any] = {
    'name': 'PS4',
    'ids': ['aa:bb:cc:00:00:02', 'aa:bb:cc:00:00:01'],
    'tags': ['device_gameconsole'],
    'upstreams': [],
    'use_global_settings': True,
    'filtering_enabled': True,
    'parental_enabled': False,
    'safebrowsing_enabled': False,
    'safe_search': SAFE_SEARCH,
    'use_global_blocked_services': True,
    'blocked_services': [],
    'blocked_services_schedule': SCHEDULE,
    'ignore_querylog': True,
    'ignore_statistics': False,
    'upstreams_cache_enabled': False,
    'upstreams_cache_size': 0,
}


@dataclass(frozen=True)
class Kind:
    """One kind, as a case drives it."""

    provider: type[base.AdGuardProvider]
    #: A declaration of every section.
    declared: dict[str, Any]
    #: A hand edit, made on the instance after it holds `declared`.
    edit: Callable[[Instance], None]
    #: The GETs a read makes.
    reads: tuple[str, ...]

    def __str__(self) -> str:
        return self.provider.kind


def _edit_dns(instance: Instance) -> None:
    instance.dns.update(ratelimit=40)
    instance.access['blocked_hosts'] = ['id.server']


def _edit_filtering(instance: Instance) -> None:
    instance.interval = 168
    instance.safebrowsing = instance.parental = False
    instance.safe_search = {**FRESH_SAFE_SEARCH, 'enabled': True, 'youtube': False}
    instance.blocked_services = {'ids': ['youtube'], 'schedule': {'time_zone': 'UTC'}}


def _edit_lists(instance: Instance) -> None:
    instance.filters[0]['name'] = 'renamed by hand'
    instance.filters.append({'url': LIST_TWO, 'name': 'hand', 'id': 9, 'rules_count': 1, 'enabled': True})


def _edit_clients(instance: Instance) -> None:
    instance.clients[0]['filtering_enabled'] = False
    instance.clients.append({**instance._client({**PS4, 'name': 'hand', 'ids': ['192.168.1.77']}), 'uid': 99})  # pyright: ignore[reportPrivateUsage]


def _edit_logs(instance: Instance) -> None:
    instance.querylog['interval'] = DAY
    instance.stats['interval'] = 30 * DAY


KINDS = (
    Kind(
        user_rules.AdGuardUserRulesProvider,
        {
            'rules': [
                '! Declared by the dns stack: an edit here is overwritten.',
                '|tube^$dnsrewrite=NOERROR;A;192.168.71.1',
                '||psn.example^$client=PS4',
            ]
        },
        lambda instance: setattr(instance, 'user_rules', ['|tube^$dnsrewrite=NOERROR;A;192.168.71.9']),
        ('filtering/status', 'rewrite/list'),
    ),
    Kind(dns_server.AdGuardDnsServerProvider, {'dns': DNS, 'access': ACCESS}, _edit_dns, ('dns_info', 'access/list')),
    Kind(
        filtering.AdGuardFilteringProvider,
        FILTERING,
        _edit_filtering,
        ('filtering/status', 'safebrowsing/status', 'parental/status', 'safesearch/status', 'blocked_services/get'),
    ),
    Kind(
        filter_lists.AdGuardFilterListsProvider,
        {
            'filters': [
                {'url': DNS_FILTER, 'name': 'AdGuard DNS filter, declared', 'enabled': True},
                {'url': LIST_ONE, 'name': 'one', 'enabled': False},
            ],
            'whitelist_filters': [{'url': ALLOW, 'name': 'allow', 'enabled': True}],
        },
        _edit_lists,
        ('filtering/status',),
    ),
    Kind(clients.AdGuardClientsProvider, {'clients': [PS4]}, _edit_clients, ('clients',)),
    Kind(log_settings.AdGuardLogSettingsProvider, LOGS, _edit_logs, ('querylog/config', 'stats/config')),
)

EVERY_KIND = pytest.mark.parametrize('kind', KINDS, ids=str)


def configured_provider(cls: type[base.AdGuardProvider], password: str = PASSWORD) -> base.AdGuardProvider:
    """A provider as an operation receives one: revived, then handed the config.

    The login arrives already decrypted, which is what the plugin does with a
    secret configuration value before calling `configure`.
    """
    revived = cls()
    revived.configure(
        dynamic.ConfigureRequest(
            config=dynamic.Config(
                {f'{PROJECT}:{base.USERNAME_CONFIG}': USERNAME, f'{PROJECT}:{base.PASSWORD_CONFIG}': password},
                PROJECT,
            )
        )
    )
    return revived


def as_engine(value: Any) -> Any:
    """`value` as the engine hands a property bag to a dynamic provider: every number a float."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, dict):
        return {key: as_engine(each) for key, each in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [as_engine(each) for each in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def checked(
    cls: type[base.AdGuardProvider], sections: dict[str, Any], *, password: str = PASSWORD, endpoint: str = ENDPOINT
) -> dict[str, Any]:
    """The inputs as the engine stores and compares them: what `check` returned, every number a float."""
    result = configured_provider(cls, password).check(
        {}, as_engine({'instance': INSTANCE, 'endpoint': endpoint, **sections})
    )
    assert not result.failures, [(failure.property, failure.reason) for failure in result.failures or []]
    return result.inputs


def created(kind: Kind) -> dict[str, Any]:
    """The stored outputs of `kind` created on the instance."""
    news = checked(kind.provider, kind.declared)
    return dict(configured_provider(kind.provider).create(news).outs or {})


def refreshed(cls: type[base.AdGuardProvider], stored: dict[str, Any]) -> dict[str, Any]:
    """What a refresh leaves as the stored outputs."""
    return dict(configured_provider(cls).read(f'{INSTANCE}|{cls.kind}', stored).outs or {})


def drifted(cls: type[base.AdGuardProvider], stored: dict[str, Any], sections: dict[str, Any]) -> bool | None:
    """Whether a refreshing preview plans a change: refresh, then diff against the declaration."""
    return configured_provider(cls).diff('an-id', refreshed(cls, stored), checked(cls, sections)).changes


# --------------------------------------------------------------------------
# What every kind does.


@EVERY_KIND
def test_a_created_kind_converges_a_fresh_instance(kind: Kind) -> None:
    """Created on what a fresh v0.107.79 holds, the instance then reads back as declared."""
    stored = created(kind)

    assert drifted(kind.provider, stored, kind.declared) is False


@EVERY_KIND
def test_a_plain_diff_with_no_declared_change_calls_neither_instance(kind: Kind, instance: Instance) -> None:
    """An unreachable UDM costs a `dns` preview nothing: `diff` compares state with the program."""
    news = checked(kind.provider, kind.declared)

    result = configured_provider(kind.provider).diff('an-id', news, news)

    assert result.changes is False
    assert instance.opened == []


@EVERY_KIND
def test_read_returns_what_the_instance_holds_as_the_inputs(kind: Kind, instance: Instance) -> None:
    """A refresh renders a hand edit as a property diff against the program.

    The inputs are what the next diff compares the program against, so a read
    that left them as the program last wrote them would hide the edit.
    """
    stored = created(kind)
    kind.edit(instance)

    result = configured_provider(kind.provider).read(f'{INSTANCE}|{kind.provider.kind}', stored)

    assert result.inputs is not None
    assert result.inputs == result.outs
    assert (
        configured_provider(kind.provider).diff('an-id', result.inputs, checked(kind.provider, kind.declared)).changes
    )


@EVERY_KIND
def test_a_moved_stamp_updates_with_no_write(kind: Kind, instance: Instance) -> None:
    """A rotation or a re-addressed instance writes neither instance.

    The instance holds what the resource declares, so there is nothing to
    write; the update records the door the instance is now written through.
    """
    olds = checked(kind.provider, kind.declared)
    for news in (
        checked(kind.provider, kind.declared, password='rotated'),
        checked(kind.provider, kind.declared, endpoint=MOVED),
    ):
        operations = configured_provider(kind.provider)

        assert operations.diff('an-id', olds, news).changes is True
        assert operations.diff('an-id', olds, news).replaces == []
        assert operations.update('an-id', olds, news).outs == news
    assert instance.opened == []


@EVERY_KIND
def test_delete_calls_nothing(kind: Kind, instance: Instance) -> None:
    """A replacement deletes after it creates, so a delete that emptied anything would undo the create."""
    configured_provider(kind.provider).delete('an-id', checked(kind.provider, kind.declared))

    assert instance.opened == []


@pytest.mark.parametrize(('kind', 'path'), [(kind, path) for kind in KINDS for path in kind.reads], ids=str)
def test_a_refused_read_raises(kind: Kind, path: str, instance: Instance) -> None:
    """A refusal says nothing about what the instance holds, so it is never read as an empty instance."""
    stored = checked(kind.provider, kind.declared)
    instance.refusing = {path}

    with pytest.raises(requests.HTTPError, match=f'{path} answered {REFUSED}'):
        _ = configured_provider(kind.provider).read('an-id', stored)


@EVERY_KIND
def test_the_id_is_the_instance_and_the_kind(kind: Kind) -> None:
    result = configured_provider(kind.provider).create(checked(kind.provider, kind.declared))

    assert result.id == f'{INSTANCE}|{kind.provider.kind}'
    assert ENDPOINT not in str(result.id)


@EVERY_KIND
def test_a_changed_instance_replaces_and_calls_nothing(kind: Kind, instance: Instance) -> None:
    olds = checked(kind.provider, kind.declared)
    news = checked(kind.provider, kind.declared) | {'instance': 'adguard-bob'}

    result = configured_provider(kind.provider).diff('an-id', olds, news)

    assert result.replaces == ['instance']
    assert result.delete_before_replace is False
    assert instance.opened == []


@EVERY_KIND
def test_an_input_still_unknown_is_an_unknown_diff(kind: Kind, instance: Instance) -> None:
    section = kind.provider.sections[0]
    news = checked(kind.provider, kind.declared) | {section: rpc.UNKNOWN}

    result = configured_provider(kind.provider).diff('an-id', checked(kind.provider, kind.declared), news)

    assert result.changes is None
    assert not result.replaces
    assert instance.opened == []


@EVERY_KIND
def test_a_write_authenticates_as_the_configured_login(kind: Kind, instance: Instance) -> None:
    _ = created(kind)

    assert {opened.auth for opened in instance.opened} == {(USERNAME, PASSWORD)}


@EVERY_KIND
def test_what_lands_in_state_is_a_provider_with_nothing_in_it(kind: Kind) -> None:
    """Every resource stores a pickle of its provider; this one is a name, identical across a rotation."""
    one = serialized(configured_provider(kind.provider))
    rotated = serialized(configured_provider(kind.provider, 'rotated'))

    assert configured_provider(kind.provider).__getstate__() == {}
    assert one == rotated
    assert PASSWORD not in one
    assert len(one) < 256


def test_the_session_stamp_names_the_door_and_fingerprints_the_login() -> None:
    session = checked(user_rules.AdGuardUserRulesProvider, KINDS[0].declared)[configured.SESSION]
    endpoint, _, fingerprint = session.partition('#')

    assert endpoint == ENDPOINT
    assert fingerprint == hashlib.sha256(f'{USERNAME}:{PASSWORD}'.encode()).hexdigest()[: configured.FINGERPRINT_LENGTH]
    assert PASSWORD not in session


def test_a_change_to_the_package_is_a_change_a_reader_can_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider is pickled by reference, so editing an operation moves nothing but the version."""
    shipped = base.VERSION
    olds = checked(user_rules.AdGuardUserRulesProvider, KINDS[0].declared)
    monkeypatch.setattr(base, 'VERSION', f'{shipped}-next')
    news = checked(user_rules.AdGuardUserRulesProvider, KINDS[0].declared)

    assert olds[configured.PROVIDER_VERSION] == shipped
    assert news[configured.PROVIDER_VERSION] == f'{shipped}-next'
    assert configured_provider(user_rules.AdGuardUserRulesProvider).diff('an-id', olds, news).changes is True


def test_a_provider_that_was_never_configured_has_no_login_to_dial_with() -> None:
    with pytest.raises(AttributeError):
        _ = user_rules.AdGuardUserRulesProvider().password


def test_a_missing_half_of_the_login_refuses_by_name() -> None:
    half = dynamic.Config({f'{PROJECT}:{base.USERNAME_CONFIG}': USERNAME}, PROJECT)

    with pytest.raises(ValueError, match=base.PASSWORD_CONFIG):
        user_rules.AdGuardUserRulesProvider().configure(dynamic.ConfigureRequest(config=half))


# --------------------------------------------------------------------------
# What `check` refuses offline, so a preview fails where the update would.


def _failures(cls: type[base.AdGuardProvider], sections: dict[str, Any]) -> list[str]:
    result = configured_provider(cls).check({}, {'instance': INSTANCE, 'endpoint': ENDPOINT, **sections})
    return [failure.reason for failure in result.failures or []]


@pytest.mark.parametrize(
    ('cls', 'sections', 'reason'),
    [
        (
            dns_server.AdGuardDnsServerProvider,
            {**KINDS[1].declared, 'dns': {**DNS, 'blocking_mode': 'drop'}},
            "blocking_mode 'drop' is none of",
        ),
        (
            dns_server.AdGuardDnsServerProvider,
            {**KINDS[1].declared, 'dns': {**DNS, 'blocking_mode': 'custom_ip'}},
            'custom_ip takes an IPv4',
        ),
        (
            dns_server.AdGuardDnsServerProvider,
            {**KINDS[1].declared, 'dns': {**DNS, 'upstream_mode': ''}},
            "upstream_mode '' is none of",
        ),
        (
            dns_server.AdGuardDnsServerProvider,
            {**KINDS[1].declared, 'dns': {key: value for key, value in DNS.items() if key != 'cache_size'}},
            'dns lacks cache_size',
        ),
        (
            clients.AdGuardClientsProvider,
            {'clients': [{**PS4, 'ids': ['not an identifier']}]},
            "'not an identifier' is no address, subnet, MAC address or ClientID",
        ),
        (
            clients.AdGuardClientsProvider,
            {'clients': [PS4, {**PS4, 'name': 'other', 'ids': ['AA:BB:CC:00:00:01']}]},
            'aa:bb:cc:00:00:01 is both PS4 and other',
        ),
        (
            filter_lists.AdGuardFilterListsProvider,
            {'filters': [{'url': '/opt/lists/local.txt', 'name': 'local', 'enabled': True}], 'whitelist_filters': []},
            "'/opt/lists/local.txt' is not an HTTP(S) URL",
        ),
        (
            filter_lists.AdGuardFilterListsProvider,
            {
                'filters': [{'url': LIST_ONE, 'name': 'one', 'enabled': True}],
                'whitelist_filters': [{'url': LIST_ONE, 'name': 'one', 'enabled': True}],
            },
            f'{LIST_ONE} is declared twice',
        ),
        (user_rules.AdGuardUserRulesProvider, {'rules': ['||a.example^\n||b.example^']}, 'rule 0 holds a line break'),
        (
            log_settings.AdGuardLogSettingsProvider,
            {
                **KINDS[5].declared,
                'stats': {'enabled': True, 'interval': 60_000, 'ignored': [], 'ignored_enabled': False},
            },
            'interval is outside an hour to a year',
        ),
        (
            filtering.AdGuardFilteringProvider,
            {**KINDS[2].declared, 'filtering': {'enabled': True, 'interval': 9000}},
            'interval is outside 0 to 8760 hours',
        ),
    ],
    ids=[
        'blocking-mode',
        'custom-ip-without-addresses',
        'empty-upstream-mode',
        'missing-key',
        'client-identifier',
        'shared-identifier',
        'list-path',
        'list-twice',
        'rule-line-break',
        'log-interval',
        'filter-interval',
    ],
)
def test_check_refuses_what_the_instance_would(
    cls: type[base.AdGuardProvider], sections: dict[str, Any], reason: str
) -> None:
    assert any(reason in failure for failure in _failures(cls, sections)), _failures(cls, sections)


@EVERY_KIND
def test_check_accepts_each_declaration(kind: Kind) -> None:
    assert _failures(kind.provider, kind.declared) == []


# --------------------------------------------------------------------------
# AdGuardUserRules.

RULES = user_rules.AdGuardUserRulesProvider
FILTERS = filtering.AdGuardFilteringProvider
LOG = log_settings.AdGuardLogSettingsProvider


def test_create_replaces_the_hand_kept_list_with_the_declared_one(instance: Instance) -> None:
    """The list is written whole: a hand-kept rule the declaration lacks is gone after the create."""
    instance.user_rules = ['||hand.example^', '|tube^$dnsrewrite=NOERROR;A;192.168.71.1']

    _ = created(KINDS[0])

    assert instance.user_rules == KINDS[0].declared['rules']


def test_the_rules_go_as_json(instance: Instance) -> None:
    """The instance answers any other body with 415, so a text body is a write that never lands."""
    _ = created(KINDS[0])

    assert ('POST', 'filtering/set_rules', {'rules': KINDS[0].declared['rules']}) in instance.requests


@pytest.mark.parametrize(
    'live',
    [
        [
            '|tube^$dnsrewrite=NOERROR;A;192.168.71.1',
            '! Declared by the dns stack: an edit here is overwritten.',
            '||psn.example^$client=PS4',
        ],
        [
            '! Declared by the dns stack: an edit here is overwritten.',
            '|tube^$dnsrewrite=NOERROR;A;192.168.71.9',
            '||psn.example^$client=PS4',
        ],
        [
            '! Declared by the dns stack: an edit here is overwritten.',
            '! a note typed in the UI',
            '|tube^$dnsrewrite=NOERROR;A;192.168.71.1',
            '||psn.example^$client=PS4',
        ],
        ['|tube^$dnsrewrite=NOERROR;A;192.168.71.1', '||psn.example^$client=PS4'],
    ],
    ids=['reordered', 'edited', 'commented', 'comment-removed'],
)
def test_a_reordered_edited_or_commented_live_list_is_drift(live: list[str], instance: Instance) -> None:
    stored = created(KINDS[0])
    instance.user_rules = live

    assert drifted(RULES, stored, KINDS[0].declared) is True


def test_a_row_in_the_rewrite_list_refuses_the_write_naming_the_row(instance: Instance) -> None:
    """The rewrite list answers before any rule, so rules written beside a row would not decide its name."""
    instance.entries = [{'domain': 'tube.ucw.phd', 'answer': '192.168.71.9'}]
    instance.user_rules = ['||hand.example^']

    with pytest.raises(user_rules.RewriteListNotEmpty, match=r'tube\.ucw\.phd -> 192\.168\.71\.9'):
        _ = created(KINDS[0])

    assert instance.user_rules == ['||hand.example^']
    assert not any(path == 'filtering/set_rules' for _method, path, _body in instance.requests)


def test_a_hand_added_rewrite_row_is_drift(instance: Instance) -> None:
    stored = created(KINDS[0])
    instance.entries = [{'domain': 'tube.ucw.phd', 'answer': '192.168.71.9'}]

    assert refreshed(RULES, stored)[user_rules.REWRITES] == [
        {'domain': 'tube.ucw.phd', 'answer': '192.168.71.9', 'enabled': True}
    ]
    assert drifted(RULES, stored, KINDS[0].declared) is True


# --------------------------------------------------------------------------
# AdGuardDnsServer.

SERVER = dns_server.AdGuardDnsServerProvider


def test_numbers_go_out_as_the_integers_the_instance_decodes(instance: Instance) -> None:
    """The engine hands every number over as a float, and the instance refuses `20.0` for an integer.

    `checked` hands the provider floats, as the engine does; a write that sent
    them as they came would be refused wherever a kind has an integer field.
    """
    news = checked(SERVER, KINDS[1].declared)
    assert isinstance(news['dns']['ratelimit'], float)

    _ = configured_provider(SERVER).create(news)

    (sent,) = [body for _method, path, body in instance.requests if path == 'dns_config']
    assert sent['ratelimit'] == 30
    assert isinstance(sent['ratelimit'], int)


def test_upstream_mode_read_back_empty_is_no_drift(instance: Instance) -> None:
    """The instance reports load balancing as `""`, and the declaration names it `load_balance`."""
    stored = created(KINDS[1])

    assert instance.dns['upstream_mode'] == 'load_balance'
    assert drifted(SERVER, stored, KINDS[1].declared) is False


def test_a_pause_is_ended_through_the_protection_endpoint(instance: Instance) -> None:
    """`dns_config`'s `protection_enabled` leaves a UI pause standing; only `/control/protection` ends it."""
    stored = created(KINDS[1])
    instance.dns['protection_enabled'], instance.paused = False, True
    held = refreshed(SERVER, stored)

    _ = configured_provider(SERVER).update('an-id', held, checked(SERVER, KINDS[1].declared))

    assert not instance.paused
    assert drifted(SERVER, held, KINDS[1].declared) is False


def test_one_dns_config_request_per_update_carrying_every_declared_field(instance: Instance) -> None:
    """One restart, and no half-applied set: the instance validates a request whole before applying any of it."""
    olds = checked(SERVER, KINDS[1].declared)
    changed = {**DNS, 'cache_optimistic': False, 'upstream_dns': ['https://dns.quad9.net/dns-query']}

    _ = configured_provider(SERVER).update('an-id', olds, checked(SERVER, {**KINDS[1].declared, 'dns': changed}))

    sent = [body for _method, path, body in instance.requests if path == 'dns_config']
    assert len(sent) == 1
    assert set(sent[0]) == set(DNS) - {'protection_enabled'}
    assert instance.restarts == 1
    assert instance.dns['cache_optimistic'] is False


def test_a_protection_change_alone_restarts_nothing(instance: Instance) -> None:
    olds = checked(SERVER, KINDS[1].declared)

    _ = configured_provider(SERVER).update(
        'an-id', olds, checked(SERVER, {**KINDS[1].declared, 'dns': {**DNS, 'protection_enabled': False}})
    )

    assert [path for _method, path, _body in instance.writes()] == ['protection']
    assert instance.restarts == 0


def test_the_blocking_addresses_count_only_under_the_custom_ip_mode(instance: Instance) -> None:
    """The instance takes the addresses only under `custom_ip`, so elsewhere they are not compared."""
    stored = created(KINDS[1])
    instance.dns.update(blocking_ipv4='192.0.2.1', blocking_ipv6='2001:db8::1')

    assert drifted(SERVER, stored, KINDS[1].declared) is False


# --------------------------------------------------------------------------
# AdGuardFilterLists.

LISTS = filter_lists.AdGuardFilterListsProvider


def _lists(filters: list[dict[str, Any]], allow: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {'filters': filters, 'whitelist_filters': allow or []}


def test_a_list_declared_disabled_is_added_then_disabled(instance: Instance) -> None:
    """`add_url` stores every list enabled, and carries no switch to say otherwise."""
    declared = _lists([{'url': LIST_ONE, 'name': 'one', 'enabled': False}])

    _ = configured_provider(LISTS).create(checked(LISTS, declared))

    assert [each['enabled'] for each in instance.filters if each['url'] == LIST_ONE] == [False]
    assert [path for _method, path, _body in instance.writes()][:2] == ['filtering/add_url', 'filtering/set_url']


def test_reconciliation_reads_the_instance(instance: Instance) -> None:
    """A present list is re-set, not added again, and an undeclared one is removed.

    The stored outputs know of neither list: what decides is what the instance
    holds now, and a second add of a present URL is refused.
    """
    instance.filters = [
        {'url': LIST_ONE, 'name': 'renamed by hand', 'id': 7, 'rules_count': 10, 'enabled': True},
        {'url': LIST_TWO, 'name': 'added by hand', 'id': 8, 'rules_count': 3, 'enabled': True},
    ]
    olds = checked(LISTS, _lists([]))
    news = checked(LISTS, _lists([{'url': LIST_ONE, 'name': 'one', 'enabled': True}]))

    _ = configured_provider(LISTS).update('an-id', olds, news)

    assert [(each['url'], each['name']) for each in instance.filters] == [(LIST_ONE, 'one')]
    assert [path for _method, path, _body in instance.writes()] == ['filtering/set_url', 'filtering/remove_url']


def test_adds_come_before_removes(instance: Instance) -> None:
    """Replacing one list with another leaves no moment with neither."""
    olds = created(
        Kind(
            LISTS,
            _lists([{'url': DNS_FILTER, 'name': 'AdGuard DNS filter', 'enabled': True}]),
            lambda _instance: None,
            (),
        )
    )
    instance.requests.clear()

    _ = configured_provider(LISTS).update(
        'an-id', olds, checked(LISTS, _lists([{'url': LIST_ONE, 'name': 'one', 'enabled': True}]))
    )

    assert [(path, body['url']) for _method, path, body in instance.writes()] == [
        ('filtering/add_url', LIST_ONE),
        ('filtering/remove_url', DNS_FILTER),
    ]


def test_a_list_that_cannot_be_fetched_fails_the_write(instance: Instance) -> None:
    with pytest.raises(requests.HTTPError, match='add_url answered 400'):
        _ = configured_provider(LISTS).create(
            checked(LISTS, _lists([{'url': LIST_TWO, 'name': 'two', 'enabled': True}]))
        )


def test_list_order_is_not_drift(instance: Instance) -> None:
    declared = _lists(
        [
            {'url': ADAWAY, 'name': 'AdAway Default Blocklist', 'enabled': False},
            {'url': DNS_FILTER, 'name': 'AdGuard DNS filter', 'enabled': True},
        ]
    )

    assert drifted(LISTS, checked(LISTS, declared), declared) is False
    assert instance.writes() == []


# --------------------------------------------------------------------------
# AdGuardClients.

CLIENTS = clients.AdGuardClientsProvider


def test_a_client_update_carries_every_field(instance: Instance) -> None:
    """The instance rebuilds a client from the body alone, so a field left out turns its flag false."""
    stored = created(KINDS[4])
    changed = {**PS4, 'filtering_enabled': False}

    _ = configured_provider(CLIENTS).update('an-id', stored, checked(CLIENTS, {'clients': [changed]}))

    (sent,) = [body for _method, path, body in instance.requests if path == 'clients/update']
    assert set(sent['data']) == set(PS4)
    assert instance.clients[0]['ignore_querylog'] is True
    assert instance.clients[0]['filtering_enabled'] is False


def test_the_stored_uid_whois_data_and_runtime_clients_are_not_drift_and_ids_compare_in_the_instances_order(
    instance: Instance,
) -> None:
    declared = {
        'clients': [
            {**PS4, 'ids': ['AA:BB:CC:00:00:02', '192.168.1.20', 'aa:bb:cc:00:00:01', '10.0.0.0/8', '192.168.1.10']}
        ]
    }
    stored = created(Kind(CLIENTS, declared, lambda _instance: None, ()))
    uid = instance.clients[0]['uid']

    assert instance.clients[0]['ids'] == [
        '192.168.1.10',
        '192.168.1.20',
        '10.0.0.0/8',
        'aa:bb:cc:00:00:01',
        'aa:bb:cc:00:00:02',
    ]
    assert drifted(CLIENTS, stored, declared) is False
    assert instance.clients[0]['uid'] == uid


def test_an_undeclared_client_is_deleted_before_a_declared_one_taking_its_id_is_added(instance: Instance) -> None:
    olds = created(KINDS[4])
    renamed = {**PS4, 'name': 'PlayStation'}

    _ = configured_provider(CLIENTS).update('an-id', olds, checked(CLIENTS, {'clients': [renamed]}))

    assert [each['name'] for each in instance.clients] == ['PlayStation']
    assert [path for _method, path, _body in instance.writes()][-2:] == ['clients/delete', 'clients/add']


# --------------------------------------------------------------------------
# Updates: what each changed section writes.


def updated(
    cls: type[base.AdGuardProvider], declared: dict[str, Any], changes: dict[str, Any], instance: Instance
) -> list[str]:
    """Create `declared`, then update to `declared` with `changes`; the paths the update wrote."""
    olds = created(Kind(cls, declared, lambda _instance: None, ()))
    instance.requests.clear()
    news = checked(cls, {**declared, **changes})

    _ = configured_provider(cls).update('an-id', olds, news)

    return [path for _method, path, _body in instance.writes()]


def test_a_changed_rule_list_is_written(instance: Instance) -> None:
    """Every route a stack adds or removes is an update of the rule list, so an update that wrote nothing would succeed with the instance unchanged."""
    rules = [*KINDS[0].declared['rules'], '|photos^$dnsrewrite=NOERROR;A;192.168.71.1']

    assert updated(RULES, KINDS[0].declared, {'rules': rules}, instance) == ['filtering/set_rules']
    assert instance.user_rules == rules


@pytest.mark.parametrize(
    ('cls', 'declared', 'section', 'value', 'writes'),
    [
        (SERVER, KINDS[1].declared, 'dns', {**DNS, 'cache_ttl_min': 60}, ['dns_config']),
        (SERVER, KINDS[1].declared, 'access', {**ACCESS, 'blocked_hosts': ['hostname.bind']}, ['access/set']),
        (FILTERS, FILTERING, 'filtering', {'enabled': True, 'interval': 12}, ['filtering/config']),
        (FILTERS, FILTERING, 'safebrowsing', {'enabled': False}, ['safebrowsing/disable']),
        (FILTERS, FILTERING, 'parental', {'enabled': False}, ['parental/disable']),
        (FILTERS, FILTERING, 'safe_search', {**FILTERING['safe_search'], 'bing': False}, ['safesearch/settings']),
        (
            FILTERS,
            FILTERING,
            'blocked_services',
            {'ids': ['tiktok', 'youtube'], 'schedule': {'time_zone': 'Europe/London'}},
            ['blocked_services/update'],
        ),
        (LOG, LOGS, 'querylog', {**LOGS['querylog'], 'interval': 7 * DAY}, ['querylog/config/update']),
        (LOG, LOGS, 'stats', {**LOGS['stats'], 'interval': DAY}, ['stats/config/update']),
    ],
    ids=[
        'dns',
        'access',
        'filtering',
        'safebrowsing',
        'parental',
        'safe-search',
        'blocked-services',
        'querylog',
        'stats',
    ],
)
def test_a_change_confined_to_one_section_writes_that_sections_endpoint_and_no_other(
    cls: type[base.AdGuardProvider],
    declared: dict[str, Any],
    section: str,
    value: dict[str, Any],
    writes: list[str],
    instance: Instance,
) -> None:
    changes = {section: value}

    assert updated(cls, declared, changes, instance) == writes
    assert drifted(cls, checked(cls, declared), {**declared, **changes}) is False


def test_settings_and_protection_changed_together_send_both_requests(instance: Instance) -> None:
    changed = {**DNS, 'cache_ttl_min': 60, 'protection_enabled': False}

    assert updated(SERVER, KINDS[1].declared, {'dns': changed}, instance) == ['dns_config', 'protection']
    assert drifted(SERVER, checked(SERVER, KINDS[1].declared), {**KINDS[1].declared, 'dns': changed}) is False


@pytest.mark.parametrize(
    ('declared', 'edit'),
    [
        (
            {**DNS, 'blocking_mode': 'custom_ip', 'blocking_ipv4': '192.0.2.1', 'blocking_ipv6': '2001:db8::1'},
            {'blocking_ipv4': '192.0.2.9'},
        ),
        (
            {**DNS, 'edns_cs_enabled': True, 'edns_cs_use_custom': True, 'edns_cs_custom_ip': '198.51.100.1'},
            {'edns_cs_custom_ip': '198.51.100.9'},
        ),
    ],
    ids=['custom-ip-blocking', 'edns-custom-address'],
)
def test_a_changed_address_is_drift_while_the_instance_uses_it(
    declared: dict[str, Any], edit: dict[str, Any], instance: Instance
) -> None:
    sections = {**KINDS[1].declared, 'dns': declared}
    stored = created(Kind(SERVER, sections, lambda _instance: None, ()))
    instance.dns.update(edit)
    held = refreshed(SERVER, stored)

    assert drifted(SERVER, stored, sections) is True

    _ = configured_provider(SERVER).update('an-id', held, checked(SERVER, sections))

    assert {key: instance.dns[key] for key in edit} == {key: declared[key] for key in edit}


def test_a_switch_only_change_to_a_present_list_goes_through_set_url(instance: Instance) -> None:
    declared = _lists([{'url': LIST_ONE, 'name': 'one', 'enabled': True}])

    writes = updated(LISTS, declared, _lists([{'url': LIST_ONE, 'name': 'one', 'enabled': False}]), instance)

    assert writes == ['filtering/set_url']
    assert [each['enabled'] for each in instance.filters if each['url'] == LIST_ONE] == [False]


def test_a_list_that_moves_kind_ends_up_in_the_other_kind(instance: Instance) -> None:
    declared = _lists([{'url': LIST_ONE, 'name': 'one', 'enabled': True}])

    _ = updated(LISTS, declared, _lists([], [{'url': LIST_ONE, 'name': 'one', 'enabled': True}]), instance)

    assert [each['url'] for each in instance.whitelist_filters] == [LIST_ONE]
    assert LIST_ONE not in [each['url'] for each in instance.filters]


def test_a_changed_client_identifier_is_drift_and_is_written(instance: Instance) -> None:
    stored = created(KINDS[4])
    instance.clients[0]['ids'] = ['aa:bb:cc:00:00:09']

    assert drifted(CLIENTS, stored, KINDS[4].declared) is True

    moved = {**PS4, 'ids': ['aa:bb:cc:00:00:03']}
    writes = updated(CLIENTS, KINDS[4].declared, {'clients': [moved]}, instance)

    assert writes == ['clients/update']
    assert instance.clients[0]['ids'] == ['aa:bb:cc:00:00:03']


def test_an_identifier_moving_to_a_client_declared_first_converges(instance: Instance) -> None:
    """The client giving the identifier up is written first, or the instance refuses the other client's update."""
    giver = {**PS4, 'name': 'giver', 'ids': ['192.168.1.10', '192.168.1.11']}
    taker = {**PS4, 'name': 'taker', 'ids': ['192.168.1.12']}
    moved = {'clients': [{**taker, 'ids': ['192.168.1.11', '192.168.1.12']}, {**giver, 'ids': ['192.168.1.10']}]}

    writes = updated(CLIENTS, {'clients': [giver, taker]}, moved, instance)

    assert writes == ['clients/update', 'clients/update']
    assert {each['name']: each['ids'] for each in instance.clients} == {
        'giver': ['192.168.1.10'],
        'taker': ['192.168.1.11', '192.168.1.12'],
    }


# --------------------------------------------------------------------------
# AdGuardRewrite: the rewrite-list provider, which the `dns` stack still
# declares until its component moves onto the kinds above.


PROPS: dict[str, Any] = {
    'instance': INSTANCE,
    'endpoint': ENDPOINT,
    'domain': 'photos.ucw.phd',
    'answer': '192.168.71.1',
}


def provider(password: str = PASSWORD) -> adguard_rewrites.AdGuardRewriteProvider:
    """A rewrite provider as an operation receives one: revived, then handed the config."""
    revived = adguard_rewrites.AdGuardRewriteProvider()
    revived.configure(
        dynamic.ConfigureRequest(
            config=dynamic.Config(
                {
                    f'{PROJECT}:{adguard_rewrites.USERNAME_CONFIG}': USERNAME,
                    f'{PROJECT}:{adguard_rewrites.PASSWORD_CONFIG}': password,
                },
                PROJECT,
            )
        )
    )
    return revived


def rewrite_checked(props: dict[str, Any], password: str = PASSWORD) -> dict[str, Any]:
    """The inputs as the engine stores and compares them: what `check` returned."""
    return provider(password).check({}, props).inputs


def test_create_adds_the_pair_and_ids_it_by_instance(instance: Instance) -> None:
    """The id names the instance rather than the address it was written at.

    The same rewrite on alice and on bob are two resources, because they are
    two writes; and re-addressing alice leaves this id untouched, which is what
    makes the move an update instead of a delete and a create.
    """
    result = provider().create(dict(PROPS))

    assert instance.entries == [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]
    assert result.id == f'{INSTANCE}|photos.ucw.phd|192.168.71.1'
    assert ENDPOINT not in str(result.id)


def test_create_adopts_an_identical_entry_rather_than_duplicating_it(instance: Instance) -> None:
    """AdGuard stores duplicates, and duplicates cannot be deleted apart.

    Which is what a retried `up` after a partial failure would produce.
    """
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    _ = provider().create(dict(PROPS))

    assert instance.posts == []


def test_a_rewrite_authenticates_as_the_configured_login(instance: Instance) -> None:
    """The instance is declared; the login that answers it is not.

    It comes from `configure`, so no property bag carries it and no caller
    could have passed a different one.
    """
    _ = provider().create(dict(PROPS))

    assert [opened.auth for opened in instance.opened] == [(USERNAME, PASSWORD)]


def test_read_reports_a_hand_removed_rewrite_as_gone() -> None:
    # Which is how a rewrite deleted in the UI is restored by the next up
    # instead of drifting unnoticed.
    result = provider().read('any', dict(PROPS))

    assert result.id is None
    # The provider host writes its own key into the outs and mutates the
    # dict, so gone must come back as a fresh empty dict, never None.
    assert result.outs == {}


def test_read_keeps_a_rewrite_that_is_still_there(instance: Instance) -> None:
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    assert provider().read('an-id', dict(PROPS)).id == 'an-id'


def test_a_changed_answer_replaces_without_a_gap() -> None:
    """There is no update endpoint, and deleting first is a LAN outage.

    Two rewrites for one name coexist harmlessly for the instant between the
    create and the delete; no answer at all does not.
    """
    changed = rewrite_checked(dict(PROPS) | {'answer': '192.168.71.2'})

    result = provider().diff('an-id', rewrite_checked(dict(PROPS)), changed)

    assert result.changes is True
    assert result.replaces == ['answer']
    assert result.delete_before_replace is False


def test_an_input_that_is_still_unknown_is_an_unknown_diff_and_plans_no_replacement(instance: Instance) -> None:
    """Every declared property is a replacement, so an unknown one must not be read.

    During a preview an answer may be another resource's unresolved output. A
    placeholder compared as a value differs from whatever is stored, which here
    would plan a delete and a create of a row about to be identical.
    """
    news = rewrite_checked(dict(PROPS) | {'answer': rpc.UNKNOWN})

    result = provider().diff('an-id', rewrite_checked(dict(PROPS)), news)

    assert result.changes is None
    assert not result.replaces
    assert instance.opened == []


def test_a_rotated_login_is_a_change_nobody_declared() -> None:
    """The point of the session stamp: a rotation is a diff with no program in it.

    No caller mentions the login, so the only thing that can carry a rotation
    into a preview is a property the provider adds to the checked inputs
    itself. It is the same row on the same instance, so it is not a replace.
    """
    olds = rewrite_checked(dict(PROPS))
    news = rewrite_checked(dict(PROPS), password='rotated')

    result = provider().diff('an-id', olds, news)

    assert result.changes is True
    assert result.replaces == []


def test_a_re_stamp_records_the_new_login_and_calls_the_instance_not_at_all(instance: Instance) -> None:
    """A rotation must not rewrite every row on both instances.

    The row the instance holds is the row the resource declares, so there is
    nothing to write; what the update does is record which login the resource
    is now written through.
    """
    olds = rewrite_checked(dict(PROPS))
    news = rewrite_checked(dict(PROPS), password='rotated')

    result = provider().update('an-id', olds, news)

    assert result.outs == news
    assert instance.opened == []


def test_a_moved_instance_converges_without_replacing_a_single_row(instance: Instance) -> None:
    """Re-addressing an instance is an update: same instance, same rows.

    The endpoint is where this run reaches the instance and never part of what
    identifies a row, so nothing is deleted at the old address and nothing is
    created at the new one. What the update does is record the door the row is
    now written through — it calls neither address.
    """
    olds = rewrite_checked(dict(PROPS))
    news = rewrite_checked(dict(PROPS) | {'endpoint': MOVED})

    result = provider().diff('an-id', olds, news)

    assert result.changes is True
    assert result.replaces == []
    assert provider().update('an-id', olds, news).outs == news
    assert instance.opened == []


def test_a_changed_instance_replaces_the_row(instance: Instance) -> None:
    """The same name on the other resolver is a different row, not a moved one.

    Which is the other half of naming the instance: it identifies the write, so
    a row that changes instances is deleted where it was and created where it
    now belongs.
    """
    changed = rewrite_checked(dict(PROPS) | {'instance': 'adguard-bob'})

    result = provider().diff('an-id', rewrite_checked(dict(PROPS)), changed)

    assert result.replaces == ['instance']
    assert instance.opened == []


def test_a_rewrite_session_stamp_names_the_door_and_fingerprints_the_login() -> None:
    """`http://10.0.5.3:80#<12 hex>` — the door, and which login opens it.

    The digest is what a preview shows on a rotation, so it is stored in the
    clear: a truncated digest of a login is not the login, and a redacted one
    would say only that something opaque changed.
    """
    session = rewrite_checked(dict(PROPS))[configured.SESSION]
    endpoint, _, fingerprint = session.partition('#')

    assert endpoint == ENDPOINT
    assert fingerprint == hashlib.sha256(f'{USERNAME}:{PASSWORD}'.encode()).hexdigest()[: configured.FINGERPRINT_LENGTH]
    assert PASSWORD not in session


def test_a_change_to_this_module_is_a_change_a_reader_can_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider is pickled by reference, so editing an operation moves nothing.

    The version constant is what makes such an edit an update instead of a
    silent no-op that leaves every resource's outputs as the old code left them.
    """
    shipped = adguard_rewrites.VERSION
    olds = rewrite_checked(dict(PROPS))
    monkeypatch.setattr(adguard_rewrites, 'VERSION', f'{shipped}-next')
    news = rewrite_checked(dict(PROPS))

    assert olds[configured.PROVIDER_VERSION] == shipped
    assert news[configured.PROVIDER_VERSION] == f'{shipped}-next'
    assert provider().diff('an-id', olds, news).changes is True


def test_what_lands_in_state_for_a_rewrite_is_a_provider_with_nothing_in_it() -> None:
    """Every resource stores a pickle of its provider; this one is a name.

    Serialized through the engine's own function, so what is asserted is what a
    `__provider` property would actually hold. A class imported from a module is
    pickled by reference and `__getstate__` returns an empty bag, so state
    carries something inert: identical for every rewrite, identical across a
    rotation, and holding nothing that a rotation would have to reach into.
    """
    one = serialized(provider())
    rotated = serialized(provider('rotated'))

    assert provider().__getstate__() == {}
    assert one == rotated
    assert PASSWORD not in one
    assert len(one) < 256


def test_a_rewrite_provider_that_was_never_configured_has_no_login_to_dial_with() -> None:
    """The attributes exist only after `configure`, and that is the design.

    A default would not make an unconfigured provider safe; it would make one
    that dials with the wrong login. The plugin configures before the first
    operation, so nothing in production sees this state.
    """
    with pytest.raises(AttributeError):
        _ = adguard_rewrites.AdGuardRewriteProvider().password


def test_a_missing_half_of_the_rewrite_login_refuses_by_name() -> None:
    """A half-filled configuration stops the run rather than the session."""
    half = dynamic.Config({f'{PROJECT}:{adguard_rewrites.USERNAME_CONFIG}': USERNAME}, PROJECT)

    with pytest.raises(ValueError, match=adguard_rewrites.PASSWORD_CONFIG):
        adguard_rewrites.AdGuardRewriteProvider().configure(dynamic.ConfigureRequest(config=half))


def test_delete_removes_exactly_the_declared_pair(instance: Instance) -> None:
    instance.entries = [
        {'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'},
        {'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'},
    ]

    provider().delete('an-id', dict(PROPS))

    assert instance.entries == [{'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'}]
    assert instance.posts[0][0] == 'delete'


def test_a_refused_add_fails_the_create_rather_than_recording_a_row(instance: Instance) -> None:
    """A create that reports success is a row state records and the instance lacks.

    LAN clients would then take the public answer for a name every preview
    shows as rewritten.
    """
    instance.refusing = {'rewrite/add'}

    with pytest.raises(requests.HTTPError):
        _ = provider().create(dict(PROPS))

    assert instance.posts == [('add', {'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'})]


def test_a_refused_delete_fails_the_delete(instance: Instance) -> None:
    """A delete that reports success drops the row from state while the instance still answers it.

    The rewrite would then stay live with nothing declaring it, and no later
    run would ever look for it again.
    """
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]
    instance.refusing = {'rewrite/delete'}

    with pytest.raises(requests.HTTPError):
        provider().delete('an-id', dict(PROPS))


def test_a_refused_list_fails_the_read_rather_than_reporting_the_row_gone(instance: Instance) -> None:
    """A refusal says nothing about what the instance holds.

    Read as an empty list, it would report a row that is still there as
    deleted, and the next up would plan a create for it. The error the read
    raises is the instance's refusal, not a failure to parse its body.
    """
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]
    instance.refusing = {'rewrite/list'}

    with pytest.raises(requests.HTTPError):
        _ = provider().read('an-id', dict(PROPS))
