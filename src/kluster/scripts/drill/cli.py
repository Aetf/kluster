"""`drill`: the command line.

    drill rebuild            resume at the first step not recorded as a pass, and run on
    drill rebuild <step>     run one step again from its first part: 0 to 9, or `pass`

Run as `mise x -- uv run drill rebuild` from the root of the checkout that
holds `.credentials/`, as every command of operations.md §3.5 is. A run under
a `.claude/` directory, where `jj` workspaces live, is refused: the slots do
not answer there, and the run's directory would be the workspace's.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from kluster.lib import workstation
from kluster.scripts import manual_run
from kluster.scripts.credentials import oci_recovery, pulumi_config
from kluster.scripts.drill import record, steps
from kluster.scripts.drill.record import Verdict
from kluster.scripts.drill.tools import Exec, Session, shell

#: Every step that ran passed.
DONE = 0
#: A step ended on a failure, or on a reading that is no reading.
STOPPED = 1
#: Refused before any command of the step ran.
REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='drill',
        description='Run the drills of operations.md §4 that a script runs: today the whole-cluster rebuild, §3.5.',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    rebuild = commands.add_parser(
        'rebuild',
        help='operations.md §3.5, step by step',
        description=(
            'Run operations.md §3.5: with no step, from the first step `.claude/drill/run.json` does not record as a '
            'pass, on through the rest until one does not pass; with a step, that step alone, from its first part. '
            "Each step writes `.claude/drill/step-<step>.log`, and stops at each act that is the operator's."
        ),
    )
    _ = rebuild.add_argument('step', nargs='?', choices=[step.key for step in steps.SEQUENCE])
    return parser


def _ask(question: str) -> str | None:
    try:
        return input(f'{question} ')
    except (EOFError, KeyboardInterrupt):
        # An end of input, or a ^C at the prompt, is no answer: the part that
        # asked stops as it stops for any answer it cannot take.
        return None


def _say(text: str) -> None:
    print(text, flush=True)


def rebuild(
    checkout: Path,
    step: str | None,
    *,
    base: Mapping[str, str],
    helm: Path,
    ask: steps.Ask = _ask,
    say: Callable[[str], None] = _say,
    exec: Exec = shell,  # noqa: A002 -- the name `Session` gives the same field
    physical: Callable[[], str] = oci_recovery.slot_passphrase,
    require_main: Callable[[Path], None] = manual_run.require_main,
    configure: Callable[[steps.Context], None] = lambda _: None,
) -> int:
    """Run `step`, or resume at the first step not recorded as a pass and run on; an exit of `DONE`, `STOPPED` or `REFUSED`.

    `configure` adjusts each step's context before its first part runs, as a
    case points the client configurations into its own directory.
    """
    if '.claude' in checkout.absolute().parts:
        say(
            f'{checkout} is under a .claude/ directory, where jj workspaces live: run from the checkout that holds .credentials/.'
        )
        return REFUSED
    directory = checkout / record.DIRECTORY
    kept = record.Record.open(directory)
    environments = manual_run.Environments(base=base, helm=helm, physical=physical)
    if step is not None:
        chosen = [next(each for each in steps.SEQUENCE if each.key == step)]
        started = [each.key for each in steps.SEQUENCE[1:] if kept.times(each.key)[0] is not None]
        if step == steps.STEPS[0].key and started:
            say(
                f'step 0 is not run again once step {started[0]} has started: its freeze and its baselines are '
                'what that step stood on, and from step 3 the way is forward only. `drill rebuild` resumes where '
                'the run stands, and `drill rebuild <n>` runs a later step again.'
            )
            return REFUSED
        if step != steps.STEPS[0].key and kept.verdict(steps.STEPS[0].key) is not Verdict.PASS:
            say(
                f'step {step} runs by name only once step 0 is recorded as a pass: the baselines and the go are what '
                'every later step stands on. A run after an actual loss, whose step 0 cannot pass, waits on a mode of '
                f'its own ({steps.LOSS_MODE}).'
            )
            return REFUSED
    else:
        pending = [each for each in steps.SEQUENCE if kept.verdict(each.key) is not Verdict.PASS]
        if not pending:
            say('every step of the rebuild is recorded as a pass')
            return DONE
        chosen = pending
    for each in chosen:
        with record.Transcript(directory / f'step-{each.key}.log') as transcript:
            session = Session(checkout=checkout, transcript=transcript, environments=environments, exec=exec)
            context = steps.Context(
                checkout=checkout, record=kept, session=session, ask=ask, say=say, require_main=require_main
            )
            configure(context)
            verdict = _step(context, each, again=step is not None)
        if verdict is None:
            return REFUSED
        if verdict is not Verdict.PASS:
            return STOPPED
    return DONE


def _step(context: steps.Context, step: steps.Step, *, again: bool) -> Verdict | None:
    """Run `step` from its first part not recorded as passed, or from its first where `again`; None where refused."""
    context.note(f'step {step.key}: {step.title}')
    try:
        context.require_main(context.checkout)
    except manual_run.CheckRefused as exc:
        context.note(f'refused before any command ran: {exc}')
        return None
    if again:
        context.record.forget(step.key)
    context.record.started(step.key, record.now())
    passed = context.record.passed_parts(step.key)
    for part in step.parts:
        if part.name in passed:
            continue
        context.note(f'-- {part.name}')
        try:
            verdict = part.act(context)
        except (pulumi_config.MachineRefused, manual_run.NoBackend) as exc:
            # `physical`'s passphrase, or another a `pulumi` run needs, that
            # this machine cannot give; or a backend the drill's environment
            # does not name.
            context.note(str(exc))
            verdict = Verdict.FAIL
        except KeyboardInterrupt:
            # A ^C between commands: a command that was running answers it
            # itself (`tools.shell`), so this is the drill's own, and the step
            # stops as for any other stop, saying what it leaves.
            context.note(f'interrupted at {part.name}')
            verdict = Verdict.FAIL
        if verdict is not Verdict.PASS:
            context.note(f'step {step.key} stops at {part.name}: {verdict}')
            context.record.ended(step.key, verdict, record.now())
            if step.on_stop is not None:
                step.on_stop(context)
            return verdict
        context.record.part_passed(step.key, part.name)
    context.note(f'step {step.key}: pass')
    context.record.ended(step.key, Verdict.PASS, record.now())
    return Verdict.PASS


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    checkout = workstation.repo_root()
    try:
        with manual_run.helm_homes(checkout, prefix='drill-helm-') as helm:
            return rebuild(checkout, args.step, base=dict(os.environ), helm=helm)
    except manual_run.HelmInCheckout as exc:
        print(exc, file=sys.stderr)
        return REFUSED
