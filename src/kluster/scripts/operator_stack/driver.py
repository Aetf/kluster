"""A run of one operator stack: `plan`, `up`, and everything else passed to `pulumi`.

What every run shares (framework/pulumi.md §3.3):

-   **The environment is the driver's**, set on the process it starts: the
    stack's backend and the operator passphrase, and none of the
    caller's variables that could steer `pulumi` or its backend
    (`kluster.lib.stack_environment`).
-   **The stack is the driver's argument**, never a flag it passes through,
    and a run refuses inside a `jj` workspace under `.claude/`, where the
    slots do not answer.
-   **`plan` is a refreshed preview** that exits 0 when nothing is planned
    and 1 when something is. **`up` refreshes, previews, asks, and applies**,
    and runs no `up` at all when nothing is planned: an `up` over nothing
    would still rewrite a committed checkpoint's timestamp and every
    ciphertext in it.
-   **Anything else goes to `pulumi`** under the same environment, but an
    `import` or a `stack import` into a stack whose state is committed, which
    would record what it brings in without the program's secret markings.
-   **A ^C is `pulumi`'s to answer** (`Cli`).
-   **A stack whose state is committed** is checked before every command and
    after every command that can write (`checkpoint`).
-   **A stack with a gate of its own** has it applied around `plan` and `up`:
    the `state-backend` stack's, which holds a replacement of the box for
    `--force` and reads the estate's backend after every run (`appliance`).
    The replacement permission the gate grants is set on the one `pulumi`
    process `up --force` starts, and removed from every other `pulumi` run of
    the stack, so a caller's shell that carries it grants nothing.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess as sp
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from kluster.conventions import identity
from kluster.lib import stack_environment
from kluster.lib.state_backend import permission
from kluster.scripts.operator_stack import appliance, checkpoint

log = logging.getLogger(__name__)

#: `plan`'s answers, and `up`'s when it applies nothing.
NOTHING_PLANNED = 0
PLANNED = 1
#: A refusal, a failed preview, or a committed checkpoint that failed its
#: checks: nothing the operator can push as it stands.
FAILED = 2
#: A gated stack's answers after the run, whatever it planned or applied: the
#: estate's backend answers and serves no stack -- a box owed a restore, or a
#: site before its first `stack init` -- or it does not answer at all
#: (`appliance.Gate.backend`).
RESTORE_OWED = 3
BACKEND_SILENT = 4

#: How long one captured `pulumi` query — an export, a config read — may take.
#: Every one of them reads the backend, so this is a network timeout.
TIMEOUT = 120

#: The step operations that change nothing: the engine's own `HasChanges` in
#: `pkg/engine/update.go` counts every operation in a summary but these. A
#: refresh's own steps are not in the summary at all.
NO_CHANGE = frozenset({'same', 'read', 'discard', 'read-replacement'})

#: What makes `pulumi preview --json` print the engine's events, one JSON
#: object per line, rather than one document at the end: the variable the
#: flag's own help names. The events are what carry the stack's outputs
#: before and after, which the single document does not.
STREAMING_JSON_ENV = 'PULUMI_ENABLE_STREAMING_JSON_PREVIEW'


class Refused(RuntimeError):
    """The run would not start, or `pulumi` failed where the driver needed an answer."""


class Pulumi(Protocol):
    """How the driver starts `pulumi`. Substituted in tests."""

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        """Run `pulumi` with the terminal's own output, returning its exit code."""
        ...

    def events(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> tuple[int, str]:
        """Run `pulumi` with its standard output captured and its standard error on the terminal."""
        ...

    def capture(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
        """Run `pulumi` for its standard output, refused as `Refused` when it fails."""
        ...


class Cli:
    """The pinned `pulumi` on `PATH`, started with exactly the environment it is handed.

    **A ^C is `pulumi`'s to answer.** The terminal sends SIGINT to the whole
    foreground process group, `pulumi` included, and `pulumi` answers the
    first one by cancelling gracefully: it finishes the steps in flight,
    records them, and releases its lock. So while a run is in progress the
    driver ignores SIGINT itself, and waits for `pulumi` to finish rather than
    killing it — `subprocess.run` would kill it a quarter of a second into
    that cancel, leaving the lock and a pending operation behind. The child
    gets SIGINT back at its default before it starts, since an ignored signal
    is inherited across `exec`.
    """

    def stream(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> int:
        return self._run(args, cwd=cwd, env=env, stdout=None)[0]

    def events(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> tuple[int, str]:
        return self._run(args, cwd=cwd, env=env, stdout=sp.PIPE)

    @staticmethod
    def _run(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout: int | None) -> tuple[int, str]:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with sp.Popen(
                ['pulumi', *args],
                cwd=cwd,
                env=dict(env),
                stdout=stdout,
                text=True,
                preexec_fn=_default_sigint,
            ) as child:
                printed, _ = child.communicate()
                return child.returncode, printed or ''
        finally:
            _ = signal.signal(signal.SIGINT, previous)

    def capture(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> str:
        completed = sp.run(
            ['pulumi', *args, '--non-interactive'],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        if completed.returncode != 0:
            # Standard error, never standard output: every query here is one
            # whose output carries secrets.
            raise Refused(f'`pulumi {" ".join(args[:2])}` failed: {completed.stderr.strip() or completed.returncode}')
        return completed.stdout


def _default_sigint() -> None:
    _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


def ask(question: str) -> bool:
    """A yes from the terminal; anything else, an end of input included, is a no."""
    try:
        return input(f'{question} [y/N] ').strip().lower() in {'y', 'yes'}
    except EOFError:
        return False


@dataclass(frozen=True)
class Step:
    """One step a preview planned, as its engine event records it."""

    op: str
    urn: str
    type: str
    #: The resource's state before the step and after it:
    #: `apitype.StepEventStateMetadata`, whose `inputs` and `outputs` are the
    #: resource's own. Empty where the step has no such side, as a create has
    #: no state before it.
    old: Mapping[str, Any] = field(default_factory=dict[str, Any])
    new: Mapping[str, Any] = field(default_factory=dict[str, Any])
    #: The top-level properties the provider names as different.
    diffs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Preview:
    """What a refreshed preview planned, read from its engine events."""

    #: The summary's count of steps by operation.
    changes: Mapping[str, int]
    #: Every step the preview planned, as its operation and the resource's URN.
    steps: tuple[tuple[str, str], ...] = ()
    #: The same steps, whole.
    details: tuple[Step, ...] = ()
    #: What the refresh, or an import, read of each resource, by URN: its
    #: outputs as the provider answers them now.
    read: Mapping[str, Mapping[str, object]] = field(default_factory=dict[str, Mapping[str, object]])
    #: The stack's outputs as the program computes them.
    stack_outputs: Mapping[str, object] = field(default_factory=dict[str, object])
    #: The stack outputs whose value would change. The engine counts no step
    #: for them — the stack resource is `same` — but an `up` writes them. A
    #: secret output reads as the same placeholder before and after, so a
    #: change to one alone is not among these.
    outputs: tuple[str, ...] = ()
    #: The resources whose options alone would change, as their URN and the
    #: option (`OPTIONS`). The engine counts a `same` step for each, and an
    #: `up` writes the new options.
    options: tuple[tuple[str, str], ...] = ()
    #: What the engine reported beside the steps: warnings and errors.
    messages: tuple[str, ...] = ()
    #: The same, each with the URN it is about (empty for none).
    diagnostics: tuple[tuple[str, str], ...] = ()

    @property
    def planned(self) -> bool:
        return (
            bool(self.outputs)
            or bool(self.options)
            or any(count for op, count in self.changes.items() if op not in NO_CHANGE)
        )


#: The options a step event carries in a resource's state before and after:
#: `apitype.StepEventStateMetadata`, whose other fields are the resource's
#: identity, its inputs and its outputs. Every other option — `dependsOn`,
#: `deleteBeforeReplace`, `ignoreChanges`, `replaceOnChanges`,
#: `additionalSecretOutputs`, `aliases`, `customTimeouts` — is absent from
#: the events, so a change to one of those alone reads as nothing planned
#: (framework/pulumi.md §3.3).
OPTIONS = ('protect', 'retainOnDelete', 'provider', 'parent')


def _changed_options(metadata: Mapping[str, Any]) -> list[str]:
    """The `OPTIONS` a step's state has one value of before it and another after it.

    An option left unset and one set to its default are the same: `protect`
    absent and `protect: false` both mean unprotected.
    """
    old = cast('dict[str, object]', metadata.get('old') or {})
    new = cast('dict[str, object]', metadata.get('new') or {})
    return [option for option in OPTIONS if (old.get(option) or None) != (new.get(option) or None)]


#: The type of the resource every stack has, whose outputs are the stack's.
STACK_TYPE = 'pulumi:pulumi:Stack'

#: The operations whose outputs are what the provider answers now: a refresh's
#: read of a resource the state holds, and an import's of one it does not.
READS = frozenset({'refresh', 'import', 'import-replacement'})


def read_preview(printed: str) -> Preview:
    """The preview recorded in `pulumi preview --json`'s streamed events, one JSON object per line.

    **It fails closed.** "Nothing planned" is the one wrong answer that stays
    quiet, so every way the events can stop saying what was planned is a
    refusal rather than an empty plan: a line that is not an event, no summary
    (`pulumi` writes one at the end of every preview that ran), a summary with
    no count of steps or one that counts none (a real preview counts the stack
    itself), and a summary that counts no change while a step event names one.
    """
    steps: list[tuple[str, str]] = []
    details: list[Step] = []
    read: dict[str, Mapping[str, object]] = {}
    stack_outputs: dict[str, object] = {}
    options: list[tuple[str, str]] = []
    outputs: set[str] = set()
    messages: list[str] = []
    diagnostics: list[tuple[str, str]] = []
    summary: object = None
    for number, line in enumerate(printed.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = cast('dict[str, Any]', json.loads(line))
        except ValueError as exc:
            raise Refused(
                f'line {number} of the preview is not an engine event, so what it planned is unknown'
            ) from exc
        if (pre := event.get('resourcePreEvent')) is not None:
            metadata = cast('dict[str, Any]', pre.get('metadata') or {})
            if metadata.get('op') != 'refresh':
                steps.append((str(metadata.get('op')), str(metadata.get('urn'))))
                details.append(
                    Step(
                        op=str(metadata.get('op')),
                        urn=str(metadata.get('urn')),
                        type=str(metadata.get('type')),
                        old=cast('dict[str, Any]', metadata.get('old') or {}),
                        new=cast('dict[str, Any]', metadata.get('new') or {}),
                        diffs=tuple(str(key) for key in cast('list[object]', metadata.get('diffs') or [])),
                    )
                )
            if metadata.get('op') == 'same':
                options += [(str(metadata.get('urn')), option) for option in _changed_options(metadata)]
        if (done := event.get('resOutputsEvent')) is not None:
            metadata = cast('dict[str, Any]', done.get('metadata') or {})
            if metadata.get('type') == STACK_TYPE and metadata.get('op') != 'refresh':
                old = cast('dict[str, object]', cast('dict[str, Any]', metadata.get('old') or {}).get('outputs') or {})
                new = cast('dict[str, object]', cast('dict[str, Any]', metadata.get('new') or {}).get('outputs') or {})
                outputs |= {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
                stack_outputs = dict(new)
            elif metadata.get('op') in READS:
                new = cast('dict[str, Any]', metadata.get('new') or {})
                read[str(metadata.get('urn'))] = cast('dict[str, object]', new.get('outputs') or {})
        if (diagnostic := event.get('diagnosticEvent')) is not None and diagnostic.get('severity') != 'debug':
            messages.append(str(diagnostic.get('message', '')).rstrip())
            diagnostics.append((str(diagnostic.get('urn') or ''), messages[-1]))
        if (ended := event.get('summaryEvent')) is not None:
            summary = cast('dict[str, Any]', ended).get('resourceChanges')
    if summary is None:
        raise Refused('the preview recorded no summary of its steps, so what it planned is unknown')
    if not isinstance(summary, dict) or not all(
        isinstance(count, int) for count in cast('dict[str, object]', summary).values()
    ):
        raise Refused(f'the preview summarized its steps as {summary!r}, which is not a count by operation')
    changes = cast('dict[str, int]', summary)
    if sum(changes.values()) == 0:
        raise Refused('the preview counted no step at all, not even the stack, so what it planned is unknown')
    preview = Preview(
        changes=changes,
        steps=tuple(steps),
        details=tuple(details),
        read=read,
        stack_outputs=stack_outputs,
        outputs=tuple(sorted(outputs)),
        options=tuple(options),
        messages=tuple(messages),
        diagnostics=tuple(diagnostics),
    )
    changing = [step for step in steps if step[0] not in NO_CHANGE]
    if changing and not any(count for op, count in changes.items() if op not in NO_CHANGE):
        raise Refused(f'the preview names {len(changing)} changing steps and its summary counts none')
    return preview


def show(preview: Preview) -> None:
    """Print what `preview` planned: the engine's messages, each changing step, each changed stack output, and the count."""
    for message in preview.messages:
        print(message)
    for op, urn in preview.steps:
        if op not in NO_CHANGE:
            print(f'  {op:>20}  {urn}')
    for key in preview.outputs:
        print(f'  {"output changes":>20}  {key}')
    for urn, option in preview.options:
        print(f'  {option + " changes":>20}  {urn}')
    print('  ' + ', '.join(f'{count} {op}' for op, count in sorted(preview.changes.items())))


@dataclass
class Run:
    """One operator stack, run from one checkout."""

    stack: str
    checkout: Path
    #: The variables the stack's runs are given (`stack_environment`).
    variables: Mapping[str, str] = field(repr=False)
    pulumi: Pulumi = field(default_factory=Cli)
    confirm: Callable[[str], bool] = ask
    #: The environment the driver itself was started in, which every run
    #: inherits less what `stack_environment` decides.
    base: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    #: The stack's own gate, where it has one (`appliance`).
    gate: appliance.Gate | None = None

    @classmethod
    def open(cls, stack: str, checkout: Path, **overrides: Any) -> Run:
        """The run of `stack` from `checkout`, refused where the checkout holds no slots.

        A `jj` workspace sits under the primary checkout's `.claude/`, holds no
        slots of its own, and is not where a checkpoint lands, so a run there
        is refused before any slot is looked for.
        """
        if '.claude' in checkout.absolute().parts:
            raise Refused(
                f'{checkout} is under a .claude/ directory, which is where jj workspaces live: the slots do not '
                'answer there. Run from the checkout that holds .credentials/.'
            )
        if stack == appliance.STACK and 'gate' not in overrides:
            overrides['gate'] = appliance.Gate.for_checkout(checkout)
        run = cls(
            stack=stack, checkout=checkout, variables=stack_environment.operator_variables(stack, checkout), **overrides
        )
        if run.committed:
            # A `file://` backend refuses a root that does not exist, and the
            # directory holds no file before the first `stack init` writes one.
            (checkout / stack_environment.CHECKPOINTS).mkdir(exist_ok=True)
        return run

    @property
    def env(self) -> dict[str, str]:
        """The environment every `pulumi` run of the stack is given, the replacement permission never among it.

        `up --force` grants the permission on the one `pulumi up` it starts
        (`_write`); a caller's shell that exported it grants nothing, to a
        passed-through `up` least of all. The backend read and the `git` and
        `jj` checks start processes of their own environment, which run no
        program and so act on no permission.
        """
        env = stack_environment.process(self.base, self.variables)
        _ = env.pop(permission.ENV, None)
        return env

    @property
    def committed(self) -> bool:
        return stack_environment.home(self.stack) is identity.StateHome.COMMITTED

    def plan(self) -> int:
        """Preview with a refresh; 0 when nothing is planned, 1 when something is.

        A gated stack answers 3 or 4 instead where the estate's backend serves
        no stack or does not answer (`_settle`).
        """
        self._require_current()
        if self.gate is not None:
            self.gate.ready()
        if (recorded := self._recorded()) is not None:
            log.warning(
                '%s stands: the checkpoint failed its checks, or a write was never checked. '
                '`operator-stack %s up` runs the checks again: %s',
                recorded.relative_to(self.checkout),
                self.stack,
                recorded.read_text().strip(),
            )
        preview = self._preview()
        if self.gate is not None:
            self.gate.report(preview)
        return self._settle(PLANNED if preview.planned else NOTHING_PLANNED)

    def up(self, *, yes: bool, force: bool = False, replace: bool = False) -> int:
        """Preview with a refresh, and apply what it planned once the operator agrees.

        With nothing planned no `up` runs, and the answer is `plan`'s — unless
        a committed checkpoint's record stands, in which case the `up` is what
        rewrites the file under the program's current secret markings and
        checks it again.

        A gated stack holds a create, replacement or delete of its box for
        `force`, naming what moved, and refuses a create beside a held address
        whatever it is given; `replace` replaces the box whether or not
        anything moved, and grants the permission as `force` does.
        """
        if (force or replace) and self.gate is None:
            raise Refused(
                f'--force and --replace hold a replacement of the state-backend box; the {self.stack} stack has none'
            )
        self._require_current()
        if self.gate is not None:
            self.gate.ready()
        preview = self._preview()
        urn: str | None = None
        if replace and self.gate is not None:
            urn = appliance.instance_urn(preview)
            log.info('previewing again with the box replaced: %s', urn)
            preview = self._preview(replace=urn)
        recorded = self._recorded()
        if self.gate is not None:
            if (beside := appliance.creates_beside_held_address(preview)) is not None:
                raise Refused(beside)
            if refused := appliance.imported_replacements(preview):
                raise Refused(appliance.refuse_imported_replacements(refused))
            self.gate.report(preview)
            box = appliance.box_steps(preview)
            if box and not (force or replace):
                log.error(
                    'this run would create, replace or delete the %s box, for %s; nothing is applied. `%s` is the run '
                    'that does, dumping the box before it goes and restoring into the new one',
                    self.stack,
                    ', '.join(appliance.moved(box)),
                    permission.REMEDY,
                )
                return PLANNED
        if not preview.planned and recorded is None:
            log.info('nothing is planned for the %s stack; nothing to apply', self.stack)
            return self._settle(NOTHING_PLANNED)
        if preview.planned and not yes and not self.confirm(f'apply these changes to the {self.stack} stack?'):
            log.info('nothing applied to the %s stack', self.stack)
            return PLANNED
        if not preview.planned:
            log.info('nothing is planned, but %s stands: running up to rewrite the checkpoint and check it', recorded)
        log.info('applying the %s stack', self.stack)
        granted = {permission.ENV: permission.GRANTED} if force or replace else {}
        replacing = ['--replace', urn] if urn is not None else []
        return self._settle(self._write(['up', '--refresh', '--yes', '--skip-preview', *replacing], granted=granted))

    def passthrough(self, args: Sequence[str]) -> int:
        """`pulumi <args>` against this stack, under its environment."""
        if not args or args[0].startswith('-'):
            raise Refused(
                f'name the pulumi command first, as in `operator-stack {self.stack} pulumi stack --show-urns`'
            )
        flags = _flags(args)
        words = _words(args)
        copies = words[0] == 'config' and 'cp' in words[1:]
        for arg in flags:
            if _names_a_stack(arg) or (copies and _names_a_destination(arg)):
                raise Refused(f'`{arg}` names a stack, or may: this run is against {self.stack} alone')
        imports = words[0] == 'import' or (words[0] == 'stack' and 'import' in words[1:])
        if self.committed and imports:
            raise Refused(
                f'the {self.stack} stack keeps its state in this repository, and `pulumi {" ".join(args)}` '
                "would record what it brings in without the program's secret markings: import through the "
                "program's `import_` option"
            )
        self._require_current()
        return self._write(list(args))

    # ----------------------------------------------------------------------

    def _with_stack(self, args: Sequence[str]) -> list[str]:
        """`args` with `--stack <this stack>` ahead of any `--`, past which a word is not a flag."""
        args = list(args)
        at = args.index('--') if '--' in args else len(args)
        return [*args[:at], '--stack', self.stack, *args[at:]]

    def _require_current(self) -> None:
        if self.committed:
            log.info("checking the working copy against the forge's main")
            try:
                checkpoint.require_current(self.checkout, stack_environment.checkpoint(self.checkout, self.stack))
            except checkpoint.CheckRefused as exc:
                raise Refused(str(exc)) from exc

    def _settle(self, code: int) -> int:
        """`code`, unless the gate reads the estate's backend as owed a restore or not answering.

        A refusal or a failed check keeps its own answer: the checkpoint is
        not to be pushed, whatever the backend holds.
        """
        if self.gate is None or code == FAILED:
            return code
        match self.gate.backend():
            case appliance.Backend.EMPTY:
                return RESTORE_OWED
            case appliance.Backend.SILENT:
                return BACKEND_SILENT if code in (NOTHING_PLANNED, PLANNED) else code
            case appliance.Backend.SERVING:
                return code

    def _preview(self, *, replace: str | None = None) -> Preview:
        """Run the refreshed preview, show what it planned, and return it.

        Read from `pulumi preview --json` with its events streamed
        (`STREAMING_JSON_ENV`), the documented machine-readable form; the
        engine's own messages, and `pulumi`'s errors, reach the terminal.
        """
        log.info('previewing the %s stack with a refresh', self.stack)
        replacing = ['--replace', replace] if replace is not None else []
        code, printed = self.pulumi.events(
            self._with_stack(['preview', '--refresh', '--json', *replacing]),
            cwd=self.checkout,
            env=self.env | {STREAMING_JSON_ENV: 'true'},
        )
        if code != 0:
            raise Refused(f'the preview of the {self.stack} stack failed (exit {code})')
        preview = read_preview(printed)
        show(preview)
        return preview

    def _recorded(self) -> Path | None:
        """This stack's record of a failed or unfinished check, where one stands."""
        if not self.committed:
            return None
        record = checkpoint.record(self.checkout, self.stack)
        return record if record.is_file() else None

    def _export(self) -> dict[str, Any]:
        return cast('dict[str, Any]', json.loads(self._query('stack', 'export', '--show-secrets')))

    def _query(self, *args: str) -> str:
        return self.pulumi.capture(self._with_stack(args), cwd=self.checkout, env=self.env)

    def _write(self, args: Sequence[str], *, granted: Mapping[str, str] | None = None) -> int:
        """Run a command that can write, and hold a committed checkpoint to what it may publish.

        **The record goes down before the command and comes off only when the
        checks pass.** Whatever stops the run between the write and the checks
        — a ^C, a query that fails or times out — then leaves the record
        standing, and the next `up` checks the file again rather than finding
        nothing to do.
        """
        env = self.env | dict(granted or {})
        if not self.committed:
            return self.pulumi.stream(self._with_stack(args), cwd=self.checkout, env=env)
        record = checkpoint.record(self.checkout, self.stack)
        if not record.is_file():
            _ = record.write_text(f'unchecked: `pulumi {" ".join(args)}` may have written the checkpoint\n')
        path = stack_environment.checkpoint(self.checkout, self.stack)
        before = path.read_bytes() if path is not None else None
        started = self._export() if path is not None else None
        code = self.pulumi.stream(self._with_stack(args), cwd=self.checkout, env=env)
        log.info('checking the %s checkpoint', self.stack)
        findings = [
            checkpoint.Finding(
                str(stray.relative_to(self.checkout)), 'is not a file the checks read, and must not be pushed'
            )
            for stray in checkpoint.strays(self.checkout, self.stack)
        ]
        path = stack_environment.checkpoint(self.checkout, self.stack)
        if path is not None:
            ended = self._export()
            rewritten = before is not None and path.read_bytes() != before
            if rewritten and before is not None and started is not None and checkpoint.same_deployment(started, ended):
                _ = path.write_bytes(before)
                log.info('the deployment is unchanged; %s keeps the bytes it had', path.relative_to(self.checkout))
            findings += self._findings(path, started, ended)
        if findings:
            _ = record.write_text(''.join(f'{finding}\n' for finding in findings))
            for finding in findings:
                log.error('%s', finding)
            log.error(
                'the %s checkpoint must not be pushed as it stands. Mark each property above secret in the program, '
                'remove each stray file, and run `operator-stack %s up`: it checks again even with nothing planned '
                'while %s stands.',
                self.stack,
                self.stack,
                record.relative_to(self.checkout),
            )
            return FAILED
        record.unlink(missing_ok=True)
        return code

    def _findings(
        self, path: Path, started: Mapping[str, Any] | None, ended: Mapping[str, Any]
    ) -> list[checkpoint.Finding]:
        """What the checkpoint would publish that it must not: secret values in the clear, and declared-secret properties in the clear.

        The secrets searched for are the configuration's and the state's
        both before and after the command: a value whose only marking the
        command removed — a provider release that stops marking a field, a
        `stack import` of an edited export — is secret before it and plain
        after it.
        """
        document = cast('dict[str, Any]', json.loads(path.read_text()))
        config = cast('dict[str, Any]', json.loads(self._query('config', '--show-secrets', '--json') or '{}'))
        secrets = checkpoint.config_secrets(config) | checkpoint.export_secrets(ended)
        if started is not None:
            known = set(secrets.values())
            for where, value in checkpoint.export_secrets(started).items():
                if value not in known:
                    secrets[f'{where} before the command'] = value
        return checkpoint.cleartext(document, secrets) + checkpoint.undeclared(document)


def _flags(args: Sequence[str]) -> list[str]:
    """The words of `args` ahead of a `--`, past which a word is an argument rather than a flag."""
    return list(args[: args.index('--')] if '--' in args else args)


def _words(args: Sequence[str]) -> list[str]:
    """Every word of `args` ahead of a `--` that is not a flag.

    `pulumi` takes a flag anywhere among a command's words, so a refusal that
    read only the words ahead of the first flag would miss
    `stack --color=never import`. A flag's value written as a word of its own
    (`--color never`) is among these too, since which flags take a value is
    `pulumi`'s to know: a refusal asks whether a word is among them rather
    than where it stands, which such a value cannot hide.
    """
    return [arg for arg in _flags(args) if not arg.startswith('-')]


def _names_a_stack(arg: str) -> bool:
    """Whether `arg` can name a stack: `--stack` in either spelling, or a short-flag cluster holding an `s`.

    `-s dev`, `-sdev` and `-ys dev` all reach the `-s` shorthand, so any single
    dash cluster with an `s` in it is refused; a short flag's value is written
    as `--flag=value` or as a word of its own instead.
    """
    if arg == '--stack' or arg.startswith('--stack='):
        return True
    return arg.startswith('-') and not arg.startswith('--') and 's' in arg[1:]


def _names_a_destination(arg: str) -> bool:
    """Whether `arg` is `config cp`'s `--dest`, which names the stack it copies into, or its `-d` shorthand."""
    if arg == '--dest' or arg.startswith('--dest='):
        return True
    return arg.startswith('-') and not arg.startswith('--') and 'd' in arg[1:]
