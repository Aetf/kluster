"""The rank of a zone pair's policies on the UniFi controller (physical/gateway.md §4.2).

The controller ranks a pair's policies through one request, `PUT
…/firewall-policies/batch-reorder`, and it refuses a batch that leaves out any
custom policy of the pair: `400 … Should include firewall policy in batch
update`. A predefined policy is left out and stays where it is. The bridged
`FirewallZonePolicyOrder` builds that batch from its declared list alone, so it
fails wherever the pair holds one more custom policy than it lists, and every
edit of a list is such a case. Pulumi creates a replacement before it deletes
the original, and it deletes a dropped policy after the updates that stopped
naming it, so during an update a policy the previous list named is still on the
pair and unlisted in the new one.

**This package is one resource: the pair, the policies it ranks in order, and
the batch sent whole.** The list is the pair's census, and the pair belongs to
the census whole:

-   **A custom policy the list does not name, and the previous list did not,
    is foreign.** `create` and `update` refuse it by name, and it stays
    unadopted, unmoved and undeleted: it becomes a row of the census, or it is
    deleted in the console. `read` reports it, so a refreshed preview proposes
    an update, and that update refuses.
-   **A policy the previous list named is the census's own in transit.** The
    batch carries it behind the list. That is the one thing the old inputs are
    for, so `read` keeps the declared list as it stands and reports what it
    found in `drift` beside it: a list rewritten from the controller would
    turn a foreign policy into one the census used to own.
-   **The derivative of a census allow is a predefined policy like any other.**
    `auto_allow_return_traffic` has the controller add `<name> (Return)` on the
    reverse pair. Read off the census's pairs on 2026-11-05 it is `predefined:
    true`, at index 30000, where the pair's predefined policies start, so it
    needs no place in a batch and the list ranks ahead of it. A controller that
    presented one as custom would have it refused by name.

`create` and `update` read the pair again after the write, and fail unless the
list is the pair's first custom policies, in order, ahead of the predefined
ones. A refused batch is retried once from a fresh read, a console edit between
the read and the write being its one benign cause. `delete` sends nothing, the
ranking being no object of its own.

**The credential is the controller's API key**, read in `configure`, under the
key `API_KEY_CONFIG` names. Where the session goes and whether it verifies the
controller's certificate are declared inputs, since a provider decides neither
(framework/pulumi.md §5.2). `check` stamps `session` and `provider_version`
through `kluster.providers.configured`.

**The only write an update skips is a stamp's.** A moved stamp, or a moved
address, changes nothing about the rank, so an update reached for one records
the stamps and calls no endpoint.

**This package is a layer that comes out whole**, with the one construction
that builds its resource, `components/gateway/unifi.py`'s `policy_order`, when
two things hold. The pinned bridged order, on a pair holding a custom policy it
does not list, applies and leaves that policy behind the ones it lists, because
the provider fills in the batch or the controller accepts a partial one. And
something else refuses a run on a census pair holding a custom policy the
census does not own: such a bridged order moves past that policy, and its
refresh reads no policy it does not list. The tripwire is a scratch
program (declarative/physical.md §6), run when the `filipowm/unifi` pin or the
controller's Network release changes; while this layer is needed, it is refused
with the message above.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, final

import pulumi
import pulumi.dynamic as dynamic
import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from kluster.providers.configured import STAMPS, ConfiguredProvider, declared_change, has_unknowns, is_unknown

__all__ = (
    'API_KEY_CONFIG',
    'COMPARED',
    'DECLARED',
    'DRIFT',
    'IDENTITY',
    'TIMEOUT',
    'VERSION',
    'BatchRefused',
    'ForeignPolicy',
    'ListOutOfPlace',
    'ListedPolicyAbsent',
    'NoPredefinedPolicy',
    'PairRefused',
    'ZonePolicyOrder',
    'ZonePolicyOrderProvider',
)

#: Long enough for a controller under parallel load, short enough that an
#: unreachable gateway fails the resource instead of hanging the stack. A
#: transient answer fails the run rather than being retried, since a re-run is
#: safe and only a console edit earns a second try (`_rank`).
TIMEOUT = 15

#: The stack-configuration key the controller's API key is read from
#: (`configured`).
API_KEY_CONFIG = 'unifiApiKey'

#: The header the controller reads an API key from.
API_KEY_HEADER = 'X-API-KEY'

#: What identifies an order, and therefore what replaces one: a pair of zones
#: on a site. The rank of another pair is another object.
IDENTITY = ('site', 'source_zone_id', 'destination_zone_id')

#: What a caller declares: the identity, the list, and where and how this run
#: reaches the controller.
DECLARED = (*IDENTITY, 'policy_ids', 'api_url', 'allow_insecure')

#: What `diff` compares, and the whole of it -- the declared properties plus
#: the two `check` stamps.
COMPARED = (*DECLARED, *STAMPS)

#: The output `read` writes what it found into: one sentence per finding, empty
#: on a pair that reads as declared. A list and not a flag, because the
#: refreshed preview prints this property's change.
DRIFT = 'drift'

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '1'


class BatchRefused(RuntimeError):
    """The controller refused a batch, with its own reason."""


class PairRefused(RuntimeError):
    """The pair does not hold what the list needs it to, by name."""


class ForeignPolicy(PairRefused):
    """A custom policy neither the list nor the previous list names."""


class ListedPolicyAbsent(PairRefused):
    """A listed policy that is no custom policy of the pair."""


class NoPredefinedPolicy(PairRefused):
    """The pair holds nothing for the list to rank ahead of."""


class ListOutOfPlace(PairRefused):
    """The list is not the pair's first custom policies, in order, ahead of the predefined ones."""


