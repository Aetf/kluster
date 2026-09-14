"""The mock monitor every declaration suite starts from, and the drain it needs.

Its own named module rather than a `conftest`, for the reason `memory_kit` is
one: test modules import it, and `conftest` is not a unique module name.

What lives here is what every suite that declares resources against Pulumi's
mocks was re-growing:

-   `Recorder`, a monitor that invents nothing and remembers every
    declaration, so a case can ask what the program handed a provider rather
    than only that it made something;
-   `run_with`, which points the runtime at a monitor and primes the one thing
    a bridged provider needs before it may register anything;
-   `declaring`, which waits until the monitor has actually seen the
    declaration -- without it every assertion about the monitor passes
    vacuously;
-   `decline_every_invoke`, the one answer to an invoke that the engine gives
    and the mock never does.

Importing this module also installs the one patch of Pulumi's own mock monitor
that the suite depends on (`_capture_request` below).

What a suite still writes for itself is the part that is its subject: which
computed outputs the provider reads back, and which invokes it answers.
"""

# Pulumi's mock monitor and its gRPC message types carry no type information,
# and the patch below reaches inside both. The unknown-type family is
# suppressed here rather than repo-wide.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import contextvars
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pulumi
import pulumi.runtime.mocks
import pulumi.runtime.settings
from pulumi.runtime.proto import resource_pb2
from pulumi.runtime.stack import wait_for_rpcs

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping


#: The engine's own root resource. Every run registers one and no case asks
#: about it; it reaches `requested` and not `declared`, so leaving it out of a
#: refusal is what makes the two records describe the same run.
_ROOT_TYPE = 'pulumi:pulumi:Stack'


def _the_one[EntryT](name: str, typ: str | None, run: list[tuple[str, str, EntryT]], verb: str) -> EntryT:
    """The single entry of `run` under this name, or a refusal saying what to do about it.

    One lookup for both records the recorder keeps -- the declarations and the
    raw registration requests -- because whether a name is ambiguous is a
    property of the run rather than of which record answers. `verb` names the
    record, and is the only thing the two messages differ by.

    The three ways this goes wrong want three different things said, which is
    why they are not one message with a count in it:

    -   Several entries answer to the name. The remedy is the type, so the
        refusal carries the types under that name and nothing else; the whole
        run would bury the one line the reader acts on.
    -   A type was named and nothing has it. The type asked for is echoed
        beside the types the name does have, because the mistake is a typo in
        one or the other.
    -   Nothing answers to the name at all. Only then is the whole run worth
        printing, because the reader has no other handle on what the run made.
    """
    under_the_name = [(entry_typ, entry) for entry_typ, entry_name, entry in run if entry_name == name]
    found = [entry for entry_typ, entry in under_the_name if typ in (None, entry_typ)]
    if len(found) == 1:
        return found[0]

    types = sorted({entry_typ for entry_typ, _ in under_the_name})
    if len(found) > 1:
        remedy = (
            f'name the type it means, one of {types}'
            if typ is None
            else f'all {len(found)} of them are {typ}, so no type tells them apart -- read `requested` instead'
        )
        raise AssertionError(f'{name} was {verb} {len(found)} times, not once; {remedy}')
    if under_the_name:
        raise AssertionError(f'{name} was never {verb} as {typ}; under that name the run has {types}')
    whole_run = sorted((entry_typ, entry_name) for entry_typ, entry_name, _ in run if entry_typ != _ROOT_TYPE)
    raise AssertionError(f'{name} was never {verb}; the run {verb} {whole_run}')


@dataclass(frozen=True)
class Declaration:
    """One resource, as the engine registered it."""

    typ: str
    name: str
    #: What the program handed the provider.
    inputs: dict[str, Any]
    #: The provider instance it was registered against, as the engine's
    #: reference to it, or the empty string for the ambient one. Which provider
    #: signs a resource is what "inherited, not re-plumbed" means, so it is
    #: recorded for every declaration rather than by the suites that ask.
    provider: str


