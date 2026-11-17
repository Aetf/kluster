"""What an `up` would write that its preview plans no step for: the second preview, and its reading field by field.

A preview's own exit counts only steps, and an `up` that creates, updates,
replaces and deletes nothing still writes the state the program registers:
a resource option moved on a `same` step -- `protect`, `retainOnDelete`,
`dependsOn`, an alias -- and an output a resource registers, a component's
as well as the stack's. Upstream calls these state-only changes. Where a
caller's first preview plans no step, a **second preview** reads them
(`arguments`, `ENVIRONMENT`): `pulumi preview --json --show-sames
--expect-no-changes --event-log <file>`, with no refresh, the caller adding
the stack in its own form and running it through its own runner. `answer`
reads what it recorded: the fields an `up` writes with no step (`Fields`,
none being the answer that it writes nothing), or a step the first preview
did not plan (`Stepped`), when it exited `REFUSED`. Any other exit is
`Failed`, carrying the diagnostics its document records, and a document or
an event log that does not say is `Unreadable`.

Two surfaces of that one preview carry the fields, and neither carries all
of them:

-   **The JSON document** (`--json --show-sames`) has a step per resource
    with the whole state before it and after it, `oldState` and `newState`,
    every option field included. Its outputs are not there: the digest takes
    an outputs event only for a refresh.
-   **The engine's event log** (`--event-log`) has, in the last
    `resOutputsEvent` of each resource, the outputs it held and the ones it
    registers, a stack's and a component's included.

**The comparison is a definition, not a list of options.** Every field of a
step's new state is compared with the same field of its old state, except the
fields a `same` step does not take from the program (`EXCLUDED`), and every
resource's old and new outputs are compared. The exclusions are the
enumerated side, so a field a later release adds to the state is compared by
default, and the answer errs toward an `up` rather than away from one.

**The one change neither caller plans** is a secret an `up` writes with no
step -- in practice an output the stack or a component registers -- whose
value alone changed: both surfaces carry it as the same placeholder before
and after, so it compares equal, and `pulumi up` is what writes that secret.
A secret input is not that change: the provider, or for a component the
engine, compares its plaintext, and a change to its value alone is a step.

**A value prints as the CLI serialized it**, and the CLI serializes a secret
as the literal `SECRET` unless it is asked to show secrets, which nothing
here asks. A secret in the event log is an object carrying the secret
signature (`SECRET_SIG`), and it prints as `SECRET` whatever it holds beside
the signature.

**This module is a layer that stands in for a flag the CLI does not have**
(style/README.md, "A workaround for an upstream defect is a layer of its
own"). It has two callers: `stack-plan`, the command the deploy chain reads
(`kluster.scripts.stack_plan`), and the operator driver
(`kluster.scripts.operator_stack.driver`), each of which runs the second
preview through its own runner. **The layer is the reading of the second
preview, not the preview**: whatever reads its document or its event log,
or exists only so that they can be read, in either caller. Today that is:

-   this module, but for `REFUSED`, which every route below still reads,
    and which moves to the reader that survives;
-   `src/kluster/scripts/stack_plan/`, the `stack-plan` entry in
    `pyproject.toml`'s `[project.scripts]`, its name in `AGENTS.md`'s list
    of console scripts, and every step that runs `stack-plan`;
-   in `src/kluster/scripts/operator_stack/driver.py`, how `Run._unrefreshed`
    reads the preview: the document and the event log, the temporary
    directory that holds the log, the section it prints, and the refusals
    only that reading makes (`Why.UNREFRESHED_UNREADABLE`,
    `Why.UNREFRESHED_LOG_IN_CHECKOUT`); and the sentences that say so in
    the docstrings of the module, `Pulumi` (why the preview is given no
    `doing`), `Run.plan`, `Run.up` and `Run._unrefreshed`;
-   `src/kluster/scripts/operator_stack/progress.py`'s paragraph on why
    that preview is announced and not narrated;
-   `tests/test_stack_plan.py`, `tests/test_stack_plan_engine.py`, and in
    `tests/test_operator_stack.py` the fake-`pulumi` cases of the second
    preview's fields and of the refusals only its reading makes, the case
    that holds the preview is given no `doing`
    (`test_the_preview_without_a_refresh_is_given_no_progress_of_its_own`),
    the `state_only.DEBUG_COMMANDS_ENV` entry of `SET_BY_THE_DRIVER`, the
    fake's support for them (`Unrefreshed`, `SUMMARY`, the `--refresh` dispatch in
    `FakePulumi.events` and `FakePulumi._without_refresh`,
    `FakePulumi.previewed_without_refresh`), the pass-through of a
    document in `AsTheBox.events`, and the section assertions of the
    real-engine cases (`test_a_real_change_no_step_carries_is_planned_shown_and_applied`,
    `test_a_real_plan_that_rewrites_beside_a_secret_prints_no_secret`);
-   the paragraph of framework/testing.md §8 naming the engine module, and
    that module's entry in the section's census; the sentences of
    framework/pulumi.md §3.3 on how the second preview is read; and the
    naming of `stack-plan` in framework/ci.md, wherever it names it.

**The driver's second preview itself is the driver's own and outlives the
layer**: the preview without a refresh, made where the refreshed one plans
no step (`Run.plan`, `Run.up`), `Preview.stepped`, the verdict that combines
the two, `Why.UNREFRESHED_FAILED`, and the rule framework/pulumi.md §3.3
states, that a value only the refresh moved is not planned on its own.

**It is removed when the pinned CLI offers a preview whose exit status
counts state-only and output-only changes.**

-   Where that is `--expect-no-changes` itself counting them
    (pulumi/pulumi#5087): every step that runs `stack-plan` runs
    `pulumi preview --diff --expect-no-changes` in its place and keeps
    reading 0, 7 or anything else, and the driver's preview without a
    refresh becomes `pulumi preview --expect-no-changes`, read the same way.
    The refreshed preview is not given the flag: it compares the program
    with the state after the refresh, where an output the refresh moved
    differs, and the driver does not plan that (framework/pulumi.md §3.3).
-   Where it is a new flag (pulumi/pulumi#25303's `--detailed-exitcode`):
    the same, with that flag on each of those unrefreshed previews in place
    of `--expect-no-changes`, and each caller's reading of the exit takes in
    whatever statuses the flag brings.

pulumi/pulumi#25423, an update plan that constrains `protect` and not
`retainOnDelete`, is the same gap read from the plan's side.

**The tripwire is the first route's, and covers both callers**:
`tests/test_stack_plan_engine.py`'s
`test_the_bare_preview_still_exits_0_on_a_change_no_step_carries` runs the
very command both callers' second preview becomes, and is red, for each
change it lists, once the pinned preview refuses that change, so the pin
bump that brings the fix fails on it. It cannot see the second route: a new
flag changes no exit that exists today, so that half of the condition
stands in this docstring alone, where a pin bump's reviewer reads it.

**Deleting the layer**, on the bump that meets the condition:

-   **deletes** this module, `stack-plan`'s package, its
    `[project.scripts]` entry and its `AGENTS.md` name (both serialized
    files, so the removal is one pull request holding both), its two test
    modules, the driver's fake-`pulumi` cases of the second preview's fields
    and of the refusals only its reading makes, with `Unrefreshed` and
    `SUMMARY`, the case that holds the preview is given no `doing`, the
    `DEBUG_COMMANDS_ENV` entry of `SET_BY_THE_DRIVER`, the two `Why` members
    of the reading, `progress.py`'s paragraph on the preview's narration, and
    the testing.md §8 paragraph and census entry;
-   **rewrites** the command in every step that runs `stack-plan`;
    `Run._unrefreshed`, to run `pulumi preview --expect-no-changes` with no
    refresh and read 0, 7 or anything else, keeping `REFUSED`, now its own,
    and `Why.UNREFRESHED_FAILED`; the refusal case of a failed exit, which
    keeps that member; the sentences of the driver's docstrings named
    above; `FakePulumi.events`, `_without_refresh` and
    `previewed_without_refresh`, to answer that preview by its exit alone;
    `AsTheBox.events`; the real-engine cases' section assertions, to the
    plan the CLI then prints; pulumi.md §3.3's sentences on how the second
    preview is read; and ci.md's naming of the command;
-   **leaves untouched** the driver's two previews, `Preview.stepped`, the
    calls in `Run.plan` and `Run.up` and the verdict they combine, the rule
    of pulumi.md §3.3, the driver's exit table and its question, its
    `up --refresh --yes --skip-preview`, the deploy chain's reading of 0, 7
    and anything else, and the real-engine cases' verdicts, which hold what
    the driver plans and applies and not how it learns of it.

What an approver or an operator reads for a plan that changes no resource
after the removal is what the CLI then reports: the per-field section goes
with the layer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, final

#: The fields of a resource's state that an `up` of a `same` step does not
#: take from the program. `id` and `outputs` are copied from the old state
#: when the step applies (`SameStep.Apply`, pkg/resource/deploy/step.go);
#: `sourcePosition` and `stackTrace` say where the registration sits, and move
#: with every edit above it. Every other field is compared.
EXCLUDED = frozenset({'id', 'outputs', 'sourcePosition', 'stackTrace'})

#: The field of a resource's state that lists a set, and the field that maps
#: each input to one. The engine gathers their members in a set and lists them
#: in its map's iteration order (`pkg/resource/deploy/source_eval.go`), so two
#: registrations of the same members list them in different orders, and no
#: `up` settles one. They compare by their members; every other list in a
#: state, those inside `inputs` among them, is a value whose order counts.
DEPENDENCIES = 'dependencies'
PROPERTY_DEPENDENCIES = 'propertyDependencies'

#: The key under which an engine event's property value names its special
#: kind, and the kind that makes it a secret (`sig.Key` and `sig.Secret`,
#: sdk/go/common/resource/sig). An asset or an archive carries the same key
#: under kinds of its own.
SIG_KEY = '4dabf18193072939515e22adb298388d'
SECRET_SIG = '1b47061264138c4ac30d75fd1eb44270'

#: How the CLI prints a secret it is not asked to show.
SECRET = '[secret]'


@final
class _Absent:
    """A field one side of a comparison does not have, which is not the same as one holding `null`."""

    def __repr__(self) -> str:
        return 'absent'


ABSENT = _Absent()


class Unreadable(ValueError):
    """The preview's document or its event log does not say what an `up` would write."""