@dataclass(frozen=True)
class Policy:
    """One policy of a pair, as far as a rank reads it."""

    id: str
    name: str
    index: int


@dataclass(frozen=True)
class Pair:
    """A zone pair as the controller holds it: its custom policies by rank, and where the predefined ones start."""

    source: str
    destination: str
    custom: tuple[Policy, ...]
    #: The lowest index among the predefined policies; `None` where the pair
    #: holds none.
    boundary: int | None

    def __str__(self) -> str:
        return f'{self.source} -> {self.destination}'

    @property
    def ahead(self) -> list[str]:
        """The ids of the custom policies ranked ahead of the predefined ones, in order."""
        boundary = self.boundary
        return [] if boundary is None else [policy.id for policy in self.custom if policy.index < boundary]


def _pair(listed: Sequence[Mapping[str, Any]], source: str, destination: str) -> Pair:
    """The pair out of the site's whole list of policies."""
    on_pair = [
        policy
        for policy in listed
        if policy.get('source', {}).get('zone_id') == source
        and policy.get('destination', {}).get('zone_id') == destination
    ]
    custom = [
        Policy(id=str(policy['_id']), name=str(policy['name']), index=int(policy['index']))
        for policy in on_pair
        if not policy['predefined']
    ]
    return Pair(
        source=source,
        destination=destination,
        custom=tuple(sorted(custom, key=lambda policy: policy.index)),
        boundary=min((int(policy['index']) for policy in on_pair if policy['predefined']), default=None),
    )


def _carried(pair: Pair, listed: Sequence[str], previous: Sequence[str]) -> list[str]:
    """The policies the previous list named that are still on the pair and unlisted now, by rank."""
    return [policy.id for policy in pair.custom if policy.id in previous and policy.id not in listed]


def _unusable(pair: Pair, listed: Sequence[str], previous: Sequence[str]) -> list[PairRefused]:
    """Why no batch can rank this list on this pair, one finding each: empty when one can."""
    held = {policy.id for policy in pair.custom}
    foreign = [
        ForeignPolicy(
            f'{policy.name!r} ({policy.id}) is a custom policy on {pair} that the census does not own: '
            'it must become a census row or be deleted in the console'
        )
        for policy in pair.custom
        if policy.id not in listed and policy.id not in previous
    ]
    absent = [
        ListedPolicyAbsent(f'{policy} is listed and is not a custom policy on {pair}')
        for policy in listed
        if policy not in held
    ]
    boundless = [] if pair.boundary is not None else [NoPredefinedPolicy(f'{pair} holds no predefined policy')]
    return [*foreign, *absent, *boundless]


def _misplaced(pair: Pair, listed: Sequence[str]) -> list[PairRefused]:
    """Why the list is not the pair's first custom policies in order, ahead of the predefined ones."""
    first = pair.ahead[: len(listed)]
    return [] if first == list(listed) else [ListOutOfPlace(f'{pair} ranks {first} first, and lists {list(listed)}')]


def _refuse(findings: Sequence[PairRefused]) -> None:
    """Raise the first finding's kind with every finding's sentence, so one run names all it found."""
    if findings:
        raise type(findings[0])('; '.join(str(finding) for finding in findings))


def _batch(pair: Pair, listed: Sequence[str], previous: Sequence[str]) -> dict[str, Any]:
    """What the controller is sent: the list, and behind it what is on its way out."""
    return {
        'source_zone_id': pair.source,
        'destination_zone_id': pair.destination,
        'before_predefined_ids': [*listed, *_carried(pair, listed, previous)],
        'after_predefined_ids': [],
    }


def _listed(props: Mapping[str, Any]) -> list[str]:
    return [str(policy) for policy in props['policy_ids']]


def _base(props: Mapping[str, Any]) -> str:
    """The controller, without the trailing slash a caller may write."""
    return str(props['api_url']).rstrip('/')


def _policies_url(props: Mapping[str, Any]) -> str:
    return f'{_base(props)}/proxy/network/v2/api/site/{props["site"]}/firewall-policies'


def _identity(props: Mapping[str, Any]) -> str:
    """The resource id: the pair, on its site."""
    return '|'.join(str(props[key]) for key in IDENTITY)


def _answered(response: requests.Response, what: str) -> requests.Response:
    """A success, or the failure named with the controller's own reason.

    Anything but a 2xx fails, redirects included: a redirect is not followed,
    since it would carry the key's header to wherever it points.
    """
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f'{what} answered {response.status_code}: {response.text}')
    return response


