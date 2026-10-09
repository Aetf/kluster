"""The `state-backend` program, its component, its hooks and its two dynamic resources (rfc-006 §4).

Under mocks, one run of the whole program is read back for what it declares:
the network and its one security list, the options of rfc-006 §4.2 on the
instance, the protected resources, what is secret, the dump key's grant and
where the hooks are bound. The hooks are then exercised as functions, over
`kluster.lib.state_backend.state` with its tools replaced, and the two dynamic
resources' refusals over a fake stream and a local TLS server. What the engine
does with the options and hooks is `tests/test_state_backend_engine.py`.

The files committed beside the Butane template are written into a directory
of the run's own and pointed at, since the checkout's may not hold them yet.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import importlib
import inspect
import ipaddress
import json
import lzma
import socket
import ssl
import threading
from collections.abc import AsyncGenerator, Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pulumi
import pulumi_b2 as b2
import pulumi_oci as oci
import pytest
import pytest_asyncio
from credentials_command_tree import named_commands
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mock_monitor import Recorder, declaring, run_under_backstop

from kluster import conventions
from kluster.components import state_backend as component
from kluster.components.state_backend import hooks
from kluster.lib import stack_environment
from kluster.lib import workstation as lib_workstation
from kluster.lib.state_backend import adoption, committed, permission, render, settings, state
from kluster.providers import oci_objects, postgres_tls
from kluster.scripts.credentials import derived, escrow, pki
from kluster.stacks import state_backend as program

NAME = conventions.STATE_BACKEND

#: How a secret arrives on the wire: Pulumi's special-signature key, carrying
#: the signature that means "secret", beside the value itself.
SECRET_SIG = '4dabf18193072939515e22adb298388d'
SECRET_MARK = '1b47061264138c4ac30d75fd1eb44270'

#: Stand-ins that open nothing and say so if they reach a diff.
OCI_USER = 'ocid1.user.oc1..a-fake-appliance-user'
OCI_FINGERPRINT = 'aa:bb:cc:a-fake-fingerprint'
OCI_KEY = 'a-fake-oci-private-key-that-signs-nothing'
B2_KEY_ID = 'a-fake-b2-management-key-id'
B2_KEY = 'a-fake-b2-management-key'
DUMP_KEY_ID = 'a-fake-dump-key-id-0000001'
DUMP_KEY = 'a-fake-dump-key-that-writes-nothing'
DUMP_BUCKET_ID = 'a-fake-dump-bucket-id'

#: Recipients in the form `age-keygen` draws, which is all a program reader
#: holds one to.
CURRENT = 'age1currentcurrentcurrentcurrentcurrentcurrentcurrentcurrentzz'
DRILL = 'age1drilldrilldrilldrilldrilldrilldrilldrilldrilldrilldrillzzzz'

#: The availability domain that offers the shape, among two that are listed.
OFFERING = 'Uocm:PHX-AD-2'

INSTANCE = 'oci:Core/instance:Instance'
SECURITY_LIST = 'oci:Core/securityList:SecurityList'
SUBNET = 'oci:Core/subnet:Subnet'
PUBLIC_IP = 'oci:Core/publicIp:PublicIp'
IMAGE_BUCKET = 'oci:ObjectStorage/bucket:Bucket'
DUMP_BUCKET = 'b2:index/bucket:Bucket'
DUMP_KEY_TYPE = 'b2:index/applicationKey:ApplicationKey'
READINESS = 'pulumi-python:dynamic/postgres_tls:Readiness'


def _host_key() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
    )


@dataclasses.dataclass(frozen=True)
class Machine:
    """The keys the box is rendered from, and the committed files beside them."""

    keys: component.Keys
    authority: pki.Authority
    directory: Path


def _machine(directory: Path) -> Machine:
    authority = pki.Authority.from_pem(pki.generate_ca_key())
    server = authority.issue_server(settings.ADDRESS)
    keys = component.Keys(
        ca_certificate=authority.certificate().cert_pem.decode(),
        server_certificate=server.cert_pem.decode(),
        server_key=server.key_pem.decode(),
        ssh_host_key=_host_key(),
    )
    directory.mkdir(parents=True, exist_ok=True)
    _ = (directory / committed.HOST_KEY.name).write_text(f'{committed.public_host_key(keys.ssh_host_key)}\n')
    (current, *_) = committed.backup_window()
    _ = (directory / committed.BACKUP_RECIPIENTS.name).write_text(f'# comment\n{current} {CURRENT}\n')
    _ = (directory / committed.DRILL_RECIPIENT.name).write_text(f'{DRILL}\n')
    return Machine(keys=keys, authority=authority, directory=directory)


@contextmanager
def _files_in(monkeypatch: pytest.MonkeyPatch, directory: Path) -> Generator[None]:
    for attribute in ('HOST_KEY', 'BACKUP_RECIPIENTS', 'DRILL_RECIPIENT'):
        path: Path = getattr(committed, attribute)
        monkeypatch.setattr(committed, attribute, directory / path.name)
    yield


def _config(keys: component.Keys) -> dict[str, str]:
    return {
        f'kluster:{program.OCI_USER_OCID}': OCI_USER,
        f'kluster:{program.OCI_FINGERPRINT}': OCI_FINGERPRINT,
        f'kluster:{program.OCI_PRIVATE_KEY}': OCI_KEY,
        f'kluster:{program.B2_KEY_ID}': B2_KEY_ID,
        f'kluster:{program.B2_KEY}': B2_KEY,
        f'kluster:{program.CA_CERTIFICATE}': keys.ca_certificate,
        f'kluster:{program.SERVER_CERTIFICATE}': keys.server_certificate,
        f'kluster:{program.SERVER_KEY}': keys.server_key,
        f'kluster:{program.SSH_HOST_KEY}': keys.ssh_host_key,
    }


class Appliance(Recorder):
    """What OCI and B2 answer that the program's inputs do not carry."""

    def __init__(self, *, address: str = settings.ADDRESS) -> None:
        super().__init__()
        self.address = address
        #: Every hook the run registered, by name: the callbacks the engine
        #: would call.
        self.hooks: dict[str, pulumi.ResourceHook] = {}

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        match args.typ:
            case 'oci:Core/publicIp:PublicIp':
                return {'ipAddress': self.address}
            case 'b2:index/applicationKey:ApplicationKey':
                return {'applicationKeyId': DUMP_KEY_ID, 'applicationKey': DUMP_KEY}
            case 'b2:index/bucket:Bucket':
                return {'bucketId': DUMP_BUCKET_ID}
            case _:
                return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        match args.token:
            case 'oci:ObjectStorage/getNamespace:getNamespace':
                return {'namespace': 'a-namespace'}
            case 'oci:Identity/getAvailabilityDomains:getAvailabilityDomains':
                return {'availabilityDomains': [{'name': 'Uocm:PHX-AD-1'}, {'name': OFFERING}]}
            case 'oci:Core/getShapes:getShapes':
                offered = cast('dict[str, Any]', args.args).get('availabilityDomain') == OFFERING
                return {'shapes': [{'name': settings.SHAPE if offered else 'VM.Standard.E4.Flex'}]}
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': 'a-vnic', 'state': 'ATTACHED'}]}
            case 'oci:Core/getPrivateIps:getPrivateIps':
                return {'privateIps': [{'id': 'secondary', 'isPrimary': False}, {'id': 'primary', 'isPrimary': True}]}
            case _:
                return {}


