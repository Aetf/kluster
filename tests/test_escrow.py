"""The escrow, against the real `age` binary.

Everything here is a property that only shows up on the day it matters: that a
generated secret is recoverable at all, that a re-wrap loses nothing, that a
registry someone has damaged says so without a key, and that the two rotations
the model separates really are separate — a new recovery key changes no
plaintext, and a new generation changes no other label.
"""

from __future__ import annotations

import functools
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from cryptography import x509
from memory_kit import MemoryKit

from kluster.scripts.credentials import age, escrow, pki
from kluster.scripts.credentials.kdbx import KdbxStore

age_binary = shutil.which(age.BINARY)
needs_age = pytest.mark.skipif(age_binary is None, reason='age is not on PATH (mise x -- ...)')

pytestmark = needs_age


@pytest.fixture
def registry(tmp_path: Path) -> escrow.Registry:
    return escrow.Registry.open(tmp_path / 'escrow')


@pytest.fixture
def kit() -> KdbxStore:
    return MemoryKit()


@pytest.fixture
def vault(kit: KdbxStore, registry: escrow.Registry) -> escrow.Vault:
    _ = escrow.init(kit, registry)
    return escrow.Vault.open(kit, registry)


@functools.cache
def console_key() -> str:
    """One PEM for every console row in the file; generating a key is the slow part."""
    return pki.generate_ca_key()


def _fill(vault: escrow.Vault, label: str) -> str:
    """One label at its next generation, however that label's value comes about."""
    origin = escrow.register()[label].origin
    if isinstance(origin, escrow.Generated):
        return escrow.generate(vault, label)
    # A console row has nothing to draw: its value arrives from outside, which
    # is what `adopt` is for.
    value = console_key()
    _ = escrow.adopt(vault, label, value)
    return value


def _filled(vault: escrow.Vault) -> dict[str, str]:
    """Every register label at generation one, and the plaintexts filed."""
    return {label: _fill(vault, label) for label in escrow.register()}


def test_init_puts_the_private_half_in_the_kit_and_the_public_half_in_the_repo(
    kit: KdbxStore, registry: escrow.Registry
) -> None:
    identity = escrow.init(kit, registry)

    assert kit.get(escrow.RECOVERY_ENTRY) == identity.secret
    # The recipient is the row's public identifier, and the same string the
    # committed file names.
    assert kit.get(escrow.RECOVERY_ENTRY, attribute='UserName') == identity.public
    assert registry.recipients() == [identity.public]


def test_init_refuses_to_replace_a_recovery_key(kit: KdbxStore, registry: escrow.Registry) -> None:
    # Overwriting it is losing every ciphertext at once, so the only path that
    # replaces one is the rotation that re-wraps first.
    _ = escrow.init(kit, registry)

    with pytest.raises(escrow.EscrowError, match='already holds a recovery key'):
        _ = escrow.init(kit, registry)


def test_init_refuses_a_recipients_file_already_in_the_repo(kit: KdbxStore, registry: escrow.Registry) -> None:
    # A kit with no recovery row beside a committed recipients file: the file
    # belongs to a key some other kit holds, and replacing it would re-point
    # every future ciphertext away from the key that opens the existing ones.
    registry.set_recipients([age.generate().public])
    before = registry.recipients_file.read_text()

    with pytest.raises(escrow.EscrowError, match='already exists'):
        _ = escrow.init(kit, registry)

    assert registry.recipients_file.read_text() == before
    assert not kit.has(escrow.RECOVERY_ENTRY)


def test_a_generated_secret_comes_back_out(vault: escrow.Vault) -> None:
    minted = escrow.generate(vault, escrow.PASSPHRASE)

    assert vault.recover(escrow.PASSPHRASE) == minted


