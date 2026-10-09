"""The machine a scratch box is rendered from, and the render itself.

`state-backend render` builds the machine from the escrow (`config.machine`):
a server certificate the escrowed CA issues with its key beside it, and the
recipients its dumps are encrypted to, the drill's among them once its public
half is committed. The render is the package's alone, so it runs from an
installed copy with no checkout around it.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import drill_recipient_redirect
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from memory_kit import MemoryKit

from kluster.lib.state_backend import render, settings
from kluster.scripts.credentials import age, escrow, pki
from kluster.scripts.state_backend import config


def _pinned(*binaries: str) -> None:
    """Refuses a pinned tool that is missing, by name, rather than skipping the cases that need it."""
    for binary in binaries:
        if shutil.which(binary) is None:
            pytest.fail(f'{binary} is not on PATH: mise.toml pins it, so run the suite under `mise x`')


@pytest.fixture
def pinned_age() -> None:
    _pinned(age.BINARY, age.KEYGEN)


@pytest.fixture
def pinned_butane() -> None:
    _pinned('butane')


needs_age = pytest.mark.usefixtures('pinned_age')
needs_butane = pytest.mark.usefixtures('pinned_butane')

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


def test_the_certificate_the_box_gets_matches_the_key_it_gets(roots: config.Roots) -> None:
    # One issuance, both halves. Two calls would give the box a certificate
    # its private key does not answer for, and 5432 would never come up.
    from cryptography import x509

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


def _escrowed(vault: escrow.Vault) -> None:
    """The appliance's roots escrowed, as `credentials derived <row> generate` escrows each."""
    for label in (escrow.CA, *escrow.backup_labels()):
        _ = escrow.generate(vault, label)


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
    _escrowed(vault)
    drill = age.generate().public
    _ = drill_recipient_file.write_text(f'# the drill key, public half\n{drill}\n')

    recipients = config.age_recipients(vault)

    generations = tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())
    assert recipients == (*generations, drill)


@needs_age
def test_no_drill_recipient_on_file_is_the_generations_alone(vault: escrow.Vault, drill_recipient_file: Path) -> None:
    # Absent is a state and not a refusal: every render before the generator
    # has run would otherwise refuse.
    _escrowed(vault)
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


