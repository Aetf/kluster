"""Properties of the provisioner that are not about talking to OCI.

The boundaries and the refusals, since the happy path is a cloud call. A
lookup that must not create what it cannot find, because `provision` is
otherwise full of `ensure_*` functions that do; where the box's own
credential comes from; what a converge decides about a running box, and what
it refuses to do to one without being asked.
"""

from __future__ import annotations

import argparse
import ast
import base64
import builtins
import datetime as dt
import functools
import gzip
import importlib
import importlib.util
import json
import logging
import os
import shutil
import time
import types
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from memory_kit import MemoryKit

from oci_conventions import with_recorded_compartment, with_unrecorded_compartment
from kluster import conventions
from kluster.scripts.credentials import b2, escrow, oci_iam, oci_slot, pki, workstation
from kluster.scripts.credentials.delivery import Delivery
from kluster.scripts.credentials.masters import CredentialRejected
from kluster.scripts.state_backend import cli, config, provision, settings
from kluster.scripts.state_backend.state import StateError


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


class _Service:
    """One OCI service client over what a compartment holds, served a page at a time.

    `kinds` is each listing's answer, keyed by what the method lists (`vcns`
    for `list_vcns`, `public_ips` for `list_public_ips`), cut into pages of
    `page_size` that a reader has to ask for by token. Every call lands in
    `calls` by name, the writes included: a method the class does not define
    is recorded and answered with nothing, which is what lets a test hold a
    read to having written nothing. `create_public_ip` is the one write with
    a body, because the reserved-address converge runs for real over this
    fake; `allocates` is the address it hands out, and a fake without one
    refuses to make any, which is what a lookup that must not allocate is
    held against.
    """

    def __init__(
        self,
        calls: list[str],
        kinds: dict[str, list[Any]],
        *,
        page_size: int = 100,
        allocates: str | None = None,
    ) -> None:
        self.calls: list[str] = calls
        self.page_size: int = page_size
        self.allocates: str | None = allocates
        self.kinds: dict[str, list[Any]] = kinds
        self.created: int = 0

    @property
    def ips(self) -> list[Any]:
        return self.kinds.setdefault('public_ips', [])

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith('_'):
            raise AttributeError(method)

        def call(*_args: object, **kwargs: object) -> Any:
            self.calls.append(method)
            if not method.startswith('list_'):
                return _Page([])
            items = self.kinds.get(method.removeprefix('list_'), [])
            start = int(cast('str | None', kwargs.get('page')) or 0)
            following = start + self.page_size
            return _Page(items[start:following], next_page=str(following) if following < len(items) else None)

        return call

    def create_public_ip(self, *_args: object, **_kwargs: object) -> Any:
        self.calls.append('create_public_ip')
        self.created += 1
        if self.allocates is None:  # pragma: no cover
            raise AssertionError('a lookup created a reserved address')
        reserved = _ip(f'{settings.NAME}-ip', self.allocates)
        self.ips.append(reserved)
        return type('Response', (), {'data': reserved})()


class _Client:
    """The clients over one compartment, sharing one call log.

    `ips` is what the reservation listing answers; the other kinds are given
    by name (`_Service`) and are empty unless a case says otherwise.
    """

    def __init__(
        self,
        ips: list[Any],
        *,
        allocates: str | None = None,
        held: bool = True,
        page_size: int = 100,
        kinds: dict[str, list[Any]] | None = None,
    ) -> None:
        self.compartment_id: str = 'ocid1.compartment.test'
        #: The appliance's own compartment unless a case says otherwise, so
        #: that every case about the hold runs on the compartment it applies to.
        self.held: bool = held
        self.calls: list[str] = []
        kinds = {**(kinds or {}), 'public_ips': ips}
        self.network: _Service = _Service(self.calls, kinds, page_size=page_size, allocates=allocates)
        self.compute: _Service = _Service(self.calls, kinds, page_size=page_size)


def _ip(name: str, address: str, state: str = 'ASSIGNED') -> Any:
    return type(
        'PublicIp',
        (),
        {'id': f'ocid1.publicip.{address}', 'display_name': name, 'ip_address': address, 'lifecycle_state': state},
    )()


#: An address that is not the one the repository records, for every case
#: about a box that is somewhere else.
ELSEWHERE = '192.0.2.99'


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
    client = _Client([_ip('state-backend-ip', settings.ADDRESS)])

    assert provision.reserved_address(client) == settings.ADDRESS  # pyright: ignore[reportArgumentType]
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
    client = _Client([_ip('state-backend-ip', settings.ADDRESS, state='TERMINATED')])

    with pytest.raises(RuntimeError):
        _ = provision.reserved_address(client)  # pyright: ignore[reportArgumentType]


# -- the box is held to the address the repository records ---------------------
# `settings.ADDRESS` is what the probe dials from another repository, with no
# OCI credential to look anything up; the converge and `ssh` read the address
# off the reservation. The two agree only because every read of the
# reservation refuses an address the constant does not name -- on the
# appliance's own compartment, the only one the constant describes.

#: A release the stream metadata could name, for every survey that is not
#: about the image: the image is looked up under the release's name, so the
#: survey reads the stream before it reads the compartment.
ARTIFACT = provision.FcosArtifact(
    release='42.20260901.3.0', url='https://example.invalid/fcos.qcow2.xz', sha256='0' * 64
)


def _surveyed(**fields: Any) -> provision.Survey:
    """A snapshot that found nothing but `fields`."""
    blank: dict[str, Any] = dict.fromkeys(
        ('instance', 'vcn', 'gateway', 'subnet', 'security_group', 'public_ip', 'image')
    )
    return provision.Survey(fcos=ARTIFACT, **{**blank, **fields})


def _ensure_reserved_ip(client: Any) -> provision.ReservedAddress:
    """The converge's read of the reservation, as `_provision` wires it: surveyed, then ensured."""
    return provision.ensure_reserved_ip(client, _surveyed(public_ip=provision.find_reserved_ip(client)))


#: Every reader of the reservation. Each is held to the refusal below, so a
#: caller that reaches the address through either one cannot see a box the
#: repository does not name.
LOOKUPS = [provision.reserved_address, _ensure_reserved_ip]


@pytest.mark.parametrize('lookup', LOOKUPS, ids=lambda f: f.__name__)
def test_a_reservation_at_another_address_is_refused_naming_both(lookup: Callable[[Any], object]) -> None:
    """A moved box is a decision, not drift: the run stops and says where the two disagree.

    Both addresses, because which one is wrong is the operator's call --
    recording the found one when the move was meant, repointing the
    reservation when it was not -- and a line naming only one sends the
    reader to the wrong file.
    """
    client = _Client([_ip('state-backend-ip', ELSEWHERE)])

    with pytest.raises(RuntimeError) as refused:
        _ = lookup(client)

    assert ELSEWHERE in str(refused.value)
    assert settings.ADDRESS in str(refused.value)
    assert 'settings.ADDRESS' in str(refused.value)


@pytest.mark.parametrize('lookup', LOOKUPS, ids=lambda f: f.__name__)
def test_a_reservation_at_the_recorded_address_is_the_answer(lookup: Callable[[Any], object]) -> None:
    client = _Client([_ip('state-backend-ip', settings.ADDRESS)])

    answer = lookup(client)

    address = answer.address if isinstance(answer, provision.ReservedAddress) else answer
    assert address == settings.ADDRESS


@pytest.mark.parametrize('lookup', LOOKUPS, ids=lambda f: f.__name__)
def test_a_run_pointed_at_another_compartment_is_not_held(
    lookup: Callable[[Any], object], caplog: pytest.LogCaptureFixture
) -> None:
    """`--compartment` names another site, whose address the constant does not describe.

    Held, such a run would end at a refusal every time, with a repair --
    record the found address -- that overwrites the real site's line. So the
    address is taken as found, and the log says why the hold did not apply.
    """
    client = _Client([_ip('state-backend-ip', ELSEWHERE)], held=False)

    with caplog.at_level(logging.INFO):
        answer = lookup(client)

    address = answer.address if isinstance(answer, provision.ReservedAddress) else answer
    assert address == ELSEWHERE
    assert 'not held to settings.ADDRESS: --compartment names another site' in caplog.text


def test_a_fresh_reservation_is_held_the_same_way() -> None:
    """A reservation OCI just chose is refused on the same terms as one it found.

    The run cannot pick the address, so on a site provisioned for the first
    time the refusal is the step that tells the operator what to record; the
    reservation stands across it, and the next run finds it and continues.
    """
    client = _Client([], allocates=ELSEWHERE)

    with pytest.raises(RuntimeError, match=f'{ELSEWHERE}.*{settings.ADDRESS}'):
        _ = _ensure_reserved_ip(client)

    assert client.network.created == 1
    assert [ip.ip_address for ip in client.network.ips] == [ELSEWHERE]


def test_a_fresh_reservation_s_refusal_offers_the_one_repair_there_is() -> None:
    """Just reserved, the address has nothing to be repointed to: record it and re-run.

    A found reservation has two repairs, because either side may be the stale
    one; a reservation this run made has one, and a refusal offering the other
    sends a first-ever provision looking for a move that never happened.
    """
    fresh = _Client([], allocates=ELSEWHERE)
    found = _Client([_ip('state-backend-ip', ELSEWHERE)])

    with pytest.raises(RuntimeError) as reserved:
        _ = _ensure_reserved_ip(fresh)
    with pytest.raises(RuntimeError) as carried:
        _ = _ensure_reserved_ip(found)

    assert 'just reserved' in str(reserved.value)
    assert f'record {ELSEWHERE} as settings.ADDRESS and re-run' in str(reserved.value)
    assert 'repoint' not in str(reserved.value)
    assert 'repoint' in str(carried.value)
    assert 'just reserved' not in str(carried.value)


# -- where the appliance's own credential comes from -------------------------

COMPARTMENT = 'ocid1.compartment.oc1..appliance'
#: The OCID the appliance's compartment is recorded against here: the test's
#: own, so no case depends on what the live entry records, and distinct from
#: `COMPARTMENT` so a superseded configuration's answer cannot pass for it.
RECORDED_COMPARTMENT = 'ocid1.compartment.oc1..appliance-recorded'
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


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> str:
    """The appliance's compartment as `conventions` records it once it exists."""
    _ = with_recorded_compartment(monkeypatch, conventions.STATE_BACKEND, RECORDED_COMPARTMENT)
    return RECORDED_COMPARTMENT


def test_the_appliance_signs_as_the_key_minted_for_it(slots: Path, recorded: str) -> None:
    _ = _mint()

    client = provision.OciClients.load()

    # The slot is the signing configuration, and where the appliance may act is
    # a convention beside it: the mapping is the one place the compartment is
    # written down, so nothing can drift from it.
    assert client.compartment_id == recorded
    assert (client.config['user'], client.config['tenancy']) == (APPLIANCE_USER, APPLIANCE_TENANCY)


