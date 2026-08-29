"""The parent backstop: what `putils` does about a forgotten ``parent=``.

A resource declared inside a component without a parent is not an error to
Pulumi. It lands on the stack instead of on the component, inherits the stack's
providers instead of the component's, and the failure that follows — if any —
names a missing provider rather than a missing parent. rfc-002 §8.2 is why
the framework refuses it instead of repairing it.

The cases below are the boundary of that refusal, in both directions. Nothing
here contacts an engine: the monitor is Pulumi's mock, and every resource is a
stand-in whose only interesting property is the options it was given.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any, cast

import pulumi
import pulumi.runtime
import pulumi.runtime.settings
import pytest
import pytest_asyncio

from putils import Component, UnparentedChildError, install_parent_backstop
from putils import component as putils_component


class Mocks(pulumi.runtime.Mocks):
    """A monitor that hands every resource its own inputs back."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        return args.name + '_id', dict(cast('dict[str, Any]', args.inputs))

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        return {}, []


class Thing(pulumi.CustomResource):
    """A leaf resource, standing in for anything a component declares."""

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__('test:index:Thing', name, {}, opts)


class Prov(pulumi.ProviderResource):
    """A provider resource: as far as parentage goes, a resource like any other."""

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__('test', name, {}, opts)


@pytest_asyncio.fixture(autouse=True)
async def backstop() -> None:
    """Mocks, an empty scope, and the backstop on the root stack resource.

    Emptying the scope is what makes this module independent of the ones ahead
    of it: a component whose ``__init__`` raises never reaches
    `register_outputs`, so it stays open, and several suites elsewhere assert
    that a component refuses a bad argument. That ends a program — which is the
    documented behavior — but not a test process, so the isolation is the test
    process's to arrange, and it is the only thing here that reaches for a
    module-private name.
    """
    putils_component._under_construction.set(())  # pyright: ignore[reportPrivateUsage]
    pulumi.runtime.set_mocks(Mocks(), project='putils', stack='test', preview=False)
    install_parent_backstop()


def refused(build: Callable[[], object]) -> str:
    """Run a construction that must be refused, and return the message.

    In a context of its own, because a refusal escapes the enclosing
    component's ``__init__`` before its `register_outputs` runs — so the
    component stays open, and without the isolation one case would decide the
    next one's answer. In a program that is the correct outcome: the run is
    over.
    """

    def run() -> str:
        with pytest.raises(UnparentedChildError) as raised:
            build()
        return str(raised.value)

    return contextvars.copy_context().run(run)


class Wellformed(Component, pulumi_type='test:Wellformed'):
    """A component whose child names it, which is the whole of the rule."""

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.thing = Thing(f'{name}-thing', opts=self.child_opts())
        self.register_outputs({})


class Forgetful(Component, pulumi_type='test:Forgetful'):
    """A component whose child names nothing, which is the mistake."""

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.thing = Thing(f'{name}-thing')
        self.register_outputs({})


@pytest.mark.asyncio
async def test_a_child_without_a_parent_is_refused_by_name() -> None:
    """The refusal names both halves: which resource, and inside which component."""
    message = refused(lambda: Forgetful('forgetful'))

    assert 'test:index:Thing' in message
    assert 'forgetful-thing' in message
    assert 'forgetful (test:Forgetful)' in message


@pytest.mark.asyncio
async def test_a_parented_child_passes() -> None:
    component = Wellformed('wellformed')
    assert await component.thing.urn.future() is not None


@pytest.mark.asyncio
async def test_a_resource_outside_any_component_passes() -> None:
    """A stack program declares resources of its own, and they have no parent."""
    loose = Thing('loose')
    assert await loose.urn.future() is not None


@pytest.mark.asyncio
async def test_a_top_level_component_needs_no_parent() -> None:
    """A component is not its own child: its own registration is judged by what encloses it."""
    component = Wellformed('top-level')
    assert await component.urn.future() is not None


@pytest.mark.asyncio
async def test_a_nested_component_without_a_parent_is_refused() -> None:
    """The scope covers sub-components, not only leaf resources."""

    class Outer(Component, pulumi_type='test:Outer'):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.inner = Wellformed(f'{name}-inner')
            self.register_outputs({})

    message = refused(lambda: Outer('outer'))

    assert 'test:Wellformed' in message
    assert 'outer (test:Outer)' in message


@pytest.mark.asyncio
async def test_a_nested_component_with_a_parent_passes() -> None:
    """Nesting is legal; naming the parent is what makes it so."""

    class Outer(Component, pulumi_type='test:NestedOuter'):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.inner = Wellformed(f'{name}-inner', opts=self.child_opts())
            self.register_outputs({})

    outer = Outer('nested')
    assert await outer.inner.thing.urn.future() is not None


