"""age identities derived from the root seed.

The state-backend's dumps are age-encrypted (physical/state-backend.md §5) to
a pair of generations plus the drill key. A generation is a *label*, not a
stored file: its identity is `backup/age/<generation>` derived from the root
seed, so rotating means deriving the next and re-provisioning — nothing to
escrow, nothing to lose.

age's X25519 identity is exactly a 32-byte scalar in Bech32 clothing, which is
what makes the derivation possible: the derived bytes *are* the key.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from . import seeds

_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'

_SECRET_HRP = 'age-secret-key-'
_PUBLIC_HRP = 'age'


def _polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i, g in enumerate(generator):
            if (top >> i) & 1:
                chk ^= g
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: bytes, frombits: int, tobits: int) -> list[int]:
    acc = 0
    bits = 0
    out: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits:
        out.append((acc << (tobits - bits)) & maxv)
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    """Bech32 (BIP-173) — age's encoding for both halves of an identity."""
    values = _convert_bits(data, 8, 5)
    checksum_input = _hrp_expand(hrp) + values + [0, 0, 0, 0, 0, 0]
    polymod = _polymod(checksum_input) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + '1' + ''.join(_CHARSET[d] for d in values + checksum)


@dataclass(frozen=True)
class Identity:
    """One age key pair. The secret is uppercase, as age writes it."""

    secret: str
    public: str


def identity_from_scalar(scalar: bytes) -> Identity:
    if len(scalar) != 32:
        raise ValueError('an X25519 scalar is 32 bytes')
    public = X25519PrivateKey.from_private_bytes(scalar).public_key().public_bytes_raw()
    return Identity(
        secret=bech32_encode(_SECRET_HRP, scalar).upper(),
        public=bech32_encode(_PUBLIC_HRP, public),
    )


def generation(root: bytes, number: int) -> Identity:
    """The identity of backup key generation `number`."""
    return identity_from_scalar(seeds.age_seed(root, number))
