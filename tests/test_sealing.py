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
sees it; the OCI mint, against the fake tenancy the OCI rows use
(`oci_tenancy`), so the sealed key is held to the tenancy that accepts it.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import io
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import b2_api
import oci_clock
import pinned_tools
import process_sessions
import pytest
import yaml
from cloudflare_api import ACCOUNT_ID, FakeApi, console_seed
from credentials_command_tree import named_leaves
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from fake_gh import RecordedGh
from fake_kubeseal import sealed_as_marked
from fake_pulumi import RecordedPulumi, Unanswering
from k8s_base_installation import SEALED, applied
from memory_kit import MemoryKit
from oci_conventions import with_recorded_compartment, with_tenancy_ocid
from oci_tenancy import ROOT_USER, TENANCY, Tenancy
from pulumi.runtime import rpc
from scratch_projects import cli_directories, scrubbed

from kluster import conventions
from kluster.conventions.backup import BACKED_NAMESPACES, ETCD_SCOPE, writer_scope
from kluster.lib import bundle, pulumi_cli
from kluster.lib.k8s import rendered_template
from kluster.lib.state_backend import settings
from kluster.scripts.credentials import (
    age,
    b2,
    cli,
    cloudflare,
    derived,
    devices,
    escrow,
    masters,
    oci_iam,
    pki,
    pulumi_config,
    sealing,
    workstation,
)
from kluster.scripts.credentials.github_secrets import Forge
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.stacks import physical

#: Bounds one `kubeseal` call: every call here is local.
TIMEOUT = 30

#: The type token the CRD SDK declares a SealedSecret under.
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'

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
    return pinned_tools.located(sealing.KUBESEAL)


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
    rendered: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Open `ciphertexts` as the controller would, as the SealedSecret `value` declares, or under another identity.

    The manifest carries the scope annotation `sealed_secret` writes for the
    row's scope, so what is opened is the object the stack will declare, and
    `rendered` as `sealed_value` turns it into the template's data.
    """
    name = name or value.name
    namespace = namespace or value.namespace
    annotations = {f'sealedsecrets.bitnami.com/{value.scope.value}': 'true'}
    metadata = {'name': name, 'namespace': namespace, 'annotations': annotations}
    template: dict[str, object] = {'metadata': metadata}
    if rendered is not None:
        template['data'] = rendered_template(value, rendered)
    return opened(
        kubeseal,
        key_pair,
        {'metadata': metadata, 'spec': {'encryptedData': dict(ciphertexts), 'template': template}},
    )


def opened(kubeseal: str, key_pair: KeyPair, declared: Mapping[str, object]) -> dict[str, str]:
    """The data of the Secret the controller makes of a SealedSecret whose `metadata` and `spec` are `declared`'s.

    `kubeseal --recovery-unseal` runs the controller's own `Unseal`, which
    decrypts each key and renders `spec.template.data` around the decrypted
    values, so the pinned tool -- the controller's own release
    (`sealing.KUBESEAL`) -- says what the cluster's Secret holds.
    """
    manifest = {
        'apiVersion': 'bitnami.com/v1alpha1',
        'kind': 'SealedSecret',
        'metadata': declared['metadata'],
        'spec': declared['spec'],
    }
    proc = process_sessions.run(
        [kubeseal, '--recovery-unseal', '--recovery-private-key', str(key_pair.private_key), '-o', 'json'],
        input=json.dumps(manifest),
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
            passphrase=lambda: 'the-stack-passphrase', physical=lambda: 'the-physical-passphrase'
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


#: Every value but the backed namespaces' writer keys, which open under any
#: name in their namespace (below); the etcd snapshots' writer key is strict.
STRICT = {
    name: value
    for name, value in conventions.sealed.VALUES.items()
    if value not in conventions.sealed.NAMESPACE_WRITER_KEYS
}
NAMESPACE_WRITER_KEYS = {value.name: value for value in conventions.sealed.NAMESPACE_WRITER_KEYS}


@pytest.mark.parametrize('value', STRICT.values(), ids=STRICT)
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


@pytest.mark.parametrize('value', NAMESPACE_WRITER_KEYS.values(), ids=NAMESPACE_WRITER_KEYS)
def test_a_writer_key_opens_under_any_name_in_its_namespace_and_in_no_other_namespace(
    value: conventions.sealed.SealedValue, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair
) -> None:
    data = {key: f'the {key} of {value.name}' for key in value.keys}

    ciphertexts = sealer.seal(value, data)

    # Every repository Secret of the namespace carries the one ciphertext
    # under the claim's own name, which no seal could have known.
    assert unseal(kubeseal, key_pair, value, ciphertexts, name='a-claim') == data
    assert unseal(kubeseal, key_pair, value, ciphertexts, name='another-claim') == data
    with pytest.raises(Unopened):
        _ = unseal(kubeseal, key_pair, value, ciphertexts, name='a-claim', namespace=f'{value.namespace}-other')


def test_a_seal_refuses_data_whose_keys_are_not_the_rows(sealer: sealing.Sealer) -> None:
    value = conventions.sealed.DNS01_TOKEN

    with pytest.raises(SlotRefused, match=value.name):
        _ = sealer.seal(value, {f'not-{key}': 'a-value' for key in value.keys})


# -- writing ------------------------------------------------------------------


def test_the_ciphertext_is_written_in_the_clear_at_its_rows_path_in_its_rows_stack(stacks: Stacks) -> None:
    value = conventions.sealed.BGP_PASSWORD
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)

    sealer.deliver(value, {'password': 'a-password'})

    # The program reads the row's path out of the row's stack, without
    # decrypting it: anywhere else is a value nothing reads, and stored as a
    # secret it is a second layer of encryption for no reader.
    runner = stacks.runner(value.stack)
    assert runner.plain == {value.path('password'): 'AgB-sealed-a-password'}
    assert runner.config == {}
    assert not [args for args in runner.invocations if '--secret' in args]
    assert stacks.runner(PHYSICAL).plain == {}


@pytest.mark.parametrize(
    ('misreports', 'read_back'),
    [('secret', pulumi_config.ReadBack.AS_SECRET), ('value', pulumi_config.ReadBack.DIFFERENT)],
    ids=['stored as a secret', 'another value'],
)
def test_a_write_that_does_not_read_back_as_written_and_as_plain_is_refused(
    stacks: Stacks, misreports: Literal['secret', 'value'], read_back: pulumi_config.ReadBack
) -> None:
    # The write's own check: a sealed value is read without decryption, so one
    # stored as a secret is in the wrong channel even though it reads back
    # equal, and one that reads back as anything else was not delivered.
    stacks.runner(K8S_BASE).misreports = misreports
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)

    with pytest.raises(pulumi_config.ReadBackRefused) as refused:
        sealer.deliver(conventions.sealed.BGP_PASSWORD, {'password': 'a-password'})
    assert refused.value.read_back is read_back
    assert refused.value.stack == K8S_BASE


def test_a_seal_into_a_stack_that_does_not_exist_is_refused_before_anything_is_sealed(stacks: Stacks) -> None:
    sealed: list[str] = []

    def recording(args: Sequence[str], *, stdin: str | None) -> str:
        sealed.append(' '.join(args))
        return sealed_as_marked(args, stdin=stdin)

    stacks.runner(K8S_BASE).stacks.clear()
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=recording)

    with pytest.raises(pulumi_config.StackMissing) as refused:
        _ = sealer.stack(conventions.sealed.DNS01_TOKEN)
    assert refused.value.stack == K8S_BASE
    assert stacks.runner(K8S_BASE).stacks == []
    assert sealed == []


#: What `kubeseal --raw` prints, in shape: base64 long enough that the CLI takes it for a secret.
SHAPED_LIKE_A_SEAL = 'AgB' + 'A' * 96


def shaped_like_kubeseal(args: Sequence[str], *, stdin: str | None) -> str:
    _ = args, stdin
    return SHAPED_LIKE_A_SEAL


#: The bound the real-CLI case below runs under. Each `pulumi` command it
#: starts goes through `pulumi_cli.run_pulumi`, bounded by `pulumi_cli.TIMEOUT`,
#: so the case's own bound sits above that rather than at the suite's, and a
#: stalled command is refused by the runner, naming it and that bound
#: (testing.md §8). Each row took 0.4 to 0.5 s
#: under the suite's four workers, and 2.5 s on a loaded machine. A
#: stop-loss; nothing asserts on elapsed time.
REAL_CLI_CASE_TIMEOUT = 2 * pulumi_cli.TIMEOUT


@pytest.mark.timeout(REAL_CLI_CASE_TIMEOUT)
@pytest.mark.parametrize('value', conventions.sealed.VALUES.values(), ids=conventions.sealed.VALUES)
def test_the_write_is_held_to_what_the_real_cli_accepts(
    value: conventions.sealed.SealedValue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row lands in the clear under `sealedSecrets`, by its name and then by its data key, as the CLI stores it.

    Read back as the program reads it, the whole structured key at once, so a
    path the CLI splits somewhere the row did not mean -- a data key with a
    dot in it -- is a value the program cannot find, whatever the path's own
    read-back says. `pulumi config set` also refuses a value that looks like a
    secret unless told which channel it takes.
    """
    pinned_tools.require('pulumi')
    project = tmp_path / 'project'
    project.mkdir()
    _ = (project / 'Pulumi.yaml').write_text('name: sealing-probe\nruntime: python\n')
    state = tmp_path / 'state'
    state.mkdir()
    # The CLI's environment is the stack's overlaid on the caller's, so the
    # run's own `PULUMI_`/`PG` variables are taken out of the caller's first:
    # the CLI sees only what this case sets (testing.md §8).
    for name in set(os.environ) - set(scrubbed(os.environ)):
        monkeypatch.delenv(name)
    for variable, directory in cli_directories(tmp_path).items():
        monkeypatch.setenv(variable, directory)
    monkeypatch.setenv('PULUMI_SKIP_UPDATE_CHECK', 'true')
    environment = pulumi_config.BackendEnvironment(
        passphrase=lambda: 'probe-passphrase', url=state.as_uri(), physical=lambda: 'probe-physical-passphrase'
    )
    stack = pulumi_config.Stack(name=value.stack, directory=project, environment=environment)
    stack.ensure()
    sealer = sealing.Sealer(certificate='', open_stack=lambda _name: stack, run=shaped_like_kubeseal)

    sealer.deliver(value, dict.fromkeys(value.keys, 'a-value'))

    committed = (project / f'Pulumi.{value.stack}.yaml').read_text()
    assert f'sealing-probe:{conventions.sealed.CONFIG_KEY}:' in committed
    assert 'secure:' not in committed
    printed = json.loads(
        pulumi_config.run_pulumi(
            ['config', 'get', conventions.sealed.CONFIG_KEY, '--json', '--stack', value.stack],
            cwd=project,
            env=stack.env,
            stdin=None,
        )
    )
    held = json.loads(printed['value'])
    assert held == {value.name: dict.fromkeys(value.keys, SHAPED_LIKE_A_SEAL)}


