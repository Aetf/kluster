"""`dispatch merge`: a pull request lands through the dispatch that owns it, and the dispatch ends with it.

`check` reads the forge and returns every precondition the merge fails, so a
refused merge reports all of them at once and has changed nothing: the pull
request belongs to a live builder record this session holds; its title leads
with the record's RFC slice; its description holds the form of dispatch.md
§1.1 for the record's `closes`; a review dispatched under the claim left a
comment naming the head the builder reported; and the forge's head is that
reported head, or the dispatcher's own rebase of it (§2 rule 8), compared
before anything rewrites the branch.

`merge` then lands it as §2 rule 4 and github.md §3 have it, at most
`ATTEMPTS` times while `main` moves under it: fetch, compare again, rebase
onto the forge's `main`, push, wait for the required checks, merge with the
pushed head as the one the forge must still hold. `jj git push` refuses a
branch the forge moved since the fetch, so the push replaces only the head the
comparison read. A conflict, a red check, a merge state other than `main`
moving, or a wait that runs out stops it with nothing merged; a stop after the
push leaves the forge's head as the dispatcher's own rebase, which a second
run takes up. Once merged it ends the dispatch: each issue the description
closes and the merge left open closed, or, where the record leaves its issue
open, a comment saying what remains; the `end` record; the claim labels off;
every workspace the claim's records name forgotten and removed. Each of those
steps is skipped where it is done already, so a close-out that stopped part
way is finished by running `merge` again.

The waits are bounded (`Wait`), and a wait that runs out is a stop-loss,
never a verdict on the checks.
"""

from __future__ import annotations

import enum
import json
import logging
import re
import shutil
import subprocess as sp
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from . import ledger, prbody
from .brief import Role
from .forge import CODE, LEDGER, TIMEOUT, ForgeError
from .prcheck import Dispatched, PullForge, PullGh, PullRequest, dispatched
from .record import Event, Record

log = logging.getLogger(__name__)

#: How many times `merge` goes round while `main` moves under the pull request.
ATTEMPTS = 3
#: The branch every pull request here merges into, whose protection names the required checks.
MAIN = 'main'

#: A full commit SHA.
FULL_SHA = re.compile(r'[0-9a-f]{40}')
#: The fewest leading characters of a head a review may name it by.
SHORT_SHA = 12
#: A review comment's first line: a heading whose text opens with `Review`.
_REVIEW_HEADING = re.compile(r'\A#{1,6}[ \t]+Review\b')
#: A run of hex digits standing alone, which is how a review names a head.
_HEX = re.compile(r'(?<![0-9A-Za-z])[0-9a-f]{12,40}(?![0-9A-Za-z])')
#: What the comment on an issue left open says before what remains.
REMAINS = 'What remains:'

#: The merge states the forge reports once it has worked one out.
BEHIND = 'BEHIND'
BLOCKED = 'BLOCKED'
UNKNOWN = 'UNKNOWN'
MERGED = 'MERGED'


class Precondition(enum.StrEnum):
    """A precondition of a merge, by what fails it."""

    #: The pull request is closed without a merge.
    CLOSED = 'closed'
    #: A live claim another dispatcher session holds (§2 rule 4).
    NOT_HELD = 'not-held'
    #: The title does not lead with the record's RFC slice (rfc.md §3.4).
    RFC_TITLE = 'rfc-title'
    #: The description does not hold its form (§1.1).
    DESCRIPTION = 'description'
    #: The reported head is not a full SHA, or the forge's head is neither it
    #: nor the dispatcher's own rebase of it (§2 rule 8).
    HEAD = 'head'
    #: No review dispatched under the claim names the reported head (§3).
    REVIEW = 'review'
    #: A record that leaves its issue open, and no word of what remains (§4).
    REMAINS = 'remains'


@dataclass(frozen=True)
class Refusal:
    precondition: Precondition
    detail: str

    def __str__(self) -> str:
        return f'{self.precondition.value}: {self.detail}'


class Refused(Exception):
    """The merge fails a precondition; nothing on the forge or in the repository was changed."""

    def __init__(self, refusals: Sequence[Refusal]) -> None:
        super().__init__('\n'.join(map(str, refusals)))
        self.refusals = tuple(refusals)


class Stopped(Exception):
    """The merge stopped part way: before the merge with nothing merged, or in the close-out after it."""


