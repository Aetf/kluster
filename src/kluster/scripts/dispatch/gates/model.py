"""The model a gate asks, behind one call: `Model.ask`, with the Messages API's structured outputs behind it.

`Messages` is the real one. It holds the Claude Console API key the
register files as the model gates' (credentials.md §3), which `key` finds
through the acquisition chain: the desktop secret store, then the
workstation slot, then `KLUSTER_GATES_API_KEY`. The client is given that
key and the API's address outright, so nothing the shell happens to carry
for another purpose, `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`, is spent
or followed.

A call the API refuses for the key's rate limit, a `429`, is waited out as
the API says, for up to `RATE_LIMIT_WAIT` over the call, each wait logged
before it starts with the API's reason and its length (`Messages.ask`): the
limit clears as the minute it is metered over passes, so a refusal for it is
no failure of the call. Any other failure the SDK retries, `RETRIES` times.
A call the API still fails after either is a reply that answered nothing,
so one failed call costs its samples and not the run.

The cases hand `Messages` an HTTP client whose transport serves recorded
responses, so the request it builds and the reply it reads are the SDK's
own, with no network and no key, and a sleep that only records its waits.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import anthropic

from kluster.lib import acquisition
from kluster.scripts.credentials import escrow
from kluster.scripts.credentials import workstation as slots

log = logging.getLogger(__name__)

#: The API's own address, given outright rather than read from the environment.
API = 'https://api.anthropic.com'

#: The most a reply may run to. A reply carries a place and a sentence per
#: finding, and Haiku's thinking ahead of it; the bound only stops a reply
#: running away, since output is paid by the token actually written.
MAX_TOKENS = 16_000

#: How many times the SDK retries a call the API answered with an overload, a
#: server error or a timeout, or could not be reached. A rate limit is not
#: among them: `Messages.ask` waits that out (`RATE_LIMIT_WAIT`).
RETRIES = 4

#: The most one call waits, over all its waits, for the key's rate limit to
#: clear before its sample answers nothing, in seconds. The API meters its
#: limits per minute and names the wait in `retry-after`. A call can be
#: refused again after its wait, while the other calls in flight
#: (`ask.PARALLEL`) take what the minute gave back, so the bound is ten of
#: those minutes: room for a calibration's calls to take turns, and short
#: enough that a limit waiting does not clear ends the run in minutes rather
#: than hours.
RATE_LIMIT_WAIT = 600.0

#: The wait for a `429` whose `retry-after` names none, in seconds: the minute
#: the API meters its limits over. A wait is never nothing, so a call refused
#: again and again reaches `RATE_LIMIT_WAIT`.
RATE_LIMIT_DEFAULT = 60.0


class KeyMissing(RuntimeError):
    """No layer of the acquisition chain holds the model gates' key, or its slot does not read."""


@dataclass(frozen=True)
class Request:
    """One call: the model, the prompt with its canon, what the call shows, and the schema the reply is held to."""

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


class RateLimited(Exception):
    """The API refused an attempt for the key's rate limit: how long it says to wait, and its reason."""

    def __init__(self, wait: float, reason: str) -> None:
        super().__init__(f'rate-limited: {reason}')
        self.wait = wait
        self.reason = reason


def rate_limited(request: anthropic.APIRequest, call_next: anthropic.CallNext) -> anthropic.APIResponse[Any]:
    """Middleware: an attempt the API answers `429` raises `RateLimited`, which the SDK does not retry.

    The SDK retries a `429` too, honoring `retry-after`, but bounds its
    retries by a count shared with every status it retries, while a rate
    limit clears with time; it logs its wait without the reason, and sleeps
    where no case can hand it a clock. So the SDK keeps every other retry,
    and this hands the `429` to `Messages.ask`, which waits it out against
    `RATE_LIMIT_WAIT` and logs why.
    """
    response = call_next(request)
    if response.status_code != 429:
        return response
    http = response.http_response
    try:
        named = float(http.headers.get('retry-after', ''))
    except ValueError:
        named = 0.0
    wait = named if named > 0 else RATE_LIMIT_DEFAULT
    try:
        body = cast(dict[str, Any], json.loads(http.read()))
        reason = str(cast(dict[str, Any], body['error'])['message'])
    except (ValueError, KeyError, TypeError):
        reason = f'{http.status_code} {http.reason_phrase}'
    finally:
        http.close()
    raise RateLimited(wait, reason)


@dataclass(frozen=True)
class Messages:
    """`Model` through the Messages API."""

    client: anthropic.Anthropic
    #: What a wait for the rate limit sleeps on; a case hands one that only records.
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def with_key(
        cls,
        key: str,
        *,
        http_client: anthropic.DefaultHttpxClient | None = None,
        retries: int = RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Messages:
        client = anthropic.Anthropic(
            api_key=key, base_url=API, max_retries=retries, http_client=http_client, middleware=[rate_limited]
        )
        return cls(client, sleep)

    def ask(self, request: Request) -> Reply:
        """The reply to `request`; a call the API still fails after the SDK's retries is a reply with no text.

        A call the API refuses for the rate limit is made again after the
        wait the API names, each wait logged before it starts, while the
        call's waits stay within `RATE_LIMIT_WAIT`; past that, it is a reply
        with no text.
        """
        waited = 0.0
        while True:
            try:
                return self._ask(request)
            except RateLimited as exc:
                if waited + exc.wait > RATE_LIMIT_WAIT:
                    log.warning(
                        'the API rate-limited a call (%s) and names a %.0f s wait, which would take it past the '
                        '%.0f s a call waits out, %.0f s of it waited; its sample answers nothing',
                        exc.reason,
                        exc.wait,
                        RATE_LIMIT_WAIT,
                        waited,
                    )
                    return Reply(None, f'the call was rate-limited past {RATE_LIMIT_WAIT:.0f} s: {exc.reason}')
                log.info(
                    'the API rate-limited a call (%s): waiting %.0f s as it says, after the %.0f s it has waited '
                    'of the %.0f s a call waits out',
                    exc.reason,
                    exc.wait,
                    waited,
                    RATE_LIMIT_WAIT,
                )
                self.sleep(exc.wait)
                waited += exc.wait

    def _ask(self, request: Request) -> Reply:
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
    """JSON as the requests carry it: stable, so the same value makes the same request."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1)