def test_a_secret_that_cannot_be_escrowed_is_never_minted(vault: escrow.Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # The safety property, from its failing side: encryption comes before the
    # value is handed over, so a caller can never hold a generated secret the
    # registry does not carry.
    def refuse(_plaintext: str, _recipients: Sequence[str]) -> str:
        raise age.AgeError('no recipient reachable')

    monkeypatch.setattr(age, 'encrypt', refuse)

    with pytest.raises(age.AgeError):
        _ = escrow.generate(vault, escrow.PASSPHRASE)

    assert vault.registry.generations(escrow.PASSPHRASE) == []


def test_generating_again_is_a_new_generation_beside_the_old_one(vault: escrow.Vault) -> None:
    first = escrow.generate(vault, escrow.PASSPHRASE)

    second = escrow.generate(vault, escrow.PASSPHRASE)

    assert first != second
    assert vault.registry.generations(escrow.PASSPHRASE) == [1, 2]
    # The predecessor is still openable: what production holds until the new
    # generation is adopted is generation one.
    assert vault.recover(escrow.PASSPHRASE, 1) == first
    assert vault.recover(escrow.PASSPHRASE) == second


def test_generations_count_as_numbers_past_nine(vault: escrow.Vault) -> None:
    # Ordered as text, `10.age` sorts before `2.age`: generation nine would
    # read as the latest, and the next write would be filed as ten, over the
    # ciphertext already there.
    minted = [escrow.generate(vault, escrow.PASSPHRASE) for _ in range(10)]
    assert vault.registry.generations(escrow.PASSPHRASE) == list(range(escrow.FIRST, escrow.FIRST + 10))
    assert vault.recover(escrow.PASSPHRASE) == minted[-1]

    eleventh = escrow.generate(vault, escrow.PASSPHRASE)

    assert vault.registry.latest(escrow.PASSPHRASE) == escrow.FIRST + 10
    assert vault.recover(escrow.PASSPHRASE) == eleventh
    assert vault.recover(escrow.PASSPHRASE, escrow.FIRST + 9) == minted[-1]


def test_one_labels_rotation_leaves_every_other_label_alone(vault: escrow.Vault) -> None:
    # The whole point of the model: rotating the passphrase is not also
    # rotating the CA.
    before = _filled(vault)

    _ = escrow.generate(vault, escrow.PASSPHRASE)

    for label, secret in before.items():
        if label != escrow.PASSPHRASE:
            assert vault.recover(label) == secret


def test_the_escrowed_ca_key_is_a_working_ca(vault: escrow.Vault) -> None:
    _ = escrow.generate(vault, escrow.CA)

    authority = pki.Authority.from_pem(vault.recover(escrow.CA))

    # It signs, and what it signs chains to it.
    leaf = x509.load_pem_x509_certificate(authority.issue_client('ci').cert_pem)
    leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(authority.certificate().cert_pem))


def test_an_escrowed_backup_identity_opens_what_it_encrypts(vault: escrow.Vault, tmp_path: Path) -> None:
    label = escrow.backup_labels()[0]
    _ = escrow.generate(vault, label)
    identity = vault.recover(label)
    path = tmp_path / 'dump.age'
    _ = path.write_text(age.encrypt('a pg_dump', [age.recipient(identity)]))

    assert age.decrypt(path, [identity]) == 'a pg_dump'


def test_a_backup_identity_label_takes_one_generation_for_its_lifetime(vault: escrow.Vault) -> None:
    label = escrow.backup_labels()[0]
    first = escrow.generate(vault, label)

    # The backup generation is the label, and every reader of it -- the
    # appliance's recipient list, the identities a restore opens a dump with --
    # takes the latest escrow generation alone (`state_backend.config`). A
    # second one would have the next provision run encrypt every dump to a key
    # no dump in retention was written to, so both writers refuse it and the
    # first generation is what the label goes on answering with.
    with pytest.raises(escrow.EscrowError, match='one value for its lifetime'):
        _ = escrow.generate(vault, label)
    with pytest.raises(escrow.EscrowError, match='one value for its lifetime'):
        _ = escrow.adopt(vault, label, age.generate().secret)

    assert vault.registry.generations(label) == [escrow.FIRST]
    assert vault.recover(label) == first


def test_every_backup_label_is_single_and_nothing_else_is() -> None:
    # The property is what the invariant credentials.md §2.2 states rests on,
    # and it holds for exactly the labels whose generation is in their name:
    # the rows that rotate by growing a generation must keep doing so.
    single = {label for label, row in escrow.register().items() if row.single}

    assert single == set(escrow.backup_labels())


def test_import_escrows_a_value_that_already_exists(vault: escrow.Vault) -> None:
    # The migration path: a live credential carries over unrotated.
    _ = escrow.adopt(vault, escrow.PASSPHRASE, 'the-live-passphrase')

    assert vault.registry.generations(escrow.PASSPHRASE) == [1]
    assert vault.recover(escrow.PASSPHRASE) == 'the-live-passphrase'


def test_import_appends_rather_than_overwriting(vault: escrow.Vault) -> None:
    # Importing onto a label that already holds something must not replace
    # what production is using; it becomes the next generation like any other.
    minted = escrow.generate(vault, escrow.PASSPHRASE)

    _ = escrow.adopt(vault, escrow.PASSPHRASE, 'from-elsewhere')

    assert vault.registry.generations(escrow.PASSPHRASE) == [1, 2]
    assert vault.recover(escrow.PASSPHRASE, 1) == minted
    assert vault.recover(escrow.PASSPHRASE) == 'from-elsewhere'


def test_import_after_import_appends_and_leaves_the_first_file_alone(vault: escrow.Vault) -> None:
    # Two imports in a row is the shape a retried ceremony step has, and the
    # retry must not land on top of the generation the first one wrote: the
    # file is compared byte for byte, because an overwrite that happened to
    # re-encrypt the same plaintext would still have destroyed the original.
    _ = escrow.adopt(vault, escrow.PASSPHRASE, 'the-live-passphrase')
    first = vault.registry.path(escrow.PASSPHRASE, 1)
    written = first.read_bytes()

    second = escrow.adopt(vault, escrow.PASSPHRASE, 'a-second-value')

    assert vault.registry.generations(escrow.PASSPHRASE) == [1, 2]
    assert second == vault.registry.path(escrow.PASSPHRASE, 2)
    assert first.read_bytes() == written
    assert vault.recover(escrow.PASSPHRASE, 1) == 'the-live-passphrase'
    assert vault.recover(escrow.PASSPHRASE, 2) == 'a-second-value'