class Bucket(enum.StrEnum):
    """Where `gh pr checks` puts a check."""

    PASS = 'pass'
    FAIL = 'fail'
    PENDING = 'pending'
    SKIPPING = 'skipping'
    CANCEL = 'cancel'


@dataclass(frozen=True)
class Check:
    name: str
    bucket: Bucket


class MergeForge(PullForge, Protocol):
    """The forge, with what landing a pull request takes."""

    def required_checks(self, number: int) -> list[Check]:
        """The pull request's required checks reported at its head; empty while none is."""
        ...

    def required_contexts(self) -> frozenset[str]:
        """The status contexts `MAIN`'s protection requires, reported or not."""
        ...

    def merge(self, number: int, head: str) -> None:
        """Merge the pull request, provided the forge still holds `head` as its head."""
        ...

    def close(self, number: int, comment: str) -> None:
        """Close a ledger issue, with a comment."""
        ...


@dataclass(frozen=True)
class MergeGh(PullGh):
    """`MergeForge` through the `gh` on `PATH`."""

    def required_checks(self, number: int) -> list[Check]:
        args = ['pr', 'checks', str(number), '--repo', CODE, '--required', '--json', 'name,bucket']
        try:
            done = sp.run(['gh', *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)
        except (OSError, sp.TimeoutExpired) as exc:
            raise ForgeError(f'`gh {" ".join(args)}` did not run: {exc}') from exc
        # The JSON is read whatever the exit status, which is not a verdict on
        # the checks; a run that prints none is no check reported only where
        # its error says so.
        try:
            raw = cast(list[dict[str, Any]], json.loads(done.stdout))
        except json.JSONDecodeError:
            if 'no ' in done.stderr and 'checks reported' in done.stderr:
                return []
            raise ForgeError(f'`gh {" ".join(args)}` failed: {done.stderr.strip() or done.returncode}') from None
        try:
            return [Check(name=str(c['name']), bucket=Bucket(c['bucket'])) for c in raw]
        except (KeyError, ValueError) as exc:
            raise ForgeError(f'`gh {" ".join(args)}` answered in a shape this module does not read: {exc}') from exc

    def required_contexts(self) -> frozenset[str]:
        raw = cast(
            dict[str, Any], self._json(['api', f'repos/{CODE}/branches/{MAIN}/protection/required_status_checks'])
        )
        return frozenset(str(context) for context in cast(list[object], raw['contexts']))

    def merge(self, number: int, head: str) -> None:
        _ = self._run(['pr', 'merge', str(number), '--repo', CODE, '--rebase', '--match-head-commit', head])

    def close(self, number: int, comment: str) -> None:
        _ = self._run(['issue', 'close', str(number), '--repo', LEDGER, '--reason', 'completed', '--comment', comment])


class Repository(Protocol):
    """The `jj` repository the dispatcher's checkout and every workspace share."""

    #: The root of the checkout the dispatcher runs in.
    @property
    def root(self) -> Path: ...

    def fetch(self) -> None:
        """Fetch from the forge, once the commits `main` does not contain are logged (dispatch.md §1.2)."""
        ...

    def commit(self, revision: str) -> str | None:
        """The full id of the one commit `revision` names; None where it names none, or several."""
        ...

    def rebase_of(self, reported: str, head: str) -> bool:
        """Whether `head` is `reported`'s change carried forward along the forge's `main`, or folded.

        A commit's fork point is where it leaves `main@origin`. `head`'s is
        `reported`'s or a descendant of it, and `head`'s tree is exactly what
        a three-way merge of `reported`'s change onto `head`'s fork point
        gives: a rebase onto a later `main`, a fold of the branch's commits
        into fewer, and a rebase whose hunks `main` already carries are all
        that, and a change to a single byte of what was reported is not. A
        merge that conflicts, and a commit the store does not hold, are no
        rebase.
        """
        ...

    def rebase(self, branch: str) -> None:
        """Rebase `branch` onto the forge's `main`."""
        ...

    def conflicted(self, branch: str) -> bool:
        """Whether a commit of `branch` above the forge's `main` holds a conflict."""
        ...

    def push(self, branch: str) -> None: ...

    def workspaces(self) -> list[str]: ...

    def workspace_root(self, name: str) -> Path: ...

    def forget(self, name: str) -> None: ...


def _symbol(name: str) -> str:
    """`name` as a `jj` revset string literal, which no character in a name can break out of."""
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


@dataclass(frozen=True)
class Jj:
    """`Repository` through the `jj` and `git` on `PATH`, run in the checkout at `root`, which `jj` colocates."""

    root: Path
    #: How long one call may take; a fetch or a push is network work.
    timeout: float = 300

    def _run(self, *args: str, program: str = 'jj', stdin: str | None = None) -> str:
        try:
            done = sp.run(
                [program, *args],
                cwd=self.root,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, sp.TimeoutExpired) as exc:
            raise ForgeError(f'`{program} {" ".join(args)}` did not run: {exc}') from exc
        if done.returncode != 0:
            raise ForgeError(f'`{program} {" ".join(args)}` failed: {done.stderr.strip() or done.returncode}')
        return done.stdout

    def fetch(self) -> None:
        unmerged = self._run('log', '-r', 'mutable() & ~::main & ~empty()', '--no-graph', '-T', 'builtin_log_oneline')
        log.info('the commits main does not contain, before the fetch:\n%s', unmerged.rstrip() or '  none')
        _ = self._run('git', 'fetch')

    def commit(self, revision: str) -> str | None:
        try:
            ids = self._run('log', '-r', revision, '--no-graph', '-T', 'commit_id ++ "\\n"').split()
        except ForgeError:
            return None
        return ids[0] if len(ids) == 1 else None

    def _fork(self, commit: str) -> str | None:
        """Where `commit` leaves the forge's `main`; None where it is on `main` itself, or not in the store."""
        fork = self.commit(f'heads(::{commit} & ::main@origin)')
        return fork if fork is not None and fork != commit else None

    def rebase_of(self, reported: str, head: str) -> bool:
        reported_fork, head_fork = self._fork(reported), self._fork(head)
        if reported_fork is None or head_fork is None or self.commit(f'{reported_fork} & ::{head_fork}') is None:
            return False
        try:
            # It exits 1 on a conflict, which no rebase of the reported change is.
            merged = self._run(
                'merge-tree', '--write-tree', f'--merge-base={reported_fork}', head_fork, reported, program='git'
            )
            tree = self._run('rev-parse', f'{head}^{{tree}}', program='git')
        except ForgeError:
            return False
        return merged.split('\n', 1)[0].strip() == tree.strip()

    def rebase(self, branch: str) -> None:
        _ = self._run('rebase', '-b', _symbol(branch), '-d', 'main@origin')

    def conflicted(self, branch: str) -> bool:
        revset = f'main@origin..{_symbol(branch)}'
        return 'conflict' in self._run('log', '-r', revset, '--no-graph', '-T', 'if(conflict, "conflict\\n")')

    def push(self, branch: str) -> None:
        _ = self._run('git', 'push', '--bookmark', branch)

    def workspaces(self) -> list[str]:
        return self._run('workspace', 'list', '-T', 'name ++ "\\n"').split()

    def workspace_root(self, name: str) -> Path:
        return Path(self._run('workspace', 'root', '--name', name).strip())

    def forget(self, name: str) -> None:
        _ = self._run('workspace', 'forget', name)


@dataclass(frozen=True)
class Wait:
    """How `merge` waits on the forge: a bound on each wait, and the pause between two reads."""

    #: For the forge to report the pushed head, or a merge state, which takes seconds.
    head: float = 5 * 60
    #: For the required checks, which take ten to twenty minutes.
    checks: float = 60 * 60
    poll: float = 20
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)


