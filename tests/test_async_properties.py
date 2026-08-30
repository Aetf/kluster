"""The native async inputs framework (rfc-001): `async_output`, `resolve`, `Component`.

What is proven here is the part that has no equivalent in a diff: which
dependencies an asynchronously computed input carries, what a preview does
with one whose upstream is unknown, and that a failure inside one fails the
run rather than hanging it.
"""

import asyncio
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pulumi.output import Unknown

from putils import Component, async_output, resolve


class Engine(Recorder):
    """A monitor that hands every resource an id derived from its name.

    Unlike the estate's suites this one is about the framework, so the
    resources are stand-ins and the only interesting answer is the id: a
    network's is what a component's async input is computed from, and
    withholding it is what a preview of an unbuilt stack looks like.
    """

    def __init__(self, *, network_id_unknown: bool = False) -> None:
        super().__init__()
        self.network_id_unknown: bool = network_id_unknown

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        identifier, outputs = super().new_resource(args)
        if args.typ != 'gcp:compute:Network':
            return identifier, {'id': identifier, **outputs}
        if self.network_id_unknown:
            return '', {'id': pulumi.UNKNOWN}
        return identifier, {'id': identifier}


class VPC(pulumi.CustomResource):
    """A resource whose id another resource's input is computed from."""

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__('gcp:compute:Network', name, {'id': None}, opts)


class Subnet(pulumi.CustomResource):
    """A resource with one plainly passed input and one computed asynchronously."""

    network_id: pulumi.Output[str]
    cidr: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        network_id: pulumi.Input[str] | None = None,
        cidr: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('gcp:compute:Subnetwork', name, {'network_id': network_id, 'cidr': cidr}, opts)


class VpcSubnetComponent(Component, pulumi_type='test:VpcSubnetComponent'):
    """The canonical new-style component used across tests."""

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(name, opts=opts)
        self.vpc: VPC = VPC(f'{name}-vpc', opts=self.child_opts())
        self.subnet: Subnet = Subnet(
            f'{name}-subnet',
            cidr='10.0.1.0/24',  # known value passed plainly
            network_id=async_output(self._network_id),
            opts=self.child_opts(),
        )
        self.register_outputs({})

    async def _network_id(self) -> str:
        vpc_id = await resolve(self.vpc.id)
        return f'subnet-for-{vpc_id}'


@pytest_asyncio.fixture
async def mocks() -> Engine:
    return await run_with(Engine(), stack='dev', project='my-project')


@pytest_asyncio.fixture
async def previewing() -> Engine:
    """A preview of a stack whose network has not been created yet."""
    return await run_with(Engine(network_id_unknown=True), stack='dev', project='my-project', preview=True)


@pytest.mark.asyncio
async def test_an_async_input_resolves_beside_a_plainly_passed_one(mocks: Engine) -> None:
    comp = VpcSubnetComponent('my-comp')

    assert await comp.subnet.network_id.future() == 'subnet-for-my-comp-vpc_id'
    assert await comp.subnet.cidr.future() == '10.0.1.0/24'


@pytest.mark.asyncio
async def test_a_components_child_is_registered_under_the_components_name(mocks: Engine) -> None:
    async with declaring():
        _ = VpcSubnetComponent('my-comp')

    assert mocks.names('gcp:compute:Subnetwork') == {'my-comp-subnet'}


@pytest.mark.asyncio
async def test_resolving_one_output_gives_its_value(mocks: Engine) -> None:
    vpc = VPC('vpc-a')

    async def single() -> str:
        return await resolve(vpc.id)

    assert await async_output(single).future() == 'vpc-a_id'


@pytest.mark.asyncio
async def test_resolving_several_outputs_gives_them_in_order(mocks: Engine) -> None:
    vpc_a = VPC('vpc-a')
    vpc_b = VPC('vpc-b')

    async def multiple() -> tuple[str, ...]:
        return await resolve(vpc_a.id, vpc_b.id)

    assert await async_output(multiple).future() == ('vpc-a_id', 'vpc-b_id')


def test_resolving_nothing_is_refused() -> None:
    with pytest.raises(TypeError, match='at least one output'):
        resolve()


