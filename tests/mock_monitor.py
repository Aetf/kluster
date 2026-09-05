"""The mock monitor every declaration suite starts from, and the drain it needs.

Its own named module rather than a `conftest`, for the reason `memory_kit` is
one: test modules import it, and `conftest` is not a unique module name.

Three things live here, and they are the three every suite that declares
resources against Pulumi's mocks was re-growing:

-   `Recorder`, a monitor that invents nothing and remembers every
    declaration, so a case can ask what the program handed a provider rather
    than only that it made something;
-   `run_with`, which points the runtime at a monitor and primes the one thing
    a bridged provider needs before it may register anything;
-   `declaring`, which waits until the monitor has actually seen the
    declaration -- without it every assertion about the monitor passes
    vacuously.

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
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pulumi
import pulumi.runtime.mocks
import pulumi.runtime.settings
from pulumi.runtime.stack import wait_for_rpcs

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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
    logical name, which is what a provider that only defines things would do.

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
        #: The same requests indexed by logical name, last one wins. It cannot
        #: tell a component from its children, so nothing here reads it; the
        #: suites that ask only *whether* a name registered at all do.
        #: TODO(kluster-ops#209): delete with those readers.
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
        """Every logical name the run registered."""
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

        Not refused at registration, where an engine would refuse it: a suite
        may declare one component twice in a run on purpose -- a variant built
        beside its baseline, read for the configuration it renders rather than
        for the program it declares -- and that run is only wrong when something
        asks it a question by name.
        """
        declarations = self.of_type(typ)
        repeated = sorted(name for name, count in Counter(it.name for it in declarations).items() if count > 1)
        if repeated:
            raise AssertionError(
                f'{typ} was declared more than once under each of {repeated}, '
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


_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def _capture_request(self: Any, request: Any) -> Any:
    """Keep the two things Pulumi's mock monitor otherwise drops.

    The request itself, because a resource's *options* -- `import_`,
    `ignore_changes`, `delete_before_replace`, `depends_on` -- reach no output
    and are exactly what several suites are about -- every one of them, rather
    than only the last under each logical name; and the per-property
    dependency edges, which the mock's response leaves empty although the
    request carried them (framework/testing.md §3.1).

    Patched on the class, once, at import: `set_mocks` builds a fresh monitor
    per run, so there is no instance to hook, and the recording lands on
    whichever `Recorder` that monitor was built around rather than on a global.
    """
    if isinstance(self.mocks, Recorder):
        self.mocks.requested.append(request)
        self.mocks.registrations[request.name] = request
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
    """
    pulumi.runtime.set_mocks(monitor, project=project, stack=stack, preview=preview)
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