def _review(dispatch: Dispatched, pull: PullRequest, head: str) -> Refusal | None:
    """The refusal when no review dispatched under the claim names `head` on the pull request."""
    reviews = [
        r
        for r in dispatch.claim.records
        if r.front is not None and r.front.role is Role.REVIEW and r.front.fields.get('pr') == pull.number
    ]
    if not reviews:
        return Refusal(
            Precondition.REVIEW,
            f'no review of #{pull.number} is dispatched under the claim of #{dispatch.issue.number} (§3)',
        )
    named = any(
        _REVIEW_HEADING.match(comment.body.lstrip())
        and any(head.startswith(sha) for sha in _HEX.findall(comment.body) if len(sha) >= SHORT_SHA)
        for comment in pull.comments
        if comment.mine
    )
    if not named:
        return Refusal(
            Precondition.REVIEW,
            f'no review comment on #{pull.number}, a heading `## Review …` first, names the reported head {head} '
            f'by its first {SHORT_SHA} characters or more (§3)',
        )
    return None


@dataclass(frozen=True)
class Checked:
    """What `check` read: the pull request, its dispatch, the head the forge holds that may land, the refusals."""

    pull: PullRequest
    dispatch: Dispatched
    #: The reported head, or the dispatcher's rebase of it the forge holds.
    head: str
    refusals: tuple[Refusal, ...]


