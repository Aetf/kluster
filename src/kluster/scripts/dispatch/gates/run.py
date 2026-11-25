"""`dispatch gates <pr>`, its model half: every gate the change triggers, asked, and the findings comment posted.

It runs after `prcheck`'s deterministic checks, on the same pull request
read from the same local store. A gate runs where the change touches a path
its trigger matches; its canonical section is read from the tree at the
head, so a change to the canon is judged by the canon it makes; its status
is its calibration record's only while that record measured the gate as it
now runs (`calibration.standing`). A gate whose canonical section does not
read at the head asks nothing, and its line in the comment says why; so
does a gate whose extractor raised (`drawn`), which leaves every other gate
to run, every gate where no model can be asked, for a key this workstation
does not hold, and every gate of a run whose record would not fit in one
comment even as verdicts alone (`comment.fits`), which asks nothing and
spends nothing.

The comment of a run at the head the pull request already has a comment of
is edited in place, keeping the appeals that comment records; a run at a new
head posts a new one. A run no gate's trigger matches posts nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..prcheck import Git
from . import comment
from .ask import Asked, judge
from .calibration import Status, standing
from .comment import Entry, Evidence, Ran
from .extract import Change, Item, change
from .forge import GateForge, PullComment
from .gate import Gate, GateUnreadable
from .model import KeyMissing, Model
from .verdict import SAMPLES, Answer, Verdict

log = logging.getLogger(__name__)

#: The samples of an item every sample finds: the longest an entry's verdicts run to.
_ALL = tuple(Answer(Verdict.FINDING, '') for _ in range(SAMPLES))


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
    """A gate's extractor raised, so the gate has no items to ask about."""


def drawn(g: Gate, changed: Change) -> list[Item]:
    """`g`'s items from `changed`. Raises `ExtractorFailed`, naming the file the extractor last read.

    An extractor is the gate's own code over a change nothing vetted, so it
    can fail on a file it was not written for, a Python file that does not
    parse among them. That fails the one gate, which then asks nothing and is
    recorded so, rather than the run of every gate on the pull request.
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


#: A gate ready to ask: the gate, its rule link, its status and why, and what it asks.
Prepared = tuple[Gate, str, Status, str, Asked]


def _unasked(g: Gate, status: Status, why: str, rule: str = '') -> Ran:
    return Ran(g.name, g.spec.model, status, why, rule, (), asked=False)


def _asked(prepared: Sequence[Prepared], model: Callable[[], Model], number: int) -> list[Ran]:
    """Each prepared gate, asked; each recorded as asking nothing where no model can be asked."""
    try:
        judged = judge([a for *_, a in prepared], model)
    except KeyMissing as exc:
        log.error('#%d: the gates ask nothing: %s', number, exc)
        return [_unasked(g, status, f'no model could be asked: {exc}', rule) for g, rule, status, _, _ in prepared]
    return [
        Ran(
            gate=g.name,
            model=g.spec.model,
            status=status,
            why=why,
            rule=rule,
            entries=tuple(Entry(j.item.path, j.item.line, j.item.end_line, j.samples) for j in verdicts),
        )
        for (g, rule, status, why, _), verdicts in zip(prepared, judged, strict=True)
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
        try:
            section = g.canon(lambda path: git.text(pull.head, path))
        except GateUnreadable as exc:
            # A record that earned blocking keeps the gate blocking: a change
            # that moves the canon out from under it does not open the gate.
            record = g.record()
            status = Status.BLOCKING if record is not None and not record.shortfalls() else Status.SHADOW
            log.error('%s asks nothing: %s', g.name, exc)
            unread.append(_unasked(g, status, f'its canonical section does not read at the head: {exc}'))
            continue
        status, why = standing(g.record(), g.hashes(section.text))
        try:
            items = drawn(g, changed)
        except ExtractorFailed as exc:
            log.error('%s asks nothing: %s', g.name, exc)
            unread.append(_unasked(g, status, str(exc), g.rule(pull.head, section)))
            continue
        asked = Asked(system=g.system(section.text), model=g.spec.model, items=items)
        prepared.append((g, g.rule(pull.head, section), status, why, asked))
    skeleton = Evidence(
        pull.head,
        tuple(
            Ran(
                g.name, g.spec.model, status, why, rule, tuple(Entry(i.path, i.line, i.end_line, _ALL) for i in a.items)
            )
            for g, rule, status, why, a in prepared
        )
        + tuple(unread),
    )
    if not comment.fits(skeleton):
        log.error('#%d: the gates ask nothing: their items would not fit in one findings comment', number)
        ran = [
            _unasked(g, status, f'its {len(a.items)} items would not fit in one comment, with the other gates', rule)
            for g, rule, status, _, a in prepared
        ]
    else:
        ran = _asked(prepared, model, number)
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

    A blocking gate cannot pass while it holds a finding or an item its
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