class _Callbacks:
    """The callback server a hook registers with, which the mock monitor has none of. Keeps each hook by name."""

    def __init__(self, hooks: dict[str, pulumi.ResourceHook]) -> None:
        self.hooks = hooks

    def register_resource_hook(self, hook: pulumi.ResourceHook) -> None:
        self.hooks[hook.name] = hook


@contextmanager
def _hooks_accepted(monitor: Appliance) -> Generator[_Callbacks]:
    callbacks = _Callbacks(monitor.hooks)
    # A property the SDK declares through a decorator the checker cannot read.
    settings_ = pulumi.runtime.settings.SETTINGS
    before: object = settings_.callbacks  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
    settings_.callbacks = callbacks  # pyright: ignore[reportAttributeAccessIssue]
    try:
        yield callbacks
    finally:
        settings_.callbacks = before  # pyright: ignore[reportAttributeAccessIssue]


async def _run(
    monitor: Appliance, machine: Machine, *, adopted: dict[str, str] | None = None, generation: int | None = None
) -> Appliance:
    config = _config(machine.keys)
    if adopted is not None:
        config[f'kluster:{adoption.KEY}'] = json.dumps(adopted)
    if generation is not None:
        config[f'kluster:{program.DUMP_KEY_GENERATION}'] = str(generation)
    pulumi.runtime.set_all_config(config)
    _ = await run_under_backstop(monitor, stack=NAME)
    with _hooks_accepted(monitor):
        async with declaring():
            await program.main()
    return monitor


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def run(tmp_path_factory: pytest.TempPathFactory) -> AsyncGenerator[Appliance]:
    """The whole program, declared once: every case below reads the same run."""
    machine = _machine(tmp_path_factory.mktemp('machine'))
    with pytest.MonkeyPatch.context() as monkeypatch, _files_in(monkeypatch, machine.directory):
        yield await _run(Appliance(), machine)


def _options(run: Appliance, typ: str) -> Any:
    (request,) = [request for request in run.requested if request.type == typ]
    return request


def _one(run: Appliance, typ: str) -> dict[str, Any]:
    (declared,) = run.of_type(typ)
    return declared.inputs


def _secret(value: object) -> bool:
    return isinstance(value, dict) and cast('dict[str, object]', value).get(SECRET_SIG) == SECRET_MARK


# --------------------------------------------------------------------------
# The network.
# --------------------------------------------------------------------------

#: The security list's rules as the provider takes them: 5432 and 22 from
#: anywhere, the ICMP rules a VCN's default list carries, and all egress, every
#: one stateful (rfc-006 §4.1).
INGRESS = {
    'tcp-5432': {
        'protocol': '6',
        'source': '0.0.0.0/0',
        'sourceType': 'CIDR_BLOCK',
        'stateless': False,
        'tcpOptions': {'min': 5432, 'max': 5432},
    },
    'tcp-22': {
        'protocol': '6',
        'source': '0.0.0.0/0',
        'sourceType': 'CIDR_BLOCK',
        'stateless': False,
        'tcpOptions': {'min': 22, 'max': 22},
    },
    'fragmentation-needed': {
        'protocol': '1',
        'source': '0.0.0.0/0',
        'sourceType': 'CIDR_BLOCK',
        'stateless': False,
        'icmpOptions': {'type': 3, 'code': 4},
    },
    'unreachable-from-the-vcn': {
        'protocol': '1',
        'source': '10.10.0.0/24',
        'sourceType': 'CIDR_BLOCK',
        'stateless': False,
        'icmpOptions': {'type': 3},
    },
}
EGRESS = {
    'all': {'protocol': 'all', 'destination': '0.0.0.0/0', 'destinationType': 'CIDR_BLOCK', 'stateless': False},
}


def test_the_list_holds_the_declared_rules_and_nothing_else(run: Appliance) -> None:
    declared = _one(run, SECURITY_LIST)

    assert len(declared['ingressSecurityRules']) == len(INGRESS)
    assert all(rule in declared['ingressSecurityRules'] for rule in INGRESS.values())
    assert len(declared['egressSecurityRules']) == len(EGRESS)
    assert all(rule in declared['egressSecurityRules'] for rule in EGRESS.values())


def test_the_subnet_carries_only_the_programs_list_and_table(run: Appliance) -> None:
    subnet = _one(run, SUBNET)

    assert subnet['securityListIds'] == [f'{NAME}-rules_id']
    assert subnet['routeTableId'] == f'{NAME}-routes_id'
    assert _one(run, 'oci:Core/routeTable:RouteTable')['routeRules'] == [
        {'networkEntityId': f'{NAME}-igw_id', 'destination': '0.0.0.0/0', 'destinationType': 'CIDR_BLOCK'}
    ]


def _route_table_ids(inputs: object) -> Iterator[object]:
    """Every `routeTableId` an input holds, at any depth."""
    if isinstance(inputs, dict):
        for key, value in cast('dict[str, object]', inputs).items():
            if key == 'routeTableId':
                yield value
            yield from _route_table_ids(value)
    elif isinstance(inputs, list):
        for value in cast('list[object]', inputs):
            yield from _route_table_ids(value)


def test_the_subnet_is_the_only_resource_handed_a_route_table(run: Appliance) -> None:
    """No route table on the interface's address or on the internet gateway (state-backend.md §1).

    OCI attaches one in two more places: to a private address, the
    interface's included, and to an internet gateway for ingress routing.
    The stack declares neither, so Pulumi compares neither, and declaring
    one later is this case going red rather than a silent change.
    """
    holding = {(declared.typ, declared.name) for declared in run.declared if list(_route_table_ids(declared.inputs))}

    assert holding == {(SUBNET, f'{NAME}-subnet')}
    assert run.of_type('oci:Core/privateIp:PrivateIp') == []
    assert len(run.of_type('oci:Core/routeTable:RouteTable')) == 1


PROTECTED = {
    'vcn': 'oci:Core/vcn:Vcn',
    'subnet': SUBNET,
    'address': PUBLIC_IP,
    'image-bucket': IMAGE_BUCKET,
    'dump-bucket': DUMP_BUCKET,
}


def test_nothing_else_is_protected(run: Appliance) -> None:
    protected = {request.type for request in run.requested if request.protect}

    assert protected == set(PROTECTED.values())


# --------------------------------------------------------------------------
# What the cutover adopts.
# --------------------------------------------------------------------------

#: Each resource the stack adopts, by the type it is declared as. The instance,
#: the image and the dump key are not among them: the stack makes its own.
ADOPTED = {
    adoption.VCN: 'oci:Core/vcn:Vcn',
    adoption.GATEWAY: 'oci:Core/internetGateway:InternetGateway',
    adoption.SUBNET: SUBNET,
    adoption.IMAGE_BUCKET: IMAGE_BUCKET,
    adoption.ADDRESS: PUBLIC_IP,
    adoption.DUMP_BUCKET: DUMP_BUCKET,
}


