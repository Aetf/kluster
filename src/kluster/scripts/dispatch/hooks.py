"""`dispatch-hook`: the Claude Code hooks that hold a session to the dispatch protocol.

    dispatch-hook agent | handback | pr-create | builder-bash | session-start

Each subcommand is the command of one hook entry: `.claude/settings.json`
holds the first three and `session-start`, the builder's agent definition
(`.claude/agents/kluster-builder.md`) holds `builder-bash`. Which call each
one holds, and what it refuses, is dispatch.md §2.2.

A `PreToolUse` hook reads the event Claude Code writes on standard input: the
tool's `tool_input`, and, inside a subagent, its `agent_type` (the Claude
Code documentation's hooks reference, "Common input fields" and "PreToolUse
input"). It answers a refusal with a `deny` decision on standard output,
whose reason Claude reads, and says nothing at all where the call may go
ahead, which leaves the call to the session's permissions: no hook here
allows anything. Every entry is `onFailure: block`, so a hook that cannot
start, or exits with anything but 0 or 2, refuses the call too; an exception
here exits 2 with its message, which reaches Claude as the reason. A hook
that crashes therefore stops every call it holds, the dispatcher's own
included, which is why dispatch.md §2.2 names the way to turn the hooks off.

`session-start` prints `dispatch audit`'s findings, which Claude Code adds to
the session's context ("SessionStart decision control"); a `SessionStart`
hook blocks nothing, so it reports a failure there rather than exiting with it.

Each check reads the forge through `HookForge` and the `jj` repository through
`Repository`, so the cases run against stand-ins and the real ones are
reached only from `HookGh` and `Jj`.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess as sp
import sys
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from ruamel.yaml import YAML, YAMLError

from kluster.lib import workstation

from . import audit, brief, ledger, prbody, record
from .forge import CODE, Forge, Gh

log = logging.getLogger(__name__)

#: The exit of a hook that ran: its decision, if any, is on standard output.
RAN = 0
#: The exit that blocks the call, with the reason on standard error (the hooks
#: reference, "Exit code 2").
BLOCK = 2

#: The agent definition of a builder (dispatch.md §1.3).
BUILDER = 'kluster-builder'
#: Where the role definitions the repository tracks sit, relative to its root.
AGENTS = Path('.claude', 'agents')

#: How long one `jj` call may take: a read of the store, never a snapshot.
JJ_TIMEOUT = 60


class EventUnreadable(ValueError):
    """Standard input does not hold the event this hook reads."""


class AgentsUnreadable(ValueError):
    """A role definition under `.claude/agents/` does not say which agent it defines."""


@dataclass(frozen=True)
class Event:
    """A `PreToolUse` event, as much of it as the hooks read."""

    tool_input: Mapping[str, object]
    #: The subagent's type; None in the main session.
    agent_type: str | None

    @classmethod
    def read(cls, text: str) -> Event:
        try:
            raw: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EventUnreadable(f'the hook input is not JSON: {exc}') from exc
        if not isinstance(raw, dict):
            raise EventUnreadable('the hook input is not a JSON object')
        fields = cast(dict[str, Any], raw)
        tool_input, agent_type = fields.get('tool_input'), fields.get('agent_type')
        if not isinstance(tool_input, dict):
            raise EventUnreadable('the hook input carries no `tool_input` object')
        if agent_type is not None and not isinstance(agent_type, str):
            raise EventUnreadable('the hook input carries an `agent_type` that is not a string')
        return cls(tool_input=cast(dict[str, object], tool_input), agent_type=agent_type)

    def text(self, key: str) -> str:
        """The string field `key` of the tool's input."""
        value = self.tool_input.get(key)
        if not isinstance(value, str):
            raise EventUnreadable(f'the tool input carries no string `{key}`')
        return value


@dataclass(frozen=True)
class Deny:
    """A refusal of the call, with the reason Claude reads."""

    reason: str

    def render(self) -> str:
        return json.dumps(
            {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'deny',
                    'permissionDecisionReason': self.reason,
                }
            }
        )


@dataclass(frozen=True)
class PullHead:
    """An open pull request of the code repository, as the report is checked against it."""

    number: int
    #: The branch it merges from.
    branch: str
    #: The full SHA of its head commit on the forge.
    head: str
    #: How many files it changes against its base.
    changed_files: int


