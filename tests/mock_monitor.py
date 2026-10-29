"""The mock monitor every declaration suite starts from, and the drain it needs.

Its own named module rather than a `conftest`, for the reason `memory_kit` is
one: test modules import it, and `conftest` is not a unique module name.

What lives here is what every suite that declares resources against Pulumi's
mocks was re-growing:

-   `Recorder`, a monitor that invents nothing and remembers every
    declaration and every function call, so a case can ask what the program
    handed a provider, and through which provider, rather than only that it
    made something;
-   `run_with`, which points the runtime at a monitor, through `set_mocks`,
    which builds each run a fresh registration queue, and hands the run a
    stand-in for the SDK's callback server (`_Callbacks`), so a run that binds
    a hook starts no server and the `Recorder` keeps the hook by name;
-   `run_under_backstop`, which is `run_with` for a run that also refuses an
    unparented resource the way a real run does;
-   `declaring`, which waits until the monitor has actually seen the
    declaration -- without it every assertion about the monitor passes
    vacuously;
-   `decline_every_invoke`, the one answer to an invoke that the engine gives
    and the mock never does;
-   `fail_on_unreceived_failures`, which fails a case whose run met a
    failure -- in an RPC (a registration, an invoke) or in an output the SDK
    tracks -- that nothing the case awaited received, judged from the mark
    `begin_case` sets; and `the_run_failed_with` and
    `the_run_failed_with_each`, which are how a case whose subject is that
    failure, or several, receives it.

Importing this module also installs the two patches of Pulumi's own mock
monitor that the suite depends on (`_capture_request` and
`_register_package` below).

The last section is a workaround for the Pulumi SDK, kept apart so that it can
be deleted whole: `_ReadFailures`, on each loop a run has work on, drops
asyncio's report of a failure never retrieved once the judgment has read the
failure's root.

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
import threading
from collections import Counter
from contextlib import AbstractContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from pulumi import runtime
from pulumi.runtime import mocks, settings
from pulumi.runtime.proto import resource_pb2
from pulumi.runtime.rpc_manager import RPCManager
from pulumi.runtime.settings import SETTINGS
from pulumi.runtime.stack import ResourceRegistrationFailed

from putils import component as putils_component
from putils import install_parent_backstop

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

    import pulumi


#: The engine's own root resource. Every run registers one and no case asks
#: about it; it reaches `requested` and not `declared`, so leaving it out of a
#: refusal is what makes the two records describe the same run.
_ROOT_TYPE = 'pulumi:pulumi:Stack'


def _the_one[EntryT](name: str, typ: str | None, run: list[tuple[str, str, EntryT]], verb: str) -> EntryT:
    """The single entry of `run` under this name, or a refusal saying what to do about it.

    One lookup for both records the recorder keeps of registrations -- the
    declarations and the raw registration requests -- because whether a name
    is ambiguous is a property of the run rather than of which record answers.
    `verb` names the record, and is the only thing the two messages differ by.

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


class Call(NamedTuple):
    """One function call, as the engine received it."""

    token: str
    #: The provider instance it went through, as the engine's reference to it,
    #: or the empty string for the ambient one -- the same reading
    #: `Declaration.provider` has.
    provider: str


class PlaceMissing(AssertionError):
    """A registration of a type the place map lists carries no place input, so the census cannot count it."""