@pytest.mark.parametrize('value', ['', '   ', '\n'])
@pytest.mark.parametrize('label', [escrow.PASSPHRASE, escrow.CA])
def test_import_refuses_a_value_that_is_not_there(vault: escrow.Vault, label: str, value: str) -> None:
    # A producer that crashed writes nothing and exits, and its traceback is
    # off the top of the screen by the time the import reports success. An
    # escrowed empty string is only discovered by the recovery that needed it.
    # Said as emptiness whatever the label expects: "this is not a PEM private
    # key" would send the operator looking at the wrong end of the pipe.
    with pytest.raises(escrow.EscrowError, match='empty'):
        _ = escrow.adopt(vault, label, value)

    assert vault.registry.generations(label) == []


def test_import_refuses_something_that_is_not_an_age_identity(vault: escrow.Vault) -> None:
    label = escrow.backup_labels()[0]

    # A wrong-but-non-empty pipe: the recipient rather than the identity is
    # the mistake the labels invite, and it survives every check but the shape.
    with pytest.raises(escrow.EscrowError, match='age identity'):
        _ = escrow.adopt(vault, label, age.generate().public)

    assert vault.registry.generations(label) == []


def test_import_takes_a_real_age_identity(vault: escrow.Vault) -> None:
    label = escrow.backup_labels()[0]
    identity = age.generate()

    _ = escrow.adopt(vault, label, identity.secret)

    assert vault.recover(label) == identity.secret


def test_import_refuses_something_that_is_not_a_private_key(vault: escrow.Vault) -> None:
    with pytest.raises(escrow.EscrowError, match='PEM private key'):
        _ = escrow.adopt(vault, escrow.CA, 'BEGIN PRIVATE KEY')

    assert vault.registry.generations(escrow.CA) == []


def test_import_refuses_a_private_key_that_stops_half_way(vault: escrow.Vault) -> None:
    truncated = pki.generate_ca_key()[:80]

    with pytest.raises(escrow.EscrowError, match='PEM private key'):
        _ = escrow.adopt(vault, escrow.CA, truncated)

    assert vault.registry.generations(escrow.CA) == []


def test_import_takes_a_real_ca_key(vault: escrow.Vault) -> None:
    key = pki.generate_ca_key()

    _ = escrow.adopt(vault, escrow.CA, key)

    assert vault.recover(escrow.CA) == key


def test_a_token_label_asks_only_that_there_be_a_value(vault: escrow.Vault) -> None:
    # Nothing recognisable about a passphrase or a bearer token, so the shape
    # is the empty check and no more: a check that guessed at length or
    # alphabet would refuse values the consumers accept.
    _ = escrow.adopt(vault, escrow.ALERTMANAGER, 'a-token-from-somewhere-else')

    assert vault.recover(escrow.ALERTMANAGER) == 'a-token-from-somewhere-else'


# --------------------------------------------------------------------------
# The rows made in a console: recorded rather than drawn, and probed rather
# than remembered.
# --------------------------------------------------------------------------


def console_rows() -> list[str]:
    """Every label whose value is made somewhere no API of this repository reaches."""
    return [label for label, row in escrow.register().items() if isinstance(row.origin, escrow.Console)]


def test_the_app_keys_are_console_rows_shaped_like_private_keys() -> None:
    # Both halves matter. Console, because nothing here can draw a GitHub App
    # key -- the tree must not offer a `generate` for one. A private-key shape,
    # because what a wrong pipe hands over is caught at the record rather than
    # on the day a workflow tries to sign a JWT with it.
    for label in (escrow.DISPATCH_KEY, escrow.TRIGGER_KEY):
        row = escrow.register()[label]

        assert isinstance(row.origin, escrow.Console)
        assert row.shape is escrow.PRIVATE_KEY
        assert row.verb == 'record'


@pytest.mark.parametrize('label', console_rows())
def test_a_console_row_cannot_be_drawn_here(vault: escrow.Vault, label: str) -> None:
    # Randomness would produce a PEM nothing on the platform has ever heard
    # of. The refusal names the command that does file one instead.
    with pytest.raises(escrow.EscrowError, match='made in a console'):
        _ = escrow.generate(vault, label)

    assert vault.registry.generations(label) == []


def test_recording_files_the_value_the_console_produced(vault: escrow.Vault) -> None:
    key = console_key()

    _ = escrow.record(vault, escrow.DISPATCH_KEY, key)

    assert vault.recover(escrow.DISPATCH_KEY) == key
    assert vault.registry.generations(escrow.DISPATCH_KEY) == [escrow.FIRST]


def test_recording_a_value_already_escrowed_files_no_second_copy(vault: escrow.Vault) -> None:
    # Idempotent by probing the product: the command that moves a key out of a
    # kit is re-run whenever anyone is unsure whether it ran, and a second
    # generation holding the same plaintext would make the registry claim a
    # rotation that never happened.
    key = console_key()
    first = escrow.record(vault, escrow.DISPATCH_KEY, key)

    again = escrow.record(vault, escrow.DISPATCH_KEY, f'{key}\n')

    assert again == first
    assert vault.registry.generations(escrow.DISPATCH_KEY) == [escrow.FIRST]


