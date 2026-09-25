"""The appliance's bill of materials, which decides whether a box gets rebuilt.

Leaf keys are random at issuance, so the naive digest — hash the rendered
certificate — makes every converge see drift and replace a box that is
perfectly fine. What the digest has to capture instead is what the box *is*:
the CA it chains to, and the address it answers on.
"""

from __future__ import annotations

import base64
import gzip
import json
import shlex
import shutil
import urllib.parse
from dataclasses import fields
from pathlib import Path
from typing import Any

import drill_recipient_redirect
import pytest
from memory_kit import MemoryKit

from kluster.scripts.credentials import age, escrow, pki
from kluster.scripts.state_backend import config, settings

needs_age = pytest.mark.skipif(shutil.which(age.BINARY) is None, reason='age is not on PATH (mise x -- ...)')
needs_butane = pytest.mark.skipif(shutil.which('butane') is None, reason='butane is not on PATH (mise x -- ...)')

ADDRESS = '192.0.2.10'
OTHER = '192.0.2.11'
RECIPIENT = 'age1exampleexampleexampleexampleexampleexampleexampleexamplezzzz'
DRILL_RECIPIENT = 'age1drilldrilldrilldrilldrilldrilldrilldrilldrilldrilldrillzzzz'

#: Where the Butane template puts the recipient list on the box.
RECIPIENTS_FILE = '/etc/kluster/age-recipients.txt'