class Recorder(runtime.Mocks):
    """A monitor that invents nothing and remembers every declaration and every call.

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
        #: Every function call the run made, in the order the monitor received
        #: them -- which is not the program's order where one call waits on a
        #: provider's registration and another does not. A list rather than
        #: an index by token, because a token does not identify a call: one
        #: lookup is made once per node or per artifact, and a record keeping
        #: one provider per token would let the call that lost its provider
        #: hide behind a sibling that kept it. `called_through` is the reader
        #: that asks about a token.
        self.called: list[Call] = []
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
        #: Held across the refusal's check and the insert after it. The SDK
        #: registers on its executor threads, several at once, so the two are
        #: one step only under a lock: two registrations of one URN would
        #: otherwise both pass the check, and the second replace the first.
        self.registering: AbstractContextManager[object] = threading.Lock()
        #: Every resource hook the run registered, by name: the callbacks the
        #: engine would call, which a case calls the same way, by the names a
        #: request binds (`_Callbacks`).
        self.hooks: dict[str, pulumi.ResourceHook] = {}

    # -- what a suite overrides ---------------------------------------------

    def computed(self, args: runtime.MockResourceArgs) -> dict[str, Any]:
        """Outputs the provider would read back beyond the inputs, if any."""
        return {}

    def answer(self, args: runtime.MockCallArgs) -> dict[str, Any]:
        """What an invoke returns; the empty answer for a token this suite does not serve."""
        return {}

    # -- the monitor --------------------------------------------------------

    def new_resource(self, args: runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        # `MockResourceArgs.inputs` is an untyped dict in the SDK.
        inputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        self.declared.append(Declaration(args.typ, args.name, inputs, args.provider or ''))
        return args.name + '_id', inputs | self.computed(args)

    def call(self, args: runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        self.called.append(Call(args.token, args.provider or ''))
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

    def called_through(self, token: str) -> str:
        """The provider instance every call to this token went through, which must be one.

        A token called through two providers -- one of its calls having lost
        the provider its siblings carry -- is refused rather than answered,
        because either answer would describe one call and stand for both. A
        token never called is refused too, naming the tokens the run did call.
        """
        providers = sorted({call.provider for call in self.called if call.token == token})
        if len(providers) == 1:
            return providers[0]
        if providers:
            raise AssertionError(
                f'{token} was called through {len(providers)} providers, not one: {providers}; '
                'read `called` to find the call that differs'
            )
        called = sorted({call.token for call in self.called})
        raise AssertionError(f'{token} was never called; the run called {called}')

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
                raise PlaceMissing(f'{urn} is a {request.type} with no {place!r} input, so it claims no place')
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


_register_resource = mocks.MockMonitor.RegisterResource


def _capture_request(self: Any, request: Any) -> Any:
    """Refuse a repeated identity, and keep what the mock drops.

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

    Patched on the class, once, at import: `set_mocks` builds a fresh monitor
    per run, so there is no instance to hook, and the recording lands on
    whichever `Recorder` that monitor was built around rather than on a global.
    """
    if isinstance(self.mocks, Recorder):
        urn = self.make_urn(request.parent, request.type, request.name)
        with self.mocks.registering:
            if urn in self.mocks.registrations:
                raise AssertionError(
                    f'duplicate resource URN {urn}; try giving it a unique name. '
                    'The engine refuses a run that registers one identity twice, so a run that '
                    'declares a variant beside its baseline names the variant something else.'
                )
            self.mocks.requested.append(request)
            self.mocks.registrations[urn] = request
    response = _register_resource(self, request)
    for name, dependencies in request.propertyDependencies.items():
        response.propertyDependencies[name].urns.extend(dependencies.urns)
    return response


mocks.MockMonitor.RegisterResource = _capture_request


def _register_package(self: Any, request: Any) -> Any:
    """A reference of its own for each package, as the engine hands out, where the mock hands out one for all.

    The SDK keeps a record of which provider serves the reference an
    extension package was registered under -- the CRD SDK's, whose classes
    the `kubernetes` provider serves -- and consults it to find a resource's
    or an invoke's provider among its parent's. Pulumi's mock monitor
    answers every registration with the one reference `mock-uuid`, and the
    settings `set_mocks` installs do not empty that record between runs
    (`RegisterPackage` and `MockSettings` in `pulumi/runtime/mocks.py`,
    pulumi 3.267.0). So once any run had registered an extension package,
    every other parameterized package -- each bridged SDK -- would share its
    reference, be taken for a `kubernetes` package, and have its invokes sent
    to no provider in whichever suite ran next.

    The reference is a function of what was registered, so a registration a
    later run answers from the SDK's own cache of references names the same
    package it did when it was made.
    """
    parameter = request.extension if request.HasField('extension') else request.parameterization
    return resource_pb2.RegisterPackageResponse(
        ref=f'mock:{request.name}@{request.version}:{parameter.name}@{parameter.version}'
    )


mocks.MockMonitor.RegisterPackage = _register_package


class _Callbacks:
    """What a mocked run registers its resource hooks with, in place of the SDK's callback server.

    A real program registers a hook with a gRPC server of the SDK's own, which
    the engine calls back into when a step of the resource runs it; the SDK
    starts that server the first time a program registers one, and stops it in
    `run_pulumi_func`, the end of a real program (`_get_callbacks` and
    `_shutdown_callbacks` in `pulumi/runtime/settings.py`). A run here is
    declared through `declaring` and never ends there, so every server a run
    started would outlive it. And the mock monitor calls no hook, so the
    server would serve no one: what a case needs is the hook itself, to call
    as the engine would. So each run gets this instead, which keeps every hook
    by name on the run's `Recorder` and asks the monitor for nothing.

    Resource hooks are all it serves. A program that registers a resource
    transform, an invoke transform or an error hook meets an `AttributeError`
    naming the registration, which is where serving it would start.
    """

    def __init__(self, hooks: dict[str, pulumi.ResourceHook]) -> None:
        self.hooks = hooks

    def register_resource_hook(self, hook: pulumi.ResourceHook) -> None:
        self.hooks[hook.name] = hook


async def run_with[MonitorT: runtime.Mocks](
    monitor: MonitorT, *, stack: str, project: str = 'kluster', preview: bool = False
) -> MonitorT:
    """Point the runtime at `monitor` and hand it back for the cases to read.

    `set_mocks` builds the run's mock monitor and its queue of outstanding
    registrations afresh, so nothing a previous run left in flight is this
    run's to wait for, and the run's callbacks are `_Callbacks` from then on. That queue is also kept here, with the loop the run
    starts on, for `fail_on_unreceived_failures` to judge the run by.
    """
    runtime.set_mocks(monitor, project=project, stack=stack, preview=preview)
    # A property the SDK declares through a decorator the checker cannot read.
    SETTINGS.callbacks = _Callbacks(monitor.hooks if isinstance(monitor, Recorder) else {})  # pyright: ignore[reportAttributeAccessIssue]
    run = _Run(settings._get_rpc_manager(), [])  # pyright: ignore[reportPrivateUsage]
    run.learn(asyncio.get_running_loop())
    _watch(run)
    _runs.append(run)
    return monitor


def _watch(run: _Run) -> None:
    """Have the run learn the loop of every RPC a declaration asks it for, as the declaration asks.

    A declaration hands the SDK's `RPCManager.do_rpc` the RPC and schedules,
    on the loop running the declaration, the task that starts it; until that
    loop turns, the RPC is in no record, and its loop is the only way to it.
    A case on a loop of its own declaring on a module's shared run is the
    case that needs this: the run never started on that loop. The wrapper is
    the run's own, on the one instance `set_mocks` built for it, so it lasts
    exactly as long as the run.
    """
    do_rpc = run.manager.do_rpc

    def asked(name: str, rpc_function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        # A declaration outside any running loop schedules on no loop a case
        # will turn, so there is nothing to learn.
        with suppress(RuntimeError):
            run.learn(asyncio.get_running_loop())
        return do_rpc(name, rpc_function)

    run.manager.do_rpc = asked


async def run_under_backstop[MonitorT: runtime.Mocks](
    monitor: MonitorT, *, stack: str, project: str = 'kluster', preview: bool = False
) -> MonitorT:
    """`run_with`, with the parent backstop on the run's root stack resource.

    Installed before anything is declared, as `kluster.main` installs it on a
    real run, so a resource a component declares without a parent is refused
    here rather than first in `pulumi preview` (framework/pulumi.md §1.3).

    The under-construction scope is emptied first, and that is not optional. A
    component whose ``__init__`` raises never reaches `register_outputs`, so it
    stays open, and several suites assert that a component refuses a bad
    argument. That ends a program -- which is the documented behavior -- but
    not a test process, so a component another suite left open would enclose
    every top-level resource of this run and have it refused. The isolation is
    the test process's to arrange, and it is the only thing here that reaches
    for a module-private name.
    """
    putils_component._under_construction.set(())  # pyright: ignore[reportPrivateUsage]
    _ = await run_with(monitor, stack=stack, project=project, preview=preview)
    install_parent_backstop()
    return monitor


@dataclass(eq=False)
class _Run:
    """One run `run_with` started, as the judgment of its failures reads it.

    `manager` is the SDK's own record of the run, which keeps every RPC the
    run has started, in the order each began: a future apiece, which holds
    the RPC's failure once it has one. That future is one record judged here.
    The SDK keeps a slot for the run's first failure besides, which a failure
    that comes while it is full never reaches, so a run judged by the slot
    would be judged by one of its failures; nothing here reads it.

    The other record is the SDK's set of tracked outputs (`_tracked`): the
    future behind every output, which leaves the set when it succeeds, or
    when an output derived from it takes it over, and stays there when it
    fails. A real program awaits every future still in it on its way out
    (`wait_for_rpcs(await_all_outstanding_tasks=True)` in
    `pulumi/runtime/stack.py`), so one that failed fails the deployment
    even where no RPC carries the failure -- an output nothing declared
    reads. The set is the process's rather than the run's, so the run's
    share of it is the futures on one of the run's loops that joined it
    since the case's mark (`begin_case`).
    """

    manager: RPCManager
    #: Each loop the run is known to have work on: the one it started on, and
    #: each one a declaration on it (`_watch`) or a barrier over it has run on
    #: since.
    loops: list[asyncio.AbstractEventLoop]
    #: How many of the run's RPCs began before the current case's mark. Those
    #: belong to an earlier case -- judged at its end, or failed already -- so
    #: a judgment and a receipt read only the ones after.
    marked: int = 0
    #: The root (`root_of`) of each failure the current case received. A
    #: failure is received when its root is one of these, so the one refusal
    #: that failed several RPCs -- every resource whose inputs it reached -- is
    #: received once, and a failure with a root of its own is not.
    received: list[BaseException] = field(default_factory=list)

    def mark(self) -> None:
        """Begin a case: every RPC started so far is an earlier case's, and so is every receipt."""
        self.marked = len(self.manager.rpcs)
        self.received.clear()

    def since_mark(self) -> list[asyncio.Future[Any]]:
        """The run's RPCs that began since the current case's mark, in the order they began."""
        return [rpc for rpc in self.manager.rpcs[self.marked :] if isinstance(rpc, asyncio.Future)]

    def outputs_since_mark(self) -> list[asyncio.Future[Any]]:
        """The tracked outputs on one of the run's loops that joined the set since the current case's mark.

        In no order the program gave them: the set keeps none. The future of
        the task asking is left out, since it cannot wait for itself.
        """
        asking = _asking()
        return [
            output
            for output in _tracked() - _outputs_at_mark
            # An output of a loop no mocked run has work on is no run's to judge.
            if output is not asking and any(output.get_loop() is loop for loop in self.loops)
        ]

    def outstanding(self, loop: asyncio.AbstractEventLoop | None = None) -> list[asyncio.Future[Any]]:
        """The RPCs and the outputs since the mark still in flight, on `loop` or on any loop not yet closed.

        Each is a future of one loop, so it can be finished only there, and one
        on a loop already closed never will be: the loop cancelled it on the way.
        """
        return [
            future
            for future in [*self.since_mark(), *self.outputs_since_mark()]
            if not future.done()
            and (future.get_loop() is loop if loop is not None else not future.get_loop().is_closed())
        ]

    def learn(self, loop: asyncio.AbstractEventLoop) -> None:
        """Add `loop` to the loops the run has work on, once."""
        if not any(loop is it for it in self.loops):
            self.loops.append(loop)
            _ReadFailures.install(loop)

    def open_loops(self) -> list[asyncio.AbstractEventLoop]:
        """Each loop the run has work on that is not yet closed, once."""
        return [loop for loop in self.loops if not loop.is_closed()]

    def failures(self) -> list[BaseException]:
        """Each failure since the mark: the RPCs', in the order they began, then the outputs'.

        Reading a failed output's failure here is also what retrieves it, so
        asyncio does not log it as never retrieved when the future is
        collected -- which can be cases later, in whichever case is running
        then.
        """
        futures = [*self.since_mark(), *self.outputs_since_mark()]
        return [
            failure
            for future in futures
            if future.done() and not future.cancelled()
            if (failure := future.exception()) is not None
        ]

    def unreceived(self) -> list[BaseException]:
        """Each of `failures` the case has not received, and that a real program's wait does not pass over.

        The one it passes over is an output the SDK faults with
        `ResourceRegistrationFailed`: it is how the engine reports a resource
        that failed to register, and the program goes on.
        """
        return [
            failure
            for failure in self.failures()
            if not isinstance(failure, ResourceRegistrationFailed) and not self.has_received(failure)
        ]

    def has_received(self, failure: BaseException) -> bool:
        """Whether a receipt of the current case, on this run or any other on the record, names the root of `failure`.

        Any run's, because an output belongs to every run on its loop and a
        receipt is the case's: every run's receipts are emptied at its mark,
        and a root is one refusal whichever run it reached.
        """
        root = root_of(failure, BaseException)
        return any(root is it for run in [self, *_runs] for it in run.received)

    def receive(self, failure: BaseException) -> None:
        """Record that `failure` has reached the case, and with it every failure that has its root."""
        if not self.has_received(failure):
            self.received.append(root_of(failure, BaseException))


