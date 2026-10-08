"""`AdGuardLogSettings`: the query log's and the statistics' settings, not their data.

Each is its endpoint's object, read from `GET /control/querylog/config` and
`GET /control/stats/config`, compared whole and written whole by `PUT
/control/querylog/config/update` and `PUT /control/stats/config/update`. The
intervals are in milliseconds, from an hour to a year; the query log's
`enabled` and `anonymize_client_ip` may not be null. The data each one keeps is what the instance measured, and
running the instance regenerates it, so nothing here clears or reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast, final

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import Api
from kluster.providers.adguard.base import ENDPOINT, INSTANCE, AdGuardProvider, missing_or_extra

__all__ = ('QUERYLOG', 'STATS', 'AdGuardLogSettings', 'AdGuardLogSettingsProvider', 'QueryLogConfig', 'StatsConfig')

QUERYLOG = 'querylog'
STATS = 'stats'

#: The shortest and the longest interval either endpoint accepts, in milliseconds.
MIN_INTERVAL = 60 * 60 * 1000
MAX_INTERVAL = 365 * 24 * MIN_INTERVAL


class QueryLogConfig(TypedDict):
    enabled: bool
    #: How long an entry is kept, in milliseconds.
    interval: int
    anonymize_client_ip: bool
    ignored: list[str]
    ignored_enabled: bool


class StatsConfig(TypedDict):
    enabled: bool
    #: How long a measurement is kept, in milliseconds.
    interval: int
    ignored: list[str]
    ignored_enabled: bool


_KEYS: Mapping[str, frozenset[str]] = {QUERYLOG: QueryLogConfig.__required_keys__, STATS: StatsConfig.__required_keys__}

_PATHS = {QUERYLOG: 'querylog/config', STATS: 'stats/config'}


@final
class AdGuardLogSettingsProvider(AdGuardProvider):
    kind = 'log-settings'
    sections = (QUERYLOG, STATS)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        for section in self.sections:
            if not missing_or_extra(section, news.get(section), _KEYS[section], failures):
                continue
            interval = cast('Mapping[str, Any]', news[section])['interval']
            if not isinstance(interval, int | float) or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
                failures.append(dynamic.CheckFailure(section, 'interval is outside an hour to a year, in milliseconds'))

    def _comparable(self, section: str, value: Any) -> object:
        held = cast('Mapping[str, Any]', value or {})
        comparable = {key: held.get(key) for key in _KEYS[section]}
        comparable['ignored'] = list(comparable['ignored'] or [])
        return comparable

    def _read(self, api: Api) -> dict[str, Any]:
        return {section: self._comparable(section, api.get(path)) for section, path in _PATHS.items()}

    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        for section in self._changed(olds, news):
            api.put(f'{_PATHS[section]}/update', self._comparable(section, news[section]))


@final
class AdGuardLogSettings(dynamic.Resource, module='adguard', name='AdGuardLogSettings'):
    """One instance's query-log and statistics settings."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    querylog: pulumi.Output[QueryLogConfig]
    stats: pulumi.Output[StatsConfig]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        querylog: pulumi.Input[QueryLogConfig],
        stats: pulumi.Input[StatsConfig],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare `instance`'s query-log and statistics settings, each whole."""
        super().__init__(
            AdGuardLogSettingsProvider(),
            name,
            {INSTANCE: instance, ENDPOINT: endpoint, QUERYLOG: querylog, STATS: stats},
            opts,
        )
