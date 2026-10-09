"""The AdGuard provider, against a stand-in for one AdGuard Home v0.107.79 instance.

**A provider under test is configured first**, because a provider in production
is: the plugin deserializes it out of a resource's `__provider` property and
calls `configure` before handing it any operation (framework/pulumi.md §5.3
E2). `configured_provider` below does that with a `ConfigureRequest` built the
way the plugin builds one -- the same class, the same project namespace -- so
what the tests exercise is the real ordering rather than attributes set by
hand.

**The stand-in is a model of the release, and it only tightens** (testing.md
§4). `Instance` serves every endpoint the providers reach, on the port the
release serves it on, starts from what a fresh v0.107.79 serves once its
first-run setup has configured it, and keeps what the release does that a
careless caller would miss. `Network` routes a request to an instance by host,
and a port nothing listens on refuses the connection:

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
11. **First run**: the API's port is closed and there is no DNS. On the setup
    port, `install/get_addresses` answers `200` and every other path `302`, to
    an `install.html` beside the path; followed, the redirects end in
    `TooManyRedirects`.
12. `install/configure`'s refusals -- a body that does not parse, a port 0, an
    unknown language, an address the box does not hold, a password under eight
    characters -- each leave first run. A success moves the API to the web
    address, creates the one account and closes the setup port.
13. The install routes answer `403` on the API's port after `configure`, and
    `404` after a restart, before any login is asked for.
14. A `configure` that answered `500` leaves the setup port up, and refuses
    later calls with `400` until a restart, which is a clean first run.
15. **No account**: every path answers without credentials. **The limiter**:
    a request without credentials is not counted, an accepted login resets the
    count, and after five refused logins the right one is refused (401) too,
    until a restart.

A case catches an operation that reads a refusal as success by expecting the
operation to raise; what the instance holds afterwards is the stand-in's doing,
not the provider's.
"""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import copy
import hashlib
import ipaddress
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast
from urllib.parse import urlsplit

import pulumi.dynamic as dynamic
import pytest
import requests
from mock_monitor import Recorder, declaring, run_with
from pulumi.runtime import rpc
from requests.auth import AuthBase, HTTPBasicAuth
from shimmed_serialization import serialized

from kluster.providers import adguard, configured
from kluster.providers.adguard import (
    api,
    base,
    clients,
    dns_server,
    filter_lists,
    filtering,
    log_settings,
    setup,
    user_rules,
)

INSTANCE = 'adguard-alice'
#: The ports an instance serves its API and its setup wizard on.
API_PORT = 80
SETUP_PORT = 3000
ENDPOINT = 'http://10.0.5.3:80'
SETUP_ENDPOINT = 'http://10.0.5.3:3000'
MOVED = 'http://10.0.5.30:80'
#: A second instance, for what one command does to each of two.
BOB = 'adguard-bob'
BOB_ENDPOINT = 'http://10.0.5.4:80'
BOB_SETUP_ENDPOINT = 'http://10.0.5.4:3000'
USERNAME = 'admin'
PASSWORD = 'a-typed-secret'
#: What a setup declares: the web server on every address on the API's port,
#: and DNS on every address.
LISTEN: dict[str, Any] = {'web': {'ip': '0.0.0.0', 'port': API_PORT}, 'dns': {'ip': '0.0.0.0', 'port': 53}}
#: Refused logins after which the instance refuses the right one too.
LIMIT = 5

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

#: What a fresh v0.107.79 serves, from a throwaway instance configured through
#: its first-run setup and nothing else. Lists the instance computes or
#: measures are left out.
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
    def __init__(
        self, url: str, payload: object, status: int = 200, text: str = '', headers: dict[str, str] | None = None
    ) -> None:
        self.url: str = url
        self.payload: object = payload
        self.status_code: int = status
        self.text: str = text
        self.headers: dict[str, str] = headers or {}

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


class Call(NamedTuple):
    """One connection attempt, answered or not."""

    method: str
    port: int
    #: The path under `/control/`.
    path: str
    #: The basic-authentication pair it carried, read as UTF-8, or `None` for none.
    auth: tuple[str, str] | None


