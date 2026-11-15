"""A stand-in for the controller's site BGP configuration, for the suites of `kluster.providers.unifi_bgp`.

**A model of what the controller was read to do** (testing.md §4), reached
through a `requests.Session` the way the provider reaches the real one, on
`v2/api/site/<site>/bgp/config` (physical/gateway.md §1.3). What it keeps:

1.  A GET answers a list: `[]` on a site that holds no configuration, read off
    a site that held none, and otherwise the one object.
2.  A POST of the object writes it, on a site that holds one as on one that
    holds none, and answers the object as stored. The stored object carries
    `_id`, `description`, `enabled`, `error`, `frr_bgpd_config`,
    `redistribute_to_sd_wan` and `uploaded_file_name`, the fields the
    controller answered a POST with, and keeps the text whole, as the key
    reads it back. The values of `error` and `redistribute_to_sd_wan` were not
    recorded, so they stand here as `None` and `False`; nothing reads them.
3.  A DELETE takes the object away, and the next GET answers `[]`.
4.  A request without the key is refused (401).

The site's one object is `held`, which a case edits the way the console does.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit


@dataclass
class Answer:
    """What `requests.Session` hands back, as far as the provider reads one."""

    status_code: int
    text: str = ''
    payload: Any = None

    def json(self) -> Any:
        if self.payload is None:
            return json.loads(self.text)
        return self.payload


@dataclass
class BgpConfigs:
    """One site's BGP configuration, as the controller holds it."""

    key: str
    site: str
    held: dict[str, Any] | None = None
    #: Every request, as `(method, path)`, in order.
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: Every object a POST sent, in order.
    writes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    #: Every session the provider opened.
    sessions: list[BgpSession] = field(default_factory=list['BgpSession'])
    #: Answers the next POST with this refusal, writing nothing.
    refuses_next_write: Answer | None = None
    #: What the controller makes of a written text, where a case says it
    #: stores something other than what it was sent.
    stores: Callable[[str], str] | None = None
    #: Answers every request with a redirect, as a login page in front of the
    #: API would, which the provider must not follow with the key's header on.
    redirects: bool = False
    #: Answers a DELETE as taken and keeps the configuration.
    ignores_delete: bool = False
    #: Configurations the site's list holds beside `held`, which no reading
    #: has shown: a site holds one, and this is the case of a controller that
    #: answered otherwise.
    also_listed: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    next_id: int = 0

    def hold(self, *, config: str, description: str, file_name: str, enabled: bool = True) -> dict[str, Any]:
        """Give the site a configuration, the way the console's upload does."""
        self.next_id += 1
        self.held = {
            '_id': f'bgp-{self.next_id}',
            'description': description,
            'enabled': enabled,
            'error': None,
            'frr_bgpd_config': config,
            'redistribute_to_sd_wan': False,
            'uploaded_file_name': file_name,
        }
        return self.held

    def session(self) -> BgpSession:
        """A `requests.Session`, as the provider builds one."""
        opened = BgpSession(self)
        self.sessions.append(opened)
        return opened

    def answer(self, session: BgpSession, method: str, url: str, body: Any) -> Answer:
        path = urlsplit(url).path
        self.requests.append((method, path))
        if session.headers.get('X-API-KEY') != self.key:
            return Answer(401, 'Unauthorized')
        if path != f'/proxy/network/v2/api/site/{self.site}/bgp/config':
            return Answer(404, f'no {method} {path}')
        if method == 'GET':
            held = [] if self.held is None else [self.held]
            return Answer(200, payload=copy.deepcopy([*held, *self.also_listed]))
        if method == 'POST':
            return self._write(cast('dict[str, Any]', body))
        if method == 'DELETE':
            if not self.ignores_delete:
                self.held = None
            return Answer(200, '')
        return Answer(405, f'no {method}')

    def _write(self, sent: dict[str, Any]) -> Answer:
        self.writes.append(copy.deepcopy(sent))
        refusal, self.refuses_next_write = self.refuses_next_write, None
        if refusal is not None:
            return refusal
        kept = None if self.held is None else self.held['_id']
        stored = self.hold(
            config=str(sent['frr_bgpd_config']) if self.stores is None else self.stores(str(sent['frr_bgpd_config'])),
            description=str(sent['description']),
            file_name=str(sent['uploaded_file_name']),
            enabled=bool(sent['enabled']),
        )
        if kept is not None:
            stored['_id'] = kept
        return Answer(200, payload=copy.deepcopy(stored))


def refusal(message: str) -> Answer:
    """The controller's answer to a write it will not take."""
    return Answer(400, json.dumps({'code': 'api.err.Invalid', 'message': message}))


class BgpSession:
    """A `requests.Session` reaching `BgpConfigs`."""

    def __init__(self, controller: BgpConfigs) -> None:
        self.controller: BgpConfigs = controller
        self.headers: dict[str, str] = {}
        self.verify: bool = True

    def _redirected(self, *, followed: bool) -> Answer | None:
        if not self.controller.redirects:
            return None
        assert not followed, 'the request followed a redirect, carrying the key to wherever it points'
        return Answer(302, 'Found')

    def get(self, url: str, timeout: int = 0, allow_redirects: bool = True) -> Answer:
        return self._redirected(followed=allow_redirects) or self.controller.answer(self, 'GET', url, None)

    def post(self, url: str, json: Any = None, timeout: int = 0, allow_redirects: bool = True) -> Answer:
        return self._redirected(followed=allow_redirects) or self.controller.answer(self, 'POST', url, json)

    def delete(self, url: str, timeout: int = 0, allow_redirects: bool = True) -> Answer:
        return self._redirected(followed=allow_redirects) or self.controller.answer(self, 'DELETE', url, None)
