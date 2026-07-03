"""
Verification suite for the native async inputs framework (RFC-001):
`async_output`, `resolve`, and the `Component` base class.
"""

import asyncio

import pytest
import pytest_asyncio
import pulumi
import pulumi.runtime.mocks
from pulumi.output import Unknown
from pulumi.runtime.proto import resource_pb2

# Monkey-patch MockMonitor to preserve property dependencies in tests.
# See docs/testing.md section 3.1.
original_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource

def patched_register_resource(self, request):
    resp = original_register_resource(self, request)
    if isinstance(resp, resource_pb2.RegisterResourceResponse):
        for k, v in request.propertyDependencies.items():
            resp.propertyDependencies[k].urns.extend(v.urns)
    return resp

pulumi.runtime.mocks.MockMonitor.RegisterResource = patched_register_resource

from putils import Component, async_output, resolve


# Mock definitions
class MyMocks(pulumi.runtime.Mocks):
    def __init__(self, unknown_network_id: bool = False):
        self.resources = []
        self.unknown_network_id = unknown_network_id

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        if args.typ == "gcp:compute:Network":
            if self.unknown_network_id:
                return ["", {"id": pulumi.UNKNOWN}]
            return [args.name + "_id", {"id": args.name + "_id"}]
        return [args.name + "_id", {"id": args.name + "_id", **args.inputs}]

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


# Custom Resources for testing
class VPC(pulumi.CustomResource):
    id: pulumi.Output[str]
    def __init__(self, name: str, opts=None):
        super().__init__("gcp:compute:Network", name, {"id": None}, opts)


class Subnet(pulumi.CustomResource):
    id: pulumi.Output[str]
    network_id: pulumi.Output[str]
    cidr: pulumi.Output[str]
    def __init__(self, name: str, network_id: pulumi.Input[str] = None, cidr: pulumi.Input[str] = None, opts=None):
        super().__init__("gcp:compute:Subnetwork", name, {"network_id": network_id, "cidr": cidr}, opts)


class VpcSubnetComponent(Component, pulumi_type='test:VpcSubnetComponent'):
    """The canonical new-style component used across tests."""

    def __init__(self, name: str, opts=None):
        super().__init__(name, opts=opts)
        self.vpc = VPC(f"{name}-vpc", opts=self.child_opts())
        self.subnet = Subnet(
            f"{name}-subnet",
            cidr="10.0.1.0/24",  # known value passed plainly
            network_id=async_output(self._network_id),
            opts=self.child_opts(),
        )
        self.register_outputs({})

    async def _network_id(self) -> str:
        vpc_id = await resolve(self.vpc.id)
        return f"subnet-for-{vpc_id}"


@pytest_asyncio.fixture
async def mocks():
    m = MyMocks()
    pulumi.runtime.set_mocks(m, project="my-project", stack="dev", preview=False)
    return m


@pytest.mark.asyncio
async def test_async_input_resolution_and_parenting(mocks):
    comp = VpcSubnetComponent("my-comp")

    assert await comp.subnet.network_id.future() == "subnet-for-my-comp-vpc_id"
    assert await comp.subnet.cidr.future() == "10.0.1.0/24"

    subnet_reg = next(r for r in mocks.resources if r.typ == "gcp:compute:Subnetwork")
    assert subnet_reg.name == "my-comp-subnet"


@pytest.mark.asyncio
async def test_resolve_single_and_multiple(mocks):
    vpc_a = VPC("vpc-a")
    vpc_b = VPC("vpc-b")

    async def single():
        return await resolve(vpc_a.id)

    async def multiple():
        return await resolve(vpc_a.id, vpc_b.id)

    assert await async_output(single).future() == "vpc-a_id"
    assert await async_output(multiple).future() == ("vpc-a_id", "vpc-b_id")

    with pytest.raises(TypeError, match="at least one output"):
        resolve()


