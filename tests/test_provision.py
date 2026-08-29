"""Properties of the provisioner that are not about talking to OCI.

Two are asserted here. One is a boundary: `provision` is full of `ensure_*`
functions that create what they cannot find, and exactly one lookup must not
do that -- the diagnosis path. The other is where the box's own credential
comes from: a workstation slot a `credentials` command mints into
(credentials.md §3), the path it superseded, or an error naming the command.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from kluster import conventions
from kluster.scripts.credentials import escrow, oci_iam, oci_slot, workstation
from kluster.scripts.state_backend import provision


class _Network:
    def __init__(self, ips: list[Any]) -> None:
        self.ips: list[Any] = ips
        self.created: int = 0

    def list_public_ips(self, **_kwargs: object) -> Any:
        return type('Response', (), {'data': self.ips})()

    def create_public_ip(self, *_args: object, **_kwargs: object) -> Any:  # pragma: no cover
        self.created += 1
        raise AssertionError('a lookup created a reserved address')


class _Client:
    def __init__(self, ips: list[Any]) -> None:
        self.compartment_id: str = 'ocid1.compartment.test'
        self.network: _Network = _Network(ips)


def _ip(name: str, address: str, state: str = 'ASSIGNED') -> Any:
    return type('PublicIp', (), {'display_name': name, 'ip_address': address, 'lifecycle_state': state})()


def test_the_address_is_looked_up_not_reserved() -> None:
    client = _Client([_ip('state-backend-ip', '192.0.2.10')])

    assert provision.reserved_address(client) == '192.0.2.10'  # pyright: ignore[reportArgumentType]
    assert client.network.created == 0


def test_a_missing_address_is_an_error_rather_than_an_allocation() -> None:
    # `ensure_reserved_ip` reserves one when none exists, which is right while
    # provisioning and wrong for `ssh`: a diagnosis command must not allocate
    # cloud resources because it could not find something.
    client = _Client([])

    with pytest.raises(RuntimeError, match='has the appliance been provisioned'):
        _ = provision.reserved_address(client)  # pyright: ignore[reportArgumentType]
    assert client.network.created == 0


def test_a_terminated_address_does_not_count() -> None:
    client = _Client([_ip('state-backend-ip', '192.0.2.10', state='TERMINATED')])

    with pytest.raises(RuntimeError):
        _ = provision.reserved_address(client)  # pyright: ignore[reportArgumentType]


# -- where the appliance's own credential comes from -------------------------

COMPARTMENT = 'ocid1.compartment.oc1..appliance'
APPLIANCE_USER = 'ocid1.user.oc1..kluster-state-backend'
APPLIANCE_TENANCY = 'ocid1.tenancy.oc1..estate'


@pytest.fixture
def slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`.credentials/` outside this checkout, with no ambient override in force."""
    monkeypatch.delenv('OCI_CLI_CONFIG_FILE', raising=False)
    monkeypatch.setattr(provision, 'LEGACY_CONFIG_FILE', tmp_path / 'legacy' / 'config')
    directory = tmp_path / '.credentials'
    monkeypatch.setattr(workstation, 'directory', lambda: directory)
    return directory


def _mint() -> Path:
    """A slot filled the way `credentials derived oci-state-backend mint` fills it."""
    private_pem, _ = oci_iam.generate_key()
    key = oci_iam.ApiKey(tenancy=APPLIANCE_TENANCY, user=APPLIANCE_USER, private_key=private_pem)
    return oci_slot.write(key)


def test_the_appliance_signs_as_the_key_minted_for_it(slots: Path) -> None:
    _ = _mint()

    client = provision.Oci.load()

    # The slot is the signing configuration, and where the appliance may act is
    # a convention beside it: the mapping is the one place the compartment is
    # written down, so nothing can drift from it.
    assert client.compartment_id == conventions.OCI_TENANCY.compartments[conventions.STATE_BACKEND].ocid
    assert (client.config['user'], client.config['tenancy']) == (APPLIANCE_USER, APPLIANCE_TENANCY)


