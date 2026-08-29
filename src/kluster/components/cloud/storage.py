"""The cloud site's storage: one block volume and one object bucket.

Two unrelated things live here because they are the same decision seen twice —
where data that outlives a node goes (docs/cluster/storage.md §2).

**The block volume** is the augmented node's extra disk (physical.md §1). It
backs the H@H cache: a slice of a globally distributed archive, so it is
neither re-derivable scratch nor part of any backup regime — its redundancy is
the network it came from (storage.md §3.3). That combination is exactly what
`protect=True` is for. The attachment carries the same protection, because
detaching the disk and deleting it cost the same thing: a node replacement
that silently proposes a detach is the failure this catches, and node
replacement is already an explicit, reviewed procedure (physical.md §2).

The volume is not given a device path. OCI's consistent-device-path feature is
gated on the image advertising support for it, which a Talos custom image does
not; the disk is selected by its properties in machine configuration instead.

**The chunk bucket** is the object storage behind the one workload the JuiceFS
quarantine admits (storage.md §6). It sits on OCI rather than on the backup
provider deliberately: the bucket backs a *replica* whose other full copy is
on the homelab NAS, so losing the tenancy loses nothing, and same-region
traffic between the node and Object Storage is free (storage.md §4). The
backup bucket — the one insuring against tenancy loss — is somewhere else on
purpose, and lives in `backup.py`.

A bucket needs a credential, and OCI's S3-compatible endpoint authenticates
with a **customer secret key**, which belongs to a user. The user declared
here exists for this bucket and nothing else: its group is granted `manage
objects` on this one bucket by name, so the credential's blast radius is the
bucket it was minted for. This is the same rule the backup keys follow
(storage.md §4, backup-integrity rules), spelled in OCI's policy language
instead of B2 capabilities.
"""

from __future__ import annotations

import pulumi
import pulumi_oci as oci

from putils import Component, async_output, resolve

#: SCIM schema URNs. A tenancy with identity domains keeps users, groups and
#: user credentials in the domain, and the legacy IAM endpoints for them are a
#: conversion shim that refuses often enough not to be relied on — so these
#: are the domains resources, addressed by the domain's own endpoint.
USER_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:User'
GROUP_SCHEMA = 'urn:ietf:params:scim:schemas:core:2.0:Group'
SECRET_KEY_SCHEMA = 'urn:ietf:params:scim:schemas:oracle:idcs:customerSecretKey'

#: The H@H cache. 50 GB is what pushes the tenancy's block footprint past the
#: free 200 GB (three boot volumes plus the state-backend appliance's), which
#: is accepted rather than designed around: folding the cache into a boot
#: volume would cost it the independent-volume boundary (nodes.md §3.2).
CACHE_VOLUME_GB = 50

#: Lower Cost (0 VPUs/GB) is the tier the storage budget is written against.
#: A read-through cache is not a database; the balanced tier's surcharge buys
#: latency nothing here needs.
CACHE_VOLUME_VPUS = 0

#: Names the program agrees on with itself. The bucket name is part of the
#: consumer's configuration, and the user and group exist only for it.
CHUNK_BUCKET = 'kluster-chunks'
CHUNK_IDENTITY = 'kluster-chunks'


