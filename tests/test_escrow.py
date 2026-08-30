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


def _fill(registry: escrow.Registry, label: str) -> str:
    """One label at its next generation, however that label's value comes about."""
    origin = escrow.register()[label].origin
    if isinstance(origin, escrow.Generated):
        return escrow.generate(registry, label)
    # A console row has nothing to draw: its value arrives from outside, which
    # is what `adopt` is for.
    value = console_key()
    _ = escrow.adopt(registry, label, value)
    return value


def _filled(registry: escrow.Registry) -> dict[str, str]:
    """Every register label at generation one, and the plaintexts filed."""
    return {label: _fill(registry, label) for label in escrow.register()}


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


def test_a_generated_secret_comes_back_out(vault: escrow.Vault) -> None:
    minted = escrow.generate(vault.registry, escrow.PASSPHRASE)

    assert vault.recover(escrow.PASSPHRASE) == minted


def test_a_secret_that_cannot_be_escrowed_is_never_minted(vault: escrow.Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # The safety property, from its failing side: encryption comes before the
    # value is handed over, so a caller can never hold a generated secret the
    # registry does not carry.
    def refuse(_plaintext: str, _recipients: Sequence[str]) -> str:
        raise age.AgeError('no recipient reachable')

    monkeypatch.setattr(age, 'encrypt', refuse)

    with pytest.raises(age.AgeError):
        _ = escrow.generate(vault.registry, escrow.PASSPHRASE)

    assert vault.registry.generations(escrow.PASSPHRASE) == []


def test_generating_again_is_a_new_generation_beside_the_old_one(vault: escrow.Vault) -> None:
    first = escrow.generate(vault.registry, escrow.PASSPHRASE)

    second = escrow.generate(vault.registry, escrow.PASSPHRASE)

    assert first != second
    assert vault.registry.generations(escrow.PASSPHRASE) == [1, 2]
    # The predecessor is still openable: what production holds until the new
    # generation is adopted is generation one.
    assert vault.recover(escrow.PASSPHRASE, 1) == first
    assert vault.recover(escrow.PASSPHRASE) == second


def test_one_labels_rotation_leaves_every_other_label_alone(vault: escrow.Vault) -> None:
    # The whole point of the model: rotating the passphrase is not also
    # rotating the CA.
    before = _filled(vault.registry)

    _ = escrow.generate(vault.registry, escrow.PASSPHRASE)

    for label, secret in before.items():
        if label != escrow.PASSPHRASE:
            assert vault.recover(label) == secret


def test_the_escrowed_ca_key_is_a_working_ca(vault: escrow.Vault) -> None:
    _ = escrow.generate(vault.registry, escrow.CA)

    authority = pki.Authority.from_pem(vault.recover(escrow.CA))

    # It signs, and what it signs chains to it.
    leaf = x509.load_pem_x509_certificate(authority.issue_client('ci').cert_pem)
    leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(authority.certificate().cert_pem))


def test_an_escrowed_backup_identity_opens_what_it_encrypts(vault: escrow.Vault, tmp_path: Path) -> None:
    label = escrow.backup_labels()[0]
    _ = escrow.generate(vault.registry, label)
    identity = vault.recover(label)
    path = tmp_path / 'dump.age'
    _ = path.write_text(age.encrypt('a pg_dump', [age.recipient(identity)]))

    assert age.decrypt(path, [identity]) == 'a pg_dump'


def test_import_escrows_a_value_that_already_exists(vault: escrow.Vault) -> None:
    # The migration path: a live credential carries over unrotated.
    _ = escrow.adopt(vault.registry, escrow.PASSPHRASE, 'the-live-passphrase')

    assert vault.registry.generations(escrow.PASSPHRASE) == [1]
    assert vault.recover(escrow.PASSPHRASE) == 'the-live-passphrase'


def test_import_appends_rather_than_overwriting(vault: escrow.Vault) -> None:
    # Importing onto a label that already holds something must not replace
    # what production is using; it becomes the next generation like any other.
    minted = escrow.generate(vault.registry, escrow.PASSPHRASE)

    _ = escrow.adopt(vault.registry, escrow.PASSPHRASE, 'from-elsewhere')

    assert vault.registry.generations(escrow.PASSPHRASE) == [1, 2]
    assert vault.recover(escrow.PASSPHRASE, 1) == minted
    assert vault.recover(escrow.PASSPHRASE) == 'from-elsewhere'