#: Every run `run_with` has started that a case may still add to, oldest first.
_runs: list[_Run] = []

#: The SDK's tracked outputs as the current case's mark found them. Each is an
#: earlier case's: judged at its end, or failed already.
_outputs_at_mark: set[asyncio.Future[Any]] = set()


def _tracked() -> set[asyncio.Future[Any]]:
    """The futures of the outputs the SDK tracks for its wait at a program's end, now.

    `SETTINGS.outputs`, which `Output._track` in `pulumi/output.py` adds every
    output's future to. One set for the process: `set_mocks` installs
    settings that carry none of their own, so each run adds to the set the
    SDK's import made. The tracking is the SDK's alone and nothing here
    changes it.
    """
    # Both are declared through the SDK's own `contextproperty` decorator,
    # which the checker cannot read as an attribute of `Settings`.
    tracked: set[asyncio.Future[Any]] = SETTINGS.outputs  # pyright: ignore[reportAttributeAccessIssue]
    lock: AbstractContextManager[object] = SETTINGS.lock  # pyright: ignore[reportAttributeAccessIssue]
    with lock:
        return set(tracked)


def _asking() -> asyncio.Future[Any] | None:
    """The task running now, or `None` outside every running loop."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class UnreceivedFailure(AssertionError):
    """A run's RPC or output failed, and nothing the case awaited received the failure; the failure is the cause."""


