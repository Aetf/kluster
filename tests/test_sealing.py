"""The SealedSecret channel end to end: sealed by the pinned `kubeseal`, written where the declaring stack reads it.

Sealing runs the real tool against a key pair this module generates, and the
proof that a value was sealed for the Secret its row names is opening it the
way the controller would, with `kubeseal --recovery-unseal` and the private
half: under its own name and namespace it opens, and under any other it does
not. No cluster is involved; the certificate a run fetches from one is the
public half of that pair here.

The write goes through the `pulumi` runner the package already fakes
(`fake_pulumi`), taught here the two invocations a seal makes -- a write at a
path in the clear, and a read-back that says how the value is stored -- and
once through the real CLI over a backend of the case's own, which is what
holds the arguments to what the pinned `pulumi` accepts.

The Cloudflare mint runs against the fake platform the derived rows use
(`cloudflare_api`), so the token's scope is read back as the token itself
sees it.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from cloudflare_api import ACCOUNT_ID, FakeApi, console_seed
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fake_pulumi import RecordedPulumi
from memory_kit import MemoryKit
from pulumi.runtime import rpc

from kluster import conventions
from kluster.scripts.credentials import cloudflare, derived, devices, pulumi_config, sealing
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.pulumi_config import SlotRefused

#: Bounds one `kubeseal` call: every call here is local.
TIMEOUT = 30

K8S_BASE = conventions.STACK_NAMES.k8s_base
PHYSICAL = conventions.STACK_NAMES.physical
KUBECONFIG = 'apiVersion: v1\nkind: Config\nclusters: [a-fake-cluster-nothing-reaches]\n'


@dataclass(frozen=True)
class KeyPair:
    """A sealing key pair of the module's own: the certificate a run seals to, and the key that opens it."""

    certificate: str
    private_key: Path


@pytest.fixture(scope='module')
def kubeseal() -> str:
    """The pinned `kubeseal`, refused when it is missing rather than skipped."""
    binary = shutil.which(sealing.KUBESEAL)
    if binary is None:
        pytest.fail(f'{sealing.KUBESEAL} is not on PATH: mise.toml pins it, so run the suite under `mise x`')
    return binary