@pytest.fixture(autouse=True)
def drill_recipient_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The drill recipient file, absent unless a case writes it.

    Pointed away from the checkout's own, so that whether the operator has
    committed one there decides nothing here: the cases below are about what
    the function does with the file, not about the repository's state.
    """
    return drill_recipient_redirect.redirect(monkeypatch, tmp_path)


@pytest.fixture
def roots() -> config.Roots:
    return config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))


def _digests(roots: config.Roots, address: str = ADDRESS) -> dict[str, str]:
    return config.digests(roots, address=address, dump_key_id='key-id', bucket_id='bucket-id')


def test_a_re_render_is_not_drift(roots: config.Roots) -> None:
    # Otherwise every converge terminates a healthy box and rebuilds it.
    assert config.drift(_digests(roots), _digests(roots)) == []


def test_the_address_the_server_answers_on_is_drift(roots: config.Roots) -> None:
    # The reserved IP is in the server certificate's SAN, and a client
    # connects to it with verify-full; a box holding the wrong one is unusable.
    assert config.drift(_digests(roots), _digests(roots, OTHER)) == ['server_cert']


def test_a_different_ca_is_drift(roots: config.Roots) -> None:
    # The CA's key comes from escrow and outlives every render, so it is the
    # one certificate compared by public key.
    other = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=roots.age_recipients)

    assert 'ca_cert' in config.drift(_digests(roots), _digests(other))


def test_a_different_backup_recipient_is_drift(roots: config.Roots) -> None:
    # Rotating the backup generation has to reach the box, which holds the
    # public halves in its Ignition.
    other = config.Roots(ca=roots.ca, age_recipients=(RECIPIENT, 'age1second'))

    assert config.drift(_digests(roots), _digests(other)) == ['age_recipients']


def test_the_server_key_is_outside_the_bill_of_materials(roots: config.Roots) -> None:
    # Not an oversight: it is random at issuance, so digesting it would be
    # digesting this render rather than this machine. Rotating it is
    # `provision --replace`.
    assert 'server_key' not in _digests(roots)


def test_the_dump_key_secret_is_outside_the_bill_of_materials(roots: config.Roots) -> None:
    # The digest map travels in the instance's metadata. What the converge
    # compares is the key's identity, which is `b2_dump_key_id`.
    assert 'b2_dump_key' not in _digests(roots)


def test_every_field_but_the_secrets_is_compared(roots: config.Roots) -> None:
    # Each field declares its own digest treatment, so a field added to the
    # machine is compared unless it says otherwise, and neither a rename nor a
    # new field can quietly drop a component out of the comparison.
    compared = set(_digests(roots)) - {'butane'}

    secrets = {'server_key', 'b2_dump_key', 'ssh_host_key'}
    assert compared == {spec.name for spec in fields(config.Machine)} - secrets


def test_the_ssh_host_key_is_outside_the_bill_of_materials(roots: config.Roots) -> None:
    # Minted fresh by every render, like the server key: digesting it would
    # make every converge see drift. What a box is held to is its *public*
    # half, which the launch records beside the digest map rather than in it.
    assert 'ssh_host_key' not in _digests(roots)


def test_the_certificate_the_box_gets_matches_the_key_it_gets(roots: config.Roots) -> None:
    # One issuance, both halves. Two calls would give the box a certificate
    # its private key does not answer for, and 5432 would never come up.
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    values = config.machine(roots, address=ADDRESS, dump_key_id='k', dump_key='s', bucket_id='b')
    cert = x509.load_pem_x509_certificate(values.server_cert.encode())
    key = serialization.load_pem_private_key(values.server_key.encode(), password=None)

    assert key.public_key().public_bytes(  # pyright: ignore[reportAttributeAccessIssue]
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@pytest.fixture
def vault(tmp_path: Path) -> escrow.Vault:
    kit = MemoryKit()
    registry = escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    return escrow.Vault.open(kit, registry)


@needs_age
def test_a_bring_up_escrows_the_roots_it_is_about_to_install(vault: escrow.Vault) -> None:
    # The appliance is the first thing to escrow: a bring-up has a kit and an
    # empty registry, and provisioning mints what it needs on the way.
    roots = config.Roots.ensure(vault, appliance_exists=False)

    for label in config.Roots.labels():
        assert vault.registry.generations(label) == [1]
    assert roots.age_recipients == tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())


@needs_age
def test_a_second_run_reuses_what_is_already_escrowed(vault: escrow.Vault) -> None:
    # Generating over a live CA would invalidate every certificate under it,
    # and over a live backup identity would orphan every dump.
    first = config.Roots.ensure(vault, appliance_exists=False)

    second = config.Roots.ensure(vault, appliance_exists=False)

    assert second.ca.key_pem == first.ca.key_pem
    assert second.age_recipients == first.age_recipients
    for label in config.Roots.labels():
        assert vault.registry.generations(label) == [1]


@needs_age
def test_writing_a_bundle_does_not_mint_a_ca(vault: escrow.Vault) -> None:
    # `recover` is the read-only door: a machine asking for a client bundle
    # against an empty registry must be told to run the bring-up, not handed a
    # brand-new CA the appliance has never heard of.
    with pytest.raises(escrow.EscrowError, match=escrow.CA):
        _ = config.Roots.recover(vault)


@needs_age
def test_the_drill_recipient_on_file_follows_the_escrowed_generations(
    vault: escrow.Vault, drill_recipient_file: Path
) -> None:
    """The box and an operator's dump encrypt to the drill key once its public half is committed.

    After the generations, because the generations are the recovery path and
    the drill key is the convenience: a reader of the box's list sees what
    the escrow holds first. Comments in the file are the generator's, and
    are not recipients.
    """
    _ = config.Roots.ensure(vault, appliance_exists=False)
    _ = drill_recipient_file.write_text(f'# the drill key, public half\n{DRILL_RECIPIENT}\n')

    recipients = config.age_recipients(vault)

    generations = tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())
    assert recipients == (*generations, DRILL_RECIPIENT)


@needs_age
def test_no_drill_recipient_on_file_is_the_generations_alone(vault: escrow.Vault, drill_recipient_file: Path) -> None:
    # Absent is a state and not a refusal: every converge before the
    # generator has run would otherwise refuse.
    _ = config.Roots.ensure(vault, appliance_exists=False)
    assert not drill_recipient_file.exists()

    recipients = config.age_recipients(vault)

    assert recipients == tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())


def test_an_empty_drill_recipient_file_is_refused_like_the_operator_keys(drill_recipient_file: Path) -> None:
    # A blank where a recipient should be is a dump the drill cannot open,
    # discovered a quarter later.
    _ = drill_recipient_file.write_text('# nothing here yet\n')

    with pytest.raises(age.AgeError, match='holds no values'):
        _ = config.drill_recipient(drill_recipient_file)


def test_a_second_drill_recipient_on_file_is_refused_by_naming_the_rotation(drill_recipient_file: Path) -> None:
    # One slot and no generational pair: a second line is a rotation done by
    # hand, and the command that does it properly is named instead.
    _ = drill_recipient_file.write_text(f'{DRILL_RECIPIENT}\nage1another\n')

    with pytest.raises(age.AgeError, match='--rotate'):
        _ = config.drill_recipient(drill_recipient_file)


def test_a_drill_recipient_that_is_not_one_is_refused(drill_recipient_file: Path) -> None:
    # A secret pasted where the public half belongs would be committed in the
    # clear and encrypt to nobody.
    _ = drill_recipient_file.write_text('AGE-SECRET-KEY-1NOTARECIPIENT\n')

    with pytest.raises(age.AgeError, match='not an age recipient'):
        _ = config.drill_recipient(drill_recipient_file)


def _delivered(ignition: str, path: str) -> str:
    """One file the rendered Ignition writes, as its text.

    Butane encodes an inline file as a `data:` URL and gzips it once it is
    worth gzipping, so a case reading the JSON straight would be asserting
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
        return raw.decode()
    raise AssertionError(f'the rendered Ignition writes no {path}')


