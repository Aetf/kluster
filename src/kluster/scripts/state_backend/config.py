"""What a scratch box is built from that only the escrow can produce, and the files a workstation writes for the appliance.

The machine itself -- the values and the Butane template they are rendered
into -- is `kluster.lib.state_backend.render`, which takes every key and
recipient as an argument. The `state-backend` stack renders the appliance from
the stable keys in its configuration; this module mints and recovers them for
`state-backend render`, which renders a scratch box for the rebuild drill
(physical/state-backend.md §7.3.1): a server certificate the escrowed CA signs
and an SSH identity, minted per render, and the age recipients whose
identities the escrow holds, with the drill recipient committed beside the
template. It also writes what a workstation keeps for the appliance -- the
client bundle and the host-key pin -- and reads the escrowed identities a
restore decrypts with.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kluster.lib import config as lib_config
from kluster.lib import workstation
from kluster.lib.bundle import CA_FILE, CERT_FILE, KEY_FILE, URL_FILE
from kluster.lib.state_backend import render, settings
from kluster.scripts.credentials import age, escrow, pki

log = logging.getLogger(__name__)

#: The public half of the drill age identity (credentials.md §3), one line,
#: `#` comments allowed. Written by `credentials derived drill-age-identity
#: generate` and committed; the private half is in the ops repository's
#: `drill` Environment and nowhere on disk. Absent until that generator has
#: run: the appliance then encrypts to the escrowed generations alone.
DRILL_RECIPIENT = 'drill-recipient.txt'
#: Where the drill recipient is read: beside the Butane template, in the
#: package this runs from. In a checkout that is the committed file; its
#: writer holds it to that (`drill_recipient_target`).
DRILL_RECIPIENT_FILE = Path(render.__file__).with_name(render.MACHINE) / DRILL_RECIPIENT

#: The tool's own host-key pin, beside the bundle for the same box. Its own
#: file rather than the operator's `~/.ssh/known_hosts`: nothing but
#: `state-backend ssh` reads it, and nothing else may write it.
KNOWN_HOSTS_FILE = 'known_hosts'

#: How much life the server certificate must have left for `state-backend
#: probe` to stay quiet (physical/state-backend.md §6), and the only home of
#: that number. Held below `settings.RENEWAL_MARGIN` by a test: the probe is
#: the backstop for an appliance whose stack nobody has run, or whose reports
#: nobody acted on, and a backstop that fires before the margin opens is the
#: first reporter rather than the last. The gap between the two is the time a
#: reported expiry has to be acted on before it is alerted as well.
EXPIRY_ALERT_MARGIN = dt.timedelta(days=30)


def drill_recipient(path: Path) -> str | None:
    """The drill key's public half as `path` holds it, or None while no such file exists.

    Absent is a state rather than a refusal: the file appears when the
    generator first runs, and every render before that would otherwise
    refuse. A file that is there is held to what the generator writes -- one
    recipient, because the drill key has one slot and no generational pair
    (physical/state-backend.md §5) -- and an empty one is refused the way
    `operator-keys.txt` is, since a blank where a recipient should be is a
    dump the drill cannot open.

    **The recipient is what the pinned `age` parses as one**
    (`age.check_recipient`), the check `escrow/RECIPIENTS` is held to: a
    hand edit or a truncated write still starts `age1`, and whatever this
    returns is baked into the Butane and encrypted to by every nightly dump.
    No refusal repeats the line, which is not known to be public until the
    tool has taken it -- the likeliest wrong line is the private half pasted
    where the public one goes.

    Then a native X25519 recipient, which is what `age-keygen` draws for the
    drill: `age1` and no second `1`, since neither `age` nor bech32's data
    alphabet holds one. Every other kind parses and breaks something. An SSH
    key opens a dump only for an SSH identity the drill does not have. A
    plugin recipient (`age1<plugin>1…`) parses wherever its plugin is on the
    operator's PATH, and the box installs `age` alone. A post-quantum one
    (`age1pq1…`) cannot be mixed with the escrowed generations' classic
    recipients, so `age` would refuse every nightly dump.
    """
    if not path.is_file():
        log.info('no drill recipient on file at %s; dumps encrypt to the escrowed generations alone', path)
        return None
    try:
        found = lib_config.lines(path, 'the drill age recipient')
    except ValueError as exc:
        raise age.AgeError(str(exc)) from exc
    if len(found) != 1:
        raise age.AgeError(
            f'{path} holds {len(found)} recipients, and the drill key has one slot: '
            '`credentials derived drill-age-identity generate --rotate` is what replaces it'
        )
    (value,) = found
    age.check_recipient(value, name=f'the line in {path}')
    if not (value.startswith(age.PUBLIC_PREFIX) and value.count('1') == 1):
        raise age.AgeError(
            f'the line in {path} is an age recipient but not a native `{age.PUBLIC_PREFIX}…` one, '
            f'which is what `{age.KEYGEN}` draws for the drill'
        )
    return value


def drill_recipient_target() -> Path:
    """Where the drill recipient's writer puts it: `DRILL_RECIPIENT_FILE`, refused outside a checkout.

    The file is one to commit, and the appliance encrypts to the committed
    recipient alone. A package running from anywhere but the checkout it
    belongs to -- installed rather than editable -- resolves the path inside
    that installation, where no commit picks the file up. Its writer pushes
    the private half before it writes the public one, so a run there would
    replace the key the drill holds and leave the appliance encrypting to the
    old recipient. So the target is checked against the checkout this package
    runs from (`workstation.repo_root`), before anything is pushed, and
    refused by name when it is not in it.
    """
    root = workstation.repo_root()
    if not DRILL_RECIPIENT_FILE.resolve().is_relative_to(root.resolve()):
        raise workstation.WorkstationError(
            f'{DRILL_RECIPIENT_FILE} is not in the checkout at {root}: the drill recipient is a file to commit, '
            'so it is written only by a package running from the checkout it belongs to'
        )
    return DRILL_RECIPIENT_FILE


def age_recipients(vault: escrow.Vault) -> tuple[str, ...]:
    """The public halves of every identity the appliance encrypts dumps to.

    The scratch box's recipient list, read from the escrow where the
    appliance's is read from the committed recipients file
    (`committed.age_recipients`): the scratch box is the drill's, rendered with
    the kit in hand.

    The escrowed generations first, then the drill recipient where one is on
    file (`DRILL_RECIPIENT_FILE`). The drill key is not a root the escrow
    holds -- it opens nothing an escrowed generation does not also open -- so
    it is read from the committed file rather than recovered.
    """
    generations = tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())
    drill = drill_recipient(DRILL_RECIPIENT_FILE)
    return generations if drill is None else (*generations, drill)


def backup_identities(vault: escrow.Vault) -> list[str]:
    """The private halves, for opening a dump — the other direction of the same list.

    All of them at once, newest first, because which generation a given
    object was written under is not a fact the object carries (§5): any dump
    still in retention opens with one of these.
    """
    return [vault.recover(label) for label in escrow.backup_labels()]


@dataclass(frozen=True, eq=False)
class Roots:
    """What a scratch box is built from that only the escrow can produce.

    Recovered once and passed down. The age recipients are public halves: the
    identities themselves stay in the escrow -- or, for the drill key, in the
    ops repository's `drill` Environment -- and the box never holds one.
    """

    ca: pki.Authority
    age_recipients: tuple[str, ...]

    @classmethod
    def recover(cls, vault: escrow.Vault) -> Roots:
        return cls(ca=pki.Authority.from_pem(vault.recover(escrow.CA)), age_recipients=age_recipients(vault))


@dataclass(frozen=True)
class ClientBundle:
    """What an operator or CI needs to reach the backend.

    The certificates print and the private key does not. A certificate is
    public by construction — it is what the box is shown — while the key is
    what authenticates as this bundle's role for as long as the leaf is valid.
    """

    name: str
    address: str
    ca_cert: bytes
    cert: bytes
    key: bytes = field(repr=False, compare=False)

    def url(self) -> str:
        """The connection string: everything about the backend, nothing about this machine.

        The three certificate files travel beside it as `PGSSLROOTCERT`,
        `PGSSLCERT` and `PGSSLKEY` (`kluster.lib.bundle.ssl_env`) rather than inside it. libpq
        expands no variable inside a connection string, and neither does the
        driver Pulumi's Postgres backend uses — but both read those variables,
        so the channel that carries a path is the environment. A string with
        the paths in it would be a string that only one checkout on one
        machine can use, and moving that checkout would silently invalidate
        the copy recorded beside the bundle.

        `verify-full` against a literal IP: the state backend's hot path must
        not depend on DNS, which is itself something this backend deploys.
        """
        return f'postgres://{self.name}@{self.address}:{settings.PORT}/{settings.DATABASE}?sslmode=verify-full'


def machine(
    roots: Roots,
    *,
    address: str,
    dump_key_id: str,
    dump_key: str,
    bucket_id: str,
    now: dt.datetime | None = None,
) -> render.Machine:
    """The machine this commit describes, at this address, with this dump key: a scratch box.

    The keys it carries are minted here and handed to the render: a server
    certificate issued for `address`, and a fresh SSH host key. `now` is the
    instant the server certificate is issued at, and it goes to `pki`
    untouched: the default is `pki`'s, in one place, so a test can pin the
    certificate's validity.
    """
    # One issuance, both halves. A leaf key is random at issuance (pki.py), so
    # asking the CA twice would hand the box a certificate its key does not
    # match -- and a box whose TLS key is wrong answers nothing.
    server = roots.ca.issue_server(address, now=now)
    # The box's SSH identity is minted here rather than generated on the box,
    # so its fingerprint is in the console banner the render writes. It is this
    # render's alone: the public half is derived from the machine
    # (`render.host_public_key`), so it can never describe a key the box was
    # not given.
    host_key = Ed25519PrivateKey.generate()
    return render.machine(
        ca_cert=roots.ca.certificate().cert_pem.decode().strip(),
        server_cert=server.cert_pem.decode().strip(),
        server_key=server.key_pem.decode().strip(),
        ssh_host_key=host_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
        .strip(),
        age_recipients=roots.age_recipients,
        dump_key_id=dump_key_id,
        dump_key=dump_key,
        bucket_id=bucket_id,
    )


def client_bundle(authority: pki.Authority, *, name: str, address: str) -> ClientBundle:
    """The `ci` or `operator` credential, and the string for reaching the box with it.

    Every call issues a fresh key: a client certificate is re-issuable from
    the CA and escrowed nowhere, so writing a bundle is minting one rather
    than reproducing one. The box needs no notice — it authenticates the CA,
    not a particular leaf.
    """
    credential = authority.issue_client(name)
    return ClientBundle(
        name=name,
        address=address,
        ca_cert=authority.certificate().cert_pem,
        cert=credential.cert_pem,
        key=credential.key_pem,
    )


def write_client_bundle(bundle: ClientBundle, directory: Path) -> None:
    """Place a bundle on disk with the permissions libpq insists on.

    libpq refuses a client key that anyone but its owner can read, so that one
    is `0600`; the directory is `0700` for the same reason one level up, which
    is what `workstation.secret_dir` gives every slot.

    The URL lands here too, because a bundle is only usable with the string
    that names the backend it authenticates against — and because
    `mise.toml` reads this file to put `PULUMI_BACKEND_URL` in the
    environment. It says nothing about this directory: what does is the three
    `PGSSL*` variables the same file derives from where the bundle was found.
    """
    _ = workstation.secret_dir(directory)
    _ = (directory / CA_FILE).write_bytes(bundle.ca_cert)
    _ = (directory / CERT_FILE).write_bytes(bundle.cert)
    _ = workstation.write(directory / KEY_FILE, bundle.key.decode())
    _ = (directory / URL_FILE).write_text(bundle.url() + '\n')
    log.info('wrote client bundle to %s', directory)


def write_known_hosts(directory: Path, *, address: str, public_key: str) -> Path:
    """Place the box's host-key pin where `state-backend ssh` reads it.

    One line -- the address the client dials, then the key it must answer
    with -- in the slot that already holds the client bundle for this box.
    The file is the tool's own: `ssh` is pointed at it and at nothing else,
    so the operator's `~/.ssh/known_hosts` is neither read nor written, and
    an entry some earlier bare login left there decides nothing.

    Rewritten whole rather than appended to. The address is reserved and the
    box is cattle, so exactly one identity is ever correct for it, and a
    second line would be a second answer this file accepts.
    """
    _ = workstation.secret_dir(directory)
    path = directory / KNOWN_HOSTS_FILE
    _ = path.write_text(f'{address} {public_key}\n')
    return path