@dataclass
class Instance:
    """One AdGuard instance: its listeners, its accounts, its configuration, and a log of what was asked of it."""

    #: The rewrite list's rows, in the order the instance holds them.
    entries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: Every POST, as `(last path segment, body)`, refused ones included.
    posts: list[tuple[str, Any]] = field(default_factory=list[tuple[str, Any]])
    #: Every request a listener took, as `(method, path under /control/, body)`, refused ones included.
    requests: list[tuple[str, str, Any]] = field(default_factory=list[tuple[str, str, Any]])
    #: Every connection attempt, a refused or timed-out one included.
    calls: list[Call] = field(default_factory=list[Call])
    #: Every session that dialed it, so a test can ask whether anything did.
    opened: list[FakeSession] = field(default_factory=list['FakeSession'])
    #: The paths under `/control/` the instance refuses. A refused request
    #: changes nothing the instance holds.
    refusing: set[str] = field(default_factory=set[str])

    #: The accounts, as `(name, password)`. Empty is an instance configured
    #: with `users: []`, which asks no request for a login (item 15).
    accounts: list[tuple[str, str]] = field(default_factory=lambda: [(USERNAME, PASSWORD)])
    #: Where the configuration's web server listens, as `(address, port)`, or
    #: `None` while there is no configuration file: in first run, and after a
    #: `configure` that failed to write it.
    web: tuple[str, int] | None = ('0.0.0.0', API_PORT)
    #: The setup wizard's port, on every address, or `None` while it is down.
    wizard: int | None = None
    #: Whether this process left first run through `configure` (item 13).
    installed_here: bool = False
    #: Whether a `configure` failed after adding the account (item 14).
    half_applied: bool = False
    #: Whether the work directory takes the file `configure` writes.
    writable: bool = True
    #: The addresses the box holds, which `configure` can bind.
    addresses: frozenset[str] = frozenset({'10.0.5.3', '10.0.5.30', '10.0.5.4', '127.0.0.1'})
    #: Refused logins since the last accepted one (item 15).
    failures: int = 0
    #: Ports whose packets are dropped, so a connection times out rather than being refused.
    dropped: set[int] = field(default_factory=set[int])
    #: Connections to the web server refused after `configure`, before it has rebound.
    rebinding: int = 0
    #: Whether an answer to the status path breaks off mid-body once a login has been taken.
    breaking: bool = False
    #: Where an answer to the status path without credentials waits for other
    #: callers, so that callers racing for one instance reach it together.
    held: threading.Barrier | None = None

    user_rules: list[str] | None = None
    #: The rewrite list's switch, which `configure` leaves on.
    rewrites_enabled: bool = True
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

    @classmethod
    def in_first_run(cls) -> Instance:
        """An instance started with no configuration file (item 11)."""
        return cls(accounts=[], web=None, wizard=SETUP_PORT)

    def restart(self) -> None:
        """Start the process again: on its file, configured; with none, in a clean first run (item 14)."""
        if self.web is None:
            self.accounts, self.wizard, self.half_applied = [], SETUP_PORT, False
        else:
            self.wizard, self.installed_here = None, False
        # The limiter's block is held in memory.
        self.failures = 0

    def writes(self) -> list[tuple[str, str, Any]]:
        """Every request but a GET."""
        return [request for request in self.requests if request[0] != 'GET']

    def credentialed(self) -> list[Call]:
        """Every connection attempt that carried a login."""
        return [call for call in self.calls if call.auth is not None]

    def answer(
        self, session: FakeSession, method: str, url: str, body: Any, *, as_json: bool, allow_redirects: bool
    ) -> FakeResponse:
        parts = urlsplit(url)
        port = parts.port or API_PORT
        path = url.split('/control/', 1)[1]
        credentials = session.credentials()
        auth = (
            None
            if credentials is None
            else (credentials[0].decode(errors='replace'), credentials[1].decode(errors='replace'))
        )
        self.calls.append(Call(method, port, path, auth))
        if session not in self.opened:
            self.opened.append(session)
        if port in self.dropped:
            raise requests.ConnectTimeout(f'connecting to {url} timed out')
        if self.web is not None and port == self.web[1] and self.web[0] in ('0.0.0.0', parts.hostname):
            if self.rebinding:
                self.rebinding -= 1
                raise requests.ConnectionError(f'connecting to {url}: connection refused')
            return self._api(credentials, method, url, path, body, as_json=as_json)
        if port == self.wizard:
            return self._wizard(method, url, path, body, as_json=as_json, allow_redirects=allow_redirects)
        raise requests.ConnectionError(f'connecting to {url}: connection refused')

    def _api(
        self, credentials: tuple[bytes, bytes] | None, method: str, url: str, path: str, body: Any, *, as_json: bool
    ) -> FakeResponse:
        """What the configuration's web server answers."""
        self.requests.append((method, path, body))
        if method == 'POST':
            self.posts.append((path.rsplit('/', 1)[-1], body))
        if path.startswith('install/'):
            # Public, so answered before any login is asked for (item 13).
            if self.installed_here:
                return FakeResponse(url, None, 403, 'application is already configured')
            return FakeResponse(url, None, 404, '404 page not found')
        if path == 'status' and credentials is None and self.held is not None:
            # A caller the lock let through alone waits out the barrier and breaks it.
            with contextlib.suppress(threading.BrokenBarrierError):
                _ = self.held.wait()
        if self.accounts and not self._admitted(credentials):
            return FakeResponse(url, None, 401, 'Unauthorized')
        if path == 'status' and credentials is not None and self.breaking:
            raise requests.exceptions.ChunkedEncodingError('Connection broken: IncompleteRead(0 bytes read)')
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

    def _admitted(self, credentials: tuple[bytes, bytes] | None) -> bool:
        """Whether a request's login opens a `/control/` path, counted by the limiter (item 15).

        The accounts hold what `configure` received as JSON, so a login matches
        as the UTF-8 of each half, against the bytes basic authentication
        carried, split at their first colon.
        """
        if credentials is None:
            return False
        if self.failures >= LIMIT:
            return False
        if credentials in [(name.encode(), password.encode()) for name, password in self.accounts]:
            self.failures = 0
            return True
        self.failures += 1
        return False

    def _wizard(
        self, method: str, url: str, path: str, body: Any, *, as_json: bool, allow_redirects: bool
    ) -> FakeResponse:
        """What the setup wizard answers, to anyone (item 11)."""
        self.requests.append((method, path, body))
        if path == 'install/get_addresses' and method == 'GET':
            return FakeResponse(url, {'interfaces': {}, 'version': 'v0.107.79', 'web_port': 80, 'dns_port': 53})
        if path == 'install/configure':
            return self._configure(url, method, body, as_json=as_json)
        if allow_redirects:
            # The redirect's target redirects again.
            raise requests.TooManyRedirects('Exceeded 30 redirects.')
        folder = path.rpartition('/')[0]
        location = f'/control/{folder}/install.html' if folder else '/control/install.html'
        return FakeResponse(url, None, 302, '', {'Location': location})

    def _configure(self, url: str, method: str, body: Any, *, as_json: bool) -> FakeResponse:
        """`install/configure`, its refusals in the release's order (item 12)."""
        if method != 'POST':
            return FakeResponse(url, None, 405, 'Method Not Allowed')
        if not as_json:
            return FakeResponse(url, None, 415, 'only content-type application/json is allowed')
        try:
            web, dns = cast('dict[str, Any]', body['web']), cast('dict[str, Any]', body['dns'])
            username, password = str(body.get('username', '')), str(body['password'])
            for part in (web, dns):
                _ = ipaddress.ip_address(part['ip'])
                if not isinstance(part['port'], int):
                    raise TypeError(part['port'])
        except (KeyError, TypeError, ValueError) as unparsed:
            return FakeResponse(url, None, 400, f'parsing request: {unparsed}')
        if not web['port'] or not dns['port']:
            return FakeResponse(url, None, 400, 'ports cannot be 0')
        if body.get('language', '') not in ('', 'en'):
            return FakeResponse(url, None, 400, f'unknown language: "{body["language"]}"')
        if web['ip'] != '0.0.0.0' and web['ip'] not in self.addresses:
            reason = f'listen tcp {web["ip"]}:{web["port"]}: bind: cannot assign requested address'
            return FakeResponse(url, None, 400, f'checking address {web["ip"]}:{web["port"]}: {reason}')
        if len(password) < 8:
            return FakeResponse(url, None, 422, 'password must be at least 8 symbols long')
        if dns['ip'] != '0.0.0.0' and dns['ip'] not in self.addresses:
            return FakeResponse(
                url, None, 400, f'listen udp {dns["ip"]}:{dns["port"]}: bind: cannot assign requested address'
            )
        if self.half_applied:
            return FakeResponse(url, None, 400, f'listen udp {dns["ip"]}:{dns["port"]}: bind: address already in use')
        # The account is added and the DNS server started before the file is written.
        self.accounts = [(username, password)]
        if not self.writable:
            self.half_applied, self.accounts = True, []
            return FakeResponse(
                url,
                None,
                500,
                "Couldn't write config: writing config file: open /data/adguard/.AdGuardHome.yaml: permission denied",
            )
        self.web, self.wizard, self.installed_here = (str(web['ip']), int(web['port'])), None, True
        return FakeResponse(url, None, 200, 'OK')

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
        elif path == 'rewrite/settings/update':
            self.rewrites_enabled = bool(body.get('enabled'))
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
            case 'rewrite/settings':
                return {'enabled': self.rewrites_enabled}
            case 'status':
                return {'version': 'v0.107.79', 'running': True, 'protection_enabled': self.dns['protection_enabled']}
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