@needs_butane
def test_the_ignition_carries_every_recipient_the_roots_name_one_per_line() -> None:
    """The box's recipient list is the roots' list, the drill key's line included.

    The dump script reads that file a line per recipient, so a third
    recipient reaches it through the same loop as the first two -- which is
    what makes the drill key's adoption a converge rather than a template
    change.
    """
    roots = config.Roots(
        ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT, 'age1second', DRILL_RECIPIENT)
    )
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')

    ignition = config.render_ignition(built)

    assert _delivered(ignition, RECIPIENTS_FILE).split() == [RECIPIENT, 'age1second', DRILL_RECIPIENT]


#: Where the Butane template puts the files that decide who may connect as whom.
HBA_FILE = '/etc/kluster/pki/pg_hba.conf'
INIT_FILE = '/etc/kluster/initdb/00-roles.sql'
DUMP_ENV_FILE = '/etc/kluster/state-dump.env'
POSTGRES_UNIT = 'pgstate.service'
CLIENT_ROLES = {settings.CI_ROLE, settings.OPERATOR_ROLE}


def _postgres_env(ignition: str) -> dict[str, str]:
    """The `--env` pairs the Postgres unit hands the image."""
    document: Any = json.loads(ignition)
    unit = next(entry for entry in document['systemd']['units'] if entry['name'] == POSTGRES_UNIT)
    argv = shlex.split(str(unit['contents']).replace('\\\n', ' '))
    return dict(argv[at + 1].split('=', 1) for at, arg in enumerate(argv) if arg == '--env')


def _statements(sql: str) -> list[str]:
    """The SQL statements in a script, comments dropped and whitespace folded."""
    code = ' '.join(line.split('--', 1)[0] for line in sql.splitlines())
    return [' '.join(statement.split()) for statement in code.split(';') if statement.strip()]


@pytest.fixture
def ignition(roots: config.Roots) -> str:
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')
    return config.render_ignition(built)


@needs_butane
def test_no_role_a_certificate_names_is_the_superuser(ignition: str) -> None:
    """The image's bootstrap superuser is a role no client certificate becomes.

    The image makes whatever `POSTGRES_USER` names its superuser, and a
    client role there would put every CI job's certificate one `COPY ... TO
    PROGRAM` away from a shell beside the server's key. pg_hba is the other
    half: the superuser is admitted on the local socket alone, and over TCP
    only the two client roles are, into the state's database alone -- so a
    certificate the CA signs for any other name is worth nothing here.
    """
    superuser = _postgres_env(ignition)['POSTGRES_USER']
    assert superuser not in CLIENT_ROLES

    rules = [line.split() for line in _delivered(ignition, HBA_FILE).splitlines() if line.strip()]
    assert [rule[:3] for rule in rules if rule[0] == 'local'] == [['local', 'all', superuser]]
    remote = [rule for rule in rules if rule[0] != 'local']
    assert remote
    for rule in remote:
        assert rule[0] == 'hostssl', rule
        assert rule[1] == settings.DATABASE, rule
        assert set(rule[2].split(',')) == CLIENT_ROLES, rule
        assert rule[4] == 'cert', rule

    # The dump reaches Postgres over the local socket, as the one role
    # admitted there.
    assert f'PG_ROLE={superuser}' in _delivered(ignition, DUMP_ENV_FILE).splitlines()


@needs_butane
def test_the_client_roles_hold_the_state_and_nothing_more(ignition: str) -> None:
    """Both client roles are non-superuser members of one owner, acting as it.

    Ownership is what the backend needs, not rows alone: it runs `CREATE
    INDEX IF NOT EXISTS` every time it opens, which Postgres answers only for
    the table's owner. Every session acting as the owner is what makes the
    table the backend creates, and whatever a restore loads, the owner's
    whichever client got there first. The grants are the rest of what the
    backend needs -- connecting, and creating in the schema its table lives
    in -- and they are held to that set, so a grant added later is a change
    this case names.
    """
    statements = _statements(_delivered(ignition, INIT_FILE))
    owners: set[str] = set()
    for role in CLIENT_ROLES:
        words = next(statement for statement in statements if statement.startswith(f'CREATE ROLE {role} ')).split()
        assert {'LOGIN', 'NOSUPERUSER'} <= set(words), words
        owner = words[words.index('IN') + 2]
        assert f'ALTER ROLE {role} IN DATABASE {settings.DATABASE} SET role = {owner}' in statements
        owners.add(owner)
    (owner,) = owners
    assert {'NOLOGIN', 'NOSUPERUSER'} <= set(
        next(statement for statement in statements if statement.startswith(f'CREATE ROLE {owner} ')).split()
    )
    assert not [statement for statement in statements if 'SUPERUSER' in statement.replace('NOSUPERUSER', '')]
    assert {statement for statement in statements if statement.startswith('GRANT ')} == {
        f'GRANT CONNECT ON DATABASE {settings.DATABASE} TO {owner}',
        f'GRANT USAGE, CREATE ON SCHEMA public TO {owner}',
    }