def test_recording_a_key_the_registry_has_never_seen_is_the_next_generation(vault: escrow.Vault) -> None:
    # The other side of the probe: a key generated on the App page after the
    # first one is a rotation, and rotating is exactly this command again.
    _ = escrow.record(vault, escrow.DISPATCH_KEY, console_key())
    successor = pki.generate_ca_key()

    _ = escrow.record(vault, escrow.DISPATCH_KEY, successor)

    assert vault.registry.generations(escrow.DISPATCH_KEY) == [1, 2]
    assert vault.recover(escrow.DISPATCH_KEY) == successor


def test_recording_something_that_is_not_a_private_key_is_refused(vault: escrow.Vault) -> None:
    with pytest.raises(escrow.EscrowError, match='PEM private key'):
        _ = escrow.record(vault, escrow.DISPATCH_KEY, 'Iv1.the-client-id')

    assert vault.registry.generations(escrow.DISPATCH_KEY) == []


def test_recording_a_row_that_is_drawn_here_is_refused(vault: escrow.Vault) -> None:
    # `record` is for a value this side cannot produce. Pointing it at the
    # passphrase would escrow whatever the operator pasted as a generation of
    # a credential every stack is encrypted under.
    with pytest.raises(escrow.EscrowError, match='drawn here'):
        _ = escrow.record(vault, escrow.PASSPHRASE, 'a-pasted-passphrase')

    assert vault.registry.generations(escrow.PASSPHRASE) == []


@pytest.mark.parametrize('label', console_rows())
def test_a_key_still_in_a_kit_is_read_out_of_it(vault: escrow.Vault, kit: KdbxStore, label: str) -> None:
    # The one path that reaches a kit for a §3 row, and it exists because a kit
    # written while these rows were seeds holds the only copy of each: the
    # alternative is a console visit that rotates a credential for no reason.
    origin = escrow.register()[label].origin
    assert isinstance(origin, escrow.Console)
    assert origin.kit is not None
    kit.put(origin.kit.entry, 'Iv1.the-client-id', '')
    kit.attach(origin.kit.entry, origin.kit.filename, console_key().encode())

    _ = escrow.record(vault, label, escrow.from_kit(kit, label))

    # Filed as the value and nothing else: an attachment carries whatever
    # trailing newline the download had, and a secret is stored the way every
    # other one here is.
    assert vault.recover(label) == console_key().strip()


def test_a_row_that_was_never_in_a_kit_says_so(kit: KdbxStore) -> None:
    with pytest.raises(escrow.EscrowError, match='never held in a kit'):
        _ = escrow.from_kit(kit, escrow.PASSPHRASE)


def test_a_kit_carrying_no_such_row_sends_the_operator_to_the_console(kit: KdbxStore) -> None:
    # A kit is filled from the seed table, so one written since these rows left
    # it holds no entry to read -- which is an ordinary kit and not a broken
    # one. "No entry seeds/GitHub App (dispatch)" would read like damage and
    # invite a hunt for the row; what the operator needs is the other source.
    with pytest.raises(escrow.EscrowError, match='carries no such row') as refused:
        _ = escrow.from_kit(kit, escrow.DISPATCH_KEY)

    assert 'credentials derived github-dispatch-key record' in str(refused.value)
    assert '--from-kit' in str(refused.value)


def test_an_unregistered_label_is_refused(vault: escrow.Vault) -> None:
    with pytest.raises(escrow.EscrowError, match='no label'):
        _ = escrow.generate(vault, 'made/up')


def test_a_label_cannot_escape_the_registry(registry: escrow.Registry) -> None:
    with pytest.raises(escrow.EscrowError, match='is not a label'):
        _ = registry.path('../../etc/passwd', 1)


def test_rewrap_preserves_every_plaintext_under_a_new_recipient(vault: escrow.Vault) -> None:
    before = _filled(vault)
    successor = age.generate()

    _ = escrow.rewrap(vault.registry, identities=[successor.secret, vault.identity], recipients=[successor.public])

    opened = escrow.Vault(registry=vault.registry, identity=successor.secret)
    assert {label: opened.recover(label) for label in before} == before
    assert vault.registry.recipients() == [successor.public]


def test_rewrap_closes_the_door_on_the_retired_key(vault: escrow.Vault) -> None:
    _ = escrow.generate(vault, escrow.PASSPHRASE)
    successor = age.generate()

    _ = escrow.rewrap(vault.registry, identities=[successor.secret, vault.identity], recipients=[successor.public])

    # This is what makes the retired kit destroyable rather than kept.
    with pytest.raises(age.AgeError):
        _ = vault.recover(escrow.PASSPHRASE)