def _copy_slots(slots: Path, destination: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`.credentials/` copied into a checkout at another path, and read from there."""
    copied = destination / '.credentials'
    _ = shutil.copytree(slots, copied)
    monkeypatch.setattr(workstation, 'directory', lambda: copied)
    return copied


def test_a_slot_copied_to_a_checkout_at_another_path_provisions_as_it_is(
    slots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str
) -> None:
    _ = _mint()
    copied = _copy_slots(slots, tmp_path / 'elsewhere', monkeypatch)
    # The minting checkout is gone, so the `key_file` the configuration was
    # written with names nothing -- which is where a copy on another machine
    # stands.
    shutil.rmtree(slots)

    client = provision.OciClients.load()

    assert client.config['key_file'] == str(oci_slot.key_path())
    assert oci_slot.key_path().is_relative_to(copied)
    # Building an SDK client validates the configuration and loads the key,
    # which is everything short of a request.
    _ = client.identity


def test_the_slot_signs_with_the_key_beside_it_rather_than_the_one_it_names(
    slots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str
) -> None:
    minted = _mint()
    _ = _copy_slots(slots, tmp_path / 'elsewhere', monkeypatch)
    # The path the copy's configuration names now holds another key, as it
    # does once the minting checkout mints again: following the entry would
    # sign with a key whose fingerprint the configuration does not carry.
    _ = (minted.parent / oci_slot.KEY).write_text(oci_iam.generate_key().private_pem)

    config = provision.OciClients.load().config

    assert config['key_file'] == str(oci_slot.key_path())
    assert oci_iam.fingerprint(Path(config['key_file']).read_text()) == config['fingerprint']


def test_a_slot_missing_its_key_names_the_repair(slots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A configuration copied without the PEM beside it: the entry it was written
    # with still opens a key, in the checkout it came from, and is not used.
    _ = _mint()
    _ = _copy_slots(slots, tmp_path / 'elsewhere', monkeypatch)
    oci_slot.key_path().unlink()

    with pytest.raises(oci_slot.SlotUnusable, match='copy the whole slot directory'):
        _ = provision.OciClients.load()


def test_the_slot_answers_with_its_credential_and_nothing_added_to_it(slots: Path, recorded: str) -> None:
    # A key pasted into the file would outrank the PEM beside it in the SDK's
    # signer, so the slot would sign as something other than its own key.
    written = _mint()
    with written.open('a') as handle:
        _ = handle.write('key_content = not-the-key-beside-it\n')

    config = provision.OciClients.load().config

    assert set(config) == {*oci_slot.CREDENTIAL, 'key_file'}


def test_a_slot_whose_profile_lost_a_field_names_the_repair(slots: Path) -> None:
    written = _mint()
    _ = written.write_text('\n'.join(line for line in written.read_text().splitlines() if 'fingerprint' not in line))

    with pytest.raises(oci_slot.SlotUnusable, match='no fingerprint in its'):
        _ = provision.OciClients.load()


def test_a_slot_that_does_not_parse_names_the_repair(slots: Path) -> None:
    _ = _mint().write_text('user = no section header above this line\n')

    with pytest.raises(oci_slot.SlotUnusable, match='does not parse'):
        _ = provision.OciClients.load()


def test_a_slot_with_no_compartment_to_act_in_is_not_told_to_name_one_in_the_slot(
    slots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The slot answers with its credential alone, so a `compartment-id`
    # written into it would be ignored: the refusal names only repairs that
    # take.
    _ = with_unrecorded_compartment(monkeypatch, conventions.STATE_BACKEND)
    _ = _mint()

    with pytest.raises(conventions.CompartmentMissing) as refused:
        _ = provision.OciClients.load()

    assert '--compartment' in str(refused.value)
    assert 'compartment-id' not in str(refused.value)


def test_an_explicit_compartment_wins_over_the_convention(slots: Path) -> None:
    _ = _mint()

    client = provision.OciClients.load('ocid1.compartment.oc1..elsewhere')

    # The drill escape: a run against a tenancy that is not this installation's
    # names its own compartment, because none of the mapping applies there --
    # the recorded address included.
    assert client.compartment_id == 'ocid1.compartment.oc1..elsewhere'
    assert client.held is False


def test_the_appliance_s_own_compartment_is_the_held_one(slots: Path, recorded: str) -> None:
    """Only the compartment `conventions` records is held to `settings.ADDRESS`.

    The mapping's default is held; the same compartment named explicitly is
    the same site, so it is held too; any other names another site.
    """
    _ = _mint()

    assert provision.OciClients.load().held is True
    assert provision.OciClients.load(recorded).held is True
    assert provision.OciClients.load('ocid1.compartment.oc1..elsewhere').held is False


def test_the_superseded_configuration_is_read_once_and_loudly(
    slots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, recorded: str
) -> None:
    # A workstation that predates the mint keeps provisioning: what is at the
    # old path is a complete answer, and the warning names its replacement.
    # Moving the file out of the slot is what makes it the old one -- it names
    # its key absolutely, so it goes on working from anywhere.
    superseded = tmp_path / 'legacy' / 'config'
    superseded.parent.mkdir()
    _mint().rename(superseded)
    # A hand-written configuration carried the compartment in the same file,
    # and that value still wins over the one `conventions` records.
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
    with pytest.raises(oci_slot.SlotUnusable, match='credentials derived oci-state-backend mint'):
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
        retire_fails: bool = False,
        address: str = settings.ADDRESS,
    ) -> None:
        self.instance_exists: bool = instance_exists
        #: What the reservation in the compartment carries. The lookup that
        #: reads it runs for real over a fake network, so a converge is held
        #: to the recorded address on the same path an operator's run is.
        self.address: str = address
        self.metadata: dict[str, str] = {} if metadata is None else metadata
        self.dump_key_current: bool = dump_key_current
        self.dump_fails: bool = dump_fails
        #: Whether retiring the dump key's predecessor raises. It runs after
        #: the launch, so a raise there leaves a new box running.
        self.retire_fails: bool = retire_fails
        #: The running box the compartment lists: the one a case starts with,
        #: then whichever a launch put there.
        self.instance_id: str = 'ocid1.instance.existing'
        #: Every instance the run pointed the reserved address at, in order.
        self.attached: list[str] = []
        self.minted: int = 0
        self.retired: int = 0
        self.terminated: int = 0
        self.launched: int = 0
        #: Bucket converges. The first thing a run creates at a provider, so
        #: the refusal that has to come before anything is created comes
        #: before this.
        self.buckets_converged: int = 0
        self.launched_metadata: dict[str, str] = {}
        #: Every host-key pin the run placed on this machine, as the address
        #: it was written against.
        self.pinned: list[tuple[Path, str, str]] = []
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

#: The account the fake seed authorizes as, and the one `conventions` records
#: for every converge below but the one that says otherwise: a converge proves
#: the two agree before it creates anything.
ACCOUNT_ID = 'account-fbb1a7'


#: A fixed clock, so a boundary case is a boundary case rather than a race
#: against the second the test runs in (`config.renewal_due` takes `now` for
#: exactly this). Every expiry below is written against it, and every converge
#: reads them against it too.
NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc)


def _expiry(days: int) -> str:
    return (NOW + dt.timedelta(days=days)).isoformat()


#: A certificate with most of its life ahead of it, as a box records it. Well
#: outside `config.RENEWAL_MARGIN`, so it is not what any of these tests vary.
FRESH = _expiry(1000)


#: The public half of the SSH host key a render hands the launch. Written out
#: rather than minted, because no case below is about the key's contents --
#: what they are about is that this exact value reaches the launch, the
#: metadata, and the `known_hosts` file the tool writes.
PIN = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExamplefbb1a7'


def _built_from(
    digests: dict[str, str],
    *,
    dump_key_id: str = 'key-id',
    expiry: str = FRESH,
    host_key: str = PIN,
) -> dict[str, str]:
    return {
        provision.CONFIG_METADATA: json.dumps(digests, sort_keys=True),
        provision.DUMP_KEY_METADATA: dump_key_id,
        provision.EXPIRY_METADATA: expiry,
        provision.HOST_KEY_METADATA: host_key,
    }


def _b2_reads(_session: b2.Session, api: str, _body: dict[str, Any]) -> object:
    """B2 over a bucket that exists with no retention rule, for a run that must only read it.

    The one call answered is the bucket listing. Every other call a converge
    makes to B2 goes through a step the fixture below replaces -- the mint,
    the bucket converge, the dump key's check -- so any other call reaching
    here fails at the call, naming it.
    """
    if api == 'b2_list_buckets':
        rules: list[object] = []
        return {'buckets': [{'bucketId': 'bucket-id', 'lifecycleRules': rules}]}
    raise AssertionError(f'{api} was called by a run that must only read B2')


@pytest.fixture
def converge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    def install(recorder: _Recorder) -> None:
        def ensure_bucket(*_args: object, **_kwargs: object) -> str:
            recorder.buckets_converged += 1
            return 'bucket-id'

        def write_dump(destination: Path, *, bundle_dir: Path, recipients: Sequence[str]) -> None:
            if recorder.dump_fails:
                raise StateError('pg_dump against the box failed: connection refused')
            recorder.dumped.append(destination)
            recorder.bundles.append(bundle_dir)
            recorder.order.append('dump')

        def mint(*_args: object, **_kwargs: object) -> Delivery[b2.AppKey]:
            recorder.minted += 1

            def retire() -> None:
                if recorder.retire_fails:
                    raise RuntimeError('retire refused')
                recorder.retired += 1
                recorder.order.append('retire')

            return Delivery.of(b2.AppKey(key_id='key-id', key='key-secret'), retire)

        def find(*_args: object, **_kwargs: object) -> Any:
            if not recorder.instance_exists:
                return None
            return type('Instance', (), {'id': recorder.instance_id, 'metadata': recorder.metadata})()

        def terminate(*_args: object, **_kwargs: object) -> None:
            recorder.terminated += 1
            recorder.instance_exists = False
            recorder.order.append('terminate')

        def launch(
            *_args: object,
            digests: dict[str, str],
            dump_key_id: str,
            server_cert_expiry: str,
            ssh_host_key_pub: str,
            **_kwargs: object,
        ) -> str:
            recorder.launched += 1
            recorder.launched_metadata = _built_from(
                digests, dump_key_id=dump_key_id, expiry=server_cert_expiry, host_key=ssh_host_key_pub
            )
            recorder.order.append('launch')
            # What the next run finds: this box, carrying what it was built from.
            recorder.instance_exists = True
            recorder.instance_id = 'ocid1.instance.new'
            recorder.metadata = recorder.launched_metadata
            return 'ocid1.instance.new'

        def attach(*_args: object, instance_id: str, **_kwargs: object) -> None:
            recorder.attached.append(instance_id)

        def write_known_hosts(directory: Path, *, address: str, public_key: str) -> Path:
            recorder.pinned.append((directory, address, public_key))
            return directory / config.KNOWN_HOSTS_FILE

        def ensure_image(*_args: object, **_kwargs: object) -> str:
            recorder.order.append('image')
            return 'image'

        # The session is what the account check reads, so it is the one thing
        # the stand-in has to carry; `conventions` records the same account
        # for the same reason the real seed's does.
        session = b2.Session(account_id=ACCOUNT_ID, api_url='https://api.example', token='unused')
        monkeypatch.setattr(b2.Session, 'from_entry', staticmethod(_returning(session)))
        monkeypatch.setattr(b2.Session, 'post', _b2_reads)
        monkeypatch.setattr(
            conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id=ACCOUNT_ID)
        )
        monkeypatch.setattr(b2, 'ensure_bucket', ensure_bucket)
        monkeypatch.setattr(b2, 'mint_dump_key', mint)
        monkeypatch.setattr(b2, 'dump_key_is_current', _returning(recorder.dump_key_current))
        clients = _Client([_ip(f'{settings.NAME}-ip', recorder.address)])
        monkeypatch.setattr(provision.OciClients, 'load', classmethod(_returning(clients)))
        monkeypatch.setattr(provision, 'fcos_artifact', _returning(ARTIFACT))
        monkeypatch.setattr(
            provision, 'ensure_network', _returning(provision.Placement(vcn_id='vcn', subnet_id='subnet'))
        )
        monkeypatch.setattr(provision, 'ensure_security_group', _returning('nsg'))
        monkeypatch.setattr(provision, 'ensure_image', ensure_image)
        monkeypatch.setattr(provision, 'shape_availability_domain', _returning('phx-ad-1'))
        monkeypatch.setattr(provision, 'find_instance', find)
        monkeypatch.setattr(provision, 'terminate_instance', terminate)
        monkeypatch.setattr(provision, 'ensure_instance', launch)
        monkeypatch.setattr(provision, 'attach_reserved_ip', attach)
        monkeypatch.setattr(provision, 'wait_for_backend', _returning(True))
        monkeypatch.setattr(cli, '_write_dump', write_dump)
        monkeypatch.setattr(config, 'machine', _returning(object()))
        monkeypatch.setattr(config, 'render_ignition', _returning('ignition'))
        monkeypatch.setattr(config, 'host_public_key', _returning(PIN))
        monkeypatch.setattr(config, 'write_known_hosts', write_known_hosts)
        monkeypatch.setattr(config, 'expires_at', _returning(FRESH))
        monkeypatch.setattr(config, 'renewal_due', functools.partial(config.renewal_due, now=NOW))
        monkeypatch.setattr(config, 'digests', _returning(dict(CURRENT)))
        monkeypatch.setattr(config, 'client_bundle', _returning(object()))
        monkeypatch.setattr(config, 'write_client_bundle', _returning(None))
        # Recovering the escrow needs the offline key and the age binary;
        # what is under test is the converge, so the roots arrive already
        # opened and the CA is a stand-in nothing here signs with.
        roots = cli.config.Roots(ca=cast('Any', object()), age_recipients=())
        monkeypatch.setattr(cli.config.Roots, 'ensure', classmethod(_returning(roots)))
        monkeypatch.setattr(cli.escrow.Vault, 'open', classmethod(_returning(object())))
        # The workstation slot, where a replacement records the restore it
        # leaves owed: the case's own, never the checkout's.
        monkeypatch.setattr(workstation, 'directory', _returning(tmp_path / '.credentials'))

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
    force: bool = False,
    dump: bool = True,
    dump_output: Path | None = None,
    bundle_dir: Path = SLOT,
) -> int:
    """One `provision`, with no flag the case does not name.

    The plain run is the default because it is the operator's: a case that
    replaces a box says which of `--force` and `--replace` let it, so a gate
    that stopped honoring either one is read by the cases that pass it alone.
    """
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

    0 says the appliance is current and owes no restore, which over an empty
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
    # Everything a checkout needs locally is in the checkout (credentials.md
    # §1 rule 6), the bundle included: nothing lands under the home directory.
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

    assert _run(force=True) == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_box_whose_dump_key_b2_no_longer_has_is_replaced(converge: Any) -> None:
    # The secret exists only inside the Ignition the box booted with, so a key
    # that is gone (or re-scoped) cannot be handed over without a new box.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT), dump_key_current=False)
    converge(recorder)

    assert _run(force=True) == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_box_without_the_bookkeeping_is_replaced(converge: Any) -> None:
    # A box that cannot say what it was built from is not evidence that it
    # matches; silence converges rather than passing.
    recorder = _Recorder(instance_exists=True, metadata={})
    converge(recorder)

    assert _run(force=True) == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_replace_rebuilds_a_box_that_matches(converge: Any) -> None:
    # `--replace` is its own approval: asking for the rebuild is asking for
    # the termination, so it needs no `--force` beside it.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    assert _run(replace=True, force=False) == PENDING

    assert (recorder.terminated, recorder.minted, recorder.launched) == (1, 1, 1)


def test_a_first_run_mints_and_launches(converge: Any) -> None:
    recorder = _Recorder(instance_exists=False)
    converge(recorder)

    assert _run() == 0

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 1, 1)


@pytest.mark.parametrize('instance_exists', [False, True], ids=['first run', 'running box'])
def test_a_seed_for_another_account_is_refused_before_anything_is_created(
    converge: Any, monkeypatch: pytest.MonkeyPatch, instance_exists: bool
) -> None:
    """The one B2 key this command mints is held to the recorded account, and early.

    The check inside the mint is not enough here: the bucket is converged, the
    network is converged and the running box is terminated before the mint
    runs, and a seed for another account would create the bucket there and
    destroy the box for a key that lands somewhere the nightly dump is never
    read back from. So the run refuses on the same step it authorizes.
    """
    recorder = _Recorder(instance_exists=instance_exists, metadata=_built_from({'butane': 'stale'}))
    converge(recorder)
    monkeypatch.setattr(
        conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id='some-other-account')
    )

    # Both accounts and the repair: on this path the operator's next action is
    # to fix whichever of the two is stale and run `provision` again.
    with pytest.raises(CredentialRejected, match=f'{ACCOUNT_ID}.*some-other-account'):
        _ = _run()

    assert recorder.buckets_converged == 0
    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 0, 0)
    assert recorder.instance_exists == instance_exists


@pytest.mark.parametrize('instance_exists', [False, True], ids=['first run', 'running box'])
def test_a_converge_over_a_box_elsewhere_stops_before_anything_uses_the_address(
    converge: Any, instance_exists: bool
) -> None:
    """Nothing downstream of the address runs on a box the repository does not name.

    The certificate is issued for the address, the bundle and the pin are
    keyed by it and the box is dumped and terminated on the strength of it, so
    the refusal has to come at the read, not at any one consumer: a converge
    that adopted the found address would rebuild the box, write bundles and a
    pin against it, and leave the probe dialing the recorded one.
    """
    recorder = _Recorder(instance_exists=instance_exists, metadata=_built_from({'butane': 'stale'}), address=ELSEWHERE)
    converge(recorder)

    with pytest.raises(RuntimeError, match=f'{ELSEWHERE}.*{settings.ADDRESS}'):
        _ = _run()

    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 0, 0)
    assert recorder.pinned == []
    assert recorder.dumped == []


def test_the_converge_hands_the_launch_what_the_box_must_carry(converge: Any) -> None:
    """One half of the loop that lets a box built now read as current later.

    This is the wiring only — that `_provision` computes the values and passes
    them down. That the launch then *stores* them is a property of
    `ensure_instance`, which this fixture replaces, and has its own test.
    """
    recorder = _Recorder(instance_exists=False)
    converge(recorder)
    _ = _run()

    assert provision.instance_config(
        type('Instance', (), {'metadata': recorder.launched_metadata})()
    ) == provision.InstanceConfig(digests=CURRENT, dump_key_id='key-id', server_cert_expiry=FRESH)
    # Outside `InstanceConfig`, which is what the converge compares: this one
    # is read for its value, by the one command that connects over SSH.
    assert recorder.launched_metadata[provision.HOST_KEY_METADATA] == PIN


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

    assert _run(force=True) == PENDING

    assert recorder.order == ['image', 'dump', 'terminate', 'launch', 'retire']
    # And it is the artifact `state-backend restore` takes, under the name the
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

    assert _run(force=True) == 1

    # Past the approval, as far as the dump and no further: the image a
    # replacement stands on is converged first, and nothing after the dump ran.
    assert recorder.order == ['image']
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

    assert _run(force=True) == PENDING

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
        _ = _run(force=True)

    assert (recorder.minted, recorder.retired) == (1, 0)


def test_no_dump_replaces_a_box_that_cannot_be_dumped(converge: Any) -> None:
    # An unreachable box is what the rebuild path is the diagnosis for
    # (state-backend.md §6), and it is the one box no dump can be taken of.
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale), dump_fails=True)
    converge(recorder)

    assert _run(force=True, dump=False) == PENDING

    assert (recorder.order, recorder.terminated, recorder.launched) == (
        ['image', 'terminate', 'launch', 'retire'],
        1,
        1,
    )


def test_a_certificate_inside_the_renewal_margin_is_drift(converge: Any) -> None:
    """The failure this component exists for: 5432 going dark for every stack.

    Nothing else in the bill of materials can see it coming, because every
    other component is re-derived from the repository and the repository
    issues a certificate that is always young.
    """
    expiring = _built_from(CURRENT, expiry=_expiry(config.RENEWAL_MARGIN.days - 1))
    recorder = _Recorder(instance_exists=True, metadata=expiring)
    converge(recorder)

    assert _run(force=True) == PENDING

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

    assert _run(force=True) == PENDING

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

    assert _run(force=True) == PENDING

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

    assert _run(force=True, dump_output=asked) == PENDING

    assert recorder.dumped == [asked]


def test_the_dump_is_taken_over_the_bundle_the_operator_named(converge: Any, tmp_path: Path) -> None:
    # A bundle that is not the workstation slot is how a drill, or a checkout
    # that keeps its credentials elsewhere, dumps the box it is replacing.
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    elsewhere = tmp_path / 'other-slot'

    assert _run(force=True, bundle_dir=elsewhere) == PENDING

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

    assert _run(force=True, dump=False) == PENDING

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

    assert _run(force=True) == 1

    (taken,) = recorder.dumped
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)


# -- what the closing instruction says, from how far the run got --------------
# Past the terminate the operator's next move depends on what is standing: no
# new box seen means nothing known to restore into, a box that has not
# answered cannot take a restore yet, and one that answers is waiting for
# exactly that.

#: What the run says when it destroyed the box and saw no new one running.
NO_BOX = 'no new box was seen running'
#: What it says of a new box that is running and has not answered.
SILENT = 'has not answered'
#: What it says of a new box that answers over an empty database.
EMPTY = 'serves an empty database'
#: The re-run both of the other two name. From the commit the run built the
#: new box from, because from any other the re-run reads that box as drifted
#: and stops short of pointing the address at it.
RE_RUN = 're-run `state-backend provision` from this commit'
#: What it says of the dump key the destroyed box held, when retiring it failed.
LIVE_KEY = 'so it is still live'
#: What it says of that key when no new box was seen: whether the re-run
#: retires it depends on whether it finds a box this run launched.
FOUND_KEY = 'if the re-run finds a box this run launched'

#: A way to break a run, applied once the converge fixture is installed.
Breakage = Callable[[pytest.MonkeyPatch, _Recorder], None]

MODULES: dict[str, types.ModuleType] = {'provision': provision, 'b2': b2, 'config': config}


def _refuse(module: str, stage: str) -> Breakage:
    """`stage` of `module` raises `<stage> refused`."""

    def breakage(monkeypatch: pytest.MonkeyPatch, _recorder: _Recorder) -> None:
        monkeypatch.setattr(MODULES[module], stage, _returning_raise(f'{stage} refused'))

    return breakage


def _lose_the_launch(monkeypatch: pytest.MonkeyPatch, _recorder: _Recorder) -> None:
    """OCI accepts the launch, and the wait for the box to be running raises."""
    launch = cast('Callable[..., str]', provision.ensure_instance)

    def accepted_then_lost(*args: object, **kwargs: object) -> str:
        _ = launch(*args, **kwargs)
        raise RuntimeError('ensure_instance refused: the new instance never reached RUNNING')

    monkeypatch.setattr(provision, 'ensure_instance', accepted_then_lost)


def _silence_the_box(monkeypatch: pytest.MonkeyPatch, _recorder: _Recorder) -> None:
    """The new box runs and never answers the readiness probe."""
    monkeypatch.setattr(provision, 'wait_for_backend', _returning(False))


def _refuse_the_retirement(_monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """Retiring the dump key's predecessor, after the launch, raises."""
    recorder.retire_fails = True


#: Every way a run fails past the terminate without having seen a new box
#: running. Each leaves the state in one file, and none can say whether a box
#: will come up: a launch OCI accepted may yet.
BEFORE_A_NEW_BOX: dict[str, Breakage] = {
    'terminate_instance': _refuse('provision', 'terminate_instance'),
    'mint_dump_key': _refuse('b2', 'mint_dump_key'),
    'machine': _refuse('config', 'machine'),
    'ensure_instance': _refuse('provision', 'ensure_instance'),
    'ensure_instance after the launch': _lose_the_launch,
}


@pytest.mark.parametrize('breakage', list(BEFORE_A_NEW_BOX.values()), ids=list(BEFORE_A_NEW_BOX))
def test_a_failure_before_a_new_box_is_seen_says_to_provision_first(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, breakage: Breakage
) -> None:
    """With no box known to be running, the restore alone may fail against nothing.

    So the instruction names the provision that brings a box up where none is
    and points the address at one that is, before the restore that fills it --
    and says the old box may still stand, since the terminate may be what
    failed. It does not describe a new box it never saw.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    breakage(monkeypatch, recorder)

    with pytest.raises(RuntimeError, match='refused'):
        _ = _run(force=True)

    (taken,) = recorder.dumped
    (said,) = [message for message in caplog.messages if NO_BOX in message]
    assert RE_RUN in said
    assert 'old box may still be standing' in said
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)
    assert not any(EMPTY in message or SILENT in message for message in caplog.messages)
    # Past the mint, the dump key the destroyed box held is still live. A
    # re-run that finds no box launches one and retires it; one that finds
    # the box a lost launch left launches nothing, so the words say both.
    # Before the mint there is no successor, and nothing to say.
    past_the_mint = breakage not in (BEFORE_A_NEW_BOX['terminate_instance'], BEFORE_A_NEW_BOX['mint_dump_key'])
    assert any(FOUND_KEY in message for message in caplog.messages) == past_the_mint
    assert not any(LIVE_KEY in message for message in caplog.messages)


#: Every way a run ends with a new box running that it has not heard answer.
NOT_ANSWERED: dict[str, Breakage] = {
    'never answers': _silence_the_box,
    'attach raises': _refuse('provision', 'attach_reserved_ip'),
    'retire raises': _refuse_the_retirement,
}


@pytest.mark.parametrize('breakage', list(NOT_ANSWERED.values()), ids=list(NOT_ANSWERED))
def test_a_new_box_that_has_not_answered_is_named_as_one(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, breakage: Breakage
) -> None:
    """A box is running, so the restore has somewhere to go -- once it answers.

    Not "serves an empty database": it serves nothing yet, and a restore run
    now fails against it. Nor "no new box": the run names the one it launched,
    and the provision that points the address at it and waits again.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    breakage(monkeypatch, recorder)

    try:
        assert _run(force=True) == 1
    except RuntimeError as refused:
        assert 'refused' in str(refused)

    assert recorder.launched == 1
    (taken,) = recorder.dumped
    (said,) = [message for message in caplog.messages if SILENT in message]
    assert 'ocid1.instance.new' in said
    assert RE_RUN in said
    assert 'points the address at it' in said
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)
    assert not any(EMPTY in message or NO_BOX in message for message in caplog.messages)
    # The dump key the destroyed box held is named where its retirement is
    # what failed, and only there: every other breakage retired it.
    assert any(LIVE_KEY in message for message in caplog.messages) == (breakage is _refuse_the_retirement)