def test_import_after_import_appends_and_leaves_the_first_file_alone(vault: escrow.Vault) -> None:
    # Two imports in a row is the shape a retried ceremony step has, and the
    # retry must not land on top of the generation the first one wrote: the
    # file is compared byte for byte, because an overwrite that happened to
    # re-encrypt the same plaintext would still have destroyed the original.
    _ = escrow.adopt(vault.registry, escrow.PASSPHRASE, 'the-live-passphrase')
    first = vault.registry.path(escrow.PASSPHRASE, 1)
    written = first.read_bytes()

    second = escrow.adopt(vault.registry, escrow.PASSPHRASE, 'a-second-value')

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
        _ = escrow.adopt(vault.registry, label, value)

    assert vault.registry.generations(label) == []


def test_import_refuses_something_that_is_not_an_age_identity(vault: escrow.Vault) -> None:
    label = escrow.backup_labels()[0]

    # A wrong-but-non-empty pipe: the recipient rather than the identity is
    # the mistake the labels invite, and it survives every check but the shape.
    with pytest.raises(escrow.EscrowError, match='age identity'):
        _ = escrow.adopt(vault.registry, label, age.generate().public)

    assert vault.registry.generations(label) == []


def test_import_takes_a_real_age_identity(vault: escrow.Vault) -> None:
    label = escrow.backup_labels()[0]
    identity = age.generate()

    _ = escrow.adopt(vault.registry, label, identity.secret)

    assert vault.recover(label) == identity.secret


def test_import_refuses_something_that_is_not_a_private_key(vault: escrow.Vault) -> None:
    with pytest.raises(escrow.EscrowError, match='PEM private key'):
        _ = escrow.adopt(vault.registry, escrow.CA, 'BEGIN PRIVATE KEY')

    assert vault.registry.generations(escrow.CA) == []


def test_import_refuses_a_private_key_that_stops_half_way(vault: escrow.Vault) -> None:
    truncated = pki.generate_ca_key()[:80]

    with pytest.raises(escrow.EscrowError, match='PEM private key'):
        _ = escrow.adopt(vault.registry, escrow.CA, truncated)

    assert vault.registry.generations(escrow.CA) == []


def test_import_takes_a_real_ca_key(vault: escrow.Vault) -> None:
    key = pki.generate_ca_key()

    _ = escrow.adopt(vault.registry, escrow.CA, key)

    assert vault.recover(escrow.CA) == key


def test_a_token_label_asks_only_that_there_be_a_value(vault: escrow.Vault) -> None:
    # Nothing recognisable about a passphrase or a bearer token, so the shape
    # is the empty check and no more: a check that guessed at length or
    # alphabet would refuse values the consumers accept.
    _ = escrow.adopt(vault.registry, escrow.ALERTMANAGER, 'a-token-from-somewhere-else')

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
        _ = escrow.generate(vault.registry, label)

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


def test_an_unregistered_label_is_refused(vault: escrow.Vault) -> None:
    with pytest.raises(escrow.EscrowError, match='no label'):
        _ = escrow.generate(vault.registry, 'made/up')


def test_a_label_cannot_escape_the_registry(registry: escrow.Registry) -> None:
    with pytest.raises(escrow.EscrowError, match='is not a label'):
        _ = registry.path('../../etc/passwd', 1)


def test_rewrap_preserves_every_plaintext_under_a_new_recipient(vault: escrow.Vault) -> None:
    before = _filled(vault.registry)
    successor = age.generate()

    _ = escrow.rewrap(vault.registry, identities=[successor.secret, vault.identity], recipients=[successor.public])

    opened = escrow.Vault(registry=vault.registry, identity=successor.secret)
    assert {label: opened.recover(label) for label in before} == before
    assert vault.registry.recipients() == [successor.public]