class Recorder(pulumi.runtime.Mocks):
    """A monitor that invents nothing and remembers every declaration.

    Every registration is answered with its own inputs and an id built from the
    logical name, which is what a provider that only defines things would do --
    except for the one registration an engine answers with an error instead,
    a second one under an identity the run already used (`_capture_request`).

    A suite whose subject needs more overrides one of the two hooks:
    `computed` for an output the provider would read back that the inputs do
    not carry -- a prefix the cloud assigns, a secret the provider generates --
    and `answer` for an invoke. Those overrides are the suite's setup that *is*
    the case; nothing else here is.
    """

    def __init__(self) -> None:
        #: Every resource the run registered, in registration order.
        self.declared: list[Declaration] = []
        #: Which provider instance each function call went through, by token.
        self.call_providers: dict[str, str] = {}
        #: Every registration request the run made, in registration order --
        #: the only place the resource *options* survive. A list rather than an
        #: index, because a logical name does not identify a registration: a
        #: component, the resource inside it and that resource's own
        #: sub-resource all carry one name. See `_capture_request`.
        self.requested: list[Any] = []
        #: The same requests indexed by the URN each registered under -- the
        #: identity the engine keys on, which qualifies a logical name by the
        #: type and the parent's type. A component, the resource inside it and
        #: that resource's own sub-resource land in three entries here where a
        #: name would have collapsed them into one. A repeated URN is refused
        #: rather than overwritten; `_capture_request` is where, and why.
        self.registrations: dict[str, Any] = {}

    # -- what a suite overrides ---------------------------------------------

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        """Outputs the provider would read back beyond the inputs, if any."""
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        """What an invoke returns; the empty answer for a token this suite does not serve."""
        return {}

    # -- the monitor --------------------------------------------------------

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        # `MockResourceArgs.inputs` is an untyped dict in the SDK.
        inputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        self.declared.append(Declaration(args.typ, args.name, inputs, args.provider or ''))
        return args.name + '_id', inputs | self.computed(args)

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        self.call_providers[args.token] = args.provider or ''
        return self.answer(args), []

    # -- reading the run back -----------------------------------------------

    @property
    def types(self) -> set[str]:
        """Every resource type the run registered."""
        return {declaration.typ for declaration in self.declared}

    @property
    def names_declared(self) -> set[str]:
        """Every logical name the run registered; membership only.

        A set collapses a name two declarations answer to, so no claim about
        *how many* times something was declared can rest on this. That one
        belongs on `of_type`, which keeps them apart.
        """
        return {declaration.name for declaration in self.declared}

    def of_type(self, typ: str) -> list[Declaration]:
        """Every declaration of one type, in registration order."""
        return [declaration for declaration in self.declared if declaration.typ == typ]

    def _one_each(self, typ: str) -> list[Declaration]:
        """Every declaration of one type, refusing a name two of them answer to.

        A record keyed by the logical name keeps only the last declaration under
        it, so on a run that declared one name twice every by-name answer is a
        claim about one of the two and silently stands for both -- which is what
        makes *any* "declared exactly once" claim built on `names` or `by_name`
        vacuous, the second declaration having collapsed into the first. The
        ambiguity is refused here rather than resolved, the way `one` and
        `options_of` already refuse theirs.

        A repeated *identity* is already refused at registration
        (`_capture_request`), so what reaches here is the ambiguity a URN
        permits and a name cannot express: one type under one name below two
        parents of different types is two distinct URNs and one by-name
        question, which is refused rather than resolved to whichever
        registered last.
        """
        declarations = self.of_type(typ)
        repeated = sorted(name for name, count in Counter(it.name for it in declarations).items() if count > 1)
        if repeated:
            raise AssertionError(
                f'{typ} was declared more than once under {repeated}, '
                'so no answer keyed by name describes this run; read `of_type` and count'
            )
        return declarations

    def names(self, typ: str) -> set[str]:
        """The logical names registered under one type, which must be one apiece."""
        return {declaration.name for declaration in self._one_each(typ)}

    def by_name(self, typ: str) -> dict[str, dict[str, Any]]:
        """What each resource of one type was declared with, by logical name."""
        return {declaration.name: declaration.inputs for declaration in self._one_each(typ)}

    def one(self, name: str, typ: str | None = None) -> Declaration:
        """The declaration under this name, which must be exactly one.

        A logical name is unique within a type rather than within a run, so a
        suite that declares the same name under two types passes `typ` too.
        """
        return _the_one(name, typ, [(it.typ, it.name, it) for it in self.declared], 'declared')

    def inputs_of(self, name: str, typ: str | None = None) -> dict[str, Any]:
        """What this resource was declared with."""
        return self.one(name, typ).inputs

    def provider_of(self, name: str, typ: str | None = None) -> str:
        """The provider instance this resource was registered against."""
        return self.one(name, typ).provider

    def options_of(self, name: str, typ: str | None = None) -> Any:
        """The registration request of this resource, which is where its options are.

        Named by type as well as by name where the run needs it. A logical name
        is unique within a type rather than within a run -- a component, the
        resource inside it and that resource's own sub-resource share the
        component's name -- so a name several registrations answer to is
        refused here rather than resolved to whichever registered last. That
        last-one-wins answer is what let a case asserting about a component
        pass on its child's options instead.

        Type and name together are still not an identity. A URN is qualified by
        the parent too, so one type under one name below two different parents
        is a legal pair this pair of arguments cannot separate -- a run holding
        one is refused rather than answered, and the case that needs it reads
        `requested`, every request in registration order, and picks by parent
        itself.
        """
        return _the_one(name, typ, [(it.type, it.name, it) for it in self.requested], 'registered')

    def depends_on(self, name: str, typ: str | None = None) -> list[str]:
        """The URNs this resource was declared to depend on, under the name `options_of` takes."""
        return list(self.options_of(name, typ).dependencies)

    def children_not_named_for_their_component(self) -> dict[str, str]:
        """Every registration whose logical name does not carry its component's, and the name it should carry.

        Keyed by URN, valued by the name of the nearest component above the
        registration. Empty on a run that keeps the rule style/pulumi.md
        states: a child's logical name is `f'{name}-…'` from the `name` its
        component was given, because a URN qualifies a name by the chain of
        parent *types* and not by any parent's name, so two instances of one
        component type can each hold a child of one type only if the child's
        name carries theirs (`_capture_request` is where a run that does not is
        stopped, and only on the census that happens to collide).

        The component is found by walking the parent chain past custom
        resources: a resource declared against a component's own resource -- a
        repository's protection, parented on the repository -- is that
        component's child for the purpose of the URN exactly as one parented on
        the component is, since the custom resource between them contributes a
        type to the chain and no name. A registration with no component above
        it -- a top-level component, a stack program's own resource -- is
        judged by nothing and never listed.

        Carrying is read as the rule's two forms and nothing looser: the
        component's name alone, or the name followed by `-`. That the name
        carried is the holding component's rather than a shorter sibling's the
        rule's form also fits is the reviewer's to read off the declaration,
        not this record's.
        """
        misnamed: dict[str, str] = {}
        for urn, request in self.registrations.items():
            component = self._component_above(request)
            if component is None:
                continue
            rest = request.name.removeprefix(component.name)
            if request.name == rest or not (rest == '' or rest.startswith('-')):
                misnamed[urn] = component.name
        return misnamed

    def places_claimed_more_than_once(self, place_of: Mapping[str, str]) -> dict[str, list[str]]:
        """Every place two or more registrations claim, and the URNs that claim it.

        `place_of` maps a resource type to the input that is the one place the
        resource occupies on its target -- for the device provider, the path a
        file or a directory sits at and the root an artifact is unpacked to
        (`device_places.PLACES`). Registrations of a type the map does not name
        are not read at all. Keyed by the value, valued by the URNs sorted --
        the order two independent registrations reach the monitor in is the
        SDK's thread pool's, not the program's, so registration order would
        make the answer vary between runs of one program. Empty on a run where
        every place has one resource.

        Read by URN and not by logical name, because a name keeps nothing off a
        place: a child is named for its component (style/pulumi.md), so two
        components asking for one path are two names, two URNs -- the engine
        accepts the run -- and one file on the device that each `create`
        writes, each refresh reports drifted, and either `delete` removes from
        under the other. One-place-one-resource is a property of the inputs,
        and this is the reader that holds it.

        A registration of a listed type whose place is missing or is not a
        string is refused rather than skipped: skipping would let the very
        resource the map is about fall out of the census.
        """
        claims: dict[str, list[str]] = {}
        for urn, request in self.registrations.items():
            place = place_of.get(request.type)
            if place is None:
                continue
            if place not in request.object:
                raise AssertionError(f'{urn} is a {request.type} with no {place!r} input, so it claims no place')
            value = request.object[place]
            if not isinstance(value, str):
                raise AssertionError(f'{urn} claims {place!r} {value!r}, which is not a place')
            claims.setdefault(value, []).append(urn)
        return {value: sorted(urns) for value, urns in claims.items() if len(urns) > 1}

    def _component_above(self, request: Any) -> Any | None:
        """The nearest component registration above this one, or `None` under the root alone."""
        parent = request.parent
        while parent in self.registrations:
            above = self.registrations[parent]
            if above.type == _ROOT_TYPE:
                return None
            if not above.custom:
                return above
            parent = above.parent
        return None


