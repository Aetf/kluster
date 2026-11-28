"""The stub gate the model-gate cases run, and what it runs against: a scratch repository, a forge, recorded replies.

Its own module because the harness, calibration and harvest cases all run the
same gate, and a test module is not imported by another
(`tests/test_suite_imports.py`). The gate is `tests/data/gates/stub/`, copied
into the case's own directory so that a record a case writes stays its own;
it is shown each file a change writes under `docs/`, its question is about
the lines a change writes into a note, and its canon is a section of
`docs/rule.md` in the scratch repository.

The replies are Messages API response bodies (`tests/data/gates/stub/messages/`)
in the shape the API documents, served by a transport handed to the real
client, so a run builds its requests and reads its replies through the SDK
with no network and no key. Each request takes the next body in turn, or,
for a call that reads the repository first, the body of the turn its
conversation is at (`Recorded.by_turn`), whichever calls are in flight
beside it. A case that needs other findings builds the body with `reply`,
and a tool round with `tool_use`. A case that needs the rate limit has the
transport refuse its first requests with a `429`, or its first requests
that carry a tool's answer, and the client's waits are recorded rather
than slept.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anthropic
import httpx2
import process_sessions
import pytest
from memory_forge import MemoryForge

from kluster.scripts.dispatch.forge import CODE
from kluster.scripts.dispatch.gates.forge import PullComment
from kluster.scripts.dispatch.gates.gate import Canon, Gate, Spec
from kluster.scripts.dispatch.gates.model import Messages
from kluster.scripts.dispatch.gates.tree import Tree, TreeGit
from kluster.scripts.dispatch.prcheck import PullRequest

#: The stub gate as committed.
STUB = Path(__file__).parent / 'data' / 'gates' / 'stub'

#: Each git call's bound: a stop-loss, which nothing asserts on.
GIT_TIMEOUT = 10

#: The document the stub gate's canon is a section of, and that section.
RULE_DOCUMENT = 'docs/rule.md'
RULE_SECTION = 'The rule'
RULE = '# Rules\n\n## The rule\n\nA note states what a service declares, never where it runs.\n\n## Another\n\nElse.\n'

PULL = 4917
BRANCH = 'b-917-notes'


#: The stub gate's declaration.
SPEC = Spec(canon=Canon(RULE_DOCUMENT, RULE_SECTION), trigger=('docs/**',))


def stub_gate(directory: Path) -> Gate:
    """The stub gate, copied into `directory`."""
    copy = directory / 'stub'
    _ = shutil.copytree(STUB, copy)
    return Gate.load(copy, SPEC)


def scratch_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty scratch repository, with the caller's git kept out of it."""
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('HOME', str(tmp_path))
    for role in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{role}_NAME', 'gates probe')
        monkeypatch.setenv(f'GIT_{role}_EMAIL', 'probe@example.invalid')
    repo = tmp_path / 'repo'
    repo.mkdir()
    _ = git(repo, 'init', '-q')
    return repo


def git(repo: Path, *arguments: str) -> str:
    return process_sessions.run(['git', *arguments], cwd=repo, text=True, check=True, timeout=GIT_TIMEOUT).stdout


def commit(repo: Path, files: Mapping[str, str], message: str = 'a commit') -> str:
    """Write `files`, commit everything with `message`, and return the commit."""
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)
    _ = git(repo, 'add', '-A')
    _ = git(repo, 'commit', '-q', '--cleanup=verbatim', '-m', message)
    return git(repo, 'rev-parse', 'HEAD').strip()


@dataclass
class GateMemoryForge(MemoryForge):
    """`memory_forge`'s forge, with each pull request read whole and its comments by id."""

    requests: dict[int, PullRequest] = field(default_factory=dict[int, PullRequest])
    threads: dict[int, list[PullComment]] = field(default_factory=dict[int, list[PullComment]])
    #: The commits the forge no longer serves.
    unserved: set[str] = field(default_factory=set[str])

    def pull_request(self, number: int) -> PullRequest:
        return self.requests[number]

    def pull_comments(self, number: int) -> list[PullComment]:
        return list(self.threads.get(number, []))

    def serves(self, commit: str) -> bool:
        return commit not in self.unserved

    def pull_commits(self, number: int) -> list[str]:
        return [self.requests[number].head]

    def comment_at(self, url: str) -> PullComment | None:
        return next((c for thread in self.threads.values() for c in thread if c.url == url), None)

    def say(self, number: int, body: str, *, mine: bool = True) -> PullComment:
        """A comment on pull request `number`, by this account or, with `mine` false, by another."""
        identity = sum(len(t) for t in self.threads.values()) + 1
        url = f'https://github.com/{CODE}/pull/{number}#issuecomment-{identity}'
        # Each comment is written a second after the one before it.
        said = PullComment(identity, body, mine, url, f'2026-11-26T00:{identity // 60:02}:{identity % 60:02}Z')
        self.threads.setdefault(number, []).append(said)
        return said

    def post(self, number: int, body: str) -> None:
        _ = self.say(number, body)

    def edit(self, comment: int, body: str) -> None:
        for thread in self.threads.values():
            for index, held in enumerate(thread):
                if held.id == comment:
                    # The forge keeps when a comment was written through an edit of it.
                    thread[index] = PullComment(held.id, body, held.mine, held.url, held.created)
                    return
        raise AssertionError(f'gh refuses to edit comment {comment}, which does not exist')