@pytest.fixture(scope='module')
def key_pair(tmp_path_factory: pytest.TempPathFactory) -> KeyPair:
    """An RSA key and a self-signed certificate over it, the shape the controller generates for itself."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'sealed-secret')])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private_key = tmp_path_factory.mktemp('sealing-key') / 'tls.key'
    _ = private_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return KeyPair(certificate=certificate.public_bytes(serialization.Encoding.PEM).decode(), private_key=private_key)


class Unopened(Exception):
    """`kubeseal --recovery-unseal` could not open a ciphertext with the key it was given."""


def unseal(
    kubeseal: str,
    key_pair: KeyPair,
    value: conventions.sealed.SealedValue,
    ciphertexts: Mapping[str, str],
    *,
    name: str | None = None,
    namespace: str | None = None,
) -> dict[str, str]:
    """Open `ciphertexts` as the controller would, as the SealedSecret `value` declares, or under another identity.

    The manifest carries the scope annotation `sealed_secret` writes for the
    row's scope, so what is opened is the object the stack will declare.
    """
    name = name or value.name
    namespace = namespace or value.namespace
    annotations = {f'sealedsecrets.bitnami.com/{value.scope.value}': 'true'}
    metadata = {'name': name, 'namespace': namespace, 'annotations': annotations}
    manifest = {
        'apiVersion': 'bitnami.com/v1alpha1',
        'kind': 'SealedSecret',
        'metadata': metadata,
        'spec': {'encryptedData': dict(ciphertexts), 'template': {'metadata': metadata}},
    }
    proc = subprocess.run(
        [kubeseal, '--recovery-unseal', '--recovery-private-key', str(key_pair.private_key), '-o', 'json'],
        input=json.dumps(manifest),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        raise Unopened(proc.stderr.strip())
    data: dict[str, str] = json.loads(proc.stdout)['data']
    return {key: base64.b64decode(encoded).decode() for key, encoded in data.items()}


@dataclass
class PathPulumi(RecordedPulumi):
    """The recorded `pulumi`, taught the invocations a seal and a cluster lookup make.

    `plain` holds what was written in the clear at a path; `config`, the
    inherited store, holds what was written as a secret. `outputs` is a stack's
    state as `stack output` prints it.
    """

    plain: dict[str, str] = field(default_factory=dict[str, str])
    outputs: dict[str, object] = field(default_factory=dict[str, object])
    #: How a read-back at a path misreports what was written, for the cases
    #: about the write's own check: stored as a secret, or another value.
    misreports: Literal['secret', 'value'] | None = None

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        match list(args):
            case ['config', 'set', '--path', '--plaintext', path, '--stack', _]:
                self.invocations.append(list(args))
                assert stdin is not None, 'a value named no argument and came on no standard input'
                self.plain[path] = stdin
                return ''
            case ['config', 'get', '--path', path, '--json', '--stack', _]:
                self.invocations.append(list(args))
                if path in self.plain:
                    value = 'tampered' if self.misreports == 'value' else self.plain[path]
                    return json.dumps({'value': value, 'secret': self.misreports == 'secret'})
                if path in self.config:
                    return json.dumps({'value': self.config[path], 'secret': True})
                raise SlotRefused(f'`pulumi config get --path {path}` failed: configuration key not found')
            case ['stack', 'output', '--json', '--show-secrets', '--stack', _]:
                self.invocations.append(list(args))
                return json.dumps(self.outputs)
            case _:
                return super().__call__(args, cwd=cwd, env=env, stdin=stdin)


@dataclass
class Stacks:
    """One recorded `pulumi` per stack, each opened the way a `credentials` run opens it."""

    runners: dict[str, PathPulumi] = field(default_factory=dict[str, PathPulumi])

    def runner(self, name: str) -> PathPulumi:
        return self.runners.setdefault(name, PathPulumi())

    def open(self, name: str) -> pulumi_config.Stack:
        # Each stack picks its own passphrase out of one environment, `physical`
        # its own, as in a run that holds the kit.
        environment = pulumi_config.BackendEnvironment(
            passphrase='the-stack-passphrase', physical=lambda: 'the-physical-passphrase'
        )
        return pulumi_config.Stack(
            name=name, directory=pulumi_config.project_dir(), environment=environment, run=self.runner(name)
        )


@pytest.fixture
def stacks() -> Stacks:
    """`k8s-base` and `physical` both exist, as they do by the time anything is sealed."""
    found = Stacks()
    found.runner(K8S_BASE).stacks.append(K8S_BASE)
    found.runner(PHYSICAL).stacks.append(PHYSICAL)
    return found


@pytest.fixture
def sealer(kubeseal: str, key_pair: KeyPair, stacks: Stacks) -> sealing.Sealer:
    """A sealer over the module's certificate and the recorded stacks, running the real tool."""
    _ = kubeseal
    return sealing.Sealer(certificate=key_pair.certificate, open_stack=stacks.open)


def written(stacks: Stacks, value: conventions.sealed.SealedValue) -> dict[str, str]:
    """What the seal left at `value`'s paths in its stack, by data key."""
    plain = stacks.runner(value.stack).plain
    return {key: plain[value.path(key)] for key in value.keys if value.path(key) in plain}


# -- sealing ------------------------------------------------------------------


@pytest.mark.parametrize('value', conventions.sealed.VALUES.values(), ids=conventions.sealed.VALUES)
def test_a_sealed_value_opens_under_its_name_and_namespace_and_under_no_other(
    value: conventions.sealed.SealedValue, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair
) -> None:
    data = {key: f'the {key} of {value.name}' for key in value.keys}

    ciphertexts = sealer.seal(value, data)

    # Opened as the controller opens the SealedSecret the stack declares from
    # the row: the plaintext comes back, under that name and namespace alone.
    # Strict scope binds both into the ciphertext, so a value copied into
    # another object -- by a hand, or by a row renamed on one side -- is
    # ciphertext nothing opens.
    assert unseal(kubeseal, key_pair, value, ciphertexts) == data
    with pytest.raises(Unopened):
        _ = unseal(kubeseal, key_pair, value, ciphertexts, name=f'{value.name}-other')
    with pytest.raises(Unopened):
        _ = unseal(kubeseal, key_pair, value, ciphertexts, namespace=f'{value.namespace}-other')


def test_a_seal_refuses_data_whose_keys_are_not_the_rows(sealer: sealing.Sealer) -> None:
    value = conventions.sealed.DNS01_TOKEN

    with pytest.raises(SlotRefused, match=value.name):
        _ = sealer.seal(value, {'token': 'a-value'})


# -- writing ------------------------------------------------------------------


def fake_kubeseal(args: Sequence[str], *, stdin: str | None) -> str:
    """Seals nothing: a ciphertext stand-in that names what it was given, for the cases about where it lands."""
    _ = args
    return f'AgB-sealed-{stdin}\n'