def test_rewrap_is_resumable(vault: escrow.Vault) -> None:
    # A run that died half way leaves some files under the successor and some
    # under the predecessor; re-running is given both and finishes the job.
    before = _filled(vault)
    successor = age.generate()
    half = vault.registry.path(escrow.CA, escrow.FIRST)
    _ = half.write_text(age.encrypt(before[escrow.CA], [successor.public]))

    _ = escrow.rewrap(vault.registry, identities=[successor.secret, vault.identity], recipients=[successor.public])

    opened = escrow.Vault(registry=vault.registry, identity=successor.secret)
    assert {label: opened.recover(label) for label in before} == before


def test_rewrap_refuses_to_lock_the_operator_out(vault: escrow.Vault) -> None:
    # Re-encrypting to a key nobody in the room holds is unrecoverable — a
    # mistyped recipient would take every credential with it — so it is
    # refused rather than warned about. Handing the escrow to a new custodian
    # is adding their recipient beside the existing one, not replacing it
    # with a key this run cannot open.
    _ = escrow.generate(vault, escrow.PASSPHRASE)

    with pytest.raises(escrow.EscrowError, match='would lock the escrow'):
        _ = escrow.rewrap(vault.registry, identities=[vault.identity], recipients=[age.generate().public])


def test_rotating_the_recovery_key_changes_no_plaintext(kit: KdbxStore, registry: escrow.Registry) -> None:
    # Kit rotation is pure re-encryption: nothing in production is touched and
    # no consumer is re-run.
    _ = escrow.init(kit, registry)
    before = _filled(escrow.Vault.open(kit, registry))
    successor = MemoryKit()

    escrow.rotate_recovery(kit, successor, registry)

    opened = escrow.Vault.open(successor, registry)
    assert {label: opened.recover(label) for label in before} == before
    assert registry.recipients() == [successor.get(escrow.RECOVERY_ENTRY, attribute='UserName')]


def test_rotating_the_recovery_key_again_reuses_the_successor_already_stored(
    kit: KdbxStore, registry: escrow.Registry
) -> None:
    # A rotation that died part way through its re-wrap: the successor kit
    # holds a key, some ciphertexts are under it, the rest are still under the
    # retired key and `RECIPIENTS` still names the retired key. The re-run must
    # finish with the key the successor holds -- a fresh one would strand every
    # ciphertext already under the first.
    _ = escrow.init(kit, registry)
    before = _filled(escrow.Vault.open(kit, registry))
    successor = MemoryKit()
    stored = age.generate()
    successor.put(escrow.RECOVERY_ENTRY, stored.public, stored.secret)
    half = registry.path(escrow.CA, escrow.FIRST)
    _ = half.write_text(age.encrypt(before[escrow.CA], [stored.public]))

    escrow.rotate_recovery(kit, successor, registry)

    assert successor.get(escrow.RECOVERY_ENTRY) == stored.secret
    assert registry.recipients() == [stored.public]
    opened = escrow.Vault.open(successor, registry)
    assert {label: opened.recover(label) for label in before} == before


def test_rotating_the_recovery_key_refuses_a_successor_holding_the_retired_key(
    kit: KdbxStore, registry: escrow.Registry
) -> None:
    # A copy of the kit in hand, or the kit itself, is not a successor: the
    # re-wrap would re-encrypt to the key already named and nothing rotates.
    _ = escrow.init(kit, registry)
    _ = escrow.generate(escrow.Vault.open(kit, registry), escrow.PASSPHRASE)
    successor = MemoryKit()
    successor.put(
        escrow.RECOVERY_ENTRY,
        kit.get(escrow.RECOVERY_ENTRY, attribute='UserName'),
        kit.get(escrow.RECOVERY_ENTRY),
    )

    with pytest.raises(escrow.EscrowError, match='the retired key itself'):
        escrow.rotate_recovery(kit, successor, registry)

    assert registry.recipients() == [kit.get(escrow.RECOVERY_ENTRY, attribute='UserName')]


def test_a_recovery_row_without_a_key_is_written_over(kit: KdbxStore, registry: escrow.Registry) -> None:
    # Present is not complete: a row the vault cannot open with is no key to
    # reuse, and reusing an empty one would re-wrap to nothing.
    _ = escrow.init(kit, registry)
    before = _filled(escrow.Vault.open(kit, registry))
    successor = MemoryKit()
    successor.put(escrow.RECOVERY_ENTRY, '', '')

    escrow.rotate_recovery(kit, successor, registry)

    opened = escrow.Vault.open(successor, registry)
    assert {label: opened.recover(label) for label in before} == before
    assert registry.recipients() == [successor.get(escrow.RECOVERY_ENTRY, attribute='UserName')]


# --------------------------------------------------------------------------
# What is escrowed opens with the kit in hand (credentials.md §2.2): every
# write derives the kit's recovery recipient and refuses a recipients file that
# does not name it. The states a rotation leaves a checkout in, one per case.
# --------------------------------------------------------------------------

ANCHOR = 'the recovery recipient of the kit in hand'


@pytest.fixture
def successor() -> age.Identity:
    """The recovery key a kit rotation hands over to."""
    return age.generate()


def _in_hand(vault: escrow.Vault, identity: str) -> escrow.Vault:
    """The same registry, opened with a different kit's recovery key."""
    return escrow.Vault(registry=vault.registry, identity=identity)