def check(
    forge: MergeForge, repository: Repository, number: int, reported: str, session: str, remains: str | None
) -> Checked:
    """The pull request, its dispatch, and every precondition its merge fails. Raises `PullUnreadable`."""
    log.info('reading #%d', number)
    pull = forge.pull_request(number)
    merged = pull.state == MERGED
    dispatch = dispatched(forge, pull, ended=merged)
    refusals: list[Refusal] = []
    if pull.state == 'CLOSED':
        refusals.append(Refusal(Precondition.CLOSED, f'#{number} is closed without a merge'))
    live = dispatch.claim.live
    if live.session != session:
        refusals.append(
            Refusal(Precondition.NOT_HELD, f'#{dispatch.issue.number} is claimed by dispatcher session {live.session}')
        )
    closes = dispatch.closes
    if closes is None:
        refusals.append(
            Refusal(Precondition.DESCRIPTION, f'the record of #{dispatch.issue.number} says nothing of `closes`')
        )
    if closes is False and not (remains and remains.strip()):
        refusals.append(
            Refusal(
                Precondition.REMAINS,
                f'the record leaves #{dispatch.issue.number} open, and its comment says what remains: '
                "pass `--remains '<what remains>'`",
            )
        )
    if merged:
        return Checked(pull, dispatch, pull.head, tuple(refusals))
    rfc = dispatch.front.fields.get('rfc')
    if isinstance(rfc, str) and not pull.title.startswith(f'{rfc}:'):
        refusals.append(Refusal(Precondition.RFC_TITLE, f'the title of #{number} does not lead with `{rfc}:`'))
    if closes is not None:
        refusals += [
            Refusal(Precondition.DESCRIPTION, problem)
            for problem in prbody.lint(pull.body, issue=dispatch.issue.number, closes=closes)
        ]
    if not FULL_SHA.fullmatch(reported):
        refusals.append(Refusal(Precondition.HEAD, f'{reported!r} is not a full 40-character SHA'))
    elif pull.head != reported:
        if repository.rebase_of(reported, pull.head):
            log.info(
                "#%d's head %s is this checkout's rebase of the reported %s", number, pull.head[:12], reported[:12]
            )
        else:
            refusals.append(
                Refusal(
                    Precondition.HEAD,
                    f"#{number}'s head on the forge is {pull.head}, neither the reported {reported} nor a rebase of "
                    "it onto main: someone else moved the branch (§2 rule 8), and the forge's timeline names when",
                )
            )
    review = _review(dispatch, pull, reported)
    if review is not None:
        refusals.append(review)
    return Checked(pull, dispatch, pull.head, tuple(refusals))


def _wait(what: str, deadline: float, wait: Wait, done: Callable[[], bool]) -> None:
    """Read `done` until it holds or `deadline` passes, when it raises `Stopped`.

    A read the forge fails is logged and read again: one failed call is no
    answer to what is waited for.
    """
    while True:
        try:
            if done():
                return
        except ForgeError as exc:
            log.warning('reading %s failed, reading again: %s', what, exc)
        if wait.clock() >= deadline:
            raise Stopped(f'the wait for {what} ran out; nothing was merged')
        wait.sleep(wait.poll)


def _checks(forge: MergeForge, number: int, head: str, wait: Wait, deadline: float) -> list[Check]:
    """Wait for the reported required checks at `head` to finish, and return them; `Stopped` on any that failed."""
    log.info(
        "waiting for #%d's required checks at %s (usually ten to twenty minutes; giving up after %.0f minutes)",
        number,
        head[:12],
        max(0.0, deadline - wait.clock()) / 60,
    )
    seen: list[Check] = []

    def finished() -> bool:
        checks = forge.required_checks(number)
        if checks != seen:
            seen[:] = checks
            log.info('  %s', ', '.join(f'{c.name}: {c.bucket}' for c in checks) or 'none reported yet')
        return bool(checks) and all(c.bucket is not Bucket.PENDING for c in checks)

    _wait(f"#{number}'s required checks", deadline, wait, finished)
    red = [c for c in seen if c.bucket in (Bucket.FAIL, Bucket.CANCEL)]
    if red:
        raise Stopped(f"#{number}'s required checks at {head}: {', '.join(f'{c.name} {c.bucket}' for c in red)}")
    return seen