@final
class ZonePolicyOrderProvider(ConfiguredProvider):
    """The rank of one zone pair, through the controller's v2 API."""

    api_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.api_key = str(config.require(API_KEY_CONFIG))

    def _credential(self) -> str:
        return self.api_key

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return _base(props)

    def _version(self) -> str:
        return VERSION

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        self._rank(props, previous=[])
        return dynamic.CreateResult(id_=_identity(props), outs={**props, DRIFT: []})

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        pair = self._read_pair(self._opened(props), props)
        listed = _listed(props)
        drift = [str(finding) for finding in (*_unusable(pair, listed, []), *_misplaced(pair, listed))]
        return dynamic.ReadResult(id_=id_, outs={**props, DRIFT: drift})

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if has_unknowns(news) or any(is_unknown(policy) for policy in news['policy_ids']):
            # A replacement's id is unknown while its order is planned, so the
            # list holds a placeholder rather than being one.
            return dynamic.DiffResult(changes=None)
        return dynamic.DiffResult(
            changes=declared_change(olds, news, COMPARED) or bool(olds.get(DRIFT)),
            replaces=[key for key in IDENTITY if olds.get(key) != news.get(key)],
            delete_before_replace=False,
        )

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        if declared_change(olds, news, ('policy_ids',)) or olds.get(DRIFT):
            self._rank(news, previous=_listed(olds))
        # The outs replace the stored output bag (framework/pulumi.md §5.3 E9),
        # so what state says about the door this pair was ranked through stays
        # true.
        return dynamic.UpdateResult(outs={**news, DRIFT: []})

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        """Nothing: a rank has no object to delete, and the policies go through their own resources."""

    def _opened(self, props: Mapping[str, Any]) -> requests.Session:
        session = requests.Session()
        session.headers[API_KEY_HEADER] = self.api_key
        session.verify = not props['allow_insecure']
        if props['allow_insecure']:
            # The posture is the caller's, stated once where it is declared; a
            # warning per request would stand above every plan.
            urllib3.disable_warnings(InsecureRequestWarning)
        return session

    def _read_pair(self, session: requests.Session, props: Mapping[str, Any]) -> Pair:
        url = _policies_url(props)
        pulumi.log.info(f'reading the policies of {props["source_zone_id"]} -> {props["destination_zone_id"]}')
        response = _answered(session.get(url, timeout=TIMEOUT, allow_redirects=False), f'GET {url}')
        return _pair(
            cast('list[dict[str, Any]]', response.json()),
            str(props['source_zone_id']),
            str(props['destination_zone_id']),
        )

    def _send(self, session: requests.Session, props: Mapping[str, Any], previous: Sequence[str]) -> None:
        """Read the pair, and send it the batch that ranks the list, unless no batch can."""
        listed = _listed(props)
        pair = self._read_pair(session, props)
        _refuse(_unusable(pair, listed, previous))
        pulumi.log.info(f'ranking {len(listed)} policies on {pair}')
        url = f'{_policies_url(props)}/batch-reorder'
        response = session.put(url, json=_batch(pair, listed, previous), timeout=TIMEOUT, allow_redirects=False)
        if response.status_code == 400:
            raise BatchRefused(f'PUT {url} answered 400: {response.text}')
        _ = _answered(response, f'PUT {url}')

    def _rank(self, props: Mapping[str, Any], previous: Sequence[str]) -> None:
        """Rank the list on its pair, and read back that it took."""
        session = self._opened(props)
        try:
            self._send(session, props, previous)
        except BatchRefused as refused:
            pulumi.log.warn(f'{refused}; reading the pair again, in case the console changed it since the read')
            self._send(session, props, previous)
        _refuse(_misplaced(self._read_pair(session, props), _listed(props)))


@final
class ZonePolicyOrder(dynamic.Resource, module='unifi_zone_policy_order', name='ZonePolicyOrder'):
    """The rank of one zone pair: `policy_ids` first, in order, ahead of the predefined policies."""

    site: pulumi.Output[str]
    source_zone_id: pulumi.Output[str]
    destination_zone_id: pulumi.Output[str]
    policy_ids: pulumi.Output[list[str]]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        allow_insecure: pulumi.Input[bool],
        site: pulumi.Input[str],
        source_zone_id: pulumi.Input[str],
        destination_zone_id: pulumi.Input[str],
        policy_ids: Sequence[pulumi.Input[str]],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `policy_ids` are the custom policies of the pair, ranked in this order.

        No property here is a secret: which pair, which policies and which
        address are all things a reviewer should read in a preview. The key
        that writes the rank is not a property at all.
        """
        super().__init__(
            ZonePolicyOrderProvider(),
            name,
            {
                'api_url': api_url,
                'allow_insecure': allow_insecure,
                'site': site,
                'source_zone_id': source_zone_id,
                'destination_zone_id': destination_zone_id,
                'policy_ids': policy_ids,
            },
            opts,
        )
