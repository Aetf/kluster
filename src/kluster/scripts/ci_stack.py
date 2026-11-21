"""`ci-stack <stack> <pulumi args…>`: a run by hand of a stack CI deploys (framework/pulumi.md §3.4).

    mise x uv -- uv run ci-stack physical preview --diff
    mise x uv -- uv run ci-stack physical refresh --run-program --target '<urn>'
    mise x uv -- uv run ci-stack k8s-base up --refresh
    mise x uv -- uv run ci-stack physical stack output kubeconfig --show-secrets > ~/.kube/kluster

The stack is the first argument, one of the stacks CI deploys: the census
(`conventions.identity.STACK_NAMES`) less the operator stacks, which
`operator-stack` runs. Everything after it goes to `pulumi` whole, with
`--stack <stack>` added ahead of any `--`, and nothing else added. Before
`pulumi` starts, in this order:

1.  a run under a `.claude/` directory, where `jj` workspaces live, is
    refused: the slots do not answer there;
2.  a word that names a stack is refused (`manual_run.stack_word`), and so
    is one that points `pulumi` at another program or configuration
    (`manual_run.elsewhere_word`): `-C`/`--cwd`, `--config-file`, `--client`;
3.  the environment is the stack's own (`manual_run.Environments`): its
    passphrase, the estate's backend, CI's Helm environment. One that names
    no backend is refused, and so is a `physical` run on a machine whose slot
    holds no passphrase for it;
4.  a working copy that does not hold the forge's `main` is refused
    (`manual_run.require_main`), for every command, reads included.

`pulumi` then runs from the checkout, attached to the terminal
(`manual_run.attached`): standard input, output and error are the terminal's
or whatever the caller redirected them to, so a value a read prints reaches a
pipe or a file untouched, and the first ^C is `pulumi`'s graceful cancel.
`ci-stack`'s own lines go to standard error alone, and it keeps no record.
A run that names no `pulumi` command is refused as a usage error. Its exit
is `pulumi`'s, 128+N where `pulumi` was killed by signal N; a refusal
before `pulumi` starts exits `REFUSED`, 125,
the convention of wrappers such as `env` and `timeout`, so that a caller can
tell it from any exit of `pulumi`'s, and a `pulumi` not found exits 127, as
`env` does.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn, Protocol, override

from kluster.conventions import identity
from kluster.lib import stack_environment, workstation
from kluster.scripts import manual_run
from kluster.scripts.credentials import oci_recovery, pulumi_config

log = logging.getLogger(__name__)

#: Refused before `pulumi` started.
REFUSED = 125
#: The `pulumi` on `PATH` was not found.
NOT_FOUND = 127

#: How a person runs `ci-stack`, which a refusal names as its remedy.
INVOCATION = 'mise x uv -- uv run ci-stack'


def stacks() -> tuple[str, ...]:
    """Every stack CI deploys: the census less the operator stacks, in declaration order."""
    return tuple(name for name in identity.STACK_NAMES.names() if name not in identity.OPERATOR_STACKS)


class Start(Protocol):
    """How `pulumi` is started once every check passes. Substituted in tests."""

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> int: ...


class Refused(RuntimeError):
    """`ci-stack` will not start `pulumi`; the message says why and what clears it."""


class _Parser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors exit `REFUSED`, as every other refusal before `pulumi` does."""

    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(REFUSED, f'{self.prog}: error: {message}\n')


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog='ci-stack',
        description=(
            "Run pulumi by hand against a stack CI deploys, under its own passphrase, the estate backend and CI's "
            "Helm environment, from a working copy that holds the forge's main (docs/framework/pulumi.md §3.4)."
        ),
        epilog=(
            f"exit status: pulumi's own, 128+N where pulumi was killed by signal N; {REFUSED} refused before "
            f'pulumi started; {NOT_FOUND} no pulumi on PATH.'
        ),
    )
    _ = parser.add_argument('stack', metavar='stack', help=f'the stack to run: one of {", ".join(stacks())}')
    _ = parser.add_argument(
        'args', nargs=argparse.REMAINDER, help='the pulumi command and its arguments, as in `preview --diff`'
    )
    return parser


def run(
    stack: str,
    args: Sequence[str],
    *,
    checkout: Path,
    base: Mapping[str, str],
    physical: Callable[[], str] = oci_recovery.slot_passphrase,
    require_main: Callable[[Path], None] = manual_run.require_main,
    start: Start = manual_run.attached,
) -> int:
    """`pulumi <args> --stack <stack>` from `checkout`, once every check passes; `pulumi`'s exit.

    `base` is the environment `mise x` started `ci-stack` with. A refusal
    raises `Refused`, or the refusal of the check that made it.
    """
    if stack in identity.OPERATOR_STACKS:
        raise Refused(f'{stack} is an operator stack, which no CI job runs: run it with `operator-stack {stack}`')
    if stack not in stacks():
        raise Refused(f'{stack} is not a stack CI deploys: name one of {", ".join(stacks())}')
    if not args:
        raise Refused(f'name the pulumi command, as in `ci-stack {stack} preview --diff`')
    if '.claude' in checkout.absolute().parts:
        raise Refused(
            f'{checkout} is under a .claude/ directory, which is where jj workspaces live: the slots do not answer '
            'there. Run from the checkout that holds .credentials/.'
        )
    word = manual_run.stack_word(args)
    if word is not None:
        raise Refused(f'`{word}` names a stack, or may: this run is against {stack} alone, which ci-stack adds')
    word = manual_run.elsewhere_word(args)
    if word is not None:
        raise Refused(
            f'`{word}` points pulumi at another program or configuration: this run is of the program in '
            f"{checkout}, under the {stack} stack's own configuration"
        )
    with manual_run.helm_homes(checkout, prefix='ci-stack-helm-') as helm:
        env = manual_run.Environments(base=base, helm=helm, physical=physical).pulumi(stack)
        log.info("checking the working copy against the forge's main")
        require_main(checkout)
        argv = ['pulumi', *manual_run.with_stack(args, stack)]
        log.info('running %s', ' '.join(argv))
        try:
            return start(argv, env=env, cwd=checkout)
        except FileNotFoundError as exc:
            log.error('%s: run as `%s`, which puts the pinned pulumi on PATH', exc, INVOCATION)
            return NOT_FOUND


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s', stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        return run(args.stack, list[str](args.args), checkout=workstation.repo_root(), base=dict(os.environ))
    except manual_run.NoBackend as exc:
        log.error('%s, as `%s %s …`', exc, INVOCATION, args.stack)
    except (
        Refused,
        manual_run.CheckRefused,
        stack_environment.EnvironmentRefused,
        pulumi_config.MachineRefused,
        workstation.WorkstationError,
    ) as exc:
        log.error('%s', exc)
    return REFUSED