@pytest.mark.asyncio
async def test_an_async_input_carries_the_resources_it_resolved(mocks: Engine) -> None:
    comp = VpcSubnetComponent('dep-comp')
    await comp.subnet.network_id.future()

    # The engine-registered subnet input must carry the vpc as a dependency.
    deps = await comp.subnet.network_id.resources()
    dep_urns = {await r.urn.future() for r in deps}
    assert await comp.vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_a_dependency_resolved_inside_a_gathered_task_is_still_carried(mocks: Engine) -> None:
    vpc = VPC('gather-vpc')

    async def prepare():
        results = await asyncio.gather(
            resolve_one(vpc.id),
            asyncio.sleep(0.01),
        )
        return f'subnet-for-{results[0]}'

    async def resolve_one(out: pulumi.Output[str]) -> str:
        # Awaited inside a nested task spawned by gather; the shared mutable
        # set in the ContextVar must still capture the dependency.
        return await resolve(out)

    out = async_output(prepare)
    assert await out.future() == 'subnet-for-gather-vpc_id'
    dep_urns = {await r.urn.future() for r in await out.resources()}
    assert await vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_a_dependency_on_a_resource_outside_the_component_is_carried(mocks: Engine) -> None:
    existing_vpc = VPC('existing-vpc')

    class ComponentWithExternalRes(Component, pulumi_type='test:ExternalRes'):
        def __init__(self, name: str, ext_vpc: VPC, opts: pulumi.ResourceOptions | None = None) -> None:
            super().__init__(name, opts=opts)
            self.subnet: Subnet = Subnet(
                f'{name}-subnet',
                cidr='10.0.1.0/24',
                network_id=async_output(self._network_id(ext_vpc)),
                opts=self.child_opts(),
            )
            self.register_outputs({})

        async def _network_id(self, ext_vpc: VPC) -> str:
            vpc_id = await resolve(ext_vpc.id)
            return f'subnet-for-{vpc_id}'

    comp = ComponentWithExternalRes('comp-ext', existing_vpc)
    await comp.subnet.network_id.future()

    dep_urns = {await r.urn.future() for r in await comp.subnet.network_id.resources()}
    assert await existing_vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_preview_unknown_keeps_sibling_inputs_known(previewing: Engine) -> None:
    # The async network_id input must become unknown while the plainly-passed
    # cidr stays concrete, which is what makes a preview's diff fine-grained.
    comp = VpcSubnetComponent('preview-comp')

    assert await comp.subnet.cidr.future() == '10.0.1.0/24'
    network_id = await comp.subnet.network_id.future(with_unknowns=True)
    assert isinstance(network_id, Unknown)


@pytest.mark.asyncio
async def test_a_failure_inside_an_async_input_fails_its_output(mocks: Engine) -> None:
    async def broken():
        raise RuntimeError('boom in async input')

    out = async_output(broken)
    with pytest.raises(RuntimeError, match='boom in async input'):
        await out.future()


@pytest.mark.asyncio
async def test_a_preview_that_aborts_on_an_unknown_still_carries_its_dependencies(previewing: Engine) -> None:
    # RFC-001 §4.2: dependencies awaited before the unknown abort must still be
    # recorded on the resulting (unknown) output.
    vpc = VPC('abort-vpc')

    async def prepare():
        vpc_id = await resolve(vpc.id)
        return f'subnet-for-{vpc_id}'

    out = async_output(prepare)
    assert await out.is_known() is False
    dep_urns = {await r.urn.future() for r in await out.resources()}
    assert await vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_an_input_that_resolved_a_secret_is_itself_secret(mocks: Engine) -> None:
    secret_in = pulumi.Output.secret('s3cret')
    plain_in = pulumi.Output.from_input('plain')

    async def uses_secret():
        return await resolve(secret_in)

    async def uses_plain():
        return await resolve(plain_in)

    secret_out = async_output(uses_secret)
    assert await secret_out.future() == 's3cret'
    assert await secret_out.is_secret() is True

    plain_out = async_output(uses_plain)
    assert await plain_out.future() == 'plain'
    assert await plain_out.is_secret() is False


@pytest.mark.asyncio
async def test_an_upstream_failure_fails_the_input_rather_than_hanging_it(mocks: Engine) -> None:
    # An output whose future fails (e.g. its resource registration failed)
    # must fail the async_output instead of hanging it.
    async def fail():
        raise RuntimeError('upstream boom')

    async def known():
        return True

    bad = pulumi.Output(set(), fail(), known())

    async def consume():
        return await resolve(bad)

    out = async_output(consume)
    with pytest.raises(RuntimeError, match='upstream boom'):
        await asyncio.wait_for(out.future(), 5)


@pytest.mark.asyncio
async def test_resolve_outside_an_async_input_is_refused(mocks: Engine) -> None:
    vpc = VPC('ctx-vpc')
    with pytest.raises(RuntimeError, match='async_output'):
        await resolve(vpc.id)
