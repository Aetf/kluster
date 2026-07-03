import asyncio
import inspect
from typing import TypedDict, Optional, List
import pytest
import pytest_asyncio
import pulumi
from pulumi import Output, UNKNOWN
from pulumi.output import Unknown
from pulumi.runtime.proto import resource_pb2
import pulumi.runtime.mocks

# Monkey-patch MockMonitor to preserve property dependencies in tests
original_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource

def patched_register_resource(self, request):
    resp = original_register_resource(self, request)
    if isinstance(resp, resource_pb2.RegisterResourceResponse):
        for k, v in request.propertyDependencies.items():
            resp.propertyDependencies[k].urns.extend(v.urns)
    return resp

pulumi.runtime.mocks.MockMonitor.RegisterResource = patched_register_resource

from putils.component import Component, setup, resolve, UnknownValueException


# Mock definitions
class MyMocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources = []
        self.mock_network_id = "vpc_id"

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        if args.typ == "gcp:compute:Network":
            physical_id = "" if self.mock_network_id is pulumi.UNKNOWN else args.name + "_id"
            return [physical_id, {"id": self.mock_network_id}]
        elif args.typ == "gcp:compute:Subnetwork":
            return [args.name + "_id", {"id": args.name + "_id", **args.inputs}]
        elif args.typ == "gcp:compute:MockResource":
            return [args.name + "_id", {"custom_val": args.inputs.get("custom_val")}]
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


# Custom Resources for testing
class VPC(pulumi.CustomResource):
    id: pulumi.Output[str]
    def __init__(self, name: str, opts = None):
        super().__init__("gcp:compute:Network", name, {"id": None}, opts)

class MockResourceWithInputs(pulumi.CustomResource):
    custom_val: pulumi.Output[str]
    def __init__(self, name: str, custom_val: pulumi.Input[str] = None, opts = None):
        super().__init__("gcp:compute:MockResource", name, {"custom_val": custom_val}, opts)


class Subnet(pulumi.CustomResource):
    id: pulumi.Output[str]
    network_id: pulumi.Output[str]
    cidr: pulumi.Output[str]
    def __init__(self, name: str, network_id: pulumi.Input[str] = None, cidr: pulumi.Input[str] = None, opts = None):
        super().__init__("gcp:compute:Subnetwork", name, {"network_id": network_id, "cidr": cidr}, opts)


# TypedDicts for type safety annotations
class VpcArgs(TypedDict):
    pass

class SubnetArgs(TypedDict):
    network_id: str
    cidr: str


@pytest_asyncio.fixture
async def mocks():
    m = MyMocks()
    pulumi.runtime.set_mocks(
        m,
        project="my-project",
        stack="dev",
        preview=False, # Standard execution mode by default
    )
    return m


@pytest.mark.asyncio
async def test_oop_direct_property_access_and_urns(mocks):
    class MyComponent(Component):
        vpc: VPC
        subnet: Subnet

        @setup("vpc")
        async def prepare_vpc(self) -> VpcArgs:
            return {}

        @setup("subnet", depends_on=["vpc"])
        async def prepare_subnet(self) -> SubnetArgs:
            vpc_id = await resolve(self.vpc.id)
            return {
                "network_id": f"subnet-for-{vpc_id}",
                "cidr": "10.0.1.0/24"
            }

    comp = MyComponent("my-comp")
    
    # Assert resolved outputs
    network_id = await comp.subnet.network_id.future()
    cidr = await comp.subnet.cidr.future()
    
    assert network_id == "subnet-for-my-comp-vpc_id"
    assert cidr == "10.0.1.0/24"

    # Assert parent/URN relation in registered resources
    subnet_reg = next(r for r in mocks.resources if r.typ == "gcp:compute:Subnetwork")
    assert subnet_reg is not None
    # Subnet name in new_resource args
    assert subnet_reg.name == "my-comp-subnet"