def begin_case() -> None:
    """Mark where a case begins on every run, and drop each run that can do nothing more.

    Everything a run did before the mark is an earlier case's, judged at its
    end or failed already, so the case's own judgment and its receipts read
    what comes after: the RPCs that begin after it, and the outputs that join
    the SDK's tracked set after it. A run stays on the record while one of
    its loops is open, however many cases ago it started: a module's shared
    run has work on the module's loop until the module ends.
    """
    _runs[:] = [run for run in _runs if run.open_loops()]
    for run in _runs:
        run.mark()
    _outputs_at_mark.clear()
    _outputs_at_mark.update(_tracked())


def fail_on_unreceived_failures() -> None:
    """Raise if an RPC or an output of a run met a failure since the case's mark that the case never received.

    Declaring a resource schedules its registration, and the SDK carries the
    registration out in a task of its own -- its RPC -- and, under the mocks,
    on an executor thread, where the recorder refuses a duplicate URN. A
    failure there reaches the program through the refused resource's
    outputs, which most cases never read, and through `wait_for_rpcs`, which
    a real program meets on its way out and which fails the deployment; a
    case that does neither passes with the failure printed beside it. The
    same wait also awaits every output the SDK still tracks, so an output
    that failed fails the deployment too, with no RPC carrying the failure:
    one nothing declared reads. This is the wait such a case does not write:
    on every run still on the record -- the ones the case started, and a
    wider fixture's run whose loop is still open -- it lets every RPC begun
    and every output tracked since the case's mark finish, on the loop each
    belongs to, and raises `UnreceivedFailure` from the first failure among
    them that the case did not receive, with each other failure of another
    root added as a note. It is called from outside every loop, once the
    case has returned and before any fixture is torn down, so what is still
    in flight finishes under the case's own arrangements; a loop already
    closed has nothing left to finish.

    Each failure is judged on its own, by the future of the RPC or the
    output that met it, and every RPC's failure is judged and not only a
    registration's -- registrations and invokes alike -- because a real run
    fails on any of them. A failure the case received fails nothing here:
    `declaring` receives the failures that have the root of what leaves it,
    and `the_run_failed_with` the ones with the root of what it hands back.
    An output awaited by the case does not count: a real program's wait
    awaits it again, and nothing here can tell what a case read.
    """
    failures: list[BaseException] = []
    for run in _runs:
        _settle_from_outside(run)
        failures.extend(run.unreceived())
        _ReadFailures.read(run.loops, run.failures())
    if failures:
        first, *others = _one_per_root(failures)
        unreceived = UnreceivedFailure(
            f'an RPC or an output of this run failed and nothing the case awaited received it: {first!r}'
        )
        for other in others:
            unreceived.add_note(f'and another RPC or output failed that nothing received either: {other!r}')
        raise unreceived from first


