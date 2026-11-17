"""`stack-plan --stack <name>`: the command line, and the two runs of the pinned CLI behind it.

    stack-plan --stack physical

It runs in the current directory, under the environment it is started
with, as `pulumi preview` itself would: the checkout holding `Pulumi.yaml`,
with the stack's backend and passphrase already set. The answer takes at
most two runs:

1.  `pulumi preview --diff --expect-no-changes`, its output on the terminal
    unchanged. `REFUSED` is a step, and the answer is `WOULD_WRITE` with
    nothing more run.
2.  After a 0 only: the second preview of `kluster.lib.state_only`, its
    output kept off the terminal. A step it plans is the two runs
    disagreeing, and is `WOULD_WRITE`; so is any field it finds, printed
    under the section it writes.

Any other exit of either run is `NO_ANSWER`, and so is a run stopped at its
bound and a document or an event log that does not say what an `up` would
write. A JSON preview that fails writes its error into the document's
`diagnostics` and nothing on standard error, so those are printed. Each run
is bounded, and ends with the command whatever signal ends it (`preview`).
On GitHub Actions, a plan that changes no resource and rewrites state is
also an annotation on the run's page (`GITHUB_ACTIONS_ENV`).
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

from kluster.lib import state_only
from kluster.lib.state_only import REFUSED
from kluster.scripts.stack_plan import preview

log = logging.getLogger(__name__)

#: An `up` would write nothing, short of the secret written with no step
#: whose value alone changed, which the second preview prints as a
#: placeholder on both sides (`kluster.lib.state_only` says which that is).
NOTHING = 0
#: No answer: a preview failed, or what it recorded does not say. Python
#: exits with it on an uncaught exception, and `argparse` exits 2 on a bad
#: argument; both are no answer too.
NO_ANSWER = 1
#: An `up` would write: a step, or state with no step. The preview's own
#: refusal, because the command exits as the preview it stands in for does: a
#: caller reads one table of exits before this layer, while it stands and once
#: it is removed.
WOULD_WRITE = REFUSED

#: The variable a GitHub Actions runner sets on every step. Under it a plan
#: that changes no resource and rewrites state also writes a `::notice::`
#: workflow command, which the run's page shows as an annotation. The command
#: writes it rather than the step that runs it: both kinds of write leave
#: with `WOULD_WRITE`, so only the command can tell them apart.
GITHUB_ACTIONS_ENV = 'GITHUB_ACTIONS'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='stack-plan',
        description=(
            "Print a stack's plan and exit by whether an `up` would write anything: a step, or a field of the "
            "stack's state that the preview plans no step for, such as a resource option or an output."
        ),
        epilog=f'exit status: {NOTHING} an up would write nothing; {WOULD_WRITE} an up would write; anything else, '
        'no answer, because a preview failed.',
    )
    _ = parser.add_argument('--stack', required=True, help='the stack to preview, as `pulumi --stack` takes it')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    stack: str = args.stack
    try:
        return plan(stack)
    except (state_only.Unreadable, preview.Stopped, OSError) as exc:
        log.error('no answer for %s: %s', stack, exc)
        return NO_ANSWER
    except preview.Signalled as signalled:
        # Here, past every block `plan` holds open, so its temporary
        # directory is gone before the command is.
        log.error('no answer for %s: %s', stack, signalled)
        preview.die_of(signalled.signum)


def plan(stack: str) -> int:
    """Print `stack`'s plan and return whether an `up` would write: `NOTHING`, `WOULD_WRITE` or `NO_ANSWER`."""
    status, _ = preview.run(['preview', '--stack', stack, '--diff', '--expect-no-changes'])
    if status == REFUSED:
        log.info('the preview of %s plans a step, so an `up` would write', stack)
        return WOULD_WRITE
    if status != 0:
        log.error('the preview of %s failed (exit %d), so there is no answer', stack, status)
        return NO_ANSWER
    with tempfile.TemporaryDirectory(prefix='stack-plan-') as directory:
        events = Path(directory) / 'events.jsonl'
        log.info(
            'previewing %s again, as JSON with an event log, for the state an `up` would write beside its steps; '
            'it takes about as long as the preview above',
            stack,
        )
        status, printed = preview.run(
            [*state_only.arguments(events), '--stack', stack], env=os.environ | state_only.ENVIRONMENT, captured=True
        )
        try:
            written = state_only.answer(status, printed, events)
        except state_only.Failed as failed:
            log.error('the JSON preview of %s failed (exit %d), so there is no answer', stack, failed.status)
            _print_diagnostics(failed)
            return NO_ANSWER
    match written:
        case state_only.Stepped():
            log.warning('the JSON preview of %s plans a step the first preview did not, so an `up` would write', stack)
            return WOULD_WRITE
        case state_only.Fields(changes=()):
            print(f'An `up` of {stack} would write nothing.', flush=True)
            return NOTHING
        case state_only.Fields(changes=found):
            print(state_only.section(stack, found), flush=True)
    if os.environ.get(GITHUB_ACTIONS_ENV) == 'true':
        print(f"::notice::the plan of {stack} changes no resource and rewrites the stack's state", flush=True)
    return WOULD_WRITE


def _print_diagnostics(failed: state_only.Failed) -> None:
    """The error and warning messages a failed JSON preview's document carries, or what stands in their way."""
    if failed.unrecorded is not None:
        log.error('%s', failed.unrecorded)
    for line in failed.diagnostics:
        print(line, flush=True)
