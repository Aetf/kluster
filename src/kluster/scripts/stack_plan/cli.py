"""`stack-plan --stack <name>`: the command line, and the two runs of the pinned CLI behind it.

    stack-plan --stack physical

It runs in the current directory, under the environment it is started
with, as `pulumi preview` itself would: the checkout holding `Pulumi.yaml`,
with the stack's backend and passphrase already set. The answer takes at
most two runs:

1.  `pulumi preview --diff --expect-no-changes`, its output on the terminal
    unchanged. `REFUSED` is a step, and the answer is `WOULD_WRITE` with
    nothing more run.
2.  After a 0 only: `pulumi preview --json --show-sames --expect-no-changes
    --event-log <file>`, its output kept off the terminal. `REFUSED` here is
    the two runs disagreeing, and is `WOULD_WRITE`. A 0 hands the document
    and the event log to `comparison`, and any field it finds is
    `WOULD_WRITE`, printed under the section it writes.

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
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import cast

from kluster.scripts.stack_plan import comparison, preview

log = logging.getLogger(__name__)

#: The CLI's exit when `--expect-no-changes` refuses the steps a preview
#: planned (`ExitNoChanges`, sdk/go/common/util/cmdutil/exit.go).
REFUSED = 7

#: An `up` would write nothing, short of a secret whose value alone changed,
#: which the previews print as a placeholder on both sides (the package
#: docstring says when that is).
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

#: The variable that makes the CLI take `--event-log`, the flag the Automation
#: API reads its events through: without it the flag is unknown and the run
#: fails. It is set on the JSON run alone, so the first run is the plain
#: preview a caller reads.
DEBUG_COMMANDS_ENV = 'PULUMI_DEBUG_COMMANDS'

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
    except (comparison.Unreadable, preview.Stopped, OSError) as exc:
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
            ['preview', '--stack', stack, '--json', '--show-sames', '--expect-no-changes', '--event-log', str(events)],
            env=os.environ | {DEBUG_COMMANDS_ENV: 'true'},
            captured=True,
        )
        if status == REFUSED:
            log.warning('the JSON preview of %s plans a step the first preview did not, so an `up` would write', stack)
            return WOULD_WRITE
        if status != 0:
            log.error('the JSON preview of %s failed (exit %d), so there is no answer', stack, status)
            _print_diagnostics(printed)
            return NO_ANSWER
        found = comparison.changes(comparison.read_steps(printed), comparison.read_outputs(events.read_text()))
    if not found:
        print(f'An `up` of {stack} would write nothing.', flush=True)
        return NOTHING
    print(comparison.section(stack, found), flush=True)
    if os.environ.get(GITHUB_ACTIONS_ENV) == 'true':
        print(f"::notice::the plan of {stack} changes no resource and rewrites the stack's state", flush=True)
    return WOULD_WRITE


def _print_diagnostics(printed: str) -> None:
    """The error and warning messages a failed JSON preview's document carries, or what stands in their way."""
    try:
        document = json.loads(printed)
    except ValueError:
        log.error('the JSON preview printed no document, so its diagnostics are unknown')
        return
    diagnostics = cast('dict[str, object]', document).get('diagnostics') if isinstance(document, dict) else None
    if not isinstance(diagnostics, list):
        log.error('the JSON preview recorded no diagnostics')
        return
    for diagnostic in cast('list[object]', diagnostics):
        if isinstance(diagnostic, dict) and (entry := cast('dict[str, object]', diagnostic)).get('severity') != 'debug':
            print(str(entry.get('message', '')).rstrip(), flush=True)