def _settle_from_outside(run: _Run) -> None:
    """Finish every RPC and output of the run since the mark, on each loop the run has open, from outside them all."""
    settled = False
    while not settled:
        while pending := run.outstanding():
            loop = pending[0].get_loop()
            _ = loop.run_until_complete(_finished(run.outstanding(loop)))
        for loop in run.open_loops():
            _ = loop.run_until_complete(_turned())
        settled = not run.outstanding()


async def _finished(futures: list[asyncio.Future[Any]]) -> None:
    """Wait until every one of `futures` has finished, however it finished."""
    _ = await asyncio.gather(*futures, return_exceptions=True)


async def _turned() -> None:
    """One turn of the loop, after which every RPC a declaration scheduled has begun.

    A declaration schedules a task that starts its RPC, and an RPC's ending
    is followed by its wrapper's (`RPCManager.do_rpc`), which can schedule
    more; one turn runs every wakeup already queued. A declaration whose
    outputs are still pending gets those turns from the wait on its
    outputs, so the turn here is for an RPC with no output pending beside
    it, which nothing else would start. It orders two events on a path only
    the loop advances, and waits on no clock (framework/testing.md §7).
    """
    await asyncio.sleep(0)


async def _settled(run: _Run) -> list[BaseException]:
    """The failures since the mark the case has not received, once every RPC and output on the running loop is done."""
    loop = asyncio.get_running_loop()
    run.learn(loop)
    settled = False
    while not settled:
        while pending := run.outstanding(loop):
            await _finished(pending)
        await _turned()
        settled = not run.outstanding(loop)
    return run.unreceived()


