"""Properties of the provisioner that are not about talking to OCI.

The boundaries and the refusals, since the happy path is a cloud call. A
lookup that must not create what it cannot find, because `provision` is
otherwise full of `ensure_*` functions that do; where the box's own
credential comes from; what a converge decides about a running box, and what
it refuses to do to one without being asked.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from memory_kit import MemoryKit

from kluster import conventions
from kluster.scripts.credentials import escrow, oci_iam, oci_slot, pki, workstation
from kluster.scripts.credentials.delivery import Delivery
from kluster.scripts.state_backend import cli, config, provision, settings
from kluster.scripts.state_backend.state import StateError


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


class _Answer:
    """One `urlopen` answer: a JSON body, or headers and no body."""

    def __init__(self, *, body: object = None, headers: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = headers or {}
        self._body: bytes = json.dumps(body).encode() if body is not None else b''

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _registry(monkeypatch: pytest.MonkeyPatch, *answers: _Answer) -> None:
    """The two calls `_image_digest` makes, in order: the token, then the manifest."""
    remaining = list(answers)

    def urlopen(*_args: object, **_kwargs: object) -> _Answer:
        return remaining.pop(0)

    monkeypatch.setattr(provision.urllib.request, 'urlopen', urlopen)


def test_a_manifest_without_a_digest_header_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`state-backend pins` exists to catch a bad pin; an empty answer must not pass as one.

    A registry that answers the HEAD without `Docker-Content-Digest` used to
    be logged as a resolution to the empty string, so the one command whose
    job is to fail on a bad pin reported success.
    """
    _registry(monkeypatch, _Answer(body={'token': 'a-pull-token'}), _Answer(headers={}))

    with pytest.raises(RuntimeError, match='without a Docker-Content-Digest header'):
        _ = provision._image_digest('docker.io/library/postgres:17')  # pyright: ignore[reportPrivateUsage]


def test_a_token_response_without_a_token_names_the_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, _Answer(body={'errors': ['nope']}))

    with pytest.raises(RuntimeError, match='the registry pull token for library/postgres has no token'):
        _ = provision._image_digest('docker.io/library/postgres:17')  # pyright: ignore[reportPrivateUsage]


def test_a_resolved_manifest_answers_its_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(
        monkeypatch,
        _Answer(body={'token': 'a-pull-token'}),
        _Answer(headers={'Docker-Content-Digest': 'sha256:abc'}),
    )

    assert provision._image_digest('docker.io/library/postgres:17') == 'sha256:abc'  # pyright: ignore[reportPrivateUsage]


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
APPLIANCE_TENANCY = 'ocid1.tenancy.oc1..installation'


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
    private_pem = oci_iam.generate_key().private_pem
    key = oci_iam.ApiKey(tenancy=APPLIANCE_TENANCY, user=APPLIANCE_USER, private_key=private_pem)
    return oci_slot.write(key)


def test_the_appliance_signs_as_the_key_minted_for_it(slots: Path) -> None:
    _ = _mint()

    client = provision.OciClients.load()

    # The slot is the signing configuration, and where the appliance may act is
    # a convention beside it: the mapping is the one place the compartment is
    # written down, so nothing can drift from it.
    assert client.compartment_id == conventions.OCI_TENANCY.compartments[conventions.STATE_BACKEND].ocid
    assert (client.config['user'], client.config['tenancy']) == (APPLIANCE_USER, APPLIANCE_TENANCY)


def test_an_explicit_compartment_wins_over_the_convention(slots: Path) -> None:
    _ = _mint()

    client = provision.OciClients.load('ocid1.compartment.oc1..elsewhere')

    # The drill escape: a run against a tenancy that is not this installation's
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
        client = provision.OciClients.load()

    assert client.compartment_id == COMPARTMENT
    assert 'credentials derived oci-state-backend mint' in caplog.text


def test_a_machine_with_no_credential_is_told_what_mints_one(slots: Path) -> None:
    # The SDK's own answer is a missing file; this one names the command that
    # creates it, which is the whole difference between a stop and a step.
    with pytest.raises(ValueError, match='credentials derived oci-state-backend mint'):
        _ = provision.OciClients.load()