@dataclass(frozen=True)
class States:
    """One step of the JSON preview: a resource's state before the step and after it."""

    urn: str
    old: Mapping[str, object]
    new: Mapping[str, object]


@dataclass(frozen=True)
class Outputs:
    """A resource's outputs before and after, as its last `resOutputsEvent` records them; none on a side with no state."""

    urn: str
    old: Mapping[str, object]
    new: Mapping[str, object]


@dataclass(frozen=True)
class Change:
    """One field an `up` would rewrite: a field of the state, or one key of a map field, by its dotted path."""

    urn: str
    field: str
    old: object
    new: object

    def __str__(self) -> str:
        return f'{self.field}: {shown(self.old)} -> {shown(self.new)}'


def shown(value: object) -> str:
    """`value` as a line of the plan prints it: JSON, with a secret as `SECRET` and a missing field as `absent`."""
    if value is ABSENT:
        return 'absent'
    return json.dumps(_masked(value), sort_keys=True, ensure_ascii=False)


def read_steps(printed: str) -> list[States]:
    """The steps of `pulumi preview --json --show-sames`'s document, each with its two states.

    A step with no state on one side -- a create, a delete -- has an empty one
    there, so every field it has on the other side is a change. A step with
    neither is unreadable: the CLI leaves out a side it fails to serialize.
    """
    try:
        document = json.loads(printed)
    except ValueError as exc:
        raise Unreadable(f'the document is not JSON: {exc}') from exc
    if not isinstance(document, dict):
        raise Unreadable('the document is not an object of steps')
    steps = cast('dict[str, object]', document).get('steps')
    if not isinstance(steps, list):
        raise Unreadable('the document records no list of steps')
    read: list[States] = []
    for number, step in enumerate(cast('list[object]', steps), 1):
        if not isinstance(step, dict):
            raise Unreadable(f'step {number} of the document is not an object')
        entry = cast('dict[str, object]', step)
        urn = entry.get('urn')
        if not isinstance(urn, str):
            raise Unreadable(f'step {number} of the document names no resource')
        if entry.get('oldState') is None and entry.get('newState') is None:
            raise Unreadable(f'step {number} of the document, {urn}, carries neither state')
        read.append(States(urn=urn, old=_state(entry, 'oldState', urn), new=_state(entry, 'newState', urn)))
    return read