def test_every_adoptable_resource_is_named_once() -> None:
    assert set(ADOPTED) == set(adoption.ADOPTABLE)


@pytest.mark.asyncio
async def test_each_adopted_id_is_its_resources_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The configuration's ids reach their resources as the program's import, and reach nothing else.

    Through the program, so each imported resource is recorded with this
    program's secret markings (framework/pulumi.md §3.3).
    """
    machine = _machine(tmp_path)
    ids = {name: f'an-id-for-{name}' for name in adoption.ADOPTABLE}

    with _files_in(monkeypatch, machine.directory):
        run = await _run(Appliance(), machine, adopted=ids)

    imported = {request.type: request.importId for request in run.requested if request.importId}
    assert imported == {ADOPTED[name]: ids[name] for name in adoption.ADOPTABLE}


def test_with_nothing_adopted_nothing_is_imported(run: Appliance) -> None:
    assert [request.type for request in run.requested if request.importId] == []


@pytest.mark.asyncio
async def test_an_adopted_name_the_stack_does_not_keep_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path)

    with (
        _files_in(monkeypatch, machine.directory),
        pytest.raises(adoption.Refused, match="names 'instance'"),
    ):
        _ = await _run(Appliance(), machine, adopted={'instance': 'ocid1.instance.oc1..box'})


# --------------------------------------------------------------------------
# The server certificate's expiry, a stack output.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_program_exports_when_the_configured_certificate_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path)
    exported: dict[str, object] = {}

    def export(name: str, value: object) -> None:
        exported[name] = value

    monkeypatch.setattr(program.pulumi, 'export', export)

    with _files_in(monkeypatch, machine.directory):
        _ = await _run(Appliance(), machine)

    certificate = pki.not_valid_after(machine.keys.server_certificate.encode())
    assert exported == {settings.CERTIFICATE_EXPIRY_OUTPUT: certificate.isoformat()}


# --------------------------------------------------------------------------
# The box.
# --------------------------------------------------------------------------


def test_the_instance_carries_the_options_of_rfc_006_4_2(run: Appliance) -> None:
    options = _options(run, INSTANCE)

    assert list(options.replaceOnChanges) == ['metadata']
    assert options.deleteBeforeReplace is True
    # An image bump would otherwise replace the boot volume of the running box
    # in place, data directory and all, with no dump hook in the way.
    assert list(options.ignoreChanges) == ['sourceDetails.sourceId']


def test_metadata_is_secret_as_an_input_and_as_an_output(run: Appliance) -> None:
    instance = _one(run, INSTANCE)

    assert _secret(instance['metadata'])
    assert 'metadata' in _options(run, INSTANCE).additionalSecretOutputs


def test_metadata_carries_the_ignition_and_nothing_else(run: Appliance) -> None:
    # A second entry would be a secret value of its own, which the driver
    # searches the checkpoint for in the clear: the dump key's id beside the
    # Ignition would refuse every write, since that id is the key's resource id.
    metadata = _one(run, INSTANCE)['metadata']['value']

    assert set(metadata) == {'user_data'}
    ignition = base64.b64decode(metadata['user_data']).decode()
    assert DUMP_KEY_ID in ignition
    assert '"ignition"' in ignition


def test_the_bill_of_materials_is_in_the_clear_and_names_no_secret(run: Appliance) -> None:
    extended = _one(run, INSTANCE)['extendedMetadata']

    assert not _secret(extended)
    assert {'butane', 'server_key', 'ssh_host_key', 'b2_dump_key_id'} <= set(extended)
    assert 'b2_dump_key' not in extended


def test_the_box_launches_where_the_shape_is_offered_and_without_a_public_address(run: Appliance) -> None:
    instance = _one(run, INSTANCE)

    assert instance['availabilityDomain'] == OFFERING
    assert instance['createVnicDetails']['assignPublicIp'] == 'false'
    assert instance['createVnicDetails']['nsgIds'] == []
    assert instance['createVnicDetails']['subnetId'] == f'{NAME}-subnet_id'


def test_the_reserved_address_points_at_the_primary_private_address(run: Appliance) -> None:
    assert _one(run, PUBLIC_IP)['privateIpId'] == 'primary'


def test_the_readiness_resource_replaces_on_the_instances_id(run: Appliance) -> None:
    readiness = _one(run, READINESS)

    assert list(_options(run, READINESS).replaceOnChanges) == ['instance_id']
    assert readiness['instance_id'] == f'{NAME}-vm_id'
    assert readiness['address'] == settings.ADDRESS


@pytest.mark.asyncio
async def test_a_reservation_at_another_address_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine = _machine(tmp_path)

    with (
        _files_in(monkeypatch, machine.directory),
        pytest.raises(ValueError, match=rf'reserved address is 192\.0\.2\.99, .* {settings.ADDRESS}'),
    ):
        _ = await _run(Appliance(address='192.0.2.99'), machine)


# --------------------------------------------------------------------------
# What is secret.
# --------------------------------------------------------------------------


def test_the_image_buckets_creator_is_secret(run: Appliance) -> None:
    assert list(_options(run, IMAGE_BUCKET).additionalSecretOutputs) == ['createdBy']


def test_the_oci_providers_user_and_fingerprint_are_secret(run: Appliance) -> None:
    provider = _one(run, 'pulumi:providers:oci')

    assert provider['userOcid'] == {SECRET_SIG: SECRET_MARK, 'value': OCI_USER}
    assert provider['fingerprint'] == {SECRET_SIG: SECRET_MARK, 'value': OCI_FINGERPRINT}
    assert _secret(provider['privateKey'])


def test_the_dump_key_is_secret_and_its_id_is_not_declared_so(run: Appliance) -> None:
    # The id is the resource's id, which the engine cannot mark: an identifier
    # rather than a credential (rfc-006 §3.3).
    declared = list(_options(run, DUMP_KEY_TYPE).additionalSecretOutputs)

    assert 'applicationKey' in declared
    assert 'applicationKeyId' not in declared


def _sdk_class(token: str) -> type:
    """The SDK class a type token registers: `<package>:<module>/<camel>:<Class>`."""
    package, module, name = token.split(':')
    root: ModuleType = {'oci': oci, 'b2': b2}[package]
    area = module.split('/')[0]
    found: ModuleType = root if area == 'index' else importlib.import_module(f'{root.__name__}.{area.lower()}')
    cls: type = getattr(found, name)
    return cls


def _output_names(cls: type) -> set[str]:
    names: set[str] = set()
    for klass in cls.__mro__:
        for attribute in vars(klass).values():
            if isinstance(attribute, property) and (name := getattr(attribute.fget, '_pulumi_name', None)):
                names.add(name)
    return names


def test_every_additional_secret_output_names_an_output_of_its_resource(run: Appliance) -> None:
    # A name spelled wrong -- `createdby` -- leaves the real output in the
    # clear, and neither the engine nor the checkpoint check notices.
    declared = {
        request.type: list(request.additionalSecretOutputs)
        for request in run.requested
        if request.additionalSecretOutputs
        and not request.type.startswith(('pulumi:providers:', 'pulumi-python:'))
        and request.custom
    }

    assert declared
    for token, names in declared.items():
        assert set(names) <= _output_names(_sdk_class(token)), token


def test_the_output_name_reader_reads_names_rather_than_attributes() -> None:
    # The control for the case above: a misspelled name is outside the set.
    names = _output_names(oci.objectstorage.Bucket)

    assert 'createdBy' in names
    assert 'createdby' not in names
    assert 'created_by' not in names


# --------------------------------------------------------------------------
# The dump bucket and its key.
# --------------------------------------------------------------------------


def test_the_dump_keys_grant_is_the_declared_one(run: Appliance) -> None:
    key = _one(run, DUMP_KEY_TYPE)

    assert key['capabilities'] == ['writeFiles']
    assert key['bucketIds'] == [DUMP_BUCKET_ID]
    assert key['namePrefix'] == f'{conventions.STATE_DUMP_PREFIX}/'


@pytest.mark.asyncio
async def test_a_new_dump_key_generation_is_a_new_key_name(
    run: Appliance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bumping the generation is the dump key's rotation, and the name is the one input a bump changes: a bump that kept it would replace no key."""
    machine = _machine(tmp_path)

    with _files_in(monkeypatch, machine.directory):
        bumped = await _run(Appliance(), machine, generation=program.FIRST_DUMP_KEY_GENERATION + 1)

    assert _one(bumped, DUMP_KEY_TYPE)['keyName'] != _one(run, DUMP_KEY_TYPE)['keyName']