@dataclass
class Network:
    """The instances, by the host each is reached at."""

    boxes: dict[str, Instance] = field(default_factory=dict[str, Instance])

    def session(self) -> FakeSession:
        """A `requests.Session`, as the provider builds one."""
        return FakeSession(self)

    def answer(
        self, session: FakeSession, method: str, url: str, body: Any, *, as_json: bool, allow_redirects: bool
    ) -> FakeResponse:
        box = self.boxes.get(urlsplit(url).hostname or '')
        if box is None:
            raise requests.ConnectionError(f'connecting to {url}: no route to host')
        return box.answer(session, method, url, body, as_json=as_json, allow_redirects=allow_redirects)


class FakeSession:
    def __init__(self, network: Network) -> None:
        self.network: Network = network
        self.auth: tuple[str, str] | AuthBase | None = None

    def credentials(self) -> tuple[bytes, bytes] | None:
        """The bytes a request's basic authentication carries, written by `requests` itself, split at the first colon."""
        if self.auth is None:
            return None
        auth = HTTPBasicAuth(*self.auth) if isinstance(self.auth, tuple) else self.auth
        prepared = requests.PreparedRequest()
        prepared.prepare_headers({})
        header = cast('Any', auth)(prepared).headers['Authorization']
        username, _, password = base64.b64decode(str(header).removeprefix('Basic ')).partition(b':')
        return username, password

    def get(self, url: str, timeout: int = 0, allow_redirects: bool = True) -> FakeResponse:
        return self.network.answer(self, 'GET', url, None, as_json=True, allow_redirects=allow_redirects)

    def post(
        self,
        url: str,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int = 0,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        sent_json = data is None and (headers or {}).get('Content-Type', 'application/json') == 'application/json'
        return self.network.answer(
            self, 'POST', url, data if json is None else json, as_json=sent_json, allow_redirects=allow_redirects
        )

    def put(self, url: str, json: Any = None, timeout: int = 0, allow_redirects: bool = True) -> FakeResponse:
        return self.network.answer(self, 'PUT', url, json, as_json=True, allow_redirects=allow_redirects)


@pytest.fixture(autouse=True)
def network(monkeypatch: pytest.MonkeyPatch) -> Network:
    """What every session the provider opens reaches. Each case is a `pulumi` command of its own, verdicts included."""
    served = Network()
    monkeypatch.setattr(requests, 'Session', served.session)
    monkeypatch.setattr(api, '_verdicts', {})
    return served


@pytest.fixture(autouse=True)
def instance(network: Network) -> Instance:
    """The instance the provider reaches at either of its addresses, configured, unless a case changes it."""
    served = Instance()
    network.boxes.update({'10.0.5.3': served, '10.0.5.30': served})
    return served


@pytest.fixture
def first_run(network: Network) -> Instance:
    """The instance the provider reaches, started with no configuration file instead."""
    served = Instance.in_first_run()
    network.boxes.update({'10.0.5.3': served, '10.0.5.30': served})
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


def _edit_rules(instance: Instance) -> None:
    instance.user_rules = ['|tube^$dnsrewrite=NOERROR;A;192.168.71.9']
    instance.rewrites_enabled = True


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
            ],
            'rewrites_enabled': False,
        },
        _edit_rules,
        ('filtering/status', 'rewrite/list', 'rewrite/settings'),
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
            # Out of URL order, which is the order the instance is read in.
            'filters': [
                {'url': LIST_ONE, 'name': 'one', 'enabled': False},
                {'url': DNS_FILTER, 'name': 'AdGuard DNS filter, declared', 'enabled': True},
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


def configured_provider[P: base.InstanceProvider](cls: type[P], password: str = PASSWORD) -> P:
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
    cls: type[base.InstanceProvider],
    sections: dict[str, Any],
    *,
    password: str = PASSWORD,
    endpoint: str = ENDPOINT,
    setup_endpoint: str = SETUP_ENDPOINT,
    instance: str = INSTANCE,
) -> dict[str, Any]:
    """The inputs as the engine stores and compares them: what `check` returned, every number a float."""
    result = configured_provider(cls, password).check(
        {}, as_engine({'instance': instance, 'endpoint': endpoint, 'setup_endpoint': setup_endpoint, **sections})
    )
    assert not result.failures, [(failure.property, failure.reason) for failure in result.failures or []]
    return result.inputs