def test_an_explicit_compartment_wins_over_the_convention(slots: Path) -> None:
    _ = _mint()

    client = provision.Oci.load('ocid1.compartment.oc1..elsewhere')

    # The drill escape: a run against a tenancy that is not this estate's
    # names its own compartment, because none of the mapping applies there.
    assert client.compartment_id == 'ocid1.compartment.oc1..elsewhere'


def test_the_superseded_configuration_is_read_once_and_loudly(
    slots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A workstation that predates the mint keeps provisioning: what is at the
    # old path is a complete answer, and the warning names its replacement.
    # Moving the file out of the slot is what makes it the old one -- it names
    # its key absolutely, so it goes on working from anywhere.
    superseded = tmp_path / 'legacy' / 'config'
    superseded.parent.mkdir()
    _mint().rename(superseded)
    # A hand-written configuration carried the compartment in the same file,
    # which is the one place that value still comes from.
    with superseded.open('a') as handle:
        _ = handle.write(f'compartment-id={COMPARTMENT}\n')
    monkeypatch.setattr(provision, 'LEGACY_CONFIG_FILE', superseded)

    with caplog.at_level(logging.WARNING):
        client = provision.Oci.load()

    assert client.compartment_id == COMPARTMENT
    assert 'credentials derived oci-state-backend mint' in caplog.text


def test_a_machine_with_no_credential_is_told_what_mints_one(slots: Path) -> None:
    # The SDK's own answer is a missing file; this one names the command that
    # creates it, which is the whole difference between a stop and a step.
    with pytest.raises(ValueError, match='credentials derived oci-state-backend mint'):
        _ = provision.Oci.load()


class _Recorder:
    """Enough of the provision surface to watch what a converge run touches."""

    def __init__(
        self,
        *,
        instance_exists: bool,
        metadata: dict[str, str] | None = None,
        dump_key_current: bool = True,
    ) -> None:
        self.instance_exists: bool = instance_exists
        self.metadata: dict[str, str] = {} if metadata is None else metadata
        self.dump_key_current: bool = dump_key_current
        self.minted: int = 0
        self.terminated: int = 0
        self.launched: int = 0
        self.launched_metadata: dict[str, str] = {}
        self.forgotten: list[str] = []


def _returning(value: Any) -> Callable[..., Any]:
    """A typed stand-in: a bare lambda leaves its parameters unannotated."""

    def stub(*_args: object, **_kwargs: object) -> Any:
        return value

    return stub


#: What a box built from the current commit records about itself. The tests
#: below vary this rather than the repository, because the property under test
#: is "box differs from commit", not any particular way of differing.
CURRENT = {'butane': 'aaaa', 'operator_keys': 'bbbb'}


def _built_from(digests: dict[str, str], *, dump_key_id: str = 'key-id') -> dict[str, str]:
    return {
        provision.CONFIG_METADATA: json.dumps(digests, sort_keys=True),
        provision.DUMP_KEY_METADATA: dump_key_id,
    }


@pytest.fixture
def converge(monkeypatch: pytest.MonkeyPatch) -> Any:
    from kluster.scripts.credentials import b2
    from kluster.scripts.state_backend import cli, config

    def install(recorder: _Recorder) -> None:
        def mint(*_args: object, **_kwargs: object) -> tuple[str, str]:
            recorder.minted += 1
            return ('key-id', 'key-secret')

        def find(*_args: object, **_kwargs: object) -> Any:
            if not recorder.instance_exists:
                return None
            return type('Instance', (), {'id': 'ocid1.instance.existing', 'metadata': recorder.metadata})()

        def terminate(*_args: object, **_kwargs: object) -> None:
            recorder.terminated += 1
            recorder.instance_exists = False

        def launch(*_args: object, digests: dict[str, str], dump_key_id: str, **_kwargs: object) -> str:
            recorder.launched += 1
            recorder.launched_metadata = _built_from(digests, dump_key_id=dump_key_id)
            return 'ocid1.instance.new'

        monkeypatch.setattr(b2.Session, 'from_entry', staticmethod(_returning(object())))
        monkeypatch.setattr(b2, 'ensure_bucket', _returning('bucket-id'))
        monkeypatch.setattr(b2, 'mint_dump_key', mint)
        monkeypatch.setattr(b2, 'dump_key_is_current', _returning(recorder.dump_key_current))
        monkeypatch.setattr(provision.Oci, 'load', classmethod(_returning(object())))
        monkeypatch.setattr(provision, 'ensure_network', _returning(('vcn', 'subnet')))
        monkeypatch.setattr(provision, 'ensure_security_group', _returning('nsg'))
        monkeypatch.setattr(provision, 'ensure_reserved_ip', _returning(('ip-id', '192.0.2.10')))
        monkeypatch.setattr(provision, 'ensure_image', _returning('image'))
        monkeypatch.setattr(provision, 'find_instance', find)
        monkeypatch.setattr(provision, 'terminate_instance', terminate)
        monkeypatch.setattr(provision, 'ensure_instance', launch)
        monkeypatch.setattr(provision, 'forget_host_key', recorder.forgotten.append)
        monkeypatch.setattr(provision, 'attach_reserved_ip', _returning(None))
        monkeypatch.setattr(provision, 'wait_for_backend', _returning(True))
        monkeypatch.setattr(config, 'render_ignition', _returning('ignition'))
        monkeypatch.setattr(config, 'digests', _returning(dict(CURRENT)))
        monkeypatch.setattr(config, 'client_bundle', _returning(object()))
        monkeypatch.setattr(config, 'write_client_bundle', _returning(None))
        # Recovering the escrow needs the offline key and the age binary;
        # what is under test is the converge, so the roots arrive already
        # opened and the CA is a stand-in nothing here signs with.
        roots = cli.config.Roots(ca=cast('Any', object()), age_recipients=())
        monkeypatch.setattr(cli.config.Roots, 'ensure', classmethod(_returning(roots)))
        monkeypatch.setattr(cli.escrow.Vault, 'open', classmethod(_returning(object())))

    return install


def _run(replace: bool = False) -> int:
    from kluster.scripts.state_backend import cli

    return cli._provision(  # pyright: ignore[reportPrivateUsage]
        object(),  # pyright: ignore[reportArgumentType]
        seed_entry='e',
        compartment=None,
        replace=replace,
        registry=escrow.Registry(root=Path('unused')),
    )


def test_a_box_that_matches_the_commit_is_left_alone(converge: Any) -> None:
    """The skip condition, and the reason the nightly dump survives a converge.

    B2 returns an application key's secret once, so the box's copy cannot be
    read back and minting a replacement revokes what the box is holding. A run
    that mints and then leaves the instance alone breaks the dump silently
    until it next fires -- which is what it did.
    """
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 0, 0)