def test_the_dump_buckets_retention_is_one_rule_on_the_dump_prefix(run: Appliance) -> None:
    bucket = _one(run, DUMP_BUCKET)

    assert bucket['bucketName'] == settings.B2_BUCKET
    (rule,) = bucket['lifecycleRules']
    assert rule['fileNamePrefix'] == f'{conventions.STATE_DUMP_PREFIX}/'
    assert rule['daysFromUploadingToHiding'] == settings.B2_RETENTION_DAYS


# --------------------------------------------------------------------------
# Where the hooks are bound.
# --------------------------------------------------------------------------


def _bound(request: Any) -> dict[str, list[str]]:
    binding = request.hooks
    kinds = ('before_create', 'after_create', 'before_update', 'after_update', 'before_delete', 'after_delete')
    return {kind: list(getattr(binding, kind)) for kind in kinds if list(getattr(binding, kind))}


def test_the_hooks_are_bound_where_rfc_006_4_3_says(run: Appliance) -> None:
    bound = {request.type: _bound(request) for request in run.requested if _bound(request)}

    assert bound == {
        INSTANCE: {'before_create': [f'{NAME}-permit'], 'before_delete': [f'{NAME}-dump']},
        READINESS: {'after_create': [f'{NAME}-restore']},
    }


def test_the_stacks_own_providers_sign_every_resource_and_every_call(run: Appliance) -> None:
    # A default provider configures itself from the workstation's own OCI
    # configuration, and nothing in this stack's configuration disables it
    # yet: the program's two are what keep every write on the appliance's
    # own credentials (rfc-006 §4.1).
    expected = {'oci': f'::{NAME}-oci::', 'b2': f'::{NAME}-b2::'}
    native = [declaration for declaration in run.declared if declaration.typ.split(':')[0] in expected]
    tokens = {call.token for call in run.called}

    assert native
    for declaration in native:
        assert expected[declaration.typ.split(':')[0]] in declaration.provider, (declaration.typ, declaration.name)
    assert tokens
    for token in tokens:
        assert expected['oci'] in run.called_through(token), token


# --------------------------------------------------------------------------
# Refused before anything is declared.
# --------------------------------------------------------------------------


def _commands_named(refusal: BaseException) -> list[list[str]]:
    """The leaf of each `credentials` command a refusal names, in its order, refused unless the parser carries it.

    A refusal names its remedy in backticks. What is returned is the leaf as
    the parser reads it -- subject, row, verb -- so the options after it (a
    `--rotate`) and their values are left out, and a row named only as an
    option's value is not taken for the row. The parser is read when the
    refusal is, since the backup generations' rows are the window the settings
    name at that moment.
    """
    named = named_commands(str(refusal))
    assert named, f'{refusal} names no `credentials` command'
    return [['credentials', parsed['subject'], parsed['member'], parsed['action']] for _, parsed in named]


def _command_named(refusal: BaseException) -> list[str]:
    """The words of the one `credentials` command a refusal names, refused unless the parser carries it."""
    (named,) = _commands_named(refusal)
    return named


@pytest.mark.asyncio
async def test_a_host_key_the_committed_line_does_not_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path)
    _ = (tmp_path / committed.HOST_KEY.name).write_text(f'{committed.public_host_key(_host_key())}\n')

    with _files_in(monkeypatch, machine.directory), pytest.raises(committed.Refused) as refused:
        _ = await _run(Appliance(), machine)

    assert derived.STATE_BACKEND_HOST_KEY_ROW in _command_named(refused.value)


@pytest.mark.asyncio
async def test_a_server_key_that_does_not_open_its_certificate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(tmp_path)
    other = machine.authority.issue_server(settings.ADDRESS)
    keys = dataclasses.replace(machine.keys, server_key=other.key_pem.decode())

    with _files_in(monkeypatch, machine.directory), pytest.raises(committed.Refused) as refused:
        _ = await _run(Appliance(), dataclasses.replace(machine, keys=keys))

    assert derived.STATE_BACKEND_SERVER_ROW in _command_named(refused.value)


#: Each committed file, and the `credentials derived` rows that write it in
#: the order a refusal names them, as the writers spell their rows.
ABSENT = {
    'host-key': (committed.HOST_KEY.name, (derived.STATE_BACKEND_HOST_KEY_ROW,)),
    'backup-recipients': (committed.BACKUP_RECIPIENTS.name, tuple(map(escrow.row_name, escrow.backup_labels()))),
    'drill-recipient': (committed.DRILL_RECIPIENT.name, (derived.DRILL_AGE_IDENTITY_ROW,)),
}


def _generating(rows: Iterable[str]) -> list[list[str]]:
    """The command that writes each row's file, as a refusal names it."""
    return [['credentials', 'derived', row, 'generate'] for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(('file', 'rows'), ABSENT.values(), ids=ABSENT.keys())
async def test_a_committed_file_absent_is_refused_naming_its_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file: str, rows: tuple[str, ...]
) -> None:
    machine = _machine(tmp_path)
    (tmp_path / file).unlink()

    with _files_in(monkeypatch, machine.directory), pytest.raises(committed.Refused) as refused:
        _ = await _run(Appliance(), machine)

    assert _commands_named(refused.value) == _generating(rows)


