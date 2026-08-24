"""Derivation of every locally-generated secret from the root seed.

Storing a passphrase, a repo password, and a backup key each would turn the
offline kit into the token drawer it is not (docs/credentials.md §2.2). One
32-byte root seed is stored instead and each secret is an HKDF-SHA256
derivation of it under a stable label — so a volume's restic password is a
function of the volume's identity, and a restore re-derives it instead of
looking it up.

Labels are API: changing one changes the secret it names, which for a backup
password means the old repository can no longer be opened. Add labels, never
edit them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from .kdbx import KdbxError, KdbxStore

#: Domain separation, so a root seed can never collide with another system's
#: use of the same HKDF construction.
_INFO_PREFIX = b'kluster/v1/'

_HASH = hashlib.sha256
_HASH_LEN = 32


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256, RFC 5869 — extract then expand."""
    if length > 255 * _HASH_LEN:
        raise ValueError(f'cannot derive more than {255 * _HASH_LEN} bytes')

    prk = hmac.new(salt or bytes(_HASH_LEN), ikm, _HASH).digest()
    okm = b''
    block = b''
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), _HASH).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive(root: bytes, label: str, length: int = 32) -> bytes:
    """A labelled secret from the root seed; empty salt, `root` being uniform already."""
    if len(root) < 32:
        raise ValueError('root seed must be at least 32 bytes')
    return hkdf(root, b'', _INFO_PREFIX + label.encode(), length)


def _token(root: bytes, label: str, length: int = 32) -> str:
    """A derived secret in a form every consumer accepts as a password."""
    return base64.urlsafe_b64encode(derive(root, label, length)).decode().rstrip('=')


def pulumi_passphrase(root: bytes) -> str:
    """`PULUMI_CONFIG_PASSPHRASE` for every stack."""
    return _token(root, 'pulumi/passphrase')


def restic_password(root: bytes, namespace: str, pvc: str) -> str:
    """The restic repository password for one backed volume.

    Pure in the volume's identity: `backed_pvc` invents nothing, and a restore
    needs only the root seed to open any repository.
    """
    return _token(root, f'restic/{namespace}/{pvc}')


def alertmanager_read_token(root: bytes) -> str:
    """Bearer token the ops-repo poller presents at the header-match route."""
    return _token(root, 'alertmanager/read')


def age_seed(root: bytes, generation: int) -> bytes:
    """X25519 scalar for the pg_dump encryption identity of `generation`.

    A generation is a label, not a stored file — rotating is deriving the next.
    """
    return derive(root, f'backup/age/{generation}')


def ca_scalar(root: bytes) -> bytes:
    """P-256 private scalar for the state-backend CA.

    P-256 rather than RSA because a private scalar is a deterministic function
    of the seed; deterministic RSA generation is a footgun.
    """
    return derive(root, 'state-backend/ca')


def cert_scalar(root: bytes, name: str) -> bytes:
    """P-256 private scalar for one certificate under the CA (`ci`, `operator`, the server)."""
    return derive(root, f'state-backend/cert/{name}')


#: Where the root seed lives in the offline store.
ROOT_ENTRY = 'seeds/Root seed'

ROOT_LENGTH = 32


def generate_root() -> bytes:
    return secrets.token_bytes(ROOT_LENGTH)


def load_root(store: KdbxStore, entry: str = ROOT_ENTRY) -> bytes:
    root = base64.b64decode(store.get(entry))
    if len(root) < ROOT_LENGTH:
        raise KdbxError(f'{entry!r} holds {len(root)} bytes; a root seed is {ROOT_LENGTH}')
    return root


def store_root(store: KdbxStore, root: bytes, entry: str = ROOT_ENTRY) -> None:
    store.put(entry, 'root-seed', base64.b64encode(root).decode())


def init_root(store: KdbxStore, entry: str = ROOT_ENTRY) -> None:
    """Create the root seed, refusing to overwrite an existing one.

    Overwriting would orphan every secret derived from the old seed —
    including the backups encrypted under it — so replacing a live root is
    the rotation path (credentials.md §4.2), never this command.
    """
    try:
        _ = store.get(entry)
    except KdbxError:
        pass
    else:
        raise KdbxError(f'{entry!r} already holds a root seed; rotating one is `credentials rotate`')
    store_root(store, generate_root(), entry)