@pytest.mark.asyncio
async def test_a_sibling_after_a_nested_component_is_still_judged() -> None:
    """The inner component's `register_outputs` restores the outer scope, not an empty one."""

    class Outer(Component, pulumi_type='test:SiblingOuter'):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.inner = Wellformed(f'{name}-inner', opts=self.child_opts())
            self.after = Thing(f'{name}-after')
            self.register_outputs({})

    message = refused(lambda: Outer('sibling'))

    assert 'sibling-after' in message
    assert 'sibling (test:SiblingOuter)' in message


@pytest.mark.asyncio
async def test_the_scope_ends_at_register_outputs() -> None:
    """Once a component is closed, the stack program is back in charge of parentage."""
    Wellformed('closed')
    loose = Thing('after-close')
    assert await loose.urn.future() is not None


class BuildsProvider(Component, pulumi_type='test:BuildsProvider'):
    """A component that builds its own provider, with or without owning it.

    rfc-002 §8.1 builds a provider inside the component whose credential opens
    it, which makes the provider that component's child like anything else.
    """

    def __init__(self, name: str, *, parented: bool) -> None:
        super().__init__(name)
        self.provider = Prov(f'{name}-prov', opts=self.child_opts() if parented else None)
        self.register_outputs({})


@pytest.mark.asyncio
async def test_a_provider_built_inside_a_component_without_a_parent_is_refused() -> None:
    """Providers are not exempt from the rule."""
    message = refused(lambda: BuildsProvider('loose-provider', parented=False))

    assert 'pulumi:providers:test' in message
    assert 'loose-provider-prov' in message
    assert 'loose-provider (test:BuildsProvider)' in message


@pytest.mark.asyncio
async def test_a_provider_owned_by_the_component_that_built_it_passes() -> None:
    """Which is the shape rfc-002 §8.1 asks for."""
    component = BuildsProvider('owned-provider', parented=True)
    assert await component.provider.urn.future() is not None


@pytest.mark.asyncio
async def test_a_forgotten_register_outputs_is_bounded_by_the_enclosing_component() -> None:
    """A component that never closes leaks its scope until something above it closes.

    Documented behavior rather than a wish: `register_outputs` is the only
    thing that ends the scope, so an enclosing component's own close is what
    takes a leaked entry away with it.
    """

    class NeverCloses(Component, pulumi_type='test:NeverCloses'):
        def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
            super().__init__(name, opts=opts)
            self.thing = Thing(f'{name}-thing', opts=self.child_opts())

    class Encloses(Component, pulumi_type='test:Encloses'):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.inner = NeverCloses(f'{name}-inner', opts=self.child_opts())
            self.register_outputs({})

    Encloses('bounded')
    loose = Thing('after-bounded')
    assert await loose.urn.future() is not None


@pytest.mark.asyncio
async def test_a_forgotten_register_outputs_misnames_the_next_refusal() -> None:
    """The other half of the same behavior, and the reason it is worth documenting.

    Nothing above the leak means nothing closes it, so the next unparented
    resource — a sibling the stack program declares afterwards — is refused in
    the leaked component's name rather than in its own. Misleading, never
    absent.
    """

    class NeverCloses(Component, pulumi_type='test:NeverClosesTop'):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.thing = Thing(f'{name}-thing', opts=self.child_opts())

    def leak_then_declare() -> None:
        NeverCloses('leaky')
        Thing('innocent-bystander')

    message = refused(leak_then_declare)
    assert 'innocent-bystander' in message
    assert 'leaky (test:NeverClosesTop)' in message


@pytest.mark.asyncio
async def test_installing_twice_registers_one_transformation() -> None:
    """The install is idempotent per stack, and a refusal cannot show that.

    A second copy of the transformation would refuse the same resources the
    first one already refused — the first raise ends the registration — so the
    only place the difference is visible is the root stack resource's own list.
    The fixture has installed it once already; these two add nothing.
    """
    install_parent_backstop()
    install_parent_backstop()

    root = pulumi.runtime.get_root_resource()
    assert root is not None
    installed = [
        transformation
        for transformation in root._transformations  # pyright: ignore[reportPrivateUsage]
        if transformation is putils_component._refuse_unparented  # pyright: ignore[reportPrivateUsage]
    ]
    assert len(installed) == 1


@pytest.mark.asyncio
async def test_installing_outside_a_program_says_so() -> None:
    """Without a root stack resource there is nothing to hang a transformation off."""

    def run() -> None:
        pulumi.runtime.settings.ROOT.set(None)
        with pytest.raises(RuntimeError, match='root stack resource'):
            install_parent_backstop()

    contextvars.copy_context().run(run)