def _current_run() -> _Run:
    """The run the runtime is pointed at, as the record keeps it.

    A run `run_with` did not start is on no record, so one is made for it
    that nothing judges and that keeps its receipts for no one.
    """
    manager = settings._get_rpc_manager()  # pyright: ignore[reportPrivateUsage]
    for run in _runs:
        if run.manager is manager:
            return run
    return _Run(manager, [])


def _one_per_root[FailureT: BaseException](failures: list[FailureT]) -> list[FailureT]:
    """The first of `failures` for each root among them, in their order."""
    roots: list[BaseException] = []
    kept: list[FailureT] = []
    for failure in failures:
        root = root_of(failure, BaseException)
        if not any(root is it for it in roots):
            roots.append(root)
            kept.append(failure)
    return kept


def _deepest[FailureT: BaseException](failure: BaseException, expected: type[FailureT]) -> FailureT | None:
    """The deepest `expected` along the causes of `failure`, or `None` where there is none."""
    seen: list[BaseException] = []
    deepest: FailureT | None = None
    link: BaseException | None = failure
    while link is not None and not any(link is it for it in seen):
        if isinstance(link, expected):
            deepest = link
        seen.append(link)
        link = link.__cause__ or link.__context__
    return deepest


def root_of[FailureT: BaseException](failure: BaseException, expected: type[FailureT]) -> FailureT:
    """The deepest `expected` along the causes of `failure`, which is the program's own exception.

    The SDK raises its own exceptions from the program's -- `While processing
    resource: …` around `While processing input: …` around the refusal a
    component raised, each a `ValueError` where the refusal is one -- and a
    resource's outputs fail with the outermost of them. Followed down to the
    deepest `expected`, two failures the one refusal caused name the same
    exception, which a case can compare by identity. A failure with nothing of
    that type behind it is refused.
    """
    deepest = _deepest(failure, expected)
    if deepest is None:
        raise AssertionError(f'{failure!r} has no {expected.__name__} behind it') from failure
    return deepest