@pytest.mark.asyncio
async def test_strict_key_declaration_enforcement(mocks):
    # 1. Missing type hints and keys should raise ValueError
    with pytest.raises(ValueError, match="must specify return type annotation"):
        class BadComponent(Component):
            vpc: VPC
            @setup("vpc")
            async def prepare_vpc(self):
                return {}
        BadComponent("bad-comp")

    # 2. Succeeds with explicit keys
    class GoodComponent(Component):
        res: MockResourceWithInputs
        @setup("res", keys=["custom_val"])
        async def prepare_res(self):
            return {"custom_val": "custom-id"}

    comp = GoodComponent("good-comp")
    # Wait for completion of setup task to ensure no errors
    await asyncio.sleep(0.05)
    custom_val = await comp.res.custom_val.future()
    assert custom_val == "custom-id"


@pytest.mark.asyncio
async def test_dry_run_preview_safety_and_async_generators():
    # Setup mocks with preview=True
    m = MyMocks()
    m.mock_network_id = pulumi.UNKNOWN # During preview, vpc.id is unknown
    pulumi.runtime.set_mocks(
        m,
        project="my-project",
        stack="dev",
        preview=True,
    )

    class GeneratorComponent(Component):
        vpc: VPC
        subnet: Subnet

        @setup("vpc")
        async def prepare_vpc(self) -> VpcArgs:
            return {}

        @setup("subnet", depends_on=["vpc"])
        async def prepare_subnet(self) -> SubnetArgs:
            yield {"cidr": "10.0.1.0/24"}
            # This resolve() will throw UnknownValueException since vpc.id is UNKNOWN in preview
            vpc_id = await resolve(self.vpc.id)
            yield {"network_id": f"subnet-for-{vpc_id}"}

    comp = GeneratorComponent("gen-comp")

    # The yielded cidr should be successfully resolved
    cidr = await comp.subnet.cidr.future()
    assert cidr == "10.0.1.0/24"

    # The un-yielded network_id should be resolved to UNKNOWN
    network_id = await comp.subnet.network_id.future(with_unknowns=True)
    assert isinstance(network_id, Unknown)


@pytest.mark.asyncio
async def test_contextvar_task_safety(mocks):
    class ConcurrentComponent(Component):
        vpc: VPC
        subnet: Subnet

        @setup("vpc")
        async def prepare_vpc(self) -> VpcArgs:
            return {}

        @setup("subnet", depends_on=["vpc"])
        async def prepare_subnet(self) -> SubnetArgs:
            async def resolve_one(out):
                val = await resolve(out)
                return val

            # Run inside concurrent asyncio.gather block
            results = await asyncio.gather(
                resolve_one(self.vpc.id),
                asyncio.sleep(0.01)
            )
            return {
                "network_id": f"subnet-for-{results[0]}",
                "cidr": "10.0.1.0/24"
            }

    comp = ConcurrentComponent("concurrent-comp")
    
    # Wait for network_id to resolve
    await comp.subnet.network_id.future()

    # Get dependencies from output
    network_id_deps = await comp.subnet.network_id.resources()
    
    # Assert that self.vpc is in the dependencies set by comparing URNs
    dep_urns = {await r.urn.future() for r in network_id_deps}
    vpc_urn = await comp.vpc.urn.future()
    assert vpc_urn in dep_urns


@pytest.mark.asyncio
async def test_external_resource_dependency_tracking(mocks):
    existing_vpc = VPC("existing-vpc")

    class ComponentWithExternalRes(Component):
        subnet: Subnet

        def __init__(self, name: str, ext_vpc: VPC, opts = None):
            self.ext_vpc = ext_vpc
            super().__init__(name, opts=opts)

        @setup("subnet")
        async def prepare_subnet(self) -> SubnetArgs:
            vpc_id = await resolve(self.ext_vpc.id)
            return {
                "network_id": f"subnet-for-{vpc_id}",
                "cidr": "10.0.1.0/24"
            }

    comp = ComponentWithExternalRes("comp-ext", existing_vpc)
    
    # Wait for network_id to resolve
    await comp.subnet.network_id.future()

    # Verify that subnet depends on existing_vpc by comparing URNs
    subnet_deps = await comp.subnet.network_id.resources()
    dep_urns = {await r.urn.future() for r in subnet_deps}
    vpc_urn = await existing_vpc.urn.future()
    assert vpc_urn in dep_urns
