"""The state-backend appliance, declared: every entity it is, in one stack (rfc-006 §4).

The appliance is the Postgres box every other stack keeps its state in
(physical/state-backend.md). Everything it stands on is declared here, so a
difference anywhere is a step the engine plans and repairs in place, and a
run that finds none writes nothing:

-   **The network**: its own VCN, internet gateway, route table and subnet,
    the VCN and the subnet protected; and one security list the program owns,
    which the subnet carries alone. The list admits nothing but `RULES`, each
    rule an opening that says why it is there, and the box's interface takes
    no public address of its own and no network security group.
-   **The image**: the pinned Fedora CoreOS release's `oraclecloud` artifact,
    uploaded to the image bucket (`kluster.providers.oci_objects`) and
    imported from there.
-   **The box**, whose options are each one of the traps of rfc-006 §4.2
    closed: `metadata` carries the Ignition and is secret, and a change to it
    replaces the box, old one first; the image the box boots from is
    ignored once launched, since Zincati keeps the running box current; and
    the hooks around a replacement (`hooks`). `extendedMetadata` carries the
    bill of materials in the clear, so a planned replacement names what
    moved.
-   **The reserved address**, protected and held to `settings.ADDRESS`,
    pointed at the box's primary private address, so re-pointing it at a new
    box is an ordinary update.
-   **Readiness**: the reserved address answering TLS as itself under the
    CA (`kluster.providers.postgres_tls`), replaced with the box, so its
    `after_create` hook restores into each new one.
-   **The dump bucket**, protected, its retention converged in place, and
    **the dump key** the box uploads with, named for the generation its
    caller passes: a new generation replaces the key, and so the box, and the
    old key goes at the end of that deployment. The key holds the
    capabilities `DUMP_KEY_CAPABILITIES` names, each beside its reason, and
    no other.

**The box's options, the readiness resource's, and each secret output
carry their reasons where they are declared.** A posture that must not loosen — the list's openings, the
interface, the dump key's grant — is written closed, with every exception
named beside why it is needed, so a loosening is a line in the diff with its
reason on it.

**No resource here is imported.** Every one is the program's own, created by it
and kept in the committed state. A lost state brings the resources back
through the program and not through `pulumi import` (physical/state-backend.md
§7.5, "Losing it"; framework/pulumi.md §3.3).

**Nothing here reads stack configuration.** The providers are the stack
program's, set on this component's options and inherited by every native
resource beneath it; the keys the box is rendered from arrive as `Keys`; the
`operator` client bundle the hooks connect with arrives as a directory. The
files committed beside the Butane template are the machine's and are read
here (`kluster.lib.state_backend.committed`).

**The component refuses before it declares anything** where its inputs are
out of step: a host key whose public half is not the committed one, a server
key that does not open its certificate, or a committed file absent. Each is
what a failed `credentials derived` run can leave, and each would boot a box
nobody can reach or whose dumps nobody can open, so nothing is planned.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pulumi
import pulumi_b2 as b2
import pulumi_oci as oci
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from kluster import conventions
from kluster.components.state_backend.hooks import Replacement
from kluster.lib.state_backend import committed, render, settings
from kluster.providers.oci_objects import ArtifactObject
from kluster.providers.postgres_tls import Readiness
from putils import Component, async_output, background, resolve

__all__ = (
    'ANYWHERE',
    'DUMP_KEY_CAPABILITIES',
    'RULES',
    'SERVER_ROW',
    'Keys',
    'Rule',
    'StateBackend',
    'certificate_expiry',
    'refuse_out_of_step',
)

#: The one address range the appliance's routes and rules name: everything.
ANYWHERE = '0.0.0.0/0'


@dataclass(frozen=True, kw_only=True)
class Rule:
    """One opening in the security list: what it admits in, or lets out, and why.

    `ports` is a TCP destination range; `icmp` an ICMP type and, where the
    rule names one, its code. Every rule is stateful, so the replies to what
    it admits are admitted too.

    `reason` is required: a rule is an exception to a list that admits
    nothing, and it says what needs it. It is kept out of the repr, which
    orders the declared list (`_sorted`), so rewording a reason moves no
    rule.
    """

    ingress: bool
    #: OCI's protocol number as a string, or `all`.
    protocol: str
    #: The source of an ingress rule, the destination of an egress one.
    peer: str
    ports: tuple[int, int] | None = None
    icmp: tuple[int, int | None] | None = None
    #: What needs this opening.
    reason: str = field(repr=False, compare=False)


#: Every opening the subnet's security list holds. The list admits nothing
#: else, so a rule added here widens what reaches the box, and its reason is
#: the line a review reads (physical/state-backend.md §4).
RULES = frozenset(
    {
        Rule(
            ingress=True,
            protocol='6',
            peer=ANYWHERE,
            ports=(settings.PORT, settings.PORT),
            reason='Postgres, from anywhere: its clients include CI runners, which have no address range a rule '
            'could name, and the client certificate it demands is the wall',
        ),
        Rule(
            ingress=True,
            protocol='6',
            peer=ANYWHERE,
            ports=(22, 22),
            reason='SSH, key authentication only, from wherever the operator is, for `state-backend ssh`',
        ),
        Rule(
            ingress=True,
            protocol='1',
            peer=ANYWHERE,
            icmp=(3, 4),
            reason='"fragmentation needed" from anywhere, which path MTU discovery needs: without it a '
            'connection over a path with a smaller MTU stalls',
        ),
        Rule(
            ingress=True,
            protocol='1',
            peer=settings.VCN_CIDR,
            icmp=(3, None),
            reason='every "destination unreachable" from inside the VCN, which the VCN\'s default list carries',
        ),
        Rule(
            ingress=False,
            protocol='all',
            peer=ANYWHERE,
            reason='all egress: the box pulls its Postgres image and its OS updates and uploads its dumps to B2, '
            'and none of those has a fixed address range',
        ),
    }
)


def _sorted(rules: frozenset[Rule]) -> list[Rule]:
    """The rules in one order, so the list's declared inputs are the same on every run."""
    return sorted(rules, key=repr)