async def the_run_failed_with[FailureT: BaseException](expected: type[FailureT]) -> FailureT:
    """The `expected` at the root of a failure of the current run, received by the case whose subject it is.

    Waits for every RPC and output the run has outstanding on the running
    loop first, so a failure still in flight is found, and hands back
    `root_of` the first failure since the case's mark that has an `expected`
    behind it and that the case has not received yet: the program's own
    exception rather than the SDK's around it. First is in the order the
    RPCs began, and then among the outputs, which keep no order. A run that
    failed nothing, or failed with nothing of that type behind it, is
    refused.

    What is received is that failure's root, so every failure the one
    refusal caused is received with it and a failure of another root is not:
    each further refusal is a receipt of its own, and a duplicate URN beside
    a received refusal still fails the case. The SDK wraps a component's
    `ValueError` refusal in a `ValueError` of its own -- an `AssertionError`
    in an `AssertionError`, and a refusal of any other type not at all -- so
    a type that wide matches whichever such refusal's RPC began first, or any
    such refusal no RPC carries, and the caller holds what comes back to the
    refusal it caught, by identity.
    """
    run = _current_run()
    failures = await _settled(run)
    if not failures:
        raise AssertionError(f'the run failed nothing, where the case expected {expected.__name__}')
    for failure in failures:
        if _deepest(failure, expected) is not None:
            run.receive(failure)
            return root_of(failure, expected)
    raise AssertionError(f'the run failed with nothing of type {expected.__name__} behind it: {failures!r}')


async def the_run_failed_with_each[FailureT: BaseException](expected: type[FailureT]) -> list[FailureT]:
    """`the_run_failed_with` for a case whose subject is several refusals: each `expected` the run failed with.

    Every failure since the case's mark that has an `expected` behind it and
    that the case has not received yet is received, and handed back as
    `root_of` it, once per root, in the order the RPCs began and then the
    outputs' failures, in no order: a program that
    refuses at several places on its own -- each node, each anchor -- fails
    once at each, and a case reads all of them rather than the first. A
    failure with nothing of that type behind it is left for the hook, and a
    run with no such failure is refused.

    The caller holds the list it gets back to the refusals it caught, as a
    set by identity: a bare receipt of a wide type receives every such
    refusal, related or not.
    """
    run = _current_run()
    found = [failure for failure in await _settled(run) if _deepest(failure, expected) is not None]
    if not found:
        raise AssertionError(f'the run failed with no {expected.__name__} the case has not received')
    for failure in found:
        run.receive(failure)
    return [root_of(failure, expected) for failure in _one_per_root(found)]


@asynccontextmanager
async def declaring() -> AsyncGenerator[None]:
    """Wait, on the way out, until the monitor has seen what the block declared.

    Declaring a resource only schedules its registration, so without this the
    monitor has seen nothing and every assertion about it passes vacuously.

    Only the tasks the block itself added are awaited. The task queue is
    process-global and holds, among other things, the deliberately failing
    outputs other modules park in it, so draining it wholesale would fail a
    suite for something another suite arranged on purpose.

    An exception that leaves the block is re-raised only after the same wait,
    because the read after a refused block is an absence read -- that the
    refusal declared nothing -- and before the wait it passes whatever the
    block declared. It is the block's exception that leaves, since that is the
    one a case's `pytest.raises` around the block waits for; a failure the wait
    meets on that path is added to it as a note instead. Catching the refusal
    inside the block, ``async with declaring(): with pytest.raises(...): ...``,
    lets the block end normally, so the wait runs as on any other exit. On
    that exit every task the block started settles before anything is raised,
    and then the first failure is, with each other failure of another root
    added to it as a note: a case that catches what is raised and reads the
    run reads a run with nothing of the block still in flight. An exception
    that is not an `Exception` -- a cancellation, an interrupt, a case's
    timeout -- leaves at once, without the wait.

    A failure among the run's RPCs and outputs that the wait meets has reached
    the case when its root is the root of the exception that leaves -- the
    SDK's wrapper of the very refusal that leaves, or that a task of the block
    failed with first. Only then is it received, so that
    `fail_on_unreceived_failures` does not fail the case for it a second time.
    A failure merely added as a note has not reached the case: a
    `pytest.raises` around the block reads no notes, and a duplicate URN noted
    on a refusal the case catches would otherwise pass.
    """
    before = asyncio.all_tasks()
    run = _current_run()
    try:
        yield
    except Exception as escaped:
        outcomes = await asyncio.gather(*_added_since(before), return_exceptions=True)
        run.receive(escaped)
        outcomes.extend(await _settled(run))
        for outcome in _one_per_root([it for it in outcomes if isinstance(it, BaseException)]):
            escaped.add_note(f'and the wait for what the block declared failed: {outcome!r}')
        raise
    # In the order the tasks finish, which a gather's results do not keep:
    # the failure raised is the first to happen, as a bare gather raises it.
    failures: list[Exception] = []
    for finished in asyncio.as_completed(_added_since(before)):
        try:
            _ = await finished
        except Exception as failure:  # noqa: BLE001 -- kept until every task has settled, then raised
            failures.append(failure)
    # The run's failures are `Exception`s here: every RPC failure is one,
    # since `RPCManager.do_rpc` catches `Exception` and nothing wider.
    failures.extend(cast('list[Exception]', await _settled(run)))
    if failures:
        first, *others = _one_per_root(failures)
        run.receive(first)
        for other in others:
            first.add_note(f'and the wait for what the block declared failed too: {other!r}')
        raise first


