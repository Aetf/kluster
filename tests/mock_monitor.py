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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pulumi
import pulumi.runtime.mocks
import pulumi.runtime.settings
from pulumi.runtime.stack import wait_for_rpcs

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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
        #: The raw registration request of each resource, by logical name --
        #: the only place the resource *options* survive. See `_capture_request`.
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

    def names(self, typ: str) -> set[str]:
        """The logical names registered under one type."""
        return {declaration.name for declaration in self.of_type(typ)}

    def by_name(self, typ: str) -> dict[str, dict[str, Any]]:
        """What each resource of one type was declared with, by logical name."""
        return {declaration.name: declaration.inputs for declaration in self.of_type(typ)}

    def one(self, name: str, typ: str | None = None) -> Declaration:
        """The declaration under this name, which must be exactly one.

        A logical name is unique within a type rather than within a run, so a
        suite that declares the same name under two types passes `typ` too.
        """
        found = [
            declaration for declaration in self.declared if declaration.name == name and typ in (None, declaration.typ)
        ]
        if len(found) != 1:
            declared = sorted((declaration.typ, declaration.name) for declaration in self.declared)
            raise AssertionError(f'{name} was declared {len(found)} times, not once; the run declared {declared}')
        return found[0]

    def inputs_of(self, name: str, typ: str | None = None) -> dict[str, Any]:
        """What this resource was declared with."""
        return self.one(name, typ).inputs

    def provider_of(self, name: str, typ: str | None = None) -> str:
        """The provider instance this resource was registered against."""
        return self.one(name, typ).provider

    def options_of(self, name: str) -> Any:
        """The registration request of this resource, which is where its options are."""
        if name not in self.registrations:
            raise AssertionError(f'{name} was never registered; the run registered {sorted(self.registrations)}')
        return self.registrations[name]

    def depends_on(self, name: str) -> list[str]:
        """The URNs this resource was declared to depend on."""
        return list(self.options_of(name).dependencies)


_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def _capture_request(self: Any, request: Any) -> Any:
    """Keep the two things Pulumi's mock monitor otherwise drops.

    The request itself, because a resource's *options* -- `import_`,
    `ignore_changes`, `delete_before_replace`, `depends_on` -- reach no output
    and are exactly what several suites are about; and the per-property
    dependency edges, which the mock's response leaves empty although the
    request carried them (framework/testing.md §3.1).

    Patched on the class, once, at import: `set_mocks` builds a fresh monitor
    per run, so there is no instance to hook, and the recording lands on
    whichever `Recorder` that monitor was built around rather than on a global.
    """
    if isinstance(self.mocks, Recorder):
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
