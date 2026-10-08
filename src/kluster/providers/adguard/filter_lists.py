"""`AdGuardFilterLists`: the block and allow lists an instance subscribes to.

**The two kinds are one set, keyed by URL.** `filters` are block lists and
`whitelist_filters` allow lists, each `{url, name, enabled}` as `GET
/control/filtering/status` reports them; the instance keeps a URL unique across
both, so the set is compared by URL, and order is not compared, since matching
does not depend on it. The instance's own `id`, `rules_count` and
`last_updated` are not read.

**Each endpoint addresses one list, so a write reconciles against a fresh
read**, never against the stored outputs:

1.  `POST /control/filtering/add_url` for a declared URL the instance lacks.
    The instance downloads the list before it stores it and refuses one it
    cannot fetch or that is empty, so the request is given a download's
    timeout. It stores every list enabled, so a list declared disabled is
    added, then disabled.
2.  `POST /control/filtering/set_url` for a present list whose name or switch
    differs: it sets name, URL and switch together, and downloads a list it
    enables, so it too is given a download's timeout.
3.  A list that changes kind is removed from the old one and added to the
    new, the one place a remove comes first, since the URL may be in only one.
4.  `POST /control/filtering/remove_url` for a URL nothing declares, last, so
    replacing one list with another leaves no moment with neither.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict, cast, final
from urllib.parse import urlsplit

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import DOWNLOAD_TIMEOUT, Api
from kluster.providers.adguard.base import ENDPOINT, INSTANCE, SETUP_ENDPOINT, AdGuardProvider, missing_or_extra

__all__ = ('ALLOW', 'BLOCK', 'AdGuardFilterLists', 'AdGuardFilterListsProvider', 'FilterList')

#: The block lists.
BLOCK = 'filters'

#: The allow lists.
ALLOW = 'whitelist_filters'


class FilterList(TypedDict):
    url: str
    name: str
    enabled: bool


_KEYS = FilterList.__required_keys__


def _lists(value: Any) -> dict[str, dict[str, Any]]:
    """A kind's lists keyed by URL, each in the declared shape."""
    return {
        str(held['url']): {key: held.get(key) for key in _KEYS}
        for held in (cast('Mapping[str, Any]', each) for each in cast('Sequence[Any]', value or []))
    }


def _is_http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ('http', 'https') and bool(parts.netloc)


@final
class AdGuardFilterListsProvider(AdGuardProvider):
    kind = 'filter-lists'
    sections = (BLOCK, ALLOW)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        seen: set[str] = set()
        for section in self.sections:
            declared = news.get(section)
            if not isinstance(declared, Sequence) or isinstance(declared, str):
                failures.append(dynamic.CheckFailure(section, f'{section} is not a list'))
                continue
            for each in cast('Sequence[Any]', declared):
                if not missing_or_extra(section, each, _KEYS, failures, label=f'a list in {section}'):
                    continue
                url = str(cast('Mapping[str, Any]', each)['url'])
                if not _is_http(url):
                    failures.append(dynamic.CheckFailure(section, f'{url!r} is not an HTTP(S) URL'))
                if url in seen:
                    # The instance keeps a URL unique across both kinds.
                    failures.append(dynamic.CheckFailure(section, f'{url} is declared twice'))
                seen.add(url)

    def _comparable(self, section: str, value: Any) -> object:
        return _lists(value)

    def _read(self, api: Api) -> dict[str, Any]:
        status = cast('Mapping[str, Any]', api.get('filtering/status'))
        return {section: list(_lists(status.get(section)).values()) for section in self.sections}

    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        held = self._read(api)
        live = {url: (section, each) for section in self.sections for url, each in _lists(held[section]).items()}
        declared = {url: (section, each) for section in self.sections for url, each in _lists(news[section]).items()}
        for url, (section, each) in declared.items():
            if url not in live:
                self._add(api, section, each)
        for url, (section, each) in declared.items():
            if url in live and live[url][0] == section and live[url][1] != each:
                api.post(
                    'filtering/set_url',
                    {'url': url, 'whitelist': section == ALLOW, 'data': each},
                    timeout=DOWNLOAD_TIMEOUT,
                )
        for url, (section, each) in declared.items():
            if url in live and live[url][0] != section:
                api.post('filtering/remove_url', {'url': url, 'whitelist': live[url][0] == ALLOW})
                self._add(api, section, each)
        for url, (section, _each) in live.items():
            if url not in declared:
                api.post('filtering/remove_url', {'url': url, 'whitelist': section == ALLOW})

    def _add(self, api: Api, section: str, each: Mapping[str, Any]) -> None:
        whitelist = section == ALLOW
        api.post(
            'filtering/add_url',
            {'name': each['name'], 'url': each['url'], 'whitelist': whitelist},
            timeout=DOWNLOAD_TIMEOUT,
        )
        if not each['enabled']:
            api.post('filtering/set_url', {'url': each['url'], 'whitelist': whitelist, 'data': dict(each)})


@final
class AdGuardFilterLists(dynamic.Resource, module='adguard', name='AdGuardFilterLists'):
    """One instance's block and allow lists, as a set."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    filters: pulumi.Output[list[dict[str, Any]]]
    whitelist_filters: pulumi.Output[list[dict[str, Any]]]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        setup_endpoint: pulumi.Input[str],
        filters: pulumi.Input[Sequence[FilterList]],
        whitelist_filters: pulumi.Input[Sequence[FilterList]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `instance` subscribes to exactly these block and allow lists."""
        super().__init__(
            AdGuardFilterListsProvider(),
            name,
            {
                INSTANCE: instance,
                ENDPOINT: endpoint,
                SETUP_ENDPOINT: setup_endpoint,
                BLOCK: filters,
                ALLOW: whitelist_filters,
            },
            opts,
        )
