# Reaching into the mock monitor and its gRPC message types, neither of which
# carries type information. The unknown-type family is suppressed here rather
# than repo-wide, the same way `test_async_properties` does it.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownParameterType=false, reportUnknownArgumentType=false
# pyright: reportMissingParameterType=false
"""The cloud site's storage.

The properties here are the ones no later diff can show, because a diff shows
what changed and these are about what must never change: that the volume
holding an unbacked-up cache cannot be deleted or detached without an explicit
unprotect, that the chunk bucket is not quietly public or quietly tiered into
a minimum-retention class, and that the credential minted for that bucket
reaches that bucket only.
"""

from typing import Any, cast

import pulumi
import pulumi.runtime.mocks
import pytest
import pytest_asyncio
from google.protobuf import struct_pb2

#: How the wire says "this value is a secret". The mock monitor ignores the
#: `additionalSecretOutputs` a resource declares — the real engine is what
#: applies them — so a suite that did not teach it would be unable to tell a
#: classified credential from an unclassified one. Teaching the fake is the
#: allowed direction (docs/framework/testing.md §4).
SPECIAL_SIG_KEY = '4dabf18193072939515e22adb298388d'
SECRET_SIG = '1b47061264138c4ac30d75fd1eb44270'

_original_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def _register_resource_honouring_secrets(self, request):
    resp = _original_register_resource(self, request)
    for name in request.additionalSecretOutputs:
        if name not in resp.object.fields:
            continue
        wrapped = struct_pb2.Struct()
        wrapped.fields[SPECIAL_SIG_KEY].string_value = SECRET_SIG
        wrapped.fields['value'].CopyFrom(resp.object.fields[name])
        resp.object.fields[name].struct_value.CopyFrom(wrapped)
    return resp


pulumi.runtime.mocks.MockMonitor.RegisterResource = _register_resource_honouring_secrets

NAMESPACE = 'axmpletenancy'
REGION = 'us-phoenix-1'
IDCS_ENDPOINT = 'https://idcs-test.identity.oraclecloud.com'
TENANCY_ID = 'ocid1.tenancy.oc1..test'
COMPARTMENT_ID = 'ocid1.compartment.oc1..test'


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'oci:Identity/domainsUser:DomainsUser':
            outputs['ocid'] = 'ocid1.user.oc1..chunks'
        if args.typ == 'oci:Identity/domainsCustomerSecretKey:DomainsCustomerSecretKey':
            outputs['accessKey'] = 'access-key-id'
            outputs['secretKey'] = 'secret-access-key'
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == 'oci:ObjectStorage/getNamespace:getNamespace':
            return {'namespace': NAMESPACE}, []
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    pulumi.runtime.set_mocks(Mocks(), project='kluster', stack='physical', preview=False)


def cache_volume() -> Any:
    from kluster.physical.storage import CacheVolume

    return CacheVolume(
        'kluster',
        compartment_id=COMPARTMENT_ID,
        availability_domain='ZRbp:PHX-AD-1',
        instance_id='ocid1.instance.oc1.phx.augmented',
    )


def chunk_store() -> Any:
    from kluster.physical.storage import ChunkStore

    return ChunkStore(
        'kluster',
        compartment_id=COMPARTMENT_ID,
        tenancy_id=TENANCY_ID,
        region=REGION,
        idcs_endpoint=IDCS_ENDPOINT,
        domain_name='Default',
        user_email='cloud@example.invalid',
    )


@pytest.mark.asyncio
async def test_the_cache_and_its_attachment_both_need_an_unprotect() -> None:
    volume = cache_volume()
    # The data is outside every backup regime, so a destroy is unrecoverable;
    # a detach proposed by a node replacement is the same loss by another name.
    assert volume.volume._protect is True  # pyright: ignore[reportPrivateUsage]
    assert volume.attachment._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_cache_volume_is_sized_and_on_the_budgeted_tier() -> None:
    from kluster.physical.storage import CACHE_VOLUME_GB

    volume = cache_volume()
    assert await volume.volume.size_in_gbs.future() == str(CACHE_VOLUME_GB)
    # 0 VPUs/GB is Lower Cost, the tier the storage budget is written against.
    assert await volume.volume.vpus_per_gb.future() == '0'