def created(kind: Kind) -> dict[str, Any]:
    """The stored outputs of `kind` created on the instance."""
    news = checked(kind.provider, kind.declared)
    return dict(configured_provider(kind.provider).create(news).outs or {})


def refreshed(cls: type[base.InstanceProvider], stored: dict[str, Any]) -> dict[str, Any]:
    """What a refresh leaves as the stored outputs."""
    return dict(configured_provider(cls).read(f'{INSTANCE}|{cls.kind}', stored).outs or {})


def drifted(cls: type[base.InstanceProvider], stored: dict[str, Any], sections: dict[str, Any]) -> bool | None:
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
        recorded = operations.update('an-id', olds, news).outs or {}
        assert {key: recorded[key] for key in base.RECORDED} == {key: news[key] for key in base.RECORDED}
    assert instance.opened == []


@EVERY_KIND
def test_a_refresh_after_a_write_reads_back_the_inputs_and_the_outputs_state_holds(
    kind: Kind, instance: Instance
) -> None:
    """A refreshing preview of an instance that holds what was written marks nothing.

    The `[diff: …]` beside a refresh step compares the inputs state holds
    with the inputs `read` returns, and `--diff` prints the outputs that
    differ beside it. So both bags are held to what a refresh reads back: a
    section stored as declared rather than as the instance reports it -- the
    rewrite list absent, a client's identifiers or the lists in the
    declaration's order -- makes a clean refresh read as drift. Held after a
    create, and after a rotation's update that wrote nothing.
    """
    news = checked(kind.provider, kind.declared)
    stored = dict(configured_provider(kind.provider).create(news).outs or {})
    rotated_news = checked(kind.provider, kind.declared, password='rotated')
    rotated = dict(configured_provider(kind.provider).update('an-id', stored, rotated_news).outs or {})

    for inputs, outputs in ((news, stored), (rotated_news, rotated)):
        result = configured_provider(kind.provider).read(f'{INSTANCE}|{kind.provider.kind}', outputs)
        assert result.inputs == inputs
        assert result.outs == outputs


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
    """Every request carries the login but the classification's first, which asks whether an account exists."""
    _ = created(kind)

    assert {call.auth for call in instance.credentialed()} == {(USERNAME, PASSWORD)}
    assert [(call.path, call.auth) for call in instance.calls if call.auth is None] == [('status', None)]


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