# -- the cluster --------------------------------------------------------------


def test_a_physical_stack_that_exports_no_kubeconfig_has_no_cluster(stacks: Stacks) -> None:
    with pytest.raises(sealing.NoCluster):
        _ = sealing.cluster_kubeconfig(stacks.open(PHYSICAL))

    stacks.runner(PHYSICAL).stacks.clear()
    with pytest.raises(sealing.NoCluster):
        _ = sealing.cluster_kubeconfig(stacks.open(PHYSICAL))


def test_a_kubeconfig_left_as_the_unknown_sentinel_is_refused_by_name(stacks: Stacks) -> None:
    stacks.runner(PHYSICAL).outputs[conventions.PHYSICAL_OUTPUTS.kubeconfig] = rpc.UNKNOWN

    with pytest.raises(pulumi_config.UnknownOutput) as refused:
        _ = sealing.cluster_kubeconfig(stacks.open(PHYSICAL))
    assert not isinstance(refused.value, sealing.NoCluster)
    assert (refused.value.stack, refused.value.output) == (PHYSICAL, conventions.PHYSICAL_OUTPUTS.kubeconfig)


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


def _public_route(zone: str) -> conventions.routes.Exposure:
    """An exposure of one public name, in one zone."""
    return conventions.routes.Exposure(
        routes=(conventions.routes.Route(host='www', internet=conventions.routes.PROXIED, zones=(zone,)),)
    )


def test_the_dns01_token_is_scoped_to_the_served_zones_and_sealed_for_the_issuer(
    api: FakeApi,
    kit: KdbxStore,
    sealer: sealing.Sealer,
    stacks: Stacks,
    kubeseal: str,
    key_pair: KeyPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One route, in one zone of several: the census this case reads is the
    # one it sets up, so a route added elsewhere is not its premise.
    monkeypatch.setattr(conventions.routes, 'EXPOSURES', {'www': _public_route(conventions.ZONE_PRIMARY)})
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
    second = next(zone for zone in conventions.ALL_ZONES if zone != conventions.ZONE_PRIMARY)
    monkeypatch.setattr(conventions.routes, 'EXPOSURES', {'www': _public_route(second)})
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

    with pytest.raises(derived.NoZoneServed):
        _ = derived.cloudflare_dns01(kit, sealer=sealer)
    assert _dns01_live(api) == []
    assert written(stacks, conventions.sealed.DNS01_TOKEN) == {}


def test_the_dns01_mint_refuses_a_missing_declaring_stack_before_anything_is_minted(
    api: FakeApi, kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks
) -> None:
    stacks.runner(K8S_BASE).stacks.clear()

    with pytest.raises(pulumi_config.StackMissing) as refused:
        _ = derived.cloudflare_dns01(kit, sealer=sealer)
    assert refused.value.stack == K8S_BASE
    assert _dns01_live(api) == []


def test_rotating_the_dns01_token_retires_the_one_it_replaces(
    api: FakeApi, kit: KdbxStore, sealer: sealing.Sealer
) -> None:
    first = derived.cloudflare_dns01(kit, sealer=sealer)

    second = derived.cloudflare_dns01(kit, sealer=sealer)

    assert second != first
    assert _dns01_live(api) == [second]


# -- the metrics reader's mint ------------------------------------------------


@pytest.fixture
def tenancy(monkeypatch: pytest.MonkeyPatch) -> Tenancy:
    """The fake account as the one `conventions` records, with `physical`'s compartment recorded and held.

    The state an installation is in by the time anything is sealed: `physical`
    has been minted for, so its compartment exists under the OCID the census
    records. The waits for a key to propagate run on the simulated clock.
    """
    _ = oci_clock.install(monkeypatch)
    with_tenancy_ocid(monkeypatch, TENANCY)
    held = Tenancy()
    held.identity.hold(with_recorded_compartment(monkeypatch, conventions.PHYSICAL, 'ocid1.compartment.oc1..physical'))
    return held


@pytest.fixture
def oci_kit(tenancy: Tenancy) -> KdbxStore:
    """A kit holding the OCI seed, created the way a bring-up creates it."""
    store = MemoryKit()
    root = masters.Credential(
        root=masters.ROOTS[masters.OCI],
        values={
            masters.OCI_TENANCY: TENANCY,
            masters.OCI_USER: ROOT_USER,
            masters.OCI_PRIVATE_KEY: oci_iam.generate_key().private_pem,
        },
    )
    _ = oci_iam.create_seed(root=root, seeds=store, seed_entry=derived.OCI_SEED_ENTRY, connect=tenancy)
    return store


def _reader_keys(tenancy: Tenancy) -> list[str]:
    """The fingerprints the reader's user holds, or none while there is no such user."""
    name = oci_iam.Identity.name_for(oci_iam.METRICS_READER)
    users = [user.id for user in tenancy.identity.users.values() if user.name == name]
    return [key for user in users for key in tenancy.identity.keys.get(user, [])]


def test_the_metrics_reader_is_sealed_for_the_pull_in_monitoring_and_signs_as_the_reader(
    tenancy: Tenancy, oci_kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks, kubeseal: str, key_pair: KeyPair
) -> None:
    value = conventions.sealed.OCI_METRICS

    user = derived.oci_metrics(oci_kit, sealer=sealer, connect=tenancy)

    # Written into `k8s-base`'s configuration, the stack that declares the
    # monitoring namespace's Secrets, and opening there under the row's name:
    # what comes out is a signing configuration the tenancy accepts, as the
    # reader's own user, with the fingerprint of the key beside it.
    assert written(stacks, value).keys() == set(value.keys)
    opened = unseal(kubeseal, key_pair, value, written(stacks, value), namespace=conventions.MONITORING_NAMESPACE)
    assert opened['user'] == user
    assert opened['fingerprint'] == oci_iam.fingerprint(opened['key_content'])
    assert _reader_keys(tenancy) == [opened['fingerprint']]
    session = oci_iam.Iam.authorize(TENANCY, opened['user'], opened['key_content'], connect=tenancy)
    assert session.key_fingerprints(opened['user']) == [opened['fingerprint']]


def test_the_metrics_reader_mint_refuses_a_missing_declaring_stack_before_anything_is_minted(
    tenancy: Tenancy, oci_kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks
) -> None:
    stacks.runner(K8S_BASE).stacks.clear()

    with pytest.raises(pulumi_config.StackMissing) as refused:
        _ = derived.oci_metrics(oci_kit, sealer=sealer, connect=tenancy)
    assert refused.value.stack == K8S_BASE
    assert _reader_keys(tenancy) == []


def test_a_metrics_reader_seal_that_fails_leaves_the_key_already_sealed_live(
    tenancy: Tenancy, oci_kit: KdbxStore, stacks: Stacks
) -> None:
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)
    _ = derived.oci_metrics(oci_kit, sealer=sealer, connect=tenancy)
    (live,) = _reader_keys(tenancy)
    stacks.runner(K8S_BASE).misreports = 'value'

    with pytest.raises(pulumi_config.ReadBackRefused):
        _ = derived.oci_metrics(oci_kit, sealer=sealer, connect=tenancy)

    # The key the cluster holds is retired only once its successor is written,
    # so a write that does not take leaves it standing beside the new one.
    held = _reader_keys(tenancy)
    assert live in held
    assert len(held) == 2