def test_the_operator_bundle_lands_in_the_workstation_slot(
    converge: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # It used to land in ~/.config/kluster/state-backend, the one local
    # artefact outside the convention (credentials.md §1 rule 6). Everything
    # a checkout needs locally is now in the checkout.
    from kluster.scripts.credentials import workstation
    from kluster.scripts.state_backend import config

    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)
    written: list[Path] = []

    def record(_bundle: object, directory: Path) -> None:
        written.append(directory)

    monkeypatch.setattr(config, 'write_client_bundle', record)
    monkeypatch.setattr(workstation, 'directory', lambda: tmp_path / '.credentials')

    assert _run() == 0

    assert written == [tmp_path / '.credentials' / 'state-backend']


def test_a_changed_machine_definition_replaces_the_box(converge: Any) -> None:
    """Not only the Butane file: any component of `config.digests`.

    This is what makes provision an apply of the current commit rather than a
    create-if-absent -- the box is compared to the repository, and the dump
    key is one component of that comparison rather than the only one that
    ever triggered a rebuild.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_box_whose_dump_key_b2_no_longer_has_is_replaced(converge: Any) -> None:
    # The secret exists only inside the Ignition the box booted with, so a key
    # that is gone (or re-scoped) cannot be handed over without a new box.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT), dump_key_current=False)
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_box_without_the_bookkeeping_is_replaced(converge: Any) -> None:
    # A box that cannot say what it was built from is not evidence that it
    # matches; silence converges rather than passing.
    recorder = _Recorder(instance_exists=True, metadata={})
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_replace_rebuilds_a_box_that_matches(converge: Any) -> None:
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    assert _run(replace=True) == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_first_run_mints_and_launches(converge: Any) -> None:
    recorder = _Recorder(instance_exists=False)
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 1, 1)


def test_a_launch_records_what_the_next_converge_compares(converge: Any) -> None:
    """Without this the loop never closes: a box built now would read as drift."""
    recorder = _Recorder(instance_exists=False)
    converge(recorder)
    _ = _run()

    assert provision.instance_config(type('Instance', (), {'metadata': recorder.launched_metadata})()) == (
        CURRENT,
        'key-id',
    )


def test_replacing_forgets_the_destroyed_box_s_host_key(converge: Any) -> None:
    """The reserved address outlives the box, so ssh sees an identity change.

    Which it reports as a possible man-in-the-middle -- on a machine the
    command in front of it just destroyed.
    """
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    _ = _run(replace=True)

    assert recorder.forgotten == ['192.0.2.10']


def test_the_readiness_probe_closes_its_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the wait can never finish on a workstation.

    `openssl s_client` keeps the connection open reading stdin once the
    handshake is done, so with a terminal inherited from the operator's shell
    a *successful* probe hangs until the timeout and is reported as no answer.
    On a machine that came up in 90 seconds this looked like packets being
    dropped for as long as the operator was willing to wait.
    """
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> Any:
        seen.update(kwargs)
        seen['argv'] = argv
        return type('Completed', (), {'returncode': 0, 'stderr': ''})()

    monkeypatch.setattr(provision.sp, 'run', fake_run)

    assert provision.wait_for_backend('192.0.2.10', timeout=1) is True
    assert seen['stdin'] is provision.sp.DEVNULL