def _failures(
    cls: type[base.InstanceProvider], sections: dict[str, Any], *, password: str = PASSWORD, endpoint: str = ENDPOINT
) -> list[str]:
    result = configured_provider(cls, password).check(
        {}, {'instance': INSTANCE, 'endpoint': endpoint, 'setup_endpoint': SETUP_ENDPOINT, **sections}
    )
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


def test_the_rewrite_switch_the_setup_leaves_on_is_turned_off_and_a_hand_toggle_is_drift(instance: Instance) -> None:
    """A rebuilt instance comes up with the rewrite list on, so the declaration is what turns it off."""
    assert instance.rewrites_enabled is True

    stored = created(KINDS[0])

    assert instance.rewrites_enabled is False
    assert ('PUT', 'rewrite/settings/update', {'enabled': False}) in instance.requests

    instance.rewrites_enabled = True

    assert drifted(RULES, stored, KINDS[0].declared) is True
    _ = configured_provider(RULES).update('an-id', refreshed(RULES, stored), checked(RULES, KINDS[0].declared))
    assert instance.rewrites_enabled is False


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
# AdGuardSetup, and the classification every kind consults.

SETUP = setup.AdGuardSetupProvider
SET_UP: dict[str, Any] = {'listen': LISTEN}

#: Every kind, the setup included, each with a declaration of every section.
SEVEN: tuple[tuple[type[base.InstanceProvider], dict[str, Any]], ...] = (
    (SETUP, SET_UP),
    *((kind.provider, kind.declared) for kind in KINDS),
)
EVERY_ONE_OF_SEVEN = pytest.mark.parametrize(('cls', 'declared'), SEVEN, ids=[cls.kind for cls, _ in SEVEN])

#: How long the status answer without credentials waits for the other callers
#: of one instance, in the concurrent case. Only a caller the lock let through
#: reaches it alone, so with the lock in place every wait runs out.
HOLD = 0.5


def test_a_first_run_instance_is_configured_with_the_declared_listen_and_the_login_and_the_six_then_write(
    first_run: Instance,
) -> None:
    """One command: the setup's create configures the instance, and each of the six then creates and converges."""
    stored = configured_provider(SETUP).create(checked(SETUP, SET_UP)).outs or {}

    (sent,) = [body for _method, path, body in first_run.requests if path == 'install/configure']
    assert sent == {**LISTEN, 'username': USERNAME, 'password': PASSWORD}
    assert [call.auth for call in first_run.calls if call.path == 'install/configure'] == [None]
    assert first_run.accounts == [(USERNAME, PASSWORD)]
    assert first_run.web == ('0.0.0.0', API_PORT)
    assert stored['listen'] == as_engine(LISTEN)
    for kind in KINDS:
        assert drifted(kind.provider, created(kind), kind.declared) is False, kind


def test_a_configured_instance_whose_login_is_accepted_is_adopted_with_no_write(instance: Instance) -> None:
    result = configured_provider(SETUP).create(checked(SETUP, SET_UP))

    assert result.id == f'{INSTANCE}|setup'
    assert instance.writes() == []
    assert [call.port for call in instance.calls] == [API_PORT, API_PORT]


@pytest.mark.parametrize('password', ['pässwörd-123', '八个字符的密码呀'], ids=['latin', 'cjk'])
def test_a_non_ascii_login_the_setup_configures_then_authenticates(password: str, first_run: Instance) -> None:
    """`configure` stores the login as the UTF-8 of its JSON, so every later request has to send that."""
    _ = configured_provider(SETUP, password).create(checked(SETUP, SET_UP, password=password))

    assert first_run.accounts == [(USERNAME, password)]
    assert [call.auth for call in first_run.credentialed()] == [(USERNAME, password)]
    assert first_run.failures == 0


def test_a_configure_answering_500_is_not_retried(first_run: Instance) -> None:
    """The instance is half-applied: it refuses every later configure, so a retry would only bury the reason."""
    first_run.writable = False

    with pytest.raises(setup.HalfApplied, match=r'500: Couldn.t write config.*half-applied.*restarts'):
        _ = configured_provider(SETUP).create(checked(SETUP, SET_UP))

    assert [path for _method, path, _body in first_run.writes()] == ['install/configure']