def test_rotating_the_metrics_reader_retires_the_key_it_replaces(
    tenancy: Tenancy, oci_kit: KdbxStore, stacks: Stacks
) -> None:
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)
    _ = derived.oci_metrics(oci_kit, sealer=sealer, connect=tenancy)
    (first,) = _reader_keys(tenancy)

    _ = derived.oci_metrics(oci_kit, sealer=sealer, connect=tenancy)

    (second,) = _reader_keys(tenancy)
    assert second != first


# -- the dump freshness key's mint ----------------------------------------------

FRESHNESS = conventions.sealed.B2_FRESHNESS_DUMPS


@pytest.fixture
def b2_account(monkeypatch: pytest.MonkeyPatch) -> b2_api.FakeApi:
    """The fake B2 account as the one `conventions` records, holding the bucket the appliance dumps into.

    The state an installation is in by the time the key is minted: the
    `state-backend` stack has declared its bucket, which the mint looks up.
    """
    api = b2_api.FakeApi()
    monkeypatch.setattr(b2.requests, 'get', api.get)
    monkeypatch.setattr(b2.requests, 'post', api.post)
    monkeypatch.setattr(
        conventions,
        'B2_ACCOUNT',
        conventions.B2Account(region=conventions.B2_ACCOUNT.region, account_id=b2_api.ACCOUNT_ID),
    )
    api.buckets['bucket-dumps'] = {
        'bucketId': 'bucket-dumps',
        'bucketName': settings.B2_BUCKET,
        'bucketType': 'allPrivate',
        'lifecycleRules': [],
    }
    return api


@pytest.fixture
def b2_kit(b2_account: b2_api.FakeApi) -> KdbxStore:
    """A kit holding the B2 seed, created the way a bring-up creates it."""
    store = MemoryKit()
    root = masters.Credential(
        root=masters.ROOTS['b2'], values={'account-id': b2_account.master.key_id, 'key': b2_account.master.secret}
    )
    _ = b2.create_seed(root=root, seeds=store, seed_entry=derived.B2_SEED_ENTRY)
    return store


def _forge() -> Forge:
    return Forge(token='admin-token', run=RecordedGh())


def test_the_freshness_key_is_sealed_for_the_check_in_monitoring_as_the_minted_key(
    b2_account: b2_api.FakeApi,
    b2_kit: KdbxStore,
    sealer: sealing.Sealer,
    stacks: Stacks,
    kubeseal: str,
    key_pair: KeyPair,
) -> None:
    key_id = derived.b2_freshness_dumps(b2_kit, _forge(), sealer=sealer)

    # Written into `k8s-base`'s configuration, the stack that declares the
    # monitoring namespace's Secrets, and opening there under the row's name
    # as the key the account now lists under the role: its two halves under
    # the fields the bucket scripts authorize a key from.
    assert written(stacks, FRESHNESS).keys() == set(FRESHNESS.keys)
    opened = unseal(
        kubeseal, key_pair, FRESHNESS, written(stacks, FRESHNESS), namespace=conventions.MONITORING_NAMESPACE
    )
    assert opened == {'AWS_ACCESS_KEY_ID': key_id, 'AWS_SECRET_ACCESS_KEY': b2_account.keys[key_id].secret}
    assert b2_account.named(b2.FRESHNESS_DUMPS_NAME) == [key_id]


def test_the_freshness_mint_refuses_a_missing_declaring_stack_before_anything_is_minted(
    b2_account: b2_api.FakeApi, b2_kit: KdbxStore, sealer: sealing.Sealer, stacks: Stacks
) -> None:
    stacks.runner(K8S_BASE).stacks.clear()
    gh = RecordedGh()

    with pytest.raises(pulumi_config.StackMissing) as refused:
        _ = derived.b2_freshness_dumps(b2_kit, Forge(token='admin-token', run=gh), sealer=sealer)
    assert refused.value.stack == K8S_BASE
    assert b2_account.named(b2.FRESHNESS_DUMPS_NAME) == []
    assert gh.invocations == []


def test_a_freshness_write_that_fails_leaves_the_key_both_consumers_hold_live(
    b2_account: b2_api.FakeApi, b2_kit: KdbxStore, stacks: Stacks
) -> None:
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)
    live = derived.b2_freshness_dumps(b2_kit, _forge(), sealer=sealer)
    stacks.runner(K8S_BASE).misreports = 'value'

    with pytest.raises(pulumi_config.ReadBackRefused):
        _ = derived.b2_freshness_dumps(b2_kit, _forge(), sealer=sealer)

    # The ops repository took the new key, the stack did not: the key both
    # consumers held is retired only once every target holds its successor,
    # so it stands beside the new one.
    held = b2_account.named(b2.FRESHNESS_DUMPS_NAME)
    assert live in held
    assert len(held) == 2


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
    assert stacks.runner(PHYSICAL).config == {physical.GATEWAY_BGP_PASSWORD: 'a-drawn-password'}
    value = conventions.sealed.BGP_PASSWORD
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {'password': 'a-drawn-password'}


def test_a_password_recorded_before_the_cluster_exists_says_where_its_sealed_copy_comes_from(
    stacks: Stacks, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = tmp_path / 'password'
    _ = secret.write_text('a-drawn-password\n')

    with caplog.at_level(logging.WARNING):
        _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=None)

    assert stacks.runner(PHYSICAL).config == {physical.GATEWAY_BGP_PASSWORD: 'a-drawn-password'}
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {}
    assert ('derived', devices.BGP, 'seal') in named_leaves(caplog.text)


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

    with pytest.raises(pulumi_config.StackMissing) as refused:
        _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=sealer)
    assert refused.value.stack == K8S_BASE
    # Refused before the gateway's end is written, for the reason a refused
    # seal is: the two ends are one value.
    assert stacks.runner(PHYSICAL).config == {}


def test_a_password_recorded_before_physical_exists_creates_it_and_writes_the_sealed_copy(
    stacks: Stacks, tmp_path: Path
) -> None:
    """The declaring stack is held to exist before the console steps; the row's own is not, and its fill creates it."""
    secret = tmp_path / 'password'
    _ = secret.write_text('a-drawn-password\n')
    stacks.runner(PHYSICAL).stacks.clear()
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)

    _ = devices.deliver(BGP, stack=stacks.open(BGP.stack), given={'password': str(secret)}, sealer=sealer)

    assert stacks.runner(PHYSICAL).stacks == [PHYSICAL]
    assert stacks.runner(PHYSICAL).config == {physical.GATEWAY_BGP_PASSWORD: 'a-drawn-password'}
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {'password': 'AgB-sealed-a-drawn-password'}


def test_seal_writes_the_worker_s_copy_of_what_the_gateway_s_stack_holds(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair
) -> None:
    stacks.runner(PHYSICAL).config[physical.GATEWAY_BGP_PASSWORD] = 'the-recorded-password'

    devices.seal(BGP, stack=stacks.open(BGP.stack), sealer=sealer)

    value = conventions.sealed.BGP_PASSWORD
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {'password': 'the-recorded-password'}


def test_seal_before_the_password_is_recorded_names_the_command_that_records_it(
    stacks: Stacks, sealer: sealing.Sealer
) -> None:
    with pytest.raises(devices.RecordMissing) as refused:
        devices.seal(BGP, stack=stacks.open(BGP.stack), sealer=sealer)
    assert refused.value.missing is devices.Missing.ABSENT
    assert refused.value.record in named_leaves(str(refused.value))
    assert ('derived', devices.BGP, 'record') in named_leaves(str(refused.value))
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {}


def test_seal_before_the_declaring_stack_exists_is_refused_and_writes_nothing(stacks: Stacks) -> None:
    stacks.runner(PHYSICAL).config[physical.GATEWAY_BGP_PASSWORD] = 'the-recorded-password'
    stacks.runner(K8S_BASE).stacks.clear()
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)

    with pytest.raises(pulumi_config.StackMissing) as refused:
        devices.seal(BGP, stack=stacks.open(BGP.stack), sealer=sealer)

    assert refused.value.stack == K8S_BASE
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {}


def unopened(name: str) -> pulumi_config.Stack:
    """A sealer's way to its declaring stack that must never be taken: nothing was read to write there."""
    raise AssertionError(f'a seal whose value was never read opened the {name} stack to write it')


def unsealed(args: Sequence[str], *, stdin: str | None) -> str:
    """A `kubeseal` that must never be run: nothing was read to seal."""
    raise AssertionError(f'a seal whose value was never read ran kubeseal {" ".join(args)}')


#: The sealer the cases about a refused read hand `seal`: reaching it at all
#: fails the case, so a read that went through cannot pass for one refused.
UNREACHED = sealing.Sealer(certificate='', open_stack=unopened, run=unsealed)


def test_seal_against_a_state_backend_that_does_not_answer_is_not_told_the_password_is_missing() -> None:
    """A backend that does not answer has said nothing about the password, so the read's own refusal travels.

    Told it is missing, the operator would draw and record a new password
    while the gateway still holds the old one; what they need is the backend,
    which the read's refusal names.
    """
    runner = Unanswering()
    slot = pulumi_config.Stack(
        name=BGP.stack,
        directory=pulumi_config.project_dir(),
        environment=pulumi_config.BackendEnvironment(physical=lambda: 'the-physical-passphrase'),
        run=runner,
    )

    with pytest.raises(SlotRefused) as refused:
        devices.seal(BGP, stack=slot, sealer=UNREACHED)

    assert not isinstance(refused.value, devices.RecordMissing)
    assert refused.value is runner.refusals[0]