#: Every capability the dump key holds, each beside its reason. B2 grants a key
#: nothing it does not name, and this key is in the box's Ignition, so what it
#: holds is what a stolen box or a leaked Ignition holds.
DUMP_KEY_CAPABILITIES: Mapping[str, str] = {
    'writeFiles': 'the box uploads its dump and reads nothing back, since the uploader keeps no index, so the '
    'key needs to neither list nor read (physical/state-backend.md §5)',
}


def _ingress(rule: Rule) -> oci.core.SecurityListIngressSecurityRuleArgs:
    return oci.core.SecurityListIngressSecurityRuleArgs(
        protocol=rule.protocol,
        source=rule.peer,
        source_type='CIDR_BLOCK',
        stateless=False,
        tcp_options=None
        if rule.ports is None
        else oci.core.SecurityListIngressSecurityRuleTcpOptionsArgs(min=rule.ports[0], max=rule.ports[1]),
        icmp_options=None
        if rule.icmp is None
        else oci.core.SecurityListIngressSecurityRuleIcmpOptionsArgs(type=rule.icmp[0], code=rule.icmp[1]),
    )


def _egress(rule: Rule) -> oci.core.SecurityListEgressSecurityRuleArgs:
    return oci.core.SecurityListEgressSecurityRuleArgs(
        protocol=rule.protocol, destination=rule.peer, destination_type='CIDR_BLOCK', stateless=False
    )


