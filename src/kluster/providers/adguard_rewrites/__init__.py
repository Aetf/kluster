"""Split-horizon rewrites on an AdGuard Home instance (dns.md §3).

AdGuard Home has no Terraform or Pulumi provider, but it has a small
idempotent REST API, so a rewrite is a dynamic-provider resource: create is
`rewrite/add`, delete is `rewrite/delete`, and read is a lookup in
`rewrite/list`. There is no update -- the API has none, and a rewrite is a
pair of strings -- so any change to what a rewrite says replaces it.

A resource names the one instance it is written to, so an instance that is
down fails its own resources and leaves the other instance's converged.

**The credential is an admin login, because that is the whole of what the
appliance offers.** AdGuard Home has no scoped API tokens: the account that
signs into the web interface is the account a rewrite call authenticates as.
It is read in `configure`, out of stack configuration, inside the plugin's
process -- no caller declares it, no component passes it, and nothing pickles
it. What that costs and how it is contained is the security audit's M6.

So that a reader still sees which login wrote a row, `check` stamps every
rewrite with `session` -- the instance and a short digest of the login -- and
`provider_version`. A rotation and a change to this module's behavior each
render as a property diff no caller declared; the machinery is
`kluster.providers.configured`, and what is this module's own is which keys
hold the login and what an endpoint is.

**The only update is a re-stamp.** Every property a caller declares is a
replacement, so an update is reached exactly when a stamp moved and nothing
else did. There is nothing to write for that -- the row the instance holds is
the row the resource declares -- so the update records the new stamps and
calls no endpoint at all.

Which names are rewritten, and on which instances, is
`kluster.components.dns.rewrites`'s business, not this package's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast, final

import pulumi
import pulumi.dynamic as dynamic
import requests

from kluster.providers.configured import STAMPS, ConfiguredProvider, declared_change, has_unknowns

__all__ = (
    'COMPARED',
    'DECLARED',
    'PASSWORD_CONFIG',
    'TIMEOUT',
    'USERNAME_CONFIG',
    'VERSION',
    'AdGuardRewrite',
    'AdGuardRewriteProvider',
)

#: Long enough for a busy resolver, short enough that an unreachable UDM
#: fails the resource instead of hanging the stack.
TIMEOUT = 15

#: The stack-configuration keys the admin login is read from (`configured`).
#: Both halves, because an admin login is a pair.
USERNAME_CONFIG = 'adguardUsername'
PASSWORD_CONFIG = 'adguardPassword'

#: What a caller declares: which instance, and the pair of strings the rewrite
#: is. Every one of them is a replacement, the API having no update.
DECLARED = ('endpoint', 'domain', 'answer')

#: What `diff` compares, and the whole of it -- the declared properties plus
#: the two `check` stamps.
COMPARED = (*DECLARED, *STAMPS)

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '1'


def _base(props: Mapping[str, Any]) -> str:
    """The instance's administration API, without the trailing slash a caller may write."""
    return str(props['endpoint']).rstrip('/')


def _entry(props: Mapping[str, Any]) -> dict[str, str]:
    """The rewrite itself, in the shape every one of the three endpoints takes."""
    return {'domain': str(props['domain']), 'answer': str(props['answer'])}


@final
class AdGuardRewriteProvider(ConfiguredProvider):
    """CRUD against one instance's `/control/rewrite/*` endpoints."""

    username: str
    password: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.username = str(config.require(USERNAME_CONFIG))
        self.password = str(config.require(PASSWORD_CONFIG))

    def _credential(self) -> str:
        """Both halves: moving to another admin account is as much a rotation as a new password."""
        return f'{self.username}:{self.password}'

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return _base(props)

    def _version(self) -> str:
        return VERSION

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        base, session = self._api(props)
        entry = _entry(props)
        # The API happily stores a duplicate pair, and duplicates are then
        # indistinguishable at delete time; adopting an existing identical
        # entry keeps create idempotent across a partially failed up.
        if entry not in self._list(base, session):
            response = session.post(f'{base}/control/rewrite/add', json=entry, timeout=TIMEOUT)
            response.raise_for_status()
        # The checked inputs go back out as the outputs, stamps included, so
        # the stored bag records the login that wrote this row.
        return dynamic.CreateResult(id_=f'{base}|{entry["domain"]}|{entry["answer"]}', outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        base, session = self._api(props)
        if _entry(props) not in self._list(base, session):
            # An absent entry is a deleted resource, which is how a rewrite
            # someone removed in the UI comes back on the next up. The outs
            # must be an empty dict, not None: the provider host writes its
            # own key into whatever this returns, and it mutates the dict,
            # so a shared constant would not do either.
            return dynamic.ReadResult(id_=None, outs={})
        return dynamic.ReadResult(id_=id_, outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if has_unknowns(news):
            # Answered before anything is compared, because every declared
            # property here is a replacement: a value nobody knows yet, read as
            # a difference, would plan a delete and a create of a row that is
            # about to be identical.
            return dynamic.DiffResult(changes=None)
        replaces = [key for key in DECLARED if olds.get(key) != news.get(key)]
        return dynamic.DiffResult(
            changes=declared_change(olds, news, COMPARED),
            replaces=replaces,
            # A rewrite is a row in a list: two of them can coexist for the
            # instant between create and delete, and the alternative -- a
            # window with no answer at all -- is a LAN outage for the name.
            delete_before_replace=False,
        )

    def update(self, _id: str, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        """A moved stamp and nothing else, so the instance is not called at all.

        The outs replace the stored output bag (rfc-002 §7.5 E9), so what state
        says about the login that last wrote this row stays true.
        """
        return dynamic.UpdateResult(outs=news)

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        base, session = self._api(props)
        response = session.post(f'{base}/control/rewrite/delete', json=_entry(props), timeout=TIMEOUT)
        response.raise_for_status()

    def _api(self, props: Mapping[str, Any]) -> tuple[str, requests.Session]:
        """The instance a property bag names, opened with the configured login."""
        session = requests.Session()
        session.auth = (self.username, self.password)
        return _base(props), session

    def _list(self, base: str, session: requests.Session) -> list[dict[str, str]]:
        response = session.get(f'{base}/control/rewrite/list', timeout=TIMEOUT)
        response.raise_for_status()
        listed = cast('list[dict[str, Any]]', response.json())
        return [{'domain': str(item.get('domain')), 'answer': str(item.get('answer'))} for item in listed]


@final
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
        domain: pulumi.Input[str],
        answer: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that the instance at `endpoint` answers `domain` with `answer`.

        No property here is a secret: which instance, which name and which
        address are all things a reviewer should read in a preview. The login
        that writes the row is not a property at all.
        """
        super().__init__(
            AdGuardRewriteProvider(),
            name,
            {
                'endpoint': endpoint,
                'domain': domain,
                'answer': answer,
            },
            opts,
        )