def test_rewrap_closes_the_door_on_the_retired_key(vault: escrow.Vault) -> None:
    _ = escrow.generate(vault.registry, escrow.PASSPHRASE)
    successor = age.generate()

    _ = escrow.rewrap(vault.registry, identities=[successor.secret, vault.identity], recipients=[successor.public])

    # This is what makes the retired kit destroyable rather than kept.
    with pytest.raises(age.AgeError):
        _ = vault.recover(escrow.PASSPHRASE)


def test_rewrap_is_resumable(vault: escrow.Vault) -> None:
    # A run that died half way leaves some files under the successor and some
    # under the predecessor; re-running is given both and finishes the job.
    before = _filled(vault.registry)
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
    _ = escrow.generate(vault.registry, escrow.PASSPHRASE)

    with pytest.raises(escrow.EscrowError, match='would lock the escrow'):
        _ = escrow.rewrap(vault.registry, identities=[vault.identity], recipients=[age.generate().public])


def test_rotating_the_recovery_key_changes_no_plaintext(kit: KdbxStore, registry: escrow.Registry) -> None:
    # Kit rotation is pure re-encryption: nothing in production is touched and
    # no consumer is re-run.
    _ = escrow.init(kit, registry)
    before = _filled(registry)
    successor = MemoryKit()

    escrow.rotate_recovery(kit, successor, registry)

    opened = escrow.Vault.open(successor, registry)
    assert {label: opened.recover(label) for label in before} == before
    assert registry.recipients() == [successor.get(escrow.RECOVERY_ENTRY, attribute='UserName')]


def test_check_is_happy_with_a_full_registry(vault: escrow.Vault) -> None:
    _ = _filled(vault.registry)

    assert escrow.check(vault.registry) == []


def test_check_names_a_label_with_nothing_escrowed(vault: escrow.Vault) -> None:
    _ = _filled(vault.registry)
    for path in vault.registry.directory(escrow.CA).iterdir():
        path.unlink()

    problems = escrow.check(vault.registry)

    assert any(escrow.CA in problem and 'nothing escrowed' in problem for problem in problems)


def test_check_catches_a_ciphertext_that_is_not_one(vault: escrow.Vault) -> None:
    _ = _filled(vault.registry)
    _ = vault.registry.path(escrow.PASSPHRASE, escrow.FIRST).write_text('not an age file at all\n')

    problems = escrow.check(vault.registry)

    assert any('not an armoured age file' in problem for problem in problems)


def test_check_catches_a_hole_in_the_generations(vault: escrow.Vault) -> None:
    _ = _filled(vault.registry)
    _ = escrow.generate(vault.registry, escrow.PASSPHRASE)
    _ = escrow.generate(vault.registry, escrow.PASSPHRASE)
    vault.registry.path(escrow.PASSPHRASE, 2).unlink()

    problems = escrow.check(vault.registry)

    assert any('without a gap' in problem for problem in problems)


def test_check_catches_a_stray_file(vault: escrow.Vault) -> None:
    _ = _filled(vault.registry)
    _ = vault.registry.directory(escrow.PASSPHRASE).joinpath('1.age.bak').write_text('oops')

    problems = escrow.check(vault.registry)

    assert any('neither a ciphertext' in problem for problem in problems)


def test_check_wants_no_kit(registry: escrow.Registry) -> None:
    # The whole point: a clone with no offline database still says what is
    # wrong. An empty registry has no recipients file either.
    problems = escrow.check(registry)

    assert any(escrow.RECIPIENTS_FILE in problem for problem in problems)
    assert len(problems) >= len(escrow.register())


def test_missing_lists_what_a_bring_up_still_owes(vault: escrow.Vault) -> None:
    assert escrow.missing(vault.registry) == list(escrow.register())

    _ = escrow.generate(vault.registry, escrow.PASSPHRASE)

    assert escrow.PASSPHRASE not in escrow.missing(vault.registry)


def test_a_retired_backup_generation_is_not_a_complaint(vault: escrow.Vault) -> None:
    # A generation that has fallen out of the window keeps its ciphertext
    # until the last dump under it ages out (state-backend.md §5), so `check`
    # must not call it an unregistered label the day the pin moves past it.
    _ = _filled(vault.registry)
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