@dataclass(frozen=True)
class Keys:
    """The stable keys the box is rendered from (rfc-006 §5), as the stack's configuration holds them.

    The certificates are PEM and public. The server key and the host key are
    secrets, kept out of the repr and out of comparison; the component passes
    them on only inside the instance's secret `metadata`.
    """

    ca_certificate: str
    server_certificate: str
    server_key: str = field(repr=False, compare=False)
    #: OpenSSH's own private-key format.
    ssh_host_key: str = field(repr=False, compare=False)


#: The `credentials derived` row that issues the server key and certificate.
SERVER_ROW = f'{conventions.STATE_BACKEND}-server'


def certificate_expiry(keys: Keys) -> str:
    """When the configured server certificate stops being valid, as an ISO 8601 instant.

    What the stack exports as `settings.CERTIFICATE_EXPIRY_OUTPUT`, for the
    driver to hold against the renewal margin (rfc-006 §7).
    """
    return x509.load_pem_x509_certificate(keys.server_certificate.encode()).not_valid_after_utc.isoformat()


def refuse_out_of_step(keys: Keys) -> None:
    """Refuse keys a failed `credentials derived` run left out of step with each other.

    The host key's public half is the committed line `state-backend ssh` pins,
    and the server key is the one its certificate carries. A key that is not
    is refused naming the command that writes both halves again.
    """
    public = committed.public_host_key(keys.ssh_host_key)
    if public != committed.host_key():
        raise committed.Refused(
            f'the configured host key is not the one {committed.HOST_KEY} names: '
            f'`credentials derived {committed.HOST_KEY_ROW} generate` writes both again'
        )
    certificate = x509.load_pem_x509_certificate(keys.server_certificate.encode())
    key = serialization.load_pem_private_key(keys.server_key.encode(), password=None)
    der, spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(der, spki) != certificate.public_key().public_bytes(der, spki):
        raise committed.Refused(
            'the configured server key does not open the configured server certificate: '
            f'`credentials derived {SERVER_ROW} issue` issues both again'
        )