def _added_since(before: set[asyncio.Task[Any]]) -> set[asyncio.Task[Any]]:
    """The tasks started since `before` was taken, other than the one asking."""
    return asyncio.all_tasks() - before - {asyncio.current_task()}


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
    mock = settings.get_monitor()
    assert isinstance(mock, mocks.MockMonitor)

    def declined(request: resource_pb2.ResourceInvokeRequest) -> resource_pb2.ResourceInvokeResponse:
        return resource_pb2.ResourceInvokeResponse(unknown=True)

    mock.Invoke = declined


# -- the workaround ------------------------------------------------------------
#
# The SDK leaves futures of an output it fails unretrieved. An output's task
# awaits its resources, its value, whether it is known and whether it is
# secret in turn, and stops at the first that fails (`Output.__init__` in
# `pulumi/output.py`); where one exception fails several of them, the rest
# are never awaited. That is every output of a refused resource -- the SDK
# fails value, known and secret together (`do_resolve` in
# `pulumi/runtime/rpc.py`) -- and every `putils.async_output` whose coroutine
# failed, since all four of its futures come from that one coroutine. asyncio
# logs each as never retrieved when it is collected, which can be cases after
# the one whose run failed it, and nothing the judgment can reach holds it, so
# what the judgment does instead is drop the report. A report made before the
# judgment has read the root is not dropped, and lands in the case that is
# still running, its own. The layer is `_ReadFailures` and its two call
# sites, `_Run.learn` and `fail_on_unreceived_failures`, and it comes out
# whole when the SDK's output awaits every future it was given, which
# `tests/test_mock_monitor_failures.py` shows: its tripwire on the SDK fails.


class _ReadFailures:
    """A loop's exception handler that drops the report of a failure never retrieved once the judgment read its root.

    A report of anything else, or of a failure whose root the judgment never
    read, goes on to the handler the loop had before, or to the loop's
    default. The roots read are kept with the handler, and so with the loop,
    which every future of the loop holds: a report made once the loop has
    closed, cases later, still finds them.
    """

    def __init__(self, previous: Callable[[asyncio.AbstractEventLoop, dict[str, Any]], object] | None) -> None:
        self.previous = previous
        #: The root (`root_of`) of every failure a judgment read on a run with work on this loop.
        self.roots: list[BaseException] = []

    def __call__(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        failure = context.get('exception')
        if (
            isinstance(failure, BaseException)
            and str(context.get('message', '')).endswith('was never retrieved')
            and any(root_of(failure, BaseException) is root for root in self.roots)
        ):
            return
        if self.previous is None:
            loop.default_exception_handler(context)
        else:
            _ = self.previous(loop, context)

    @classmethod
    def install(cls, loop: asyncio.AbstractEventLoop) -> None:
        """Put one in front of whatever handler `loop` has, unless one is there already."""
        previous = loop.get_exception_handler()
        if not isinstance(previous, cls):
            loop.set_exception_handler(cls(previous))

    @classmethod
    def read(cls, loops: list[asyncio.AbstractEventLoop], failures: list[BaseException]) -> None:
        """Record on each of `loops` that the judgment read `failures`."""
        roots = [root_of(failure, BaseException) for failure in failures]
        for loop in loops:
            handler = loop.get_exception_handler()
            if not isinstance(handler, cls):
                continue
            for root in roots:
                if not any(root is it for it in handler.roots):
                    handler.roots.append(root)
