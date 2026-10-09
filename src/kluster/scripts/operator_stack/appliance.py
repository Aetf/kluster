"""The `state-backend` stack's gate: what a run of it does beyond what every operator stack's run does (rfc-006 §7).

The stack declares the box every other stack keeps its state in, so three
things are read off each refreshed preview before anything is applied:

-   **A pending replacement**: a create, a replacement or a delete of the
    instance (`box_steps`). `up` writes nothing while one is planned, and names
    what moved and `--force`; `--force` sets the replacement permission the
    instance's hooks read (`kluster.lib.state_backend.permission`), and
    `--replace` sets it and replaces the box whether or not anything moved.
-   **A create beside a held address**: a create of the instance while the
    reserved address -- as the refresh reads it, or the import that adopts
    it -- is assigned to something and the refreshed state holds no instance
    is refused, with `--force` or without (`creates_beside_held_address`).
    That is a box another workstation's run launched and has not landed the
    checkpoint of, or one the stack does not know: a second box beside it
    would take the address from it.
-   **A replacement of an adopted resource**: the engine refuses to replace a
    resource whose declaration still carries an import id, and fails the
    `up` at that step, after every step before it has run
    (`imported_replacements`). So the driver refuses the run before it
    starts, naming each such resource.
-   **The server certificate's expiry**, a stack output, against the renewal
    margin (`renewal`).

And one thing is read off the estate's backend after every run, which is the
record of a restore owed: a backend that answers and holds no stack is a box
owed a restore or a site before its first `stack init`, and one that does not
answer is neither (`Gate.backend`).
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pulumi
import pulumi_oci as oci

from kluster import conventions
from kluster.lib import stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.lib.state_backend import permission, settings, state
from kluster.scripts.credentials import derived

if TYPE_CHECKING:
    from kluster.scripts.operator_stack.driver import Preview, Step

log = logging.getLogger(__name__)

#: The stack this gate is for.
STACK = conventions.STACK_NAMES.state_backend


def _type_token(klass: type) -> str:
    """The type token `klass` registers its resources under, which every step event about one names."""
    token = pulumi.get_type_token(klass)
    if token is None:
        raise TypeError(f'{klass.__qualname__} states no type token')
    return token


#: The resource types the gate reads -- the box, and the reserved address --
#: read off the SDK classes the stack declares them with, so they are the
#: types the events name: one that differs from those by so much as its case
#: matches no step, and every guard below would pass in silence.
INSTANCE = _type_token(oci.core.Instance)
RESERVED_ADDRESS = _type_token(oci.core.PublicIp)

#: The step operations that create, replace or delete a resource.
BOX_STEPS = frozenset({'create', 'replace', 'create-replacement', 'delete-replaced', 'delete', 'import-replacement'})

#: Where the box carries its bill of materials, a digest per component, in the
#: clear (`kluster.lib.state_backend.render.bill_of_materials`).
BILL_OF_MATERIALS = 'extendedMetadata'

#: The outputs of the reserved address that say it is pointed at something.
ASSIGNMENT = ('assignedEntityId', 'privateIpId')

#: The step operations that replace a resource: under `delete_before_replace`
#: the engine emits `delete-replaced` first, carrying only the old state, then
#: `replace` and `create-replacement`.
REPLACING = frozenset({'replace', 'create-replacement', 'delete-replaced', 'import-replacement'})
IMPORTING = frozenset({'import', 'import-replacement'})

#: What the engine says, at the pinned CLI, of a replacement of a resource
#: whose declaration carries an import id: a warning in a preview, an error that
#: fails the `up` (`step_generator.go` at 3.257.0, L1975–L1999).
IMPORTED_REPLACEMENT = 'previously-imported resources that still specify an ID may not be replaced'


class Refused(RuntimeError):
    """The run would launch a box beside the one the reserved address is assigned to."""


class Silent(RuntimeError):
    """The estate's backend did not answer."""


class Backend(enum.Enum):
    """What the estate's backend answers after a run."""

    #: It serves at least one stack.
    SERVING = 'serving'
    #: It answers and serves none: a box owed a restore, or a site before its first `stack init`.
    EMPTY = 'empty'
    #: It does not answer.
    SILENT = 'silent'


def box_steps(preview: Preview) -> list[Step]:
    """The steps that would create, replace or delete the instance."""
    return [step for step in preview.details if step.type == INSTANCE and step.op in BOX_STEPS]


