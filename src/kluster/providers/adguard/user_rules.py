"""`AdGuardUserRules`: an instance's custom filtering rules, the whole list.

**The list is written whole and compared whole.** `POST
/control/filtering/set_rules` replaces it in one request, with no
compare-and-set, so the resource owns every line: compared element by element
and in order, comment lines included, so a line edited, reordered or added in
the UI is drift. An empty list reads back as `null`. The body is JSON: the
instance answers a `text/plain` one with 415.

**The rewrite list is guarded, not written.** Once the list is switched on
(`filtering.rewrites_enabled`), a row there outranks every user rule and decides
its name's answer whatever the rules say; while it is off, a row waits for the
switch. Either way nothing declares a row. `read` reports its rows as
`rewrites`, which the declaration holds empty, so a refresh shows a hand-added
row as drift; and a write refuses, naming the rows, before it sends anything.

**The switch is declared off**, as `rewrites_enabled`, by every resource of the
kind rather than by its caller: an instance's first-run setup leaves it on, and
the guard above is what the declaration means. It is read from `GET
/control/rewrite/settings` and written by `PUT /control/rewrite/settings/update`
with an explicit body, so a switch turned on by hand is drift and the next
write turns it off.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast, final

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import Api
from kluster.providers.adguard.base import ENDPOINT, INSTANCE, SETUP_ENDPOINT, AdGuardProvider

__all__ = (
    'REWRITES',
    'REWRITES_ENABLED',
    'RULES',
    'AdGuardUserRules',
    'AdGuardUserRulesProvider',
    'RewriteListNotEmpty',
)

#: The declared list.
RULES = 'rules'

#: The rewrite list's rows, as `read` reports them. Never declared: the
#: declaration is that there are none.
REWRITES = 'rewrites'

#: The rewrite list's switch, which every resource of the kind declares off.
REWRITES_ENABLED = 'rewrites_enabled'


class RewriteListNotEmpty(RuntimeError):
    """The instance's rewrite list holds a row, which outranks the rules once the list is switched on."""


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(cast('Mapping[str, Any]', row)) for row in cast('Sequence[Any]', value or [])]


@final
class AdGuardUserRulesProvider(AdGuardProvider):
    kind = 'user-rules'
    sections = (RULES, REWRITES, REWRITES_ENABLED)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        rules = news.get(RULES)
        if not isinstance(rules, Sequence) or isinstance(rules, str):
            failures.append(dynamic.CheckFailure(RULES, 'rules is not a list'))
            return
        for index, rule in enumerate(cast('Sequence[Any]', rules)):
            if not isinstance(rule, str):
                failures.append(dynamic.CheckFailure(RULES, f'rule {index} is not a string'))
            elif '\n' in rule or '\r' in rule:
                # The instance stores the element, and its filter engine reads
                # the list as lines: one element would answer as two rules and
                # read back as one.
                failures.append(dynamic.CheckFailure(RULES, f'rule {index} holds a line break: {rule!r}'))

    def _comparable(self, section: str, value: Any) -> object:
        if section == REWRITES:
            return _rows(value)
        if section == REWRITES_ENABLED:
            return bool(value)
        return list(cast('Sequence[Any]', value or []))

    def _read(self, api: Api) -> dict[str, Any]:
        status = cast('Mapping[str, Any]', api.get('filtering/status'))
        switch = cast('Mapping[str, Any]', api.get('rewrite/settings'))
        return {
            RULES: list(status.get('user_rules') or []),
            REWRITES: _rows(api.get('rewrite/list')),
            REWRITES_ENABLED: bool(switch.get('enabled')),
        }

    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        if rows := _rows(api.get('rewrite/list')):
            listed = ', '.join(f'{row.get("domain")} -> {row.get("answer")}' for row in rows)
            raise RewriteListNotEmpty(
                f'{news[INSTANCE]}: the rewrite list holds {listed}. It outranks every user rule once the rewrite '
                'list is switched on, and nothing declares it: remove the rows in the instance\'s "DNS rewrites" '
                'page, then run again.'
            )
        changed = self._changed(olds, news)
        if RULES in changed:
            api.post('filtering/set_rules', {'rules': list(news[RULES])})
        if REWRITES_ENABLED in changed:
            api.put('rewrite/settings/update', {'enabled': bool(news[REWRITES_ENABLED])})


@final
class AdGuardUserRules(dynamic.Resource, module='adguard', name='AdGuardUserRules'):
    """The whole custom-rule list of one instance."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    rules: pulumi.Output[list[str]]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        setup_endpoint: pulumi.Input[str],
        rules: pulumi.Input[Sequence[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `instance`, reached at `endpoint`, holds exactly `rules`, in order, and its rewrite list off.

        `instance` is which resolver this is and never moves; `endpoint` and
        `setup_endpoint` are where this run finds its API and its setup, and
        may. The login that writes the list is not a property: it is read in
        `configure`.
        """
        super().__init__(
            AdGuardUserRulesProvider(),
            name,
            {
                INSTANCE: instance,
                ENDPOINT: endpoint,
                SETUP_ENDPOINT: setup_endpoint,
                RULES: rules,
                REWRITES_ENABLED: False,
            },
            opts,
        )
