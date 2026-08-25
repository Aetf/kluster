"""Derivation of every locally-generated secret from the derivation seed.

Storing a passphrase, a CA key, and a backup key each would turn the offline
kit into the token drawer it is not (docs/credentials.md §2.2). One 32-byte
derivation seed is stored instead and each secret is an HKDF-SHA256 derivation of it
under a stable label.

Every label here is consumed offline — at bring-up, at rotation, or while
provisioning the appliance. Secrets a running program generates live in Pulumi
state instead (credentials.md §1 rule 6), so no runtime process ever holds the
derivation seed.

Labels are API: changing one changes the secret it names, which for the age
identity means the dumps encrypted under the old one can no longer be opened.
Add labels, never edit them.
"""

from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import entries
from .kdbx import KdbxError, KdbxStore

#: Domain separation, so a derivation seed can never collide with another system's
#: use of the same HKDF construction.
_INFO_PREFIX = b'kluster/v1/'

_HASH = hashes.SHA256()
_HASH_LEN = _HASH.digest_size


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256, RFC 5869 — `cryptography`'s, checked against the RFC's vectors.

    Wrapped rather than called inline so the vector tests exercise the exact
    construction the derivations use. An empty salt is the RFC's absent salt,
    which is what `None` means here.
    """
    return HKDF(algorithm=_HASH, length=length, salt=salt or None, info=info).derive(ikm)


def derive(seed: bytes, label: str, length: int = 32) -> bytes:
    """A labelled secret from the derivation seed; empty salt, `seed` being uniform already."""
    if len(seed) < 32:
        raise ValueError('derivation seed must be at least 32 bytes')
    return hkdf(seed, b'', _INFO_PREFIX + label.encode(), length)


def _token(seed: bytes, label: str, length: int = 32) -> str:
    """A derived secret in a form every consumer accepts as a password."""
    return base64.urlsafe_b64encode(derive(seed, label, length)).decode().rstrip('=')


def pulumi_passphrase(seed: bytes) -> str:
    """`PULUMI_CONFIG_PASSPHRASE` for every stack."""
    return _token(seed, 'pulumi/passphrase')


def alertmanager_read_token(seed: bytes) -> str:
    """Bearer token the ops-repo poller presents at the header-match route."""
    return _token(seed, 'alertmanager/read')


def age_seed(seed: bytes, generation: int) -> bytes:
    """X25519 scalar for the pg_dump encryption identity of `generation`.

    A generation is a label, not a stored file — rotating is deriving the next.
    """
    return derive(seed, f'backup/age/{generation}')


def ca_scalar(seed: bytes) -> bytes:
    """P-256 private scalar for the state-backend CA.

    P-256 rather than RSA because a private scalar is a deterministic function
    of the seed; deterministic RSA generation is a footgun.
    """
    return derive(seed, 'state-backend/ca')


def cert_scalar(seed: bytes, name: str) -> bytes:
    """P-256 private scalar for one certificate under the CA (`ci`, `operator`, the server)."""
    return derive(seed, f'state-backend/cert/{name}')


#: Where the derivation seed lives in the offline store -- the register's
#: decision, not this module's (entries.py).
SEED_ENTRY = entries.SEEDS['derivation'].entry

SEED_LENGTH = 32


def generate_seed() -> bytes:
    return secrets.token_bytes(SEED_LENGTH)


def load_seed(store: KdbxStore, entry: str = SEED_ENTRY) -> bytes:
    seed = base64.b64decode(store.get(entry))
    if len(seed) < SEED_LENGTH:
        raise KdbxError(f'{entry!r} holds {len(seed)} bytes; a derivation seed is {SEED_LENGTH}')
    return seed


def store_seed(store: KdbxStore, seed: bytes, entry: str = SEED_ENTRY) -> None:
    store.put(entry, 'seed-seed', base64.b64encode(seed).decode())


def init_seed(store: KdbxStore, entry: str = SEED_ENTRY) -> None:
    """Create the derivation seed, refusing to overwrite an existing one.

    Overwriting would orphan every secret derived from the old seed —
    including the backups encrypted under it — so replacing a live seed is
    the rotation path (credentials.md §4.2), never this command.
    """
    try:
        _ = store.get(entry)
    except KdbxError:
        pass
    else:
        raise KdbxError(f'{entry!r} already holds a derivation seed; rotating one is `credentials rotate`')
    store_seed(store, generate_seed(), entry)
