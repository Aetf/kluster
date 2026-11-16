"""The session a provider here opens with the UniFi controller's Network v2 API.

Every provider that writes the controller opens the same session, and this
module is that session with the parts that differ left to the provider: which
endpoint under the site it reaches, and what of a refusal its messages may
carry.

**The credential is the controller's API key**, read in `configure` under the
stack-configuration key `API_KEY_CONFIG` names, and sent as the `X-API-KEY`
header of every request. Where the session goes and whether it verifies the
controller's certificate are declared inputs of each resource, `api_url` and
`allow_insecure`, since a provider decides neither (framework/pulumi.md
§5.2). `check` stamps `session` and `provider_version` through
`kluster.providers.configured`, the session over the address and the key.

**Anything but a 2xx is a refusal, redirects included** (`answered`). A
redirect is not followed, since it would carry the key's header to wherever
it points. A refusal names the request and the status, and carries as much of
the controller's answer as the provider says it may: by default its length
alone (`withheld`), the whole body where the provider sends nothing that
needs keeping out of a message (`relayed`), or the provider's own reading of
it.

**A request is bounded by `TIMEOUT` and never retried here.** Whether a
second attempt is safe is the operation's question, so a provider that
retries says why where it does.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Mapping
from typing import Any

import pulumi.dynamic as dynamic
import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from kluster.providers.configured import ConfiguredProvider

__all__ = (
    'API_KEY_CONFIG',
    'API_KEY_HEADER',
    'TIMEOUT',
    'ControllerProvider',
    'ControllerRefused',
    'answered',
    'base',
    'relayed',
    'site_url',
    'withheld',
)

#: Long enough for a controller under parallel load, short enough that an
#: unreachable gateway fails the resource instead of hanging the stack.
TIMEOUT = 15

#: The stack-configuration key the controller's API key is read from
#: (`configured`).
API_KEY_CONFIG = 'unifiApiKey'

#: The header the controller reads an API key from.
API_KEY_HEADER = 'X-API-KEY'


class ControllerRefused(RuntimeError):
    """The controller answered a request with anything but a 2xx."""


def base(props: Mapping[str, Any]) -> str:
    """The controller, without the trailing slash a caller may write."""
    return str(props['api_url']).rstrip('/')


def site_url(props: Mapping[str, Any], path: str) -> str:
    """`path` under the declared site, on the Network application's v2 API."""
    return f'{base(props)}/proxy/network/v2/api/site/{props["site"]}/{path}'


def withheld(response: requests.Response) -> str:
    """An answer named by its length alone."""
    return f'{len(response.text)} characters, not relayed'


def relayed(response: requests.Response) -> str:
    """An answer whole."""
    return response.text


def answered(
    response: requests.Response,
    what: str,
    *,
    reason: Callable[[requests.Response], str] = withheld,
    refused: type[ControllerRefused] = ControllerRefused,
) -> requests.Response:
    """A success, or `refused` naming `what`, the status, and `reason`'s reading of the answer."""
    if not 200 <= response.status_code < 300:
        raise refused(f'{what} answered {response.status_code}: {reason(response)}')
    return response


class ControllerProvider(ConfiguredProvider, abc.ABC):
    """A configured provider whose session is the controller's: its key, at the declared address and posture.

    A subclass supplies its version, as every configured provider does, and
    its operations.
    """

    api_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.api_key = str(config.require(API_KEY_CONFIG))

    def _credential(self) -> str:
        return self.api_key

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return base(props)

    def _opened(self, props: Mapping[str, Any]) -> requests.Session:
        """A session carrying the key, verifying the controller's certificate unless the inputs say otherwise."""
        session = requests.Session()
        session.headers[API_KEY_HEADER] = self.api_key
        session.verify = not props['allow_insecure']
        if props['allow_insecure']:
            # The posture is the caller's, stated once where it is declared; a
            # warning per request would stand above every plan.
            urllib3.disable_warnings(InsecureRequestWarning)
        return session