@pytest.fixture
def age_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where every argv `age` is started with during the case lands, one JSON line each.

    A stand-in `age` on PATH records its argv and hands over to the real one
    (the probe `tests/test_age.py` holds `age.check_recipient` to), so what a
    case checks is what any process on the machine could have read while the
    drill recipient was being checked. The file is absent until the tool is
    started.
    """
    real = shutil.which(age.BINARY)
    assert real is not None
    shim = tmp_path / 'bin' / age.BINARY
    shim.parent.mkdir()
    log = tmp_path / 'argv.jsonl'
    # A shell stub rather than a shebang naming the interpreter: the kernel
    # splits a shebang line at spaces and truncates it, and the path of the
    # interpreter running this suite is not ours to constrain.
    program = (
        'import json, os, sys\n'
        f'with open({str(log)!r}, "a") as f: f.write(json.dumps([{real!r}, *sys.argv[1:]]) + "\\n")\n'
        f'os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n'
    )
    _ = shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -IS -c {shlex.quote(program)} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv('PATH', f'{shim.parent}{os.pathsep}{os.environ["PATH"]}')
    return log


def _started(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


@needs_age
def test_a_drill_recipient_is_taken_on_the_tools_parse(drill_recipient_file: Path, age_argv: Path) -> None:
    # The generator's shape -- comment lines, then the recipient -- and the
    # pinned `age` asked about the line, on standard input rather than argv.
    public = age.generate().public
    _ = drill_recipient_file.write_text(f'# The drill age identity, public half.\n{public}\n')

    assert config.drill_recipient(drill_recipient_file) == public

    started = _started(age_argv)
    assert len(started) == 1
    assert public not in started[0]


def _mistyped(public: str) -> str:
    """`public` with its last six characters replaced: the prefix survives and the checksum does not."""
    return f'{public[:-6]}{"qqqqqq" if not public.endswith("qqqqqq") else "pppppp"}'


@needs_age
def test_a_drill_recipient_with_a_character_wrong_is_refused(drill_recipient_file: Path) -> None:
    # It still starts `age1`, and only its checksum says it names nobody: baked
    # into the Butane, every nightly dump would be encrypted to it.
    mistyped = _mistyped(age.generate().public)
    _ = drill_recipient_file.write_text(f'# The drill age identity, public half.\n{mistyped}\n')

    with pytest.raises(age.AgeError, match='not an age recipient') as refused:
        _ = config.drill_recipient(drill_recipient_file)

    assert mistyped not in str(refused.value)


def _pasted(secret: str, shape: str) -> str:
    """A private key in the shape it is pasted in: as `age-keygen` prints it, quoted, or lower-cased."""
    return {'bare': secret, 'quoted': f'"{secret}"', 'lower': secret.lower()}[shape]


@needs_age
@pytest.mark.parametrize('shape', ['bare', 'quoted', 'lower'])
def test_a_private_key_pasted_as_the_drill_recipient_is_refused_unseen(
    shape: str,
    drill_recipient_file: Path,
    age_argv: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The private half where the public one goes: refused, and never repeated.

    Committed, it would sit in the clear in a public repository and encrypt
    to nobody. The refusal names the file and not the line, and the line
    reaches no argv, because it is refused before the tool is started.
    """
    secret = age.generate().secret
    value = _pasted(secret, shape)
    _ = drill_recipient_file.write_text(f'# The drill age identity, public half.\n{value}\n')

    with caplog.at_level(logging.DEBUG), pytest.raises(age.AgeError, match='private half') as refused:
        _ = config.drill_recipient(drill_recipient_file)

    # Any ten characters of the key's body, not only its well-known prefix: a
    # refusal carrying the tail of the line would leak the key all the same.
    body = secret[len(age.SECRET_PREFIX) :].upper()
    pieces = [body[i : i + 10] for i in range(len(body) - 9)]
    captured = capsys.readouterr()
    for said in (str(refused.value), caplog.text, captured.out, captured.err):
        assert age.SECRET_PREFIX not in said.upper()
        assert not [piece for piece in pieces if piece in said.upper()]
    assert _started(age_argv) == []


def test_a_drill_recipient_with_no_tool_to_ask_says_where_the_tool_is_pinned(
    drill_recipient_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file on disk is a question for `age`, and a missing `age` is not a
    # wrong line: the operator is sent to the PATH rather than to the file.
    monkeypatch.setattr(age, 'BINARY', 'age-that-is-not-installed')
    _ = drill_recipient_file.write_text(f'{DRILL_RECIPIENT}\n')

    with pytest.raises(age.AgeMissing, match=re.escape('mise.toml')):
        _ = config.drill_recipient(drill_recipient_file)


@needs_age
def test_a_recipient_the_generator_does_not_draw_is_refused(drill_recipient_file: Path) -> None:
    # An SSH key is a recipient `age` takes, and not one the drill can open:
    # the drill's identity is an age key, so its public half is an `age1` one.
    ssh = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode()
    )
    _ = drill_recipient_file.write_text(f'{ssh}\n')

    with pytest.raises(age.AgeError, match='not a native `age1') as refused:
        _ = config.drill_recipient(drill_recipient_file)

    assert ssh not in str(refused.value)


@needs_age
def test_a_post_quantum_drill_recipient_is_refused(drill_recipient_file: Path) -> None:
    # `age1pq1…` parses, and passes a bare `age1` prefix test, but `age` will
    # not mix it with the escrowed generations' classic recipients: every
    # nightly dump would fail to encrypt. Native means one `1`, the separator.
    drawn = subprocess.run([age.KEYGEN, '-pq'], capture_output=True, text=True, check=True, timeout=age.TIMEOUT)
    secret = next(line for line in drawn.stdout.splitlines() if line.startswith(age.SECRET_STEM))
    pq = age.recipient(secret)
    assert pq.startswith(f'{age.PUBLIC_PREFIX}pq1')
    _ = drill_recipient_file.write_text(f'{pq}\n')

    with pytest.raises(age.AgeError, match='not a native `age1') as refused:
        _ = config.drill_recipient(drill_recipient_file)

    assert pq not in str(refused.value)


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
    what makes the drill key's adoption a replacement rather than a template
    change.
    """
    roots = config.Roots(
        ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT, 'age1second', DRILL_RECIPIENT)
    )
    built = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')

    ignition = render.render_ignition(built)

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
    return render.render_ignition(built)


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


# -- the render, from the package alone ----------------------------------------

#: What the installed copy is asked to do: build the machine from the
#: arguments it is handed, render it and take its bill of materials, and say where its code came
#: from and whether a checkout was there to be found. The arguments arrive on
#: standard input and the answer leaves on standard output, both as JSON.
INSTALLED_RENDER = """
import json, sys
from dataclasses import fields
from kluster.lib import workstation
from kluster.lib.state_backend import render