class _RunMonitor(pulumi.runtime.mocks.MockMonitor):
    """Pulumi's mock monitor, carrying which kind of run it was built for.

    The flag is here rather than read off the runtime because of where the
    monitor runs: the SDK dispatches `RegisterResource` onto an executor thread
    that has no Python context of its own, and there `is_dry_run()` does not
    answer for the run in hand (`_capture_request`). `run_with` builds one of
    these for every run, so the run's own answer travels with the monitor to
    the thread that needs it.
    """

    def __init__(self, mocks: pulumi.runtime.Mocks, *, dry_run: bool) -> None:
        super().__init__(mocks)
        self.dry_run: bool = dry_run


_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def _register_as_the_run(monitor: _RunMonitor, request: Any) -> Any:
    """The SDK's own registration, deserialized under the run's kind rather than the process's.

    Run inside a copy of the executor thread's context, never on the thread
    itself, so the thread is left as it was found. The SDK's setter for
    `dry_run` treats the first value set in a context as the process-wide
    default for every thread that has none, and a copy starts with none: so
    the assignment below both answers this registration and leaves the
    process default at the run most recently registered, and it can never
    find an earlier run's value already in place, which is the condition
    under which the setter keeps the earlier one.
    """
    # The SDK declares the property without a setter; the descriptor behind
    # it supplies one, and `set_mocks` assigns through it the same way.
    pulumi.runtime.settings.SETTINGS.dry_run = monitor.dry_run  # pyright: ignore[reportAttributeAccessIssue]
    return _register_resource(monitor, request)


