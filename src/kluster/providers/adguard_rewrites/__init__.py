"""Split-horizon rewrites on an AdGuard Home instance (dns.md §3).

AdGuard Home has no Terraform or Pulumi provider, but it has a small
idempotent REST API, so a rewrite is a dynamic-provider resource: create is
`rewrite/add`, delete is `rewrite/delete`, and read is a lookup in
`rewrite/list`. There is no update -- the API has none, and a rewrite is a
pair of strings -- so any change replaces.

A resource names the one instance it is written to, so an instance that is
down fails its own resources and leaves the other instance's converged.

The credential is AdGuard's admin login: it has no scoped API tokens, which
is the residual the security audit records as L11. It is an input on every
rewrite; rfc-002 §7.4 states the shape that moves it into the provider's own
process instead.

Which names are rewritten, and on which instances, is
`kluster.components.dns.adguard`'s business, not this package's.
"""

from __future__ import annotations

from typing import Any, cast

import pulumi
import pulumi.dynamic as dynamic
import requests

__all__ = ('TIMEOUT', 'AdGuardRewrite', 'AdGuardRewriteProvider')

#: Long enough for a busy resolver, short enough that an unreachable UDM
#: fails the resource instead of hanging the stack.
TIMEOUT = 15


def _session(props: dict[str, Any]) -> tuple[str, requests.Session]:
    session = requests.Session()
    session.auth = (str(props['username']), str(props['password']))
    return str(props['endpoint']).rstrip('/'), session


def _entry(props: dict[str, Any]) -> dict[str, str]:
    return {'domain': str(props['domain']), 'answer': str(props['answer'])}


class AdGuardRewriteProvider(dynamic.ResourceProvider):
    """CRUD against one instance's `/control/rewrite/*` endpoints."""

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        base, session = _session(props)
        entry = _entry(props)
        # The API happily stores a duplicate pair, and duplicates are then
        # indistinguishable at delete time; adopting an existing identical
        # entry keeps create idempotent across a partially failed up.
        if entry not in self._list(base, session):
            response = session.post(f'{base}/control/rewrite/add', json=entry, timeout=TIMEOUT)
            response.raise_for_status()
        return dynamic.CreateResult(id_=f'{base}|{entry["domain"]}|{entry["answer"]}', outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        base, session = _session(props)
        if _entry(props) not in self._list(base, session):
            # An absent entry is a deleted resource, which is how a rewrite
            # someone removed in the UI comes back on the next up. The outs
            # must be an empty dict, not None: the provider host writes its
            # own key into whatever this returns, and it mutates the dict,
            # so a shared constant would not do either.
            return dynamic.ReadResult(id_=None, outs={})
        return dynamic.ReadResult(id_=id_, outs=props)

    def diff(self, _id: str, _olds: dict[str, Any], _news: dict[str, Any]) -> dynamic.DiffResult:
        changed = [key for key in ('endpoint', 'domain', 'answer') if _olds.get(key) != _news.get(key)]
        return dynamic.DiffResult(
            changes=bool(changed) or _olds.get('password') != _news.get('password'),
            replaces=changed,
            # A rewrite is a row in a list: two of them can coexist for the
            # instant between create and delete, and the alternative -- a
            # window with no answer at all -- is a LAN outage for the name.
            delete_before_replace=False,
        )

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        base, session = _session(_props)
        response = session.post(f'{base}/control/rewrite/delete', json=_entry(_props), timeout=TIMEOUT)
        response.raise_for_status()

    def _list(self, base: str, session: requests.Session) -> list[dict[str, str]]:
        response = session.get(f'{base}/control/rewrite/list', timeout=TIMEOUT)
        response.raise_for_status()
        listed = cast('list[dict[str, Any]]', response.json())
        return [{'domain': str(item.get('domain')), 'answer': str(item.get('answer'))} for item in listed]


class AdGuardRewrite(dynamic.Resource):
    """One rewrite on one instance."""

    endpoint: pulumi.Output[str]
    domain: pulumi.Output[str]
    answer: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        endpoint: pulumi.Input[str],
        username: pulumi.Input[str],
        password: pulumi.Input[str],
        domain: pulumi.Input[str],
        answer: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            AdGuardRewriteProvider(),
            name,
            {
                'endpoint': endpoint,
                'username': username,
                'password': password,
                'domain': domain,
                'answer': answer,
            },
            pulumi.ResourceOptions.merge(
                # The provider echoes its inputs back as outputs, so the
                # credential would otherwise land in state in the clear.
                pulumi.ResourceOptions(additional_secret_outputs=['password']),
                opts,
            ),
        )