def test_the_ciphertext_is_written_in_the_clear_at_its_rows_path_in_its_rows_stack(stacks: Stacks) -> None:
    value = conventions.sealed.BGP_PASSWORD
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=fake_kubeseal)

    sealer.deliver(value, {'password': 'a-password'})

    # The program reads the row's path out of the row's stack, without
    # decrypting it: anywhere else is a value nothing reads, and stored as a
    # secret it is a second layer of encryption for no reader.
    runner = stacks.runner(value.stack)
    assert runner.plain == {f'{conventions.sealed.CONFIG_KEY}.bgp-password.password': 'AgB-sealed-a-password'}
    assert runner.config == {}
    assert not [args for args in runner.invocations if '--secret' in args]
    assert stacks.runner(PHYSICAL).plain == {}


@pytest.mark.parametrize(
    ('misreports', 'refusal'),
    [('secret', 'not stored in the clear'), ('value', 'does not read back')],
    ids=['stored as a secret', 'another value'],
)
def test_a_write_that_does_not_read_back_as_written_and_as_plain_is_refused(
    stacks: Stacks, misreports: Literal['secret', 'value'], refusal: str
) -> None:
    # The write's own check: a sealed value is read without decryption, so one
    # stored as a secret is in the wrong channel even though it reads back
    # equal, and one that reads back as anything else was not delivered.
    stacks.runner(K8S_BASE).misreports = misreports
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=fake_kubeseal)

    with pytest.raises(SlotRefused, match=refusal):
        sealer.deliver(conventions.sealed.BGP_PASSWORD, {'password': 'a-password'})


def test_a_seal_into_a_stack_that_does_not_exist_is_refused_before_anything_is_sealed(stacks: Stacks) -> None:
    sealed: list[str] = []

    def recording(args: Sequence[str], *, stdin: str | None) -> str:
        sealed.append(' '.join(args))
        return fake_kubeseal(args, stdin=stdin)

    stacks.runner(K8S_BASE).stacks.clear()
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=recording)

    with pytest.raises(SlotRefused, match='does not exist'):
        _ = sealer.stack(conventions.sealed.DNS01_TOKEN)
    assert stacks.runner(K8S_BASE).stacks == []
    assert sealed == []


#: What `kubeseal --raw` prints, in shape: base64 long enough that the CLI takes it for a secret.
SHAPED_LIKE_A_SEAL = 'AgB' + 'A' * 96


def shaped_like_kubeseal(args: Sequence[str], *, stdin: str | None) -> str:
    _ = args, stdin
    return SHAPED_LIKE_A_SEAL