def _refused(vault: escrow.Vault) -> None:
    """A generation and an import are both refused, and nothing is filed."""
    with pytest.raises(escrow.EscrowError, match=ANCHOR):
        _ = escrow.generate(vault, escrow.PASSPHRASE)
    with pytest.raises(escrow.EscrowError, match=ANCHOR):
        _ = escrow.adopt(vault, escrow.ALERTMANAGER, 'a-token-from-somewhere-else')
    assert vault.registry.generations(escrow.PASSPHRASE) == [escrow.FIRST]
    assert vault.registry.generations(escrow.ALERTMANAGER) == []


def test_a_clone_that_predates_a_rotation_is_refused_by_the_successor(
    vault: escrow.Vault, successor: age.Identity
) -> None:
    # The checkout never took the rotation's commit: its recipients file and
    # every ciphertext beside it still name the retired key, and agree with
    # each other. Only the kit in hand says otherwise.
    _ = escrow.generate(vault, escrow.PASSPHRASE)

    _refused(_in_hand(vault, successor.secret))


def test_an_interrupted_rotation_is_refused_by_the_successor(vault: escrow.Vault, successor: age.Identity) -> None:
    # A rotation writes the recipients file last, so one that stopped part way
    # leaves some ciphertexts under the successor and a file naming the retired
    # key. Writing beside them is not the way out: finishing the rotation is.
    _ = escrow.generate(vault, escrow.PASSPHRASE)
    half = vault.registry.path(escrow.PASSPHRASE, escrow.FIRST)
    _ = half.write_text(age.encrypt(vault.recover(escrow.PASSPHRASE), [successor.public]))

    _refused(_in_hand(vault, successor.secret))


def test_the_retired_kit_writes_into_an_interrupted_rotation_that_the_resumed_one_finishes(
    kit: KdbxStore, vault: escrow.Vault, successor: age.Identity
) -> None:
    # The file still names the retired key and the retired kit is in hand, so
    # the write is one that kit opens -- and the resumed rotation opens every
    # file with either key, so the new generation is re-wrapped with the rest.
    before = escrow.generate(vault, escrow.PASSPHRASE)
    half = vault.registry.path(escrow.PASSPHRASE, escrow.FIRST)
    _ = half.write_text(age.encrypt(before, [successor.public]))

    written = escrow.generate(vault, escrow.CA)

    resumed = MemoryKit()
    resumed.put(escrow.RECOVERY_ENTRY, successor.public, successor.secret)
    escrow.rotate_recovery(kit, resumed, vault.registry)
    opened = escrow.Vault.open(resumed, vault.registry)
    assert opened.recover(escrow.PASSPHRASE) == before
    assert opened.recover(escrow.CA) == written


def test_the_retired_kit_is_refused_by_a_registry_rotated_away_from_it(kit: KdbxStore, vault: escrow.Vault) -> None:
    # The rotation is merged and the old envelope is the one that came out of
    # the drawer: whatever it wrote would open only with the key the registry
    # has just stopped naming.
    _ = escrow.generate(vault, escrow.PASSPHRASE)
    escrow.rotate_recovery(kit, MemoryKit(), vault.registry)

    _refused(vault)


def test_a_custodian_beside_the_kit_is_written_to_and_named(
    vault: escrow.Vault, caplog: pytest.LogCaptureFixture
) -> None:
    # A second custodian's recipient is a line the file may legitimately
    # carry, so the rule passes it -- and nothing here can tell a custodian
    # from a line nobody reviewed, so every write says who else can open it.
    custodian = age.generate()
    vault.registry.set_recipients([*vault.registry.recipients(), custodian.public])

    minted = escrow.generate(vault, escrow.PASSPHRASE)
    _ = escrow.adopt(vault, escrow.ALERTMANAGER, 'a-token-from-somewhere-else')

    named = [record.getMessage() for record in caplog.records if record.levelname == 'WARNING']
    assert len([message for message in named if custodian.public in message]) == 2
    assert escrow.Vault(registry=vault.registry, identity=custodian.secret).recover(escrow.PASSPHRASE) == minted


def test_recording_in_a_stale_clone_is_refused_in_the_rules_words(vault: escrow.Vault, successor: age.Identity) -> None:
    # `record` opens what is filed to compare before it writes; against a
    # registry the kit in hand cannot open, that walk would fail as a
    # generation no identity matched, which names neither cause nor way out.
    _ = escrow.record(vault, escrow.DISPATCH_KEY, console_key())

    with pytest.raises(escrow.EscrowError, match=ANCHOR):
        _ = escrow.record(_in_hand(vault, successor.secret), escrow.DISPATCH_KEY, pki.generate_ca_key())

    assert vault.registry.generations(escrow.DISPATCH_KEY) == [escrow.FIRST]


