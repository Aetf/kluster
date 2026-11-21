"""How a step of the rebuild starts a command, and the environment the command gets.

**Every command is a pinned tool on `PATH`, or one of the host's.** The
drill runs as `mise x -- uv run drill rebuild`, from the root of the checkout
that holds `.credentials/`: `mise x` puts the pins of `mise.toml` on `PATH`,
`gh` among them, and `uv run` the project's console scripts, `credentials`
and `perf` among them, so a step names each tool bare and starts the pin.
`HOST_TOOLS` are the commands found on `PATH` by the same name with no pin
behind them, which run the host's own release.

**A command is started in one of three modes** (`Mode`), and its transcript
keeps what that mode lets it keep:

-   `CAPTURED`: read for its output, which goes to the transcript unless the
    step marks it secret, when the transcript says it withheld it. Bounded,
    since such a read answers in seconds.
-   `SHOWN`: what the operator reads as it happens, a preview's diff, a wait;
    standard error is merged into standard output, which is echoed line by
    line and kept.
-   `ATTACHED`: the operator's terminal, for a command that asks something of
    a person -- a `pulumi up`'s confirmation, a kit's master password, a
    webhook's id. Nothing it prints is kept.

**A ^C is the command's to answer**, in every mode, as in `ci-stack`
(`manual_run.attached`): `pulumi` answers the first SIGINT by cancelling
gracefully, so while a command runs the drill ignores SIGINT and waits for
it, and the command gets SIGINT back at its default before it starts.

**The environment is chosen per stack** (`manual_run.Environments`):
`physical`'s passphrase where the stack is `physical`, `mise.toml`'s for every
other stack, through `credentials`'s `BackendEnvironment`, which is the one
place that maps a stack to its passphrase, and a `pulumi` run whose
environment names no backend is refused. CI's Helm environment is laid under
every command (`manual_run.ci_helm`).
"""

from __future__ import annotations

import enum
import signal
import subprocess as sp
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kluster.scripts import manual_run
from kluster.scripts.drill import record

#: The commands a step starts from the host's `PATH`, with no pin of
#: `mise.toml` behind them: `ssh`, which reaches the gateway with the
#: operator's own keys and configuration, and `curl`. Every other command a
#: step starts is a pin or a console script of the project.
HOST_TOOLS = frozenset({'curl', 'ssh'})

#: The bound of a `CAPTURED` command: a stop-loss, since every such read
#: answers within seconds. A `pulumi` export reads the backend, which is the
#: slowest of them.
CAPTURED_TIMEOUT = 300.0


class Mode(enum.Enum):
    CAPTURED = 'captured'
    SHOWN = 'shown'
    ATTACHED = 'attached'


@dataclass(frozen=True)
class Completed:
    """A command that ran: its exit, and what it printed where its mode keeps that."""

    argv: tuple[str, ...]
    exit: int
    out: str = ''
    err: str = ''

    @property
    def ok(self) -> bool:
        return self.exit == 0

    @property
    def said(self) -> str:
        """The last line of standard error, or the exit where there is none: what a refusal quotes."""
        lines = self.err.strip().splitlines()
        return lines[-1] if lines else f'exit {self.exit}'


class Exec(Protocol):
    """How a command is started. Substituted in tests."""

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, mode: Mode) -> Completed: ...


def shell(argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, mode: Mode) -> Completed:
    """Start `argv` in `mode`, with SIGINT left to it while it runs; a command not found exits 127."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if mode is Mode.CAPTURED:
            done = sp.run(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=sp.DEVNULL,
                capture_output=True,
                text=True,
                timeout=CAPTURED_TIMEOUT,
                check=False,
                preexec_fn=manual_run.default_sigint,
            )
            return Completed(tuple(argv), done.returncode, done.stdout, done.stderr)
        if mode is Mode.ATTACHED:
            return Completed(tuple(argv), manual_run.attached(argv, env=env, cwd=cwd))
        lines: list[str] = []
        with sp.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=sp.DEVNULL,
            stdout=sp.PIPE,
            stderr=sp.STDOUT,
            text=True,
            preexec_fn=manual_run.default_sigint,
        ) as child:
            assert child.stdout is not None
            for line in child.stdout:
                _ = sys.stdout.write(line)
                lines.append(line)
            _ = sys.stdout.flush()
        return Completed(tuple(argv), child.returncode, ''.join(lines))
    except FileNotFoundError as exc:
        return Completed(tuple(argv), 127, '', str(exc))
    except sp.TimeoutExpired as exc:
        return Completed(tuple(argv), 124, '', f'stopped at its bound of {exc.timeout:g}s')
    finally:
        _ = signal.signal(signal.SIGINT, previous)


@dataclass
class Session:
    """The commands one step starts, each written to the step's transcript."""

    checkout: Path
    transcript: record.Transcript
    environments: manual_run.Environments
    exec: Exec = shell

    def run(
        self,
        argv: Sequence[str],
        *,
        stack: str | None = None,
        mode: Mode = Mode.CAPTURED,
        secret: bool = False,
        quiet: bool = False,
    ) -> Completed:
        """Start `argv` from the checkout and write it to the transcript; `stack` picks a `pulumi` environment.

        A `secret` command is `CAPTURED`, and its transcript keeps its exit and
        none of what it printed. Its standard error is not handed back either,
        so a refusal built from it names the exit alone. A `quiet` one keeps
        the size of what it printed in place of an export too long to read.
        """
        env = self.environments.pulumi(stack) if stack is not None else self.environments.tool()
        if secret:
            mode = Mode.CAPTURED
        started = record.now()
        done = self.exec(argv, env=env, cwd=self.checkout, mode=mode)
        if secret:
            printed = record.WITHHELD
            done = Completed(done.argv, done.exit, done.out)
        elif quiet:
            printed = f'(output not kept: {len(done.out)} characters){chr(10) + done.err if done.err.strip() else ""}'
        elif mode is Mode.ATTACHED:
            printed = record.ATTACHED
        else:
            printed = done.out + (f'\n{done.err}' if done.err.strip() else '')
        self.transcript.command(argv, started=started, exit=done.exit, printed=printed)
        return done
