"""The model a gate asks, behind one call: `Model.ask`, with the Messages API's tool runner and structured outputs behind it.

`Messages` is the real one. It holds the Claude Console API key the
register files as the model gates' (credentials.md §3), which `key` finds
through the acquisition chain: the desktop secret store, then the
workstation slot, then `KLUSTER_GATES_API_KEY`. The client is given that
key and the API's address outright, so nothing the shell happens to carry
for another purpose, `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`, is spent
or followed.

A call is a conversation the SDK's tool runner holds: the model may call
the tools of the call's tree (`tree.Tree.tools`) before it answers, a
round at a time, up to `ROUNDS`, and its answer is held to the call's
schema by structured outputs, which the API combines with strict tools on
every model it serves them on. A call that is still reading at the bound is
a reply that answered nothing. How many rounds a call took, and the input
and output tokens its requests spent, is logged and carried on its reply
(`Usage`).

Each request asks the API to cache its prompt (`CACHE`): every round sends
the conversation again, and a call's samples send the same prefix, so
later rounds and sibling samples read what an earlier request wrote rather
than paying for it again. What a call wrote to the cache and read from it
is counted beside its other input tokens.

A request of the call the API refuses for the key's rate limit, a `429`,
is waited out as the API says, for up to `RATE_LIMIT_WAIT` over every
request of the call, its tool rounds included, each wait logged before it
starts with the API's reason and its length (`Waits`): the limit clears
as the minute it is metered over passes, so a refusal for it is no failure
of the call. Any other failure the SDK retries, `RETRIES` times. A call the
API still fails after either is a reply that answered nothing, so one
failed call costs its samples and not the run.

The cases hand `Messages` an HTTP client whose transport serves recorded
responses, so the requests it builds and the replies it reads are the SDK's
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
from anthropic.types.beta import BetaCacheControlEphemeralParam, BetaMessage

from kluster.lib import acquisition
from kluster.scripts.credentials import escrow
from kluster.scripts.credentials import workstation as slots

from .tree import Tree

log = logging.getLogger(__name__)

#: The API's own address, given outright rather than read from the environment.
API = 'https://api.anthropic.com'

#: The most a reply may run to. A reply carries a place and a sentence per
#: finding, and Haiku's thinking ahead of it; the bound only stops a reply
#: running away, since output is paid by the token actually written. It
#: bounds each reply of a call, a tool round's among them, so a call that
#: reads the repository first needs no more of it for its answer.
MAX_TOKENS = 16_000

#: The most tool rounds a call takes before its answer: a reply that calls a
#: tool, and the tool's answer sent back. The rules the tools serve turn on
#: a definition elsewhere, the other side of an assertion, or the members of
#: a count, each a search and a read or two, so this is room for a few of
#: them; a call that is still reading past it is lost rather than closer, and
#: answers nothing. Each round sends the conversation again, so it also bounds
#: what a call costs, with `tree.TOOL_BYTES`. One tool is called a round
#: (`disable_parallel_tool_use`), so a round adds one answer.
ROUNDS = 10

#: The prompt caching every request of a call asks for: automatic caching,
#: whose breakpoint the API places on the last cacheable block and moves
#: forward as the conversation grows. A call's prompt, canon, examples and
#: parts open every request of it and of its siblings (`verdict.SAMPLES`), and
#: each tool round adds to the end, so each request but the first reads its
#: prefix from the cache at a fraction of the price of input.
CACHE: BetaCacheControlEphemeralParam = {'type': 'ephemeral'}

#: How many times the SDK retries a call the API answered with an overload, a
#: server error or a timeout, or could not be reached. A rate limit is not
#: among them: `Waits` waits that out (`RATE_LIMIT_WAIT`).
RETRIES = 4

#: The most one call waits for the key's rate limit to clear before its
#: sample answers nothing, in seconds, over all the waits of all its
#: requests, its tool rounds' among them. The API meters its limits per
#: minute and names the wait in `retry-after`. A request can be refused again
#: after its wait, while the other calls in flight (`ask.PARALLEL`) take what
#: the minute gave back, so the bound is ten of those minutes: room for a
#: calibration's calls to take turns, and short enough that a limit waiting
#: does not clear ends the run in minutes rather than hours.
RATE_LIMIT_WAIT = 600.0

#: The wait for a `429` whose `retry-after` names none, in seconds: the minute
#: the API meters its limits over. A wait is never nothing, so a call refused
#: again and again reaches `RATE_LIMIT_WAIT`.
RATE_LIMIT_DEFAULT = 60.0


class KeyMissing(RuntimeError):
    """No layer of the acquisition chain holds the model gates' key, or its slot does not read."""


@dataclass(frozen=True)
class Request:
    """One call: the model, the prompt with its canon, what the call shows, the schema the reply is held to, and the tree its tools read."""

    model: str
    system: str
    user: str
    schema: Mapping[str, object]
    tree: Tree


@dataclass(frozen=True)
class Usage:
    """What a call spent: its tool rounds, and the tokens of its requests.

    The input tokens are those the cache neither wrote nor read; the tokens
    written to the cache and read from it are counted apart, as the API
    reports and prices them.
    """

    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.rounds + other.rounds,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    def __str__(self) -> str:
        return (
            f'{self.rounds} tool rounds, {self.input_tokens} input tokens, {self.cache_creation_input_tokens} '
            f'written to the cache and {self.cache_read_input_tokens} read from it, {self.output_tokens} output tokens'
        )