class StateBackend(Component, pulumi_type='kluster:state_backend:StateBackend'):
    """The appliance, whole: its network, image, box, address, readiness, dump bucket and dump key."""

    def __init__(
        self,
        name: str,
        *,
        compartment_id: str,
        tenancy_id: str,
        region: str,
        keys: Keys,
        dump_key_generation: int,
        bundle_dir: Path,
        dump_directory: Path,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the appliance.

        `keys` are the stable keys of rfc-006 §5, `dump_key_generation` the
        dump key's generation, `bundle_dir` the `operator` client bundle the
        hooks connect with and `dump_directory` where a replacement's dump is
        written. The OCI and B2 providers come in on `opts`.
        """
        refuse_out_of_step(keys)
        recipients = committed.age_recipients()
        super().__init__(name, opts=opts)
        self.compartment_id = compartment_id
        self.keys = keys
        self.certificate_expiry = certificate_expiry(keys)
        self.replacement = Replacement(bundle_dir=bundle_dir, recipients=recipients, dump_directory=dump_directory)

        self.vcn = oci.core.Vcn(
            f'{name}-vcn',
            compartment_id=compartment_id,
            cidr_blocks=[settings.VCN_CIDR],
            display_name=f'{settings.NAME}-vcn',
            dns_label='statebackend',
            opts=self.child_opts(protect=True),
        )
        self.gateway = oci.core.InternetGateway(
            f'{name}-igw',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            enabled=True,
            display_name=f'{settings.NAME}-igw',
            opts=self.child_opts(),
        )
        self.routes = oci.core.RouteTable(
            f'{name}-routes',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            display_name=f'{settings.NAME}-routes',
            route_rules=[
                oci.core.RouteTableRouteRuleArgs(
                    network_entity_id=self.gateway.id, destination=ANYWHERE, destination_type='CIDR_BLOCK'
                )
            ],
            opts=self.child_opts(),
        )
        self.rules = oci.core.SecurityList(
            f'{name}-rules',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            display_name=f'{settings.NAME}-rules',
            ingress_security_rules=[_ingress(rule) for rule in _sorted(RULES) if rule.ingress],
            egress_security_rules=[_egress(rule) for rule in _sorted(RULES) if not rule.ingress],
            opts=self.child_opts(),
        )
        self.subnet = oci.core.Subnet(
            f'{name}-subnet',
            compartment_id=compartment_id,
            vcn_id=self.vcn.id,
            cidr_block=settings.SUBNET_CIDR,
            display_name=f'{settings.NAME}-subnet',
            dns_label='sb',
            prohibit_public_ip_on_vnic=False,
            route_table_id=self.routes.id,
            security_list_ids=[self.rules.id],
            opts=self.child_opts(protect=True),
        )

        self.image_bucket = oci.objectstorage.Bucket(
            f'{name}-images',
            compartment_id=compartment_id,
            name=settings.IMAGE_BUCKET,
            namespace=async_output(self._namespace),
            access_type='NoPublicAccess',
            # `createdBy` is the OCID of the user that made the bucket, which
            # the provider does not mark: an output that identifies a
            # principal is secret in a stack whose state is committed
            # (rfc-006 §3.3).
            opts=self.child_opts(protect=True, additional_secret_outputs=['createdBy']),
        )
        self.artifact = ArtifactObject(
            f'{name}-fcos',
            url=settings.FCOS_ARTIFACT_URL,
            sha256=settings.FCOS_ARTIFACT_SHA256,
            region=region,
            tenancy=tenancy_id,
            namespace=self.image_bucket.namespace,
            bucket=self.image_bucket.name,
            # The digest in the name, so a corrected digest for one release
            # is a new object rather than the same one uploaded over.
            object_name=f'fedora-coreos-{settings.FCOS_RELEASE}-{settings.FCOS_ARTIFACT_SHA256[:12]}.qcow2',
            opts=self.child_opts(),
        )
        self.image = oci.core.Image(
            f'{name}-image',
            compartment_id=compartment_id,
            display_name=f'{settings.NAME}-fcos-{settings.FCOS_RELEASE}',
            launch_mode='PARAVIRTUALIZED',
            image_source_details=oci.core.ImageImageSourceDetailsArgs(
                source_type='objectStorageTuple',
                namespace_name=self.artifact.namespace,
                bucket_name=self.artifact.bucket,
                object_name=self.artifact.object_name,
                source_image_type='QCOW2',
            ),
            opts=self.child_opts(),
        )

        self.dump_bucket = b2.Bucket(
            f'{name}-dumps',
            bucket_name=settings.B2_BUCKET,
            bucket_type='allPrivate',
            lifecycle_rules=[
                b2.BucketLifecycleRuleArgs(
                    file_name_prefix=f'{settings.B2_PREFIX}/',
                    days_from_uploading_to_hiding=settings.B2_RETENTION_DAYS,
                    days_from_hiding_to_deleting=1,
                )
            ],
            # A bucket's name is unique across all of B2, which refuses a
            # second bucket under it, so a replacement created before the
            # delete would fail.
            opts=self.child_opts(protect=True, delete_before_replace=True),
        )
        # No `additional_secret_outputs`: the SDK marks the key secret, and its
        # id is the resource's id, which the engine cannot mark, and an
        # identifier rather than a credential (rfc-006 §3.3).
        self.dump_key = b2.ApplicationKey(
            f'{name}-dump-key',
            key_name=f'{settings.B2_DUMP_KEY_NAME}-{dump_key_generation}',
            capabilities=list(DUMP_KEY_CAPABILITIES),
            # Confined to the dump bucket, and within it to the prefix the
            # retention rule reads, so whatever it writes is pruned.
            bucket_ids=[self.dump_bucket.bucket_id],
            name_prefix=f'{settings.B2_PREFIX}/',
            opts=self.child_opts(),
        )

        self.instance = oci.core.Instance(
            f'{name}-vm',
            compartment_id=compartment_id,
            availability_domain=async_output(self._availability_domain),
            shape=settings.SHAPE,
            display_name=f'{settings.NAME}-vm',
            source_details=oci.core.InstanceSourceDetailsArgs(
                source_type='image',
                source_id=self.image.id,
                boot_volume_size_in_gbs=str(settings.BOOT_VOLUME_GB),
            ),
            create_vnic_details=oci.core.InstanceCreateVnicDetailsArgs(
                subnet_id=self.subnet.id,
                # No public address at launch: a private address carries one
                # public address, and the box's is the reserved one below, the
                # address the certificate and every client bundle name. An
                # ephemeral one would take its place and change with every
                # replacement.
                assign_public_ip='false',
                # In no network security group, so the subnet's list is all
                # that admits anything to the box; declared empty, a group put
                # on the interface by hand is a difference the next run
                # removes in place.
                nsg_ids=[],
                display_name=f'{settings.NAME}-vnic',
            ),
            # Secret as an input, so the output of the same name is too, and
            # `user_data` its only entry: a second entry would be a value of
            # its own in the state's secrets, which the driver then searches
            # the checkpoint for in the clear (framework/pulumi.md §3.3).
            metadata=pulumi.Output.secret(async_output(self._metadata)),
            extended_metadata=async_output(self._bill_of_materials),
            # Legacy IMDS serves the machine configuration without authentication.
            instance_options=oci.core.InstanceInstanceOptionsArgs(are_legacy_imds_endpoints_disabled=True),
            preserve_boot_volume=False,
            opts=self.child_opts(
                # A changed `user_data` replaces the box -- the provider forces
                # a new instance for it, and `replace_on_changes` holds that
                # whatever the provider does -- and Ignition reads it only at
                # first boot: a changed Ignition is a new box, which passes
                # through the permission and the dump hook like any
                # replacement (rfc-006 §4.2).
                replace_on_changes=['metadata'],
                # The old box goes first. The dump is taken before the delete
                # and the restore after the new box answers, so created first,
                # the new box would be restored before the old one was dumped;
                # deleted first, the old box is dumped through the reserved
                # address while it still points there (rfc-006 §4.3).
                delete_before_replace=True,
                # The provider updates the image in place by replacing the
                # running box's boot volume, data directory and all, with no
                # dump hook in the way. Zincati keeps the running box current,
                # so the image matters only at the next launch (rfc-006 §4.2).
                ignore_changes=['sourceDetails.sourceId'],
                # Secret as an output whatever the provider returns: the engine
                # wraps a named output whole, and this one is the Ignition,
                # which holds the server key and the host key.
                additional_secret_outputs=['metadata'],
                hooks=pulumi.ResourceHookBinding(
                    before_create=[pulumi.ResourceHook(f'{name}-permit', self.replacement.permit)],
                    before_delete=[pulumi.ResourceHook(f'{name}-dump', self.replacement.dump)],
                ),
            ),
        )
        self.address = oci.core.PublicIp(
            f'{name}-ip',
            compartment_id=compartment_id,
            lifetime='RESERVED',
            display_name=f'{settings.NAME}-ip',
            private_ip_id=async_output(self._primary_private_ip),
            opts=self.child_opts(protect=True),
        )
        self.readiness = Readiness(
            f'{name}-ready',
            address=async_output(self._held_address),
            port=settings.PORT,
            ca_certificate=keys.ca_certificate,
            instance_id=self.instance.id,
            opts=self.child_opts(
                # A new box replaces the resource rather than updating it: the
                # dynamic provider has no `diff`, so the engine would update
                # it, and an update runs no `after_create`, so the restore
                # below would never run (rfc-006 §4.3).
                replace_on_changes=['instance_id'],
                hooks=pulumi.ResourceHookBinding(
                    after_create=[pulumi.ResourceHook(f'{name}-restore', self.replacement.restore)]
                ),
            ),
        )

        self.register_outputs({})

    def _machine(self, *, dump_key_id: str, dump_key: str, bucket_id: str) -> render.Machine:
        return render.machine(
            ca_cert=self.keys.ca_certificate,
            server_cert=self.keys.server_certificate,
            server_key=self.keys.server_key,
            ssh_host_key=self.keys.ssh_host_key,
            age_recipients=self.replacement.recipients,
            dump_key_id=dump_key_id,
            dump_key=dump_key,
            bucket_id=bucket_id,
        )

    async def _metadata(self) -> dict[str, str]:
        dump_key_id, dump_key, bucket_id = await resolve(
            self.dump_key.application_key_id, self.dump_key.application_key, self.dump_bucket.bucket_id
        )
        machine = self._machine(dump_key_id=dump_key_id, dump_key=dump_key, bucket_id=bucket_id)
        ignition = await background(render.render_ignition)(machine)
        return {'user_data': base64.b64encode(ignition.encode()).decode()}

    async def _bill_of_materials(self) -> dict[str, str]:
        # The dump key's secret is not resolved: the map reads it through its
        # id, and resolving a secret here would mark the whole map secret.
        dump_key_id, bucket_id = await resolve(self.dump_key.application_key_id, self.dump_bucket.bucket_id)
        return render.bill_of_materials(self._machine(dump_key_id=dump_key_id, dump_key='', bucket_id=bucket_id))

    async def _namespace(self) -> str:
        found = await resolve(
            oci.objectstorage.get_namespace_output(
                compartment_id=self.compartment_id, opts=pulumi.InvokeOptions(parent=self)
            )
        )
        return found.namespace

    async def _availability_domain(self) -> str:
        """The availability domain that offers the shape: it is offered in one of the region's, not in all."""
        domains = await resolve(
            oci.identity.get_availability_domains_output(
                compartment_id=self.compartment_id, opts=pulumi.InvokeOptions(parent=self)
            )
        )
        names = [domain.name for domain in domains.availability_domains]
        for domain in names:
            offered = await resolve(
                oci.core.get_shapes_output(
                    compartment_id=self.compartment_id,
                    availability_domain=domain,
                    opts=pulumi.InvokeOptions(parent=self),
                )
            )
            if settings.SHAPE in {shape.name for shape in offered.shapes}:
                return domain
        raise ValueError(f'{settings.SHAPE} is offered in none of {", ".join(names)}')

    async def _primary_private_ip(self) -> str:
        """The box's primary private address, which the reserved address points at."""
        instance_id = await resolve(self.instance.id)
        attachments = await resolve(
            oci.core.get_vnic_attachments_output(
                compartment_id=self.compartment_id, instance_id=instance_id, opts=pulumi.InvokeOptions(parent=self)
            )
        )
        attached = {it.vnic_id for it in attachments.vnic_attachments if it.state == 'ATTACHED'}
        if len(attached) != 1:
            raise ValueError(
                f'{instance_id} has {len(attached)} attached network interfaces, and the launch gives it one: '
                'a box OCI is still provisioning has none yet, and the next run finds it attached'
            )
        (vnic_id,) = attached
        addresses = await resolve(
            oci.core.get_private_ips_output(vnic_id=vnic_id, opts=pulumi.InvokeOptions(parent=self))
        )
        (primary,) = [it.id for it in addresses.private_ips if it.is_primary]
        return primary

    async def _held_address(self) -> str:
        """The reserved address, refused unless it is `settings.ADDRESS`.

        The address every client bundle names and the certificate carries is
        recorded, not looked up, so a reservation at another address is a
        decision the repository has to record, not one to follow.
        """
        address = await resolve(self.address.ip_address)
        if address != settings.ADDRESS:
            raise ValueError(
                f'the reserved address is {address}, and the appliance is recorded at {settings.ADDRESS} '
                '(kluster.lib.state_backend.settings.ADDRESS)'
            )
        return address
