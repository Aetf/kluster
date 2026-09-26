"""The workflow files and composite actions under `.github/`, read as the suites read them.

`test_conventions` holds the forge census to the literals these files spell,
`test_workflow_shape` holds every job to the shape framework/ci.md §3 asks of
it, and `test_gate_command` holds every test run they execute to the gate's
form. Each finds the files through `workflows_and_actions`, so they agree on
which files exist; the census and the shape test also read them through the
rest of the loaders here, so those agree on how a failure names a file and
what a job is.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

GITHUB = Path(__file__).parent.parent / '.github'


def workflows_and_actions() -> list[Path]:
    """The workflow files and the composite actions at `.github/actions/*/`.

    A step inside a composite action carries an `if:` and a `run:` GitHub
    evaluates exactly as it does a step written in the workflow, so a login or
    a label compared there is one the workflow branches on, and a census that
    read the workflows alone would be blind to it. Workflows can live nowhere
    but `.github/workflows/`, so that half of the set is closed by GitHub. A
    local action can live in any directory of the repository, and an action's
    step can `uses:` another, so that half is closed by a case instead: every
    local `uses:` in a file here resolves to a file here
    (`test_conventions.test_every_local_action_a_step_uses_is_one_the_censuses_read`),
    which is what makes reading this set the same as reading every step a
    workflow runs out of this repository.

    Both spellings of the suffix, because GitHub reads `.yml` and `.yaml`
    identically, and a file named the other way would be invisible the same way.
    """
    return sorted(
        path
        for pattern in ('workflows/*.yml', 'workflows/*.yaml', 'actions/*/action.yml', 'actions/*/action.yaml')
        for path in GITHUB.glob(pattern)
    )


def github_name(path: Path) -> str:
    """How a failure names a file: relative to `.github/`, since every action file is `action.yml`."""
    return str(path.relative_to(GITHUB))


def mapping(value: object, what: str) -> dict[str, object]:
    """`value` as a mapping, or a failure naming `what` was not one."""
    assert isinstance(value, dict), f'{what} is not a mapping'
    return cast('dict[str, object]', value)


def read_workflow(path: Path) -> dict[str, object]:
    """A workflow or action file, parsed."""
    return mapping(yaml.safe_load(path.read_text()), github_name(path))


def workflow_jobs(workflow: dict[str, object], what: str) -> dict[str, dict[str, object]]:
    """A workflow's jobs by name; `what` names the workflow in a failure."""
    jobs = mapping(workflow.get('jobs'), f'{what} jobs:')
    return {name: mapping(job, f'{what} job {name}') for name, job in jobs.items()}