#: Every failure past a launch OCI accepted, after which the reserved address
#: may not point at the new box -- and with no ephemeral address, nothing
#: reaches it but through the reservation.
PAST_THE_LAUNCH: dict[str, Breakage] = {
    'the wait for the launch': _lose_the_launch,
    'the key retirement': _refuse_the_retirement,
    'the attach': _refuse('provision', 'attach_reserved_ip'),
}


@pytest.mark.parametrize('breakage', list(PAST_THE_LAUNCH.values()), ids=list(PAST_THE_LAUNCH))
def test_a_plain_re_run_points_the_address_at_the_box_a_failed_run_launched(
    converge: Any, monkeypatch: pytest.MonkeyPatch, breakage: Breakage
) -> None:
    """The way back from a failure past the launch is the command the run names.

    The re-run finds the new box, which matches the commit it was built from,
    and a run that leaves a matching box standing still points the address at
    it: otherwise it would wait on an address that reaches nothing, every time.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    ensure_instance, attach = provision.ensure_instance, provision.attach_reserved_ip
    breakage(monkeypatch, recorder)
    with pytest.raises(RuntimeError, match='refused'):
        _ = _run(force=True)
    assert recorder.attached == []
    monkeypatch.setattr(provision, 'ensure_instance', ensure_instance)
    monkeypatch.setattr(provision, 'attach_reserved_ip', attach)
    recorder.retire_fails = False

    # The box it points the address at is the empty one, so the restore the
    # stopped run named is still owed (the case below is about that).
    assert _run(force=False) == PENDING

    assert recorder.attached == ['ocid1.instance.new']
    assert (recorder.terminated, recorder.launched) == (1, 1)


def test_a_new_box_that_answers_is_named_as_empty(converge: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The shape the replacement is for: up, empty, and waiting for the dump."""
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run(force=True) == PENDING

    assert any(EMPTY in message and 'ocid1.instance.new' in message for message in caplog.messages)
    assert not any(SILENT in message or NO_BOX in message for message in caplog.messages)