def test_seal_of_a_password_the_stack_holds_but_cannot_be_read_is_not_told_it_is_missing(stacks: Stacks) -> None:
    """The stack lists the key, so whatever the read was refused for, the password is not absent."""
    stacks.runner(PHYSICAL).undecryptable.add(physical.GATEWAY_BGP_PASSWORD)

    with pytest.raises(SlotRefused) as refused:
        devices.seal(BGP, stack=stacks.open(BGP.stack), sealer=UNREACHED)

    assert not isinstance(refused.value, devices.RecordMissing)
    assert written(stacks, conventions.sealed.BGP_PASSWORD) == {}


(WEBHOOK,) = [row for row in devices.SEALED_RECORDS.values() if row.sealed == conventions.sealed.ALERT_WEBHOOK]

#: A webhook id the shape Home Assistant's automation editor draws one in: the
#: automation's slug, a hyphen, and 24 characters of URL-safe base64.
WEBHOOK_ID = 'alertmanager-intake-q2_Zx-9VbN4kLmP0aRtYw7Hs'

#: Where Home Assistant takes a webhook's `POST`, before the id
#: (`URL_WEBHOOK_PATH` in its `webhook` component).
WEBHOOK_PATH = '/api/webhook/'


def _webhook_id_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / 'webhook-id'
    _ = path.write_text(f'{content}\n')
    return path


def test_alertmanagers_webhook_id_is_sealed_and_written_nowhere_else(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair, tmp_path: Path
) -> None:
    devices.record_sealed(WEBHOOK, sealer=sealer, given={'webhook-id': str(_webhook_id_file(tmp_path, WEBHOOK_ID))})

    value = conventions.sealed.ALERT_WEBHOOK
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {'id': WEBHOOK_ID}
    assert all(not runner.config for runner in stacks.runners.values())