class HookForge(Forge, Protocol):
    """The forge, with the one read only the hooks make."""

    def pull(self, number: int) -> PullHead: ...


@dataclass(frozen=True)
class HookGh(Gh):
    """`HookForge` through the `gh` on `PATH`."""

    def pull(self, number: int) -> PullHead:
        raw = cast(
            dict[str, Any],
            self._json(['pr', 'view', str(number), '--repo', CODE, '--json', 'headRefName,headRefOid,changedFiles']),
        )
        return PullHead(
            number=number,
            branch=str(raw['headRefName']),
            head=str(raw['headRefOid']),
            changed_files=int(raw['changedFiles']),
        )


class Repository(Protocol):
    """The `jj` repository every workspace of the checkout shares."""

    def workspaces(self) -> list[str]: ...

    def parents(self, workspace: str) -> list[str]:
        """The full commit ids of the parents of `workspace`'s working-copy commit: its `@-`."""
        ...


@dataclass(frozen=True)
class Jj:
    """`Repository` through the `jj` on `PATH`, run in the checkout at `root`.

    Every call passes `--ignore-working-copy`: a hook reads the store as the
    workspaces last left it, and never snapshots the operator's working copy
    into `@` as a side effect of a check.
    """

    root: Path

    def _run(self, *args: str) -> str:
        return sp.run(
            ['jj', '--ignore-working-copy', *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=JJ_TIMEOUT,
            check=True,
        ).stdout

    def workspaces(self) -> list[str]:
        return self._run('workspace', 'list', '-T', 'name ++ "\\n"').split()

    def parents(self, workspace: str) -> list[str]:
        name = workspace.replace('\\', '\\\\').replace('"', '\\"')
        return self._run('log', '-r', f'"{name}"@-', '--no-graph', '-T', 'commit_id ++ "\\n"').split()


# -- what a command runs ------------------------------------------------------

#: The characters that end a word without whitespace, as the shell reads them.
_PUNCTUATION = '();<>|&`'
#: The punctuation that ends a simple command: a list, a pipe, a subshell, a
#: command substitution. A redirection's `<` or `>` does not.
_ENDS_COMMAND = frozenset(';&|()`')
#: A command substitution inside a word: `$(…)`, or the backquoted form.
_SUBSTITUTION = re.compile(r'\$\(([^()]*)\)|`([^`]*)`')
#: The shells whose `-c` argument is a script.
_SHELLS = frozenset({'sh', 'bash', 'zsh', 'dash'})
#: A here-document's body, from the line after its `<<WORD` to the line
#: holding only `WORD`: data the shell hands a command, never a command.
_HEREDOC = re.compile(r'<<-?[ \t]*([\'"]?)(?P<word>[\w.-]+)\1[^\n]*\n.*?\n[ \t]*(?P=word)[ \t]*(?=\n|$)', re.S)


def simple_commands(command: str) -> list[list[str]] | None:
    """The words of each simple command in `command`; None where the shell's quoting does not read.

    Commands joined by a list operator, a pipe, a subshell or a command
    substitution come apart; here-document bodies are dropped first. A
    command substitution inside a quoted word, and the script a shell runs
    with `-c`, are read as commands too. Quoting is not tracked further than
    that, so a substitution in single quotes, which the shell leaves as text,
    is read as a command as well, and a newline outside quotes joins the
    commands either side of it into one: either way a check below sees more
    than the command runs, never less.
    """
    lexer = shlex.shlex(_HEREDOC.sub('', command), posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(_PUNCTUATION) and set(token) & _ENDS_COMMAND:
            commands.append([])
        else:
            commands[-1].append(token)
    found = [words for words in commands if words]
    for words in list(found):
        inner = [m[1] or m[2] for word in words for m in _SUBSTITUTION.finditer(word)]
        inner += [
            script
            for program, flag, script in zip(words, words[1:], words[2:], strict=False)
            if _program(program) in _SHELLS and flag == '-c'
        ]
        for script in inner:
            nested = simple_commands(script)
            if nested is None:
                return None
            found += nested
    return found


def _operands(words: Sequence[str], start: int, valued: Collection[str]) -> Iterator[str]:
    """The words after `words[start]` that are not options, skipping each value an option in `valued` takes."""
    skip = False
    for word in words[start + 1 :]:
        if skip:
            skip = False
        elif word.startswith('-'):
            skip = word in valued
        else:
            yield word


def _leads(words: Sequence[str], start: int, valued: Collection[str], *subcommand: str) -> bool:
    """Whether the operands after `words[start]` open with `subcommand`."""
    operands = _operands(words, start, valued)
    return all(next(operands, None) == word for word in subcommand)


#: The global options of `git` and `jj` that take a value as the next word.
_GIT_VALUED = frozenset({'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'})
_JJ_VALUED = frozenset({'-R', '--repository', '--at-op', '--at-operation', '--config', '--config-file', '--color'})
#: The `dispatch` subcommands that act as a dispatcher (dispatch.md §2.1).
_DISPATCHER_STEPS = ('claim', 'queue', 'abandon')
#: What a builder's command is refused for, when it cannot be read as words.
_UNREAD_BUILDER = re.compile(r'\bgit\s+(?:fetch|pull)\b|\bdispatch\s+(?:claim|queue|abandon)\b')


def _program(word: str) -> str:
    return PurePosixPath(word).name


def builder_refusals(command: str) -> list[str]:
    """What a builder's `command` is refused for: a fetch, or a dispatcher's step.

    A word is checked wherever it stands in a simple command, so a program
    behind a wrapper (`mise x -- jj git fetch`, `timeout 60 git fetch`) is
    found; the cost is a refusal of the rare command that only names one of
    these as an argument, which the builder words another way.
    """
    commands = simple_commands(command)
    if commands is None:
        return (
            [
                "the command does not read as shell words, and its text names a fetch or a dispatcher's step; "
                'run that as a plain command, or not at all'
            ]
            if _UNREAD_BUILDER.search(command)
            else []
        )
    refusals: set[str] = set()
    for words in commands:
        for index, word in enumerate(words):
            program = _program(word)
            fetches = (
                program == 'git' and any(_leads(words, index, _GIT_VALUED, sub) for sub in ('fetch', 'pull'))
            ) or (program == 'jj' and _leads(words, index, _JJ_VALUED, 'git', 'fetch'))
            if fetches:
                refusals.add(
                    'a builder never fetches: the dispatcher fetches, and brings the branch up to date at merge '
                    '(AGENTS.md, "Working beside other agents"; dispatch.md §1.2). A command that only names a '
                    'fetch in a quoted string, a commit message say, puts that text in a file instead'
                )
            if program == 'dispatch' and any(_leads(words, index, (), step) for step in _DISPATCHER_STEPS):
                refusals.add(
                    "`dispatch claim`, `queue` and `abandon` are the dispatcher's steps, and a builder takes none "
                    'of them (dispatch.md §1.3, §2.1); report instead'
                )
    return sorted(refusals)


# -- briefs and their records -------------------------------------------------

#: An absolute path to a markdown file, as a prompt or a report names a brief.
_MARKDOWN_PATH = re.compile(r'(?<![\w.~/-])/[^\s\'"`<>()\[\]*]+?\.md(?=$|[\s\'"`<>()\[\],;:.!?*])')


@dataclass(frozen=True)
class Named:
    """The briefs a text names, and why each other markdown path it names is none."""

    briefs: tuple[tuple[Path, brief.Brief], ...]
    others: tuple[str, ...]


def named_briefs(text: str) -> Named:
    """Every file `text` names by absolute path that opens with a brief's front matter."""
    briefs: dict[Path, brief.Brief] = {}
    others: list[str] = []
    for found in dict.fromkeys(_MARKDOWN_PATH.findall(text)):
        path = Path(found)
        try:
            briefs[path] = brief.read(path.read_bytes())
        except OSError as exc:
            others.append(f'{path}: {exc.strerror or exc}')
        except brief.BriefRefused as exc:
            others.append(f'{path}: {exc}')
    return Named(briefs=tuple(briefs.items()), others=tuple(others))


def _one_brief(text: str, what: str) -> tuple[Path, brief.Brief] | Deny:
    named = named_briefs(text)
    if len(named.briefs) == 1:
        return named.briefs[0]
    if named.briefs:
        paths = ', '.join(str(path) for path, _ in named.briefs)
        return Deny(f'{what} names more than one brief ({paths}); name only the one this dispatch is for')
    tried = f' Of the markdown files it names: {"; ".join(named.others)}.' if named.others else ''
    return Deny(
        f'{what} names no brief: a brief is named by its absolute path, and opens with its front matter.{tried}'
    )


def dispatch_record(forge: Forge, found: brief.Brief) -> record.Record | str:
    """The live record of the dispatch `found` is the brief of; why there is none, where there is none."""
    number = found.front.issue
    log.info('reading #%d and its records', number)
    issue = forge.issue(number)
    claim_ = f'`dispatch claim {number} <brief>`'
    if not ledger.claimed(issue):
        return f'#{number}, the issue the brief names, carries no claim: {claim_} is the dispatch (dispatch.md §2.1)'
    try:
        live = record.claim(number, forge.comments(number))
    except record.RecordUnreadable as exc:
        return f'the records of #{number} do not read: {exc}'
    match = next((r for r in reversed(live.records) if r.brief == found.sha256), None) if live else None
    if match is None:
        return (
            f'no live dispatch record on #{number} is for this brief (sha256 {found.sha256}): '
            f'{claim_} records it, and a brief edited after its claim is claimed again'
        )
    return match


# -- the hooks ----------------------------------------------------------------


def repository_agents(root: Path) -> frozenset[str]:
    """The names of the role definitions the checkout at `root` tracks under `.claude/agents/`."""
    names: set[str] = set()
    for path in sorted((root / AGENTS).glob('*.md')):
        match = re.match(r'\A---\n(?P<block>.*?)\n---\n', path.read_text(), re.S)
        if match is None:
            raise AgentsUnreadable(f'{path} opens with no front matter, so its agent cannot be named')
        try:
            raw: object = YAML(typ='safe', pure=True).load(match['block'])  # pyright: ignore[reportUnknownMemberType] -- ruamel's `load` is untyped
        except YAMLError as exc:
            raise AgentsUnreadable(f'the front matter of {path} is not YAML: {exc}') from exc
        name = cast(dict[str, object], raw).get('name') if isinstance(raw, dict) else None
        if not isinstance(name, str):
            raise AgentsUnreadable(f'the front matter of {path} names no agent')
        names.add(name)
    return frozenset(names)


def agent(event: Event, forge: HookForge, agents: Collection[str]) -> Deny | None:
    """An `Agent` call spawning one of `agents` goes ahead only under its brief's dispatch record."""
    kind = event.tool_input.get('subagent_type')
    if kind not in agents:
        return None
    found = _one_brief(event.text('prompt'), f'The prompt for a `{kind}`')
    if isinstance(found, Deny):
        return found
    _, brief_ = found
    held = dispatch_record(forge, brief_)
    return Deny(f'This `{kind}` is spawned under no dispatch: {held}') if isinstance(held, str) else None


#: What a report that stops to ask opens its first line with (dispatch.md §2.2).
STOPPED = 'Stopped:'
_STOPPED = re.compile(rf'\A\s*{re.escape(STOPPED)}')
#: A pull request of the code repository, by URL.
_PULL_URL = re.compile(rf'https://github\.com/{re.escape(CODE)}/pull/(?P<number>\d+)\b')
#: A full commit SHA, standing alone.
_FULL_SHA = re.compile(r'(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])')


def handback(event: Event, forge: HookForge, repository: Repository) -> Deny | None:
    """A builder's report goes back only naming its brief, and a pull request whose head is the work.

    The report names the brief it was dispatched with. A report whose first
    line opens `Stopped:` goes back on that alone: it stops to ask, and its
    workspace stands until the dispatcher answers. Otherwise, with a pull
    request, it names it by URL and its full head SHA, which is the forge's
    head and the workspace's `@-`, and the pull request changes something
    against its base; with none, the workspace is gone (dispatch.md §2.2, §4).
    """
    if event.agent_type != BUILDER:
        return None
    message = event.text('message')
    found = _one_brief(message, 'The report')
    if isinstance(found, Deny):
        return found
    _, brief_ = found
    held = dispatch_record(forge, brief_)
    if isinstance(held, str):
        return Deny(f'The report is for no dispatch: {held}')
    if _STOPPED.match(message):
        return None
    front = brief_.front
    numbers = sorted({int(n) for n in _PULL_URL.findall(message)})
    if not numbers:
        if front.workspace is not None and front.workspace in repository.workspaces():
            return Deny(
                f'The report names no pull request, and the workspace {front.workspace} still stands. A '
                f'report that stops to ask opens its first line with `{STOPPED}` and keeps the workspace; a '
                'finished one with no pull request forgets the workspace from the primary and removes its '
                'directory, or names the pull request by its URL (AGENTS.md, "Working beside other agents")'
            )
        return None
    log.info('reading the pull requests the report names')
    pulls = [forge.pull(number) for number in numbers]
    pull = next((p for p in pulls if p.branch == front.branch), None)
    if pull is None:
        named = ', '.join(f'#{p.number} on {p.branch}' for p in pulls)
        return Deny(f"The report names no pull request on the brief's branch {front.branch}: it names {named}")
    problems: list[str] = []
    reported = _FULL_SHA.findall(message)
    if pull.head not in reported:
        given = f'it gives {", ".join(reported)}' if reported else 'it gives no full 40-character SHA'
        problems.append(
            f"it does not name #{pull.number}'s head on the forge, {pull.head}, in full: {given}. The head is "
            f'read after the push with `jj log -r {pull.branch} --no-graph -T commit_id`'
        )
    if pull.changed_files == 0:
        problems.append(
            f'#{pull.number} changes no file against its base: the work was left in `@` when the bookmark was '
            'set, so the branch holds none of it'
        )
    if front.workspace is not None:
        parents = repository.parents(front.workspace) if front.workspace in repository.workspaces() else None
        if parents is None:
            problems.append(f'the workspace {front.workspace} does not stand, and it stays until the merge')
        elif pull.head not in parents:
            problems.append(
                f"the workspace {front.workspace}'s `@-` is {', '.join(parents)}, not the pushed head: the "
                'bookmark was not set on the work, or `@-` moved after the push'
            )
    if problems:
        return Deny('The report does not hold: ' + '; '.join(problems) + ' (dispatch.md §4; AGENTS.md)')
    return None


#: The `gh` options that take a value as the next word, by their short and long names.
_GH_SHORT = {
    '-B': '--base',
    '-b': '--body',
    '-F': '--body-file',
    '-H': '--head',
    '-t': '--title',
    '-R': '--repo',
    '-l': '--label',
    '-a': '--assignee',
    '-r': '--reviewer',
    '-m': '--milestone',
    '-p': '--project',
    '-T': '--template',
}
_GH_VALUED = frozenset({*_GH_SHORT, *_GH_SHORT.values(), '--recover'})
#: A command that names `gh pr create` in text the shell's quoting does not read.
_UNREAD_PR_CREATE = re.compile(r'\bgh\b.*\bpr\s+create\b', re.S)


def _gh_options(words: Sequence[str]) -> dict[str, str]:
    """The value of each valued option in `words`, by its long name; the first, where one repeats."""
    options: dict[str, str] = {}
    index = 0
    while index < len(words):
        word = words[index]
        name, value = word, None
        if word.startswith('--') and '=' in word:
            name, value = word.split('=', 1)
        elif word[:2] in _GH_SHORT and len(word) > 2 and not word.startswith('--'):
            name, value = word[:2], word[2:]
        name = _GH_SHORT.get(name, name)
        if value is None and name in _GH_VALUED and index + 1 < len(words):
            index += 1
            value = words[index]
        if value is not None:
            _ = options.setdefault(name, value)
        index += 1
    return options


def _pr_creates(command: str) -> list[list[str]] | None:
    """The words after `gh` of each `gh pr create` in `command`; None where the command does not read."""
    commands = simple_commands(command)
    if commands is None:
        return None
    return [
        words[index + 1 :]
        for words in commands
        for index, word in enumerate(words)
        if _program(word) == 'gh' and _leads(words, index, _GH_VALUED, 'pr', 'create')
    ]


def _branch_record(forge: Forge, branch: str) -> record.Front | str:
    """The front matter of the live record that names `branch`; why there is none, where there is none."""
    log.info('reading every live claim for the record of %s', branch)
    claims = ledger.claims(forge)
    fronts = {
        r.front.issue: r.front
        for c in claims.live.values()
        if c is not None
        for r in c.records
        if r.front is not None and r.front.fields.get('branch') == branch
    }
    if len(fronts) > 1:
        return f'the live records of {", ".join(f"#{n}" for n in sorted(fronts))} all name branch {branch}'
    if not fronts:
        unread = f' (the records of {", ".join(f"#{n}" for n in sorted(claims.unreadable))} do not read)'
        return (
            f'no live dispatch record names branch {branch}'
            + (unread if claims.unreadable else '')
            + ': the work is claimed with `dispatch claim` before its pull request opens'
        )
    return next(iter(fronts.values()))


def pr_create(event: Event, forge: HookForge) -> Deny | None:
    """A `gh pr create` on the code repository goes ahead only with a description of the form `prbody` reads."""
    command = event.text('command')
    calls = _pr_creates(command)
    if calls is None:
        if _UNREAD_PR_CREATE.search(command):
            return Deny(
                'The command names `gh pr create` and does not read as shell words; '
                'run `gh pr create` as a plain command'
            )
        return None
    for words in calls:
        options = _gh_options(words)
        repo = options.get('--repo')
        if repo is None:
            return Deny(f'`gh pr create` names no repository: pass `--repo {CODE}`')
        if repo.lower() != CODE.lower():
            continue
        head = options.get('--head')
        if head is None:
            return Deny('`gh pr create` names no branch: pass `--head <branch>`, the one the brief names')
        branch = head.rsplit(':', 1)[-1]
        if '--body' in options and _SUBSTITUTION.search(options['--body']):
            return Deny(
                'The description is computed by the shell, which this hook cannot read: pass `--body-file <file>`'
            )
        if '--body' in options:
            body = options['--body']
        elif options.get('--body-file', '-') == '-':
            return Deny('`gh pr create` takes its description from a file here: pass `--body-file <file>`')
        else:
            path = Path(options['--body-file']).expanduser()
            if not path.is_absolute():
                return Deny(
                    f'`--body-file` takes an absolute path here, since the hook cannot tell which directory '
                    f'the command runs in: {path} is relative'
                )
            try:
                body = path.read_text()
            except OSError as exc:
                return Deny(f'The description file {path} does not read: {exc.strerror or exc}')
        front = _branch_record(forge, branch)
        if isinstance(front, str):
            return Deny(f'This pull request belongs to no dispatch: {front}')
        closes = front.fields.get('closes')
        if not isinstance(closes, bool):
            return Deny(f'The record of #{front.issue} for branch {branch} says nothing of `closes`')
        problems = prbody.lint(body, issue=front.issue, closes=closes)
        if problems:
            return Deny('The description does not hold its form: ' + '; '.join(problems) + ' (dispatch.md §1.1)')
    return None


def builder_bash(event: Event) -> Deny | None:
    """A builder's command goes ahead unless it fetches or takes a dispatcher's step."""
    refusals = builder_refusals(event.text('command'))
    return Deny('; '.join(refusals)) if refusals else None


def session_start(forge: Forge, repository: Repository) -> str:
    """The context a session starts with: `dispatch audit`'s findings, or why it could not run."""
    try:
        findings = audit.audit(forge, repository.workspaces())
    except Exception as exc:  # noqa: BLE001 -- the audit only informs; a session starts whatever it hit
        return f'`dispatch audit` could not run at session start: {type(exc).__name__}: {exc}'
    if not findings:
        return '`dispatch audit` at session start: the ledger shows no anomaly.'
    lines = [f'`dispatch audit` at session start, {len(findings)} findings (dispatch.md §2.1):']
    return '\n'.join([*lines, *(f'- {finding}' for finding in findings)])


#: Each `PreToolUse` hook, by its subcommand: what it decides from the event.
_PRE_TOOL_USE: Mapping[str, Callable[[Event, Path], Deny | None]] = {
    'agent': lambda event, root: agent(event, HookGh(), repository_agents(root)),
    'handback': lambda event, root: handback(event, HookGh(), Jj(root)),
    'pr-create': lambda event, _: pr_create(event, HookGh()),
    'builder-bash': lambda event, _: builder_bash(event),
}
SESSION_START = 'session-start'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='dispatch-hook',
        description='The Claude Code hooks of dispatch.md §2.2. Reads the hook event on standard input.',
    )
    _ = parser.add_argument('hook', choices=[*_PRE_TOOL_USE, SESSION_START])
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s', stream=sys.stderr)
    args = build_parser().parse_args(argv)
    root = workstation.repo_root()
    try:
        if args.hook == SESSION_START:
            print(session_start(HookGh(), Jj(root)))
            return RAN
        decision = _PRE_TOOL_USE[args.hook](Event.read(sys.stdin.read()), root)
    except Exception as exc:  # noqa: BLE001 -- a hook fails closed: whatever it hit refuses the call
        print(f'dispatch-hook {args.hook} could not decide, so the call is refused: {exc}', file=sys.stderr)
        return BLOCK
    if decision is not None:
        print(decision.render())
    return RAN