def _reports(forge: MergeForge, number: int, head: str) -> Callable[[], bool]:
    """Whether the forge reports `head` as the head of pull request `number`, read afresh on each call."""
    return lambda: forge.pull_request(number).head == head


def _merge_state(forge: MergeForge, number: int, wait: Wait) -> str:
    """The pull request's merge state once the forge has worked it out: `MERGED` where it merged."""
    state: list[str] = []

    def known() -> bool:
        pull = forge.pull_request(number)
        state[:] = [MERGED if pull.state == MERGED else pull.merge_state]
        return state[0] != UNKNOWN

    _wait(f"#{number}'s merge state", wait.clock() + wait.head, wait, known)
    return state[0]


def _merges(forge: MergeForge, number: int, head: str, wait: Wait) -> bool:
    """Merge at `head` once the checks pass: True where it merged, False where `main` moved under it.

    A merge the forge refuses is read by its merge state. `BEHIND` is `main`
    moving, which goes round again. `BLOCKED` while a required context is
    still unreported waits, within the same bound, until every required
    context is reported, and only then waits on the checks and asks for the
    merge again. Anything else stops, naming the state.
    """
    deadline = wait.clock() + wait.checks
    while True:
        reported = _checks(forge, number, head, wait, deadline)
        log.info('merging #%d at %s', number, head[:12])
        try:
            forge.merge(number, head)
        except ForgeError as exc:
            log.warning('the forge did not merge #%d: %s', number, exc)
        state = _merge_state(forge, number, wait)
        if state == MERGED:
            return True
        if state == BEHIND:
            log.info('#%d is %s: main moved under it, going round again', number, BEHIND)
            return False
        contexts = forge.required_contexts() if state == BLOCKED else frozenset[str]()
        unreported = sorted(contexts - {c.name for c in reported})
        if not unreported:
            raise Stopped(f'the forge did not merge #{number} at {head}: its merge state is {state}')
        log.info('#%d is %s with %s not reported yet; waiting for them', number, state, ', '.join(unreported))
        _wait(
            f"#{number}'s required contexts {', '.join(unreported)} to be reported (its merge state is {state})",
            deadline,
            wait,
            _all_reported(forge, number, contexts),
        )


def _all_reported(forge: MergeForge, number: int, contexts: frozenset[str]) -> Callable[[], bool]:
    """Whether every one of `contexts` is among the pull request's reported checks, read afresh on each call."""
    return lambda: contexts <= {c.name for c in forge.required_checks(number)}


def _land(forge: MergeForge, repository: Repository, pull: PullRequest, head: str, reported: str, wait: Wait) -> None:
    """Bring the branch up to date, push it, and merge it once its checks pass; `Stopped` where it cannot.

    `head` is what the forge holds: the reported head, or the dispatcher's
    rebase of it.
    """
    expected = head
    #: Whether this run has pushed, after which nothing it hits is a refusal.
    pushing = False
    for attempt in range(1, ATTEMPTS + 1):
        try:
            log.info('fetching (attempt %d of %d)', attempt, ATTEMPTS)
            repository.fetch()
            forge_head = forge.pull_request(pull.number).head
            remote = repository.commit(f'{_symbol(pull.branch)}@origin')
            if forge_head != expected or remote != expected:
                raise Stopped(
                    f"#{pull.number}'s head is {forge_head} on the forge and {remote} as fetched, not {expected}: "
                    'someone else moved the branch (§2 rule 8); nothing was merged'
                )
            local = repository.commit(_symbol(pull.branch))
            if local is None:
                raise Stopped(f'the bookmark {pull.branch} names no single commit here; nothing was merged')
            if local != expected and not repository.rebase_of(expected, local):
                raise Stopped(
                    f'the bookmark {pull.branch} here holds another change than {expected}, which the push would '
                    'carry; nothing was merged'
                )
            log.info("rebasing %s onto the forge's main", pull.branch)
            repository.rebase(pull.branch)
            if repository.conflicted(pull.branch):
                raise Stopped(
                    f'{pull.branch} conflicts with main once rebased: it goes back to the builder as a fix cycle '
                    '(§2 rule 4); nothing was merged'
                )
            pushed = repository.commit(_symbol(pull.branch))
            if pushed is None:
                raise Stopped(f'the bookmark {pull.branch} names no single commit after the rebase; nothing was merged')
            if pushed != expected:
                log.info('pushing %s at %s', pull.branch, pushed[:12])
                pushing = True
                repository.push(pull.branch)
            log.info('waiting for the forge to report %s as the head of #%d', pushed[:12], pull.number)
            _wait(
                f'the forge to report {pushed} as the head of #{pull.number}',
                wait.clock() + wait.head,
                wait,
                _reports(forge, pull.number, pushed),
            )
            expected = pushed
            if _merges(forge, pull.number, pushed, wait):
                return
        except ForgeError as exc:
            if not pushing:
                raise
            raise Stopped(
                f'#{pull.number} stopped after this run pushed it: {exc}. Nothing was merged, and '
                f'`dispatch merge {pull.number} {reported}` takes it up again'
            ) from exc
    raise Stopped(f'#{pull.number} did not merge in {ATTEMPTS} attempts, main moving each time')


