"""The model a gate asks, behind one call: `Model.ask`, with the Messages API's structured outputs behind it.

`Messages` is the real one. It holds the Claude Console API key the
register files as the model gates' (credentials.md §3), which `key` finds
through the acquisition chain: the desktop secret store, then the
workstation slot, then `KLUSTER_GATES_API_KEY`. The client is given that
key and the API's address outright, so nothing the shell happens to carry
for another purpose, `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`, is spent
or followed. A call the API still fails after the SDK's retries is a reply
that answered nothing, so one failed call costs its samples and not the
run. The cases hand `Messages` an HTTP client whose transport serves
recorded responses, so the request it builds and the reply it reads are the SDK's
own, with no network and no key.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import anthropic

from kluster.lib import acquisition
from kluster.scripts.credentials import escrow
from kluster.scripts.credentials import workstation as slots

log = logging.getLogger(__name__)

#: The API's own address, given outright rather than read from the environment.
API = 'https://api.anthropic.com'

#: The most a reply may run to. A reply carries a verdict and a sentence per
#: item, and Haiku's thinking ahead of it; the bound only stops a reply
#: running away, since output is paid by the token actually written.
MAX_TOKENS = 16_000

#: How many times the SDK retries a call the API answered with a rate limit,
#: an overload or a server error.
RETRIES = 4


class KeyMissing(RuntimeError):
    """No layer of the acquisition chain holds the model gates' key, or its slot does not read."""


@dataclass(frozen=True)
class Request:
    """One call: the model, the prompt with its canon, the items, and the schema the reply is held to."""

    model: str
    system: str
    user: str
    schema: Mapping[str, object]


@dataclass(frozen=True)
class Reply:
    """What a call returned: the reply's text, or why there is none."""

    text: str | None
    #: Why the reply has no text to read: a refusal, a reply cut short. None where it has.
    unanswered: str | None = None


class Model(Protocol):
    def ask(self, request: Request) -> Reply: ...


@dataclass(frozen=True)
class Messages:
    """`Model` through the Messages API."""

    client: anthropic.Anthropic

    @classmethod
    def with_key(
        cls, key: str, *, http_client: anthropic.DefaultHttpxClient | None = None, retries: int = RETRIES
    ) -> Messages:
        return cls(anthropic.Anthropic(api_key=key, base_url=API, max_retries=retries, http_client=http_client))

    def ask(self, request: Request) -> Reply:
        """The reply to `request`; a call the API still fails after the SDK's retries is a reply with no text."""
        try:
            message = self.client.messages.create(
                model=request.model,
                max_tokens=MAX_TOKENS,
                system=request.system,
                messages=[{'role': 'user', 'content': request.user}],
                output_config={'format': {'type': 'json_schema', 'schema': dict(request.schema)}},
            )
        except anthropic.APIStatusError as exc:
            return Reply(None, f'the call failed: {type(exc).__name__} {exc.status_code}')
        except anthropic.APIError as exc:
            return Reply(None, f'the call failed: {type(exc).__name__}')
        if message.stop_reason != 'end_turn':
            return Reply(None, f'the reply stopped on {message.stop_reason}')
        text = next((block.text for block in message.content if block.type == 'text'), None)
        return Reply(text) if text is not None else Reply(None, 'the reply holds no text')


def key() -> str:
    """The model gates' key, from the first layer of the acquisition chain that holds it. Raises `KeyMissing`."""
    path = slots.gates_key_path()
    try:
        found = acquisition.find(slots.GATES_KEY_ACCOUNT, path, slots.GATES_KEY_ENV)
    except OSError as exc:
        raise KeyMissing(f'the model gates key slot {path} cannot be read ({exc.strerror})') from exc
    if found is None:
        row = escrow.row_name(escrow.GATES_KEY)
        raise KeyMissing(
            f'no model gates key on this machine: not in the desktop secret store, not in {path}, not in '
            f'{slots.GATES_KEY_ENV}. `credentials derived {row} record` escrows one made in the Console, and '
            f'`credentials derived {row} recover` puts it in the store, or in the slot where there is no store'
        )
    value, layer = found
    log.info('the model gates key is from %s', layer)
    return value


def dumps(value: object) -> str:
    """JSON as the requests carry it: stable, so the same items make the same request."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1)
