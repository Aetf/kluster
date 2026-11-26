"""`dispatch gates <pr>`, its model half: every gate the change triggers, asked, and the findings comment posted.

It runs after `prcheck`'s deterministic checks, on the same pull request
read from the same local store. A gate runs where the change touches a path
its trigger matches, and is shown each file and commit message its trigger
matches (`view.parts`); its canonical section is read from the tree at the
head, so a change to the canon is judged by the canon it makes. How a gate
is made ready to ask, and how the gates are asked, is shared with the
claim, which asks the gates about a brief (`prepare`, `asked`): a gate's
status is its calibration record's only while that record measured the
gate as it now runs (`calibration.standing`); a gate whose canonical
section does not read asks nothing, and its line in the comment says why;
so does every gate where no model can be asked, for a key this workstation
does not hold (`messages`). So does every gate of a run whose record would
not fit in one comment even as verdicts alone (`comment.fits`): their
answers are dropped, and the comment says why.

The comment of a run at the head the pull request already has a comment of
is edited in place, keeping the appeals that comment records; a run at a new
head posts a new one. A run no gate's trigger matches posts nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..prcheck import Git
from . import comment, view
from .ask import Asked, judge
from .calibration import Status, standing
from .comment import Evidence, Ran
from .extract import Change, Item, change
from .forge import GateForge, PullComment
from .gate import Gate, GateUnreadable
from .model import KeyMissing, Messages, Model, key

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Posted:
    """What a run left on the pull request."""

    evidence: Evidence
    #: Whether it edited a comment the head already had, rather than posting one.
    edited: bool


def findings_comment(comments: Sequence[PullComment], head: str) -> PullComment | None:
    """The findings comment of `head` this account wrote, the latest where there are several."""
    found = [c for c in comments if c.mine and comment.head_of(c.body) == head]
    return found[-1] if found else None


class ExtractorFailed(RuntimeError):
    """A gate's extractor raised, so it drew no item."""


def drawn(g: Gate, changed: Change) -> list[Item]:
    """`g`'s items from `changed`, which `harvest` records a review's tags as. Raises `ExtractorFailed`.

    An extractor is the gate's own code over a change nothing vetted, so it
    can fail on a file it was not written for, a Python file that does not
    parse among them. That fails the one gate at that head, named by the file
    the extractor last read, rather than the harvest of every pull request.
    """
    read: list[str] = []

    def reading(source: Callable[[str], str | None]) -> Callable[[str], str | None]:
        def text(path: str) -> str | None:
            read.append(path)
            return source(path)

        return text

    try:
        return list(g.spec.extract(replace(changed, text=reading(changed.text), base_text=reading(changed.base_text))))
    except Exception as exc:
        where = f' after reading {read[-1]}' if read else ''
        raise ExtractorFailed(f'its extractor failed{where}: {type(exc).__name__}: {exc}') from exc


def messages() -> Model:
    """The model the gates ask, through the key this workstation holds for them. Raises `KeyMissing`."""
    return Messages.with_key(key())


@dataclass(frozen=True)
class Prepared:
    """A gate ready to ask: the gate, the link to its rule, its status and why, and what it asks."""

    gate: Gate
    rule: str
    status: Status
    why: str
    asked: Asked


def _unasked(g: Gate, status: Status, why: str, rule: str = '') -> Ran:
    return Ran(g.name, g.spec.model, status, why, rule, (), asked=False)


def prepare(g: Gate, canon: Callable[[str], str | None], changed: Change, at: str | None = None) -> Prepared | Ran:
    """`g` ready to ask about `changed`, its canonical section read through `canon`; where it cannot ask, why.

    Its status is what its calibration record earns against the gate as it
    now runs. A gate whose section does not read asks nothing, and stays
    blocking where its record earned that: a canon moved out from under a
    gate does not open it. `at` is the commit whose tree `canon` reads, which
    the link to the rule names; a tree that is no commit, a checkout, gives
    no link.
    """
    try:
        section = g.canon(canon)
    except GateUnreadable as exc:
        record = g.record()
        status = Status.BLOCKING if record is not None and not record.shortfalls() else Status.SHADOW
        log.error('%s asks nothing: %s', g.name, exc)
        return _unasked(g, status, f'its canonical section does not read: {exc}')
    status, why = standing(g.record(), g.hashes(section.text))
    rule = g.rule(at, section) if at is not None else ''
    parts = view.parts(changed, g.spec.trigger)
    return Prepared(g, rule, status, why, Asked(system=g.system(section.text), model=g.spec.model, parts=parts))


def asked(prepared: Sequence[Prepared], model: Callable[[], Model]) -> list[Ran]:
    """Each prepared gate, asked; each recorded as asking nothing where no model can be asked."""
    try:
        judged = judge([p.asked for p in prepared], model)
    except KeyMissing as exc:
        log.error('the gates ask nothing: %s', exc)
        return [_unasked(p.gate, p.status, f'no model could be asked: {exc}', p.rule) for p in prepared]
    return [
        Ran(
            gate=p.gate.name,
            model=p.gate.spec.model,
            status=p.status,
            why=p.why,
            rule=p.rule,
            entries=tuple(entries),
        )
        for p, entries in zip(prepared, judged, strict=True)
    ]


def review(forge: GateForge, git: Git, number: int, gates: Sequence[Gate], model: Callable[[], Model]) -> Posted | None:
    """Run every gate pull request `number` triggers and post or edit its findings comment; None where none ran."""
    pull = forge.pull_request(number)
    base = git.merge_base(pull.base, pull.head)
    changed = change(git, base, pull.head, pull.body)
    triggered = [g for g in gates if g.triggered(changed.paths)]
    if not triggered:
        log.info('#%d: no model gate is triggered by the paths it changes', number)
        return None
    log.info('#%d: running %s', number, ', '.join(g.name for g in triggered))
    prepared: list[Prepared] = []
    unread: list[Ran] = []
    for g in triggered:
        ready = prepare(g, lambda path: git.text(pull.head, path), changed, pull.head)
        if isinstance(ready, Prepared):
            prepared.append(ready)
        else:
            unread.append(ready)
    ran = asked(prepared, model)
    if not comment.fits(Evidence(pull.head, tuple(ran + unread))):
        log.error('#%d: the gates answered more than one findings comment holds; their answers are dropped', number)
        ran = [
            replace(
                r,
                why=f'its {len(r.entries)} places would not fit in one comment, with the other gates',
                entries=(),
                asked=False,
                dropped=True,
            )
            for r in ran
        ]
    held = findings_comment(forge.pull_comments(number), pull.head)
    evidence = Evidence(pull.head, tuple(ran + unread)).appealed(comment.parse(held.body) if held else None)
    body = comment.render(evidence)
    if held is not None:
        log.info('#%d: editing the findings comment of %s', number, pull.head[:12])
        forge.edit(held.id, body)
    else:
        log.info('#%d: posting the findings comment of %s', number, pull.head[:12])
        forge.post(number, body)
    return Posted(evidence, edited=held is not None)


def refuses(deterministic: int, posted: Posted | None) -> bool:
    """Whether `dispatch gates` exits non-zero: on a deterministic finding, or where a blocking gate cannot pass.

    A blocking gate cannot pass while it holds a finding or a place its
    samples left undecided that no appeal answers, or asked nothing, so a gate
    that blocks never fails open. A shadow gate decides nothing here, whatever
    it found or failed to ask.
    """
    if deterministic:
        return True
    if posted is None:
        return False
    evidence = posted.evidence
    return bool(evidence.unanswered) or any(r.status is Status.BLOCKING and r.unheard for r in evidence.gates)