def _state(step: Mapping[str, object], side: str, urn: str) -> Mapping[str, object]:
    state = step.get(side)
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise Unreadable(f'the {side} of {urn} in the document is not an object')
    return cast('dict[str, object]', state)


def read_outputs(log: str) -> list[Outputs]:
    """Each resource's outputs from the event log, one JSON event per line, as its last `resOutputsEvent` has them.

    A resource has an outputs event when it registers and another when its
    outputs are registered, so the last is the one that holds what the
    program registers. A log that ends before the preview's summary was cut
    short, and what it would have said is unknown.
    """
    last: dict[str, Outputs] = {}
    summarized = False
    for number, line in enumerate(log.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise Unreadable(f'line {number} of the event log is not an engine event: {exc}') from exc
        if not isinstance(event, dict):
            raise Unreadable(f'line {number} of the event log is not an engine event')
        entry = cast('dict[str, Any]', event)
        summarized = summarized or 'summaryEvent' in entry
        if (done := entry.get('resOutputsEvent')) is None:
            continue
        metadata = cast('object', cast('dict[str, Any]', done).get('metadata') if isinstance(done, dict) else None)
        if not isinstance(metadata, dict):
            raise Unreadable(f'the outputs event on line {number} of the event log carries no step')
        step = cast('dict[str, object]', metadata)
        urn = step.get('urn')
        if not isinstance(urn, str):
            raise Unreadable(f'the outputs event on line {number} of the event log names no resource')
        last.pop(urn, None)
        last[urn] = Outputs(urn=urn, old=_outputs(step, 'old', number), new=_outputs(step, 'new', number))
    if not summarized:
        raise Unreadable('the event log ends before the preview summary, so it was cut short')
    return list(last.values())


def _outputs(step: Mapping[str, object], side: str, number: int) -> Mapping[str, object]:
    state = step.get(side)
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise Unreadable(f'the {side} state of the outputs event on line {number} of the event log is not an object')
    outputs = cast('dict[str, object]', state).get('outputs')
    if outputs is None:
        return {}
    if not isinstance(outputs, dict):
        raise Unreadable(f'the {side} outputs on line {number} of the event log are not an object')
    return cast('dict[str, object]', outputs)


def changes(steps: list[States], outputs: list[Outputs]) -> list[Change]:
    """Every field an `up` would rewrite, grouped by resource: the steps' order first, then the outputs'.

    Every step's resource has an outputs event: the engine writes one after
    each step it completes, and the logger drops an event it fails to write
    and goes on, so a resource without one is a log that lost it.
    """
    unrecorded = [step.urn for step in steps if step.urn not in {resource.urn for resource in outputs}]
    if unrecorded:
        raise Unreadable(f'the event log has no outputs event for {", ".join(unrecorded)}')
    grouped: dict[str, list[Change]] = {}
    for step in steps:
        grouped.setdefault(step.urn, []).extend(
            _fields(step.urn, '', _by_members(step.old), _by_members(step.new), skip=EXCLUDED)
        )
    for resource in outputs:
        grouped.setdefault(resource.urn, []).extend(
            _fields(resource.urn, 'outputs.', resource.old, resource.new, skip=frozenset())
        )
    return [change for found in grouped.values() for change in found]


def _fields(
    urn: str, prefix: str, old: Mapping[str, object], new: Mapping[str, object], *, skip: frozenset[str]
) -> Iterator[Change]:
    """The keys of `old` and `new` whose values differ, a map value opened one level so the change names its key."""
    for key in sorted(old.keys() | new.keys()):
        if key in skip:
            continue
        before, after = old.get(key, ABSENT), new.get(key, ABSENT)
        if _same(before, after):
            continue
        if _map_or_absent(before) and _map_or_absent(after) and not (before is ABSENT and after is ABSENT):
            inner_old = {} if isinstance(before, _Absent) else cast('dict[str, object]', before)
            inner_new = {} if isinstance(after, _Absent) else cast('dict[str, object]', after)
            for inner in sorted(inner_old.keys() | inner_new.keys()):
                was, now = inner_old.get(inner, ABSENT), inner_new.get(inner, ABSENT)
                if not _same(was, now):
                    yield Change(urn=urn, field=f'{prefix}{key}.{inner}', old=was, new=now)
        else:
            yield Change(urn=urn, field=f'{prefix}{key}', old=before, new=after)


def _by_members(state: Mapping[str, object]) -> Mapping[str, object]:
    """`state` with `DEPENDENCIES` and each list of `PROPERTY_DEPENDENCIES` in one order, and each member once."""
    ordered = dict(state)
    if isinstance(listed := ordered.get(DEPENDENCIES), list):
        ordered[DEPENDENCIES] = _members(cast('list[object]', listed))
    if isinstance(mapped := ordered.get(PROPERTY_DEPENDENCIES), dict):
        ordered[PROPERTY_DEPENDENCIES] = {
            key: _members(cast('list[object]', value)) if isinstance(value, list) else value
            for key, value in cast('dict[str, object]', mapped).items()
        }
    return ordered


def _members(listed: list[object]) -> list[object]:
    """The members of `listed`, each once, in the order of their JSON."""
    by_json = {json.dumps(member, sort_keys=True): member for member in listed}
    return [by_json[key] for key in sorted(by_json)]


def _map_or_absent(value: object) -> bool:
    """Absent, or a map of keys: not a secret, an asset or an archive, which are values whole."""
    return value is ABSENT or (isinstance(value, dict) and SIG_KEY not in value)


def _same(before: object, after: object) -> bool:
    """Equal as JSON, where `true` is not `1`: Python's own equality would call those the same."""
    if before is ABSENT or after is ABSENT:
        return before is after
    return json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)