def _capture_request(self: Any, request: Any) -> Any:
    """Refuse a repeated identity, keep what the mock drops, and read the inputs under the run's own kind.

    **The refusal.** A URN is a resource's identity, and a program that
    registers one twice is a program the engine stops: `Duplicate resource URN
    <urn>; try giving it a unique name`. Pulumi's mock monitor does not stop
    it -- it writes its resource table under that same URN and the second
    registration silently replaces the first, so a run no engine would have
    accepted reads back as a run with one resource in it. Refusing here is
    what makes the double answer the way the thing it stands in for does, and
    it catches the shape rather than the readers: a case that asks nothing by
    name is caught too.

    The identity is the URN the mock computes, which is the engine's shape --
    stack, project, the parent's type, the type, the logical name -- with one
    difference: it qualifies by the *immediate* parent's type where the engine
    carries the whole chain of them. That makes the refusal here at worst
    stricter than the engine's, never laxer. Note what neither form carries:
    the parent's *name*. Two components of one type, each holding a child of
    one type under one name, are a duplicate URN in the engine too, which is
    why a child's name carries the component's (style/pulumi.md).

    **What is kept.** The request itself, because a resource's *options* --
    `import_`, `ignore_changes`, `delete_before_replace`, `depends_on` -- reach
    no output and are exactly what several suites are about -- every one of
    them, rather than only the last under each logical name; and the
    per-property dependency edges, which the mock's response leaves empty
    although the request carried them (framework/testing.md §3.1).

    **Which kind of run the inputs are read under.** The SDK runs this method
    on an executor thread with no Python context, and the runtime's `dry_run`
    is a context variable whose answer on such a thread is a process-wide
    default the SDK's setter fixes at the first value set in a context. The
    mock deserializes the request's inputs right here, and an unknown nested
    in them becomes an `Unknown` under a preview and a dropped key otherwise
    -- so, left alone, what a case reads back off the recorder for a nested
    unknown follows whichever run set the flag first in the context this run
    shares, not the run in hand (framework/testing.md §3.3). A monitor
    `run_with` built carries its run's own flag, and the SDK's method runs
    under it, in a context of its own so the thread is left untouched.

    Patched on the class, once, at import: `run_with` builds a fresh monitor
    per run, so there is no instance to hook, and the recording lands on
    whichever `Recorder` that monitor was built around rather than on a global.
    """
    if isinstance(self.mocks, Recorder):
        urn = self.make_urn(request.parent, request.type, request.name)
        if urn in self.mocks.registrations:
            raise AssertionError(
                f'duplicate resource URN {urn}; try giving it a unique name. '
                'The engine refuses a run that registers one identity twice, so a run that '
                'declares a variant beside its baseline names the variant something else.'
            )
        self.mocks.requested.append(request)
        self.mocks.registrations[urn] = request
    if isinstance(self, _RunMonitor):
        response = contextvars.copy_context().run(_register_as_the_run, self, request)
    else:
        response = _register_resource(self, request)
    for name, dependencies in request.propertyDependencies.items():
        response.propertyDependencies[name].urns.extend(dependencies.urns)
    return response


