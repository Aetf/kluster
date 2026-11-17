"""What an `up` would write that its preview plans no step for, read field by field.

Two surfaces of one `pulumi preview` carry it, and neither carries all of it:

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
default, and the answer errs toward an `up` rather than away from one. A
secret is the exception: both surfaces carry it as a placeholder, so a secret
whose value alone changed compares equal.

**A value prints as the CLI serialized it**, and the CLI serializes a secret
as the literal `SECRET` unless it is asked to show secrets, which nothing
here asks. A secret in the event log is an object carrying the secret
signature (`SECRET_SIG`), and it prints as `SECRET` whatever it holds beside
the signature.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
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
    """The preview's JSON or its event log does not say what an `up` would write."""


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
        raise Unreadable(f'the JSON preview printed something that is not JSON: {exc}') from exc
    if not isinstance(document, dict):
        raise Unreadable('the JSON preview printed no document of steps')
    steps = cast('dict[str, object]', document).get('steps')
    if not isinstance(steps, list):
        raise Unreadable('the JSON preview recorded no list of steps')
    read: list[States] = []
    for number, step in enumerate(cast('list[object]', steps), 1):
        if not isinstance(step, dict):
            raise Unreadable(f'step {number} of the JSON preview is not an object')
        entry = cast('dict[str, object]', step)
        urn = entry.get('urn')
        if not isinstance(urn, str):
            raise Unreadable(f'step {number} of the JSON preview names no resource')
        if entry.get('oldState') is None and entry.get('newState') is None:
            raise Unreadable(f'step {number} of the JSON preview, {urn}, carries neither state')
        read.append(States(urn=urn, old=_state(entry, 'oldState', urn), new=_state(entry, 'newState', urn)))
    return read


def _state(step: Mapping[str, object], side: str, urn: str) -> Mapping[str, object]:
    state = step.get(side)
    if state is None:
        return {}
    if not isinstance(state, dict):
        raise Unreadable(f'the {side} of {urn} in the JSON preview is not an object')
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


def section(stack: str, found: list[Change]) -> str:
    """The plan's own section for a plan that changes no resource and rewrites state: each field, under its resource."""
    lines = [f"An `up` of {stack} changes no resource. It rewrites these fields of the stack's state:", '']
    urn = None
    for change in found:
        if change.urn != urn:
            urn = change.urn
            lines.append(f'  {urn}')
        lines.append(f'      {change}')
    return '\n'.join(lines)
