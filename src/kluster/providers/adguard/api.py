"""The transport: one instance's administration API, opened with one login.

Every request goes to `<endpoint>/control/<path>` and carries the login as HTTP
basic authentication, which the instance accepts on every `/control/` path. A
request the instance refuses raises `requests.HTTPError` naming the method, the
path, the status and the instance's own reason, which it writes as the body of
every refusal: a refusal is never read as an empty answer.

**A body goes out as JSON whose whole numbers are integers.** A property bag
reaches a dynamic provider with every number a float, and the instance decodes
its integer fields with Go's `encoding/json`, which refuses `24.0` for a
`uint32`. So `wire` writes a float that holds a whole number as an integer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import requests

__all__ = ('DOWNLOAD_TIMEOUT', 'TIMEOUT', 'Api', 'wire')

#: Long enough for a busy resolver, short enough that an unreachable UDM fails
#: the resource instead of hanging the stack.
TIMEOUT = 15

#: For a request that makes the instance download a filter list before it
#: answers. The instance bounds its own download by its HTTP client's timeout,
#: which is its server's five-minute write timeout (`home/httpclient.go`,
#: `home/web.go` `writeTimeout`), so the request is given that and a margin.
DOWNLOAD_TIMEOUT = 330


def wire(value: Any) -> Any:
    """`value` with every float that holds a whole number written as an integer, at any depth."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {key: wire(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [wire(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


class Api:
    """One session onto one instance."""

    def __init__(self, endpoint: str, username: str, password: str) -> None:
        self.base: str = endpoint.rstrip('/')
        self.session: requests.Session = requests.Session()
        self.session.auth = (username, password)

    def get(self, path: str) -> Any:
        """The instance's answer to a GET, decoded."""
        url = self._url(path)
        response = self.session.get(url, timeout=TIMEOUT)
        _refused(response, 'GET', path)
        return response.json()

    def post(self, path: str, body: object, *, timeout: int = TIMEOUT) -> None:
        response = self.session.post(self._url(path), json=wire(body), timeout=timeout)
        _refused(response, 'POST', path)

    def put(self, path: str, body: object) -> None:
        response = self.session.put(self._url(path), json=wire(body), timeout=TIMEOUT)
        _refused(response, 'PUT', path)

    def _url(self, path: str) -> str:
        return f'{self.base}/control/{path}'


def _refused(response: requests.Response, method: str, path: str) -> None:
    if response.status_code >= 400:
        reason = response.text.strip()
        raise requests.HTTPError(f'{method} /control/{path} answered {response.status_code}: {reason}')