class _Recorder:
    """Enough of the provision surface to watch what a converge run touches."""

    def __init__(
        self,
        *,
        instance_exists: bool,
        metadata: dict[str, str] | None = None,
        dump_key_current: bool = True,
        dump_fails: bool = False,
    ) -> None:
        self.instance_exists: bool = instance_exists
        self.metadata: dict[str, str] = {} if metadata is None else metadata
        self.dump_key_current: bool = dump_key_current
        self.dump_fails: bool = dump_fails
        self.minted: int = 0
        self.retired: int = 0
        self.terminated: int = 0
        self.launched: int = 0
        self.launched_metadata: dict[str, str] = {}
        self.forgotten: list[str] = []
        self.dumped: list[Path] = []
        #: Where each dump was taken from. Unrecorded, `--bundle` could be
        #: dropped on the way down and nothing would notice.
        self.bundles: list[Path] = []
        #: What the run did to the running box, in the order it did it. The
        #: dump is only worth anything before the termination, and the dump
        #: key's predecessor is only spent safely after the box that replaces
        #: the one holding it exists.
        self.order: list[str] = []


def _returning(value: Any) -> Callable[..., Any]:
    """A typed stand-in: a bare lambda leaves its parameters unannotated."""

    def stub(*_args: object, **_kwargs: object) -> Any:
        return value

    return stub


#: What a box built from the current commit records about itself. The tests
#: below vary this rather than the repository, because the property under test
#: is "box differs from commit", not any particular way of differing.
CURRENT = {'butane': 'aaaa', 'operator_keys': 'bbbb'}