@pytest.mark.parametrize(
    ('pasted', 'url'),
    [
        (f'http://{conventions.HOME_ASSISTANT.v4}:{conventions.HOME_ASSISTANT_PORT}{WEBHOOK_PATH}{WEBHOOK_ID}', True),
        (f'{WEBHOOK_PATH}{WEBHOOK_ID}', True),
        (f'{WEBHOOK_ID} {WEBHOOK_ID}', False),
    ],
    ids=['whole-url', 'path-and-id', 'two-words'],
)
def test_a_value_that_is_not_a_webhook_id_is_refused_before_anything_is_sealed(
    pasted: str,
    url: bool,
    stacks: Stacks,
    recorded: tuple[sealing.Sealer, RecordedKubeseal],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A URL pasted where the id belongs is refused and named as one, and the refusal never prints what was pasted.

    Sealed, the URL would land in the path of the URL the cluster composes
    around it, and every alert would post to a webhook that does not exist.
    """
    sealer, run = recorded

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(devices.NotAWebhookId) as refused,
    ):
        devices.record_sealed(WEBHOOK, sealer=sealer, given={'webhook-id': str(_webhook_id_file(tmp_path, pasted))})

    assert refused.value.url is url
    assert not run.seals()
    assert written(stacks, conventions.sealed.ALERT_WEBHOOK) == {}
    assert WEBHOOK_ID not in str(refused.value)
    assert WEBHOOK_ID not in caplog.text


@pytest.mark.asyncio
async def test_the_recorded_webhook_id_opens_as_the_url_at_home_assistants_address_wherever_that_is(
    stacks: Stacks,
    sealer: sealing.Sealer,
    kubeseal: str,
    key_pair: KeyPair,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded once, the id opens in the cluster as the URL at the address `HOME_ASSISTANT` holds, whatever it holds.

    The SealedSecret the `k8s-base` program declares from the recorded
    ciphertext is opened as the controller opens it. Moving Home Assistant
    moves the URL the Secret holds and leaves the ciphertext as it was: the
    address is the program's, and only the id is sealed.
    """
    devices.record_sealed(WEBHOOK, sealer=sealer, given={'webhook-id': str(_webhook_id_file(tmp_path, WEBHOOK_ID))})
    value = conventions.sealed.ALERT_WEBHOOK
    ciphertexts = written(stacks, value)
    here = conventions.HOME_ASSISTANT
    moved = dataclasses.replace(here, v4=next(host for host in here.network.v4.hosts() if host != here.v4))

    for host in (here, moved):
        monkeypatch.setattr(conventions, 'HOME_ASSISTANT', host)
        run = await applied(sealed={**SEALED, value.name: ciphertexts})
        (declared,) = [
            it.inputs for it in run.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == value.name
        ]

        assert declared['spec']['encryptedData'] == ciphertexts
        assert opened(kubeseal, key_pair, declared) == {
            'id': WEBHOOK_ID,
            'url': f'http://{host.v4}:{conventions.HOME_ASSISTANT_PORT}{WEBHOOK_PATH}{WEBHOOK_ID}',
        }


#: Forms whose literal text holds braces beside the field, as a configuration
#: file would: a lone `{` and a `{{` before it, a shell-style `${...}`, a JSON
#: object, a quote and a backslash, and a character outside the Basic
#: Multilingual Plane, which the literal's escaping has to carry too: JSON's
#: ASCII escape would write it as a surrogate pair, which Go refuses.
BRACED_FORMS = {
    'brace-before': 'x{{{id}',
    'double-brace-before': 'a{{{{b/{id}',
    'shell': '${{{id}}}',
    'json': '{{"token": "{id}"}}',
    'non-bmp': '😀 {{{id}',
    'quote-and-backslash': 'say "\\{{" {id}',
}


@pytest.mark.parametrize('form', BRACED_FORMS.values(), ids=BRACED_FORMS)
def test_a_rendered_form_opens_with_its_literal_braces_as_written(
    form: str, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair
) -> None:
    """The controller renders the literal text of a form as written, beside the decrypted value, whatever braces it holds.

    Opened through the pinned controller's own `Unseal`: copied into the Go
    template unquoted, a brace beside the field's action would be parsed as
    part of it, and the controller would refuse the whole SealedSecret.
    """
    value = conventions.sealed.ALERT_WEBHOOK
    ciphertexts = sealer.seal(value, {'id': WEBHOOK_ID})

    assert unseal(kubeseal, key_pair, value, ciphertexts, rendered={'conf': form}) == {
        'id': WEBHOOK_ID,
        'conf': form.format(id=WEBHOOK_ID),
    }


# -- the mail relay's DKIM key --------------------------------------------------

(DKIM,) = [row for row in devices.SEALED_RECORDS.values() if row.sealed == conventions.sealed.DKIM_EXIM]
APPS = conventions.STACK_NAMES.apps


#: How a PEM private key is encoded: PKCS#1, which cert-manager writes unless a
#: `Certificate` asks otherwise and the legacy one does not, and PKCS#8.
ENCODINGS = {
    'pkcs1': serialization.PrivateFormat.TraditionalOpenSSL,
    'pkcs8': serialization.PrivateFormat.PKCS8,
}


def _dkim_key(encoding: serialization.PrivateFormat = ENCODINGS['pkcs1']) -> tuple[str, str]:
    """A DKIM key pair generated here: the private half as PEM, and the TXT record that would publish it.

    PKCS#1 unless asked otherwise, because that is what the legacy Secret holds.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, encoding, serialization.NoEncryption()).decode()
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, f'v=DKIM1; k=rsa; p={base64.b64encode(der).decode()}'


def _published_as(txt: str) -> devices.SealedRecord:
    """The row as it is, but checked against `txt` rather than the key the mail zones really publish."""
    return dataclasses.replace(DKIM, verify=devices.matches_published_dkim(txt, key='tls.key'))


def _piped(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Standard input as a pipe carrying `text`, the way the command's printed pipeline hands it the key."""
    monkeypatch.setattr('sys.stdin', io.StringIO(text))


def _body(pem: str) -> list[str]:
    """The lines of a PEM that are the key itself: any one of them found anywhere is a leak."""
    return [line for line in pem.splitlines() if line and not line.startswith('-----')]


@dataclass
class RecordedKubeseal:
    """The pinned `kubeseal`, run as the sealer runs it, with every call's arguments and standard input kept."""

    calls: list[tuple[list[str], str | None]] = field(default_factory=list[tuple[list[str], str | None]])

    def __call__(self, args: Sequence[str], *, stdin: str | None) -> str:
        self.calls.append((list(args), stdin))
        return sealing.run_kubeseal(args, stdin=stdin)

    def seals(self) -> list[tuple[list[str], str | None]]:
        """The calls that sealed a value, as opposed to fetching a certificate."""
        return [(args, stdin) for args, stdin in self.calls if '--raw' in args]


@pytest.fixture
def recorded(kubeseal: str, key_pair: KeyPair, stacks: Stacks) -> tuple[sealing.Sealer, RecordedKubeseal]:
    """A sealer over the module's certificate whose every `kubeseal` call is kept."""
    _ = kubeseal
    run = RecordedKubeseal()
    return sealing.Sealer(certificate=key_pair.certificate, open_stack=stacks.open, run=run), run


@pytest.fixture
def apps(stacks: Stacks) -> PathPulumi:
    """`apps` exists, as it does by the time exim's key is carried."""
    runner = stacks.runner(APPS)
    runner.stacks.append(APPS)
    return runner


def test_a_dkim_key_the_mail_zones_do_not_publish_is_refused_before_anything_is_sealed(
    stacks: Stacks,
    recorded: tuple[sealing.Sealer, RecordedKubeseal],
    apps: PathPulumi,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The row as shipped, against `DKIM_K8S`: a key generated here is not the published one."""
    sealer, run = recorded
    pem, _ = _dkim_key()
    key = tmp_path / 'tls.key'
    _ = key.write_text(pem)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(devices.KeyNotPublished) as refused,
    ):
        devices.record_sealed(DKIM, sealer=sealer, given={'key': str(key)})

    assert not run.seals()
    assert written(stacks, conventions.sealed.DKIM_EXIM) == {}
    assert not apps.plain and not apps.config
    assert not any(line in str(refused.value) or line in caplog.text for line in _body(pem))


def test_the_dkim_key_is_written_nowhere_but_its_ciphertext(
    stacks: Stacks,
    recorded: tuple[sealing.Sealer, RecordedKubeseal],
    apps: PathPulumi,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Taken on a pipe, as the printed pipeline hands it over: it leaves only as `kubeseal`'s standard input.

    No log line, no output, no stack value, no argument of any `pulumi` or
    `kubeseal` call holds a line of it, and `kubeseal` reads it from its
    standard input rather than from a file something wrote it into.
    """
    sealer, run = recorded
    pem, txt = _dkim_key()
    _piped(monkeypatch, pem)

    with caplog.at_level(logging.DEBUG):
        devices.record_sealed(_published_as(txt), sealer=sealer, given={'key': None})

    out, err = capsys.readouterr()
    held = [
        *out.splitlines(),
        *err.splitlines(),
        caplog.text,
        *(json.dumps(runner.config) for runner in stacks.runners.values()),
        *(json.dumps(runner.plain) for runner in stacks.runners.values()),
        *(json.dumps(runner.invocations) for runner in stacks.runners.values()),
        *(json.dumps(args) for args, _ in run.calls),
    ]
    assert written(stacks, conventions.sealed.DKIM_EXIM)
    assert not [line for line in _body(pem) if any(line in text for text in held)]
    assert run.seals()
    assert all('--from-file=/dev/stdin' in args and stdin == pem.strip() for args, stdin in run.seals())


@pytest.mark.parametrize('encoding', ENCODINGS.values(), ids=ENCODINGS)
def test_the_dkim_key_is_sealed_at_its_path_for_exims_secret_and_read_back(
    stacks: Stacks,
    sealer: sealing.Sealer,
    kubeseal: str,
    key_pair: KeyPair,
    apps: PathPulumi,
    tmp_path: Path,
    encoding: serialization.PrivateFormat,
) -> None:
    pem, txt = _dkim_key(encoding)
    key = tmp_path / 'tls.key'
    _ = key.write_text(pem)

    devices.record_sealed(_published_as(txt), sealer=sealer, given={'key': str(key)})

    value = conventions.sealed.DKIM_EXIM
    assert set(apps.plain) == {value.path('tls.key')}
    assert unseal(kubeseal, key_pair, value, written(stacks, value)) == {'tls.key': pem.strip()}

    apps.misreports = 'value'
    with pytest.raises(pulumi_config.ReadBackRefused) as refused:
        devices.record_sealed(_published_as(txt), sealer=sealer, given={'key': str(key)})
    assert refused.value.read_back is pulumi_config.ReadBack.DIFFERENT


@pytest.mark.usefixtures('apps')
@pytest.mark.parametrize('given', [None, devices.STDIN], ids=['no flag', '--key-file -'])
def test_a_dkim_key_is_never_taken_at_a_terminal(
    monkeypatch: pytest.MonkeyPatch, sealer: sealing.Sealer, given: str | None
) -> None:
    """A terminal would echo it, and a prompt would cut it at its first line: named as `-` or not named at all."""

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr('sys.stdin', Terminal('-----BEGIN PRIVATE KEY-----\n'))

    with pytest.raises(devices.FieldRefused) as refused:
        devices.record_sealed(DKIM, sealer=sealer, given={'key': given})
    assert refused.value.answer is devices.Answer.AT_TERMINAL


# -- Authelia's storage key ---------------------------------------------------

STORAGE_KEY = conventions.sealed.AUTHELIA_STORAGE_KEY
STORAGE_LABEL = escrow.AUTHELIA_STORAGE_KEY
STORAGE_ROW = derived.AUTHELIA_STORAGE_KEY_ROW


@pytest.fixture
def vault(tmp_path: Path) -> escrow.Vault:
    """An escrow of the case's own, under a recovery key in a kit of the case's own, with the pinned `age`."""
    pinned_tools.require(*pinned_tools.AGE)
    kit, registry = MemoryKit(), escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    return escrow.Vault.open(kit, registry)


def _storage_key(n: int) -> str:
    """A key Authelia would accept, one per `n`, so two generations are told apart."""
    return f'storage-key-{n}-'.ljust(32, 'x')


def test_seal_hands_the_sealer_the_newest_generation_for_the_rows_secret_at_its_path(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair, apps: PathPulumi, vault: escrow.Vault
) -> None:
    """The recovery after a rebuild: what lands is the newest escrowed key, bound to the Secret the server reads.

    Two generations, so a seal of the first is caught: the second is the key
    the database is under. Opened as the controller opens the row's
    SealedSecret, and under no other name, since the row is strict.
    """
    for n in (1, 2):
        _ = escrow.adopt(vault, STORAGE_LABEL, _storage_key(n))

    assert derived.authelia_storage_key_seal(vault, sealer=sealer) == 2

    (key,) = STORAGE_KEY.keys
    assert set(apps.plain) == {STORAGE_KEY.path(key)}
    assert not apps.config
    ciphertexts = written(stacks, STORAGE_KEY)
    assert unseal(kubeseal, key_pair, STORAGE_KEY, ciphertexts) == {key: _storage_key(2)}
    with pytest.raises(Unopened):
        _ = unseal(kubeseal, key_pair, STORAGE_KEY, ciphertexts, name='authelia')


def test_import_seals_the_value_the_registry_returns_not_the_one_it_was_handed(
    stacks: Stacks,
    sealer: sealing.Sealer,
    kubeseal: str,
    key_pair: KeyPair,
    apps: PathPulumi,
    vault: escrow.Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Secret the server reads and the ciphertext a recovery opens are one value.

    A registry that files something other than what it was handed stands in
    for every way the two could part; the sealed value follows the registry.
    """
    adopt = escrow.adopt

    def files_otherwise(vault: escrow.Vault, label: str, secret: str) -> Path:
        return adopt(vault, label, f'{secret}-as-filed')

    monkeypatch.setattr(escrow, 'adopt', files_otherwise)
    _ = apps

    assert derived.authelia_storage_key_import(vault, _storage_key(1), sealer=sealer) == 1

    (key,) = STORAGE_KEY.keys
    sealed = unseal(kubeseal, key_pair, STORAGE_KEY, written(stacks, STORAGE_KEY))
    assert sealed == {key: vault.recover(STORAGE_LABEL)}
    assert sealed[key] != _storage_key(1)


def test_seal_with_nothing_escrowed_refuses_naming_import_and_writes_nothing(
    stacks: Stacks, recorded: tuple[sealing.Sealer, RecordedKubeseal], apps: PathPulumi, vault: escrow.Vault
) -> None:
    sealer, run = recorded

    with pytest.raises(escrow.EscrowRefused) as refused:
        _ = derived.authelia_storage_key_seal(vault, sealer=sealer)

    assert refused.value.refusal is escrow.Refusal.NOTHING_ESCROWED
    assert ('derived', STORAGE_ROW, 'import') in named_leaves(str(refused.value))
    assert not run.seals()
    assert written(stacks, STORAGE_KEY) == {}
    assert not apps.plain and not apps.config


def test_import_of_a_key_authelia_refuses_files_no_generation_and_seals_nothing(
    recorded: tuple[sealing.Sealer, RecordedKubeseal], apps: PathPulumi, vault: escrow.Vault
) -> None:
    sealer, run = recorded

    with pytest.raises(escrow.EscrowRefused) as refused:
        _ = derived.authelia_storage_key_import(vault, 'too-short', sealer=sealer)

    assert refused.value.refusal is escrow.Refusal.NOT_ITS_SHAPE
    assert vault.registry.generations(STORAGE_LABEL) == []
    assert not run.seals()
    assert not apps.plain


def test_import_into_an_apps_stack_that_does_not_exist_escrows_nothing(
    recorded: tuple[sealing.Sealer, RecordedKubeseal], vault: escrow.Vault
) -> None:
    # A key escrowed with nowhere to seal it would be a recovery copy of a
    # value nothing reads, and the re-run would file it a second time.
    sealer, run = recorded

    with pytest.raises(pulumi_config.StackMissing):
        _ = derived.authelia_storage_key_import(vault, _storage_key(1), sealer=sealer)

    assert vault.registry.generations(STORAGE_LABEL) == []
    assert not run.seals()


def test_a_seal_that_fails_after_the_import_sends_the_operator_to_seal_not_to_a_second_import(
    stacks: Stacks, apps: PathPulumi, vault: escrow.Vault
) -> None:
    def refusing(args: Sequence[str], *, stdin: str | None) -> str:
        _ = args, stdin
        raise SlotRefused('kubeseal refused: the controller is restarting')

    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=refusing)

    with pytest.raises(SlotRefused) as refused:
        _ = derived.authelia_storage_key_import(vault, _storage_key(1), sealer=sealer)

    # The key is filed, so the repair is the verb that seals what is filed;
    # a second import would file the same key again as generation 2.
    assert vault.registry.generations(STORAGE_LABEL) == [1]
    assert named_leaves(str(refused.value)) == [('derived', STORAGE_ROW, 'seal')]
    assert not apps.plain


# -- the writer keys ----------------------------------------------------------

BACKUP_KEYS = conventions.PHYSICAL_OUTPUTS.backup_keys

#: Every writer key `physical` mints, by its scope, as the stack's state
#: carries it: each half named after its scope and its half. The etcd
#: snapshots' first, then the census's in its order, each namespace's families
#: sorted, as the seal walks them.
EXPORTED = {
    scope: {'id': f'{scope}-id', 'secret': f'{scope}-secret'}
    for scope in (
        ETCD_SCOPE,
        *(
            writer_scope(namespace, family)
            for namespace, families in BACKED_NAMESPACES.items()
            for family in sorted(families)
        ),
    )
}

#: Every writer key the seal writes, by the scope `physical` exports it under.
WRITER_ROWS = {
    ETCD_SCOPE: conventions.sealed.ETCD_WRITER_KEY,
    **{
        writer_scope(namespace, family): conventions.sealed.writer_key(namespace, family)
        for namespace, families in BACKED_NAMESPACES.items()
        for family in families
    },
}

#: Every stack a writer key is sealed into: the one that declares what reads
#: it, by its row.
DECLARING = sorted({value.stack for value in WRITER_ROWS.values()})


def nothing_written(stacks: Stacks) -> bool:
    """Whether no stack a writer key is sealed into holds anything in the clear."""
    return not any(stacks.runner(stack).plain for stack in DECLARING)


@dataclass
class StatePulumi(PathPulumi):
    """`physical`'s recorded `pulumi`, whose state opens under that stack's own passphrase and no other."""

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        if list(args[:2]) == ['stack', 'output'] and env.get('PULUMI_CONFIG_PASSPHRASE') != 'the-physical-passphrase':
            raise SlotRefused('`pulumi stack output` failed: error: incorrect passphrase')
        return super().__call__(args, cwd=cwd, env=env, stdin=stdin)


@pytest.fixture
def backed(stacks: Stacks) -> Stacks:
    """`apps` exists beside the two, and `physical`'s state exports every writer key the census names."""
    stacks.runners[PHYSICAL] = StatePulumi(stacks=[PHYSICAL], outputs={BACKUP_KEYS: EXPORTED})
    stacks.runner(APPS).stacks.append(APPS)
    return stacks


def test_every_writer_key_is_sealed_out_of_physicals_state_in_the_clear_at_its_rows_path(backed: Stacks) -> None:
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    sealed = derived.b2_writer_seal(sealer=sealer)

    # Read under `physical`'s own passphrase, which the recorded state alone
    # opens, and written as plain values, never as secrets, into the stack each
    # row names: `apps` for an application's namespace, `k8s-base` for the
    # platform's. Each half under the variable a mover reads it from: restic's
    # S3 backend, `vmbackup`'s and `talos-backup`'s, take the key's id as
    # `AWS_ACCESS_KEY_ID` and the key itself as `AWS_SECRET_ACCESS_KEY`.
    expected: dict[str, dict[str, str]] = {}
    for scope, row in WRITER_ROWS.items():
        held = expected.setdefault(row.stack, {})
        held[row.path('AWS_ACCESS_KEY_ID')] = f'AgB-sealed-{scope}-id'
        held[row.path('AWS_SECRET_ACCESS_KEY')] = f'AgB-sealed-{scope}-secret'
    for stack, held in expected.items():
        runner = backed.runner(stack)
        assert runner.plain == held, stack
        assert runner.config == {}, stack
        assert not [args for args in runner.invocations if '--secret' in args], stack
    assert backed.runner(PHYSICAL).plain == {}
    assert sorted(sealed) == sorted(value.name for value in WRITER_ROWS.values())


def test_the_etcd_snapshots_key_is_sealed_alone_into_k8s_base_without_any_other_key_exported(backed: Stacks) -> None:
    """The sitting that seals the cluster's own values seals this key before any namespace backs up.

    `physical` exports nothing else here, so a seal that read every key
    would refuse; and nothing but this key's path is written.
    """
    backed.runner(PHYSICAL).outputs = {BACKUP_KEYS: {ETCD_SCOPE: EXPORTED[ETCD_SCOPE]}}
    row = conventions.sealed.ETCD_WRITER_KEY
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    sealed = derived.b2_writer_seal(sealer=sealer, only=row.name)

    assert sealed == [row.name]
    assert backed.runner(row.stack).plain == {
        row.path('AWS_ACCESS_KEY_ID'): f'AgB-sealed-{ETCD_SCOPE}-id',
        row.path('AWS_SECRET_ACCESS_KEY'): f'AgB-sealed-{ETCD_SCOPE}-secret',
    }
    assert all(not backed.runner(stack).plain for stack in DECLARING if stack != row.stack)


def test_the_etcd_snapshots_key_physical_does_not_export_refuses_its_seal_before_anything_is_written(
    backed: Stacks,
) -> None:
    backed.runner(PHYSICAL).outputs = {
        BACKUP_KEYS: {scope: key for scope, key in EXPORTED.items() if scope != ETCD_SCOPE}
    }
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    with pytest.raises(derived.WriterKeyMissing) as refused:
        _ = derived.b2_writer_seal(sealer=sealer, only=conventions.sealed.ETCD_WRITER_KEY.name)

    assert refused.value.scope == ETCD_SCOPE
    assert nothing_written(backed)


def test_an_only_that_names_no_writer_key_refuses_before_physical_is_read(backed: Stacks) -> None:
    # The alert webhook is a sealed value of `k8s-base`, and no writer key.
    name = conventions.sealed.ALERT_WEBHOOK.name
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    with pytest.raises(derived.UnknownWriterKey) as refused:
        _ = derived.b2_writer_seal(sealer=sealer, only=name)

    assert refused.value.name == name
    assert not [args for args in backed.runner(PHYSICAL).invocations if args[:2] == ['stack', 'output']]
    assert nothing_written(backed)


def test_a_writer_key_physical_does_not_export_refuses_the_seal_before_anything_is_written(backed: Stacks) -> None:
    # The last the census names, so a seal that wrote each key as it read it
    # would have written every other one first.
    missing = list(EXPORTED)[-1]
    backed.runner(PHYSICAL).outputs = {BACKUP_KEYS: {scope: key for scope, key in EXPORTED.items() if scope != missing}}
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    with pytest.raises(derived.WriterKeyMissing) as refused:
        _ = derived.b2_writer_seal(sealer=sealer)

    assert refused.value.scope == missing
    assert nothing_written(backed)


@pytest.mark.parametrize(
    'exported',
    [{'secret': 'a-secret'}, {'id': '', 'secret': 'a-secret'}, {'id': None, 'secret': 'a-secret'}],
    ids=['absent', 'empty', 'not a string'],
)
def test_a_writer_key_half_that_is_not_a_key_half_refuses_the_seal_before_anything_is_written(
    backed: Stacks, exported: dict[str, object]
) -> None:
    # Sealed as it came, a missing half would put the text `None`, or
    # nothing, in a repository Secret, which the first backup finds.
    scope = list(EXPORTED)[-1]
    backed.runner(PHYSICAL).outputs = {BACKUP_KEYS: EXPORTED | {scope: exported}}
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    with pytest.raises(derived.WriterKeyMissing) as refused:
        _ = derived.b2_writer_seal(sealer=sealer)

    assert refused.value.scope == scope
    assert nothing_written(backed)


def test_a_writer_key_left_as_the_unknown_sentinel_is_refused_by_name(backed: Stacks) -> None:
    scope = next(iter(EXPORTED))
    backed.runner(PHYSICAL).outputs = {BACKUP_KEYS: EXPORTED | {scope: {'id': rpc.UNKNOWN, 'secret': rpc.UNKNOWN}}}
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    with pytest.raises(pulumi_config.UnknownOutput) as refused:
        _ = derived.b2_writer_seal(sealer=sealer)

    assert (refused.value.stack, refused.value.output) == (PHYSICAL, BACKUP_KEYS)
    assert nothing_written(backed)


@pytest.mark.parametrize('stack', DECLARING)
def test_a_writer_key_seal_into_a_stack_that_does_not_exist_reads_nothing_of_physical(
    backed: Stacks, stack: str
) -> None:
    backed.runner(stack).stacks.clear()
    sealer = sealing.Sealer(certificate='', open_stack=backed.open, run=sealed_as_marked)

    with pytest.raises(pulumi_config.StackMissing) as refused:
        _ = derived.b2_writer_seal(sealer=sealer)

    assert refused.value.stack == stack
    assert not [args for args in backed.runner(PHYSICAL).invocations if args[:2] == ['stack', 'output']]


@dataclass
class UnderOwnPassphrases:
    """Every stack's recorded `pulumi` behind the one runner a `credentials` command starts.

    A run against a stack is refused with `incorrect passphrase` unless it is
    given that stack's own, as its stack file's salt refuses it. Listing the
    stacks decrypts nothing and is answered under any passphrase.
    """

    runners: dict[str, PathPulumi]
    passphrases: dict[str, str]

    def __call__(
        self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None, timeout: float | None = None
    ) -> str:
        _ = timeout
        if list(args) == ['stack', 'ls', '--json']:
            return json.dumps([{'name': name} for name in self.runners])
        stack = args[list(args).index('--stack') + 1]
        if env.get(pulumi_config.PASSPHRASE_ENV) != self.passphrases[stack]:
            raise pulumi_cli.PulumiRefused(f'`pulumi {" ".join(args)}` failed: error: incorrect passphrase')
        return self.runners[stack](args, cwd=cwd, env=env, stdin=stdin)


def test_the_writer_seal_needs_no_kit_on_a_machine_holding_both_passphrase_slots(
    tmp_path: Path, credential_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`credentials derived b2-writer seal --only writer-etcd`, the command as the operator runs it.

    It reads `physical`'s state under that stack's passphrase and writes into
    `k8s-base` under the stack passphrase, each from its workstation slot.
    The kit is a real one, locked, every prompt fails the case, and the
    escrow holds nothing, so a run that reached for either would stop. Only
    the certificate fetch is stood in for, which reads the kubeconfig as the
    command does and asks `kubeseal` nothing.
    """
    credential_files.mkdir(parents=True)
    _ = (credential_files / workstation.PASSPHRASE).write_text('the-stack-passphrase\n')
    _ = (credential_files / workstation.PHYSICAL_PASSPHRASE).write_text('the-physical-passphrase\n')
    kit = KdbxStore.create(tmp_path / 'kit.kdbx', 'kit-password')
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    _ = (bundle / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')

    def unasked(prompt: str = '') -> str:
        raise AssertionError(f'the run prompted: {prompt}')

    monkeypatch.setattr('getpass.getpass', unasked)
    row = conventions.sealed.ETCD_WRITER_KEY
    physical_state: dict[str, object] = {
        conventions.PHYSICAL_OUTPUTS.kubeconfig: KUBECONFIG,
        BACKUP_KEYS: {ETCD_SCOPE: EXPORTED[ETCD_SCOPE]},
    }
    backend = UnderOwnPassphrases(
        runners={PHYSICAL: PathPulumi(outputs=physical_state), row.stack: PathPulumi()},
        passphrases={PHYSICAL: 'the-physical-passphrase', row.stack: 'the-stack-passphrase'},
    )
    monkeypatch.setattr(pulumi_cli, 'run_pulumi', backend)

    def certificate_unfetched(
        physical: pulumi_config.Stack,
        *,
        open_stack: Callable[[str], pulumi_config.Stack],
        run: sealing.Kubeseal = sealed_as_marked,
    ) -> sealing.Sealer:
        _ = run, sealing.cluster_kubeconfig(physical)
        return sealing.Sealer(certificate='', open_stack=open_stack, run=sealed_as_marked)

    monkeypatch.setattr(sealing, 'cluster_sealer', certificate_unfetched)

    status = cli.main(
        [
            '--kdbx',
            str(kit.path),
            '--escrow',
            str(tmp_path / 'escrow'),
            'derived',
            derived.B2_WRITER_ROW,
            'seal',
            '--only',
            row.name,
            '--bundle-dir',
            str(bundle),
        ]
    )

    assert status == 0
    assert backend.runners[row.stack].plain == {
        row.path('AWS_ACCESS_KEY_ID'): f'AgB-sealed-{ETCD_SCOPE}-id',
        row.path('AWS_SECRET_ACCESS_KEY'): f'AgB-sealed-{ETCD_SCOPE}-secret',
    }


# -- the etcd restore-verify's age identity ---------------------------------------

ETCD_DRILL = conventions.sealed.ETCD_DRILL_IDENTITY


@pytest.fixture
def pinned_age() -> None:
    """The pinned `age-keygen`, which draws the identity, and `age`, which opens what is encrypted to it.

    Refused when either is missing rather than skipped.
    """
    pinned_tools.require(*pinned_tools.AGE)


def recipients_on_file(path: Path) -> list[str]:
    """The recipient lines of a recipient file, its `#` comments aside."""
    return [line for line in path.read_text().splitlines() if line.strip() and not line.startswith('#')]


@pytest.mark.usefixtures('pinned_age')
def test_what_is_encrypted_to_the_recipient_on_file_opens_with_the_sealed_identity_as_a_file(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair, tmp_path: Path
) -> None:
    """The sealed value, opened as the controller opens the row's SealedSecret, is the recipient file's identity.

    Written out as a pod mounts the Secret, one file per data key, it is an
    identity file `age -i` reads, and it opens a file encrypted to the one
    recipient the run committed -- which is the whole of what the two halves
    owe each other.
    """
    recipient_file = tmp_path / 'etcd-drill-recipient.txt'

    returned = derived.etcd_drill_age_identity(sealer=sealer, recipient_file=recipient_file)

    k8s_base = stacks.runner(K8S_BASE)
    assert set(k8s_base.plain) == {ETCD_DRILL.path(key) for key in ETCD_DRILL.keys}
    assert not k8s_base.config
    assert recipients_on_file(recipient_file) == [returned]
    mounted = tmp_path / 'mounted'
    mounted.mkdir()
    for key, value in unseal(kubeseal, key_pair, ETCD_DRILL, written(stacks, ETCD_DRILL)).items():
        _ = (mounted / key).write_text(value)
    (identity_file,) = mounted.iterdir()
    snapshot = tmp_path / 'snapshot.age'
    _ = snapshot.write_text(age.encrypt('an etcd snapshot', [returned]))
    opened = process_sessions.run(
        [age.BINARY, '--decrypt', '--identity', str(identity_file), str(snapshot)],
        text=True,
        check=True,
        timeout=TIMEOUT,
    )
    assert opened.stdout == 'an etcd snapshot'


@pytest.mark.usefixtures('pinned_age')
def test_running_the_etcd_drill_generator_again_replaces_both_halves(stacks: Stacks, tmp_path: Path) -> None:
    """A second run draws a new identity: one recipient on file, the new one, and the sealed value is its identity."""
    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=sealed_as_marked)
    recipient_file = tmp_path / 'etcd-drill-recipient.txt'
    first = derived.etcd_drill_age_identity(sealer=sealer, recipient_file=recipient_file)

    second = derived.etcd_drill_age_identity(sealer=sealer, recipient_file=recipient_file)

    assert second != first
    assert recipients_on_file(recipient_file) == [second]
    (sealed,) = written(stacks, ETCD_DRILL).values()
    assert age.recipient(sealed.removeprefix('AgB-sealed-').strip()) == second


def test_the_etcd_drill_generator_into_a_k8s_base_that_does_not_exist_draws_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing declaring stack refuses before an identity is drawn, and no recipient lands on file."""
    absent = Stacks()

    def undrawn() -> age.Identity:
        raise AssertionError('an identity was drawn before the seal found its stack')

    monkeypatch.setattr(derived.age, 'generate', undrawn)
    sealer = sealing.Sealer(certificate='', open_stack=absent.open, run=sealed_as_marked)
    recipient_file = tmp_path / 'etcd-drill-recipient.txt'

    with pytest.raises(pulumi_config.StackMissing):
        _ = derived.etcd_drill_age_identity(sealer=sealer, recipient_file=recipient_file)
    assert not recipient_file.exists()
    assert absent.runner(K8S_BASE).plain == {}


@pytest.mark.usefixtures('pinned_age')
def test_a_seal_that_fails_leaves_the_recipient_on_file_as_it_was(stacks: Stacks, tmp_path: Path) -> None:
    """The file is written only after the ciphertext: a refused seal leaves the recipient in service untouched."""

    def prints_nothing(args: Sequence[str], *, stdin: str | None) -> str:
        del args, stdin
        return ''

    sealer = sealing.Sealer(certificate='', open_stack=stacks.open, run=prints_nothing)
    recipient_file = tmp_path / 'etcd-drill-recipient.txt'
    _ = recipient_file.write_text('# the key in service\nage1inservice\n')

    with pytest.raises(SlotRefused):
        _ = derived.etcd_drill_age_identity(sealer=sealer, recipient_file=recipient_file)
    assert recipients_on_file(recipient_file) == ['age1inservice']
    assert stacks.runner(K8S_BASE).plain == {}


@pytest.mark.usefixtures('pinned_age')
def test_the_etcd_drill_generator_needs_no_kit_on_a_machine_holding_both_passphrase_slots(
    tmp_path: Path, credential_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`credentials derived etcd-drill-age-identity generate`, the command as the operator runs it.

    It reads `physical`'s state under that stack's passphrase for the
    cluster's certificate and writes into `k8s-base` under the stack
    passphrase, each from its workstation slot. The kit is a real one,
    locked, every prompt fails the case, and the escrow holds nothing, so a
    run that reached for either would stop. Only the certificate fetch is
    stood in for, which reads the kubeconfig as the command does and asks
    `kubeseal` nothing, and the recipient file is the case's own.
    """
    credential_files.mkdir(parents=True)
    _ = (credential_files / workstation.PASSPHRASE).write_text('the-stack-passphrase\n')
    _ = (credential_files / workstation.PHYSICAL_PASSPHRASE).write_text('the-physical-passphrase\n')
    kit = KdbxStore.create(tmp_path / 'kit.kdbx', 'kit-password')
    bundle_dir = tmp_path / 'bundle'
    bundle_dir.mkdir()
    _ = (bundle_dir / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')

    def unasked(prompt: str = '') -> str:
        raise AssertionError(f'the run prompted: {prompt}')

    monkeypatch.setattr('getpass.getpass', unasked)
    backend = UnderOwnPassphrases(
        runners={
            PHYSICAL: PathPulumi(outputs={conventions.PHYSICAL_OUTPUTS.kubeconfig: KUBECONFIG}),
            K8S_BASE: PathPulumi(),
        },
        passphrases={PHYSICAL: 'the-physical-passphrase', K8S_BASE: 'the-stack-passphrase'},
    )
    monkeypatch.setattr(pulumi_cli, 'run_pulumi', backend)

    def certificate_unfetched(
        physical: pulumi_config.Stack,
        *,
        open_stack: Callable[[str], pulumi_config.Stack],
        run: sealing.Kubeseal = sealed_as_marked,
    ) -> sealing.Sealer:
        _ = run, sealing.cluster_kubeconfig(physical)
        return sealing.Sealer(certificate='', open_stack=open_stack, run=sealed_as_marked)

    monkeypatch.setattr(sealing, 'cluster_sealer', certificate_unfetched)
    recipient_file = tmp_path / 'etcd-drill-recipient.txt'

    def in_the_case(path: Path) -> Path:
        assert path == derived.ETCD_DRILL_RECIPIENT_FILE
        return recipient_file

    monkeypatch.setattr(derived, 'committed_target', in_the_case)

    status = cli.main(
        [
            '--kdbx',
            str(kit.path),
            '--escrow',
            str(tmp_path / 'escrow'),
            'derived',
            derived.ETCD_DRILL_AGE_IDENTITY_ROW,
            'generate',
            '--bundle-dir',
            str(bundle_dir),
        ]
    )

    assert status == 0
    (on_file,) = recipients_on_file(recipient_file)
    assert set(backend.runners[K8S_BASE].plain) == {ETCD_DRILL.path(key) for key in ETCD_DRILL.keys}
    (sealed,) = backend.runners[K8S_BASE].plain.values()
    assert age.recipient(sealed.removeprefix('AgB-sealed-').strip()) == on_file


# -- what the stack files hold ------------------------------------------------


def committed_sealed() -> dict[str, dict[str, dict[str, str]]]:
    """What every committed `Pulumi.<stack>.yaml` holds under the sealed key, by stack."""
    project = pulumi_config.project_dir()
    key = f'{yaml.safe_load((project / "Pulumi.yaml").read_text())["name"]}:{conventions.sealed.CONFIG_KEY}'
    found: dict[str, dict[str, dict[str, str]]] = {}
    for path in sorted(project.glob('Pulumi.*.yaml')):
        config = cast('dict[str, object]', yaml.safe_load(path.read_text()).get('config') or {})
        held = config.get(key)
        if held is not None:
            found[path.name.removeprefix('Pulumi.').removesuffix('.yaml')] = cast('dict[str, dict[str, str]]', held)
    return found


def is_sealed(ciphertext: object) -> bool:
    """Whether `ciphertext` has the shape of `kubeseal`'s hybrid encryption.

    Base64 of a two-byte length, an RSA-encrypted session key of that length
    -- a whole RSA modulus, 256 bytes or more -- and the sealed data after it.
    A value in the clear, a password or a token, does not decode to that.
    """
    if not isinstance(ciphertext, str):
        return False
    try:
        raw = base64.b64decode(ciphertext, validate=True)
    except ValueError:
        return False
    length = int.from_bytes(raw[:2], 'big')
    return length >= 256 and length % 256 == 0 and len(raw) > 2 + length


def test_the_ciphertext_check_takes_what_kubeseal_seals_and_refuses_a_value_in_the_clear(
    sealer: sealing.Sealer,
) -> None:
    (ciphertext,) = sealer.seal(conventions.sealed.DNS01_TOKEN, {'api-token': 'a-token'}).values()

    assert is_sealed(ciphertext)
    assert not is_sealed('a-token')
    assert not is_sealed(base64.b64encode(b'a password in the clear').decode())


def test_every_value_a_stack_file_holds_sealed_is_ciphertext() -> None:
    """Nothing under the sealed key is a secret in the clear, ported or sealed here.

    The ciphertexts ported from the legacy cluster are copied in by hand
    (declarative/workloads.md §5), and a hand can paste the wrong half of a
    manifest. `apps` holds them, so it has something under the key.
    """
    held = committed_sealed()

    assert held.get(APPS)
    for stack, values in held.items():
        for name, data in values.items():
            assert data, f'{stack}: {name} holds no data key'
            unsealed = sorted(key for key, ciphertext in data.items() if not is_sealed(ciphertext))
            assert not unsealed, f'{stack}: {name} holds {unsealed} as something other than kubeseal ciphertext'


def test_a_value_a_stack_file_holds_under_a_census_rows_name_carries_that_rows_keys() -> None:
    """A value under the name of a census row is that row's, so no ported value is named like one.

    Both live under the one sealed key of the declaring stack, keyed by name:
    a writer key named like an application's own ported Secret would land
    beside that Secret's data keys, and the program would read the two as one.
    """
    for stack, values in committed_sealed().items():
        for name, data in values.items():
            row = conventions.sealed.VALUES.get(name)
            if row is not None and row.stack == stack:
                assert sorted(data) == sorted(row.keys), f'{stack}: {name}'


# -- the state table's upkeep bundle --------------------------------------------

UPKEEP_BUNDLE = conventions.sealed.STATE_UPKEEP_BUNDLE


def _public(key: object) -> bytes:
    """A public key as DER SubjectPublicKeyInfo, which two keys compare by."""
    assert isinstance(key, ec.EllipticCurvePublicKey), type(key)
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def test_the_upkeep_seal_writes_a_bundle_of_upkeep_at_the_rows_paths(
    stacks: Stacks, sealer: sealing.Sealer, kubeseal: str, key_pair: KeyPair, vault: escrow.Vault, tmp_path: Path
) -> None:
    """Every part lands at the row's path in `k8s-base`, and the parts open as a client bundle for `upkeep`.

    Opened as the controller opens the row's SealedSecret and written under
    each data key into a directory, which is how the pod mounts it: the
    directory reads as a bundle naming `upkeep` at the box's address, its
    certificate names `upkeep` and is the escrowed CA's, and its key is the
    certificate's. That such a bundle logs in as `upkeep` on a box is
    `tests/test_state_roles.py`'s.
    """
    _ = escrow.generate(vault, escrow.CA)
    authority = pki.Authority.from_pem(vault.recover(escrow.CA))

    expires = derived.state_backend_upkeep(lambda: vault, sealer=sealer)

    k8s_base = stacks.runner(K8S_BASE)
    assert set(k8s_base.plain) == {UPKEEP_BUNDLE.path(key) for key in UPKEEP_BUNDLE.keys}
    assert not k8s_base.config
    directory = tmp_path / 'upkeep'
    directory.mkdir()
    for name, part in unseal(kubeseal, key_pair, UPKEEP_BUNDLE, written(stacks, UPKEEP_BUNDLE)).items():
        _ = (directory / name).write_text(part)
    assert bundle.read(directory, bundle.Purpose.QUERY) == (
        f'postgres://{settings.UPKEEP_ROLE}@{settings.ADDRESS}:{settings.PORT}/{settings.DATABASE}?sslmode=verify-full'
    )
    certificate = x509.load_pem_x509_certificate(Path(bundle.ssl_env(directory)[bundle.CERT_ENV]).read_bytes())
    ca = x509.load_pem_x509_certificate(Path(bundle.ssl_env(directory)[bundle.CA_ENV]).read_bytes())
    key = serialization.load_pem_private_key(Path(bundle.ssl_env(directory)[bundle.KEY_ENV]).read_bytes(), None)
    assert [name.value for name in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)] == [
        settings.UPKEEP_ROLE
    ]
    certificate.verify_directly_issued_by(ca)
    assert _public(ca.public_key()) == _public(
        x509.load_pem_x509_certificate(authority.certificate().cert_pem).public_key()
    )
    assert _public(key.public_key()) == _public(certificate.public_key())
    assert expires == certificate.not_valid_after_utc


def test_the_upkeep_seal_into_a_k8s_base_that_does_not_exist_opens_no_escrow() -> None:
    """A missing declaring stack refuses before the escrow is opened, which is the kit's prompt."""
    absent = Stacks()

    def unopened() -> escrow.Vault:
        raise AssertionError('the escrow was opened before the seal found its stack')

    sealer = sealing.Sealer(certificate='', open_stack=absent.open, run=sealed_as_marked)

    with pytest.raises(pulumi_config.StackMissing):
        _ = derived.state_backend_upkeep(unopened, sealer=sealer)
    assert absent.runner(K8S_BASE).plain == {}