def _inputs(recorded: Mapping[str, Any], key: str) -> Mapping[str, object]:
    found = cast('Mapping[str, Any]', recorded.get('inputs') or {}).get(key)
    return cast('Mapping[str, object]', found) if isinstance(found, dict) else {}


#: What `moved` answers for a replacement the preview plans while naming no
#: digest and no input as changed: the provider replaces the box for a reason
#: the events do not carry, and the plan's own diff is where to read it.
NOTHING_NAMED = 'a replacement the preview plans without naming a digest or an input that moved; read the plan'


def moved(steps: Sequence[Step]) -> list[str]:
    """What the box's pending create, replacement or delete is for, as the operator reads it.

    A replacement is read off the steps that carry both sides -- `replace` and
    `create-replacement` -- and not off `delete-replaced`, which the engine
    emits first under `delete_before_replace` with the old state alone: each
    digest of the bill of materials that moved, by component, and each other
    input the provider names as different. A create has no old box to compare
    with, and a lone delete no new one, and each says so.
    """
    ops = {step.op for step in steps}
    reasons: list[str] = []
    for step in steps:
        if step.op == 'create':
            reasons.append('no box in the state: a create')
        elif step.op == 'delete' or (step.op == 'delete-replaced' and ops <= {'delete-replaced'}):
            reasons.append('the program no longer declares the box: a delete')
        elif step.old and step.new:
            before, after = _inputs(step.old, BILL_OF_MATERIALS), _inputs(step.new, BILL_OF_MATERIALS)
            reasons += [
                f'the {key} digest' for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)
            ]
            reasons += [f'the {key} input' for key in step.diffs if key != BILL_OF_MATERIALS]
    return sorted(set(reasons), key=reasons.index) or [NOTHING_NAMED]


def imported_replacements(preview: Preview) -> list[str]:
    """Each resource the preview would replace while its declaration carries an import id.

    Read two ways, each enough alone: a replacing step on a resource the same
    preview imports, and the engine's own warning, which also covers a
    resource imported by an earlier run whose declaration still names its id.
    """
    imported = {step.urn for step in preview.details if step.op in IMPORTING}
    replaced = {step.urn for step in preview.details if step.op in REPLACING}
    warned = {urn for urn, message in preview.diagnostics if IMPORTED_REPLACEMENT in message}
    return sorted((imported & replaced) | warned)


def instance_urn(preview: Preview) -> str:
    """The instance's URN, read off any step the preview planned for it."""
    urns = {step.urn for step in preview.details if step.type == INSTANCE}
    if len(urns) != 1:
        raise Refused(f'the preview names {len(urns)} instances of the box, so there is no one URN to replace')
    (urn,) = urns
    return urn


def _address_outputs(preview: Preview) -> Mapping[str, object] | None:
    """The reserved address's outputs as the provider answers them now.

    What a refresh or an import read (`Preview.read`), else the state a step
    of the address starts from. An import's own `resourcePreEvent` carries no
    such state -- its read comes after it, on its `resOutputsEvent` -- and an
    `update` that follows the import starts from what the import read.
    """
    for step in preview.details:
        if step.type == RESERVED_ADDRESS:
            read = preview.read.get(step.urn)
            if read is not None:
                return read
            if step.old:
                return cast('Mapping[str, object]', step.old.get('outputs') or {})
    return None


def held_address(preview: Preview) -> str | None:
    """What the reserved address, as the provider answers it now, is assigned to, or None while it is assigned to nothing."""
    outputs = _address_outputs(preview)
    if outputs is None:
        return None
    for key in ASSIGNMENT:
        if isinstance(value := outputs.get(key), str) and value:
            return value
    return None


def creates_beside_held_address(preview: Preview) -> str | None:
    """Why the preview would launch a box beside the one the address points at, or None where it would not.

    A create of the instance -- not a replacement, so the refreshed state
    holds no instance -- while the address, refreshed or imported, is
    assigned. On a first launch, or after the box is lost, the address is
    assigned to nothing: the instance's termination deletes the private
    address it pointed at.
    """
    if not any(step.op == 'create' for step in box_steps(preview)):
        return None
    held = held_address(preview)
    if held is None:
        return None
    return (
        f'this run would launch a box while the reserved address {settings.ADDRESS} is assigned to {held} and '
        "the stack's state holds no box: that is a box another workstation launched and has not landed the "
        'checkpoint of, or one the stack never declared. Land that checkpoint, or terminate that box by hand '
        '(never import it), and plan again'
    )