def test_a_half_applied_instance_is_refused_naming_the_restart_it_needs(
    first_run: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a `500` the instance looks like first run to the next command, and refuses its configure."""
    first_run.writable = False
    with pytest.raises(setup.HalfApplied, match='Restart the machine or container'):
        _ = configured_provider(SETUP).create(checked(SETUP, SET_UP))
    # The next command.
    monkeypatch.setattr(api, '_verdicts', {})

    with pytest.raises(setup.SetupRefused, match=r'400: .*address already in use.*half-applied.*restart the machine'):
        _ = configured_provider(SETUP).create(checked(SETUP, SET_UP))


def test_a_refused_configure_raises_with_the_instances_reason_and_leaves_first_run(first_run: Instance) -> None:
    elsewhere = {'listen': {**LISTEN, 'web': {'ip': '10.0.5.99', 'port': API_PORT}}}

    with pytest.raises(setup.SetupRefused, match=r'400: checking address 10\.0\.5\.99:80.*has applied nothing'):
        _ = configured_provider(SETUP).create(checked(SETUP, elsewhere, endpoint='http://10.0.5.99:80'))

    assert first_run.wizard == SETUP_PORT
    assert first_run.accounts == []
    assert [path for _method, path, _body in first_run.writes()] == ['install/configure']


def test_the_setup_waits_out_the_rebind(first_run: Instance, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection after `configure` is the web server not rebound yet, and is tried again."""
    monkeypatch.setattr(setup, 'POLL_INTERVAL', 0)
    first_run.rebinding = 2

    _ = configured_provider(SETUP).create(checked(SETUP, SET_UP))

    assert [call.path for call in first_run.credentialed()] == ['status'] * 3


def test_the_setup_never_retries_an_answer_to_the_login(first_run: Instance, monkeypatch: pytest.MonkeyPatch) -> None:
    """An answer is final, because every refused login counts toward the instance's limiter."""
    monkeypatch.setattr(setup, 'POLL_INTERVAL', 0)
    monkeypatch.setattr(setup, 'REBIND_TIMEOUT', 1)
    first_run.refusing = {'status'}

    with pytest.raises(api.Unusable, match='took its configure, but'):
        _ = configured_provider(SETUP).create(checked(SETUP, SET_UP))

    assert [call.path for call in first_run.credentialed()] == ['status']


def test_concurrent_reads_with_a_refused_login_send_one_credentialed_request_per_instance(
    instance: Instance, network: Network
) -> None:
    """Every resource of one command shares its instance's one verdict, whatever order and concurrency the host reads in.

    The workers start together, and each instance holds its answer to the
    uncredentialed status request until its other callers arrive or `HOLD`
    runs out, so callers the lock did not serialize meet inside the
    classification and each send the login.
    """
    bob = Instance()
    network.boxes['10.0.5.4'] = bob
    reads: list[tuple[type[base.InstanceProvider], dict[str, Any]]] = []
    for name, endpoint, setup_endpoint, box in (
        (INSTANCE, ENDPOINT, SETUP_ENDPOINT, instance),
        (BOB, BOB_ENDPOINT, BOB_SETUP_ENDPOINT, bob),
    ):
        box.held = threading.Barrier(len(SEVEN), timeout=HOLD)
        reads += [
            (cls, checked(cls, declared, instance=name, endpoint=endpoint, setup_endpoint=setup_endpoint))
            for cls, declared in SEVEN
        ]
    start = threading.Barrier(len(reads))

    def read(cls: type[base.InstanceProvider], stored: dict[str, Any]) -> Exception | None:
        provider = configured_provider(cls, 'not-the-password')
        _ = start.wait()
        try:
            _ = provider.read(f'{stored["instance"]}|{cls.kind}', stored)
        except Exception as raised:  # noqa: BLE001 -- the outcome is what is compared
            return raised
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(reads)) as pool:
        outcomes = list(pool.map(read, *zip(*reads, strict=True)))

    assert [type(outcome) for outcome in outcomes] == [api.Unusable] * len(reads)
    assert all('refused the login' in str(outcome) and '15 minutes' in str(outcome) for outcome in outcomes)
    assert [len(box.credentialed()) for box in (instance, bob)] == [1, 1]
    assert [box.failures for box in (instance, bob)] == [1, 1]


def test_a_classification_that_breaks_off_after_the_login_is_a_verdict_too(instance: Instance) -> None:
    """Every request of the classification can fail after the login went out; none of them sends it twice."""
    instance.breaking = True

    for cls, declared in SEVEN:
        with pytest.raises(api.Unusable, match='ChunkedEncodingError'):
            _ = configured_provider(cls).read('an-id', checked(cls, declared))

    assert len(instance.credentialed()) == 1


@EVERY_ONE_OF_SEVEN
def test_nothing_is_sent_to_the_setup_port_while_the_api_port_answers(
    cls: type[base.InstanceProvider], declared: dict[str, Any], instance: Instance
) -> None:
    _ = configured_provider(cls).read('an-id', checked(cls, declared))

    assert SETUP_PORT not in {call.port for call in instance.calls}