class CacheVolume(Component):
    """The augmented node's block volume, attached to that node."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        availability_domain: pulumi.Input[str],
        instance_id: pulumi.Input[str],
        size_gb: int = CACHE_VOLUME_GB,
        vpus_per_gb: int = CACHE_VOLUME_VPUS,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.size_gb = size_gb

        self.volume = oci.core.Volume(
            f'{name}-cache',
            compartment_id=compartment_id,
            availability_domain=availability_domain,
            display_name=f'{name}-cache',
            size_in_gbs=str(size_gb),
            vpus_per_gb=str(vpus_per_gb),
            # Data-bearing and outside every backup regime (storage.md §3.3).
            opts=self.child_opts(protect=True),
        )

        self.attachment = oci.core.VolumeAttachment(
            f'{name}-cache-attach',
            # Paravirtualized rather than iSCSI: an iSCSI attachment needs a
            # login the node would have to perform, and Talos ships no agent
            # to perform it.
            attachment_type='paravirtualized',
            instance_id=instance_id,
            volume_id=self.volume.id,
            display_name=f'{name}-cache',
            opts=self.child_opts(protect=True),
        )

        self.register_outputs({})


class ChunkStore(Component):
    """The JuiceFS chunk bucket and the credential confined to it.

    Three tenancy-level facts arrive from outside because the resources here
    are not all in one place: `tenancy_id`, where the policy must hang to be
    able to grant on the compartment below it; `idcs_endpoint` and
    `domain_name`, the identity domain that owns users and their credentials
    and the name a policy statement calls it by.
    """

    def __init__(
        self,
        name: str,
        *,
        compartment_id: pulumi.Input[str],
        tenancy_id: pulumi.Input[str],
        region: str,
        idcs_endpoint: pulumi.Input[str],
        domain_name: str,
        user_email: str,
        bucket_name: str = CHUNK_BUCKET,
        identity_name: str = CHUNK_IDENTITY,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self.compartment_id = compartment_id
        self.bucket_name = bucket_name
        self.identity_name = identity_name
        self.domain_name = domain_name
        self.region = region

        #: The tenancy's Object Storage namespace: a system-generated string
        #: assigned at account creation, and half of every endpoint below.
        self.namespace: pulumi.Output[str] = async_output(self._namespace)

        self.bucket = oci.objectstorage.Bucket(
            f'{name}-chunks',
            compartment_id=compartment_id,
            namespace=self.namespace,
            name=bucket_name,
            access_type='NoPublicAccess',
            storage_tier='Standard',
            # Auto-tiering moves cold objects to Infrequent Access, which
            # carries a 31-day minimum retention and a retrieval fee — a trap
            # for a chunk store whose reads are cache misses (storage.md §4).
            auto_tiering='Disabled',
            # Chunks are content-addressed and never rewritten, so versioning
            # would keep copies of objects that cannot differ.
            versioning='Disabled',
            opts=self.child_opts(protect=True),
        )

        self.user = oci.identity.DomainsUser(
            f'{name}-chunks-user',
            idcs_endpoint=idcs_endpoint,
            schemas=[USER_SCHEMA],
            user_name=identity_name,
            display_name=identity_name,
            description='S3-compatible access to the JuiceFS chunk bucket',
            # A domain refuses a user without a primary email address.
            emails=[
                oci.identity.DomainsUserEmailArgs(value=user_email, type='work', primary=True),
                oci.identity.DomainsUserEmailArgs(value=user_email, type='recovery'),
            ],
            opts=self.child_opts(),
        )

        self.group = oci.identity.DomainsGroup(
            f'{name}-chunks-group',
            idcs_endpoint=idcs_endpoint,
            schemas=[GROUP_SCHEMA],
            display_name=identity_name,
            members=[oci.identity.DomainsGroupMemberArgs(type='User', value=self.user.id)],
            opts=self.child_opts(),
        )

        # Policies are IAM's own concept rather than the domain's, so this one
        # is the legacy resource even in a domains tenancy. It is attached to
        # the tenancy because a policy may only grant on the compartment it is
        # attached to or a descendant of it.
        self.policy = oci.identity.Policy(
            f'{name}-chunks-policy',
            compartment_id=tenancy_id,
            name=f'{identity_name}-access',
            description='The chunk bucket credential reaches the chunk bucket and nothing else',
            statements=[async_output(self._grant)],
            opts=self.child_opts(),
        )

        self.key = oci.identity.DomainsCustomerSecretKey(
            f'{name}-chunks-key',
            idcs_endpoint=idcs_endpoint,
            schemas=[SECRET_KEY_SCHEMA],
            display_name=identity_name,
            user=oci.identity.DomainsCustomerSecretKeyUserArgs(value=self.user.id, ocid=self.user.ocid),
            # The provider does not mark the secret half as secret, so state
            # would carry it in the clear without this.
            opts=self.child_opts(additional_secret_outputs=['secretKey']),
        )

        self.register_outputs({})

    @property
    def access_key_id(self) -> pulumi.Output[str]:
        """The S3 access key id half of the credential."""
        return self.key.access_key

    @property
    def secret_access_key(self) -> pulumi.Output[str]:
        """The S3 secret half of the credential."""
        return self.key.secret_key

    @property
    def endpoint(self) -> pulumi.Output[str]:
        """The S3-compatible endpoint, which is what a consumer configures.

        Object Storage answers on two hostnames: its native API and this
        S3-compatible one. Only the latter accepts a customer secret key.
        """
        return self.namespace.apply(lambda ns: f'https://{ns}.compat.objectstorage.{self.region}.oraclecloud.com')

    @property
    def native_endpoint(self) -> pulumi.Output[str]:
        """The native Object Storage endpoint, for callers that speak it."""
        return pulumi.Output.from_input(f'https://objectstorage.{self.region}.oraclecloud.com')

    async def _namespace(self) -> str:
        compartment_id = await resolve(self.compartment_id)
        found = await oci.objectstorage.get_namespace_output(compartment_id=compartment_id).future()
        assert found is not None
        return found.namespace

    async def _grant(self) -> str:
        compartment_id = await resolve(self.compartment_id)
        return statement(
            domain=self.domain_name,
            group=self.identity_name,
            compartment_id=compartment_id,
            bucket=self.bucket_name,
        )


def statement(*, domain: str, group: str, compartment_id: str, bucket: str) -> str:
    """The one policy statement the chunk credential runs on.

    `manage objects` covers reading, writing and deleting the objects inside
    the bucket; it does not cover the bucket itself, so the credential cannot
    delete the bucket out from under its own data. The `where` clause is what
    makes the grant bucket-scoped rather than compartment-wide.

    The compartment is named by OCID rather than by name. Policy syntax accepts
    either, and the name form is a path that has to be rewritten whenever a
    compartment moves — an OCID is the fact the rest of the program already
    passes around.
    """
    return (
        f"Allow group '{domain}'/'{group}' to manage objects "
        f"in compartment id {compartment_id} where target.bucket.name = '{bucket}'"
    )