def refuse_imported_replacements(urns: Sequence[str]) -> str:
    """Why a run that would replace these adopted resources is refused."""
    return (
        f'this run would replace {", ".join(urns)}, which the program imports by id, and the engine refuses '
        'to replace a resource whose declaration still carries one: it would fail the run at that step, after '
        'every step before it. That is a finding about the adoption to resolve, not a replacement to force'
    )


def renewal(preview: Preview, now: dt.datetime) -> str | None:
    """The reissue the server certificate is due for, or None while it has more life than the margin."""
    exported = preview.stack_outputs.get(settings.CERTIFICATE_EXPIRY_OUTPUT)
    if not isinstance(exported, str):
        return None
    expiry = dt.datetime.fromisoformat(exported)
    remaining = expiry - now
    if remaining > settings.RENEWAL_MARGIN:
        return None
    reissue = (
        f'`credentials derived {derived.STATE_BACKEND_SERVER_ROW} issue`, then `{permission.REMEDY}`, which replaces the box '
        'with one carrying it'
    )
    if remaining.total_seconds() < 0:
        return f'the server certificate expired on {expiry.date().isoformat()}: {reissue}'
    return (
        f'the server certificate expires on {expiry.date().isoformat()}, {remaining.days} day(s) from now and '
        f'inside the {settings.RENEWAL_MARGIN.days}-day renewal margin: {reissue}'
    )


def _connection(bundle_dir: Path) -> state.Connection:
    """The `operator` client bundle's connection, refused where the slot holds none."""
    try:
        return state.connection(bundle_dir)
    except state.StateError as exc:
        raise Refused(str(exc)) from exc


def _served(bundle_dir: Path) -> list[str]:
    """The stacks the estate's backend serves, over the `operator` client bundle in `bundle_dir`."""
    target = _connection(bundle_dir)
    try:
        return state.stacks(target)
    except state.StateError as exc:
        raise Silent(f'{state.endpoint(target.url)} did not answer: {exc}') from exc


@dataclass
class Gate:
    """The gate, with the two things it asks the world: the estate's backend, and the time."""

    #: The stacks the estate's backend serves; raises `Silent` where it does
    #: not answer.
    served: Callable[[], list[str]]
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))
    #: Refuses, as `Refused`, a checkout with no way to ask the backend: the
    #: run's hooks connect with the same bundle, so it is asked before the run
    #: starts rather than after it has written.
    ready: Callable[[], None] = field(default=lambda: None)

    @classmethod
    def for_checkout(cls, checkout: Path) -> Gate:
        """The gate asking the backend over the `operator` client bundle in `checkout`'s slot."""
        bundle_dir = checkout / lib_workstation.DIRECTORY / stack_environment.BUNDLE_SLOT

        def ready() -> None:
            _ = _connection(bundle_dir)

        return cls(served=lambda: _served(bundle_dir), ready=ready)

    def report(self, preview: Preview) -> None:
        """Say what the preview shows that the operator acts on: a create beside a held address, an adopted resource replaced, and a renewal due."""
        if (beside := creates_beside_held_address(preview)) is not None:
            log.warning('%s', beside)
        if refused := imported_replacements(preview):
            log.warning('%s', refuse_imported_replacements(refused))
        if (due := renewal(preview, self.now())) is not None:
            log.warning('%s', due)

    def backend(self) -> Backend:
        """What the estate's backend answers, said in the terms the operator acts on."""
        log.info("asking the estate's backend which stacks it serves")
        try:
            held = self.served()
        except Silent as exc:
            log.error(
                "the estate's backend does not answer (%s): neither a restore owed nor a site with no state "
                'can be told from that; `state-backend ssh` reaches the box',
                exc,
            )
            return Backend.SILENT
        if held:
            log.info("the estate's backend serves %d stack(s)", len(held))
            return Backend.SERVING
        log.warning(
            "the estate's backend answers and serves no stack: a box owed a restore -- `state-backend restore "
            '<file>` with the dump its replacement took, or the newest nightly one -- or a site before its first '
            '`pulumi stack init`, which needs nothing more'
        )
        return Backend.EMPTY
