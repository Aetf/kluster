"""The stub gate the model-gate cases run, and what it runs against: a scratch repository, a forge, recorded replies.

Its own module because the harness, calibration and harvest cases all run the
same gate, and a test module is not imported by another
(`tests/test_suite_imports.py`). The gate is `tests/data/gates/stub/`, copied
into the case's own directory so that a record a case writes stays its own;
it is shown each file a change writes under `docs/`, its question is about
the lines a change writes into a note, and its canon is a section of
`docs/rule.md` in the scratch repository.

A real gate's extractor cases read a recorded change (`real_change`):
excerpts of a pull request's files at its base and its head, under
`tests/data/gates/<gate>/`.

The replies are Messages API response bodies (`tests/data/gates/stub/messages/`)
in the shape the API documents, served by a transport handed to the real
client, so a run builds its requests and reads its replies through the SDK
with no network and no key. Each call takes the next body in turn. A case
that needs other findings builds the body with `reply`.
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
from kluster.scripts.dispatch.gates.extract import Change, Item
from kluster.scripts.dispatch.gates.forge import PullComment
from kluster.scripts.dispatch.gates.gate import Canon, Gate, Spec
from kluster.scripts.dispatch.gates.model import Messages
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


def extract(change: Change) -> list[Item]:
    """The stub gate's extractor: every line the change writes under `docs/notes/`."""
    return list(change.written('docs/notes/**'))


#: The stub gate's declaration.
SPEC = Spec(canon=Canon(RULE_DOCUMENT, RULE_SECTION), trigger=('docs/**',), extract=extract)


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


#: The suffix a recorded file carries, so no check of this repository reads an excerpt of another commit as its own.
EXCERPT = '.excerpt'


def files(directory: Path) -> dict[str, str]:
    """Every recorded file under `directory`, by its path relative to it with `EXCERPT` cut; none where none is."""
    if not directory.is_dir():
        return {}
    return {
        str(f.relative_to(directory)).removesuffix(EXCERPT): f.read_text()
        for f in sorted(directory.rglob(f'*{EXCERPT}'))
    }


def real_change(
    repo: Path, fixture: Path, *, beside: Mapping[str, str] | None = None, reverse: bool = False
) -> tuple[str, str]:
    """Commit a recorded change in `repo`, and return its base and its head.

    `fixture` holds excerpts of a pull request's files, each named for its
    path with `EXCERPT` after it: `base/` as the base has them and `head/` as
    its head does, a file under `base/` alone being one the change deletes;
    `tree/`, files the change leaves as they are. `beside` adds files to both
    sides, and `reverse` commits the change undone: `head/` as the base and
    `base/` as the head.
    """
    old, new = ('head', 'base') if reverse else ('base', 'head')
    unchanged = {**(beside or {}), **files(fixture / 'tree')}
    at_base = {**unchanged, **files(fixture / old)}
    # A change that only adds files has a base with none in it.
    _ = git(repo, 'commit', '-q', '--allow-empty', '-m', 'the base')
    base = commit(repo, at_base) if at_base else git(repo, 'rev-parse', 'HEAD').strip()
    head_files = files(fixture / new)
    for gone in at_base.keys() - unchanged.keys() - head_files.keys():
        (repo / gone).unlink()
    return base, commit(repo, head_files or unchanged)


@dataclass
class GateMemoryForge(MemoryForge):
    """`memory_forge`'s forge, with each pull request read whole and its comments by id."""

    requests: dict[int, PullRequest] = field(default_factory=dict[int, PullRequest])
    threads: dict[int, list[PullComment]] = field(default_factory=dict[int, list[PullComment]])

    def pull_request(self, number: int) -> PullRequest:
        return self.requests[number]

    def pull_comments(self, number: int) -> list[PullComment]:
        return list(self.threads.get(number, []))

    def say(self, number: int, body: str, *, mine: bool = True) -> PullComment:
        """A comment on pull request `number`, by this account or, with `mine` false, by another."""
        identity = sum(len(t) for t in self.threads.values()) + 1
        said = PullComment(identity, body, mine, f'https://github.com/{CODE}/pull/{number}#issuecomment-{identity}')
        self.threads.setdefault(number, []).append(said)
        return said

    def post(self, number: int, body: str) -> None:
        _ = self.say(number, body)

    def edit(self, comment: int, body: str) -> None:
        for thread in self.threads.values():
            for index, held in enumerate(thread):
                if held.id == comment:
                    thread[index] = PullComment(held.id, body, held.mine, held.url)
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
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    headers: list[httpx2.Headers] = field(default_factory=list[httpx2.Headers])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _serve(self, request: httpx2.Request) -> httpx2.Response:
        with self._lock:
            served = self.bodies[len(self.requests) % len(self.bodies)]
            self.requests.append(cast(dict[str, Any], json.loads(request.content)))
            self.headers.append(request.headers)
        return httpx2.Response(self.status, json=served)

    def model(self) -> Messages:
        """The real client, its transport this one, and a key that is no key; it retries no call."""
        return Messages.with_key(
            'not-a-key',
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self._serve)),
            retries=0,
        )


def reply(findings: list[dict[str, object]]) -> dict[str, Any]:
    """A Messages API body whose text is a gate's reply holding `findings`."""
    return {
        'id': 'msg_stub',
        'type': 'message',
        'role': 'assistant',
        'model': 'claude-haiku-5-5',
        'content': [{'type': 'text', 'text': json.dumps({'findings': findings})}],
        'stop_reason': 'end_turn',
        'stop_sequence': None,
        'usage': {'input_tokens': 412, 'output_tokens': 96},
    }


def recorded(messages: Path = STUB / 'messages') -> Recorded:
    """The recorded replies of `messages`, the stub gate's unless another gate's are named."""
    return Recorded([cast(dict[str, Any], json.loads(p.read_text())) for p in sorted(messages.iterdir())])