@pytest.mark.parametrize('setup_port', ['refused', 'dropped'])
@EVERY_ONE_OF_SEVEN
def test_an_instance_down_on_both_ports_raises_in_every_kind(
    cls: type[base.InstanceProvider], declared: dict[str, Any], setup_port: str, instance: Instance
) -> None:
    """A dropped setup port is what a caller that reaches the API's port alone, CI's, meets on a first-run instance."""
    instance.web = None
    if setup_port == 'dropped':
        instance.wizard, instance.dropped = SETUP_PORT, {SETUP_PORT}
    provider = configured_provider(cls)
    stored = checked(cls, declared)

    with pytest.raises(api.Unusable, match='unreachable'):
        _ = provider.read('an-id', stored)
    with pytest.raises(api.Unusable, match='unreachable'):
        _ = provider.create(stored)
    assert instance.credentialed() == []


@EVERY_ONE_OF_SEVEN
def test_a_first_run_instance_reads_as_gone_in_every_kind(
    cls: type[base.InstanceProvider], declared: dict[str, Any], first_run: Instance
) -> None:
    """It holds no configuration at all, so a refreshing run drops the resource and re-creates it after the setup."""
    result = configured_provider(cls).read('an-id', checked(cls, declared))

    assert result.id is None
    assert result.outs == {}
    assert first_run.credentialed() == []


@pytest.mark.parametrize('kind', KINDS, ids=str)
def test_the_six_refuse_to_write_a_first_run_instance_their_setup_has_not_configured(
    kind: Kind, first_run: Instance
) -> None:
    provider = configured_provider(kind.provider)

    with pytest.raises(base.SetupNotRun, match='is in first run'):
        _ = provider.create(checked(kind.provider, kind.declared))

    assert first_run.credentialed() == []
    assert first_run.writes() == []


@EVERY_ONE_OF_SEVEN
def test_an_instance_with_no_account_raises(
    cls: type[base.InstanceProvider], declared: dict[str, Any], instance: Instance
) -> None:
    """Every path answers it without credentials, so anyone who reaches it can configure it."""
    instance.accounts = []

    with pytest.raises(api.Unusable, match='holds no account'):
        _ = configured_provider(cls).read('an-id', checked(cls, declared))
    assert instance.credentialed() == []


def test_a_changed_listen_raises_at_update(instance: Instance) -> None:
    olds = checked(SETUP, SET_UP)
    news = checked(SETUP, {'listen': {**LISTEN, 'dns': {'ip': '10.0.5.3', 'port': 53}}})
    provider = configured_provider(SETUP)

    assert provider.diff('an-id', olds, news).changes is True
    with pytest.raises(setup.ListenChanged, match=r"Only an instance's first run sets it"):
        _ = provider.update('an-id', olds, news)
    assert instance.opened == []