def _masked(value: object) -> object:
    """`value` with every secret in it replaced by `SECRET`."""
    if isinstance(value, dict):
        entry = cast('dict[str, object]', value)
        if entry.get(SIG_KEY) == SECRET_SIG:
            return SECRET
        return {key: _masked(inner) for key, inner in entry.items()}
    if isinstance(value, list):
        return [_masked(inner) for inner in cast('list[object]', value)]
    return value


def section(stack: str, found: Sequence[Change]) -> str:
    """The plan's own section for a plan that changes no resource and rewrites state: each field, under its resource."""
    lines = [f"An `up` of {stack} changes no resource. It rewrites these fields of the stack's state:", '']
    urn = None
    for change in found:
        if change.urn != urn:
            urn = change.urn
            lines.append(f'  {urn}')
        lines.append(f'      {change}')
    return '\n'.join(lines)


#: The CLI's exit when `--expect-no-changes` refuses the steps a preview
#: planned (`ExitNoChanges`, sdk/go/common/util/cmdutil/exit.go).
REFUSED = 7

#: The variable that makes the CLI take `--event-log`, the flag the Automation
#: API reads its events through: without it the flag is unknown and the run
#: fails.
DEBUG_COMMANDS_ENV = 'PULUMI_DEBUG_COMMANDS'