def _expiry(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()


#: A certificate with most of its life ahead of it, as a box records it. Well
#: outside `config.RENEWAL_MARGIN`, so it is not what any of these tests vary.
FRESH = _expiry(1000)


def _built_from(digests: dict[str, str], *, dump_key_id: str = 'key-id', expiry: str = FRESH) -> dict[str, str]:
    return {
        provision.CONFIG_METADATA: json.dumps(digests, sort_keys=True),
        provision.DUMP_KEY_METADATA: dump_key_id,
        provision.EXPIRY_METADATA: expiry,
    }


@pytest.fixture
def converge(monkeypatch: pytest.MonkeyPatch) -> Any:
    from kluster.scripts.credentials import b2

    def install(recorder: _Recorder) -> None:
        def write_dump(destination: Path, *, bundle_dir: Path, recipients: Sequence[str]) -> None:
            if recorder.dump_fails:
                raise StateError('pg_dump against the box failed: connection refused')
            recorder.dumped.append(destination)
            recorder.bundles.append(bundle_dir)
            recorder.order.append('dump')

        def mint(*_args: object, **_kwargs: object) -> Delivery[b2.AppKey]:
            recorder.minted += 1

            def retire() -> None:
                recorder.retired += 1
                recorder.order.append('retire')

            return Delivery.of(b2.AppKey(key_id='key-id', key='key-secret'), retire)

        def find(*_args: object, **_kwargs: object) -> Any:
            if not recorder.instance_exists:
                return None
            return type('Instance', (), {'id': 'ocid1.instance.existing', 'metadata': recorder.metadata})()

        def terminate(*_args: object, **_kwargs: object) -> None:
            recorder.terminated += 1
            recorder.instance_exists = False
            recorder.order.append('terminate')

        def launch(
            *_args: object, digests: dict[str, str], dump_key_id: str, server_cert_expiry: str, **_kwargs: object
        ) -> str:
            recorder.launched += 1
            recorder.launched_metadata = _built_from(digests, dump_key_id=dump_key_id, expiry=server_cert_expiry)
            recorder.order.append('launch')
            return 'ocid1.instance.new'

        monkeypatch.setattr(b2.Session, 'from_entry', staticmethod(_returning(object())))
        monkeypatch.setattr(b2, 'ensure_bucket', _returning('bucket-id'))
        monkeypatch.setattr(b2, 'mint_dump_key', mint)
        monkeypatch.setattr(b2, 'dump_key_is_current', _returning(recorder.dump_key_current))
        monkeypatch.setattr(provision.OciClients, 'load', classmethod(_returning(object())))
        monkeypatch.setattr(
            provision, 'ensure_network', _returning(provision.Placement(vcn_id='vcn', subnet_id='subnet'))
        )
        monkeypatch.setattr(provision, 'ensure_security_group', _returning('nsg'))
        monkeypatch.setattr(
            provision, 'ensure_reserved_ip', _returning(provision.ReservedAddress(id='ip-id', address='192.0.2.10'))
        )
        monkeypatch.setattr(provision, 'ensure_image', _returning('image'))
        monkeypatch.setattr(provision, 'find_instance', find)
        monkeypatch.setattr(provision, 'terminate_instance', terminate)
        monkeypatch.setattr(provision, 'ensure_instance', launch)
        monkeypatch.setattr(provision, 'forget_host_key', recorder.forgotten.append)
        monkeypatch.setattr(provision, 'attach_reserved_ip', _returning(None))
        monkeypatch.setattr(provision, 'wait_for_backend', _returning(True))
        monkeypatch.setattr(cli, '_write_dump', write_dump)
        monkeypatch.setattr(config, 'machine', _returning(object()))
        monkeypatch.setattr(config, 'render_ignition', _returning('ignition'))
        monkeypatch.setattr(config, 'expires_at', _returning(FRESH))
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


#: What a run that replaced the box exits with: the appliance is up, the state
#: is not back in it yet. Written out rather than read from `cli`, because a
#: test that imports the value it is checking moves with it — every case below
#: would pass just as well with the status set to 0, which is the answer the
#: whole exit code exists to avoid.
PENDING = 3

#: The bundle `_run` dumps over unless a case says otherwise.
SLOT = Path('bundle')


def _run(
    replace: bool = False,
    *,
    force: bool = True,
    dump: bool = True,
    dump_output: Path | None = None,
    bundle_dir: Path = SLOT,
) -> int:
    return cli._provision(  # pyright: ignore[reportPrivateUsage]
        object(),  # pyright: ignore[reportArgumentType]
        seed_entry='e',
        compartment=None,
        replace=replace,
        force=force,
        dump=dump,
        dump_output=dump_output,
        bundle_dir=bundle_dir,
        registry=escrow.Registry(root=Path('unused')),
    )


def test_the_replaced_status_is_neither_success_nor_failure() -> None:
    """Its whole job is to be told apart from the two statuses beside it.

    0 says the appliance is current and holds its state, which over an empty
    database is the dangerous answer. 1 says the run failed and nothing more:
    it may have stopped before touching anything, or after destroying the box,
    and which it was is in the run's last words rather than in the status. So
    a replacement needs a status of its own, one that always carries the same
    meaning. A wrapper reads this, so it is published in `provision --help`
    and in the appliance's README.
    """
    assert cli.RESTORE_PENDING == PENDING
    assert cli.RESTORE_PENDING not in (0, 1)


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

    assert _run() == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_box_whose_dump_key_b2_no_longer_has_is_replaced(converge: Any) -> None:
    # The secret exists only inside the Ignition the box booted with, so a key
    # that is gone (or re-scoped) cannot be handed over without a new box.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT), dump_key_current=False)
    converge(recorder)

    assert _run() == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_box_without_the_bookkeeping_is_replaced(converge: Any) -> None:
    # A box that cannot say what it was built from is not evidence that it
    # matches; silence converges rather than passing.
    recorder = _Recorder(instance_exists=True, metadata={})
    converge(recorder)

    assert _run() == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_replace_rebuilds_a_box_that_matches(converge: Any) -> None:
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    assert _run(replace=True) == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_first_run_mints_and_launches(converge: Any) -> None:
    recorder = _Recorder(instance_exists=False)
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 1, 1)