@pytest.mark.parametrize(
    ('generation', 'width'),
    [(committed.FIRST_GENERATION, 1), (committed.FIRST_GENERATION + 1, 2)],
    ids=['first', 'after-a-rotation'],
)
def test_an_absent_recipients_file_names_each_generation_of_the_window_current_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generation: int, width: int
) -> None:
    monkeypatch.setattr(settings, 'AGE_GENERATION', generation)

    with pytest.raises(committed.Refused) as refused:
        _ = committed.backup_recipients(tmp_path / committed.BACKUP_RECIPIENTS.name)

    window = escrow.backup_labels()
    assert (window[0], len(window)) == (f'{escrow.BACKUP}/{generation}', width)
    assert _commands_named(refused.value) == _generating(map(escrow.row_name, window))


def test_the_current_generation_absent_from_the_recipients_file_is_refused(tmp_path: Path) -> None:
    (current, *_) = committed.backup_window()
    backup, drill = tmp_path / 'backup', tmp_path / 'drill'
    _ = backup.write_text(f'{current.rsplit("/", 1)[0]}/{settings.AGE_GENERATION + 1} {CURRENT}\n')
    _ = drill.write_text(f'{DRILL}\n')

    with pytest.raises(committed.Refused, match=f'no recipient for {current}'):
        _ = committed.age_recipients(backup=backup, drill=drill)


def test_the_box_encrypts_to_the_window_then_the_drill(tmp_path: Path) -> None:
    (current, *_) = committed.backup_window()
    backup, drill = tmp_path / 'backup', tmp_path / 'drill'
    _ = backup.write_text(f'{current} {CURRENT}\n')
    _ = drill.write_text(f'{DRILL}\n')

    assert committed.age_recipients(backup=backup, drill=drill) == (CURRENT, DRILL)


@pytest.mark.parametrize(
    'line',
    [
        'AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ',
        'age1pq1notclassic',
        'ssh-ed25519 AAAA',
    ],
)
def test_a_recipient_not_in_the_drawn_form_is_refused_unquoted(tmp_path: Path, line: str) -> None:
    (current, *_) = committed.backup_window()
    backup = tmp_path / 'backup'
    _ = backup.write_text(f'{current} {line.replace(" ", "")}\n')

    with pytest.raises(committed.Refused) as refused:
        _ = committed.backup_recipients(backup)

    assert line.replace(' ', '')[4:] not in str(refused.value)


# --------------------------------------------------------------------------
# The two spellings held equal.
# --------------------------------------------------------------------------


def test_the_readers_spell_what_the_writers_spell() -> None:
    assert committed.BACKUP_LABEL_PREFIX == escrow.BACKUP
    assert committed.backup_window() == escrow.backup_labels()
    assert committed.HOST_KEY_ROW == derived.STATE_BACKEND_HOST_KEY_ROW
    assert committed.DRILL_RECIPIENT_ROW == derived.DRILL_AGE_IDENTITY_ROW
    assert component.SERVER_ROW == derived.STATE_BACKEND_SERVER_ROW
    assert all(committed.backup_row(label) == escrow.row_name(label) for label in escrow.backup_labels())


def test_the_program_reads_the_keys_the_rows_write() -> None:
    assert (program.OCI_USER_OCID, program.OCI_FINGERPRINT, program.OCI_PRIVATE_KEY) == (
        derived.OCI_USER_KEY,
        derived.OCI_FINGERPRINT_KEY,
        derived.OCI_PRIVATE_KEY_KEY,
    )
    assert (oci_objects.USER_CONFIG, oci_objects.FINGERPRINT_CONFIG, oci_objects.PRIVATE_KEY_CONFIG) == (
        derived.OCI_USER_KEY,
        derived.OCI_FINGERPRINT_KEY,
        derived.OCI_PRIVATE_KEY_KEY,
    )
    assert (program.B2_KEY_ID, program.B2_KEY) == (derived.B2_KEY_ID_KEY, derived.B2_KEY_KEY)
    assert (program.SERVER_KEY, program.SERVER_CERTIFICATE, program.CA_CERTIFICATE, program.SSH_HOST_KEY) == (
        derived.SERVER_KEY_KEY,
        derived.SERVER_CERT_KEY,
        derived.CA_CERT_KEY,
        derived.HOST_KEY_KEY,
    )


# --------------------------------------------------------------------------
# The render: a function of the commit (rfc-006 §4.4).
# --------------------------------------------------------------------------


def _render_machine(keys: component.Keys) -> render.Machine:
    return render.machine(
        ca_cert=keys.ca_certificate,
        server_cert=keys.server_certificate,
        server_key=keys.server_key,
        ssh_host_key=keys.ssh_host_key,
        age_recipients=(CURRENT, DRILL),
        dump_key_id=DUMP_KEY_ID,
        dump_key=DUMP_KEY,
        bucket_id=DUMP_BUCKET_ID,
    )


def test_two_renders_from_the_same_inputs_are_equal_byte_for_byte(tmp_path: Path) -> None:
    keys = _machine(tmp_path).keys

    first, second = _render_machine(keys), _render_machine(keys)

    assert render.render_ignition(first) == render.render_ignition(second)
    assert render.bill_of_materials(first) == render.bill_of_materials(second)


def _changed(field: str, value: object, machine: Machine) -> object:
    """Another valid value for one field of the render."""
    other = _machine(machine.directory / 'other')
    match field:
        case 'ca_cert':
            return other.keys.ca_certificate
        case 'server_cert':
            return machine.authority.issue_server(settings.ADDRESS).cert_pem.decode()
        case 'server_key':
            return other.keys.server_key
        case 'ssh_host_key':
            return other.keys.ssh_host_key
        case _:
            pass
    if isinstance(value, tuple):
        return (*value, 'another')  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, int):
        return value + 1
    return f'{value}-another'


@pytest.mark.parametrize('field', [spec.name for spec in dataclasses.fields(render.Machine)])
def test_each_single_input_change_moves_exactly_its_own_digest(tmp_path: Path, field: str) -> None:
    machine = _machine(tmp_path)
    before = _render_machine(machine.keys)
    after = dataclasses.replace(before, **{field: _changed(field, getattr(before, field), machine)})

    old, new = render.bill_of_materials(before), render.bill_of_materials(after)
    moved = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}

    # The dump key's secret has no public half; it is named by its id, which
    # moves with it.
    assert moved == (set() if field == 'b2_dump_key' else {field})