pulumi.runtime.mocks.MockMonitor.RegisterResource = _capture_request


async def run_with[MonitorT: pulumi.runtime.Mocks](
    monitor: MonitorT, *, stack: str, project: str = 'kluster', preview: bool = False
) -> MonitorT:
    """Point the runtime at `monitor` and hand it back for the cases to read.

    The priming call is what a bridged provider needs: a bridged SDK is a
    *parameterized* package, so before it may register a resource it registers
    its own package, and it gates that on a feature flag it reads out of a
    synchronous cache. The mock monitor answers the feature and serves the
    registration, but nothing on the mock path performs the async negotiation
    that fills the cache, so a bridged provider refuses under mocks until it is
    primed once. It costs a round trip against the mock and is done for every
    suite, so that adding a bridged resource to a program is not also a puzzle
    in whichever suite declares it.

    The monitor is built here rather than left to `set_mocks`, so that it
    carries `preview` to the thread the mock deserializes on
    (`_capture_request`).
    """
    pulumi.runtime.set_mocks(
        monitor, project=project, stack=stack, preview=preview, monitor=_RunMonitor(monitor, dry_run=preview)
    )
    # Registrations are dispatched onto a queue that lives in module state and
    # so outlives the event loop of whichever test made them. Emptying it as a
    # run begins is what lets `declaring` mean "what this run declared" rather
    # than "everything any run ever declared", half of it owned by loops that
    # are closed.
    pulumi.runtime.settings._get_rpc_manager().clear()  # pyright: ignore[reportPrivateUsage]
    _ = await pulumi.runtime.settings.monitor_supports_feature('parameterization')
    return monitor


@asynccontextmanager
async def declaring() -> AsyncGenerator[None]:
    """Wait, on the way out, until the monitor has seen what the block declared.

    Declaring a resource only schedules its registration, so without this the
    monitor has seen nothing and every assertion about it passes vacuously.

    Only the tasks the block itself added are awaited. The task queue is
    process-global and holds, among other things, the deliberately failing
    outputs other modules park in it, so draining it wholesale would fail a
    suite for something another suite arranged on purpose.
    """
    before = asyncio.all_tasks()
    yield
    pending = asyncio.all_tasks() - before - {asyncio.current_task()}
    _ = await asyncio.gather(*pending)
    await wait_for_rpcs(await_all_outstanding_tasks=False)


def decline_every_invoke() -> None:
    """Answer every invoke the way the engine answers one it cannot service yet.

    An invoke is gated on its dependencies having been created, and while one
    is pending -- skipped by a `--target`ed update, say -- the engine answers
    `unknown` in place of a result (`ResourceInvokeResponse.unknown`) rather
    than calling the provider. Pulumi's mock monitor never sets the field, so
    the run's monitor is given that answer here, for the run alone: it is the
    instance `run_with` built that is patched, and the next run builds a fresh
    one. Every token, because a suite reaching for this has one invoke and it
    is the subject.
    """
    mock = pulumi.runtime.settings.get_monitor()
    assert isinstance(mock, pulumi.runtime.mocks.MockMonitor)

    def declined(request: resource_pb2.ResourceInvokeRequest) -> resource_pb2.ResourceInvokeResponse:
        return resource_pb2.ResourceInvokeResponse(unknown=True)

    mock.Invoke = declined