@pytest.mark.asyncio
async def test_the_attachment_needs_no_agent_in_the_guest() -> None:
    volume = cache_volume()
    # An iSCSI attachment expects the guest to perform a login; Talos ships
    # nothing that would.
    assert await volume.attachment.attachment_type.future() == 'paravirtualized'
    assert await volume.attachment.volume_id.future() == await volume.volume.id.future()


@pytest.mark.asyncio
async def test_the_chunk_bucket_is_private_untiered_and_protected() -> None:
    store = chunk_store()
    assert await store.bucket.access_type.future() == 'NoPublicAccess'
    # Infrequent Access carries a 31-day minimum retention and a retrieval
    # fee, which is the wrong trade for objects read on cache misses.
    assert await store.bucket.auto_tiering.future() == 'Disabled'
    assert await store.bucket.storage_tier.future() == 'Standard'
    assert store.bucket._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_the_bucket_lands_in_the_tenancy_namespace() -> None:
    store = chunk_store()
    assert await store.bucket.namespace.future() == NAMESPACE


@pytest.mark.asyncio
async def test_the_endpoint_is_the_s3_compatible_one() -> None:
    store = chunk_store()
    # A customer secret key authenticates against this host and not against
    # the native Object Storage API.
    assert await store.endpoint.future() == f'https://{NAMESPACE}.compat.objectstorage.{REGION}.oraclecloud.com'
    assert await store.native_endpoint.future() == f'https://objectstorage.{REGION}.oraclecloud.com'


@pytest.mark.asyncio
async def test_the_credential_reaches_its_own_bucket_and_no_other() -> None:
    from kluster.physical.storage import CHUNK_BUCKET

    store = chunk_store()
    statements = await store.policy.statements.future()
    assert statements is not None
    assert len(statements) == 1
    granted = statements[0]

    # Bucket-scoped, not compartment-wide: the `where` clause is the whole
    # difference between this key and one that can empty the backup bucket.
    assert f"target.bucket.name = '{CHUNK_BUCKET}'" in granted
    assert 'manage objects' in granted
    # `manage buckets` would let the credential delete the bucket its own data
    # lives in.
    assert 'manage buckets' not in granted
    assert f"'Default'/'{CHUNK_BUCKET}'" in granted
    # By OCID: a compartment name is a path that goes stale when a compartment
    # moves, and silently grants somewhere else if the path is reused.
    assert f'in compartment id {COMPARTMENT_ID} ' in granted


@pytest.mark.asyncio
async def test_the_policy_is_attached_where_it_can_grant() -> None:
    store = chunk_store()
    # A policy may only grant on the compartment it hangs off or a descendant,
    # so a policy about the cluster compartment lives above it.
    assert await store.policy.compartment_id.future() == TENANCY_ID


@pytest.mark.asyncio
async def test_the_key_belongs_to_the_bucket_user() -> None:
    store = chunk_store()
    user = await store.key.user.future()
    assert user is not None
    assert user.value == await store.user.id.future()
    assert await store.access_key_id.future() == 'access-key-id'


@pytest.mark.asyncio
async def test_the_secret_half_is_marked_secret() -> None:
    store = chunk_store()
    # The provider does not classify it, so state would carry the secret in
    # the clear if the component did not say so itself.
    assert await store.secret_access_key.is_secret() is True
    assert await store.access_key_id.is_secret() is False


@pytest.mark.asyncio
async def test_the_user_carries_a_primary_email() -> None:
    store = chunk_store()
    emails = await store.user.emails.future()
    assert emails is not None
    # A domain refuses a user without one, and the refusal arrives at create
    # time with a message about SCIM rather than about email.
    assert any(email.primary for email in emails)