# --------------------------------------------------------------------------
# The hooks, as functions.
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Tools:
    """`state`'s tools, replaced: what each was asked, and what the backend serves."""

    calls: list[str] = dataclasses.field(default_factory=list[str])
    served: list[str] = dataclasses.field(default_factory=list[str])
    dump_fails: bool = False
    #: The bundle directory every connection was built from.
    bundles: list[Path] = dataclasses.field(default_factory=list[Path])


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> Tools:
    found = Tools()

    def connection(bundle_dir: Path) -> state.Connection:
        found.bundles.append(bundle_dir)
        return state.Connection(url=f'postgres://box/{bundle_dir.name}', env={})

    def pg_dump(_target: state.Connection, destination: Path) -> None:
        found.calls.append('pg_dump')
        if found.dump_fails:
            raise state.StateError('pg_dump against postgres://box failed: connection refused')
        _ = destination.write_bytes(b'PGDMP the archive')

    def verify_dump(archive: Path) -> list[str]:
        found.calls.append('verify_dump')
        return [str(archive)]

    def encrypt(source: Path, destination: Path, recipients: tuple[str, ...]) -> None:
        found.calls.append(f'encrypt to {len(recipients)}')
        _ = destination.write_bytes(b'age-encryption.org/' + source.read_bytes())

    def pg_restore(_target: state.Connection, archive: Path) -> None:
        found.calls.append(f'pg_restore {archive.read_bytes().decode()}')
        found.served.append('organization/kluster-py/physical')

    def stacks(_target: state.Connection) -> list[str]:
        found.calls.append('stacks')
        return list(found.served)

    for name, replacement in {
        'connection': connection,
        'pg_dump': pg_dump,
        'verify_dump': verify_dump,
        'encrypt': encrypt,
        'pg_restore': pg_restore,
        'stacks': stacks,
    }.items():
        monkeypatch.setattr(state, name, replacement)
    return found


def _replacement(tmp_path: Path, *, granted: bool) -> hooks.Replacement:
    return hooks.Replacement(
        bundle_dir=tmp_path / 'bundle',
        recipients=(CURRENT, DRILL),
        dump_directory=tmp_path,
        granted=lambda: granted,
    )


@pytest.mark.parametrize('hook', ['permit_now', 'dump_now'])
def test_without_the_permission_the_create_and_delete_hooks_refuse_and_nothing_is_dumped(
    tools: Tools, tmp_path: Path, hook: str
) -> None:
    replacement = _replacement(tmp_path, granted=False)

    with pytest.raises(hooks.HookRefused, match=r'up --force'):
        _ = getattr(replacement, hook)()

    assert tools.calls == []
    assert replacement.archive is None
    assert not list(tmp_path.glob('*.dump.age'))


def test_with_the_permission_the_delete_hook_dumps_and_keeps_the_plaintext(tools: Tools, tmp_path: Path) -> None:
    replacement = _replacement(tmp_path, granted=True)

    dumped = replacement.dump_now()

    assert tools.calls == ['pg_dump', 'verify_dump', 'encrypt to 2']
    assert dumped.parent == tmp_path
    assert dumped.name.endswith('.dump.age')
    assert dumped.read_bytes().startswith(b'age-encryption.org/')
    assert replacement.archive is not None
    assert replacement.archive.read_bytes() == b'PGDMP the archive'
    assert not replacement.archive.is_relative_to(tmp_path)


def test_a_failed_dump_raises_and_keeps_nothing(tools: Tools, tmp_path: Path) -> None:
    tools.dump_fails = True
    replacement = _replacement(tmp_path, granted=True)

    with pytest.raises(state.StateError, match='connection refused'):
        _ = replacement.dump_now()

    assert replacement.archive is None
    assert not list(tmp_path.glob('*.dump.age'))


def test_the_restore_hook_restores_from_the_plaintext(tools: Tools, tmp_path: Path) -> None:
    replacement = _replacement(tmp_path, granted=True)
    _ = replacement.dump_now()

    held = replacement.restore_now()

    assert tools.calls[-2:] == ['pg_restore PGDMP the archive', 'stacks']
    assert held == ['organization/kluster-py/physical']


def test_without_a_plaintext_over_a_backend_that_holds_no_stack_the_restore_hook_refuses(
    tools: Tools, tmp_path: Path
) -> None:
    replacement = _replacement(tmp_path, granted=True)

    with pytest.raises(hooks.HookRefused, match=r'state-backend restore <file>') as refused:
        _ = replacement.restore_now()

    assert 'stack init' in str(refused.value)
    assert tools.calls == ['stacks']


def test_without_a_plaintext_over_a_backend_that_holds_stacks_the_restore_hook_passes(
    tools: Tools, tmp_path: Path
) -> None:
    # A readiness resource re-created against a box this run did not replace
    # (the refused replacement, rfc-006 slice 0's X1) finds the state there.
    tools.served.append('organization/kluster-py/dns')
    replacement = _replacement(tmp_path, granted=False)

    assert replacement.restore_now() == ['organization/kluster-py/dns']
    assert tools.calls == ['stacks']


def test_the_restore_hook_never_restores_over_a_backend_that_serves_a_stack(tools: Tools, tmp_path: Path) -> None:
    # The guard `state-backend restore` runs before the same `pg_restore --clean`.
    replacement = _replacement(tmp_path, granted=True)
    dumped = replacement.dump_now()
    tools.served.append('organization/kluster-py/dns')

    with pytest.raises(hooks.HookRefused, match=str(dumped)):
        _ = replacement.restore_now()

    assert not [call for call in tools.calls if call.startswith('pg_restore')]


def test_the_permission_is_the_one_value_in_the_one_variable() -> None:
    assert permission.granted({permission.ENV: permission.GRANTED})
    assert not permission.granted({permission.ENV: 'yes'})
    assert not permission.granted({})


# --------------------------------------------------------------------------
# The hooks the engine calls: the ones the program registers.
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Wired:
    """A run of the whole program, its registered hooks, and where they should reach."""

    run: Appliance
    #: The `operator` client bundle's slot, as the program should name it.
    bundle: Path
    #: The directory the program runs in, where a dump is written.
    work: Path