@pytest.mark.asyncio
async def test_dependency_tracking(mocks):
    comp = VpcSubnetComponent("dep-comp")
    await comp.subnet.network_id.future()

    # The engine-registered subnet input must carry the vpc as a dependency.
    deps = await comp.subnet.network_id.resources()
    dep_urns = {await r.urn.future() for r in deps}
    assert await comp.vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_dependency_tracking_inside_gather(mocks):
    vpc = VPC("gather-vpc")

    async def prepare():
        results = await asyncio.gather(
            resolve_one(vpc.id),
            asyncio.sleep(0.01),
        )
        return f"subnet-for-{results[0]}"

    async def resolve_one(out):
        # Awaited inside a nested task spawned by gather; the shared mutable
        # set in the ContextVar must still capture the dependency.
        return await resolve(out)

    out = async_output(prepare)
    assert await out.future() == "subnet-for-gather-vpc_id"
    dep_urns = {await r.urn.future() for r in await out.resources()}
    assert await vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_external_resource_dependency_tracking(mocks):
    existing_vpc = VPC("existing-vpc")

    class ComponentWithExternalRes(Component, pulumi_type='test:ExternalRes'):
        def __init__(self, name: str, ext_vpc: VPC, opts=None):
            super().__init__(name, opts=opts)
            self.subnet = Subnet(
                f"{name}-subnet",
                cidr="10.0.1.0/24",
                network_id=async_output(self._network_id(ext_vpc)),
                opts=self.child_opts(),
            )
            self.register_outputs({})

        async def _network_id(self, ext_vpc: VPC) -> str:
            vpc_id = await resolve(ext_vpc.id)
            return f"subnet-for-{vpc_id}"

    comp = ComponentWithExternalRes("comp-ext", existing_vpc)
    await comp.subnet.network_id.future()

    dep_urns = {await r.urn.future() for r in await comp.subnet.network_id.resources()}
    assert await existing_vpc.urn.future() in dep_urns


@pytest.mark.asyncio
async def test_preview_unknown_keeps_sibling_inputs_known():
    # During preview vpc.id is unknown; the async network_id input must become
    # unknown while the plainly-passed cidr stays concrete (fine-grained diff).
    pulumi.runtime.set_mocks(
        MyMocks(unknown_network_id=True),
        project="my-project", stack="dev", preview=True,
    )

    comp = VpcSubnetComponent("preview-comp")

    assert await comp.subnet.cidr.future() == "10.0.1.0/24"
    network_id = await comp.subnet.network_id.future(with_unknowns=True)
    assert isinstance(network_id, Unknown)


@pytest.mark.asyncio
async def test_exception_in_async_input_propagates(mocks):
    async def broken():
        raise RuntimeError("boom in async input")

    out = async_output(broken)
    with pytest.raises(RuntimeError, match="boom in async input"):
        await out.future()


@pytest.mark.asyncio
async def test_legacy_sync_setup(mocks):
    class LegacyProbe(Component, pulumi_type='test:LegacyProbe'):
        namespace: pulumi.Output[str]

        def setup(self, name, *pargs, opts=None, **kwargs):
            return {'namespace': 'default'}

    comp = LegacyProbe("probe")
    assert await asyncio.wait_for(comp.namespace.future(), 5) == 'default'


@pytest.mark.asyncio
async def test_legacy_async_setup(mocks):
    class LegacyAsync(Component, pulumi_type='test:LegacyAsync'):
        vpc_id: pulumi.Output[str]

        async def setup(self, name, *pargs, opts=None, **kwargs):
            vpc = VPC(f'{name}-vpc', opts=pulumi.ResourceOptions(parent=self))
            return {'vpc_id': vpc.id}

    comp = LegacyAsync("legacy-async")
    vpc_id = await asyncio.wait_for(comp.vpc_id.future(), 5)
    # vpc_id output resolves to the inner Output's value once unwrapped
    assert await pulumi.Output.from_input(vpc_id).future() == 'legacy-async-vpc_id'