#: The provision stages that outlast an operator's patience, and a word that
#: has to appear in what the run says *before* each one starts. Announcing on
#: completion only is what makes a long step indistinguishable from a hang.
#: The readiness wait announces from inside itself, and has its own test below.
SLOW_STAGES = {
    'find_instance': 'comparing',
    'ensure_image': 'image',
    'ensure_instance': 'launching',
}


def test_every_slow_stage_announces_itself_before_it_starts(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run's output has to distinguish a slow step from a stuck one."""
    caplog.set_level(logging.INFO)
    converge(_Recorder(instance_exists=False))

    said: dict[str, list[str]] = {}

    def watch(name: str) -> None:
        wrapped: Callable[..., Any] = getattr(provision, name)

        def call(*args: object, **kwargs: object) -> Any:
            said[name] = list(caplog.messages)
            return wrapped(*args, **kwargs)

        monkeypatch.setattr(provision, name, call)

    for stage in SLOW_STAGES:
        watch(stage)

    assert _run() == 0

    for stage, word in SLOW_STAGES.items():
        assert any(word in message for message in said[stage]), f'{stage} ran without announcing itself'


def test_the_readiness_wait_states_its_condition_before_probing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The longest silence in a provision run is the one after the launch."""
    caplog.set_level(logging.INFO)
    said: list[str] = []

    def fake_run(_argv: list[str], **_kwargs: object) -> Any:
        said.extend(caplog.messages)
        return type('Completed', (), {'returncode': 0, 'stderr': ''})()

    monkeypatch.setattr(provision.sp, 'run', fake_run)

    assert provision.wait_for_backend('192.0.2.10', timeout=900) is True
    announcement = next(message for message in said if 'waiting' in message)
    # The condition, the retry cadence, why it is slow, and the ceiling.
    assert '192.0.2.10' in announcement
    assert 'every 15s' in announcement
    assert 'minutes' in announcement
    assert '15m00s' in announcement