@pytest_asyncio.fixture
async def wired(tools: Tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wired:
    machine = _machine(tmp_path / 'machine')
    slots = tmp_path / 'credentials'
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.setattr(lib_workstation, 'directory', lambda: slots)
    monkeypatch.chdir(work)
    monkeypatch.delenv(permission.ENV, raising=False)
    with _files_in(monkeypatch, machine.directory):
        run = await _run(Appliance(), machine)
    assert tools.calls == []
    return Wired(run=run, bundle=slots / stack_environment.BUNDLE_SLOT, work=work)


async def _call(wired: Wired, hook: str, typ: str) -> None:
    """Call a registered hook the way the engine does: by its name, with the resource it runs for."""
    args = pulumi.ResourceHookArgs(urn=f'urn:pulumi:{NAME}::kluster::{typ}::x', id='x', name='x', type=typ)
    called = wired.run.hooks[f'{NAME}-{hook}'].callback(args)
    if inspect.isawaitable(called):
        await called


@pytest.mark.asyncio
@pytest.mark.parametrize('hook', ['permit', 'dump'])
async def test_without_the_permission_the_registered_instance_hooks_refuse_and_dump_nothing(
    wired: Wired, tools: Tools, hook: str
) -> None:
    with pytest.raises(hooks.HookRefused, match=permission.ENV):
        await _call(wired, hook, INSTANCE)

    assert tools.calls == []
    assert not list(wired.work.glob('*.dump.age'))


@pytest.mark.asyncio
async def test_with_the_permission_the_registered_hooks_dump_and_restore_through_the_operator_bundle(
    wired: Wired, tools: Tools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(permission.ENV, permission.GRANTED)

    await _call(wired, 'permit', INSTANCE)
    assert tools.calls == []

    await _call(wired, 'dump', INSTANCE)
    assert tools.calls == ['pg_dump', 'verify_dump', 'encrypt to 2']
    (dumped,) = wired.work.glob('*.dump.age')
    assert dumped.read_bytes().startswith(b'age-encryption.org/')

    await _call(wired, 'restore', READINESS)
    assert tools.calls[3:] == ['stacks', 'pg_restore PGDMP the archive', 'stacks']
    assert set(tools.bundles) == {wired.bundle}


# --------------------------------------------------------------------------
# The image upload.
# --------------------------------------------------------------------------

ARTIFACT = lzma.compress(b'a disk image ' * 4096)


class _Stream:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def iter_content(self, size: int) -> Iterator[bytes]:
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


@pytest.fixture
def uploaded(monkeypatch: pytest.MonkeyPatch) -> Callable[[bytes], list[bytes]]:
    """Serve `body` as the artifact, and answer what reached the bucket."""
    sent: list[bytes] = []

    def serve(body: bytes) -> list[bytes]:
        def fetch(_url: str) -> _Stream:
            return _Stream(body)

        def upload(_config: object, _props: object, path: Path) -> None:
            sent.append(path.read_bytes())

        monkeypatch.setattr(oci_objects, 'fetch', fetch)
        monkeypatch.setattr(oci_objects, 'upload', upload)
        return sent

    return serve


def _artifact_provider() -> oci_objects.ArtifactObjectProvider:
    provider = oci_objects.ArtifactObjectProvider()
    provider.user, provider.fingerprint, provider.private_key = OCI_USER, OCI_FINGERPRINT, OCI_KEY
    return provider


def _props(sha256: str) -> dict[str, Any]:
    return {
        'url': 'https://example.invalid/artifact.qcow2.xz',
        'sha256': sha256,
        'region': 'us-phoenix-1',
        'tenancy': 'ocid1.tenancy.oc1..test',
        'namespace': 'a-namespace',
        'bucket': settings.IMAGE_BUCKET,
        'object_name': 'artifact.qcow2',
    }


def test_the_upload_sends_the_decompressed_artifact(uploaded: Callable[[bytes], list[bytes]]) -> None:
    sent = uploaded(ARTIFACT)

    created = _artifact_provider().create(_props(hashlib.sha256(ARTIFACT).hexdigest()))

    assert sent == [lzma.decompress(ARTIFACT)]
    assert created.id == f'a-namespace/{settings.IMAGE_BUCKET}/artifact.qcow2'


def test_the_upload_refuses_an_artifact_with_the_wrong_digest(uploaded: Callable[[bytes], list[bytes]]) -> None:
    sent = uploaded(ARTIFACT)

    with pytest.raises(oci_objects.DigestMismatch):
        _ = _artifact_provider().create(_props(hashlib.sha256(b'another artifact').hexdigest()))

    assert sent == []


def test_the_upload_refuses_a_truncated_artifact(uploaded: Callable[[bytes], list[bytes]]) -> None:
    # The digest is the truncated bytes' own, so only the end of the stream
    # can tell.
    truncated = ARTIFACT[: len(ARTIFACT) // 2]
    sent = uploaded(truncated)

    with pytest.raises(oci_objects.TruncatedArtifact):
        _ = _artifact_provider().create(_props(hashlib.sha256(truncated).hexdigest()))

    assert sent == []


def test_a_wrong_digest_is_refused_before_a_byte_is_decompressed(
    uploaded: Callable[[bytes], list[bytes]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stream of zeros compresses some thousands to one: decompressed before
    # its digest was compared, a small download would fill the disk.
    bomb = lzma.compress(bytes(64 << 20), preset=9)
    _ = uploaded(bomb)
    opened: list[int] = []
    monkeypatch.setattr(oci_objects.lzma, 'LZMADecompressor', lambda: opened.append(1))
    path = tmp_path / 'artifact'

    with pytest.raises(oci_objects.DigestMismatch):
        oci_objects.materialize('https://example.invalid/bomb.xz', hashlib.sha256(b'other').hexdigest(), path)

    assert opened == []
    assert list(tmp_path.iterdir()) == []


def test_a_matching_artifact_is_decompressed_a_bounded_amount_at_a_time(
    uploaded: Callable[[bytes], list[bytes]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = bytes(16 << 20)
    compressed = lzma.compress(whole, preset=9)
    _ = uploaded(compressed)
    largest: list[int] = []
    real = lzma.LZMADecompressor

    class Measured:
        def __init__(self) -> None:
            self.inner = real()

        @property
        def eof(self) -> bool:
            return self.inner.eof

        @property
        def needs_input(self) -> bool:
            return self.inner.needs_input

        def decompress(self, data: bytes, max_length: int = -1) -> bytes:
            out = self.inner.decompress(data, max_length)
            largest.append(len(out))
            return out

    monkeypatch.setattr(oci_objects.lzma, 'LZMADecompressor', Measured)
    path = tmp_path / 'artifact'

    oci_objects.materialize('https://example.invalid/zeros.xz', hashlib.sha256(compressed).hexdigest(), path)

    assert path.read_bytes() == whole
    assert max(largest) <= oci_objects.CHUNK_BYTES
    assert [entry.name for entry in tmp_path.iterdir()] == ['artifact']


def test_a_replacement_that_keeps_the_objects_name_deletes_the_old_one_first() -> None:
    # Created before the old one is deleted, the new object would be removed
    # by that delete, the two being one object.
    provider = _artifact_provider()
    olds = provider.check({}, _props('a' * 64)).inputs
    corrected = provider.check({}, _props('b' * 64)).inputs
    renamed = provider.check({}, {**_props('b' * 64), 'object_name': 'next.qcow2'}).inputs

    assert provider.diff('id', olds, corrected).delete_before_replace is True
    assert provider.diff('id', olds, renamed).delete_before_replace is False


def test_the_appliances_object_is_named_for_its_digest(run: Appliance) -> None:
    (artifact,) = run.of_type('pulumi-python:dynamic/oci_objects:ArtifactObject')

    assert settings.FCOS_ARTIFACT_SHA256[:12] in artifact.inputs['object_name']


def test_a_rotation_alone_re_stamps_and_a_release_bump_replaces(uploaded: Callable[[bytes], list[bytes]]) -> None:
    provider = _artifact_provider()
    olds = provider.check({}, _props('a' * 64)).inputs
    provider.private_key = 'a-rotated-key'
    rotated = provider.check({}, _props('a' * 64)).inputs
    bumped = provider.check({}, {**_props('b' * 64), 'object_name': 'next.qcow2'}).inputs

    assert provider.diff('id', olds, rotated).replaces == []
    assert provider.diff('id', olds, rotated).changes is True
    assert set(provider.diff('id', olds, bumped).replaces or ()) == {'sha256', 'object_name'}
    assert uploaded(ARTIFACT) == []


# --------------------------------------------------------------------------
# The readiness wait.
# --------------------------------------------------------------------------

LOOPBACK = '127.0.0.1'


@contextmanager
def _postgres_tls(credential: pki.Credential, directory: Path) -> Generator[int]:
    """A server on the loopback that answers one `SSLRequest` and then a TLS handshake with `credential`."""
    cert, key = directory / 'server.crt', directory / 'server.key'
    _ = cert.write_bytes(credential.cert_pem)
    _ = key.write_bytes(credential.key_pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.create_server((LOOPBACK, 0))

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            _ = connection.recv(len(postgres_tls.SSL_REQUEST))
            connection.sendall(b'S')
            try:
                with context.wrap_socket(connection, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        server.join(timeout=10)


def test_the_wait_takes_a_certificate_that_chains_to_the_ca_and_names_the_address(tmp_path: Path) -> None:
    authority = pki.Authority.from_pem(pki.generate_ca_key())

    with _postgres_tls(authority.issue_server(LOOPBACK), tmp_path) as port:
        postgres_tls.wait(LOOPBACK, port, authority.certificate().cert_pem.decode(), timeout=30)


def _without_authority_key_identifier(authority: pki.Authority, address: str) -> pki.Credential:
    """A server certificate the CA signed, for `address`, as `pki` issued them before it named the CA's key."""
    key = ec.generate_private_key(pki.CURVE)
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, pki.CA_COMMON_NAME)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + pki.LEAF_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), critical=False)
        .sign(authority.key, hashes.SHA256())
    )
    return pki.Credential(
        key_pem=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
    )


def test_the_wait_holds_the_chain_to_strict_checks_on_every_interpreter(tmp_path: Path) -> None:
    # Python's default context is strict from 3.13 only; the wait sets it
    # itself, so a chain one interpreter refuses is refused on all of them.
    # The certificate is otherwise sound -- signed by the CA, for the address
    # -- so what refuses it is the strict check alone.
    authority = pki.Authority.from_pem(pki.generate_ca_key())
    credential = _without_authority_key_identifier(authority, LOOPBACK)
    x509.load_pem_x509_certificate(credential.cert_pem).verify_directly_issued_by(
        x509.load_pem_x509_certificate(authority.certificate().cert_pem)
    )

    with (
        _postgres_tls(credential, tmp_path) as port,
        pytest.raises(postgres_tls.WrongCertificate, match='Authority Key Identifier'),
    ):
        postgres_tls.wait(LOOPBACK, port, authority.certificate().cert_pem.decode(), timeout=30)


def test_the_wait_refuses_a_certificate_naming_another_address(tmp_path: Path) -> None:
    authority = pki.Authority.from_pem(pki.generate_ca_key())

    with (
        _postgres_tls(authority.issue_server('192.0.2.10'), tmp_path) as port,
        pytest.raises(postgres_tls.WrongCertificate, match=LOOPBACK),
    ):
        postgres_tls.wait(LOOPBACK, port, authority.certificate().cert_pem.decode(), timeout=30)


def test_the_wait_refuses_a_certificate_from_another_authority(tmp_path: Path) -> None:
    authority = pki.Authority.from_pem(pki.generate_ca_key())
    other = pki.Authority.from_pem(pki.generate_ca_key())

    with _postgres_tls(other.issue_server(LOOPBACK), tmp_path) as port, pytest.raises(postgres_tls.WrongCertificate):
        postgres_tls.wait(LOOPBACK, port, authority.certificate().cert_pem.decode(), timeout=30)


@dataclasses.dataclass
class _Clock:
    """`postgres_tls`'s clock in the wait's cases: only the wait's own sleeps move it, and each read is counted.

    It replaces `postgres_tls`'s own name `time` rather than anything in the
    `time` module, so the wait is the one thing that reads it and everything
    else the case runs keeps the real clock.
    """

    now: float = 0.0
    reads: int = 0

    def monotonic(self) -> float:
        self.reads += 1
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def read(self) -> float:
        """The time an attempt is made at, refused if the wait took its deadline from another clock.

        The wait reads its clock for the deadline before its first attempt, so
        an attempt that finds this clock unread is one about to be waited out
        in real seconds, and the case stops there instead of paying them.
        """
        assert self.reads, '`postgres_tls.wait` read another clock for its deadline'
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    found = _Clock()
    monkeypatch.setattr(postgres_tls, 'time', found)
    return found


def test_the_wait_waits_out_a_box_that_does_not_answer_yet(clock: _Clock) -> None:
    attempts: list[float] = []

    def attempt(_address: str, _port: int, _ca: str) -> None:
        attempts.append(clock.read())
        if len(attempts) < 3:
            raise postgres_tls.NotAnswering('connection refused')

    postgres_tls.wait(LOOPBACK, 5432, 'unused', timeout=postgres_tls.TIMEOUT, attempt=attempt)

    assert attempts == [0, postgres_tls.INTERVAL, 2 * postgres_tls.INTERVAL]


def test_the_wait_says_what_it_waits_on_before_it_starts_and_after_each_attempt_that_finds_no_answer(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Through the engine's diagnostics, the channel `pulumi` draws while the
    # run goes: the standard `logging` module's `info` is dropped in the
    # process a dynamic provider runs in.
    events: list[tuple[str, str]] = []

    def said(message: str, *_args: object, **_kwargs: object) -> None:
        events.append(('said', message))

    def attempt(_address: str, _port: int, _ca: str) -> None:
        events.append(('attempt', str(clock.read())))
        if sum(1 for kind, _ in events if kind == 'attempt') < 3:
            raise postgres_tls.NotAnswering('connection refused')

    monkeypatch.setattr(postgres_tls.pulumi.log, 'info', said)

    postgres_tls.wait(LOOPBACK, 5432, 'unused', timeout=postgres_tls.TIMEOUT, attempt=attempt)

    assert [kind for kind, _ in events] == ['said', 'attempt', 'said', 'attempt', 'said', 'attempt', 'said']
    said_lines = [text for kind, text in events if kind == 'said']
    before, *between, answered = said_lines
    for text in (before, *between, answered):
        assert f'{LOOPBACK}:5432' in text, text
    assert f'{postgres_tls.INTERVAL}s' in before, before
    assert f'{postgres_tls.TIMEOUT}s' in before, before
    for text in between:
        assert 'connection refused' in text, text
    assert 'answered' in answered, answered


def test_the_wait_gives_up_on_a_box_that_never_answers(clock: _Clock) -> None:
    attempts: list[float] = []

    def attempt(_address: str, _port: int, _ca: str) -> None:
        attempts.append(clock.read())
        raise postgres_tls.NotAnswering('connection refused')

    with pytest.raises(postgres_tls.NotAnswering, match=f'within {postgres_tls.TIMEOUT}s'):
        postgres_tls.wait(LOOPBACK, 5432, 'unused', timeout=postgres_tls.TIMEOUT, attempt=attempt)

    assert attempts[-1] <= postgres_tls.TIMEOUT < attempts[-1] + postgres_tls.INTERVAL
