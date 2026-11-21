"""The deploy chain on the forge, as `gh` reports it: which run a dispatch created, and which run applied a merge.

**A dispatched run is named without a clock**, and without reading what
`gh workflow run` prints, which is a release's to decide: a time filter would
compare the forge's clock with the workstation's. So the drill lists the
dispatched runs of `deploy.yml` before the dispatch, and the run it created is
the one listed afterwards that was not listed before (`created`). Run ids
only grow, so a run that was listed before is never mistaken for the new one.

**The run that applied a merge is not always the merge's own.** `deploy.yml`'s
`deploy` concurrency group keeps one run pending behind the one in progress,
and a later push to `main` -- a Renovate automerge -- replaces the pending
run, which then ends `cancelled`. Every later push run on `main` checks out a
commit that holds the merge, so the run that applied it is the first push run
from the merge's own on that did not end cancelled (`applied`).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, cast

from kluster.conventions import forge

REPOSITORY = f'{forge.ACCOUNT.login}/{forge.DEPLOYMENT.name}'
#: The deploy chain's workflow, `.github/workflows/deploy.yml`.
WORKFLOW = 'deploy.yml'
#: The jobs of the chain that apply the two stacks a rebuild brings back.
APPLYING_JOBS = ('up-k8s-base', 'up-apps')

#: What `gh run list` is asked for.
FIELDS = 'databaseId,url,headSha,status,conclusion,event'
#: How many runs one listing reads: far more than a rebuild's sitting adds.
LIMIT = '50'

CANCELLED = 'cancelled'


@dataclass(frozen=True)
class Run:
    id: int
    url: str
    head: str
    status: str
    conclusion: str
    event: str


def listing(event: str) -> list[str]:
    """The `gh run list` of `deploy.yml`'s runs of `event`, on `main`."""
    return [
        'gh',
        'run',
        'list',
        '--repo',
        REPOSITORY,
        '--workflow',
        WORKFLOW,
        '--branch',
        'main',
        '--event',
        event,
        '--limit',
        LIMIT,
        '--json',
        FIELDS,
    ]


def runs(rows: Sequence[object]) -> list[Run]:
    """The runs a `listing` printed, parsed, oldest first; `KeyError` or `TypeError` where a row is no run."""
    return sorted(
        (
            Run(
                id=int(row['databaseId']),
                url=str(row.get('url', '')),
                head=str(row.get('headSha', '')),
                status=str(row.get('status', '')),
                conclusion=str(row.get('conclusion', '')),
                event=str(row.get('event', '')),
            )
            for row in (cast('dict[str, Any]', each) for each in rows)
        ),
        key=lambda run: run.id,
    )


class Ambiguous(ValueError):
    """More than one run appeared where the dispatch created one."""


def created(before: Collection[int], after: Sequence[Run]) -> Run | None:
    """The dispatched run listed after the dispatch and not before; None where none is listed yet."""
    new = [run for run in after if run.id not in before]
    if len(new) > 1:
        raise Ambiguous(f'{len(new)} dispatched runs appeared: {", ".join(run.url for run in new)}')
    return new[0] if new else None


def applied(merge: str, pushed: Sequence[Run]) -> Run | None:
    """The push run on `main` that applied the commit `merge`; None where the merge's own run is not listed yet."""
    own = [index for index, run in enumerate(pushed) if run.head == merge]
    if not own:
        return None
    return next((run for run in pushed[own[0] :] if run.conclusion != CANCELLED), None)


def unconverged(viewed: str) -> str | None:
    """Why a run's `up-k8s-base` and `up-apps` did not converge in that one run, or None where they did."""
    view = cast('dict[str, Any]', json.loads(viewed))
    if int(view.get('attempt', 1)) != 1:
        return f'the run took {view.get("attempt")} attempts'
    jobs = {
        str(job.get('name')): str(job.get('conclusion')) for job in cast('list[dict[str, Any]]', view.get('jobs') or [])
    }
    failing = [f'{name} {jobs.get(name, "absent")}' for name in APPLYING_JOBS if jobs.get(name) != 'success']
    return '; '.join(failing) or None