# -- recovering from a replacement that stopped part way ----------------------
# The stopped run's last words name a re-run and a restore. Everything below
# drives that recovery on the same fixture, run after run: the compartment a
# run leaves is the one the next run finds.

#: Both boxes a re-run meets after a replacement that stopped part way: none,
#: because the run stopped before launching, so the re-run launches one; or
#: the new one, because it stopped after, so the re-run points the address at
#: it. Each answers over an empty database.
STOPPED_PART_WAY: dict[str, Breakage] = {
    'no box: the mint': _refuse('b2', 'mint_dump_key'),
    'the new box: the attach': _refuse('provision', 'attach_reserved_ip'),
}


@pytest.mark.parametrize('breakage', list(STOPPED_PART_WAY.values()), ids=list(STOPPED_PART_WAY))
def test_a_re_run_after_a_replacement_that_stopped_part_way_still_owes_the_restore(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, breakage: Breakage
) -> None:
    """The re-run the stopped run names is a step of the recovery, not its end.

    Its box answers and holds nothing, so 0 -- "current and holds its state"
    -- is the answer a script would stop at with every stack's state still in
    one file. It exits as the replacement would have, naming that file again.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    mint, attach = b2.mint_dump_key, provision.attach_reserved_ip
    breakage(monkeypatch, recorder)
    with pytest.raises(RuntimeError, match='refused'):
        _ = _run(force=True)
    (taken,) = recorder.dumped
    monkeypatch.setattr(b2, 'mint_dump_key', mint)
    monkeypatch.setattr(provision, 'attach_reserved_ip', attach)
    caplog.clear()

    assert _run() == PENDING

    assert (recorder.terminated, recorder.launched, recorder.attached) == (1, 1, ['ocid1.instance.new'])
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)


def _a_backend(monkeypatch: pytest.MonkeyPatch, stacks: Callable[[object], list[str]]) -> None:
    """The backend a restore talks to, listing what `stacks` answers; the load itself succeeds."""
    monkeypatch.setattr(cli.state, 'connection', _returning(type('Connection', (), {'url': 'postgres://box'})()))
    monkeypatch.setattr(cli.state, 'endpoint', _returning('box:5432'))
    monkeypatch.setattr(cli.state, 'stacks', stacks)
    monkeypatch.setattr(cli.state, 'encrypted', _returning(False))
    monkeypatch.setattr(cli.state, 'verify_dump', _returning(['TABLE public stacks']))
    monkeypatch.setattr(cli.state, 'pg_restore', _returning(None))


def _restore_into(bundle_dir: Path, source: Path) -> int:
    """`state-backend restore <source> --bundle <bundle_dir>`, over a backend that takes it.

    The backend lists no stacks before the load and one after, which is a
    restore that verifies; what is under test is what it does beside that.
    """
    return cli._restore(  # pyright: ignore[reportPrivateUsage]
        None,
        registry=escrow.Registry(root=Path('unused')),
        bundle_dir=bundle_dir,
        source=source,
        identity=None,
        force=False,
    )


def test_the_restore_over_the_slot_is_what_settles_it(
    converge: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replacement owes its restore until one lands in the backend its slot names.

    A restore into some other backend -- the rehearsal's scratch box, over a
    bundle of its own -- is not that restore, so the next converge still
    names it; the one over the workstation slot's bundle is, and the next
    converge is 0 again.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    assert _run(force=True) == PENDING
    (taken,) = recorder.dumped
    listed: list[list[str]] = []

    def stacks(_target: object) -> list[str]:
        listed.append([] if len(listed) % 2 == 0 else ['physical'])
        return listed[-1]

    _a_backend(monkeypatch, stacks)

    assert _restore_into(tmp_path / 'scratch', taken) == 0
    assert _run() == PENDING

    assert _restore_into(workstation.bundle_dir(), taken) == 0
    assert _run() == 0


def test_a_retirement_that_fails_names_the_key_it_left_live(converge: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The dump key the destroyed box held outlives it when its retirement fails.

    The re-run that recovers the new box launches nothing, so it mints and
    retires nothing either: the key stays live until the next run that
    launches a box. The last words say which key, and which run retires it.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale, dump_key_id='key-old'), retire_fails=True)
    converge(recorder)

    with pytest.raises(RuntimeError, match='retire refused'):
        _ = _run(force=True)

    (said,) = [message for message in caplog.messages if LIVE_KEY in message]
    assert 'key-old' in said
    assert 'the next run that launches a box retires it' in said
    assert '`state-backend provision --replace`' in said


def test_a_re_run_over_a_box_still_provisioning_is_told_to_re_run(
    converge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box a lost launch leaves can still be provisioning when the re-run finds it.

    It matches the commit, so the re-run points the address at it -- and a
    box OCI has not finished has no attached network interface to point at.
    The refusal says why and what to do, which an index into an empty listing
    did not.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    _lose_the_launch(monkeypatch, recorder)
    with pytest.raises(RuntimeError, match='never reached RUNNING'):
        _ = _run(force=True)
    # The attach runs for real: OCI lists the accepted box's interface as
    # still attaching, the state a launch shows before the box is running.
    monkeypatch.setattr(provision, 'attach_reserved_ip', WRITERS['attach_reserved_ip'])
    clients = cast('_Client', provision.OciClients.load())
    clients.compute.kinds['vnic_attachments'] = [
        type('Attachment', (), {'vnic_id': 'ocid1.vnic.new', 'lifecycle_state': 'ATTACHING'})()
    ]

    with pytest.raises(RuntimeError, match='still provisioning') as refused:
        _ = _run()

    assert 'Re-run `state-backend provision` from this commit' in str(refused.value)
    assert 'ocid1.instance.new' in str(refused.value)


def test_a_box_that_has_no_interface_listed_at_all_is_refused_the_same_way(converge: Any) -> None:
    # What OCI lists for a launch it has only just accepted: nothing yet.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)
    clients = provision.OciClients.load()

    with pytest.raises(RuntimeError, match='still provisioning'):
        WRITERS['attach_reserved_ip'](clients, instance_id=recorder.instance_id, public_ip_id='ocid1.publicip.box')


def test_a_no_dump_replacement_of_a_box_left_empty_names_the_dump_the_state_is_in(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The chain a re-run from a later commit sets off, ending where the state is.

    The box the stopped run launched no longer matches the commit, so the
    plain re-run stops; `--force` cannot dump the box; and `--no-dump` is
    what is left. The state is still in the dump the first run
    took, which is newer than any nightly object, so that is the file the
    refusal and the last words name -- not the newest object in B2.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    attach = provision.attach_reserved_ip
    _refuse('provision', 'attach_reserved_ip')(monkeypatch, recorder)
    with pytest.raises(RuntimeError, match='refused'):
        _ = _run(force=True)
    (taken,) = recorder.dumped
    monkeypatch.setattr(provision, 'attach_reserved_ip', attach)
    # The commit moves on before the re-run.
    monkeypatch.setattr(config, 'digests', _returning(dict(CURRENT) | {'butane': 'yyyy'}))
    assert _run() == 1
    # A box nothing has opened lists no table, so its dump fails.
    recorder.dump_fails = True
    caplog.clear()

    assert _run(force=True) == 1
    assert any(str(taken) in message and '--no-dump' in message for message in caplog.messages)

    caplog.clear()
    assert _run(force=True, dump=False) == PENDING
    assert recorder.terminated == 2
    # Said as the replacement starts, and again as its last words.
    assert any(message.startswith('--no-dump') and str(taken) in message for message in caplog.messages)
    assert any(f'state-backend restore {taken}' in message for message in caplog.messages)
    assert not any('so the state is the newest object in B2' in message for message in caplog.messages)
    assert not any('is lost' in message for message in caplog.messages)


def test_a_stale_record_is_not_read_as_an_empty_box(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The record says a restore is owed; it cannot say the box is still empty.

    The state went back from another checkout, so this one's record stands
    over a box serving it. Then the commit moves and Postgres dies -- the
    rebuild path §6 sends a dead backend to -- so `--force` cannot dump the
    box. Read as an empty box, the refusal would call `--no-dump` free and
    send the restore to the record's dump, losing everything since. So the
    refusal gives both readings, and names the record to delete and the
    nightly object that is then the state.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    assert _run(force=True) == PENDING
    (taken,) = recorder.dumped
    listed: list[list[str]] = []

    def stacks(_target: object) -> list[str]:
        listed.append([] if len(listed) % 2 == 0 else ['physical'])
        return listed[-1]

    _a_backend(monkeypatch, stacks)
    # Another checkout's restore: its own bundle, and this record untouched.
    assert _restore_into(tmp_path / 'another-checkout', taken) == 0
    monkeypatch.setattr(config, 'digests', _returning(dict(CURRENT) | {'butane': 'yyyy'}))
    recorder.dump_fails = True
    caplog.clear()

    assert _run(force=True) == 1

    (said,) = [message for message in caplog.messages if cli.RESTORE_OWED in message]
    assert str(taken) in said
    assert 'delete that file first' in said
    assert 'nightly' in said
    assert 'newest object in B2 rather than that dump' in said

    # And the replacement itself, as it starts, says the same of what it loses.
    caplog.clear()
    assert _run(force=True, dump=False) == PENDING
    (warned,) = [message for message in caplog.messages if message.startswith('--no-dump')]
    assert cli.RESTORE_OWED in warned
    assert 'loses what is not in the nightly object' in warned
    assert 'newest object in B2 rather than that dump' in warned


def test_a_box_that_dumps_but_cannot_list_its_stacks_is_not_replaced(
    converge: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Which dump holds the state is asked of the box, and a box that does not answer is not read as empty.

    The record stands over a box serving state that went back from another
    checkout; the dump of it passes, and then `pulumi stack ls` fails. Read as
    empty, the record would keep the old dump and the replacement would go on,
    naming the fresh dump that holds the state in one line only. So the run
    stops with nothing destroyed, and the record as it was.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    assert _run(force=True) == PENDING
    record = workstation.bundle_dir() / cli.RESTORE_OWED
    before = record.read_text()
    monkeypatch.setattr(config, 'digests', _returning(dict(CURRENT) | {'butane': 'yyyy'}))

    def unanswered(_target: object) -> list[str]:
        raise StateError('pulumi stack ls: connection reset by peer')

    _a_backend(monkeypatch, unanswered)

    assert _run(force=True, dump_output=tmp_path / 'fresh.dump.age') == 1

    assert recorder.terminated == 1
    assert record.read_text() == before


def test_a_dump_of_a_box_left_empty_does_not_replace_the_record(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The first run's dump is where the state is, until a box serves a stack.

    Anything that opens the empty box -- `restore`'s own check, a `pulumi`
    preview -- creates the backend's table, so its dump lists one and passes
    while holding no stack. A record that gave way to it would name a dump
    whose restore brings nothing back, and the first dump nowhere.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    attach = provision.attach_reserved_ip
    _refuse('provision', 'attach_reserved_ip')(monkeypatch, recorder)
    with pytest.raises(RuntimeError, match='refused'):
        _ = _run(force=True)
    monkeypatch.setattr(provision, 'attach_reserved_ip', attach)
    monkeypatch.setattr(config, 'digests', _returning(dict(CURRENT) | {'butane': 'yyyy'}))
    _a_backend(monkeypatch, _returning([]))
    caplog.clear()

    assert _run(force=True, dump_output=Path('second.dump.age')) == PENDING

    first, second = recorder.dumped
    assert any(f'state-backend restore {first}' in message for message in caplog.messages)
    assert not any(f'state-backend restore {second}' in message for message in caplog.messages)
    caplog.clear()
    assert _run() == PENDING
    assert any(f'state-backend restore {first}' in message for message in caplog.messages)


def test_a_lost_launch_names_the_key_its_re_run_leaves_live(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OCI accepted the launch and the wait was lost: minted, not retired, not seen.

    The re-run finds that box, which matches, so it launches nothing and
    retires nothing: the key the destroyed box held stays live past it, and
    the last words have to say so without a box to name.
    """
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale, dump_key_id='key-old'))
    converge(recorder)
    _lose_the_launch(monkeypatch, recorder)

    with pytest.raises(RuntimeError, match='never reached RUNNING'):
        _ = _run(force=True)

    assert (recorder.minted, recorder.retired) == (1, 0)
    (said,) = [message for message in caplog.messages if FOUND_KEY in message]
    assert 'key-old' in said
    assert '`state-backend provision --replace`' in said