#: What the second preview's environment adds to its caller's, on that run
#: alone.
ENVIRONMENT = {DEBUG_COMMANDS_ENV: 'true'}


def arguments(events: Path) -> list[str]:
    """The second preview's arguments, its event log written to `events`; the caller adds the stack.

    It refreshes nothing: the comparison is the program's registration
    against the state the backend holds, and `--show-sames` puts every
    resource's step, `same` ones included, in the document.
    """
    return ['preview', '--json', '--show-sames', '--expect-no-changes', '--event-log', str(events)]


@dataclass(frozen=True)
class Fields:
    """The second preview planned no step: each field an `up` would rewrite, none when it writes nothing."""

    changes: tuple[Change, ...]


@dataclass(frozen=True)
class Stepped:
    """The second preview exited `REFUSED`: it planned a step that the caller's first preview did not."""


class Failed(RuntimeError):
    """The second preview exited with neither 0 nor `REFUSED`, so it said nothing of what an `up` writes.

    `diagnostics` is what the run's document records: the error and warning
    messages, one per line, since a JSON preview that fails writes its error
    there and nothing on standard error. Where the document records none,
    `diagnostics` is empty and `unrecorded` says why.
    """

    def __init__(self, status: int, diagnostics: tuple[str, ...], unrecorded: str | None) -> None:
        super().__init__(f'the preview exited {status}')
        self.status: int = status
        self.diagnostics: tuple[str, ...] = diagnostics
        self.unrecorded: str | None = unrecorded


def _failed(status: int, printed: str) -> Failed:
    """The failure of a run that exited `status`, with the messages its document records or why it records none."""
    try:
        document = json.loads(printed)
    except ValueError:
        return Failed(status, (), 'the document is not JSON, so its diagnostics are unknown')
    diagnostics = cast('dict[str, object]', document).get('diagnostics') if isinstance(document, dict) else None
    if not isinstance(diagnostics, list):
        return Failed(status, (), 'the document records no diagnostics')
    return Failed(
        status,
        tuple(
            str(entry.get('message', '')).rstrip()
            for diagnostic in cast('list[object]', diagnostics)
            if isinstance(diagnostic, dict)
            and (entry := cast('dict[str, object]', diagnostic)).get('severity') != 'debug'
        ),
        None,
    )


def answer(status: int, printed: str, events: Path) -> Fields | Stepped:
    """What the second preview says, from its exit, its standard output and its event log at `events`.

    `Failed` for an exit other than 0 and `REFUSED`; `Unreadable` for a
    document or an event log that does not say what an `up` would write, a
    log the run never wrote among them.
    """
    if status == REFUSED:
        return Stepped()
    if status != 0:
        raise _failed(status, printed)
    try:
        log = events.read_text()
    except OSError as exc:
        raise Unreadable(f'the event log could not be read: {exc}') from exc
    return Fields(tuple(changes(read_steps(printed), read_outputs(log))))