def pull_request(forge: GateMemoryForge, base: str, head: str) -> None:
    """Pull request `PULL` in `forge`, from `base` to `head`."""
    forge.requests[PULL] = PullRequest(
        PULL, 'docs: the notes', 'Refs Aetf/kluster-ops#917', 'OPEN', BRANCH, head, base, ()
    )


@dataclass
class Recorded:
    """A transport serving recorded Messages API bodies in turn, and every request it was sent."""

    bodies: list[dict[str, Any]]
    #: The status every call is answered with.
    status: int = 200
    #: How many requests, the first ones, are refused for the rate limit, each naming `retry_after`.
    limited: int = 0
    #: Whether a request takes the body of the turn its conversation is at, the replies it already holds,
    #: the last body for every turn past them, rather than the next body in turn.
    by_turn: bool = False
    #: How many requests that carry a tool's answer, the first ones, are refused for the rate limit.
    limited_rounds: int = 0
    #: The wait a refusal names, in seconds, as the API's `retry-after` header carries it.
    retry_after: str = '20'
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    headers: list[httpx2.Headers] = field(default_factory=list[httpx2.Headers])
    #: Every wait the client slept, in seconds, in order.
    slept: list[float] = field(default_factory=list[float])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _serve(self, request: httpx2.Request) -> httpx2.Response:
        sent = cast(dict[str, Any], json.loads(request.content))
        turn = sum(1 for m in cast(list[dict[str, Any]], sent['messages']) if m['role'] == 'assistant')
        with self._lock:
            limited = len(self.requests) < self.limited
            if turn and self.limited_rounds:
                self.limited_rounds -= 1
                limited = True
            served = self.bodies[
                min(turn, len(self.bodies) - 1) if self.by_turn else len(self.requests) % len(self.bodies)
            ]
            self.requests.append(sent)
            self.headers.append(request.headers)
        if limited:
            return httpx2.Response(429, headers={'retry-after': self.retry_after}, json=RATE_LIMITED)
        return httpx2.Response(self.status, json=served)

    def _sleep(self, seconds: float) -> None:
        with self._lock:
            self.slept.append(seconds)

    def model(self) -> Messages:
        """The real client, its transport this one, and a key that is no key; it retries no call, and sleeps on no wait."""
        return Messages.with_key(
            'not-a-key',
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self._serve)),
            retries=0,
            sleep=self._sleep,
        )


#: The body of a `429`, in the shape the API documents for a rate limit.
RATE_LIMITED: dict[str, Any] = {
    'type': 'error',
    'error': {
        'type': 'rate_limit_error',
        'message': 'This request would exceed the rate limit for your organization of 50,000 input tokens per minute.',
    },
}


def reply(findings: list[dict[str, object]]) -> dict[str, Any]:
    """A Messages API body whose text is a gate's reply holding `findings`.

    The schema requires each item's verdict, so an item that gives none is
    sent as one that judges its place to break the rule.
    """
    items = [{'breaks_rule': True, **item} for item in findings]
    return {
        'id': 'msg_stub',
        'type': 'message',
        'role': 'assistant',
        'model': 'claude-haiku-5-5',
        'content': [{'type': 'text', 'text': json.dumps({'findings': items})}],
        'stop_reason': 'end_turn',
        'stop_sequence': None,
        'usage': {'input_tokens': 412, 'output_tokens': 96},
    }


def tool_use(name: str, arguments: dict[str, object], cached: int = 0) -> dict[str, Any]:
    """A Messages API body whose reply calls the tool `name` with `arguments`, and stops for its answer.

    Its request wrote `cached` tokens of its prompt to the cache.
    """
    return {
        'id': 'msg_stub_tool',
        'type': 'message',
        'role': 'assistant',
        'model': 'claude-haiku-5-5',
        'content': [{'type': 'tool_use', 'id': f'toolu_{name}', 'name': name, 'input': arguments}],
        'stop_reason': 'tool_use',
        'stop_sequence': None,
        'usage': {'input_tokens': 412, 'output_tokens': 24, 'cache_creation_input_tokens': cached},
    }


def tree(repository: Path, head: str, base: str) -> Tree:
    """The tree a call's tools read in `repository`, at `head` and `base`."""
    return Tree(TreeGit(repository), head, base)


def recorded(messages: Path = STUB / 'messages') -> Recorded:
    """The recorded replies of `messages`, the stub gate's unless another gate's are named."""
    return Recorded([cast(dict[str, Any], json.loads(p.read_text())) for p in sorted(messages.iterdir())])