arguments = json.load(sys.stdin)
arguments['age_recipients'] = tuple(arguments['age_recipients'])
machine = render.machine(**arguments)
try:
    checkout = str(workstation.repo_root())
except workstation.WorkstationError:
    checkout = None
json.dump(
    {
        'module': render.__file__,
        'checkout': checkout,
        'machine': {spec.name: getattr(machine, spec.name) for spec in fields(machine)},
        'butane': render.butane(machine),
        'digests': render.bill_of_materials(machine),
    },
    sys.stdout,
)
"""


#: Bounds the wheel build, and the render from the unpacked wheel. The case
#: runs both in sequence, so the two together sit below the case's own bound,
#: which the case reads back.
BUILD_TIMEOUT = 30
RENDER_TIMEOUT = 20


def test_the_package_renders_from_an_installed_copy_with_no_checkout_around_it(
    roots: config.Roots, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The machine's files travel inside the package, so a render needs no checkout.

    The wheel is built from this tree and unpacked where no `mise.toml` sits
    above it, and a process whose import path starts there builds the machine
    from the keys and recipients the render takes as arguments -- reading the
    operator keys and the dump script out of the package -- and renders it and
    takes its bill of materials. The machine, the Butane document and the bill
    of materials all equal
    the checkout's. A render that reached for the checkout -- a path found
    from `repo_root`, a file read from outside the package -- fails here,
    because there is none to find.

    The build runs offline: its backend comes from the cache the
    environment's sync filled, so an unreachable index, or a backend
    released since, changes nothing here.
    """
    bound = float(cast(str, request.config.getoption('timeout', None) or request.config.getini('timeout')))
    assert bound > BUILD_TIMEOUT + RENDER_TIMEOUT
    if any((level / 'mise.toml').is_file() for level in tmp_path.parents):
        pytest.skip('the temporary directory is itself inside a checkout')
    uv = os.environ.get('UV') or shutil.which('uv')
    assert uv is not None, 'uv builds the wheel, and it is not on PATH (mise x uv -- ...)'
    root = Path(__file__).parent.parent
    dist = tmp_path / 'dist'
    built = subprocess.run(
        [uv, 'build', '--wheel', '--out-dir', str(dist), str(root)],
        capture_output=True,
        text=True,
        env={**os.environ, 'UV_OFFLINE': '1'},
        timeout=BUILD_TIMEOUT,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    (wheel,) = dist.glob('*.whl')
    site = tmp_path / 'site'
    shutil.unpack_archive(wheel, site, format='zip')

    # What the script mints and recovers, and passes in.
    minted = config.machine(roots, address=ADDRESS, dump_key_id='key-id', dump_key='secret', bucket_id='bucket')
    arguments: dict[str, Any] = {
        'ca_cert': minted.ca_cert,
        'server_cert': minted.server_cert,
        'server_key': minted.server_key,
        'ssh_host_key': minted.ssh_host_key,
        'age_recipients': minted.age_recipients,
        'dump_key_id': minted.b2_dump_key_id,
        'dump_key': minted.b2_dump_key,
        'bucket_id': minted.b2_bucket_id,
    }
    machine = render.machine(**arguments)
    rendered = subprocess.run(
        [sys.executable, '-c', INSTALLED_RENDER],
        input=json.dumps(arguments),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(site)},
        timeout=RENDER_TIMEOUT,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    answer = json.loads(rendered.stdout)

    assert Path(answer['module']).is_relative_to(site)
    assert answer['checkout'] is None
    expected = {spec.name: getattr(machine, spec.name) for spec in fields(machine)}
    assert answer['machine'] == json.loads(json.dumps(expected))
    assert answer['butane'] == render.butane(machine)
    assert answer['digests'] == render.bill_of_materials(machine)