def test_a_moved_login_on_setup_sends_one_credentialed_request_and_a_refusal_raises(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No endpoint changes an account, so a rotation is a reset: the update says so rather than recording it."""
    olds = checked(SETUP, SET_UP)
    rotated = checked(SETUP, SET_UP, password='a-rotated-secret')

    with pytest.raises(api.Unusable, match=r'refused the login.*reset'):
        _ = configured_provider(SETUP, 'a-rotated-secret').update('an-id', olds, rotated)
    assert [call.auth for call in instance.credentialed()] == [(USERNAME, 'a-rotated-secret')]

    # The next command, after the instance's reset made the rotated login its account.
    monkeypatch.setattr(api, '_verdicts', {})
    instance.calls.clear()
    instance.accounts = [(USERNAME, 'a-rotated-secret')]

    assert configured_provider(SETUP, 'a-rotated-secret').update('an-id', olds, rotated).outs == rotated
    assert [call.auth for call in instance.credentialed()] == [(USERNAME, 'a-rotated-secret')]


def test_a_moved_endpoint_on_setup_is_recorded_with_no_call(instance: Instance) -> None:
    olds = checked(SETUP, SET_UP)
    news = checked(SETUP, SET_UP, endpoint=MOVED, setup_endpoint='http://10.0.5.30:3000')
    provider = configured_provider(SETUP)

    assert provider.diff('an-id', olds, news).changes is True
    assert provider.diff('an-id', olds, news).replaces == []
    assert provider.update('an-id', olds, news).outs == news
    assert instance.opened == []


def test_a_configured_setup_reads_back_its_stored_bag(instance: Instance) -> None:
    """`GET /control/status` reports the expanded interface list, not the bind address, so `listen` is not read."""
    stored = checked(SETUP, SET_UP)

    result = configured_provider(SETUP).read(f'{INSTANCE}|setup', stored)

    assert result.outs == stored
    assert result.inputs == stored
    assert instance.writes() == []


@pytest.mark.parametrize(
    ('password', 'username', 'key'),
    [
        ('a7chars', USERNAME, base.PASSWORD_CONFIG),
        ('a-typed-secret', '', base.USERNAME_CONFIG),
        ('a-typed-secret', 'ad:min', base.USERNAME_CONFIG),
    ],
    ids=['short-password', 'empty-username', 'colon-username'],
)
def test_check_refuses_a_short_password_and_an_empty_username_by_key_never_by_value(
    password: str, username: str, key: str
) -> None:
    provider = SETUP()
    provider.configure(
        dynamic.ConfigureRequest(
            config=dynamic.Config(
                {f'{PROJECT}:{base.USERNAME_CONFIG}': username, f'{PROJECT}:{base.PASSWORD_CONFIG}': password},
                PROJECT,
            )
        )
    )

    result = provider.check(
        {}, {'instance': INSTANCE, 'endpoint': ENDPOINT, 'setup_endpoint': SETUP_ENDPOINT, **SET_UP}
    )

    failures = [(failure.property, failure.reason) for failure in result.failures or []]
    assert [prop for prop, _reason in failures] == [key]
    assert all(password not in reason for _prop, reason in failures)
    assert all(username not in reason for _prop, reason in failures if username)


@pytest.mark.parametrize(
    ('listen', 'endpoint', 'reason'),
    [
        (LISTEN, 'http://10.0.5.3:8080', 'endpoint http://10.0.5.3:8080 is not on listen.web.port 80'),
        (
            {**LISTEN, 'web': {'ip': '10.0.5.3', 'port': 80}},
            'http://10.0.5.30:80',
            'endpoint http://10.0.5.30:80 is not on listen.web.ip 10.0.5.3',
        ),
        ({**LISTEN, 'web': {'ip': '0.0.0.0', 'port': 0}}, ENDPOINT, 'listen.web.port 0 is not a port'),
        ({**LISTEN, 'dns': {'ip': '0.0.0.0', 'port': 80}}, ENDPOINT, 'listen.web and listen.dns are on one port'),
        ({**LISTEN, 'dns': {'ip': 'bogus', 'port': 53}}, ENDPOINT, "listen.dns.ip 'bogus' is not an address"),
        ({'web': LISTEN['web']}, ENDPOINT, 'listen lacks dns'),
    ],
    ids=['endpoint-port', 'endpoint-host', 'port-zero', 'one-port', 'unparsed-address', 'missing-part'],
)
def test_check_refuses_a_listen_configure_would_refuse_or_an_endpoint_it_would_not_reach(
    listen: dict[str, Any], endpoint: str, reason: str
) -> None:
    assert any(reason in failure for failure in _failures(SETUP, {'listen': listen}, endpoint=endpoint)), _failures(
        SETUP, {'listen': listen}, endpoint=endpoint
    )


def test_check_accepts_the_setup_declaration() -> None:
    assert _failures(SETUP, SET_UP) == []


def test_a_redirect_is_a_refusal_naming_where_it_points(first_run: Instance) -> None:
    """A first-run instance redirects every path but its install routes, and following them ends nowhere useful."""
    stored = checked(RULES, KINDS[0].declared, endpoint=SETUP_ENDPOINT)

    with pytest.raises(api.Unusable, match=r'answered 302 to /control/install\.html'):
        _ = configured_provider(RULES).read('an-id', stored)


def test_a_redirect_answering_a_credentialed_request_is_a_refusal(first_run: Instance) -> None:
    with pytest.raises(requests.HTTPError, match=r'GET /control/status answered 302 to /control/install\.html'):
        _ = api.Api(SETUP_ENDPOINT, USERNAME, PASSWORD).get('status')


def test_the_setup_is_a_kind_with_the_shared_lifecycle(instance: Instance) -> None:
    """Its id is the instance and the kind; a changed instance replaces; delete calls nothing."""
    olds = checked(SETUP, SET_UP)
    provider = configured_provider(SETUP)

    assert provider.diff('an-id', olds, checked(SETUP, SET_UP, instance=BOB)).replaces == ['instance']
    assert provider.diff('an-id', olds, olds).changes is False
    provider.delete('an-id', olds)
    assert instance.opened == []


#: Each kind's resource class, with what a declaration passes it besides the instance and the endpoints.
RESOURCES: tuple[tuple[Callable[..., dynamic.Resource], dict[str, Any]], ...] = (
    (adguard.AdGuardSetup, SET_UP),
    (adguard.AdGuardUserRules, {'rules': KINDS[0].declared['rules']}),
    (adguard.AdGuardDnsServer, KINDS[1].declared),
    (adguard.AdGuardFiltering, KINDS[2].declared),
    (adguard.AdGuardFilterLists, KINDS[3].declared),
    (adguard.AdGuardClients, KINDS[4].declared),
    (adguard.AdGuardLogSettings, KINDS[5].declared),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('resource', 'sections'), RESOURCES, ids=[getattr(cls, '__name__', '') for cls, _ in RESOURCES]
)
async def test_every_kind_declares_through_the_engine_and_resolves_its_outputs(
    resource: Callable[..., dynamic.Resource], sections: dict[str, Any]
) -> None:
    """The SDK holds a resolved output to its annotation, and refuses a dict where the class says a `TypedDict`."""
    _ = await run_with(Recorder(), stack='dns')
    async with declaring():
        declared = resource('kind', instance=INSTANCE, endpoint=ENDPOINT, setup_endpoint=SETUP_ENDPOINT, **sections)

    for section, value in sections.items():
        output = getattr(declared, section)
        assert await output.future() == as_engine(value)