def test_the_converge_hands_the_launch_what_the_box_must_carry(converge: Any) -> None:
    """One half of the loop that lets a box built now read as current later.

    This is the wiring only — that `_provision` computes the three values and
    passes them down. That the launch then *stores* them is a property of
    `ensure_instance`, which this fixture replaces, and has its own test.
    """
    recorder = _Recorder(instance_exists=False)
    converge(recorder)
    _ = _run()

    assert provision.instance_config(
        type('Instance', (), {'metadata': recorder.launched_metadata})()
    ) == provision.InstanceConfig(digests=CURRENT, dump_key_id='key-id', server_cert_expiry=FRESH)


def test_a_drifted_box_is_reported_and_left_standing(converge: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Drift is a reason to replace the box, not permission to.

    The box holds every stack's state and its boot volume goes with it, so a
    converge an operator ran expecting a converge must not be the command that
    destroys it. It says what it would replace and stops.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run(force=False) == 1

    assert (recorder.terminated, recorder.minted, recorder.launched, recorder.dumped) == (0, 0, 0, [])
    # And the reason is on the way out, so the operator approving it knows
    # what they are approving.
    assert any('butane' in message for message in caplog.messages)


def test_a_first_run_needs_no_approval(converge: Any) -> None:
    # There is nothing to destroy, so there is nothing to approve.
    recorder = _Recorder(instance_exists=False)
    converge(recorder)

    assert _run(force=False) == 0

    assert (recorder.terminated, recorder.launched) == (0, 1)


def test_a_box_is_dumped_before_it_is_terminated(converge: Any) -> None:
    """The window between the nightly dump and a rebuild is a day of state.

    Taking one here closes it without the operator having to remember to, and
    the order is the whole point: a dump after the termination is a dump of
    nothing.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run() == PENDING

    assert recorder.order == ['dump', 'terminate', 'launch', 'retire']
    # And it is the artefact `state-backend restore` takes, under the name the
    # appliance's own objects carry, so the playbook's next step names a file
    # that is already there.
    (taken,) = recorder.dumped
    assert taken.name.startswith(settings.NAME) and taken.name.endswith('.dump.age')


def test_a_dump_that_fails_leaves_the_box_standing(converge: Any) -> None:
    # The replacement now depends on the dump, so a dump that did not happen
    # stops the run: proceeding is exactly the data loss the dump prevents.
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale), dump_fails=True)
    converge(recorder)

    assert _run() == 1

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 0, 0)


def test_the_uploader_is_confined_to_the_prefix_the_bucket_retires() -> None:
    """The grant and the retention are two statements that must agree.

    The bucket's lifecycle rule is what keeps the dump history from growing
    forever, and it governs one prefix; the key the appliance holds is confined
    to a prefix of its own. Uploads under a prefix the rule does not name would
    be kept for good, so the two read one home rather than two settings.
    """
    from kluster.scripts.credentials import b2

    assert b2.dumps('bucket-id').name_prefix == f'{settings.B2_PREFIX}/'


def test_the_dump_key_s_predecessor_is_retired_only_once_the_new_box_exists(converge: Any) -> None:
    """The order every mint in the credentials package has, on this one too.

    Launching the box is the dump key's push: B2 discloses an application
    key's secret once, so the successor exists in this process alone until the
    Ignition carrying it has been handed to OCI. Retired before that, a launch
    that then failed would leave the account holding no key for this bucket at
    all -- and the predecessor, which the next run could at least have swept by
    name, already gone.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run() == PENDING

    assert recorder.order.index('retire') > recorder.order.index('launch')
    assert (recorder.minted, recorder.retired) == (1, 1)


def test_a_launch_that_fails_leaves_the_superseded_dump_key_standing(
    converge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the order above buys, stated as the failure it prevents."""
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    monkeypatch.setattr(provision, 'ensure_instance', _returning_raise('the shape has no capacity'))

    with pytest.raises(RuntimeError, match='the shape has no capacity'):
        _ = _run()

    assert (recorder.minted, recorder.retired) == (1, 0)


def test_no_dump_replaces_a_box_that_cannot_be_dumped(converge: Any) -> None:
    # An unreachable box is what the rebuild path is the diagnosis for
    # (state-backend.md §6), and it is the one box no dump can be taken of.
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale), dump_fails=True)
    converge(recorder)

    assert _run(dump=False) == PENDING

    assert (recorder.order, recorder.terminated, recorder.launched) == (['terminate', 'launch', 'retire'], 1, 1)


def test_a_certificate_inside_the_renewal_margin_is_drift(converge: Any) -> None:
    """The failure this component exists for: 5432 going dark for every stack.

    Nothing else in the bill of materials can see it coming, because every
    other component is re-derived from the repository and the repository
    issues a certificate that is always young.
    """
    expiring = _built_from(CURRENT, expiry=_expiry(config.RENEWAL_MARGIN.days - 1))
    recorder = _Recorder(instance_exists=True, metadata=expiring)
    converge(recorder)

    assert _run() == PENDING

    assert (recorder.terminated, recorder.launched) == (1, 1)


def test_a_certificate_with_life_left_is_not_drift(converge: Any) -> None:
    # The other half: a threshold rather than a date is what keeps a
    # time-dependent component from replacing a healthy box every day.
    fresh = _built_from(CURRENT, expiry=_expiry(config.RENEWAL_MARGIN.days + 1))
    recorder = _Recorder(instance_exists=True, metadata=fresh)
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.launched) == (0, 0)


def test_a_box_that_records_no_expiry_is_drift(converge: Any) -> None:
    # Silence is not evidence that a certificate is healthy, and a box built
    # before this was recorded cannot be asked.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT, expiry=''))
    converge(recorder)

    assert _run() == PENDING

    assert (recorder.terminated, recorder.launched) == (1, 1)


def test_a_replaced_box_ends_the_run_holding_nothing(converge: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The run is half an operation, and the half it did leaves 5432 empty.

    A zero exit and `backend answering on <ip>:5432` over a database with no
    rows in it reads as done. So the run names the dump it took and the
    command that puts it back, and does not claim success.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run() == PENDING

    (taken,) = recorder.dumped
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)


def test_the_dump_goes_where_the_operator_asked(converge: Any, tmp_path: Path) -> None:
    """`--dump-output` is the only surviving copy of every stack's state.

    Dropped on the way down, the dump lands in the working directory instead
    — which is the checkout, where `.gitignore`'s `*.dump.age` then hides it.
    The operator looks at the path they named, finds nothing, and the file
    they need is invisible in the repository root.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    asked = tmp_path / 'elsewhere' / 'taken.dump.age'

    assert _run(dump_output=asked) == PENDING

    assert recorder.dumped == [asked]


def test_the_dump_is_taken_over_the_bundle_the_operator_named(converge: Any, tmp_path: Path) -> None:
    # A bundle that is not the workstation slot is how a drill, or a checkout
    # that keeps its credentials elsewhere, dumps the box it is replacing.
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    elsewhere = tmp_path / 'other-slot'

    assert _run(bundle_dir=elsewhere) == PENDING

    assert recorder.bundles == [elsewhere]


def test_a_replacement_without_a_dump_still_says_where_the_state_is(
    converge: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # The `--no-dump` run has no file to name, and the operator is left with
    # exactly one place to get the state from.
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale), dump_fails=True)
    converge(recorder)

    assert _run(dump=False) == PENDING

    assert any('state-backend restore' in message for message in caplog.messages)
    assert any('B2' in message for message in caplog.messages)


def test_a_box_that_never_answers_still_names_the_dump(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The worst case for the closing instruction, and the one that skipped it.

    The old box is gone, the state is in one file, and the new box is not
    answering — so `ssh core@<ip> to look` had been the run's last words.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    monkeypatch.setattr(provision, 'wait_for_backend', _returning(False))

    assert _run() == 1

    (taken,) = recorder.dumped
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)


#: Every step the run takes between destroying the old box and the readiness
#: probe. Each is a real failure mode — B2 refusing the mint, OCI refusing the
#: launch, the image import running out of time — and by the time any of them
#: raises there is no box left and the state is in one file.
AFTER_THE_TERMINATE = ['mint_dump_key', 'ensure_image', 'ensure_instance', 'attach_reserved_ip']


@pytest.mark.parametrize('stage', AFTER_THE_TERMINATE)
def test_a_failure_after_the_terminate_still_names_the_dump(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    """The window the published exit statuses depend on being closed.

    `provision` tells a reader that a run which says nothing about a dump left
    the old box serving. That is only true if every way out of the stretch
    after the termination says something — and that stretch is minutes to an
    hour long, with an image import in the middle of it.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    def explode(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError(f'{stage} refused')

    from kluster.scripts.credentials import b2

    monkeypatch.setattr(b2 if stage == 'mint_dump_key' else provision, stage, explode)

    with pytest.raises(RuntimeError, match=f'{stage} refused'):
        _ = _run()

    # The box is gone, so the dump is the only copy of the state there is.
    assert recorder.terminated == 1
    (taken,) = recorder.dumped
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)


def test_a_terminate_that_raises_part_way_still_names_the_dump(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The terminate asks OCI to destroy the box, then waits for it to be gone.

    A failure in the waiting half — a timeout, a service error — leaves the
    request already sent, so the box is going away whatever the exception
    says. That is why the run counts itself as having destroyed something
    before the call rather than after it.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    monkeypatch.setattr(provision, 'terminate_instance', _returning_raise('timed out waiting for TERMINATED'))

    with pytest.raises(RuntimeError, match='timed out waiting for TERMINATED'):
        _ = _run()

    (taken,) = recorder.dumped
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)


def test_a_run_that_destroyed_nothing_says_nothing_about_a_dump(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the promise, and the one that makes silence readable.

    A run that fails before it terminates anything must not print a restore
    instruction: an operator who saw one would go looking for state that never
    left the box.
    """
    caplog.set_level(logging.WARNING)
    recorder = _Recorder(instance_exists=False)
    converge(recorder)
    monkeypatch.setattr(provision, 'ensure_image', _returning_raise('the image import timed out'))

    with pytest.raises(RuntimeError, match='the image import timed out'):
        _ = _run()

    assert recorder.terminated == 0
    assert not any('state-backend restore' in message for message in caplog.messages)


def _returning_raise(message: str) -> Callable[..., Any]:
    def stub(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError(message)

    return stub


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
    'find_instance': 'looking for a box',
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


# -- what the command line reaches the converge as ---------------------------


def test_every_provision_flag_reaches_the_converge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The seam between the parser and the handler, which nothing else covers.

    A flag the parser accepts and `main` then drops is worse than one that
    does not exist: it is accepted, echoed back in `--help`, and silently
    ignored. `--dump-output` is the one that matters — dropped, the dump lands
    in the working directory rather than where the operator sent it.
    """
    seen: dict[str, Any] = {}

    def record(_store: object, **kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, '_provision', record)
    monkeypatch.setattr(cli.KdbxStore, 'from_env', classmethod(_returning(object())))
    monkeypatch.setattr(cli.escrow.Registry, 'open', classmethod(_returning(escrow.Registry(root=tmp_path))))
    asked = tmp_path / 'taken.dump.age'
    slot = tmp_path / 'other-slot'

    assert cli.main(['provision', '--force', '--dump-output', str(asked), '--bundle', str(slot)]) == 0

    assert seen['dump_output'] == asked
    assert seen['bundle_dir'] == slot
    assert (seen['force'], seen['replace'], seen['dump']) == (True, False, True)

    # And the flags that turn things off arrive turned off.
    seen.clear()
    assert cli.main(['provision', '--replace', '--no-dump']) == 0
    assert (seen['force'], seen['replace'], seen['dump']) == (False, True, False)
    assert seen['dump_output'] is None


def _provision_help() -> str:
    """`provision --help`, without letting argparse exit the test process."""
    for action in cli.build_parser()._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            chosen = cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)  # pyright: ignore[reportPrivateUsage]
            return chosen.choices['provision'].format_help()
    raise AssertionError('the parser grew no subcommands')


def test_the_replaced_status_is_published_in_help() -> None:
    # A number a wrapper branches on has to be readable without opening the
    # source. `deploy/state-backend/README.md` carries the same statement for
    # a reader who is not at a terminal.
    help_text = _provision_help()

    assert str(cli.RESTORE_PENDING) in help_text
    assert 'state-backend restore' in help_text


# -- finding the box among everything OCI still lists -------------------------


class _Page:
    """One page of an OCI list response, in the shape `oci.pagination` reads.

    The two token attributes are the whole point of the class: a stand-in
    without them answers a paginated call and an unpaginated one identically,
    which is how a listing that reads one page passes for one that reads all
    of them.
    """

    def __init__(self, data: list[Any], *, next_page: str | None = None) -> None:
        self.data: list[Any] = data
        self.next_page: str | None = next_page
        self.has_next_page: bool = next_page is not None
        self.status: int = 200
        self.headers: dict[str, str] = {}
        self.request: Any = None


def _instance(state: str = 'RUNNING') -> Any:
    """A box under the appliance's name, in the state given."""
    return type('Instance', (), {'display_name': f'{settings.NAME}-vm', 'lifecycle_state': state})()


class _PagedCompute:
    """A compartment whose instance list runs to more than one page."""

    def __init__(self, pages: list[list[Any]]) -> None:
        self.pages: list[list[Any]] = pages
        self.asked: list[str | None] = []

    def list_instances(self, _compartment_id: str, **kwargs: object) -> _Page:
        token = cast('str | None', kwargs.get('page'))
        self.asked.append(token)
        index = int(token) if token is not None else 0
        following = str(index + 1) if index + 1 < len(self.pages) else None
        return _Page(self.pages[index], next_page=following)


def test_the_running_box_is_found_behind_a_page_of_terminated_ones() -> None:
    """The compartment lists the boxes that ever were, not the one that is.

    A terminated instance stays in the listing and this box is cattle, so the
    list grows by one on every replacement while the answer stays a single
    box — and the running one is the newest, which is to say the furthest
    from the first page. Everything that reads this answer reads a miss as
    "no appliance": no approval in front of a replacement, no dump of the box
    about to be destroyed, and an escrow that mints a fresh CA and age
    identity over a live one.
    """
    graveyard = [_instance('TERMINATED') for _ in range(3)]
    compute = _PagedCompute([graveyard, [_instance()]])
    clients = cast('Any', type('Clients', (), {'compute': compute, 'compartment_id': 'ocid1.compartment.test'})())

    found = provision.find_instance(clients)

    assert found is not None and found.lifecycle_state == 'RUNNING'
    # The second page was asked for with the token the first one returned.
    assert compute.asked == [None, '1']


# -- what the launch actually puts on the box --------------------------------


class _Compute:
    """Enough of the compute client to watch one launch: no box, then one."""

    def __init__(self) -> None:
        self.launched: Any = None

    def list_instances(self, _compartment_id: str, **_kwargs: object) -> _Page:
        return _Page([])

    def launch_instance(self, details: Any) -> Any:
        self.launched = details
        return type('Response', (), {'data': type('Instance', (), {'id': 'ocid1.instance.launched'})()})()

    def get_instance(self, _instance_id: str) -> Any:
        return type('Response', (), {'data': type('Instance', (), {'lifecycle_state': 'RUNNING'})()})()


def test_a_launch_puts_the_whole_bill_of_materials_on_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the box carries is the only thing the next converge can read.

    A value the run computes and then does not attach is invisible: the box
    comes back reporting nothing for it, the next converge calls that drift,
    and — for the expiry — every run after it reports the same replacement,
    which once forced hands the operator a restore. So this drives the real launch and reads the
    metadata off the request, rather than off a fake that was handed the
    values.
    """
    # Which availability domain offers the shape is a question for OCI and
    # not part of what a launch records.
    monkeypatch.setattr(provision, '_shape_domain', _returning('phx-ad-1'))
    compute = _Compute()
    clients = cast('Any', type('Clients', (), {'compute': compute, 'compartment_id': 'ocid1.compartment.test'})())

    instance_id = provision.ensure_instance(
        clients,
        subnet_id='subnet',
        nsg_id='nsg',
        image_id='image',
        ignition='ignition',
        digests=CURRENT,
        dump_key_id='key-id',
        server_cert_expiry=FRESH,
    )

    assert instance_id == 'ocid1.instance.launched'
    metadata = cast('dict[str, str]', compute.launched.metadata)
    assert json.loads(metadata[provision.CONFIG_METADATA]) == CURRENT
    assert metadata[provision.DUMP_KEY_METADATA] == 'key-id'
    assert metadata[provision.EXPIRY_METADATA] == FRESH
    # And the loop closes: what the launch wrote is what the reader gets back.
    assert provision.instance_config(type('Instance', (), {'metadata': metadata})()) == provision.InstanceConfig(
        digests=CURRENT, dump_key_id='key-id', server_cert_expiry=FRESH
    )


# -- what a live appliance forbids -------------------------------------------


@pytest.fixture
def empty_escrow(tmp_path: Path) -> escrow.Vault:
    """A kit with a registry that holds nothing — a bring-up, or a wrong path."""
    kit = MemoryKit()
    registry = escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    return escrow.Vault.open(kit, registry)


def test_an_empty_escrow_beside_a_running_box_refuses_to_generate(empty_escrow: escrow.Vault) -> None:
    """`--escrow` pointed at the wrong directory must not mint a second CA.

    Generating one rebuilds the box under an authority no client bundle chains
    to and encrypts its dumps to a recipient no object in retention was
    written to. Both halves of the recovery story break at once, and neither
    shows until it is needed.
    """
    with pytest.raises(escrow.EscrowError, match=f'nothing escrowed for {escrow.CA}'):
        _ = config.Roots.ensure(empty_escrow, appliance_exists=True)

    # Nothing was written on the way to the refusal.
    for label in config.Roots.labels():
        assert empty_escrow.registry.generations(label) == []


# -- the certificate the box is walking towards the end of ---------------------

#: A fixed clock, so a boundary case is a boundary case rather than a race
#: against the second the test runs in (`config.renewal_due` takes `now` for
#: exactly this).
NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc)


def test_the_recorded_expiry_is_the_certificate_s_death_not_its_birth() -> None:
    """A box recording its issuance date would ask to be replaced forever.

    Which is the flapping the threshold exists to rule out, and no converge
    test can see it: they all replace this function with a stand-in.
    """
    roots = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=('age1example',))
    built = config.machine(roots, address='192.0.2.10', dump_key_id='key-id', dump_key='secret', bucket_id='bucket')

    recorded = dt.datetime.fromisoformat(config.expires_at(built))

    ahead = recorded - dt.datetime.now(dt.timezone.utc)
    assert abs(ahead - pki.LEAF_VALIDITY) < dt.timedelta(days=1)
    # Which is what makes a freshly built box no reason to touch anything --
    # the other end of the same value.
    assert config.renewal_due(config.expires_at(built)) is None


def test_a_certificate_with_life_left_is_no_reason_to_do_anything() -> None:
    outside = NOW + config.RENEWAL_MARGIN + dt.timedelta(days=1)

    assert config.renewal_due(outside.isoformat(), now=NOW) is None


def test_a_certificate_inside_the_margin_says_when_it_dies() -> None:
    inside = NOW + config.RENEWAL_MARGIN - dt.timedelta(days=1)

    reason = config.renewal_due(inside.isoformat(), now=NOW)

    assert reason is not None
    assert 'expires on' in reason and str(config.RENEWAL_MARGIN.days) in reason


def test_a_certificate_already_dead_says_so() -> None:
    # `in -3 days` is not something to print at the worst possible moment.
    reason = config.renewal_due((NOW - dt.timedelta(days=3)).isoformat(), now=NOW)

    assert reason is not None and reason.startswith('the server certificate expired on')


def test_an_expiry_that_is_not_a_date_is_read_as_no_expiry_at_all() -> None:
    # A value nothing can parse is the same evidence as no value: none.
    assert config.renewal_due('the day after tomorrow', now=NOW) is not None