def test_a_terminate_that_takes_the_box_and_then_raises_still_owes_the_restore(
    converge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record goes down before the terminate, because the terminate can raise late.

    Its wait for TERMINATED can give out after OCI has already taken the
    box. The re-run then finds no box and launches one, which answers over an
    empty database -- the restore is owed exactly as if the run had gone on.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    terminate = cast('Callable[..., None]', provision.terminate_instance)

    def taken_then_raises(*args: object, **kwargs: object) -> None:
        terminate(*args, **kwargs)
        raise RuntimeError('the old instance never reached TERMINATED within 15m00s')

    monkeypatch.setattr(provision, 'terminate_instance', taken_then_raises)
    with pytest.raises(RuntimeError, match='never reached TERMINATED'):
        _ = _run(force=True)

    assert _run() == PENDING
    assert recorder.launched == 1


def test_a_restore_that_fails_its_verification_leaves_the_restore_owed(
    converge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record goes only once Pulumi lists what came back: a load that
    # lands nothing is not the restore that was owed.
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    assert _run(force=True) == PENDING
    (taken,) = recorder.dumped
    _a_backend(monkeypatch, _returning([]))

    assert _restore_into(workstation.bundle_dir(), taken) == 1

    assert _run() == PENDING


def test_a_restore_refused_over_a_serving_backend_calls_the_record_stale(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Where the record stands over a backend that already serves stacks, the
    # step is deleting the record; `--force` would overwrite that state.
    caplog.set_level(logging.ERROR)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    assert _run(force=True) == PENDING
    (taken,) = recorder.dumped
    _a_backend(monkeypatch, _returning(['physical']))

    assert _restore_into(workstation.bundle_dir(), taken) == 1

    (said,) = [message for message in caplog.messages if 'record is stale' in message]
    assert 'not --force' in said
    assert _run() == PENDING


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


# -- what runs before the barrier --------------------------------------------
# The dump and the terminate are the barrier: past them the estate has no
# backend. Everything that can fail and does not need the old box gone runs
# ahead of them, so that its failure leaves the old box serving.

#: Every step of the groundwork a launch stands on, as the module that owns it
#: and its name. The image import is the long one.
GROUNDWORK = [
    ('b2', 'ensure_bucket'),
    ('provision', 'ensure_network'),
    ('provision', 'ensure_security_group'),
    ('provision', 'ensure_reserved_ip'),
    ('provision', 'ensure_image'),
    ('provision', 'shape_availability_domain'),
]


def test_the_image_is_converged_before_anything_is_destroyed(converge: Any) -> None:
    """A release not imported yet is a download, an upload and an import.

    None of it needs the old box gone, so none of it runs in the window where
    the estate has no backend.
    """
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)

    assert _run(force=True) == PENDING

    after = recorder.order[recorder.order.index('terminate') :]
    assert 'image' not in after
    assert recorder.order.index('image') < recorder.order.index('dump')


@pytest.mark.parametrize(('module', 'stage'), GROUNDWORK, ids=[stage for _, stage in GROUNDWORK])
def test_a_groundwork_failure_leaves_the_old_box_serving(
    converge: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, module: str, stage: str
) -> None:
    """The run stops with nothing destroyed, nothing dumped, and nothing to restore."""
    caplog.set_level(logging.WARNING)
    stale = dict(CURRENT) | {'butane': 'zzzz'}
    recorder = _Recorder(instance_exists=True, metadata=_built_from(stale))
    converge(recorder)
    monkeypatch.setattr(MODULES[module], stage, _returning_raise(f'{stage} refused'))

    with pytest.raises(RuntimeError, match=f'{stage} refused'):
        _ = _run(force=True)

    assert (recorder.terminated, recorder.minted, recorder.dumped) == (0, 0, [])
    assert recorder.instance_exists
    assert not any('state-backend restore' in message for message in caplog.messages)


# -- a run that leaves the box standing writes nothing but the address -------

#: The box's primary private IP, which a reservation attached to it points at.
PRIMARY = 'ocid1.privateip.box'


class _Unwritable(_Service):
    """`_Service` for a run that must only read: a write fails at the call, naming it.

    What a listing holds is `kinds`, as for `_Service`; a `get_` is answered
    as `_Service` answers it, except the route table, which is answered with
    no rules -- the state a converge would write into -- and the reservation,
    which points at `points_at`. The box has one VNIC whose primary private IP
    is `PRIMARY`. A write named in `allowed` is recorded rather than refused,
    and a repointed reservation is recorded with where it was pointed:
    `updated` holds each as the reservation's id and the private IP it now
    names.
    """

    def __init__(
        self,
        calls: list[str],
        kinds: dict[str, list[Any]],
        *,
        points_at: str = PRIMARY,
        allowed: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(calls, kinds)
        self.points_at: str = points_at
        self.allowed: frozenset[str] = allowed
        self.updated: list[tuple[str, str]] = []

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith(('list_', 'get_', '_')) or method in self.__dict__.get('allowed', ()):
            return super().__getattr__(method)
        raise AssertionError(f'{method} was called by a run that must only read OCI')

    def create_public_ip(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError('create_public_ip was called by a run that must only read OCI')

    def update_public_ip(self, public_ip_id: str, details: Any) -> Any:
        if 'update_public_ip' not in self.allowed:
            raise AssertionError('update_public_ip was called by a run that must only read OCI')
        self.calls.append('update_public_ip')
        self.updated.append((public_ip_id, str(details.private_ip_id)))
        return _Page([])

    def get_route_table(self, *_args: object, **_kwargs: object) -> Any:
        self.calls.append('get_route_table')
        return type('Response', (), {'data': type('RouteTable', (), {'route_rules': []})()})()

    def list_vnic_attachments(self, *_args: object, **_kwargs: object) -> _Page:
        self.calls.append('list_vnic_attachments')
        return _Page([type('Attachment', (), {'vnic_id': 'ocid1.vnic.box', 'lifecycle_state': 'ATTACHED'})()])

    def list_private_ips(self, *_args: object, **_kwargs: object) -> _Page:
        self.calls.append('list_private_ips')
        return _Page([type('PrivateIp', (), {'id': PRIMARY, 'is_primary': True})()])

    def get_public_ip(self, *_args: object, **_kwargs: object) -> Any:
        self.calls.append('get_public_ip')
        return type('Response', (), {'data': type('PublicIp', (), {'private_ip_id': self.points_at})()})()


def _unwritable(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reserved: bool = True,
    points_at: str = PRIMARY,
    allowed: frozenset[str] = frozenset(),
) -> _Client:
    """A compartment holding everything under the appliance's names, loaded by the next run.

    `reserved` False leaves the reservation out. Every step that writes runs
    for real over it, rather than as the converge fixture's stand-in.
    """
    clients = _compartment()
    kinds = clients.network.kinds
    if not reserved:
        kinds['public_ips'] = []
    clients.network = _Unwritable(clients.calls, kinds, points_at=points_at, allowed=allowed)
    clients.compute = _Unwritable(clients.calls, kinds, points_at=points_at, allowed=allowed)
    monkeypatch.setattr(provision.OciClients, 'load', classmethod(_returning(clients)))
    for name, writer in WRITERS.items():
        monkeypatch.setattr(provision, name, writer)
    monkeypatch.setattr(b2, 'ensure_bucket', BUCKET_CONVERGE)
    return clients


def _b2_without_the_bucket(_session: b2.Session, api: str, _body: dict[str, Any]) -> object:
    """`_b2_reads` over an account that holds no dump bucket."""
    if api == 'b2_list_buckets':
        buckets: list[object] = []
        return {'buckets': buckets}
    raise AssertionError(f'{api} was called by a run that must only read B2')


#: The steps a converge writes through, as the module under test defines them.
#: Taken at import, before any fixture replaces them, so that the cases below
#: run them over their fakes rather than trusting the stand-ins not to write.
WRITERS: dict[str, Callable[..., Any]] = {
    name: getattr(provision, name)
    for name in ('ensure_network', 'ensure_security_group', 'ensure_reserved_ip', 'ensure_image', 'attach_reserved_ip')
}
BUCKET_CONVERGE = b2.ensure_bucket

DRIFTED = dict(CURRENT) | {'butane': 'zzzz'}


@pytest.mark.parametrize(
    ('digests', 'reserved', 'bucket', 'status', 'said'),
    [
        (CURRENT, True, True, 0, 'nothing to change'),
        (DRIFTED, True, True, 1, 'the machine definition changed: butane'),
        (CURRENT, False, True, 1, 'no reserved address carries the appliance name'),
        (CURRENT, True, False, 1, f'the bucket its dumps go to, {settings.B2_BUCKET}, does not exist'),
    ],
    ids=['matching box', 'drifted box', 'box without its reservation', 'box without its bucket'],
)
def test_a_plain_provision_writes_nothing_to_either_provider(
    converge: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    digests: dict[str, str],
    reserved: bool,
    bucket: bool,
    status: int,
    said: str,
) -> None:
    """A report is a read: without a flag, the run compares and leaves everything as it found it.

    Everything the appliance adopts by name is there, bar what a case leaves
    out, and the box is running with the reservation pointed at it. Nothing
    around it is as a converge would leave it -- no retention rule on the
    bucket, no route, no security rules -- so a run that converged before it
    judged would write here. A box whose reservation or bucket is missing is
    reported rather than given one.
    """
    caplog.set_level(logging.INFO)
    recorder = _Recorder(instance_exists=True, metadata=_built_from(digests))
    converge(recorder)
    clients = _unwritable(monkeypatch, reserved=reserved)
    if not bucket:
        monkeypatch.setattr(b2.Session, 'post', _b2_without_the_bucket)

    assert _run(force=False) == status

    assert [call for call in clients.calls if not call.startswith(('list_', 'get_'))] == []
    assert (recorder.terminated, recorder.minted, recorder.launched) == (0, 0, 0)
    assert any(said in message for message in caplog.messages)


def test_a_plain_provision_points_a_loose_reservation_back_at_a_matching_box(
    converge: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one write a run over a standing box makes, and only when it is needed.

    A reservation pointing anywhere but the box leaves the box with no public
    address at all -- it has no ephemeral one -- which is what a run that
    stopped between a launch and its attach leaves behind.
    """
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)
    clients = _unwritable(monkeypatch, points_at='ocid1.privateip.elsewhere', allowed=frozenset({'update_public_ip'}))
    (reservation,) = clients.network.ips

    assert _run(force=False) == 0

    assert [call for call in clients.calls if not call.startswith(('list_', 'get_'))] == ['update_public_ip']
    # And the write is the repair rather than a call: the reservation now
    # names the box's primary private IP, not wherever it pointed before.
    assert cast('_Unwritable', clients.network).updated == [(reservation.id, PRIMARY)]


def _returning_raise(message: str) -> Callable[..., Any]:
    def stub(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError(message)

    return stub


def test_a_run_that_built_a_box_leaves_its_pin_beside_the_bundle(converge: Any) -> None:
    """The operator's next `state-backend ssh` has the answer without a fetch.

    The reserved address outlives the box, so a replace hands the same address
    a different identity. Writing the one this run delivered is what keeps the
    change from being something an operator has to decide about.
    """
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    _ = _run(replace=True)

    assert [(address, public_key) for _directory, address, public_key in recorder.pinned] == [(recorder.address, PIN)]


def test_a_run_that_built_no_box_writes_no_pin(converge: Any) -> None:
    # Nothing about the box changed, and `ssh` re-reads the pin from the
    # instance's metadata on every exec -- so a file written here could only
    # ever restate what the box already says.
    recorder = _Recorder(instance_exists=True, metadata=_built_from(CURRENT))
    converge(recorder)

    assert _run() == 0

    assert recorder.pinned == []


def test_the_readiness_probe_closes_its_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the wait can never finish on a workstation.

    `openssl s_client` keeps the connection open reading stdin once the
    handshake is done, so with a terminal inherited from the operator's shell
    a *successful* probe hangs until the timeout and is reported as no answer.
    On a machine that came up in 90 seconds this looked like packets being
    dropped for as long as the operator was willing to wait.

    The clock is the test's, advanced only by the wait's own `sleep`, so the
    one-second budget bounds a fake that never answers to a single probe and
    is not a second of wall time: a process stalled between computing the
    deadline and checking it -- swap, a contended machine -- cannot decide a
    case whose subject is the probe's stdin.
    """
    seen: dict[str, object] = {}
    clock = [0.0]

    def fake_run(argv: list[str], **kwargs: object) -> Any:
        seen.update(kwargs)
        seen['argv'] = argv
        return type('Completed', (), {'returncode': 0, 'stderr': ''})()

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(provision.sp, 'run', fake_run)
    monkeypatch.setattr(provision.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provision.time, 'sleep', nap)

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
    """The longest silence in a provision run is the one after the launch.

    The ceiling in the announcement is the wait's `timeout`, and the clock
    the wait reads it against is the test's, advanced only by the wait's own
    `sleep`: fifteen minutes here is sixty probes of a fake that never
    answers, not a second of wall time.
    """
    caplog.set_level(logging.INFO)
    said: list[str] = []
    probes: list[int] = []
    clock = [0.0]

    def fake_run(_argv: list[str], **_kwargs: object) -> Any:
        probes.append(1)
        said.extend(caplog.messages)
        return type('Completed', (), {'returncode': 0, 'stderr': ''})()

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(provision.sp, 'run', fake_run)
    monkeypatch.setattr(provision.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provision.time, 'sleep', nap)

    assert provision.wait_for_backend('192.0.2.10', timeout=900) is True, f'gave up after {len(probes)} probes'
    announcement = next(message for message in said if 'waiting' in message)
    # The condition, the retry cadence, why it is slow, and the ceiling.
    assert '192.0.2.10' in announcement
    assert 'every 15s' in announcement
    assert 'minutes' in announcement
    assert '15m00s' in announcement


def test_the_readiness_wait_that_gives_up_says_what_it_last_saw_and_where_to_look(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The wait's failure is the run's last word, and it has to be a lead.

    A port that never answers has two very different causes -- a box still
    starting or broken, and a route dropping the packets -- and the wait
    cannot tell them apart from here. So it reports the last thing the probe
    said, verbatim, and the one command that separates the two from another
    host. A `False` with neither is an operator left to guess.

    The clock is the test's, advanced only by the wait's own `sleep`: a
    one-minute budget here is four refusals of a fake, not a minute of wall
    time.
    """
    caplog.set_level(logging.ERROR)
    clock = [0.0]
    probe: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> Any:
        probe[:] = argv
        return type('Completed', (), {'returncode': 1, 'stderr': '\nconnect: Connection refused\n'})()

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(provision.sp, 'run', fake_run)
    monkeypatch.setattr(provision.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provision.time, 'sleep', nap)

    assert provision.wait_for_backend('192.0.2.10', timeout=60) is False

    assert 'last attempt said: connect: Connection refused' in caplog.messages
    separating = next(message for message in caplog.messages if 'broken box from a broken route' in message)
    # The command the operator runs is the probe the wait ran, spelled for a
    # shell with the stdin the probe closes -- so that the answer from another
    # host is comparable to the wait's own.
    assert f'{" ".join(probe)} </dev/null' in separating
    assert 'state-backend ssh' in separating


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


def test_a_refused_provision_is_one_line_and_no_traceback(
    converge: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider saying no is a decision, and `main` dresses it as one.

    The message carries the repair; a traceback buries it under the stack and
    reads as a crash at the appliance. B2 refusing the seed key is the case
    because it is the refusal `_provision` meets first.
    """
    converge(_Recorder(instance_exists=False))
    monkeypatch.setattr(cli.KdbxStore, 'from_env', classmethod(_returning(object())))
    monkeypatch.setattr(cli.escrow.Registry, 'open', classmethod(_returning(escrow.Registry(root=tmp_path))))

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise CredentialRejected('B2 refused the seed key: unauthorized')

    monkeypatch.setattr(cli.b2.Session, 'from_entry', staticmethod(refuse))
    caplog.set_level(logging.ERROR)

    assert cli.main(['provision']) == 1

    [record] = caplog.records
    assert record.getMessage() == 'B2 refused the seed key: unauthorized'
    assert record.exc_info is None


#: Where the walk below stops: a module outside this package is somebody
#: else's program, and what it raises is not a refusal of this one.
_OURS = 'kluster'


def _imports(tree: ast.Module, package: str) -> Iterator[str]:
    """Every module under `_OURS` a file names in an import, at any depth.

    A `from a.b import c` names `a.b` and may name the module `a.b.c`; which
    of those exist is settled by importing them, and a name that is not a
    module is an attribute the walk has no use for.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                base = importlib.util.resolve_name('.' * node.level + base, package)
            names = [base, *(f'{base}.{alias.name}' for alias in node.names)]
        else:
            continue
        for name in names:
            if name == _OURS or name.startswith(f'{_OURS}.'):
                yield name


def _closure(start: str) -> dict[str, ast.Module]:
    """Every module `start` reaches through imports, transitively, with its syntax tree."""
    found: dict[str, ast.Module] = {}
    pending = [start]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # An imported name that is not a module: an attribute, which the
            # walk has no use for. Any other failure is a real one.
            if exc.name != name:
                raise
            continue
        if module.__file__ is None:
            continue
        tree = ast.parse(Path(module.__file__).read_text())
        found[name] = tree
        pending.extend(_imports(tree, module.__package__ or name))
    return found


def _named(node: ast.expr, module: types.ModuleType) -> object:
    """What a dotted name in `module` refers to at module scope, or None."""
    if isinstance(node, ast.Name):
        return getattr(module, node.id, getattr(builtins, node.id, None))
    if isinstance(node, ast.Attribute):
        owner = _named(node.value, module)
        return None if owner is None else getattr(owner, node.attr, None)
    return None


def _raised(closure: dict[str, ast.Module]) -> dict[type[BaseException], set[str]]:
    """Each exception class this repository defines that the closure raises, and where."""
    raised: dict[type[BaseException], set[str]] = {}
    for name, tree in closure.items():
        module = importlib.import_module(name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            target = _named(node.exc.func, module)
            assert isinstance(target, type) and issubclass(target, BaseException), (
                f'{name}:{node.lineno} raises {ast.unparse(node.exc.func)}, which is not a class at module scope'
            )
            if target.__module__.startswith(f'{_OURS}.'):
                raised.setdefault(target, set()).add(name)
    return raised


def test_main_turns_every_refusal_the_program_can_raise_into_one_line() -> None:
    """`cli.REFUSALS` is a census of the import closure, held in both directions.

    Forward: every exception class this repository defines that some module
    `cli` reaches can raise is caught, so no refusal surfaces as a traceback.
    Backward: every member of the tuple is raised somewhere in that closure,
    so no member is a name nothing raises. The closure is the import graph
    rather than a call graph, which is what lets the test be written; the
    two members that over-approximation adds are named at the tuple.
    """
    raised = _raised(_closure(cli.__name__))
    assert raised, 'the walk found no raise of a repository exception, so it walked nothing'

    uncaught = {cls.__name__: sorted(where) for cls, where in raised.items() if not issubclass(cls, cli.REFUSALS)}
    assert not uncaught, f'raised on a path main reaches and not in cli.REFUSALS: {uncaught}'

    unraised = [member.__name__ for member in cli.REFUSALS if not any(issubclass(cls, member) for cls in raised)]
    assert not unraised, f'in cli.REFUSALS and raised nowhere main reaches: {unraised}'


def _provision_help() -> str:
    """`provision --help`, without letting argparse exit the test process."""
    for action in cli.build_parser()._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            chosen = cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)  # pyright: ignore[reportPrivateUsage]
            return chosen.choices['provision'].format_help()
    raise AssertionError('the parser grew no subcommands')


def test_the_replaced_status_is_published_in_help(monkeypatch: pytest.MonkeyPatch) -> None:
    # A number a wrapper branches on has to be readable without opening the
    # source. `deploy/state-backend/README.md` carries the same statement for
    # a reader who is not at a terminal. argparse wraps at the width
    # `shutil.get_terminal_size` reports and breaks inside `state-backend`; a
    # width nothing wraps at keeps the phrase whole on every terminal.
    monkeypatch.setenv('COLUMNS', '1000')
    help_text = _provision_help()

    assert str(cli.RESTORE_PENDING) in help_text
    assert 'state-backend restore' in help_text


# -- finding the box among everything OCI still lists -------------------------


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


# -- the survey: every adopt-by-name read, before the first write --------------
# One rule for all of them, held per kind so that a kind cannot fall out of it:
# every page is read, a terminated holder does not count, and two live holders
# stop the run naming both.

#: Each listing the survey reads: the `Survey` field it answers, and the
#: suffix the appliance's resource of that kind is named with.
NAMED = {
    'instances': ('instance', 'vm'),
    'vcns': ('vcn', 'vcn'),
    'internet_gateways': ('gateway', 'igw'),
    'subnets': ('subnet', 'subnet'),
    'network_security_groups': ('security_group', 'nsg'),
    'public_ips': ('public_ip', 'ip'),
    'images': ('image', f'fcos-{ARTIFACT.release}'),
}

#: What a listing says of a resource that is gone: an image says `DELETED`
#: where every other kind says `TERMINATED`.
GONE = {listing: 'DELETED' if listing == 'images' else 'TERMINATED' for listing in NAMED}


def _resource(listing: str, *, name: str | None = None, state: str = 'AVAILABLE', tag: str = 'one') -> Any:
    """One listed resource of the kind `listing` answers, under the appliance's name unless told otherwise."""
    _, suffix = NAMED[listing]
    return type(
        'Resource',
        (),
        {
            'id': f'ocid1.{listing}.{tag}',
            'display_name': f'{settings.NAME}-{suffix}' if name is None else name,
            'lifecycle_state': state,
            'ip_address': settings.ADDRESS,
        },
    )()


def _clients(kinds: dict[str, list[Any]], *, page_size: int = 100) -> Any:
    """`_Client` over `kinds`, as the untyped SDK boundary hands it to the survey."""
    return cast('Any', _Client(kinds.pop('public_ips', []), page_size=page_size, kinds=kinds))


def _compartment(ahead: dict[str, list[Any]] | None = None, *, page_size: int = 100) -> Any:
    """One live resource of every kind under the appliance's name, each listed behind what `ahead` puts before it."""
    ahead = ahead or {}
    return _clients({listing: [*ahead.get(listing, []), _resource(listing)] for listing in NAMED}, page_size=page_size)


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FCOS stream metadata, answered without the network."""
    monkeypatch.setattr(provision, 'fcos_artifact', _returning(ARTIFACT))


@pytest.mark.usefixtures('stream')
def test_the_survey_reads_every_page_of_every_listing() -> None:
    """A resource on the second page is found, for every kind the survey reads.

    A stranger fills the first page of each listing, so a read of one page
    answers absent for all of them -- and absent is the answer that creates a
    duplicate.
    """
    strangers = {listing: [_resource(listing, name='someone-elses', tag='stranger')] for listing in NAMED}
    clients = _compartment(strangers, page_size=1)

    found = provision.survey(clients)

    for listing, (field, _) in NAMED.items():
        assert getattr(found, field) is not None, f'{listing}: the second page was not read'


@pytest.mark.usefixtures('stream')
@pytest.mark.parametrize('listing', list(NAMED))
def test_two_live_holders_of_the_name_are_refused_naming_both(listing: str) -> None:
    """The first of two is not an answer: the run stops and says which two."""
    clients = _compartment({listing: [_resource(listing, tag='other')]})

    with pytest.raises(RuntimeError) as refused:
        _ = provision.survey(clients)

    _, suffix = NAMED[listing]
    assert f'{settings.NAME}-{suffix}' in str(refused.value)
    assert f'ocid1.{listing}.other' in str(refused.value)
    assert f'ocid1.{listing}.one' in str(refused.value)


@pytest.mark.usefixtures('stream')
@pytest.mark.parametrize('listing', list(NAMED))
def test_a_holder_of_the_name_that_is_gone_is_not_adopted(listing: str) -> None:
    """A listing keeps what was terminated; the survey answers absent for it."""
    kinds = {other: [_resource(other)] for other in NAMED if other != listing}
    kinds[listing] = [_resource(listing, state=GONE[listing])]
    clients = _clients(kinds)

    found = provision.survey(clients)

    field, _ = NAMED[listing]
    assert getattr(found, field) is None


@pytest.mark.usefixtures('stream')
@pytest.mark.parametrize('clients', [_clients({}), _compartment()], ids=['nothing exists', 'everything exists'])
def test_the_survey_writes_nothing(clients: Any) -> None:
    """Every call the survey makes is a listing, whether it finds everything or nothing."""
    _ = provision.survey(clients)

    assert clients.calls, 'the survey read nothing'
    assert [call for call in clients.calls if not call.startswith('list_')] == []


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


def _launch(compute: Any) -> str:
    """The real launch, over `compute` and nothing else of OCI."""
    clients = cast('Any', type('Clients', (), {'compute': compute, 'compartment_id': 'ocid1.compartment.test'})())
    return provision.ensure_instance(
        clients,
        subnet_id='subnet',
        nsg_id='nsg',
        image_id='image',
        # Which availability domain offers the shape is a question for OCI and
        # not part of what a launch records.
        availability_domain='phx-ad-1',
        ignition='ignition',
        digests=CURRENT,
        dump_key_id='key-id',
        server_cert_expiry=FRESH,
        ssh_host_key_pub=PIN,
    )


def test_a_launch_puts_the_whole_bill_of_materials_on_the_box() -> None:
    """What the box carries is the only thing the next converge can read.

    A value the run computes and then does not attach is invisible: the box
    comes back reporting nothing for it, the next converge calls that drift,
    and — for the expiry — every run after it reports the same replacement,
    which once forced hands the operator a restore. So this drives the real launch and reads the
    metadata off the request, rather than off a fake that was handed the
    values.
    """
    compute = _Compute()

    instance_id = _launch(compute)

    assert instance_id == 'ocid1.instance.launched'
    metadata = cast('dict[str, str]', compute.launched.metadata)
    assert json.loads(metadata[provision.CONFIG_METADATA]) == CURRENT
    assert metadata[provision.DUMP_KEY_METADATA] == 'key-id'
    assert metadata[provision.EXPIRY_METADATA] == FRESH
    assert metadata[provision.HOST_KEY_METADATA] == PIN
    # And the loop closes: what the launch wrote is what the reader gets back.
    assert provision.instance_config(type('Instance', (), {'metadata': metadata})()) == provision.InstanceConfig(
        digests=CURRENT, dump_key_id='key-id', server_cert_expiry=FRESH
    )


def test_a_launch_gives_the_box_no_address_but_the_reserved_one() -> None:
    """An ephemeral public address is a second way in that nothing here names.

    The reservation, the host-key pin and the certificate's SAN are all
    written against the reserved address; a box also reachable at another one
    is reachable where none of them apply.
    """
    compute = _Compute()

    _ = _launch(compute)

    assert compute.launched.create_vnic_details.assign_public_ip is False


def test_a_launch_turns_off_the_unauthenticated_metadata_endpoint() -> None:
    """The legacy metadata endpoint serves the instance's metadata without a token.

    That metadata is the Ignition, with the box's secrets in it, handed to
    anything on the box that asks.
    """
    compute = _Compute()

    _ = _launch(compute)

    assert compute.launched.instance_options.are_legacy_imds_endpoints_disabled is True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The provisioner's clock, advanced only by its own `sleep`.

    A wait's budget is then a number of polls rather than of seconds, so a
    case about what a wait does with an answer never races the machine it
    runs on.
    """
    now = [0.0]

    def nap(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(provision.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(provision.time, 'sleep', nap)
    return now


def _instance_in(state: str) -> Any:
    """A `get_instance` response for an instance in `state`."""
    return type('Response', (), {'data': type('Instance', (), {'lifecycle_state': state})()})()


def _polled(*answers: Any) -> tuple[Callable[[], Any], list[int]]:
    """A `fetch` for `_await_state` that gives `answers` in order, raising any that is an exception.

    The second value counts the polls, so a case can tell a wait that ended
    on an answer from one that ran out of budget after it.
    """
    remaining = list(answers)
    polls: list[int] = []

    def fetch() -> Any:
        polls.append(1)
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return fetch, polls


def _service_error(status: int) -> Any:
    """What the SDK raises for an OCI error answer, reached through the module that catches it."""
    headers: dict[str, str] = {}
    return provision.oci.exceptions.ServiceError(status, 'NotAuthorizedOrNotFound', headers, 'not yet visible')


@pytest.mark.usefixtures('clock')
def test_a_wait_rides_out_the_404_a_young_resource_answers_with() -> None:
    # OCI answers 404 for a resource it has just accepted until it is
    # visible to reads, which on a new tenancy takes a while; a wait that
    # gave up on the first one would lose an import it had started.
    fetch, polls = _polled(_service_error(404), _service_error(404), _instance_in('RUNNING'))

    resource = provision._await_state(fetch, 'RUNNING', what='the box')  # pyright: ignore[reportPrivateUsage]

    assert resource.lifecycle_state == 'RUNNING'
    assert len(polls) == 3


@pytest.mark.usefixtures('clock')
def test_a_wait_does_not_ride_out_any_other_error() -> None:
    # The control for the case above: only the 404 is transient.
    fetch, _ = _polled(_service_error(500))

    with pytest.raises(provision.oci.exceptions.ServiceError):
        _ = provision._await_state(fetch, 'RUNNING', what='the box')  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures('clock')
def test_a_wait_ends_at_a_failed_state_by_naming_it() -> None:
    """A resource that failed does not recover, and the wait says so at once.

    Rather than polling it for the rest of the budget -- up to an hour for an
    image -- and then reporting a timeout, which sends the reader looking for
    something slow instead of something broken.
    """
    fetch, polls = _polled(_instance_in('PROVISIONING'), _instance_in('FAILED'))

    with pytest.raises(RuntimeError, match='the box ended in FAILED'):
        _ = provision._await_state(fetch, 'RUNNING', what='the box')  # pyright: ignore[reportPrivateUsage]

    assert len(polls) == 2


class _Terminating:
    """Enough of the compute client to watch one termination: the call, then the instance going away."""

    def __init__(self) -> None:
        self.terminated: list[tuple[str, dict[str, object]]] = []
        self.states: list[str] = ['TERMINATING', 'TERMINATED']

    def terminate_instance(self, instance_id: str, **kwargs: object) -> Any:
        self.terminated.append((instance_id, kwargs))
        return None

    def get_instance(self, _instance_id: str) -> Any:
        return _instance_in(self.states.pop(0) if len(self.states) > 1 else self.states[0])


@pytest.mark.usefixtures('clock')
def test_a_terminated_box_takes_its_boot_volume_with_it() -> None:
    """A preserved boot volume is a second copy of the state, kept by nobody.

    It would outlive every replacement, one more per rebuild, holding
    whatever the box held -- the database included -- outside the dump's
    retention and the escrow's recipients.
    """
    compute = _Terminating()
    clients = cast('Any', type('Clients', (), {'compute': compute})())

    provision.terminate_instance(clients, 'ocid1.instance.old')

    assert compute.terminated == [('ocid1.instance.old', {'preserve_boot_volume': False})]
    # And the call returns once the box is gone, not once it was asked to go.
    assert compute.states == ['TERMINATED']


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


def test_the_recorded_expiry_is_the_certificate_s_death_not_its_birth() -> None:
    """A box recording its issuance date would ask to be replaced forever.

    Which is the flapping the threshold exists to rule out, and no converge
    test can see it: they all replace this function with a stand-in.
    """
    roots = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=('age1example',))
    built = config.machine(
        roots, address='192.0.2.10', dump_key_id='key-id', dump_key='secret', bucket_id='bucket', now=NOW
    )

    recorded = dt.datetime.fromisoformat(config.expires_at(built))

    # Exact: x509 keeps whole seconds and `NOW` carries none.
    assert recorded - NOW == pki.LEAF_VALIDITY
    # Which is what makes a freshly built box no reason to touch anything --
    # the other end of the same value, read at the same instant.
    assert config.renewal_due(config.expires_at(built), now=NOW) is None


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


@pytest.fixture
def far_from_utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The process's local zone ten hours east of UTC, for the length of the case.

    What tells "read as UTC" apart from "read as local time" is a machine
    whose local time is not UTC; a CI runner's is, so the case sets one.
    """
    before = os.environ.get('TZ')
    monkeypatch.setenv('TZ', 'Etc/GMT-10')
    time.tzset()
    yield
    # `monkeypatch` restores the variable after this teardown, too late for
    # the zone the C library reads from it, so it is put back here first.
    if before is None:
        monkeypatch.delenv('TZ')
    else:
        monkeypatch.setenv('TZ', before)
    time.tzset()


@pytest.mark.usefixtures('far_from_utc')
def test_an_expiry_recorded_without_an_offset_is_read_as_utc() -> None:
    """An older render could record the expiry naive, and it still answers, in UTC.

    Compared as it is against an aware `now`, a naive value raises rather than
    answering, which ends the converge on a box that says nothing wrong. Read
    as the machine's local time, it moves by the zone's offset. UTC is what
    every writer of the field means, so a point six hours either side of the
    margin lands on the side it would with its offset written, ten hours from
    UTC or not.
    """
    outside = (NOW + config.RENEWAL_MARGIN + dt.timedelta(hours=6)).replace(tzinfo=None)
    inside = (NOW + config.RENEWAL_MARGIN - dt.timedelta(hours=6)).replace(tzinfo=None)

    assert config.renewal_due(outside.isoformat(), now=NOW) is None
    reason = config.renewal_due(inside.isoformat(), now=NOW)
    assert reason is not None and inside.date().isoformat() in reason


# -- the box's SSH identity: minted at render, delivered, pinned at exec ------

needs_butane = pytest.mark.skipif(shutil.which('butane') is None, reason='butane is not on PATH (mise x -- ...)')

#: The address every case in this section renders and dials: the one the
#: repository records, which is the only one `ssh` agrees to dial.
PINNED_ADDRESS = settings.ADDRESS

HOST_KEY_FILE = '/etc/ssh/ssh_host_ed25519_key'


class _Execed(Exception):
    """What stands in for `os.execvp` never returning."""


def _machine() -> config.Machine:
    roots = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=('age1example',))
    return config.machine(roots, address=PINNED_ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')


def _delivered(ignition: str, path: str) -> tuple[str, int]:
    """One file the rendered Ignition writes, as its text and its mode.

    Butane encodes an inline file as a `data:` URL and gzips it once it is
    worth gzipping, so a case that read the JSON straight would be asserting
    against whichever of the two shapes the value happened to take.
    """
    document: Any = json.loads(ignition)
    for entry in document['storage']['files']:
        if entry['path'] != path:
            continue
        head, _, body = str(entry['contents']['source']).partition(',')
        raw = base64.b64decode(body) if head.endswith(';base64') else urllib.parse.unquote_to_bytes(body)
        if entry['contents'].get('compression') == 'gzip':
            raw = gzip.decompress(raw)
        return raw.decode(), int(entry['mode'])
    raise AssertionError(f'the rendered Ignition writes no {path}')


def _public_half(private_key: str) -> str:
    key = serialization.load_ssh_private_key(private_key.encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )


def test_the_host_key_minted_at_every_render_is_not_drift() -> None:
    """Otherwise every converge would report drift and ask to replace the box.

    The key is random per render like the server key beside it, so digesting
    its value would make the bill of materials describe the render rather than
    the machine -- and a converge that always finds drift is a converge that
    always rebuilds.
    """
    roots = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=('age1example',))
    ask = functools.partial(config.digests, roots, address=PINNED_ADDRESS, dump_key_id='key-id', bucket_id='bucket')

    assert config.drift(ask(), ask()) == []


@needs_butane
def test_the_ignition_delivers_the_host_key_the_machine_carries() -> None:
    """The box gets its identity from the render rather than generating one.

    Which is what lets the launch record a pin for a box that has not booted
    yet. The public half rides along because the console banner's fingerprints
    are read from the `.pub` file, and that banner is the one way to check the
    pin against something the Ignition never touched.
    """
    built = _machine()

    ignition = config.render_ignition(built)

    private_key, mode = _delivered(ignition, HOST_KEY_FILE)
    # The file, not the value: an OpenSSH private key file ends in a newline.
    assert private_key == built.ssh_host_key + '\n'
    assert mode == 0o600
    public_key, public_mode = _delivered(ignition, f'{HOST_KEY_FILE}.pub')
    assert public_key.strip() == config.host_public_key(built)
    assert public_mode == 0o644


@needs_butane
def test_the_pin_the_launch_records_is_the_key_the_ignition_delivered() -> None:
    """A pin from a second render is a box locked out of its own diagnosis path.

    Both facts have to come off one `config.machine` call: the Ignition
    carries the private half, the launch metadata carries the public one, and
    nothing downstream can tell a pin on the wrong key from an interposer.
    """
    compute = _Compute()
    clients = cast('Any', type('Clients', (), {'compute': compute, 'compartment_id': 'ocid1.compartment.test'})())
    roots = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=('age1example',))

    launched = cli._launch_box(  # pyright: ignore[reportPrivateUsage]
        clients,
        roots,
        dump_key=b2.AppKey(key_id='key-id', key='key-secret'),
        ground=cli.Groundwork(
            bucket_id='bucket-id',
            placement=provision.Placement(vcn_id='vcn', subnet_id='subnet'),
            nsg_id='nsg',
            reserved=provision.ReservedAddress(id='ip-id', address=PINNED_ADDRESS),
            image_id='image',
            # Which availability domain offers the shape is a question for OCI.
            availability_domain='phx-ad-1',
        ),
    )

    metadata = cast('dict[str, str]', compute.launched.metadata)
    private_key, _mode = _delivered(base64.b64decode(metadata['user_data']).decode(), HOST_KEY_FILE)

    assert metadata[provision.HOST_KEY_METADATA] == _public_half(private_key)
    assert launched.host_public_key == _public_half(private_key)


def _running(pin: str, *, address: str = PINNED_ADDRESS) -> Any:
    """A compartment holding the appliance, at the address every case dials."""
    instance = type(
        'Instance',
        (),
        {
            'display_name': f'{settings.NAME}-vm',
            'lifecycle_state': 'RUNNING',
            'metadata': {provision.HOST_KEY_METADATA: pin} if pin else {},
        },
    )()
    return cast(
        'Any',
        type(
            'Clients',
            (),
            {
                'compute': _PagedCompute([[instance]]),
                'network': _Service([], {'public_ips': [_ip(f'{settings.NAME}-ip', address)]}),
                'compartment_id': 'ocid1.compartment.test',
                'held': True,
            },
        )(),
    )


@pytest.fixture
def execed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """`ssh` argv instead of an `ssh` process, with the slot under `tmp_path`."""
    captured: list[list[str]] = []

    def execvp(file: str, args: Sequence[str]) -> None:
        assert file == 'ssh'
        captured.append(list(args))
        raise _Execed

    monkeypatch.setattr(provision.os, 'execvp', execvp)
    monkeypatch.setattr(workstation, 'directory', lambda: tmp_path / '.credentials')
    return captured


def test_ssh_holds_the_box_to_the_key_its_instance_metadata_records(execed: list[list[str]], tmp_path: Path) -> None:
    """First contact is not trust-on-first-use: a wrong answer is refused.

    Every option here is load-bearing. Without the strict setting the client
    accepts an unknown key and writes it down; without a file of its own it
    would consult whatever this machine already trusts; without the algorithm
    the box's other host key types are answers the pin does not cover; and
    without `-F /dev/null` the client still reads a configuration whose
    `ControlMaster` or `KnownHostsCommand` decides the question instead
    (`test_ssh_pin.py` holds that one against a live client).
    """
    with pytest.raises(_Execed):
        provision.ssh(_running(PIN), ['journalctl', '-u', 'postgres'])

    argv = execed[0]
    assert argv[argv.index('-F') + 1] == '/dev/null'
    options = [argv[index + 1] for index, token in enumerate(argv) if token == '-o']
    assert 'StrictHostKeyChecking=yes' in options
    assert 'GlobalKnownHostsFile=/dev/null' in options
    assert 'HostKeyAlgorithms=ssh-ed25519' in options
    assert argv[-4:] == [f'core@{PINNED_ADDRESS}', 'journalctl', '-u', 'postgres']

    named = [option.removeprefix('UserKnownHostsFile=') for option in options if 'UserKnownHostsFile=' in option]
    assert len(named) == 1
    known_hosts = Path(named[0])
    # The tool's own file, in the slot that holds this box's client bundle --
    # never the operator's, which the tool neither reads nor writes.
    assert known_hosts == tmp_path / '.credentials' / 'state-backend' / config.KNOWN_HOSTS_FILE
    assert known_hosts.read_text() == f'{PINNED_ADDRESS} {PIN}\n'


def test_the_refusal_is_framed_before_the_connection_is_made(
    execed: list[list[str]], caplog: pytest.LogCaptureFixture
) -> None:
    """`os.execvp` replaces the process, so afterwards there is nobody to explain.

    And the explanation has two halves that lead to different actions: the box
    was replaced since the pin was read, or something is interposed on the
    path. A line naming only one of them sends the reader down one of them.
    """
    caplog.set_level(logging.INFO)

    with pytest.raises(_Execed):
        provision.ssh(_running(PIN), [])

    framing = [message for message in caplog.messages if 'interposed' in message]
    assert len(framing) == 1
    assert 'replaced' in framing[0]
    assert PIN in framing[0]


def test_ssh_refuses_a_box_elsewhere_before_it_pins_anything(execed: list[list[str]], tmp_path: Path) -> None:
    """The pin is keyed by the address, so a wrong address is refused before the pin is written.

    Otherwise `ssh` would hold the box to its host key at an address the
    repository never named, and leave that entry beside the bundle for the
    next exec to trust.
    """
    with pytest.raises(RuntimeError, match=f'{ELSEWHERE}.*{settings.ADDRESS}'):
        provision.ssh(_running(PIN, address=ELSEWHERE), [])

    assert execed == []
    assert not (tmp_path / '.credentials' / 'state-backend' / config.KNOWN_HOSTS_FILE).exists()


def test_a_box_that_records_no_host_key_is_refused_rather_than_trusted() -> None:
    """Silence is the state the pin exists to rule out.

    A box built before the pin was recorded would otherwise fall back to the
    behavior this replaced: whatever answers at the address is the box.
    """
    with pytest.raises(RuntimeError, match='records no SSH host key'):
        _ = provision.host_key_pin(_running(''))