def test_a_rewrap_names_every_recipient_beyond_the_kit(vault: escrow.Vault, caplog: pytest.LogCaptureFixture) -> None:
    # `kit rewrap` is the command for a hand-edited recipients file, and it
    # re-encrypts every generation to whatever that file names: the write
    # where a line nobody reviewed gains the most, so it says who else can
    # open what it wrote.
    _ = escrow.generate(vault, escrow.PASSPHRASE)
    custodian = age.generate()
    vault.registry.set_recipients([*vault.registry.recipients(), custodian.public])
    caplog.clear()

    _ = escrow.rewrap(vault.registry, identities=[vault.identity])

    warned = [record.getMessage() for record in caplog.records if record.levelname == 'WARNING']
    assert len(warned) == 1
    assert custodian.public in warned[0]


def test_a_missing_age_is_not_a_wrong_recipient_line(vault: escrow.Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Reported as a line the tool refused, it would send the operator to a
    # file that is fine; the tool's absence is raised as itself instead.
    monkeypatch.setattr(age, 'BINARY', 'age-that-is-not-installed')

    with pytest.raises(age.AgeMissing):
        _ = vault.registry.recipients()
    with pytest.raises(age.AgeMissing):
        _ = escrow.check(vault.registry)


def test_a_record_that_files_nothing_names_nobody(vault: escrow.Vault, caplog: pytest.LogCaptureFixture) -> None:
    # The warning says who else can open what a write wrote. A re-run that
    # finds the value already filed writes nothing, so it has nobody to name
    # -- and a warning there would teach the operator to read past it.
    custodian = age.generate()
    vault.registry.set_recipients([*vault.registry.recipients(), custodian.public])
    _ = escrow.record(vault, escrow.DISPATCH_KEY, console_key())
    assert any(custodian.public in record.getMessage() for record in caplog.records)
    caplog.clear()

    _ = escrow.record(vault, escrow.DISPATCH_KEY, console_key())

    assert vault.registry.generations(escrow.DISPATCH_KEY) == [escrow.FIRST]
    assert not [record for record in caplog.records if record.levelname == 'WARNING']


def test_a_refused_write_draws_nothing(vault: escrow.Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Refused before the mint rather than after it: a value drawn and then
    # dropped is harmless, but only for as long as nothing between the two
    # steps hands it on.
    drawn: list[str] = []

    def mint() -> str:
        drawn.append('drawn')
        return 'a-token'

    monkeypatch.setattr(escrow, '_token', mint)
    vault.registry.set_recipients([age.generate().public])

    with pytest.raises(escrow.EscrowError, match=ANCHOR):
        _ = escrow.generate(vault, escrow.PASSPHRASE)

    assert drawn == []


def test_a_kit_without_a_recovery_key_is_sent_to_bootstrap(registry: escrow.Registry) -> None:
    # The anchor is read out of the kit, so a kit that has none has nothing to
    # anchor a write to; the row that holds one is created by the bootstrap.
    with pytest.raises(escrow.EscrowError, match='kit bootstrap --only recovery'):
        _ = escrow.Vault.open(MemoryKit(), registry)


def test_check_is_happy_with_a_full_registry(vault: escrow.Vault) -> None:
    _ = _filled(vault)

    assert escrow.check(vault.registry) == []


def test_check_names_a_label_with_nothing_escrowed(vault: escrow.Vault) -> None:
    _ = _filled(vault)
    for path in vault.registry.directory(escrow.CA).iterdir():
        path.unlink()

    problems = escrow.check(vault.registry)

    assert any(escrow.CA in problem and 'nothing escrowed' in problem for problem in problems)


def test_check_catches_a_ciphertext_that_is_not_one(vault: escrow.Vault) -> None:
    _ = _filled(vault)
    _ = vault.registry.path(escrow.PASSPHRASE, escrow.FIRST).write_text('not an age file at all\n')

    problems = escrow.check(vault.registry)

    assert any('not an armoured age file' in problem for problem in problems)


def test_check_catches_a_hole_in_the_generations(vault: escrow.Vault) -> None:
    _ = _filled(vault)
    _ = escrow.generate(vault, escrow.PASSPHRASE)
    _ = escrow.generate(vault, escrow.PASSPHRASE)
    vault.registry.path(escrow.PASSPHRASE, 2).unlink()

    problems = escrow.check(vault.registry)

    assert any('without a gap' in problem for problem in problems)


def test_check_catches_a_stray_file(vault: escrow.Vault) -> None:
    _ = _filled(vault)
    _ = vault.registry.directory(escrow.PASSPHRASE).joinpath('1.age.bak').write_text('oops')

    problems = escrow.check(vault.registry)

    assert any('neither a ciphertext' in problem for problem in problems)


def test_check_names_a_label_the_register_does_not(vault: escrow.Vault) -> None:
    # A ciphertext nothing in the register reads is either a label renamed out
    # from under its consumer or a secret nobody will ever rotate; both are
    # the operator's to decide, so `check` says so rather than passing.
    _ = _filled(vault)
    unregistered = 'nobody/reads-this'
    assert unregistered not in escrow.register()
    stray = vault.registry.path(unregistered, escrow.FIRST)
    stray.parent.mkdir(parents=True)
    _ = stray.write_text(age.encrypt('a secret', vault.registry.recipients()))

    (problem,) = escrow.check(vault.registry)

    assert problem.startswith(f'{unregistered}:')


def test_check_names_a_recipient_that_is_not_one(vault: escrow.Vault) -> None:
    # Every later write encrypts to the file's lines, so a line that is not an
    # age recipient is a write that fails on the day a secret needs escrowing.
    _ = _filled(vault)
    # The likeliest slip: the private half pasted where the public one goes.
    malformed = age.generate().secret
    vault.registry.set_recipients([*vault.registry.recipients(), malformed])

    (problem,) = escrow.check(vault.registry)

    # Named by its line, and never repeated: the report of a pasted private
    # key is read on a screen and pasted into issues.
    assert 'line 2' in problem
    assert malformed not in problem


def test_check_names_a_recipient_whose_checksum_is_wrong(vault: escrow.Vault) -> None:
    # A mistyped or truncated hand edit keeps the `age1` prefix; the checksum
    # is what says the line names nobody, and only the tool's own parse reads
    # it. Every later write to that line would fail.
    _ = _filled(vault)
    (recipient,) = vault.registry.recipients()
    vault.registry.set_recipients([_mistyped(recipient)])

    (problem,) = escrow.check(vault.registry)

    assert 'line 1' in problem


def _mistyped(recipient: str) -> str:
    """`recipient` with its last six characters replaced, which keeps its prefix and breaks its checksum."""
    tail = 'qqqqqq' if not recipient.endswith('qqqqqq') else 'pppppp'
    return f'{recipient[:-6]}{tail}'


@pytest.mark.parametrize('kind', ['mistyped', 'private'])
def test_a_writer_refuses_a_recipient_line_that_is_not_one_by_its_number(vault: escrow.Vault, kind: str) -> None:
    # Beside the kit's own recipient, so the refusal is the line's and not the
    # anchor's: every writer reads the file through the one parse `check` does,
    # and a line age would refuse is refused before anything is drawn -- named
    # by its number, with a pasted private key never repeated.
    (recipient,) = vault.registry.recipients()
    wrong = _mistyped(recipient) if kind == 'mistyped' else age.generate().secret
    vault.registry.set_recipients([recipient, wrong])

    with pytest.raises(escrow.EscrowError, match='line 2') as refused:
        _ = escrow.generate(vault, escrow.PASSPHRASE)

    assert wrong not in str(refused.value)
    assert vault.registry.generations(escrow.PASSPHRASE) == []


def test_check_wants_no_kit(registry: escrow.Registry) -> None:
    # The whole point: a clone with no offline database still says what is
    # wrong. An empty registry has no recipients file either.
    problems = escrow.check(registry)

    assert any(escrow.RECIPIENTS_FILE in problem for problem in problems)
    assert len(problems) >= len(escrow.register())


def test_missing_lists_what_a_bring_up_still_owes(vault: escrow.Vault) -> None:
    assert escrow.missing(vault.registry) == list(escrow.register())

    _ = escrow.generate(vault, escrow.PASSPHRASE)

    assert escrow.PASSPHRASE not in escrow.missing(vault.registry)


def test_a_retired_backup_generation_is_not_a_complaint(vault: escrow.Vault) -> None:
    # A generation that has fallen out of the window keeps its ciphertext
    # until the last dump under it ages out (state-backend.md §5), so `check`
    # must not call it an unregistered label the day the pin moves past it.
    _ = _filled(vault)
    retired = vault.registry.path(f'{escrow.BACKUP}/99', escrow.FIRST)
    retired.parent.mkdir(parents=True)
    _ = retired.write_text(age.encrypt(age.generate().secret, vault.registry.recipients()))

    assert escrow.check(vault.registry) == []


def test_the_first_generation_has_no_predecessor(monkeypatch: pytest.MonkeyPatch) -> None:
    # The `[N, N-1]` window only starts at the second generation. Naming
    # `backup/age/0` would have a bring-up mint a key for a generation that
    # never existed, encrypt every dump to it, and leave `check` demanding
    # its ciphertext forever.
    from kluster.scripts.state_backend import settings

    monkeypatch.setattr(settings, 'AGE_GENERATION', escrow.FIRST)

    assert escrow.backup_labels() == (f'{escrow.BACKUP}/1',)
    assert f'{escrow.BACKUP}/0' not in escrow.register()


def test_a_rotated_pin_names_the_generation_before_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # From the second generation on, both are recipients: any object in
    # retention opens with the current key or the previous one.
    from kluster.scripts.state_backend import settings

    monkeypatch.setattr(settings, 'AGE_GENERATION', 3)

    assert escrow.backup_labels() == (f'{escrow.BACKUP}/3', f'{escrow.BACKUP}/2')
    assert set(escrow.backup_labels()) <= set(escrow.register())


def test_the_backup_labels_follow_the_appliance_pin() -> None:
    # The Butane file names exactly these recipients, so the register and the
    # box cannot disagree about which generations exist.
    from kluster.scripts.state_backend import settings

    assert escrow.backup_labels()[0] == f'{escrow.BACKUP}/{settings.AGE_GENERATION}'
    for label in escrow.backup_labels():
        assert label in escrow.register()
