"""`AdGuardFiltering`: the filtering switches, safe search and the blocked services.

Each section is its endpoint's object, compared whole and written whole, one
request per section that differs:

-   `filtering`, `{enabled, interval}`, read from `GET /control/filtering/status`
    and written together by `POST /control/filtering/config`, which writes an
    absent interval as 0. The interval is in hours, at most a year.
-   `safebrowsing` and `parental`, `{enabled}`, through each one's `status`,
    `enable` and `disable`.
-   `safe_search`, through `GET /control/safesearch/status` and `PUT
    /control/safesearch/settings`.
-   `blocked_services`, `{ids, schedule}`, through `GET
    /control/blocked_services/get` and `PUT /control/blocked_services/update`,
    the schedule included.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast, final

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import Api
from kluster.providers.adguard.base import ENDPOINT, INSTANCE, SETUP_ENDPOINT, AdGuardProvider, missing_or_extra

__all__ = (
    'BLOCKED_SERVICES',
    'FILTERING',
    'PARENTAL',
    'SAFEBROWSING',
    'SAFE_SEARCH',
    'AdGuardFiltering',
    'AdGuardFilteringProvider',
    'BlockedServices',
    'FilteringSwitch',
    'SafeSearch',
    'Switch',
)

FILTERING = 'filtering'
SAFEBROWSING = 'safebrowsing'
PARENTAL = 'parental'
SAFE_SEARCH = 'safe_search'
BLOCKED_SERVICES = 'blocked_services'

#: The longest filter-list update interval the instance accepts, in hours.
MAX_INTERVAL = 365 * 24


class FilteringSwitch(TypedDict):
    enabled: bool
    #: Hours between filter-list updates; 0 turns updates off.
    interval: int


class Switch(TypedDict):
    enabled: bool


class SafeSearch(TypedDict):
    """The safe-search object, globally and on a persistent client alike."""

    enabled: bool
    bing: bool
    duckduckgo: bool
    ecosia: bool
    google: bool
    pixabay: bool
    yandex: bool
    youtube: bool


class BlockedServices(TypedDict):
    ids: list[str]
    #: The instance's weekly schedule object: `time_zone`, and a `{start, end}`
    #: in milliseconds under each day it blocks on.
    schedule: dict[str, Any]


_KEYS: Mapping[str, frozenset[str]] = {
    FILTERING: FilteringSwitch.__required_keys__,
    SAFEBROWSING: Switch.__required_keys__,
    PARENTAL: Switch.__required_keys__,
    SAFE_SEARCH: SafeSearch.__required_keys__,
    BLOCKED_SERVICES: BlockedServices.__required_keys__,
}


@final
class AdGuardFilteringProvider(AdGuardProvider):
    kind = 'filtering'
    sections = (FILTERING, SAFEBROWSING, PARENTAL, SAFE_SEARCH, BLOCKED_SERVICES)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        for section in self.sections:
            missing_or_extra(section, news.get(section), _KEYS[section], failures)
        switch = news.get(FILTERING)
        interval = cast('Mapping[str, Any]', switch).get('interval') if isinstance(switch, Mapping) else None
        if isinstance(interval, int | float) and not 0 <= interval <= MAX_INTERVAL:
            failures.append(dynamic.CheckFailure(FILTERING, f'interval is outside 0 to {MAX_INTERVAL} hours'))

    def _comparable(self, section: str, value: Any) -> object:
        held = cast('Mapping[str, Any]', value or {})
        comparable = {key: held.get(key) for key in _KEYS[section]}
        if section == BLOCKED_SERVICES:
            comparable['ids'] = list(comparable['ids'] or [])
        return comparable

    def _read(self, api: Api) -> dict[str, Any]:
        status = cast('Mapping[str, Any]', api.get('filtering/status'))
        held = {
            FILTERING: status,
            SAFEBROWSING: api.get('safebrowsing/status'),
            PARENTAL: api.get('parental/status'),
            SAFE_SEARCH: api.get('safesearch/status'),
            BLOCKED_SERVICES: api.get('blocked_services/get'),
        }
        return {section: self._comparable(section, value) for section, value in held.items()}

    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        for section in self._changed(olds, news):
            declared = cast('dict[str, Any]', self._comparable(section, news[section]))
            if section == FILTERING:
                api.post('filtering/config', declared)
            elif section in (SAFEBROWSING, PARENTAL):
                api.post(f'{section}/{"enable" if declared["enabled"] else "disable"}', {})
            elif section == SAFE_SEARCH:
                api.put('safesearch/settings', declared)
            else:
                api.put('blocked_services/update', declared)


@final
class AdGuardFiltering(dynamic.Resource, module='adguard', name='AdGuardFiltering'):
    """One instance's filtering switches, safe search and blocked services."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    filtering: pulumi.Output[dict[str, Any]]
    safebrowsing: pulumi.Output[dict[str, Any]]
    parental: pulumi.Output[dict[str, Any]]
    safe_search: pulumi.Output[dict[str, Any]]
    blocked_services: pulumi.Output[dict[str, Any]]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        setup_endpoint: pulumi.Input[str],
        filtering: pulumi.Input[FilteringSwitch],
        safebrowsing: pulumi.Input[Switch],
        parental: pulumi.Input[Switch],
        safe_search: pulumi.Input[SafeSearch],
        blocked_services: pulumi.Input[BlockedServices],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare `instance`'s filtering switches, safe search and blocked services, each whole."""
        super().__init__(
            AdGuardFilteringProvider(),
            name,
            {
                INSTANCE: instance,
                ENDPOINT: endpoint,
                SETUP_ENDPOINT: setup_endpoint,
                FILTERING: filtering,
                SAFEBROWSING: safebrowsing,
                PARENTAL: parental,
                SAFE_SEARCH: safe_search,
                BLOCKED_SERVICES: blocked_services,
            },
            opts,
        )
