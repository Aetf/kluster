"""The gateway's controller, for the suites that declare its firewall and the provider that ranks it.

**A mock monitor.** The firewall looks up the controller's two stock zones by
name rather than creating them, so a run under mocks needs something to answer
that lookup. The answer is an invention: every zone exists, is empty, and has
the id `zone_id` spells for it. A suite asserting which zone a rule names
compares against `zone_id` rather than against a spelling of its own.

Every suite whose run reaches the firewall shares this module rather than
carrying a copy -- its own suites and the whole physical program's -- because a
copy is a second place to change when the lookup does, and the copy nobody
updates answers a question the program no longer asks.

**A stand-in for the controller's v2 API**, `ZonePolicies`, which is a model
of what the controller was read to do (testing.md §4), reached through a
`requests.Session` the way the provider reaches the real one. What it keeps:

1.  A pair ends in predefined policies, from index 30000 to 2147483647, as
    read off the census's pairs.
2.  A custom policy is created behind the pair's other custom policies, ahead
    of the predefined ones, starting from index 10000.
3.  `firewall-policies/batch-reorder` refuses a batch that leaves out a custom
    policy of its pair: `400 … Should include firewall policy in batch update`.
    That is the refusal the bridged order meets. It also refuses a batch naming
    anything that is not a custom policy of the pair, which is an assumption
    rather than a reading: nothing has sent the controller such a batch.
4.  A batch that passes ranks what it names and moves nothing else.
5.  A flagged allow's `<name> (Return)` sits on the reverse pair, predefined,
    at index 30000, and needs no place in a batch.
6.  A request without the key is refused (401).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast, override
from urllib.parse import urlsplit

import pulumi
from mock_monitor import Recorder

#: The invoke the firewall resolves a stock zone through.
ZONE_LOOKUP = 'unifi:index/getFirewallZone:getFirewallZone'


def zone_id(name: str) -> str:
    """The id `Controller` answers for the zone of this name."""
    return f'zone-{name}'


class Controller(Recorder):
    """A monitor that answers the firewall's zone lookups; every zone exists and is empty.

    The zone is reported on the site the lookup asked about. A lookup that
    names none gets `site`, standing in for the site the real provider is
    configured with, which is what it falls back to.
    """

    def __init__(self, site: str) -> None:
        super().__init__()
        self.site: str = site

    @override
    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == ZONE_LOOKUP:
            asked = cast('dict[str, Any]', args.args)
            name = str(asked['name'])
            return {'id': zone_id(name), 'name': name, 'networks': [], 'site': asked.get('site', self.site)}
        return {}


#: Where the predefined policies of a pair start, and the last of them
#: (read off Internal -> External).
FIRST_PREDEFINED = 30000
LAST_PREDEFINED = 2147483647

#: Where a pair's custom policies start.
FIRST_CUSTOM = 10000


@dataclass
class Answer:
    """What `requests.Session` hands back, as far as the provider reads one."""

    status_code: int
    text: str = ''
    payload: Any = None

    def json(self) -> Any:
        return self.payload


@dataclass
class ZonePolicies:
    """One site's zone policies, as the controller holds them."""

    key: str
    site: str
    policies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: Every batch sent, in order, the refused ones included.
    batches: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: Every session the provider opened.
    sessions: list[ControllerSession] = field(default_factory=list['ControllerSession'])
    #: Runs once, after the read that comes before the next write: a console
    #: edit landing between the two.
    before_next_write: Callable[[], None] | None = None
    #: Answers a batch as taken and ranks nothing.
    ignores_rank: bool = False
    #: Answers every request with a redirect, as a login page in front of the
    #: API would, which the provider must not follow with the key's header on.
    redirects: bool = False
    next_id: int = 0

    def pair(self, source: str, destination: str) -> None:
        """Give a zone pair its predefined policies."""
        for index, name in ((FIRST_PREDEFINED, 'Block Invalid Traffic'), (LAST_PREDEFINED, 'Allow All Traffic')):
            self._add(name, source, destination, predefined=True, index=index)

    def add(self, name: str, source: str, destination: str) -> str:
        """Create a custom policy, the way the controller places one, and answer its id."""
        ranks = [policy['index'] for policy in self.custom(source, destination)]
        return self._add(name, source, destination, predefined=False, index=max(ranks, default=FIRST_CUSTOM - 1) + 1)

    def derive(self, origin: str, source: str, destination: str) -> str:
        """Add the `(Return)` of the allow named `origin`, on the pair `source` -> `destination`."""
        return self._add(f'{origin} (Return)', source, destination, predefined=True, index=FIRST_PREDEFINED)

    def delete(self, policy: str) -> None:
        self.policies = [held for held in self.policies if held['_id'] != policy]

    def move(self, policy: str, index: int) -> None:
        """What dragging a policy in the console does."""
        (held,) = [held for held in self.policies if held['_id'] == policy]
        held['index'] = index

    def custom(self, source: str, destination: str) -> list[dict[str, Any]]:
        """A pair's custom policies, by rank."""
        return sorted(
            (policy for policy in self._on(source, destination) if not policy['predefined']),
            key=lambda policy: policy['index'],
        )

    def ranked(self, source: str, destination: str) -> list[str]:
        """The names of a pair's custom policies, by rank."""
        return [policy['name'] for policy in self.custom(source, destination)]

    def session(self) -> ControllerSession:
        """A `requests.Session`, as the provider builds one."""
        opened = ControllerSession(self)
        self.sessions.append(opened)
        return opened

    def _on(self, source: str, destination: str) -> list[dict[str, Any]]:
        return [
            policy
            for policy in self.policies
            if policy['source']['zone_id'] == source and policy['destination']['zone_id'] == destination
        ]

    def _add(self, name: str, source: str, destination: str, *, predefined: bool, index: int) -> str:
        self.next_id += 1
        policy = f'policy-{self.next_id}'
        self.policies.append(
            {
                '_id': policy,
                'name': name,
                'predefined': predefined,
                'index': index,
                'source': {'zone_id': source},
                'destination': {'zone_id': destination},
            }
        )
        return policy

    def answer(self, session: ControllerSession, method: str, url: str, body: Any) -> Answer:
        path = urlsplit(url).path
        if session.headers.get('X-API-KEY') != self.key:
            return Answer(401, 'Unauthorized')
        root = f'/proxy/network/v2/api/site/{self.site}/firewall-policies'
        if (method, path) == ('GET', root):
            return Answer(200, payload=copy.deepcopy(self.policies))
        if (method, path) == ('PUT', f'{root}/batch-reorder'):
            return self._reorder(cast('dict[str, Any]', body))
        return Answer(404, f'no {method} {path}')

    def _reorder(self, batch: dict[str, Any]) -> Answer:
        self.batches.append(batch)
        if self.before_next_write is not None:
            edit, self.before_next_write = self.before_next_write, None
            edit()
        custom = self.custom(batch['source_zone_id'], batch['destination_zone_id'])
        before, after = batch['before_predefined_ids'], batch['after_predefined_ids']
        if {policy['_id'] for policy in custom} - {*before, *after}:
            return refusal('Should include firewall policy in batch update')
        if {*before, *after} - {policy['_id'] for policy in custom}:
            return refusal('Policy not found in zone pair')
        if not self.ignores_rank:
            ranks = {policy: FIRST_CUSTOM + rank for rank, policy in enumerate(before)}
            ranks |= {policy: LAST_PREDEFINED + 1 + rank for rank, policy in enumerate(after)}
            for policy in custom:
                policy['index'] = ranks[policy['_id']]
        return Answer(200, payload=[])


def refusal(message: str) -> Answer:
    """The controller's answer to a request it will not take."""
    return Answer(400, json.dumps({'code': 'api.err.Invalid', 'message': message}))


class ControllerSession:
    """A `requests.Session` reaching `ZonePolicies`."""

    def __init__(self, controller: ZonePolicies) -> None:
        self.controller: ZonePolicies = controller
        self.headers: dict[str, str] = {}
        self.verify: bool = True

    def _redirected(self, *, followed: bool) -> Answer | None:
        if not self.controller.redirects:
            return None
        assert not followed, 'the request followed a redirect, carrying the key to wherever it points'
        return Answer(302, 'Found')

    def get(self, url: str, timeout: int = 0, allow_redirects: bool = True) -> Answer:
        return self._redirected(followed=allow_redirects) or self.controller.answer(self, 'GET', url, None)

    def put(self, url: str, json: Any = None, timeout: int = 0, allow_redirects: bool = True) -> Answer:
        return self._redirected(followed=allow_redirects) or self.controller.answer(self, 'PUT', url, json)