def _under_workspaces(root: Path) -> bool:
    return root.parent.name == 'workspaces' and root.parent.parent.name == '.claude'


def close_out(
    forge: MergeForge,
    repository: Repository,
    pull: PullRequest,
    dispatch: Dispatched,
    session: str,
    remains: str | None,
) -> None:
    """End the dispatch of a merged pull request, skipping each step that is done already.

    The steps: each issue the description closes, the dispatch's own among
    them where its record says so, closed where the merge left it open; or
    the comment on what remains; the `end` record; the claim labels; the
    workspaces.
    """
    number = dispatch.issue.number
    closing = {int(n) for n in ledger.CLOSES.findall(pull.body)} | ({number} if dispatch.closes else set[int]())
    for issue in sorted(closing):
        if forge.issue(issue).open:
            log.info('closing #%d, which the merge left open', issue)
            forge.close(issue, f'Closed by {pull.url}, merged through `dispatch merge`.')
    if not dispatch.closes:
        said = any(c.mine and pull.url in c.body and REMAINS in c.body for c in forge.comments(number))
        if not said:
            log.info('commenting on #%d what remains', number)
            forge.comment(number, f'{pull.url} merged, and leaves this issue open. {REMAINS} {remains}')
    if not dispatch.ended:
        log.info('posting the end record on #%d', number)
        forge.comment(number, Record(event=Event.END, session=session).render())
    labels = forge.issue(number).labels
    held = [label for label in ledger.CLAIMS if label in labels]
    if held:
        forge.relabel(number, remove=held)
    standing = repository.workspaces()
    names = dict.fromkeys(
        r.front.workspace for r in dispatch.claim.records if r.front is not None and r.front.workspace is not None
    )
    for name in names:
        if name not in standing:
            continue
        root = repository.workspace_root(name)
        if root.resolve() == repository.root.resolve() or not _under_workspaces(root):
            log.warning(
                'left the workspace %s standing: %s is the checkout this runs in, or not a workspace', name, root
            )
            continue
        log.info('forgetting the workspace %s and removing %s', name, root)
        repository.forget(name)
        if root.is_dir():
            shutil.rmtree(root)


def merge(
    forge: MergeForge,
    repository: Repository,
    number: int,
    reported: str,
    session: str,
    *,
    remains: str | None = None,
    wait: Wait | None = None,
) -> PullRequest:
    """Merge pull request `number` at the head its builder `reported`, and end its dispatch.

    Raises `Refused` with nothing changed, and `Stopped` part way.
    """
    checked = check(forge, repository, number, reported, session, remains)
    if checked.refusals:
        raise Refused(checked.refusals)
    if checked.pull.state != MERGED:
        _land(forge, repository, checked.pull, checked.head, reported, wait or Wait())
        log.info('#%d is merged', number)
    else:
        log.info('#%d is merged already; ending its dispatch', number)
    try:
        close_out(forge, repository, checked.pull, checked.dispatch, session, remains)
    except ForgeError as exc:
        raise Stopped(
            f'#{number} is merged, and its close-out stopped: {exc}. `dispatch merge {number} {reported}` finishes it'
        ) from exc
    return forge.pull_request(number)