def test_the_write_is_held_to_what_the_real_cli_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pulumi config set` refuses a value that looks like a secret unless told which channel it takes."""
    if shutil.which('pulumi') is None:
        pytest.fail('the pinned pulumi CLI is not on PATH: run the suite under `mise x`')
    project = tmp_path / 'project'
    project.mkdir()
    _ = (project / 'Pulumi.yaml').write_text('name: sealing-probe\nruntime: python\n')
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('PULUMI_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    environment = pulumi_config.BackendEnvironment(passphrase='probe-passphrase', url=state.as_uri())
    stack = pulumi_config.Stack(name=K8S_BASE, directory=project, environment=environment)
    stack.ensure()
    value = conventions.sealed.DNS01_TOKEN
    sealer = sealing.Sealer(certificate='', open_stack=lambda _name: stack, run=shaped_like_kubeseal)

    sealer.deliver(value, {'api-token': 'a-token'})

    committed = (project / f'Pulumi.{K8S_BASE}.yaml').read_text()
    assert 'sealing-probe:sealedSecrets:' in committed
    assert 'secure:' not in committed
    assert f'api-token: {SHAPED_LIKE_A_SEAL}' in committed


# -- the cluster --------------------------------------------------------------


def test_a_physical_stack_that_exports_no_kubeconfig_has_no_cluster(stacks: Stacks) -> None:
    with pytest.raises(sealing.NoCluster):
        _ = sealing.cluster_kubeconfig(stacks.open(PHYSICAL))

    stacks.runner(PHYSICAL).stacks.clear()
    with pytest.raises(sealing.NoCluster):
        _ = sealing.cluster_kubeconfig(stacks.open(PHYSICAL))


def test_a_kubeconfig_left_as_the_unknown_sentinel_is_refused_by_name(stacks: Stacks) -> None:
    stacks.runner(PHYSICAL).outputs[conventions.PHYSICAL_OUTPUTS.kubeconfig] = rpc.UNKNOWN

    with pytest.raises(SlotRefused, match='unknown sentinel') as refused:
        _ = sealing.cluster_kubeconfig(stacks.open(PHYSICAL))
    assert not isinstance(refused.value, sealing.NoCluster)


def test_the_certificate_is_fetched_from_the_controller_the_conventions_name_with_physicals_kubeconfig(
    stacks: Stacks,
) -> None:
    stacks.runner(PHYSICAL).outputs[conventions.PHYSICAL_OUTPUTS.kubeconfig] = KUBECONFIG
    seen: list[tuple[list[str], str]] = []

    def fetching(args: Sequence[str], *, stdin: str | None) -> str:
        _ = stdin
        argv = list(args)
        seen.append((argv, Path(argv[argv.index('--kubeconfig') + 1]).read_text()))
        return '-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n'

    sealer = sealing.cluster_sealer(stacks.open(PHYSICAL), open_stack=stacks.open, run=fetching)

    ((argv, kubeconfig),) = seen
    assert kubeconfig == KUBECONFIG
    assert argv[argv.index('--controller-name') + 1] == conventions.SEALING_CONTROLLER
    assert argv[argv.index('--controller-namespace') + 1] == conventions.SEALING_NAMESPACE
    assert '--fetch-cert' in argv
    assert 'BEGIN CERTIFICATE' in sealer.certificate


# -- the DNS-01 mint ----------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(cloudflare.requests, 'get', fake.get)
    monkeypatch.setattr(cloudflare.requests, 'request', fake.request)
    for name in conventions.ALL_ZONES:
        _ = fake.add_zone(name)
    monkeypatch.setattr(conventions, 'CLOUDFLARE_ACCOUNT', conventions.CloudflareAccount(account_id=ACCOUNT_ID))
    return fake


@pytest.fixture
def kit(api: FakeApi) -> KdbxStore:
    store = MemoryKit()
    _ = cloudflare.adopt_seed(token=console_seed(api), seeds=store, seed_entry=derived.CLOUDFLARE_SEED_ENTRY)
    return store


def _dns01_live(api: FakeApi) -> list[str]:
    return [str(token['id']) for token in api.tokens.values() if token['name'] == cloudflare.DNS01.name]


def test_the_dns01_token_is_scoped_to_the_served_zones_and_sealed_for_the_issuer(
    api: FakeApi, kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks, kubeseal: str, key_pair: KeyPair
) -> None:
    value = conventions.sealed.DNS01_TOKEN

    token_id = derived.cloudflare_dns01(kit, sealer=sealer)

    # The token is what the issuer's Secret opens to, and it sees exactly the
    # zones the cluster issues a certificate for: the derivation `k8s-base`
    # declares its certificates from, not every zone the installation holds.
    (token,) = unseal(kubeseal, key_pair, value, written(stacks, value)).values()
    assert _dns01_live(api) == [token_id]
    assert api.values[token] == token_id
    scoped = {zone.name for zone in cloudflare.Session.authorize(token).zones()}
    assert scoped == set(conventions.routes.served_zones())
    assert scoped < set(conventions.ALL_ZONES)


def test_a_route_in_another_zone_widens_the_dns01_token_to_it(
    api: FakeApi,
    kit: KdbxStore,
    sealer: sealing.Sealer,
    stacks: Stacks,
    kubeseal: str,
    key_pair: KeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = 'unlimitedcodeworks.xyz'
    monkeypatch.setattr(conventions.routes, 'ROUTES', (conventions.routes.Route(host='www', zones=(second,)),))
    value = conventions.sealed.DNS01_TOKEN

    _ = derived.cloudflare_dns01(kit, sealer=sealer)

    # The census as it stands when the mint runs, so a row added in a new zone
    # is a wider token on the next run of this command.
    (token,) = unseal(kubeseal, key_pair, value, written(stacks, value)).values()
    scoped = {zone.name for zone in cloudflare.Session.authorize(token).zones()}
    assert scoped == {conventions.ZONE_PRIMARY, second}


def test_the_dns01_mint_refuses_a_scope_with_no_zone_before_anything_is_minted(
    api: FakeApi, kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conventions.routes, 'served_zones', lambda: ())

    with pytest.raises(SlotRefused, match='serves no zone'):
        _ = derived.cloudflare_dns01(kit, sealer=sealer)
    assert _dns01_live(api) == []
    assert written(stacks, conventions.sealed.DNS01_TOKEN) == {}


def test_the_dns01_mint_refuses_a_missing_declaring_stack_before_anything_is_minted(
    api: FakeApi, kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks
) -> None:
    stacks.runner(K8S_BASE).stacks.clear()

    with pytest.raises(SlotRefused, match='does not exist'):
        _ = derived.cloudflare_dns01(kit, sealer=sealer)
    assert _dns01_live(api) == []


def test_rotating_the_dns01_token_retires_the_one_it_replaces(
    api: FakeApi, kit: KdbxStore, sealer: sealing.Sealer
) -> None:
    first = derived.cloudflare_dns01(kit, sealer=sealer)

    second = derived.cloudflare_dns01(kit, sealer=sealer)

    assert second != first
    assert _dns01_live(api) == [second]


# -- the rows made by hand ----------------------------------------------------


BGP = devices.DEVICES[devices.BGP]


def test_the_session_password_is_recorded_for_the_gateway_and_sealed_for_the_worker(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair, tmp_path: Path
) -> None:
    secret = tmp_path / 'password'
    _ = secret.write_text('a-drawn-password\n')

    _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=sealer)

    # One value, both ends of the session: a config secret of the stack that
    # writes the gateway's end, and the worker's end sealed for Cilium.
    assert stacks.runner(PHYSICAL).config == {'gatewayBgpPassword': 'a-drawn-password'}
    value = conventions.sealed.BGP_PASSWORD
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {'password': 'a-drawn-password'}


def test_a_password_recorded_before_the_cluster_exists_says_where_its_sealed_copy_comes_from(
    stacks: Stacks, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = tmp_path / 'password'
    _ = secret.write_text('a-drawn-password\n')

    with caplog.at_level(logging.WARNING):
        _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=None)

    assert stacks.runner(PHYSICAL).config == {'gatewayBgpPassword': 'a-drawn-password'}
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {}
    assert f'credentials derived {devices.BGP} seal' in caplog.text


def test_a_seal_that_fails_leaves_the_gateways_end_unwritten(stacks: Stacks, tmp_path: Path) -> None:
    secret = tmp_path / 'password'
    _ = secret.write_text('a-drawn-password\n')

    def refusing(args: Sequence[str], *, stdin: str | None) -> str:
        _ = args, stdin
        raise SlotRefused('kubeseal refused: a certificate it cannot use')

    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=refusing)

    with pytest.raises(SlotRefused):
        _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=sealer)
    # The two ends are one value: a gateway configured with a password the
    # worker never received is a session down until somebody notices.
    assert stacks.runner(PHYSICAL).config == {}


def test_a_record_whose_declaring_stack_does_not_exist_writes_neither_end(
    stacks: Stacks, sealer: sealing.Sealer, tmp_path: Path
) -> None:
    secret = tmp_path / 'password'
    _ = secret.write_text('a-drawn-password\n')
    stacks.runner(K8S_BASE).stacks.clear()

    with pytest.raises(SlotRefused, match='does not exist'):
        _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=sealer)
    # Refused before the gateway's end is written, for the reason a refused
    # seal is: the two ends are one value.
    assert stacks.runner(PHYSICAL).config == {}


def test_seal_writes_the_worker_s_copy_of_what_the_gateway_s_stack_holds(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair
) -> None:
    stacks.runner(PHYSICAL).config['gatewayBgpPassword'] = 'the-recorded-password'

    devices.seal(BGP, stack=stacks.open(BGP.stack), sealer=sealer)

    value = conventions.sealed.BGP_PASSWORD
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {'password': 'the-recorded-password'}


def test_seal_before_the_password_is_recorded_names_the_command_that_records_it(
    stacks: Stacks, sealer: sealing.Sealer
) -> None:
    with pytest.raises(SlotRefused, match=f'credentials derived {devices.BGP} record'):
        devices.seal(BGP, stack=stacks.open(BGP.stack), sealer=sealer)
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {}


def test_alertmanagers_webhook_is_sealed_and_written_nowhere_else(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair, tmp_path: Path
) -> None:
    record = devices.SEALED_RECORDS['alert-webhook']
    url = tmp_path / 'url'
    _ = url.write_text('https://home.example/api/webhook/an-id\n')

    devices.record_sealed(record, sealer=sealer, given={'url': str(url)})

    value = conventions.sealed.ALERT_WEBHOOK
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {
        'url': 'https://home.example/api/webhook/an-id'
    }
    assert all(not runner.config for runner in stacks.runners.values())