@dataclass(frozen=True)
class Reply:
    """What a call returned: the reply's text, or why there is none, and what the call spent."""

    text: str | None
    #: Why the reply has no text to read: a refusal, a reply cut short. None where it has.
    unanswered: str | None = None
    usage: Usage = Usage()


class Model(Protocol):
    def ask(self, request: Request) -> Reply: ...


class RateLimited(Exception):
    """The API refused a request of a call for the key's rate limit past `RATE_LIMIT_WAIT`, and why."""

    def __init__(self, reason: str) -> None:
        super().__init__(f'rate-limited: {reason}')
        self.reason = reason


def _limit(response: anthropic.APIResponse[Any]) -> tuple[float, str]:
    """The wait a `429` names, and the API's reason for it."""
    http = response.http_response
    try:
        named = float(http.headers.get('retry-after', ''))
    except ValueError:
        named = 0.0
    try:
        body = cast(dict[str, Any], json.loads(http.read()))
        reason = str(cast(dict[str, Any], body['error'])['message'])
    except (ValueError, KeyError, TypeError):
        reason = f'{http.status_code} {http.reason_phrase}'
    finally:
        http.close()
    return (named if named > 0 else RATE_LIMIT_DEFAULT), reason


@dataclass
class Waits(anthropic.Middleware):
    """Middleware of one call: a request the API answers `429` is sent again after the wait it names.

    The SDK retries a `429` too, honoring `retry-after`, but bounds its
    retries by a count shared with every status it retries, while a rate
    limit clears with time; it logs its wait without the reason, and sleeps
    where no case can hand it a clock. So the SDK keeps every other retry,
    and each call's own `Waits` waits the `429`s of all its requests out
    against `RATE_LIMIT_WAIT`, which is why it is made a call at a time
    (`Messages.ask`). Past the bound it raises `RateLimited`, which the SDK
    does not retry.
    """

    sleep: Callable[[float], None]
    #: How long the call has waited, over all its requests.
    waited: float = 0.0

    def handle(self, request: anthropic.APIRequest, call_next: anthropic.CallNext) -> anthropic.APIResponse[Any]:
        while True:
            response = call_next(request)
            if response.status_code != 429:
                return response
            wait, reason = _limit(response)
            if self.waited + wait > RATE_LIMIT_WAIT:
                log.warning(
                    'the API rate-limited a call (%s) and names a %.0f s wait, which would take it past the '
                    '%.0f s a call waits out, %.0f s of it waited; its sample answers nothing',
                    reason,
                    wait,
                    RATE_LIMIT_WAIT,
                    self.waited,
                )
                raise RateLimited(reason)
            log.info(
                'the API rate-limited a call (%s): waiting %.0f s as it says, after the %.0f s it has waited '
                'of the %.0f s a call waits out',
                reason,
                wait,
                self.waited,
                RATE_LIMIT_WAIT,
            )
            self.sleep(wait)
            self.waited += wait


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
        client = anthropic.Anthropic(api_key=key, base_url=API, max_retries=retries, http_client=http_client)
        return cls(client, sleep)

    def ask(self, request: Request) -> Reply:
        """The reply to `request`, after the tool rounds it takes; with no text where the call gives no answer.

        A call that still asks for a tool after `ROUNDS` rounds, that the API
        still fails after the SDK's retries, or that is rate-limited past
        `RATE_LIMIT_WAIT`, is a reply with no text. Whichever it is, the
        reply carries what the call spent, which is logged.
        """
        client = self.client.with_options(middleware=[Waits(self.sleep)])
        # The full conversation in every request (`thread=False`), rather than a thread the server holds between
        # turns: what a request carries is then what the call asked, read whole by the transport a case hands it.
        runner = client.beta.messages.tool_runner(
            model=request.model,
            max_tokens=MAX_TOKENS,
            system=request.system,
            messages=[{'role': 'user', 'content': request.user}],
            tools=request.tree.tools(),
            tool_choice={'type': 'auto', 'disable_parallel_tool_use': True},
            output_config={'format': {'type': 'json_schema', 'schema': dict(request.schema)}},
            max_iterations=ROUNDS + 1,
            cache_control=CACHE,
            thread=False,
        )
        usage = Usage()
        last: BetaMessage | None = None
        try:
            for message in runner:
                last = message
                spent = message.usage
                usage += Usage(
                    int(message.stop_reason == 'tool_use'),
                    spent.input_tokens,
                    spent.output_tokens,
                    spent.cache_creation_input_tokens or 0,
                    spent.cache_read_input_tokens or 0,
                )
        except RateLimited as exc:
            reply = Reply(None, f'the call was rate-limited past {RATE_LIMIT_WAIT:.0f} s: {exc.reason}', usage)
        except anthropic.APIStatusError as exc:
            reply = Reply(None, f'the call failed: {type(exc).__name__} {exc.status_code}', usage)
        except anthropic.APIError as exc:
            reply = Reply(None, f'the call failed: {type(exc).__name__}', usage)
        else:
            reply = _answer(last, usage)
        log.info('a call took %s', usage)
        return reply


def _answer(message: BetaMessage | None, usage: Usage) -> Reply:
    """The reply the call's last message gives."""
    if message is None:
        return Reply(None, 'the call returned no reply', usage)
    if message.stop_reason == 'tool_use':
        return Reply(None, f'the call was still reading the repository after {ROUNDS} tool rounds', usage)
    if message.stop_reason != 'end_turn':
        return Reply(None, f'the reply stopped on {message.stop_reason}', usage)
    text = next((block.text for block in message.content if block.type == 'text'), None)
    return Reply(text, usage=usage) if text is not None else Reply(None, 'the reply holds no text', usage)


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
