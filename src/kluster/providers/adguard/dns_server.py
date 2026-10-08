"""`AdGuardDnsServer`: an instance's DNS server settings, its protection switch, and its access lists.

**`dns` is every field `GET /control/dns_info` reports** but the two the
instance computes, `default_local_ptr_upstreams` and
`protection_disabled_until`. It is compared field by field, with the
instance's own readings:

-   `upstream_mode` reads back as `""` for load balancing, which compares equal
    to `load_balance`;
-   the blocking addresses are compared only under the `custom_ip` blocking
    mode, and the EDNS custom address only while `edns_cs_use_custom` is on,
    since the instance takes them only then.

**Every field but protection goes in one `POST /control/dns_config`.** The
endpoint sets only the fields present, validates the request whole before it
applies any of it, and **restarts the instance's DNS server** for most of them,
so one request is one restart and no half-applied set. Protection goes through
`POST /control/protection`, the one endpoint that also ends a pause the UI set:
`dns_config`'s `protection_enabled` leaves a pause standing, and the instance
reports protection off until it expires, which reads here as drift.

**`access` is the three access lists**, which `POST /control/access/set`
replaces together; an empty one reads back as `null`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, cast, final, get_args

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import Api
from kluster.providers.adguard.base import ENDPOINT, INSTANCE, SETUP_ENDPOINT, AdGuardProvider, missing_or_extra

__all__ = (
    'ACCESS',
    'DNS',
    'AccessLists',
    'AdGuardDnsServer',
    'AdGuardDnsServerProvider',
    'DnsSettings',
)

#: The DNS server's settings, protection among them.
DNS = 'dns'

#: The access lists.
ACCESS = 'access'

#: The modes `dns_config` accepts for `upstream_mode`, as this provider writes
#: them; the instance also accepts `""`, which it reads as `load_balance`.
UpstreamMode = Literal['load_balance', 'parallel', 'fastest_addr']

#: The modes `dns_config` accepts for `blocking_mode`.
BlockingMode = Literal['default', 'refused', 'nxdomain', 'null_ip', 'custom_ip']


class DnsSettings(TypedDict):
    """The fields of `GET /control/dns_info` an instance is configured with, in its own names."""

    upstream_dns: list[str]
    upstream_dns_file: str
    bootstrap_dns: list[str]
    fallback_dns: list[str]
    upstream_mode: UpstreamMode
    #: In whole seconds.
    upstream_timeout: int
    local_ptr_upstreams: list[str]
    use_private_ptr_resolvers: bool
    #: Whether clients' addresses are resolved to names: the `rdns` runtime source.
    resolve_clients: bool
    protection_enabled: bool
    ratelimit: int
    ratelimit_subnet_len_ipv4: int
    ratelimit_subnet_len_ipv6: int
    ratelimit_whitelist: list[str]
    blocking_mode: BlockingMode
    blocking_ipv4: str
    blocking_ipv6: str
    blocked_response_ttl: int
    edns_cs_enabled: bool
    edns_cs_use_custom: bool
    edns_cs_custom_ip: str
    dnssec_enabled: bool
    disable_ipv6: bool
    cache_enabled: bool
    cache_size: int
    cache_ttl_min: int
    cache_ttl_max: int
    cache_optimistic: bool


class AccessLists(TypedDict):
    """`GET /control/access/list`, whole."""

    allowed_clients: list[str]
    disallowed_clients: list[str]
    blocked_hosts: list[str]


_DNS_KEYS = DnsSettings.__required_keys__
_ACCESS_KEYS = AccessLists.__required_keys__
_LISTS = frozenset({'upstream_dns', 'bootstrap_dns', 'fallback_dns', 'local_ptr_upstreams', 'ratelimit_whitelist'})
_UPSTREAM_MODES = frozenset(get_args(UpstreamMode))
_BLOCKING_MODES = frozenset(get_args(BlockingMode))

#: The field that goes through `/control/protection` instead of `dns_config`.
_PROTECTION = 'protection_enabled'


def _dns(value: Any) -> dict[str, Any]:
    held = cast('Mapping[str, Any]', value or {})
    comparable = {key: held.get(key) for key in _DNS_KEYS}
    for key in _LISTS:
        comparable[key] = list(comparable[key] or [])
    if comparable['upstream_mode'] == '':
        comparable['upstream_mode'] = 'load_balance'
    if comparable['blocking_mode'] != 'custom_ip':
        del comparable['blocking_ipv4'], comparable['blocking_ipv6']
    if not comparable['edns_cs_use_custom']:
        del comparable['edns_cs_custom_ip']
    return comparable


def _access(value: Any) -> dict[str, Any]:
    held = cast('Mapping[str, Any]', value or {})
    return {key: list(held.get(key) or []) for key in _ACCESS_KEYS}


def _is_ip(value: Any, version: int) -> bool:
    try:
        return ipaddress.ip_address(str(value)).version == version
    except ValueError:
        return False


@final
class AdGuardDnsServerProvider(AdGuardProvider):
    kind = 'dns-server'
    sections = (DNS, ACCESS)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        if missing_or_extra(DNS, news.get(DNS), _DNS_KEYS, failures):
            dns = cast('Mapping[str, Any]', news[DNS])
            if dns['upstream_mode'] not in _UPSTREAM_MODES:
                failures.append(
                    dynamic.CheckFailure(
                        DNS, f'upstream_mode {dns["upstream_mode"]!r} is none of {sorted(_UPSTREAM_MODES)}'
                    )
                )
            if dns['blocking_mode'] not in _BLOCKING_MODES:
                failures.append(
                    dynamic.CheckFailure(
                        DNS, f'blocking_mode {dns["blocking_mode"]!r} is none of {sorted(_BLOCKING_MODES)}'
                    )
                )
            if dns['blocking_mode'] == 'custom_ip' and not (
                _is_ip(dns['blocking_ipv4'], 4) and _is_ip(dns['blocking_ipv6'], 6)
            ):
                failures.append(
                    dynamic.CheckFailure(
                        DNS, 'blocking_mode custom_ip takes an IPv4 blocking_ipv4 and an IPv6 blocking_ipv6'
                    )
                )
            if dns['upstream_timeout'] < 1:
                failures.append(dynamic.CheckFailure(DNS, 'upstream_timeout is under one second'))
        missing_or_extra(ACCESS, news.get(ACCESS), _ACCESS_KEYS, failures)

    def _comparable(self, section: str, value: Any) -> object:
        return _dns(value) if section == DNS else _access(value)

    def _read(self, api: Api) -> dict[str, Any]:
        info = cast('Mapping[str, Any]', api.get('dns_info'))
        dns = _dns(info) | {key: info.get(key) for key in ('blocking_ipv4', 'blocking_ipv6', 'edns_cs_custom_ip')}
        return {DNS: dns, ACCESS: _access(api.get('access/list'))}

    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        declared = _dns(news[DNS])
        held = None if olds is None else _dns(olds.get(DNS))
        settings = {key: value for key, value in declared.items() if key != _PROTECTION}
        if held is None or settings != {key: value for key, value in held.items() if key != _PROTECTION}:
            dns = cast('Mapping[str, Any]', news[DNS])
            api.post('dns_config', {key: dns[key] for key in sorted(_DNS_KEYS - {_PROTECTION})})
        if held is None or declared[_PROTECTION] != held[_PROTECTION]:
            api.post('protection', {'enabled': declared[_PROTECTION], 'duration': 0})
        if olds is None or _access(olds.get(ACCESS)) != _access(news[ACCESS]):
            api.post('access/set', _access(news[ACCESS]))


@final
class AdGuardDnsServer(dynamic.Resource, module='adguard', name='AdGuardDnsServer'):
    """One instance's DNS server settings, protection and access lists."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    dns: pulumi.Output[dict[str, Any]]
    access: pulumi.Output[dict[str, Any]]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        setup_endpoint: pulumi.Input[str],
        dns: pulumi.Input[DnsSettings],
        access: pulumi.Input[AccessLists],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare `instance`'s DNS server settings and access lists, each whole."""
        super().__init__(
            AdGuardDnsServerProvider(),
            name,
            {INSTANCE: instance, ENDPOINT: endpoint, SETUP_ENDPOINT: setup_endpoint, DNS: dns, ACCESS: access},
            opts,
        )
